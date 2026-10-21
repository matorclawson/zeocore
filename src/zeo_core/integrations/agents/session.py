"""
Managed OpenAI Agents API sessions, keyless (software-factories-masterclass-SOW-02).

This is the ``client.beta.agents`` product (OpenAI runs the agent harness), not the
``openai-agents`` SDK. The caller supplies the client, which reads ``OPENAI_API_KEY``
from its own environment; this module never accepts, stores or logs a key.

The primary tool route is the function-action seam: the agent asks for a client-side
function (``agent.session.requires_action``), the caller's handler runs it, and the
result goes back as ``agent.session.input.tool_result`` with the matching ``turn_id``
and ``call_id``. No public endpoint is needed. An MCP server can be declared too
(:func:`mcp_server`); that route needs an endpoint OpenAI can reach.

A briefing succeeds only when the turn completed AND its output was inspected: an idle
session or a closed stream is not success. Refusals carry a code and no provider text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

Status = Literal["completed", "failed", "cancelled", "incomplete"]
Handler = Callable[[Mapping[str, Any]], "str | Mapping[str, Any]"]

MAX_ROUNDS: Final = 8
MAX_RESULT_BYTES: Final = 4 * 1024 * 1024
_TERMINAL: Final = {
    "agent.session.turn.completed": "completed",
    "agent.session.turn.failed": "failed",
    "agent.session.turn.cancelled": "cancelled",
    "agent.session.failed": "failed",
}


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

    def send(self, session_id: str, event: Mapping[str, Any]) -> Iterable[Any]:
        """Post one input event to a session; return the resulting event stream."""
        ...


class SdkTransport:
    """
    The documented ``client.beta.agents.sessions`` calls.

    Needs an ``openai`` release that has ``beta.agents``; without one it refuses with
    ``agents_api_unavailable`` rather than guess. The call shapes follow the OpenAI
    Agents API guides and are exercised against a fake in tests; the first run against
    the real service is the facilitator's.
    """

    def __init__(self, client: Any) -> None:  # noqa: ANN401 -- the openai client
        agents = getattr(getattr(client, "beta", None), "agents", None)
        if agents is None or not hasattr(agents, "sessions"):
            raise AgentsRefused("agents_api_unavailable")
        self._sessions = agents.sessions

    def start(
        self, *, agent: Mapping[str, Any], environment: Mapping[str, Any], text: str
    ) -> tuple[str, Iterable[Any]]:
        stream = self._sessions.create(
            agent=dict(agent), environment=dict(environment), input=text, stream=True
        )
        session_id = getattr(stream, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise AgentsRefused("no_session_id")
        return session_id, stream

    def send(self, session_id: str, event: Mapping[str, Any]) -> Iterable[Any]:
        stream: Iterable[Any] = self._sessions.events.create(
            session_id, events=[dict(event)], stream=True
        )
        return stream


class ScriptedTransport:
    """
    A test double: each start or send returns the next scripted event list.

    For consumers that want to exercise their handlers and their success logic without
    a key or a network. sent records every event posted back, so a test can check
    that a function action was answered with the right turn_id and call_id.
    """

    def __init__(
        self, *scripts: list[Mapping[str, Any]], session_id: str = "ses_test"
    ) -> None:
        self._scripts = list(scripts)
        self._session_id = session_id
        self.sent: list[Mapping[str, Any]] = []
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

    def send(self, session_id: str, event: Mapping[str, Any]) -> Iterable[Any]:
        if session_id != self._session_id:
            raise AgentsRefused("unknown_session")
        self.sent.append(event)
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
        """True only for a completed turn that produced inspected output."""
        return self.status == "completed" and bool(self.output_text)


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


def mcp_server(
    label: str,
    url: str,
    *,
    allowed_tools: Iterable[str],
    authorization_env_var: str | None = None,
) -> dict[str, Any]:
    """
    An optional MCP server declaration. The OpenAI side connects to ``url``, so it must
    be publicly reachable; a localhost URL is refused. A credential is named by its
    environment variable, never passed as a value.
    """
    if not url.startswith("https://") or "localhost" in url or "127.0.0.1" in url:
        raise AgentsRefused("mcp_url_not_reachable")
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


def _get(event: Any, key: str) -> Any:  # noqa: ANN401 -- SDK objects or dicts
    return event.get(key) if isinstance(event, Mapping) else getattr(event, key, None)


def _calls(event: Any) -> list[ToolCall]:  # noqa: ANN401
    found = []
    for item in _get(event, "required_actions") or [event]:
        if _get(item, "type") != "function_call":
            continue
        arguments = _get(item, "arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                raise AgentsRefused("tool_arguments_not_json") from None
        turn_id, call_id, name = (_get(item, k) for k in ("turn_id", "call_id", "name"))
        if not (
            isinstance(turn_id, str)
            and isinstance(call_id, str)
            and isinstance(name, str)
        ) or not isinstance(arguments, Mapping):
            raise AgentsRefused("malformed_function_call")
        found.append(ToolCall(turn_id, call_id, name, arguments))
    return found


def _drain(stream: Iterable[Any]) -> tuple[Status | None, str, list[ToolCall], bool]:
    """Read one stream to its end: terminal status, text, pending calls, saw-idle."""
    status: Status | None = None
    text, calls, idle = "", [], False
    for event in stream:
        kind = _get(event, "type")
        if kind == "agent.session.turn.output_text.done":
            text = _get(event, "text") or ""
        elif kind == "agent.session.requires_action":
            calls.extend(_calls(event))
        elif kind == "agent.session.idle":
            idle = True
        elif kind in _TERMINAL:
            status = _TERMINAL[kind]  # type: ignore[assignment]
        elif kind == "error":
            status = "failed"
    return status, text if isinstance(text, str) else "", calls, idle


def _answer(call: ToolCall, handlers: Mapping[str, Handler]) -> dict[str, Any]:
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


def run(
    transport: Transport,
    *,
    agent: Mapping[str, Any],
    environment: Mapping[str, Any],
    text: str,
    handlers: Mapping[str, Handler] | None = None,
    session_id: str | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> TurnResult:
    """
    Run one user message to a terminal result, answering function actions as they come.

    With ``session_id`` the message continues that session. Status is ``completed`` only
    when ``agent.session.turn.completed`` arrived; a stream that ends without a terminal
    event, or that goes idle first, is ``incomplete`` and never ``ok``.
    """
    handlers = handlers or {}
    if session_id is None:
        session_id, stream = transport.start(
            agent=agent, environment=environment, text=text
        )
    else:
        message = {
            "type": "agent.session.input.message",
            "content": [{"type": "input_text", "text": text}],
        }
        stream = transport.send(session_id, message)
    seen: list[ToolCall] = []
    output = ""
    for _ in range(max_rounds):
        status, text_out, calls, _idle = _drain(stream)
        output = text_out or output
        seen.extend(calls)
        if calls and status is None:
            replies = [_answer(call, handlers) for call in calls]
            stream = _send_all(transport, session_id, replies)
            continue
        final: Status = status or "incomplete"
        return TurnResult(
            session_id,
            final,
            output,
            tuple(seen),
            None if final == "completed" and output else _why(final, output),
        )
    return TurnResult(session_id, "incomplete", output, tuple(seen), "too_many_rounds")


def _send_all(
    transport: Transport, session_id: str, replies: list[dict[str, Any]]
) -> Iterable[Any]:
    streams: list[Any] = []
    for reply in replies:
        streams.extend(transport.send(session_id, reply))
    return streams


def _why(status: Status, output: str) -> str:
    if status == "completed":
        return "completed_without_output"
    return {"failed": "turn_failed", "cancelled": "turn_cancelled"}.get(
        status, "no_terminal_event"
    )
