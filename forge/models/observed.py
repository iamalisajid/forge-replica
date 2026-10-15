"""What Extract concludes from recorded traffic. Every claim cites evidence (exchange or step ids)."""
from __future__ import annotations

from typing import Literal

from forge.models.base import ArtifactHeader, Strict


class ObservedField(Strict):
    name: str
    json_type: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    format: Literal["none", "date-time", "date", "email", "uri", "uuid"]
    nullable: bool
    in_create_request: bool
    server_generated: bool
    enum_values: list[str]                    # from select options seen in the UI
    required: Literal["observed", "inferred", "unknown"]   # observed = the original rejected a missing value
    references: str | None                   # entity name for a foreign key
    samples: int
    evidence: list[str]


class ObservedEntity(Strict):
    name: str
    id_field: str
    id_type: Literal["integer", "uuid", "string"]
    fields: list[ObservedField]
    evidence: list[str]


class ObservedOperation(Strict):
    id: str
    kind: Literal["list", "get", "create", "update", "delete", "action"]
    method: str
    path_template: str
    path_params: list[str]
    entity: str | None
    request_fields: list[str]
    response: Literal["object", "list", "list_envelope", "empty"]
    list_key: str | None
    success_statuses: list[int]
    error_statuses: list[int]
    triggered_by: list[str]                   # step ids
    evidence: list[str]                       # exchange ids
    search_param: str | None = None           # query parameter a list operation filters by (e.g. "query")


class ObservedRoute(Strict):
    template: str                             # "/projects/{id}"
    examples: list[str]
    snapshot: str | None                      # evidence blob sha256 of a representative state


class ObservedModel(Strict):
    header: ArtifactHeader
    origin: str
    entities: list[ObservedEntity]
    operations: list[ObservedOperation]
    routes: list[ObservedRoute]
    select_options: list[str]                 # "Status: open | in_progress | closed"
    unknowns: list[str]
