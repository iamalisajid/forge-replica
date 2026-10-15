"""Build stage: a guarded Claude Agent SDK session that finishes the generated replica.

The builder has two primary goals: make every screen look like the original (exact colors, fonts, sizes, spacing
and layout from the curated per-screen references, compared against the original's screenshots) and make the app
behave like it (implement the stubs, fix failing checks), without changing roles or accessible names.

Each round is one SDK session with ``cwd`` set to the generated repo, the file tools (Read/Edit/Write/Glob/Grep)
behind the PreToolUse guard, no Bash, and an in-process ``checks`` MCP server (``run_checks`` and
``get_screenshot``; screenshots of the original reach the agent only through that tool). After every round the
harness, not the agent:

1. confirms the guard was consulted, the immutable files still match their hashes and every change is inside the
   editable globs; otherwise the round is rejected and its edits are reverted with ``git checkout``;
2. collects ``contract-change-request.json`` (if the agent wrote one) into the report;
3. re-runs typecheck and build (and scenarios when the stack is available) on a fresh snapshot;
4. commits the round (``build round N``).

It stops when the checks are green, or when the rounds or the budget run out. The CLI shows the agent its exact
remaining USD budget every turn and the prompt tells it when to wrap up. If the final state still fails
typecheck or build, a salvage step reverts the builder's changes file by file, then by area, until a state passes,
because a red replica can be neither verified nor delivered.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolAnnotations,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from forge.core import gitrepo
from forge.core.config import EFFORT, REPO_ROOT, RUNS_DIR, Limits, api_key, cost_usd
from forge.models.appspec import AppSpecDraft, SpecElement, op_wires
from forge.models.base import ArtifactHeader, NameValue, Strict
from forge.models.run import CodegenManifest
from forge.models.scenario import ScenarioResult
from forge.stages.build.checks import KINDS, CheckReport, describe_step, run_check_suite, run_checks
from forge.stages.build.guard import (
    CONTRACT_CHANGE_FILE,
    FILE_TOOLS,
    HOOK_MATCHER,
    Guard,
    fold,
    glob_regex,
    make_guard,
)

PROMPT_FILE = REPO_ROOT / "forge" / "prompts" / "build.md"
CHECK_TOOLS = ("mcp__checks__run_checks", "mcp__checks__get_screenshot")
ALLOWED_TOOLS = [*FILE_TOOLS, *CHECK_TOOLS]
HARNESS_KINDS = ["typecheck", "build", "data", "scenarios"]
GATE_KINDS = ["typecheck", "build"]   # a replica that fails these cannot be verified or delivered
STAGE_VERSION = "1"
MAX_TURNS = 120                 # per round; the budget is the real cap
MIN_ROUND_USD = 0.25
EARLY_ROUND_SHARE = 0.8         # share of the remaining budget a non-final round may spend (fidelity work)
REFERENCE_SUFFIXES = (".snapshot.txt", ".dom.html", ".styles.json")   # per-screen files in the context dir
MAX_CHECK_CALLS = 12            # agent-initiated checks per round
SALVAGE_STEPS = 3               # rounds of reverting the files that the failing checks name
MAX_SCREENSHOT_BYTES = 3_750_000
MAX_SCREENSHOT_SIDE = 7900
MAX_REQUEST_CHARS = 20_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class BuildError(RuntimeError):
    pass


class GuardDenialRecord(Strict):
    round: int
    tool: str
    target: str
    reason: str


class BuildRound(Strict):
    round: int
    budget_usd: float
    cost_usd: float = 0.0
    turns: int = 0
    subtype: str | None = None
    terminal_reason: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_uses: list[NameValue] = []
    check_calls: int = 0
    screenshot_calls: int = 0
    rejected: bool = False
    violations: list[str] = []
    commit: str | None = None
    checks: list[CheckReport] = []
    green: bool = False
    contract_change_request: str | None = None


class BuildReport(Strict):
    header: ArtifactHeader
    repo: str
    base_commit: str
    final_commit: str
    green: bool
    rounds: list[BuildRound]
    commits: list[str]
    checks: list[CheckReport]               # the harness's last fresh-snapshot checks
    cost_usd: float
    turns: int
    guard_denials: list[GuardDenialRecord]
    terminal_reason: str | None
    contract_change_requests: list[str]
    notes: list[str]
    reverted_files: list[str] = []          # reverted to the starting version by the salvage step


# ---------------------------------------------------------------- checks MCP server
class CheckTools:
    """State and handlers behind the in-process ``checks`` MCP server."""

    def __init__(self, repo: Path, spec: AppSpecDraft, screenshots: dict[str, Path], *, run_id: str,
                 timeout_s: int, max_calls: int = MAX_CHECK_CALLS, context_dir: Path | None = None,
                 scenarios: list | None = None):
        self.repo = repo
        self.context_dir = context_dir
        self.scenarios = list(spec.scenarios if scenarios is None else scenarios)
        self.operations = op_wires(spec)
        self.screens = {s.id: s for s in spec.screens}
        self.screenshots = {str(k): Path(v) for k, v in screenshots.items()}
        self.run_id = run_id
        self.timeout_s = timeout_s
        self.max_calls = max_calls
        self.lock = asyncio.Lock()
        self.check_calls = 0
        self.screenshot_calls = 0
        self.reports: list[CheckReport] = []

    def new_round(self) -> None:
        self.check_calls = 0
        self.screenshot_calls = 0

    async def run_checks(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind")
        if kind not in KINDS:
            return _text(f"unknown kind {kind!r}; use one of: {', '.join(KINDS)}.", error=True)
        ids = args.get("scenario_ids") or None
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            return _text("scenario_ids must be a list of scenario id strings.", error=True)
        if self.check_calls >= self.max_calls:
            return _text(f"check limit for this round reached ({self.max_calls}). Finish your edits and end the "
                         "round; the harness re-runs every check afterwards.", error=True)
        self.check_calls += 1
        async with self.lock:   # one check at a time, even when the model calls tools in parallel
            report = await asyncio.to_thread(run_checks, self.repo, kind, ids, run_id=self.run_id,
                                             timeout_s=self.timeout_s, scenarios=self.scenarios,
                                             operations=self.operations)
        self.reports.append(report)
        return _text(report.text())

    async def get_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        screen_id = str(args.get("screen_id") or "")
        path = self.screenshots.get(screen_id)
        if path is None:
            available = ", ".join(sorted(self.screenshots)) or "none"
            return _text(f"no screenshot for screen {screen_id!r}. Available: {available}.", error=True)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return _text(f"the screenshot of {screen_id!r} is unreadable ({type(exc).__name__}).", error=True)
        problem = png_problem(data)
        if problem:
            return _text(f"the screenshot of {screen_id!r} cannot be shown: {problem}.", error=True)
        self.screenshot_calls += 1
        screen = self.screens.get(screen_id)
        where = f" (route {screen.route}, title \"{screen.title}\")" if screen else ""
        refs = reference_files(self.context_dir, screen_id) if self.context_dir else []
        exact = (f" Exact values and structure: {', '.join(str(self.context_dir / r) for r in refs)}."
                 if refs else "")
        caption = (f"Screenshot of the ORIGINAL app, screen {screen_id}{where}. Make the replica's screen look the "
                   f"same; keep its roles and accessible names.{exact}")
        return {"content": [{"type": "text", "text": caption},
                            {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}]}

    def server(self):
        run_checks_tool = tool(
            "run_checks",
            "Check a snapshot of the current worktree in a fresh, offline container. kind: 'typecheck' (tsc for "
            "api and web, ~10-60 s), 'build' (production vite + tsc build, ~10-90 s), 'data' (instant: finds records "
            "copied from the original into source) or 'scenarios' (starts the stack and runs the acceptance "
            "scenarios, minutes; optional scenario_ids). Returns PASSED/FAILED with the first errors. Rate-limited "
            "per round.",
            {"type": "object", "additionalProperties": False, "required": ["kind"],
             "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                            "scenario_ids": {"type": "array", "items": {"type": "string"}}}},
            annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=40_000),
        )(self.run_checks)
        screenshot_tool = tool(
            "get_screenshot",
            "Screenshot (PNG) of the ORIGINAL app's screen with this id (ids are listed in the brief). This is the "
            "only way to see the original app.",
            {"type": "object", "additionalProperties": False, "required": ["screen_id"],
             "properties": {"screen_id": {"type": "string"}}},
            annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=8_000_000),
        )(self.get_screenshot)
        return create_sdk_mcp_server(name="checks", version="1.0.0", tools=[run_checks_tool, screenshot_tool])


def _text(message: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": message}]}
    if error:
        out["is_error"] = True
    return out


def png_problem(data: bytes) -> str | None:
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        return "it is not a PNG file"
    width, height = struct.unpack(">II", data[16:24])
    if len(data) > MAX_SCREENSHOT_BYTES:
        return f"the file is too large ({len(data) // 1024} KB)"
    if max(width, height) > MAX_SCREENSHOT_SIDE:
        return f"the image is too large ({width}x{height} px)"
    return None


# ---------------------------------------------------------------- spend estimate
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
OUTPUT_SAFETY = 1.5   # streamed usage reports output tokens early; visible output is a floor, thinking is unseen


class SpendMeter:
    """Estimates a session's cost from the streamed messages, for sessions that die without a ResultMessage.

    Streamed usage is per API call and exact for input and cache tokens, but reports output tokens before the
    message is complete. Output is therefore taken as the larger of the reported count and the visible output
    (text, tool input, thinking; about 3 characters per token), times ``OUTPUT_SAFETY``. (Pacing needs no
    estimate: with ``max_budget_usd`` set, the CLI shows the agent its exact remaining budget every turn.)"""

    def __init__(self, model: str):
        self.model = model
        self.usage: dict[str, dict[str, int]] = {}
        self.visible: dict[str, int] = {}

    def observe(self, message: AssistantMessage) -> None:
        key = message.message_id or f"message-{len(self.usage)}"
        usage = message.usage or {}
        seen = self.usage.get(key, {})
        self.usage[key] = {k: max(int(seen.get(k) or 0), int(usage.get(k) or 0)) for k in _USAGE_KEYS}
        chars = 0
        for block in message.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
            elif isinstance(block, ToolUseBlock):
                chars += len(json.dumps(block.input, default=str))
            elif isinstance(block, ThinkingBlock):
                chars += len(block.thinking)
        self.visible[key] = self.visible.get(key, 0) + chars   # one API message can arrive as several blocks

    def spent(self) -> float:
        total = 0.0
        for key, usage in self.usage.items():
            output = max(usage["output_tokens"], self.visible.get(key, 0) // 3) * OUTPUT_SAFETY
            total += cost_usd(self.model, {**usage, "output_tokens": output})
        return total


# ---------------------------------------------------------------- the build loop
async def run_builder(repo: Path, spec: AppSpecDraft, manifest: CodegenManifest, *, context_dir: Path,
                      screenshots: dict[str, Path], failing: list[ScenarioResult], run_id: str, budget_usd: float,
                      model: str, rounds: int = 2, max_turns: int = MAX_TURNS,
                      check_timeout_s: int = Limits().run_checks_timeout_s,
                      scenarios: list | None = None) -> BuildReport:
    """Run up to ``rounds`` guarded builder sessions on ``repo`` and return what happened.

    ``repo`` is the generated git repository (or a worktree of it) with a clean working tree. ``context_dir`` is a
    curated, read-only directory the agent may read (never the run directory). ``screenshots`` maps screen ids to
    PNGs of the original; they must live outside ``repo`` and ``context_dir``. ``scenarios`` are the ones the
    replica must pass (the protected ones; default: all of the spec's), ``failing`` their results on the baseline.
    """
    required = list(spec.scenarios if scenarios is None else scenarios)
    repo = Path(repo).resolve()
    context_dir = Path(context_dir).resolve()
    key = api_key()
    if not key:
        raise BuildError("ANTHROPIC_API_KEY is not set (looked in the environment and ~/.forge.env)")
    _validate_inputs(repo, context_dir, screenshots)
    base_commit = gitrepo.head_sha(repo)
    notes: list[str] = []
    reference = _reference_hashes(repo, manifest, notes)
    editable = [glob_regex(g) for g in manifest.editable_globs]
    guard = make_guard(repo, [context_dir], set(reference), editable_globs=manifest.editable_globs,
                       extra_writable=[CONTRACT_CHANGE_FILE], allow_tools=CHECK_TOOLS)
    tools = CheckTools(repo, spec, screenshots, run_id=run_id, timeout_s=check_timeout_s, context_dir=context_dir,
                       scenarios=required)
    server = tools.server()
    system_append = PROMPT_FILE.read_text()

    async def suite(kinds: list[str]) -> list[CheckReport]:
        return await asyncio.to_thread(run_check_suite, repo, kinds, None, run_id=run_id, timeout_s=check_timeout_s,
                                       scenarios=required, operations=op_wires(spec))

    log: list[BuildRound] = []
    commits: list[str] = []
    denials: list[GuardDenialRecord] = []
    requests: list[str] = []
    last_checks: list[CheckReport] = []
    feedback: str | None = None
    spent = 0.0
    green = False
    for n in range(1, max(0, rounds) + 1):
        remaining = budget_usd - spent
        round_budget = remaining if n == rounds else remaining * EARLY_ROUND_SHARE
        if round_budget < MIN_ROUND_USD:
            notes.append(f"stopped before round {n}: ${remaining:.2f} of the budget left")
            break
        pre_sha = gitrepo.head_sha(repo)
        tools.new_round()
        seen_before = len(guard.denials) + sum(guard.allowed.values())
        denials_before = len(guard.denials)
        record = BuildRound(round=n, budget_usd=round(round_budget, 4))
        prompt = round_brief(repo=repo, spec=spec, manifest=manifest, failing=failing, screenshots=screenshots,
                             context_dir=context_dir, round_no=n, rounds=rounds, budget_usd=round_budget,
                             feedback=feedback)
        meter = SpendMeter(model)
        options = _options(repo, guard, server, system_append=system_append, model=model, budget_usd=round_budget,
                           max_turns=max_turns, key=key, check_timeout_s=check_timeout_s)
        result, uses, error = await _session(prompt, options, meter)
        _record_result(record, result, uses, error)
        if result is None:   # no ResultMessage, so no reported cost: count the estimate rather than nothing
            record.cost_usd = round(meter.spent(), 6)
        record.check_calls, record.screenshot_calls = tools.check_calls, tools.screenshot_calls
        spent += record.cost_usd
        denials += [GuardDenialRecord(round=n, tool=d.tool, target=d.target, reason=d.reason)
                    for d in guard.denials[denials_before:]]

        violations = _violations(repo, reference, editable)
        file_uses = sum(c for name, c in uses.items() if name in FILE_TOOLS)
        if file_uses and len(guard.denials) + sum(guard.allowed.values()) == seen_before:
            violations.insert(0, f"the guard was not consulted for {file_uses} file tool call(s)")
        if violations:
            _revert(repo, pre_sha)
            record.rejected, record.violations = True, violations
            feedback = ("Your previous round was discarded and all of its edits were reverted, because it changed "
                        "files you may not change: " + "; ".join(violations[:10]) + ". Start again from the files "
                        "as they are now and edit only the allowed paths.")
            log.append(record)
            if violations[0].startswith("the guard was not consulted"):
                notes.append(f"round {n}: hooks were not active; stopping the build")
                break
            continue

        record.contract_change_request = _take_contract_request(repo)
        if record.contract_change_request:
            requests.append(record.contract_change_request)
        record.checks = await suite(HARNESS_KINDS)
        last_checks = record.checks
        record.green = green = is_green(record.checks)
        record.commit = gitrepo.commit_all(repo, f"build round {n}")
        commits.append(record.commit)
        log.append(record)
        if green:
            break
        feedback = ("After your previous round the harness re-ran the checks on a fresh snapshot:\n"
                    + "\n\n".join(c.text() for c in record.checks if not c.passed)
                    + "\nFix these first.")

    if not last_checks:
        last_checks = await suite(HARNESS_KINDS)
        green = is_green(last_checks)
        notes.append("no round reached the harness checks; the checks above are of the starting commit")
    reverted: list[str] = []
    if commits and not passes_gate(last_checks):
        last_checks, reverted, salvage_notes = await _salvage(repo, base_commit, last_checks, suite)
        green = is_green(last_checks)
        notes += salvage_notes
    if any(c.kind == "scenarios" and c.status == "unavailable" for c in last_checks):
        notes.append("scenario checks were unavailable; green means typecheck and build passed")
    header = ArtifactHeader(
        schema_id="forge.build/1", run_id=run_id, stage="build", stage_version=STAGE_VERSION, model=model,
        prompt_sha=hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest()[:16],
        inputs=[NameValue(name="manifest_commit", value=manifest.commit), NameValue(name="base_commit",
                                                                                     value=base_commit)])
    return BuildReport(
        header=header, repo=str(repo), base_commit=base_commit, final_commit=gitrepo.head_sha(repo), green=green,
        rounds=log, commits=commits, checks=last_checks, cost_usd=round(spent, 4),
        turns=sum(r.turns for r in log), guard_denials=denials,
        terminal_reason=next((r.terminal_reason for r in reversed(log) if r.terminal_reason), None),
        contract_change_requests=requests, notes=notes, reverted_files=reverted)


def passes_gate(checks: list[CheckReport]) -> bool:
    by_kind = {c.kind: c for c in checks}
    return all(by_kind.get(k) is not None and by_kind[k].passed for k in GATE_KINDS)


def is_green(checks: list[CheckReport]) -> bool:
    by_kind = {c.kind: c for c in checks}
    required = [by_kind.get("typecheck"), by_kind.get("build")]
    if any(c is None or not c.passed for c in required):
        return False
    return all(c.passed or (c.kind == "scenarios" and c.status == "unavailable") for c in checks)


def _options(repo: Path, guard: Guard, server, *, system_append: str, model: str, budget_usd: float,
             max_turns: int, key: str, check_timeout_s: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
        tools=list(FILE_TOOLS),
        allowed_tools=list(ALLOWED_TOOLS),
        mcp_servers={"checks": server},
        strict_mcp_config=True,
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=str(repo),
        hooks={"PreToolUse": [HookMatcher(matcher=HOOK_MATCHER, hooks=[guard]),
                              HookMatcher(matcher="*", hooks=[guard.catch_all])]},
        max_turns=max_turns,
        max_budget_usd=round(budget_usd, 4),
        model=model,
        effort=EFFORT["build"],
        max_buffer_size=32 * 1024 * 1024,
        env={"ANTHROPIC_API_KEY": key, "MAX_MCP_OUTPUT_TOKENS": "60000",
             "MCP_TOOL_TIMEOUT": str((check_timeout_s + 180) * 1000), "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )


async def _session(prompt: str, options: ClaudeAgentOptions,
                   meter: SpendMeter | None = None) -> tuple[ResultMessage | None, Counter, str | None]:
    result: ResultMessage | None = None
    uses: Counter = Counter()
    error: str | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                uses.update(block.name for block in message.content if isinstance(block, ToolUseBlock))
                if meter is not None:
                    meter.observe(message)
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as exc:  # noqa: BLE001 - a crashed session is a round outcome, the harness still checks
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    return result, uses, error


def _record_result(record: BuildRound, result: ResultMessage | None, uses: Counter, error: str | None) -> None:
    record.error = error
    record.tool_uses = [NameValue(name=k, value=str(v)) for k, v in sorted(uses.items())]
    if result is None:
        return
    usage = result.usage or {}
    record.cost_usd = round(float(result.total_cost_usd or 0.0), 6)
    record.turns = int(result.num_turns or 0)
    record.subtype = result.subtype
    record.terminal_reason = result.terminal_reason
    record.input_tokens = int(usage.get("input_tokens") or 0)
    record.output_tokens = int(usage.get("output_tokens") or 0)
    record.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    record.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    if result.is_error and not error:
        record.error = "; ".join(result.errors or []) or result.subtype


# ---------------------------------------------------------------- salvage
_PATH_IN_OUTPUT = re.compile(r"(?<![\w@./-])((?:api|web)/[\w@./-]+?\.(?:tsx?|jsx?|mjs|cjs|css|json|html))(?![\w-])")


def named_paths(checks: list[CheckReport]) -> list[str]:
    """Repo paths mentioned by the failing gate checks, in order of appearance."""
    out: list[str] = []
    for check in checks:
        if check.kind in GATE_KINDS and not check.passed:
            for match in _PATH_IN_OUTPUT.finditer(check.summary):
                if match.group(1) not in out:
                    out.append(match.group(1))
    return out


async def _salvage(repo: Path, base_sha: str, checks: list[CheckReport],
                   suite) -> tuple[list[CheckReport], list[str], list[str]]:
    """The final state fails typecheck or build, so it can be neither verified nor delivered. Revert, in widening
    steps, the builder's changes to the files the errors name, then to the failing area (api/ or web/), then all
    of them, re-checking after each step, and commit the first state that passes. If nothing passes, keep the
    builder's version. Returns (checks, reverted files, notes)."""
    agent_sha = gitrepo.head_sha(repo)
    changed = _changed_since(repo, base_sha)
    if not changed:
        return checks, [], []
    original, reverted = checks, []

    async def revert(paths: list[str]) -> list[CheckReport]:
        _restore_files(repo, base_sha, paths)
        reverted.extend(paths)
        return await suite(GATE_KINDS)

    for _ in range(SALVAGE_STEPS):
        named = [p for p in named_paths(checks) if p in changed and p not in reverted]
        if not named:
            break
        checks = await revert(named)
        if passes_gate(checks):
            break
    if not passes_gate(checks):
        areas = {p.split("/", 1)[0] for p in named_paths(checks)} or {"api", "web"}
        area = [p for p in changed if p.split("/", 1)[0] in areas and p not in reverted]
        if area:
            checks = await revert(area)
    if not passes_gate(checks):
        rest = [p for p in changed if p not in reverted]
        if rest:
            checks = await revert(rest)
    if not passes_gate(checks):
        _revert(repo, agent_sha)
        return original, [], ["typecheck/build fail with and without the builder's changes; kept the builder's version"]
    checks = await suite(HARNESS_KINDS)
    reverted = sorted(set(reverted))
    gitrepo.commit_all(repo, "build: revert files that failed the checks\n\n" + "\n".join(reverted))
    return checks, reverted, [f"typecheck/build failed after the last round; reverted {len(reverted)} file(s) to the "
                              f"starting version: {', '.join(reverted[:20])}"]


def _changed_since(repo: Path, base_sha: str) -> list[str]:
    out = gitrepo.git(repo, "--literal-pathspecs", "diff", "--name-only", "-z", "--no-renames", base_sha, "HEAD")
    return [p for p in out.split("\0") if p]


def _restore_files(repo: Path, base_sha: str, paths: list[str]) -> None:
    """Put ``paths`` back to their version at ``base_sha``; files that did not exist there are removed."""
    in_base = set(gitrepo.git(repo, "ls-tree", "-r", "-z", "--name-only", base_sha).split("\0"))
    existing = [p for p in paths if p in in_base]
    if existing:
        gitrepo.git(repo, "--literal-pathspecs", "checkout", "-q", base_sha, "--", *existing)
    for p in paths:
        if p not in in_base:
            (repo / p).unlink(missing_ok=True)


# ---------------------------------------------------------------- repo state
def _validate_inputs(repo: Path, context_dir: Path, screenshots: dict[str, Path]) -> None:
    if not (repo / ".git").exists():
        raise BuildError(f"{repo} is not a git repository")
    dirty = _status_paths(repo)
    if dirty:
        raise BuildError(f"{repo} has uncommitted changes ({', '.join(dirty[:5])}); commit them before building")
    if not context_dir.is_dir():
        raise BuildError(f"context directory {context_dir} does not exist")
    runs = RUNS_DIR.resolve()
    if context_dir == runs or context_dir.parent == runs or (context_dir / "ledger.sqlite").exists() \
            or (context_dir / "evidence").exists():
        raise BuildError(f"{context_dir} looks like a run directory; give the builder a curated context directory")
    for screen_id, path in screenshots.items():
        real = Path(path).resolve()
        if real.is_relative_to(repo) or real.is_relative_to(context_dir):
            raise BuildError(f"screenshot {screen_id} ({path}) must live outside the worktree and the context "
                             "directory; the agent sees screenshots only through get_screenshot")


def _reference_hashes(repo: Path, manifest: CodegenManifest, notes: list[str]) -> dict[str, str]:
    """Immutable path -> expected sha256. Files that already differ from the manifest keep their current hash
    (Verify will report them; the builder is only held to not changing them further)."""
    reference: dict[str, str] = {}
    for entry in manifest.immutable:
        current = _sha256(repo / entry.path)
        if current != entry.sha256:
            notes.append(f"{entry.path} already differed from the manifest before the build "
                         f"({'missing' if current is None else 'hash mismatch'})")
            reference[entry.path] = current or "missing"
        else:
            reference[entry.path] = entry.sha256
    return reference


def _sha256(path: Path) -> str | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return "not-a-regular-file"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _violations(repo: Path, reference: dict[str, str], editable: list) -> list[str]:
    out = [f"{rel} (immutable) changed" for rel, sha in reference.items()
           if (_sha256(repo / rel) or "missing") != sha]
    allowed_extra = fold(CONTRACT_CHANGE_FILE)
    for rel in _status_paths(repo):
        folded = fold(rel)
        if folded == allowed_extra or any(rx.fullmatch(folded) for rx in editable):
            if not os.path.islink(repo / rel):
                continue
            out.append(f"{rel} is a symbolic link")
            continue
        if f"{rel} (immutable) changed" not in out:
            out.append(f"{rel} is outside the editable paths")
    return out


def _status_paths(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", *gitrepo.IDENTITY, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "status",
         "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        cwd=repo, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise BuildError(f"git status failed in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    entries = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [entry[3:] for entry in entries if len(entry) > 3]


def _revert(repo: Path, sha: str) -> None:
    """Discard a rejected round: restore tracked files at ``sha`` and remove new untracked (not ignored) files."""
    gitrepo.git(repo, "checkout", "-q", "-f", sha, "--", ".")
    gitrepo.git(repo, "clean", "-fdq")


def _take_contract_request(repo: Path) -> str | None:
    path = repo / CONTRACT_CHANGE_FILE
    try:
        st = os.lstat(path)
    except OSError:
        return None
    try:
        text = path.read_text(errors="replace")[:MAX_REQUEST_CHARS] if stat.S_ISREG(st.st_mode) else None
    finally:
        path.unlink(missing_ok=True)   # a request travels in the report, not in the replica's history
    return text


# ---------------------------------------------------------------- the round brief
def round_brief(*, repo: Path, spec: AppSpecDraft, manifest: CodegenManifest, failing: list[ScenarioResult],
                screenshots: dict[str, Path], context_dir: Path, round_no: int, rounds: int, budget_usd: float,
                feedback: str | None) -> str:
    screen_ids = [s.id for s in spec.screens] + sorted(set(screenshots) - {s.id for s in spec.screens})
    per_screen = {sid: reference_files(context_dir, sid) for sid in screen_ids}
    referenced = {name for names in per_screen.values() for name in names}
    out = [f"# Build round {round_no} of {rounds}: {spec.app_name}", "",
           "Primary goals, in this order: (1) the failing scenarios below pass, so the replica behaves and is "
           "structured like the original (they already passed on the original); (2) every screen looks the same "
           "as the original (exact values from the reference files, compared with get_screenshot). Records come "
           "from the API, never from the original's pages. Roles and accessible names stay unchanged.", "",
           f"Worktree (repo root): {repo}",
           f"Read-only context directory: {context_dir}" + _listing(context_dir, exclude=referenced),
           f"Budget for this round: ${budget_usd:.2f} (the session stops when it is spent).", ""]
    if feedback:
        out += ["## Result of the previous round", feedback, ""]

    ops = {o.id: o for o in spec.operations}
    stub_dir = repo / "api" / "src" / "stubs"
    stub_files = sorted(p.relative_to(repo).as_posix() for p in stub_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()) if stub_dir.is_dir() else []
    out.append("## Stubs to implement")
    for op_id in manifest.stubs:
        op = ops.get(op_id)
        if op is None:
            out.append(f"- {op_id} (not described in the spec)")
            continue
        response = op.response + (f" (list key \"{op.list_key}\")" if op.list_key else "")
        out.append(f"- {op.id}: {op.method} {op.path}, {op.kind} on {op.entity}, responds {op.success_status} "
                   f"with {response}")
        if op.request_fields:
            out.append(f"  request fields: {', '.join(op.request_fields)}")
        if op.action_sets:
            out.append("  sets: " + ", ".join(f"{nv.name} = {nv.value}" for nv in op.action_sets))
        if op.action_note:
            out.append(f"  what the original does: {op.action_note}")
        files = [f for f in stub_files if fold(op.id) in fold(f)]
        if files:
            out.append(f"  stub file: {', '.join(files)}")
    if not manifest.stubs:
        out.append("(none)")
    if stub_files:
        out.append(f"All stub files: {', '.join(stub_files)}")

    out += ["", "## Data model"]
    for entity in spec.entities:
        fields = [f for f in spec.fields if f.entity == entity.name]
        out.append(f"- {entity.name} ({entity.id_type} id): " + "; ".join(_field_text(f) for f in fields))

    out += ["", "## Screens: match each one to the original; keep every role and accessible name below exactly"]
    for screen in spec.screens:
        shot = "screenshot: get_screenshot" if screen.id in screenshots else "no screenshot"
        out.append(f"- screen {screen.id}: route {screen.route}, \"{screen.title}\" ({screen.kind}; {shot})")
        if per_screen[screen.id]:
            out.append(f"  reference files: {', '.join(per_screen[screen.id])}")
        out += _element_tree([e for e in spec.elements if e.screen == screen.id])
    extra = [sid for sid in screen_ids if sid not in {s.id for s in spec.screens}]
    for sid in extra:
        refs = f" (reference files: {', '.join(per_screen[sid])})" if per_screen[sid] else ""
        out.append(f"- other screenshot: {sid}{refs}")

    out += ["", "## Failing scenarios"]
    scenarios = {s.id: s for s in spec.scenarios}
    shown: set[str] = set()
    for result in failing:
        if result.passed or result.scenario_id in shown:
            continue
        shown.add(result.scenario_id)
        where = f" at step {result.failed_step}" if result.failed_step is not None else ""
        out.append(f"- {result.scenario_id}{where} ({result.failure_kind}): {(result.message or '')[:400]}")
        scenario = scenarios.get(result.scenario_id)
        if scenario is not None:
            out.append(f"  \"{scenario.title}\" steps:")
            for i, step in enumerate(scenario.steps):
                mark = "   <-- failed here" if i == result.failed_step else ""
                out.append(f"    {i}. {describe_step(step)}{mark}")
    if not shown:
        out.append("(none reported)")
    out += ["", "When typecheck and build pass and every screen matches the original as closely as the budget "
                "allows, stop and summarize what changed per screen and what still differs."]
    return "\n".join(out)


def _field_text(field) -> str:
    bits = [field.name, field.type]
    if field.enum_values:
        bits.append("|".join(field.enum_values))
    if field.references:
        bits.append(f"-> {field.references}")
    if field.required:
        bits.append("required")
    if field.default is not None:
        bits.append(f"default {field.default}")
    if field.generated != "none":
        bits.append(f"generated {field.generated}")
    return " ".join(bits)


def _element_tree(elements: list[SpecElement]) -> list[str]:
    ids = {e.id for e in elements}
    children: dict[str | None, list[SpecElement]] = {}
    for e in elements:
        children.setdefault(e.parent if e.parent in ids else None, []).append(e)
    lines: list[str] = []
    stack = [(e, 1) for e in sorted(children.get(None, []), key=lambda e: e.order, reverse=True)]
    seen: set[str] = set()
    while stack:
        element, depth = stack.pop()
        if element.id in seen:
            continue
        seen.add(element.id)
        name = f"\"{element.name}\"" if element.name else (
            f"(name from the {element.field} value)" if element.field else "(no name)")
        lines.append(f"{'  ' * depth}- {element.role} {name} [{element.purpose}]")
        stack += [(c, depth + 1) for c in sorted(children.get(element.id, []), key=lambda e: e.order, reverse=True)]
    return lines


def reference_files(context_dir: Path, screen_id: str) -> list[str]:
    """The per-screen reference files (snapshot, sanitized DOM, computed styles) present in the context dir."""
    out = []
    for suffix in REFERENCE_SUFFIXES:
        path = context_dir / f"{screen_id}{suffix}"
        if path.is_file() and not path.is_symlink():
            out.append(path.name)
    return out


def _listing(path: Path, limit: int = 40, exclude: set[str] = frozenset()) -> str:
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir()
                       if not p.is_symlink() and p.name not in exclude)
    except OSError:
        return ""
    refs = " per-screen reference files (listed under Screens)" if exclude else ""
    if not names:
        return f" (contains:{refs})" if refs else " (empty)"
    more = f", ... ({len(names) - limit} more)" if len(names) > limit else ""
    return " (contains: " + ", ".join(names[:limit]) + more + (f", plus{refs}" if refs else "") + ")"
