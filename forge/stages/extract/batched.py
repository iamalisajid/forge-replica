"""Batched-command adapter: sync APIs that send ``{"commands": [{"type": "item_add", "args": {...}}]}`` to one
endpoint and return resource arrays (``items``, ``projects``, ``labels``...). Deterministic, like the REST adapter:
command types become operations, response arrays become entities, ``<x>_id`` fields become foreign keys.
"""
from __future__ import annotations

import json
from collections import defaultdict

from forge.models.observed import ObservedEntity, ObservedField, ObservedModel, ObservedOperation
from forge.models.trace import Trace
from forge.stages.extract.rest import _format, _json_type, camel, singular

VERB_KIND = {"add": "create", "create": "create", "update": "update", "edit": "update", "delete": "delete",
             "remove": "delete"}
SKIP_ARGS = {"id", "temp_id", "uuid"}


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def is_batched(body) -> bool:
    return isinstance(body, dict) and isinstance(body.get("commands"), list)


def adapt(trace: Trace, observed: ObservedModel, select_options: dict[str, list[str]] | None = None) -> ObservedModel:
    commands: dict[str, list[tuple[str, dict]]] = defaultdict(list)     # type -> [(exchange id, args)]
    rows: dict[str, list[dict]] = defaultdict(list)                     # entity -> objects seen in responses
    endpoint: dict[str, tuple[str, str]] = {}                           # type -> (method, path)
    sync_ids: list[str] = []
    for ex in trace.exchanges:
        body = _json(ex.request_body)
        if not is_batched(body):
            continue
        sync_ids.append(ex.id)
        for cmd in body["commands"]:
            if isinstance(cmd, dict) and isinstance(cmd.get("type"), str):
                commands[cmd["type"]].append((ex.id, cmd.get("args") or {}))
                endpoint.setdefault(cmd["type"], (ex.method, ex.path))
        resp = _json(ex.response_body)
        if isinstance(resp, dict):
            for key, value in resp.items():
                if isinstance(value, list) and value and isinstance(value[0], dict) and "id" in value[0]:
                    rows[singular(key)] += value
    if not sync_ids:
        return observed

    known_entities = set(rows) | {t.split("_", 1)[0] for t in commands}
    operations: list[ObservedOperation] = []
    create_args: dict[str, set[str]] = defaultdict(set)
    for ctype, uses in sorted(commands.items()):
        entity, _, verb = ctype.partition("_")
        kind = VERB_KIND.get(verb, "action")
        args = sorted({k for _, a in uses for k in a if k not in SKIP_ARGS})
        if kind == "create":
            create_args[entity] |= set(args)
        method, path = endpoint[ctype]
        operations.append(ObservedOperation(
            id=camel(verb or "do", entity), kind=kind, method=method, path_template=f"{path}#{ctype}",
            path_params=[], entity=entity, request_fields=args, response="object", list_key=None,
            success_statuses=[200], error_statuses=[], triggered_by=[],
            evidence=sorted({i for i, _ in uses})))
    sync_path = next(ex.path for ex in trace.exchanges if ex.id == sync_ids[0])
    for entity in sorted(rows):
        operations.append(ObservedOperation(
            id=camel("list", entity + "s"), kind="list", method="POST", path_template=f"{sync_path}#read:{entity}",
            path_params=[], entity=entity, request_fields=[], response="list", list_key=None,
            success_statuses=[200], error_statuses=[], triggered_by=[], evidence=sync_ids[:5]))

    entity_ids = {e: {str(r.get("id")) for r in rs} for e, rs in rows.items()}
    entities: list[ObservedEntity] = []
    options = {k.lower().replace(" ", "_"): v for k, v in (select_options or {}).items()}
    for entity, objs in sorted(rows.items()):
        values: dict[str, list] = defaultdict(list)
        for obj in objs:
            for k, v in obj.items():
                values[k].append(v)
        fields = []
        for name, vals in values.items():
            types = {_json_type(v) for v in vals if v is not None}
            jtype = types.pop() if len(types) == 1 else ("number" if types == {"integer", "number"} else "string")
            ref = None
            if name.endswith("_id") and name != "id":
                target = name[:-3]
                target = entity if target == "parent" else target      # parent_id: a tree of the same entity
                if target in known_entities and {str(v) for v in vals if v is not None} & entity_ids.get(target, set()):
                    ref = target
            fields.append(ObservedField(
                name=name, json_type=jtype, format=_format(vals), nullable=any(v is None for v in vals),
                in_create_request=name in create_args.get(entity, set()),
                server_generated=name not in create_args.get(entity, set()),
                enum_values=options.get(name, []), required="unknown", references=ref, samples=len(vals),
                evidence=sync_ids[:3]))
        entities.append(ObservedEntity(name=entity, id_field="id",
                                       id_type="integer" if all(str(r.get("id")).isdigit() for r in objs) else "string",
                                       fields=fields, evidence=sync_ids[:5]))

    batched_paths = {ex.path for ex in trace.exchanges if ex.id in sync_ids}
    keep_unknowns = [u for u in observed.unknowns
                     if not any(u.startswith(f"RPC/GraphQL-style body at POST {p} ") for p in batched_paths)]
    merged = observed.model_copy(update={
        "entities": [e for e in observed.entities if e.name not in rows] + entities,
        "operations": observed.operations + operations,
        "unknowns": keep_unknowns + [f"batched commands interpreted deterministically: {', '.join(sorted(commands))}"]})
    return merged
