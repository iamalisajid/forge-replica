"""AppSpec: the flat contract for the replica, written by the Specify stage.

Everything downstream is rendered from it by code (Prisma schema, migrations, OpenAPI, Fastify routes,
CRUD screens), so the parts can never disagree. Arrays reference each other by name or id instead of
nesting, because structured outputs cannot express recursive schemas.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from forge.models.base import ArtifactHeader, NameValue, Strict
from forge.models.scenario import Scenario

Basis = Literal["observed", "inferred", "assumed"]


class SpecEntity(Strict):
    name: str                 # singular snake_case, e.g. "issue"
    label: str                # "Issue"
    plural_label: str         # "Issues"
    id_type: Literal["integer", "uuid"]
    owned_by_user: bool       # rows are scoped to the signed-in user when auth == "session"
    basis: Basis
    evidence: list[str]


FieldType = Literal["string", "text", "integer", "number", "boolean", "datetime", "date", "enum", "reference"]


class SpecField(Strict):
    """A column. ``name`` is the wire name seen on the original and is reused for the DB and API."""

    entity: str
    name: str
    type: FieldType
    enum_values: list[str]
    references: str | None     # entity name when type == "reference"
    required: bool
    unique: bool
    default: str | None        # literal default, e.g. "open"
    generated: Literal["none", "id", "created_at", "updated_at"]
    basis: Basis
    evidence: list[str]


class PathParam(Strict):
    name: str                  # as used in the path template, e.g. "project_id"
    entity: str                # whose id fills it


class SpecOperation(Strict):
    """One API operation with its exact wire format (source-aligned)."""

    id: str                    # operationId, e.g. "createIssue"
    entity: str
    kind: Literal["list", "get", "create", "update", "delete", "action"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str                  # template with {param}, e.g. "/api/projects/{project_id}/issues"
    path_params: list[PathParam]
    request_fields: list[str]
    response: Literal["object", "list", "list_envelope", "empty"]
    list_key: str | None       # e.g. "items" for {"items": [...]}
    search_param: str | None   # list operations: query parameter that filters by text (e.g. "query"), else null
    success_status: int
    action_sets: list[NameValue]   # deterministic effect of an action, e.g. status=closed
    action_note: str | None    # behavior codegen cannot express; the builder implements it
    source_method: str
    source_path: str
    basis: Basis
    evidence: list[str]


    @model_validator(mode="before")
    @classmethod
    def _spec_before_search(cls, data):
        if isinstance(data, dict) and "search_param" not in data:
            data = {**data, "search_param": None}
        return data


class SpecScreen(Strict):
    id: str
    route: str                 # react-router syntax, e.g. "/projects/:project_id"
    title: str
    entity: str | None
    kind: Literal["list", "detail", "form", "static"]
    parent: str | None
    evidence: list[str]


ElementPurpose = Literal[
    "heading", "text", "navigate", "list", "list_row", "container",
    "open_create", "field", "submit_create", "submit_update", "cancel",
    "delete", "action", "status_message",
]


class SpecElement(Strict):
    """An element of a screen with the original's exact role and accessible name."""

    id: str
    screen: str
    parent: str | None         # element id of the container (dialog, form, list, row)
    role: str
    name: str
    purpose: ElementPurpose
    operation: str | None      # operation id for submit/delete/action/list
    field: str | None          # field name for inputs and row cells
    target_screen: str | None  # for navigate
    order: int
    evidence: list[str]


class DataSpecDraft(Strict):
    """Specify call 1 of 3: data model and wire format."""

    app_name: str
    auth: Literal["none", "session"]
    entities: list[SpecEntity]
    fields: list[SpecField]
    operations: list[SpecOperation]
    assumptions: list[str]
    unsupported: list[str]


class UiSpecDraft(Strict):
    """Specify call 2 of 3: screens and their elements (given the data spec)."""

    screens: list[SpecScreen]
    elements: list[SpecElement]


class ScenarioDraft(Strict):
    """Specify call 3 of 3: scenarios (given the data and UI specs)."""

    scenarios: list[Scenario]
    pilot_scenario: str        # scenario id of the create -> reload -> edit -> delete workflow


class AppSpecDraft(Strict):
    """The assembled spec. Too large for one structured call (the API rejects the grammar), so Specify
    fills DataSpecDraft, UiSpecDraft and ScenarioDraft separately and ``assemble`` joins them."""

    app_name: str
    auth: Literal["none", "session"]
    entities: list[SpecEntity]
    fields: list[SpecField]
    operations: list[SpecOperation]
    screens: list[SpecScreen]
    elements: list[SpecElement]
    scenarios: list[Scenario]
    pilot_scenario: str        # scenario id of the create -> reload -> edit -> delete workflow
    assumptions: list[str]
    unsupported: list[str]


def assemble(data: DataSpecDraft, ui: UiSpecDraft, scenarios: ScenarioDraft) -> AppSpecDraft:
    return AppSpecDraft(
        app_name=data.app_name, auth=data.auth, entities=data.entities, fields=data.fields,
        operations=data.operations, screens=ui.screens, elements=ui.elements, scenarios=scenarios.scenarios,
        pilot_scenario=scenarios.pilot_scenario, assumptions=data.assumptions, unsupported=data.unsupported)


class AppSpec(Strict):
    header: ArtifactHeader
    spec: AppSpecDraft


class OpWire(Strict):
    """What the scenario runner needs to call or recognise one operation on either app."""

    id: str
    kind: str
    source_method: str
    source_path: str
    replica_method: str
    replica_path: str
    list_key: str | None


def op_wires(spec: AppSpecDraft) -> list[OpWire]:
    return [
        OpWire(id=o.id, kind=o.kind, source_method=o.source_method, source_path=o.source_path,
               replica_method=o.method, replica_path=o.path, list_key=o.list_key)
        for o in spec.operations
    ]
