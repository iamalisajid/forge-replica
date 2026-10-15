"""Specify stage: three structured calls (data, UI, scenarios) assembled into an AppSpec, then integrity-checked.

One call per part keeps each schema small enough for structured outputs (the whole AppSpec schema is rejected
as too large), and lets a failing part be retried with its own errors.
"""
from __future__ import annotations

import hashlib
import json
import re

from forge.core.config import REPO_ROOT
from forge.core.llm import structured_call
from forge.core.store import RunStore
from forge.models.appspec import (AppSpec, DataSpecDraft, PathParam, ScenarioDraft, SpecOperation, UiSpecDraft,
                                  assemble)
from forge.models.base import ArtifactHeader, NameValue
from forge.models.observed import ObservedModel
from forge.models.trace import Trace
from forge.stages.extract.rest import camel
from forge.stages.specify.integrity import check as integrity_check


def check(spec) -> list[str]:
    """Integrity checks plus everything the renderer would refuse (prefixed so retries target the data part)."""
    errors = integrity_check(spec)
    try:
        from forge.stages.specify.render import validate as render_validate

        errors += [e if e.startswith(("entity", "field", "operation", "screen", "element")) else f"operation: {e}"
                   for e in render_validate(spec)]
    except ImportError:
        pass
    return errors

OP_ERROR = re.compile(r"^operation '([^']+)'")
ELEMENT_ERROR = re.compile(r"^element '([^']+)'")


def complete(spec):
    """A detail screen loads its record by id. Sync-style APIs only expose list reads, so when a detail screen's
    entity has a list operation but no get, add a get-by-id on the replica's own API (same source as the list)."""
    have = {o.entity for o in spec.operations if o.kind == "get"}
    taken = {o.id for o in spec.operations} | {(o.method, o.path) for o in spec.operations}
    added = []
    for ent in sorted({sc.entity for sc in spec.screens if sc.kind == "detail" and sc.entity} - have):
        lst = next((o for o in spec.operations if o.entity == ent and o.kind == "list" and not o.path_params), None)
        if lst is None:
            continue
        op_id, path = camel("get", ent), lst.path.rstrip("/") + "/{id}"
        if op_id in taken or ("GET", path) in taken:
            continue
        added.append(SpecOperation(
            id=op_id, entity=ent, kind="get", method="GET", path=path, path_params=[PathParam(name="id", entity=ent)],
            request_fields=[], response="object", list_key=None, success_status=200, action_sets=[], action_note=None,
            source_method=lst.source_method, source_path=lst.source_path, basis="inferred", evidence=lst.evidence))
    if not added:
        return spec
    return spec.model_copy(update={"operations": spec.operations + added, "assumptions": spec.assumptions + [
        f"{o.id}: get-by-id added for the {o.entity} detail screen (the original only exposed list reads)"
        for o in added]})


def repair(spec, errors: list[str], *, degrade: bool = False):
    """Fix what needs no model. An operation the renderer cannot express (e.g. a GET labelled as an action) that no
    element, capture or effect uses is left out. With ``degrade`` (last resort, after the model's retry), an element
    the renderer cannot wire is kept as static text with its role and name, for the builder to wire. Every change is
    recorded in ``unsupported`` so it shows in the report and reaches the builder."""
    used = {e.operation for e in spec.elements if e.operation}
    for sc in spec.scenarios:
        used |= {c.operation for c in sc.captures} | {f.operation for f in sc.effects}
    bad_ops: dict[str, str] = {}
    bad_elements: dict[str, str] = {}
    for err in errors:
        reason = err.split(": ", 1)[-1]
        if (m := OP_ERROR.match(err)) and m.group(1) not in used:
            bad_ops.setdefault(m.group(1), reason)
        elif degrade and (m := ELEMENT_ERROR.match(err)):
            bad_elements.setdefault(m.group(1), reason)
    if not bad_ops and not bad_elements:
        return spec
    notes = [f"{o.method} {o.path} ({o.id}) left out: {bad_ops[o.id]}" for o in spec.operations if o.id in bad_ops]
    elements = []
    for e in spec.elements:
        if e.id in bad_elements:
            notes.append(f"{e.role} '{e.name}' ({e.id}, screen {e.screen}) is static in the generated UI: "
                         f"{bad_elements[e.id]}; the builder wires it")
            e = e.model_copy(update={"purpose": "text", "operation": None, "field": None, "target_screen": None})
        elements.append(e)
    return spec.model_copy(update={"operations": [o for o in spec.operations if o.id not in bad_ops],
                                   "elements": elements, "unsupported": spec.unsupported + notes})


def settle(spec, errors: list[str], *, degrade: bool):
    """Repair and re-check until nothing changes (the renderer reports later problems once earlier ones are gone)."""
    for _ in range(4):
        if not errors:
            break
        fixed = repair(spec, errors, degrade=degrade)
        if fixed == spec:
            break
        spec, errors = fixed, check(fixed)
    return spec, errors


PROMPT_FILE = REPO_ROOT / "forge" / "prompts" / "specify.md"
NOISE = re.compile(r" \[(?:ref=[^\]]+|cursor=pointer|active)\]")
SNAPSHOT_CHARS = 7000

DATA_TASK = """Write the DATA part of the spec from the observed model below.
- One entity per observed entity (skip ones with no fields). Copy fields with their exact names; map JSON types to the field types (ISO date-time strings -> datetime, enum values -> enum, foreign keys -> reference).
- Mark generated fields: "id" -> generated "id", server-set creation/update timestamps -> "created_at"/"updated_at".
- One operation per observed operation with the same method and path (source_method/source_path identical). Copy search_param for list operations that have one (the query parameter that filters the list by text). For actions, put the observable effect in action_sets when the response shows a field changing to a fixed value (e.g. status=closed); otherwise describe it in action_note.
- auth: "session" only if the explorer had to sign in; otherwise "none". owned_by_user: true only for per-user data when auth is "session".
- app_name: a short name for the app."""

UI_TASK = """Write the UI part of the spec (screens and their elements) for the data spec and the screen snapshots below.
- One screen per observed route template, with react-router syntax (":project_id"), its main entity and kind.
- Elements in visual order, with exact roles and accessible names from the snapshots. Use purposes:
  heading (name, or field for a record title), navigate (links; field for row links bound to a record, target_screen),
  open_create (button that opens a create dialog/form; operation = the create operation), container (dialog or form; for a create dialog set parent = its open_create button; for an edit form set operation = the update operation),
  field (textbox/combobox/checkbox inside a container; field = entity field name), submit_create / submit_update (operation), cancel,
  list (table or list with its accessible name; operation = the list operation), list_row (row or listitem; parent = the list),
  text (static text or a row cell bound to a field), action (custom action buttons; operation), delete (operation; target_screen when deleting navigates away),
  status_message (alert/status messages; name = the success text such as "Saved", empty for error alerts).
- Row parts (links, cells, buttons) have parent = the list_row element."""

SCENARIO_TASK = """Write the acceptance SCENARIOS for the spec, based on what the explorer actually did (steps below).
- pilot_scenario: one scenario for the pilot feature "{pilot}" covering create -> reload -> edit -> delete. The edit must change the record's name or title (the main text typed when creating it) and its description if it has one, then assert the new text after a reload; changing attributes such as priority or dates may be added, but does not replace editing the text. Build it from the explorer's recorded steps for that feature: reuse the same targets (role, name, scope) in the same order, replacing typed values with binds. Do not invent a different path through the UI. If its records live under a parent, create the parent inside the same scenario first, the way the explorer did.
{search_rule}- Add one scenario for each other behavior the explorer exercised (each custom action, validation of a required field, deleting a parent).
- Steps use targets with exact role + accessible name from the UI spec. Scope ambiguous elements: buttons inside a dialog -> within_role "dialog", within_name = the dialog name; row buttons -> within_role "row" or "listitem" with within_has_text = a bound value in that row.
- Every typed value comes from a bind (kind uniq_text) referenced as {{{{name}}}}; never use literal values the explorer typed.
- After each write, assert on the bound value (expect_visible a link or heading named {{{{name}}}}), then "reread" and assert again. A delete is checked with expect_hidden after a reread.
- captures: the ids of records you create (operation = the create operation id, field "id"), used as {{{{captured.name}}}} in routes or effects.
- effects: confirm the final state through a list operation (present true/false) with path params from captures.
- goto routes are paths like "/" or "/projects/{{{{captured.project_id}}}}". Every scenario starts with a goto.
- If an operation's source_path is shared by several operations (an RPC or sync endpoint), do not use it in captures or effects; rely on UI assertions and rereads, and navigate by clicking links instead of routes with captured ids."""


def _clean_snapshot(text: str) -> str:
    return NOISE.sub("", text)[:SNAPSHOT_CHARS]


def _screens_context(store: RunStore, observed: ObservedModel) -> str:
    parts = []
    for route in observed.routes:
        body = ""
        if route.snapshot:
            try:
                body = _clean_snapshot(store.blob_path(route.snapshot).read_text())
            except FileNotFoundError:
                body = "(snapshot missing)"
        parts.append(f"### Route {route.template} (e.g. {', '.join(route.examples)})\n{body}")
    return "\n\n".join(parts)


def _steps_context(trace: Trace) -> str:
    by_id = {x.id: x for x in trace.exchanges}
    lines = []
    for s in trace.steps:
        t = s.target
        target = ""
        if t:
            target = f'{t.role} "{t.name or ""}"' + ("" if t.exact else " (partial name)")
            if t.within_role and t.within_name:
                target += f' within {t.within_role} named "{t.within_name}"'
            elif t.within_role:
                target += f' within {t.within_role} containing text "{t.within_has_text or ""}"'
            if not s.portable:
                target += " [recorded target was not unique on the page]"
        calls = [f"{by_id[i].method} {by_id[i].path}"
                 + ("?" + "&".join(f"{q.name}={q.value[:24]}" for q in by_id[i].query) if by_id[i].query else "")
                 + f" {by_id[i].status}" for i in s.exchange_ids
                 if i in by_id and by_id[i].resource_type in ("xhr", "fetch")]
        lines.append(f"{s.id} {s.action} {target} value={s.value!r} url={s.url_after} "
                     f"{'calls=' + '; '.join(calls) if calls else ''}{' ERROR ' + s.error if s.error else ''}")
    return "\n".join(lines)


SEARCH_RULE = """- Search (the user asked for it): if the explorer searched (a search box, a search dialog or a filter field in the steps below), the pilot scenario also searches, right after the reload: open the search the same way, fill the search input with the record's bound name ({{name}}), expect the record in the results with the role and name the explorer saw, then close the search the same way.
"""


def specify(store: RunStore, observed: ObservedModel, trace: Trace, *, run_id: str, model: str,
            pilot_feature: str | None, features: list[str] | None = None) -> AppSpec:
    search_rule = SEARCH_RULE if "search" in (features or []) else ""
    failed = store.dir / "artifacts" / "appspec.errors.json"
    if failed.exists() and store.has("appspec"):
        prior = store.read("appspec", AppSpec)
        inputs = {n.name: n.value for n in prior.header.inputs}
        if inputs == {"observed": store.sha("observed"), "trace": store.sha("trace")}:
            spec, errors = settle(complete(prior.spec), check(complete(prior.spec)), degrade=True)
            if not errors:
                failed.unlink()
                return _store(store, spec, errors, run_id=run_id, model=model)
    system = PROMPT_FILE.read_text()
    observed_json = observed.model_dump_json(exclude={"header"})
    screens = _screens_context(store, observed)
    steps = _steps_context(trace)
    ledger = store.ledger

    efforts = {DataSpecDraft: "high", UiSpecDraft: "medium", ScenarioDraft: "medium"}

    def call(task: str, context: str, output, errors: list[str]):
        content = task + "\n\n" + context
        if errors:
            content += "\n\nYour previous answer failed these checks; fix them:\n- " + "\n- ".join(errors[:40])
        return structured_call(stage="specify", system=system, content=content, output=output, model=model,
                               ledger=ledger, effort=efforts[output]).parsed

    data = call(DATA_TASK, f"## Observed model\n{observed_json}", DataSpecDraft, [])
    data_json = data.model_dump_json()
    ui = call(UI_TASK, f"## Data spec\n{data_json}\n\n## Screen snapshots\n{screens}", UiSpecDraft, [])
    ui_json = ui.model_dump_json()
    pilot = pilot_feature or (data.entities[0].name if data.entities else "main")
    scenario_context = f"## Data spec\n{data_json}\n\n## UI spec\n{ui_json}\n\n## Explorer steps\n{steps}"
    scenarios = call(SCENARIO_TASK.format(pilot=pilot, search_rule=search_rule), scenario_context, ScenarioDraft, [])

    spec = complete(assemble(data, ui, scenarios))
    spec, errors = settle(spec, check(spec), degrade=False)
    if errors:
        data_errors = [e for e in errors if e.startswith(("entity", "field", "operation"))]
        ui_errors = [e for e in errors if e.startswith(("screen", "element"))]
        sc_errors = [e for e in errors if e not in data_errors and e not in ui_errors]
        if data_errors:
            data = call(DATA_TASK, f"## Observed model\n{observed_json}", DataSpecDraft, data_errors)
        if ui_errors or data_errors:
            ui = call(UI_TASK, f"## Data spec\n{data.model_dump_json()}\n\n## Screen snapshots\n{screens}",
                      UiSpecDraft, ui_errors)
        if sc_errors or ui_errors or data_errors:
            scenarios = call(SCENARIO_TASK.format(pilot=pilot, search_rule=search_rule),
                             f"## Data spec\n{data.model_dump_json()}\n\n## UI spec\n{ui.model_dump_json()}\n\n"
                             f"## Explorer steps\n{steps}", ScenarioDraft, sc_errors)
        spec = complete(assemble(data, ui, scenarios))
        spec, errors = settle(spec, check(spec), degrade=False)
        spec, errors = settle(spec, errors, degrade=True)
    return _store(store, spec, errors, run_id=run_id, model=model)


def _store(store: RunStore, spec, errors: list[str], *, run_id: str, model: str) -> AppSpec:
    header = ArtifactHeader(
        schema_id="forge.appspec/1", run_id=run_id, stage="specify", model=model,
        prompt_sha=hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest()[:16],
        inputs=[NameValue(name="observed", value=store.sha("observed")), NameValue(name="trace", value=store.sha("trace"))])
    result = AppSpec(header=header, spec=spec)
    store.write("appspec", result)
    if errors:
        (store.dir / "artifacts" / "appspec.errors.json").write_text(json.dumps(errors, indent=1))
        raise SpecIntegrityError(errors)
    return result


class SpecIntegrityError(RuntimeError):
    def __init__(self, errors: list[str]):
        super().__init__(f"{len(errors)} integrity errors, e.g. {errors[:3]}")
        self.errors = errors
