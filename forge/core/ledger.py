"""SQLite ledger: every stage execution and model call, with cost. Budgets are enforced from it."""
from __future__ import annotations

import json
import functools
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, cache_key TEXT, status TEXT NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost_usd REAL NOT NULL, duration_s REAL, detail TEXT, at REAL NOT NULL
);
"""


class BudgetExceeded(RuntimeError):
    pass


def _locked(method):
    """Stages call models from worker threads too; one lock serializes every use of the shared connection."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Ledger:
    def __init__(self, path: Path, budget_usd: float | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.budget_usd = budget_usd

    @_locked
    def spent(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls").fetchone()[0]

    @_locked
    def remaining(self) -> float | None:
        return None if self.budget_usd is None else max(0.0, self.budget_usd - self.spent())

    @_locked
    def check_budget(self, need_usd: float = 0.0) -> None:
        remaining = self.remaining()
        if remaining is not None and remaining < need_usd:
            raise BudgetExceeded(f"budget exhausted: spent ${self.spent():.2f} of ${self.budget_usd:.2f}")

    @_locked
    def record_call(self, *, stage: str, kind: str, model: str, usage: dict, cost_usd: float,
                    duration_s: float, detail: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO model_calls (stage, kind, model, input_tokens, output_tokens, cache_read, cache_write,"
            " cost_usd, duration_s, detail, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (stage, kind, model, usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
             cost_usd, duration_s, json.dumps(detail or {}), time.time()))
        self.db.commit()

    @_locked
    def stage_started(self, stage: str, cache_key: str | None) -> int:
        cur = self.db.execute("INSERT INTO stage_runs (stage, cache_key, status, started_at) VALUES (?,?,?,?)",
                              (stage, cache_key, "running", time.time()))
        self.db.commit()
        return cur.lastrowid

    @_locked
    def stage_finished(self, row_id: int, status: str, detail: str | None = None) -> None:
        self.db.execute("UPDATE stage_runs SET status=?, finished_at=?, detail=? WHERE id=?",
                        (status, time.time(), detail, row_id))
        self.db.commit()

    @_locked
    def cost_by_stage(self) -> list[tuple[str, float, int]]:
        return self.db.execute(
            "SELECT stage, SUM(cost_usd), COUNT(*) FROM model_calls GROUP BY stage ORDER BY MIN(at)").fetchall()

    @_locked
    def stages(self) -> list[tuple]:
        return self.db.execute(
            "SELECT stage, status, started_at, finished_at, detail FROM stage_runs ORDER BY id").fetchall()
