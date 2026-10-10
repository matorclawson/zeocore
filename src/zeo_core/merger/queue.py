"""The Architect's merge register (events-v1.jsonl, QUEUE-SPEC-v1), read strictly.

The merger only reads it. A PR's state is its last event, and a ready entry is
a candidate only once every PR in its ``after`` list has a merged event. An
unknown field, a missing field, a malformed line or an unknown event refuses
the whole register, so a damaged or forged line can never be skipped past
into a merge. (Port of zeomerger internal/queue.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

_SHA40: Final = re.compile(r"[0-9a-f]{40}")
_REPO: Final = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_AFTER: Final = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")
_MAX_LINE: Final = 1 << 20

_FIELDS: Final = frozenset(
    {
        "v", "event", "time", "repo", "repo_id", "pr", "head", "head_ref",
        "base_ref", "base_sha_at_review", "author_seat", "after", "receipt_id",
        "approved_by", "evidence", "note", "reason", "merge_commit",
        "merged_head", "merged_head_was_ready",
    }
)  # fmt: skip
_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "ready": (
        "v", "event", "time", "repo", "repo_id", "pr", "head", "head_ref",
        "base_ref", "base_sha_at_review", "author_seat", "after", "receipt_id",
        "approved_by",
    ),
    "held": ("v", "event", "time", "repo", "pr", "head", "reason"),
    "withdrawn": ("v", "event", "time", "repo", "pr", "head", "reason"),
    "merged": (
        "v", "event", "time", "repo", "pr", "merge_commit", "merged_head",
        "merged_head_was_ready",
    ),
}  # fmt: skip


class RegisterError(ValueError):
    """The register can't be trusted as a whole."""


@dataclass(frozen=True)
class Event:
    v: int
    event: str
    time: str
    repo: str
    pr: int
    line: int
    repo_id: int = 0
    head: str = ""
    head_ref: str = ""
    base_ref: str = ""
    base_sha: str = ""
    author_seat: str = ""
    after: tuple[str, ...] = ()
    receipt: str = ""
    approver: str = ""
    reason: str = ""
    merge_commit: str = ""
    merged_head: str = ""


@dataclass(frozen=True)
class Candidate:
    """A PR whose last event is ready; ``blocked`` lists unmerged after entries."""

    event: Event
    blocked: tuple[str, ...] = ()

    # Shorthands, as the Go Candidate embeds its Event.
    @property
    def repo(self) -> str:
        return self.event.repo

    @property
    def pr(self) -> int:
        return self.event.pr

    @property
    def head(self) -> str:
        return self.event.head

    @property
    def base_ref(self) -> str:
        return self.event.base_ref

    @property
    def base_sha(self) -> str:
        return self.event.base_sha

    @property
    def repo_id(self) -> int:
        return self.event.repo_id

    @property
    def receipt(self) -> str:
        return self.event.receipt

    @property
    def line(self) -> int:
        return self.event.line


@dataclass
class Register:
    events: list[Event] = field(default_factory=list)
    _last: dict[tuple[str, int], Event] = field(default_factory=dict)
    _merged: set[tuple[str, int]] = field(default_factory=set)

    def candidates(self) -> list[Candidate]:
        """Every PR whose last event is ready, in register order."""
        out: list[Candidate] = []
        for event in self._last.values():
            if event.event != "ready":
                continue
            blocked = []
            for entry in event.after:
                match = _AFTER.fullmatch(entry)
                if match is None:  # validated at parse; never trusted blindly
                    raise RegisterError(f'bad after entry "{entry}"')
                if (match[1], int(match[2])) not in self._merged:
                    blocked.append(entry)
            out.append(Candidate(event, tuple(blocked)))
        return sorted(out, key=lambda candidate: candidate.line)

    def state(self, repo: str, pr: int) -> str:
        event = self._last.get((repo.strip(), pr))
        return event.event if event is not None else ""


def parse_register(text: str) -> Register:
    """Parse the whole register, or refuse it."""
    register = Register()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    for number, raw in enumerate(lines, start=1):
        if len(raw) > _MAX_LINE:
            raise RegisterError(f"line {number}: too long")
        if not raw.strip():
            raise RegisterError(f"line {number}: empty line")
        try:
            data = json.loads(raw, object_pairs_hook=_no_duplicates)
        except ValueError as error:
            raise RegisterError(f"line {number}: {error}") from None
        if not isinstance(data, dict):
            raise RegisterError(f"line {number}: not an object")
        try:
            event = _event(data, number)
        except (RegisterError, TypeError, ValueError) as error:
            raise RegisterError(f"line {number}: {error}") from None
        register.events.append(event)
        key = (event.repo, event.pr)
        register._last[key] = event
        if event.event == "merged":
            register._merged.add(key)
    return register


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate field {key}")
        out[key] = value
    return out


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegisterError(f"{name} is not an integer")
    return value


def _str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RegisterError(f"{name} is not a string")
    return value


def _event(  # noqa: C901 -- the Go reader's field checks, in order
    data: dict[str, Any], line: int
) -> Event:
    unknown = set(data) - _FIELDS
    if unknown:
        raise RegisterError(f"unknown field {sorted(unknown)[0]}")
    if _int(data.get("v"), "v") != 1:
        raise RegisterError(f"v is {data.get('v')}, want 1")
    kind = _str(data.get("event"), "event")
    if kind not in _REQUIRED:
        raise RegisterError(f'unknown event "{kind}"')
    for name in _REQUIRED[kind]:
        if name not in data:
            raise RegisterError(f"{kind} event lacks {name}")
    repo = _str(data["repo"], "repo")
    pr = _int(data["pr"], "pr")
    if not _REPO.fullmatch(repo) or pr <= 0:
        raise RegisterError("bad repo or pr")
    after = data.get("after") or []
    if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
        raise RegisterError("after is not a list of strings")
    event = Event(
        v=1,
        event=kind,
        time=_str(data["time"], "time"),
        repo=repo,
        pr=pr,
        line=line,
        repo_id=_int(data.get("repo_id", 0), "repo_id"),
        head=_str(data.get("head", ""), "head"),
        head_ref=_str(data.get("head_ref", ""), "head_ref"),
        base_ref=_str(data.get("base_ref", ""), "base_ref"),
        base_sha=_str(data.get("base_sha_at_review", ""), "base_sha_at_review"),
        author_seat=_str(data.get("author_seat", ""), "author_seat"),
        after=tuple(after),
        receipt=_str(data.get("receipt_id", ""), "receipt_id"),
        approver=_str(data.get("approved_by", ""), "approved_by"),
        reason=_str(data.get("reason", ""), "reason"),
        merge_commit=_str(data.get("merge_commit", ""), "merge_commit"),
        merged_head=_str(data.get("merged_head", ""), "merged_head"),
    )
    if kind == "ready":
        if (
            not _SHA40.fullmatch(event.head)
            or not _SHA40.fullmatch(event.base_sha)
            or event.repo_id <= 0
            or not event.base_ref
        ):
            raise RegisterError(
                "ready event has a bad head, base sha, repo_id or base_ref"
            )
        want = f"apr1-{event.repo_id}-{event.pr}-{event.head}"
        if event.receipt != want:
            raise RegisterError(f'receipt_id "{event.receipt}", want "{want}"')
        for entry in event.after:
            if not _AFTER.fullmatch(entry):
                raise RegisterError(f'bad after entry "{entry}"')
    elif kind in ("held", "withdrawn"):
        if not _SHA40.fullmatch(event.head):
            raise RegisterError(f"{kind} event has a bad head")
    elif kind == "merged":
        if not _SHA40.fullmatch(event.merge_commit) or not _SHA40.fullmatch(
            event.merged_head
        ):
            raise RegisterError("merged event has a bad sha")
        if not isinstance(data["merged_head_was_ready"], bool):
            raise RegisterError("merged_head_was_ready is not a boolean")
    return event
