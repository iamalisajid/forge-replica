"""contract/openapi.yaml: the replica's API contract, rendered from the spec's operations."""
from __future__ import annotations

import yaml

from forge.models.appspec import SpecField
from forge.stages.specify.render.model import Entity, Operation, SpecIndex

ERROR_REF = {"$ref": "#/components/schemas/Error"}


def _field_schema(ix: SpecIndex, f: SpecField) -> dict:
    if f.type in ("string", "text"):
        schema: dict = {"type": "string"}
    elif f.type == "enum":
        schema = {"type": "string", "enum": list(f.enum_values)}
    elif f.type == "integer":
        schema = {"type": "integer", "format": "int32"}
    elif f.type == "number":
        schema = {"type": "number"}
    elif f.type == "boolean":
        schema = {"type": "boolean"}
    elif f.type == "datetime" or f.generated in ("created_at", "updated_at"):
        schema = {"type": "string", "format": "date-time"}
    elif f.type == "date":
        schema = {"type": "string", "format": "date"}
    else:  # reference
        target = ix.entities.get(f.references or "")
        uuid = target is not None and target.spec.id_type == "uuid"
        schema = {"type": "string", "format": "uuid"} if uuid else {"type": "integer"}
    if f.generated == "id":
        owner = ix.entities[f.entity]
        schema = {"type": "string", "format": "uuid"} if owner.spec.id_type == "uuid" else {"type": "integer"}
    if f.generated != "none":
        schema["readOnly"] = True
    nullable = not f.required and f.generated == "none"
    if nullable:
        schema["type"] = [schema["type"], "null"]
        if "enum" in schema:
            schema["enum"] = [*schema["enum"], None]
    if f.default is not None and f.generated == "none":
        schema["x-default"] = f.default
    return schema


def _entity_schema(ix: SpecIndex, entity: Entity) -> dict:
    return {
        "type": "object",
        "title": entity.spec.label,
        "required": [f.name for f in entity.fields],
        "properties": {f.name: _field_schema(ix, f) for f in entity.fields},
    }


def _param_schema(ix: SpecIndex, entity_name: str) -> dict:
    entity = ix.entities[entity_name]
    return {"type": "string", "format": "uuid"} if entity.spec.id_type == "uuid" else {"type": "integer"}


def _operation(ix: SpecIndex, op: Operation) -> dict:
    spec = op.spec
    entity = ix.entities[spec.entity]
    ref = {"$ref": f"#/components/schemas/{entity.model}"}
    out: dict = {"operationId": spec.id, "tags": [entity.name]}
    if spec.action_note:
        out["description"] = spec.action_note
    if op.params:
        out["parameters"] = [
            {"name": p.name, "in": "path", "required": True, "schema": _param_schema(ix, p.entity),
             "description": f"Id of the {p.entity}"}
            for p in op.params
        ]
    if spec.request_fields:
        fields = [entity.field(n) for n in spec.request_fields]
        required = [f.name for f in fields if f and spec.kind == "create" and f.required and f.default is None]
        body: dict = {"type": "object",
                      "properties": {f.name: _field_schema(ix, f) for f in fields if f}}
        if required:
            body["required"] = required
        out["requestBody"] = {"required": spec.kind == "create",
                              "content": {"application/json": {"schema": body}}}
    if spec.response == "empty" or spec.success_status == 204:
        success: dict = {"description": "Done"}
    else:
        if spec.response == "list_envelope":
            schema: dict = {"type": "object", "required": [spec.list_key],
                            "properties": {spec.list_key: {"type": "array", "items": ref}}}
        elif spec.response == "list":
            schema = {"type": "array", "items": ref}
        else:
            schema = ref
        success = {"description": "Created" if spec.success_status == 201 else "OK",
                   "content": {"application/json": {"schema": schema}}}
    responses: dict = {str(spec.success_status): success}
    if spec.method != "GET":
        responses["403"] = {"$ref": "#/components/responses/Error"}
    if op.params:
        responses["404"] = {"$ref": "#/components/responses/Error"}
    if spec.request_fields or spec.kind == "create":
        responses["422"] = {"$ref": "#/components/responses/Error"}
    if op.stub:
        responses["501"] = {"$ref": "#/components/responses/Error"}
    out["responses"] = responses
    if op.stub:
        out["x-forge-stub"] = True
    return out


def render_openapi(ix: SpecIndex) -> str:
    paths: dict[str, dict] = {}
    for op in ix.operations.values():
        paths.setdefault(op.spec.path, {})[op.spec.method.lower()] = _operation(ix, op)
    schemas: dict = {e.model: _entity_schema(ix, e) for e in ix.entities.values()}
    schemas["Error"] = {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}}}
    doc = {
        "openapi": "3.1.0",
        "info": {"title": ix.spec.app_name, "version": "1.0.0",
                 "description": "Generated by forge from app-spec.json. Paths, methods, fields and status codes "
                                "follow the original application's wire format."},
        "paths": paths,
        "components": {
            "schemas": schemas,
            "responses": {"Error": {"description": "Error", "content": {"application/json": {"schema": ERROR_REF}}}},
        },
    }
    return "# Generated by forge from app-spec.json. Do not edit: change the spec and regenerate.\n" + yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096)
