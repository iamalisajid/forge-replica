"""Builders for a fake generated repo (tests/fixtures/oracle/replica) with its spec, manifest and scenarios.

The fake replica implements the forge stack contract with plain Node (no dependencies): services
db/migrate/app, the app on :3000, /__forge/health, /__forge/routes in test mode, FORGE_MUTANT and data
kept on a named volume. It lets verify and the negative control run end to end without codegen.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from forge.core.gitrepo import commit_all, init_repo
from forge.models.appspec import AppSpecDraft, PathParam, SpecEntity, SpecField, SpecOperation
from forge.models.base import ArtifactHeader
from forge.models.run import CodegenManifest, ManifestEntry
from forge.models.scenario import Bind, Capture, Scenario, ScenarioStep, Target

HERE = Path(__file__).parent
IMMUTABLE = ("compose.yaml", "Dockerfile")


def make_repo(dest: Path, *, extra_script: str = "") -> Path:
    """A new git repo with the fake replica committed (``extra_script`` is injected into the page)."""
    init_repo(dest)
    for name in ("compose.yaml", "Dockerfile", "server.mjs"):
        shutil.copy(HERE / "replica" / name, dest / name)
    html = (HERE / "app.html").read_text()
    if extra_script:
        html = html.replace("</body>", f"<script>{extra_script}</script>\n</body>")
    (dest / "app.html").write_text(html)
    commit_all(dest, "G1: fake replica")
    return dest


def manifest(repo: Path, commit: str) -> CodegenManifest:
    return CodegenManifest(
        header=ArtifactHeader(schema_id="codegen-manifest", run_id="lanec-test", stage="generate"),
        repo=str(repo), commit=commit,
        immutable=[ManifestEntry(path=p, sha256=hashlib.sha256((repo / p).read_bytes()).hexdigest())
                   for p in IMMUTABLE],
        editable_globs=["server.mjs", "app.html"], stubs=[])


def _op(id, kind, method, path, params=(), list_key=None, status=200, response="object"):
    return SpecOperation(
        id=id, entity="item", kind=kind, method=method, path=path,
        path_params=[PathParam(name=p, entity="item") for p in params],
        request_fields=["name"] if method == "POST" else [],
        response=response, list_key=list_key, success_status=status, action_sets=[], action_note=None,
        source_method=method, source_path=path, basis="observed", evidence=["test"])


def spec() -> AppSpecDraft:
    def field(name, type, generated="none"):
        return SpecField(entity="item", name=name, type=type, enum_values=[], references=None, required=True,
                         unique=False, default=None, generated=generated, basis="observed", evidence=["test"])

    return AppSpecDraft(
        app_name="Fixture items", auth="none",
        entities=[SpecEntity(name="item", label="Item", plural_label="Items", id_type="integer", owned_by_user=False,
                             basis="observed", evidence=["test"])],
        fields=[field("id", "integer", "id"), field("name", "string")],
        operations=[
            _op("listItems", "list", "GET", "/api/items", list_key="items", response="list_envelope"),
            _op("createItem", "create", "POST", "/api/items", status=201),
            _op("getItem", "get", "GET", "/api/items/{item_id}", ["item_id"]),
            _op("deleteItem", "delete", "DELETE", "/api/items/{item_id}", ["item_id"], status=204, response="empty"),
        ],
        screens=[], elements=[], scenarios=[], pilot_scenario="", assumptions=[], unsupported=[])


def _t(role, name=None, within=None, within_text=None) -> Target:
    return Target(role=role, name=name, exact=True, has_text=None, within_role=within[0] if within else None,
                  within_name=within[1] if within else None, within_has_text=within_text)


def _s(kind, target=None, value=None, route=None) -> ScenarioStep:
    return ScenarioStep(kind=kind, target=target, value=value, route=route)


DIALOG = ("dialog", "New item")


def create_steps() -> list[ScenarioStep]:
    return [
        _s("goto", route="/"),
        _s("click", _t("button", "New item")),
        _s("fill", _t("textbox", "Name", DIALOG), "{{name}}"),
        _s("click", _t("button", "Create", DIALOG)),
        _s("expect_visible", _t("link", "{{name}}")),
    ]


def scenario(id: str, steps: list[ScenarioStep], mutating: bool = True) -> Scenario:
    return Scenario(id=id, feature="items", title=id, mutating=mutating,
                    binds=[Bind(name="name", kind="uniq_text", value=None)], setup=[], steps=steps,
                    captures=[Capture(name="item_id", operation="createItem", field="id")], effects=[],
                    evidence=["test"])


def create_item() -> Scenario:
    return scenario("create-item", create_steps() + [_s("reread"), _s("expect_visible", _t("link", "{{name}}"))])


def delete_item() -> Scenario:
    return scenario("delete-item", create_steps() + [
        _s("reread"),
        _s("click", _t("button", "Delete", ("listitem", None), "{{name}}")),
        _s("expect_hidden", _t("link", "{{name}}")),
        _s("reread"),
        _s("expect_hidden", _t("link", "{{name}}")),
    ])


def open_and_cancel() -> Scenario:
    """Writes nothing, so the mutant cannot make it fail: never protected."""
    return scenario("open-and-cancel", [
        _s("goto", route="/"),
        _s("click", _t("button", "New item")),
        _s("expect_visible", _t("dialog", "New item")),
        _s("click", _t("button", "Cancel", DIALOG)),
        _s("expect_hidden", _t("dialog", "New item")),
    ], mutating=False)
