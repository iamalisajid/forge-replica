"""Builder checks: typecheck, build and scenarios of a worktree, each in a fresh, offline container.

The worktree is never mounted. Its git-tracked and untracked (not ignored) regular files are copied into a temp
build context (no symlinks, no ``node_modules``, no ``dist``, no ``.git``) and baked into an image ``FROM`` the
pinned base image, so a check sees exactly what a clean clone would and can never write back to the host.
``typecheck`` and ``build`` run with ``--network none``. ``scenarios`` brings the replica's own stack up with
Verify's internal-only overlay and runs the scenarios through the runner image inside that network.

Results are short, agent-readable text (the first errors, trimmed). Every container and image a check creates
is named ``forge-<run_id>-check-<n>-*`` and removed before the call returns, whatever happened.
"""
from __future__ import annotations

import contextlib
import itertools
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal, Protocol

from forge.core import docker
from forge.core.config import BASE_IMAGE, Limits
from forge.models.appspec import OpWire
from forge.models.base import Strict
from forge.models.scenario import Scenario, ScenarioResult

CheckKind = Literal["typecheck", "build", "data", "scenarios"]
CheckStatus = Literal["passed", "failed", "error", "timeout", "unavailable"]
KINDS: tuple[str, ...] = ("typecheck", "build", "data", "scenarios")
# Values the explorer typed carry this marker; seeing one in source means a record was copied from the original.
RECORD_MARKER = re.compile(r"\brpl-[0-9a-f]{6}\b")
SOURCE_DIRS = ("web/src", "api/src")
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".json")
SKIP_PARTS = frozenset({"node_modules", "dist", ".git"})
MAX_CONTEXT_BYTES = 300 * 1024 * 1024
SUMMARY_CHARS = 3500
MAX_ERRORS = 12

CHECK_DOCKERFILE = """\
FROM {base}
WORKDIR /app
COPY . /app/
RUN if [ -f db/schema.prisma ]; then \\
      DATABASE_URL=postgresql://check:check@localhost:5432/check \\
      npx prisma generate --schema db/schema.prisma > /tmp/prisma-generate.log 2>&1 \\
      || {{ cat /tmp/prisma-generate.log; exit 1; }}; \\
    fi
"""

_counter = itertools.count(1)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TS_ERROR = re.compile(r"error TS\d+:")
_CONTAINER_PATH = re.compile(r"(?<![\w/.:])/app/")
_BUILD_ERROR = re.compile(r"error during build|\[vite\]|\[plugin|rollup|error:|Error:|ERR!", re.IGNORECASE)


class CheckError(RuntimeError):
    pass


class ScenarioBackendUnavailable(RuntimeError):
    """The scenario stack cannot run in this installation (e.g. Verify's compose helpers are missing)."""


class CheckReport(Strict):
    kind: CheckKind
    status: CheckStatus
    passed: bool
    summary: str
    duration_s: float
    image: str | None = None
    results: list[ScenarioResult] = []

    def text(self) -> str:
        label = {"passed": "PASSED", "failed": "FAILED", "error": "ERROR", "timeout": "TIMED OUT",
                 "unavailable": "UNAVAILABLE"}[self.status]
        head = f"{self.kind}: {label} ({self.duration_s:.0f}s)"
        return f"{head}\n{self.summary}" if self.summary else head


class ScenarioBackend(Protocol):
    def __call__(self, context: Path, scenarios: list[Scenario], operations: list[OpWire], *, run_id: str,
                 name: str, timeout_s: float) -> list[ScenarioResult]: ...


# ---------------------------------------------------------------- public API
def run_checks(repo: Path, kind: CheckKind, scenario_ids: list[str] | None = None, *, run_id: str,
               timeout_s: int = Limits().run_checks_timeout_s, scenarios: list[Scenario] | None = None,
               operations: list[OpWire] | None = None,
               scenario_backend: ScenarioBackend | None = None) -> CheckReport:
    """Run one check of ``repo`` in a fresh container. Never raises for check failures; see ``CheckReport``.

    ``scenarios``/``operations`` are what a ``scenarios`` check may run (the spec's, never held-out ones);
    ``scenario_ids`` narrows them.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown check kind {kind!r}; use one of {', '.join(KINDS)}")
    return run_check_suite(repo, [kind], scenario_ids, run_id=run_id, timeout_s=timeout_s, scenarios=scenarios,
                           operations=operations, scenario_backend=scenario_backend)[0]


def run_check_suite(repo: Path, kinds: list[CheckKind], scenario_ids: list[str] | None = None, *, run_id: str,
                    timeout_s: int = Limits().run_checks_timeout_s, scenarios: list[Scenario] | None = None,
                    operations: list[OpWire] | None = None,
                    scenario_backend: ScenarioBackend | None = None) -> list[CheckReport]:
    """Run several checks against one snapshot of ``repo`` (one copy, one image), in order.

    ``scenarios`` is skipped when a ``build`` earlier in the same suite failed: the stack could not start.
    """
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(f"unknown check kind(s) {unknown}; use {', '.join(KINDS)}")
    started = time.monotonic()
    deadline = started + max(1, timeout_s)
    name = check_name(run_id)
    tag = f"{name}:latest"
    workdir = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    containers: list[str] = []
    image: tuple[str, str] | None = None        # (status, output) of the one image build
    try:
        context = workdir / "context"
        try:
            copy_build_context(Path(repo), context)
        except (CheckError, OSError) as exc:
            return [_report(kind=k, status="error", summary=f"could not snapshot the worktree: {exc}",
                            started=started) for k in kinds]
        reports: list[CheckReport] = []
        for kind in kinds:
            t0 = time.monotonic()
            if kind == "data":
                reports.append(data_check(context, started=t0))
                continue
            if kind == "scenarios":
                if any(r.kind == "build" and not r.passed for r in reports):
                    reports.append(_report(kind=kind, status="error", started=t0,
                                           summary="skipped: the build failed, so the stack cannot start."))
                else:
                    reports.append(_scenario_check(context, scenario_ids, scenarios, operations, run_id=run_id,
                                                   name=f"{name}-stack", deadline=deadline,
                                                   backend=scenario_backend))
                continue
            if image is None:
                image = _build_image(context, workdir, tag, run_id=run_id, deadline=deadline)
            status, output = image
            if status != "passed":
                summary = ("the check image did not finish building in time." if status == "timeout" else
                           "the check image did not build:\n" + summarize(output))
                reports.append(_report(kind=kind, status="timeout" if status == "timeout" else "error",
                                       summary=summary, started=t0, image=tag))
                continue
            container = f"{name}-{kind}"
            containers.append(container)
            reports.append(_run_npm(tag, kind, container, run_id=run_id, deadline=deadline))
        return reports
    finally:
        _cleanup(containers, tag if image is not None else None)
        shutil.rmtree(workdir, ignore_errors=True)


def copied_records(root: Path) -> list[str]:
    """``path:line: value`` for every source line that holds a record copied from the original (see RECORD_MARKER)."""
    hits: list[str] = []
    for top in SOURCE_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for no, line in enumerate(lines, 1):
                if RECORD_MARKER.search(line):
                    hits.append(f"{path.relative_to(root).as_posix()}:{no}: {line.strip()[:120]}")
    return hits


def data_check(context: Path, *, started: float) -> CheckReport:
    hits = copied_records(context)
    if not hits:
        return _report(kind="data", status="passed", started=started,
                       summary="no records copied from the original in web/src or api/src.")
    return _report(kind="data", status="failed", started=started, summary=(
        f"{len(hits)} line(s) hold records copied from the original (values the explorer created, marked "
        "rpl-xxxxxx). Render records from the API (list/get operations) instead of writing them into code:\n"
        + "\n".join(hits[:MAX_ERRORS])))


def check_name(run_id: str) -> str:
    """``forge-<run_id>-check-<n>-<rand>``: unique per call, lowercase, valid as an image and container name."""
    base = docker.project_name(run_id, f"check-{next(_counter)}-{secrets.token_hex(2)}")
    return re.sub(r"[^a-z0-9_.-]", "-", base)


# ---------------------------------------------------------------- snapshot
def copy_build_context(repo: Path, dest: Path) -> int:
    """Copy the worktree's regular files (tracked + untracked, not ignored) into ``dest``. Returns bytes copied.

    Symlinks, files below symlinked directories, special files and anything under ``node_modules``, ``dist``
    or ``.git`` are skipped. Files are opened with ``O_NOFOLLOW`` so a link swapped in late is not followed.
    """
    repo = Path(repo)
    if not repo.is_dir():
        raise CheckError(f"{repo} is not a directory")
    dest.mkdir(parents=True, exist_ok=True)
    safe_dirs: dict[str, bool] = {"": True}
    total = 0
    for rel in _list_files(repo):
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts) or any(p in SKIP_PARTS for p in parts):
            continue
        if not _dirs_are_real(repo, parts[:-1], safe_dirs):
            continue
        src = repo.joinpath(*parts)
        try:
            st = os.lstat(src)
        except OSError:
            continue                      # deleted in the worktree but still in the index
        if not stat.S_ISREG(st.st_mode):
            continue
        total += st.st_size
        if total > MAX_CONTEXT_BYTES:
            raise CheckError(f"the worktree is larger than {MAX_CONTEXT_BYTES // (1024 * 1024)} MB")
        dst = dest.joinpath(*parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_nofollow(src, dst)
        os.chmod(dst, 0o755 if st.st_mode & 0o111 else 0o644)
    return total


def _list_files(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "-C", str(repo), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"], capture_output=True, timeout=120)
    if proc.returncode == 0:
        return sorted({p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p})
    files = []
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
        rel_dir = os.path.relpath(dirpath, repo)
        for f in filenames:
            files.append(f if rel_dir == "." else f"{rel_dir}/{f}".replace(os.sep, "/"))
    return sorted(files)


def _dirs_are_real(repo: Path, parts: list[str], cache: dict[str, bool]) -> bool:
    for i in range(1, len(parts) + 1):
        key = "/".join(parts[:i])
        if key not in cache:
            try:
                st = os.lstat(repo.joinpath(*parts[:i]))
                cache[key] = stat.S_ISDIR(st.st_mode)
            except OSError:
                cache[key] = False
        if not cache[key]:
            return False
    return True


def _copy_nofollow(src: Path, dst: Path) -> None:
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


# ---------------------------------------------------------------- docker
def base_image_for(repo: Path) -> str:
    """The base image the replica's own Dockerfile builds from when it is a local forge base, else the current one."""
    try:
        text = (Path(repo) / "Dockerfile").read_text(errors="replace")
    except OSError:
        text = ""
    match = re.search(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    if match and match.group(1).startswith(BASE_IMAGE + ":") and docker.image_exists(match.group(1)):
        return match.group(1)
    return docker.ensure_base_image()


def _build_image(context: Path, workdir: Path, tag: str, *, run_id: str, deadline: float) -> tuple[str, str]:
    try:
        base = base_image_for(context)
    except (docker.DockerError, subprocess.TimeoutExpired, OSError) as exc:
        return "error", f"base image unavailable: {exc}"
    dockerfile = workdir / "Dockerfile.check"
    dockerfile.write_text(CHECK_DOCKERFILE.format(base=base))
    code, output = _docker(["build", "--network", "none", "--progress", "plain", "--label", f"forge.run={run_id}",
                            "--label", "forge.role=check", "-f", str(dockerfile), "-t", tag, str(context)],
                           deadline=deadline)
    if code is None:
        return "timeout", output
    return ("passed" if code == 0 else "error"), output


def _run_npm(tag: str, kind: str, container: str, *, run_id: str, deadline: float) -> CheckReport:
    started = time.monotonic()
    code, output = _docker(
        ["run", "--rm", "--name", container, "--network", "none", "--label", f"forge.run={run_id}",
         "--label", "forge.role=check", "--memory", "4g", "--pids-limit", "4096", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "-e", "CI=1", "-e", "NO_COLOR=1", "-e", "FORCE_COLOR=0",
         tag, "npm", "run", "--silent", kind], deadline=deadline)
    if code is None:
        return _report(kind=kind, status="timeout", started=started, image=tag,
                       summary=f"`npm run {kind}` did not finish within the time limit.\n" + summarize(output))
    if code == 0:
        return _report(kind=kind, status="passed", summary="", started=started, image=tag)
    return _report(kind=kind, status="failed", summary=summarize(output) or f"`npm run {kind}` exited {code}",
                   started=started, image=tag)


def _docker(args: list[str], *, deadline: float) -> tuple[int | None, str]:
    """Run the docker CLI with the remaining time; (exit code, combined output). Exit code None on timeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, ""
    try:
        proc = subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=remaining)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or exc.output or b""
        return None, out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    except OSError as exc:   # docker CLI missing or not executable
        return 127, f"docker is not available: {exc}"


def _cleanup(containers: list[str], tag: str | None) -> None:
    """Remove this check's containers and image. Best effort: never raises."""
    commands = [["docker", "rm", "-f", name] for name in containers]
    if tag:
        commands.append(["docker", "image", "rm", "-f", tag])
    for command in commands:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(command, capture_output=True, timeout=180)


# ---------------------------------------------------------------- scenarios
def _scenario_check(context: Path, scenario_ids: list[str] | None, scenarios: list[Scenario] | None,
                    operations: list[OpWire] | None, *, run_id: str, name: str, deadline: float,
                    backend: ScenarioBackend | None) -> CheckReport:
    started = time.monotonic()
    pool = {s.id: s for s in scenarios or []}
    if not pool:
        return _report(kind="scenarios", status="unavailable", started=started,
                       summary="no scenarios are available to this check.")
    wanted = list(dict.fromkeys(scenario_ids)) if scenario_ids else list(pool)
    unknown = [i for i in wanted if i not in pool]
    chosen = [pool[i] for i in wanted if i in pool]
    if not chosen:
        return _report(kind="scenarios", status="error", started=started,
                       summary=f"unknown scenario id(s): {', '.join(unknown)}. Available: {', '.join(pool)}.")
    backend = backend or default_scenario_backend()
    if backend is None:
        return _report(kind="scenarios", status="unavailable", started=started,
                       summary="the scenario stack is not available in this installation; typecheck and build "
                               "are the checks that run.")
    remaining = deadline - time.monotonic()
    if remaining <= 5:
        return _report(kind="scenarios", status="timeout", started=started, summary="no time left for scenarios.")
    try:
        results = backend(context, chosen, list(operations or []), run_id=run_id, name=name, timeout_s=remaining)
    except ScenarioBackendUnavailable as exc:
        return _report(kind="scenarios", status="unavailable", started=started, summary=str(exc))
    except subprocess.TimeoutExpired:
        return _report(kind="scenarios", status="timeout", started=started,
                       summary="the scenario stack did not finish within the time limit.")
    except Exception as exc:  # noqa: BLE001 - a broken stack is a check result, not a crash
        return _report(kind="scenarios", status="error", started=started,
                       summary=f"the scenario stack failed: {type(exc).__name__}: {_clip(str(exc), 1500)}")
    by_id: dict[str, list[ScenarioResult]] = {}
    for result in results:
        by_id.setdefault(result.scenario_id, []).append(result)
    lines, ok = [], True
    messages: set[str] = set()
    for scenario in chosen:
        runs = by_id.get(scenario.id, [])
        failed = [r for r in runs if not r.passed]
        if runs and not failed:
            lines.append(f"PASS {scenario.id}")
            continue
        ok = False
        if not runs:
            lines.append(f"FAIL {scenario.id}: no result")
            continue
        first = failed[0]
        step = scenario.steps[first.failed_step] if first.failed_step is not None and \
            0 <= first.failed_step < len(scenario.steps) else None
        where = f" at step {first.failed_step}" if first.failed_step is not None else ""
        what = f" [{describe_step(step)}]" if step is not None else ""
        message = first.message or ""
        shown = "(same message as above)" if message and message in messages else _clip(message, 1200)
        messages.add(message)
        lines.append(f"FAIL {scenario.id}{where}{what} ({first.failure_kind}): {shown}")
    if unknown:
        lines.append(f"(ignored unknown id(s): {', '.join(unknown)})")
    return _report(kind="scenarios", status="passed" if ok else "failed", started=started,
                   summary=_clip("\n".join(lines), SUMMARY_CHARS), results=results)


def describe_step(step) -> str:
    target = step.target
    parts = [step.kind]
    if target is not None:
        parts.append(f'{target.role} "{target.name}"' if target.name else target.role)
        if target.within_role:
            parts.append(f"within {target.within_role} \"{target.within_name or target.within_has_text or ''}\"")
    if step.value is not None:
        parts.append(f"value={step.value!r}")
    if step.route is not None:
        parts.append(f"route={step.route}")
    return " ".join(parts)


def default_scenario_backend() -> ScenarioBackend | None:
    """Verify's compose helpers (lane C) when present: replica stack + internal-only overlay + runner image."""
    try:
        from forge.stages.verify import checks as verify_checks  # noqa: PLC0415 - optional dependency
        from forge.stages.verify import compose as verify_compose  # noqa: PLC0415
    except ImportError:
        return None
    needed = ("write_overlay", "lint", "find_compose_file", "compose_files", "up", "down", "logs", "run_in_network",
              "APP_URL")
    if not all(hasattr(verify_compose, n) for n in needed) or not hasattr(verify_checks, "health"):
        return None
    return verify_compose_backend


def verify_compose_backend(context: Path, scenarios: list[Scenario], operations: list[OpWire], *, run_id: str,
                           name: str, timeout_s: float) -> list[ScenarioResult]:
    """Bring the snapshot's own stack up under Verify's pinned overlay (every network internal, no published
    ports, the app reachable as Verify's browser-safe alias), wait for health, run the scenarios with the runner
    inside the network, and always tear the stack down. Mirrors ``oracle.negative.run_on_stack`` with a deadline.
    When the compose file supports it, images build offline FROM the template base."""
    from forge.models.run import RunnerRequest  # noqa: PLC0415
    from forge.stages.verify import checks as verify_checks  # noqa: PLC0415
    from forge.stages.verify import compose as vc  # noqa: PLC0415

    deadline = time.monotonic() + timeout_s
    io = context.parent / "io"
    io.mkdir(exist_ok=True)
    io.chmod(0o777)   # the runner writes its result as root
    try:
        runner_image = docker.ensure_runner_image()
        base = base_image_for(context)
    except (docker.DockerError, subprocess.TimeoutExpired, OSError) as exc:
        raise ScenarioBackendUnavailable(f"the scenario images are unavailable: {exc}") from exc
    compose_file = vc.find_compose_file(context)
    offline = "FORGE_BASE_IMAGE" in compose_file.read_text(errors="replace")
    if offline:
        # The generated compose passes FORGE_BASE_IMAGE to the Dockerfile as BASE_IMAGE; FROM the template base
        # (node_modules and Prisma engines baked in) the build needs no network. Compose reads .env from the
        # project directory, so the setting stays local to this stack.
        (context / ".env").write_text(f"FORGE_BASE_IMAGE={base}\n")
    try:
        vc.write_overlay(context, io, runner_image, build_network="none" if offline else None)
    except TypeError:   # an overlay writer without the build_network option
        vc.write_overlay(context, io, runner_image)
    problems = vc.lint(compose_file)
    if problems:
        raise CheckError("compose lint failed: " + "; ".join(problems[:5]))
    files = vc.compose_files(context)
    try:
        try:
            vc.up(name, files, timeout=_left(deadline))
        except docker.DockerError as exc:
            return _startup_failures(scenarios, f"the stack did not start: {_clip(str(exc)[-1200:], 1200)}")
        health = verify_checks.health(name, files, timeout_s=min(180, _left(deadline)))
        if health.status != "pass":
            return _startup_failures(scenarios, f"{health.detail}; app logs:\n{vc.logs(name, files)[-1200:]}")
        request = RunnerRequest(target="replica", base_url=vc.APP_URL, scenarios=scenarios, operations=operations,
                                allowed_origins=[vc.APP_URL], runs=1, parallel=1)
        return vc.run_in_network(name, files, request, timeout=_left(deadline)).results
    finally:
        vc.down(name, files)


def _left(deadline: float) -> int:
    left = int(deadline - time.monotonic())
    if left <= 0:
        raise subprocess.TimeoutExpired("scenario stack", 0)
    return left


def _startup_failures(scenarios: list[Scenario], message: str) -> list[ScenarioResult]:
    return [ScenarioResult(scenario_id=s.id, target="replica", run=1, passed=False, failure_kind="startup",
                           message=message) for s in scenarios]


# ---------------------------------------------------------------- output
def summarize(output: str, *, max_errors: int = MAX_ERRORS, limit: int = SUMMARY_CHARS) -> str:
    """The first compiler/bundler errors of a check's output, with container paths made repo-relative."""
    text = _CONTAINER_PATH.sub("", _ANSI.sub("", output or "").replace("\r", ""))
    lines = [line.rstrip() for line in text.splitlines()]
    blocks: list[str] = []
    for i, line in enumerate(lines):
        if _TS_ERROR.search(line):
            block, j = [line.strip()], i + 1
            while j < len(lines) and lines[j].startswith("  ") and not _TS_ERROR.search(lines[j]) and len(block) < 5:
                block.append(lines[j])
                j += 1
            blocks.append("\n".join(block))
    if blocks:
        out = f"{len(blocks)} TypeScript error(s):\n" + "\n".join(blocks[:max_errors])
        if len(blocks) > max_errors:
            out += f"\n... and {len(blocks) - max_errors} more"
        return _clip(out, limit)
    start = next((i for i, line in enumerate(lines) if _BUILD_ERROR.search(line)), None)
    window = lines[start:start + 40] if start is not None else lines[-40:]
    window = [line for line in window if line.strip() and not line.lstrip().startswith("at ")]
    return _clip("\n".join(window), limit)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n... (truncated)"


def _report(*, kind: str, status: str, summary: str, started: float, image: str | None = None,
            results: list[ScenarioResult] | None = None) -> CheckReport:
    return CheckReport(kind=kind, status=status, passed=status == "passed", summary=summary.strip(),
                       duration_s=round(time.monotonic() - started, 2), image=image, results=results or [])
