"""The runner image: forge's oracle imports with only pydantic + playwright, and runs the pilot on the demo.

Needs Docker and the demo (``docker compose -f demo/issue-tracker/compose.yaml up -d``).
"""
from __future__ import annotations

import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from forge.models.appspec import AppSpecDraft, op_wires
from forge.models.run import RunnerRequest, RunnerResponse
from forge.models.scenario import Scenario

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.docker


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


def _demo_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4001/api/projects", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


@pytest.fixture(scope="module")
def runner_image() -> str:
    if not _docker_ok():
        pytest.skip("docker daemon not available")
    from forge.core.docker import ensure_runner_image

    return ensure_runner_image()


def test_oracle_modules_import_in_the_runner_image(runner_image):
    modules = "forge.stages.oracle.interpreter, forge.stages.oracle.runner_cli, forge.stages.oracle.probe, " \
              "forge.stages.oracle.validate, forge.stages.oracle.negative"
    proc = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3", runner_image, "-c",
                           f"import {modules}; print('ok')"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


def test_runner_image_runs_the_pilot_against_the_demo(runner_image, tmp_path):
    if not _demo_ok():
        pytest.skip("demo not running: docker compose -f demo/issue-tracker/compose.yaml up -d")
    spec = AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())
    pilot = Scenario.model_validate_json((ROOT / "demo/issue-tracker/scenarios/pilot.json").read_text())
    base = "http://host.docker.internal:4001"
    io = tmp_path / "io"
    io.mkdir()
    io.chmod(0o777)
    request = RunnerRequest(target="source", base_url=base, scenarios=[pilot], operations=op_wires(spec),
                            allowed_origins=[base], runs=1)
    (io / "in.json").write_text(request.model_dump_json())
    proc = subprocess.run(
        ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway", "--shm-size", "512m",
         "-v", f"{io}:/io", runner_image, "--request", "/io/in.json", "--result", "/io/out.json"],
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-2000:]
    [result] = RunnerResponse.model_validate_json((io / "out.json").read_text()).results
    assert result.passed, result.message
    assert result.first_write_step == 3 and result.non_vacuous_assertions == 8
    assert {c.name for c in result.captured} == {"project_id", "issue_id"}
    assert result.offorigin_requests == []
