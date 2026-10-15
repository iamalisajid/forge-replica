> This is the original implementation plan, kept as written when the build started. Some details changed
> while building (for example, quick runs now include the builder, and runs can reuse an earlier
> exploration); the [README](../README.md) describes how forge works today.

# Implementation plan — forge (URL → runnable full-stack replica)

## Context
The architecture is settled in [`docs/architecture.html`](architecture.html) (v2, reviewed several times). This plan turns it into code in **this repository** (empty when the plan was written).

The goal is `forge run <url>`. It explores a web app and cites what it saw, then writes a flat spec. From that it generates a TypeScript full-stack app (React + Fastify + Postgres + Prisma migrations) as a **new git repository**. It proves scenarios on the original first and improves fidelity with a guarded agent. It verifies the replica on a network with no route to the original, and the delivered app runs with `docker compose up`.

The machine is ready:
- The API key in `~/.forge.env` works and has been verified.
- Docker 28.5, uv 0.11, pnpm 10, Node 23.9 and Python 3.13.2 are installed.
- Cached images: `node:22-alpine`, `postgres:16-alpine`.

An earlier prototype contributed ideas only:
- `provider.py`: API key read from the environment, truncation checks.
- `inspect.cjs`: origin guard on navigation.
- `schema.py`: strict spec validation before codegen.
- `templates/server.py`: Origin, Content-Type and body-size checks on writes.

**Confirmed with the user:**
- Build in this repo; commit per milestone on `builder`, no push.
- `claude-opus-5-5` for every model stage, including development runs.
- Parallel lanes run by background subagents in isolated worktrees, after the contracts and the demo exist.
- **Every URL gets its own new git repository, and so does every run.** Running the same URL again creates another repo; earlier ones are left untouched.

**User decision after approval:** nothing is cut. `forge run <url> --quick` adds a fast path: pilot workflow only, 1 validation run on the original (instead of 3), no builder agent, no mutant check. The report labels such runs `quick` (reduced assurance). The default `forge run` keeps full rigor.

**Execution:** four lanes in parallel, all in this working tree with disjoint directories. Lane B (subagent) builds template + codegen (per-entity and per-screen generated files over a shared component library). Lane C (subagent) builds the oracle, negative control and verify. Lane D (subagent) builds the builder agent. Lane A (me) builds CLI, intake, explore, extract, specify and deliver. I commit after integrating.

## Where things live
| What | Location | In git? |
|---|---|---|
| Orchestrator code, template, demo target, tests | this repo | yes |
| Generated app (one per run) | `--out` root, default `~/forge-apps/<app-slug>-<runid>/` | **its own repo** — see below |
| Run state: artifacts, evidence (raw traffic, screenshots), ledger, cassettes | `~/.forge/runs/<runid>/` | never; outside every repo, purged after retention |
| Credentials: API key, encrypted storage states, encryption key | `~/.forge.env`, `~/.forge/sessions/`, `~/.forge/keys/` | never |
| Example for reviewers | this repo: `examples/issue-tracker/` (run report, `verification.json`, `replica.bundle`; `git clone replica.bundle` restores the repo) | report + bundle only |

The generated repo is created with `git init` at Generate. It gets:
- a `G1` commit for the baseline;
- one commit per builder round;
- a tag `forge/verified` or `forge/partial` at the verified SHA;
- a final commit adding the report.

Commits use a tool identity, `forge <forge@localhost>`. Builder rounds run in worktrees of that repo, and Verify clones it.

## Pinned versions and technical choices
**Orchestrator** (Python 3.13 via uv):
- `anthropic>=1.14`; `claude-agent-sdk==0.2.167`.
- `playwright==1.63.0`, matching the runner image `mcr.microsoft.com/playwright/python:v1.63.0-noble` (no 1.64 image exists).
- `pydantic 2.14`, `typer`, `genson 1.4`, `pyyaml`, `cryptography`, `httpx`, `pytest`, `pytest-asyncio`.

**Structured calls:**
- Use `client.messages.parse(..., output_format=Model)` → `parsed_output`.
- Large outputs are streamed with `output_config.format`, then validated with `model_validate_json`.
- Server-side refusal fallback (`fallbacks: "default"`) is on, checked against the installed SDK.
- Models are flat, `extra="forbid"`, no recursion, **no open maps** (`binds`, `contains`, `typed` are `[{name, value}]` lists).
- Two model sets: the schema the model fills in, and the stored artifact envelope (header with input hashes and versions).

**Model:** `claude-opus-5-5` with effort per stage: explore `medium`, specify `high`, build `high`.

**Template** (generated app):
- Node 22 on `node:22-alpine` with openssl. The base image bakes in `node_modules` and the Prisma `linux-musl-arm64-openssl-3.0.x` engines, with `CHECKPOINT_DISABLE=1`, so `prisma generate`, `tsc` and `vite build` run offline.
- Fastify 5.12 serves the API, the built SPA and an SPA fallback.
- React 19 + react-router, Vite 7.3.7, Tailwind 4.3, TypeScript 5.9.3.
- Prisma 6.19.3, Postgres 16, openapi-typescript 7.13 + openapi-fetch 0.17.
- npm with a committed lockfile. `package.json`, the lockfile and `.dockerignore` are immutable and identical to the base image's copies.
- The AppSpec has `auth: none|session`; the auth pack is only wired when it is `session`.

**Runner image:**
- Built `FROM mcr.microsoft.com/playwright/python:v1.63.0-noble` with a venv; the image refuses a plain `pip install` (externally managed environment).
- Contains `forge.models` and `forge.oracle`, which import only pydantic and playwright.
- Tagged by the hash of its source.

**Demo target** (a different stack): Express 5 + `node:sqlite` + a vanilla-JS SPA on `127.0.0.1:4001`.
- Projects ↔ issues, a status enum, 422 on an empty title, and a custom `close` action.
- `AUTH=on` adds a cookie login (used later).

**Isolation:**
- Worktrees are **copied into images** (`COPY`), never bind-mounted.
- Verify adds a pinned overlay that makes every network `internal: true`. Health, routes, effect, drift and audit checks run **from containers inside that network**.
- `doctor` proves egress is blocked: from inside the network, `host.docker.internal:4001` and an internet host must both fail.
- After Verify, one smoke check runs the delivered compose file on its published port.

## Repo layout
```
pyproject.toml, uv.lock, README.md
forge/
  cli.py                 doctor | login | run | resume | report | purge | cleanup | bundle
  config.py              settings, ~/.forge.env, price table, limits, paths (~/.forge, ~/forge-apps)
  models/                FROZEN CONTRACTS: run, trace, observed, appspec (incl. wire format, auth mode), scenario (flat Step + kind enum,
                         ScenarioResult with failed_step + failure_kind), verification; llm-fill schemas separate from stored envelopes
  core/                  store.py, runner.py (stage DAG, cache keys, resume, --pilot-only, --no-build), ledger.py (SQLite), budget.py,
                         llm.py (messages.parse + cassettes), docker.py (compose/images, forge-<runid>-* naming), gitrepo.py (generated repos)
  stages/
    intake.py            doctor (incl. egress probe), preflight
    explore/             harness.py (Playwright async, asyncio.Lock around every tool call, step ids under the lock, capture, allowlist,
                         write policy, created-id ledger, cleanup), tools.py (observe/act/navigate; capped snapshot size),
                         locators.py (portable locators + uniqueness), agent.py
    extract/             redact.py, rest.py (templating, genson, server-generated, FK/enum/constraints-from-probes)
    specify/             specify.py, integrity.py, render/ (prisma.py, openapi.py, routes.py, screens.py — pure, serializer-only)
    oracle/              interpreter.py (scenario DSL), validate.py (non-vacuity, 3/3, env failures), negative.py (baseline vs mutant),
                         runner_cli.py (runner image entrypoint)
    generate/            codegen.py (new repo, render, G1 commit), manifest.py (immutable/editable + hashes)
    build/               guard.py, checks.py (run_checks: image build + timeout), agent.py
    verify/              compose.py (clone, overlay, lint), checks.py (hashes, regenerate-and-diff, routes == contract), report.py
    deliver.py           tag, README, report commit, bundle
  prompts/               explore.md, specify.md, build.md (hash is part of cache keys)
template/                web/, api/ (db.write() helper with FORGE_MUTANT=rollback-writes, /__forge/routes in test mode, SPA fallback,
                         auth pack), db/, compose.yaml, .forge/verify.yaml, Dockerfile, docker/base.Dockerfile, docker/runner.Dockerfile
demo/issue-tracker/      target app + compose.yaml + scenarios/pilot.json + heldout/*.json (5, never shown to the builder)
tests/                   unit, golden, $0 golden pipeline, scripted-driver integration, `-m live` smoke tests (≤ $0.50 each)
examples/issue-tracker/  report + verification.json + replica.bundle
```

## Frozen contracts (milestone 2) — what lets lanes work without waiting
- **Data models:** trace, observed model, AppSpec, scenario, verification.
- **Fixtures:** a hand-written AppSpec for the demo, a recorded-by-hand trace, the pilot scenario and 5 held-out scenarios.
- **Stack contract:** services `db`/`migrate`/`app`/`runner`, app port 3000, `/__forge/health`, test-mode `/__forge/routes`, `FORGE_MUTANT`, network and project names `forge-<runid>-*`.
- **Runner I/O:** CLI args, bindings in, `ScenarioResult` out (`passed`, `failed_step`, `failure_kind` = assertion | action | env | startup).
- **Wire format per operation:** method, path template, request and response fields, list envelope, status codes. Effect checks call the same logical operation on the original and the replica.
- **Capture rule:** `{{created.<entity>.id}}` is read from the create response matched by the operation.
- **Function interfaces:** `render(appspec) -> dict[path, str]`, `generate(appspec, out_root) -> CodegenManifest`, `run_scenarios(target, scenarios, *, storage_state=None, parallel=1) -> list[ScenarioResult]`.
- **Contract test:** one test that both lanes run against the fixtures.

## Stage notes (only what isn't obvious)
**Runner**
- Cache key = sha256 of input artifact hashes, stage `VERSION`, prompt hash, model id and relevant config. `forge resume <id> [--from stage]`.
- Pilot first: `--pilot-only` runs one workflow (create → reload → edit → delete) through every stage, with **Build skipped (G1 is the candidate)**. A normal run does the pilot, then the rest.

**Explore** — `query()` with these options:
- `tools=[]`; MCP server `browser` with the exact tool names `mcp__browser__observe`, `mcp__browser__act`, `mcp__browser__navigate` in `allowed_tools`.
- `permission_mode="dontAsk"`, `setting_sources=[]`, `max_turns`, `max_budget_usd` with headroom.
- The inventory comes from `output_format` and is optional, because max-turns or budget stops return no structured output.

**Explore harness:**
- Every tool call is serialized with an `asyncio.Lock`, because the SDK runs tool calls concurrently.
- Snapshots are size-capped: results over the size limit are saved to a file the explorer cannot read.
- Requests to other origins are blocked. Non-GET requests are blocked unless `--mutations test-account`.
- Nonces are injected into free-text fills.
- Correlation uses the nonce, or else entity id + method/path + before/after state.
- Cleanup deletes the ids in the write-ahead ledger and confirms a 404.
- **Spike first:** `aria_snapshot(mode="ai")` and `aria-ref=` locators in Python 1.63. The fallback is CDP `Accessibility.getPartialAXTree`.

**Extract** (pilot scope):
- Redaction by value.
- REST adapter: `{id}` templating, genson per method + template.
- Server-generated fields = response keys − request keys.
- Foreign keys by name + value subset; enums from observed select options.
- Constraints only from rejected negative probes; otherwise labeled `inferred`.

**Specify:**
- One streamed structured call → flat AppSpec, plus one retry carrying the integrity errors.
- Renderers are pure functions; strings go out only through serializers.

**Oracle:**
- Interpreter for the flat Step DSL, with targets `{role, name, exact, within, has_text}`.
- Non-vacuity: expectations are evaluated before the first action.
- Validate: 3/3 runs in fresh contexts (sequential for the pilot), plus an environment-failure class.
- The source runner runs in its own process; the storage state is decrypted in memory only.

**Negative control** (in `oracle/negative.py`):
- A candidate becomes **protected** when it passes on the baseline and the `rollback-writes` mutant fails with `failure_kind=assertion` at **any step after the first write**. Generated screens refetch after writes, so an earlier assertion legitimately catches the missing write.
- Startup or action failures on the mutant don't count.

**Build:**
- `tools=["Read","Edit","Write","Glob","Grep"]`, MCP `checks` (`run_checks`, `get_screenshot`).
- **`allowed_tools` lists every one of them by exact name**; under `dontAsk`, anything not listed is denied.
- `setting_sources=[]`; no Bash.
- PreToolUse guard, covering Read/Edit/Write/Glob/Grep: real path, NFC/NFD and case folded; reads allowed in the worktree or a **curated, redacted context directory** (never `~/.forge/runs/<id>`); writes only in the worktree; dot-config files, symlinks and immutable-list files denied; absolute Glob patterns and the Grep `path` argument are checked too.
- `run_checks` builds `FROM base` + `COPY worktree` under a timeout, runs typecheck and build with `--network none`, and runs scenarios through the runner on the internal network.
- ≤2 rounds; the harness re-checks in a fresh stack.

**Verify:** clone at the candidate SHA, then:
1. Inject the pinned `compose.yaml`, Dockerfiles and overlay, checked by hash.
2. Compose lint.
3. Bring the stack up with all networks internal.
4. Run checks from inside the network: health, routes == contract, protected and held-out scenarios, request audit, restart + re-read, and the mutant for pending scenarios.
5. Static checks: immutable hashes and regenerate-and-diff.
6. Write `verification.json` with a status per workflow; the overall status is `verified` only if every in-scope workflow is verified, otherwise `partial`.

**Deliver:** tag, README, report commit, then the published-port smoke check.

**Deferred until after the pilot works:**
- Extract interpreter + citation verifier (the demo is pure REST), design tokens.
- Shadow-DB drift check (regenerate-and-diff covers one migration).
- Auth pack + `AUTH=on`.
- Hygiene scans beyond hashes, visual score, user Playwright specs.
- Parallel 3/3 runs, background image pulls.

## Milestones (relative effort in brackets; commit after each)
1. **Spikes [1]** — each prints PASS:
   - SDK: snapshot + PNG tool result, `act` on a ref, a serialized parallel call, `dontAsk` with exact tool names, structured output with `tools=[]`, and a hook denying a write outside the repo.
   - Playwright: refs resolve to role locators.
   - Base image: `prisma generate`, `tsc`, `vite build` and `migrate diff` work offline.
   - Network: the runner reaches `app:3000` but not the host or the internet.
   - Runner image: forge packages import.
   - Live: the AppSpec and Scenario schemas are accepted by the API.
2. **Demo, contracts and core [2]:**
   - The demo runs on `:4001`.
   - Models and interface contracts are frozen; fixtures validate.
   - `doctor` passes, including the egress probe.
   - Schema-compatibility and round-trip tests pass.
3. **Lane B (subagent): template and codegen [3]:**
   - The fixture AppSpec renders a G1 repo (new git repo) that starts healthy on an empty database.
   - Routes == contract; rendering twice gives identical files; manifest hashes are written.
   - The mutant returns 2xx and persists nothing.
4. **Lane C (subagent): oracle [1.5]:**
   - Pilot and held-out scenarios pass 3/3 on the demo.
   - Environment failures are classified separately; results carry `failure_kind`.
5. **Lane C: negative control and Verify [2.5]** (starts once milestone 3's codegen lands):
   - The baseline passes; the mutant fails at an assertion after the first write.
   - Verify from a fresh clone writes `verification.json`.
   - Tampering, a fetch to the original and a vacuous scenario each fail.
   - The $0 golden pipeline test passes.
6. **Lane A (me, in parallel with 3–5): harness and extract [2]:**
   - A scripted driver (no model) records the pilot with nonce-correlated writes, unique locators and redaction.
   - The REST adapter matches a golden `observed-model.json`.
7. **Lane A: agents [1.5]:**
   - A live explore run (≤ $2) covers create, edit and delete.
   - The Specify output passes integrity; the specify call is saved as a cassette.
8. **Pilot end to end [1.5]:**
   - `forge run http://127.0.0.1:4001 --mutations test-account --pilot-only --budget-usd 4` ends with the pilot workflow `verified` at G1, in a new repo under `~/forge-apps/`.
   - Replaying the cassette and recorded trace reproduces it for $0.
9. **Build agent [2]:**
   - Guard tests pass; `close` is implemented.
   - A fresh-stack re-check passes with immutable files unchanged in ≤2 rounds.
   - A tag is set at the verified SHA.
10. **Full run, delivery and hardening [3]:**
    - All workflows run; held-out results are reported.
    - The delivered repo works on its published port.
    - `examples/issue-tracker/` holds the report and bundle.
    - Resume, budget stops and cleanup/purge work.
    - Then the deferred items (auth pack with `AUTH=on`, interpreter, scans).

After milestone 10, the architecture page's Deliver stage text is updated: separate repo per run + bundle, plus the corrected "mutant fails at an assertion after the first write" rule.

## Verification
**`uv run pytest`** — no API key needed:
- Unit tests: templating, genson merge, server-generated fields, FK/enum/constraint inference, integrity, renderers (golden files), manifest.
- Guard tests: symlinks, NFC/NFD, case, dot-config, absolute Glob, Grep path.
- Also: compose lint, bindings, non-vacuity on local HTML fixtures.
- The $0 golden pipeline: fixture AppSpec → codegen → baseline/mutant → Verify, with injected tampering.
- Scripted-driver integration against the demo.

**`uv run pytest -m live`** — capped smoke tests for the SDK and schema acceptance.

**Pilot:**
```
docker compose -f demo/issue-tracker/compose.yaml up -d
uv run forge doctor
uv run forge run http://127.0.0.1:4001 --mutations test-account --pilot-only --budget-usd 4
```
Expected: the pilot workflow is `verified`, a new repo appears under `~/forge-apps/`, and the cost shows in `forge report`.

**Full run:** `uv run forge run http://127.0.0.1:4001 --mutations test-account --heldout demo/issue-tracker/heldout --budget-usd 15`. Expected: a status per workflow, 5 held-out results, and the mutant failing at assertions.

**Delivered app:** in the new repo, `docker compose up`; create an issue in the browser, restart, and the data persists.

**Negative checks:** each of these must fail Verify:
- edit an immutable file → integrity fails;
- add a fetch to the original → the request audit fails;
- a scenario that passes on the mutant → dropped.
