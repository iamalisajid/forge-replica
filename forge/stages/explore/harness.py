"""The explore harness: a real browser the agent drives through three tools, recording everything.

The agent only chooses actions. The harness enforces the origin allowlist and the write policy, injects
nonces into free-text fills, ties every request to the step that caused it, keeps a write-ahead ledger of
records it created on the original, and saves snapshots and screenshots as evidence. Tool calls are
serialised with a lock because the SDK runs parallel tool calls concurrently.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from playwright.async_api import Browser, BrowserContext, Page, Request, Response, async_playwright

from forge.core.store import STATIC_TYPES, RunStore, first_party, looks_like_write, origin_of, same_site, site_of
from forge.models.base import ArtifactHeader, NameValue
from forge.models.trace import CreatedRecord, Exchange, Step, Trace
from forge.stages.explore import locators
from forge.stages.extract.redact import Redactor

STYLE_DIGEST_JS = """() => {
  const pick = (el) => { const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
    return {tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), label: el.getAttribute('aria-label'),
      text: (el.innerText || '').trim().slice(0, 40), box: [r.x, r.y, r.width, r.height].map(Math.round),
      font: cs.fontFamily, size: cs.fontSize, weight: cs.fontWeight, color: cs.color, bg: cs.backgroundColor,
      radius: cs.borderRadius, border: cs.border, padding: cs.padding, gap: cs.gap, shadow: cs.boxShadow,
      lineHeight: cs.lineHeight}; };
  const sel = 'body, header, nav, aside, main, h1, h2, h3, button, a, input, textarea, select, li, [role=listitem], ' +
              '[role=row], [role=dialog], [role=checkbox], [role=navigation], [role=main], [role=textbox]';
  const els = [...document.querySelectorAll(sel)].filter(e => { const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0; }).slice(0, 160);
  const vars = {};
  for (const sheet of [...document.styleSheets]) { try { for (const rule of sheet.cssRules) {
    if ([':root', 'html', 'body', '.theme_light', '[data-theme]'].includes(rule.selectorText)) {
      for (const name of rule.style) if (name.startsWith('--')) vars[name] = rule.style.getPropertyValue(name).trim(); } }
  } catch (e) {} }
  return {viewport: [innerWidth, innerHeight], css_variables: Object.fromEntries(Object.entries(vars).slice(0, 400)),
          elements: els.map(pick)};
}"""
DOM_JS = """() => { const c = document.body.cloneNode(true);
  c.querySelectorAll('script,noscript,iframe,template,link').forEach(n => n.remove());
  c.querySelectorAll('*').forEach(n => { for (const a of [...n.attributes]) if (a.name.startsWith('on')) n.removeAttribute(a.name); });
  return c.outerHTML.slice(0, 400000); }"""

TEXT_INPUT_TYPES = {"text", "search", "textarea", "", "url"}
# A search query is a lookup, not a new record: typing it unmarked lets it match what the explorer created.
SEARCH_INPUT_JS = """e => {
  const words = /search|find|filter|query|lookup/i;
  const attrs = [e.type, e.getAttribute('role'), e.getAttribute('aria-label'), e.getAttribute('placeholder'),
                 e.getAttribute('name'), e.id].filter(Boolean).join(' ');
  if (e.type === 'search' || e.getAttribute('role') === 'searchbox' || words.test(attrs)) return true;
  const box = e.closest('[role=search], [role=dialog], [role=combobox], form');
  return !!box && words.test([box.getAttribute('role'), box.getAttribute('aria-label')].filter(Boolean).join(' '));
}"""
BODY_LIMIT = 200_000
ACTIONS = ("click", "fill", "select", "check", "uncheck", "press")


class Harness:
    def __init__(self, store: RunStore, *, url: str, mutations: str, storage_state: dict | None = None,
                 extra_origins: list[str] | None = None, snapshot_chars: int = 40_000, headless: bool | None = None):
        self.store = store
        self.url = url
        self.origin = origin_of(url)
        self.allowed = {self.origin, *(extra_origins or [])}
        self.site = site_of(url)
        self.mutations = mutations
        self.storage_state = storage_state
        self.snapshot_chars = snapshot_chars
        import os

        self.headless = (os.environ.get("FORGE_HEADED") != "1") if headless is None else headless
        self.lock = asyncio.Lock()
        self.steps: list[Step] = []
        self.exchanges: list[Exchange] = []
        self.created: list[CreatedRecord] = []
        self.blocked: list[str] = []
        self.redactor = Redactor.from_storage_state(storage_state)
        self._pending: set[asyncio.Task] = set()
        self._current_step: str | None = None
        self._step_nonce: str | None = None
        self._nonces: list[str] = []          # every nonce typed this run (a form's POST happens on a later step)
        self._snapshot_pages: list[str] = []
        self._styled_routes: set[str] = set()
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        from forge.core.config import browser_channel

        self.browser = await self._pw.chromium.launch(headless=self.headless, channel=browser_channel())
        self.context = await self.browser.new_context(
            locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 900},
            service_workers="block", accept_downloads=False, storage_state=self.storage_state,
            record_har_path=str(self.store.dir / "evidence" / "session.har"), record_har_content="embed",
        )
        await self.context.route("**/*", self._route)
        self.page = await self.context.new_page()
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

    async def close(self) -> None:
        await self._drain()
        if self.context:
            await self.context.close()  # writes the HAR
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    # ---------- network policy and capture ----------
    async def _route(self, route) -> None:
        request: Request = route.request
        url = request.url
        if url.startswith(("data:", "blob:", "about:")):
            return await route.continue_()
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in self.allowed and not first_party(url, self.site):
            # Static assets (CDN scripts, styles, fonts, images) load from anywhere, read-only, like any browser.
            # Data and tracking traffic (xhr/fetch/beacons/websockets) to third parties stays blocked.
            if request.method == "GET" and request.resource_type in STATIC_TYPES:
                return await route.continue_()
            self.blocked.append(f"origin {request.resource_type} {url[:120]}")
            return await route.abort("blockedbyclient")
        if self.mutations != "test-account" and looks_like_write(request.method, request.post_data):
            self.blocked.append(f"write {request.method} {urlparse(url).path}")
            return await route.abort("blockedbyclient")
        await route.continue_()

    def _on_request_failed(self, request: Request) -> None:
        if request.failure and "blockedbyclient" not in (request.failure or ""):
            self.blocked.append(f"failed {request.method} {urlparse(request.url).path}: {request.failure}")

    def _on_response(self, response: Response) -> None:
        task = asyncio.ensure_future(self._capture(response, self._current_step, self._step_nonce))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _capture(self, response: Response, step_id: str | None, nonce: str | None) -> None:
        request = response.request
        if request.resource_type not in ("xhr", "fetch", "document"):
            return
        url = urlparse(request.url)
        if not same_site(request.url, self.site):
            return
        req_ct = (await request.all_headers()).get("content-type")
        resp_headers = await response.all_headers()
        resp_ct = resp_headers.get("content-type")
        body = None
        if request.resource_type != "document" and response.status != 204:
            try:
                body = (await response.body()).decode("utf-8", "replace")[:BODY_LIMIT]
            except Exception:  # noqa: BLE001 - bodies of redirects/aborted responses are unavailable
                body = None
        req_body = request.post_data
        exchange = Exchange(
            id=f"x{len(self.exchanges) + 1:04d}", step_id=step_id,
            correlated_by=self._correlation(step_id, nonce, request.url, req_body),
            method=request.method, url=self.redactor.text(request.url), path=url.path,
            query=[NameValue(name=k, value=self.redactor.text(v)) for k, v in parse_qsl(url.query)],
            resource_type=request.resource_type, status=response.status,
            request_content_type=req_ct, request_body=self.redactor.text(req_body) if req_body else None,
            response_content_type=resp_ct, response_body=self.redactor.text(body) if body else None,
            started_at=time.time(),
        )
        self.exchanges.append(exchange)
        self._ledger(exchange)

    def _correlation(self, step_id, nonce, url, body) -> str:
        if step_id is None:
            return "none"
        if any((body and n in body) or n in url for n in ([nonce] if nonce else []) + self._nonces):
            return "nonce"
        path = urlparse(url).path
        if any(re.search(rf"/{re.escape(c.id_value)}(/|$)", path) for c in self.created):
            return "entity_id"
        return "time_window"

    def _ledger(self, ex: Exchange) -> None:
        """Write-ahead ledger: remember every record we created so cleanup can delete exactly those."""
        if ex.method == "POST" and ex.status in (200, 201) and ex.response_body:
            try:
                data = json.loads(ex.response_body)
            except ValueError:
                return
            if isinstance(data, dict) and "id" in data and not re.search(r"/\d+/[a-z_-]+$", ex.path):
                self.created.append(CreatedRecord(exchange_id=ex.id, path=ex.path, id_value=str(data["id"]),
                                                  entity_hint=ex.path.rstrip("/").split("/")[-1]))
        if ex.method == "DELETE" and ex.status in (200, 202, 204):
            for c in self.created:
                if ex.path.rstrip("/").endswith(f"/{c.id_value}"):
                    c.deleted = True

    async def _drain(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001 - long-polling pages never go idle; a short wait is enough
            pass
        await asyncio.sleep(0.25)
        await self._drain()

    # ---------- evidence ----------
    async def _snapshot(self) -> str:
        return await self.page.aria_snapshot(mode="ai")

    def _paginate(self, snapshot: str) -> list[str]:
        if len(snapshot) <= self.snapshot_chars:
            return [snapshot]
        pages, current = [], []
        size = 0
        for line in snapshot.splitlines(keepends=True):
            if size + len(line) > self.snapshot_chars and current:
                pages.append("".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line)
        pages.append("".join(current))
        return pages

    def _render_snapshot(self, page_no: int = 1) -> str:
        total = len(self._snapshot_pages)
        body = self._snapshot_pages[page_no - 1]
        head = f"URL: {self.page.url}\nSnapshot page {page_no}/{total}"
        if total > 1:
            head += " (call observe with page=N for the rest; refs stay valid until the next action)"
        return head + "\n" + body

    # ---------- tools ----------
    async def observe(self, page_no: int = 1, screenshot: bool = False) -> tuple[str, bytes | None]:
        async with self.lock:
            if page_no <= 1 or not self._snapshot_pages:
                await self._settle()
                self._snapshot_pages = self._paginate(await self._snapshot())
                page_no = 1
            page_no = min(page_no, len(self._snapshot_pages))
            png = await self.page.screenshot(type="png") if screenshot else None
            return self._render_snapshot(page_no), png

    async def navigate(self, url: str) -> str:
        async with self.lock:
            target = url if url.startswith("http") else self.origin + ("" if url.startswith("/") else "/") + url
            if origin_of(target) != self.origin:
                return f"Refused: {url} is outside {self.origin}. Stay on the app being explored."
            step = await self._begin("navigate", None, None, None, None)
            try:
                await self.page.goto(target, wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:  # noqa: BLE001
                step.error = f"navigation failed: {exc}"
            return await self._finish(step)

    async def act(self, ref: str, action: str, value: str | None = None) -> str:
        async with self.lock:
            if action not in ACTIONS:
                return f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."
            snapshot = "".join(self._snapshot_pages) if self._snapshot_pages else await self._snapshot()
            locator = self.page.locator(f"aria-ref={ref}")
            try:
                if await locator.count() != 1:
                    return f"Ref {ref} is not on the page any more. Call observe and use a current ref."
            except Exception:  # noqa: BLE001
                return f"Ref {ref} is not valid. Call observe and use a current ref."
            target, portable = await locators.portable_target(self.page, snapshot, ref)
            nonce = None
            if action == "fill" and value:
                kind = await locator.evaluate("e => (e.tagName === 'TEXTAREA' ? 'textarea' : (e.type || ''))")
                if kind in TEXT_INPUT_TYPES and not await locator.evaluate(SEARCH_INPUT_JS):
                    nonce = f"rpl-{secrets.token_hex(3)}"
                    value = f"{value} {nonce}"
                    self._nonces.append(nonce)
            step = await self._begin(action, ref, target, portable, value, nonce=nonce)
            try:
                if action == "click":
                    await locator.click(timeout=6000)
                elif action == "fill":
                    try:
                        await locator.fill(value or "", timeout=3000)
                    except Exception:  # noqa: BLE001 - rich inputs that refuse fill still take typed keys
                        await locator.click(timeout=3000)
                        await self.page.keyboard.press("ControlOrMeta+A")
                        await self.page.keyboard.type(value or "", delay=15)
                elif action == "select":
                    try:
                        await locator.select_option(label=value, timeout=6000)
                    except Exception:  # noqa: BLE001 - fall back to option value
                        await locator.select_option(value=value, timeout=6000)
                elif action == "check":
                    await locator.check(timeout=6000)
                elif action == "uncheck":
                    await locator.uncheck(timeout=6000)
                elif action == "press":
                    await locator.press(value or "Enter", timeout=6000)
            except Exception as exc:  # noqa: BLE001 - report back to the agent instead of crashing the run
                step.error = f"{action} failed: {str(exc).splitlines()[0]}"
            return await self._finish(step)

    async def _begin(self, action, ref, target, portable, value, *, nonce=None) -> Step:
        await self._drain()
        step_id = f"s{len(self.steps) + 1:03d}"
        before = await self._snapshot()
        step = Step(id=step_id, action=action, ref=ref, target=target, portable=bool(portable), value=value,
                    nonce=nonce, url_before=self.page.url, url_after=self.page.url,
                    snapshot_before=self.store.put_blob(before.encode(), "txt"), snapshot_after=None,
                    screenshot=None, ts=time.time())
        self._current_step, self._step_nonce = step_id, nonce
        self.steps.append(step)
        return step

    async def _finish(self, step: Step) -> str:
        await self._settle()
        snapshot = await self._snapshot()
        self._snapshot_pages = self._paginate(snapshot)
        step.url_after = self.page.url
        step.snapshot_after = self.store.put_blob(snapshot.encode(), "txt")
        step.screenshot = self.store.put_blob(await self.page.screenshot(type="png"), "png")
        await self._design_evidence(step)
        step.exchange_ids = [x.id for x in self.exchanges if x.step_id == step.id]
        self._step_nonce = None   # keep the step id: late responses still belong to this step
        calls = [f"{x.method} {x.path} -> {x.status}" for x in self.exchanges
                 if x.step_id == step.id and x.resource_type in ("xhr", "fetch")]
        blocked = [b for b in self.blocked[-5:]]
        lines = [f"{step.id} {step.action}" + (f" {step.ref}" if step.ref else "") +
                 (f" value={step.value!r}" if step.value else "") + (f" ERROR: {step.error}" if step.error else " ok")]
        if calls:
            lines.append("API calls: " + "; ".join(calls))
        if blocked:
            lines.append("Blocked by policy: " + "; ".join(blocked))
        return "\n".join(lines) + "\n" + self._render_snapshot(1)

    async def _design_evidence(self, step: Step) -> None:
        """Keep the original's DOM and computed styles for the builder: on navigation and on each route's first visit."""
        from forge.stages.extract.rest import template_of

        route, _ = template_of(urlparse(step.url_after).path or "/")
        if step.action != "navigate" and route in self._styled_routes:
            return
        self._styled_routes.add(route)
        try:
            styles = await self.page.evaluate(STYLE_DIGEST_JS)
            dom = await self.page.evaluate(DOM_JS)
        except Exception:  # noqa: BLE001 - design evidence is best effort
            return
        step.styles = self.store.put_blob(json.dumps(styles).encode(), "json")
        step.dom = self.store.put_blob((self.redactor.text(dom) or "").encode(), "html")

    # ---------- cleanup on the original ----------
    async def cleanup(self) -> list[str]:
        """Delete exactly the records this run created (newest first), confirming each is gone."""
        report = []
        if self.mutations != "test-account":
            return report
        for record in reversed(self.created):
            if record.deleted:
                continue
            item = f"{self.origin}{record.path.rstrip('/')}/{record.id_value}"
            try:
                resp = await self.context.request.delete(item, headers={"Origin": self.origin})
                gone = await self.context.request.get(item)
                record.deleted = resp.status in (200, 202, 204) and gone.status == 404
                report.append(f"{'deleted' if record.deleted else 'left'} {record.path}/{record.id_value} "
                              f"(delete {resp.status}, get {gone.status})")
            except Exception as exc:  # noqa: BLE001
                report.append(f"left {record.path}/{record.id_value}: {exc}")
        return report

    def trace(self, header: ArtifactHeader, pass_name: str, feature: str | None, notes: list[str]) -> Trace:
        return Trace(header=header, target_url=self.url, origin=self.origin, pass_name=pass_name, feature=feature,
                     steps=self.steps, exchanges=self.exchanges, created=self.created,
                     notes=notes + [f"blocked: {b}" for b in self.blocked])
