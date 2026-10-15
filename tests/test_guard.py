"""Unit tests for the builder's PreToolUse guard. Pure file-system tests: no network, no Docker, no API."""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from forge.stages.build.guard import CONTRACT_CHANGE_FILE, HOOK_MATCHER, glob_regex, make_guard

IMMUTABLE = {"package.json", "package-lock.json", "web/src/main.tsx", "api/src/server.ts", ".dockerignore"}
EDITABLE = ["web/src/generated/screens/**", "web/src/styles/**", "api/src/stubs/**"]
MCP_TOOLS = ["mcp__checks__run_checks", "mcp__checks__get_screenshot"]


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def layout(tmp_path: Path):
    wt = tmp_path / "wt"
    for rel in ["package.json", "package-lock.json", ".dockerignore", "web/src/main.tsx", "api/src/server.ts",
                "api/src/stubs/closeIssue.ts", "web/src/generated/screens/Projects.tsx", "web/src/styles/app.css",
                "README.md", ".git/config", ".git/hooks/pre-commit", ".env", ".claude/settings.json", "CLAUDE.md",
                ".mcp.json", ".forge/verify.yaml", "sub/.env.local", ".github/workflows/ci.yml"]:
        _write(wt / rel, f"content of {rel}")
    ctx = tmp_path / "ctx"
    _write(ctx / "spec.json", "{}")
    _write(ctx / ".env", "SECRET=1")
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", "secret")
    return wt, ctx, outside


@pytest.fixture
def guard(layout):
    wt, ctx, _ = layout
    return make_guard(wt, [ctx], IMMUTABLE, editable_globs=EDITABLE, allow_tools=MCP_TOOLS)


def allowed(guard, tool, **args) -> bool:
    return guard.check(tool, args) is None


def case_insensitive_fs(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("x")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


# ---- hook protocol ----
async def test_deny_has_exact_hook_shape(guard, layout):
    _, _, outside = layout
    out = await guard({"hook_event_name": "PreToolUse", "tool_name": "Read",
                       "tool_input": {"file_path": str(outside / "secret.txt")}}, "toolu_1", {"signal": None})
    assert set(out) == {"hookSpecificOutput"}
    spec = out["hookSpecificOutput"]
    assert set(spec) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert "outside the worktree" in spec["permissionDecisionReason"]
    assert guard.denials and guard.denials[-1].tool == "Read"


async def test_allowed_call_returns_no_decision(guard, layout):
    wt, _, _ = layout
    out = await guard({"tool_name": "Read", "tool_input": {"file_path": str(wt / "README.md")}}, "t", {})
    assert out == {}
    assert guard.allowed["Read"] == 1


def test_matcher_covers_every_file_tool():
    assert set(HOOK_MATCHER.split("|")) == {"Read", "Edit", "Write", "Glob", "Grep", "MultiEdit", "NotebookEdit"}


# ---- inside / outside ----
def test_read_and_write_inside_worktree(guard, layout):
    wt, _, _ = layout
    assert allowed(guard, "Read", file_path=str(wt / "README.md"))
    assert allowed(guard, "Read", file_path="README.md")          # relative to the session cwd
    assert allowed(guard, "Edit", file_path=str(wt / "api/src/stubs/closeIssue.ts"), old_string="a", new_string="b")
    assert allowed(guard, "Write", file_path=str(wt / "web/src/generated/screens/new/Detail.tsx"), content="x")
    assert allowed(guard, "Write", file_path=str(wt / CONTRACT_CHANGE_FILE), content="{}")


def test_outside_worktree_denied(guard, layout, tmp_path):
    _, _, outside = layout
    for tool in ("Read", "Write", "Edit"):
        reason = guard.check(tool, {"file_path": str(outside / "secret.txt"), "content": "x",
                                    "old_string": "a", "new_string": "b"})
        assert reason and "outside the worktree" in reason
    assert not allowed(guard, "Read", file_path=str(Path.home() / ".forge.env"))
    assert not allowed(guard, "Read", file_path="/etc/passwd")
    # a sibling whose name merely starts with the worktree's name is not inside it
    sibling = _write(tmp_path / "wt-evil" / "x.txt")
    assert not allowed(guard, "Read", file_path=str(sibling))


def test_dotdot_traversal_denied(guard, layout):
    wt, _, outside = layout
    assert not allowed(guard, "Read", file_path=f"{wt}/../outside/secret.txt")
    assert not allowed(guard, "Read", file_path="../outside/secret.txt")
    # even when it would land back inside the worktree
    assert not allowed(guard, "Read", file_path=f"{wt}/web/../README.md")
    reason = guard.check("Write", {"file_path": "api/src/stubs/../../../outside/x.ts", "content": "x"})
    assert reason and "'..'" in reason


def test_tilde_whitespace_and_invisible_characters_denied(guard, layout):
    wt, _, _ = layout
    assert not allowed(guard, "Read", file_path="~/.forge.env")
    assert not allowed(guard, "Read", file_path="~")
    assert not allowed(guard, "Read", file_path=" ~/.forge.env")          # the tool would trim, then expand ~
    assert not allowed(guard, "Read", file_path="﻿~/.forge.env")     # BOM is trimmed by JS, not by Python
    assert not allowed(guard, "Read", file_path=f"{wt}/README.md ")
    assert not allowed(guard, "Read", file_path=f"{wt}/READ​ME.md")  # zero-width space
    assert not allowed(guard, "Read", file_path=f"{wt}/README.md\x00")
    assert not allowed(guard, "Read", file_path=f"{wt}\\README.md")
    assert not allowed(guard, "Read", file_path="")
    assert not allowed(guard, "Read")


# ---- symlinks and hard links ----
def test_symlink_inside_worktree_pointing_outside_denied(guard, layout):
    wt, _, outside = layout
    (wt / "web/src/styles/leak.css").symlink_to(outside / "secret.txt")
    (wt / "web/src/generated/screens/outdir").symlink_to(outside, target_is_directory=True)
    assert not allowed(guard, "Read", file_path=str(wt / "web/src/styles/leak.css"))
    assert not allowed(guard, "Write", file_path=str(wt / "web/src/styles/leak.css"), content="x")
    assert not allowed(guard, "Read", file_path=str(wt / "web/src/generated/screens/outdir/secret.txt"))
    assert not allowed(guard, "Write", file_path=str(wt / "web/src/generated/screens/outdir/new.txt"), content="x")
    assert not allowed(guard, "Grep", pattern="secret", path=str(wt / "web/src/generated/screens/outdir"))
    assert not allowed(guard, "Glob", pattern="*", path=str(wt / "web/src/generated/screens/outdir"))


def test_symlink_inside_worktree_pointing_inside_denied(guard, layout):
    wt, _, _ = layout
    (wt / "web/src/styles/alias.css").symlink_to(wt / "web/src/styles/app.css")
    (wt / "web/src/styles/pkg.json").symlink_to(wt / "package.json")
    (wt / "web/src/styles/screens").symlink_to(wt / "web/src/generated/screens", target_is_directory=True)
    reason = guard.check("Read", {"file_path": str(wt / "web/src/styles/alias.css")})
    assert reason and "symbolic link" in reason
    assert not allowed(guard, "Write", file_path=str(wt / "web/src/styles/pkg.json"), content="{}")
    assert not allowed(guard, "Write", file_path=str(wt / "web/src/styles/screens/Projects.tsx"), content="x")


def test_worktree_reached_through_symlinked_prefix_is_fine(guard, layout, tmp_path):
    wt, _, _ = layout
    alias = tmp_path / "alias-to-wt"
    alias.symlink_to(wt, target_is_directory=True)
    assert allowed(guard, "Read", file_path=str(alias / "README.md"))
    assert allowed(guard, "Write", file_path=str(alias / "api/src/stubs/closeIssue.ts"), content="x")
    assert not allowed(guard, "Write", file_path=str(alias / "package.json"), content="{}")


def test_hard_link_denied(guard, layout):
    wt, _, outside = layout
    os.link(outside / "secret.txt", wt / "web/src/styles/hard.css")
    reason = guard.check("Read", {"file_path": str(wt / "web/src/styles/hard.css")})
    assert reason and "hard-linked" in reason


# ---- case and unicode normalisation ----
def test_case_variants_cannot_reach_protected_files(guard, layout):
    wt, _, _ = layout
    assert not allowed(guard, "Write", file_path=str(wt / "PACKAGE.JSON"), content="{}")
    assert not allowed(guard, "Write", file_path=str(wt / "Web/Src/Main.tsx"), content="x")
    assert not allowed(guard, "Read", file_path=str(wt / ".GIT/config"))
    assert not allowed(guard, "Write", file_path=str(wt / ".Git/hooks/pre-commit"), content="x")
    assert not allowed(guard, "Read", file_path=str(wt / "claude.MD"))
    assert not allowed(guard, "Write", file_path=str(wt / ".Claude/settings.json"), content="{}")


def test_case_variant_of_worktree_root(guard, layout):
    wt, _, _ = layout
    variant = Path(str(wt)[:-2] + "WT")
    if case_insensitive_fs(wt.parent):
        assert allowed(guard, "Read", file_path=str(variant / "README.md"))
        assert not allowed(guard, "Write", file_path=str(variant / "package.json"), content="{}")
    else:   # a different directory on a case-sensitive volume: casefolding must not make it "inside"
        _write(variant / "README.md")
        assert not allowed(guard, "Read", file_path=str(variant / "README.md"))


def test_nfc_nfd_variants_of_immutable_file(tmp_path):
    wt = tmp_path / "wt"
    nfc = unicodedata.normalize("NFC", "web/src/styles/café.css")
    nfd = unicodedata.normalize("NFD", "web/src/styles/café.css")
    assert nfc != nfd
    _write(wt / nfc)
    g = make_guard(wt, [], {nfc}, editable_globs=EDITABLE)
    assert not allowed(g, "Write", file_path=str(wt / nfc), content="x")
    assert not allowed(g, "Write", file_path=str(wt / nfd), content="x")
    assert allowed(g, "Read", file_path=str(wt / nfd))
    g2 = make_guard(wt, [], {nfd}, editable_globs=EDITABLE)   # manifest spelled NFD, agent writes NFC
    assert not allowed(g2, "Write", file_path=str(wt / nfc), content="x")


def test_nfd_spelling_of_worktree_root(tmp_path):
    root_nfc = tmp_path / unicodedata.normalize("NFC", "répo")
    _write(root_nfc / "README.md")
    g = make_guard(root_nfc, [], set())
    nfd_path = str(tmp_path / unicodedata.normalize("NFD", "répo") / "README.md")
    if os.path.exists(nfd_path):   # normalisation-insensitive volume (APFS): same file, must be inside
        assert allowed(g, "Read", file_path=nfd_path)
    else:
        assert not allowed(g, "Read", file_path=nfd_path)


# ---- dot-config entries ----
@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", ".git", ".claude/settings.json",
                                 "CLAUDE.md", ".mcp.json", ".env", "sub/.env.local", ".forge/verify.yaml",
                                 ".github/workflows/ci.yml", ".gitignore", "web/src/styles/.npmrc"])
def test_dot_config_denied(guard, layout, rel):
    wt, _, _ = layout
    read = guard.check("Read", {"file_path": str(wt / rel)})
    write = guard.check("Write", {"file_path": str(wt / rel), "content": "x"})
    assert read and "protected configuration" in read
    assert write and "protected configuration" in write


def test_dot_config_in_editable_area_denied(guard, layout):
    wt, _, _ = layout
    assert not allowed(guard, "Write", file_path=str(wt / "web/src/generated/screens/.claude/settings.json"),
                       content="{}")
    assert not allowed(guard, "Write", file_path=str(wt / "api/src/stubs/CLAUDE.md"), content="x")


# ---- immutable and editable ----
def test_immutable_file_write_denied_read_allowed(guard, layout):
    wt, _, _ = layout
    reason = guard.check("Edit", {"file_path": str(wt / "package.json"), "old_string": "a", "new_string": "b"})
    assert reason and "immutable" in reason and CONTRACT_CHANGE_FILE in reason
    assert not allowed(guard, "Write", file_path=str(wt / "api/src/server.ts"), content="x")
    assert not allowed(guard, "Write", file_path=str(wt / ".dockerignore"), content="x")
    assert allowed(guard, "Read", file_path=str(wt / "package.json"))
    assert allowed(guard, "Read", file_path=str(wt / "api/src/server.ts"))


def test_immutable_without_editable_globs(layout):
    wt, ctx, _ = layout
    g = make_guard(wt, [ctx], IMMUTABLE)      # the minimal signature: only immutable files are protected
    assert allowed(g, "Write", file_path=str(wt / "web/src/anything.ts"), content="x")
    assert not allowed(g, "Write", file_path=str(wt / "package.json"), content="{}")


def test_writes_limited_to_editable_globs(guard, layout):
    wt, _, _ = layout
    reason = guard.check("Write", {"file_path": str(wt / "web/src/other.ts"), "content": "x"})
    assert reason and "not editable" in reason and "web/src/generated/screens/**" in reason
    assert not allowed(guard, "Write", file_path=str(wt / "README.md"), content="x")
    assert not allowed(guard, "Write", file_path=str(wt), content="x")
    assert allowed(guard, "Write", file_path=str(wt / "web/src/styles/theme/tokens.css"), content="x")


def test_glob_regex():
    rx = glob_regex("web/src/generated/screens/**")
    assert rx.fullmatch("web/src/generated/screens/a.tsx")
    assert rx.fullmatch("web/src/generated/screens/x/y/z.tsx")
    assert not rx.fullmatch("web/src/generated/other.tsx")
    one = glob_regex("api/src/stubs/*.ts")
    assert one.fullmatch("api/src/stubs/close.ts") and not one.fullmatch("api/src/stubs/a/close.ts")


# ---- context directory ----
def test_context_dir_read_allowed_write_denied(guard, layout):
    _, ctx, _ = layout
    assert allowed(guard, "Read", file_path=str(ctx / "spec.json"))
    reason = guard.check("Write", {"file_path": str(ctx / "spec.json"), "content": "{}"})
    assert reason and "read-only" in reason
    assert not allowed(guard, "Edit", file_path=str(ctx / "spec.json"), old_string="{", new_string="[")
    assert not allowed(guard, "Read", file_path=str(ctx / ".env"))
    assert allowed(guard, "Grep", pattern="x", path=str(ctx))
    assert allowed(guard, "Glob", pattern="*.json", path=str(ctx))


def test_context_dir_must_be_narrow(layout, tmp_path):
    wt, _, _ = layout
    with pytest.raises(ValueError):
        make_guard(wt, [Path.home()], set())
    with pytest.raises(ValueError):
        make_guard(wt, [Path("/")], set())
    with pytest.raises(ValueError):
        make_guard(wt, [tmp_path], set())          # contains the worktree
    with pytest.raises(ValueError):
        make_guard(wt, [wt / "web"], set())        # inside the worktree


# ---- Glob and Grep ----
def test_glob_patterns(guard, layout):
    wt, _, outside = layout
    assert allowed(guard, "Glob", pattern="**/*.tsx")
    assert allowed(guard, "Glob", pattern="web/src/**/*.{ts,tsx}", path=str(wt))
    for pattern in ["/etc/*", str(outside / "*"), "~/.ssh/*", "../outside/*", "web/../../*", "{/etc,src}/*",
                    "src/{a,/etc}/*", "\\/etc/*", "**/.env*", ".git/**", "**/.claude/*", "**/CLAUDE.md"]:
        reason = guard.check("Glob", {"pattern": pattern})
        assert reason, pattern
    reason = guard.check("Glob", {"pattern": "/etc/*"})
    assert reason and "absolute" in reason
    assert not allowed(guard, "Glob", pattern="*", path=str(outside))
    assert not allowed(guard, "Glob", pattern="*", path=f"{wt}/..")
    assert not allowed(guard, "Glob", pattern="*", path=str(wt / ".git"))
    assert not allowed(guard, "Glob")


def test_grep_path_checked(guard, layout):
    wt, _, outside = layout
    assert allowed(guard, "Grep", pattern="TODO")                       # default: the session cwd
    assert allowed(guard, "Grep", pattern="TODO", path=str(wt / "api"), output_mode="content", **{"-n": True})
    assert allowed(guard, "Grep", pattern="TODO", path="web/src")
    reason = guard.check("Grep", {"pattern": "secret", "path": str(outside)})
    assert reason and "outside the worktree" in reason
    assert not allowed(guard, "Grep", pattern="x", path="/")
    assert not allowed(guard, "Grep", pattern="x", path=str(Path.home()))
    assert not allowed(guard, "Grep", pattern="x", path=str(wt / ".git"))
    assert not allowed(guard, "Grep", pattern="KEY", glob=".env*")
    assert not allowed(guard, "Grep", pattern="KEY", glob="/etc/*")
    assert allowed(guard, "Grep", pattern="x", glob="*.tsx")


# ---- tools ----
@pytest.mark.parametrize("tool", ["Bash", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "", "mcp__other__x"])
def test_unknown_tools_denied(guard, tool):
    reason = guard.check(tool, {"command": "cat ~/.forge.env"})
    assert reason and "not available" in reason


def test_multiedit_and_notebookedit_denied(guard, layout):
    wt, _, _ = layout
    assert not allowed(guard, "MultiEdit", file_path=str(wt / "api/src/stubs/closeIssue.ts"), edits=[])
    assert not allowed(guard, "NotebookEdit", notebook_path=str(wt / "x.ipynb"), new_source="")


def test_unexpected_path_argument_denied(guard, layout):
    wt, _, outside = layout
    reason = guard.check("Read", {"file_path": str(wt / "README.md"), "extra_path": str(outside / "secret.txt")})
    assert reason and "unexpected path argument" in reason


async def test_catch_all_hook(guard, layout):
    wt, _, _ = layout
    passthrough = await guard.catch_all({"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}}, "t", {})
    assert passthrough == {}                 # file tools are decided by the main matcher
    assert await guard.catch_all({"tool_name": "mcp__checks__run_checks", "tool_input": {"kind": "build"}},
                                 "t", {}) == {}
    denied = await guard.catch_all({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t", {})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_fails_closed_on_bad_input(guard):
    assert not allowed(guard, "Read", file_path=123)
    assert not allowed(guard, "Read", file_path=["a"])
    assert not allowed(guard, "Glob", pattern=None)
