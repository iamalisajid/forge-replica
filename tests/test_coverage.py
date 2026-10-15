"""Coverage separates what was explored end to end, what is built, and what was only seen."""
import json

from forge.core.store import RunStore
from forge.coverage import coverage, markdown, summary_lines


def test_coverage_marks_built_recorded_and_seen_only(tmp_path):
    store, _ = RunStore.create("https://app.example.com/app/inbox", runs_dir=tmp_path)
    art = store.dir / "artifacts"
    (art / "explore.json").write_text(json.dumps({"probed": ["Tasks"], "inventory": {"pilot_feature": "Tasks", "features": [
        {"name": "Tasks", "entity": "task", "screens": ["/app/inbox", "/app/today"], "can_create": True, "can_edit": True,
         "can_delete": True, "actions": []},
        {"name": "Projects", "entity": "project", "screens": ["/app/project/:slug-:id"], "can_create": True,
         "can_edit": False, "can_delete": False, "actions": []}]}}))
    (art / "observed.json").write_text(json.dumps({"routes": [{"template": "/app/inbox"}, {"template": "/app/project/x-1"}],
        "operations": [{"method": "POST", "path_template": "/api/sync#item_add"},
                       {"method": "POST", "path_template": "/.sentry"},
                       {"method": "GET", "path_template": "/api/user"}]}))
    (art / "appspec.json").write_text(json.dumps({"spec": {"screens": [{"route": "/app/inbox", "title": "Inbox"}],
        "operations": [{"source_path": "/api/sync#item_add"}], "scenarios": [{"id": "crud"}], "pilot_scenario": "crud",
        "unsupported": ["GET /api/user left out"]}}))
    (art / "oracle.json").write_text(json.dumps({"candidates": ["crud"]}))
    cov = coverage(store)
    assert [(f["name"], f["probed"]) for f in cov["features"]] == [("Tasks", True), ("Projects", False)]
    by_route = {s["route"]: (s["recorded"], s["built"]) for s in cov["screens"]}
    assert by_route == {"/app/inbox": (True, True), "/app/today": (False, False),
                        "/app/project/:slug-:id": (True, False)}
    assert [(o["call"], o["built"]) for o in cov["operations"]] == [("POST /api/sync#item_add", True),
                                                                      ("GET /api/user", False)]
    assert cov["scenarios"] == [{"id": "crud", "title": None, "validated": True, "pilot": True}]
    assert any("seen but not built" in line and "/app/today" in line for line in summary_lines(cov))
    assert "| `/app/today` | no | no |" in markdown(cov)
