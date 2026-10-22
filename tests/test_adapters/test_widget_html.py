"""The HTML renderer of a WidgetBoard snapshot (masterclass SOW-02, zeocore-SOW-43)."""

from __future__ import annotations

import ast
import copy
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from zeo_core.adapters import widget_html
from zeo_core.adapters.widget_html import render_card_html, render_snapshot_html
from zeo_core.contracts.ui.widgets import (
    CardState,
    DataRef,
    WidgetBoard,
    WidgetUpsert,
)

T = "2026-10-22T13:40:00Z"

PAYLOADS: dict[str, dict[str, Any]] = {
    "flight_status": {
        "flight_number": "BA117",
        "route": {"from": "LHR", "to": "JFK"},
        "scheduled_departure": "2026-10-22T14:25:00Z",
        "estimated_departure": None,
        "scheduled_arrival": "2026-10-22T21:10:00Z",
        "estimated_arrival": "2026-10-22T21:35:00Z",
        "terminal": None,
        "reported_status": "Delayed",
        "source_timestamp": T,
    },
    "weather_hourly": {
        "location": "New York",
        "timezone": "America/New_York",
        "valid_from": "2026-10-22T20:00:00Z",
        "valid_to": "2026-10-23T02:00:00Z",
        "hourly": [
            {
                "time": "2026-10-22T20:00:00Z",
                "temp_c": 13.0,
                "precip_prob": 10,
                "precip_mm": 0.0,
            },
            {
                "time": "2026-10-22T21:00:00Z",
                "temp_c": 14.5,
                "precip_prob": 30,
                "precip_mm": 0.2,
            },
            {
                "time": "2026-10-22T22:00:00Z",
                "temp_c": 12.0,
                "precip_prob": 60,
                "precip_mm": 1.1,
            },
        ],
        "arrival_hour": "2026-10-22T21:00:00Z",
    },
    "arrival_timeline": {
        "steps": [
            {"label": "Land", "time": "2026-10-22T21:35:00Z"},
            {"label": "Customs", "time": "2026-10-22T22:05:00Z"},
        ],
        "transfer_minutes": {"value": 75, "kind": "assumption"},
        "meeting_time": "2026-10-22T22:00:00Z",
        "margin_minutes": -50,
    },
    "market_watchlist": {
        "symbols": [
            {"symbol": "AAPL", "price": 231.4, "change": -1.9, "change_pct": -0.8},
            {"symbol": "MSFT", "price": 410.25, "change": 2.1, "change_pct": 0.5},
        ],
        "market_status": "open",
        "quote_time": T,
        "delayed": True,
        "series": {
            "AAPL": [
                {"time": T, "value": 231.4},
                {"time": "2026-10-22T13:50:00Z", "value": 232.0},
            ]
        },
    },
}
WIDGET_IDS = {
    "flight_status": "flight",
    "weather_hourly": "arrival-weather",
    "arrival_timeline": "timeline",
    "market_watchlist": "watchlist",
}


def ref(component: str, **over: Any) -> DataRef:  # noqa: ANN401
    body: dict[str, Any] = {
        "id": f"ref-{component}",
        "source": "provider.x",
        "fetched_at": T,
        "as_of": T,
        "replay": False,
        "delayed": True if component == "market_watchlist" else None,
        "assumptions": (
            [{"label": "transfer_minutes", "value": 75, "kind": "assumption"}]
            if component == "arrival_timeline"
            else []
        ),
        "payload": copy.deepcopy(PAYLOADS[component]),
        "error": None,
    }
    return DataRef(**(body | over))


def snapshot(*components: str, **over: Any) -> list[CardState]:  # noqa: ANN401
    board = WidgetBoard()
    board.begin_request("req-1")
    for component in components or tuple(PAYLOADS):
        data = ref(component, **over)
        board.apply(
            WidgetUpsert(
                type="widget.upsert",
                request_id="req-1",
                widget_id=WIDGET_IDS[component],
                component=component,  # type: ignore[arg-type]
                data_ref=data.id,
                revision=1,
            ),
            data,
        )
    return board.snapshot()


def card_with(component: str, **over: Any) -> CardState:  # noqa: ANN401
    """A CardState whose data_ref is built WITHOUT the board's checks."""
    good = snapshot(component)[0]
    data = DataRef.model_construct(**(good.data_ref.model_dump() | over))
    return CardState.model_construct(**(good.model_dump() | {"data_ref": data}))


FULL = snapshot()
HTML = render_snapshot_html(FULL)


# --- what a page may never contain --------------------------------------------------


def test_nothing_active_nothing_external_in_the_output() -> None:
    lowered = HTML.lower()
    for forbidden in (
        "<script",
        "javascript:",
        "http://",
        "https://",
        "<iframe",
        "<object",
        "<embed",
        "<link",
        "<img",
        "<a ",
        "@import",
        "url(",
        "src=",
        "href=",
        "<form",
        "<input",
    ):
        assert forbidden not in lowered, forbidden
    assert not re.search(r"\bon[a-z]+\s*=", lowered)


def test_the_document_form_carries_a_closed_content_security_policy() -> None:
    page = render_snapshot_html(FULL, document=True, title="Brief me")
    assert page.startswith("<!doctype html>")
    assert "default-src 'none'" in page and "style-src 'unsafe-inline'" in page
    assert "<script" not in page.lower() and "<title>Brief me</title>" in page


def test_the_output_is_deterministic() -> None:
    assert render_snapshot_html(snapshot()) == render_snapshot_html(snapshot())


def test_an_empty_snapshot_renders_a_message_not_an_error() -> None:
    out = render_snapshot_html([])
    assert "zc-board" in out and "no cards" in out.lower()


def test_cards_keep_the_snapshot_order() -> None:
    order = [m.group(1) for m in re.finditer(r'data-widget="([^"]+)"', HTML)]
    assert order == sorted(WIDGET_IDS.values())  # snapshot order is widget order


# --- escaping ------------------------------------------------------------------------


def test_every_value_is_escaped() -> None:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = 'Tom & "Jerry" it\'s'
    out = render_snapshot_html(snapshot("flight_status", payload=payload))
    assert "Tom &amp; &quot;Jerry&quot; it&#x27;s" in out
    assert 'Tom & "Jerry"' not in out


@pytest.mark.parametrize("field", ["source"])
def test_unvalidated_provenance_text_is_escaped(field: str) -> None:
    card = card_with("flight_status", **{field: '<img src=x onerror="alert(1)">'})
    out = render_card_html(card)
    assert "<img" not in out.lower() and "onerror" not in out and "[withheld]" in out


def test_the_title_and_widget_id_are_escaped() -> None:
    out = render_snapshot_html(
        FULL, document=True, title='</title><script>alert("x")</script>'
    )
    assert "<script" not in out.lower() and "&lt;/title&gt;" in out
    card = card_with("flight_status")
    card = CardState.model_construct(
        **(card.model_dump() | {"widget_id": '"><script>'})
    )
    assert "<script" not in render_card_html(card).lower()


# --- the honest labels ---------------------------------------------------------------


def test_replay_delayed_assumption_and_superseded_are_visible() -> None:
    out = render_snapshot_html(snapshot(replay=True))
    assert out.count("REPLAY") >= 4  # on every card
    assert "ASSUMPTION" in out
    assert 'class="zc-fresh"' not in out  # a REPLAY has no freshness line
    assert "DELAYED" in render_snapshot_html(snapshot())  # a live delayed quote says so
    replaced = card_with("weather_hourly")
    replaced = CardState.model_construct(**(replaced.model_dump() | {"replaced": True}))
    assert "SUPERSEDED" in render_card_html(replaced)


def test_a_live_card_has_no_replay_label() -> None:
    assert "REPLAY" not in render_card_html(snapshot("flight_status")[0])


def test_a_missing_provider_time_and_a_missing_terminal_are_said_so() -> None:
    out = render_card_html(snapshot("flight_status", as_of=None)[0])
    assert "provider update time not given" in out


def test_the_retrieval_time_is_shown() -> None:
    assert T in render_card_html(snapshot("flight_status")[0])


# --- the four components -------------------------------------------------------------


def test_flight_status_shows_route_times_and_the_providers_own_status() -> None:
    out = render_card_html(snapshot("flight_status")[0])
    for needle in (
        "BA117",
        "LHR",
        "JFK",
        "14:25 UTC",  # no Place: times are UTC and say so
        "21:35 UTC",
        "2026-10-22T21:35:00Z",  # the full ISO stays in the details line
        "Delayed",
    ):
        assert needle in out


def test_weather_is_an_hourly_chart_with_the_arrival_hour_marked() -> None:
    out = render_card_html(snapshot("weather_hourly")[0])
    assert "<svg" in out and "<polyline" in out and "<title>" in out
    assert out.count('class="zc-precip"') == 3 and 'class="zc-arrival"' in out
    for needle in ("New York", "14.5", "times in UTC"):
        assert needle in out


def test_the_timeline_shows_the_transfer_as_an_assumption_and_the_margin() -> None:
    out = render_card_html(snapshot("arrival_timeline")[0])
    assert "<ol" in out and "Land" in out and "Customs" in out  # a list in time order
    assert "75" in out and "ASSUMPTION" in out
    assert "zc-late" in out and "50" in out and "late" in out.lower()
    payload = copy.deepcopy(PAYLOADS["arrival_timeline"])
    payload["margin_minutes"] = 20
    spare = render_card_html(snapshot("arrival_timeline", payload=payload)[0])
    assert "zc-late" not in spare and "spare" in spare.lower()


def test_the_watchlist_is_a_table_with_the_delay_and_a_sparkline() -> None:
    out = render_card_html(snapshot("market_watchlist")[0])
    assert out.count("<tr") >= 3 and "AAPL" in out and "MSFT" in out and "231.4" in out
    assert "DELAYED" in out and "open" in out and T in out
    assert out.count("<polyline") == 1  # one series


# --- invalid references --------------------------------------------------------------


def test_an_error_reference_renders_as_unavailable_with_its_reason() -> None:
    out = render_card_html(
        snapshot("flight_status", payload={}, error={"reason": "provider timed out"})[0]
    )
    assert "unavailable" in out.lower() and "provider timed out" in out
    assert "BA117" not in out


def test_a_payload_that_does_not_fit_its_component_is_unavailable_and_unrendered() -> (
    None
):
    card = card_with("flight_status", payload=PAYLOADS["weather_hourly"])
    out = render_card_html(card)
    assert "unavailable" in out.lower() and "New York" not in out and "14.5" not in out


def test_a_payload_with_markup_is_unavailable_not_rendered() -> None:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = "<script>alert(1)</script>"
    out = render_card_html(card_with("flight_status", payload=payload))
    assert (
        "unavailable" in out.lower()
        and "<script" not in out.lower()
        and "alert(1)" not in out
    )


def test_an_unknown_component_is_unavailable() -> None:
    card = CardState.model_construct(
        **(snapshot("flight_status")[0].model_dump() | {"component": "iframe"})
    )
    out = render_card_html(card)
    assert "unavailable" in out.lower() and "<iframe" not in out.lower()


# --- no key and no capability, ever --------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abc123def456ghi789jkl012mno",
        "sk-ant-api03-abcdefghij0123456789",
        "Bearer abcdefghijklmnop12345678",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN PRIVATE KEY-----",
        "https://svc.example/path?token=abc",
        "javascript:alert(1)",
    ],
)
def test_a_key_shaped_or_capability_value_is_withheld(secret: str) -> None:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = secret
    out = render_snapshot_html(
        snapshot("flight_status", payload=payload, source=secret),
        document=True,
        title=secret,
    )
    assert secret not in out and "[withheld]" in out
    unvalidated = render_card_html(card_with("flight_status", source=secret))
    assert secret not in unvalidated


def test_no_internal_identifier_is_rendered() -> None:
    out = render_card_html(snapshot("flight_status")[0])
    assert "ref-flight_status" not in out and "req-1" not in out


# --- purity --------------------------------------------------------------------------


def test_the_module_does_no_io_and_takes_no_key() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(widget_html) or "").read_text())
    imported = {
        (node.module or "").split(".")[0]
        if isinstance(node, ast.ImportFrom)
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (
            node.names if isinstance(node, ast.Import | ast.ImportFrom) else []
        )
    }
    assert not imported & {
        "os",
        "socket",
        "urllib",
        "http",
        "subprocess",
        "requests",
        "httpx",
        "pathlib",
        "importlib",
    }
    for fn in (render_snapshot_html, render_card_html):
        assert not [
            p
            for p in inspect.signature(fn).parameters
            if "key" in p or "token" in p or "url" in p
        ]


def test_the_functions_do_not_mutate_their_input() -> None:
    before = [c.model_dump() for c in FULL]
    render_snapshot_html(FULL)
    assert [c.model_dump() for c in FULL] == before


def test_any_card_with_assumptions_carries_the_assumption_label() -> None:
    card = card_with(
        "flight_status", assumptions=[{"label": "x", "value": 1, "kind": "assumption"}]
    )
    assert "ASSUMPTION" in render_card_html(card)
    assert "ASSUMPTION" not in render_card_html(
        card_with("flight_status", assumptions=[])
    )


# ==================================================================================
# The Council's must-NOT probes (2026-10-22-masterclass-cards-mustnot-probe-plan.md).
# Each is an executable test against the REAL renderer. The named mutant that must
# fail it is run by hand at the PR head and logged with the PR.
# ==================================================================================

CANARIES = [
    "sk-proj-CANARY0123456789abcdefghij",
    "sk-ant-CANARY0123456789abcdefghij",
    "brf_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t",  # a 43-character capability
    "BRIEFING_MCP_TOKEN=mcp-secret-CANARY-9f8e7d6c5b4a",
]


def _eight_char_pieces(canary: str) -> list[str]:
    return [canary[i : i + 8] for i in range(len(canary) - 7)]


def _leaks(haystack: str, canary: str) -> list[str]:
    return [piece for piece in _eight_char_pieces(canary) if piece in haystack]


# M1 -- only Core-validated DataRef values render, never model prose ------------------


def test_m1_model_prose_never_reaches_the_page() -> None:
    payload = copy.deepcopy(PAYLOADS["weather_hourly"])
    payload["hourly"][0]["temp_c"] = 17.0
    prose = "temperature 99, gate B42, delayed 3h"  # what a model said; not an input
    page = render_snapshot_html(snapshot("weather_hourly", payload=payload))
    assert "17" in page
    for invented in ("99", "B42", "3h", prose):
        assert invented not in page


@pytest.mark.parametrize(
    "not_a_card", [{"temp_c": 99, "gate": "B42"}, "temperature 99", None, 17]
)
def test_m1_a_non_validated_input_renders_unavailable(not_a_card: Any) -> None:  # noqa: ANN401
    out = render_card_html(not_a_card)
    assert "unavailable" in out.lower() and "99" not in out and "B42" not in out


def test_m1_an_unvalidated_payload_renders_unavailable_not_its_values() -> None:
    payload = copy.deepcopy(PAYLOADS["weather_hourly"])
    payload["hourly"][0]["temp_c"] = "99"  # a string where a number belongs
    out = render_card_html(card_with("weather_hourly", payload=payload))
    assert "unavailable" in out.lower() and "99" not in out and "New York" not in out


# M2 -- the REPLAY / provider label is inside EVERY card, on every path --------------


def _articles(page: str) -> list[str]:
    return re.findall(r"<article.*?</article>", page, flags=re.S)


def test_m2_every_card_carries_its_label_on_every_path() -> None:
    cards = [*snapshot(replay=True)]
    error_card = snapshot(
        "flight_status",
        replay=True,
        payload={},
        error={"reason": "the source did not answer"},
    )[0]
    bad_card = card_with("weather_hourly", replay=True, payload={"x": 1})
    replaced = CardState.model_construct(
        **(card_with("market_watchlist", replay=True).model_dump() | {"replaced": True})
    )
    cards += [error_card, bad_card, replaced]
    page = render_snapshot_html(cards)
    articles = _articles(page)
    assert len(articles) == len(cards) == 7
    assert all("REPLAY" in a for a in articles)
    assert page.count("REPLAY") == len(cards)  # one label per card, not one per page


def test_m2_a_live_card_says_provider_data_never_replay() -> None:
    for article in _articles(render_snapshot_html(snapshot())):
        assert "REPLAY" not in article
        if "zc-arrival_timeline" in article:  # derived from other values: COMPUTED
            assert "COMPUTED from the flight estimate" in article
        else:
            assert "PROVIDER DATA" in article


def test_m2_the_estimate_from_a_live_position_says_so() -> None:
    label = widget_html.LIVE_POSITION_ASSUMPTION
    computed = snapshot(
        "flight_status",
        assumptions=[{"label": label, "value": "position", "kind": "assumption"}],
    )[0]
    out = render_card_html(computed)
    assert "COMPUTED FROM LIVE POSITION" in out and "ASSUMPTION" not in out
    assert "COMPUTED FROM LIVE POSITION" not in render_card_html(
        snapshot("flight_status")[0]
    )


def test_m2_every_card_has_an_attribution_line() -> None:
    for article in _articles(render_snapshot_html(snapshot())):
        assert 'class="zc-attr"' in article and "provider.x" in article


# M3 -- no key, token or capability in a card, page, URL or log ------------------------


def _hostile_fields(canary: str) -> dict[str, Any]:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = canary
    return {"source": f"https://svc.example/feed?key={canary}", "payload": payload}


@pytest.mark.parametrize("canary", CANARIES)
def test_m3_canaries_never_appear_in_a_page(
    canary: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = _hostile_fields(canary)
    cards = [
        snapshot("flight_status", **fields)[0],
        card_with("flight_status", **fields),
    ]
    error_card = snapshot(
        "market_watchlist", payload={}, error={"reason": f"failed with {canary}"}
    )[0]
    page = render_snapshot_html([*cards, error_card], document=True, title=canary)
    assert _leaks(page, canary) == []
    captured = capsys.readouterr()
    assert _leaks(caplog.text + captured.out + captured.err, canary) == []


def test_m3_the_renderer_neither_logs_nor_prints() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(widget_html) or "").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imports = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not names & {"print", "logging", "logger"} and "logging" not in imports


def test_m3_no_url_with_a_query_is_echoed() -> None:
    page = render_snapshot_html(
        snapshot("flight_status", source="https://svc.example/feed?token=abc123")
    )
    assert "svc.example" not in page and "token=abc123" not in page


# M4 -- local only: nothing external loads, nothing links out --------------------------


def test_m4_no_external_resource_can_load() -> None:
    page = render_snapshot_html(snapshot(), document=True)
    lowered = page.lower()
    for forbidden in (
        "http://",
        "https://",
        "//cdn",
        "<script",
        "<link",
        "<img",
        "<iframe",
        "<object",
        "<embed",
        "<source",
        "<video",
        "<audio",
        "srcset",
        "@font-face",
        "@import",
        "url(",
        "xlink:href",
        " href=",
        " src=",
    ):
        assert forbidden not in lowered, forbidden
    assert "default-src 'none'" in page and "connect-src" not in page


def test_m4_this_renderer_opens_no_socket_and_binds_nothing() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(widget_html) or "").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
        )
    }
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not imported & {"socket", "socketserver", "http", "asyncio", "ssl"}
    assert "bind" not in attributes and "listen" not in attributes
    assert "0.0.0" + ".0" not in ast.dump(tree)  # (tabindex is markup, not a bind)


# M5 -- everything escaped; an invalid reference is exactly 'unavailable' --------------

HOSTILE = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=1>',
    "javascript:alert(1)",
    "</style><b>x</b>",
    "line sep para",
    "rtl‮override",
    "nul\x00byte",
]


def _clean(page: str) -> None:
    lowered = page.lower()
    for bad in (
        "<script",
        "onerror=",
        "javascript:",
        "</style><b>",
        "<img",
        " ",
        " ",
        "‮",
        "\x00",
    ):
        assert bad not in lowered, bad


@pytest.mark.parametrize("hostile", HOSTILE)
def test_m5_hostile_text_in_every_unvalidated_field_is_neutralised(
    hostile: str,
) -> None:
    card = card_with("flight_status", source=hostile, fetched_at=hostile, as_of=hostile)
    card = CardState.model_construct(
        **(card.model_dump() | {"widget_id": hostile, "component": "flight_status"})
    )
    _clean(render_card_html(card))
    _clean(render_snapshot_html([card], document=True, title=hostile))
    unvalidated_error = card_with(
        "flight_status", payload={}, error={"reason": hostile}
    )
    _clean(render_card_html(unvalidated_error))


def test_m5_a_javascript_url_in_a_field_is_withheld_not_linked() -> None:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = "javascript:alert(1)"
    out = render_card_html(card_with("flight_status", payload=payload))
    assert "javascript:" not in out and "[withheld]" in out and "href" not in out


@pytest.mark.parametrize(
    "hostile", [h for h in HOSTILE if not h.startswith("javascript:")]
)
def test_m5_hostile_text_in_a_payload_leaves_that_card_unavailable_and_empty(
    hostile: str,
) -> None:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["reported_status"] = hostile
    payload["flight_number"] = "SHOULD-NOT-SHOW"
    out = render_card_html(card_with("flight_status", payload=payload))
    assert "unavailable" in out.lower()
    assert "SHOULD-NOT-SHOW" not in out and "LHR" not in out and "Delayed" not in out
    _clean(out)


@pytest.mark.parametrize(
    "broken",
    [
        {"flight_number": "BA117"},  # a missing required field
        {**PAYLOADS["flight_status"], "terminal": 5},  # a wrong type
        {**PAYLOADS["flight_status"], "extra": "x"},  # an unknown key
    ],
)
def test_m5_a_missing_field_or_wrong_type_renders_only_unavailable(
    broken: dict[str, Any],
) -> None:
    out = render_card_html(card_with("flight_status", payload=broken))
    assert "unavailable" in out.lower()
    for value in ("BA117", "LHR", "JFK", "Delayed", "2026-10-22T14:25:00Z"):
        assert value not in out


def test_m5_an_error_reason_is_shown_only_as_plain_prose() -> None:
    prose = render_card_html(
        snapshot(
            "flight_status", payload={}, error={"reason": "the source did not answer"}
        )[0]
    )
    assert "the source did not answer" in prose
    for identifier in ("provider_timeout_504", "ERR_CODE_17", "a/b/c", "x" * 130):
        out = render_card_html(
            snapshot("flight_status", payload={}, error={"reason": identifier})[0]
        )
        assert identifier not in out and "unavailable" in out.lower()


def test_the_document_title_is_the_visible_page_heading() -> None:
    page = render_snapshot_html(
        snapshot(),
        document=True,
        title="Constructed REPLAY briefing: meeting time required",
    )
    assert (
        '<h1 class="zc-title">Constructed REPLAY briefing: meeting time required</h1>'
        in page
    )
    assert "<h1" not in render_snapshot_html(snapshot())  # the fragment has no heading


# --- real sources: absence is shown as absence (rasa, 2026-10-22) -----------


def _flight_without_schedules() -> CardState:
    payload = copy.deepcopy(PAYLOADS["flight_status"])
    payload["scheduled_departure"] = None
    del payload["scheduled_arrival"]
    payload["estimated_arrival"] = None
    return snapshot("flight_status", payload=payload)[0]


def test_a_flight_without_schedules_shows_not_provided_and_no_made_up_date() -> None:
    out = render_card_html(_flight_without_schedules())
    assert out.count("airline schedule not provided by this source") == 1  # once
    for invented in ("1970", "2026-10-22T00:00:00Z", "None"):
        assert invented not in out


def test_a_forecast_without_a_probability_has_no_bar_and_no_zero() -> None:
    payload = copy.deepcopy(PAYLOADS["weather_hourly"])
    for hour in payload["hourly"]:
        hour["precip_prob"] = None
    out = render_card_html(snapshot("weather_hourly", payload=payload)[0])
    assert 'class="zc-precip"' not in out and "<polyline" in out
    assert "0 %" not in out
    assert out.count("rain probability not provided by this source") == 1


def test_one_missing_probability_drops_only_its_bar() -> None:
    payload = copy.deepcopy(PAYLOADS["weather_hourly"])
    payload["hourly"][1]["precip_prob"] = None
    out = render_card_html(snapshot("weather_hourly", payload=payload)[0])
    assert out.count('class="zc-precip"') == 2 and out.count("not provided") == 1


# --- the market card on real daily reference data (Council probes K1-K6) ----


def _ecb(**over: Any) -> dict[str, Any]:  # noqa: ANN401
    payload: dict[str, Any] = {
        "symbols": [
            {"symbol": "EURUSD", "price": 1.1634, "change": 0.0021, "change_pct": 0.18}
        ],
        "market_status": "daily reference",
        "quote_time": None,
        "quote_date": "2026-10-21",
        "rate_basis": "daily_reference",
        "delayed": False,
        "series": {
            "EURUSD": [
                {"time": "2026-10-20", "value": 1.1613},
                {"time": "2026-10-21", "value": 1.1634},
            ]
        },
    }
    return payload | over


def _ecb_card(**over: Any) -> str:  # noqa: ANN401
    return render_card_html(
        snapshot("market_watchlist", payload=_ecb(**over), delayed=False)[0]
    )


def test_k1_a_daily_rate_says_so_and_shows_its_publication_date() -> None:
    out = _ecb_card()
    assert "daily reference rate, not a live quote" in out and "2026-10-21" in out
    assert out.count('class="zc-fresh"') == 1 and "DELAYED" not in out
    lowered = out.lower()
    assert "live" not in lowered.replace("not a live quote", "")  # never a live claim
    assert (
        " now" not in lowered
        and "real-time" not in lowered
        and "real time" not in lowered
    )


def test_k2_the_date_shown_is_the_payloads_not_the_clock() -> None:
    friday = _ecb_card(quote_date="2026-10-16")  # the weekend answer is still Friday's
    assert "2026-10-16" in friday and "2026-10-21" not in friday
    assert "published: <strong>2026-10-16" in friday


def test_k2_no_midnight_publication_time_is_invented() -> None:
    out = _ecb_card()
    assert "T00:00" not in out and "quote time" not in out


def test_k3_a_row_without_a_price_makes_the_card_unavailable_not_zero() -> None:
    payload = _ecb()
    payload["symbols"].append({"symbol": "EURGBP", "change": None, "change_pct": None})
    out = render_card_html(
        card_with("market_watchlist", payload=payload, delayed=False)
    )
    assert "unavailable" in out.lower() and "EURUSD" not in out and "EURGBP" not in out


def test_k3_the_card_lists_exactly_the_rows_it_was_given() -> None:
    out = _ecb_card()
    assert out.count("<tr") == 2  # header + one row; nothing added


def test_k4_a_missing_previous_value_says_change_unavailable_not_zero() -> None:
    payload = _ecb()
    payload["symbols"][0]["change"] = None
    payload["symbols"][0]["change_pct"] = None
    payload["series"] = {}
    out = render_card_html(
        snapshot("market_watchlist", payload=payload, delayed=False)[0]
    )
    assert out.count("change unavailable") == 2
    assert "0 %" not in out and ">0<" not in out and "0.00" not in out


def test_k4_the_change_is_shown_exactly_as_given() -> None:
    out = _ecb_card()
    assert "1.1634" in out and "<td>&lt;0.01</td>" in out  # 0.0021 to 2 places
    assert "0.18 %" in out


def test_k5_the_attribution_line_names_the_source() -> None:
    out = render_card_html(
        snapshot(
            "market_watchlist",
            payload=_ecb(),
            delayed=False,
            source="European Central Bank (ECB), daily euro reference rates",
        )[0]
    )
    assert 'class="zc-attr"' in out and "European Central Bank (ECB)" in out


def test_k6_a_delayed_stock_quote_is_labelled_delayed_never_daily() -> None:
    out = render_card_html(snapshot("market_watchlist")[0])
    assert "DELAYED" in out and "DAILY REFERENCE" not in out
    assert "daily reference rate" not in out


def test_numbers_keep_their_precision_and_are_never_rounded_away() -> None:
    from zeo_core.adapters.widget_html import _num

    cases = {
        1.1634: "1.1634",
        231.4: "231.4",
        410.25: "410.25",
        0.0021: "0.0021",
        13.0: "13",
        -0.8: "-0.8",
        0.30000000000000004: "0.30000000000000004",
        # dt-proofreader NUM-01: a nonzero value is never shown as zero or rounded
        0.0000004: "0.0000004",
        -0.0000004: "-0.0000004",
        0.0000006: "0.0000006",
        1.23456789: "1.23456789",
        1e-10: "0.0000000001",
        1e22: "10000000000000000000000",
        -1234567.891: "-1234567.891",
        0.0: "0",
    }
    assert {value: _num(value) for value in cases} == cases
    assert _num(-0.0) == "0"  # not "-0"
    assert _num(float("nan")) == "n/a" and _num(float("inf")) == "n/a"


def test_a_tiny_nonzero_value_is_never_shown_as_zero_on_a_card() -> None:
    payload = _ecb()
    payload["symbols"][0].update(price=1.1634, change=0.0000004, change_pct=-0.0000004)
    out = render_card_html(
        snapshot("market_watchlist", payload=payload, delayed=False)[0]
    )
    assert "<0.0001" not in out  # never a raw '<' in the markup
    assert "&lt;0.01" in out and "-&lt;0.01 %" in out
    assert ">0.0000<" not in out and "-0.00 %" not in out


# --- scenario L1 (rasa-ops v6.2): one provenance label, one freshness line -----


def _provenance_labels(article: str) -> list[str]:
    return re.findall(r"zc-tag-(replay|provider|derived)", article)


def _fresh_lines(article: str) -> list[str]:
    return re.findall(r'<p class="zc-fresh"><strong>(.*?)</strong></p>', article)


def _mixed_cards() -> list[CardState]:
    live = snapshot()
    replay = snapshot(replay=True)
    daily = snapshot("market_watchlist", payload=_ecb(), delayed=False)
    return [*live, *replay, *daily]


def test_l1_every_card_has_exactly_one_provenance_label() -> None:
    articles = _articles(render_snapshot_html(_mixed_cards()))
    assert len(articles) == 9
    for article in articles:
        assert len(_provenance_labels(article)) == 1, article[:120]


def test_l1_a_replay_has_no_freshness_line_and_the_rest_have_exactly_one() -> None:
    for article in _articles(render_snapshot_html(_mixed_cards())):
        labels = _provenance_labels(article)
        lines = _fresh_lines(article)
        assert (labels == ["replay"]) == (lines == []), article[:120]
        assert len(lines) <= 1


def test_l1_the_freshness_line_matches_the_data_reference() -> None:
    live = _fresh_lines(render_card_html(snapshot("flight_status")[0]))
    delayed = _fresh_lines(render_card_html(snapshot("market_watchlist")[0]))
    daily = _fresh_lines(
        render_card_html(snapshot("market_watchlist", payload=_ecb(), delayed=False)[0])
    )
    assert live == ["live"]
    assert delayed == ["DELAYED, not real time"]
    assert daily == ["daily reference rate, not a live quote"]


def test_l1_live_is_never_shown_for_a_daily_or_delayed_value() -> None:
    for card in (
        snapshot("market_watchlist", payload=_ecb(), delayed=False)[0],
        snapshot("market_watchlist")[0],
    ):
        out = render_card_html(card)
        body = out.replace("not a live quote", "")
        assert "live" not in body.lower(), out[:200]


def test_l1_an_unavailable_card_has_a_label_and_no_freshness_line() -> None:
    out = render_card_html(card_with("flight_status", payload={"x": 1}))
    assert _provenance_labels(out) == ["provider"] and _fresh_lines(out) == []


def test_l1_the_computed_value_names_its_input_and_is_not_a_second_card_label() -> None:
    label = widget_html.LIVE_POSITION_ASSUMPTION
    card = snapshot(
        "flight_status",
        assumptions=[{"label": label, "value": "position", "kind": "assumption"}],
    )[0]
    out = render_card_html(card)
    assert out.count("COMPUTED FROM LIVE POSITION") == 1
    assert _provenance_labels(out) == [
        "provider"
    ]  # still ONE card-level provenance label


def test_l1_a_live_basis_quote_that_is_delayed_shows_one_delayed_line() -> None:
    payload = _ecb(quote_time=T, quote_date=None, rate_basis="live", delayed=True)
    out = render_card_html(
        snapshot("market_watchlist", payload=payload, delayed=True)[0]
    )
    assert _fresh_lines(out) == ["DELAYED, not real time"]


def test_l6_the_computed_value_says_it_is_not_an_airline_time() -> None:
    label = widget_html.LIVE_POSITION_ASSUMPTION
    card = snapshot(
        "flight_status",
        assumptions=[{"label": label, "value": "position", "kind": "assumption"}],
    )[0]
    out = render_card_html(card)
    assert "COMPUTED FROM LIVE POSITION, not an airline time" in out
    plain = render_card_html(snapshot("flight_status")[0])
    assert "airline time" not in plain


# --- zeoreviewer CORE163-F1: derived timeline values, REPLAY precedence -------


def test_f1_a_live_timeline_is_computed_with_its_inputs_named_not_provider_data() -> (
    None
):
    out = render_card_html(snapshot("arrival_timeline")[0])
    assert _provenance_labels(out) == ["derived"]
    assert "COMPUTED from the flight estimate, the stated transfer time" in out
    assert "the meeting time" in out and "PROVIDER DATA" not in out
    assert _fresh_lines(out) == [
        "computed from a live position 0 s old, as of 13:40 UTC"
    ]


def test_f1_a_replay_timeline_is_replay_only_never_computed() -> None:
    out = render_card_html(snapshot("arrival_timeline", replay=True)[0])
    assert _provenance_labels(out) == ["replay"]
    assert (
        "COMPUTED" not in out and "PROVIDER DATA" not in out and _fresh_lines(out) == []
    )


def test_f1_a_replay_flight_with_a_live_position_assumption_is_not_computed() -> None:
    label = widget_html.LIVE_POSITION_ASSUMPTION
    card = snapshot(
        "flight_status",
        replay=True,
        assumptions=[{"label": label, "value": "position", "kind": "assumption"}],
    )[0]
    out = render_card_html(card)
    assert _provenance_labels(out) == ["replay"]
    assert "COMPUTED" not in out and "airline time" not in out


def test_f1_each_live_card_has_one_header_label_and_the_timeline_a_derived_one() -> (
    None
):
    labels = {
        card.widget_id: _provenance_labels(render_card_html(card))
        for card in snapshot()
    }
    assert labels == {
        "arrival-weather": ["provider"],
        "flight": ["provider"],
        "timeline": ["derived"],
        "watchlist": ["provider"],
    }


# --- Council CHANGES on #163 5388cce0: R1-R4 and the cheap P2s --------------

KEY = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "source",
    [
        f"api.openweathermap.org/data/2.5/forecast?appid={KEY}",  # R1: no scheme
        f"api.example.org?key={KEY}",
        "host/path",
        "user@host",
        "a:b",
        "100%",
        "x=y",
        "a&b",
    ],
)
def test_r1_the_attribution_is_a_plain_provider_name_or_it_is_withheld(
    source: str,
) -> None:
    out = render_card_html(card_with("flight_status", source=source))
    assert KEY not in out and "appid" not in out and "[withheld]" in out
    assert source not in out


@pytest.mark.parametrize(
    "source",
    [
        "MET Norway (api.met.no), CC BY 4.0",
        "European Central Bank (ECB), daily euro reference rates",
        "adsb.lol",
        "provider.x",
    ],
)
def test_r1_a_real_attribution_is_shown(source: str) -> None:
    assert source in render_card_html(snapshot("flight_status", source=source)[0])


ZW = "\u200b"


@pytest.mark.parametrize("where", ["source", "payload", "title", "reason"])
def test_r2_a_zero_width_character_cannot_hide_a_key(where: str) -> None:
    canary = f"sk-{ZW}proj-CANARY0123456789abcdefghij"
    pieces = [canary.replace(ZW, "")[i : i + 8] for i in range(len(canary) - 8)]
    if where == "source":
        page = render_card_html(card_with("flight_status", source=canary))
    elif where == "payload":
        payload = copy.deepcopy(PAYLOADS["flight_status"])
        payload["reported_status"] = canary
        # the contract refuses the character; a card built around it must still be safe
        page = render_card_html(card_with("flight_status", payload=payload))
    elif where == "title":
        page = render_snapshot_html(snapshot(), document=True, title=canary)
    else:
        error = {"reason": f"the key was {canary} today"}
        page = render_card_html(card_with("flight_status", payload={}, error=error))
    assert not [piece for piece in pieces if piece in page]
    assert ZW not in page


def test_r2_the_extra_format_characters_are_replaced_too() -> None:
    for char in ("\U000110bd", "\U000110cd", "\U00013430", "\U0001bca0", "\U0001d173"):
        page = render_snapshot_html(snapshot(), document=True, title=f"on{char}time")
        assert char not in page and "on\ufffdtime" in page


def test_r3_a_market_card_whose_delayed_flag_disagrees_is_unavailable() -> None:
    out = render_card_html(
        card_with("market_watchlist", delayed=False)
    )  # payload says True
    assert "unavailable" in out.lower() and "AAPL" not in out


def test_r3_a_timeline_without_its_transfer_assumption_is_unavailable() -> None:
    out = render_card_html(card_with("arrival_timeline", assumptions=[]))
    assert "unavailable" in out.lower() and "Customs" not in out


def test_r3_a_card_whose_reference_has_no_check_is_unavailable() -> None:
    card = card_with("flight_status")
    card = CardState.model_construct(
        **(card.model_dump() | {"data_ref": {"payload": {}}})
    )
    assert "unavailable" in render_card_html(card).lower()


def test_r4_the_computed_tag_names_its_inputs() -> None:
    label = widget_html.LIVE_POSITION_ASSUMPTION
    card = snapshot(
        "flight_status",
        source="adsb.lol",
        assumptions=[{"label": label, "value": "position", "kind": "assumption"}],
    )[0]
    out = render_card_html(card)
    assert (
        "COMPUTED FROM LIVE POSITION, not an airline time (inputs: live position from"
        in out
    )
    assert "adsb.lol" in out and "fetched 13:40 UTC" in out  # ISO is in the details


def test_p2_a_reason_with_an_id_or_a_host_name_is_not_shown() -> None:
    for reason in (
        "request 3f2a9c1e7b4d failed",
        "service api.example.org did not answer",
    ):
        out = render_card_html(
            snapshot("flight_status", payload={}, error={"reason": reason})[0]
        )
        assert (
            "unavailable" in out.lower()
            and "3f2a9c1e" not in out
            and "api.example" not in out
        )


def test_p2_the_page_policy_allows_no_images() -> None:
    page = render_snapshot_html(snapshot(), document=True)
    assert "img-src" not in page and "default-src 'none'" in page


# --- the market change is COMPUTED, not provider data (ruling 9dc5ee68) -------------

CHANGE_LABEL = "change_computed_from_two_reference_days"


def _ecb_computed(**over: Any) -> str:  # noqa: ANN401
    assumptions = [
        {"label": CHANGE_LABEL, "value": "two ECB days", "kind": "assumption"}
    ]
    return render_card_html(
        snapshot(
            "market_watchlist",
            payload=_ecb(),
            delayed=False,
            **({"assumptions": assumptions} | over),
        )[0]
    )


def test_the_label_constant_is_the_published_one() -> None:
    assert widget_html.MARKET_CHANGE_ASSUMPTION == CHANGE_LABEL


def test_c1_a_computed_change_says_so_and_names_its_two_days_and_source() -> None:
    out = _ecb_computed()
    assert out.count("COMPUTED from two reference days") == 1
    assert "2026-10-20 and 2026-10-21" in out and "provider.x" in out
    assert "not provider data" in out and "(inputs:" in out
    assert _provenance_labels(out) == ["provider"]  # the price keeps its ONE label
    assert ">ASSUMPTION<" not in out  # a derivation note is not an assumption tag


def test_c2_without_the_declaration_the_change_claims_nothing_computed() -> None:
    out = _ecb_computed(assumptions=[])
    assert "COMPUTED" not in out and "not provider data" not in out


def test_c3_a_replay_never_claims_a_computation() -> None:
    out = _ecb_computed(replay=True)
    assert "COMPUTED" not in out and _provenance_labels(out) == ["replay"]


def test_c4_no_previous_value_means_no_computation_claim() -> None:
    payload = _ecb()
    payload["symbols"][0] |= {"change": None, "change_pct": None}
    assumptions = [{"label": CHANGE_LABEL, "value": "x", "kind": "assumption"}]
    out = render_card_html(
        snapshot(
            "market_watchlist", payload=payload, delayed=False, assumptions=assumptions
        )[0]
    )
    assert "change unavailable" in out and "COMPUTED" not in out


def test_c5_the_input_source_is_withheld_when_it_is_not_a_plain_name() -> None:
    out = _ecb_computed(source="https://x.test/?key=sk-ant-abcdefgh12345678")
    assert "sk-ant" not in out and "x.test" not in out and "COMPUTED" in out


def test_c6_the_computed_line_is_not_a_second_freshness_line() -> None:
    out = _ecb_computed()
    assert _fresh_lines(out) == ["daily reference rate, not a live quote"]


# --- a forecast and a computed timeline are not "live"; tables stay in their card ---

FRESH_FORECAST_UTC = "forecast, issued 13:40 UTC"
FRESH_TIMELINE_0S = "computed from a live position 0 s old, as of 13:40 UTC"


def test_fr1_each_component_gets_the_freshness_line_that_is_true_of_it() -> None:
    lines = {
        c: _fresh_lines(render_card_html(snapshot(c)[0]))
        for c in ("flight_status", "weather_hourly", "arrival_timeline")
    }
    assert lines == {
        "flight_status": ["live"],  # the position is read now
        "weather_hourly": [FRESH_FORECAST_UTC],  # the provider's own as_of
        "arrival_timeline": [FRESH_TIMELINE_0S],
    }


def test_fr2_a_replay_still_has_no_freshness_line_whatever_the_component() -> None:
    for component in ("weather_hourly", "arrival_timeline"):
        out = render_card_html(snapshot(component, replay=True)[0])
        assert _fresh_lines(out) == [] and _provenance_labels(out) == ["replay"]


def test_fr3_the_daily_rule_still_beats_the_component_rule() -> None:
    daily = render_card_html(
        snapshot("market_watchlist", payload=_ecb(), delayed=False)[0]
    )
    assert _fresh_lines(daily) == ["daily reference rate, not a live quote"]


def test_fr4_tables_scroll_inside_their_card_instead_of_overflowing_it() -> None:
    style = widget_html.STYLE
    assert (
        ".zc-card{" in style
        and "min-width:0" in style.split(".zc-card{")[1].split("}")[0]
    )
    assert ".zc-scroll{overflow-x:auto;max-width:100%}" in style
    for article in _articles(render_snapshot_html(FULL)):
        for table in re.findall(r"<table.*?</table>", article, flags=re.S):
            assert re.search(
                r'<div class="zc-scroll" role="region" tabindex="0" '
                r'aria-label="[^"]+">' + re.escape(table) + "</div>",
                article,
            )  # every table is in a labelled, keyboard-focusable scroll region
    assert 'class="zc-scroll"' in render_card_html(snapshot("market_watchlist")[0])
    assert 'class="zc-scroll"' in render_card_html(snapshot("weather_hourly")[0])


def test_fr5_no_value_is_clipped_or_hidden_by_the_layout() -> None:
    style = widget_html.STYLE
    assert "overflow:hidden" not in style and "text-overflow" not in style


# --- the traveller cards: local time at the place, answer-first, rounded ---

NYC = widget_html.Place("JFK", "New York JFK", "America/New_York")


def _big(article: str) -> str:
    match = re.search(r'<p class="zc-big">(.*?)</p>', article)
    assert match is not None, article[:200]
    return re.sub(r"&deg;", "°", match[1])


@pytest.mark.parametrize(
    "bad",
    [
        ("jfk", "New York", "America/New_York"),
        ("JFK", "", "America/New_York"),
        ("JFK", "<b>x</b>", "America/New_York"),
        ("JFK", "New York", "Not/AZone"),
        ("JFK", "New York", "../etc/passwd"),
        ("JFK", "New York", ""),
    ],
)
def test_t1_a_place_is_validated(bad: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError, match="invalid_place"):
        widget_html.Place(*bad)


def test_t2_times_are_in_the_places_zone_and_say_so_or_say_utc() -> None:
    card = snapshot("flight_status")[0]
    with_place = render_card_html(card, place=NYC)
    without = render_card_html(card)
    assert "17:35 EDT" in with_place and "21:35 UTC" not in with_place
    assert "21:35 UTC" in without and "EDT" not in without  # never guessed
    assert render_snapshot_html([card], place=NYC).count("17:35 EDT") >= 1


def test_t3_the_flight_card_leads_with_when_it_lands_and_the_schedule_note() -> None:
    sample = copy.deepcopy(PAYLOADS["flight_status"])
    for key in ("scheduled_departure", "scheduled_arrival", "terminal"):
        sample[key] = None
    out = render_card_html(snapshot("flight_status", payload=sample)[0], place=NYC)
    head = _big(out)
    assert head.startswith("Landing about 17:35 at JFK, in about ")  # from fetched_at
    assert out.count("airline schedule not provided by this source") == 1
    for noise in ("departure: not provided", "terminal: not provided"):
        assert noise not in out  # absent fields are left out, not listed


def test_t4_an_empty_route_does_not_print_not_provided_arrows() -> None:
    sample = copy.deepcopy(PAYLOADS["flight_status"])
    sample["route"] = {"from": "not provided", "to": "not provided"}
    sample["estimated_arrival"] = None
    out = render_card_html(snapshot("flight_status", payload=sample)[0])
    assert _big(out) == "No landing estimate"
    assert "not provided &rarr;" not in out and "&rarr; not provided" not in out


def test_t5_weather_leads_with_the_temperature_and_rain_at_landing() -> None:
    out = render_card_html(snapshot("weather_hourly")[0], place=NYC)
    assert _big(out) == "14 °C, 0.2 mm rain at landing"  # the arrival hour's row
    assert "times in EDT" in out and "New York JFK" in out
    assert "21:00" not in out and "17:00" in out  # 21:00Z is 17:00 EDT
    assert "2026-10-22T21:00:00Z" not in out.split("<details")[0]  # ISO only in details


def test_t6_the_weather_freshness_is_the_providers_issue_time_never_fetch_time() -> (
    None
):
    card = snapshot("weather_hourly", as_of="2026-10-22T13:10:00Z")[0]
    assert _fresh_lines(render_card_html(card, place=NYC)) == [
        "forecast, issued 09:10 EDT"
    ]
    blank = snapshot("weather_hourly", as_of=None)[0]
    assert _fresh_lines(render_card_html(blank, place=NYC)) == [
        "forecast, issue time not provided"
    ]  # fetched_at is not substituted


def test_t7_the_timeline_freshness_is_the_age_of_the_position() -> None:
    old = snapshot("arrival_timeline", as_of="2026-10-22T13:35:00Z")[0]  # 5 min before
    assert _fresh_lines(render_card_html(old)) == [
        "computed from a live position 5 min old, as of 13:40 UTC"
    ]
    young = snapshot("arrival_timeline", as_of="2026-10-22T13:39:30Z")[0]
    assert _fresh_lines(render_card_html(young)) == [
        "computed from a live position 30 s old, as of 13:40 UTC"
    ]
    none = snapshot("arrival_timeline", as_of=None)[0]
    assert _fresh_lines(render_card_html(none)) == [
        "computed from a live position (age not provided)"
    ]


def test_t8_precedence_replay_then_delayed_then_daily_then_component() -> None:
    from types import SimpleNamespace

    replay = render_card_html(snapshot("weather_hourly", replay=True)[0])
    assert _fresh_lines(replay) == []
    # the contract refuses a card that is both, so the order is checked on _fresh itself
    card = snapshot("market_watchlist")[0]  # its data reference says delayed
    both = SimpleNamespace(rate_basis="daily_reference", delayed=False)
    assert "DELAYED, not real time" in widget_html._fresh(card, both)
    weather = snapshot("weather_hourly")[0]
    daily = SimpleNamespace(rate_basis="daily_reference", delayed=False)
    assert "daily reference rate" in widget_html._fresh(
        weather, daily
    )  # beats forecast


def test_t9_the_timeline_leads_with_the_verdict_in_plain_words() -> None:
    out = render_card_html(snapshot("arrival_timeline")[0], place=NYC)
    assert _big(out) == "You will miss it: 50 min late (assuming a 75 min transfer)"
    ok = copy.deepcopy(PAYLOADS["arrival_timeline"])
    ok["margin_minutes"] = 1925
    assert (
        _big(render_card_html(snapshot("arrival_timeline", payload=ok)[0]))
        == "You will make it: 32 h 5 min spare (assuming a 75 min transfer)"
    )
    assert "18:00 EDT" in out  # the meeting 22:00Z in New York time


def test_t10_the_market_card_leads_with_the_rate_rounded_to_four_places() -> None:
    sample = _ecb()
    sample["symbols"][0].update(price=1.123449, change=0.0021449, change_pct=0.183749)
    out = _ecb_card(**{"symbols": sample["symbols"]})
    assert _big(out) == "1 EUR = 1.1234 USD"
    assert "<td>1.1234</td>" in out and "<td>&lt;0.01</td>" in out
    assert "<td>0.18 %</td>" in out
    assert "1.123449" not in out and "0.183749" not in out


def test_t11_rounding_is_half_even_and_a_nonzero_value_is_never_zero() -> None:
    assert widget_html._fixed(1.12345, 4) == "1.1234"  # half-even on the exact decimal
    assert widget_html._fixed(1.12355, 4) == "1.1236"
    assert widget_html._fixed(0.0000004, 2) == "&lt;0.01"
    assert widget_html._fixed(-0.0000004, 2) == "-&lt;0.01"
    assert (
        widget_html._fixed(0.0, 2) == "0.00" and widget_html._fixed(-0.0, 2) == "0.00"
    )


def test_t12_hostile_place_text_cannot_reach_the_markup() -> None:
    with pytest.raises(ValueError, match="invalid_place"):
        widget_html.Place("JFK", "<script>alert(1)</script>", "America/New_York")


def test_t13_the_weather_chart_has_labelled_hours_and_degrees_in_local_time() -> None:
    out = render_card_html(snapshot("weather_hourly")[0], place=NYC)
    chart = re.search(r"<svg.*?</svg>", out, flags=re.S)[0]  # type: ignore[index]
    assert "16h" in chart and "°C".replace("°", "&deg;") in chart
    assert "13&deg;C" in chart or "12&deg;C" in chart  # the low end of the scale


def test_t14_without_a_place_the_headline_time_says_utc_never_a_bare_clock() -> None:
    out = render_card_html(snapshot("flight_status")[0])
    assert _big(out).startswith("Landing about 21:35 UTC")  # not read as local time
    weather = _big(render_card_html(snapshot("weather_hourly")[0]))
    assert "UTC" not in weather  # a temperature has no clock; its header names the zone


def test_t15_the_board_uses_the_rasa_palette_container_columns_and_no_clipping() -> (
    None
):
    style = widget_html.STYLE
    for token in ("#FAF6F0", "#FFFBF5", "#181210", "#C8C0B6", "#AA1E54", "#C6FEE1"):
        assert token in style  # the deck's taupe, ink, pink and mint
    assert "container-type:inline-size" in style  # columns follow the container
    assert "@container (min-width:960px)" in style and "minmax(0,1fr)" in style
    assert "font-size:42px" in style and "font:500 32px/36px" in style  # big fact
    assert "overflow:hidden" not in style and "text-overflow" not in style
    assert "@import" not in style and "url(" not in style  # no font or image fetch
    html = render_snapshot_html(FULL)
    assert '<div class="zc-grid">' in html and html.count("<article") == 4


# --- council R1/R2/R4 and the design seat's browser findings ---


def test_r1_the_headline_names_the_routes_destination_not_the_places_code() -> None:
    sample = copy.deepcopy(PAYLOADS["flight_status"])
    sample["route"] = {"from": "LHR", "to": "EWR"}
    out = render_card_html(snapshot("flight_status", payload=sample)[0], place=NYC)
    assert _big(out).startswith(
        "Landing about 17:35 at EWR"
    )  # the Place gives the zone
    assert "Landing about 17:35 at JFK" not in _big(out)  # the destination is EWR
    assert _big(out).endswith("as of 09:40 at JFK")  # JFK is only the clock's place


def test_r2_the_timeline_headline_carries_its_assumption() -> None:
    out = render_card_html(snapshot("arrival_timeline")[0])
    assert "(assuming a 75 min transfer)" in _big(out)


def _dst_hours(times: list[str]) -> dict[str, Any]:
    payload = copy.deepcopy(PAYLOADS["weather_hourly"])
    payload["hourly"] = [
        {"time": t, "temp_c": 10.0 + i, "precip_prob": 10, "precip_mm": 0.0}
        for i, t in enumerate(times)
    ]
    payload["valid_from"], payload["valid_to"] = times[0], times[-1]
    payload["arrival_hour"] = times[1]
    return payload


def test_r4_across_the_new_york_dst_change_hours_are_edt_then_est() -> None:
    # New York falls back on 2026-11-01: 05:00Z is 01:00 EDT, 06:00Z is 01:00 EST
    times = [
        "2026-11-01T04:00:00Z",  # 00:00 EDT
        "2026-11-01T05:00:00Z",  # 01:00 EDT
        "2026-11-01T06:00:00Z",  # 01:00 EST
        "2026-11-01T07:00:00Z",  # 02:00 EST
    ]
    out = render_card_html(
        snapshot("weather_hourly", payload=_dst_hours(times))[0], place=NYC
    )
    rows = re.findall(r"<tr><td>(\d\d:\d\d \w{3})</td>", out)
    assert rows == ["00:00 EDT", "01:00 EDT", "01:00 EST", "02:00 EST"]  # not ambiguous
    assert "local time (EDT/EST)" in out and "times in EDT<" not in out


def test_r4_a_meeting_after_the_change_shows_est() -> None:
    sample = copy.deepcopy(PAYLOADS["arrival_timeline"])
    sample["meeting_time"] = "2026-11-01T18:00:00Z"  # 13:00 EST
    sample["steps"] = [
        {"label": "Land", "time": "2026-11-01T04:30:00Z"},
        {"label": "Customs", "time": "2026-11-01T05:30:00Z"},
    ]
    out = render_card_html(snapshot("arrival_timeline", payload=sample)[0], place=NYC)
    assert "Meeting at 13:00 EST" in out
    assert re.search(r"<strong>(?:\w{3} )?00:30 EDT</strong> Land", out)  # 04:30Z


def test_b1_the_weather_chart_is_drawn_near_its_rendered_size() -> None:
    out = render_card_html(snapshot("weather_hourly")[0], place=NYC)
    assert 'viewBox="0 0 300 300"' in out  # not a 560-wide box scaled to a fifth
    style = widget_html.STYLE
    assert ".zc-svg text{font-size:20px" in style  # 20 units at about 1:1 is 16 px+
    assert "min-height:220px" in style and "max-height:300px" in style
    assert out.count("<text") >= 4 and "&deg;C" in out


def test_b2_no_raw_iso_time_is_visible_outside_the_details() -> None:
    iso = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    for component in ("flight_status", "weather_hourly", "arrival_timeline"):
        out = render_card_html(snapshot(component)[0], place=NYC)
        visible = re.sub(r"<details.*?</details>", "", out, flags=re.S)
        assert iso.search(visible) is None, (component, iso.search(visible))
    flight = render_card_html(snapshot("flight_status")[0], place=NYC)
    assert "fetched 09:40 EDT" in flight


def test_b3_changes_are_shown_to_two_places() -> None:
    sample = _ecb()
    sample["symbols"][0].update(price=1.1234, change=0.0112, change_pct=-0.0049)
    out = _ecb_card(symbols=sample["symbols"])
    assert "<td>0.01</td>" in out and "<td>-&lt;0.01 %</td>" in out  # not "-0.00 %"
    assert "0.0112" not in out
    sample["symbols"][0].update(change_pct=-0.0079)
    assert "<td>-0.01 %</td>" in _ecb_card(symbols=sample["symbols"])  # rounds, not <


def test_b4_the_board_says_whose_clock_the_times_are_on() -> None:
    with_place = render_snapshot_html(FULL, place=NYC)
    assert "Times are in New York JFK (JFK) local time" in with_place
    without = render_snapshot_html(FULL)
    assert "Destination local time unavailable; times are in UTC" in without


def test_b5_the_timeline_lists_its_stops_in_time_order() -> None:
    out = render_card_html(snapshot("arrival_timeline")[0], place=NYC)
    order = re.findall(r"<li><strong>(?:\w{3} )?(\d\d:\d\d)</strong> (\w+)", out)
    # 17:35 Land, 18:00 Meeting, 18:05 Customs: the fixture lists Customs before Meeting
    assert [label for _, label in order] == ["Land", "Meeting", "Customs"]


def test_o1_the_answer_comes_before_the_freshness_and_the_labels() -> None:
    for component in ("flight_status", "weather_hourly", "arrival_timeline"):
        out = render_card_html(snapshot(component)[0], place=NYC)
        position = {
            "answer": out.index('class="zc-big"'),
            "fresh": out.index('class="zc-fresh"'),
            "labels": out.index('class="zc-labels"'),
        }
        assert position["answer"] < position["fresh"] < position["labels"], component
        assert out.index("<h3>") < position["answer"]
        assert (
            "zc-tag" not in out[: position["answer"]]
        )  # no badge row before the answer


def test_o2_a_replay_and_a_superseded_card_keep_their_state_beside_the_title() -> None:
    replay = render_card_html(snapshot("weather_hourly", replay=True)[0], place=NYC)
    title = re.search(r"<h3>(.*?)</h3>", replay)[1]  # type: ignore[index]
    assert "REPLAY" in title and replay.index("REPLAY") < replay.index('class="zc-big"')
    old = card_with("market_watchlist", replay=True).model_dump() | {"replaced": True}
    stale = render_card_html(CardState.model_construct(**old))
    assert "SUPERSEDED" in re.search(r"<h3>(.*?)</h3>", stale)[1]  # type: ignore[index]


def test_o3_a_card_with_no_big_fact_still_renders_in_order() -> None:
    out = render_card_html(
        snapshot("market_watchlist", payload={}, error={"reason": "Source is down"})[0]
    )
    assert "unavailable" in out and 'class="zc-big"' not in out


def test_n1_a_supplied_now_sets_the_relative_text_and_naive_now_is_refused() -> None:
    card = snapshot("flight_status")[0]  # lands 21:35Z; the card was fetched 13:40Z
    default = _big(render_card_html(card, place=NYC))
    later = _big(
        render_card_html(
            card, place=NYC, now=datetime(2026, 10, 22, 19, 35, tzinfo=UTC)
        )
    )
    assert "in about 7 h 55 min" in default and "in about 2 h" in later
    with pytest.raises(ValueError, match="invalid_now"):
        render_card_html(card, now=datetime(2026, 10, 22, 19, 35))  # noqa: DTZ001
    assert "in about 2 h" in render_snapshot_html(
        [card], place=NYC, now=datetime(2026, 10, 22, 19, 35, tzinfo=UTC)
    )


def test_n2_when_the_stops_span_two_days_every_stop_has_its_weekday() -> None:
    sample = copy.deepcopy(PAYLOADS["arrival_timeline"])
    sample["steps"] = [
        {"label": "Land", "time": "2026-10-22T21:35:00Z"},
        {"label": "Customs", "time": "2026-10-22T22:50:00Z"},
    ]
    sample["meeting_time"] = "2026-10-23T07:30:00Z"  # 03:30 Fri in New York
    out = render_card_html(snapshot("arrival_timeline", payload=sample)[0], place=NYC)
    stops = re.findall(r"<li><strong>(.*?)</strong> (\w+)", out)
    assert stops == [
        ("Thu 17:35", "Land"),
        ("Thu 18:50", "Customs"),
        ("Fri 03:30", "Meeting"),
    ]


def test_n3_a_300_unit_chart_box_renders_20_unit_labels_over_16_px_at_262() -> None:
    out = render_card_html(snapshot("weather_hourly")[0], place=NYC)
    assert 'viewBox="0 0 300 300"' in out
    assert 262 / 300 * 20 >= 16  # the 320 px viewport case measured by the design seat


def test_a1_a_relative_time_is_shown_with_the_moment_it_counts_from() -> None:
    card = snapshot("flight_status")[0]
    head = _big(render_card_html(card, place=NYC))
    # the moment is IN the same line as the relative time, not a footer far below
    assert head.endswith("in about 7 h 55 min, as of 09:40 at JFK"), head
    later = _big(
        render_card_html(
            card, place=NYC, now=datetime(2026, 10, 22, 19, 35, tzinfo=UTC)
        )
    )
    assert later.endswith("in about 2 h, as of 15:35 at JFK"), later
    # without a Place the moment is UTC and says so
    assert _big(render_card_html(card)).endswith("as of 13:40 UTC")


def test_a3_the_timeline_freshness_line_carries_its_as_of_moment() -> None:
    card = snapshot("arrival_timeline", as_of="2026-10-22T13:35:00Z")[0]
    line = _fresh_lines(render_card_html(card, place=NYC))
    assert line == ["computed from a live position 5 min old, as of 09:40 at JFK"]
    unknown = snapshot("arrival_timeline", as_of=None)[0]
    assert _fresh_lines(render_card_html(unknown, place=NYC)) == [
        "computed from a live position (age not provided)"  # no age claimed, no as-of
    ]


def test_a2_no_relative_text_means_no_as_of() -> None:
    sample = copy.deepcopy(PAYLOADS["flight_status"])
    sample["estimated_arrival"] = None  # nothing to count to
    out = render_card_html(snapshot("flight_status", payload=sample)[0], place=NYC)
    assert "in about" not in out and "as of" not in _big(out)
    for component in ("weather_hourly", "market_watchlist"):
        assert (
            "as of"
            not in _fresh_lines(render_card_html(snapshot(component)[0], place=NYC))[0]
        )
