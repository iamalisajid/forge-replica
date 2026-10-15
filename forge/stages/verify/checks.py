"""Verify checks. Network checks run from the runner container inside the stack's internal network.

* health: ``GET /__forge/health`` answers 200 (with retries while the stack starts);
* routes: ``GET /__forge/routes`` (test mode) equals the spec's operations, method + path;
* immutable hashes: every immutable file of the CodegenManifest is byte-identical;
* regenerate and diff: generated files equal a fresh render of the spec;
* restart persistence: list results are identical after ``docker compose restart db app``;
* egress: the host (where the original runs) and the internet are unreachable;
* request audit: scenarios on the replica made no request outside the replica's origin.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from itertools import product
from pathlib import Path
from typing import Any

from forge.core import docker
from forge.models.appspec import AppSpecDraft
from forge.models.run import CodegenManifest, HEALTH_PATH, ROUTES_PATH
from forge.models.scenario import ScenarioResult
from forge.models.verification import CheckResult
from forge.stages.verify.compose import APP_URL, runner_exec

DEFAULT_EGRESS_TARGETS = ("host.docker.internal:4001", "https://example.com")
_PROBE = ["python3", "-m", "forge.stages.oracle.probe"]
_SKIP_METHODS = {"HEAD", "OPTIONS"}


def _probe(project: str, files: list[Path], args: list[str], *, timeout: int = 300) -> tuple[int, Any]:
    proc = runner_exec(project, files, [*_PROBE, *args], timeout=timeout)
    try:
        return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return proc.returncode, {"error": (proc.stderr or proc.stdout).strip()[-500:]}


def fetch(project: str, files: list[Path], urls: list[str]) -> list[dict]:
    if not urls:
        return []
    args = ["fetch"]
    for url in urls:
        args += ["--url", url]
    _, data = _probe(project, files, args)
    return data if isinstance(data, list) else [{"url": u, "status": None, "error": str(data)} for u in urls]


# ----- health -----
def health(project: str, files: list[Path], *, timeout_s: int = 180, base_url: str = APP_URL) -> CheckResult:
    retries = max(1, timeout_s // 2)
    code, data = _probe(project, files, ["http", "--url", base_url + HEALTH_PATH, "--retries", str(retries),
                                         "--interval", "2"], timeout=timeout_s + 60)
    if code == 0:
        return CheckResult(name="health", status="pass", detail=f"{HEALTH_PATH} answered 200")
    got = data.get("status") or data.get("error") if isinstance(data, dict) else data
    return CheckResult(name="health", status="fail", detail=f"{HEALTH_PATH} not healthy: {got}")


# ----- routes == contract -----
def normalize_path(path: str) -> str:
    path = re.sub(r"\{[^/{}]+\}|:[A-Za-z_]\w*", "{}", path.split("?", 1)[0])
    return path.rstrip("/") or "/"


def _route_entries(data: Any) -> list[tuple[str, str]]:
    if isinstance(data, dict):
        for key in ("routes", "items", "data"):
            if isinstance(data.get(key), list):
                return _route_entries(data[key])
        return []
    out: list[tuple[str, str]] = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, str):
            method, _, path = entry.strip().partition(" ")
            out.append((method, path.strip()))
        elif isinstance(entry, dict):
            methods = entry.get("method") or entry.get("methods") or []
            path = entry.get("path") or entry.get("url") or entry.get("route") or ""
            for method in methods if isinstance(methods, list) else [methods]:
                out.append((str(method), str(path)))
    return out


def route_set(data: Any) -> set[tuple[str, str]]:
    """Normalised (METHOD, path) pairs, without HEAD/OPTIONS, forge's own routes and static wildcards."""
    out = set()
    for method, path in _route_entries(data):
        method = method.upper()
        if method in _SKIP_METHODS or not path or path.startswith("/__forge") or "*" in path:
            continue
        out.add((method, normalize_path(path)))
    return out


def contract_routes(spec: AppSpecDraft) -> set[tuple[str, str]]:
    return {(op.method.upper(), normalize_path(op.path)) for op in spec.operations}


def compare_routes(actual: set[tuple[str, str]], expected: set[tuple[str, str]]) -> CheckResult:
    missing, extra = sorted(expected - actual), sorted(actual - expected - {("GET", "/")})
    if not missing and not extra:
        return CheckResult(name="routes", status="pass", detail=f"{len(expected)} routes match the contract")
    parts = []
    if missing:
        parts.append("missing " + ", ".join(f"{m} {p}" for m, p in missing))
    if extra:
        parts.append("not in the contract " + ", ".join(f"{m} {p}" for m, p in extra))
    return CheckResult(name="routes", status="fail", detail="; ".join(parts))


def routes(project: str, files: list[Path], spec: AppSpecDraft) -> CheckResult:
    [res] = fetch(project, files, [APP_URL + ROUTES_PATH])
    if res.get("status") != 200 or "json" not in res:
        return CheckResult(name="routes", status="fail",
                           detail=f"{ROUTES_PATH} unavailable in test mode: {res.get('status') or res.get('error')}")
    return compare_routes(route_set(res["json"]), contract_routes(spec))


# ----- immutable files -----
def _git_blob(repo: Path, sha: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def copied_records(clone: Path) -> CheckResult:
    """The replica renders records from its own API: values the explorer created on the original (marked
    rpl-xxxxxx) must not be written into the web or API source."""
    from forge.stages.build.checks import copied_records as scan

    hits = scan(clone)
    if hits:
        return CheckResult(name="copied_records", status="fail",
                           detail=f"{len(hits)} source line(s) hold records copied from the original: "
                                  + "; ".join(hits[:5]))
    return CheckResult(name="copied_records", status="pass",
                       detail="no records copied from the original in web/src or api/src")


# Written by Deliver after a verification (README with the report, the report itself, the local port), so a
# delivered repo can be verified again.
DELIVERY_FILES = frozenset({"README.md", "verification.json", ".env"})


def immutable_hashes(clone: Path, manifest: CodegenManifest, *, sha: str | None = None,
                     overwritten: frozenset[str] = frozenset()) -> CheckResult:
    """Every immutable file must hash to the manifest's value. Files the verify stage itself overwrote
    (its overlay) are checked in the commit instead of the working tree; files Deliver owns are skipped."""
    bad = []
    root = clone.resolve()
    entries = [e for e in manifest.immutable if e.path not in DELIVERY_FILES]
    for entry in entries:
        rel = Path(entry.path)
        if rel.is_absolute() or ".." in rel.parts:
            bad.append(f"{entry.path}: path escapes the repo")
            continue
        if entry.path in overwritten and sha:
            data = _git_blob(clone, sha, entry.path)
        else:
            path = root / rel
            if path.is_symlink():
                bad.append(f"{entry.path}: replaced by a symlink")
                continue
            data = path.read_bytes() if path.is_file() else None
        if data is None:
            bad.append(f"{entry.path}: missing")
        elif hashlib.sha256(data).hexdigest() != entry.sha256:
            bad.append(f"{entry.path}: modified")
    if bad:
        return CheckResult(name="immutable_hashes", status="fail", detail="; ".join(bad[:20]))
    return CheckResult(name="immutable_hashes", status="pass",
                       detail=f"{len(entries)} immutable files unchanged")


# ----- regenerate and diff -----
def regenerate_and_diff(clone: Path, spec: AppSpecDraft) -> CheckResult:
    """Generated (non-editable) files must equal a fresh render of the spec."""
    try:
        from forge.stages.generate.codegen import render_check
    except ImportError:
        return CheckResult(name="regenerate_and_diff", status="skip", detail="codegen is not available")
    try:
        differs = render_check(spec, clone)
    except Exception as err:  # noqa: BLE001 - a render failure is a failed check, not a crash
        return CheckResult(name="regenerate_and_diff", status="fail", detail=f"render failed: {err}"[:500])
    if differs:
        return CheckResult(name="regenerate_and_diff", status="fail",
                           detail="differ from a fresh render: " + ", ".join(differs[:10]))
    return CheckResult(name="regenerate_and_diff", status="pass", detail="generated files match a fresh render")


# ----- restart persistence -----
def list_urls(spec: AppSpecDraft, top_level: dict[str, Any] | None = None, *, per_param: int = 3) -> list[str]:
    """URLs of every list operation. Nested lists take ids from the parent entity's top-level list."""
    lists = [op for op in spec.operations if op.kind == "list" and op.method.upper() == "GET"]
    urls = [APP_URL + op.path for op in lists if not op.path_params]
    if top_level is None:
        return urls
    ids_by_entity: dict[str, list[str]] = {}
    for op in lists:
        if op.path_params or (APP_URL + op.path) not in top_level:
            continue
        body = top_level[APP_URL + op.path]
        items = body.get(op.list_key) if isinstance(body, dict) and op.list_key else body
        ids = [str(i["id"]) for i in items if isinstance(i, dict) and "id" in i] if isinstance(items, list) else []
        ids_by_entity[op.entity] = ids[:per_param]
    for op in lists:
        if not op.path_params:
            continue
        choices = [ids_by_entity.get(p.entity, []) for p in op.path_params]
        for combo in product(*choices):
            path = op.path
            for param, value in zip(op.path_params, combo, strict=True):
                path = path.replace("{" + param.name + "}", value).replace(":" + param.name, value)
            urls.append(APP_URL + path)
    return urls


def snapshot(project: str, files: list[Path], spec: AppSpecDraft) -> dict[str, Any]:
    first = {r["url"]: r.get("json") for r in fetch(project, files, list_urls(spec)) if r.get("status") == 200}
    nested = [u for u in list_urls(spec, first) if u not in first]
    rest = {r["url"]: (r.get("json") if r.get("status") == 200 else {"status": r.get("status")})
            for r in fetch(project, files, nested)}
    return {**first, **rest}


def _rows(snap: dict[str, Any]) -> int:
    total = 0
    for body in snap.values():
        if isinstance(body, list):
            total += len(body)
        elif isinstance(body, dict):
            total += sum(len(v) for v in body.values() if isinstance(v, list))
    return total


def restart_persistence(project: str, files: list[Path], spec: AppSpecDraft) -> CheckResult:
    if not list_urls(spec):
        return CheckResult(name="restart_persistence", status="skip", detail="the spec has no top-level list operation")
    before = snapshot(project, files, spec)
    if not before:
        return CheckResult(name="restart_persistence", status="fail", detail="list operations did not answer 200")
    docker.compose(project, files, ["restart", "-t", "10", "db", "app"], timeout=300)
    up = health(project, files, timeout_s=120)
    if up.status != "pass":
        return CheckResult(name="restart_persistence", status="fail", detail=f"after restart: {up.detail}")
    after = snapshot(project, files, spec)
    if after != before:
        changed = sorted(u for u in set(before) | set(after) if before.get(u) != after.get(u))
        return CheckResult(name="restart_persistence", status="fail",
                           detail="lists changed across a restart: " + ", ".join(changed[:5]))
    rows = _rows(before)
    if rows == 0:
        return CheckResult(name="restart_persistence", status="skip", detail="no rows to compare")
    return CheckResult(name="restart_persistence", status="pass",
                       detail=f"{len(before)} list responses ({rows} rows) identical after restarting db and app")


# ----- egress -----
def egress(project: str, files: list[Path], targets: tuple[str, ...] | list[str] = DEFAULT_EGRESS_TARGETS
           ) -> tuple[CheckResult, list[str]]:
    """Returns the check and the targets that were reachable (they must all be unreachable)."""
    args = ["egress", "--timeout", "4"]
    for target in targets:
        args += ["--target", target]
    _, data = _probe(project, files, args, timeout=120)
    if not isinstance(data, list):
        return CheckResult(name="egress_blocked", status="fail", detail=f"probe failed: {data}"), list(targets)
    reachable = [d["target"] for d in data if d.get("reachable")]
    if reachable:
        detail = "reachable from inside: " + ", ".join(reachable)
        return CheckResult(name="egress_blocked", status="fail", detail=detail), reachable
    detail = "unreachable from inside: " + ", ".join(targets)
    return CheckResult(name="egress_blocked", status="pass", detail=detail), []


# ----- request audit -----
def request_audit(results: list[ScenarioResult]) -> CheckResult:
    hits = sorted({url for r in results for url in r.offorigin_requests})
    if hits:
        return CheckResult(name="request_audit", status="fail",
                           detail=f"{len(hits)} request(s) left the replica: " + ", ".join(hits[:5]))
    return CheckResult(name="request_audit", status="pass", detail=f"{len(results)} runs stayed on the replica origin")
