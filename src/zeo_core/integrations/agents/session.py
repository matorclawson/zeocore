"""
Managed OpenAI Agents API sessions, keyless (zeocore-SOW-43).

This is the ``client.beta.agents`` product (OpenAI runs the agent harness), not the
``openai-agents`` SDK. The caller supplies the client, which reads ``OPENAI_API_KEY``
from its own environment; this module never accepts, stores or logs a key.

The primary tool route is the function-action seam: the agent asks for client-side
functions (``agent.session.requires_action``), the caller's handlers run them, and the
results go back in ONE ``agent.session.input.tool_result`` batch carrying each matching
``turn_id`` and ``call_id``. A ``(turn_id, call_id)`` runs its handler once: a repeated
action is answered from the cached reply. No public endpoint is needed. An MCP server
can be declared too (:func:`mcp_server`); that route needs an endpoint OpenAI can reach.

A briefing succeeds only when the turn completed AND its output was inspected: an idle
session or a closed stream is not success, and a failure or cancellation is sticky.
Everything is bounded (calls per turn, events per stream, a deadline between events,
argument size). A stream that blocks inside the client is bounded by the client's own
timeout: build the client with ``timeout=``. Refusals carry a code, never provider text.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

Status = Literal["completed", "failed", "cancelled", "incomplete"]
Handler = Callable[[Mapping[str, Any]], "str | Mapping[str, Any]"]

MAX_ROUNDS: Final = 8
MAX_CALLS_PER_TURN: Final = 16
MAX_EVENTS_PER_DRAIN: Final = 10_000
DEADLINE_SECONDS: Final = 300.0
MAX_ARGUMENT_BYTES: Final = 64 * 1024
MAX_RESULT_BYTES: Final = 4 * 1024 * 1024
_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_FAILURES: Final = {
    "agent.session.turn.failed": "failed",
    "agent.session.failed": "failed",
    "error": "failed",
    "agent.session.turn.cancelled": "cancelled",
}
_RANK: Final = {"failed": 3, "cancelled": 2, "completed": 1}


class AgentsRefused(ValueError):  # noqa: N818 -- a refusal with a code, not a fault
    """A refusal; ``code`` says why and never carries provider text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Transport(Protocol):
    """The two calls the module needs; :class:`SdkTransport` is the real one."""

    def start(
        self, *, agent: Mapping[str, Any], environment: Mapping[str, Any], text: str
    ) -> tuple[str, Iterable[Any]]:
        """Create a session with a first message; return its id and event stream."""
        ...

    def send(
        self, session_id: str, events: Sequence[Mapping[str, Any]]
    ) -> Iterable[Any]:
        """Post input events to a session in ONE call; return the event stream."""
        ...


class SdkTransport:
    """
    The documented ``client.beta.agents.sessions`` calls.

    Needs an ``openai`` release that has ``beta.agents``; without one it refuses with
    ``agents_api_unavailable`` rather than guess. A provider exception becomes
    ``provider_error`` (no text). The call shapes follow the OpenAI Agents API guides
    and are exercised against a fake in tests; the first run against the real service
    is the facilitator's.
    """

    def __init__(self, client: object) -> None:
        agents = getattr(getattr(client, "beta", None), "agents", None)
        if agents is None or not hasattr(agents, "sessions"):
            raise AgentsRefused("agents_api_unavailable")
        self._sessions = agents.sessions

    def start(
        self, *, agent: Mapping[str, Any], environment: Mapping[str, Any], text: str
    ) -> tuple[str, Iterable[Any]]:
        try:
            stream = self._sessions.create(
                agent=dict(agent),
                environment=dict(environment),
                input=text,
                stream=True,
            )
        except Exception:  # noqa: BLE001 -- coded, no provider text
            raise AgentsRefused("provider_error") from None
        session_id = getattr(stream, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise AgentsRefused("no_session_id")
        return session_id, stream

    def send(
        self, session_id: str, events: Sequence[Mapping[str, Any]]
    ) -> Iterable[Any]:
        try:
            stream: Iterable[Any] = self._sessions.events.create(
                session_id, events=[dict(event) for event in events], stream=True
            )
        except Exception:  # noqa: BLE001 -- coded, no provider text
            raise AgentsRefused("provider_error") from None
        return stream


class ScriptedTransport:
    """
    A test double: each ``start`` or ``send`` returns the next scripted event list.

    For consumers that want to exercise their handlers and their success logic without
    a key or a network. ``sent`` records every event posted back, in order, so a test
    can check that a function action was answered with the right ``turn_id`` and
    ``call_id``; ``batches`` records how many events each ``send`` carried.
    """

    def __init__(
        self, *scripts: Sequence[Mapping[str, Any]], session_id: str = "ses_test"
    ) -> None:
        self._scripts = [list(script) for script in scripts]
        self._session_id = session_id
        self.sent: list[Mapping[str, Any]] = []
        self.batches: list[int] = []
        self.started: dict[str, Any] | None = None

    def start(
        self, *, agent: Mapping[str, Any], environment: Mapping[str, Any], text: str
    ) -> tuple[str, Iterable[Any]]:
        self.started = {
            "agent": dict(agent),
            "environment": dict(environment),
            "text": text,
        }
        return self._session_id, self._next()

    def send(
        self, session_id: str, events: Sequence[Mapping[str, Any]]
    ) -> Iterable[Any]:
        if session_id != self._session_id:
            raise AgentsRefused("unknown_session")
        self.sent.extend(events)
        self.batches.append(len(events))
        return self._next()

    def _next(self) -> list[Mapping[str, Any]]:
        if not self._scripts:
            raise AgentsRefused("script_exhausted")
        return self._scripts.pop(0)


@dataclass(frozen=True)
class ToolCall:
    turn_id: str
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    status: Status
    output_text: str
    calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        """True only for a completed turn that produced inspected, non-blank output."""
        return self.status == "completed" and bool(self.output_text.strip())


def function_tool(
    name: str, description: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """A ``type: function`` tool declaration; closed parameters are enforced."""
    if (
        parameters.get("type") != "object"
        or parameters.get("additionalProperties") is not False
    ):
        raise AgentsRefused("tool_parameters_not_closed")
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }


def _public_host(host: str) -> bool:
    """A host name or address OpenAI could reach: not local, private or numeric."""
    lowered = host.lower().rstrip(".")
    if (
        not lowered
        or lowered == "localhost"
        or lowered.endswith((".localhost", ".local", ".internal", ".lan"))
    ):
        return False
    try:
        return ipaddress.ip_address(lowered).is_global
    except ValueError:
        pass
    # 127.1, 2130706433 and 0x7f.1 are numeric shorthands for an address: refuse them.
    return (
        not re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*", lowered)
        and "." in lowered
    )


def mcp_server(
    label: str,
    url: str,
    *,
    allowed_tools: Iterable[str],
    authorization_env_var: str | None = None,
) -> dict[str, Any]:
    """
    An optional MCP server declaration. The OpenAI side connects to ``url``, so it must
    be an ``https`` URL of a public host with no credential inside it. A credential is
    named by its environment variable, never passed as a value.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 -- raises ValueError on a bad port
    except ValueError:
        raise AgentsRefused("mcp_url_not_reachable") from None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or not _public_host(host)
    ):
        raise AgentsRefused("mcp_url_not_reachable")
    if authorization_env_var is not None and not _ENV_NAME.fullmatch(
        authorization_env_var
    ):
        raise AgentsRefused("mcp_credential_must_be_an_env_var_name")
    declared: dict[str, Any] = {
        "type": "mcp",
        "server_label": label,
        "transport": {"type": "http", "server_url": url},
        "connection_origin": "service",
        "allowed_tools": sorted(allowed_tools),
    }
    if authorization_env_var is not None:
        declared["authorization_env_var"] = authorization_env_var
    return declared


def _get(event: object, key: str) -> Any:  # noqa: ANN401 -- SDK objects or dicts
    return event.get(key) if isinstance(event, Mapping) else getattr(event, key, None)


def _refuse_constant(_: str) -> None:
    raise AgentsRefused("tool_arguments_not_json")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AgentsRefused("tool_arguments_not_json")
    return dict(pairs)


def _arguments(raw: object) -> Mapping[str, Any]:
    if isinstance(raw, str):
        if len(raw.encode()) > MAX_ARGUMENT_BYTES:
            raise AgentsRefused("tool_arguments_too_large")
        try:
            raw = json.loads(
                raw,
                object_pairs_hook=_no_duplicates,
                parse_constant=_refuse_constant,
            )
        except ValueError, RecursionError:
            raise AgentsRefused("tool_arguments_not_json") from None
    if not isinstance(raw, Mapping):
        raise AgentsRefused("malformed_function_call")
    return raw


def _calls(event: object) -> list[ToolCall]:
    actions = _get(event, "required_actions")
    if actions is None:
        actions = [event]
    if not isinstance(actions, list | tuple):
        raise AgentsRefused("malformed_function_call")
    found = []
    for item in actions:
        if _get(item, "type") != "function_call":
            continue
        turn_id, call_id, name = (_get(item, k) for k in ("turn_id", "call_id", "name"))
        if not (
            isinstance(turn_id, str)
            and isinstance(call_id, str)
            and isinstance(name, str)
        ):
            raise AgentsRefused("malformed_function_call")
        found.append(
            ToolCall(turn_id, call_id, name, _arguments(_get(item, "arguments")))
        )
    return found


def _drain(
    stream: Iterable[Any], deadline: float, max_events: int
) -> tuple[Status | None, str, list[ToolCall], str | None]:
    """Read one stream to its end: strongest terminal status, text, calls, a limit."""
    status: Status | None = None
    text, calls, limit = "", [], None
    for count, event in enumerate(stream, start=1):
        if count > max_events:
            limit = "too_many_events"
            break
        if time.monotonic() > deadline:
            limit = "deadline_exceeded"
            break
        kind = _get(event, "type")
        if not isinstance(kind, str):
            raise AgentsRefused("malformed_event")
        if kind == "agent.session.turn.output_text.done":
            done = _get(event, "text")
            text = done if isinstance(done, str) else ""
        elif kind == "agent.session.requires_action":
            calls.extend(_calls(event))
        elif kind == "agent.session.turn.completed":
            seen: Status = "completed"
            status = seen if status is None else status
        elif kind in _FAILURES:
            seen = _FAILURES[kind]  # type: ignore[assignment]
            if status is None or _RANK[seen] > _RANK[status]:
                status = seen
    return status, text, calls, limit


def _result(call: ToolCall, handlers: Mapping[str, Handler]) -> dict[str, Any]:
    handler = handlers.get(call.name)
    if handler is None:
        return _failed(call, "unknown_tool")
    try:
        output = handler(call.arguments)
        encoded = (
            output if isinstance(output, str) else json.dumps(output, sort_keys=True)
        )
    except Exception:  # noqa: BLE001 -- a handler fault becomes a coded failure result
        return _failed(call, "tool_failed")
    if len(encoded.encode()) > MAX_RESULT_BYTES:
        return _failed(call, "tool_result_too_large")
    return {
        "type": "agent.session.input.tool_result",
        "turn_id": call.turn_id,
        "call_id": call.call_id,
        "success": True,
        "output": encoded,
    }


def _failed(call: ToolCall, code: str) -> dict[str, Any]:
    return {
        "type": "agent.session.input.tool_result",
        "turn_id": call.turn_id,
        "call_id": call.call_id,
        "success": False,
        "error": code,
    }


def run(  # noqa: PLR0913 -- the bounds are separate, named, keyword-only knobs
    transport: Transport,
    *,
    agent: Mapping[str, Any],
    environment: Mapping[str, Any],
    text: str,
    handlers: Mapping[str, Handler] | None = None,
    session_id: str | None = None,
    max_rounds: int = MAX_ROUNDS,
    max_calls_per_turn: int = MAX_CALLS_PER_TURN,
    max_events: int = MAX_EVENTS_PER_DRAIN,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> TurnResult:
    """
    Run one user message to a terminal result, answering function actions as they come.

    With ``session_id`` the message continues that session. Status is ``completed`` only
    when ``agent.session.turn.completed`` arrived and no failure or cancellation did; a
    stream that ends without a terminal event is ``incomplete`` and never ``ok``. The
    output is the terminal round's text, stripped. Each ``(turn_id, call_id)`` runs its
    handler once; a repeat is answered from the cached reply.
    """
    handlers = handlers or {}
    deadline = time.monotonic() + deadline_seconds
    if session_id is None:
        session_id, stream = transport.start(
            agent=agent, environment=environment, text=text
        )
    else:
        message = {
            "type": "agent.session.input.message",
            "content": [{"type": "input_text", "text": text}],
        }
        stream = transport.send(session_id, [message])
    answered: dict[tuple[str, str], dict[str, Any]] = {}
    seen: list[ToolCall] = []
    for _ in range(max_rounds):
        status, output, calls, limit = _drain(stream, deadline, max_events)
        if limit is not None:
            return TurnResult(session_id, "incomplete", "", tuple(seen), limit)
        if calls and status is None:
            if len(answered) + len(calls) > max_calls_per_turn:
                return TurnResult(
                    session_id, "incomplete", "", tuple(seen), "too_many_calls"
                )
            replies = []
            for call in calls:
                key = (call.turn_id, call.call_id)
                if key not in answered:
                    answered[key] = _result(call, handlers)
                    seen.append(call)
                if answered[key] not in replies:
                    replies.append(answered[key])
            stream = transport.send(session_id, replies)
            continue
        final: Status = status or "incomplete"
        output = output.strip()
        return TurnResult(session_id, final, output, tuple(seen), _why(final, output))
    return TurnResult(session_id, "incomplete", "", tuple(seen), "too_many_rounds")


def _why(status: Status, output: str) -> str | None:
    if status == "completed":
        return None if output else "completed_without_output"
    return {"failed": "turn_failed", "cancelled": "turn_cancelled"}.get(
        status, "no_terminal_event"
    )
