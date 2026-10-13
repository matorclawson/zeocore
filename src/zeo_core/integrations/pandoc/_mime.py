"""
Decoding one raw RFC 822 message (ZEOCORE-SOW-33, section 2A).

Standard-library ``email`` only: charset and transfer decoding. No attachment is
extracted, no link is followed, nothing is fetched. The message is bounded before
and while it is read (bytes, header bytes, number of headers, parts and nesting),
and every exception the email package can raise on a hostile message becomes a
typed refusal, so a caller sees ``NewsletterRefusalError`` or a result, never a
traceback.

The sender is a CLAIM. ``From`` can say anything, so ``DecodedMessage`` also carries
what Gmail's own mail server concluded: the TOPMOST ``Authentication-Results`` header
only, and only when it names ``mx.google.com`` as its authserv-id. Gmail prepends its
own header, so every header below it was written by the sender or a hop on the way,
and is ignored. ``sender_verified`` is True only for ``dmarc=pass`` where the domain
DMARC checked equals the domain in ``From``.
"""

from __future__ import annotations

import email
import email.parser
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from typing import Any, Final

from zeo_core.integrations.pandoc._runner import Deadline
from zeo_core.integrations.pandoc.refusals import refusal

RAW_MAX_BYTES: Final = 10_485_760
MAX_HEADER_BYTES: Final = 262_144
MAX_HEADERS: Final = 1_000
MAX_MIME_PARTS: Final = 64
MAX_MIME_DEPTH: Final = 8
DEFAULT_TIMEOUT_S: Final = 30.0

#: The message is handed to the parser in pieces of this size, so the deadline is
#: checked between them; the parser itself cannot be interrupted inside one piece.
_FEED_CHUNK: Final = 65_536
#: A cheap pre-scan bound on the number of parts: no message with at most
#: ``MAX_MIME_PARTS`` parts has more than twice that many ``Content-Type`` headers.
_PART_HEADER: Final = re.compile(rb"^content-type:", re.IGNORECASE | re.MULTILINE)

#: The only authserv-id whose Authentication-Results is believed.
TRUSTED_AUTHSERV_ID: Final = "mx.google.com"

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_METHODS: Final = ("dkim", "spf", "dmarc")
_AUTH_STOP: Final = frozenset(' \t\r\n;="()/')


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    """
    The bodies and a few headers of one decoded RFC 822 message.

    ``list_unsubscribe`` is data and never an instruction: nothing may act on it.
    ``authentication`` and ``sender_verified`` say what Gmail concluded about the
    sender; ``sender`` itself is only what the message claims.
    """

    html: str | None
    text: str | None
    sender: str
    date: str
    subject: str
    message_id: str
    list_unsubscribe: str
    warnings: tuple[str, ...] = field(default=())
    authentication: dict[str, str] = field(default_factory=dict)
    sender_verified: bool = False


def _header(message: Any, name: str) -> str:  # noqa: ANN401 -- an email Message
    try:
        value = str(message.get(name, "") or "")
    except ValueError, LookupError, TypeError, AttributeError:
        return ""
    return _CONTROL.sub(" ", value).strip()[:1000]


def _walk_limits(message: Any, depth: int, seen: list[int]) -> None:  # noqa: ANN401
    seen[0] += 1
    if seen[0] > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH:
        raise refusal("message_too_complex")
    if message.is_multipart():
        for part in message.get_payload():
            _walk_limits(part, depth + 1, seen)


def _body(message: Any, kind: str, warnings: list[str]) -> str | None:  # noqa: ANN401
    try:
        part = message.get_body(preferencelist=(kind,))
        if part is None:
            return None
        content = part.get_content()
    except LookupError, ValueError, UnicodeError, KeyError, AttributeError:
        warnings.append(f"the {kind} body could not be decoded and was skipped")
        return None
    return content if isinstance(content, str) else None


def _date_text(raw: str, warnings: list[str]) -> str:
    if not raw:
        return ""
    try:
        return email.utils.parsedate_to_datetime(raw).isoformat()
    except TypeError, ValueError, IndexError:
        warnings.append("the Date header is not a valid date and was kept as written")
        return raw


def _skip_comment(value: str, start: int) -> int | None:
    """The index after the comment opening at ``start``; ``None`` if unfinished."""
    depth, i, n = 1, start + 1, len(value)
    while i < n and depth:
        if value[i] == "\\":
            i += 1
        elif value[i] == "(":
            depth += 1
        elif value[i] == ")":
            depth -= 1
        i += 1
    return None if depth else i


def _read_quoted(value: str, start: int) -> tuple[str, int] | None:
    """The text of the quoted string at ``start`` and the index after it."""
    j, n, text = start + 1, len(value), []
    while j < n and value[j] != '"':
        if value[j] == "\\" and j + 1 < n:
            j += 1
        text.append(value[j])
        j += 1
    return None if j >= n else ("".join(text), j + 1)


def _auth_tokens(value: str) -> list[tuple[str, str]] | None:
    """
    Tokenise an Authentication-Results value per RFC 8601, or ``None`` if malformed.

    Comments (nestable, with backslash escapes) are dropped. A quoted string is ONE
    token of kind ``quoted``, so a ``;`` or ``=`` inside it is text and can never
    start a result. ``;``, ``=`` and ``/`` are tokens of their own kind; anything
    else up to a separator is an ``atom``.
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            skipped = _skip_comment(value, i)
            if skipped is None:
                return None  # a comment that never ends
            i = skipped
        elif c == '"':
            quoted = _read_quoted(value, i)
            if quoted is None:
                return None  # a quoted string that never ends
            out.append(("quoted", quoted[0]))
            i = quoted[1]
        elif c in ";=/":
            out.append((c, c))
            i += 1
        else:
            j = i
            while j < n and value[j] not in _AUTH_STOP:
                j += 1
            if j == i:
                return None  # a stray ")"
            out.append(("atom", value[i:j]))
            i = j
    return out


def _auth_clause(
    clause: list[tuple[str, str]],
) -> tuple[str, str, list[str]] | None:
    """One ``method=result`` clause as (method, result, ``header.from`` values)."""
    if not clause or clause[0][0] != "atom":
        return None
    method, i = clause[0][1].lower(), 1
    if i + 1 < len(clause) and clause[i][0] == "/":
        i += 2  # the method's version
    if i + 1 >= len(clause) or clause[i][0] != "=" or clause[i + 1][0] != "atom":
        return None
    result, i = clause[i + 1][1].lower(), i + 2
    header_from: list[str] = []
    while i + 2 < len(clause) + 0 and i + 2 <= len(clause) - 1 + 0:
        key, eq, value = clause[i], clause[i + 1], clause[i + 2]
        if key[0] != "atom" or eq[0] != "=" or value[0] not in ("atom", "quoted"):
            break
        if key[1].lower() == "header.from":
            header_from.append(value[1].lower().rstrip("."))
        i += 3
    return method, result, header_from


def parse_authentication(value: str) -> dict[str, str]:
    """
    The dkim, spf and dmarc results in ONE Authentication-Results value, or ``{}``.

    Empty unless the value's authserv-id is exactly ``mx.google.com``. The value is
    tokenised per RFC 8601 (quoted strings and comments are never structure). A
    method that appears more than once is ``ambiguous``, and ``dmarc_header_from`` is
    set only for a single ``dmarc`` result that names exactly one ``header.from``.
    """
    tokens = _auth_tokens(value)
    if tokens is None:
        return {}
    clauses: list[list[tuple[str, str]]] = [[]]
    for token in tokens:
        if token[0] == ";":
            clauses.append([])
        else:
            clauses[-1].append(token)
    head = clauses[0]
    if (
        not head
        or head[0][0] != "atom"
        or head[0][1].lower() != TRUSTED_AUTHSERV_ID
        or len(head) > 2
        or (len(head) == 2 and (head[1][0] != "atom" or not head[1][1].isdigit()))
    ):
        return {}
    results: dict[str, list[str]] = {}
    dmarc_from: list[str] = []
    for clause in clauses[1:]:
        parsed = _auth_clause(clause)
        if parsed is None or parsed[0] not in _METHODS:
            continue
        results.setdefault(parsed[0], []).append(parsed[1])
        if parsed[0] == "dmarc":
            dmarc_from.extend(parsed[2])
    found: dict[str, str] = {"authserv_id": TRUSTED_AUTHSERV_ID}
    for method, values in results.items():
        found[method] = values[0] if len(values) == 1 else "ambiguous"
    if len(results.get("dmarc", [])) == 1 and len(dmarc_from) == 1:
        found["dmarc_header_from"] = dmarc_from[0]
    return found


def _authentication(message: Any, sender: str) -> tuple[dict[str, str], bool]:  # noqa: ANN401
    """Gmail's verdict on the sender, from the topmost Authentication-Results only."""
    try:
        values = message.get_all("Authentication-Results") or []
        top = _CONTROL.sub(" ", str(values[0])) if values else ""
    except ValueError, LookupError, TypeError, AttributeError, IndexError:
        return {}, False
    result = parse_authentication(top[:4000])
    if not result:
        return {}, False
    domain = email.utils.parseaddr(sender)[1].rpartition("@")[2].lower().rstrip(".")
    verified = (
        result.get("dmarc") == "pass"
        and bool(domain)
        and result.get("dmarc_header_from") == domain
    )
    return result, verified


def _bound(raw: bytes, max_bytes: int) -> None:
    """Refuse an oversized message before the email package sees a byte of it."""
    if not raw:
        raise refusal("message_empty")
    if len(raw) > max_bytes:
        raise refusal("message_too_large")
    split = min(
        (i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i >= 0),
        default=len(raw),
    )
    if split > MAX_HEADER_BYTES or raw.count(b"\n", 0, split) > 4 * MAX_HEADERS:
        raise refusal("message_too_complex")
    for count, _ in enumerate(_PART_HEADER.finditer(raw), 1):
        if count > 2 * MAX_MIME_PARTS:
            raise refusal("message_too_complex")


def decode_raw_message(
    raw: bytes,
    *,
    max_bytes: int = RAW_MAX_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> DecodedMessage:
    """
    Decode one raw RFC 822 message into its bodies and headers.

    Uses the standard-library ``email`` package with ``policy.default`` and
    ``get_body``: charset and transfer decoding only. No attachment is extracted, no
    link is followed, nothing is fetched. A message over ``max_bytes``, with over
    256 KiB of headers or 1000 headers, with more than 64 parts, or nested deeper
    than 8 levels, is refused, and so is anything the email package cannot read.

    ``timeout_s`` is checked between the 64 KiB pieces handed to the parser and
    between the steps after it. The parser cannot be interrupted inside one piece, so
    a caller reading hostile mail should still run this under its own watchdog (a
    process or thread timeout); a child process with a resource limit is planned.
    """
    deadline = Deadline(timeout_s)
    _bound(raw, max_bytes)
    try:
        return _decode(raw, deadline)
    except Exception as error:  # noqa: BLE001 -- every email-package failure refuses
        if getattr(error, "code", None) is not None:
            raise
        raise refusal("message_too_complex") from error


def _parse(raw: bytes, deadline: Deadline) -> Any:  # noqa: ANN401 -- an email Message
    parser = email.parser.BytesFeedParser(policy=email.policy.default)
    for start in range(0, len(raw), _FEED_CHUNK):
        parser.feed(raw[start : start + _FEED_CHUNK])
        deadline.check()
    return parser.close()


def _decode(raw: bytes, deadline: Deadline) -> DecodedMessage:
    message = _parse(raw, deadline)
    if len(message.keys()) > MAX_HEADERS:
        raise refusal("message_too_complex")
    _walk_limits(message, 1, [0])
    deadline.check()
    warnings: list[str] = []
    html_body = _body(message, "html", warnings)
    deadline.check()
    text_body = _body(message, "plain", warnings)
    deadline.check()
    if html_body is None and text_body is None:
        warnings.append("the message has no text or HTML body")
    sender = _header(message, "From")
    authentication, verified = _authentication(message, sender)
    return DecodedMessage(
        html=html_body,
        text=text_body,
        sender=sender,
        date=_date_text(_header(message, "Date"), warnings),
        subject=_header(message, "Subject"),
        message_id=_header(message, "Message-ID"),
        list_unsubscribe=_header(message, "List-Unsubscribe"),
        warnings=tuple(warnings),
        authentication=authentication,
        sender_verified=verified,
    )
