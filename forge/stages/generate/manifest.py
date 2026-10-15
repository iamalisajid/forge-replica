"""Which files of a generated repo are immutable (hash-checked by Verify) and which the builder may edit."""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from forge.core.gitrepo import git
from forge.models.base import ArtifactHeader, NameValue
from forge.models.run import CodegenManifest, ManifestEntry

SCHEMA_ID = "forge.codegen/1"
STAGE_VERSION = "1"
EDITABLE_GLOBS = ["web/src/generated/screens/**", "web/src/styles/**", "api/src/stubs/**"]


@lru_cache(maxsize=64)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """``**`` matches across directories, ``*`` and ``?`` within one path segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
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
    return re.compile("".join(out) + r"\Z")


def is_editable(path: str, globs: list[str] = EDITABLE_GLOBS) -> bool:
    return any(glob_regex(g).match(path) for g in globs)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tracked_files(repo: Path) -> list[str]:
    out = git(repo, "-c", "core.quotePath=false", "ls-files", "-z")
    return sorted(p for p in out.split("\0") if p)


def build_manifest(repo: Path, commit: str, run_id: str, *, inputs: list[NameValue],
                   stubs: list[str]) -> CodegenManifest:
    immutable = [ManifestEntry(path=p, sha256=sha256_file(repo / p)) for p in tracked_files(repo) if not is_editable(p)]
    return CodegenManifest(
        header=ArtifactHeader(schema_id=SCHEMA_ID, run_id=run_id, stage="generate", inputs=inputs,
                              stage_version=STAGE_VERSION),
        repo=str(repo), commit=commit, immutable=immutable, editable_globs=list(EDITABLE_GLOBS), stubs=stubs)


def check_immutable(repo: Path, manifest: CodegenManifest) -> list[str]:
    """Paths whose content no longer matches the manifest (changed or missing)."""
    bad = []
    for entry in manifest.immutable:
        path = repo / entry.path
        if not path.is_file() or sha256_file(path) != entry.sha256:
            bad.append(entry.path)
    return bad
