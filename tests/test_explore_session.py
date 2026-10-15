"""An explorer session that stops at its turn or budget limit keeps what it recorded instead of failing explore."""
import pytest
from claude_agent_sdk import ResultError, ResultMessage

from forge.stages.explore import agent


class _Ledger:
    def __init__(self):
        self.calls = []

    def record_call(self, **kw):
        self.calls.append(kw)


class _Store:
    def __init__(self):
        self.ledger = _Ledger()


def _result(subtype: str) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1000, duration_api_ms=900, is_error=subtype != "success",
                         num_turns=70, session_id="s", total_cost_usd=1.25, usage={}, result=None)


def _fake_query(subtype: str, status: int | None = None, yield_result: bool = True):
    async def fake(prompt, options):
        if yield_result:
            yield _result(subtype)
        raise ResultError("Claude Code returned an error result: Reached maximum number of turns (70)",
                          {"subtype": subtype, "terminal_reason": "max_turns", "is_error": True,
                           "api_error_status": status})
    return fake


async def test_max_turns_ends_the_session_with_its_result(monkeypatch):
    monkeypatch.setattr(agent, "query", _fake_query("error_max_turns"))
    store = _Store()
    out = await agent._session(store, None, "probe", model="m", budget_usd=1, max_turns=70, label="probe:x")
    assert out.result is not None and out.result.subtype == "error_max_turns" and out.structured is None
    assert store.ledger.calls and store.ledger.calls[0]["cost_usd"] == 1.25


async def test_rejected_key_or_no_result_still_fails(monkeypatch):
    monkeypatch.setattr(agent, "query", _fake_query("success", status=401))
    with pytest.raises(ResultError):
        await agent._session(_Store(), None, "p", model="m", budget_usd=1, max_turns=5, label="breadth")
    monkeypatch.setattr(agent, "query", _fake_query("error_max_turns", yield_result=False))
    with pytest.raises(ResultError):
        await agent._session(_Store(), None, "p", model="m", budget_usd=1, max_turns=5, label="breadth")
