"""Render an AppSpecDraft into the generated files of a replica repository.

``render(spec)`` is pure and deterministic: the same spec always yields byte-identical files. It covers
every generated file except the initial migration SQL, which Generate produces with ``prisma migrate
diff`` from the rendered schema.

Generated files (paths relative to the repository root):

* ``app-spec.json``                          the spec itself
* ``contract/openapi.yaml``                  the API contract
* ``db/schema.prisma``, ``db/migrations/migration_lock.toml``
* ``api/src/generated/routes.ts``            one registerOperation(...) per operation
* ``api/src/stubs/<operationId>.ts``         actions codegen cannot express (answer 501 until implemented)
* ``web/index.html``, ``web/src/generated/contract.ts``, ``web/src/generated/routes.tsx``
* ``web/src/generated/screens/<Screen>.tsx`` one component per screen
"""
from __future__ import annotations

import json

from forge.models.appspec import AppSpecDraft
from forge.stages.specify.render.model import RenderError, SpecIndex, index_spec
from forge.stages.specify.render.openapi import render_openapi
from forge.stages.specify.render.prisma import MIGRATION_LOCK, render_prisma
from forge.stages.specify.render.routes import render_api_routes, render_stub, stub_path
from forge.stages.specify.render.screens import (
    render_index_html,
    render_screens,
    render_web_contract,
    render_web_routes,
)

__all__ = ["RenderError", "render", "render_with_warnings", "validate", "stub_operations", "SCHEMA_PATH",
           "MIGRATION_PATH", "MIGRATION_LOCK_PATH"]

SCHEMA_PATH = "db/schema.prisma"
MIGRATION_PATH = "db/migrations/0001_init/migration.sql"
MIGRATION_LOCK_PATH = "db/migrations/migration_lock.toml"


def _spec_json(spec: AppSpecDraft) -> str:
    return json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def render_with_warnings(spec: AppSpecDraft) -> tuple[dict[str, str], list[str]]:
    """Render every generated file. Raises RenderError listing every problem; also returns non-fatal warnings."""
    ix: SpecIndex = index_spec(spec)
    warnings: list[str] = []
    files: dict[str, str] = {}
    if not ix.problems:
        files["app-spec.json"] = _spec_json(spec)
        files["contract/openapi.yaml"] = render_openapi(ix)
        files[SCHEMA_PATH] = render_prisma(ix)
        files[MIGRATION_LOCK_PATH] = MIGRATION_LOCK
        files["api/src/generated/routes.ts"] = render_api_routes(ix)
        for op in ix.operations.values():
            if op.stub:
                files[stub_path(op)] = render_stub(op)
        files["web/index.html"] = render_index_html(ix)
        files["web/src/generated/contract.ts"] = render_web_contract(ix)
        files["web/src/generated/routes.tsx"] = render_web_routes(ix)
        files.update(render_screens(ix, warnings))
    if ix.problems:
        raise RenderError(ix.problems)
    return dict(sorted(files.items())), warnings


def render(spec: AppSpecDraft) -> dict[str, str]:
    return render_with_warnings(spec)[0]


def validate(spec: AppSpecDraft) -> list[str]:
    """Every reason the spec cannot be rendered (empty when it can). Specify can feed these back to the model."""
    try:
        render_with_warnings(spec)
    except RenderError as err:
        return err.problems
    return []


def stub_operations(spec: AppSpecDraft) -> list[str]:
    """Operation ids whose behaviour is left to the builder (api/src/stubs/<id>.ts)."""
    ix = index_spec(spec)
    if ix.problems:
        raise RenderError(ix.problems)
    return [op.id for op in ix.operations.values() if op.stub]
