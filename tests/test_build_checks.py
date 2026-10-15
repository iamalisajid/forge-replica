"""Tests for the build stage's checks (run_checks) and the builder loop (run_builder).

* Unit tests (no Docker, no API): snapshot rules, output summaries, scenario reporting, the round brief, the
  tamper/scope verdict and revert, the screenshot tool, and a scripted driver that plays the SDK's part so the whole
  round loop (guard, rejection, contract requests, commits, stop on green) runs for $0.
* ``-m docker``: real offline typecheck/build checks of a minimal repo, and of a lane B generated repo when
  ``forge.stages.generate.codegen.generate`` exists.
* ``-m live`` (about $0.75, also needs Docker): the builder implements a trivial stub.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from forge.core import docker, gitrepo
from forge.core.config import DEFAULT_MODEL
from forge.models.appspec import AppSpecDraft
from forge.models.base import ArtifactHeader
from forge.models.run import CodegenManifest, ManifestEntry
from forge.models.scenario import ScenarioResult
from forge.stages.build import agent
from forge.stages.build.checks import (
    CheckReport,
    check_name,
    copy_build_context,
    run_check_suite,
    run_checks,
    summarize,
)
from forge.stages.build.guard import CONTRACT_CHANGE_FILE, glob_regex

ROOT = Path(__file__).resolve().parents[1]
EDITABLE = ["web/src/generated/screens/**", "web/src/styles/**", "api/src/stubs/**"]
STUB = "api/src/stubs/closeIssue.ts"

FIXTURE_FILES = {
    "package.json": json.dumps({
        "name": "forge-check-fixture", "private": True, "version": "1.0.0", "type": "module",
        "scripts": {"typecheck": "tsc -p api --noEmit && tsc -p web --noEmit",
                    "build": "vite build --config web/vite.config.ts && tsc -p api"}}, indent=2) + "\n",
    "api/tsconfig.json": json.dumps({"compilerOptions": {
        "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext", "outDir": "dist",
        "rootDir": "src", "strict": True, "skipLibCheck": True}, "include": ["src"]}) + "\n",
    "api/src/types.ts": 'export interface Issue { id: number; title: string; status: "open" | "closed"; }\n',
    "api/src/server.ts": ('import type { Issue } from "./types.js";\n'
                          'import { closeIssue } from "./stubs/closeIssue.js";\n'
                          'const issue: Issue = { id: 1, title: "Fix login", status: "open" };\n'
                          "console.log(closeIssue(issue).status);\n"),
    STUB: ('import type { Issue } from "../types.js";\n\n'
           "// STUB closeIssue (POST /api/issues/{issue_id}/close). The original app sets status to \"closed\"\n"
           "// and returns the updated issue.\n"
           "export function closeIssue(issue: Issue): Issue {\n"
           '  throw new Error("not implemented: closeIssue");\n'
           "}\n"),
    "web/tsconfig.json": json.dumps({"compilerOptions": {
        "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler", "jsx": "react-jsx", "strict": True,
        "skipLibCheck": True, "noEmit": True, "types": ["vite/client"]}, "include": ["src"]}) + "\n",
    "web/vite.config.ts": ('import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\n'
                           'export default defineConfig({ root: "web", plugins: [react()], '
                           'build: { outDir: "dist", emptyOutDir: true } });\n'),
    "web/index.html": ('<!doctype html><html><head><meta charset="utf-8"><title>Fixture</title></head><body>'
                       '<div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>\n'),
    "web/src/main.tsx": ('import { createRoot } from "react-dom/client";\nimport "./styles/app.css";\n'
                         'import { Projects } from "./generated/screens/Projects";\n'
                         'createRoot(document.getElementById("root")!).render(<Projects />);\n'),
    "web/src/generated/screens/Projects.tsx": (
        "export function Projects() {\n  return (\n    <main>\n      <h1>Projects</h1>\n"
        '      <button type="button">New project</button>\n      <p>No projects yet.</p>\n    </main>\n  );\n}\n'),
    "web/src/styles/app.css": "body { font-family: sans-serif; }\n",
    ".gitignore": "node_modules/\ndist/\n",
}


# ---------------------------------------------------------------- helpers
def demo_spec() -> AppSpecDraft:
    return AppSpecDraft.model_validate_json((ROOT / "tests/fixtures/demo_appspec.json").read_text())


def solid_png(size: int = 64, rgb=(40, 90, 200)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_fixture_repo(path: Path, base_image: str = "forge-template-base:none") -> tuple[Path, CodegenManifest]:
    """A minimal 'generated' repo: immutable app files, an editable stub, screen and stylesheet, one commit."""
    files = dict(FIXTURE_FILES, Dockerfile=f"FROM {base_image}\n")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    gitrepo.init_repo(path)
    sha = gitrepo.commit_all(path, "G1")
    editable = [glob_regex(g) for g in EDITABLE]
    immutable = [ManifestEntry(path=rel, sha256=hashlib.sha256((path / rel).read_bytes()).hexdigest())
                 for rel in sorted(files) if not any(rx.fullmatch(rel.lower()) for rx in editable)]
    manifest = CodegenManifest(header=ArtifactHeader(schema_id="forge.manifest/1", run_id="laned", stage="generate"),
                               repo=str(path), commit=sha, immutable=immutable, editable_globs=EDITABLE,
                               stubs=["closeIssue"])
    return path, manifest


def local_base_image() -> str:
    """An existing forge base image (any tag), else the current one built from template/."""
    tag = docker.base_image_tag()
    if docker.image_exists(tag):
        return tag
    proc = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", docker.BASE_IMAGE],
                          capture_output=True, text=True)
    tags = [t for t in proc.stdout.split() if not t.endswith(":<none>")]
    return tags[0] if tags else docker.ensure_base_image()


def leftovers(run_id: str) -> list[str]:
    names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    images = subprocess.run(["docker", "images", "--format", "{{.Repository}}"], capture_output=True, text=True).stdout
    prefix = f"forge-{run_id}-check-"
    return [n for n in (names + images).split() if n.startswith(prefix)]


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(not docker_available(), reason="Docker daemon is not running")


# ---------------------------------------------------------------- snapshot and summaries (no Docker)
def test_copy_build_context_takes_tracked_and_untracked_but_no_links_or_outputs(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (repo / "web/src/styles/new.css").write_text("a{}")                  # untracked: copied
    (repo / "node_modules/x").mkdir(parents=True)
    (repo / "node_modules/x/index.js").write_text("x")                   # ignored + skipped
    (repo / "api/dist").mkdir()
    (repo / "api/dist/server.js").write_text("x")                        # build output
    (repo / "web/src/styles/leak.css").symlink_to(outside / "secret.txt")
    (repo / "web/src/generated/screens/out").symlink_to(outside, target_is_directory=True)
    os.mkfifo(repo / "web/src/styles/pipe")
    dest = tmp_path / "ctx"
    copy_build_context(repo, dest)
    copied = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() or p.is_symlink())
    assert "web/src/styles/new.css" in copied and STUB in copied and "package.json" in copied
    assert not any(p.startswith(("node_modules", "api/dist", ".git/")) for p in copied)
    assert not any("leak" in p or "/out" in p or "pipe" in p for p in copied)
    assert not any(p.is_symlink() for p in dest.rglob("*"))


def test_copy_build_context_skips_files_below_a_replaced_directory(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    (outside / "screens").mkdir(parents=True)
    (outside / "screens/Projects.tsx").write_text("stolen")
    real = repo / "web/src/generated/screens"
    for f in real.iterdir():
        f.unlink()
    real.rmdir()
    real.symlink_to(outside / "screens", target_is_directory=True)       # tracked paths now go through a link
    dest = tmp_path / "ctx"
    copy_build_context(repo, dest)
    assert not (dest / "web/src/generated/screens/Projects.tsx").exists()


def test_summarize_typescript_errors():
    out = ("\x1b[31m/app/api/src/a.ts(2,7): error TS2322: Type 'number' is not assignable to type 'string'.\x1b[0m\n"
           "api/src/b.ts(3,1): error TS2304: Cannot find name 'x'.\n"
           "  Did you mean 'y'?\n"
           + "".join(f"web/src/c{i}.tsx(1,1): error TS1005: ';' expected.\n" for i in range(20)))
    text = summarize(out)
    assert text.startswith("22 TypeScript error(s):")
    assert "api/src/a.ts(2,7): error TS2322" in text and "/app/" not in text and "\x1b" not in text
    assert "Did you mean 'y'?" in text
    assert "... and 10 more" in text


def test_summarize_vite_error_drops_stack():
    out = ("vite v7.3.7 building for production...\ntransforming...\n"
           "error during build:\nCould not resolve \"./nope\" from \"/app/web/src/main.tsx\"\n"
           "file: /app/web/src/main.tsx\n    at getRollupError (file:///app/node_modules/rollup/x.js:1:1)\n")
    text = summarize(out)
    assert text.startswith("error during build:")
    assert 'from "web/src/main.tsx"' in text and "getRollupError" not in text


def test_summarize_clips_long_output():
    assert len(summarize("Error: " + "x" * 10_000)) < 3700


def test_check_name_is_unique_and_docker_safe():
    a, b = check_name("20261015-212400-AB_12"), check_name("20261015-212400-AB_12")
    assert a != b and a.startswith("forge-20261015-212400-ab-12-check-")
    assert all(c.isalnum() and c.islower() or c.isdigit() or c in "-_." for c in a)


def test_unknown_kind_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError):
        run_checks(tmp_path, "lint", run_id="laned")   # type: ignore[arg-type]


# ---------------------------------------------------------------- scenarios through a backend (no Docker)
def _result(sid: str, passed: bool, step: int | None = None, kind: str = "none", msg: str | None = None):
    return ScenarioResult(scenario_id=sid, target="replica", run=1, passed=passed, failed_step=step,
                          failure_kind=kind, message=msg)


def test_scenarios_check_reports_each_scenario(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    spec = demo_spec()
    seen = {}

    def backend(context, scenarios, operations, *, run_id, name, timeout_s):
        seen.update(context=context, ids=[s.id for s in scenarios], ops=len(operations), name=name)
        assert (context / STUB).is_file()
        return [_result(scenarios[0].id, False, 6, "assertion", "expected link 'abc' to be visible")]

    report = run_checks(repo, "scenarios", None, run_id="laned", timeout_s=60, scenarios=spec.scenarios,
                        operations=agent.op_wires(spec), scenario_backend=backend)
    assert report.status == "failed" and not report.passed
    assert seen["ids"] == [spec.pilot_scenario] and seen["ops"] == len(spec.operations)
    assert seen["name"].startswith("forge-laned-check-")
    assert f"FAIL {spec.pilot_scenario} at step 6" in report.summary and "(assertion)" in report.summary
    assert report.results and report.results[0].failed_step == 6

    ok = run_checks(repo, "scenarios", [spec.pilot_scenario, "nope"], run_id="laned", timeout_s=60,
                    scenarios=spec.scenarios, scenario_backend=lambda c, s, o, **k: [_result(s[0].id, True)])
    assert ok.passed and f"PASS {spec.pilot_scenario}" in ok.summary and "nope" in ok.summary
    unknown = run_checks(repo, "scenarios", ["nope"], run_id="laned", scenarios=spec.scenarios,
                         scenario_backend=lambda c, s, o, **k: [])
    assert unknown.status == "error" and "unknown scenario id" in unknown.summary


def test_scenarios_check_unavailable_and_crashing_backends(tmp_path, monkeypatch):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    spec = demo_spec()
    monkeypatch.setattr("forge.stages.build.checks.default_scenario_backend", lambda: None)
    report = run_checks(repo, "scenarios", run_id="laned", scenarios=spec.scenarios)
    assert report.status == "unavailable"
    assert run_checks(repo, "scenarios", run_id="laned", scenarios=[]).status == "unavailable"

    def boom(*args, **kwargs):
        raise RuntimeError("compose up failed")
    crashed = run_checks(repo, "scenarios", run_id="laned", scenarios=spec.scenarios, scenario_backend=boom)
    assert crashed.status == "error" and "compose up failed" in crashed.summary


# ---------------------------------------------------------------- builder helpers (no Docker, no API)
def test_round_brief_states_the_contract(tmp_path):
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    spec = demo_spec()
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "spec.json").write_text("{}")
    for suffix in (".snapshot.txt", ".dom.html", ".styles.json"):
        (ctx / f"projects{suffix}").write_text("x")
    (ctx / "issue.styles.json").write_text("{}")
    failing = [_result(spec.pilot_scenario, False, 3, "action", "button 'New issue' not found")]
    brief = agent.round_brief(repo=repo, spec=spec, manifest=manifest, failing=failing,
                              screenshots={"projects": tmp_path / "p.png"}, context_dir=ctx, round_no=1, rounds=2,
                              budget_usd=1.23, feedback=None)
    assert str(repo) in brief and "spec.json" in brief and "$1.23" in brief
    assert "Primary goals, in this order: (1) the failing scenarios below pass" in brief
    assert "(2) every screen looks the same as the original" in brief and "Records come from the API" in brief
    assert "reference files: projects.snapshot.txt, projects.dom.html, projects.styles.json" in brief
    assert "reference files: issue.styles.json" in brief
    context_line = next(line for line in brief.splitlines() if line.startswith("Read-only context directory"))
    assert "projects.dom.html" not in context_line and "per-screen reference files" in context_line
    assert "closeIssue: POST /api/issues/{issue_id}/close" in brief and "status = closed" in brief
    assert f"stub file: {STUB}" in brief
    assert 'button "New project" [open_create]' in brief and 'dialog "New project"' in brief
    assert "screen projects: route /" in brief and "screenshot: get_screenshot" in brief
    assert "3. " in brief and "<-- failed here" in brief


def test_violations_detect_tampering_and_scope_then_revert(tmp_path):
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    reference = {e.path: e.sha256 for e in manifest.immutable}
    editable = [glob_regex(g) for g in EDITABLE]
    pre = gitrepo.head_sha(repo)
    assert agent._violations(repo, reference, editable) == []
    (repo / STUB).write_text("export const ok = 1;\n")                       # allowed
    (repo / CONTRACT_CHANGE_FILE).write_text("{}")                            # allowed
    assert agent._violations(repo, reference, editable) == []
    (repo / "package.json").write_text("{}")                                  # immutable
    (repo / "web/src/extra.ts").write_text("x")                               # outside the editable globs
    found = agent._violations(repo, reference, editable)
    assert "package.json (immutable) changed" in found
    assert "web/src/extra.ts is outside the editable paths" in found
    assert not any(v.startswith("package.json is outside") for v in found)
    agent._revert(repo, pre)
    assert agent._status_paths(repo) == []
    assert (repo / "package.json").read_text() == FIXTURE_FILES["package.json"]
    assert (repo / STUB).read_text() == FIXTURE_FILES[STUB]


def test_contract_request_is_taken_out_of_the_tree(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    assert agent._take_contract_request(repo) is None
    (repo / CONTRACT_CHANGE_FILE).write_text('{"requests": []}')
    assert agent._take_contract_request(repo) == '{"requests": []}'
    assert not (repo / CONTRACT_CHANGE_FILE).exists()


def test_is_green():
    def rep(kind, status):
        return CheckReport(kind=kind, status=status, passed=status == "passed", summary="", duration_s=0)
    assert agent.is_green([rep("typecheck", "passed"), rep("build", "passed"), rep("scenarios", "unavailable")])
    assert not agent.is_green([rep("typecheck", "passed"), rep("build", "failed"), rep("scenarios", "unavailable")])
    assert not agent.is_green([rep("typecheck", "passed"), rep("build", "passed"), rep("scenarios", "failed")])
    assert not agent.is_green([rep("scenarios", "passed")])


def test_png_problem():
    assert agent.png_problem(solid_png()) is None
    assert agent.png_problem(b"GIF89a....") is not None
    huge = bytearray(solid_png())
    huge[16:24] = struct.pack(">II", 1280, 9000)
    assert "too large" in agent.png_problem(bytes(huge))


async def test_check_tools_screenshot_and_rate_limit(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo")
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "projects.png").write_bytes(solid_png())
    (shots / "bad.png").write_text("not a png")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "projects.styles.json").write_text("{}")
    tools = agent.CheckTools(repo, demo_spec(), {"projects": shots / "projects.png", "bad": shots / "bad.png"},
                             run_id="laned", timeout_s=60, max_calls=0, context_dir=ctx)
    out = await tools.get_screenshot({"screen_id": "projects"})
    assert out["content"][0]["type"] == "text" and "ORIGINAL" in out["content"][0]["text"]
    assert str(ctx / "projects.styles.json") in out["content"][0]["text"]
    image = out["content"][1]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == solid_png()
    missing = await tools.get_screenshot({"screen_id": "../../etc/passwd"})
    assert missing.get("is_error") and "Available: bad, projects" in missing["content"][0]["text"]
    assert (await tools.get_screenshot({"screen_id": "bad"})).get("is_error")
    limited = await tools.run_checks({"kind": "typecheck"})
    assert limited.get("is_error") and "limit" in limited["content"][0]["text"]
    assert (await tools.run_checks({"kind": "lint"})).get("is_error")
    assert tools.screenshot_calls == 1


def test_run_builder_refuses_bad_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    kwargs = dict(failing=[], run_id="laned", budget_usd=1.0, model="claude-opus-5-5")
    import asyncio
    (repo / STUB).write_text("dirty")
    with pytest.raises(agent.BuildError, match="uncommitted"):
        asyncio.run(agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, **kwargs))
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, check=True)
    (repo.parent / "evidence").mkdir()
    with pytest.raises(agent.BuildError, match="run directory"):
        asyncio.run(agent.run_builder(repo, demo_spec(), manifest, context_dir=repo.parent, screenshots={},
                                      **kwargs))
    (ctx / "p.png").write_bytes(solid_png())
    with pytest.raises(agent.BuildError, match="outside the worktree"):
        asyncio.run(agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx,
                                      screenshots={"projects": ctx / "p.png"}, **kwargs))


# ---------------------------------------------------------------- the round loop, driven by a script ($0)
def _matches(matcher: str | None, tool_name: str) -> bool:
    return matcher in (None, "", "*", ".*") or tool_name in matcher.split("|")


class ScriptedSDK:
    """Plays the CLI: runs each scripted tool call through the registered PreToolUse hooks, performs it when no
    hook denies it, and ends each session with a ResultMessage."""

    def __init__(self, rounds: list[list[tuple]], *, bypass_hooks: bool = False, usage: dict | None = None):
        self.rounds = rounds
        self.prompts: list[str] = []
        self.denied: list[str] = []
        self.contexts: list[str] = []
        self.bypass_hooks = bypass_hooks
        self.usage = usage

    def query(self, *, prompt, options):
        self.prompts.append(prompt)
        actions = self.rounds.pop(0)

        async def stream():
            for i, (tool_name, args, effect) in enumerate(actions):
                yield AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name=tool_name, input=args)],
                                       model="scripted", usage=self.usage, message_id=f"msg-{len(self.prompts)}-{i}")
                denied = False
                if not self.bypass_hooks:
                    for matcher in options.hooks["PreToolUse"]:
                        if not _matches(matcher.matcher, tool_name):
                            continue
                        for hook in matcher.hooks:
                            out = await hook({"hook_event_name": "PreToolUse", "tool_name": tool_name,
                                              "tool_input": args, "tool_use_id": "t"}, "t", {"signal": None})
                            spec = out.get("hookSpecificOutput") or {}
                            denied = denied or spec.get("permissionDecision") == "deny"
                if denied:
                    self.denied.append(f"{tool_name} {args.get('file_path', '')}")
                    continue
                if effect is not None:
                    effect()
                for matcher in options.hooks.get("PostToolUse", []):
                    if _matches(matcher.matcher, tool_name):
                        for hook in matcher.hooks:
                            out = await hook({"hook_event_name": "PostToolUse", "tool_name": tool_name,
                                              "tool_input": args, "tool_response": "ok"}, "t", {"signal": None})
                            extra = (out.get("hookSpecificOutput") or {}).get("additionalContext")
                            if extra:
                                self.contexts.append(extra)
            yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=10, is_error=False,
                                num_turns=len(actions) + 1, session_id="scripted", total_cost_usd=0.05,
                                usage={"input_tokens": 100, "output_tokens": 50}, terminal_reason="completed")
        return stream()


def _fake_suite(calls: list, *, typecheck=None):
    """Build fails while the stub throws; ``typecheck(repo)`` may return an error summary (None = passes)."""
    def suite(repo, kinds, ids, *, run_id, timeout_s, scenarios, operations):
        implemented = "not implemented" not in (Path(repo) / STUB).read_text()
        calls.append(implemented)
        error = typecheck(Path(repo)) if typecheck else None
        reports = {
            "typecheck": CheckReport(kind="typecheck", status="failed" if error else "passed", passed=not error,
                                     summary=error or "", duration_s=0.1),
            "build": CheckReport(kind="build", status="passed" if implemented else "failed", passed=implemented,
                                 summary="" if implemented else "Error: closeIssue throws", duration_s=0.1),
            "data": CheckReport(kind="data", status="passed", passed=True, summary="", duration_s=0),
            "scenarios": CheckReport(kind="scenarios", status="unavailable", passed=False, summary="n/a",
                                     duration_s=0)}
        return [reports[k] for k in kinds]
    return suite


async def test_run_builder_round_loop_scripted(tmp_path, monkeypatch):
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "notes.md").write_text("reference")
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "projects.png").write_bytes(solid_png())

    def write(rel, text):
        return lambda: (repo / rel).write_text(text)

    request = '{"requests": [{"file": "api/src/server.ts", "change": "x", "reason": "y"}]}'
    sdk = ScriptedSDK([
        # round 1: the guard is bypassed (simulated) and an immutable file changes -> rejected and reverted
        [("Write", {"file_path": str(repo / "web/src/styles/app.css"), "content": "body{}"},
          write("web/src/styles/app.css", "body{}")),
         ("Read", {"file_path": str(repo / "README.md")}, write("package.json", "{}"))],
        # round 2: denied attempts, a contract request and a styling change; the stub still throws -> red
        [("Read", {"file_path": str(repo / STUB)}, None),
         ("Write", {"file_path": str(repo / "package.json"), "content": "{}"}, write("package.json", "{}")),
         ("Read", {"file_path": str(Path.home() / ".forge.env")}, None),
         ("Bash", {"command": "cat ~/.forge.env"}, None),
         ("Write", {"file_path": str(repo / CONTRACT_CHANGE_FILE), "content": request},
          write(CONTRACT_CHANGE_FILE, request)),
         ("Edit", {"file_path": str(repo / "web/src/styles/app.css"), "old_string": "sans-serif",
                   "new_string": "Inter, sans-serif"},
          write("web/src/styles/app.css", "body { font-family: Inter; }\n"))],
        # round 3: implements the stub -> green, stop
        [("Write", {"file_path": str(repo / STUB), "content": "x"},
          write(STUB, 'import type { Issue } from "../types.js";\nexport function closeIssue(issue: Issue): Issue '
                      '{\n  return { ...issue, status: "closed" };\n}\n'))],
        [],   # never reached
    ])
    calls: list[bool] = []
    monkeypatch.setattr(agent, "query", sdk.query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite(calls))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")

    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx,
                                     screenshots={"projects": shots / "projects.png"}, failing=[], run_id="laned",
                                     budget_usd=1.0, model="claude-opus-5-5", rounds=4)

    r1, r2, r3 = report.rounds
    assert r1.rejected and "package.json (immutable) changed" in r1.violations and r1.commit is None
    assert "discarded" in sdk.prompts[1]
    assert not r2.rejected and not r2.green and r2.commit and r2.contract_change_request == request
    assert "closeIssue throws" in sdk.prompts[2]
    assert r3.green and r3.commit == report.final_commit == gitrepo.head_sha(repo)
    assert report.green and report.commits == [r2.commit, r3.commit] and calls == [False, True]
    assert report.contract_change_requests == [request]
    assert report.cost_usd == pytest.approx(0.15) and report.turns == r1.turns + r2.turns + r3.turns
    assert {d.tool for d in report.guard_denials} == {"Write", "Read", "Bash"}
    assert all(d.round == 2 for d in report.guard_denials)
    assert len(sdk.denied) == 3
    log = gitrepo.git(repo, "log", "--format=%s")
    assert log.splitlines()[:2] == ["build round 3", "build round 2"]
    tracked = gitrepo.git(repo, "ls-files").splitlines()
    assert CONTRACT_CHANGE_FILE not in tracked
    for entry in manifest.immutable:
        assert hashlib.sha256((repo / entry.path).read_bytes()).hexdigest() == entry.sha256
    assert "Inter" in (repo / "web/src/styles/app.css").read_text()
    assert any("scenario checks were unavailable" in n for n in report.notes)
    assert report.header.stage == "build" and report.header.prompt_sha


async def test_run_builder_stops_when_hooks_are_not_active(tmp_path, monkeypatch):
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    sdk = ScriptedSDK([[("Write", {"file_path": str(repo / STUB), "content": "x"},
                         lambda: (repo / STUB).write_text("export {};\n"))], []], bypass_hooks=True)
    calls: list[bool] = []
    monkeypatch.setattr(agent, "query", sdk.query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite(calls))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, failing=[],
                                     run_id="laned", budget_usd=1.0, model="claude-opus-5-5", rounds=2)
    assert len(report.rounds) == 1 and report.rounds[0].rejected
    assert "guard was not consulted" in report.rounds[0].violations[0]
    assert (repo / STUB).read_text() == FIXTURE_FILES[STUB]
    assert report.commits == [] and any("hooks were not active" in n for n in report.notes)


# ---------------------------------------------------------------- real checks (Docker)
@pytest.mark.docker
@needs_docker
def test_run_checks_minimal_repo_pass_then_fail(tmp_path):
    repo, _ = make_fixture_repo(tmp_path / "repo", local_base_image())
    reports = run_check_suite(repo, ["typecheck", "build"], run_id="laned-test", timeout_s=600)
    assert [r.status for r in reports] == ["passed", "passed"], "\n".join(r.text() for r in reports)
    (repo / STUB).write_text('import type { Issue } from "../types.js";\n'
                             "export function closeIssue(issue: Issue): Issue { return issue.title; }\n")
    (repo / "web/src/generated/screens/Projects.tsx").write_text(
        'import Missing from "./Missing";\nexport function Projects() { return <Missing />; }\n')
    typecheck = run_checks(repo, "typecheck", run_id="laned-test", timeout_s=600)
    assert typecheck.status == "failed"
    assert "api/src/stubs/closeIssue.ts" in typecheck.summary and "TS2322" in typecheck.summary
    build = run_checks(repo, "build", run_id="laned-test", timeout_s=600)
    assert build.status == "failed" and "Missing" in build.summary
    assert leftovers("laned-test") == []


STANDIN_STACK = {
    "compose.yaml": ("services:\n  app:\n    build: .\n    ports:\n      - \"127.0.0.1:3999:3000\"\n"
                     "    networks: [backend, edge]\nnetworks:\n  backend:\n    internal: true\n  edge: {}\n"),
    "Dockerfile": 'FROM node:22-alpine\nWORKDIR /app\nCOPY server.mjs .\nCMD ["node", "server.mjs"]\n',
    "server.mjs": ('import http from "node:http";\n'
                   "const page = `<!doctype html><html><head><title>Projects</title></head><body><main>"
                   '<h1>Projects</h1><button type="button">New project</button></main></body></html>`;\n'
                   "http.createServer((req, res) => {\n"
                   '  if (req.url === "/__forge/health") { res.writeHead(200); return res.end("{}"); }\n'
                   '  res.writeHead(200, {"content-type": "text/html"}); res.end(page);\n'
                   '}).listen(3000, "0.0.0.0");\n'),
}


@pytest.mark.docker
@needs_docker
def test_scenarios_check_on_a_stand_in_stack(tmp_path):
    """The real scenario path: Verify's internal-only overlay, health from inside, the runner image, teardown."""
    from forge.models.scenario import Scenario, ScenarioStep, Target

    repo = tmp_path / "stack"
    repo.mkdir()
    for rel, text in STANDIN_STACK.items():
        (repo / rel).write_text(text)
    gitrepo.init_repo(repo)
    gitrepo.commit_all(repo, "stand-in")

    def target(role, name):
        return Target(role=role, name=name, exact=True, has_text=None, within_role=None, within_name=None,
                      within_has_text=None)

    def scenario(sid, steps):
        return Scenario(id=sid, feature="projects", title=sid, mutating=False, binds=[], setup=[], steps=steps,
                        captures=[], effects=[], evidence=[])
    goto = ScenarioStep(kind="goto", target=None, value=None, route="/")
    click = ScenarioStep(kind="click", target=target("button", "New project"), value=None, route=None)
    sees = [goto, ScenarioStep(kind="expect_visible", target=target("heading", "Projects"), value=None, route=None),
            click]
    missing = [goto, click,
               ScenarioStep(kind="expect_visible", target=target("button", "Delete all"), value=None, route=None)]
    report = run_checks(repo, "scenarios", None, run_id="laned-stack", timeout_s=900,
                        scenarios=[scenario("sees-projects", sees), scenario("sees-delete", missing)], operations=[])
    assert report.status == "failed", report.text()
    assert "PASS sees-projects" in report.summary
    assert 'FAIL sees-delete at step 2 [expect_visible button "Delete all"] (assertion)' in report.summary
    leftover = subprocess.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
                               "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert "laned-stack" not in leftover
    assert leftovers("laned-stack") == []


@pytest.mark.docker
@needs_docker
def test_run_checks_on_lane_b_generated_repo(tmp_path):
    """typecheck, build and the pilot scenario on a real generated repo (offline image build, internal network)."""
    try:
        from forge.stages.generate import codegen
    except ImportError:
        pytest.skip("lane B's forge.stages.generate.codegen has not landed yet")
    if not hasattr(codegen, "generate"):
        pytest.skip("forge.stages.generate.codegen.generate() is not available yet")
    params = inspect.signature(codegen.generate).parameters
    args = [demo_spec(), tmp_path / "apps"] + (["laned-gen"] if len(params) >= 3 else [])
    manifest = codegen.generate(*args)
    repo = Path(manifest.repo)
    spec = demo_spec()
    reports = run_check_suite(repo, ["typecheck", "build", "scenarios"], [spec.pilot_scenario], run_id="laned-gen",
                              timeout_s=1200, scenarios=spec.scenarios, operations=agent.op_wires(spec))
    assert [r.status for r in reports] == ["passed", "passed", "passed"], "\n".join(r.text() for r in reports)
    assert f"PASS {spec.pilot_scenario}" in reports[2].summary
    assert leftovers("laned-gen") == []
    stacks = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert "laned-gen" not in stacks


# ---------------------------------------------------------------- live smoke test (API + Docker, ~$0.75)
ORIGINAL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Projects</title><style>
:root { --color-fg: #1f2328; --color-muted: #59636e; --color-accent: #1f883d; --color-border: #d1d9e0;
        --color-canvas: #f6f8fa; --radius: 6px; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
       color: var(--color-fg); background: var(--color-canvas); }
header { height: 56px; background: #24292f; color: #fff; display: flex; align-items: center; gap: 8px;
         padding: 0 24px; font-weight: 600; font-size: 16px; }
main { max-width: 880px; margin: 32px auto; background: #fff; border: 1px solid var(--color-border);
       border-radius: 12px; padding: 24px 32px; box-shadow: 0 1px 3px rgba(31, 35, 40, 0.12); }
.top { display: flex; justify-content: space-between; align-items: center; }
h1 { font-size: 24px; font-weight: 600; line-height: 32px; margin: 0; }
button { display: inline-flex; align-items: center; gap: 6px; background: var(--color-accent); color: #fff;
         border: 1px solid rgba(31, 35, 40, 0.15); border-radius: var(--radius); padding: 5px 16px; font: inherit;
         font-size: 14px; font-weight: 500; line-height: 20px; }
p { color: var(--color-muted); font-size: 14px; margin: 24px 0 0; padding: 32px; text-align: center;
    border: 1px dashed var(--color-border); border-radius: var(--radius); }
</style></head><body>
<header><svg aria-hidden="true" width="24" height="24" viewBox="0 0 16 16"><path fill="currentColor"
 d="M1.5 3.25c0-.966.784-1.75 1.75-1.75h9.5c.966 0 1.75.784 1.75 1.75v9.5A1.75 1.75 0 0 1 12.75 14.5h-9.5
 A1.75 1.75 0 0 1 1.5 12.75Zm1.75-.25a.25.25 0 0 0-.25.25v9.5c0 .138.112.25.25.25h9.5
 a.25.25 0 0 0 .25-.25v-9.5a.25.25 0 0 0-.25-.25Z"/></svg>Issue Tracker</header>
<main><div class="top"><h1>Projects</h1><button type="button"><svg aria-hidden="true" width="16" height="16"
 viewBox="0 0 16 16"><path fill="currentColor" d="M7.75 2a.75.75 0 0 1 .75.75V7h4.25a.75.75 0 0 1 0 1.5H8.5
 v4.25a.75.75 0 0 1-1.5 0V8.5H2.75a.75.75 0 0 1 0-1.5H7V2.75A.75.75 0 0 1 7.75 2Z"/></svg>New project</button></div>
<p>No projects yet.</p></main></body></html>"""

STYLE_PROBE = """() => {
  const props = ["font-family", "font-size", "font-weight", "line-height", "color", "background-color",
                 "border-top", "border-radius", "padding", "gap", "box-shadow", "display", "margin"];
  const pick = (el) => { const cs = getComputedStyle(el); const out = {};
    for (const p of props) out[p] = cs.getPropertyValue(p); return out; };
  const els = [["body", "body"], ["header", "header"], ["main", "main"], ["h1", "heading Projects"],
               ["button", "button New project"], ["button svg", "icon in button"], ["p", "paragraph"]];
  const root = {};
  for (const sheet of document.styleSheets) for (const rule of sheet.cssRules)
    if (rule.selectorText === ":root")
      for (const name of rule.style) root[name] = rule.style.getPropertyValue(name).trim();
  return { viewport: { width: innerWidth, height: innerHeight }, root_variables: root,
           elements: els.map(([sel, label]) => {
             const el = document.querySelector(sel); const r = el.getBoundingClientRect();
             return { selector: sel, label, box: { x: r.x, y: r.y, width: r.width, height: r.height },
                      styles: pick(el) }; }) };
}"""


def render_original(ctx: Path, shots: Path) -> Path:
    """Render a styled 'original' Projects screen and write the per-screen reference files the pipeline provides."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_content(ORIGINAL_PAGE)
        (ctx / "projects.snapshot.txt").write_text(page.locator("body").aria_snapshot())
        (ctx / "projects.dom.html").write_text(page.content())
        (ctx / "projects.styles.json").write_text(json.dumps(page.evaluate(STYLE_PROBE), indent=1))
        shot = shots / "projects.png"
        page.screenshot(path=str(shot))
        browser.close()
    return shot


@pytest.mark.live
@pytest.mark.docker
@needs_docker
async def test_builder_implements_a_stub_and_restyles_live(tmp_path):
    if not agent.api_key():
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    repo, manifest = make_fixture_repo(tmp_path / "repo", local_base_image())
    ctx, shots = tmp_path / "ctx", tmp_path / "shots"
    ctx.mkdir()
    shots.mkdir()
    import asyncio
    shot = await asyncio.to_thread(render_original, ctx, shots)   # sync Playwright needs its own thread
    full = demo_spec()
    keep = {"projects-heading", "new-project", "projects-empty"}
    spec = full.model_copy(update={"screens": [s for s in full.screens if s.id == "projects"],
                                   "elements": [e for e in full.elements if e.id in keep], "scenarios": []})
    report = await agent.run_builder(repo, spec, manifest, context_dir=ctx, screenshots={"projects": shot},
                                     failing=[], run_id="laned-live", budget_usd=0.75, model=DEFAULT_MODEL, rounds=1)
    print(report.model_dump_json(indent=2))
    stub = (repo / STUB).read_text()
    assert "not implemented" not in stub and "closed" in stub
    assert report.commits and report.final_commit == gitrepo.head_sha(repo)
    assert report.cost_usd <= 1.0
    for entry in manifest.immutable:
        assert hashlib.sha256((repo / entry.path).read_bytes()).hexdigest() == entry.sha256
    checks = {c.kind: c.status for c in report.checks}
    assert checks["typecheck"] == "passed" and checks["build"] == "passed"
    styled = "".join(p.read_text() for p in (repo / "web/src").rglob("*") if p.is_file() and
                     p.suffix in (".tsx", ".ts", ".css")).lower()
    assert any(v in styled for v in ("#1f883d", "#24292f", "#f6f8fa", "#d1d9e0", "--color-accent")), \
        "no exact value from projects.styles.json reached the screen or the styles"
    screen = (repo / "web/src/generated/screens/Projects.tsx").read_text()
    for name in ("Projects", "New project", "No projects yet."):
        assert name in screen
    assert leftovers("laned-live") == []


# ---------------------------------------------------------------- pacing and salvage ($0)
def test_spend_meter_counts_visible_output_when_the_stream_reports_too_little():
    from claude_agent_sdk import TextBlock
    meter = agent.SpendMeter("claude-opus-5-5")
    write = ToolUseBlock(id="t", name="Write", input={"file_path": "x.tsx", "content": "x" * 30_000})
    meter.observe(AssistantMessage(content=[TextBlock(text="y" * 3_000)], model="m", message_id="a",
                                   usage={"output_tokens": 16, "cache_creation_input_tokens": 100_000}))
    meter.observe(AssistantMessage(content=[write], model="m", message_id="a",
                                   usage={"output_tokens": 16, "cache_creation_input_tokens": 100_000}))
    # input side counted once per API message ($0.50); output: ~33k visible chars / 3 = ~11k tokens, x1.5 safety
    assert meter.spent() == pytest.approx(0.5 + 11_000 * 1.5 * 20 / 1_000_000, rel=0.02)


async def test_crashed_session_counts_the_estimated_spend(tmp_path, monkeypatch):
    repo, manifest, ctx = _setup(tmp_path)

    def crashing_query(*, prompt, options):
        async def stream():
            yield AssistantMessage(content=[], model="m", message_id="a",
                                   usage={"cache_creation_input_tokens": 40_000})       # $0.20
            raise RuntimeError("CLI exited")
        return stream()
    monkeypatch.setattr(agent, "query", crashing_query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite([]))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, failing=[],
                                     run_id="laned", budget_usd=1.0, model="claude-opus-5-5", rounds=1)
    assert report.rounds[0].error and "CLI exited" in report.rounds[0].error
    assert report.cost_usd == pytest.approx(0.2)


def _setup(tmp_path):
    repo, manifest = make_fixture_repo(tmp_path / "repo")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    return repo, manifest, ctx


IMPLEMENTED = ('import type { Issue } from "../types.js";\nexport function closeIssue(issue: Issue): Issue '
               '{\n  return { ...issue, status: "closed" };\n}\n')
SCREEN = "web/src/generated/screens/Projects.tsx"


async def test_salvage_reverts_only_the_file_that_breaks_the_checks(tmp_path, monkeypatch):
    repo, manifest, ctx = _setup(tmp_path)

    def write(rel, text):
        return lambda: (repo / rel).write_text(text)
    sdk = ScriptedSDK([[
        ("Write", {"file_path": str(repo / STUB), "content": "x"}, write(STUB, IMPLEMENTED)),
        ("Write", {"file_path": str(repo / "web/src/styles/app.css"), "content": "x"},
         write("web/src/styles/app.css", "body { color: #1f2328; }\n")),
        ("Write", {"file_path": str(repo / "web/src/generated/screens/Icons.tsx"), "content": "x"},
         write("web/src/generated/screens/Icons.tsx", "export const Plus = () => <svg aria-hidden />;\n")),
        ("Write", {"file_path": str(repo / SCREEN), "content": "x"}, write(SCREEN, "export function Projects( {\n")),
    ]])
    calls: list[bool] = []

    def typecheck(r: Path):
        if "Projects( {" in (r / SCREEN).read_text():
            return (f"2 TypeScript error(s):\n{SCREEN}(1,26): error TS1005: ')' expected.\n"
                    f"{SCREEN}(2,1): error TS1005: '}}' expected.")
        return None
    monkeypatch.setattr(agent, "query", sdk.query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite(calls, typecheck=typecheck))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, failing=[],
                                     run_id="laned", budget_usd=1.0, model="claude-opus-5-5", rounds=1)
    assert not report.rounds[0].green and report.green
    assert report.reverted_files == [SCREEN]
    assert (repo / SCREEN).read_text() == FIXTURE_FILES[SCREEN]
    assert (repo / STUB).read_text() == IMPLEMENTED                      # the good work is kept
    assert "#1f2328" in (repo / "web/src/styles/app.css").read_text()
    assert (repo / "web/src/generated/screens/Icons.tsx").exists()
    subjects = gitrepo.git(repo, "log", "--format=%s").splitlines()
    assert subjects[:2] == ["build: revert files that failed the checks", "build round 1"]
    assert report.final_commit == gitrepo.head_sha(repo) and agent._status_paths(repo) == []
    assert any("reverted 1 file(s)" in n for n in report.notes)


async def test_salvage_keeps_the_builder_version_when_nothing_passes(tmp_path, monkeypatch):
    repo, manifest, ctx = _setup(tmp_path)
    broken_stub = 'export function closeIt() { return 1; }\n'          # the route's import no longer resolves
    sdk = ScriptedSDK([[
        ("Write", {"file_path": str(repo / STUB), "content": "x"}, lambda: (repo / STUB).write_text(broken_stub)),
        ("Write", {"file_path": str(repo / "web/src/styles/app.css"), "content": "x"},
         lambda: (repo / "web/src/styles/app.css").write_text("body { color: red; }\n")),
    ]])

    def typecheck(r: Path):
        if "closeIt" in (r / STUB).read_text():
            return ("1 TypeScript error(s):\napi/src/server.ts(2,10): error TS2305: Module '\"./stubs/closeIssue.js\"'"
                    " has no exported member 'closeIssue'.")
        return None
    monkeypatch.setattr(agent, "query", sdk.query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite([], typecheck=typecheck))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, failing=[],
                                     run_id="laned", budget_usd=1.0, model="claude-opus-5-5", rounds=1)
    # Reverting api/ brings back the starting stub, whose build already failed (the fake build fails while the stub
    # throws), and reverting everything is the starting state: nothing passes, so the builder's version stays.
    assert report.reverted_files == [] and not report.green
    assert (repo / STUB).read_text() == broken_stub
    assert "red" in (repo / "web/src/styles/app.css").read_text()
    assert report.final_commit == report.commits[-1] == gitrepo.head_sha(repo)
    assert agent._status_paths(repo) == []
    assert any("kept the builder's version" in n for n in report.notes)


async def test_salvage_area_step_recovers(tmp_path, monkeypatch):
    repo, manifest, ctx = _setup(tmp_path)
    icons = "web/src/generated/screens/Icons.tsx"
    sdk = ScriptedSDK([[
        ("Write", {"file_path": str(repo / STUB), "content": "x"}, lambda: (repo / STUB).write_text(IMPLEMENTED)),
        ("Write", {"file_path": str(repo / icons), "content": "x"},
         lambda: (repo / icons).write_text("export const Plus = () => <svg aria-hidden />;\n")),
        ("Write", {"file_path": str(repo / SCREEN), "content": "x"},
         lambda: (repo / SCREEN).write_text("import { Plus } from './Icons';\nexport function Projects() "
                                            "{ return <Plus />; }\n")),
    ]])

    def typecheck(r: Path):
        if (r / icons).exists():   # blames the unchanged entry point, not the files that caused it
            return "1 TypeScript error(s):\nweb/src/main.tsx(3,10): error TS2614: Module has no exported member."
        return None
    monkeypatch.setattr(agent, "query", sdk.query)
    monkeypatch.setattr(agent, "run_check_suite", _fake_suite([], typecheck=typecheck))
    monkeypatch.setattr(agent, "api_key", lambda: "sk-test")
    report = await agent.run_builder(repo, demo_spec(), manifest, context_dir=ctx, screenshots={}, failing=[],
                                     run_id="laned", budget_usd=1.0, model="claude-opus-5-5", rounds=1)
    assert report.green and report.reverted_files == sorted([icons, SCREEN])
    assert not (repo / icons).exists() and (repo / SCREEN).read_text() == FIXTURE_FILES[SCREEN]
    assert (repo / STUB).read_text() == IMPLEMENTED                      # api/ was not the failing area


def test_data_check_finds_records_copied_from_the_original(tmp_path):
    from forge.stages.build.checks import copied_records, data_check

    screens = tmp_path / "web" / "src" / "generated" / "screens"
    screens.mkdir(parents=True)
    (screens / "Shell.tsx").write_text('const P = [{ name: "rpl-ca71bf-project-name" }];\nconst ok = "rpl-"; // no marker\n')
    (tmp_path / "app-spec.json").write_text('{"name": "rpl-0a1b2c-outside-src"}')
    hits = copied_records(tmp_path)
    assert hits == ['web/src/generated/screens/Shell.tsx:1: const P = [{ name: "rpl-ca71bf-project-name" }];']
    report = data_check(tmp_path, started=0.0)
    assert report.kind == "data" and report.status == "failed" and "Render records from the API" in report.summary
    (screens / "Shell.tsx").write_text("export const P = [];\n")
    assert data_check(tmp_path, started=0.0).passed
