"""Verify: overlay and lint, contract checks, negative control, and verify end to end.

Unit tests need nothing. ``docker`` tests build stacks named forge-lanec-* and always tear them down;
the fake replica (tests/fixtures/oracle/replica) stands in for a generated repo, and the last test runs
on a real generated repo once ``forge.stages.generate.codegen.generate`` exists.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

from forge.core.docker import project_name
from forge.core.gitrepo import commit_all, head_sha
from forge.models.appspec import AppSpecDraft, op_wires
from forge.models.base import ArtifactHeader
from forge.models.run import CodegenManifest, ManifestEntry, RunnerRequest
from forge.models.scenario import Scenario, ScenarioResult
from forge.stages.oracle.negative import judge, negative_control
from forge.stages.verify import checks
from forge.stages.verify.compose import (
    APP_URL,
    _load,
    compose_files,
    down,
    interpolate,
    io_dir_of,
    lint,
    lint_config,
    overlay_for,
    prepare,
    run_in_network,
    up,
    write_overlay,
)
from forge.stages.verify.report import verify
from tests.fixtures.oracle import fake_replica

ROOT = Path(__file__).resolve().parents[1]


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


needs_docker = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")


def _leftovers(prefix: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    nets = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True).stdout
    return [n for n in (out + nets).split() if n.startswith(prefix)]


def _header(run_id: str) -> ArtifactHeader:
    return ArtifactHeader(schema_id="verification", run_id=run_id, stage="verify")


# ---------- overlay ----------
BASE = {
    "services": {
        "db": {"image": "postgres:16-alpine", "networks": ["backend"]},
        "app": {"build": ".", "ports": ["127.0.0.1:3000:3000"], "networks": ["backend", "edge"]},
    },
    "networks": {"backend": {"internal": True}, "edge": {}},
}


def test_overlay_makes_every_network_internal_and_adds_the_runner(tmp_path):
    overlay = overlay_for(BASE, tmp_path, "forge-runner:abc")
    assert overlay["networks"] == {"backend": {"internal": True}, "edge": {"internal": True}}
    app = overlay["services"]["app"]
    assert app["environment"] == {"FORGE_TEST_MODE": "1"} and app["ports"] == [] and "build" not in app
    assert app["networks"] == {"backend": {"aliases": ["replica"]}, "edge": {"aliases": ["replica"]}}
    runner = overlay["services"]["runner"]
    assert runner["image"] == "forge-runner:abc" and runner["networks"] == ["backend", "edge"]
    assert runner["entrypoint"] == ["sleep", "infinity"]
    assert runner["volumes"] == [{"type": "bind", "source": str(tmp_path), "target": "/io"}]
    offline = overlay_for(BASE, tmp_path, "img", base_image="forge-template-base:abc")
    assert offline["services"]["app"]["build"] == {"network": "none", "args": {"BASE_IMAGE": "forge-template-base:abc"}}
    assert "db" not in offline["services"]  # nothing to change for an image-only service
    # A service on the implicit default network makes the default network internal too.
    loose = overlay_for({"services": {"app": {"image": "x"}}}, tmp_path, "img")
    assert loose["networks"] == {"default": {"internal": True}}
    assert loose["services"]["runner"]["networks"] == ["default"]


def test_overlay_file_resets_ports_and_records_the_io_dir(tmp_path):
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(BASE))
    io = tmp_path / "io"
    io.mkdir()
    path = write_overlay(tmp_path, io, "forge-runner:abc")
    text = path.read_text()
    assert "ports: !reset []" in text
    assert _load(path)["services"]["app"]["ports"] == []
    assert io_dir_of([tmp_path / "compose.yaml", path]) == Path(str(io.resolve()))


# ---------- lint ----------
def test_lint_accepts_the_template_and_a_clean_file(tmp_path):
    assert lint_config(BASE) == []
    template = ROOT / "template" / "compose.yaml"
    if template.exists():
        assert lint(template) == []


@pytest.mark.parametrize("service, expected", [
    ({"privileged": True}, "privileged"),
    ({"network_mode": "host"}, "network_mode host"),
    ({"pid": "host"}, "pid host"),
    ({"cap_add": ["SYS_ADMIN"]}, "cap_add SYS_ADMIN"),
    ({"devices": ["/dev/kvm"]}, "host devices"),
    ({"security_opt": ["seccomp:unconfined"]}, "disables confinement"),
    ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, "Docker socket"),
    ({"volumes": [{"type": "bind", "source": "/run/docker.sock", "target": "/s"}]}, "Docker socket"),
    ({"volumes": ["./src:/app/src"]}, "bind-mounts a host path"),
    ({"volumes": [{"type": "bind", "source": "/etc", "target": "/etc2"}]}, "bind-mounts a host path"),
    ({"ports": ["3000:3000"]}, "not bound to 127.0.0.1"),
    ({"ports": ["0.0.0.0:3000:3000"]}, "not bound to 127.0.0.1"),
    ({"ports": [3000]}, "not bound to 127.0.0.1"),
    ({"ports": [{"target": 3000, "published": 3000}]}, "not bound to 127.0.0.1"),
    ({"container_name": "fixed"}, "container_name"),
    ({"extends": {"file": "../other.yaml", "service": "x"}}, "extends another file"),
])
def test_lint_rejects_sandbox_escapes(service, expected):
    problems = lint_config({"services": {"app": {"image": "x", **service}}})
    assert any(expected in p for p in problems), problems


def test_lint_allows_named_volumes_loopback_ports_and_the_runner_io(tmp_path):
    io = tmp_path / "io"
    io.mkdir()
    config = {"services": {"db": {"image": "x", "volumes": ["db-data:/var/lib/postgresql/data", "/anonymous"]},
                           "app": {"image": "x", "ports": ["127.0.0.1:3000:3000", "[::1]:3001:3001",
                                                          {"target": 3000, "published": 3002, "host_ip": "127.0.0.1"}]},
                           "runner": {"image": "r", "volumes": [{"type": "bind", "source": str(io), "target": "/io"}]}},
              "volumes": {"db-data": {}}}
    assert lint_config(config, allowed_binds=[io]) == []
    assert any("bind-mounts" in p for p in lint_config(config))  # the io dir is only allowed when named
    bad = {"networks": {"edge": {"external": True}}, "volumes": {"v": {"driver_opts": {"type": "none", "o": "bind",
                                                                                         "device": "/home"}}}}
    problems = lint_config(bad)
    assert any("external" in p for p in problems) and any("host path" in p for p in problems)


def test_interpolation_uses_defaults():
    assert interpolate({"ports": ["127.0.0.1:${PORT:-3000}:3000"], "x": "$$HOME ${A} ${B-b}"}) == \
        {"ports": ["127.0.0.1:3000:3000"], "x": "$HOME  b"}


# ---------- contract checks (pure) ----------
def demo_spec() -> AppSpecDraft:
    return AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())


def test_routes_compare_against_the_contract():
    spec = demo_spec()
    expected = checks.contract_routes(spec)
    served = [{"method": op.method, "path": op.path} for op in spec.operations]
    served += [{"method": "HEAD", "path": "/api/projects"}, {"method": "GET", "path": "/*"},
               {"method": "GET", "path": "/__forge/health"}]
    assert checks.compare_routes(checks.route_set(served), expected).status == "pass"
    fastify_style = [f"{op.method} {op.path.replace('{project_id}', ':id').replace('{issue_id}', ':id')}"
                     for op in spec.operations]
    assert checks.compare_routes(checks.route_set({"routes": fastify_style}), expected).status == "pass"
    result = checks.compare_routes(checks.route_set(served[1:] + [{"method": "GET", "path": "/api/debug"}]), expected)
    assert result.status == "fail"
    assert "missing GET /api/projects" in result.detail and "GET /api/debug" in result.detail


def test_list_urls_fill_nested_parents():
    spec = demo_spec()
    top = checks.list_urls(spec)
    assert top == [f"{APP_URL}/api/projects"]
    nested = checks.list_urls(spec, {top[0]: {"items": [{"id": 4}, {"id": 2}]}})
    assert nested == top + [f"{APP_URL}/api/projects/4/issues", f"{APP_URL}/api/projects/2/issues"]


def test_immutable_hashes_detect_edits_symlinks_and_deletions(tmp_path):
    repo = fake_replica.make_repo(tmp_path / "repo")
    sha = head_sha(repo)
    manifest = fake_replica.manifest(repo, sha)
    assert checks.immutable_hashes(repo, manifest).status == "pass"
    (repo / "Dockerfile").write_text("FROM scratch\n")
    result = checks.immutable_hashes(repo, manifest)
    assert result.status == "fail" and "Dockerfile: modified" in result.detail
    # A file the verify stage overwrote itself is checked in the commit instead.
    assert checks.immutable_hashes(repo, manifest, sha=sha, overwritten=frozenset({"Dockerfile"})).status == "pass"
    (repo / "Dockerfile").unlink()
    (repo / "Dockerfile").symlink_to(repo / "server.mjs")
    assert "symlink" in checks.immutable_hashes(repo, manifest).detail
    (repo / "compose.yaml").unlink()
    assert "compose.yaml: missing" in checks.immutable_hashes(repo, manifest).detail
    escape = manifest.model_copy(update={"immutable": [ManifestEntry(path="../x", sha256="0")]})
    assert "escapes" in checks.immutable_hashes(repo, escape).detail


def _result(passed, kind="none", failed=None, first_write=3) -> ScenarioResult:
    return ScenarioResult(scenario_id="s", target="replica", run=1, passed=passed, failure_kind=kind,
                          failed_step=failed, first_write_step=first_write)


def test_negative_control_rule():
    base = _result(True)
    assert judge("s", base, _result(False, "assertion", 4)).protected
    assert judge("s", base, _result(False, "assertion", 10)).protected  # any assertion after the write
    assert not judge("s", base, _result(False, "assertion", 3)).protected  # not after the first write
    assert not judge("s", base, _result(False, "assertion", 2)).protected
    assert not judge("s", base, _result(False, "action", 5)).protected
    assert not judge("s", base, _result(False, "startup", 0)).protected
    assert not judge("s", base, _result(False, "env", 5)).protected
    assert not judge("s", base, _result(True)).protected  # passes on the mutant: dropped
    assert not judge("s", _result(False, "assertion", 4), _result(False, "assertion", 4)).protected
    assert not judge("s", base, _result(False, "assertion", 4, first_write=None)).protected
    assert not judge("s", base, None).protected
    nc = judge("s", base, _result(False, "assertion", 4))
    summary = (nc.baseline_passed, nc.mutant_failure_kind, nc.mutant_failed_step, nc.first_write_step)
    assert summary == (True, "assertion", 4, 3)


# ---------- docker: stacks of the fake replica ----------
def _demo_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4001/api/projects", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


@pytest.mark.docker
@needs_docker
def test_overlay_blocks_egress_but_the_runner_reaches_the_app(tmp_path):
    repo = fake_replica.make_repo(tmp_path / "replica")
    clone = prepare(repo, head_sha(repo), tmp_path / "work")
    files = compose_files(clone)
    project = project_name("lanec-t-egress", "stack")
    try:
        up(project, files)
        assert checks.health(project, files).status == "pass"
        egress, reachable = checks.egress(project, files)
        assert egress.status == "pass" and reachable == [], egress.detail
        assert checks.routes(project, files, fake_replica.spec()).status == "pass"
        request = RunnerRequest(target="replica", base_url=APP_URL, scenarios=[fake_replica.create_item()],
                                operations=op_wires(fake_replica.spec()), allowed_origins=[APP_URL])
        [result] = run_in_network(project, files, request).results
        assert result.passed, result.message
        assert list(io_dir_of(files).iterdir()) == []  # request/result files are cleaned up
    finally:
        down(project, files)
    assert _leftovers(project) == []
    if _demo_ok():  # control: the same probe on a normal network does reach the host
        from forge.core.docker import ensure_runner_image

        proc = subprocess.run(["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
                               "--entrypoint", "python3", ensure_runner_image(), "-m", "forge.stages.oracle.probe",
                               "egress", "--target", "host.docker.internal:4001"], capture_output=True, text=True,
                              timeout=120)
        assert json.loads(proc.stdout)[0]["reachable"] is True


@pytest.mark.docker
@needs_docker
def test_negative_control_protects_only_write_sensitive_scenarios(tmp_path):
    repo = fake_replica.make_repo(tmp_path / "replica")
    run_id = "lanec-t-negative"
    controls = negative_control(repo, run_id, [fake_replica.create_item(), fake_replica.open_and_cancel()],
                                op_wires(fake_replica.spec()))
    by_id = {c.scenario_id: c for c in controls}
    create = by_id["create-item"]
    assert create.protected and create.baseline_passed
    assert (create.mutant_failure_kind, create.mutant_failed_step, create.first_write_step) == ("assertion", 4, 3)
    cancel = by_id["open-and-cancel"]
    assert cancel.baseline_passed and not cancel.protected and cancel.mutant_failure_kind == "none"
    assert _leftovers(f"forge-{run_id}") == []


@pytest.mark.docker
@needs_docker
def test_verify_end_to_end_on_the_fake_replica(tmp_path):
    repo = fake_replica.make_repo(tmp_path / "replica")
    sha = head_sha(repo)
    run_id = "lanec-t-verify"
    v = verify(repo, sha, fake_replica.spec(), fake_replica.manifest(repo, sha), [fake_replica.create_item()],
               [fake_replica.delete_item(), fake_replica.open_and_cancel()], run_id, _header(run_id),
               source_url="http://127.0.0.1:4001", regenerate=False)
    statuses = {c.name: c.status for c in v.checks}
    assert statuses == {"compose_lint": "pass", "immutable_hashes": "pass", "health": "pass", "routes": "pass",
                        "egress_blocked": "pass", "request_audit": "pass", "restart_persistence": "pass"}, v.checks
    workflows = {w.scenario_id: w for w in v.workflows}
    assert workflows["create-item"].status == "verified" and not workflows["create-item"].held_out
    assert workflows["delete-item"].status == "verified" and workflows["delete-item"].held_out
    assert workflows["open-and-cancel"].status == "verified"
    assert v.overall == "verified" and v.source_reachable is False and v.commit == sha
    [control] = v.negative_control
    assert control.scenario_id == "delete-item" and control.protected
    assert _leftovers(f"forge-{run_id}") == []


@pytest.mark.docker
@needs_docker
def test_verify_fails_on_tampering_and_on_a_fetch_to_the_original(tmp_path):
    leak = "fetch('http://host.docker.internal:4001/api/projects').catch(() => {});"
    repo = fake_replica.make_repo(tmp_path / "replica", extra_script=leak)
    manifest = fake_replica.manifest(repo, head_sha(repo))
    (repo / "Dockerfile").write_text((repo / "Dockerfile").read_text() + "LABEL tampered=true\n")
    sha = commit_all(repo, "builder round: edits an immutable file")
    run_id = "lanec-t-tamper"
    v = verify(repo, sha, fake_replica.spec(), manifest, [fake_replica.create_item()], [], run_id, _header(run_id),
               mutant_for_heldout=False, regenerate=False)
    checks_by_name = {c.name: c for c in v.checks}
    hashes = checks_by_name["immutable_hashes"]
    assert hashes.status == "fail" and "Dockerfile" in hashes.detail
    assert checks_by_name["request_audit"].status == "fail"
    assert "host.docker.internal:4001" in checks_by_name["request_audit"].detail
    [workflow] = v.workflows
    assert workflow.status == "failed" and "requested other origins" in workflow.detail
    assert v.overall == "failed"
    assert _leftovers(f"forge-{run_id}") == []


# ---------- docker: a repo generated by codegen (lane B) ----------
def _codegen():
    try:
        from forge.stages.generate.codegen import generate
    except ImportError:
        return None
    return generate


@pytest.mark.docker
@needs_docker
def test_verify_a_generated_repo_end_to_end(tmp_path):
    generate = _codegen()
    if generate is None:
        pytest.skip("forge.stages.generate.codegen.generate (lane B) is not available yet")
    spec = demo_spec()
    run_id = "lanec-t-g1"
    manifest = generate(spec, tmp_path / "apps", run_id)
    assert isinstance(manifest, CodegenManifest)
    repo, sha = Path(manifest.repo), manifest.commit
    pilot = Scenario.model_validate_json((ROOT / "demo/issue-tracker/scenarios/pilot.json").read_text())
    heldout = [Scenario.model_validate_json(f.read_text())
               for f in sorted((ROOT / "demo/issue-tracker/heldout").glob("*.json"))]

    details: dict = {}
    [control] = negative_control(repo, run_id, [pilot], op_wires(spec), sha=sha, details=details)
    assert control.baseline_passed, details
    assert control.protected, details
    assert (control.mutant_failure_kind, control.mutant_failed_step, control.first_write_step) == ("assertion", 4, 3)

    v = verify(repo, sha, spec, manifest, [pilot], heldout, run_id, _header(run_id), prior_controls=[control],
               source_url="http://127.0.0.1:4001")
    statuses = {c.name: c.status for c in v.checks}
    for name in ("compose_lint", "immutable_hashes", "regenerate_and_diff", "health", "routes", "egress_blocked",
                 "request_audit"):
        assert statuses.get(name) == "pass", [c for c in v.checks if c.name == name]
    assert statuses.get("restart_persistence") in ("pass", "skip")
    workflows = {w.scenario_id: w for w in v.workflows}
    assert workflows[pilot.id].status == "verified", workflows[pilot.id]
    assert v.overall in ("verified", "partial") and v.source_reachable is False
    assert _leftovers(f"forge-{run_id}") == []

    # Tampering with an immutable file is caught (static check on a clone of the tampered commit).
    target = next(e.path for e in manifest.immutable if (repo / e.path).is_file())
    (repo / target).write_bytes((repo / target).read_bytes() + b"\n")
    tampered = commit_all(repo, "tamper")
    clone = tmp_path / "tampered"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", tampered], check=True)
    result = checks.immutable_hashes(clone, manifest)
    assert result.status == "fail" and target in result.detail
    assert hashlib.sha256((clone / target).read_bytes()).hexdigest() != next(
        e.sha256 for e in manifest.immutable if e.path == target)
