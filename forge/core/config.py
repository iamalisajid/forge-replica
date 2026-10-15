"""Paths, model defaults, prices and environment loading. Secrets stay in the process environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
FORGE_HOME = Path(os.environ.get("FORGE_HOME", Path.home() / ".forge"))
RUNS_DIR = FORGE_HOME / "runs"
SESSIONS_DIR = FORGE_HOME / "sessions"
KEYS_DIR = FORGE_HOME / "keys"
APPS_ROOT = Path(os.environ.get("FORGE_APPS", Path.home() / "forge-apps"))
ENV_FILE = Path(os.environ.get("FORGE_ENV_FILE", Path.home() / ".forge.env"))
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "template"

BASE_IMAGE = "forge-template-base"
RUNNER_IMAGE = "forge-runner"

# USD per million tokens: input, output, cache read, cache write (5 min).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

# Effort per stage (Opus 5.5 defaults to medium; set it explicitly everywhere).
EFFORT = {"explore": "medium", "extract": "medium", "specify": "high", "build": "high", "schema-check": "low"}


def browser_channel() -> str | None:
    """Host-side browsers (explore, validate on the original) use the installed Google Chrome when present;
    FORGE_BROWSER=chromium forces Playwright's bundled Chromium. Containers always use Chromium (no Chrome on ARM Linux)."""
    if os.environ.get("FORGE_BROWSER", "chrome").lower() == "chromium":
        return None
    for path in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome",
                 "/usr/bin/google-chrome-stable", "C:/Program Files/Google/Chrome/Application/chrome.exe"):
        if os.path.exists(path):
            return "chrome"
    return None


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines into os.environ without overriding values that are already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str | None:
    load_env_file()
    return os.environ.get("ANTHROPIC_API_KEY")


def cost_usd(model: str, usage: dict) -> float:
    price_in, price_out, price_read, price_write = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return (
        usage.get("input_tokens", 0) * price_in
        + usage.get("output_tokens", 0) * price_out
        + usage.get("cache_read_input_tokens", 0) * price_read
        + usage.get("cache_creation_input_tokens", 0) * price_write
    ) / 1_000_000


@dataclass(frozen=True)
class Limits:
    max_browsers: int = 3
    max_agents: int = 2
    max_stacks: int = 2
    snapshot_chars: int = 40_000
    run_checks_timeout_s: int = 600
