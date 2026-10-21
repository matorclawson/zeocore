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
Everything is bounded (calls per turn, events per stream, argument size) with one
limit to read exactly: ``deadline_seconds`` covers the WHOLE run but is checked only
when an event ARRIVES at this loop. A read that blocks is bounded by the client's own
per-read timeout alone (build the client with ``timeout=``), and keep-alive frames the
client swallows never reach this loop, so they do not advance the deadline. A stream
that trickles real events is cut at the deadline; a stream that never yields an event
is cut only by the client timeout. Errors raised by the provider while a stream is
being read become ``provider_error``, like those of ``create`` and ``send``. Refusals
carry a code, never provider text.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

Status = Literal["completed", "failed", "cancelled", "incomplete"]
Handler = Callable[[Mapping[str, Any]], "str | Mapping[str, Any]"]

MAX_ROUNDS: Final = 8
MAX_CALLS_PER_TURN: Final = 16
MAX_EVENTS_PER_DRAIN: Final = 10_000
DEADLINE_SECONDS: Final = 300.0
CLIENT_TIMEOUT_SECONDS: Final = 60.0
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
    The calls as the Agents API guides summarise them (see :func:`run_native` instead).

    NOT the real SDK's contract: in ``openai`` 3.36.0 ``Events.create`` returns ``None``
    and takes no ``stream``, and the SDK dispatches function actions itself. Use
    :func:`run_native` against the real SDK; this adapter remains for transports that
    really do hand back a stream per post.

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
    if isinstance(allowed_tools, str | bytes):
        raise AgentsRefused("allowed_tools_must_be_a_list")
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


def _finite_float(token: str) -> float:
    value = float(token)
    if value in (float("inf"), float("-inf")):
        raise AgentsRefused("tool_arguments_not_json")
    return value


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
                parse_float=_finite_float,
            )
        except (ValueError, RecursionError):
            raise AgentsRefused("tool_arguments_not_json") from None
    if not isinstance(raw, Mapping):
        raise AgentsRefused("malformed_function_call")
    try:
        size = len(json.dumps(raw, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError):
        raise AgentsRefused("malformed_function_call") from None
    if size > MAX_ARGUMENT_BYTES:
        raise AgentsRefused("tool_arguments_too_large")
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


def _coded(stream: Iterable[Any]) -> Iterator[Any]:
    """Iterate a provider stream; whatever it raises becomes ``provider_error``."""
    try:
        iterator = iter(stream)
        while True:
            try:
                yield next(iterator)
            except StopIteration:
                return
    except AgentsRefused:
        raise
    except Exception:  # noqa: BLE001 -- coded, no provider text
        raise AgentsRefused("provider_error") from None


def _drain(
    stream: Iterable[Any], deadline: float, max_events: int
) -> tuple[Status | None, str, list[ToolCall], str | None]:
    """Read one stream to its end: strongest terminal status, text, calls, a limit."""
    status: Status | None = None
    text, calls, limit = "", [], None
    events = _coded(stream)
    for count, event in enumerate(events, start=1):
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
            output
            if isinstance(output, str)
            else json.dumps(output, sort_keys=True, allow_nan=False)
        )
        size = len(encoded.encode())  # a lone surrogate cannot be encoded: a failure
    except Exception:  # noqa: BLE001 -- a handler fault becomes a coded failure result
        return _failed(call, "tool_failed")
    if size > MAX_RESULT_BYTES:
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
            fresh = {(c.turn_id, c.call_id) for c in calls} - answered.keys()
            if len(answered) + len(fresh) > max_calls_per_turn:
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


@dataclass(frozen=True)
class NativeTurn:
    """The outcome of :func:`run_native`; ``ok`` needs an established answer."""

    session_id: str | None
    status: Status
    output_text: str
    tools_called: tuple[str, ...] = field(default_factory=tuple)
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and bool(self.output_text.strip())


#: What the SDK's ``AgentTurnResultError.reason`` can say (openai 3.29.0), as a status
#: and a code of ours. Anything else is "result_not_established".
_REASONS: Final[Mapping[str, tuple[Status, str]]] = {
    "failed": ("failed", "turn_failed"),
    "cancelled": ("cancelled", "turn_cancelled"),
    "requires_action": ("incomplete", "requires_action"),
    "incomplete": ("incomplete", "turn_incomplete"),
    "observation_failed": ("incomplete", "observation_failed"),
}


class _NativeState:
    """What the guarded handlers saw. The SDK swallows every handler exception and
    carries on, so a refusal is recorded HERE and the run is stopped by the loop."""

    def __init__(self) -> None:
        self.called: list[str] = []
        self.refusal: str | None = None

    def refuse(self, code: str) -> AgentsRefused:
        self.refusal = self.refusal or code
        return AgentsRefused(code)


def _guard(
    name: str, handler: Handler, state: _NativeState, max_calls: int
) -> Callable[[dict[str, Any]], str]:
    """A handler the SDK may call: bounded, size-capped, finite, and never echoing."""

    def guarded(arguments: dict[str, Any]) -> str:
        if len(state.called) >= max_calls:
            raise state.refuse("too_many_calls")
        state.called.append(name)
        try:
            size = len(json.dumps(arguments, allow_nan=False).encode())
        except (TypeError, ValueError, RecursionError):
            raise state.refuse("tool_arguments_not_json") from None
        if size > MAX_ARGUMENT_BYTES:
            raise state.refuse("tool_arguments_too_large")
        output = handler(
            arguments
        )  # a fault here is the tool's own: the SDK reports it
        try:
            encoded = (
                output
                if isinstance(output, str)
                else json.dumps(output, sort_keys=True, allow_nan=False)
            )
            size = len(encoded.encode())
        except (TypeError, ValueError, RecursionError):
            raise state.refuse("tool_result_not_encodable") from None
        if size > MAX_RESULT_BYTES:
            raise state.refuse("tool_result_too_large")
        return encoded

    return guarded


def _close(stream: object) -> None:
    """Close a provider stream without cancelling the backend turn; best effort."""
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001, S110 -- closing must never raise provider text
            pass


def _watch(
    stream: Iterable[object], state: _NativeState, max_events: int, deadline: float
) -> str | None:
    """Iterate the SDK stream (this runs the handlers); a code if the run must stop."""
    for count, _ in enumerate(stream, start=1):
        if state.refusal is not None:
            return state.refusal
        if count > max_events:
            return "too_many_events"
        if time.monotonic() > deadline:
            return "deadline_exceeded"
    return state.refusal


def _native_failure(error: Exception, state: _NativeState) -> NativeTurn:
    """A coded NativeTurn for an SDK exception; never its text."""
    called = tuple(state.called)
    if state.refusal is not None:
        return NativeTurn(None, "incomplete", "", called, state.refusal)
    if type(error).__name__ not in {"AgentTurnResultError", "AgentOutputParseError"}:
        return NativeTurn(None, "failed", "", called, "provider_error")
    # Only the SDK's own result errors may name a session (by class name).
    known = getattr(error, "session_id", None)
    reason = getattr(error, "reason", None)
    status, code = _REASONS.get(
        reason if isinstance(reason, str) else "",
        ("incomplete", "result_not_established"),
    )
    return NativeTurn(
        known if isinstance(known, str) else None, status, "", called, code
    )


def run_native(  # noqa: PLR0913 -- separate, named, keyword-only inputs
    client: object,
    *,
    agent: Mapping[str, Any],
    environment: Mapping[str, Any],
    text: str,
    handlers: Mapping[str, Handler] | None = None,
    max_calls: int = MAX_CALLS_PER_TURN,
    max_events: int = MAX_EVENTS_PER_DRAIN,
    deadline_seconds: float = DEADLINE_SECONDS,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> NativeTurn:
    """
    Run one message on a NEW managed session through the SDK's own contract.

    ``client.beta.agents.sessions.create(..., stream=True, tool_handlers=...)``, then
    ``with_result_collection()``, then OUR loop over the stream, then
    ``get_final_result()`` (read from openai 3.29.0 and 3.36.0; the stream has no
    ``session_id``, the result and the SDK's result error do). The SDK runs the function
    actions with our guarded handlers (the mapping is the allowlist); it swallows what
    a handler raises, so a guard refusal (call cap, argument or result size, non-finite
    JSON) is recorded and stops the run as ``incomplete`` with its code: a capped run is
    never ``ok``. A tool's own fault is reported to the model by the SDK as a generic
    failed result and the run goes on. A refusal that comes AFTER a handler ran cannot
    undo that handler's side effects: a tool with side effects can repeat up to
    ``max_calls``.

    Bounds: ``max_events`` and ``deadline_seconds`` (the whole run) are checked as each
    event ARRIVES in this loop; a read that blocks is bounded by ``timeout`` (passed to
    ``create`` as the SDK's per-request timeout) alone. A breach closes the stream
    (without cancelling the backend turn) and returns ``incomplete``. The turn is
    ``completed`` only when ``get_final_result`` returns a non-blank ``output_text``;
    ``AgentTurnResultError`` is mapped from its fixed ``reason``. Every SDK exception
    becomes a code with no provider text. A follow-up is a NEW session whose input
    carries the prior brief: ``tool_handlers`` on a saved session was not verified.
    Needs ``openai>=3.29.0``; the repository's own lock is older.
    """
    agents = getattr(getattr(client, "beta", None), "agents", None)
    sessions = getattr(agents, "sessions", None)
    if sessions is None or not hasattr(sessions, "create"):
        raise AgentsRefused("agents_api_unavailable")
    state = _NativeState()
    guarded = {
        name: _guard(name, handler, state, max_calls)
        for name, handler in (handlers or {}).items()
    }
    stream: Any = None
    deadline = time.monotonic() + deadline_seconds
    try:
        stream = sessions.create(
            agent=dict(agent),
            environment=dict(environment),
            input=text,
            stream=True,
            tool_handlers=guarded,
            timeout=timeout,
        )
        stream = stream.with_result_collection()
        limit = _watch(stream, state, max_events, deadline)
        if limit is not None:
            _close(stream)
            return NativeTurn(None, "incomplete", "", tuple(state.called), limit)
        result = stream.get_final_result()
    except AgentsRefused:
        raise
    except Exception as error:  # noqa: BLE001 -- coded, no provider text
        _close(stream)
        return _native_failure(error, state)
    if state.refusal is not None:
        return NativeTurn(None, "incomplete", "", tuple(state.called), state.refusal)
    output = getattr(result, "output_text", None)
    session_id = getattr(result, "session_id", None)
    output = output.strip() if isinstance(output, str) else ""
    return NativeTurn(
        session_id if isinstance(session_id, str) else None,
        "completed",
        output,
        tuple(state.called),
        None if output else "completed_without_output",
    )
