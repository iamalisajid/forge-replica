"""Thin wrappers around the docker CLI. Every resource a run creates is named forge-<runid>-*."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from forge.core.config import BASE_IMAGE, REPO_ROOT, RUNNER_IMAGE, TEMPLATE_DIR


class DockerError(RuntimeError):
    pass


def run(args: list[str], *, timeout: int = 600, check: bool = True, cwd: Path | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd, input=input)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return proc


def compose(project: str, files: list[Path], args: list[str], *, timeout: int = 900, check: bool = True,
            env_file: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["compose", "-p", project]
    for f in files:
        cmd += ["-f", str(f)]
    if env_file:
        cmd += ["--env-file", str(env_file)]
    return run(cmd + args, timeout=timeout, check=check)


def project_name(run_id: str, suffix: str) -> str:
    return f"forge-{run_id}-{suffix}".lower().replace("_", "-")


def image_exists(tag: str) -> bool:
    return run(["image", "inspect", tag], check=False).returncode == 0


def _hash_paths(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        files = sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts) if path.is_dir() else [path]
        for f in files:
            h.update(str(f.relative_to(REPO_ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def base_image_tag() -> str:
    t = TEMPLATE_DIR
    return f"{BASE_IMAGE}:{_hash_paths([t / 'package.json', t / 'package-lock.json', t / 'docker'])}"


def runner_image_tag() -> str:
    paths = [REPO_ROOT / "forge" / "__init__.py", REPO_ROOT / "forge" / "models",
             REPO_ROOT / "forge" / "stages" / "__init__.py", REPO_ROOT / "forge" / "stages" / "oracle",
             REPO_ROOT / "docker" / "runner.Dockerfile"]
    return f"{RUNNER_IMAGE}:{_hash_paths(paths)}"


def ensure_base_image() -> str:
    tag = base_image_tag()
    if not image_exists(tag):
        run(["build", "-q", "-f", str(TEMPLATE_DIR / "docker" / "base.Dockerfile"), "-t", tag, str(TEMPLATE_DIR)],
            timeout=1800)
    return tag


def ensure_runner_image() -> str:
    tag = runner_image_tag()
    if not image_exists(tag):
        run(["build", "-q", "-f", str(REPO_ROOT / "docker" / "runner.Dockerfile"), "-t", tag, str(REPO_ROOT)],
            timeout=1800)
    return tag
