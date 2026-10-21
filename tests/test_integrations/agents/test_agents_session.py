"""Managed Agents API sessions (software-factories-masterclass-SOW-02)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from zeo_core.integrations.agents import session as agents
from zeo_core.integrations.agents.session import (
    AgentsRefused,
    SdkTransport,
    function_tool,
    mcp_server,
    run,
)

AGENT = {"model": "m", "instructions": "i"}
ENV = {"type": "openai_hosted"}
DONE = {"type": "agent.session.turn.completed"}


def text(value: str) -> dict[str, Any]:
    return {"type": "agent.session.turn.output_text.done", "text": value}


def call(
    call_id: str = "c1", name: str = "get_flight", **arguments: object
) -> dict[str, Any]:
    return {
        "type": "agent.session.requires_action",
        "required_actions": [
            {
                "type": "function_call",
                "turn_id": "t1",
                "call_id": call_id,
                "name": name,
                "arguments": arguments or {"flight": "BA117"},
            }
        ],
    }


class Fake:
    """Scripted transport: each start/send pops the next list of events."""

    def __init__(self, *scripts: list[dict[str, Any]]) -> None:
        self.scripts = list(scripts)
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
        return "ses_1", self.scripts.pop(0)

    def send(self, session_id: str, event: Mapping[str, Any]) -> Iterable[Any]:
        assert session_id == "ses_1"
        self.sent.append(event)
        return self.scripts.pop(0)


def test_a_completed_turn_with_output_is_ok() -> None:
    result = run(Fake([text("hi"), DONE]), agent=AGENT, environment=ENV, text="x")
    assert (result.status, result.output_text, result.ok, result.session_id) == (
        "completed",
        "hi",
        True,
        "ses_1",
    )


def test_a_function_action_is_answered_with_the_matching_ids() -> None:
    fake = Fake([call()], [text("done"), DONE])
    seen: list[Mapping[str, Any]] = []

    def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        seen.append(arguments)
        return {"status": "Delayed"}

    result = run(
        fake, agent=AGENT, environment=ENV, text="x", handlers={"get_flight": handler}
    )
    assert result.ok and seen == [{"flight": "BA117"}]
    assert fake.sent == [
        {
            "type": "agent.session.input.tool_result",
            "turn_id": "t1",
            "call_id": "c1",
            "success": True,
            "output": json.dumps({"status": "Delayed"}, sort_keys=True),
        }
    ]
    assert [c.name for c in result.calls] == ["get_flight"]


def test_parallel_actions_are_each_answered() -> None:
    both = {
        "type": "agent.session.requires_action",
        "required_actions": [
            {
                "type": "function_call",
                "turn_id": "t1",
                "call_id": "a",
                "name": "n",
                "arguments": {},
            },
            {
                "type": "function_call",
                "turn_id": "t1",
                "call_id": "b",
                "name": "n",
                "arguments": {},
            },
        ],
    }
    fake = Fake([both], [], [text("ok"), DONE])
    result = run(
        fake, agent=AGENT, environment=ENV, text="x", handlers={"n": lambda a: "1"}
    )
    assert result.ok and [e["call_id"] for e in fake.sent] == ["a", "b"]


@pytest.mark.parametrize(
    "events",
    [[text("partial")], [text("partial"), {"type": "agent.session.idle"}], []],
)
def test_idle_or_a_closed_stream_is_not_success(events: list[dict[str, Any]]) -> None:
    result = run(Fake(events), agent=AGENT, environment=ENV, text="x")
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "no_terminal_event",
    )


@pytest.mark.parametrize(
    ("event", "status", "code"),
    [
        ("agent.session.turn.failed", "failed", "turn_failed"),
        ("agent.session.turn.cancelled", "cancelled", "turn_cancelled"),
        ("agent.session.failed", "failed", "turn_failed"),
        ("error", "failed", "turn_failed"),
    ],
)
def test_failure_events_are_coded_without_provider_text(
    event: str, status: str, code: str
) -> None:
    result = run(
        Fake([text("x"), {"type": event, "turn": {"error": "SECRET PROVIDER TEXT"}}]),
        agent=AGENT,
        environment=ENV,
        text="x",
    )
    assert (result.status, result.ok, result.error_code) == (status, False, code)
    assert "SECRET" not in repr(result)


def test_completed_without_output_is_not_ok() -> None:
    result = run(Fake([DONE]), agent=AGENT, environment=ENV, text="x")
    assert (result.status, result.ok, result.error_code) == (
        "completed",
        False,
        "completed_without_output",
    )


def test_an_unknown_tool_is_answered_as_a_failure() -> None:
    fake = Fake([call()], [text("ok"), DONE])
    run(fake, agent=AGENT, environment=ENV, text="x")
    assert fake.sent[0]["success"] is False and fake.sent[0]["error"] == "unknown_tool"


def test_a_handler_fault_does_not_leak_its_message() -> None:
    def boom(arguments: Mapping[str, Any]) -> str:
        msg = "secret-token-123"
        raise RuntimeError(msg)

    fake = Fake([call()], [text("ok"), DONE])
    run(fake, agent=AGENT, environment=ENV, text="x", handlers={"get_flight": boom})
    assert fake.sent[0]["error"] == "tool_failed" and "secret" not in json.dumps(
        fake.sent[0]
    )


def test_an_oversized_result_is_refused_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents, "MAX_RESULT_BYTES", 10)
    fake = Fake([call()], [text("ok"), DONE])
    run(
        fake,
        agent=AGENT,
        environment=ENV,
        text="x",
        handlers={"get_flight": lambda a: "x" * 11},
    )
    assert fake.sent[0]["error"] == "tool_result_too_large"


@pytest.mark.parametrize(
    "item",
    [
        {
            "type": "function_call",
            "turn_id": "t",
            "call_id": "c",
            "name": "n",
            "arguments": "{bad",
        },
        {
            "type": "function_call",
            "turn_id": "t",
            "call_id": "c",
            "name": "n",
            "arguments": [1],
        },
        {"type": "function_call", "turn_id": "t", "name": "n", "arguments": {}},
    ],
)
def test_a_malformed_function_call_is_refused(item: dict[str, Any]) -> None:
    event = {"type": "agent.session.requires_action", "required_actions": [item]}
    with pytest.raises(AgentsRefused):
        run(Fake([event]), agent=AGENT, environment=ENV, text="x")


def record(seen: list[Any]) -> Callable[[Mapping[str, Any]], str]:
    def handler(arguments: Mapping[str, Any]) -> str:
        seen.append(dict(arguments))
        return "1"

    return handler


def test_string_arguments_are_parsed() -> None:
    item = {
        "type": "function_call",
        "turn_id": "t",
        "call_id": "c",
        "name": "n",
        "arguments": '{"a": 1}',
    }
    seen: list[Any] = []
    fake = Fake(
        [{"type": "agent.session.requires_action", "required_actions": [item]}],
        [text("ok"), DONE],
    )
    run(
        fake,
        agent=AGENT,
        environment=ENV,
        text="x",
        handlers={"n": record(seen)},
    )
    assert seen == [{"a": 1}]


def test_a_loop_of_actions_is_stopped() -> None:
    fake = Fake(*([[call()]] * 10))
    result = run(
        fake,
        agent=AGENT,
        environment=ENV,
        text="x",
        handlers={"get_flight": lambda a: "1"},
        max_rounds=3,
    )
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_rounds",
    )


def test_a_follow_up_continues_the_session() -> None:
    fake = Fake([text("again"), DONE])
    result = run(fake, agent=AGENT, environment=ENV, text="refresh", session_id="ses_1")
    assert result.ok and fake.started is None
    assert fake.sent == [
        {
            "type": "agent.session.input.message",
            "content": [{"type": "input_text", "text": "refresh"}],
        }
    ]


class _Sessions:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.events = SimpleNamespace(create=self._events)
        self.event_calls: list[Any] = []

    def create(self, **kwargs: object) -> SimpleNamespace:  # noqa: ANN003
        self.created = kwargs
        return SimpleNamespace(session_id="ses_9", __iter__=None)

    def _events(self, session_id: str, **kwargs: object) -> list[Any]:  # noqa: ANN003
        self.event_calls.append((session_id, kwargs))
        return []


def test_the_sdk_transport_uses_the_documented_calls() -> None:
    sessions = _Sessions()
    transport = SdkTransport(
        SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions)))
    )
    session_id, _ = transport.start(agent=AGENT, environment=ENV, text="hi")
    assert session_id == "ses_9"
    assert sessions.created == {
        "agent": AGENT,
        "environment": ENV,
        "input": "hi",
        "stream": True,
    }
    transport.send("ses_9", {"type": "agent.session.input.message"})
    assert (
        sessions.event_calls[0][0] == "ses_9"
        and sessions.event_calls[0][1]["stream"] is True
    )


@pytest.mark.parametrize(
    "client",
    [
        SimpleNamespace(),
        SimpleNamespace(beta=SimpleNamespace()),
        SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace())),
    ],
)
def test_an_sdk_without_the_agents_api_is_refused_not_guessed(client: object) -> None:
    with pytest.raises(AgentsRefused) as refused:
        SdkTransport(client)
    assert refused.value.code == "agents_api_unavailable"


def test_a_function_tool_must_have_closed_parameters() -> None:
    ok = function_tool(
        "n", "d", {"type": "object", "properties": {}, "additionalProperties": False}
    )
    assert ok["type"] == "function" and ok["name"] == "n"
    with pytest.raises(AgentsRefused):
        function_tool("n", "d", {"type": "object", "properties": {}})


@pytest.mark.parametrize(
    "url", ["http://x.example/mcp", "https://localhost/mcp", "https://127.0.0.1/mcp"]
)
def test_an_mcp_server_must_be_reachable_from_openai(url: str) -> None:
    with pytest.raises(AgentsRefused) as refused:
        mcp_server("l", url, allowed_tools=["a"])
    assert refused.value.code == "mcp_url_not_reachable"


def test_an_mcp_credential_is_named_never_valued() -> None:
    declared = mcp_server(
        "l",
        "https://svc.example/mcp",
        allowed_tools=["b", "a"],
        authorization_env_var="BRIEFING_MCP_TOKEN",
    )
    assert (
        declared["allowed_tools"] == ["a", "b"]
        and declared["connection_origin"] == "service"
    )
    assert (
        declared["authorization_env_var"] == "BRIEFING_MCP_TOKEN"
        and "authorization" not in declared
    )


def test_no_entry_point_accepts_a_key() -> None:
    for fn in (run, SdkTransport.__init__, mcp_server, function_tool):
        assert not [
            p for p in inspect.signature(fn).parameters if "key" in p or "token" in p
        ]


def test_the_scripted_transport_serves_consumers_without_a_key() -> None:
    from zeo_core.integrations.agents.session import ScriptedTransport

    transport = ScriptedTransport([call()], [text("done"), DONE])
    result = run(
        transport,
        agent=AGENT,
        environment=ENV,
        text="x",
        handlers={"get_flight": lambda a: "ok"},
    )
    assert result.ok and result.session_id == "ses_test"
    assert transport.started is not None and transport.started["text"] == "x"
    assert transport.sent[0]["call_id"] == "c1"
    with pytest.raises(AgentsRefused) as exhausted:
        transport.send("ses_test", {})
    assert exhausted.value.code == "script_exhausted"
    with pytest.raises(AgentsRefused) as unknown:
        transport.send("other", {})
    assert unknown.value.code == "unknown_session"
