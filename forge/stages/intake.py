"""Intake: `forge doctor` (machine readiness, incl. an egress probe) and preflight (is this app in scope?).

Preflight is code only — no model calls — so an unsupported app costs nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from forge.core import docker
from forge.core.config import api_key
from forge.core.store import RunStore, looks_like_write, origin_of, same_site, site_of
from forge.models.base import ArtifactHeader
from forge.models.run import Feasibility, ProtocolShare
from forge.core.store import is_sync_read

PULL_IMAGES = ["postgres:16-alpine", "node:22-alpine", "mcr.microsoft.com/playwright/python:v1.63.0-noble"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def doctor(fix: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("python", sys.version_info >= (3, 12), sys.version.split()[0]))
    has_docker = shutil.which("docker") is not None
    checks.append(Check("docker cli", has_docker, shutil.which("docker") or "not found"))
    if has_docker:
        info = docker.run(["info", "--format", "{{.ServerVersion}}"], check=False, timeout=30)
        checks.append(Check("docker daemon", info.returncode == 0, info.stdout.strip() or info.stderr.strip()[:120]))
        compose = docker.run(["compose", "version", "--short"], check=False, timeout=30)
        checks.append(Check("docker compose", compose.returncode == 0, compose.stdout.strip()))
        for image in PULL_IMAGES:
            present = docker.image_exists(image)
            if not present and fix:
                present = docker.run(["pull", image], check=False, timeout=1800).returncode == 0
            checks.append(Check(f"image {image}", present, "present" if present else "missing (run doctor --fix)"))
        for label, ensure, tag in (("base image", docker.ensure_base_image, docker.base_image_tag),
                                   ("runner image", docker.ensure_runner_image, docker.runner_image_tag)):
            try:
                built = ensure() if fix else (tag() if docker.image_exists(tag()) else None)
                checks.append(Check(label, bool(built), built or "missing"))
            except Exception as exc:  # noqa: BLE001
                checks.append(Check(label, False, str(exc)[:200]))
        checks.append(egress_probe())
    checks.append(_browser_check())
    checks.append(_key_check())
    return checks


def _browser_check() -> Check:
    try:
        proc = subprocess.run([sys.executable, "-c", (
            "import asyncio\nfrom playwright.async_api import async_playwright\n"
            "async def m():\n    async with async_playwright() as p:\n        b = await p.chromium.launch(); await b.close()\n"
            "asyncio.run(m())")], capture_output=True, text=True, timeout=60)
        ok = proc.returncode == 0
        return Check("playwright chromium", ok, "launches" if ok else "run: uv run playwright install chromium")
    except Exception as exc:  # noqa: BLE001
        return Check("playwright chromium", False, str(exc)[:200])


def _key_check() -> Check:
    key = api_key()
    if not key:
        return Check("anthropic api key", False, "set ANTHROPIC_API_KEY or add it to ~/.forge.env")
    try:
        resp = httpx.get("https://api.anthropic.com/v1/models", timeout=20,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        return Check("anthropic api key", resp.status_code == 200, f"HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        return Check("anthropic api key", False, str(exc)[:200])


def egress_probe() -> Check:
    """Prove that a container on the verify network cannot reach the host or the internet."""
    compose_yaml = """services:
  probe:
    image: node:22-alpine
    command: ["sh", "-c", "wget -q -T 3 -O - http://host.docker.internal:4001 >/dev/null 2>&1 && echo HOST_REACHABLE; wget -q -T 3 -O - https://example.com >/dev/null 2>&1 && echo NET_REACHABLE; echo DONE"]
    networks: [edge]
networks:
  edge: { internal: true }
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "compose.yaml"
        path.write_text(compose_yaml)
        proc = docker.compose("forge-doctor-egress", [path], ["run", "--rm", "probe"], check=False, timeout=120)
        docker.compose("forge-doctor-egress", [path], ["down", "-v"], check=False, timeout=60)
    out = proc.stdout
    ok = "DONE" in out and "REACHABLE" not in out
    return Check("egress blocked on verify network", ok, "host and internet unreachable" if ok else out.strip()[-200:])


async def preflight(store: RunStore, url: str, *, storage_state: dict | None, run_id: str) -> Feasibility:
    from playwright.async_api import async_playwright

    origin = origin_of(url)
    responses: list[tuple[str, str, str, str]] = []   # method, path, content-type, body-head
    websockets = 0
    reachable, challenge, login_wall = False, False, False
    final_url, canvas_ratio, interactive = url, 0.0, 0
    async with async_playwright() as p:
        from forge.core.config import browser_channel

        browser = await p.chromium.launch(channel=browser_channel())
        context = await browser.new_context(storage_state=storage_state, locale="en-US", timezone_id="UTC",
                                            viewport={"width": 1440, "height": 900}, service_workers="block")

        async def route(r):
            if looks_like_write(r.request.method, r.request.post_data):
                return await r.abort("blockedbyclient")
            await r.continue_()

        await context.route("**/*", route)
        page = await context.new_page()

        async def on_response(resp):
            req = resp.request
            if req.resource_type in ("xhr", "fetch") and same_site(req.url, site_of(origin)):
                ct = (await resp.all_headers()).get("content-type", "")
                responses.append((req.method, urlparse(req.url).path, ct, (req.post_data or "")[:2000]))

        page.on("response", on_response)
        page.on("websocket", lambda ws: None)
        try:
            first = await page.goto(url, wait_until="networkidle", timeout=30_000)
            reachable = first is not None and first.status < 500
            headers = await first.all_headers() if first else {}
            title = (await page.title()).lower()
            challenge = "cf-mitigated" in headers or any(t in title for t in ("just a moment", "attention required",
                                                                              "verify you are human"))
            final_url = page.url
            stats = await page.evaluate("""() => {
              const vw = innerWidth * innerHeight;
              const canvas = [...document.querySelectorAll('canvas')].reduce((a, c) => { const r = c.getBoundingClientRect(); return a + r.width * r.height; }, 0);
              const inter = document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link]').length;
              const pw = document.querySelector('input[type=password]') !== null;
              return {canvas: canvas / vw, inter, pw};
            }""")
            canvas_ratio, interactive, login_wall = stats["canvas"], stats["inter"], stats["pw"]
            links = await page.evaluate("""(origin) => [...new Set([...document.querySelectorAll('a[href]')]
              .map(a => a.href).filter(h => h.startsWith(origin) && !h.includes('#')))].slice(0, 20)""", origin)
            for link in links:
                try:
                    await page.goto(link, wait_until="networkidle", timeout=15_000)
                except Exception:  # noqa: BLE001 - a slow page is not a failed preflight
                    continue
        except Exception as exc:  # noqa: BLE001
            final_url = f"error: {exc}"
        await browser.close()

    total = max(1, len(responses))
    kinds = {"rest_json": 0, "graphql": 0, "rpc": 0, "forms": 0, "other": 0}
    for method, path, ct, body in responses:
        if "graphql" in path or '"query"' in body or '"operationName"' in body:
            kinds["graphql"] += 1
        elif '"commands"' in body or "commands=" in body or is_sync_read(body):
            kinds["rpc"] += 1
        elif "json" in ct:
            kinds["rest_json"] += 1
        elif "form" in ct:
            kinds["forms"] += 1
        else:
            kinds["other"] += 1
    protocols = [ProtocolShare(protocol=k, share=round(v / total, 2)) for k, v in kinds.items() if v]
    stop, reason = False, None
    if not reachable:
        stop, reason = True, f"not reachable: {final_url}"
    elif challenge:
        stop, reason = True, "bot challenge page; the harness never evades it"
    elif canvas_ratio > 0.5 and interactive < 10:
        stop, reason = True, "canvas/WebGL-rendered app: no accessibility tree to observe"
    feasibility = Feasibility(
        header=ArtifactHeader(schema_id="forge.feasibility/1", run_id=run_id, stage="intake"),
        reachable=reachable, final_url=final_url, login_wall=login_wall, challenge=challenge,
        canvas_ratio=round(canvas_ratio, 3), interactive_nodes=interactive, protocols=protocols, features=[],
        stop=stop, reason=reason)
    store.write("feasibility", feasibility)
    return feasibility
