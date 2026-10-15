"""Renderers: pure, deterministic, serializer-only output; golden files for the demo spec; strict identifiers.

Regenerate the golden files after an intentional change with:
    FORGE_UPDATE_GOLDEN=1 uv run pytest tests/test_render.py
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from forge.models.appspec import AppSpecDraft, PathParam, SpecElement, SpecEntity, SpecField, SpecOperation, SpecScreen
from forge.models.base import NameValue
from forge.stages.generate.codegen import render_check
from forge.stages.generate.manifest import EDITABLE_GLOBS, glob_regex, is_editable
from forge.stages.specify.render import RenderError, render, render_with_warnings, stub_operations, validate

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "issue-tracker"
UPDATE = os.environ.get("FORGE_UPDATE_GOLDEN") == "1"


def demo_spec() -> AppSpecDraft:
    return AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())


def mutate(spec: AppSpecDraft, fn) -> AppSpecDraft:
    data = spec.model_dump(mode="json")
    fn(data)
    return AppSpecDraft.model_validate(data)


def find(items: list[dict], **match) -> dict:
    return next(i for i in items if all(i.get(k) == v for k, v in match.items()))


# ---------------------------------------------------------------- a broader spec
def _f(entity, name, type, *, required=False, enum=(), ref=None, default=None, generated="none", unique=False):
    return SpecField(entity=entity, name=name, type=type, enum_values=list(enum), references=ref, required=required,
                     unique=unique, default=default, generated=generated, basis="observed", evidence=["test"])


def _op(id, entity, kind, method, path, params=(), fields=(), response="object", list_key=None, status=200, sets=(),
        note=None):
    return SpecOperation(id=id, entity=entity, kind=kind, method=method, path=path,
                         path_params=[PathParam(name=n, entity=e) for n, e in params], request_fields=list(fields),
                         response=response, list_key=list_key, success_status=status,
                         action_sets=[NameValue(name=k, value=v) for k, v in sets], action_note=note,
                         source_method=method, source_path=path, basis="observed", evidence=["test"])


def _el(id, screen, role, name, purpose, *, parent=None, op=None, field=None, target=None, order=0):
    return SpecElement(id=id, screen=screen, parent=parent, role=role, name=name, purpose=purpose, operation=op,
                       field=field, target_screen=target, order=order, evidence=["test"])


def wiki_spec() -> AppSpecDraft:
    """Teams (uuid ids), members (self-referencing mentor) and pages (two references to members)."""
    T, M, P = "team", "member", "page"
    entities = [SpecEntity(name=n, label=n.title(), plural_label=n.title() + "s", id_type=t, owned_by_user=False,
                           basis="observed", evidence=["test"])
                for n, t in ((T, "uuid"), (M, "integer"), (P, "integer"))]
    fields = [
        _f(T, "id", "string", required=True, generated="id"),
        _f(T, "name", "string", required=True, unique=True),
        _f(T, "created_at", "datetime", required=True, generated="created_at"),
        _f(M, "id", "integer", required=True, generated="id"),
        _f(M, "team_id", "reference", required=True, ref=T),
        _f(M, "name", "string", required=True),
        _f(M, "email", "string", unique=True),
        _f(M, "mentor_id", "reference", ref=M),
        _f(M, "active", "boolean", required=True, default="true"),
        _f(M, "joined_on", "date"),
        _f(M, "score", "number"),
        _f(M, "level", "integer", default="1"),
        _f(M, "created_at", "datetime", required=True, generated="created_at"),
        _f(M, "updated_at", "datetime", required=True, generated="updated_at"),
        _f(P, "id", "integer", required=True, generated="id"),
        _f(P, "team_id", "reference", required=True, ref=T),
        _f(P, "author_id", "reference", required=True, ref=M),
        _f(P, "reviewer_id", "reference", ref=M),
        _f(P, "title", "string", required=True),
        _f(P, "body", "text"),
        _f(P, "state", "enum", required=True, enum=("draft", "review", "published"), default="draft"),
        _f(P, "due_at", "datetime"),
        _f(P, "wordCount", "integer"),
        _f(P, "created_at", "datetime", required=True, generated="created_at"),
        _f(P, "updated_at", "datetime", required=True, generated="updated_at"),
    ]
    tid, mid, pid = [("team_id", T)], [("id", M)], [("page_id", P)]
    member_fields = ["name", "email", "mentor_id", "active", "joined_on", "score", "level"]
    operations = [
        _op("listTeams", T, "list", "GET", "/api/teams", response="list"),
        _op("createTeam", T, "create", "POST", "/api/teams", fields=["name"], status=201),
        _op("getTeam", T, "get", "GET", "/api/teams/{team_id}", tid),
        _op("deleteTeam", T, "delete", "DELETE", "/api/teams/{team_id}", tid, response="empty", status=204),
        _op("listMembers", M, "list", "GET", "/api/teams/{team_id}/members", tid, response="list_envelope",
            list_key="members"),
        _op("createMember", M, "create", "POST", "/api/teams/{team_id}/members", tid, member_fields, status=201),
        _op("getMember", M, "get", "GET", "/api/members/{id}", mid),
        _op("updateMember", M, "update", "PUT", "/api/members/{id}", mid, member_fields),
        _op("deleteMember", M, "delete", "DELETE", "/api/members/{id}", mid, response="empty", status=204),
        _op("listPages", P, "list", "GET", "/api/teams/{team_id}/pages", tid, response="list_envelope",
            list_key="data"),
        _op("createPage", P, "create", "POST", "/api/teams/{team_id}/pages", tid,
            ["title", "body", "author_id", "reviewer_id", "state", "due_at", "wordCount"], status=201),
        _op("getPage", P, "get", "GET", "/api/pages/{page_id}", pid),
        _op("updatePage", P, "update", "PATCH", "/api/pages/{page_id}", pid,
            ["title", "body", "state", "reviewer_id", "due_at"]),
        _op("publishPage", P, "action", "POST", "/api/pages/{page_id}/publish", pid, sets=[("state", "published")]),
        _op("archivePage", P, "action", "POST", "/api/pages/{page_id}/archive", pid,
            note='Moves the page to the archive */ and emails the author.\n"quoted"'),
        _op("deletePage", P, "delete", "DELETE", "/api/pages/{page_id}", pid, response="empty", status=204),
    ]
    screens = [
        SpecScreen(id="teams", route="/", title="Teams", entity=T, kind="list", parent=None, evidence=["test"]),
        SpecScreen(id="team", route="/teams/:team_id", title="Team", entity=T, kind="detail", parent="teams",
                   evidence=["test"]),
        SpecScreen(id="member", route="/members/:id", title="Member", entity=M, kind="detail", parent="team",
                   evidence=["test"]),
        SpecScreen(id="page", route="/pages/:page_id", title="Page", entity=P, kind="detail", parent="team",
                   evidence=["test"]),
        SpecScreen(id="new-page", route="/teams/:team_id/pages/new", title="New page", entity=P, kind="form",
                   parent="team", evidence=["test"]),
    ]
    elements = [
        _el("teams-h", "teams", "heading", "Teams", "heading", order=1),
        _el("add-team", "teams", "form", "Add team", "container", op="createTeam", order=2),
        _el("add-team-name", "teams", "textbox", "Team name", "field", parent="add-team", field="name", order=3),
        _el("add-team-err", "teams", "alert", "", "status_message", parent="add-team", order=4),
        _el("add-team-go", "teams", "button", "Add", "submit_create", parent="add-team", op="createTeam", order=5),
        _el("teams-grid", "teams", "grid", "All teams", "list", op="listTeams", order=6),
        _el("teams-row", "teams", "row", "", "list_row", parent="teams-grid", order=7),
        _el("teams-row-link", "teams", "link", "", "navigate", parent="teams-row", field="name", target="team",
            order=8),
        _el("teams-row-created", "teams", "gridcell", "", "text", parent="teams-row", field="created_at", order=9),
        _el("teams-row-del", "teams", "button", "Remove", "delete", parent="teams-row", op="deleteTeam", order=10),
        _el("teams-empty", "teams", "paragraph", "No teams yet", "text", order=11),

        _el("team-back", "team", "link", "Teams", "navigate", target="teams", order=1),
        _el("team-h", "team", "heading", "", "heading", field="name", order=2),
        _el("add-member", "team", "button", "Add member", "open_create", op="createMember", order=3),
        _el("add-member-dlg", "team", "dialog", "Add member", "container", parent="add-member", order=4),
        _el("am-name", "team", "textbox", "Name", "field", parent="add-member-dlg", field="name", order=5),
        _el("am-email", "team", "textbox", "Email", "field", parent="add-member-dlg", field="email", order=6),
        _el("am-mentor", "team", "combobox", "Mentor", "field", parent="add-member-dlg", field="mentor_id", order=7),
        _el("am-active", "team", "checkbox", "Active", "field", parent="add-member-dlg", field="active", order=8),
        _el("am-joined", "team", "textbox", "Joined", "field", parent="add-member-dlg", field="joined_on", order=9),
        _el("am-score", "team", "spinbutton", "Score", "field", parent="add-member-dlg", field="score", order=10),
        _el("am-level", "team", "spinbutton", "Level", "field", parent="add-member-dlg", field="level", order=11),
        _el("am-err", "team", "alert", "", "status_message", parent="add-member-dlg", order=12),
        _el("am-save", "team", "button", "Save member", "submit_create", parent="add-member-dlg",
            op="createMember", order=13),
        _el("am-cancel", "team", "button", "Close", "cancel", parent="add-member-dlg", order=14),
        _el("members", "team", "list", "Members", "list", op="listMembers", order=15),
        _el("members-row", "team", "listitem", "", "list_row", parent="members", order=16),
        _el("members-row-link", "team", "link", "", "navigate", parent="members-row", field="name", target="member",
            order=17),
        _el("members-row-email", "team", "generic", "", "text", parent="members-row", field="email", order=18),
        _el("members-row-del", "team", "button", "Remove member", "delete", parent="members-row",
            op="deleteMember", order=19),
        _el("write-page", "team", "button", "Write page", "open_create", target="new-page", order=20),
        _el("pages", "team", "table", "Pages", "list", op="listPages", order=21),
        _el("pages-row", "team", "row", "", "list_row", parent="pages", order=22),
        _el("pages-row-link", "team", "link", "", "navigate", parent="pages-row", field="title", target="page",
            order=23),
        _el("pages-row-state", "team", "cell", "", "text", parent="pages-row", field="state", order=24),
        _el("pages-row-publish", "team", "button", "Publish", "action", parent="pages-row", op="publishPage",
            order=25),
        _el("pages-row-archive", "team", "button", "Archive", "action", parent="pages-row", op="archivePage",
            order=26),
        _el("pages-row-del", "team", "button", "Delete", "delete", parent="pages-row", op="deletePage", order=27),
        _el("delete-team", "team", "button", "Delete team", "delete", op="deleteTeam", order=28),

        _el("member-back", "member", "link", "Back", "navigate", target="team", order=1),
        _el("member-h", "member", "heading", "", "heading", field="name", order=2),
        _el("member-email", "member", "paragraph", "", "text", field="email", order=3),
        _el("member-form", "member", "form", "Member details", "container", op="updateMember", order=4),
        _el("mf-name", "member", "textbox", "Name", "field", parent="member-form", field="name", order=5),
        _el("mf-active", "member", "checkbox", "Active", "field", parent="member-form", field="active", order=6),
        _el("mf-level", "member", "spinbutton", "Level", "field", parent="member-form", field="level", order=7),
        _el("mf-saved", "member", "status", "Member saved", "status_message", parent="member-form", order=8),
        _el("mf-go", "member", "button", "Update", "submit_update", parent="member-form", op="updateMember", order=9),

        _el("page-h", "page", "heading", "", "heading", field="title", order=1),
        _el("page-form", "page", "form", "Page", "container", op="updatePage", order=2),
        _el("pf-title", "page", "textbox", "Title", "field", parent="page-form", field="title", order=3),
        _el("pf-body", "page", "textbox", "Body", "field", parent="page-form", field="body", order=4),
        _el("pf-state", "page", "combobox", "State", "field", parent="page-form", field="state", order=5),
        _el("pf-reviewer", "page", "combobox", "Reviewer", "field", parent="page-form", field="reviewer_id", order=6),
        _el("pf-due", "page", "textbox", "Due", "field", parent="page-form", field="due_at", order=7),
        _el("pf-err", "page", "alert", "", "status_message", parent="page-form", order=8),
        _el("pf-go", "page", "button", "Save page", "submit_update", parent="page-form", op="updatePage", order=9),
        _el("page-saved", "page", "status", "Page saved", "status_message", order=10),
        _el("page-publish", "page", "button", "Publish", "action", op="publishPage", order=11),
        _el("page-delete", "page", "button", "Delete page", "delete", op="deletePage", target="team", order=12),

        _el("np-h", "new-page", "heading", "New page", "heading", order=1),
        _el("np-form", "new-page", "form", "New page", "container", op="createPage", order=2),
        _el("np-title", "new-page", "textbox", "Title", "field", parent="np-form", field="title", order=3),
        _el("np-body", "new-page", "textbox", "Body", "field", parent="np-form", field="body", order=4),
        _el("np-author", "new-page", "combobox", "Author", "field", parent="np-form", field="author_id", order=5),
        _el("np-state", "new-page", "combobox", "State", "field", parent="np-form", field="state", order=6),
        _el("np-err", "new-page", "alert", "", "status_message", parent="np-form", order=7),
        _el("np-go", "new-page", "button", "Create page", "submit_create", parent="np-form", op="createPage",
            target="page", order=8),
    ]
    return AppSpecDraft(app_name='Team "Wiki" <beta>', auth="none", entities=entities, fields=fields,
                        operations=operations, screens=screens, elements=elements, scenarios=[], pilot_scenario="",
                        assumptions=[], unsupported=[])


# ---------------------------------------------------------------- determinism and golden files
def test_render_is_deterministic():
    first = render(demo_spec())
    second = render(AppSpecDraft.model_validate(json.loads(demo_spec().model_dump_json())))
    assert first == second
    assert render(wiki_spec()) == render(wiki_spec())


def test_rendered_paths():
    files = render(demo_spec())
    assert sorted(files) == [
        "api/src/generated/routes.ts",
        "app-spec.json",
        "contract/openapi.yaml",
        "db/migrations/migration_lock.toml",
        "db/schema.prisma",
        "web/index.html",
        "web/src/generated/contract.ts",
        "web/src/generated/routes.tsx",
        "web/src/generated/screens/Issue.tsx",
        "web/src/generated/screens/Project.tsx",
        "web/src/generated/screens/Projects.tsx",
    ]
    assert json.loads(files["app-spec.json"]) == demo_spec().model_dump(mode="json")
    assert 'provider = "postgresql"' in files["db/migrations/migration_lock.toml"]


GOLDEN_FILES = [
    "db/schema.prisma",
    "contract/openapi.yaml",
    "api/src/generated/routes.ts",
    "web/index.html",
    "web/src/generated/contract.ts",
    "web/src/generated/routes.tsx",
    "web/src/generated/screens/Projects.tsx",
    "web/src/generated/screens/Project.tsx",
    "web/src/generated/screens/Issue.tsx",
]


@pytest.mark.parametrize("path", GOLDEN_FILES)
def test_golden(path):
    rendered = render(demo_spec())[path]
    golden = GOLDEN / path
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered)
    assert golden.is_file(), f"missing golden file {golden}; run with FORGE_UPDATE_GOLDEN=1"
    assert rendered == golden.read_text(), f"{path} differs from {golden} (FORGE_UPDATE_GOLDEN=1 to accept)"


# ---------------------------------------------------------------- what the demo spec renders to
def test_prisma_schema_for_demo():
    schema = render(demo_spec())["db/schema.prisma"]
    assert "model Project {" in schema and "model Issue {" in schema
    assert re.search(r"id\s+Int\s+@id @default\(autoincrement\(\)\)", schema)
    assert re.search(r"project_id\s+Int\n", schema)
    assert re.search(r'project\s+Project\s+@relation\("Issue_project_id", fields: \[project_id\], '
                     r"references: \[id\], onDelete: Cascade\)", schema)
    assert re.search(r'status\s+String\s+@default\("open"\)', schema)
    assert re.search(r"updated_at\s+DateTime @updatedAt", schema)
    assert '@@map("issues")' in schema and '@@map("projects")' in schema


def test_routes_register_every_operation_with_literal_config():
    routes = render(demo_spec())["api/src/generated/routes.ts"]
    calls = re.findall(r"registerOperation\(app, ENTITIES, \{", routes)
    assert len(calls) == len(demo_spec().operations)
    for op in demo_spec().operations:
        assert f'"id": "{op.id}"' in routes and f'"path": "{op.path}"' in routes


def test_screens_carry_exact_names():
    files = render(demo_spec())
    project = files["web/src/generated/screens/Project.tsx"]
    for text in ('trigger={"New issue"}', 'title={"New issue"}', '"label": "Create issue"', '"label": "Cancel"',
                 'label={"Issues"}', 'empty={"No issues yet."}', '"label": "Close"', 'label={"All projects"}'):
        assert text in project
    issue = files["web/src/generated/screens/Issue.tsx"]
    assert 'label={"Edit issue"}' in issue and 'successNotice={"Saved"}' in issue
    assert '"params": {"project_id": {"from": "record", "field": "project_id"}}' in issue
    projects = files["web/src/generated/screens/Projects.tsx"]
    assert "<EntityList" in projects and 'empty={"No projects yet."}' in projects
    # element order is kept: heading, then the create dialog, then the list
    assert projects.index("<Heading") < projects.index("<CreateDialog") < projects.index("<EntityList")


def test_no_stubs_for_the_demo():
    assert stub_operations(demo_spec()) == []
    assert not any(p.startswith("api/src/stubs/") for p in render(demo_spec()))


# ---------------------------------------------------------------- a broader spec
def test_wiki_spec_renders_without_warnings():
    files, warnings = render_with_warnings(wiki_spec())
    assert warnings == []
    assert "web/src/generated/screens/NewPage.tsx" in files
    assert stub_operations(wiki_spec()) == ["archivePage"]


def test_wiki_prisma_relations():
    schema = render(wiki_spec())["db/schema.prisma"]
    assert re.search(r"id\s+String\s+@id @default\(uuid\(\)\)", schema)
    assert re.search(r"team_id\s+String\n", schema)                       # FK to a uuid entity
    assert re.search(r"name\s+String\s+@unique", schema)
    assert re.search(r"email\s+String\?\s+@unique", schema)
    assert re.search(r"active\s+Boolean\s+@default\(true\)", schema)
    assert re.search(r"level\s+Int\?\s+@default\(1\)", schema)
    assert re.search(r"joined_on\s+DateTime\?\s+@db.Date", schema)
    assert re.search(r"score\s+Float\?", schema)
    # two references to member from page and one self-reference: every relation is named and unique
    for rel in ("Page_author_id", "Page_reviewer_id", "Member_mentor_id", "Member_team_id", "Page_team_id"):
        assert schema.count(f'@relation("{rel}"') == 2, rel
    assert re.search(r"pages_by_author_id\s+Page\[\]", schema)
    assert re.search(r"members_by_mentor_id\s+Member\[\]", schema)


def test_wiki_stub_note_is_serialized():
    files = render(wiki_spec())
    stub = files["api/src/stubs/archivePage.ts"]
    note = wiki_spec().operations[14].action_note
    assert f"export const NOTE = {json.dumps(note, ensure_ascii=False)};" in stub
    assert "ctx.notImplemented()" in stub
    routes = files["api/src/generated/routes.ts"]
    assert 'import stub_archivePage from "../stubs/archivePage.js";' in routes
    assert re.search(r'"id": "archivePage"[\s\S]*?"stub": true[\s\S]*?\}, stub_archivePage\);', routes)


def test_wiki_parameter_sources():
    files = render(wiki_spec())
    member = files["web/src/generated/screens/Member.tsx"]
    # "Back" goes to /teams/:team_id using the member record's team_id
    assert '"params": {"team_id": {"from": "record", "field": "team_id"}}' in member
    assert '"operation": "getMember", "params": {"id": {"from": "route", "name": "id"}}' in member
    new_page = files["web/src/generated/screens/NewPage.tsx"]
    assert "<CreateForm" in new_page
    assert '"page_id": {"from": "result", "field": "id"}' in new_page       # go to the created page
    assert '"operation": "listMembers"' in new_page                          # author options
    team = files["web/src/generated/screens/Team.tsx"]
    assert "<NavButton" in team and 'label={"Write page"}' in team
    assert 'navigateTo={{"path": "/", "params": {}}}' in team                # deleting the team goes to its parent
    teams = files["web/src/generated/screens/Teams.tsx"]
    assert 'role={"grid"}' in teams and "<CreateForm" in teams


# ---------------------------------------------------------------- serializers only
def test_strings_only_reach_code_through_serializers():
    nasty = 'x"}); alert(`1`); ({"y": ${z}} </script> '

    def poison(d):
        d["app_name"] = nasty
        find(d["elements"], id="new-project")["name"] = nasty
        find(d["elements"], id="projects-empty")["name"] = "No " + nasty
        find(d["screens"], id="projects")["title"] = nasty
    files = render(mutate(demo_spec(), poison))
    literal = json.dumps(nasty, ensure_ascii=False).replace(" ", "\\u2028")
    projects = files["web/src/generated/screens/Projects.tsx"]
    assert f"trigger={{{literal}}}" in projects and f"title={{{literal}}}" in projects
    assert " " not in projects
    assert f"export const APP_NAME = {literal};" in files["web/src/generated/routes.tsx"]
    assert "<title>x&quot;}); alert(`1`); ({&quot;y&quot;: ${z}} &lt;/script&gt;" in files["web/index.html"]
    # every JSON literal in the screen parses back to the same value
    for match in re.finditer(r'trigger=\{(".*?")\}\n', projects):
        assert json.loads(match.group(1)) == nasty


BAD_SPECS = [
    ("entity name", lambda d: d["entities"][0].update(name="Project Item"), "snake_case"),
    ("field name", lambda d: find(d["fields"], entity="issue", name="title").update(name='title"; drop'), "letter"),
    ("operation id", lambda d: find(d["operations"], id="createIssue").update(id="create-issue"), "id must start"),
    ("operation id clash", lambda d: find(d["operations"], id="getIssue").update(id="createissue"), "clashes"),
    ("api path", lambda d: find(d["operations"], id="getIssue").update(path="/api/issues/{issue_id}/../x"), "path"),
    ("path params", lambda d: find(d["operations"], id="getIssue").update(path="/api/issues/{id}"), "path parameters"),
    ("screen id", lambda d: find(d["screens"], id="issue").update(id="issue/../x"), "id must start"),
    ("screen route", lambda d: find(d["screens"], id="issue").update(route="/issues/*"), "route"),
    ("list key", lambda d: find(d["operations"], id="listIssues").update(list_key="items-x"), "list_key"),
    ("enum default", lambda d: find(d["fields"], name="status").update(default="done"), "default"),
    ("unknown reference", lambda d: find(d["fields"], name="project_id").update(references="team"), "unknown entity"),
    ("request field", lambda d: find(d["operations"], id="createIssue").update(request_fields=["title", "nope"]),
     "not a field"),
    ("generated request field", lambda d: find(d["operations"], id="createIssue").update(request_fields=["id"]),
     "generated"),
    ("get without id", lambda d: find(d["operations"], id="getIssue").update(path="/api/issues", path_params=[]),
     "needs a path parameter"),
    ("duplicate route", lambda d: find(d["operations"], id="getProject").update(path="/api/issues/{issue_id}",
                                                                                   path_params=[{"name": "issue_id",
                                                                                                 "entity": "issue"}],
                                                                                   entity="issue"),
     "already used"),
    ("read method", lambda d: find(d["operations"], id="listIssues").update(method="POST"), "must use GET"),
    ("param names", lambda d: find(d["operations"], id="deleteIssue").update(
        path="/api/issues/{id}", path_params=[{"name": "id", "entity": "issue"}]), "names its parameters"),
    ("missing id field", lambda d: find(d["fields"], entity="project", name="id").update(generated="none"),
     "exactly one field"),
    ("unknown target screen", lambda d: find(d["elements"], id="issue-back").update(target_screen="nowhere"),
     "unknown target screen"),
    ("unknown operation", lambda d: find(d["elements"], id="issues-row-close").update(operation="reopenIssue"),
     "unknown operation"),
    ("bound heading without record", lambda d: find(d["elements"], id="projects-heading").update(field="name"),
     "loads no record"),
]


@pytest.mark.parametrize("label,change,message", BAD_SPECS, ids=[b[0] for b in BAD_SPECS])
def test_invalid_specs_are_rejected(label, change, message):
    spec = mutate(demo_spec(), change)
    with pytest.raises(RenderError) as err:
        render(spec)
    assert any(message in p for p in err.value.problems), err.value.problems
    assert validate(spec) == err.value.problems


def test_validate_accepts_good_specs():
    assert validate(demo_spec()) == []
    assert validate(wiki_spec()) == []


# ---------------------------------------------------------------- regenerate-and-diff, editable globs
def test_render_check_flags_immutable_changes_only(tmp_path):
    spec = demo_spec()
    for rel, content in render(spec).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content)
    assert render_check(spec, tmp_path) == []
    (tmp_path / "db/schema.prisma").write_text("model Hacked {}\n")
    (tmp_path / "web/src/generated/screens/Issue.tsx").write_text("// restyled by the builder\n")
    (tmp_path / "api/src/generated/routes.ts").unlink()
    assert render_check(spec, tmp_path) == ["api/src/generated/routes.ts", "db/schema.prisma"]


def test_editable_globs():
    assert EDITABLE_GLOBS == ["web/src/generated/screens/**", "web/src/styles/**", "api/src/stubs/**"]
    assert is_editable("web/src/generated/screens/Issue.tsx")
    assert is_editable("web/src/styles/app.css") and is_editable("api/src/stubs/closeIssue.ts")
    assert not is_editable("web/src/generated/routes.tsx")
    assert not is_editable("web/src/lib/forms.tsx") and not is_editable("package.json")
    assert glob_regex("a/*.ts").match("a/x.ts") and not glob_regex("a/*.ts").match("a/b/x.ts")
