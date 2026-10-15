"""Scripted-driver test of the explore harness against the running demo (no model involved)."""
import json

import pytest

from forge.core.store import RunStore
from forge.stages.explore import locators
from forge.stages.explore.harness import Harness

DEMO = "http://127.0.0.1:4001/"
pytestmark = pytest.mark.docker


def ref_for(snapshot: str, role: str, name: str) -> str:
    for ref, node in locators.parse(snapshot).items():
        if node.role == role and node.name == name:
            return ref
    raise AssertionError(f"{role} {name!r} not in snapshot")


async def test_scripted_pilot_records_correlated_writes(tmp_path):
    store, config = RunStore.create(DEMO, mutations="test-account", runs_dir=tmp_path / "runs", out_root=tmp_path / "apps")
    h = Harness(store, url=DEMO, mutations="test-account")
    await h.start()
    try:
        await h.navigate(DEMO)
        snap, _ = await h.observe()
        await h.act(ref_for(snap, "button", "New project"), "click")
        snap, _ = await h.observe()
        await h.act(ref_for(snap, "textbox", "Name"), "fill", "Harness project")
        snap, _ = await h.observe()
        out = await h.act(ref_for(snap, "button", "Create project"), "click")
        assert "POST /api/projects -> 201" in out
        cleanup = await h.cleanup()
    finally:
        await h.close()
    create = next(x for x in h.exchanges if x.method == "POST")
    assert create.correlated_by == "nonce"
    fill = next(s for s in h.steps if s.action == "fill")
    assert fill.nonce and fill.nonce in json.loads(create.request_body)["name"]
    click = h.steps[-1]
    assert click.target.role == "button" and click.target.name == "Create project" and click.portable
    assert h.created and h.created[0].deleted, cleanup
    assert (store.dir / "evidence" / "session.har").exists()


async def test_read_only_policy_blocks_writes(tmp_path):
    store, _ = RunStore.create(DEMO, runs_dir=tmp_path / "runs", out_root=tmp_path / "apps")
    h = Harness(store, url=DEMO, mutations="read-only")
    await h.start()
    try:
        await h.navigate(DEMO)
        snap, _ = await h.observe()
        await h.act(ref_for(snap, "button", "New project"), "click")
        snap, _ = await h.observe()
        await h.act(ref_for(snap, "textbox", "Name"), "fill", "Blocked")
        snap, _ = await h.observe()
        out = await h.act(ref_for(snap, "button", "Create project"), "click")
    finally:
        await h.close()
    assert "Blocked by policy: write POST /api/projects" in out
    assert not any(x.method == "POST" for x in h.exchanges)


async def test_duplicate_buttons_get_scoped_targets(tmp_path):
    store, _ = RunStore.create(DEMO, mutations="test-account", runs_dir=tmp_path / "runs", out_root=tmp_path / "apps")
    h = Harness(store, url=DEMO, mutations="test-account")
    await h.start()
    try:
        for name in ("Alpha scope", "Beta scope"):
            await h.navigate(DEMO)
            snap, _ = await h.observe()
            await h.act(ref_for(snap, "button", "New project"), "click")
            snap, _ = await h.observe()
            await h.act(ref_for(snap, "textbox", "Name"), "fill", name)
            snap, _ = await h.observe()
            await h.act(ref_for(snap, "button", "Create project"), "click")
        snap, _ = await h.observe()
        nodes = locators.parse(snap)
        delete_refs = [r for r, n in nodes.items() if n.role == "button" and n.name == "Delete"]
        assert len(delete_refs) >= 2
        target, portable = await locators.portable_target(h.page, snap, delete_refs[0])
        assert portable and target.within_role == "listitem" and target.within_has_text
        await h.cleanup()
    finally:
        await h.close()
