"""zeocore-SOW-42: the one-card ``zeo.ui-result/v1`` subset, vector by vector.

The vectors follow SOW-38 rev 8 section 9 for the parts a single read-only card uses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from zeo_core.cli.schemas import render_schema
from zeo_core.contracts.ui import UiRefused, errors_of, validate_bytes
from zeo_core.contracts.ui import _unicode16 as unicode16
from zeo_core.contracts.ui.card import _PATTERN_D
from zeo_core.contracts.ui.wire import MAX_COMPONENT_BYTES, MAX_RESPONSE_BYTES

FIXTURE = (
    Path(__file__).parent / "fixtures" / "ui_result_card" / "valid-labels-card.json"
)


def valid() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(FIXTURE.read_text())
    return value


def dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def refusal(value: object) -> UiRefused:
    with pytest.raises(UiRefused) as caught:
        validate_bytes(value if isinstance(value, bytes) else dump(value))
    return caught.value


def card(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["components"][0]  # type: ignore[no-any-return]


def with_fact(**fact: Any) -> dict[str, Any]:  # noqa: ANN401
    doc = valid()
    card(doc)["props"]["facts"] = [{"label": "L", **fact}]
    return doc


def test_the_valid_card_is_accepted_and_round_trips_unchanged() -> None:
    doc = valid()
    result = validate_bytes(dump(doc))
    assert result.model_dump(mode="json", by_alias=True, exclude_none=True) == doc
    assert errors_of(dump(doc)) == []


# --- envelope and grammar -------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "schema",
        "result_id",
        "in_reply_to",
        "produced_at",
        "complete",
        "plain_text",
        "components",
    ],
)
def test_every_envelope_key_is_required(key: str) -> None:
    doc = valid()
    del doc[key]
    assert refusal(doc).code == "missing"


def test_an_unknown_key_is_refused_at_every_level() -> None:
    for path in (
        [],
        ["in_reply_to"],
        ["components", 0],
        ["components", 0, "props"],
        ["components", 0, "snapshot"],
        ["components", 0, "snapshot", "source"],
        ["components", 0, "props", "facts", 0],
    ):
        doc = valid()
        node: Any = doc
        for step in path:
            node = node[step]
        node["extra"] = 1
        assert refusal(doc).code == "extra_forbidden", path


def test_a_producer_key_is_an_unknown_key() -> None:
    doc = valid()
    doc["producer"] = "broker"
    assert refusal(doc).code == "extra_forbidden"


def test_a_duplicate_key_is_refused() -> None:
    raw = b'{"schema":"zeo.ui-result/v1","schema":"zeo.ui-result/v1"}'
    assert refusal(raw).code == "duplicate_key"


@pytest.mark.parametrize(
    "tag", ["zeo.ui-result/v2", "zeo.ui-component/v1", "", "ZEO.ui-result/v1"]
)
def test_only_the_exact_wire_tag_is_accepted(tag: str) -> None:
    doc = valid()
    doc["schema"] = tag
    assert refusal(doc).code == "literal_error"


@pytest.mark.parametrize(
    "kind", ["table", "chart", "form", "action_button", "list", "Card"]
)
def test_every_other_kind_is_refused_not_coerced(kind: str) -> None:
    doc = valid()
    card(doc)["kind"] = kind
    assert refusal(doc).code == "literal_error"


def test_a_second_component_and_an_empty_list_are_refused() -> None:
    doc = valid()
    doc["components"].append(copy.deepcopy(doc["components"][0]))
    assert refusal(doc).code == "too_long"
    doc["components"] = []
    assert refusal(doc).code == "too_short"


def test_action_refs_must_be_empty_in_the_read_only_subset() -> None:
    doc = valid()
    card(doc)["props"]["action_refs"] = ["a1"]
    assert refusal(doc).code == "too_long"


def test_fallback_text_and_accessibility_label_are_required_and_nonempty() -> None:
    doc = valid()
    card(doc)["fallback_text"] = ""
    assert refusal(doc).code == "string_too_short"
    del card(doc)["fallback_text"]
    assert refusal(doc).code == "missing"
    doc = valid()
    del card(doc)["accessibility_label"]
    assert refusal(doc).code == "missing"


@pytest.mark.parametrize(
    ("where", "key"),
    [
        ((), "snapshot"),
        (("props",), "status"),
        (("props",), "progress"),
    ],
)
def test_an_optional_key_is_absent_never_null(where: tuple[str, ...], key: str) -> None:
    doc = valid()
    node = card(doc)
    for step in where:
        node = node[step]
    node[key] = None
    error = refusal(doc)
    assert (error.code, error.loc) == (
        "ui_optional_null",
        ("components", 0, *where, key),
    )


def test_null_is_refused_for_expires_at_currency_and_scale() -> None:
    doc = valid()
    card(doc)["snapshot"]["expires_at"] = None
    error = refusal(doc)
    assert (error.code, error.loc) == (
        "ui_optional_null",
        ("components", 0, "snapshot", "expires_at"),
    )
    for key in ("currency", "scale"):
        doc = with_fact(value_type="text", value="x")
        card(doc)["props"]["facts"][0][key] = None
        error = refusal(doc)
        assert (error.code, error.loc) == (
            "ui_optional_null",
            ("components", 0, "props", "facts", 0, key),
        )


def test_complete_must_be_a_boolean() -> None:
    for bad in (1, "true", None):
        doc = valid()
        doc["complete"] = bad
        assert refusal(doc).code == "bool_type"
    doc = valid()
    del doc["complete"]
    assert refusal(doc).code == "missing"


# --- identifiers ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Upper", "a b", "1a", "a" * 129, "", "a\n"])
def test_component_ids_follow_their_grammar(bad: str) -> None:
    doc = valid()
    card(doc)["id"] = bad
    error = refusal(doc)
    assert (error.code, error.loc) == (
        "string_pattern_mismatch",
        ("components", 0, "id"),
    )


def test_component_id_boundary_is_128_bytes() -> None:
    doc = valid()
    card(doc)["id"] = "a" * 128
    validate_bytes(dump(doc))
    card(doc)["id"] = "a" * 129
    assert refusal(doc).code == "string_pattern_mismatch"


@pytest.mark.parametrize(
    "ref", ["hubspot.newsroom@1", "a b", "é", "", "x" * 129, "r\n"]
)
def test_owner_refs_refuse_what_does_not_fit(ref: str) -> None:
    doc = valid()
    doc["result_id"] = ref
    error = refusal(doc)
    assert (error.code, error.loc) == ("string_pattern_mismatch", ("result_id",))


def test_operation_names_are_lower_case() -> None:
    doc = valid()
    doc["in_reply_to"]["operation"] = "Google.Gmail"
    assert refusal(doc).code == "string_pattern_mismatch"


# --- timestamps ----------------------------------------------------------------


@pytest.mark.parametrize("ok", ["0000-02-29T00:00:00Z", "2000-02-29T00:00:00Z"])
def test_real_calendar_dates_are_accepted(ok: str) -> None:
    doc = valid()
    doc["produced_at"] = ok
    card(doc)["snapshot"]["observed_at"] = "0000-01-01T00:00:00Z"
    validate_bytes(dump(doc))


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ("1900-02-29T00:00:00Z", "ui_timestamp_calendar"),
        ("2026-13-01T00:00:00Z", "ui_timestamp_calendar"),
        ("2026-10-17T14:00:60Z", "ui_timestamp_time"),
        ("2026-10-17T24:00:00Z", "ui_timestamp_time"),
        ("2026-10-17T14:00:00+01:00", "string_too_long"),
        ("2026-10-17T14:00:00.5Z", "string_too_long"),
        ("2026-10-17T14:00:00Z\n", "string_too_long"),
        ("2026-10-17t14:00:00z", "string_pattern_mismatch"),
        ("２０２６-10-17T14:00:00Z", "string_pattern_mismatch"),
    ],
)
def test_other_timestamps_are_refused(bad: str, code: str) -> None:
    doc = valid()
    doc["produced_at"] = bad
    error = refusal(doc)
    assert (error.code, error.loc) == (code, ("produced_at",))


def test_snapshot_times_are_ordered() -> None:
    doc = valid()
    card(doc)["snapshot"]["expires_at"] = card(doc)["snapshot"]["observed_at"]
    error = refusal(doc)
    assert (error.code, error.loc) == (
        "ui_expiry_order",
        ("components", 0, "snapshot"),
    )
    card(doc)["snapshot"]["expires_at"] = "2026-10-19T04:30:00Z"
    validate_bytes(dump(doc))
    doc["produced_at"] = "2026-10-19T03:00:00Z"  # observed_at is later than produced_at
    assert refusal(doc).code == "ui_observed_after_produced"


# --- facts: values -------------------------------------------------------------


@pytest.mark.parametrize(
    "fact",
    [
        {"value_type": "text", "value": "x"},
        {"value_type": "integer", "value": 2**53 - 1},
        {"value_type": "integer", "value": -(2**53 - 1)},
        {"value_type": "boolean", "value": False},
        {"value_type": "null", "value": None},
        {"value_type": "timestamp", "value": "2026-10-19T03:00:00Z"},
        {"value_type": "url", "value": "https://example.com:8443/a/b?c=d"},
        {
            "value_type": "decimal_string",
            "value": "-12.50",
            "currency": "EUR",
            "scale": 2,
        },
        {"value_type": "decimal_string", "value": "0"},
    ],
)
def test_well_typed_facts_are_accepted(fact: dict[str, Any]) -> None:
    validate_bytes(dump(with_fact(**fact)))


@pytest.mark.parametrize(
    "fact",
    [
        {"value_type": "integer", "value": 1.0},
        {"value_type": "integer", "value": True},
        {"value_type": "integer", "value": 2**53},
        {"value_type": "integer", "value": "1"},
        {"value_type": "boolean", "value": 0},
        {"value_type": "null", "value": 0},
        {"value_type": "text", "value": None},
        {"value_type": "text", "value": 5},
        {"value_type": "text", "value": "a" * 1025},
        {"value_type": "decimal_string", "value": "1e3"},
        {"value_type": "decimal_string", "value": "+1"},
        {"value_type": "decimal_string", "value": "1.0e2"},
        {"value_type": "decimal_string", "value": "-0"},
        {"value_type": "decimal_string", "value": "-0.00"},
        {"value_type": "decimal_string", "value": "1.5", "currency": "EUR"},
        {
            "value_type": "decimal_string",
            "value": "1.5",
            "currency": "EUR",
            "scale": 1 + 1,
        },
        {"value_type": "decimal_string", "value": "1.5", "currency": "eur", "scale": 1},
        {"value_type": "text", "value": "x", "currency": "EUR", "scale": 2},
        {"value_type": "timestamp", "value": "2026-10-19"},
        {"value_type": "url", "value": "http://example.com/"},
        {"value_type": "url", "value": "https://user@example.com/"},
        {"value_type": "url", "value": "https://example.com/a b"},
        {"value_type": "url", "value": "https://example.com/#frag"},
        {"value_type": "url", "value": "https://example.com:99999/"},
        {"value_type": "url", "value": "https://example.com/" + "a" * 1024},
        {"value_type": "bogus", "value": 1},
    ],
)
def test_badly_typed_facts_are_refused(fact: dict[str, Any]) -> None:
    assert errors_of(dump(with_fact(**fact)))


def test_a_url_of_exactly_1024_bytes_is_accepted() -> None:
    url = "https://example.com/" + "a" * (1024 - len("https://example.com/"))
    validate_bytes(dump(with_fact(value_type="url", value=url)))


def test_a_fact_without_its_value_is_refused() -> None:
    doc = valid()
    card(doc)["props"]["facts"] = [{"label": "L", "value_type": "text"}]
    assert refusal(doc).code == "missing" or errors_of(dump(doc))


def test_progress_is_finite_and_absent_means_unknown() -> None:
    doc = valid()
    card(doc)["props"]["progress"] = {"current": 0, "total": 10, "unit": "items"}
    assert validate_bytes(dump(doc)).components[0].props.progress is not None
    for bad in (True, "1", 2**53, None):
        doc = valid()
        card(doc)["props"]["progress"] = {"current": bad, "total": 10, "unit": "items"}
        assert errors_of(dump(doc))


# --- bounds --------------------------------------------------------------------


def test_fact_evidence_and_text_counts_have_a_first_refused() -> None:
    doc = valid()
    card(doc)["props"]["facts"] = [
        {"label": "L", "value_type": "null", "value": None}
    ] * 12
    validate_bytes(dump(doc))
    card(doc)["props"]["facts"].append(
        {"label": "L", "value_type": "null", "value": None}
    )
    assert refusal(doc).code == "too_long"
    doc = valid()
    card(doc)["props"]["evidence"] = [{"kind": "receipt", "ref": "r1"}] * 8
    validate_bytes(dump(doc))
    card(doc)["props"]["evidence"].append({"kind": "receipt", "ref": "r1"})
    assert refusal(doc).code == "too_long"


@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", 256), ("accessibility_label", 256), ("fallback_text", 4096)],
)
def test_text_bounds_are_in_utf8_bytes(field: str, limit: int) -> None:
    doc = valid()
    card(doc)[field] = "a" * limit
    validate_bytes(dump(doc))
    card(doc)[field] = "a" * (limit + 1)
    assert errors_of(dump(doc))
    # 100 three-byte characters are 300 bytes: refused as a title
    if field == "title":
        card(doc)[field] = "€" * 100
        assert errors_of(dump(doc))


def test_the_summary_bound_is_4096_bytes() -> None:
    doc = valid()
    card(doc)["props"]["summary"] = "a" * 4096
    validate_bytes(dump(doc))
    card(doc)["props"]["summary"] = "a" * 4097
    assert errors_of(dump(doc))


def test_the_response_bound_is_first_refused_at_one_byte_over() -> None:
    assert refusal(b" " * (MAX_RESPONSE_BYTES + 1)).code == "response_too_large"
    doc = valid()
    raw = dump(doc)
    padded = raw + b" " * (MAX_RESPONSE_BYTES - len(raw))
    assert len(padded) == MAX_RESPONSE_BYTES
    validate_bytes(padded)


def test_the_component_bound_is_measured_on_the_text_as_received() -> None:
    doc = valid()
    raw = dump(doc).decode()
    start = raw.index('{"id"')
    end = raw.index("}]}", start) + 1  # the component object ends before the list
    component = raw[start:end]
    pad = MAX_COMPONENT_BYTES - len(component.encode())
    ok = raw[:start] + component[:-1] + " " * pad + "}" + raw[end:]
    validate_bytes(ok.encode())
    too_big = raw[:start] + component[:-1] + " " * (pad + 1) + "}" + raw[end:]
    assert refusal(too_big.encode()).code == "component_too_large"


def test_depth_is_bounded_at_nine_before_content_is_read() -> None:
    assert refusal(b"[" * 10 + b"]" * 10).code == "too_deep"
    assert refusal(b"[" * 10_000).code == "too_deep"
    nine = b'{"a":' * 4 + b"[" * 5 + b"]" * 5 + b"}" * 4
    assert refusal(nine).code != "too_deep"


# --- JSON layer ---------------------------------------------------------------


def test_json_layer_refusals() -> None:
    good = dump(valid())
    assert refusal(b"\xef\xbb\xbf" + good).code == "byte_order_mark"
    assert refusal(good[:-1] + b"\xff}").code == "invalid_utf8"
    assert refusal(good + b" {}").code == "invalid_json"
    assert refusal(good + b"x").code == "invalid_json"
    assert refusal(b"[]").code == "not_an_object"
    assert refusal(b"").code == "invalid_json"
    assert refusal(b'{"a": NaN}').code == "not_a_finite_number"
    assert refusal(b'{"a": Infinity}').code == "not_a_finite_number"
    assert refusal(b'{"a": 1e999}').code == "not_a_finite_number"


def test_a_lone_surrogate_escape_is_refused_anywhere() -> None:
    raw = dump(valid()).replace(b"Gmail labels", b"G\\ud800")
    assert refusal(raw).code == "lone_surrogate"
    raw = dump(valid()).replace(b'"kind"', b'"ki\\udc00nd"', 1)
    assert refusal(raw).code == "lone_surrogate"
    paired = dump(valid()).replace(b"Gmail labels", b"G\\ud83d\\ude00")
    validate_bytes(paired)  # a valid pair is a valid character


@pytest.mark.parametrize(
    "char", ["\x00", "\x1b", "\n", "\x7f", "\x85", "\ud800", "\U0001fffe", "͸"]
)
def test_class_s_refuses_controls_surrogates_and_unassigned(char: str) -> None:
    doc = valid()
    card(doc)["title"] = f"a{char}b"
    assert errors_of(json.dumps(doc).encode())  # \ud800 stays escaped: surrogate


@pytest.mark.parametrize("char", ["­", "​", "‮", "﻿", "\U000e0001"])
def test_class_s_refuses_default_ignorable_but_class_d_and_l_allow_it(
    char: str,
) -> None:
    doc = valid()
    card(doc)["title"] = f"a{char}b"
    assert errors_of(dump(doc))
    doc = valid()
    card(doc)["props"]["summary"] = f"a{char}b"
    card(doc)["props"]["facts"] = [
        {"label": "L", "value_type": "text", "value": f"a{char}b"}
    ]
    validate_bytes(dump(doc))


def test_lf_is_allowed_only_in_class_l_and_a_literal_escape_is_text() -> None:
    doc = valid()
    card(doc)["props"]["summary"] = "a\nb"
    validate_bytes(dump(doc))
    card(doc)["props"]["facts"] = [
        {"label": "L", "value_type": "text", "value": "a\nb"}
    ]
    assert errors_of(dump(doc))
    doc = valid()
    card(doc)["props"]["summary"] = (
        "\\x1b and \\u001b and https://example.com and <b>x</b>"
    )
    validate_bytes(dump(doc))


# --- Unicode tables -------------------------------------------------------------


def test_the_pinned_tables_are_unchanged_and_match_unicodedata_when_it_is_16() -> None:
    pinned = hashlib.sha256(
        repr((unicode16.UNASSIGNED, unicode16.DEFAULT_IGNORABLE)).encode()
    ).hexdigest()
    assert pinned == "37371e8064ae742b3b0c7df49003d9f79388b6bf70c3523705facd3d1be404cc"
    if unicodedata.unidata_version != unicode16.UNICODE_VERSION:
        return  # the pinned tables are the contract; the interpreter is not
    for cp in range(0x110000):
        expected = unicodedata.category(chr(cp)) == "Cn"
        assert unicode16.is_unassigned(cp) is expected, hex(cp)


def test_default_ignorable_ranges_match_the_design() -> None:
    for cp in (
        0xAD,
        0x34F,
        0x61C,
        0x115F,
        0x1160,
        0x17B4,
        0x180B,
        0x200B,
        0x202E,
        0x2060,
        0x206F,
        0x3164,
        0xFE00,
        0xFEFF,
        0xFFA0,
        0xFFF8,
        0x1BCA0,
        0x1D173,
        0xE0000,
        0xE0FFF,
    ):
        assert unicode16.is_default_ignorable(cp), hex(cp)
    for cp in (0xAC, 0xAE, 0x41, 0x200A, 0x2010, 0xFE10, 0x1BC9F, 0xE1000):
        assert not unicode16.is_default_ignorable(cp), hex(cp)


# --- no side effects, CLI, schema -----------------------------------------------


def test_validation_is_pure_and_leaves_the_input_untouched() -> None:
    raw = dump(valid())
    snapshot = bytes(raw)
    validate_bytes(raw)
    assert raw == snapshot


def test_errors_never_echo_input_values() -> None:
    doc = valid()
    card(doc)["title"] = "SECRET-" + "x" * 300
    doc["unknown_key_SECRET"] = "SECRET-value"  # noqa: S105
    text = json.dumps(errors_of(dump(doc)))
    assert "SECRET" not in text


def test_schema_describes_the_same_subset() -> None:
    schema = render_schema("ui.result-card")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"]["const"] == "zeo.ui-result/v1"
    assert schema["properties"]["components"]["minItems"] == 1
    assert schema["properties"]["components"]["maxItems"] == 1
    props = schema["$defs"]["CardProps"]["properties"]
    assert props["action_refs"]["maxItems"] == 0
    assert props["facts"]["maxItems"] == 12
    assert props["evidence"]["maxItems"] == 8
    assert schema["$defs"]["CardComponent"]["properties"]["kind"]["const"] == "card"
    assert set(schema["required"]) == {
        "schema",
        "result_id",
        "in_reply_to",
        "produced_at",
        "complete",
        "plain_text",
        "components",
    }


# --- the exported JSON Schema agrees with the validator (Rasa, source inspection) ---


def _schema_accepts(doc: object) -> bool:
    import jsonschema  # type: ignore[import-untyped]  # no stubs; the host runtime provides jsonschema

    schema = render_schema("ui.result-card")
    return bool(jsonschema.Draft202012Validator(schema).is_valid(doc))


def _runtime_accepts(doc: object) -> bool:
    return not errors_of(dump(doc))


_AGREEMENT_FACTS: list[dict[str, Any]] = [
    {"value_type": "text", "value": "x"},
    {"value_type": "integer", "value": 3},
    {"value_type": "decimal_string", "value": "1.50", "currency": "EUR", "scale": 2},
    {"value_type": "decimal_string", "value": "7"},
    {"value_type": "boolean", "value": True},
    {"value_type": "null", "value": None},
    {"value_type": "timestamp", "value": "2026-10-19T03:00:00Z"},
    {"value_type": "url", "value": "https://example.com/a"},
    # refused by the validator and expressible in JSON Schema
    {"value_type": "integer", "value": "not-an-integer"},
    {"value_type": "integer", "value": 2**53},
    {"value_type": "text", "value": 5},
    {"value_type": "text", "value": None},
    {"value_type": "boolean", "value": 0},
    {"value_type": "null", "value": 0},
    {"value_type": "decimal_string", "value": "1e3"},
    {"value_type": "decimal_string", "value": 1.5},
    {"value_type": "decimal_string", "value": "1.5", "currency": "EUR"},
    {"value_type": "decimal_string", "value": "1.5", "currency": "eur", "scale": 1},
    {"value_type": "text", "value": "x", "currency": "EUR", "scale": 2},
    {"value_type": "timestamp", "value": "2026-10-19"},
    {"value_type": "url", "value": "http://example.com/"},
    {"value_type": "bogus", "value": 1},
]


@pytest.mark.parametrize("fact", _AGREEMENT_FACTS)
def test_schema_and_validator_agree_on_typed_facts(fact: dict[str, Any]) -> None:
    doc = with_fact(**fact)
    assert _schema_accepts(doc) == _runtime_accepts(doc)


def test_the_schema_never_refuses_what_the_validator_accepts() -> None:
    for doc in (valid(), *(with_fact(**f) for f in _AGREEMENT_FACTS)):
        if _runtime_accepts(doc):
            assert _schema_accepts(doc)


def test_the_schema_refuses_explicit_nulls_of_optional_keys() -> None:
    for where, key in (
        ("snapshot", "snapshot"),
        ("props", "status"),
        ("props", "progress"),
    ):
        doc = valid()
        target = card(doc) if where == "snapshot" else card(doc)["props"]
        target[key] = None
        assert not _schema_accepts(doc)
        assert not _runtime_accepts(doc)
    doc = with_fact(value_type="text", value="x")
    card(doc)["props"]["facts"][0]["currency"] = None
    assert not _schema_accepts(doc)
    assert not _runtime_accepts(doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.pop("complete"),
        lambda d: d.update(complete="true"),
        lambda d: d.update(schema="zeo.ui-result/v2"),
        lambda d: d["components"].append(copy.deepcopy(d["components"][0])),
        lambda d: card(d).update(kind="table"),
        lambda d: card(d)["props"].update(action_refs=["a1"]),
        lambda d: card(d)["props"]["facts"].extend([{}] * 13),
        lambda d: card(d)["props"].update(summary="a" * 4097),
        lambda d: card(d).update(id="Upper"),
        lambda d: d["in_reply_to"].update(operation="Upper"),
        lambda d: d.update(result_id="has space"),
    ],
)
def test_structural_refusals_agree(mutate: Any) -> None:  # noqa: ANN401
    doc = valid()
    mutate(doc)
    assert not _schema_accepts(doc)
    assert not _runtime_accepts(doc)


def test_the_valid_card_is_accepted_by_both() -> None:
    assert _schema_accepts(valid())
    assert _runtime_accepts(valid())


# --- council round on #148: more vectors ------------------------------------------


def test_a_nested_duplicate_key_is_refused() -> None:
    raw = dump(valid()).replace(
        b'"kind":"operation"', b'"kind":"operation","kind":"receipt"', 1
    )
    assert refusal(raw).code == "duplicate_key"


def test_minus_infinity_and_huge_integers_are_refused_unread() -> None:
    assert refusal(b'{"a": -Infinity}').code == "not_a_finite_number"
    assert refusal(b'{"a": ' + b"9" * 5000 + b"}").code == "integer_too_large"
    assert refusal(b'{"a": 12345678901234567}').code == "integer_too_large"
    assert refusal(b'{"a": -12345678901234567}').code == "integer_too_large"


@pytest.mark.parametrize("char", ["\t", "\r", "\x1b", "\x00"])
def test_tab_cr_and_escape_are_refused_in_class_l_d_and_s(char: str) -> None:
    doc = valid()
    card(doc)["props"]["summary"] = f"a{char}b"
    assert errors_of(dump(doc))
    doc = valid()
    card(doc)["props"]["facts"] = [
        {"label": "L", "value_type": "text", "value": f"a{char}b"}
    ]
    assert errors_of(dump(doc))
    doc = valid()
    card(doc)["title"] = f"a{char}b"
    assert errors_of(dump(doc))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "//example.com/",
        "HTTPS://example.com/",
        "ftp://example.com/",
    ],
)
def test_only_https_urls_are_accepted(url: str) -> None:
    error = refusal(with_fact(value_type="url", value=url))
    assert error.code == "ui_url_profile"


def test_the_astral_default_ignorable_is_refused_by_the_validator_not_the_pattern() -> (
    None
):
    doc = valid()
    card(doc)["title"] = "a\U000e0001b"
    assert refusal(doc).code == "ui_text_ignorable"
    card(doc)["title"] = "a\u202eb"
    assert refusal(doc).code == "string_pattern_mismatch"


def test_the_exported_schema_says_it_is_necessary_but_not_sufficient() -> None:
    assert (
        "NECESSARY BUT NOT SUFFICIENT" in render_schema("ui.result-card")["description"]
    )


_BAD_FACT_CODES = [
    "ui_fact_integer", "ui_fact_integer", "ui_fact_integer_range", "ui_fact_integer",
    "ui_fact_boolean", "ui_fact_null", "ui_fact_string", "ui_fact_string",
    "ui_text_bytes", "ui_decimal_shape", "ui_decimal_shape", "ui_decimal_shape",
    "ui_decimal_negative_zero", "ui_decimal_negative_zero", "ui_currency_needs_scale",
    "ui_scale_digits", "string_pattern_mismatch", "ui_currency_scale_kind",
    "ui_timestamp_shape", "ui_url_profile", "ui_url_profile", "ui_url_profile",
    "ui_url_fragment", "ui_url_port", "ui_url_profile", "literal_error",
]  # fmt: skip


def test_every_badly_typed_fact_has_its_exact_code() -> None:
    marks = [
        m
        for m in test_badly_typed_facts_are_refused.pytestmark
        if m.name == "parametrize"
    ]
    facts = list(marks[0].args[1])
    assert len(facts) == len(_BAD_FACT_CODES)
    for fact, code in zip(facts, _BAD_FACT_CODES, strict=True):
        assert refusal(dump(with_fact(**fact))).code == code, fact


def test_the_exported_text_fact_carries_the_class_d_pattern() -> None:
    schema = render_schema("ui.result-card")
    fact = next(v for k, v in schema["$defs"].items() if k.startswith("Fact"))
    text = next(
        b["then"]["properties"]["value"]
        for b in fact["allOf"]
        if b["if"]["properties"]["value_type"]["const"] == "text"
    )
    assert text["pattern"] == _PATTERN_D
