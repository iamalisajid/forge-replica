"""forge CLI: doctor | login | run | resume | report | cleanup | bundle | purge."""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Optional

import typer

from forge.core.config import APPS_ROOT, DEFAULT_MODEL, RUNS_DIR, load_env_file

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Turn a web app URL into an independently runnable full-stack replica, verified against the original.")


@app.callback()
def _setup() -> None:
    load_env_file()


@app.command()
def doctor(fix: bool = typer.Option(True, help="Pull and build missing images.")) -> None:
    """Check this machine: Docker, images, browser, API key, and that the verify network has no egress."""
    from forge.stages.intake import doctor as run_doctor

    checks = run_doctor(fix=fix)
    for c in checks:
        typer.echo(f"{'✓' if c.ok else '✗'} {c.name:<36} {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def login(url: str,
          playwright: bool = typer.Option(False, "--playwright", help="Use Playwright's Chromium instead of your Google Chrome."),
          email: Optional[str] = typer.Option(None, help="With --playwright: fill a standard login form automatically."),
          password: Optional[str] = typer.Option(None),
          headless: bool = typer.Option(False)) -> None:
    """Sign in once; the session is stored encrypted under ~/.forge/sessions.

    By default your real Google Chrome opens normally (CAPTCHAs and SSO work as usual); forge only reads the
    session afterwards. --playwright uses the automation browser (fine for local test apps)."""
    from forge.core.sessions import find_chrome, login as do_login, login_with_chrome

    if not playwright and find_chrome():
        state = asyncio.run(login_with_chrome(url))
    else:
        state = asyncio.run(do_login(url, email=email, password=password, headless=headless))
    typer.echo(f"saved session for {url} ({len(state.get('cookies', []))} cookies, "
               f"{len(state.get('origins', []))} origins with storage)")


@app.command()
def run(
    url: str,
    quick: bool = typer.Option(False, "--quick", help="Fast path: pilot workflow only, 1 validation run, no mutant check; the builder still matches the UI (1 round)."),
    mutations: str = typer.Option("read-only", help="read-only | test-account (allow creating/editing/deleting records on the original)."),
    budget_usd: float = typer.Option(15.0, help="Hard cap on model spend for this run."),
    model: str = typer.Option(DEFAULT_MODEL),
    heldout: Optional[Path] = typer.Option(None, help="Directory of held-out scenario JSON files, run only at Verify."),
    out: Path = typer.Option(APPS_ROOT, help="Where the new git repository is created."),
    pilot_only: bool = typer.Option(False, help="Stop after the pilot workflow (full rigor, one workflow)."),
    no_build: bool = typer.Option(False, help="Skip the builder agent; the generated baseline is verified as is."),
    watch: bool = typer.Option(False, "--watch", help="Show the browser while the agent explores and while scenarios run on the original."),
    features: str = typer.Option("crud", "--features", help="What to replicate, comma-separated: crud, search. Forge finds where each one lives in the app; you do not point it at pages."),
    reuse: Optional[str] = typer.Option(None, "--reuse", help="Reuse the stages before --from from this earlier run of the same app (its exploration, spec and validated scenarios); the new run still gets its own repo."),
    from_stage: str = typer.Option("generate", "--from", help="With --reuse: the first stage to run (explore, extract, specify, validate or generate)."),
) -> None:
    """Explore URL, specify, prove on the original, generate a new repo, build, verify and deliver."""
    from forge.core.pipeline import Pipeline, reuse_run
    from forge.core.store import RunStore

    if watch:
        import os

        os.environ["FORGE_HEADED"] = "1"
    if mutations not in ("read-only", "test-account"):
        raise typer.BadParameter("mutations must be read-only or test-account")
    from forge.models.run import KNOWN_FEATURES

    wanted = sorted({f.strip().lower() for f in features.split(",") if f.strip()} | {"crud"})
    unknown = [f for f in wanted if f not in KNOWN_FEATURES]
    if unknown:
        raise typer.BadParameter(f"unknown feature(s) {', '.join(unknown)}; known: {', '.join(KNOWN_FEATURES)}")
    store, config = RunStore.create(url, mutations=mutations, budget_usd=budget_usd, model=model,
                                    pilot_only=pilot_only, no_build=no_build,
                                    heldout_dir=str(heldout) if heldout else None, out_root=out,
                                    features=wanted)
    (store.dir / "artifacts" / "mode.txt").write_text("quick" if quick else "full")
    typer.echo(f"run {config.run_id} ({'quick' if quick else 'full'}) → {config.url}\n  state: {store.dir}"
               f"\n  features: {', '.join(config.features)}")
    if reuse:
        try:
            reused = reuse_run(store, reuse, from_stage)
        except (ValueError, FileNotFoundError) as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo(f"  reusing {', '.join(reused)} from run {reuse}; starting at {from_stage}")
    started = time.time()
    asyncio.run(Pipeline(store, quick=quick, log=_logger(store)).run())
    typer.echo(f"finished in {(time.time() - started) / 60:.1f} min, spent ${store.ledger.spent():.2f}")


def _logger(store):
    """Print each pipeline line and keep it in the run directory, where `forge watch` reads it."""
    path = store.dir / "run.log"

    def log(line: str) -> None:
        typer.echo(line)
        with path.open("a") as fh:
            fh.write(line + "\n")
    return log


@app.command()
def coverage(run_id: str) -> None:
    """What the run recorded on the original, what it builds, and what it saw but leaves out."""
    from forge.core.store import RunStore
    from forge.coverage import print_report

    print_report(RunStore.open(run_id), echo=typer.echo)


@app.command()
def watch(run_id: str, port: int = typer.Option(8790, help="Local port for the progress page."),
          log: Optional[Path] = typer.Option(None, help="Log file to show (default: the run's run.log).")) -> None:
    """Live progress of a run in the browser: stages, time, cost, log, and the builder's changes."""
    from forge.watch import serve

    typer.echo(f"forge watch: http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    serve(run_id, port, log)


@app.command()
def resume(run_id: str, from_stage: Optional[str] = typer.Option(None, "--from"),
           watch: bool = typer.Option(False, "--watch", help="Show the browser on the host.")) -> None:
    """Continue a run; finished stages are reused. --from STAGE reruns that stage and everything after it."""
    if watch:
        import os

        os.environ["FORGE_HEADED"] = "1"
    from forge.core.pipeline import STAGES, Pipeline
    from forge.core.store import RunStore

    if from_stage and from_stage not in STAGES:
        raise typer.BadParameter(f"stage must be one of {', '.join(STAGES)}")
    store = RunStore.open(run_id)
    quick = (store.dir / "artifacts" / "mode.txt").read_text().strip() == "quick" \
        if (store.dir / "artifacts" / "mode.txt").exists() else False
    asyncio.run(Pipeline(store, quick=quick, log=_logger(store)).run(from_stage))


@app.command()
def report(run_id: str) -> None:
    """Stages, model spend per stage, and the verification result of a run."""
    import json

    from forge.core.store import RunStore

    store = RunStore.open(run_id)
    config = store.config
    typer.echo(f"run {run_id}: {config.url} (budget ${config.budget_usd:.2f}, spent ${store.ledger.spent():.2f})")
    for stage, status, started, finished, detail in store.ledger.stages():
        took = f"{finished - started:.0f}s" if finished else "-"
        typer.echo(f"  {stage:<9} {status:<8} {took:>6}  {(detail or '')[:100]}")
    for stage, cost, calls in store.ledger.cost_by_stage():
        typer.echo(f"  cost {stage:<9} ${cost:.2f} over {calls} call(s)")
    path = store.artifact_path("verification")
    if path.exists():
        v = json.loads(path.read_text())
        typer.echo(f"verification: {v['overall']} at {v['commit'][:12]} in {v['repo']}")
        for w in v["workflows"]:
            typer.echo(f"  {w['status']:<8} {w['scenario_id']}{' (held-out)' if w['held_out'] else ''}")


@app.command()
def cleanup(run_id: str) -> None:
    """Delete records this run created on the original that are still there (from the write-ahead ledger)."""
    import httpx

    from forge.core.sessions import load
    from forge.core.store import RunStore
    from forge.models.trace import Trace

    store = RunStore.open(run_id)
    trace = store.read("trace", Trace)
    state = load(trace.origin) or {}
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
    with httpx.Client(base_url=trace.origin, cookies=cookies, headers={"Origin": trace.origin}) as client:
        for record in reversed(trace.created):
            if record.deleted:
                continue
            path = f"{record.path.rstrip('/')}/{record.id_value}"
            status = client.delete(path).status_code
            typer.echo(f"DELETE {path} -> {status}, now GET -> {client.get(path).status_code}")


@app.command()
def bundle(run_id: str, out: Optional[Path] = typer.Option(None)) -> None:
    """Write a git bundle of the run's generated repository (git clone the file to restore it)."""
    from forge.core import gitrepo
    from forge.core.store import RunStore
    from forge.models.run import CodegenManifest

    store = RunStore.open(run_id)
    repo = Path(store.read("codegen", CodegenManifest).repo)
    target = (out or Path(f"{repo.name}.bundle")).resolve()     # git runs inside the repo; pass an absolute path
    gitrepo.bundle(repo, target)
    typer.echo(f"wrote {target}")


@app.command()
def purge(run_id: Optional[str] = typer.Argument(None), older_than_days: Optional[int] = typer.Option(None)) -> None:
    """Delete run state (evidence, traffic, ledger) for one run or for runs older than N days."""
    if run_id:
        shutil.rmtree(RUNS_DIR / run_id, ignore_errors=False)
        typer.echo(f"deleted {RUNS_DIR / run_id}")
        return
    if older_than_days is None:
        raise typer.BadParameter("give a run id or --older-than-days")
    cutoff = time.time() - older_than_days * 86400
    for path in RUNS_DIR.glob("*"):
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path)
            typer.echo(f"deleted {path}")


if __name__ == "__main__":
    app()
