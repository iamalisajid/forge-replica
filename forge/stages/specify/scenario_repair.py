"""Repair a scenario that failed on the original app, from what the page offered at the failing step.

The original is the ground truth: when a scenario cannot run there, its targets or steps are wrong, not the app.
One structured call gets the scenario, the failure and the page's accessibility snapshot, and returns the scenario
with only the wrong steps changed. Validation then runs it again; nothing is accepted without passing there.
"""
from __future__ import annotations

from forge.core.ledger import Ledger
from forge.core.llm import structured_call
from forge.models.base import Strict
from forge.models.scenario import Bind, Scenario, ScenarioResult, ScenarioStep

TASK = """An acceptance scenario failed on the ORIGINAL app, so the scenario is wrong, not the app. Fix it.

Failure: step {step} ({kind}): {message}

Rules:
- Steps before the failing step ran on the original. Return restart_from = the failing step and the steps from there to the end; use an earlier restart_from only if an earlier step must change (rare).
- Keep the scenario's intent; keep every write (create, edit, delete) and the assertions that follow them.
- Change only what does not match the page: a target (exact role + accessible name as the snapshot shows it), a missing step (a menu, tab or dialog that must be opened first, a confirmation), or the order.
- Use only roles and names that appear in the snapshot or in the explorer's recorded steps. Scope ambiguous targets with within_role/within_name.
- An element without an accessible name: target it by role inside its named container (within_role/within_name), or by has_text.
- Typed values stay {{{{bind}}}} references.

## Scenario (steps numbered from 0)
{scenario}

## Page accessibility snapshot at the failing step
{snapshot}

## Steps that already ran on the original in other scenarios (reuse their targets where the flow is the same)
{proven}

## What the explorer did on this app
{steps}"""


def numbered(steps) -> str:
    return "\n".join(f"{i}: {st.model_dump_json(exclude_none=True)}" for i, st in enumerate(steps))


class ScenarioFix(Strict):
    restart_from: int                  # first step replaced; the steps before it are kept exactly as they are
    steps: list[ScenarioStep]          # replaces steps[restart_from:]
    new_binds: list[Bind]              # binds to add (usually none)


def repair_scenario(scenario: Scenario, result: ScenarioResult, *, explorer_steps: str, system: str, model: str,
                    ledger: Ledger | None, proven_steps: str = "") -> Scenario:
    content = TASK.format(step=result.failed_step, kind=result.failure_kind, message=result.message or "",
                          scenario=f"{scenario.model_dump_json(exclude={'steps'})}\nsteps:\n{numbered(scenario.steps)}",
                          snapshot=result.failure_snapshot or "(no snapshot was captured)",
                          proven=proven_steps[:10_000] or "(none yet)", steps=explorer_steps[:12_000])
    fix = structured_call(stage="specify", system=system, content=content, output=ScenarioFix, model=model,
                          ledger=ledger, effort="medium").parsed
    start = max(0, min(fix.restart_from, result.failed_step or 0))
    names = {b.name for b in scenario.binds}
    return scenario.model_copy(update={"steps": scenario.steps[:start] + fix.steps,
                                       "binds": scenario.binds + [b for b in fix.new_binds if b.name not in names]})
