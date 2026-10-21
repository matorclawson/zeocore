"""Managed Agents API sessions (zeocore-SOW-43)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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


# --- council round 2 on #160 -------------------------------------------------------


def blowing_up(
    after: list[Event], message: str = "SECRET provider text"
) -> Iterable[Event]:
    yield from after
    raise RuntimeError(message)


def test_an_error_while_reading_the_stream_is_a_code_without_provider_text() -> None:
    class Failing(ScriptedTransport):
        def start(self, **kwargs: Any) -> tuple[str, Iterable[Any]]:  # noqa: ANN401
            return "ses_test", blowing_up([text("partial")])

    with pytest.raises(AgentsRefused) as refused:
        go(Failing())
    assert refused.value.code == "provider_error"
    assert "SECRET" not in repr(refused.value)
    # `from None`: the provider exception is neither the cause nor the context
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


def test_a_refusal_raised_inside_the_stream_keeps_its_own_code() -> None:
    bad = requires(action(arguments="{bad"))
    with pytest.raises(AgentsRefused) as refused:
        go(ScriptedTransport([bad]))
    assert refused.value.code == "tool_arguments_not_json"


def test_a_stream_that_raises_on_the_follow_up_send_is_also_coded() -> None:
    class Failing(ScriptedTransport):
        def send(
            self, session_id: str, events: Sequence[Mapping[str, Any]]
        ) -> Iterable[Any]:
            return blowing_up([])

    with pytest.raises(AgentsRefused) as refused:
        go(Failing([call()]), handlers={"get_flight": lambda a: "1"})
    assert refused.value.code == "provider_error"


def test_a_huge_float_is_not_finite_and_is_refused() -> None:
    with pytest.raises(AgentsRefused) as refused:
        go(ScriptedTransport([requires(action(arguments='{"a": 1e999}'))]))
    assert refused.value.code == "tool_arguments_not_json"


def test_arguments_given_as_a_mapping_obey_the_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents, "MAX_ARGUMENT_BYTES", 8)
    with pytest.raises(AgentsRefused) as refused:
        go(ScriptedTransport([requires(action(arguments={"a": "0123456789"}))]))
    assert refused.value.code == "tool_arguments_too_large"


def test_a_replayed_call_does_not_count_again_towards_the_cap() -> None:
    runs: list[int] = []
    transport = ScriptedTransport(
        [call("c1")], [call("c1")], [call("c1")], [text("ok"), DONE]
    )
    result = go(
        transport, handlers={"get_flight": counting(runs)}, max_calls_per_turn=1
    )
    assert result.ok and len(runs) == 1


def test_a_handler_result_with_nan_or_a_lone_surrogate_is_a_coded_failure() -> None:
    for value in ({"x": float("nan")}, "bad\ud800text"):
        transport = ScriptedTransport([call()], [text("ok"), DONE])
        go(transport, handlers={"get_flight": lambda a, v=value: v})
        assert transport.sent[0]["success"] is False
        assert transport.sent[0]["error"] == "tool_failed"


def test_allowed_tools_must_be_a_list_of_names() -> None:
    with pytest.raises(AgentsRefused) as refused:
        mcp_server("l", "https://svc.example/mcp", allowed_tools="abc")
    assert refused.value.code == "allowed_tools_must_be_a_list"


def test_the_deadline_is_documented_as_checked_only_when_an_event_arrives() -> None:
    doc = agents.__doc__ or ""
    assert "WHOLE run" in doc and "ARRIVES" in doc and "client timeout" in doc


def test_a_refusal_raised_by_the_stream_itself_keeps_its_code() -> None:
    def refusing() -> Iterable[Event]:
        yield text("x")
        raise AgentsRefused("script_exhausted")

    class Refusing(ScriptedTransport):
        def start(self, **kwargs: Any) -> tuple[str, Iterable[Any]]:  # noqa: ANN401
            return "ses_test", refusing()

    with pytest.raises(AgentsRefused) as refused:
        go(Refusing())
    assert refused.value.code == "script_exhausted"


# --- run_native: the real openai SDK contract (read at v3.29.0 and v3.36.0) --------
# The fake has the REAL 3.29.0 signature of Sessions.create (3.36.0 only adds
# spend_control), a stream with NO session_id, and dispatches like the real one: a
# handler call happens AFTER its event is yielded, and EVERY handler exception is
# swallowed (the model is told "Tool handler failed."), the run goes on.


class _SdkResultError(Exception):
    """Named like the SDK's AgentTurnResultError; run_native checks the name."""

    def __init__(self, reason: str, session_id: str | None) -> None:
        super().__init__("SECRET provider detail")
        self.reason = reason
        self.session_id = session_id


_SdkResultError.__name__ = "AgentTurnResultError"


def AgentTurnResultError(reason: str, session_id: str | None = None) -> Exception:  # noqa: N802
    return _SdkResultError(reason, session_id)


class _RealShapedStream:
    def __init__(
        self,
        handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
        events: Sequence[tuple[str, ...] | tuple[str, str, dict[str, Any]]],
        final: Callable[[], Any],
    ) -> None:
        self._handlers, self._events, self._final = handlers, events, final
        self.collecting = False
        self.closed = False
        self.consumed = 0

    def with_result_collection(self) -> _RealShapedStream:
        self.collecting = True
        return self

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for event in self._events:
            self.consumed += 1
            yield SimpleNamespace(type=event[0])
            if event[0] == "call":  # dispatch AFTER the yield; swallow like the SDK
                try:
                    arguments = event[2] if len(event) > 2 else {}
                    self._handlers[event[1]](
                        arguments if isinstance(arguments, dict) else {}
                    )
                except Exception:  # noqa: BLE001, S110
                    pass

    def get_final_result(self) -> Any:  # noqa: ANN401
        if not self.collecting:
            raise AgentTurnResultError("observation_failed")
        return self._final()

    def close(self) -> None:
        self.closed = True


class _RealShapedSessions:
    def __init__(self, events: Sequence[Any], final: Callable[[], Any]) -> None:
        self._events, self._final = events, final
        self.seen: dict[str, Any] = {}
        self.stream: _RealShapedStream | None = None

    def create(  # noqa: PLR0913
        self,
        *,
        environment: Mapping[str, Any],
        output_type: object = None,
        tool_handlers: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
        stream: bool,
        agent: Mapping[str, Any] | None = None,
        agent_id: str | None = None,
        input: object = None,  # noqa: A002
        metadata: object = None,
        vault_ids: object = None,
        extra_headers: object = None,
        extra_query: object = None,
        extra_body: object = None,
        timeout: object = None,
    ) -> _RealShapedStream:
        self.seen = {
            "environment": environment,
            "agent": agent,
            "input": input,
            "stream": stream,
            "handlers": sorted(tool_handlers or {}),
            "timeout": timeout,
        }
        self.stream = _RealShapedStream(tool_handlers or {}, self._events, self._final)
        return self.stream


def answer(text_value: object = "the answer") -> Callable[[], SimpleNamespace]:
    return lambda: SimpleNamespace(output_text=text_value, session_id="ses_native")


def native_client(
    events: Sequence[Any] = (), final: Callable[[], Any] | None = None
) -> tuple[SimpleNamespace, _RealShapedSessions]:
    sessions = _RealShapedSessions(events, final or answer())
    return (
        SimpleNamespace(
            beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions))
        ),
        sessions,
    )


def native(client: object, **kwargs: Any) -> agents.NativeTurn:  # noqa: ANN401
    return agents.run_native(client, agent=AGENT, environment=ENV, text="x", **kwargs)


def calls_of(name: str, count: int, **arguments: Any) -> list[tuple[Any, ...]]:  # noqa: ANN401
    return [("call", name, arguments or {})] * count


def test_run_native_uses_exactly_the_real_create_contract() -> None:
    client, sessions = native_client()
    result = native(client, handlers={"get_flight": lambda a: "1"}, timeout=12.5)
    assert (result.status, result.ok, result.output_text) == (
        "completed",
        True,
        "the answer",
    )
    assert result.session_id == "ses_native"
    assert sessions.seen == {
        "environment": ENV,
        "agent": AGENT,
        "input": "x",
        "stream": True,
        "handlers": ["get_flight"],
        "timeout": 12.5,
    }
    assert sessions.stream is not None and sessions.stream.collecting


def test_the_sdk_dispatches_our_guarded_handlers_with_a_dict() -> None:
    seen: list[Mapping[str, Any]] = []

    def flight(arguments: Mapping[str, Any]) -> dict[str, str]:
        seen.append(arguments)
        return {"s": "Delayed"}

    client, _ = native_client(calls_of("get_flight", 1, flight="BA117"))
    result = native(client, handlers={"get_flight": flight})
    assert (
        result.ok
        and seen == [{"flight": "BA117"}]
        and result.tools_called == ("get_flight",)
    )


def test_only_declared_handlers_are_offered_to_the_sdk() -> None:
    client, sessions = native_client()
    native(client, handlers={"a": lambda x: "1"})
    assert sessions.seen["handlers"] == ["a"]
    client, sessions = native_client()
    native(client)
    assert sessions.seen["handlers"] == []


# P1-B: the SDK swallows what a handler raises; the cap must still stop the run.


def test_a_capped_run_is_incomplete_and_never_ok_even_if_the_sdk_would_answer() -> None:
    runs: list[int] = []

    def counting_handler(arguments: Mapping[str, Any]) -> str:
        runs.append(1)
        return "1"

    client, sessions = native_client(calls_of("n", 3))
    result = native(client, handlers={"n": counting_handler}, max_calls=1)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_calls",
    )
    assert len(runs) == 1 and sessions.stream is not None and sessions.stream.closed


@pytest.mark.parametrize(
    ("arguments", "output", "code"),
    [
        ({"a": "0123456789"}, "1", "tool_arguments_too_large"),
        ({}, {"x": float("nan")}, "tool_result_not_encodable"),
        ({}, "bad\ud800text", "tool_result_not_encodable"),
        ({}, "x" * 20, "tool_result_too_large"),
    ],
)
def test_a_guard_refusal_stops_the_run_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    output: object,
    code: str,
) -> None:
    monkeypatch.setattr(agents, "MAX_ARGUMENT_BYTES", 8)
    monkeypatch.setattr(agents, "MAX_RESULT_BYTES", 10)
    client, sessions = native_client([("call", "n", arguments), ("event",), ("event",)])
    result = native(client, handlers={"n": lambda a: output})
    assert (result.status, result.ok, result.error_code) == ("incomplete", False, code)
    assert (
        sessions.stream is not None
        and sessions.stream.closed
        and sessions.stream.consumed < 3
    )


def test_a_refusal_on_the_last_event_is_still_never_ok() -> None:
    client, _ = native_client(calls_of("n", 2))
    result = native(client, handlers={"n": lambda a: "1"}, max_calls=1)
    assert (result.ok, result.error_code) == (False, "too_many_calls")


def test_a_tools_own_fault_is_reported_by_the_sdk_and_the_run_goes_on() -> None:
    def boom(arguments: Mapping[str, Any]) -> str:
        msg = "secret-token-123"
        raise RuntimeError(msg)

    client, _ = native_client(calls_of("n", 1))
    result = native(client, handlers={"n": boom})
    assert result.ok and result.tools_called == ("n",)
    assert "secret" not in repr(result)


# P1-A: our loop bounds the run.


def test_too_many_events_close_the_stream_and_return_incomplete() -> None:
    client, sessions = native_client([("event",)] * 50)
    result = native(client, max_events=10)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "too_many_events",
    )
    assert (
        sessions.stream is not None
        and sessions.stream.closed
        and sessions.stream.consumed <= 11
    )


def test_a_passed_deadline_closes_the_stream_and_returns_incomplete() -> None:
    client, sessions = native_client([("event",)] * 5)
    result = native(client, deadline_seconds=-1.0)
    assert (result.status, result.ok, result.error_code) == (
        "incomplete",
        False,
        "deadline_exceeded",
    )
    assert sessions.stream is not None and sessions.stream.closed


def test_the_client_timeout_is_passed_to_create_by_default() -> None:
    client, sessions = native_client()
    native(client)
    assert sessions.seen["timeout"] == agents.CLIENT_TIMEOUT_SECONDS


# P2: the SDK's fixed error reasons map to our codes; no provider text.


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("failed", "failed", "turn_failed"),
        ("cancelled", "cancelled", "turn_cancelled"),
        ("requires_action", "incomplete", "requires_action"),
        ("incomplete", "incomplete", "turn_incomplete"),
        ("observation_failed", "incomplete", "observation_failed"),
        ("something_new", "incomplete", "result_not_established"),
    ],
)
def test_the_sdks_result_error_reason_maps_to_a_code(
    reason: str, status: str, code: str
) -> None:
    def final() -> None:
        raise AgentTurnResultError(reason, "ses_from_error")

    client, sessions = native_client(final=final)
    result = native(client)
    assert (result.status, result.ok, result.error_code) == (status, False, code)
    assert result.session_id == "ses_from_error" and "SECRET" not in repr(result)
    assert sessions.stream is not None and sessions.stream.closed


def test_any_other_sdk_exception_is_a_coded_failure_without_its_text() -> None:
    def final() -> None:
        msg = "SECRET provider detail"
        raise RuntimeError(msg)

    client, _ = native_client(final=final)
    result = native(client)
    assert (result.status, result.ok, result.error_code) == (
        "failed",
        False,
        "provider_error",
    )
    assert "SECRET" not in repr(result)


def test_only_the_sdks_result_errors_may_supply_a_session_id() -> None:
    class Other(Exception):  # noqa: N818 -- any other exception, carrying an id
        session_id = "ses_must_not_be_trusted"

    def final() -> None:
        raise Other

    client, _ = native_client(final=final)
    result = native(client)
    assert (result.session_id, result.error_code) == (None, "provider_error")


def test_the_session_id_comes_from_the_result_never_from_the_stream() -> None:
    client, sessions = native_client()
    stream = client.beta.agents.sessions.create(environment=ENV, stream=True)
    assert not hasattr(stream, "session_id")  # as in openai 3.29.0 and 3.36.0
    assert native(client).session_id == "ses_native"


@pytest.mark.parametrize("value", ["", "  \n", None, 5])
def test_a_blank_or_missing_answer_is_not_ok(value: object) -> None:
    client, _ = native_client(final=answer(value))
    result = native(client)
    assert (result.ok, result.error_code) == (False, "completed_without_output")


def test_a_create_call_the_sdk_would_reject_is_a_coded_failure() -> None:
    class Old:
        def create(self, **kwargs: object) -> None:
            msg = "unexpected keyword 'tool_handlers'"
            raise TypeError(msg)

    client = SimpleNamespace(
        beta=SimpleNamespace(agents=SimpleNamespace(sessions=Old()))
    )
    result = native(client)
    assert (result.status, result.error_code) == ("failed", "provider_error")


@pytest.mark.parametrize(
    "client",
    [
        SimpleNamespace(),
        SimpleNamespace(beta=SimpleNamespace()),
        SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace())),
    ],
)
def test_run_native_refuses_an_sdk_without_the_agents_api(client: object) -> None:
    with pytest.raises(AgentsRefused) as refused:
        native(client)
    assert refused.value.code == "agents_api_unavailable"


def test_run_native_takes_no_key() -> None:
    assert not [
        p
        for p in inspect.signature(agents.run_native).parameters
        if "key" in p or "token" in p
    ]


def test_dev17s_start_cannot_start_on_a_stream_shaped_like_the_real_sdk() -> None:
    """The defect run_native replaces: SdkTransport.start needs stream.session_id."""
    client, _ = native_client()
    with pytest.raises(AgentsRefused) as refused:
        SdkTransport(client).start(agent=AGENT, environment=ENV, text="x")
    assert refused.value.code in {"provider_error", "no_session_id"}
