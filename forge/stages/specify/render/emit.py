"""Serializers for generated TypeScript/TSX. Spec text only ever enters code through ``js()``.

``js()`` returns JSON, which is also a valid JS/TS expression. Objects and arrays stay on one line
when they fit and expand one entry per line otherwise, so generated files read like hand-written code
while staying byte-for-byte deterministic.
"""
from __future__ import annotations

import json

WIDTH = 110


def _escape(text: str) -> str:
    # Valid in JSON strings but historically not in JS string literals.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def _pretty(value: object, col: int, base: int, width: int) -> str:
    flat = _compact(value)
    if not isinstance(value, (dict, list)) or not value or col + len(flat) <= width:
        return flat
    inner = base + 2
    pad = " " * inner
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            prefix = json.dumps(key, ensure_ascii=False) + ": "
            lines.append(pad + prefix + _pretty(item, inner + len(prefix), inner, width))
        return "{\n" + ",\n".join(lines) + "\n" + " " * base + "}"
    lines = [pad + _pretty(item, inner, inner, width) for item in value]
    return "[\n" + ",\n".join(lines) + "\n" + " " * base + "]"


def js(value: object, *, col: int = 0, base: int = 0, width: int = WIDTH) -> str:
    """A JSON literal placed at column ``col`` on a line indented by ``base`` spaces."""
    return _escape(_pretty(value, col, base, width))


def jsx(component: str, props: list[tuple[str, object]], indent: int, children: list[str] | None = None) -> str:
    """``<Component a={...} b={...} />`` (or with children), one prop per line when it does not fit."""
    pad = " " * indent
    flat = " ".join(f"{name}={{{_escape(_compact(value))}}}" for name, value in props)
    opener = f"{pad}<{component}{' ' + flat if flat else ''}"
    if len(opener) + 3 <= WIDTH:
        if children is None:
            return opener + " />"
        return opener + ">\n" + "\n".join(children) + f"\n{pad}</{component}>"
    lines = [f"{pad}<{component}"]
    for name, value in props:
        prefix = f"{pad}  {name}={{"
        lines.append(prefix + js(value, col=len(prefix), base=indent + 2) + "}")
    if children is None:
        return "\n".join(lines) + f"\n{pad}/>"
    return "\n".join(lines) + f"\n{pad}>\n" + "\n".join(children) + f"\n{pad}</{component}>"
