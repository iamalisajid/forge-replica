"""Shared base classes for every forge contract.

Two kinds of models live in forge.models:

* Draft models (``*Draft``) are filled in by a model through structured outputs. They must stay
  compatible with the Messages API JSON-schema subset: no recursion, ``additionalProperties: false``
  everywhere, no open maps, and every property required (nullable instead of optional).
* Stored artifacts wrap data with an ``ArtifactHeader`` recording what produced them, so stages can be
  cached and invalidated by content hash.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Forbid unknown keys so drafts and artifacts never carry silent extras."""

    model_config = ConfigDict(extra="forbid")


class NameValue(Strict):
    """A key/value pair. Used instead of dicts so schemas stay closed."""

    name: str
    value: str


class ArtifactHeader(Strict):
    schema_id: str
    run_id: str
    stage: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    inputs: list[NameValue] = Field(default_factory=list, description="input artifact name -> sha256")
    stage_version: str = "1"
    prompt_sha: str | None = None
    model: str | None = None
