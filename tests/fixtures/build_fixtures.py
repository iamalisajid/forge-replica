"""Build the hand-written fixtures for the demo issue tracker.

Outputs (committed):
  tests/fixtures/demo_appspec.json          the AppSpec Specify should produce for the demo
  demo/issue-tracker/scenarios/pilot.json   the pilot workflow: create -> reload -> edit -> delete
  demo/issue-tracker/heldout/*.json         five held-out scenarios, never shown to the builder

Run: uv run python tests/fixtures/build_fixtures.py
"""
from __future__ import annotations

import json
from pathlib import Path

from forge.models.appspec import AppSpecDraft, PathParam, SpecElement, SpecEntity, SpecField, SpecOperation, SpecScreen
from forge.models.base import NameValue
from forge.models.scenario import Bind, Capture, Effect, Scenario, ScenarioStep, Target

ROOT = Path(__file__).resolve().parents[2]


# ---------- small builders ----------
def T(role, name=None, *, has_text=None, within=None, exact=True) -> Target:
    w_role, w_name, w_text = within or (None, None, None)
    return Target(role=role, name=name, exact=exact, has_text=has_text,
                  within_role=w_role, within_name=w_name, within_has_text=w_text)


def S(kind, target=None, value=None, route=None) -> ScenarioStep:
    return ScenarioStep(kind=kind, target=target, value=value, route=route)


def uniq(name) -> Bind:
    return Bind(name=name, kind="uniq_text", value=None)


NEW_PROJECT = ("dialog", "New project", None)
NEW_ISSUE = ("dialog", "New issue", None)
EDIT_FORM = ("form", "Edit issue", None)


def create_project_steps(var="project"):
    return [
        S("goto", route="/"),
        S("click", T("button", "New project")),
        S("fill", T("textbox", "Name", within=NEW_PROJECT), "{{%s}}" % var),
        S("click", T("button", "Create project", within=NEW_PROJECT)),
        S("expect_visible", T("link", "{{%s}}" % var)),
    ]


def create_issue_steps(project="project", title="title", priority=None):
    steps = [
        S("click", T("link", "{{%s}}" % project)),
        S("expect_visible", T("heading", "{{%s}}" % project)),
        S("click", T("button", "New issue")),
        S("fill", T("textbox", "Title", within=NEW_ISSUE), "{{%s}}" % title),
    ]
    if priority:
        steps.append(S("select", T("combobox", "Priority", within=NEW_ISSUE), priority))
    steps += [
        S("click", T("button", "Create issue", within=NEW_ISSUE)),
        S("expect_visible", T("link", "{{%s}}" % title)),
    ]
    return steps


def scenario(id, feature, title, steps, binds, captures=(), effects=(), mutating=True, evidence=("fixture",)):
    return Scenario(id=id, feature=feature, title=title, mutating=mutating, binds=list(binds), setup=[],
                    steps=steps, captures=list(captures), effects=list(effects), evidence=list(evidence))


PROJECT_ID = Capture(name="project_id", operation="createProject", field="id")
ISSUE_ID = Capture(name="issue_id", operation="createIssue", field="id")


# ---------- scenarios ----------
def pilot() -> Scenario:
    steps = create_project_steps() + create_issue_steps(priority="high") + [
        S("reread"),
        S("expect_visible", T("link", "{{title}}")),
        S("click", T("link", "{{title}}")),
        S("fill", T("textbox", "Title", within=EDIT_FORM), "{{title2}}"),
        S("click", T("button", "Save changes")),
        S("expect_visible", T("heading", "{{title2}}")),
        S("reread"),
        S("expect_visible", T("heading", "{{title2}}")),
        S("expect_value", T("combobox", "Priority", within=EDIT_FORM), "high"),
        S("click", T("button", "Delete issue")),
        S("expect_visible", T("heading", "{{project}}")),
        S("expect_hidden", T("link", "{{title2}}")),
        S("reread"),
        S("expect_hidden", T("link", "{{title2}}")),
    ]
    return scenario(
        "pilot-issue-lifecycle", "issues", "Create, reload, edit and delete an issue", steps,
        [uniq("project"), uniq("title"), uniq("title2")], [PROJECT_ID, ISSUE_ID],
        [Effect(operation="listIssues", path_params=[NameValue(name="project_id", value="{{captured.project_id}}")],
                field="title", value="{{title2}}", present=False),
         Effect(operation="listProjects", path_params=[], field="name", value="{{project}}", present=True)])


def heldout() -> list[Scenario]:
    create_project = scenario(
        "heldout-create-project", "projects", "A new project is listed after a reload",
        create_project_steps() + [S("reread"), S("expect_visible", T("link", "{{project}}"))],
        [uniq("project")], [PROJECT_ID])
    close_issue = scenario(
        "heldout-close-issue", "issues", "Closing an issue persists its status",
        create_project_steps() + create_issue_steps() + [
            S("click", T("button", "Close", within=("row", None, "{{title}}"))),
            S("expect_text", T("row", has_text="{{title}}"), "closed"),
            S("reread"),
            S("expect_text", T("row", has_text="{{title}}"), "closed"),
        ],
        [uniq("project"), uniq("title")], [PROJECT_ID, ISSUE_ID])
    empty_title = scenario(
        "heldout-empty-title-rejected", "issues", "An issue without a title is rejected",
        create_project_steps() + [
            S("click", T("link", "{{project}}")),
            S("click", T("button", "New issue")),
            S("click", T("button", "Create issue", within=NEW_ISSUE)),
            S("expect_text", T("alert", within=NEW_ISSUE), "required"),
            S("click", T("button", "Cancel", within=NEW_ISSUE)),
            S("reread"),
            S("expect_visible", T("heading", "{{project}}")),
            S("expect_hidden", T("table", "Issues")),
        ],
        [uniq("project")], [PROJECT_ID])
    edit_priority = scenario(
        "heldout-edit-priority", "issues", "Changing priority persists after a reload",
        create_project_steps() + create_issue_steps() + [
            S("click", T("link", "{{title}}")),
            S("select", T("combobox", "Priority", within=EDIT_FORM), "low"),
            S("click", T("button", "Save changes")),
            S("reread"),
            S("expect_value", T("combobox", "Priority", within=EDIT_FORM), "low"),
        ],
        [uniq("project"), uniq("title")], [PROJECT_ID, ISSUE_ID])
    delete_project = scenario(
        "heldout-delete-project", "projects", "A deleted project stays gone after a reload",
        create_project_steps() + [
            S("reread"),
            S("click", T("button", "Delete", within=("listitem", None, "{{project}}"))),
            S("expect_hidden", T("link", "{{project}}")),
            S("reread"),
            S("expect_hidden", T("link", "{{project}}")),
        ],
        [uniq("project")], [PROJECT_ID])
    return [create_project, close_issue, empty_title, edit_priority, delete_project]


# ---------- AppSpec ----------
def F(entity, name, type, *, required=False, enum=(), ref=None, default=None, generated="none", unique=False):
    return SpecField(entity=entity, name=name, type=type, enum_values=list(enum), references=ref, required=required,
                     unique=unique, default=default, generated=generated, basis="observed", evidence=["fixture"])


def OP(id, entity, kind, method, path, params=(), fields=(), response="object", list_key=None, status=200, sets=()):
    return SpecOperation(
        id=id, entity=entity, kind=kind, method=method, path=path,
        path_params=[PathParam(name=n, entity=e) for n, e in params], request_fields=list(fields), response=response,
        list_key=list_key, success_status=status, action_sets=[NameValue(name=k, value=v) for k, v in sets],
        action_note=None, source_method=method, source_path=path, basis="observed", evidence=["fixture"])


def E(id, screen, role, name, purpose, *, parent=None, op=None, field=None, target=None, order=0):
    return SpecElement(id=id, screen=screen, parent=parent, role=role, name=name, purpose=purpose, operation=op,
                       field=field, target_screen=target, order=order, evidence=["fixture"])


def appspec() -> AppSpecDraft:
    P, I = "project", "issue"
    pid, iid = [("project_id", P)], [("issue_id", I)]
    entities = [
        SpecEntity(name=P, label="Project", plural_label="Projects", id_type="integer", owned_by_user=False,
                   basis="observed", evidence=["fixture"]),
        SpecEntity(name=I, label="Issue", plural_label="Issues", id_type="integer", owned_by_user=False,
                   basis="observed", evidence=["fixture"]),
    ]
    fields = [
        F(P, "id", "integer", required=True, generated="id"),
        F(P, "name", "string", required=True),
        F(P, "created_at", "datetime", required=True, generated="created_at"),
        F(I, "id", "integer", required=True, generated="id"),
        F(I, "project_id", "reference", required=True, ref=P),
        F(I, "title", "string", required=True),
        F(I, "status", "enum", required=True, enum=("open", "in_progress", "closed"), default="open"),
        F(I, "priority", "enum", required=True, enum=("low", "medium", "high"), default="medium"),
        F(I, "created_at", "datetime", required=True, generated="created_at"),
        F(I, "updated_at", "datetime", required=True, generated="updated_at"),
    ]
    operations = [
        OP("listProjects", P, "list", "GET", "/api/projects", response="list_envelope", list_key="items"),
        OP("createProject", P, "create", "POST", "/api/projects", fields=["name"], status=201),
        OP("getProject", P, "get", "GET", "/api/projects/{project_id}", pid),
        OP("updateProject", P, "update", "PATCH", "/api/projects/{project_id}", pid, ["name"]),
        OP("deleteProject", P, "delete", "DELETE", "/api/projects/{project_id}", pid, response="empty", status=204),
        OP("listIssues", I, "list", "GET", "/api/projects/{project_id}/issues", pid, response="list_envelope",
           list_key="items"),
        OP("createIssue", I, "create", "POST", "/api/projects/{project_id}/issues", pid,
           ["title", "status", "priority"], status=201),
        OP("getIssue", I, "get", "GET", "/api/issues/{issue_id}", iid),
        OP("updateIssue", I, "update", "PATCH", "/api/issues/{issue_id}", iid, ["title", "status", "priority"]),
        OP("deleteIssue", I, "delete", "DELETE", "/api/issues/{issue_id}", iid, response="empty", status=204),
        OP("closeIssue", I, "action", "POST", "/api/issues/{issue_id}/close", iid, sets=[("status", "closed")]),
    ]
    screens = [
        SpecScreen(id="projects", route="/", title="Projects", entity=P, kind="list", parent=None, evidence=["fixture"]),
        SpecScreen(id="project", route="/projects/:project_id", title="Project", entity=P, kind="detail",
                   parent="projects", evidence=["fixture"]),
        SpecScreen(id="issue", route="/issues/:issue_id", title="Issue", entity=I, kind="detail", parent="project",
                   evidence=["fixture"]),
    ]
    elements = [
        # Projects screen
        E("projects-heading", "projects", "heading", "Projects", "heading", order=1),
        E("new-project", "projects", "button", "New project", "open_create", op="createProject", order=2),
        E("new-project-dialog", "projects", "dialog", "New project", "container", parent="new-project", order=3),
        E("new-project-name", "projects", "textbox", "Name", "field", parent="new-project-dialog", field="name", order=4),
        E("new-project-error", "projects", "alert", "", "status_message", parent="new-project-dialog", order=5),
        E("new-project-submit", "projects", "button", "Create project", "submit_create", parent="new-project-dialog",
          op="createProject", order=6),
        E("new-project-cancel", "projects", "button", "Cancel", "cancel", parent="new-project-dialog", order=7),
        E("projects-list", "projects", "list", "Projects", "list", op="listProjects", order=8),
        E("projects-row", "projects", "listitem", "", "list_row", parent="projects-list", order=9),
        E("projects-row-link", "projects", "link", "", "navigate", parent="projects-row", field="name",
          target="project", order=10),
        E("projects-row-delete", "projects", "button", "Delete", "delete", parent="projects-row", op="deleteProject",
          order=11),
        E("projects-empty", "projects", "paragraph", "No projects yet.", "text", order=12),
        # Project screen
        E("project-back", "project", "link", "All projects", "navigate", target="projects", order=1),
        E("project-heading", "project", "heading", "", "heading", field="name", order=2),
        E("new-issue", "project", "button", "New issue", "open_create", op="createIssue", order=3),
        E("new-issue-dialog", "project", "dialog", "New issue", "container", parent="new-issue", order=4),
        E("new-issue-title", "project", "textbox", "Title", "field", parent="new-issue-dialog", field="title", order=5),
        E("new-issue-status", "project", "combobox", "Status", "field", parent="new-issue-dialog", field="status",
          order=6),
        E("new-issue-priority", "project", "combobox", "Priority", "field", parent="new-issue-dialog",
          field="priority", order=7),
        E("new-issue-error", "project", "alert", "", "status_message", parent="new-issue-dialog", order=8),
        E("new-issue-submit", "project", "button", "Create issue", "submit_create", parent="new-issue-dialog",
          op="createIssue", order=9),
        E("new-issue-cancel", "project", "button", "Cancel", "cancel", parent="new-issue-dialog", order=10),
        E("issues-table", "project", "table", "Issues", "list", op="listIssues", order=11),
        E("issues-row", "project", "row", "", "list_row", parent="issues-table", order=12),
        E("issues-row-link", "project", "link", "", "navigate", parent="issues-row", field="title", target="issue",
          order=13),
        E("issues-row-status", "project", "cell", "", "text", parent="issues-row", field="status", order=14),
        E("issues-row-priority", "project", "cell", "", "text", parent="issues-row", field="priority", order=15),
        E("issues-row-close", "project", "button", "Close", "action", parent="issues-row", op="closeIssue", order=16),
        E("issues-row-delete", "project", "button", "Delete", "delete", parent="issues-row", op="deleteIssue",
          order=17),
        E("issues-empty", "project", "paragraph", "No issues yet.", "text", order=18),
        # Issue screen
        E("issue-back", "issue", "link", "Back to project", "navigate", target="project", order=1),
        E("issue-heading", "issue", "heading", "", "heading", field="title", order=2),
        E("edit-issue-form", "issue", "form", "Edit issue", "container", op="updateIssue", order=3),
        E("edit-issue-title", "issue", "textbox", "Title", "field", parent="edit-issue-form", field="title", order=4),
        E("edit-issue-status", "issue", "combobox", "Status", "field", parent="edit-issue-form", field="status",
          order=5),
        E("edit-issue-priority", "issue", "combobox", "Priority", "field", parent="edit-issue-form",
          field="priority", order=6),
        E("edit-issue-error", "issue", "alert", "", "status_message", parent="edit-issue-form", order=7),
        E("edit-issue-submit", "issue", "button", "Save changes", "submit_update", parent="edit-issue-form",
          op="updateIssue", order=8),
        E("edit-issue-saved", "issue", "status", "Saved", "status_message", order=9),
        E("delete-issue", "issue", "button", "Delete issue", "delete", op="deleteIssue", target="project", order=10),
    ]
    return AppSpecDraft(
        app_name="Issue Tracker", auth="none", entities=entities, fields=fields, operations=operations,
        screens=screens, elements=elements, scenarios=[pilot()], pilot_scenario="pilot-issue-lifecycle",
        assumptions=["Lists are ordered newest first."], unsupported=[])


def dump(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n")
    print("wrote", path.relative_to(ROOT))


if __name__ == "__main__":
    dump(ROOT / "tests/fixtures/demo_appspec.json", appspec())
    dump(ROOT / "demo/issue-tracker/scenarios/pilot.json", pilot())
    for s in heldout():
        dump(ROOT / f"demo/issue-tracker/heldout/{s.id}.json", s)
