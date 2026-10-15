"""Verify a generated repo at one commit and produce the Verification report.

Steps: fresh clone at the SHA with the pinned overlay; compose lint (as written and as resolved);
immutable hashes and regenerate-and-diff; stack up with every network internal; from inside the
network: health, routes == contract, egress probe, protected and held-out scenarios (request audit),
restart persistence; then the rollback-writes mutant for held-out scenarios that were never
negative-controlled. The stacks are always torn down. A failed check blocks every workflow from being
``verified``.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from forge.core.docker import DockerError, project_name
from forge.models.appspec import AppSpecDraft, op_wires
from forge.models.base import ArtifactHeader
from forge.models.run import CodegenManifest, RunnerRequest
from forge.models.scenario import Scenario, ScenarioResult
from forge.models.verification import CheckResult, NegativeControl, Verification, WorkflowStatus, overall_status
from forge.stages.oracle.negative import judge, run_on_stack
from forge.stages.verify import checks
from forge.stages.verify.compose import (
    APP_URL,
    OVERLAY,
    VerifyError,
    compose_files,
    down,
    find_compose_file,
    io_dir_of,
    lint,
    lint_config,
    logs,
    prepare,
    resolved_config,
    run_in_network,
    up,
    write_mutant_overlay,
)


def _egress_targets(source_url: str | None) -> list[str]:
    targets = list(checks.DEFAULT_EGRESS_TARGETS)
    if source_url:
        from urllib.parse import urlsplit

        parts = urlsplit(source_url)
        host = parts.hostname or ""
        if host in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            host = "host.docker.internal"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        target = f"{host}:{port}"
        if target not in targets:
            targets.append(target)
    return targets


def _workflow(s: Scenario, held_out: bool, results: list[ScenarioResult], not_run: str | None,
              negative: dict[str, NegativeControl]) -> WorkflowStatus:
    def status(value: str, detail: str | None) -> WorkflowStatus:
        return WorkflowStatus(scenario_id=s.id, feature=s.feature, held_out=held_out, status=value, detail=detail)

    if not results:
        return status("failed", f"not run: {not_run or 'no result from the runner'}")
    failed = [r for r in results if not r.passed]
    if failed:
        r = failed[0]
        return status("failed", f"run {r.run}: {r.failure_kind} at step {r.failed_step}: {r.message}")
    leaks = sorted({u for r in results for u in r.offorigin_requests})
    if leaks:
        return status("failed", f"passed, but requested other origins: {', '.join(leaks[:3])}")
    if min(r.non_vacuous_assertions for r in results) < 1:
        return status("failed", "vacuous on the replica: every assertion held before the first action")
    control = negative.get(s.id)
    if control is not None and not control.protected:
        return status("derived", f"dropped: the rollback-writes mutant ended with {control.mutant_failure_kind} at "
                                 f"step {control.mutant_failed_step} (first write {control.first_write_step})")
    runs = len(results)
    return status("verified", f"passed {runs}/{runs} on the replica" + (" (held out)" if held_out else ""))


def verify(repo: Path, sha: str, spec: AppSpecDraft, manifest: CodegenManifest, protected: list[Scenario],
           heldout: list[Scenario], run_id: str, header: ArtifactHeader, *, source_url: str | None = None,
           runs: int = 1, parallel: int = 2, mutant_for_heldout: bool = True, cost_usd: float = 0.0,
           prior_controls: list[NegativeControl] | None = None, regenerate: bool = True,
           workdir: Path | None = None, keep_workdir: bool = False) -> Verification:
    """Verify ``repo`` at ``sha``. ``protected`` passed the negative control; ``heldout`` never reached the builder.

    ``prior_controls`` (the negative control that made ``protected`` protected) are copied into the report
    next to the ones run here for held-out scenarios.
    """
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix=f"forge-{run_id}-verify-"))
    project = project_name(run_id, "verify")
    check_list: list[CheckResult] = []
    limitations: list[str] = []
    results: list[ScenarioResult] = []
    negative: dict[str, NegativeControl] = {}
    not_run: str | None = None
    source_reachable = False
    files: list[Path] = []
    clone: Path | None = None
    seen: set[str] = set()
    scenarios = [s for s in [*protected, *heldout] if not (s.id in seen or seen.add(s.id))]
    targets = _egress_targets(source_url)
    try:
        clone = prepare(Path(repo), sha, work)
        files = compose_files(clone)
        problems = lint(find_compose_file(clone))
        try:
            problems += [f"resolved: {p}" for p in lint_config(resolved_config(project, files), base_dir=clone,
                                                                allowed_binds=[io_dir_of(files)], resolved=True)]
        except DockerError as err:
            problems.append(f"docker compose config failed: {str(err)[-300:]}")
        check_list.append(CheckResult(name="compose_lint", status="fail" if problems else "pass",
                                      detail="; ".join(problems[:10]) if problems else
                                      "no privileged, host-namespace, socket, bind-mount or public-port settings"))
        check_list.append(checks.immutable_hashes(clone, manifest, sha=sha, overwritten=frozenset({str(OVERLAY)})))
        check_list.append(checks.copied_records(clone))
        if regenerate:
            check_list.append(checks.regenerate_and_diff(clone, spec))
        if problems:
            not_run = "compose lint failed, so the stack was not started"
        else:
            try:
                up(project, files)
            except DockerError as err:
                not_run = f"the stack did not start: {str(err)[-400:]}"
                check_list.append(CheckResult(name="health", status="fail", detail=not_run))
            if not_run is None:
                health = checks.health(project, files)
                if health.status != "pass":
                    detail = f"{health.detail}; app logs: {logs(project, files)[-600:]}"
                    health = health.model_copy(update={"detail": detail})
                    not_run = "the app never became healthy"
                check_list.append(health)
            if not_run is None:
                check_list.append(checks.routes(project, files, spec))
                egress, reachable = checks.egress(project, files, targets)
                check_list.append(egress)
                source_reachable = bool(reachable)
                request = RunnerRequest(target="replica", base_url=APP_URL, scenarios=scenarios,
                                        operations=op_wires(spec), allowed_origins=[APP_URL], runs=runs,
                                        parallel=parallel)
                results = run_in_network(project, files, request).results
                check_list.append(checks.request_audit(results))
                check_list.append(checks.restart_persistence(project, files, spec))
    except (DockerError, VerifyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as err:
        check_list.append(CheckResult(name="verify", status="fail", detail=f"{type(err).__name__}: {err}"[:600]))
        not_run = not_run or f"verify stopped early ({type(err).__name__})"
    finally:
        if files:
            down(project, files)

    by_id: dict[str, list[ScenarioResult]] = defaultdict(list)
    for r in results:
        by_id[r.scenario_id].append(r)

    # The mutant for held-out scenarios: they were validated on the source but never negative-controlled.
    pending = [s for s in heldout if s.mutating and by_id.get(s.id) and all(r.passed for r in by_id[s.id])]
    if mutant_for_heldout and pending and clone is not None and not any(c.status == "fail" for c in check_list):
        try:
            write_mutant_overlay(clone)
            mutant = run_on_stack(clone, project_name(run_id, "verify-mutant"), pending, op_wires(spec),
                                  mutant=True, parallel=parallel)
            for s in pending:
                negative[s.id] = judge(s.id, by_id[s.id][0], next((m for m in mutant if m.scenario_id == s.id), None))
        except (DockerError, VerifyError, OSError, subprocess.SubprocessError) as err:
            limitations.append(f"The mutant check for held-out scenarios did not run: {err}")
    if not keep_workdir and workdir is None:
        shutil.rmtree(work, ignore_errors=True)

    workflows = [_workflow(s, False, by_id.get(s.id, []), not_run, negative) for s in protected]
    workflows += [_workflow(s, True, by_id.get(s.id, []), not_run, negative)
                  for s in heldout if s.id not in {p.id for p in protected}]
    blocking = ", ".join(c.name for c in check_list if c.status == "fail")
    if blocking:  # a passing scenario proves nothing about a stack that failed integrity or isolation
        workflows = [w.model_copy(update={"status": "failed",
                                          "detail": f"blocked by failed checks ({blocking}); scenario: {w.detail}"})
                     if w.status == "verified" else w for w in workflows]

    limitations += [
        f"Scenarios ran {runs} time(s) on the replica, from a runner inside the internal network.",
        "Egress was probed for " + ", ".join(targets) + "; other destinations were not tried.",
        "Restart persistence compares the responses of list operations only.",
    ]
    if not regenerate:
        limitations.append("Regenerate-and-diff was not run; integrity relies on the manifest's immutable hashes.")
    if not mutant_for_heldout:
        limitations.append("Held-out scenarios were not run against the rollback-writes mutant.")
    return Verification(header=header, repo=str(repo), commit=sha, source_reachable=source_reachable,
                        overall=overall_status(workflows), workflows=workflows,
                        negative_control=[*(prior_controls or []), *negative.values()], checks=check_list,
                        limitations=limitations,
                        cost_usd=cost_usd)
