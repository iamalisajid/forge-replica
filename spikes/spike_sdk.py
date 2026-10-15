"""Spike: Agent SDK with in-process tools (text + image results), serialized parallel calls,
dontAsk with exact tool names, structured output with tools=[], and a PreToolUse guard."""
import asyncio
import base64
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    ToolAnnotations,
    create_sdk_mcp_server,
    query,
    tool,
)


def load_key() -> str:
    for line in Path("~/.forge.env").expanduser().read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")


def solid_png(rgb=(220, 30, 30), size=8) -> str:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


lock = asyncio.Lock()
state = {"active": 0, "max_active": 0, "observe": 0, "act": 0}


@tool("observe", "Return the current page snapshot and a screenshot.", {"scope": str},
      annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=200_000))
async def observe(args):
    async with lock:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        state["observe"] += 1
        await asyncio.sleep(0.3)
        state["active"] -= 1
    return {"content": [{"type": "text", "text": '- button "Create" [ref=e1]'},
                        {"type": "image", "data": solid_png(), "mimeType": "image/png"}]}


@tool("act", "Perform an action on an element ref.", {"ref": str, "action": str})
async def act(args):
    async with lock:
        state["act"] += 1
    return {"content": [{"type": "text", "text": f"{args['action']} {args['ref']}: ok"}]}


async def run(prompt, options):
    result = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg
    return result


async def main():
    key = load_key()
    env = {"ANTHROPIC_API_KEY": key, "MAX_MCP_OUTPUT_TOKENS": "50000"}
    server = create_sdk_mcp_server(name="browser", version="0.1.0", tools=[observe, act])
    schema = {"type": "object", "additionalProperties": False,
              "required": ["observed", "acted", "image_color"],
              "properties": {"observed": {"type": "integer"}, "acted": {"type": "boolean"},
                             "image_color": {"type": "string"}}}
    t0 = time.time()
    r1 = await run(
        "Call the observe tool twice in parallel (both calls in one message, scope 'main' and 'dialog'). "
        "Then call act with ref e1 and action click. Report how many observe calls you made, whether act "
        "succeeded, and the dominant color of the screenshot in one word.",
        ClaudeAgentOptions(
            system_prompt="You operate a browser through tools. Follow instructions exactly.",
            tools=[], mcp_servers={"browser": server},
            allowed_tools=["mcp__browser__observe", "mcp__browser__act"],
            permission_mode="dontAsk", setting_sources=[], max_turns=8, max_budget_usd=1.0,
            model="claude-opus-5-5", effort="low", env=env,
            output_format={"type": "json_schema", "schema": schema},
        ),
    )
    print(f"[sdk tools] {time.time()-t0:.1f}s subtype={r1.subtype} terminal={r1.terminal_reason} "
          f"cost=${r1.total_cost_usd} turns={r1.num_turns} denials={r1.permission_denials}")
    print("[sdk tools] structured_output:", r1.structured_output, "| state:", state)

    tmp = Path(tempfile.mkdtemp(prefix="forge-spike-"))
    outside = Path("/tmp/forge-spike-outside.txt")
    outside.unlink(missing_ok=True)
    denied = []

    async def guard(input_data, tool_use_id, context):
        target = os.path.realpath(input_data["tool_input"].get("file_path", ""))
        if not target.startswith(os.path.realpath(tmp) + os.sep):
            denied.append(target)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "path is outside the worktree"}}
        return {}

    r2 = await run(
        f"Use the Write tool to write the text 'hi' to {outside}. Then write the text 'ok' to {tmp}/inside.txt.",
        ClaudeAgentOptions(
            tools=["Write"], allowed_tools=["Write"], permission_mode="dontAsk", setting_sources=[],
            cwd=str(tmp), max_turns=6, max_budget_usd=1.0, model="claude-opus-5-5", effort="low", env=env,
            hooks={"PreToolUse": [HookMatcher(matcher="Write", hooks=[guard])]},
        ),
    )
    print(f"[guard] subtype={r2.subtype} cost=${r2.total_cost_usd} denied={denied} "
          f"outside_exists={outside.exists()} inside_exists={(tmp / 'inside.txt').exists()}")

    ok = (r1.structured_output and r1.structured_output.get("observed") == 2 and r1.structured_output.get("acted")
          and state["max_active"] == 1 and denied and not outside.exists() and (tmp / "inside.txt").exists())
    print("PASS" if ok else "CHECK OUTPUT")


asyncio.run(main())
