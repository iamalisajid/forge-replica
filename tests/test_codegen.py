"""Generate a replica repo from the demo spec, run it with Docker Compose and drive it like the original.

Needs Docker and Playwright's Chromium. Every Docker resource is named forge-laneb-* and removed at the end:
    uv run pytest tests/test_codegen.py -m docker
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from forge.core import docker
from forge.core.gitrepo import git
from forge.models.run import APP_URL_IN_NETWORK
from forge.models.scenario import Scenario
from forge.stages.generate.codegen import CodegenError, generate, render_check
from forge.stages.generate.manifest import EDITABLE_GLOBS, is_editable, sha256_file, tracked_files
from tests.test_render import demo_spec, wiki_spec

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "demo/issue-tracker/scenarios/pilot.json"
TIMEOUT_MS = 10_000

if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not available", allow_module_level=True)


# ---------------------------------------------------------------- stack helpers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """One compose project for a generated repo (forge-laneb-*)."""

    def __init__(self, repo: Path, project: str, *, files: list[str] | None = None, env: dict[str, str] | None = None):
        self.repo, self.project, self.files = repo, project, files or ["compose.yaml"]
        self.port = free_port()
        self.env = {**os.environ, "PORT": str(self.port), "FORGE_TEST_MODE": "1", **(env or {})}
        self.base_url = f"http://127.0.0.1:{self.port}"

    def compose(self, *args: str, env: dict[str, str] | None = None, timeout: int = 1200,
                check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-p", self.project]
        for f in self.files:
            cmd += ["-f", f]
        proc = subprocess.run(cmd + list(args), cwd=self.repo, env={**self.env, **(env or {})}, capture_output=True,
                              text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise AssertionError(f"docker compose {' '.join(args)} failed:\n{proc.stderr[-4000:]}")
        return proc

    def up(self, env: dict[str, str] | None = None) -> None:
        self.compose("up", "-d", "--build", env=env)
        self.wait_healthy()

    def wait_healthy(self, timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.base_url + "/__forge/health", timeout=3).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        logs = self.compose("logs", "--no-color", "--tail", "80", check=False)
        raise AssertionError(f"{self.project} never became healthy:\n{logs.stdout[-4000:]}")

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", "--rmi", "local", "-t", "5", check=False, timeout=300)


def leftovers(prefix: str) -> list[str]:
    out = []
    for kind in (["ps", "-a"], ["volume", "ls"], ["network", "ls"]):
        proc = subprocess.run(["docker", *kind, "--format", "{{.Names}}" if kind[0] == "ps" else "{{.Name}}"],
                              capture_output=True, text=True)
        out += [n for n in proc.stdout.split() if n.startswith(prefix)]
    return out


# ---------------------------------------------------------------- a minimal scenario interpreter
def _bind(text: str | None, values: dict[str, str]) -> str | None:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], text) if text is not None else None


def _locate(page, target, values):
    scope = page
    if target.within_role:
        within_name = _bind(target.within_name, values)
        scope = (page.get_by_role(target.within_role, name=within_name, exact=True) if within_name
                 else page.get_by_role(target.within_role))
        if target.within_has_text:
            scope = scope.filter(has_text=_bind(target.within_has_text, values))
    name = _bind(target.name, values)
    loc = scope.get_by_role(target.role, name=name, exact=target.exact) if name is not None else scope.get_by_role(
        target.role)
    if target.has_text:
        loc = loc.filter(has_text=_bind(target.has_text, values))
    return loc


def run_scenario(browser, base_url: str, scenario: Scenario) -> tuple[bool, int | None, str]:
    """Run every step; returns (passed, failed step index, message)."""
    from playwright.sync_api import expect

    token = secrets.token_hex(3)
    values = {b.name: f"{b.name}-{token}" for b in scenario.binds}
    context = browser.new_context()
    page = context.new_page()
    try:
        for i, step in enumerate(scenario.steps):
            value = _bind(step.value, values)
            try:
                if step.kind == "goto":
                    page.goto(base_url + _bind(step.route, values))
                elif step.kind == "reread":
                    url = page.url
                    context.close()
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(url)
                else:
                    loc = _locate(page, step.target, values)
                    if step.kind == "click":
                        loc.click(timeout=TIMEOUT_MS)
                    elif step.kind == "fill":
                        loc.fill(value, timeout=TIMEOUT_MS)
                    elif step.kind == "select":
                        loc.select_option(value, timeout=TIMEOUT_MS)
                    elif step.kind == "expect_visible":
                        expect(loc).to_be_visible(timeout=TIMEOUT_MS)
                    elif step.kind == "expect_hidden":
                        expect(loc).to_be_hidden(timeout=TIMEOUT_MS)
                    elif step.kind == "expect_text":
                        expect(loc).to_contain_text(value, timeout=TIMEOUT_MS)
                    elif step.kind == "expect_value":
                        expect(loc).to_have_value(value, timeout=TIMEOUT_MS)
                    else:
                        raise AssertionError(f"unsupported step kind {step.kind}")
            except Exception as exc:  # noqa: BLE001 - report the step that failed
                return False, i, f"step {i} {step.kind} {step.target}: {str(exc).splitlines()[0]}"
        return True, None, "passed"
    finally:
        context.close()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


# ---------------------------------------------------------------- the demo replica
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    run_id = f"laneb-{secrets.token_hex(3)}"
    manifest = generate(demo_spec(), tmp_path_factory.mktemp("apps"), run_id)
    stack = Stack(Path(manifest.repo), f"forge-{run_id}")
    try:
        stack.up()
        yield manifest, stack
    finally:
        stack.down()
        assert leftovers(stack.project) == []


def test_generates_a_new_git_repo(demo, tmp_path):
    manifest, stack = demo
    repo = Path(manifest.repo)
    assert repo.name.startswith("issue-tracker-laneb-")
    assert git(repo, "log", "--format=%an <%ae>|%s") == "forge <forge@localhost>|G1: generated baseline"
    assert git(repo, "rev-parse", "HEAD") == manifest.commit
    assert git(repo, "status", "--porcelain") == ""
    files = tracked_files(repo)
    assert "db/migrations/0001_init/migration.sql" in files and "README.md" in files
    assert not any(f.startswith(("docker/", "node_modules/")) or "/dist/" in f for f in files)
    sql = (repo / "db/migrations/0001_init/migration.sql").read_text()
    assert 'CREATE TABLE "issues"' in sql and "ON DELETE CASCADE" in sql
    # the same run id never reuses a directory
    with pytest.raises(CodegenError, match="already exists"):
        generate(demo_spec(), repo.parent, manifest.header.run_id)


def test_manifest_hashes_every_immutable_file(demo):
    manifest, _ = demo
    repo = Path(manifest.repo)
    assert manifest.editable_globs == EDITABLE_GLOBS
    assert manifest.stubs == []
    hashed = {e.path: e.sha256 for e in manifest.immutable}
    tracked = tracked_files(repo)
    assert set(hashed) == {p for p in tracked if not is_editable(p)}
    assert "web/src/generated/screens/Issue.tsx" not in hashed and "web/src/styles/app.css" not in hashed
    assert all(sha256_file(repo / p) == h for p, h in hashed.items())


def test_regenerate_and_diff_is_clean(demo):
    manifest, _ = demo
    assert render_check(demo_spec(), Path(manifest.repo), include_migration=True) == []


def test_health_and_routes_match_the_contract(demo):
    _, stack = demo
    assert httpx.get(stack.base_url + "/__forge/health").json() == {"status": "ok"}
    routes = httpx.get(stack.base_url + "/__forge/routes").json()
    expected = sorted((o.method, o.path) for o in demo_spec().operations)
    assert sorted((r["method"], r["path"]) for r in routes) == expected


def test_app_is_reachable_as_replica_inside_the_network(demo):
    """Runners inside the stack use http://replica:3000 (APP_URL_IN_NETWORK), on the internal network too."""
    _, stack = demo
    for network in ("backend", "edge"):
        proc = subprocess.run(
            ["docker", "run", "--rm", "--name", f"{stack.project}-probe-{network}", "--network",
             f"{stack.project}_{network}", "node:22-alpine", "wget", "-qO-", f"{APP_URL_IN_NETWORK}/__forge/health"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0 and json.loads(proc.stdout) == {"status": "ok"}, proc.stderr


def test_api_follows_the_wire_format(demo):
    _, stack = demo
    with httpx.Client(base_url=stack.base_url, timeout=10) as c:
        assert c.post("/api/projects", json={}).json() == {"error": "Name is required"}
        project = c.post("/api/projects", json={"name": "Wire"})
        assert project.status_code == 201 and set(project.json()) == {"id", "name", "created_at"}
        pid = project.json()["id"]
        issues = f"/api/projects/{pid}/issues"
        assert c.post(issues, json={"title": " "}).json() == {"error": "Title is required"}
        bad = c.post(issues, json={"title": "x", "status": "bogus"})
        assert (bad.status_code, bad.json()) == (422, {"error": "Status is not valid"})
        issue = c.post(issues, json={"title": "Wire issue"}).json()
        assert (issue["status"], issue["priority"]) == ("open", "medium")
        assert c.post(f"/api/issues/{issue['id']}/close").json()["status"] == "closed"
        assert list(c.get(issues).json()) == ["items"]
        assert c.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.delete(f"/api/projects/{pid}").status_code == 204
        assert c.get(issues).status_code == 404                      # cascade: the project is gone
        assert c.get("/projects/1", headers={"Accept": "text/html"}).text.lower().startswith("<!doctype html>")


def test_pilot_scenario_passes_in_the_browser(demo, browser):
    _, stack = demo
    scenario = Scenario.model_validate_json(PILOT.read_text())
    passed, step, message = run_scenario(browser, stack.base_url, scenario)
    assert passed, message


def test_rollback_writes_mutant(demo, browser):
    """Writes answer normally but persist nothing, so the pilot fails at its first assertion after a write."""
    _, stack = demo
    stack.compose("down", "-v", "-t", "5")
    stack.up(env={"FORGE_MUTANT": "rollback-writes"})
    created = httpx.post(stack.base_url + "/api/projects", json={"name": "Ghost"})
    assert created.status_code == 201 and created.json()["name"] == "Ghost"
    assert httpx.get(stack.base_url + "/api/projects").json() == {"items": []}
    scenario = Scenario.model_validate_json(PILOT.read_text())
    passed, step, message = run_scenario(browser, stack.base_url, scenario)
    first_write = next(i for i, s in enumerate(scenario.steps)
                       if s.kind == "click" and s.target.name == "Create project")
    assert not passed and step > first_write and scenario.steps[step].kind.startswith("expect_"), message


# ---------------------------------------------------------------- a broader spec, built offline like Verify does
def test_wiki_replica_builds_offline_and_runs(tmp_path, browser):
    run_id = f"laneb-{secrets.token_hex(3)}"
    manifest = generate(wiki_spec(), tmp_path / "apps", run_id)
    repo = Path(manifest.repo)
    assert manifest.stubs == ["archivePage"]
    assert "api/src/stubs/archivePage.ts" not in {e.path for e in manifest.immutable}

    # Verify builds with `network: none` from forge's base image.
    overlay = repo.parent / f"{run_id}-offline.yaml"
    overlay.write_text(json.dumps({"services": {s: {"build": {"network": "none"}} for s in ("app", "migrate")}}))
    stack = Stack(repo, f"forge-{run_id}", files=["compose.yaml", str(overlay)],
                  env={"FORGE_BASE_IMAGE": docker.ensure_base_image()})
    try:
        stack.up()
        typecheck = stack.compose("run", "--rm", "--no-deps", "-T", "app", "npm", "run", "--silent", "typecheck",
                                  check=False)
        assert typecheck.returncode == 0, typecheck.stdout[-3000:] + typecheck.stderr[-3000:]
        with httpx.Client(base_url=stack.base_url, timeout=10) as c:
            team = c.post("/api/teams", json={"name": "Core"})
            assert team.status_code == 201 and re.fullmatch(r"[0-9a-f-]{36}", team.json()["id"])
            assert c.post("/api/teams", json={"name": "Core"}).status_code == 409          # unique
            tid = team.json()["id"]
            ada = c.post(f"/api/teams/{tid}/members", json={"name": "Ada", "joined_on": "2026-02-03", "score": 4.5})
            assert ada.status_code == 201, ada.text
            body = ada.json()
            assert (body["active"], body["level"], body["joined_on"], body["score"]) == (True, 1, "2026-02-03", 4.5)
            bob = c.post(f"/api/teams/{tid}/members", json={"name": "Bob", "mentor_id": body["id"]}).json()
            assert bob["mentor_id"] == body["id"]
            assert c.post(f"/api/teams/{tid}/members", json={"name": "Cy", "mentor_id": 99999}).json() == {
                "error": "Mentor id is not valid"}
            assert [m["name"] for m in c.get(f"/api/teams/{tid}/members").json()["members"]] == ["Bob", "Ada"]
            put = c.put(f"/api/members/{bob['id']}", json={"level": "3", "active": False})
            assert (put.json()["level"], put.json()["active"]) == (3, False)
            page = c.post(f"/api/teams/{tid}/pages", json={"title": "Intro", "author_id": body["id"]}).json()
            assert page["state"] == "draft" and page["wordCount"] is None
            assert c.post(f"/api/pages/{page['id']}/publish").json()["state"] == "published"
            stub = c.post(f"/api/pages/{page['id']}/archive")
            assert (stub.status_code, stub.json()) == (501, {"error": "Not implemented: archivePage"})
            assert isinstance(c.get("/api/teams").json(), list)
        # The UI: the inline create form, the grid and the empty state.
        context = browser.new_context()
        ui = context.new_page()
        from playwright.sync_api import expect

        ui.goto(stack.base_url + "/")
        form = ui.get_by_role("form", name="Add team", exact=True)
        form.get_by_role("textbox", name="Team name", exact=True).fill("Docs")
        form.get_by_role("button", name="Add", exact=True).click()
        expect(ui.get_by_role("grid", name="All teams", exact=True).get_by_role("link", name="Docs",
                                                                               exact=True)).to_be_visible()
        ui.get_by_role("link", name="Core", exact=True).click()
        expect(ui.get_by_role("heading", name="Core", exact=True)).to_be_visible()
        expect(ui.get_by_role("list", name="Members", exact=True).get_by_role("link", name="Ada")).to_be_visible()
        ui.get_by_role("button", name="Add member", exact=True).click()
        dialog = ui.get_by_role("dialog", name="Add member", exact=True)
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Dee")
        dialog.get_by_role("combobox", name="Mentor", exact=True).select_option(label="Ada")
        dialog.get_by_role("button", name="Save member", exact=True).click()
        expect(ui.get_by_role("link", name="Dee", exact=True)).to_be_visible()
        context.close()
    finally:
        stack.down()
        assert leftovers(stack.project) == []
