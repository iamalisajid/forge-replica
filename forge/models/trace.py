"""What the Explore harness records. Stored artifact only (never filled in by a model)."""
from __future__ import annotations

from typing import Literal

from forge.models.base import ArtifactHeader, NameValue, Strict
from forge.models.scenario import Target


class Exchange(Strict):
    """One HTTP request/response pair, redacted, tied to the step that caused it."""

    id: str                                  # "x0001"
    step_id: str | None
    correlated_by: Literal["nonce", "entity_id", "state_diff", "time_window", "none"]
    method: str
    url: str
    path: str
    query: list[NameValue] = []
    resource_type: str                       # fetch | xhr | document | ...
    status: int | None
    request_content_type: str | None = None
    request_body: str | None = None          # redacted text (JSON as a string)
    response_content_type: str | None = None
    response_body: str | None = None         # redacted, truncated text
    started_at: float


class Step(Strict):
    id: str                                  # "s007"
    action: Literal["navigate", "click", "fill", "select", "check", "uncheck", "press"]
    ref: str | None                          # aria ref the agent used
    target: Target | None                    # portable locator synthesised by the harness
    portable: bool                           # False when only a CSS fallback could identify the element
    value: str | None                        # value typed or chosen (after nonce injection)
    nonce: str | None
    url_before: str
    url_after: str
    snapshot_before: str | None              # evidence blob sha256
    snapshot_after: str | None
    screenshot: str | None
    dom: str | None = None                   # sanitized DOM of the page (evidence blob), first visit of a route
    styles: str | None = None                # computed-style digest + CSS variables (evidence blob)
    exchange_ids: list[str] = []
    console_errors: list[str] = []
    error: str | None = None
    ts: float


class CreatedRecord(Strict):
    """Write-ahead ledger entry for cleanup on the original."""

    exchange_id: str
    path: str                                # collection path the create was sent to
    id_value: str
    entity_hint: str | None
    deleted: bool = False


class Trace(Strict):
    header: ArtifactHeader
    target_url: str
    origin: str
    pass_name: Literal["breadth", "probe", "scripted"]
    feature: str | None
    steps: list[Step]
    exchanges: list[Exchange]
    created: list[CreatedRecord]
    notes: list[str] = []
