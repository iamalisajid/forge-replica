"""Scripted pilot on the running demo -> trace -> REST adapter. No model involved."""
import pytest

from forge.core.store import RunStore
from forge.models.base import ArtifactHeader
from forge.stages.explore import locators
from forge.stages.explore.harness import Harness
from forge.stages.extract.rest import extract, template_of

DEMO = "http://127.0.0.1:4001/"


def ref(snapshot: str, role: str, name: str, *, prefix: bool = False, last: bool = False) -> str:
    hits = [r for r, n in locators.parse(snapshot).items()
            if n.role == role and n.name is not None and (n.name.startswith(name) if prefix else n.name == name)]
    assert hits, f"{role} {name!r} not in snapshot"
    return hits[-1] if last else hits[0]


async def scripted_pilot(store: RunStore) -> Harness:
    h = Harness(store, url=DEMO, mutations="test-account")
    await h.start()
    try:
        async def do(role, name, action="click", value=None, **kw):
            snap, _ = await h.observe()
            return await h.act(ref(snap, role, name, **kw), action, value)

        await h.navigate(DEMO)
        await do("button", "New project")
        await do("textbox", "Name", "fill", "Driver project")
        await do("button", "Create project")
        await do("link", "Driver project", prefix=True)
        await do("button", "New issue")
        await do("textbox", "Title", "fill", "Fix header")
        await do("combobox", "Priority", "select", "high")
        await do("button", "Create issue")
        await do("button", "New issue")                       # negative probe: empty title
        await do("button", "Create issue")
        await do("button", "Cancel")
        await do("button", "Close")
        await do("link", "Fix header", prefix=True)
        await do("textbox", "Title", "fill", "Fix header v2")
        await do("combobox", "Status", "select", "in_progress")
        await do("button", "Save changes")
        await h.navigate(h.page.url)
        await do("button", "Delete issue")
        await h.navigate(DEMO)
        snap, _ = await h.observe()
        nodes = locators.parse(snap)
        mine = next(r for r, n in nodes.items() if n.role == "link" and (n.name or "").startswith("Driver project"))
        row = nodes[mine].parent
        delete = next(c.ref for c in row.children if c.role == "button" and c.name == "Delete")
        await h.act(delete, "click")
        await h.cleanup()
    finally:
        await h.close()
    return h


def test_template_of():
    assert template_of("/api/projects/12/issues") == ("/api/projects/{project_id}/issues", ["project_id"])
    assert template_of("/api/issues/7/close") == ("/api/issues/{issue_id}/close", ["issue_id"])


@pytest.mark.docker
async def test_rest_adapter_on_scripted_pilot(tmp_path):
    store, config = RunStore.create(DEMO, mutations="test-account", runs_dir=tmp_path / "runs",
                                    out_root=tmp_path / "apps")
    h = await scripted_pilot(store)
    trace = h.trace(ArtifactHeader(schema_id="forge.trace/1", run_id=config.run_id, stage="explore"),
                    "scripted", "issues", [])
    model = extract(store, trace, config.run_id)
    ops = {(o.method, o.path_template): o for o in model.operations}
    assert ops[("POST", "/api/projects")].kind == "create"
    assert ops[("GET", "/api/projects/{project_id}/issues")].kind == "list"
    assert ops[("GET", "/api/projects/{project_id}/issues")].list_key == "items"
    assert ops[("POST", "/api/issues/{issue_id}/close")].kind == "action"
    assert ops[("PATCH", "/api/issues/{issue_id}")].kind == "update"
    assert ops[("DELETE", "/api/issues/{issue_id}")].kind == "delete"
    issue = next(e for e in model.entities if e.name == "issue")
    f = {x.name: x for x in issue.fields}
    assert f["project_id"].references == "project"
    assert f["priority"].enum_values == ["low", "medium", "high"]
    assert f["status"].enum_values == ["open", "in_progress", "closed"]
    assert f["title"].required == "observed"
    assert f["created_at"].server_generated and f["created_at"].format == "date-time"
    assert f["id"].server_generated
    assert any(r.template == "/projects/{project_id}" for r in model.routes)
    assert all(c.deleted for c in h.created), h.created
