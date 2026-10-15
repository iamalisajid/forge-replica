"""What a run recorded on the original, what it builds into the replica, and what it saw but leaves out.

Built from the run's own artifacts (explore inventory, observed model, spec, validation), so it can be shown right
after Specify, before anything is built, and again in the delivered README.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from forge.core.store import RunStore

TELEMETRY = re.compile(r"sentry|cohorts|unique_events|analytics|telemetry|tooltips|version\.json|pricing|payments|"
                       r"installations|ui_extensions|guess_timezone")


def _load(store: RunStore, name: str) -> dict | None:
    path = store.dir / "artifacts" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _pattern(route: str) -> re.Pattern[str]:
    """A route template (``/app/project/:project_id``, ``/app/project/:slug-:id``) as a regex for concrete paths."""
    parts = []
    for seg in route.strip("/").split("/"):
        parts.append("[^/]+" if ":" in seg or seg.startswith("{") else re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "/?$")


def _same_screen(a: str, b: str) -> bool:
    """Do two routes (templates or concrete paths) name the same screen?"""
    na = re.sub(r"[^/]*:[^/]*", "*", a.rstrip("/"))
    nb = re.sub(r"[^/]*:[^/]*", "*", b.rstrip("/"))
    return na == nb or bool(_pattern(a).match(b)) or bool(_pattern(b).match(a))


def coverage(store: RunStore) -> dict:
    explore = _load(store, "explore") or {}
    observed = _load(store, "observed") or {}
    appspec = _load(store, "appspec") or {}
    oracle = _load(store, "oracle") or {}
    spec = appspec.get("spec") or {}
    inventory = explore.get("inventory") or {}
    probed = set(explore.get("probed") or [])

    recorded_routes = [r["template"] for r in observed.get("routes", [])]
    spec_screens = spec.get("screens", [])
    screens: list[dict] = []
    for sc in spec_screens:
        screens.append({"route": sc["route"], "title": sc.get("title"), "recorded": True, "built": True})
    seen = []
    for f in inventory.get("features", []):
        seen += f.get("screens", [])
    for route in dict.fromkeys(seen):
        if any(_same_screen(route, s["route"]) for s in screens):
            continue
        recorded = any(_same_screen(route, r) for r in recorded_routes)
        screens.append({"route": route, "title": None, "recorded": recorded, "built": False})

    spec_ops = spec.get("operations", [])
    built_sources = {o.get("source_path") for o in spec_ops}
    operations = []
    for op in observed.get("operations", []):
        path = op["path_template"]
        if TELEMETRY.search(path):
            continue
        operations.append({"call": f"{op['method']} {path}", "built": path in built_sources})

    candidates = set(oracle.get("candidates") or [])
    scenarios = [{"id": s["id"], "title": s.get("title"), "validated": s["id"] in candidates,
                  "pilot": s["id"] == spec.get("pilot_scenario")} for s in spec.get("scenarios", [])]
    features = [{"name": f["name"], "entity": f.get("entity"), "probed": f["name"] in probed,
                 "pilot": f["name"] == inventory.get("pilot_feature"),
                 "can": [k for k in ("create", "edit", "delete") if f.get(f"can_{k}")],
                 "actions": f.get("actions", [])} for f in inventory.get("features", [])]
    notes = (inventory.get("notes") or [])
    pilot = next((sc for sc in spec.get("scenarios", []) if sc["id"] == spec.get("pilot_scenario")), None)
    pilot_searches = bool(pilot) and any(
        st.get("kind") == "fill" and re.search(r"search|find|filter|query", json.dumps(st.get("target") or {}), re.I)
        for st in pilot.get("steps", []))
    search = {"seen": [n.split(":", 1)[1].strip() for n in notes if n.lower().startswith("search:")],
              "api_params": sorted({f"{o['method']} {o['path_template']}?{o['search_param']}="
                                    for o in observed.get("operations", []) if o.get("search_param")}),
              "built_params": sorted({f"{o['method']} {o['path']}?{o['search_param']}="
                                      for o in spec_ops if o.get("search_param")}),
              "in_pilot": pilot_searches}
    run = _load(store, "run") or {}
    verification = _load(store, "verification") or {}
    pilot_id = spec.get("pilot_scenario")
    verified = any(w.get("scenario_id") == pilot_id and w.get("status") == "verified"
                   for w in verification.get("workflows", []))
    validated = pilot_id in candidates if pilot_id else False
    found_search = bool(search["api_params"]) or any(not s.lower().startswith("none") for s in search["seen"])
    # Which record fields the pilot changes: the request fields of the update operations its edit steps reach.
    update_fields = sorted({f for o in spec_ops if o.get("kind") == "update" for f in o.get("request_fields", [])})
    pilot_text = json.dumps(pilot or {})
    pilot_updates = [f for f in update_fields if re.search(rf"\b{re.escape(f)}\b", pilot_text, re.I)]
    requested = []
    for name in run.get("features") or ["crud"]:
        if name == "crud":
            pilot_feature = next((f for f in features if f["pilot"]), None)
            found = bool(pilot_feature and {"create", "edit", "delete"} <= set(pilot_feature["can"]))
            in_pilot = bool(pilot_id)
        elif name == "search":
            found, in_pilot = found_search, search["in_pilot"]
        else:
            found = in_pilot = False
        requested.append({"feature": name, "found": found, "in_pilot": in_pilot,
                          "validated": in_pilot and validated, "verified": in_pilot and verified,
                          **({"updates": pilot_updates} if name == "crud" else {})})
    return {"features": features, "screens": screens, "operations": operations, "scenarios": scenarios,
            "search": search, "requested": requested, "left_out": spec.get("unsupported", [])}


def summary_lines(cov: dict) -> list[str]:
    """A short version for the run log."""
    asked = "; ".join(f"{r['feature']}: {'found' if r['found'] else 'NOT found'}"
                      f"{', in the pilot' if r['in_pilot'] else ''}" for r in cov.get("requested", []))
    built = [s["route"] for s in cov["screens"] if s["built"]]
    missing = [s["route"] for s in cov["screens"] if not s["built"]]
    deep = [f["name"] for f in cov["features"] if f["probed"]]
    light = [f["name"] for f in cov["features"] if not f["probed"]]
    ops = [o["call"].split(" ", 1)[1].split("#")[-1] for o in cov["operations"] if o["built"]]
    lines = [f"  coverage: requested: {asked}",
             f"  coverage: builds {len(built)} screens: {', '.join(built)}",
             f"  coverage: explored end to end: {', '.join(deep) or 'none'}"
             + (f"; seen only: {', '.join(light)}" if light else ""),
             f"  coverage: recorded writes/reads built: {', '.join(ops) or 'none'}"]
    if missing:
        lines.append(f"  coverage: seen but not built (not recorded in this run): {', '.join(missing)}")
    srch = cov.get("search") or {}
    if srch.get("seen") or srch.get("api_params"):
        lines.append(f"  coverage: search seen: {'; '.join(srch.get('seen') or ['(from traffic)'])}"
                     f"; API search: {', '.join(srch.get('built_params') or []) or 'none (filtered in the browser)'}"
                     f"; in the pilot scenario: {'yes' if srch.get('in_pilot') else 'no'}")
    return lines


def markdown(cov: dict) -> str:
    out = ["## Coverage", "", "What the explorer recorded on the original, and what this replica builds from it.", "",
           "| Requested feature | Found in the app | In the pilot scenario | Validated on the original | Verified on the replica |",
           "|---|---|---|---|---|"]
    yn = lambda v: "yes" if v else "no"  # noqa: E731
    for r in cov.get("requested", []):
        out.append(f"| {r['feature']} | {yn(r['found'])} | {yn(r['in_pilot'])} | {yn(r['validated'])} | {yn(r['verified'])} |")
    out += ["",
           "| Feature | Explored | Can |", "|---|---|---|"]
    for f in cov["features"]:
        out.append(f"| {f['name']}{' (pilot)' if f['pilot'] else ''} | "
                   f"{'end to end' if f['probed'] else 'seen in the overview only'} | {', '.join(f['can']) or '-'} |")
    out += ["", "| Screen | Recorded | Built |", "|---|---|---|"]
    for s in cov["screens"]:
        out.append(f"| `{s['route']}` | {'yes' if s['recorded'] else 'no'} | {'yes' if s['built'] else 'no'} |")
    out += ["", "| API call seen on the original | Built |", "|---|---|"]
    for o in cov["operations"]:
        out.append(f"| `{o['call']}` | {'yes' if o['built'] else 'no'} |")
    srch = cov.get("search") or {}
    out += ["", f"Search seen by the explorer: {'; '.join(srch.get('seen') or []) or 'none'}. "
                f"Search in the replica API: {', '.join(srch.get('built_params') or []) or 'none'}. "
                f"Pilot scenario searches: {'yes' if srch.get('in_pilot') else 'no'}."]
    if cov["scenarios"]:
        out += ["", "| Scenario | Validated on the original |", "|---|---|"]
        for s in cov["scenarios"]:
            out.append(f"| {s['id']}{' (pilot)' if s['pilot'] else ''} | {'yes' if s['validated'] else 'no'} |")
    return "\n".join(out) + "\n"


def print_report(store: RunStore, echo=print) -> None:
    cov = coverage(store)
    yes = lambda v: "yes" if v else "no"  # noqa: E731
    echo("Requested features (forge run --features)")
    for r in cov["requested"]:
        echo(f"  {r['feature']:<8} found in the app: {yes(r['found']):<4} in the pilot: {yes(r['in_pilot']):<4}"
             f" validated on the original: {yes(r['validated']):<4} verified on the replica: {yes(r['verified'])}"
             + (f"   updates: {', '.join(r['updates']) or 'none named in the pilot'}" if "updates" in r else ""))
    echo("Features the explorer found")
    for f in cov["features"]:
        echo(f"  {'●' if f['probed'] else '○'} {f['name']:<12} {'explored end to end' if f['probed'] else 'seen only':<20}"
             f" can: {', '.join(f['can']) or '-'}{'   (pilot)' if f['pilot'] else ''}")
    echo("Screens")
    for s in cov["screens"]:
        mark = "built" if s["built"] else ("recorded, not built" if s["recorded"] else "seen, not recorded")
        echo(f"  {'●' if s['built'] else '○'} {s['route']:<40} {mark}")
    echo("API calls recorded on the original (telemetry left out)")
    for o in cov["operations"]:
        echo(f"  {'●' if o['built'] else '○'} {o['call']:<45} {'built' if o['built'] else 'not built'}")
    if cov["scenarios"]:
        echo("Scenarios")
        for s in cov["scenarios"]:
            echo(f"  {'●' if s['validated'] else '○'} {s['id']:<28} {'validated on the original' if s['validated'] else 'not validated'}"
                 f"{'   (pilot)' if s['pilot'] else ''}")
    srch = cov.get("search") or {}
    echo("Search")
    echo(f"  seen by the explorer: {'; '.join(srch.get('seen') or []) or 'none'}")
    echo(f"  search parameters in recorded API calls: {', '.join(srch.get('api_params') or []) or 'none'}")
    echo(f"  built into the replica API: {', '.join(srch.get('built_params') or []) or 'none (the UI filters in the browser)'}")
    echo(f"  part of the pilot scenario: {'yes' if srch.get('in_pilot') else 'no'}")
    if cov["left_out"]:
        echo("Left out of the spec")
        for item in cov["left_out"]:
            echo(f"  - {item}")


def write_markdown(store: RunStore, path: Path) -> None:
    path.write_text(markdown(coverage(store)))
