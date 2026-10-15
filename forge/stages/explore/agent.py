"""Explore stage: Claude Agent SDK sessions that drive the harness, a breadth pass then feature probes."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from claude_agent_sdk import (ClaudeAgentOptions, ResultError, ResultMessage, ToolAnnotations, create_sdk_mcp_server,
                              query, tool)
from forge.core.config import EFFORT, REPO_ROOT, api_key
from forge.core.store import RunStore
from forge.models.base import ArtifactHeader, Strict
from forge.models.run import RunConfig
from forge.models.trace import Trace
from forge.stages.explore.harness import ACTIONS, Harness

PROMPT_FILE = REPO_ROOT / "forge" / "prompts" / "explore.md"
TOOL_NAMES = ["mcp__browser__observe", "mcp__browser__act", "mcp__browser__navigate"]


class FeatureItem(Strict):
    name: str
    entity: str
    screens: list[str]
    can_create: bool
    can_edit: bool
    can_delete: bool
    actions: list[str]
    needs_parent: str | None
    priority: int


class Inventory(Strict):
    app_name: str
    requires_login: bool
    features: list[FeatureItem]
    pilot_feature: str
    notes: list[str]


class ExploreResult(Strict):
    header: ArtifactHeader
    inventory: Inventory | None
    probed: list[str]
    cleanup: list[str]
    sessions: list[str]
    cost_usd: float


def browser_tools(h: Harness):
    @tool("observe", "Accessibility snapshot of the current page (refs in [ref=eN]); page=N for long pages; "
          "screenshot=true adds a PNG.",
          {"type": "object", "properties": {"page": {"type": "integer", "minimum": 1},
                                            "screenshot": {"type": "boolean"}}, "required": []},
          annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=200_000))
    async def observe(args):
        text, png = await h.observe(int(args.get("page") or 1), bool(args.get("screenshot")))
        content = [{"type": "text", "text": text}]
        if png:
            content.append({"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"})
        return {"content": content}

    @tool("act", "Perform one action on an element ref from the latest snapshot. Returns API calls caused and a "
          "fresh snapshot.",
          {"type": "object", "properties": {"ref": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
                                            "value": {"type": "string"}}, "required": ["ref", "action"]},
          annotations=ToolAnnotations(maxResultSizeChars=200_000))
    async def act(args):
        return {"content": [{"type": "text", "text": await h.act(args["ref"], args["action"], args.get("value"))}]}

    @tool("navigate", "Open a path or URL on the same app. Navigating to the current URL reloads it.",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
          annotations=ToolAnnotations(maxResultSizeChars=200_000))
    async def navigate(args):
        return {"content": [{"type": "text", "text": await h.navigate(args["url"])}]}

    return [observe, act, navigate]


@dataclass
class SessionOutcome:
    result: ResultMessage | None
    structured: dict | None


async def _session(store: RunStore, server, prompt: str, *, model: str, budget_usd: float, max_turns: int,
                   label: str, output_schema: dict | None = None) -> SessionOutcome:
    options = ClaudeAgentOptions(
        system_prompt=PROMPT_FILE.read_text(), tools=[], mcp_servers={"browser": server},
        allowed_tools=TOOL_NAMES, permission_mode="dontAsk", setting_sources=[], max_turns=max_turns,
        max_budget_usd=budget_usd, model=model, effort=EFFORT["explore"],
        env={"ANTHROPIC_API_KEY": api_key() or "", "MAX_MCP_OUTPUT_TOKENS": "60000"},
        output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
    )
    result = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
    except ResultError as exc:
        # The CLI reports a stopped session (turn or budget limit, a failed API call mid-session) as an error after
        # yielding its result. What the browser recorded so far is still good evidence, so the session just ends;
        # only a session that never produced a result, or a rejected key, stops the stage.
        if result is None or exc.api_error_status in (401, 403):
            raise
    if result is not None:
        store.ledger.record_call(stage="explore", kind=f"agent:{label}", model=model, usage=result.usage or {},
                                 cost_usd=result.total_cost_usd or 0.0, duration_s=(result.duration_ms or 0) / 1000,
                                 detail={"turns": result.num_turns, "terminal": result.terminal_reason,
                                         "subtype": result.subtype})
    structured = result.structured_output if result is not None and isinstance(result.structured_output, dict) else None
    return SessionOutcome(result, structured)


BREADTH = """Explore {url} and build a feature inventory.
Visit every screen you can reach from the navigation and from list items. Open every "new"/"create" dialog or form to read its fields and the options of every select, then cancel it (do not submit in this pass). Note custom actions on records (buttons other than open/edit/delete).{extra}
When done, return the inventory: one feature per main entity (name, entity in singular snake_case, screens visited, which of create/edit/delete exist, custom actions, the parent entity if records live under another one, priority 1 = most central). Pick as pilot_feature the most central feature that supports create, edit and delete."""

PROBE = """Probe the feature "{feature}" ({entity}) of {url} end to end. Work through these in order:
1. If its records live under a parent ({parent}), create a new parent record first and open it.
2. Create a record, filling every field and choosing non-default options. Confirm it appears.
3. Reload the page (navigate to the current URL) and confirm it is still there.
{search_step}4. Open the record's detail page, edit its name or title, its description if it has one, and at least one other field, save, reload and confirm the changes.
5. Submit the create form once with the required fields left empty, read the validation message, then cancel.
6. Perform each custom action ({actions}) on your record and confirm the effect after a reload.
7. Delete the record you created, confirm it is gone after a reload. Then delete the parent you created, if any.
Finish with a short summary of what each step showed."""


SEARCH_NOTE = """
The user wants search replicated. Find it yourself anywhere in the app: a search box, a search button that opens a search dialog or command palette, or a filter field above a list. Open each one, type a word, read the results, then close it. Add one note per search you found, written "search: <screen> via <control>, finds <what>". If the app has no search, add the note "search: none found"."""

SEARCH_STEP = """3b. Search (the user asked for it): find your record with the app's search. Open the search the way a user would, type a distinctive word of the record's name, confirm the record is in the results, open it from there if the results allow, then close the search.
"""


def _breadth_extra(config: RunConfig) -> str:
    return SEARCH_NOTE if "search" in config.features else ""


async def explore(store: RunStore, config: RunConfig, *, quick: bool, storage_state: dict | None = None,
                  budget_usd: float | None = None) -> tuple[Trace, ExploreResult]:
    budget = budget_usd if budget_usd is not None else max(2.5 if quick else 4.0, config.budget_usd * 0.35)
    harness = Harness(store, url=config.url, mutations=config.mutations, storage_state=storage_state)
    sessions: list[str] = []
    inventory: Inventory | None = None
    probed: list[str] = []
    cleanup: list[str] = []
    await harness.start()
    try:
        await harness.navigate(config.url)
        server = create_sdk_mcp_server(name="browser", version="1.0.0", tools=browser_tools(harness))
        outcome = await _session(store, server, BREADTH.format(url=config.url, extra=_breadth_extra(config)),
                                 model=config.model,
                                 budget_usd=budget * 0.35, max_turns=50, label="breadth",
                                 output_schema=Inventory.model_json_schema())
        sessions.append(_describe("breadth", outcome))
        if outcome.structured:
            inventory = Inventory.model_validate(outcome.structured)
        features = _choose(inventory, quick)
        per_probe = budget * 0.65 / max(1, len(features))
        for feature in features:
            outcome = await _session(
                store, server,
                PROBE.format(feature=feature.name, entity=feature.entity, url=config.url,
                             search_step=SEARCH_STEP if "search" in config.features else "",
                             parent=feature.needs_parent or "none", actions=", ".join(feature.actions) or "none"),
                model=config.model, budget_usd=per_probe, max_turns=100, label=f"probe:{feature.name}")
            sessions.append(_describe(f"probe:{feature.name}", outcome))
            probed.append(feature.name)
        cleanup = await harness.cleanup()
    finally:
        await harness.close()
    header = ArtifactHeader(schema_id="forge.trace/1", run_id=config.run_id, stage="explore", model=config.model,
                            prompt_sha=hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest()[:16])
    trace = harness.trace(header, "probe", inventory.pilot_feature if inventory else None, notes=cleanup)
    result = ExploreResult(header=header.model_copy(update={"schema_id": "forge.explore/1"}), inventory=inventory,
                           probed=probed, cleanup=cleanup, sessions=sessions,
                           cost_usd=sum(c for s, c, _ in store.ledger.cost_by_stage() if s == "explore"))
    store.write("trace", trace)
    store.write("explore", result)
    return trace, result


def _choose(inventory: Inventory | None, quick: bool) -> list[FeatureItem]:
    if inventory is None or not inventory.features:
        return [FeatureItem(name="main", entity="item", screens=[], can_create=True, can_edit=True, can_delete=True,
                            actions=[], needs_parent=None, priority=1)]
    ordered = sorted(inventory.features, key=lambda f: f.priority)
    pilot = next((f for f in ordered if f.name == inventory.pilot_feature), ordered[0])
    return [pilot] if quick else [pilot] + [f for f in ordered if f is not pilot]


def _describe(label: str, outcome: SessionOutcome) -> str:
    r = outcome.result
    if r is None:
        return f"{label}: no result"
    return (f"{label}: {r.subtype}, {r.num_turns} turns, ${(r.total_cost_usd or 0):.2f}, "
            f"terminal={r.terminal_reason}")
