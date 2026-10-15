"""Runner image entrypoint: read a RunnerRequest, run the scenarios headless, write a RunnerResponse.

    python3 -m forge.stages.oracle.runner_cli --request in.json --result out.json

Exit status: 0 when results were written (even if scenarios failed), 2 for an invalid request, 1 when
the runner itself crashed (a result file with ``env`` failures is still written when possible).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from forge.models.run import RunnerRequest, RunnerResponse
from forge.models.scenario import ScenarioResult
from forge.stages.oracle.interpreter import Timeouts, run_scenarios, with_timeouts


def _write(path: Path, response: RunnerResponse) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(response.model_dump_json(indent=2))
    os.chmod(tmp, 0o666)  # the host user must be able to read and remove it (the runner runs as root)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-runner", description=__doc__.splitlines()[0])
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--headed", action="store_true", help="show the browser (debugging on a desktop)")
    parser.add_argument("--channel", default=None, help="an installed browser, e.g. chrome (default: bundled Chromium)")
    parser.add_argument("--expect-timeout-ms", type=int, default=None)
    parser.add_argument("--action-timeout-ms", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        req = RunnerRequest.model_validate_json(args.request.read_text())
    except (OSError, ValidationError) as err:
        print(f"forge-runner: invalid request {args.request}: {err}", file=sys.stderr)
        return 2

    overrides = {k: v for k, v in (("expect_ms", args.expect_timeout_ms), ("action_ms", args.action_timeout_ms))
                 if v is not None}
    timeouts = with_timeouts(Timeouts(), **overrides)
    try:
        results = asyncio.run(run_scenarios(req, timeouts=timeouts, headless=not args.headed, channel=args.channel))
    except Exception as err:  # noqa: BLE001 - always leave a result file behind for the caller
        message = f"runner crashed: {type(err).__name__}: {err}"
        print(f"forge-runner: {message}", file=sys.stderr)
        results = [ScenarioResult(scenario_id=s.id, target=req.target, run=run, passed=False, failure_kind="env",
                                  message=message)
                   for s in req.scenarios for run in range(1, max(1, req.runs) + 1)]
        _write(args.result, RunnerResponse(results=results))
        return 1
    _write(args.result, RunnerResponse(results=results))
    passed = sum(r.passed for r in results)
    print(f"forge-runner: {passed}/{len(results)} runs passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
