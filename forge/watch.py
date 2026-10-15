"""Live progress of a run in the browser (``forge watch <run>``): stages, time and cost, the log, and the files the
builder is changing in the generated repo. Read-only; it only reads the run directory and the repo."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from forge.core.pipeline import STAGES
from forge.core.store import RunStore


def _tail(path: Path | None, lines: int = 60) -> list[str]:
    if path is None or not path.exists():
        return []
    text = path.read_text(errors="replace").splitlines()
    return [line for line in text if "connectors are disabled" not in line][-lines:]


def _git(repo: Path, *args: str) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def run_state(store: RunStore, log_path: Path | None = None) -> dict:
    db = sqlite3.connect(store.dir / "ledger.sqlite")
    try:
        rows = db.execute("SELECT stage, status, started_at, finished_at FROM stage_runs ORDER BY id").fetchall()
        costs = dict(db.execute("SELECT stage, SUM(cost_usd) FROM model_calls GROUP BY stage").fetchall())
    finally:
        db.close()
    last = {stage: (status, started, finished) for stage, status, started, finished in rows}
    now = time.time()
    stages = []
    for stage in STAGES:
        status, seconds = "pending", None
        if stage in last:
            raw, started, finished = last[stage]
            status = {"ok": "done"}.get(raw, raw)
            seconds = (finished or now) - started
        marker = store.dir / "stages" / f"{stage}.done"
        if marker.exists():
            text = marker.read_text()
            if text.startswith("reused"):
                status = "reused"
            elif text == "skipped":
                status = "skipped"
            elif status != "running":
                status = "done"
        stages.append({"stage": stage, "status": status, "seconds": seconds, "cost": round(costs.get(stage) or 0, 2)})
    config = store.config
    repo, replica, commits, changes = None, None, [], []
    codegen = store.dir / "artifacts" / "codegen.json"
    if codegen.exists():
        repo = Path(json.loads(codegen.read_text())["repo"])
        commits = _git(repo, "log", "--oneline", "-8")
        changes = _git(repo, "status", "--short")[:40]
        env = repo / ".env"
        if env.exists():
            port = next((l.split("=", 1)[1].strip() for l in env.read_text().splitlines() if l.startswith("PORT=")), "")
            replica = f"http://127.0.0.1:{port}" if port else None
    verification = None
    vpath = store.dir / "artifacts" / "verification.json"
    if vpath.exists():
        v = json.loads(vpath.read_text())
        verification = {"overall": v.get("overall"),
                        "workflows": [(w["scenario_id"], w["status"]) for w in v.get("workflows", [])],
                        "checks": [(c["name"], c["status"]) for c in v.get("checks", [])]}
    cov = None
    if (store.dir / "artifacts" / "explore.json").exists():
        from forge.coverage import coverage

        try:
            cov = coverage(store)
        except Exception:  # noqa: BLE001 - partial artifacts while a stage writes them
            cov = None
    return {"coverage": cov, "run_id": config.run_id, "url": config.url, "created_at": config.created_at,
            "mode": (store.dir / "artifacts" / "mode.txt").read_text().strip()
            if (store.dir / "artifacts" / "mode.txt").exists() else "full",
            "stages": stages, "spent": round(sum(s["cost"] for s in stages), 2), "budget": config.budget_usd,
            "repo": str(repo) if repo else None, "replica": replica, "commits": commits, "changes": changes,
            "verification": verification, "log": _tail(log_path or store.dir / "run.log"), "now": now}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>forge watch</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--muted:#5d6677;--line:#dfe3ea;--run:#a8521f;--ok:#1f7a4d;--bad:#b42318;--soft:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#191d25;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2b313d;--run:#e08a52;--ok:#4cc38a;--bad:#f97066;--soft:#232933}}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px;display:grid;gap:16px}
h1{margin:0;font-size:20px}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:320px minmax(0,1fr);gap:16px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.st{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-bottom:1px solid var(--line);font-family:ui-monospace,Menlo,monospace}
.st:last-child{border-bottom:0}.chip{font:600 11px/1 system-ui;padding:4px 7px;border-radius:99px;background:var(--soft);color:var(--muted)}
.running{background:var(--run);color:#fff}.done,.ok{background:var(--ok);color:#fff}.failed,.stopped{background:var(--bad);color:#fff}
.reused{background:var(--soft);color:var(--ok)}pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,Menlo,monospace;max-height:420px;overflow:auto}
a{color:var(--run)}h2{margin:0 0 8px;font-size:14px}
</style></head><body><div class="wrap"><div><h1 id="title">forge watch</h1><div class="muted" id="sub"></div></div>
<div class="grid"><div class="card"><h2>Stages</h2><div id="stages"></div><div class="muted" id="cost" style="margin-top:8px"></div></div>
<div style="display:grid;gap:16px;min-width:0"><div class="card"><h2>Replica</h2><div id="replica" class="muted">not generated yet</div></div>
<div class="card"><h2>Coverage: recorded on the original → built</h2><div id="cov" class="muted">after explore</div></div>
<div class="card"><h2>Builder activity (repo commits and changed files)</h2><pre id="repo"></pre></div>
<div class="card"><h2>Log</h2><pre id="log"></pre></div></div></div></div>
<script>
const fmt=s=>s==null?"":s<90?Math.round(s)+" s":(s/60).toFixed(1)+" min";
async function tick(){try{const r=await fetch("state.json",{cache:"no-store"});const s=await r.json();
document.getElementById("title").textContent="forge · "+s.run_id+" ("+s.mode+")";
document.getElementById("sub").textContent=s.url;
document.getElementById("stages").innerHTML=s.stages.map(x=>`<div class="st"><span>${x.stage}</span><span>${fmt(x.seconds)} ${x.cost?"$"+x.cost.toFixed(2):""} <span class="chip ${x.status}">${x.status}</span></span></div>`).join("");
document.getElementById("cost").textContent="spent $"+s.spent.toFixed(2)+" of $"+s.budget;
let rep=s.repo?("repo: "+s.repo):"not generated yet";if(s.replica)rep+=`<br>replica: <a href="${s.replica}/" target="_blank">${s.replica}</a> (after <code>docker compose up --build -d</code> in the repo)`;
if(s.verification)rep+="<br>verify: <b>"+s.verification.overall+"</b> · "+s.verification.workflows.map(w=>w.join(" ")).join(", ");
document.getElementById("replica").innerHTML=rep;
if(s.coverage){const c=s.coverage;const row=(ok,t,n)=>`<div class="st"><span>${t}</span><span class="chip ${ok?"done":""}">${n}</span></div>`;
document.getElementById("cov").innerHTML="<b>Features</b>"+c.features.map(f=>row(f.probed,f.name+(f.pilot?" (pilot)":""),f.probed?"explored end to end":"seen only")).join("")
+"<br><b>Screens</b>"+c.screens.map(x=>row(x.built,x.route,x.built?"built":(x.recorded?"recorded, not built":"seen, not recorded"))).join("")
+"<br><b>API calls</b>"+c.operations.map(o=>row(o.built,o.call,o.built?"built":"not built")).join("");}
document.getElementById("repo").textContent=(s.commits.join("\\n")||"")+(s.changes.length?"\\n\\nuncommitted changes:\\n"+s.changes.join("\\n"):"");
const log=document.getElementById("log");const atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-8;log.textContent=s.log.join("\\n");if(atEnd)log.scrollTop=log.scrollHeight;
}catch(e){document.getElementById("sub").textContent="waiting for the run directory…"}}
tick();setInterval(tick,2000);
</script></body></html>"""


def serve(run_id: str, port: int, log_path: Path | None = None) -> None:
    store = RunStore.open(run_id)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path.startswith("/state.json"):
                body, ctype = json.dumps(run_state(store, log_path)).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
