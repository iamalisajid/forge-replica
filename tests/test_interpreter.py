"""Scenario interpreter: bindings, targets, failure kinds, non-vacuity, captures, effects and the audit.

Most tests run against a local fixture app (tests/fixtures/oracle). The demo tests need the demo:
``docker compose -f demo/issue-tracker/compose.yaml up -d`` (they skip when it is not running).
"""
from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

import pytest

from forge.models.appspec import AppSpecDraft, OpWire, op_wires
from forge.models.base import NameValue
from forge.models.run import RunnerRequest
from forge.models.scenario import Bind, Capture, Effect, Scenario, ScenarioStep, Target
from forge.stages.oracle.interpreter import (
    Bindings,
    Timeouts,
    UnresolvedBinding,
    fill_path,
    generate_bindings,
    origin_of,
    path_regex,
    probe_index,
    run_scenarios,
    url_regex,
)
from forge.stages.oracle.validate import validate_on_source, verdicts
from tests.fixtures.oracle.server import ops_json, serve

ROOT = Path(__file__).resolve().parents[1]
DEMO = "http://127.0.0.1:4001"
FAST = Timeouts(expect_ms=2_000, action_ms=2_000, navigation_ms=8_000, effect_ms=1_000, settle_cap_ms=1_000,
                probe_idle_ms=2_000)
OPS = [OpWire.model_validate(o) for o in ops_json()]


# ---------- builders ----------
def T(role, name=None, *, has_text=None, within=None, exact=True) -> Target:
    w_role, w_name, w_text = within or (None, None, None)
    return Target(role=role, name=name, exact=exact, has_text=has_text, within_role=w_role, within_name=w_name,
                  within_has_text=w_text)


def S(kind, target=None, value=None, route=None) -> ScenarioStep:
    return ScenarioStep(kind=kind, target=target, value=value, route=route)


def scenario(id, steps, *, binds=(), captures=(), effects=(), setup=()) -> Scenario:
    return Scenario(id=id, feature="items", title=id, mutating=True, binds=list(binds), setup=list(setup),
                    steps=steps, captures=list(captures), effects=list(effects), evidence=["test"])


def uniq(name) -> Bind:
    return Bind(name=name, kind="uniq_text", value=None)


DIALOG = ("dialog", "New item", None)
ITEM_ID = Capture(name="item_id", operation="createItem", field="id")


def create_steps(var="name"):
    return [
        S("goto", route="/"),
        S("click", T("button", "New item")),
        S("fill", T("textbox", "Name", within=DIALOG), f"{{{{{var}}}}}"),
        S("click", T("button", "Create", within=DIALOG)),
        S("expect_visible", T("link", f"{{{{{var}}}}}")),
    ]


async def run(base_url, scenarios, *, target="source", runs=1, parallel=1, ops=OPS, fixtures=(), allowed=None,
              timeouts=FAST, channel=None):
    req = RunnerRequest(target=target, base_url=base_url, scenarios=scenarios, operations=ops,
                        allowed_origins=allowed if allowed is not None else [base_url], runs=runs,
                        parallel=parallel, fixtures=list(fixtures))
    return await run_scenarios(req, timeouts=timeouts, channel=channel)


@pytest.fixture
def app():
    with serve() as url:
        yield url


@pytest.fixture
def mutant_app():
    with serve(mutant=True) as url:
        yield url


# ---------- pure helpers ----------
def test_bindings_are_fresh_and_well_formed():
    binds = [uniq("title"), Bind(name="mail", kind="uniq_email", value=None),
             Bind(name="n", kind="uniq_int", value=None), Bind(name="lit", kind="literal", value="hello"),
             Bind(name="who", kind="fixture", value="user_email")]
    a = generate_bindings(binds, {"user_email": "a@example.test"})
    b = generate_bindings(binds, {"user_email": "a@example.test"})
    assert a["title"].startswith("rpl-") and a["title"].endswith("-title")
    assert len(a["title"]) == len("rpl-abcdef-title")
    assert a["mail"].endswith("-mail@example.test")
    assert a["n"].isdigit() and len(a["n"]) == 6
    assert a["lit"] == "hello" and a["who"] == "a@example.test"
    assert a["title"] != b["title"]
    seeded = [generate_bindings(binds, {"user_email": "x"}, seed="s", salt="sc|1") for _ in range(2)]
    assert seeded[0] == seeded[1]
    with pytest.raises(UnresolvedBinding):
        generate_bindings([Bind(name="who", kind="fixture", value="missing")], {})


def test_binding_render_and_missing():
    b = Bindings({"project": "rpl-1"})
    assert b.render("/p/{{project}}") == "/p/rpl-1"
    assert b.missing("{{captured.id}} {{project}}") == ["captured.id"]
    b.captured["id"] = "7"
    assert b.render("/x/{{ captured.id }}/{{project}}") == "/x/7/rpl-1"
    with pytest.raises(UnresolvedBinding):
        b.render("{{nope}}")


def test_templates_and_origins():
    rx = path_regex("/api/projects/{project_id}/issues")
    assert rx.match("/api/projects/12/issues") and rx.match("/api/projects/12/issues/")
    assert not rx.match("/api/projects/12/issues/3") and not rx.match("/api/projects/issues")
    assert path_regex("/projects/:id").match("/projects/abc")
    assert path_regex("/").match("/") and path_regex("/").match("")
    rx, with_query = url_regex("/items/{id}?tab=1")
    assert with_query and rx.match("/items/4?tab=1") and not rx.match("/items/4?tab=2")
    assert fill_path("/api/projects/{project_id}/issues", {"project_id": "5"}) == "/api/projects/5/issues"
    assert fill_path("/api/x/:id", {"id": "a b"}) == "/api/x/a%20b"
    assert origin_of("http://App:80/x") == "http://app" and origin_of("https://h:8443/") == "https://h:8443"
    assert origin_of("ws://127.0.0.1:4001/s") == "http://127.0.0.1:4001"
    assert origin_of("data:text/plain,hi") is None and origin_of("about:blank") is None
    assert probe_index([S("goto", route="/"), S("goto", route="/x"), S("click", T("button", "a"))]) == 2


# ---------- local fixture app ----------
async def test_create_passes_with_capture_effect_and_first_write(app):
    s = scenario("create", create_steps() + [S("reread"), S("expect_visible", T("link", "{{name}}"))],
                 binds=[uniq("name")], captures=[ITEM_ID],
                 effects=[Effect(operation="getItem",
                                 path_params=[NameValue(name="item_id", value="{{captured.item_id}}")],
                                 field="name", value="{{name}}", present=True),
                          Effect(operation="listItems", path_params=[], field="name", value="{{name}}", present=True)])
    [r] = await run(app, [s])
    assert r.passed, r.message
    assert r.failure_kind == "none" and r.failed_step is None
    assert r.first_write_step == 3  # the "Create" click sends the POST
    assert [c.name for c in r.captured] == ["item_id"] and r.captured[0].value.isdigit()
    assert r.vacuous_assertions == [] and r.non_vacuous_assertions == 2
    assert r.offorigin_requests == []


async def test_vacuous_assertions_are_recorded(app):
    s = scenario("vacuous", [
        S("goto", route="/"),
        S("expect_visible", T("heading", "Items")),              # true before anything happens
        S("click", T("button", "New item")),
        S("expect_visible", T("dialog", "New item")),            # needs the click
        S("click", T("button", "Cancel", within=DIALOG)),
        S("expect_hidden", T("dialog", "New item")),             # already hidden before the first action
    ])
    [r] = await run(app, [s])
    assert r.passed, r.message
    assert r.vacuous_assertions == [1, 5]
    assert r.non_vacuous_assertions == 1


async def test_only_vacuous_assertions_make_no_candidate(app):
    s = scenario("all-vacuous", [S("goto", route="/"), S("expect_visible", T("heading", "Items")),
                                 S("expect_text", T("paragraph", has_text="No items"), "no items yet")])
    results, candidates = validate_on_source([s], OPS, app, runs=2, timeouts=FAST)
    assert all(r.passed for r in results) and candidates == []
    [v] = verdicts([s], results, 2)
    assert v.verdict == "vacuous"


async def test_expect_on_missing_element_is_an_assertion_failure(app):
    s = scenario("missing", [S("goto", route="/"), S("click", T("button", "New item")),
                             S("expect_visible", T("button", "Nowhere"))])
    [r] = await run(app, [s])
    assert not r.passed and r.failure_kind == "assertion" and r.failed_step == 2
    assert 'button "Nowhere"' in r.message


async def test_click_on_missing_element_is_an_action_failure(app):
    s = scenario("no-button", [S("goto", route="/"), S("click", T("button", "Launch rockets"))])
    [r] = await run(app, [s])
    assert not r.passed and r.failure_kind == "action" and r.failed_step == 1
    assert "not found" in r.message


async def test_ambiguous_target_is_an_action_failure(app):
    s = scenario("ambiguous", [S("goto", route="/"), S("click", T("button", "Twin"))])
    [r] = await run(app, [s])
    assert not r.passed and r.failure_kind == "action" and r.failed_step == 1
    assert "ambiguous" in r.message


async def test_failed_effect_is_an_assertion_at_len_steps(app):
    s = scenario("effect", create_steps(), binds=[uniq("name")], captures=[ITEM_ID],
                 effects=[Effect(operation="listItems", path_params=[], field="name", value="{{name}}", present=False)])
    [r] = await run(app, [s])
    assert not r.passed and r.failure_kind == "assertion" and r.failed_step == len(s.steps)
    assert "absent" in r.message


async def test_mutant_fails_at_an_assertion_after_the_first_write(mutant_app):
    s = scenario("create", create_steps(), binds=[uniq("name")], captures=[ITEM_ID])
    [r] = await run(mutant_app, [s])
    assert not r.passed and r.failure_kind == "assertion"
    assert r.first_write_step == 3 and r.failed_step == 4 > r.first_write_step
    assert r.captured and r.captured[0].name == "item_id"  # the mutant still answers 201 with an id


@pytest.mark.parametrize("route, kind", [
    ("/status/403", "env"), ("/status/401", "env"), ("/status/429", "env"),
    ("/status/503", "startup"), ("/challenge", "env"),
])
async def test_environment_failures_are_classified(app, route, kind):
    [r] = await run(app, [scenario("env", [S("goto", route=route), S("expect_visible", T("heading", "Items"))])])
    assert not r.passed and r.failure_kind == kind and r.failed_step == 0


async def test_later_page_load_5xx_is_not_startup(app):
    s = scenario("later", [S("goto", route="/"), S("goto", route="/status/500")])
    [r] = await run(app, [s])
    assert r.failure_kind == "action" and r.failed_step == 1


async def test_unreachable_app_is_a_startup_failure():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    [r] = await run(f"http://127.0.0.1:{port}", [scenario("down", [S("goto", route="/")])])
    assert not r.passed and r.failure_kind == "startup" and r.failed_step == 0


async def test_offorigin_requests_are_recorded_not_blocked(app):
    s = scenario("ping", [S("goto", route="/"), S("click", T("button", "Ping elsewhere")),
                          S("expect_visible", T("heading", "Items"))])
    [r] = await run(app, [s])
    assert r.passed, r.message
    other = app.replace("127.0.0.1", "localhost")
    assert r.offorigin_requests == [f"{other}/api/ping"]
    [r2] = await run(app, [s], allowed=[app, other])
    assert r2.offorigin_requests == []


async def test_captured_values_drive_routes_and_expect_url(app):
    s = scenario("detail", create_steps() + [
        S("click", T("link", "{{name}}")),
        S("expect_url", value="/items/{{captured.item_id}}"),
        S("expect_url", value="/items/{item_id}"),
        S("expect_visible", T("heading", "{{name}}")),
        S("goto", route="/"),
        S("goto", route="/items/{{captured.item_id}}"),
        S("reread"),
        S("expect_visible", T("heading", "{{name}}")),
        S("expect_url", value="/items/{{captured.item_id}}"),
    ], binds=[uniq("name")], captures=[ITEM_ID])
    [r] = await run(app, [s])
    assert r.passed, r.message


async def test_setup_scenarios_share_bindings_and_context(app):
    setup = scenario("make-item", create_steps(), binds=[uniq("name")], captures=[ITEM_ID])
    main = scenario("delete-item", [
        S("click", T("button", "Delete", within=("listitem", None, "{{name}}"))),
        S("expect_hidden", T("link", "{{name}}")),
        S("reread"),
        S("expect_hidden", T("link", "{{name}}")),
    ], binds=[uniq("name")], setup=["make-item"],
        effects=[Effect(operation="getItem", path_params=[NameValue(name="item_id", value="{{captured.item_id}}")],
                        field="id", value="{{captured.item_id}}", present=False)])
    results = await run(app, [main, setup])
    by_id = {r.scenario_id: r for r in results}
    assert by_id["delete-item"].passed, by_id["delete-item"].message
    assert by_id["delete-item"].first_write_step == 0  # the setup's write does not count
    assert by_id["make-item"].passed


async def test_select_check_and_expect_value(app):
    s = scenario("controls", [
        S("goto", route="/"),
        S("select", T("combobox", "Kind"), "Beta"),
        S("expect_value", T("combobox", "Kind"), "Beta"),
        S("expect_value", T("combobox", "Kind"), "b"),
        S("check", T("checkbox", "Agree")),
        S("expect_text", T("status"), "agreed"),
        S("uncheck", T("checkbox", "Agree")),
        S("click", T("button", "New item")),
        S("fill", T("textbox", "Name", within=DIALOG), "typed"),
        S("expect_value", T("textbox", "Name", within=DIALOG), "typed"),
        S("press", T("textbox", "Name", within=DIALOG), "Escape"),
        S("expect_hidden", T("dialog", "New item")),
    ])
    [r] = await run(app, [s])
    assert r.passed, r.message
    bad = scenario("bad-option", [S("goto", route="/"), S("select", T("combobox", "Kind"), "Gamma")])
    [r] = await run(app, [bad])
    assert r.failure_kind == "action" and "Gamma" in r.message


async def test_native_dialogs_are_accepted(app):
    s = scenario("confirm", [S("goto", route="/"), S("click", T("button", "Confirm me")),
                             S("expect_text", T("status"), "confirmed")])
    [r] = await run(app, [s])
    assert r.passed, r.message


async def test_scope_by_contained_text_without_a_container_role(app):
    s = scenario("delete-by-text", create_steps("a") + [
        S("click", T("button", "New item")),
        S("fill", T("textbox", "Name", within=DIALOG), "{{b}}"),
        S("click", T("button", "Create", within=DIALOG)),
        S("expect_visible", T("link", "{{b}}")),
        # Two rows have a "Delete" button; the innermost element containing {{a}} picks the right one.
        S("click", T("button", "Delete", within=(None, None, "{{a}}"))),
        S("expect_hidden", T("link", "{{a}}")),
        S("expect_visible", T("link", "{{b}}")),
    ], binds=[uniq("a"), uniq("b")])
    [r] = await run(app, [s])
    assert r.passed, r.message


async def test_scenario_errors_fail_without_running(app):
    s = scenario("typo", [S("goto", route="/"), S("expect_visible", T("link", "{{nmae}}"))], binds=[uniq("name")])
    [r] = await run(app, [s])
    assert not r.passed and r.failure_kind == "action" and r.failed_step == 1 and "not a declared bind" in r.message
    s2 = scenario("bad-fixture", [S("goto", route="/")], binds=[Bind(name="who", kind="fixture", value="nobody")])
    [r2] = await run(app, [s2])
    assert r2.failure_kind == "env" and "fixture" in r2.message


@pytest.mark.parametrize("channel", ["chrome", "no-such-browser"])
async def test_browser_channels_with_fallback_to_bundled_chromium(app, channel):
    from forge.core.config import browser_channel

    if channel == "chrome" and browser_channel() != "chrome":
        pytest.skip("Google Chrome is not installed on this host")
    s = scenario("create", create_steps(), binds=[uniq("name")], captures=[ITEM_ID])
    [r] = await run(app, [s], channel=channel)
    assert r.passed, r.message


async def test_runs_are_ordered_and_use_fresh_bindings(app):
    s = scenario("create", create_steps(), binds=[uniq("name")], captures=[ITEM_ID])
    results = await run(app, [s], runs=3, parallel=3)
    assert [r.run for r in results] == [1, 2, 3] and all(r.passed for r in results)
    assert len({r.captured[0].value for r in results}) == 3


# ---------- the demo ("the original") ----------
def demo_running() -> bool:
    try:
        with urllib.request.urlopen(f"{DEMO}/api/projects", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


needs_demo = pytest.mark.skipif(not demo_running(),
                                reason="demo not running: docker compose -f demo/issue-tracker/compose.yaml up -d")


def demo_spec() -> AppSpecDraft:
    return AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())


def demo_scenarios() -> list[Scenario]:
    heldout = sorted((ROOT / "demo/issue-tracker/heldout").glob("*.json"))
    files = [ROOT / "demo/issue-tracker/scenarios/pilot.json", *heldout]
    return [Scenario.model_validate_json(f.read_text()) for f in files]


@needs_demo
def test_demo_pilot_and_heldout_pass_three_of_three():
    scenarios = demo_scenarios()
    results, candidates = validate_on_source(scenarios, op_wires(demo_spec()), DEMO, runs=3, parallel=3)
    failures = [f"{r.scenario_id}#{r.run}: {r.failure_kind} at {r.failed_step}: {r.message}"
                for r in results if not r.passed]
    assert not failures, failures
    assert len(results) == 18
    assert candidates == [s.id for s in scenarios]
    for r in results:
        assert r.first_write_step == 3 and r.offorigin_requests == []
    pilot = [r for r in results if r.scenario_id == "pilot-issue-lifecycle"]
    assert all(r.vacuous_assertions == [23, 25] and r.non_vacuous_assertions == 8 for r in pilot)


@needs_demo
async def test_demo_vacuous_scenario_is_recorded():
    s = scenario("demo-vacuous", [
        S("goto", route="/"),
        S("expect_visible", T("heading", "Projects")),
        S("click", T("button", "New project")),
        S("click", T("button", "Cancel", within=("dialog", "New project", None))),
        S("expect_visible", T("heading", "Projects")),
    ])
    [r] = await run(DEMO, [s], ops=op_wires(demo_spec()))
    assert r.passed, r.message
    assert r.vacuous_assertions == [1, 4] and r.non_vacuous_assertions == 0


@needs_demo
async def test_demo_failure_kinds():
    ops = op_wires(demo_spec())
    missing = scenario("demo-missing", [S("goto", route="/"), S("click", T("button", "New project")),
                                        S("expect_visible", T("heading", "Archived projects"))])
    no_button = scenario("demo-no-button", [S("goto", route="/"), S("click", T("button", "Archive everything"))])
    r1, r2 = await run(DEMO, [missing, no_button], ops=ops, parallel=2)
    assert (r1.failure_kind, r1.failed_step) == ("assertion", 2)
    assert (r2.failure_kind, r2.failed_step) == ("action", 1)


@needs_demo
async def test_demo_captures_and_effects():
    ops = op_wires(demo_spec())
    pilot = demo_scenarios()[0]
    [r] = await run(DEMO, [pilot], ops=ops, timeouts=Timeouts())
    assert r.passed, r.message
    captured = {c.name: c.value for c in r.captured}
    assert set(captured) == {"project_id", "issue_id"} and all(v.isdigit() for v in captured.values())
    with urllib.request.urlopen(f"{DEMO}/api/projects/{captured['project_id']}") as resp:
        assert resp.status == 200  # the capture is the real id on the source
    # An effect that cannot hold fails as an assertion after the last step.
    wrong = pilot.model_copy(update={"id": "pilot-wrong-effect", "effects": [
        Effect(operation="listProjects", path_params=[], field="name", value="{{project}}", present=False)]})
    [r2] = await run(DEMO, [wrong], ops=ops, timeouts=Timeouts())
    assert not r2.passed and r2.failure_kind == "assertion" and r2.failed_step == len(pilot.steps)
