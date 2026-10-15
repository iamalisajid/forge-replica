"""Interpreter fallback for traffic the REST adapter can't parse (GraphQL, batched RPC, sync commands).

One structured call reads compacted samples and returns operations whose every field cites an exchange id and
a JSON path. Code then resolves each citation in the recorded exchange; a field whose citation doesn't resolve
is rejected, and an operation without a single verified citation is dropped. Nothing unverified gets through.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from forge.core.llm import structured_call
from forge.core.store import RunStore
from forge.models.base import Strict
from forge.models.observed import ObservedEntity, ObservedField, ObservedModel, ObservedOperation
from forge.models.trace import Exchange, Trace

PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class Citation(Strict):
    exchange_id: str
    side: Literal["request", "response"]
    json_path: str              # dot path with [n] indexes, e.g. "data.createIssue.title" or "commands[0].args.content"


class InterpretedField(Strict):
    name: str
    json_type: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    citation: Citation


class InterpretedOperation(Strict):
    id: str
    kind: Literal["list", "get", "create", "update", "delete", "action"]
    entity: str
    method: str
    path: str
    discriminator: str | None   # operationName, command type, etc.
    request_fields: list[InterpretedField]
    response_fields: list[InterpretedField]
    evidence: list[Citation]


class Interpretation(Strict):
    operations: list[InterpretedOperation]
    notes: list[str]


SYSTEM = """You interpret recorded API traffic that is not plain REST (GraphQL, batched commands, RPC) into logical
operations on entities. Every field you report must cite the exact exchange id, side and JSON path where you saw it.
Only report what the samples show."""


def _normalise(path: str) -> str:
    path = path.strip()
    path = re.sub(r"^\$\.?", "", path)                     # "$.a.b" / "$a"
    path = re.sub(r"^(request|response)(\.body)?\.", "", path)
    path = re.sub(r"\[['\"]([^'\"]+)['\"]\]", r".\1", path)   # a['b'] -> a.b
    return path.lstrip(".")


def resolve(data, path: str):
    path = _normalise(path)
    node = data
    for name, index in PATH_TOKEN.findall(path):
        if name:
            if not isinstance(node, dict) or name not in node:
                raise KeyError(path)
            node = node[name]
        else:
            if not isinstance(node, list) or int(index) >= len(node):
                raise KeyError(path)
            node = node[int(index)]
    return node


def _body(ex: Exchange, side: str):
    text = ex.request_body if side == "request" else ex.response_body
    return json.loads(text) if text else None


def verify(interp: Interpretation, exchanges: dict[str, Exchange]) -> tuple[list[InterpretedOperation], int]:
    rejected = 0
    kept: list[InterpretedOperation] = []
    for op in interp.operations:
        def ok(c: Citation) -> bool:
            ex = exchanges.get(c.exchange_id)
            if ex is None:
                return False
            try:
                resolve(_body(ex, c.side), c.json_path)
                return True
            except (KeyError, ValueError, TypeError):
                return False

        req = [f for f in op.request_fields if ok(f.citation)]
        resp = [f for f in op.response_fields if ok(f.citation)]
        evidence = [c for c in op.evidence if ok(c)]
        rejected += (len(op.request_fields) - len(req)) + (len(op.response_fields) - len(resp)) \
            + (len(op.evidence) - len(evidence))
        if evidence or req or resp:
            kept.append(op.model_copy(update={"request_fields": req, "response_fields": resp, "evidence": evidence}))
    return kept, rejected


def interpret(store: RunStore, trace: Trace, observed: ObservedModel, *, model: str) -> tuple[ObservedModel, int]:
    """Interpret exchanges listed in observed.unknowns and merge verified operations into the model."""
    ids = re.findall(r"\[(x\d+)\]", "\n".join(observed.unknowns))
    exchanges = {x.id: x for x in trace.exchanges}
    samples = [exchanges[i] for i in ids if i in exchanges]
    if not samples:
        return observed, 0
    lines, budget = [], 400_000
    for x in samples:
        line = json.dumps({"id": x.id, "method": x.method, "path": x.path, "status": x.status,
                           "request": (x.request_body or "")[:8000], "response": (x.response_body or "")[:16000]})
        if budget - len(line) < 0:
            break
        budget -= len(line)
        lines.append(line)
    content = "Recorded exchanges (bodies truncated):\n" + "\n".join(lines)
    interp = structured_call(stage="extract", system=SYSTEM, content=content, output=Interpretation, model=model,
                             ledger=store.ledger).parsed
    (store.dir / "artifacts" / "interpretation.json").write_text(interp.model_dump_json(indent=1))
    kept, rejected = verify(interp, exchanges)
    operations = list(observed.operations)
    entities = {e.name: e for e in observed.entities}
    for op in kept:
        operations.append(ObservedOperation(
            id=op.id, kind=op.kind, method=op.method, path_template=op.path, path_params=[], entity=op.entity,
            request_fields=[f.name for f in op.request_fields], response="object", list_key=None,
            success_statuses=[], error_statuses=[], triggered_by=[],
            evidence=sorted({c.exchange_id for c in op.evidence})))
        fields = {f.name: f for f in (entities[op.entity].fields if op.entity in entities else [])}
        for f in op.response_fields + op.request_fields:
            fields.setdefault(f.name, ObservedField(
                name=f.name, json_type=f.json_type, format="none", nullable=False,
                in_create_request=f in op.request_fields, server_generated=False, enum_values=[],
                required="unknown", references=None, samples=1, evidence=[f.citation.exchange_id]))
        entities[op.entity] = ObservedEntity(name=op.entity, id_field="id", id_type="string",
                                             fields=list(fields.values()),
                                             evidence=sorted({c.exchange_id for c in op.evidence}))
    merged = observed.model_copy(update={
        "operations": operations, "entities": list(entities.values()),
        "unknowns": observed.unknowns + ([f"interpreter: {rejected} citations rejected"] if rejected else [])})
    store.write("observed", merged)
    return merged, rejected
