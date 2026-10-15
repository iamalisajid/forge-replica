"""Parse Playwright ai-mode aria snapshots and synthesise portable, unique role locators from refs.

A snapshot line looks like ``  - button "Delete" [ref=e13] [cursor=pointer]``. Indentation gives the tree,
which is how a duplicate element gets scoped to its row, list item, dialog or form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from forge.models.scenario import Target

LINE = re.compile(r'^(?P<indent>\s*)- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
REF = re.compile(r"\[ref=([A-Za-z0-9_-]+)\]")
TEXT_CHILD = re.compile(r'^\s*- (?:text|cell|paragraph|link|heading)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?::\s*(?P<text>.+))?')
CONTAINERS = ("row", "listitem", "dialog", "form", "article", "region", "group", "navigation", "tabpanel")


@dataclass
class Node:
    role: str
    name: str | None
    ref: str | None
    indent: int
    line_no: int
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    text_hint: str | None = None     # first visible text inside (for unnamed rows and dialogs)


def parse(snapshot: str) -> dict[str, Node]:
    """Return ref -> Node, with parent links."""
    by_ref: dict[str, Node] = {}
    stack: list[Node] = []
    for i, raw in enumerate(snapshot.splitlines()):
        m = LINE.match(raw)
        if not m:
            continue
        indent = len(m.group("indent"))
        name = m.group("name")
        ref_match = REF.search(m.group("rest") or "")
        node = Node(role=m.group("role"), name=name.replace('\\"', '"') if name is not None else None,
                    ref=ref_match.group(1) if ref_match else None, indent=indent, line_no=i)
        while stack and stack[-1].indent >= indent:
            stack.pop()
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
        stack.append(node)
        if node.ref:
            by_ref[node.ref] = node
        # Record the first named descendant text on every unnamed ancestor (row/dialog scoping).
        label = name or _inline_text(raw)
        if label:
            for ancestor in stack[:-1]:
                if ancestor.text_hint is None and not ancestor.name:
                    ancestor.text_hint = label
    return by_ref


def _inline_text(raw: str) -> str | None:
    m = re.search(r":\s*(.+)$", raw)
    if m and not m.group(1).startswith("/"):
        text = m.group(1).strip().strip('"')
        return text or None
    return None


def _container(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if cur.role in CONTAINERS:
            return cur
        cur = cur.parent
    return None


def target_for(node: Node, *, scoped: bool) -> Target:
    container = _container(node) if scoped else None
    within_role = container.role if container else None
    within_name = container.name if container and container.name else None
    within_text = None
    if container and not container.name:
        within_text = container.text_hint
    return Target(role=node.role, name=node.name if node.name else None, exact=True, has_text=None,
                  within_role=within_role, within_name=within_name, within_has_text=within_text)


async def portable_target(page, snapshot: str, ref: str) -> tuple[Target | None, bool]:
    """Synthesise a Target for ``ref`` that resolves to exactly one element. Returns (target, portable)."""
    nodes = parse(snapshot)
    node = nodes.get(ref)
    if node is None:
        return None, False
    candidates = [node]
    role, name = await engine_name(page, ref)
    if role == node.role and name and name != node.name:
        # The snapshot's label can differ from the accessible name get_by_role matches (e.g. a description
        # appended to the name); the role engine's own name is the one replays will see.
        candidates.insert(0, Node(**{**node.__dict__, "name": name}))
    for cand in candidates:
        for scoped in (False, True):
            target = target_for(cand, scoped=scoped)
            if target.name is None and target.within_role is None:
                continue
            try:
                if await resolve(page, target).count() == 1:
                    return target, True
            except Exception:  # noqa: BLE001 - an unresolvable candidate just isn't portable
                continue
    return target_for(candidates[0], scoped=True), False


async def engine_name(page, ref: str) -> tuple[str | None, str | None]:
    """Role and accessible name of ``ref`` as Playwright's role engine computes them (what get_by_role matches)."""
    try:
        text = await page.locator(f"aria-ref={ref}").aria_snapshot(timeout=2000)
    except Exception:  # noqa: BLE001 - best effort
        return None, None
    m = re.match(r'\s*-\s*([a-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?', text or "")
    return (m.group(1), m.group(2).replace('\\"', '"') if m.group(2) else None) if m else (None, None)


def resolve(page, target: Target):
    """The same resolution rules the scenario interpreter uses (kept here so the harness can check uniqueness)."""
    scope = page
    if target.within_role:
        scope = page.get_by_role(target.within_role, name=target.within_name, exact=True) if target.within_name \
            else page.get_by_role(target.within_role)
        if target.within_has_text:
            scope = scope.filter(has_text=target.within_has_text)
    loc = scope.get_by_role(target.role, name=target.name, exact=target.exact) if target.name \
        else scope.get_by_role(target.role)
    if target.has_text:
        loc = loc.filter(has_text=target.has_text)
    return loc


def comboboxes(snapshot: str) -> list[tuple[str, list[str]]]:
    """Every combobox with its options: [("Status", ["open", "in_progress", "closed"]), ...]."""
    out: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str], int] | None = None
    for raw in snapshot.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        indent = len(m.group("indent"))
        if current and indent <= current[2]:
            out.append((current[0], current[1]))
            current = None
        if m.group("role") == "combobox" and m.group("name"):
            current = (m.group("name"), [], indent)
        elif current and m.group("role") == "option" and m.group("name") is not None:
            current[1].append(m.group("name"))
    if current:
        out.append((current[0], current[1]))
    return out
