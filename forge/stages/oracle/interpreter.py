"""Scenario interpreter: runs the flat Step DSL with Playwright on the source app or on the replica.

It runs on the host (validation on the source) and inside the runner image (checks on the replica's
internal network), so it imports only the standard library, pydantic, playwright and forge.models.

One run is fresh bindings plus a fresh browser context:

1. Setup scenarios (if any) run first in the same context and share the binding namespace.
2. The leading ``goto`` steps load the first page. Then every expectation that can already be
   evaluated is checked once, without waiting (the non-vacuity probe). An expectation that holds
   before any action proves nothing, so its index goes into ``vacuous_assertions``.
3. The remaining steps run in order with auto-waiting, strict locators and bounded expectations.
4. Effects confirm the change through the API of the app under test.

Every request is audited. A non-GET request to the app's origin marks ``first_write_step``, and
requests to origins outside ``allowed_origins`` are recorded but never blocked (Verify decides).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Locator,
    Page,
    Request,
    Response,
    Route,
    WebSocket,
    async_playwright,
    expect,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from forge.models.appspec import OpWire
from forge.models.base import NameValue
from forge.models.run import RunnerRequest
from forge.models.scenario import Bind, Scenario, ScenarioResult, ScenarioStep, Target

T = TypeVar("T")

ACTION_KINDS = frozenset({"click", "fill", "select", "check", "uncheck", "press"})
EXPECT_KINDS = frozenset({"expect_visible", "expect_hidden", "expect_text", "expect_value", "expect_url"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_AUDIT_ENTRIES = 200

# Connection-level failures that mean "the environment is broken", not "the app behaved differently".
_NET_ERRORS = (
    "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_FAILED",
    "ERR_CONNECTION_TIMED_OUT", "ERR_EMPTY_RESPONSE", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE", "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED", "ERR_SSL_PROTOCOL_ERROR", "ERR_CERT_",
    "ERR_PROXY_", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_SOCKET_NOT_CONNECTED",
)
_CLOSED_ERRORS = ("has been closed", "Target closed", "browser has disconnected", "Browser closed")
_CHALLENGE_TITLES = re.compile(
    r"^(just a moment\.*|attention required!? \| cloudflare|verifying you are human.*|ddos-guard|"
    r"pardon our interruption|please verify you are a human|human verification)$", re.I)

_REF = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_PARAM = re.compile(r"\{([A-Za-z_]\w*)\}|:([A-Za-z_]\w*)")

_VALUES_JS = """(el) => {
  const out = [];
  if (el instanceof HTMLSelectElement) {
    out.push(el.value);
    for (const o of el.selectedOptions) out.push(o.value, o.label, (o.textContent || '').trim());
  } else if ('value' in el && typeof el.value === 'string') {
    out.push(el.value);
  } else {
    for (const a of ['aria-valuetext', 'aria-valuenow', 'value']) {
      const v = el.getAttribute(a);
      if (v !== null) out.push(v);
    }
    out.push((el.textContent || '').trim());
  }
  return out;
}"""
_OPTIONS_JS = """(el) => el instanceof HTMLSelectElement
  ? Array.from(el.options).map(o => [o.label, o.value, (o.textContent || '').trim()]) : null"""


@dataclass(frozen=True)
class Timeouts:
    """Bounds for one step. Expectations retry until ``expect_ms``; actions auto-wait up to ``action_ms``."""

    expect_ms: int = 7_000
    action_ms: int = 7_000
    navigation_ms: int = 30_000
    effect_ms: int = 5_000          # retry window for API effect checks
    request_ms: int = 10_000        # one API request
    settle_min_ms: int = 75         # after an action, wait at least this long for requests it triggers
    settle_quiet_ms: int = 100      # ... then until no app request is in flight for this long
    settle_cap_ms: int = 2_000      # ... but never longer than this
    probe_idle_ms: int = 5_000      # wait for network idle before the non-vacuity probe
    run_s: int = 300                # hard cap for one run


class StepFailure(Exception):
    def __init__(self, kind: str, message: str, step: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.step = step


class UnresolvedBinding(Exception):
    pass


# ---------------------------------------------------------------------------------------------------
# Pure helpers (bindings, templates, origins)
# ---------------------------------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:24] or "v"


def _nonce(seed: str | None, salt: str, name: str) -> str:
    if seed is None:
        return secrets.token_hex(3)
    return hashlib.sha256(f"{seed}|{salt}|{name}".encode()).hexdigest()[:6]


def generate_bindings(binds: list[Bind], fixtures: dict[str, str], *, seed: str | None = None,
                      salt: str = "") -> dict[str, str]:
    """Fresh values for one run. ``uniq_*`` kinds get a new nonce every run (deterministic with ``seed``)."""
    values: dict[str, str] = {}
    for bind in binds:
        if bind.name in values:
            continue  # first declaration wins (setup scenarios share the namespace)
        if bind.kind == "literal":
            values[bind.name] = bind.value or ""
        elif bind.kind == "fixture":
            key = bind.value or bind.name
            if key in fixtures:
                values[bind.name] = fixtures[key]
            elif bind.name in fixtures:
                values[bind.name] = fixtures[bind.name]
            else:
                raise UnresolvedBinding(f"fixture {key!r} was not provided")
        else:
            nonce = _nonce(seed, salt, bind.name)
            if bind.kind == "uniq_text":
                values[bind.name] = f"rpl-{nonce}-{_slug(bind.name)}"
            elif bind.kind == "uniq_email":
                values[bind.name] = f"rpl-{nonce}-{_slug(bind.name)}@example.test"
            else:  # uniq_int
                values[bind.name] = str(int(nonce, 16) % 900_000 + 100_000)
    return values


class Bindings:
    """``{{name}}`` resolves from binds; ``{{captured.name}}`` from values read off API responses."""

    def __init__(self, values: dict[str, str]):
        self.values = values
        self.captured: dict[str, str] = {}

    @staticmethod
    def refs(text: str | None) -> list[str]:
        return _REF.findall(text or "")

    def missing(self, text: str | None) -> list[str]:
        out = []
        for ref in self.refs(text):
            if ref.startswith("captured."):
                if ref[len("captured."):] not in self.captured:
                    out.append(ref)
            elif ref not in self.values:
                out.append(ref)
        return out

    def render(self, text: str) -> str:
        def sub(match: re.Match) -> str:
            ref = match.group(1)
            if ref.startswith("captured."):
                key = ref[len("captured."):]
                if key in self.captured:
                    return self.captured[key]
            elif ref in self.values:
                return self.values[ref]
            raise UnresolvedBinding(ref)

        return _REF.sub(sub, text)


def step_texts(step: ScenarioStep) -> list[str]:
    t = step.target
    texts = [step.value, step.route]
    if t is not None:
        texts += [t.name, t.has_text, t.within_name, t.within_has_text]
    return [x for x in texts if x]


def path_regex(template: str) -> re.Pattern[str]:
    """``/api/projects/{project_id}/issues`` (or ``:project_id``) -> an anchored regex over a URL path."""
    if "://" in template:
        template = urlsplit(template).path or "/"
    parts, pos = [], 0
    for match in _PARAM.finditer(template):
        parts.append(re.escape(template[pos:match.start()]))
        parts.append(r"[^/]+")
        pos = match.end()
    parts.append(re.escape(template[pos:]))
    body = "".join(parts)
    while body.endswith("/"):
        body = body[:-1]
    return re.compile("^" + body + "/?$")


def url_regex(template: str) -> tuple[re.Pattern[str], bool]:
    """Regex for ``expect_url``; the boolean says whether the query string takes part in the match."""
    if "://" in template:
        parts = urlsplit(template)
        template = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    path, _, query = template.partition("?")
    rx = path_regex(path or "/")
    if not query:
        return rx, False
    return re.compile(rx.pattern[:-3] + "/?" + re.escape("?" + query) + "$"), True


def fill_path(template: str, params: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        if name not in params:
            raise UnresolvedBinding(f"path parameter {name!r}")
        return quote(params[name], safe="")

    return _PARAM.sub(sub, template)


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` with default ports dropped; ws(s) maps to http(s). None for data:, blob: ..."""
    try:
        parts = urlsplit(url)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    default = 80 if scheme == "http" else 443
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def _field(item: Any, path: str) -> Any:
    cur = item
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


_MISSING = object()


def _scalar(value: Any) -> str | None:
    if value is _MISSING:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _items(body: Any, list_key: str | None, kind: str) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if list_key and isinstance(body.get(list_key), list):
            return body[list_key]
        if kind == "list":
            lists = [v for v in body.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        return [body]
    return []


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def clean_error(err: BaseException | str, limit: int = 400) -> str:
    """First meaningful lines of a Playwright error, without the call log and the aria snapshot."""
    text = str(err)
    for marker in ("\nCall log:", "\nAria snapshot:", "\n===="):
        text = text.split(marker, 1)[0]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("1)", "2)", "3)"))]
    out = " | ".join(lines[:3])
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _is_strict(err: BaseException) -> bool:
    return "strict mode violation" in str(err)


_ABORT_CODES = (
    ("ECONNREFUSED", "connectionrefused"), ("ECONNRESET", "connectionreset"), ("ENOTFOUND", "namenotresolved"),
    ("EAI_AGAIN", "namenotresolved"), ("ETIMEDOUT", "timedout"), ("Timeout", "timedout"),
    ("EHOSTUNREACH", "addressunreachable"), ("ENETUNREACH", "addressunreachable"),
)


def _abort_code(text: str) -> str:
    """Map a failed ``route.fetch`` to the network error the page would have seen."""
    return next((code for marker, code in _ABORT_CODES if marker in text), "failed")


def _is_closed(err: BaseException) -> bool:
    text = str(err)
    return any(marker in text for marker in _CLOSED_ERRORS)


def _net_error(text: str) -> str | None:
    for code in _NET_ERRORS:
        if code in text:
            match = re.search(r"net::(ERR_[A-Z_]+)", text)
            return match.group(1) if match else code
    return None


def _loosen(t: Target) -> list[Target]:
    """Looser readings of a target, most specific first (see _Run._pick)."""
    out: list[Target] = []

    def add(**changes) -> None:
        cand = t.model_copy(update=changes)
        if cand != t and cand not in out:
            out.append(cand)

    if t.name and t.exact:
        add(exact=False)
    if t.within_role and t.within_name:
        add(within_name=None, within_has_text=t.within_name)
        add(within_name=None, within_has_text=t.within_name, exact=False)
    if t.within_role or t.within_has_text:
        add(within_role=None, within_name=None, within_has_text=None)
        add(within_role=None, within_name=None, within_has_text=None, exact=False)
    return out


def describe(target: Target | None, render: Callable[[str], str] | None = None) -> str:
    if target is None:
        return "page"

    def r(text: str) -> str:
        if render is None:
            return text
        try:
            return render(text)
        except UnresolvedBinding:
            return text

    out = target.role + (f' "{r(target.name)}"' if target.name is not None else "")
    if target.has_text:
        out += f' with text "{r(target.has_text)}"'
    if target.within_role or target.within_has_text:
        scope = target.within_role or "element"
        if target.within_name:
            scope += f' "{r(target.within_name)}"'
        if target.within_has_text:
            scope += f' with text "{r(target.within_has_text)}"'
        out += f" in {scope}"
    return out


def probe_index(steps: list[ScenarioStep]) -> int:
    """Index of the first step after the leading ``goto`` steps: the probe runs just before it."""
    i = 0
    while i < len(steps) and steps[i].kind == "goto":
        i += 1
    return i


# ---------------------------------------------------------------------------------------------------
# One run of one scenario
# ---------------------------------------------------------------------------------------------------
@dataclass
class _Shared:
    browser: Browser
    req: RunnerRequest
    ops: dict[str, OpWire]
    scenarios: dict[str, Scenario]
    storage_state: dict | str | None
    timeouts: Timeouts
    fixtures: dict[str, str]
    app_origin: str
    allowed: frozenset[str]


@dataclass
class _Pending:
    name: str
    method: str
    pattern: re.Pattern[str]
    field: str


class _Run:
    def __init__(self, shared: _Shared, scenario: Scenario, run_no: int):
        self.sh = shared
        self.t = shared.timeouts
        self.scenario = scenario
        self.run_no = run_no
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.generation = 0
        self.phase = "init"
        self.step: int | None = None
        self.navigated = False
        self.first_write_step: int | None = None
        self.offorigin: list[str] = []
        self._offorigin_seen: set[str] = set()
        self.env_problem: str | None = None
        self.inflight: set[Request] = set()
        self.last_request_at = 0.0
        self.pending: list[_Pending] = []
        self.capture_seq: dict[str, int] = {}
        self.request_seq = 0
        self.vacuous: list[int] = []
        self.probed = False
        self.bindings = Bindings({})

    # ----- result -----
    async def execute(self) -> ScenarioResult:
        started = time.monotonic()
        passed, kind, failed_step, message, snapshot = False, "none", None, None, None
        try:
            await asyncio.wait_for(self._execute(), timeout=self.t.run_s)
            passed = True
        except StepFailure as failure:
            kind, failed_step, message = failure.kind, failure.step, failure.message
            if kind in ("assertion", "action") and self.env_problem:
                kind, message = "env", f"{self.env_problem}; then {message}"
            if kind in ("assertion", "action"):
                snapshot = await self._failure_snapshot()
        except TimeoutError:
            kind, failed_step = "env", self.step
            message = f"run exceeded {self.t.run_s}s"
        except PlaywrightError as err:  # unexpected browser-level failure outside a step
            kind, failed_step = ("env" if _is_closed(err) else "action"), self.step
            message = f"browser error: {clean_error(err)}"
        except Exception as err:  # noqa: BLE001 - an interpreter bug must not lose the other results
            kind, failed_step = "env", self.step
            message = f"runner error: {type(err).__name__}: {err}"
        finally:
            await self._close()
        expects = sum(1 for s in self.scenario.steps if s.kind in EXPECT_KINDS)
        return ScenarioResult(
            scenario_id=self.scenario.id, target=self.sh.req.target, run=self.run_no, passed=passed,
            failed_step=None if passed else failed_step, failure_kind="none" if passed else kind,
            message=None if passed else message, vacuous_assertions=list(self.vacuous),
            non_vacuous_assertions=(expects - len(self.vacuous)) if self.probed else 0,
            first_write_step=self.first_write_step,
            captured=[NameValue(name=k, value=v) for k, v in self.bindings.captured.items()],
            offorigin_requests=list(self.offorigin), duration_ms=int((time.monotonic() - started) * 1000),
            failure_snapshot=snapshot)

    async def _failure_snapshot(self, limit: int = 20_000) -> str | None:
        """What the page offered when a step failed (roles and accessible names), so the scenario can be repaired."""
        try:
            if self.page is None or self.page.is_closed():
                return None
            return (await self.page.locator("body").aria_snapshot(timeout=3000))[:limit]
        except Exception:  # noqa: BLE001 - best effort; the failure itself is already recorded
            return None

    async def _execute(self) -> None:
        setups = self._setups()
        self._bind(setups)
        await self._open(self.sh.storage_state)
        for setup in setups:
            await self._run_setup(setup)
        await self._run_main()
        await self._run_effects()

    def _setups(self) -> list[Scenario]:
        out, seen = [], {self.scenario.id}
        for sid in self.scenario.setup:
            if sid in seen:
                raise StepFailure("action", f"setup scenario {sid!r} is repeated or circular")
            if sid not in self.sh.scenarios:
                raise StepFailure("action", f"setup scenario {sid!r} is not in the request")
            seen.add(sid)
            out.append(self.sh.scenarios[sid])
        return out

    def _bind(self, setups: list[Scenario]) -> None:
        everyone = [*setups, self.scenario]
        try:
            values = generate_bindings([b for s in everyone for b in s.binds], self.sh.fixtures,
                                       seed=self.sh.req.seed, salt=f"{self.scenario.id}|{self.run_no}")
        except UnresolvedBinding as err:
            raise StepFailure("env", f"cannot bind values: {err}") from None
        self.bindings = Bindings(values)
        captures = {c.name: c for s in everyone for c in s.captures}
        for cap in captures.values():
            op = self.sh.ops.get(cap.operation)
            if op is None:
                continue
            method, path = self._wire(op)
            self.pending.append(_Pending(cap.name, method.upper(), path_regex(path), cap.field))
        known_caps = {p.name for p in self.pending}
        # Static check: every reference must name a declared bind or a capture with a known operation.
        for scenario in everyone:
            texts = [(i, x) for i, st in enumerate(scenario.steps) for x in step_texts(st)]
            texts += [(len(scenario.steps), x) for e in scenario.effects
                      for x in [e.value, *(p.value for p in e.path_params)]]
            for index, text in texts:
                for ref in Bindings.refs(text):
                    name = ref[len("captured."):] if ref.startswith("captured.") else None
                    ok = (name in known_caps) if name is not None else (ref in values)
                    if not ok:
                        where = index if scenario is self.scenario else None
                        hint = (f"capture {name!r} is not declared or uses an unknown operation"
                                if name is not None else f"{{{{{ref}}}}} is not a declared bind")
                        raise StepFailure("action", f"scenario error in {scenario.id}: {hint}", where)

    def _wire(self, op: OpWire) -> tuple[str, str]:
        if self.sh.req.target == "source":
            return op.source_method, op.source_path
        return op.replica_method, op.replica_path

    # ----- browser plumbing -----
    async def _open(self, storage_state: dict | str | None) -> None:
        self.generation += 1
        gen = self.generation
        ctx = await self.sh.browser.new_context(
            storage_state=storage_state, locale="en-US", timezone_id="UTC", service_workers="block",
            viewport={"width": 1280, "height": 900})
        ctx.set_default_timeout(self.t.action_ms)
        ctx.set_default_navigation_timeout(self.t.navigation_ms)
        ctx.on("request", lambda r: self._on_request(gen, r))
        ctx.on("requestfinished", lambda r: self._on_request_done(gen, r))
        ctx.on("requestfailed", lambda r: self._on_request_failed(gen, r))
        ctx.on("response", lambda r: self._on_response(gen, r))
        ctx.on("page", lambda p: self._wire_page(gen, p))
        captures = self._capture_url_regex()
        if captures is not None:
            # Captures read the response through interception: Chromium never reports the body of a
            # response the page does not read (``fetch`` + ``res.ok`` only), so response events miss it.
            await ctx.route(captures, lambda route: self._capture_route(gen, route))
        self.inflight = set()
        self.ctx = ctx
        self.page = await ctx.new_page()

    def _wire_page(self, gen: int, page: Page) -> None:
        page.on("dialog", lambda d: asyncio.ensure_future(self._accept(d)))
        page.on("websocket", lambda ws: self._on_websocket(gen, ws))

    @staticmethod
    async def _accept(dialog: Dialog) -> None:
        try:
            await dialog.accept()
        except PlaywrightError:
            pass

    async def _close(self) -> None:
        self.generation += 1
        if self.ctx is not None:
            await self._retire(self.ctx)
        self.ctx = self.page = None

    @staticmethod
    async def _retire(ctx: BrowserContext) -> None:
        try:
            await ctx.unroute_all(behavior="ignoreErrors")
            await ctx.close()
        except PlaywrightError:
            pass

    def _audit(self, url: str) -> str | None:
        origin = origin_of(url)
        if origin is None:
            return None
        if origin not in self.sh.allowed and url not in self._offorigin_seen:
            if len(self.offorigin) < MAX_AUDIT_ENTRIES:
                self.offorigin.append(url)
            self._offorigin_seen.add(url)
        return origin

    def _on_request(self, gen: int, request: Request) -> None:
        if gen != self.generation:
            return
        origin = self._audit(request.url)
        if origin != self.sh.app_origin:
            return
        self.inflight.add(request)
        self.last_request_at = time.monotonic()
        if (request.method.upper() not in SAFE_METHODS and self.phase == "main" and self.step is not None
                and self.first_write_step is None):
            self.first_write_step = self.step

    def _on_request_done(self, gen: int, request: Request) -> None:
        if gen == self.generation:
            self.inflight.discard(request)

    def _on_request_failed(self, gen: int, request: Request) -> None:
        if gen != self.generation:
            return
        self.inflight.discard(request)
        if origin_of(request.url) != self.sh.app_origin:
            return
        code = _net_error(request.failure or "")
        if code:
            self._flag_env(f"network error {code} on {request.method} {urlsplit(request.url).path}")

    def _on_websocket(self, gen: int, ws: WebSocket) -> None:
        if gen == self.generation:
            self._audit(ws.url)

    def _on_response(self, gen: int, response: Response) -> None:
        if gen != self.generation or origin_of(response.url) != self.sh.app_origin:
            return
        request = response.request
        # Headers are in: the request no longer holds the page back (a body the page never reads is
        # never reported as finished).
        self.inflight.discard(request)
        status = response.status
        path = urlsplit(response.url).path
        if status == 429 and (self._is_page_load(request) or self._is_scenario_call(request.method, path)):
            # Only limits on the page or on the scenario's own data calls stop it; a background call being
            # rate limited (device registration, telemetry) does not change what the scenario observes.
            self._flag_env(f"rate limited (HTTP 429) on {request.method} {path}")
        elif status in (401, 403) and self._is_page_load(request):
            self._flag_env(f"page load returned HTTP {status} for {path}")

    def _is_scenario_call(self, method: str, path: str) -> bool:
        """Does this request match one of the spec's operations on the app under test?"""
        source = self.sh.req.target == "source"
        for op in self.sh.req.operations:
            wire_method = op.source_method if source else op.replica_method
            wire_path = (op.source_path if source else op.replica_path).split("#", 1)[0]
            if method == wire_method and path_regex(wire_path).match(path):
                return True
        return False

    def _capture_url_regex(self) -> re.Pattern[str] | None:
        if not self.pending:
            return None
        paths = "|".join(p.pattern.pattern[1:-1] for p in self.pending)  # drop ^ and $
        return re.compile(r"^https?://[^/?#]+(?:" + paths + r")(?:[?#].*)?$")

    async def _capture_route(self, gen: int, route: Route) -> None:
        request = route.request
        matches: list[_Pending] = []
        if gen == self.generation and origin_of(request.url) in self.sh.allowed:
            method, path = request.method.upper(), urlsplit(request.url).path
            matches = [p for p in self.pending if p.name not in self.bindings.captured and p.method == method
                       and p.pattern.match(path)]
        if not matches:
            try:
                await route.fallback()
            except PlaywrightError:
                pass
            return
        self.request_seq += 1
        seq = self.request_seq
        try:
            response = await route.fetch(timeout=self.t.navigation_ms)
        except PlaywrightError as err:
            try:
                await route.abort(_abort_code(str(err)))
            except PlaywrightError:
                pass
            return
        try:
            if 200 <= response.status < 300:
                self._assign(matches, json.loads((await response.body()) or b"null"), seq)
        except (PlaywrightError, ValueError):
            pass
        try:
            await route.fulfill(response=response)
        except PlaywrightError:
            pass

    def _assign(self, matches: list[_Pending], body: Any, seq: int) -> None:
        for pending in matches:
            value = _scalar(_field(body, pending.field))
            if value is None or value == "null":
                continue
            # "The first request matching": an earlier request wins even if its response is slower.
            if pending.name not in self.capture_seq or seq < self.capture_seq[pending.name]:
                self.capture_seq[pending.name] = seq
                self.bindings.captured[pending.name] = value

    @staticmethod
    def _is_page_load(request: Request) -> bool:
        try:
            return request.is_navigation_request() and request.frame.parent_frame is None
        except PlaywrightError:
            return False

    def _flag_env(self, problem: str) -> None:
        if self.env_problem is None:
            self.env_problem = problem

    async def _settle(self) -> None:
        """Let requests triggered by the last action start (and finish) so they are attributed to it."""
        start = time.monotonic()
        await asyncio.sleep(self.t.settle_min_ms / 1000)
        cap, quiet = self.t.settle_cap_ms / 1000, self.t.settle_quiet_ms / 1000
        while time.monotonic() - start < cap:
            if not self.inflight and time.monotonic() - self.last_request_at >= quiet:
                return
            await asyncio.sleep(0.025)

    # ----- bindings at run time -----
    async def _render(self, text: str, timeout_ms: int) -> str:
        """Render a template, waiting (bounded) for captured values whose responses are still in flight."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            missing = self.bindings.missing(text)
            if not missing:
                return self.bindings.render(text)
            if time.monotonic() >= deadline:
                raise UnresolvedBinding(", ".join(missing))
            await asyncio.sleep(0.05)

    async def _target(self, target: Target, timeout_ms: int) -> Target:
        fields = {}
        for key in ("name", "has_text", "within_name", "within_has_text"):
            value = getattr(target, key)
            fields[key] = await self._render(value, timeout_ms) if value is not None else None
        return target.model_copy(update=fields)

    def _locate(self, t: Target) -> Locator:
        """Role + accessible name, optionally scoped to one container (role/name and/or contained text)."""
        assert self.page is not None
        page = self.page

        def by_role(scope: Page | Locator, role: str, name: str | None) -> Locator:
            if name is None:
                return scope.get_by_role(role)  # type: ignore[arg-type]
            return scope.get_by_role(role, name=name, exact=t.exact)  # type: ignore[arg-type]

        scope: Page | Locator = page
        if t.within_role:
            scope = by_role(page, t.within_role, t.within_name)
            if t.within_has_text:
                scope = scope.filter(has_text=t.within_has_text)
        elif t.within_has_text:
            # No container role: use the innermost element that contains both the text and the target.
            inner = by_role(page, t.role, t.name)
            holder = page.locator("*").filter(has_text=t.within_has_text).filter(has=inner)
            scope = holder.filter(has_not=page.locator("*").filter(has_text=t.within_has_text).filter(has=inner))
        loc = by_role(scope, t.role, t.name)
        if t.has_text:
            loc = loc.filter(has_text=t.has_text)
        return loc

    async def _pick(self, t: Target, timeout_ms: int) -> Locator:
        """Locator for an action. The strict target wins whenever it matches; if it matches nothing but exactly one
        element matches a looser reading of the same target (partial name, the scope's text instead of its name,
        or no scope), that element is used: the scope and exact name only disambiguate. Assertions stay strict."""
        strict = self._locate(t)
        looser = [self._locate(x) for x in _loosen(t)]
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if await self._count(strict) > 0:
                return strict
            for loc in looser:
                if await self._count(loc) == 1:
                    return loc
            if time.monotonic() >= deadline:
                return strict
            await asyncio.sleep(0.25)

    async def _retry_strict(self, attempt: Callable[[int], Awaitable[T]], timeout_ms: int) -> T:
        """Playwright fails at once on an ambiguous locator; a transient duplicate gets the full timeout."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                return await attempt(remaining)
            except (PlaywrightError, AssertionError) as err:
                if _is_strict(err) and time.monotonic() + 0.2 < deadline:
                    await asyncio.sleep(0.2)
                    continue
                raise

    # ----- phases -----
    async def _run_setup(self, setup: Scenario) -> None:
        self.phase = "setup"
        try:
            for i, step in enumerate(setup.steps):
                self.step = i
                await self._step(i, step)
        except StepFailure as failure:
            kind = failure.kind if failure.kind in ("env", "startup") else "action"
            raise StepFailure(kind, f"setup {setup.id} step {failure.step}: {failure.message}") from None
        finally:
            self.step = None

    async def _run_main(self) -> None:
        steps = self.scenario.steps
        first = probe_index(steps)
        self.phase = "main"
        for i, step in enumerate(steps):
            if i == first:
                self.step = None
                await self._probe(steps)
            self.step = i
            await self._step(i, step)
        if first >= len(steps):
            self.step = None
            await self._probe(steps)

    async def _step(self, i: int, step: ScenarioStep) -> None:
        try:
            if step.kind == "goto":
                await self._goto(step)
            elif step.kind == "reread":
                await self._reread()
            elif step.kind in ACTION_KINDS:
                await self._act(step)
            else:
                await self._expect(step)
        except StepFailure as failure:
            failure.step = i
            raise
        except UnresolvedBinding as err:
            kind = "assertion" if step.kind in EXPECT_KINDS else "action"
            raise StepFailure(kind, f"{step.kind}: value not available: {err}", i) from None
        except PlaywrightError as err:
            kind = "env" if _is_closed(err) else "action"
            raise StepFailure(kind, f"{step.kind} {describe(step.target)}: {clean_error(err)}", i) from None

    # ----- navigation -----
    async def _goto(self, step: ScenarioStep) -> None:
        route = await self._render(step.route or step.value or "/", self.t.action_ms)
        base = self.sh.req.base_url.rstrip("/") + "/"
        url = urljoin(base, route.lstrip("/")) if "://" not in route else route
        if origin_of(url) != self.sh.app_origin:
            raise StepFailure("action", f"goto {route!r} leaves the app origin {self.sh.app_origin}")
        await self._navigate(url, route)

    async def _reread(self) -> None:
        assert self.ctx is not None and self.page is not None
        url = self.page.url if self.page.url.startswith("http") else self.sh.req.base_url
        state = await self.ctx.storage_state()
        old = self.ctx
        await self._open(state)  # a new context carrying the same cookies and local storage
        await self._retire(old)
        await self._navigate(url, urlsplit(url).path or "/")

    async def _navigate(self, url: str, label: str) -> None:
        assert self.page is not None
        first = not self.navigated
        try:
            response = await self.page.goto(url, wait_until="load", timeout=self.t.navigation_ms)
        except PlaywrightTimeout:
            raise StepFailure("startup" if first else "env",
                              f"loading {label} timed out after {self.t.navigation_ms} ms") from None
        except PlaywrightError as err:
            if _is_closed(err):
                raise
            code = _net_error(str(err))
            kind = "startup" if first else ("env" if code else "action")
            raise StepFailure(kind, f"loading {label} failed: {code or clean_error(err)}") from None
        self.navigated = True
        status = response.status if response is not None else 200
        if status in (401, 403, 429):
            raise StepFailure("env", f"loading {label} returned HTTP {status}")
        if status >= 500:
            kind = "startup" if first else ("env" if status in (502, 503, 504) else "action")
            raise StepFailure(kind, f"loading {label} returned HTTP {status}")
        await self._check_challenge(response, label)
        await self._settle()

    async def _check_challenge(self, response: Response | None, label: str) -> None:
        assert self.page is not None
        if response is not None:
            try:
                headers = await response.all_headers()
            except PlaywrightError:
                headers = {}
            if headers.get("cf-mitigated", "").lower() == "challenge":
                raise StepFailure("env", f"loading {label} hit a bot challenge (cf-mitigated)")
        try:
            title = (await self.page.title()).strip()
        except PlaywrightError:
            return
        if _CHALLENGE_TITLES.match(title):
            raise StepFailure("env", f"loading {label} hit a challenge page ({title!r})")

    # ----- actions -----
    async def _act(self, step: ScenarioStep) -> None:
        kind = step.kind
        if kind == "press" and step.target is None:
            assert self.page is not None
            await self.page.keyboard.press(await self._render(step.value or "", self.t.action_ms))
            await self._settle()
            return
        if step.target is None:
            raise StepFailure("action", f"{kind} needs a target")
        target = await self._target(step.target, self.t.action_ms)
        value = await self._render(step.value, self.t.action_ms) if step.value is not None else None
        loc = await self._pick(target, self.t.action_ms)
        what = f"{kind} {describe(target)}"

        async def attempt(ms: int) -> None:
            if kind == "click":
                await loc.click(timeout=ms)
            elif kind == "fill":
                try:
                    await loc.fill(value or "", timeout=min(ms, 3000))
                except PlaywrightTimeout:
                    # Rich inputs (command palettes, editors) that refuse fill still take typed keys.
                    assert self.page is not None
                    await loc.click(timeout=ms)
                    await self.page.keyboard.press("ControlOrMeta+A")
                    await self.page.keyboard.type(value or "", delay=15)
            elif kind == "check":
                await loc.check(timeout=ms)
            elif kind == "uncheck":
                await loc.uncheck(timeout=ms)
            elif kind == "press":
                await loc.press(value or "", timeout=ms)
            else:
                await self._select(loc, value or "", ms)

        try:
            await self._retry_strict(attempt, self.t.action_ms)
        except PlaywrightTimeout:
            count = await self._count(loc)
            reason = ("element not found" if count == 0 else
                      f"{count} elements match (ambiguous)" if count > 1 else
                      "element is not visible, enabled or stable")
            raise StepFailure("action", f"{what}: {reason} after {self.t.action_ms} ms") from None
        except PlaywrightError as err:
            if _is_closed(err):
                raise
            if _is_strict(err):
                raise StepFailure("action", f"{what}: ambiguous target ({clean_error(err)})") from None
            raise StepFailure("action", f"{what}: {clean_error(err)}") from None
        await self._settle()

    async def _select(self, loc: Locator, label: str, ms: int) -> None:
        options = await loc.evaluate(_OPTIONS_JS, timeout=ms)
        if options is None:  # an ARIA combobox: open it and pick the option by name
            await loc.click(timeout=ms)
            assert self.page is not None
            await self.page.get_by_role("option", name=label, exact=True).click(timeout=ms)
            return
        for lbl, _, text in options:
            if label in (lbl, text):
                await loc.select_option(label=lbl, timeout=ms)
                return
        for _, val, _ in options:
            if label == val:
                await loc.select_option(value=val, timeout=ms)
                return
        for lbl, _, text in options:
            if label.lower() in (lbl.lower(), text.lower()):
                await loc.select_option(label=lbl, timeout=ms)
                return
        shown = ", ".join(repr(o[0]) for o in options[:12])
        raise StepFailure("action", f"no option labelled {label!r} (options: {shown})")

    @staticmethod
    async def _count(loc: Locator) -> int:
        try:
            return await loc.count()
        except PlaywrightError:
            return -1

    # ----- expectations -----
    async def _expect(self, step: ScenarioStep) -> None:
        kind = step.kind
        ms = self.t.expect_ms
        if kind == "expect_url":
            await self._expect_url(await self._render(step.value or step.route or "", ms))
            return
        if step.target is None:
            raise StepFailure("assertion", f"{kind} needs a target")
        target = await self._target(step.target, ms)
        value = await self._render(step.value, ms) if step.value is not None else None
        loc = self._locate(target)
        what = f"{kind} {describe(target)}"
        try:
            if kind == "expect_visible":
                await self._retry_strict(lambda t: expect(loc).to_be_visible(timeout=t), ms)
            elif kind == "expect_hidden":
                await expect(loc.filter(visible=True)).to_have_count(0, timeout=ms)
            elif kind == "expect_text":
                await self._retry_strict(
                    lambda t: expect(loc).to_contain_text(value or "", ignore_case=True, timeout=t), ms)
            else:
                await self._expect_value(loc, value or "", what)
        except AssertionError as err:
            if _is_strict(err):
                raise StepFailure("action", f"{what}: ambiguous target ({clean_error(err)})") from None
            detail = clean_error(err)
            if kind == "expect_text":
                detail = f"expected to contain {value!r}; {detail}"
            raise StepFailure("assertion", f"{what}: {detail}") from None

    async def _expect_value(self, loc: Locator, expected: str, what: str) -> None:
        deadline = time.monotonic() + self.t.expect_ms / 1000
        last = "element not found"
        ambiguous = False
        while True:
            count = await self._count(loc)
            ambiguous = count > 1
            if count == 1:
                try:
                    values = await loc.evaluate(_VALUES_JS, timeout=1_000)
                    if expected in values:
                        return
                    last = f"current value {values[0]!r}" if values else "no value"
                except PlaywrightError as err:
                    if _is_closed(err):
                        raise
                    last = clean_error(err)
            elif ambiguous:
                last = f"{count} elements match (ambiguous)"
            else:
                last = "element not found"
            if time.monotonic() >= deadline:
                kind = "action" if ambiguous else "assertion"
                raise StepFailure(kind, f"{what}: expected value {expected!r}, {last}")
            await asyncio.sleep(0.1)

    async def _expect_url(self, template: str) -> None:
        rx, with_query = url_regex(template)
        deadline = time.monotonic() + self.t.expect_ms / 1000
        while True:
            assert self.page is not None
            parts = urlsplit(self.page.url)
            current = parts.path + (f"?{parts.query}" if with_query and parts.query else "")
            if rx.match(current):
                return
            if time.monotonic() >= deadline:
                raise StepFailure("assertion", f"expect_url {template!r}: current path is {current!r}")
            await asyncio.sleep(0.1)

    # ----- non-vacuity -----
    async def _probe(self, steps: list[ScenarioStep]) -> None:
        """Before the first action: an expectation that already holds proves nothing about the actions."""
        assert self.page is not None
        self.probed = True
        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.t.probe_idle_ms)
        except PlaywrightError:
            pass
        for i, step in enumerate(steps):
            if step.kind not in EXPECT_KINDS:
                continue
            if any(self.bindings.missing(x) for x in step_texts(step)):
                continue  # depends on a value the actions will produce
            try:
                if await self._holds_now(step):
                    self.vacuous.append(i)
            except (PlaywrightError, UnresolvedBinding, StepFailure):
                continue

    async def _holds_now(self, step: ScenarioStep) -> bool:
        assert self.page is not None
        render = self.bindings.render
        if step.kind == "expect_url":
            rx, with_query = url_regex(render(step.value or step.route or ""))
            parts = urlsplit(self.page.url)
            return bool(rx.match(parts.path + (f"?{parts.query}" if with_query and parts.query else "")))
        if step.target is None:
            return False
        target = step.target.model_copy(update={
            k: render(getattr(step.target, k)) if getattr(step.target, k) is not None else None
            for k in ("name", "has_text", "within_name", "within_has_text")})
        loc = self._locate(target)
        if step.kind == "expect_hidden":
            return await loc.filter(visible=True).count() == 0
        if await loc.count() != 1:
            return False
        if step.kind == "expect_visible":
            return await loc.is_visible()
        value = render(step.value or "")
        if step.kind == "expect_text":
            return _norm(value) in _norm(await loc.text_content(timeout=1_000) or "")
        return value in await loc.evaluate(_VALUES_JS, timeout=1_000)

    # ----- effects -----
    async def _run_effects(self) -> None:
        self.phase = "effects"
        index = len(self.scenario.steps)
        for n, effect in enumerate(self.scenario.effects):
            try:
                await self._effect(effect)
            except StepFailure as failure:
                failure.step = index
                failure.message = f"effect {n} ({effect.operation}): {failure.message}"
                raise
            except UnresolvedBinding as err:
                raise StepFailure("assertion", f"effect {n} ({effect.operation}): value not available: {err}",
                                  index) from None

    async def _effect(self, effect) -> None:
        op = self.sh.ops.get(effect.operation)
        if op is None:
            raise StepFailure("action", f"unknown operation {effect.operation!r}")
        method, template = self._wire(op)
        if method.upper() != "GET":
            raise StepFailure("action", f"effects only call read operations, {effect.operation} is {method}")
        params = {p.name: await self._render(p.value, self.t.expect_ms) for p in effect.path_params}
        path = fill_path(template, params)
        url = urljoin(self.sh.req.base_url.rstrip("/") + "/", path.lstrip("/"))
        value = await self._render(effect.value, self.t.expect_ms)
        want = "present" if effect.present else "absent"
        deadline = time.monotonic() + self.t.effect_ms / 1000
        while True:
            assert self.ctx is not None
            try:
                response = await self.ctx.request.get(url, timeout=self.t.request_ms, fail_on_status_code=False)
            except PlaywrightError as err:
                if _is_closed(err):
                    raise
                raise StepFailure("env", f"GET {path} failed: {clean_error(err)}") from None
            status = response.status
            if status in (401, 403, 429):
                raise StepFailure("env", f"GET {path} returned HTTP {status}")
            found: bool | None = None
            if status == 404:
                found = False  # the parent or the item itself is gone
                last = "HTTP 404"
            elif 200 <= status < 300:
                try:
                    body = await response.json()
                except (PlaywrightError, ValueError):
                    body = None
                    last = "response is not JSON"
                else:
                    items = _items(body, op.list_key, op.kind)
                    found = any(_scalar(_field(item, effect.field)) == value for item in items)
                    last = f"{len(items)} item(s), {effect.field}={value!r} {'found' if found else 'not found'}"
            else:
                last = f"HTTP {status}"
            await response.dispose()
            if found is not None and found == effect.present:
                return
            if time.monotonic() >= deadline:
                raise StepFailure("assertion", f"expected {effect.field}={value!r} {want} in GET {path}: {last}")
            await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------
async def _launch(pw: Any, headless: bool, channel: str | None) -> Browser:
    import os

    if os.environ.get("FORGE_HEADED") == "1":
        headless = False
    args = ["--disable-dev-shm-usage"]
    if channel:
        try:
            return await pw.chromium.launch(headless=headless, channel=channel, args=args)
        except PlaywrightError:
            pass  # e.g. Chrome missing or not startable here: fall back to the bundled Chromium
    return await pw.chromium.launch(headless=headless, args=args)


def _failed(req: RunnerRequest, scenario: Scenario, run: int, kind: str, message: str) -> ScenarioResult:
    return ScenarioResult(scenario_id=scenario.id, target=req.target, run=run, passed=False,
                          failure_kind=kind, message=message)


async def run_scenarios(req: RunnerRequest, *, storage_state: dict | None = None,
                        timeouts: Timeouts | None = None, headless: bool = True,
                        channel: str | None = None) -> list[ScenarioResult]:
    """Run every scenario ``req.runs`` times (fresh bindings, fresh context) and return one result per run.

    Results are ordered by scenario, then run number (1-based). ``storage_state`` (in memory) overrides
    ``req.storage_state_path`` so a decrypted session never has to touch the disk. ``channel`` picks an
    installed browser (``"chrome"`` on the host); None, or a channel that fails to start, means
    Playwright's bundled Chromium (always the case inside the runner image).
    """
    timeouts = timeouts or Timeouts()
    jobs = [(s, run) for s in req.scenarios for run in range(1, max(1, req.runs) + 1)]
    app_origin = origin_of(req.base_url)
    if app_origin is None:
        return [_failed(req, s, r, "startup", f"base_url {req.base_url!r} is not an http(s) URL") for s, r in jobs]
    state: dict | str | None = storage_state
    if state is None and req.storage_state_path:
        if not Path(req.storage_state_path).is_file():
            return [_failed(req, s, r, "env", "storage state file not found") for s, r in jobs]
        state = req.storage_state_path
    allowed = frozenset(o for o in (origin_of(x) for x in [*req.allowed_origins, req.base_url]) if o)

    async with async_playwright() as pw:
        try:
            browser = await _launch(pw, headless, channel)
        except PlaywrightError as err:
            return [_failed(req, s, r, "env", f"browser did not start: {clean_error(err)}") for s, r in jobs]
        shared = _Shared(browser=browser, req=req, ops={o.id: o for o in req.operations},
                         scenarios={s.id: s for s in req.scenarios}, storage_state=state, timeouts=timeouts,
                         fixtures={f.name: f.value for f in req.fixtures}, app_origin=app_origin, allowed=allowed)
        gate = asyncio.Semaphore(max(1, req.parallel))

        async def one(scenario: Scenario, run: int) -> ScenarioResult:
            async with gate:
                return await _Run(shared, scenario, run).execute()

        try:
            return list(await asyncio.gather(*(one(s, r) for s, r in jobs)))
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass


def with_timeouts(base: Timeouts | None = None, **overrides: int) -> Timeouts:
    return replace(base or Timeouts(), **overrides)
