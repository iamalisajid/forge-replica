# forge

Turn a web app URL into an independently runnable full-stack replica — frontend, backend, database and
migrations — and prove it by running the same scenarios on the original and on the replica, with the replica
cut off from the original.

```sh
forge doctor                                   # machine check (Docker, images, browser, API key, egress)
forge run http://127.0.0.1:4001 --mutations test-account --heldout demo/issue-tracker/heldout
forge run <url> --mutations test-account --quick --features crud,search   # fast path, named features
forge watch <run-id>                           # live progress page on http://127.0.0.1:8790
forge coverage <run-id>                        # what was recorded, built, validated and verified
forge report <run-id>
```

Every run creates a **new git repository** under `~/forge-apps/<app>-<run-id>/` that runs with
`docker compose up --build`. Run state (traffic, screenshots, ledger) stays in `~/.forge/runs/<run-id>/`,
outside every repository.

## How it works

A deterministic Python control plane runs ten stages. Model work is limited to two Claude Agent SDK sessions
(explore, build) and structured-output calls (specify; an interpreter for non-REST traffic). Agents propose;
code verifies.

| Stage | What happens | Who |
|---|---|---|
| intake | preflight in code ($0): reachable, login wall, bot challenge, canvas-only, protocol mix | code |
| explore | an agent drives a real browser through `observe` / `act` / `navigate`; the harness records every step, request and response, injects nonces, enforces the origin allowlist and write policy, and deletes what it created | Agent SDK |
| extract | REST adapter: path templates, JSON shapes, server-generated fields, foreign keys, enums from select options, required fields from rejected probes; non-REST traffic goes to an interpreter whose citations are checked against the recording | code (+ model) |
| specify | three structured calls (data, UI, scenarios) assembled into a flat AppSpec, integrity-checked, retried with errors | model + code |
| validate | scenarios run on the **original** as data; only non-vacuous scenarios that pass every run become candidates | code |
| generate | template + codegen into a new repo: Prisma schema and offline-generated migration, Fastify routes, React screens with the original's roles and names, OpenAPI, compose | code |
| negative | a write-rollback mutant of the replica: a candidate is protected only if it passes on the baseline and fails on the mutant after its first write | code |
| build | a guarded coding agent improves fidelity and implements actions codegen can't express; it can't edit protected files or leave its worktree; the harness re-checks in a fresh container | Agent SDK |
| verify | fresh clone, empty database, replica on an internal Docker network with no route to the original: protected and held-out scenarios, request audit, restart persistence, routes == contract, integrity hashes | code |
| deliver | tag the verified commit, write README and verification report into the generated repo | code |

`--quick` keeps every correctness gate but trims cost: pilot workflow only, one validation run and no mutant
check. The builder still runs (two rounds), so the replica still looks and behaves like the original. The
report says the run was quick.

## Name features, not pages

`--features crud,search` tells forge what to replicate; it finds where each feature lives by itself. With
`search` requested, the explorer looks for every search box, search dialog, command palette or list filter,
and searches for a record it created; the pilot scenario then includes that search, so it is validated on the
original, built and verified like the rest. CRUD is always part of the pilot.

`forge coverage <run-id>` (also printed right after specify, before anything is built, and written into the
generated README) lists each requested feature as found / in the pilot / validated on the original / verified
on the replica, the screens that were recorded and built, the API calls seen, and what was only seen.

## Self-repair

Every failure class forge meets is handled by forge, not by hand:

- **Specify** repairs what needs no model (a detail screen without a get-by-id gets one on the replica's API;
  operations the renderer cannot express are left out; unwireable elements stay static for the builder) and
  retries the model with the remaining errors.
- **Validate** treats a scenario that fails on the original as a wrong test: it repairs the scenario from the
  page's accessibility snapshot at the failing step, keeps steps that already ran, and runs it again while it
  makes progress.
- **Build** starts from the protected scenarios' real failures on the generated baseline, works behavior first
  and look second, and is checked after every round (typecheck, build, a `data` check against records copied
  from the original, scenarios).

## Reuse a recording and watch a run

The app rarely changes between runs, so a new run can start from an earlier run's exploration, spec and
validated scenarios and still get its own repository:

```sh
forge run <url> --quick --reuse <earlier-run-id> --from generate
forge resume <run-id> --from <stage>           # continue a stopped run
forge watch <run-id>                           # stages, time, cost, coverage, log, builder's changes
```

## Requirements

Python 3.12+ with [uv](https://docs.astral.sh/uv/), Docker with Compose v2, and an Anthropic API key in
`ANTHROPIC_API_KEY` or `~/.forge.env`. `forge doctor` pulls and builds the images it needs.

```sh
uv sync
uv run playwright install chromium
uv run forge doctor
```

## Try it on the demo

`demo/issue-tracker` is a small app on a different stack (Express + node:sqlite + vanilla JS) used as the
"original":

```sh
docker compose -f demo/issue-tracker/compose.yaml up -d --build     # http://127.0.0.1:4001
uv run forge run http://127.0.0.1:4001 --mutations test-account --quick
```

Its five held-out scenarios (`demo/issue-tracker/heldout/`) are never shown to the builder; pass
`--heldout demo/issue-tracker/heldout` to run them at verify.

## Signing in

`forge login <url>` opens a real browser for you to sign in once (SSO, 2FA and CAPTCHA are yours to
complete); the session is stored encrypted under `~/.forge/sessions/` and used for exploration and validation.
It never enters model context, run artifacts or the generated repository.

## Responsible use

Point forge only at apps you own or are allowed to test, and sign in with a test account. Writes to the
original happen only with `--mutations test-account`, every record the explorer creates is marked and deleted
afterwards, requests to other sites are blocked, and forge never tries to get past CAPTCHAs or bot challenges:
you complete sign-in yourself with `forge login`. Replicas run locally; publishing one is your responsibility.

## Docs

- [`docs/architecture.html`](docs/architecture.html): the architecture and design decisions.
- [`docs/PLAN.md`](docs/PLAN.md): the original implementation plan.
- [`examples/issue-tracker/`](examples/issue-tracker/): a finished run on the demo app, with its report and the
  generated repository as a git bundle.

## Repository layout

```
forge/            control plane: cli, core (pipeline, store, ledger, llm, docker, git), stages/*, models (contracts)
template/         the pinned replica stack (React + Vite + Tailwind, Fastify, Prisma, Postgres) and shared library
demo/             the demo target and its pilot + held-out scenarios
docker/           the scenario runner image
docs/             architecture and the original plan
examples/         a finished run on the demo app
tests/            unit, golden and docker/live-marked integration tests
spikes/           the milestone-1 feasibility spikes
```

## Tests

```sh
uv run pytest                      # unit + golden, no network or Docker
uv run pytest -m docker            # needs Docker and the demo on :4001
uv run pytest -m live              # spends a little API money (schema acceptance, smoke runs)
```

## License

[MIT](LICENSE)
