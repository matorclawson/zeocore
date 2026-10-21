"""Managed Agents API sessions (zeocore-SOW-43)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from zeo_core.integrations.agents import session as agents
from zeo_core.integrations.agents.session import (
    AgentsRefused,
    ScriptedTransport,
    SdkTransport,
    function_tool,
    mcp_server,
    run,
)

AGENT = {"model": "m", "instructions": "i"}
ENV = {"type": "openai_hosted"}
DONE = {"type": "agent.session.turn.completed"}
Event = dict[str, Any]


def text(value: str) -> Event:
    return {"type": "agent.session.turn.output_text.done", "text": value}


def action(
    call_id: str = "c1",
    name: str = "get_flight",
    turn_id: str = "t1",
    arguments: object = None,
) -> Event:
    return {
        "type": "function_call",
        "turn_id": turn_id,
        "call_id": call_id,
        "name": name,
        "arguments": {"flight": "BA117"} if arguments is None else arguments,
    }


def requires(*items: Event) -> Event:
    return {"type": "agent.session.requires_action", "required_actions": list(items)}


def call(call_id: str = "c1", **kwargs: Any) -> Event:  # noqa: ANN401
    return requires(action(call_id, **kwargs))


def go(transport: ScriptedTransport, **kwargs: Any) -> agents.TurnResult:  # noqa: ANN401
    return run(transport, agent=AGENT, environment=ENV, text="x", **kwargs)


def counting(box: list[int], value: str = "1") -> Callable[[Mapping[str, Any]], str]:
    def handler(arguments: Mapping[str, Any]) -> str:
        box.append(1)
        return value

    return handler


def test_a_completed_turn_with_output_is_ok() -> None:
    result = go(ScriptedTransport([text("hi"), DONE]))
    assert (result.status, result.output_text, result.ok) == ("completed", "hi", True)
    assert result.session_id == "ses_test"


def test_a_function_action_is_answered_with_the_matching_ids() -> None:
    transport = ScriptedTransport([call()], [text("done"), DONE])
    seen: list[Mapping[str, Any]] = []

    def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        seen.append(arguments)
        return {"status": "Delayed"}

    result = go(transport, handlers={"get_flight": handler})
    assert result.ok and seen == [{"flight": "BA117"}]
    assert transport.sent == [
        {
            "type": "agent.session.input.tool_result",
            "turn_id": "t1",
            "call_id": "c1",
            "success": True,
            "output": json.dumps({"status": "Delayed"}, sort_keys=True),
        }
    ]
    assert [c.name for c in result.calls] == ["get_flight"]


def test_parallel_actions_are_answered_in_one_call() -> None:
    both = requires(action("a", name="n"), action("b", name="n"))
    transport = ScriptedTransport([both], [text("ok"), DONE])
    result = go(transport, handlers={"n": lambda a: "1"})
    assert result.ok and [e["call_id"] for e in transport.sent] == ["a", "b"]
    assert transport.batches == [2]


def test_a_repeated_call_id_runs_its_handler_once() -> None:
    runs: list[int] = []
    twice = requires(action("c1"), action("c1"))
    transport = ScriptedTransport([twice], [call("c1")], [text("ok"), DONE])
    result = go(transport, handlers={"get_flight": counting(runs)})
    assert result.ok and len(runs) == 1
    # a repeat in one batch is sent once; a later replay gets the cached reply
    assert [e["call_id"] for e in transport.sent] == ["c1", "c1"]
    assert transport.sent[0] == transport.sent[1]


def test_the_same_call_id_in_another_turn_is_another_call() -> None:
    runs: list[int] = []
    transport = ScriptedTransport(
        [call("c1", turn_id="t1")], [call("c1", turn_id="t2")], [text("ok"), DONE]
    )
    assert go(transport, handlers={"get_flight": counting(runs)}).ok
    assert len(runs) == 2


def test_too_many_calls_in_a_turn_run_none_of_them() -> None:
    runs: list[int] = []
    many = requires(*[action(f"c{i}") for i in range(5)])
    result = go(
        ScriptedTransport([many]),
        handlers={"get_flight": counting(runs)},
        max_calls_per_turn=4,
    )
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_calls",
    )
    assert runs == []


def test_a_stream_of_too_many_events_stops() -> None:
    noise: list[Event] = [{"type": "agent.session.turn.output_text.delta"}] * 50
    result = go(ScriptedTransport(noise + [text("x"), DONE]), max_events=10)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_events",
    )


def test_a_deadline_stops_a_slow_stream() -> None:
    def slow() -> Iterable[Event]:
        yield text("x")
        yield DONE

    class Slow(ScriptedTransport):
        def start(self, **kwargs: Any) -> tuple[str, Iterable[Any]]:  # noqa: ANN401
            return "ses_test", slow()

    result = go(Slow(), deadline_seconds=-1.0)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "deadline_exceeded",
    )


@pytest.mark.parametrize(
    "events",
    [[text("partial")], [text("partial"), {"type": "agent.session.idle"}], []],
)
def test_idle_or_a_closed_stream_is_not_success(events: list[Event]) -> None:
    result = go(ScriptedTransport(events))
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
    secret = {"type": event, "turn": {"error": "SECRET PROVIDER TEXT"}}
    result = go(ScriptedTransport([text("x"), secret]))
    assert (result.status, result.ok, result.error_code) == (status, False, code)
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize(
    "first", ["error", "agent.session.turn.failed", "agent.session.turn.cancelled"]
)
def test_a_failure_is_sticky_even_if_a_completion_follows(first: str) -> None:
    result = go(ScriptedTransport([text("x"), {"type": first}, DONE]))
    assert result.status != "completed" and not result.ok
    result = go(ScriptedTransport([text("x"), DONE, {"type": first}]))
    assert result.status != "completed" and not result.ok


def test_completed_without_output_is_not_ok() -> None:
    result = go(ScriptedTransport([DONE]))
    assert (result.status, result.ok, result.error_code) == (
        "completed",
        False,
        "completed_without_output",
    )


def test_whitespace_output_is_not_ok() -> None:
    result = go(ScriptedTransport([text("  \n "), DONE]))
    assert (result.ok, result.error_code) == (False, "completed_without_output")


def test_only_the_terminal_round_supplies_the_output() -> None:
    transport = ScriptedTransport([text("I will look that up."), call()], [DONE])
    result = go(transport, handlers={"get_flight": lambda a: "1"})
    assert (result.status, result.ok, result.output_text) == ("completed", False, "")
    transport = ScriptedTransport([text("preamble"), call()], [text(" answer "), DONE])
    assert go(transport, handlers={"get_flight": lambda a: "1"}).output_text == "answer"


def test_an_unknown_tool_is_answered_as_a_failure() -> None:
    transport = ScriptedTransport([call()], [text("ok"), DONE])
    go(transport)
    assert (
        transport.sent[0]["success"] is False
        and transport.sent[0]["error"] == "unknown_tool"
    )


def test_a_handler_fault_does_not_leak_its_message() -> None:
    def boom(arguments: Mapping[str, Any]) -> str:
        msg = "secret-token-123"
        raise RuntimeError(msg)

    transport = ScriptedTransport([call()], [text("ok"), DONE])
    go(transport, handlers={"get_flight": boom})
    assert transport.sent[0]["error"] == "tool_failed"
    assert "secret" not in json.dumps(transport.sent[0])


def test_an_oversized_result_is_refused_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents, "MAX_RESULT_BYTES", 10)
    transport = ScriptedTransport([call()], [text("ok"), DONE])
    go(transport, handlers={"get_flight": lambda a: "x" * 11})
    assert transport.sent[0]["error"] == "tool_result_too_large"


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (action(arguments="{bad"), "tool_arguments_not_json"),
        (action(arguments=[1]), "malformed_function_call"),
        (action(arguments='{"a": 1, "a": 2}'), "tool_arguments_not_json"),
        (action(arguments='{"a": NaN}'), "tool_arguments_not_json"),
        (action(arguments='{"a": Infinity}'), "tool_arguments_not_json"),
        (action(arguments='{"a": -Infinity}'), "tool_arguments_not_json"),
        (action(arguments="[" * 60_000), "tool_arguments_not_json"),
        (
            {"type": "function_call", "turn_id": "t", "name": "n", "arguments": {}},
            "malformed_function_call",
        ),
    ],
)
def test_malformed_arguments_are_refused_with_the_right_code(
    item: Event, code: str
) -> None:
    # a script that would answer, so only the refusal itself can raise
    transport = ScriptedTransport([requires(item)], [text("ok"), DONE])
    with pytest.raises(AgentsRefused) as refused:
        go(transport, handlers={"get_flight": lambda a: "1", "n": lambda a: "1"})
    assert refused.value.code == code


def test_oversized_arguments_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "MAX_ARGUMENT_BYTES", 8)
    with pytest.raises(AgentsRefused) as refused:
        go(ScriptedTransport([requires(action(arguments='{"a": "0123456789"}'))]))
    assert refused.value.code == "tool_arguments_too_large"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "agent.session.requires_action", "required_actions": "call"},
        {"type": "agent.session.requires_action", "required_actions": 7},
        {"type": ["not", "hashable"]},
        {"type": 5},
        requires(
            {
                "type": "function_call",
                "turn_id": 1,
                "call_id": "c",
                "name": "n",
                "arguments": {},
            }
        ),
    ],
)
def test_malformed_events_are_a_code_not_a_type_error(event: Event) -> None:
    with pytest.raises(AgentsRefused):
        go(ScriptedTransport([event]))


def test_string_arguments_are_parsed() -> None:
    seen: list[Any] = []

    def handler(arguments: Mapping[str, Any]) -> str:
        seen.append(dict(arguments))
        return "1"

    transport = ScriptedTransport(
        [requires(action(arguments='{"a": 1}'))], [text("ok"), DONE]
    )
    go(transport, handlers={"get_flight": handler})
    assert seen == [{"a": 1}]


def test_a_loop_of_actions_is_stopped() -> None:
    transport = ScriptedTransport(*([[call(f"c{i}")] for i in range(10)]))
    result = go(transport, handlers={"get_flight": lambda a: "1"}, max_rounds=3)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_rounds",
    )


def test_a_follow_up_continues_the_session() -> None:
    transport = ScriptedTransport([text("again"), DONE])
    result = run(
        transport, agent=AGENT, environment=ENV, text="refresh", session_id="ses_test"
    )
    assert result.ok and transport.started is None
    assert transport.sent == [
        {
            "type": "agent.session.input.message",
            "content": [{"type": "input_text", "text": "refresh"}],
        }
    ]


class _Sessions:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.events = SimpleNamespace(create=self._events)
        self.event_calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def create(self, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        if self.fail:
            msg = "SECRET provider detail"
            raise RuntimeError(msg)
        self.created = kwargs
        return SimpleNamespace(session_id="ses_9")

    def _events(self, session_id: str, **kwargs: Any) -> list[Any]:  # noqa: ANN401
        if self.fail:
            msg = "SECRET provider detail"
            raise RuntimeError(msg)
        self.event_calls.append((session_id, kwargs))
        return []


def _client(sessions: _Sessions) -> SimpleNamespace:
    return SimpleNamespace(
        beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions))
    )


def test_the_sdk_transport_uses_the_documented_calls() -> None:
    sessions = _Sessions()
    transport = SdkTransport(_client(sessions))
    session_id, _ = transport.start(agent=AGENT, environment=ENV, text="hi")
    assert session_id == "ses_9"
    assert sessions.created == {
        "agent": AGENT,
        "environment": ENV,
        "input": "hi",
        "stream": True,
    }
    batch = [{"type": "a", "call_id": "1"}, {"type": "a", "call_id": "2"}]
    transport.send("ses_9", batch)
    assert sessions.event_calls == [("ses_9", {"events": batch, "stream": True})]


def test_a_provider_exception_becomes_a_code_without_its_text() -> None:
    sessions = _Sessions()
    transport = SdkTransport(_client(sessions))
    sessions.fail = True
    for attempt in (
        lambda: transport.start(agent=AGENT, environment=ENV, text="hi"),
        lambda: transport.send("ses_9", [{}]),
    ):
        with pytest.raises(AgentsRefused) as refused:
            attempt()
        assert refused.value.code == "provider_error"
        assert "SECRET" not in str(refused.value)


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
    "url",
    [
        "http://x.example/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://127.1/mcp",
        "https://2130706433/mcp",
        "https://0x7f.1/mcp",
        "https://[::1]/mcp",
        "https://10.0.0.5/mcp",
        "https://192.168.1.1/mcp",
        "https://169.254.169.254/mcp",
        "https://box.local/mcp",
        "https://svc.internal/mcp",
        "https://user:SECRET@svc.example/mcp",
        "https://user@svc.example/mcp",
        "https:///mcp",
        "https://svc.example:99999/mcp",
        "https://singlelabel/mcp",
    ],
)
def test_an_mcp_server_must_be_a_public_https_host_without_credentials(
    url: str,
) -> None:
    with pytest.raises(AgentsRefused) as refused:
        mcp_server("l", url, allowed_tools=["a"])
    assert refused.value.code == "mcp_url_not_reachable"


def test_a_public_host_that_merely_contains_localhost_is_accepted() -> None:
    declared = mcp_server("l", "https://notlocalhost.example/mcp", allowed_tools=["a"])
    assert declared["transport"]["server_url"] == "https://notlocalhost.example/mcp"
    assert mcp_server("l", "https://8.8.8.8/mcp", allowed_tools=["a"])["type"] == "mcp"


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
    for bad in ("sk-abc123SECRET", "lower_case", "HAS SPACE", ""):
        with pytest.raises(AgentsRefused) as refused:
            mcp_server(
                "l",
                "https://svc.example/mcp",
                allowed_tools=[],
                authorization_env_var=bad,
            )
        assert refused.value.code == "mcp_credential_must_be_an_env_var_name"


def test_no_entry_point_accepts_a_key() -> None:
    for fn in (run, SdkTransport.__init__, mcp_server, function_tool):
        assert not [
            p for p in inspect.signature(fn).parameters if "key" in p or "token" in p
        ]


def test_the_scripted_transport_serves_consumers_without_a_key() -> None:
    transport = ScriptedTransport([call()], [text("done"), DONE])
    result = go(transport, handlers={"get_flight": lambda a: "ok"})
    assert (
        result.ok and transport.started is not None and transport.started["text"] == "x"
    )
    assert transport.sent[0]["call_id"] == "c1"
    with pytest.raises(AgentsRefused) as exhausted:
        transport.send("ses_test", [{}])
    assert exhausted.value.code == "script_exhausted"
    with pytest.raises(AgentsRefused) as unknown:
        transport.send("other", [{}])
    assert unknown.value.code == "unknown_session"


def test_the_transport_protocol_takes_a_batch() -> None:
    assert "events" in inspect.signature(SdkTransport.send).parameters
    assert "events" in inspect.signature(ScriptedTransport.send).parameters
    _: Sequence[Mapping[str, Any]] = []


def test_a_failure_stays_the_strongest_terminal_state() -> None:
    failed, cancelled = "agent.session.turn.failed", "agent.session.turn.cancelled"
    result = go(ScriptedTransport([text("x"), {"type": failed}, {"type": cancelled}]))
    assert result.status == "failed"
    result = go(ScriptedTransport([text("x"), {"type": cancelled}, {"type": failed}]))
    assert result.status == "failed"


def test_the_last_final_text_of_the_terminal_round_is_the_output() -> None:
    result = go(ScriptedTransport([text("first"), text("second"), DONE]))
    assert result.output_text == "second"
