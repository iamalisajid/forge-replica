"""A tiny items app for interpreter tests: list, create (dialog), delete, detail page, plus env pages.

``serve(mutant=True)`` answers writes with 2xx but persists nothing, like FORGE_MUTANT=rollback-writes.
Extra routes: ``/status/<code>`` answers with that status, ``/challenge`` looks like a bot challenge.
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterator

APP_HTML = (Path(__file__).parent / "app.html").read_bytes()
CHALLENGE = b"<!doctype html><html><head><title>Just a moment...</title></head><body>Checking</body></html>"


class _State:
    def __init__(self, mutant: bool):
        self.mutant = mutant
        self.items: list[dict] = []
        self.next_id = 1
        self.lock = threading.Lock()


def _handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep test output quiet
            pass

        def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers=()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode())

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/items":
                with state.lock:
                    return self._json(200, {"items": list(reversed(state.items))})
            if m := re.fullmatch(r"/api/items/(\d+)", path):
                with state.lock:
                    item = next((i for i in state.items if i["id"] == int(m.group(1))), None)
                return self._json(200, item) if item else self._json(404, {"error": "Not found"})
            if path == "/api/ping":
                return self._json(200, {"pong": True})
            if m := re.fullmatch(r"/status/(\d{3})", path):
                return self._send(int(m.group(1)), b"<!doctype html><title>status</title>", "text/html")
            if path == "/challenge":
                return self._send(200, CHALLENGE, "text/html")
            return self._send(200, APP_HTML, "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/items":
                return self._json(404, {"error": "Not found"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body.get("name") or "").strip()
            if not name:
                return self._json(422, {"error": "Name is required"})
            with state.lock:
                item = {"id": state.next_id, "name": name}
                state.next_id += 1
                if not state.mutant:
                    state.items.append(item)
            return self._json(201, item)

        def do_DELETE(self) -> None:  # noqa: N802
            m = re.fullmatch(r"/api/items/(\d+)", self.path)
            if not m:
                return self._json(404, {"error": "Not found"})
            with state.lock:
                before = len(state.items)
                if not state.mutant:
                    state.items = [i for i in state.items if i["id"] != int(m.group(1))]
                gone = state.mutant or len(state.items) < before
            return self._send(204) if gone else self._json(404, {"error": "Not found"})

    return Handler


@contextmanager
def serve(mutant: bool = False) -> Iterator[str]:
    """Serve the fixture app on an ephemeral port of 127.0.0.1; yields the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(_State(mutant)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def ops_json() -> list[dict]:
    """OpWire dicts for the fixture app (same paths on 'source' and 'replica')."""
    def op(id, kind, method, path, list_key=None):
        return {"id": id, "kind": kind, "source_method": method, "source_path": path,
                "replica_method": method, "replica_path": path, "list_key": list_key}
    return [op("listItems", "list", "GET", "/api/items", "items"),
            op("createItem", "create", "POST", "/api/items"),
            op("getItem", "get", "GET", "/api/items/{item_id}"),
            op("deleteItem", "delete", "DELETE", "/api/items/{item_id}")]
