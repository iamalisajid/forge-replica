"""Negative control: a scenario only counts if it catches a replica that forgets every write.

Two stacks of the generated repo run side by side on internal networks: the baseline and a mutant with
``FORGE_MUTANT=rollback-writes`` (writes answer 2xx but persist nothing). A candidate becomes
**protected** when it passes on the baseline and the mutant fails with ``failure_kind == "assertion"``
at a step after its first write. Generated screens refetch after writes, so any later assertion
legitimately catches the missing write. Startup, action and env failures on the mutant do not count.

Host-side module: the docker helpers are imported inside the function so that this package still
imports cleanly in the runner image (which ships only pydantic, playwright and forge.models).
"""
from __future__ import annotations

import concurrent.futures
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from forge.models.appspec import OpWire
from forge.models.run import RunnerRequest
from forge.models.scenario import Scenario, ScenarioResult
from forge.models.run import APP_URL_IN_NETWORK
from forge.models.verification import NegativeControl

APP_URL = APP_URL_IN_NETWORK  # the app's network alias: Chromium upgrades http://app (an HSTS-preloaded TLD)
log = logging.getLogger("forge.negative")


def judge(scenario_id: str, baseline: ScenarioResult | None, mutant: ScenarioResult | None) -> NegativeControl:
    """Apply the protection rule to one scenario's baseline and mutant results."""
    base_ok = bool(baseline and baseline.passed)
    kind = mutant.failure_kind if mutant is not None else "startup"
    failed_step = mutant.failed_step if mutant is not None else None
    first_write = mutant.first_write_step if mutant is not None else None
    if first_write is None and baseline is not None:
        first_write = baseline.first_write_step  # reported for context; protection needs the mutant's own
    protected = (base_ok and mutant is not None and not mutant.passed and kind == "assertion"
                 and mutant.first_write_step is not None and failed_step is not None
                 and failed_step > mutant.first_write_step)
    return NegativeControl(scenario_id=scenario_id, baseline_passed=base_ok, mutant_failure_kind=kind,
                           mutant_failed_step=failed_step, first_write_step=first_write, protected=protected)


def _request(scenarios: list[Scenario], ops: list[OpWire], parallel: int) -> RunnerRequest:
    return RunnerRequest(target="replica", base_url=APP_URL, scenarios=scenarios, operations=ops,
                         allowed_origins=[APP_URL], runs=1, parallel=parallel)


def _first(results: list[ScenarioResult], scenario_id: str) -> ScenarioResult | None:
    return next((r for r in results if r.scenario_id == scenario_id), None)


def _startup_failures(scenarios: list[Scenario], message: str) -> list[ScenarioResult]:
    return [ScenarioResult(scenario_id=s.id, target="replica", run=1, passed=False, failure_kind="startup",
                           message=message) for s in scenarios]


def run_on_stack(clone: Path, project: str, scenarios: list[Scenario], ops: list[OpWire], *, mutant: bool,
                 parallel: int = 2) -> list[ScenarioResult]:
    """Bring up one stack from a prepared clone, run the scenarios from inside it, tear it down."""
    from forge.core.docker import DockerError
    from forge.stages.verify import checks
    from forge.stages.verify.compose import compose_files, down, logs, run_in_network, up, VerifyError

    files = compose_files(clone, mutant=mutant)
    try:
        try:
            up(project, files)  # retries once, and waits for network pools held by a sibling stack
        except (DockerError, subprocess.SubprocessError) as err:
            log.warning("%s did not start: %s", project, err)
            return _startup_failures(scenarios, f"stack did not start: {str(err)[-500:]}")
        try:
            health = checks.health(project, files)
            if health.status != "pass":
                message = f"{health.detail}; app logs: {logs(project, files)[-800:]}"
                log.warning("%s is not healthy: %s", project, message)
                return _startup_failures(scenarios, message)
            return run_in_network(project, files, _request(scenarios, ops, parallel)).results
        except (VerifyError, DockerError, subprocess.SubprocessError) as err:
            log.warning("%s: runner failed: %s", project, err)
            return _startup_failures(scenarios, f"runner failed: {err}")
    finally:
        down(project, files)


def negative_control(repo: Path, run_id: str, candidates: list[Scenario], ops: list[OpWire], *,
                     parallel: int = 2, sha: str | None = None, workdir: Path | None = None,
                     keep_workdir: bool = False,
                     details: dict[str, list[ScenarioResult]] | None = None) -> list[NegativeControl]:
    """Run ``candidates`` on a baseline and a rollback-writes mutant of ``repo`` (HEAD unless ``sha``).

    Pass a dict as ``details`` to receive the raw results under "base" and "mutant" (messages included).
    """
    if not candidates:
        return []
    from forge.core.docker import project_name
    from forge.core.gitrepo import head_sha
    from forge.stages.verify.compose import prepare, write_mutant_overlay

    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix=f"forge-{run_id}-negative-"))
    try:
        clone = prepare(Path(repo), sha or head_sha(Path(repo)), work)
        write_mutant_overlay(clone)
        jobs = {"base": False, "mutant": True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {name: pool.submit(run_on_stack, clone, project_name(run_id, name), candidates, ops,
                                         mutant=is_mutant, parallel=parallel)
                       for name, is_mutant in jobs.items()}
            baseline, mutant = futures["base"].result(), futures["mutant"].result()
        if details is not None:
            details.update(base=baseline, mutant=mutant)
        return [judge(s.id, _first(baseline, s.id), _first(mutant, s.id)) for s in candidates]
    finally:
        if not keep_workdir and workdir is None:
            shutil.rmtree(work, ignore_errors=True)
