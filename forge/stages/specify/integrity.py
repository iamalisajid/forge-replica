"""Deterministic integrity checks over an assembled AppSpec: every reference must resolve."""
from __future__ import annotations

import re

from forge.models.appspec import AppSpecDraft

PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
BINDING = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def check(spec: AppSpecDraft) -> list[str]:
    errors: list[str] = []
    entities = {e.name for e in spec.entities}
    fields = {(f.entity, f.name) for f in spec.fields}
    ops = {o.id: o for o in spec.operations}
    screens = {s.id for s in spec.screens}
    elements = {e.id: e for e in spec.elements}

    for e in spec.entities:
        if not IDENT.match(e.name):
            errors.append(f"entity {e.name!r}: name must be singular snake_case")
        if (e.name, "id") not in fields:
            errors.append(f"entity {e.name}: missing an 'id' field")
    for f in spec.fields:
        if f.entity not in entities:
            errors.append(f"field {f.entity}.{f.name}: unknown entity")
        if f.type == "reference" and f.references not in entities:
            errors.append(f"field {f.entity}.{f.name}: references unknown entity {f.references!r}")
        if f.type == "enum" and not f.enum_values:
            errors.append(f"field {f.entity}.{f.name}: enum without values")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", f.name):
            errors.append(f"field {f.entity}.{f.name}: invalid name")
    for o in spec.operations:
        if o.entity not in entities:
            errors.append(f"operation {o.id}: unknown entity {o.entity!r}")
        names = PARAM.findall(o.path)
        if sorted(names) != sorted(p.name for p in o.path_params):
            errors.append(f"operation {o.id}: path params {names} do not match path_params")
        for p in o.path_params:
            if p.entity not in entities:
                errors.append(f"operation {o.id}: path param {p.name} names unknown entity {p.entity!r}")
        for name in o.request_fields:
            if (o.entity, name) not in fields:
                errors.append(f"operation {o.id}: request field {name!r} is not a field of {o.entity}")
        for kv in o.action_sets:
            if (o.entity, kv.name) not in fields:
                errors.append(f"operation {o.id}: action sets unknown field {kv.name!r}")
        if o.response == "list_envelope" and not o.list_key:
            errors.append(f"operation {o.id}: list_envelope needs list_key")
    for s in spec.screens:
        if s.entity and s.entity not in entities:
            errors.append(f"screen {s.id}: unknown entity {s.entity!r}")
        if s.parent and s.parent not in screens:
            errors.append(f"screen {s.id}: unknown parent screen {s.parent!r}")
    for el in spec.elements:
        if el.screen not in screens:
            errors.append(f"element {el.id}: unknown screen {el.screen!r}")
        if el.parent and el.parent not in elements:
            errors.append(f"element {el.id}: unknown parent {el.parent!r}")
        if el.operation and el.operation not in ops:
            errors.append(f"element {el.id}: unknown operation {el.operation!r}")
        if el.purpose in ("submit_create", "submit_update", "delete", "action") and not el.operation:
            errors.append(f"element {el.id}: {el.purpose} needs an operation")
        if el.target_screen and el.target_screen not in screens:
            errors.append(f"element {el.id}: unknown target screen {el.target_screen!r}")
        if el.purpose == "field" and not el.field:
            errors.append(f"element {el.id}: field element needs a field name")
    ids = [s.id for s in spec.scenarios]
    if len(ids) != len(set(ids)):
        errors.append("scenario ids must be unique")
    if spec.pilot_scenario not in ids:
        errors.append(f"pilot_scenario {spec.pilot_scenario!r} is not a scenario id")
    for sc in spec.scenarios:
        bound = {b.name for b in sc.binds} | {f"captured.{c.name}" for c in sc.captures}
        for c in sc.captures:
            if c.operation not in ops:
                errors.append(f"scenario {sc.id}: capture uses unknown operation {c.operation!r}")
        for ef in sc.effects:
            if ef.operation not in ops:
                errors.append(f"scenario {sc.id}: effect uses unknown operation {ef.operation!r}")
            elif ops[ef.operation].kind not in ("list", "get"):
                errors.append(f"scenario {sc.id}: effect operation {ef.operation} must be a list or get")
        for setup in sc.setup:
            if setup not in ids:
                errors.append(f"scenario {sc.id}: unknown setup scenario {setup!r}")
        for i, step in enumerate(sc.steps):
            texts = [step.value or "", step.route or ""]
            if step.target:
                texts += [step.target.name or "", step.target.has_text or "", step.target.within_name or "",
                          step.target.within_has_text or ""]
            for text in texts:
                for name in BINDING.findall(text):
                    if name not in bound:
                        errors.append(f"scenario {sc.id} step {i}: unbound {{{{{name}}}}}")
            if step.kind in ("click", "fill", "select", "check", "uncheck", "press") and step.target is None:
                errors.append(f"scenario {sc.id} step {i}: {step.kind} needs a target")
            if step.kind == "goto" and not step.route:
                errors.append(f"scenario {sc.id} step {i}: goto needs a route")
        if sc.mutating and not any(s.kind.startswith("expect_") for s in sc.steps):
            errors.append(f"scenario {sc.id}: a data-changing scenario needs assertions")
    return errors
