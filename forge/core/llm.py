"""Structured model calls (Messages API) with cost accounting and record/replay cassettes.

Mode is chosen by FORGE_LLM: ``live`` (default), ``record`` (live + save a cassette) or ``replay`` (no network;
fail if the cassette is missing). Cassettes are keyed by a hash of model, prompt, schema and settings, so a
changed prompt never silently reuses an old answer. Agent SDK sessions are not recorded; they are tested with
scripted drivers instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

from forge.core.config import DEFAULT_MODEL, EFFORT, REPO_ROOT, api_key, cost_usd
from forge.core.ledger import Ledger

T = TypeVar("T", bound=BaseModel)
CASSETTES = Path(os.environ.get("FORGE_CASSETTES", REPO_ROOT / "tests" / "cassettes"))


class ModelRefused(RuntimeError):
    pass


class ModelTruncated(RuntimeError):
    pass


@dataclass
class StructuredResult(Generic[T]):
    parsed: T
    usage: dict
    cost_usd: float
    model: str
    cassette_key: str
    replayed: bool


def _cassette_key(model: str, system: str, content, schema: dict, effort: str, max_tokens: int) -> str:
    blob = json.dumps({"model": model, "system": system, "content": content, "schema": schema,
                       "effort": effort, "max_tokens": max_tokens}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def structured_call(*, stage: str, system: str, content, output: type[T], max_tokens: int = 32_000,
                    effort: str | None = None, model: str = DEFAULT_MODEL, ledger: Ledger | None = None,
                    cassettes: Path = CASSETTES) -> StructuredResult[T]:
    """One schema-constrained call. ``content`` is a string or a list of content blocks (text/image)."""
    effort = effort or EFFORT.get(stage, "high")
    schema = output.model_json_schema()
    key = _cassette_key(model, system, content, schema, effort, max_tokens)
    mode = os.environ.get("FORGE_LLM", "live")
    path = cassettes / f"{stage}-{key}.json"

    if mode == "replay":
        if not path.exists():
            raise FileNotFoundError(f"no cassette for {stage} ({path.name}); record it with FORGE_LLM=record")
        saved = json.loads(path.read_text())
        return StructuredResult(output.model_validate_json(saved["text"]), saved["usage"], 0.0, model, key, True)

    if ledger:
        ledger.check_budget()
    import anthropic

    # Long structured calls can think for minutes before the first token; allow it instead of timing out.
    client = anthropic.Anthropic(api_key=api_key(), timeout=1800.0, max_retries=2)
    started = time.time()
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=output,
        output_config={"effort": effort},
    ) as stream:
        message = stream.get_final_message()
    usage = message.usage.model_dump() if message.usage else {}
    cost = cost_usd(model, usage)
    if ledger:
        ledger.record_call(stage=stage, kind="structured", model=model, usage=usage, cost_usd=cost,
                           duration_s=time.time() - started, detail={"cassette": key})
    if message.stop_reason == "refusal":
        raise ModelRefused(f"{stage}: the model declined the request")
    if message.stop_reason == "max_tokens":
        raise ModelTruncated(f"{stage}: output hit max_tokens={max_tokens}")
    text = "".join(block.text for block in message.content if block.type == "text")
    parsed = output.model_validate_json(text)
    if mode == "record":
        cassettes.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": stage, "model": model, "usage": usage, "text": text}, indent=1))
    return StructuredResult(parsed, usage, cost, model, key, False)
