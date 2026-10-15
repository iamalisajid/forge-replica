"""Validate scenarios on the source app before they may judge the replica.

A scenario becomes a *candidate* only when it passed every run (3/3 by default, fresh bindings and a
fresh context each time), never hit an environment failure, and has at least one non-vacuous
assertion. Environment and startup failures are reported apart from real failures: they say nothing
about whether the scenario is right, only that the source could not be exercised this time.

Runs on the host and calls the interpreter in-process, so a decrypted storage state stays in memory.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar
from collections.abc import Coroutine

from forge.models.appspec import OpWire
from forge.models.run import RunnerRequest
from forge.models.scenario import Scenario, ScenarioResult
from forge.stages.oracle.interpreter import Timeouts, origin_of, run_scenarios

T = TypeVar("T")
ENV_KINDS = frozenset({"env", "startup"})

Verdict = Literal["candidate", "failed", "env", "vacuous"]


@dataclass
class ValidationVerdict:
    scenario_id: str
    verdict: Verdict
    runs: int
    passed_runs: int
    reasons: list[str] = field(default_factory=list)


def run_sync(coro: Coroutine[object, object, T]) -> T:
    """Run a coroutine from sync code, also when the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def verdicts(scenarios: list[Scenario], results: list[ScenarioResult], runs: int) -> list[ValidationVerdict]:
    """One verdict per scenario: candidate, failed (the scenario or the source disagrees), env, or vacuous."""
    by_id: dict[str, list[ScenarioResult]] = defaultdict(list)
    for result in results:
        by_id[result.scenario_id].append(result)
    out = []
    for scenario in scenarios:
        mine = sorted(by_id.get(scenario.id, []), key=lambda r: r.run)
        passed = [r for r in mine if r.passed]
        env = [r for r in mine if not r.passed and r.failure_kind in ENV_KINDS]
        wrong = [r for r in mine if not r.passed and r.failure_kind not in ENV_KINDS]
        reasons = [f"run {r.run}: {r.failure_kind} at step {r.failed_step}: {r.message}" for r in wrong + env]
        if len(mine) < runs:
            reasons.append(f"only {len(mine)} of {runs} runs reported")
        if wrong:
            verdict: Verdict = "failed"
        elif env or len(mine) < runs:
            verdict = "env"
        elif min((r.non_vacuous_assertions for r in passed), default=0) < 1:
            verdict = "vacuous"
            reasons.append("every assertion already holds before the first action")
        else:
            verdict = "candidate"
        out.append(ValidationVerdict(scenario.id, verdict, len(mine), len(passed), reasons))
    return out


def candidates_from(scenarios: list[Scenario], results: list[ScenarioResult], runs: int) -> list[str]:
    return [v.scenario_id for v in verdicts(scenarios, results, runs) if v.verdict == "candidate"]


def validate_on_source(scenarios: list[Scenario], ops: list[OpWire], base_url: str, runs: int = 3,
                       storage_state: dict | str | Path | None = None, parallel: int = 1, *,
                       timeouts: Timeouts | None = None, fixtures: list | None = None,
                       allowed_origins: list[str] | None = None,
                       channel: str | None = "auto") -> tuple[list[ScenarioResult], list[str]]:
    """Run ``scenarios`` ``runs`` times on the source; return all results and the candidate scenario ids.

    ``storage_state`` may be a dict (kept in memory) or a path to a Playwright storage-state file.
    ``channel="auto"`` uses the host's Google Chrome when installed (forge.core.config.browser_channel).
    Use :func:`verdicts` on the results for the per-scenario reasons (env failures stay separate).
    """
    if channel == "auto":
        from forge.core.config import browser_channel  # host only; the runner image has no forge.core

        channel = browser_channel()
    origin = origin_of(base_url)
    if origin is None:
        raise ValueError(f"not an http(s) URL: {base_url}")
    in_memory = storage_state if isinstance(storage_state, dict) else None
    path = str(storage_state) if isinstance(storage_state, (str, Path)) else None
    req = RunnerRequest(target="source", base_url=base_url, scenarios=scenarios, operations=ops,
                        allowed_origins=allowed_origins or [origin], runs=runs, parallel=parallel,
                        storage_state_path=path, fixtures=fixtures or [])
    results = run_sync(run_scenarios(req, storage_state=in_memory, timeouts=timeouts, channel=channel))
    return results, candidates_from(scenarios, results, runs)
