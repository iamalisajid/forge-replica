"""Contract tests: fixtures round-trip, and draft schemas stay inside the structured-output subset."""
import json
from pathlib import Path

import pytest

from forge.models.appspec import AppSpecDraft, DataSpecDraft, ScenarioDraft, UiSpecDraft, op_wires
from forge.models.scenario import Scenario

ROOT = Path(__file__).resolve().parents[1]
# The models a model fills in through structured outputs (AppSpecDraft itself is assembled in code).
DRAFT_MODELS = [DataSpecDraft, UiSpecDraft, ScenarioDraft]


def load_appspec() -> AppSpecDraft:
    return AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())


def test_fixture_appspec_round_trips():
    spec = load_appspec()
    again = AppSpecDraft.model_validate(json.loads(spec.model_dump_json()))
    assert again == spec
    assert spec.pilot_scenario in {s.id for s in spec.scenarios}


def test_demo_scenarios_validate():
    files = [ROOT / "demo/issue-tracker/scenarios/pilot.json", *sorted((ROOT / "demo/issue-tracker/heldout").glob("*.json"))]
    assert len(files) == 6
    for f in files:
        Scenario.model_validate_json(f.read_text())


def test_scenario_operations_exist_in_fixture_spec():
    spec = load_appspec()
    ops = {o.id for o in spec.operations}
    for s in spec.scenarios:
        assert {c.operation for c in s.captures} <= ops
        assert {e.operation for e in s.effects} <= ops


def test_op_wires_cover_every_operation():
    spec = load_appspec()
    assert [w.id for w in op_wires(spec)] == [o.id for o in spec.operations]


def _walk(node, defs, seen_refs, path="$"):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref:
            name = ref.rsplit("/", 1)[-1]
            assert name not in seen_refs, f"recursive schema through {name} at {path}"
            _walk(defs[name], defs, seen_refs | {name}, f"{path}->{name}")
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"open object at {path}"
            props = set(node.get("properties", {}))
            assert props == set(node.get("required", [])), f"optional properties at {path}: {props - set(node.get('required', []))}"
        for key, value in node.items():
            if key != "$defs":
                _walk(value, defs, seen_refs, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, defs, seen_refs, f"{path}[{i}]")


@pytest.mark.parametrize("model", DRAFT_MODELS, ids=lambda m: m.__name__)
def test_draft_schema_is_structured_output_compatible(model):
    schema = model.model_json_schema()
    _walk(schema, schema.get("$defs", {}), frozenset())


@pytest.mark.live
@pytest.mark.parametrize("model", DRAFT_MODELS, ids=lambda m: m.__name__)
def test_api_accepts_draft_schema(model):
    """Spike 6: the Messages API compiles each draft schema (small prompt, capped output)."""
    from forge.core.llm import structured_call

    result = structured_call(
        stage="schema-check",
        system="Return a minimal but valid instance of the requested schema for a one-entity notes app.",
        content="Keep every list to at most one item.",
        output=model,
        max_tokens=6000,
        effort="low",
    )
    assert isinstance(result.parsed, model)
