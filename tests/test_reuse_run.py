"""A new run can start at a later stage with an earlier run's artifacts and evidence (same app, own repo)."""
import pytest

from forge.core.pipeline import reuse_run
from forge.core.store import RunStore


def _finished(tmp_path, url):
    src, _ = RunStore.create(url, runs_dir=tmp_path)
    for name in ("feasibility", "explore", "trace", "observed", "appspec", "oracle", "codegen"):
        (src.dir / "artifacts" / f"{name}.json").write_text("{}")
    (src.dir / "stages").mkdir()
    for stage in ("intake", "explore", "extract", "specify", "validate", "generate"):
        (src.dir / "stages" / f"{stage}.done").write_text("1s")
    (src.dir / "evidence" / "ab12.png").write_bytes(b"png")
    return src


def test_reuse_copies_stages_before_from_and_links_evidence(tmp_path):
    src = _finished(tmp_path, "https://app.example.com/app/inbox")
    new, _ = RunStore.create("https://app.example.com/app/inbox", runs_dir=tmp_path)
    reused = reuse_run(new, src.dir.name, "generate", runs_dir=tmp_path)
    assert reused == ["intake", "explore", "extract", "specify", "validate"]
    assert (new.dir / "artifacts" / "oracle.json").exists() and not (new.dir / "artifacts" / "codegen.json").exists()
    assert (new.dir / "stages" / "validate.done").read_text() == f"reused from {src.dir.name}"
    assert not (new.dir / "stages" / "generate.done").exists()
    assert (new.dir / "evidence" / "ab12.png").read_bytes() == b"png"


def test_reuse_refuses_another_app_later_stages_and_unfinished_runs(tmp_path):
    src = _finished(tmp_path, "https://app.example.com/")
    other, _ = RunStore.create("https://other.example.org/", runs_dir=tmp_path)
    with pytest.raises(ValueError, match="explored"):
        reuse_run(other, src.dir.name, "generate", runs_dir=tmp_path)
    same, _ = RunStore.create("https://app.example.com/", runs_dir=tmp_path)
    with pytest.raises(ValueError, match="own repo"):
        reuse_run(same, src.dir.name, "build", runs_dir=tmp_path)
    (src.dir / "stages" / "validate.done").unlink()
    with pytest.raises(ValueError, match="has not finished validate"):
        reuse_run(same, src.dir.name, "generate", runs_dir=tmp_path)
