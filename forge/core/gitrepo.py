"""Generated apps are separate git repositories. Commits use a tool identity, never the user's."""
from __future__ import annotations

import subprocess
from pathlib import Path

IDENTITY = ["-c", "user.name=forge", "-c", "user.email=forge@localhost", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *IDENTITY, *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return head_sha(repo)


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def tag(repo: Path, name: str, sha: str | None = None) -> None:
    git(repo, "tag", "-f", name, *([sha] if sha else []))


def clone_at(repo: Path, dest: Path, sha: str) -> Path:
    subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True, capture_output=True)
    git(dest, "checkout", "-q", sha)
    return dest


def bundle(repo: Path, out_file: Path) -> Path:
    git(repo, "bundle", "create", str(out_file), "--all")
    return out_file
