"""The verification report, bound to one commit of one generated repo."""
from __future__ import annotations

from typing import Literal

from forge.models.base import ArtifactHeader, Strict


class CheckResult(Strict):
    name: str
    status: Literal["pass", "fail", "skip"]
    detail: str | None = None


class WorkflowStatus(Strict):
    scenario_id: str
    feature: str
    held_out: bool
    status: Literal["verified", "failed", "derived", "pending"]
    detail: str | None = None


class NegativeControl(Strict):
    scenario_id: str
    baseline_passed: bool
    mutant_failure_kind: str
    mutant_failed_step: int | None
    first_write_step: int | None
    protected: bool


class Verification(Strict):
    header: ArtifactHeader
    repo: str
    commit: str
    source_reachable: bool
    overall: Literal["verified", "partial", "failed"]
    workflows: list[WorkflowStatus]
    negative_control: list[NegativeControl]
    checks: list[CheckResult]
    limitations: list[str]
    cost_usd: float


def overall_status(workflows: list[WorkflowStatus]) -> str:
    """Verified only when every in-scope workflow is verified; otherwise partial (or failed if none pass)."""
    in_scope = [w for w in workflows if w.status != "derived"]
    if in_scope and all(w.status == "verified" for w in in_scope):
        return "verified"
    if any(w.status == "verified" for w in in_scope):
        return "partial"
    return "failed"
