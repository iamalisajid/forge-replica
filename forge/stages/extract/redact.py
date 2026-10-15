"""Redaction by value and by shape, applied before anything is saved or shown to a model.

Ids and ordinary data stay intact (they are evidence). Removed: the exact secret values from the session
(cookies, local storage tokens), JWT-shaped strings, and values of token/password-named JSON fields.
"""
from __future__ import annotations

import json
import re

JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
SECRET_KEYS = re.compile(r"(?i)^(password|passwd|secret|token|access_token|refresh_token|id_token|api_key|apikey|"
                         r"authorization|session|sessionid|sid|csrf|xsrf|client_secret)$")
MASK = "<redacted>"


class Redactor:
    def __init__(self, secrets: set[str] | None = None):
        self.secrets = {s for s in (secrets or set()) if s and len(s) >= 8}

    @classmethod
    def from_storage_state(cls, state: dict | None) -> "Redactor":
        values: set[str] = set()
        for cookie in (state or {}).get("cookies", []):
            values.add(str(cookie.get("value", "")))
        for origin in (state or {}).get("origins", []):
            for item in origin.get("localStorage", []):
                value = str(item.get("value", ""))
                if len(value) >= 16 and not value.strip().startswith(("{", "[")):
                    values.add(value)
        return cls(values)

    def add(self, value: str) -> None:
        if value and len(value) >= 8:
            self.secrets.add(value)

    def text(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.dumps(self._json(json.loads(stripped)), separators=(",", ":"))
            except ValueError:
                pass
        return self._plain(value)

    def _plain(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, MASK)
        return JWT.sub(MASK, value)

    def _json(self, node):
        if isinstance(node, dict):
            return {k: (MASK if SECRET_KEYS.match(k) and isinstance(v, str) else self._json(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [self._json(v) for v in node]
        if isinstance(node, str):
            return self._plain(node)
        return node
