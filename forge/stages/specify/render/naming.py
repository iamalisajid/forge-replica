"""Identifier rules and name derivations shared by every renderer.

Spec strings only reach generated code in two ways: as identifiers that passed one of the strict
patterns below, or as literals produced by a serializer (``json.dumps``, ``yaml.safe_dump``,
``html.escape``). Nothing is spliced into code any other way.
"""
from __future__ import annotations

import re

# Spec names that become identifiers in code, schema or paths.
ENTITY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")            # "issue", "line_item"
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")       # wire names: "project_id", "createdAt"
OPERATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")     # "createIssue" (also a file name)
SCREEN_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")       # "projects", "issue-detail"
PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")      # "{project_id}" / ":project_id"
LIST_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SEGMENT = r"[A-Za-z0-9._~-]+"
API_PATH = re.compile(rf"^(?:/(?:{_SEGMENT}|\{{[A-Za-z_][A-Za-z0-9_]{{0,62}}\}}))+$")
SCREEN_ROUTE = re.compile(rf"^/$|^(?:/(?:{_SEGMENT}|:[A-Za-z_][A-Za-z0-9_]{{0,62}}))+$")
API_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ROUTE_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

# Prisma model names that clash with Prisma's own exports or JavaScript globals in the generated client.
RESERVED_MODELS = frozenset({
    "Prisma", "PrismaClient", "Decimal", "Json", "Bytes", "Date", "String", "Number", "Boolean", "Object",
    "Array", "Error", "Map", "Set", "Promise", "Symbol", "Function", "Record", "BigInt", "Buffer", "JSON",
    "Math", "RegExp", "Uint8Array", "Partial", "Required", "Readonly", "Pick", "Omit", "Exclude", "Extract",
})


def pascal(name: str) -> str:
    """"line_item" / "issue-detail" -> "LineItem" / "IssueDetail" (inner capitals are kept)."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[_\-\s]+", name) if part)


def camel(name: str) -> str:
    p = pascal(name)
    return p[:1].lower() + p[1:]


def snake(name: str) -> str:
    """"createdAt" -> "created_at"; "line_item" stays."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace("-", "_").lower()


def plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def field_label(name: str) -> str:
    """The label used in API error messages: "project_id" -> "Project id"."""
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def model_name(entity: str) -> str:
    name = pascal(entity)
    return name + "Record" if name in RESERVED_MODELS else name


def slug(text: str, *, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "app"


def has_dot_segment(path: str) -> bool:
    """"." and ".." segments are normalised away by URL handling, so they can never be matched literally."""
    return any(segment in (".", "..") for segment in path.split("/"))


def api_params(path: str) -> list[str]:
    return API_PARAM.findall(path)


def route_params(route: str) -> list[str]:
    return ROUTE_PARAM.findall(route)


def route_shape(path: str) -> str:
    """A path with every parameter replaced, for duplicate and conflict checks."""
    return ROUTE_PARAM.sub("{}", API_PARAM.sub("{}", path))
