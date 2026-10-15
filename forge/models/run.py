"""Run configuration, feasibility, and the runner/codegen interface contracts shared by the lanes."""
from __future__ import annotations

from typing import Literal

from forge.models.appspec import OpWire
from forge.models.base import ArtifactHeader, NameValue, Strict
from forge.models.scenario import Scenario, ScenarioResult


class RunConfig(Strict):
    run_id: str
    url: str
    origin: str
    mutations: Literal["read-only", "test-account"]
    budget_usd: float
    model: str
    pilot_only: bool
    no_build: bool
    heldout_dir: str | None
    out_root: str
    run_dir: str
    created_at: str
    features: list[str] = ["crud"]     # what the user wants replicated; forge finds where each one lives


# Capabilities a run can be asked for (forge run --features). CRUD is always part of the pilot.
KNOWN_FEATURES = ("crud", "search")


class ProtocolShare(Strict):
    protocol: Literal["rest_json", "graphql", "rpc", "forms", "websocket", "other"]
    share: float


class FeatureSupport(Strict):
    name: str
    support: Literal["supported", "degraded", "out_of_scope"]
    reason: str | None = None


class Feasibility(Strict):
    header: ArtifactHeader
    reachable: bool
    final_url: str
    login_wall: bool
    challenge: bool
    canvas_ratio: float
    interactive_nodes: int
    protocols: list[ProtocolShare]
    features: list[FeatureSupport]
    stop: bool
    reason: str | None = None


# ---- Stack contract shared by template (lane B), oracle/verify (lane C) and the runner ----
STACK_SERVICES = ("db", "migrate", "app")
APP_PORT = 3000
# Inside the stack network, reach the app as http://replica:3000 — NOT http://app:3000: Chromium treats "app" as
# the HSTS-preloaded .app TLD and force-upgrades it to HTTPS (ERR_SSL_PROTOCOL_ERROR).
APP_ALIAS = "replica"
APP_URL_IN_NETWORK = "http://replica:3000"
HEALTH_PATH = "/__forge/health"
ROUTES_PATH = "/__forge/routes"          # only when FORGE_TEST_MODE=1
MUTANT_ENV = "FORGE_MUTANT"              # value "rollback-writes"
TEST_MODE_ENV = "FORGE_TEST_MODE"


class RunnerRequest(Strict):
    """Input file for the runner image: ``python3 -m forge.stages.oracle.runner_cli --request in.json --result out.json``."""

    target: Literal["source", "replica"]
    base_url: str
    scenarios: list[Scenario]
    operations: list[OpWire]
    allowed_origins: list[str]
    runs: int = 1
    parallel: int = 1
    storage_state_path: str | None = None
    fixtures: list[NameValue] = []
    seed: str | None = None


class RunnerResponse(Strict):
    results: list[ScenarioResult]


class ManifestEntry(Strict):
    path: str
    sha256: str


class CodegenManifest(Strict):
    """Which generated-repo files are immutable (hash-checked) and which the builder may edit."""

    header: ArtifactHeader
    repo: str
    commit: str
    immutable: list[ManifestEntry]
    editable_globs: list[str]
    stubs: list[str]                     # operation ids left for the builder
