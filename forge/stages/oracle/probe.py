"""Network probes run from inside the verify network (stdlib only; ships in the runner image).

    python3 -m forge.stages.oracle.probe http --url http://replica:3000/__forge/health --retries 60
    python3 -m forge.stages.oracle.probe fetch --url http://replica:3000/api/projects [--url ...]
    python3 -m forge.stages.oracle.probe egress --target host.docker.internal:4001 --target https://example.com

Each command prints one JSON document on stdout.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def http_get(url: str, timeout: float = 5.0) -> dict:
    """GET a URL; returns status, parsed JSON (when it is JSON) or a text excerpt, or the error."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - URLs come from forge itself
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    except (urllib.error.URLError, OSError, ValueError) as err:
        return {"url": url, "status": None, "error": f"{type(err).__name__}: {getattr(err, 'reason', err)}"}
    try:
        return {"url": url, "status": status, "json": json.loads(raw.decode() or "null")}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"url": url, "status": status, "text": raw[:500].decode(errors="replace")}


def wait_http(url: str, retries: int, interval: float, expect_status: int) -> dict:
    last: dict = {}
    for attempt in range(1, max(1, retries) + 1):
        last = http_get(url)
        last["attempts"] = attempt
        if last.get("status") == expect_status:
            return last
        time.sleep(interval)
    return last


def can_connect(target: str, timeout: float) -> dict:
    """TCP-connect to ``host:port`` or to an http(s) URL's host; reachable means the handshake completed."""
    if "://" in target:
        parts = urlsplit(target)
        host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    else:
        host, _, port_text = target.rpartition(":")
        port = int(port_text)
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            reachable, error = True, None
    except OSError as err:
        reachable, error = False, f"{type(err).__name__}: {err}"
    return {"target": target, "reachable": reachable, "error": error,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-probe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_http = sub.add_parser("http")
    p_http.add_argument("--url", required=True)
    p_http.add_argument("--retries", type=int, default=1)
    p_http.add_argument("--interval", type=float, default=1.0)
    p_http.add_argument("--expect-status", type=int, default=200)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--url", action="append", required=True)
    p_egress = sub.add_parser("egress")
    p_egress.add_argument("--target", action="append", required=True)
    p_egress.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    if args.cmd == "http":
        result = wait_http(args.url, args.retries, args.interval, args.expect_status)
        print(json.dumps(result))
        return 0 if result.get("status") == args.expect_status else 1
    if args.cmd == "fetch":
        print(json.dumps([http_get(url) for url in args.url]))
        return 0
    print(json.dumps([can_connect(t, args.timeout) for t in args.target]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
