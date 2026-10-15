"""Scenario DSL: portable, data-only acceptance tests that run on the original and on the replica.

Scenarios are interpreted by forge.stages.oracle (never compiled to code). Values can contain bindings:
``{{name}}`` for a bind declared in ``binds`` and ``{{captured.name}}`` for a value captured from a
response during the run. Targets are accessibility-based so the same scenario resolves on both apps.
"""
from __future__ import annotations

from typing import Literal

from forge.models.base import NameValue, Strict


class Target(Strict):
    """An element located by ARIA role and accessible name, optionally scoped to one container.

    Scoping is one level deep on purpose (structured outputs cannot express recursion): a row, dialog,
    list item or region identified by its own role plus a name or contained text.
    """

    role: str
    name: str | None
    exact: bool
    has_text: str | None
    within_role: str | None
    within_name: str | None
    within_has_text: str | None


StepKind = Literal[
    "goto",            # route (supports bindings), relative to the app's base URL
    "click",
    "fill",            # value
    "select",          # value = option label
    "check",
    "uncheck",
    "press",           # value = key, e.g. "Enter"
    "expect_visible",
    "expect_hidden",
    "expect_text",     # target contains value
    "expect_value",    # input/select current value equals value
    "expect_url",      # current path matches value (route template with bindings)
    "reread",          # reload the current URL in a fresh browser context (same session)
]


class ScenarioStep(Strict):
    kind: StepKind
    target: Target | None
    value: str | None
    route: str | None


class Bind(Strict):
    """A value generated at run time. ``uniq_*`` kinds produce a fresh nonce on every run."""

    name: str
    kind: Literal["uniq_text", "uniq_email", "uniq_int", "fixture", "literal"]
    value: str | None


class Capture(Strict):
    """Read ``field`` from the JSON response of the first request matching ``operation``."""

    name: str
    operation: str
    field: str


class Effect(Strict):
    """Confirm a change through the API, not only on screen.

    Calls the logical ``operation`` (a list or get operation) on the target being tested and checks that
    an item whose ``field`` equals ``value`` is present (or absent when ``present`` is false).
    """

    operation: str
    path_params: list[NameValue]
    field: str
    value: str
    present: bool


class Scenario(Strict):
    id: str
    feature: str
    title: str
    mutating: bool
    binds: list[Bind]
    setup: list[str]
    steps: list[ScenarioStep]
    captures: list[Capture]
    effects: list[Effect]
    evidence: list[str]


class ScenarioSet(Strict):
    scenarios: list[Scenario]


FailureKind = Literal["none", "assertion", "action", "env", "startup"]


class ScenarioResult(Strict):
    scenario_id: str
    target: Literal["source", "replica"]
    run: int
    passed: bool
    failed_step: int | None = None
    failure_kind: FailureKind = "none"
    message: str | None = None
    vacuous_assertions: list[int] = []
    non_vacuous_assertions: int = 0
    first_write_step: int | None = None
    captured: list[NameValue] = []
    offorigin_requests: list[str] = []
    duration_ms: int = 0
    failure_snapshot: str | None = None   # accessibility snapshot of the page when a step failed (for repair)
