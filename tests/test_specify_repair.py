"""Deterministic spec repairs: get-by-id for detail screens, unusable operations, and last-resort degrading."""
import json
from pathlib import Path

from forge.models.appspec import AppSpecDraft, PathParam, SpecOperation
from forge.stages.specify.specify import check, complete, repair, settle

FIXTURE = Path(__file__).parent / "fixtures" / "demo_appspec.json"


def demo() -> AppSpecDraft:
    data = json.loads(FIXTURE.read_text())
    return AppSpecDraft.model_validate(data.get("spec", data))


def test_fixture_is_clean():
    assert check(demo()) == []


def test_complete_adds_get_for_detail_screen_with_only_list_reads():
    spec = demo()
    spec = spec.model_copy(update={"operations": [o for o in spec.operations if o.id != "getProject"]})
    done = complete(spec)
    added = [o for o in done.operations if o.kind == "get" and o.entity == "project"]
    assert [(o.id, o.method, o.path) for o in added] == [("getProject", "GET", "/api/projects/{id}")]
    assert added[0].basis == "inferred" and any("getProject" in a for a in done.assumptions)
    assert complete(done) == done                                   # idempotent
    assert complete(demo()) == demo()                               # nothing to add when a get exists


def test_repair_drops_unrenderable_unused_operation():
    spec = demo()
    bad = SpecOperation(id="fetchMe", entity="project", kind="action", method="GET", path="/api/me", path_params=[],
                        request_fields=[], response="object", list_key=None, success_status=200, action_sets=[],
                        action_note=None, source_method="GET", source_path="/api/me", basis="observed", evidence=[])
    spec = spec.model_copy(update={"operations": spec.operations + [bad]})
    errors = check(spec)
    assert any(e.startswith("operation 'fetchMe'") for e in errors)
    fixed, remaining = settle(spec, errors, degrade=False)
    assert remaining == [] and "fetchMe" not in {o.id for o in fixed.operations}
    assert any("fetchMe" in u for u in fixed.unsupported)


def test_degrade_makes_unwireable_element_static_only_when_asked():
    spec = demo()
    el = next(e for e in spec.elements if e.purpose == "navigate" and e.target_screen == "issue")
    broken = el.model_copy(update={"screen": "projects", "parent": None})   # no issue in reach on the list screen
    spec = spec.model_copy(update={"elements": [broken if e.id == el.id else e for e in spec.elements]})
    errors = check(spec)
    assert any(e.startswith(f"element '{el.id}'") for e in errors)
    assert repair(spec, errors, degrade=False) == spec
    fixed, remaining = settle(spec, errors, degrade=True)
    assert remaining == []
    static = next(e for e in fixed.elements if e.id == el.id)
    assert (static.purpose, static.role, static.name, static.target_screen) == ("text", el.role, el.name, None)
    assert any(el.id in u and "builder" in u for u in fixed.unsupported)


def test_loosen_tries_partial_name_then_scope_text_then_no_scope():
    from forge.models.scenario import Target
    from forge.stages.oracle.interpreter import _loosen

    t = Target(role="button", name="Add task", exact=True, has_text=None, within_role="listitem",
               within_name="Add task", within_has_text=None)
    got = [(x.exact, x.within_role, x.within_name, x.within_has_text) for x in _loosen(t)]
    assert got == [(False, "listitem", "Add task", None), (True, "listitem", None, "Add task"),
                   (False, "listitem", None, "Add task"), (True, None, None, None), (False, None, None, None)]
    plain = Target(role="textbox", name="Name", exact=True, has_text=None, within_role=None, within_name=None,
                   within_has_text=None)
    assert [x.exact for x in _loosen(plain)] == [False]
