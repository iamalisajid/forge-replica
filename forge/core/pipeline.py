"""The deterministic control plane: runs the stages in order, caches finished stages, resumes, and stops early.

Stage state lives in typed artifacts in the run dir; a stage is skipped on resume when its done-marker exists.
Generate runs alongside Validate (codegen only needs the spec). `--quick` keeps every stage that matters for
correctness and the UI fidelity pass, but trims cost: pilot workflow only, one validation run, no mutant check, one
builder round.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from forge.core import sessions
from forge.core.store import RunStore
from forge.models.appspec import AppSpec, op_wires
from forge.models.base import ArtifactHeader, Strict
from forge.models.observed import ObservedModel
from forge.models.run import CodegenManifest, RunConfig
from forge.models.scenario import Scenario, ScenarioResult
from forge.models.trace import Trace
from forge.models.verification import NegativeControl, Verification

STAGES = ["intake", "explore", "extract", "specify", "validate", "generate", "negative", "build", "verify", "deliver"]


# Artifacts each stage writes; a run that reuses another run's stages copies these.
STAGE_ARTIFACTS = {
    "intake": ["feasibility"], "explore": ["explore", "trace"], "extract": ["observed"], "specify": ["appspec"],
    "validate": ["oracle"], "generate": ["codegen"], "negative": ["negative"], "build": ["build"],
    "verify": ["verification"], "deliver": ["delivery"],
}
REUSABLE_FROM = ("explore", "extract", "specify", "validate", "generate")   # later stages need this run's own repo


def reuse_run(store: RunStore, source_id: str, from_stage: str, *, runs_dir: Path | None = None) -> list[str]:
    """Start ``store``'s run at ``from_stage`` with the earlier stages' artifacts and evidence from run
    ``source_id`` of the same app: the app has not changed, so exploring, specifying and validating again would only
    repeat the work. The new run still gets its own repository. Returns the reused stages."""
    if from_stage not in REUSABLE_FROM:
        raise ValueError(f"--from must be one of {', '.join(REUSABLE_FROM)} (later stages need the run's own repo)")
    source = RunStore.open(source_id, **({"runs_dir": runs_dir} if runs_dir else {}))
    from forge.core.store import site_of

    if site_of(source.config.url) != site_of(store.config.url):
        raise ValueError(f"run {source_id} explored {source.config.url}, not {store.config.url}")
    reused = STAGES[:STAGES.index(from_stage)]
    missing = [s for s in reused if not (source.dir / "stages" / f"{s}.done").exists()]
    if missing:
        raise ValueError(f"run {source_id} has not finished {', '.join(missing)}")
    (store.dir / "stages").mkdir(exist_ok=True)
    for stage in reused:
        for name in STAGE_ARTIFACTS[stage]:
            src = source.dir / "artifacts" / f"{name}.json"
            if src.exists():
                shutil.copy2(src, store.dir / "artifacts" / src.name)
        (store.dir / "stages" / f"{stage}.done").write_text(f"reused from {source_id}")
    for blob in (source.dir / "evidence").iterdir():      # content-addressed: link, do not copy
        target = store.dir / "evidence" / blob.name
        if not target.exists():
            try:
                os.link(blob, target)
            except OSError:
                shutil.copy2(blob, target)
    return reused


class StopRun(RuntimeError):
    pass


class OracleReport(Strict):
    header: ArtifactHeader
    runs: int
    results: list[ScenarioResult]
    candidates: list[str]
    env_failures: list[str]


class NegativeReport(Strict):
    header: ArtifactHeader
    controls: list[NegativeControl]
    protected: list[str]
    skipped: bool


class Pipeline:
    def __init__(self, store: RunStore, *, quick: bool, log: Callable[[str], None] = print):
        self.store = store
        self.config: RunConfig = store.config
        self.quick = quick
        self.log = log
        self.storage_state = sessions.load(self.config.origin)
        (store.dir / "stages").mkdir(exist_ok=True)

    # ---------- orchestration ----------
    def _marker(self, stage: str) -> Path:
        return self.store.dir / "stages" / f"{stage}.done"

    def done(self, stage: str) -> bool:
        return self._marker(stage).exists()

    def _skip_reason(self, stage: str) -> str | None:
        if stage == "negative" and self.quick:
            return "quick run: no mutant check (protected = validated candidates)"
        if stage == "build" and self.config.no_build:
            return "builder skipped (--no-build): the generated baseline is the candidate"
        return None

    async def run(self, from_stage: str | None = None) -> None:
        if from_stage:
            for stage in STAGES[STAGES.index(from_stage):]:
                self._marker(stage).unlink(missing_ok=True)
        for stage in STAGES:
            if self.done(stage):
                self.log(f"  {stage:<9} cached")
                continue
            reason = self._skip_reason(stage)
            if reason:
                self.log(f"  {stage:<9} skipped — {reason}")
                self._marker(stage).write_text("skipped")
                continue
            started = time.time()
            row = self.store.ledger.stage_started(stage, None)
            self.log(f"▶ {stage}")
            try:
                await getattr(self, f"_{stage}")()
            except StopRun as exc:
                self.store.ledger.stage_finished(row, "stopped", str(exc))
                self.log(f"■ stopped at {stage}: {exc}")
                return
            except Exception as exc:
                self.store.ledger.stage_finished(row, "failed", repr(exc)[:2000])
                self.log(f"✗ {stage} failed: {exc}")
                raise
            self.store.ledger.stage_finished(row, "ok")
            self._marker(stage).write_text(f"{time.time() - started:.1f}s")
            self.log(f"  {stage:<9} done in {time.time() - started:.0f}s  (spent ${self.store.ledger.spent():.2f})")

    def header(self, schema: str, stage: str) -> ArtifactHeader:
        return ArtifactHeader(schema_id=schema, run_id=self.config.run_id, stage=stage, model=self.config.model)

    # ---------- stages ----------
    async def _intake(self) -> None:
        from forge.stages.intake import preflight

        feasibility = await preflight(self.store, self.config.url, storage_state=self.storage_state,
                                      run_id=self.config.run_id)
        shares = ", ".join(f"{p.protocol} {p.share:.0%}" for p in feasibility.protocols) or "no API traffic seen"
        self.log(f"  reachable={feasibility.reachable} login_wall={feasibility.login_wall} protocols: {shares}")
        if feasibility.login_wall and self.storage_state is None:
            self.log("  note: the app shows a login form; run `forge login <url>` first to explore signed in")
        if feasibility.stop:
            raise StopRun(feasibility.reason or "out of scope")

    async def _explore(self) -> None:
        from forge.stages.explore.agent import explore

        _, result = await explore(self.store, self.config, quick=self.quick or self.config.pilot_only,
                                  storage_state=self.storage_state)
        for line in result.sessions:
            self.log(f"  {line}")
        if result.cleanup:
            self.log(f"  cleanup on the original: {len(result.cleanup)} records")

    async def _extract(self) -> None:
        from forge.stages.extract.rest import extract

        trace = self.store.read("trace", Trace)
        model = extract(self.store, trace, self.config.run_id)
        if any("RPC/GraphQL" in u for u in model.unknowns):
            from forge.stages.extract.batched import adapt

            options = {o.split(":", 1)[0]: [v.strip() for v in o.split(":", 1)[1].split("|")]
                       for o in model.select_options if ":" in o}
            model = adapt(trace, model, options)
            self.store.write("observed", model)
            self.log(f"  batched-command adapter: {model.unknowns[-1]}")
        if any("RPC/GraphQL" in u for u in model.unknowns):
            from forge.stages.extract.interpreter import interpret

            model, rejected = interpret(self.store, trace, model, model=self.config.model)
            self.log(f"  interpreter: non-REST traffic interpreted, {rejected} unverifiable citations rejected")
        self.log(f"  {len(model.entities)} entities, {len(model.operations)} operations, {len(model.routes)} routes"
                 + (f", {len(model.unknowns)} unparsed exchanges" if model.unknowns else ""))

    async def _specify(self) -> None:
        from forge.stages.explore.agent import ExploreResult
        from forge.stages.specify.specify import specify

        observed = self.store.read("observed", ObservedModel)
        trace = self.store.read("trace", Trace)
        explore = self.store.read("explore", ExploreResult)
        pilot = explore.inventory.pilot_feature if explore.inventory else None
        spec = specify(self.store, observed, trace, run_id=self.config.run_id, model=self.config.model,
                       features=self.config.features,
                       pilot_feature=pilot).spec
        self.log(f"  {spec.app_name}: {len(spec.entities)} entities, {len(spec.operations)} operations, "
                 f"{len(spec.screens)} screens, {len(spec.elements)} elements, {len(spec.scenarios)} scenarios")
        from forge.coverage import coverage, summary_lines

        for line in summary_lines(coverage(self.store)):    # what will be built, and what was only seen
            self.log(line)

    def _scenarios(self) -> list[Scenario]:
        spec = self.store.read("appspec", AppSpec).spec
        if self.quick or self.config.pilot_only:
            ids = {spec.pilot_scenario}
            if self.store.has("oracle"):       # quick fallback: other scenarios that passed when the pilot could not
                ids |= set(self.store.read("oracle", OracleReport).candidates)
            return [s for s in spec.scenarios if s.id in ids]
        return spec.scenarios

    async def _validate(self) -> None:
        from forge.stages.oracle.validate import validate_on_source

        appspec = self.store.read("appspec", AppSpec)
        spec = appspec.spec
        runs = 1 if self.quick else 3
        narrow = self.quick or self.config.pilot_only
        scenarios = [s for s in spec.scenarios if s.id == spec.pilot_scenario] if narrow else spec.scenarios

        async def run(batch: list[Scenario]):
            # Routes in the spec are absolute paths, so scenarios resolve them against the origin, not the start URL.
            return await call(validate_on_source, batch, op_wires(spec), self.config.origin, runs=runs,
                              storage_state=self.storage_state)

        results, candidates = await run(scenarios)
        proven: dict[str, str] = {}
        spec, results, candidates = await self._heal(spec, scenarios, results, candidates, run, rounds=6,
                                                     proven=proven)
        if narrow and not candidates and len(spec.scenarios) > 1:
            others = [s for s in spec.scenarios if s.id != spec.pilot_scenario]
            self.log(f"  the pilot did not pass on the original; trying the other {len(others)} scenarios")
            more, more_ok = await run(others)
            spec, more, more_ok = await self._heal(spec, others, more, more_ok, run, rounds=4, proven=proven)
            results, candidates = results + more, candidates + more_ok
        if spec != appspec.spec:
            self.store.write("appspec", appspec.model_copy(update={"spec": spec}))
        env = sorted({r.scenario_id for r in results if r.failure_kind == "env"})
        report = OracleReport(header=self.header("forge.oracle/1", "validate"), runs=runs, results=results,
                              candidates=candidates, env_failures=env)
        self.store.write("oracle", report)
        failed = sorted({r.scenario_id for r in results if not r.passed} - set(env) - set(candidates))
        self.log(f"  {len(candidates)} candidates from {len({r.scenario_id for r in results})} scenarios × {runs} runs"
                 + (f"; failed on the original: {', '.join(failed)}" if failed else "")
                 + (f"; environment failures: {', '.join(env)}" if env else ""))
        if not self.done("generate"):              # after any repair, so the repo is rendered from the final spec
            await asyncio.to_thread(self._generate_sync)
            self._marker("generate").write_text("with validate")
        if not candidates:
            raise StopRun("no scenario passed on the original; nothing can be verified (see oracle.json)")

    async def _heal(self, spec, scenarios: list[Scenario], results: list[ScenarioResult], candidates: list[str],
                    run, *, rounds: int, proven: dict[str, str]):
        """A scenario that fails on the original at an action or assertion is wrong, not the app. Repair it from the
        page snapshot at the failing step, keep the repair only if the spec still checks, and validate it again.
        Each round fixes what the run reached; rounds continue while a scenario gets further, up to ``rounds``.
        ``proven`` collects steps that ran on the original, shared with the repairs of other scenarios."""
        from forge.stages.specify.scenario_repair import numbered, repair_scenario
        from forge.stages.specify.specify import PROMPT_FILE, _steps_context, check

        explorer_steps = _steps_context(self.store.read("trace", Trace))
        system = PROMPT_FILE.read_text()
        stalled: set[str] = set()
        best = {s.id: s for s in scenarios}       # furthest-reaching version of each scenario so far
        for round_no in range(1, rounds + 1):
            last = {r.scenario_id: r for r in results if not r.passed}
            broken = [(s, last[s.id]) for s in scenarios
                      if s.id in last and s.id not in candidates and s.id not in stalled
                      and last[s.id].failure_kind in ("action", "assertion")]
            if not broken:
                break
            fixed: list[Scenario] = []
            for sc, result in broken:
                others = "\n\n".join(f"{k}:\n{v}" for k, v in proven.items() if k != sc.id)
                try:
                    new = await asyncio.to_thread(repair_scenario, sc, result, explorer_steps=explorer_steps,
                                                  system=system, model=self.config.model, ledger=self.store.ledger,
                                                  proven_steps=others)
                except Exception as exc:  # noqa: BLE001 - a failed repair leaves the scenario as it was
                    self.log(f"  could not repair {sc.id}: {exc}")
                    stalled.add(sc.id)
                    continue
                trial = spec.model_copy(update={"scenarios": [new if x.id == sc.id else x for x in spec.scenarios]})
                problems = [e for e in check(trial) if sc.id in e]
                if problems:
                    self.log(f"  repair of {sc.id} rejected: {problems[0]}")
                    stalled.add(sc.id)
                    continue
                spec, fixed = trial, fixed + [new]
            if not fixed:
                break
            again, again_ok = await run(fixed)
            ids = {f.id for f in fixed}
            results = [r for r in results if r.scenario_id not in ids] + again
            candidates = [c for c in candidates if c not in ids] + again_ok
            scenarios = [next((f for f in fixed if f.id == s.id), s) for s in scenarios]
            outcome = {r.scenario_id: r for r in again}
            appspec = self.store.read("appspec", AppSpec)        # keep each round's progress if the run stops here
            for f in fixed:
                now, before = outcome[f.id], last[f.id]
                reached = len(f.steps) if f.id in again_ok else (now.failed_step or 0)
                if f.id not in again_ok and reached < (before.failed_step or 0):
                    # Got less far than the version before: undo this repair and stop spending on it.
                    prior = best[f.id]
                    spec = spec.model_copy(update={"scenarios": [prior if x.id == f.id else x for x in spec.scenarios]})
                    results = [r for r in results if r.scenario_id != f.id] + [before]
                    scenarios = [prior if s.id == f.id else s for s in scenarios]
                    stalled.add(f.id)
                    continue
                best[f.id] = f
                proven[f.id] = numbered(f.steps[:reached])
                if f.id not in again_ok and ((now.failed_step or 0) <= (before.failed_step or 0)
                                             or now.message == before.message):
                    stalled.add(f.id)          # no further than last time: stop spending on it
            self.store.write("appspec", appspec.model_copy(update={"spec": spec}))
            self.log(f"  repaired on the original (round {round_no}): " + "; ".join(
                f"{i} passes" if i in again_ok else f"{i} still fails at step {outcome[i].failed_step}: "
                f"{(outcome[i].message or '')[:120]}" for i in sorted(ids)))
        return spec, results, candidates

    def _generate_sync(self) -> CodegenManifest:
        from forge.stages.generate.codegen import generate

        spec = self.store.read("appspec", AppSpec).spec
        if self.store.has("codegen"):
            # Resumed before generate: this run's earlier repo is replaced, so the run keeps exactly one repo.
            old = Path(self.store.read("codegen", CodegenManifest).repo)
            if (old.is_dir() and old.resolve().parent == Path(self.config.out_root).resolve()
                    and old.name.endswith(self.config.run_id)):
                shutil.rmtree(old)
                self.log(f"  replaced this run's earlier repo {old}")
        manifest = generate(spec, Path(self.config.out_root), self.config.run_id)
        self.store.write("codegen", manifest)
        self.log(f"  generated repo {manifest.repo} at {manifest.commit[:10]}")
        return manifest

    async def _generate(self) -> None:
        await asyncio.to_thread(self._generate_sync)

    async def _negative(self) -> None:
        from forge.stages.oracle.negative import negative_control

        spec = self.store.read("appspec", AppSpec).spec
        manifest = self.store.read("codegen", CodegenManifest)
        oracle = self.store.read("oracle", OracleReport)
        candidates = [s for s in self._scenarios() if s.id in oracle.candidates]
        controls = await call(negative_control, Path(manifest.repo), self.config.run_id, candidates, op_wires(spec))
        protected = [c.scenario_id for c in controls if c.protected]
        self.store.write("negative", NegativeReport(header=self.header("forge.negative/1", "negative"),
                                                    controls=controls, protected=protected, skipped=False))
        self.log(f"  protected {len(protected)}/{len(candidates)} (each failed on the rollback mutant after its first write)")
        if not protected:
            raise StopRun("no candidate failed on the mutant at an assertion; the suite cannot detect missing writes")

    def _protected(self) -> list[Scenario]:
        oracle = self.store.read("oracle", OracleReport)
        ids = set(oracle.candidates)
        if self.store.has("negative"):
            ids = set(self.store.read("negative", NegativeReport).protected)
        return [s for s in self._scenarios() if s.id in ids]

    async def _build(self) -> None:
        from forge.stages.build.agent import run_builder
        from forge.stages.build.checks import run_checks

        spec = self.store.read("appspec", AppSpec).spec
        manifest = self.store.read("codegen", CodegenManifest)
        repo = Path(manifest.repo)
        context_dir, screenshots = self._builder_context(spec)
        protected = self._protected()
        # The generated baseline rarely has the original's structure yet: run the scenarios the replica must pass
        # on it first, so the builder starts from their real failures (step, message) instead of guessing.
        baseline = await asyncio.to_thread(run_checks, repo, "scenarios", [s.id for s in protected],
                                           run_id=self.config.run_id, scenarios=protected,
                                           operations=op_wires(spec))
        failing = [r for r in baseline.results if not r.passed]
        self.log(f"  baseline: {baseline.status}; " + ("; ".join(
            f"{r.scenario_id} fails at step {r.failed_step}: {(r.message or '')[:100]}" for r in failing)
            or "the protected scenarios pass"))
        started = time.time()
        report = await run_builder(repo, spec, manifest, context_dir=context_dir, screenshots=screenshots,
                                   failing=failing, scenarios=protected, run_id=self.config.run_id,
                                   budget_usd=max(1.0, (self.store.ledger.remaining() or 10) * 0.6),
                                   model=self.config.model, rounds=2)
        data = report.model_dump(mode="json") if hasattr(report, "model_dump") else report.__dict__
        (self.store.dir / "artifacts" / "build.json").write_text(json.dumps(data, indent=2, default=str))
        self.store.ledger.record_call(stage="build", kind="agent", model=self.config.model, usage={},
                                      cost_usd=float(data.get("cost_usd") or 0), duration_s=time.time() - started,
                                      detail={"rounds": len(data.get("rounds") or [])})
        checks = ", ".join(f"{c['kind']} {c['status']}" for c in data.get("checks") or [])
        self.log(f"  builder: {len(data.get('rounds') or [])} round(s), ${float(data.get('cost_usd') or 0):.2f}, "
                 f"green={data.get('green')}; checks: {checks}"
                 + (f"; stopped: {data.get('terminal_reason')}" if data.get("terminal_reason") else ""))

    def _builder_context(self, spec) -> tuple[Path, dict[str, Path]]:
        """Curated, redacted context for the builder (never the raw run dir) plus original screenshots per screen."""
        context = self.store.dir / "builder-context"
        context.mkdir(exist_ok=True)
        (context / "app-spec.json").write_text(spec.model_dump_json(indent=2))
        trace = self.store.read("trace", Trace)
        screenshots: dict[str, Path] = {}
        for screen in spec.screens:
            pattern = "^" + re.sub(r":[a-z_]+", "[^/]+", screen.route.rstrip("/") or "/") + "/?$"
            for step in reversed(trace.steps):
                path = re.sub(r"^https?://[^/]+", "", step.url_after).split("?")[0] or "/"
                if step.screenshot and re.match(pattern, path):
                    screenshots.setdefault(screen.id, self.store.blob_path(step.screenshot))
                    if step.snapshot_after and not (context / f"{screen.id}.snapshot.txt").exists():
                        (context / f"{screen.id}.snapshot.txt").write_text(
                            self.store.blob_path(step.snapshot_after).read_text())
                    if step.dom and not (context / f"{screen.id}.dom.html").exists():
                        (context / f"{screen.id}.dom.html").write_text(self.store.blob_path(step.dom).read_text())
                        (context / f"{screen.id}.styles.json").write_text(
                            self.store.blob_path(step.styles).read_text())
                    if (context / f"{screen.id}.dom.html").exists() and screen.id in screenshots:
                        break
        return context, screenshots

    async def _verify(self) -> None:
        from forge.stages.verify.report import verify

        spec = self.store.read("appspec", AppSpec).spec
        manifest = self.store.read("codegen", CodegenManifest)
        from forge.core import gitrepo

        sha = gitrepo.head_sha(Path(manifest.repo))
        heldout = _load_scenarios(self.config.heldout_dir)
        controls = self.store.read("negative", NegativeReport).controls if self.store.has("negative") else None
        verification = await call(verify, Path(manifest.repo), sha, spec, manifest, self._protected(), heldout,
                                  self.config.run_id, self.header("forge.verification/1", "verify"),
                                  source_url=self.config.url, prior_controls=controls,
                                  cost_usd=self.store.ledger.spent())
        verification.cost_usd = self.store.ledger.spent()
        if self.quick:
            verification.limitations.append("quick run: pilot only, 1 validation run, no mutant check, 1 builder round")
        self.store.write("verification", verification)
        for w in verification.workflows:
            self.log(f"  {w.status:<8} {w.scenario_id}{' (held-out)' if w.held_out else ''}")
        for c in verification.checks:
            self.log(f"  check {c.name}: {c.status}")
        self.log(f"  overall: {verification.overall}")

    async def _deliver(self) -> None:
        from forge.stages.deliver import deliver

        spec = self.store.read("appspec", AppSpec).spec
        manifest = self.store.read("codegen", CodegenManifest)
        verification = self.store.read("verification", Verification)
        from forge.coverage import coverage, markdown

        commit = deliver(Path(manifest.repo), self.config, spec, verification, quick=self.quick,
                         extra=markdown(coverage(self.store)))
        (self.store.dir / "artifacts" / "delivery.json").write_text(json.dumps(
            {"repo": manifest.repo, "commit": commit, "overall": verification.overall}, indent=2))
        port = next((l.split("=", 1)[1] for l in (Path(manifest.repo) / ".env").read_text().splitlines()
                     if l.startswith("PORT=")), "3000")
        self.log(f"  repo: {manifest.repo}")
        self.log(f"  original: {self.config.url}")
        self.log(f"  replica:  cd {manifest.repo} && docker compose up --build   → http://127.0.0.1:{port}")


def _load_scenarios(directory: str | None) -> list[Scenario]:
    if not directory:
        return []
    return [Scenario.model_validate_json(p.read_text()) for p in sorted(Path(directory).glob("*.json"))]


async def _maybe_async(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def call(fn, *args, **kwargs):
    """Await async stage functions; run sync ones (which may start their own event loop) in a thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)
