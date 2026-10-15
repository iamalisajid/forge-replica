"""REST adapter: recorded JSON traffic -> ObservedModel (entities, fields, operations, routes), with evidence.

Deterministic rules, no model: path templating, merged JSON shapes, server-generated fields (in the create
response but not the request), foreign keys (name + value match), enums (select options seen in the UI),
and required fields only from negative probes the original rejected.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlparse

from forge.core.store import RunStore
from forge.models.base import ArtifactHeader
from forge.models.observed import ObservedEntity, ObservedField, ObservedModel, ObservedOperation, ObservedRoute
from forge.models.trace import Exchange, Trace
from forge.stages.explore import locators
from forge.core.store import is_sync_read

ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{20,}|"
                    r"[0-9A-HJKMNP-TV-Z]{26})$", re.I)
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def singular(word: str) -> str:
    word = word.replace("-", "_")
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def camel(*parts: str) -> str:
    words = [w for p in parts for w in re.split(r"[_\-\s]+", p) if w]
    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


def template_of(path: str) -> tuple[str, list[str]]:
    segs = [s for s in path.strip("/").split("/") if s]
    out, params = [], []
    for i, seg in enumerate(segs):
        if ID_SEG.match(seg):
            base = singular(segs[i - 1]) if i > 0 else "item"
            name = f"{base}_id"
            out.append("{" + name + "}")
            params.append(name)
        else:
            out.append(seg)
    return "/" + "/".join(out), params


def _json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _list_payload(data) -> tuple[list | None, str | None]:
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        lists = [(k, v) for k, v in data.items() if isinstance(v, list) and (not v or isinstance(v[0], dict))]
        if len(lists) == 1 and "id" not in data:
            return lists[0][1], lists[0][0]
    return None, None


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _format(values: list) -> str:
    strings = [v for v in values if isinstance(v, str)]
    if not strings:
        return "none"
    for fmt, rx in (("date-time", ISO_DATETIME), ("date", ISO_DATE), ("email", EMAIL), ("uuid", UUID)):
        if all(rx.match(v) for v in strings):
            return fmt
    if all(v.startswith(("http://", "https://")) for v in strings):
        return "uri"
    return "none"


class _Group:
    def __init__(self, method: str, template: str, params: list[str]):
        self.method, self.template, self.params = method, template, params
        self.exchanges: list[Exchange] = []

    @property
    def segments(self) -> list[str]:
        return [s for s in self.template.strip("/").split("/") if s]


def _acts_on_parent(g: "_Group", responses: list) -> bool:
    """POST /things/{id}/verb acts on that thing when the response is the same record; POST /things/{id}/items
    creates a new child. Without a response body, a plural last segment means a collection."""
    for x, resp in zip([x for x in g.exchanges if x.status and x.status < 400], responses):
        if isinstance(resp, dict) and "id" in resp:
            segs = [s for s in x.path.strip("/").split("/") if s]
            return len(segs) >= 2 and str(resp["id"]) == segs[-2]
    return not g.segments[-1].endswith("s")


SEARCH_NAMES = re.compile(r"^(q|query|search|search_query|searchterm|term|keyword|keywords|filter|text)$", re.I)


def typed_texts(trace: Trace) -> list[str]:
    """What the explorer typed: fill values, and key-by-key typing joined per field."""
    texts: list[str] = []
    buf, field = "", None
    for s in trace.steps:
        if s.action == "fill" and s.value:
            texts.append(s.value)
        elif s.action == "press" and s.value:
            target = (s.target.role, s.target.name) if s.target else None
            if target != field:
                if buf:
                    texts.append(buf)
                buf, field = "", target
            if len(s.value) == 1:
                buf += s.value
            elif s.value == "Space":
                buf += " "
            elif buf:
                texts.append(buf)
                buf = ""
    if buf:
        texts.append(buf)
    return [t.strip() for t in texts if t.strip()]


def search_param(exchanges, typed: list[str]) -> str | None:
    """The query parameter that carries a search: named like one, or carrying (a prefix of) text the user typed."""
    lowered = [t.lower() for t in typed]
    for x in exchanges:
        for q in getattr(x, "query", []) or []:
            value = (q.value or "").strip().lower()
            if not value:
                continue
            if SEARCH_NAMES.match(q.name) or (len(value) >= 3 and any(t.startswith(value) for t in lowered)):
                return q.name
    return None


def extract(store: RunStore, trace: Trace, run_id: str) -> ObservedModel:
    groups: dict[tuple[str, str], _Group] = {}
    unknowns: list[str] = []
    for ex in trace.exchanges:
        if ex.resource_type not in ("xhr", "fetch"):
            continue
        is_json = "json" in (ex.response_content_type or "") or ex.status == 204
        if not is_json:
            unknowns.append(f"non-JSON {ex.method} {ex.path} ({ex.response_content_type}) [{ex.id}]")
            continue
        body = _json(ex.request_body)
        raw = ex.request_body or ""
        rpc_form = "commands=" in raw or is_sync_read(raw)
        if rpc_form or (isinstance(body, dict) and ({"query", "operationName"} & body.keys() or "commands" in body)):
            unknowns.append(f"RPC/GraphQL-style body at {ex.method} {ex.path} [{ex.id}]")
            continue
        tpl, params = template_of(ex.path)
        groups.setdefault((ex.method, tpl), _Group(ex.method, tpl, params)).exchanges.append(ex)

    # ---- operations ----
    operations: list[ObservedOperation] = []
    entity_samples: dict[str, list[dict]] = defaultdict(list)
    create_requests: dict[str, list[dict]] = defaultdict(list)
    create_responses: dict[str, list[dict]] = defaultdict(list)
    probe_rejections: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    entity_ids: dict[str, set[str]] = defaultdict(set)

    typed = typed_texts(trace)
    for (method, tpl), g in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        segs = g.segments
        last = segs[-1] if segs else ""
        last_is_param = last.startswith("{")
        ok = [x for x in g.exchanges if x.status and x.status < 400]
        responses = [_json(x.response_body) for x in ok]
        kind, entity, response, list_key = "get", None, "object", None
        if method == "GET":
            payload = next((p for p in responses if p is not None), None)
            items, list_key = _list_payload(payload)
            if items is not None:
                kind, response = "list", ("list_envelope" if list_key else "list")
                entity = singular(last) if not last_is_param else None
                for p in responses:
                    rows, _ = _list_payload(p)
                    for row in rows or []:
                        if isinstance(row, dict):
                            entity_samples[entity].append(row)
            else:
                kind = "get"
                entity = g.params[-1][:-3] if last_is_param else singular(last)
                entity_samples[entity] += [p for p in responses if isinstance(p, dict)]
        elif method == "POST":
            if len(segs) >= 2 and segs[-2].startswith("{") and not last_is_param and _acts_on_parent(g, responses):
                kind, entity = "action", g.params[-1][:-3]
            else:
                kind, entity = "create", singular(last)
            entity_samples[entity] += [p for p in responses if isinstance(p, dict) and "id" in p]
            if kind == "create":
                for x in g.exchanges:
                    req = _json(x.request_body) or {}
                    if x.status and x.status < 400:
                        create_requests[entity].append(req)
                        resp = _json(x.response_body)
                        if isinstance(resp, dict):
                            create_responses[entity].append(resp)
                    elif x.status and 400 <= x.status < 500:
                        probe_rejections[entity].append((req, x.response_body or ""))
        elif method in ("PATCH", "PUT"):
            kind, entity = "update", g.params[-1][:-3] if g.params else singular(last)
            entity_samples[entity] += [p for p in responses if isinstance(p, dict)]
            for x in g.exchanges:
                if x.status and 400 <= x.status < 500:
                    probe_rejections[entity].append((_json(x.request_body) or {}, x.response_body or ""))
        elif method == "DELETE":
            kind, entity, response = "delete", (g.params[-1][:-3] if g.params else singular(last)), "empty"
        if response == "object" and all(x.status == 204 for x in ok) and ok:
            response = "empty"
        req_fields = sorted({k for x in g.exchanges for k in (_json(x.request_body) or {}).keys()
                             if isinstance(_json(x.request_body), dict)})
        op_id = camel(kind if kind != "action" else last, entity or "item") if kind != "list" else \
            camel("list", (entity or "item") + "s")
        if kind == "action":
            op_id = camel(last, entity or "item")
        operations.append(ObservedOperation(
            id=op_id, kind=kind, method=method, path_template=tpl, path_params=g.params, entity=entity,
            request_fields=req_fields, response=response, list_key=list_key,
            success_statuses=sorted({x.status for x in ok if x.status}),
            error_statuses=sorted({x.status for x in g.exchanges if x.status and x.status >= 400}),
            triggered_by=sorted({x.step_id for x in g.exchanges if x.step_id}),
            evidence=[x.id for x in g.exchanges],
            search_param=search_param(g.exchanges, typed) if kind == "list" else None))

    for entity, rows in entity_samples.items():
        for row in rows:
            if "id" in row:
                entity_ids[entity].add(str(row["id"]))

    # ---- enums from select options seen in the UI ----
    options: dict[str, list[str]] = {}
    for step in trace.steps:
        for digest in (step.snapshot_before, step.snapshot_after):
            if not digest:
                continue
            try:
                text = store.blob_path(digest).read_text()
            except FileNotFoundError:
                continue
            for label, opts in locators.comboboxes(text):
                if opts:
                    options.setdefault(label, opts)

    # ---- entities and fields ----
    entities: list[ObservedEntity] = []
    for entity, rows in sorted(entity_samples.items()):
        if not entity or not rows or not any("id" in r for r in rows):
            continue   # singleton/config payloads without ids are not entities
        keys: dict[str, list] = defaultdict(list)
        for row in rows:
            for k, v in row.items():
                keys[k].append(v)
        create_keys = {k for r in create_requests.get(entity, []) for k in r}
        created_resp_keys = {k for r in create_responses.get(entity, []) for k in r}
        fields: list[ObservedField] = []
        for name, values in keys.items():
            types = {_json_type(v) for v in values if v is not None}
            jtype = types.pop() if len(types) == 1 else ("number" if types == {"integer", "number"} else "string")
            references = None
            if name.endswith("_id") and name != "id":
                ref = name[:-3]
                if ref in entity_samples and ({str(v) for v in values if v is not None} & entity_ids.get(ref, set())
                                             or ref in entity_ids):
                    references = ref
            enum_values = next((opts for label, opts in options.items()
                                if label.lower().replace(" ", "_") == name.lower()), [])
            required = "unknown"
            label_word = name.replace("_", " ").lower()
            for req, err in probe_rejections.get(entity, []):
                if (name not in req or req.get(name) in ("", None)) and label_word in err.lower():
                    required = "observed"
            if required == "unknown" and create_keys and all(r.get(name) not in ("", None)
                                                             for r in create_requests.get(entity, []) if r):
                required = "inferred" if name in create_keys else "unknown"
            fields.append(ObservedField(
                name=name, json_type=jtype, format=_format(values), nullable=any(v is None for v in values),
                in_create_request=name in create_keys,
                server_generated=name in created_resp_keys and name not in create_keys,
                enum_values=enum_values, required=required, references=references, samples=len(values),
                evidence=[o.evidence[0] for o in operations if o.entity == entity and o.evidence][:3]))
        id_values = [str(r.get("id")) for r in rows if "id" in r]
        id_type = "uuid" if id_values and all(UUID.match(v) for v in id_values) else \
            ("integer" if id_values and all(v.isdigit() for v in id_values) else "string")
        entities.append(ObservedEntity(name=entity, id_field="id", id_type=id_type, fields=fields,
                                       evidence=[e for o in operations if o.entity == entity for e in o.evidence][:5]))

    # ---- UI routes ----
    routes: dict[str, ObservedRoute] = {}
    for step in trace.steps:
        url = urlparse(step.url_after)
        if f"{url.scheme}://{url.netloc}" != trace.origin:
            continue
        tpl, _ = template_of(url.path or "/")
        route = routes.setdefault(tpl, ObservedRoute(template=tpl, examples=[], snapshot=step.snapshot_after))
        if url.path not in route.examples and len(route.examples) < 3:
            route.examples.append(url.path)

    header = ArtifactHeader(schema_id="forge.observed/1", run_id=run_id, stage="extract")
    model = ObservedModel(
        header=header, origin=trace.origin, entities=entities, operations=operations,
        routes=sorted(routes.values(), key=lambda r: r.template),
        select_options=[f"{label}: {' | '.join(opts)}" for label, opts in options.items()],
        unknowns=sorted(set(unknowns)))
    store.write("observed", model)
    return model
