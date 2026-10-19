"""
A bounded reading of the selectors of a ``<style>`` rule, to prove a rule CANNOT match.

A rule with a selector that is not a plain ``.class`` or ``#id`` used to mark the whole
sheet as not modelled whenever its body could set a property a proof depends on. Here
such a rule is kept as a *pending* selector instead, and each candidate element is
checked against it: the rule leaves the proof alone only if the selector, read in the
grammar below, provably cannot match the candidate and its ancestors. Anything outside
the grammar, or over a bound, still marks the sheet as not modelled (fail closed).

The grammar is a comma list of descendant chains. A chain is compounds separated by
ASCII whitespace; a compound is an optional type (``td``) followed by any number of
``.class`` and ``#id`` parts, and is not empty. Names are ASCII. Not read, so not
modelled: attribute selectors, pseudo-classes and pseudo-elements, the combinators
``>``, ``+`` and ``~``, ``*``, namespaces, escapes, ``:is``/``:where``/``:not``, and
every at-rule. Class, id and type names are compared case-insensitively (a browser
does so for class and id in quirks mode, which an HTML fragment may be in), which can
only make a rule *more* likely to match.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: Pending selectors are stored in the id table under this key, joined by ``\x01``.
PENDING: Final = "\x00pending"
_JOIN: Final = "\x01"
#: At most this many selectors are examined for one sheet; more mark it not modelled.
MAX_PENDING: Final = 32
#: At most this many compounds in one chain.
MAX_COMPOUNDS: Final = 8

_ASCII_SPACE: Final = re.compile(r"[ \t\n\f\r]+")
_COMPOUND: Final = re.compile(
    r"^(?P<type>[A-Za-z][A-Za-z0-9-]*)?(?P<parts>(?:[.#][A-Za-z_][A-Za-z0-9_-]*)*)$"
)
_PART: Final = re.compile(r"([.#])([A-Za-z_][A-Za-z0-9_-]*)")

Compound = tuple[str, tuple[str, ...], tuple[str, ...]]  # type, classes, ids


def parse_chain(selector: str) -> tuple[Compound, ...] | None:
    """The compounds of one descendant chain, or None when it is outside the grammar."""
    words = _ASCII_SPACE.split(selector.strip(" \t\n\f\r"))
    if not words or words == [""] or len(words) > MAX_COMPOUNDS:
        return None
    chain: list[Compound] = []
    for word in words:
        found = _COMPOUND.match(word)
        if found is None or not word:
            return None
        parts = _PART.findall(found.group("parts"))
        kind = (found.group("type") or "").lower()
        if not kind and not parts:
            return None
        chain.append(
            (
                kind,
                tuple(name.lower() for mark, name in parts if mark == "."),
                tuple(name.lower() for mark, name in parts if mark == "#"),
            )
        )
    return tuple(chain)


def add_pending(ids: dict[str, str], selector: str) -> bool:
    """Keep ``selector`` to be checked per element; False when a bound is passed."""
    current = ids.get(PENDING)
    parts = current.split(_JOIN) if current else []
    if selector in parts:
        return True
    if len(parts) >= MAX_PENDING:
        return False
    parts.append(selector)
    ids[PENDING] = _JOIN.join(parts)
    return True


def _matches(element: Any, compound: Compound) -> bool:  # noqa: ANN401
    kind, classes, ids = compound
    if getattr(element, "name", None) is None or not hasattr(element, "get"):
        return False
    if kind and str(element.name).lower() != kind:
        return False
    if classes:
        raw = element.get("class") or ""
        if not isinstance(raw, str):
            raw = " ".join(raw)
        have = {token.lower() for token in _ASCII_SPACE.split(raw.strip(" \t\n\f\r"))}
        if not all(name in have for name in classes):
            return False
    if ids:
        value = element.get("id")
        if not isinstance(value, str) or any(value.lower() != name for name in ids):
            return False
    return True


def may_match(tag: Any, ids: dict[str, str]) -> bool:  # noqa: ANN401
    """Whether any pending selector can match ``tag`` (read through its ancestors)."""
    stored = ids.get(PENDING)
    if not stored:
        return False
    for selector in stored.split(_JOIN):
        chain = parse_chain(selector)
        if chain is None:
            return True  # not readable: it cannot be proven not to match
        if not _matches(tag, chain[-1]):
            continue
        wanted = len(chain) - 2
        for ancestor in tag.parents:
            if wanted < 0:
                break
            if _matches(ancestor, chain[wanted]):
                wanted -= 1
        if wanted < 0:
            return True
    return False
