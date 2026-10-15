"""PreToolUse guard for the builder agent: the boundary between Claude's file tools and the host.

Claude Code's built-in Read/Edit/Write/Glob/Grep are not confined to the session's ``cwd``, so this hook
decides every call before it runs. The policy:

* A path is resolved with ``os.path.realpath`` and compared after NFC normalisation and casefolding, because
  the default macOS file system is case- and normalisation-insensitive. Containment is confirmed a second time
  by inode, so casefolding can never make a different directory on a case-sensitive volume look "inside".
* Reads are allowed inside the worktree and the curated context directories. Writes are allowed only inside the
  worktree, never to immutable files and, when editable globs are given, only to files they match.
* Dot-config entries (``.git*``, ``.claude*``, ``.mcp*``, ``CLAUDE.md``, ``.env*``, ``.forge``, ``.npmrc``) are
  denied anywhere in the path, and so are symlinks (the target or any parent inside an allowed root), hard-linked
  files and special files.
* Inputs the tool would reinterpret are denied instead of emulated: ``..`` segments, ``~``, backslashes, control
  or invisible characters and leading/trailing whitespace. The guard therefore never disagrees with the tool
  about which file a path names.
* Glob patterns must be relative and free of ``..``; the ``path`` argument of Glob and Grep is checked like a
  read. MultiEdit, NotebookEdit and every tool the guard does not know are denied (fail closed).
"""
from __future__ import annotations

import contextlib
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

READ_TOOL = "Read"
WRITE_TOOLS = ("Edit", "Write")
SEARCH_TOOLS = ("Glob", "Grep")
FILE_TOOLS = (READ_TOOL, *WRITE_TOOLS, *SEARCH_TOOLS)
DENIED_TOOLS = ("MultiEdit", "NotebookEdit")
HOOK_MATCHER = "|".join(FILE_TOOLS + DENIED_TOOLS)

#: The one file outside the editable globs the builder may write: its channel for asking for contract changes.
CONTRACT_CHANGE_FILE = "contract-change-request.json"

_DOT_PREFIXES = (".git", ".claude", ".mcp", ".env", ".forge")
_DOT_NAMES = frozenset({"claude.md", "claude.local.md", ".npmrc"})
_EDGE_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})       # what JS String.trim() would strip
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})         # control, format (zero-width, BOM), separators
# Tool arguments that name paths. Any other argument whose name looks like a path is refused rather than ignored.
_PATH_ARGS = {"Read": {"file_path"}, "Edit": {"file_path"}, "Write": {"file_path"},
              "Glob": {"path", "pattern"}, "Grep": {"path", "glob"}}
_PATHLIKE_ARG = re.compile(r"path|file|dir|root|cwd", re.IGNORECASE)


def fold(text: str) -> str:
    """Compare paths the way a case- and normalisation-insensitive file system does."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def is_dot_config(name: str) -> bool:
    folded = fold(name)
    return folded in _DOT_NAMES or folded.startswith(_DOT_PREFIXES)


def deny(reason: str) -> dict[str, Any]:
    """The PreToolUse hook output that blocks a tool call; the reason is shown to the agent."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a manifest glob (``**`` crosses directories, ``*`` and ``?`` do not) over folded POSIX paths."""
    pattern = fold(_clean_rel(pattern))
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


@dataclass(frozen=True)
class Denial:
    tool: str
    target: str
    reason: str


class _Deny(Exception):
    pass


@dataclass(frozen=True)
class _Root:
    real: str               # realpath of the root
    folded: str
    dev_ino: tuple[int, int]
    writable: bool
    label: str


def _clean_rel(path: str) -> str:
    path = posixpath.normpath(path.replace("\\", "/"))
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _dev_ino(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _contains(outer: str, inner: str) -> bool:
    """Whether real path ``inner`` is ``outer`` or below it (folded comparison)."""
    o, i = fold(outer).rstrip("/"), fold(inner)
    return i == o or i.startswith(o + "/") or o == ""


class Guard:
    """Async PreToolUse hook callback. Keeps a log of every denial in ``denials``."""

    def __init__(self, worktree: Path, context_dirs: Iterable[Path], immutable: Iterable[str], *,
                 editable_globs: Iterable[str] | None = None,
                 extra_writable: Iterable[str] = (CONTRACT_CHANGE_FILE,),
                 allow_tools: Iterable[str] = ()):
        worktree = Path(worktree)
        immutable = list(immutable)                       # iterated twice below; accept any iterable
        editable_globs = list(editable_globs) if editable_globs is not None else None
        if not worktree.is_dir():
            raise ValueError(f"worktree {worktree} is not a directory")
        self.worktree = os.path.normpath(os.path.abspath(worktree))   # the session cwd: base for relative paths
        self.roots = [self._root(worktree, writable=True, label="worktree")]
        for ctx in context_dirs:
            self.roots.append(self._root(Path(ctx), writable=False, label="context directory"))
        self._validate_roots()
        wt = self.roots[0]

        self.immutable = {fold(_clean_rel(p)) for p in immutable}
        self._immutable_ids: set[tuple[int, int]] = set()
        for rel in immutable:
            try:
                st = os.lstat(os.path.join(wt.real, _clean_rel(rel)))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                self._immutable_ids.add(_dev_ino(st))
        self._protected_ids = self._scan_protected(wt.real)
        self.editable = [glob_regex(g) for g in editable_globs] if editable_globs is not None else None
        self.editable_globs = list(editable_globs) if editable_globs is not None else None
        self.extra_writable = {fold(_clean_rel(p)) for p in extra_writable}
        self.allow_tools = frozenset(allow_tools)
        self.denials: list[Denial] = []
        self.allowed: dict[str, int] = {}

    # ---- hook entry points ----
    async def __call__(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool = str((input_data or {}).get("tool_name") or "")
        args = (input_data or {}).get("tool_input") or {}
        return self._decide(tool, args if isinstance(args, dict) else {"_": args})

    async def catch_all(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """Second hook for every tool: denies anything that is neither guarded here nor explicitly allowed."""
        tool = str((input_data or {}).get("tool_name") or "")
        if tool in FILE_TOOLS or tool in DENIED_TOOLS:
            return {}   # decided by the main guard (its own matcher)
        return await self(input_data, tool_use_id, context)

    def check(self, tool: str, args: dict[str, Any]) -> str | None:
        """None when the call may run, else the reason it is denied. Never raises."""
        try:
            self._check(tool, args)
        except _Deny as exc:
            return f"{tool} denied: {exc}"
        except Exception as exc:   # fail closed on anything unexpected
            return f"{tool} denied: the guard could not evaluate this call ({type(exc).__name__}: {exc})."
        return None

    def _decide(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        reason = self.check(tool, args)
        if reason is None:
            self.allowed[tool] = self.allowed.get(tool, 0) + 1
            return {}
        target = next((str(args.get(k)) for k in ("file_path", "path", "pattern", "glob", "notebook_path")
                       if args.get(k) is not None), "")
        self.denials.append(Denial(tool=tool, target=target[:300], reason=reason))
        return deny(reason)

    # ---- policy ----
    def _check(self, tool: str, args: dict[str, Any]) -> None:
        if tool in self.allow_tools:
            return
        if tool in DENIED_TOOLS:
            raise _Deny(f"{tool} is not available to the builder; use Edit (one change per call) or Write.")
        if tool not in FILE_TOOLS:
            raise _Deny(f"{tool} is not available to the builder. Use Read, Edit, Write, Glob, Grep and the "
                        "checks tools.")
        for key in args:
            if key not in _PATH_ARGS[tool] and _PATHLIKE_ARG.search(str(key)):
                raise _Deny(f"unexpected path argument {key!r}.")
        if tool == READ_TOOL:
            self._check_path(args.get("file_path"), write=False)
        elif tool in WRITE_TOOLS:
            self._check_path(args.get("file_path"), write=True)
        elif tool == "Glob":
            self._check_pattern(args.get("pattern"), "pattern", required=True)
            self._check_search_root(args.get("path"))
        else:   # Grep
            self._check_search_root(args.get("path"))
            if args.get("glob") not in (None, ""):
                self._check_pattern(args.get("glob"), "glob", required=False)

    def _check_path(self, raw: Any, *, write: bool) -> None:
        lexical = self._lexical(raw)
        real = os.path.realpath(lexical)
        root, rel = self._locate(raw, real)
        display = self._display(real, root, rel)
        for part in rel.split("/") if rel else ():
            if is_dot_config(part):
                raise _Deny(f"{display} is protected configuration ({part}); it cannot be read or written.")
        link = self._symlink_below(lexical, root)
        if link is not None:
            raise _Deny(f"{link} is a symbolic link; links are not followed.")
        ancestor = self._protected_ancestor(real, root)
        if ancestor is not None:
            raise _Deny(f"{display} is inside protected configuration ({ancestor}).")
        try:
            st = os.lstat(lexical)
        except FileNotFoundError:
            st = None
        except NotADirectoryError:
            raise _Deny(f"{display} does not name a file (a parent is not a directory).") from None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise _Deny(f"{display} is a symbolic link; links are not followed.")
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise _Deny(f"{display} is not a regular file or directory.")
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise _Deny(f"{display} is hard-linked to another file; it cannot be used.")
        if not write:
            return
        if not root.writable:
            raise _Deny(f"{raw} is in a read-only {root.label}; only files inside the worktree "
                        f"({self.worktree}) can be written.")
        if not rel or (st is not None and stat.S_ISDIR(st.st_mode)):
            raise _Deny(f"{display} is a directory; write files, not directories.")
        if rel in self.immutable or (st is not None and _dev_ino(st) in self._immutable_ids):
            raise _Deny(f"{display} is immutable (generated from the spec and hash-checked by Verify). Do not "
                        f"change it; if the contract itself must change, describe the change in "
                        f"{CONTRACT_CHANGE_FILE} at the repo root.")
        if self.editable is not None and rel not in self.extra_writable \
                and not any(rx.fullmatch(rel) for rx in self.editable):
            allowed = ", ".join([*(self.editable_globs or []), *sorted(self.extra_writable)])
            raise _Deny(f"{display} is not editable. You may write only: {allowed}.")

    def _check_search_root(self, raw: Any) -> None:
        if raw is None or raw == "":
            return   # defaults to the session cwd, which is the worktree
        self._check_path(raw, write=False)

    def _check_pattern(self, pattern: Any, label: str, *, required: bool) -> None:
        if pattern is None and not required:
            return
        if not isinstance(pattern, str) or not pattern:
            raise _Deny(f"{label} must be a non-empty string.")
        self._check_chars(pattern, label)
        if pattern.startswith(("/", "~")):
            raise _Deny(f"absolute {label}s are not allowed; use a pattern relative to the worktree and pass a "
                        f"directory inside it as `path` instead.")
        if "\\" in pattern:
            raise _Deny(f"backslashes are not allowed in the {label}.")
        if ".." in pattern:
            raise _Deny(f"'..' is not allowed in the {label}.")
        if re.search(r"[{,(|]\s*[/~]", pattern):
            raise _Deny(f"the {label} contains an absolute alternative; keep every part relative.")
        for token in re.split(r"[/{},()|]", pattern):
            token = token.strip().lstrip("!@+*?")
            if token and is_dot_config(token):
                raise _Deny(f"the {label} targets protected configuration ({token}).")

    # ---- helpers ----
    def _lexical(self, raw: Any) -> str:
        if not isinstance(raw, str) or raw == "":
            raise _Deny("a non-empty path is required.")
        self._check_chars(raw, "path")
        if raw.startswith("~"):
            raise _Deny(f"'~' paths are not allowed; use absolute paths inside the worktree ({self.worktree}).")
        if "\\" in raw:
            raise _Deny("backslashes are not allowed in paths.")
        if ".." in raw.split("/"):
            raise _Deny(f"'..' segments are not allowed; use absolute paths inside the worktree ({self.worktree}).")
        joined = raw if os.path.isabs(raw) else os.path.join(self.worktree, raw)
        return os.path.normpath(joined)

    @staticmethod
    def _check_chars(text: str, label: str) -> None:
        if any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text):
            raise _Deny(f"the {label} contains control or invisible characters.")
        if unicodedata.category(text[0]) in _EDGE_CATEGORIES or unicodedata.category(text[-1]) in _EDGE_CATEGORIES:
            raise _Deny(f"the {label} has leading or trailing whitespace.")

    def _locate(self, raw: Any, real: str) -> tuple[_Root, str]:
        for root in self.roots:
            rel = self._relative(real, root)
            if rel is not None and self._inside_by_inode(real, root):
                return root, rel
        where = "the worktree" + (" or the read-only context directories" if len(self.roots) > 1 else "")
        via_link = any(self._relative(os.path.normpath(raw if os.path.isabs(raw) else
                                                       os.path.join(self.worktree, raw)), r) is not None
                       for r in self.roots)
        raise _Deny(f"{raw} {'resolves through a symbolic link to a path ' if via_link else 'is '}outside "
                    f"{where}. Allowed: " + ", ".join(f"{r.real} ({r.label})" for r in self.roots) + ".")

    @staticmethod
    def _display(real: str, root: _Root, rel: str) -> str:
        """The relative path as the agent spelled it (``rel`` itself is folded)."""
        if real.startswith(root.real.rstrip("/") + "/"):
            return real[len(root.real.rstrip("/")) + 1:]
        return rel or "."

    @staticmethod
    def _relative(real: str, root: _Root) -> str | None:
        folded = fold(real)
        if folded == root.folded:
            return ""
        prefix = root.folded.rstrip("/") + "/"
        return folded[len(prefix):] if folded.startswith(prefix) else None

    @staticmethod
    def _inside_by_inode(real: str, root: _Root) -> bool:
        """Walk up from ``real`` (already free of links) and look for the root's inode."""
        cur = real
        while True:
            try:
                st = os.stat(cur)
                break
            except (FileNotFoundError, NotADirectoryError):
                parent = os.path.dirname(cur)
                if parent == cur:
                    return False
                cur = parent
        while True:
            if _dev_ino(st) == root.dev_ino:
                return True
            parent = os.path.dirname(cur)
            if parent == cur:
                return False
            cur = parent
            st = os.stat(cur)

    def _symlink_below(self, lexical: str, root: _Root) -> str | None:
        """The first symlink on ``lexical`` that lives inside ``root`` (the root itself may be reached via links)."""
        cur = lexical
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            parent_real = os.path.realpath(parent)
            if self._relative(parent_real, root) is None or not self._inside_by_inode(parent_real, root):
                return None
            if os.path.islink(cur):
                return cur
            cur = parent

    def _protected_ancestor(self, real: str, root: _Root) -> str | None:
        """Catch spellings of protected entries the name check misses, by inode of every existing ancestor."""
        if not self._protected_ids:
            return None
        cur = real
        while True:
            try:
                st = os.lstat(cur)
                if _dev_ino(st) in self._protected_ids:
                    return os.path.basename(cur)
            except OSError:
                pass
            if fold(cur) == root.folded:
                return None
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            cur = parent

    @staticmethod
    def _scan_protected(root_real: str) -> set[tuple[int, int]]:
        ids: set[tuple[int, int]] = set()
        for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
            for name in list(dirnames) + filenames:
                if is_dot_config(name):
                    with contextlib.suppress(OSError):
                        ids.add(_dev_ino(os.lstat(os.path.join(dirpath, name))))
            dirnames[:] = [d for d in dirnames if not is_dot_config(d) and d not in ("node_modules", "dist")]
        return ids

    @staticmethod
    def _root(path: Path, *, writable: bool, label: str) -> _Root:
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            raise ValueError(f"{label} {path} is not a directory")
        return _Root(real=real, folded=fold(real), dev_ino=_dev_ino(os.stat(real)), writable=writable, label=label)

    def _validate_roots(self) -> None:
        from forge.core.config import ENV_FILE, FORGE_HOME

        home = os.path.realpath(Path.home())
        sensitive = [os.path.realpath(FORGE_HOME), os.path.realpath(ENV_FILE)]
        worktree = self.roots[0]
        for root in self.roots:
            if root.real == "/" or _contains(root.real, home):
                raise ValueError(f"{root.label} {root.real} is too broad (it contains the home directory)")
        for ctx in self.roots[1:]:
            if _contains(ctx.real, worktree.real) or _contains(worktree.real, ctx.real):
                raise ValueError(f"context directory {ctx.real} overlaps the worktree {worktree.real}")
            for path in sensitive:
                if _contains(ctx.real, path):
                    raise ValueError(f"context directory {ctx.real} contains {path}")


def make_guard(worktree: Path, context_dirs: list[Path], immutable: set[str], **options: Any) -> Guard:
    """Build the PreToolUse hook callback for a builder session.

    ``immutable`` holds repo-relative paths (from the codegen manifest). Options: ``editable_globs`` (restrict
    writes to these globs plus ``extra_writable``), ``allow_tools`` (tool names, e.g. the checks MCP tools, the
    guard lets through when it is also registered as a catch-all).
    """
    return Guard(worktree, context_dirs, immutable, **options)
