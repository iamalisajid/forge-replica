"""A validated, resolved view of an AppSpecDraft that every renderer reads.

Validation is strict about anything that becomes an identifier, a path or a schema construct, and
collects every problem it finds so Specify can retry with the full list.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field as dc_field

from forge.models.appspec import AppSpecDraft, SpecEntity, SpecField, SpecOperation, SpecScreen
from forge.stages.specify.render import naming

INT_MIN, INT_MAX = -2147483648, 2147483647
_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Literals safe to embed in a Prisma string default (anything else is applied by the API instead).
PRISMA_SAFE_STRING = re.compile(r"^[A-Za-z0-9 _.,:/@+-]*$")

READ_KINDS = {"list": ("GET",), "get": ("GET",)}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class RenderError(ValueError):
    """The spec cannot be rendered. ``problems`` lists every reason, one per line."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("cannot render the spec:\n" + "\n".join(f"- {p}" for p in self.problems))


@dataclass
class Entity:
    spec: SpecEntity
    model: str                 # Prisma model name
    delegate: str              # Prisma client property
    table: str                 # @@map table name
    fields: list[SpecField]
    id_field: str
    order_by: str

    @property
    def name(self) -> str:
        return self.spec.name

    def field(self, name: str | None) -> SpecField | None:
        return next((f for f in self.fields if f.name == name), None) if name else None

    def reference_to(self, entity: str, prefer: str | None = None) -> SpecField | None:
        refs = [f for f in self.fields if f.type == "reference" and f.references == entity]
        return next((f for f in refs if f.name == prefer), refs[0] if refs else None)


@dataclass
class Param:
    name: str
    entity: str
    role: str                  # "self" | "parent"
    field: str | None          # FK on the operation's entity for "parent"


@dataclass
class Operation:
    spec: SpecOperation
    params: list[Param]
    stub: bool
    html_fallback: bool = False

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def entity(self) -> str:
        return self.spec.entity


def literal_ok(spec_field: SpecField, literal: str, id_type: str | None) -> bool:
    """Mirror of the API's coerceLiteral: can ``literal`` be a value of this field?"""
    t, low = spec_field.type, literal.strip().lower()
    if t in ("string", "text"):
        return True
    if not spec_field.required and low == "null":
        return True
    if t == "enum":
        return literal in spec_field.enum_values
    if t == "integer":
        s = literal.strip()
        return bool(re.fullmatch(r"[-+]?\d{1,10}", s)) and INT_MIN <= int(s) <= INT_MAX
    if t == "number":
        try:
            return literal.strip() != "" and math.isfinite(float(literal))
        except ValueError:
            return False
    if t == "boolean":
        return low in ("true", "false", "1", "0")
    if t == "datetime":
        return low == "now" or bool(_ISO_DATETIME.match(literal.strip()))
    if t == "date":
        m = _ISO_DATE.match(literal.strip())
        if low in ("today", "now"):
            return True
        if not m:
            return False
        y, mo, d = (int(x) for x in m.groups())
        return 1 <= mo <= 12 and 1 <= d <= [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                                             31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    if t == "reference":
        if id_type == "uuid":
            return bool(_UUID.match(literal.strip()))
        s = literal.strip()
        return bool(re.fullmatch(r"[-+]?\d{1,10}", s)) and INT_MIN <= int(s) <= INT_MAX
    return False


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _is_param(segment: str) -> bool:
    return segment.startswith(":") or (segment.startswith("{") and segment.endswith("}"))


def paths_overlap(a: str, b: str) -> bool:
    """True when some URL could match both paths (literal segments equal or either side a parameter)."""
    sa, sb = _segments(a), _segments(b)
    return len(sa) == len(sb) and all(x == y or _is_param(x) or _is_param(y) for x, y in zip(sa, sb, strict=True))


@dataclass
class SpecIndex:
    spec: AppSpecDraft
    problems: list[str] = dc_field(default_factory=list)
    entities: dict[str, Entity] = dc_field(default_factory=dict)
    operations: dict[str, Operation] = dc_field(default_factory=dict)
    screens: dict[str, SpecScreen] = dc_field(default_factory=dict)

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    # ---- entities and fields ----
    def _index_entities(self) -> None:
        seen_models: dict[str, str] = {}
        seen_tables: dict[str, str] = {}
        for e in self.spec.entities:
            if not naming.ENTITY_NAME.match(e.name):
                self.problem(f"entity name {e.name!r} must be snake_case (letters, digits, underscores)")
                continue
            if e.name in self.entities:
                self.problem(f"entity {e.name!r} is declared twice")
                continue
            model = naming.model_name(e.name)
            table = naming.plural(naming.snake(e.name))
            for seen, key, what in ((seen_models, model, "Prisma model"), (seen_tables, table, "table")):
                if key in seen:
                    self.problem(f"entities {seen[key]!r} and {e.name!r} map to the same {what} {key!r}")
                seen[key] = e.name
            fields = [f for f in self.spec.fields if f.entity == e.name]
            self.entities[e.name] = Entity(spec=e, model=model, delegate=naming.camel(model), table=table,
                                           fields=fields, id_field="", order_by="")
        for f in self.spec.fields:
            if f.entity not in self.entities and naming.ENTITY_NAME.match(f.entity):
                self.problem(f"field {f.entity}.{f.name} belongs to an unknown entity")
        for entity in self.entities.values():
            self._check_fields(entity)

    def _check_fields(self, entity: Entity) -> None:
        names: set[str] = set()
        for f in entity.fields:
            where = f"field {entity.name}.{f.name}"
            if not naming.FIELD_NAME.match(f.name):
                self.problem(f"{where}: name must start with a letter and use only letters, digits and underscores")
                continue
            if f.name in names:
                self.problem(f"{where} is declared twice")
            names.add(f.name)
            if f.type == "reference":
                if not f.references or f.references not in {e.name for e in self.spec.entities}:
                    self.problem(f"{where}: references unknown entity {f.references!r}")
            elif f.references:
                self.problem(f"{where}: only reference fields may set references")
            if f.type == "enum":
                if not f.enum_values:
                    self.problem(f"{where}: enum has no values")
                if len(set(f.enum_values)) != len(f.enum_values):
                    self.problem(f"{where}: enum values repeat")
            if f.default is not None and f.generated == "none":
                ref = self.spec_entity(f.references) if f.type == "reference" else None
                if not literal_ok(f, f.default, ref.id_type if ref else None):
                    self.problem(f"{where}: default {f.default!r} is not a valid {f.type}")
        ids = [f for f in entity.fields if f.generated == "id"]
        if len(ids) != 1:
            self.problem(f"entity {entity.name!r} needs exactly one field with generated='id' (found {len(ids)})")
        entity.id_field = ids[0].name if ids else "id"
        created = next((f for f in entity.fields if f.generated == "created_at"), None)
        entity.order_by = entity.id_field if entity.spec.id_type == "integer" or not created else created.name
        for f in entity.fields:
            if f.generated in ("created_at", "updated_at") and f.type not in ("datetime", "date"):
                self.problem(f"field {entity.name}.{f.name}: generated {f.generated} must be a datetime")

    def spec_entity(self, name: str | None) -> SpecEntity | None:
        return next((e for e in self.spec.entities if e.name == name), None)

    # ---- operations ----
    def _index_operations(self) -> None:
        seen_lower: dict[str, str] = {}
        routes: dict[tuple[str, str], str] = {}
        shapes: dict[str, str] = {}
        for op in self.spec.operations:
            where = f"operation {op.id!r}"
            if not naming.OPERATION_ID.match(op.id):
                self.problem(f"{where}: id must start with a letter and use only letters, digits and underscores")
                continue
            if op.id.lower() in seen_lower:
                self.problem(f"{where}: id clashes with {seen_lower[op.id.lower()]!r} (ids are file names)")
                continue
            seen_lower[op.id.lower()] = op.id
            entity = self.entities.get(op.entity)
            if entity is None:
                self.problem(f"{where}: unknown entity {op.entity!r}")
                continue
            if op.kind in READ_KINDS and op.method not in READ_KINDS[op.kind]:
                self.problem(f"{where}: a {op.kind} operation must use GET")
            if op.kind not in READ_KINDS and op.method not in WRITE_METHODS:
                self.problem(f"{where}: a {op.kind} operation must use POST, PUT, PATCH or DELETE")
            if not naming.API_PATH.match(op.path) or naming.has_dot_segment(op.path):
                self.problem(f"{where}: path {op.path!r} must be /-separated literal segments and {{param}}s")
                continue
            key = (op.method, naming.route_shape(op.path))
            if key in routes:
                self.problem(f"{where}: {op.method} {op.path} is already used by {routes[key]!r}")
            routes[key] = op.id
            other = shapes.setdefault(key[1], op.path)
            if other != op.path:
                self.problem(f"{where}: path {op.path} names its parameters differently from {other}")
            if not 200 <= op.success_status <= 299:
                self.problem(f"{where}: success_status must be 2xx")
            if op.response == "list_envelope" and not (op.list_key and naming.LIST_KEY.match(op.list_key)):
                self.problem(f"{where}: list_envelope needs a list_key that is a plain identifier")
            for name in op.request_fields:
                f = entity.field(name)
                if f is None:
                    self.problem(f"{where}: request field {name!r} is not a field of {entity.name!r}")
                elif f.generated != "none":
                    self.problem(f"{where}: request field {name!r} is generated by the server")
            for s in op.action_sets:
                f = entity.field(s.name)
                if f is None or f.generated != "none":
                    self.problem(f"{where}: action sets unknown or generated field {s.name!r}")
                else:
                    ref = self.spec_entity(f.references) if f.type == "reference" else None
                    if not literal_ok(f, s.value, ref.id_type if ref else None):
                        self.problem(f"{where}: action value {s.value!r} is not a valid {f.type} for {s.name!r}")
            if op.action_sets and op.kind != "action":
                self.problem(f"{where}: only action operations may set action_sets")
            params = self._params(op, entity, where)
            if params is None:
                continue
            self_param = next((p for p in params if p.role == "self"), None)
            if op.kind in ("get", "update", "delete") and self_param is None:
                self.problem(f"{where}: a {op.kind} operation needs a path parameter for the {op.entity} id")
            stub = op.kind == "action" and ((bool(op.action_note) and not op.action_sets) or self_param is None)
            self.operations[op.id] = Operation(spec=op, params=params, stub=stub)

    def _params(self, op: SpecOperation, entity: Entity, where: str) -> list[Param] | None:
        names = naming.api_params(op.path)
        declared = [p.name for p in op.path_params]
        ok = True
        if sorted(names) != sorted(declared) or len(set(names)) != len(names):
            self.problem(f"{where}: path parameters {names} do not match path_params {declared}")
            ok = False
        params: list[Param] = []
        for p in op.path_params:
            if not naming.PARAM_NAME.match(p.name):
                self.problem(f"{where}: path parameter {p.name!r} is not a plain identifier")
                ok = False
                continue
            if p.entity not in self.entities:
                self.problem(f"{where}: path parameter {p.name!r} names unknown entity {p.entity!r}")
                ok = False
                continue
            if p.entity == entity.name:
                if any(q.role == "self" for q in params):
                    self.problem(f"{where}: more than one path parameter names the {entity.name} itself")
                    ok = False
                if op.kind in ("list", "create"):
                    self.problem(f"{where}: a {op.kind} operation cannot take the {entity.name}'s own id")
                    ok = False
                params.append(Param(p.name, p.entity, "self", None))
            else:
                fk = entity.reference_to(p.entity, prefer=p.name)
                params.append(Param(p.name, p.entity, "parent", fk.name if fk else None))
        return params if ok else None

    # ---- screens ----
    def _index_screens(self) -> None:
        pascal_seen: dict[str, str] = {}
        shapes: dict[str, str] = {}
        for s in self.spec.screens:
            where = f"screen {s.id!r}"
            if not naming.SCREEN_ID.match(s.id):
                self.problem(f"{where}: id must start with a letter and use letters, digits, '-' or '_'")
                continue
            key = naming.pascal(s.id).lower()
            if key in pascal_seen:
                self.problem(f"{where}: id clashes with screen {pascal_seen[key]!r}")
                continue
            pascal_seen[key] = s.id
            if not naming.SCREEN_ROUTE.match(s.route) or naming.has_dot_segment(s.route):
                self.problem(f"{where}: route {s.route!r} must be '/' or /-separated literal segments and :params")
                continue
            if len(set(naming.route_params(s.route))) != len(naming.route_params(s.route)):
                self.problem(f"{where}: route {s.route!r} repeats a parameter")
            shape = naming.route_shape(s.route)
            if shape in shapes:
                self.problem(f"{where}: route {s.route!r} is the same as screen {shapes[shape]!r}")
            shapes[shape] = s.id
            if s.entity is not None and s.entity not in self.entities:
                self.problem(f"{where}: unknown entity {s.entity!r}")
            self.screens[s.id] = s
        for s in self.spec.screens:
            if s.parent is not None and s.id in self.screens and s.parent not in self.screens:
                self.problem(f"screen {s.id!r}: unknown parent screen {s.parent!r}")
        for op in self.operations.values():
            if op.spec.method == "GET":
                op.html_fallback = any(paths_overlap(op.spec.path, s.route) for s in self.screens.values())

    def build(self) -> SpecIndex:
        self._index_entities()
        self._index_operations()
        self._index_screens()
        return self

    # ---- lookups used by the renderers ----
    def operation_param_entities(self) -> dict[str, str]:
        """Path parameter name -> entity, when every operation agrees on it."""
        seen: dict[str, set[str]] = {}
        for op in self.operations.values():
            for p in op.params:
                seen.setdefault(p.name, set()).add(p.entity)
        return {name: next(iter(ents)) for name, ents in seen.items() if len(ents) == 1}


def index_spec(spec: AppSpecDraft) -> SpecIndex:
    return SpecIndex(spec=spec).build()
