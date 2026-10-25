"""G9-v2 shape/byte/explicit-input vectors, never live permission.

sow: zeocore-SOW-39 amendment G9-v2-citation-metadata.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from zeo_core.contracts.library.view import (
    CitationBinding,
    LibraryView,
    ViewRefused,
    errors_of,
    item_ref,
    validate_binding,
    validate_bytes,
)
from zeo_core.contracts.proof.report import canonical_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "library_view"
NAMES = (
    "item_ref_known_answer.json",
    "item_ref_without_domain_differs.json",
    "refuse-bad-citation-url.json",
    "refuse-bad-digest.json",
    "refuse-bytes-4097.json",
    "refuse-citation-at.json",
    "refuse-citation-fragment.json",
    "refuse-citation-host-not-web.json",
    "refuse-citation-host-sending.json",
    "refuse-citation-id-not-cit.json",
    "refuse-citation-id-record-mismatch.json",
    "refuse-citation-map-sha-mismatch.json",
    "refuse-citation-no-human.json",
    "refuse-citation-nonascii.json",
    "refuse-citation-port.json",
    "refuse-citation-query.json",
    "refuse-citation-trailing-dot.json",
    "refuse-citation-upper-host.json",
    "refuse-citation-url-513.json",
    "refuse-citation-url-differs-from-cit-record.json",
    "refuse-citation-url-differs-from-stored.json",
    "refuse-citation-url-g11.json",
    "refuse-citation-url-host-case.json",
    "refuse-citation-url-percent.json",
    "refuse-duplicate-key.json",
    "refuse-field-account_id.json",
    "refuse-field-allowed-use.json",
    "refuse-field-app_id.json",
    "refuse-field-authors.json",
    "refuse-field-binding_id.json",
    "refuse-field-connection_id.json",
    "refuse-field-content.json",
    "refuse-field-device_id.json",
    "refuse-field-excerpt.json",
    "refuse-field-image.json",
    "refuse-field-item-id.json",
    "refuse-field-labels.json",
    "refuse-field-message.json",
    "refuse-field-producer-receipt.json",
    "refuse-field-provider_message_id.json",
    "refuse-field-quote-budget.json",
    "refuse-field-raw_ref.json",
    "refuse-field-receipt.json",
    "refuse-field-rfc_message_id.json",
    "refuse-field-selection.json",
    "refuse-field-source-kind.json",
    "refuse-field-source_ref.json",
    "refuse-field-span.json",
    "refuse-field-summary.json",
    "refuse-field-title.json",
    "refuse-item-ref-li-id.json",
    "refuse-item-ref-not-hex.json",
    "refuse-item-ref-prefix.json",
    "refuse-missing-citation.json",
    "refuse-nonfinite.json",
    "refuse-publisher-257-bytes.json",
    "refuse-publisher-decomposed.json",
    "refuse-rights-class.json",
    "refuse-schema-v1.json",
    "refuse-surrogate.json",
    "refuse-unknown-attribution.json",
    "refuse-unknown-citation.json",
    "refuse-unknown-revision.json",
    "refuse-unknown-rights.json",
    "refuse-unknown-root.json",
    "refuse-utf8.json",
    "valid-bytes-4096.json",
    "valid-citation-id.json",
    "valid-citation-link-only.json",
    "valid-citation-record-binding.json",
    "valid-citation-web-host.json",
    "valid-maximal.json",
    "valid-operator-owned.json",
)


def test_manifest_is_closed() -> None:
    assert {p.name for p in FIXTURES.iterdir()} == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_view_fixture(name: str) -> None:
    raw = (FIXTURES / name).read_bytes()
    if name.startswith("item_ref_"):
        known = json.loads(raw)
        if "without_domain" in name:
            preimage = {"item_id": known["item_id"]}
            assert item_ref(known["item_id"]) != known["expected_item_ref"]
        else:
            preimage = {
                "domain": "zeo.library-view/item_ref/v2",
                "item_id": known["item_id"],
            }
            assert item_ref(known["item_id"]) == known["expected_item_ref"]
        assert canonical_bytes(preimage) == known["canonical_utf8"].encode()
        assert known["expected_item_ref"] == (
            "ir_" + hashlib.sha256(known["canonical_utf8"].encode()).hexdigest()
        )
        return
    # Context vectors carry inert contract inputs, not new outward wire fields.
    value: dict[str, Any] | None = None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        pass
    if isinstance(value, dict) and "binding" in value:
        view = validate_bytes(json.dumps(value["view"]).encode())
        if name.startswith("valid-"):
            validate_binding(view, CitationBinding(**value["binding"]))
        else:
            with pytest.raises(ViewRefused, match="^citation_mismatch$"):
                validate_binding(view, CitationBinding(**value["binding"]))
    elif name.startswith("valid-"):
        view = validate_bytes(raw)
        assert view.producer == "dt-library"
        assert errors_of(raw) == []
    else:
        with pytest.raises(ViewRefused):
            validate_bytes(raw)
        assert errors_of(raw) == [{"loc": [], "type": "library_view_refused"}]


def test_binding_positive() -> None:
    view = validate_bytes((FIXTURES / "valid-citation-link-only.json").read_bytes())
    validate_binding(
        view,
        CitationBinding(
            stored_url="https://example.org/article",
            record_url="https://example.org/article",
            record_id="cit_" + "4" * 64,
            map_sha256="3" * 64,
            publisher="Example Org",
            web_host="example.org",
            host_kind="web",
            recorded_by="Named Human",
        ),
    )


@pytest.mark.parametrize("value", ["li_bad", "ir_" + "1" * 64, "li_" + "G" * 64])
def test_invalid_internal_item_id(value: str) -> None:
    with pytest.raises(ValueError, match="^item_id$"):
        item_ref(value)


def test_cli_dispatch_preserves_redaction_and_alias() -> None:
    from zeo_core.cli.__main__ import _validate_bytes_contract
    from zeo_core.cli.schemas import BYTE_CONTRACTS, resolve

    assert resolve("zeo.library-view/v2") == "library.view-v2"
    assert resolve("zeo.access-receipt/v2") == "access.receipt-v2"
    assert "library.view-v2" in BYTE_CONTRACTS
    status, answer = _validate_bytes_contract(
        "library.view-v2", (FIXTURES / "valid-citation-link-only.json").read_bytes()
    )
    assert status == 0 and answer["ok"] is True
    status, answer = _validate_bytes_contract(
        "library.view-v2", (FIXTURES / "refuse-unknown-root.json").read_bytes()
    )
    assert status == 20
    assert "PRIVATE_SENTINEL" not in json.dumps(answer)
    assert "EXTRA_SENTINEL" not in json.dumps(answer)
    # Existing UI dispatch remains separate and refuses this library document.
    status, _ = _validate_bytes_contract(
        "ui.result-card", (FIXTURES / "valid-citation-link-only.json").read_bytes()
    )
    assert status == 20


def test_byte_bound_exact() -> None:
    assert len((FIXTURES / "valid-bytes-4096.json").read_bytes()) == 4096
    assert len((FIXTURES / "refuse-bytes-4097.json").read_bytes()) == 4097


def test_known_answer_is_independent_sha() -> None:
    fixture = json.loads((FIXTURES / "item_ref_known_answer.json").read_bytes())
    assert (
        "ir_" + hashlib.sha256(fixture["canonical_utf8"].encode()).hexdigest()
        == fixture["expected_item_ref"]
    )


def test_nonfinite_refuses_before_model_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    known = validate_bytes((FIXTURES / "valid-citation-link-only.json").read_bytes())
    calls: list[object] = []

    def unexpected(value: object) -> LibraryView:
        calls.append(value)
        return known

    monkeypatch.setattr(LibraryView, "model_validate", staticmethod(unexpected))
    with pytest.raises(ViewRefused, match="^invalid_view$"):
        validate_bytes((FIXTURES / "refuse-nonfinite.json").read_bytes())
    assert calls == []


def test_schema_url_pattern_and_code_only_byte_annotation() -> None:
    import re

    from zeo_core.cli.schemas import render_schema

    schema = render_schema("library.view-v2")
    publisher = schema["$defs"]["Attribution"]["properties"]["publisher"]
    assert publisher["maxLength"] == 256
    assert publisher["x-maxUtf8Bytes"] == 256
    citation = schema["$defs"]["Citation"]["properties"]["url"]
    pattern = re.compile(citation["pattern"])
    assert pattern.search("https://example.org/article")
    for url in [
        "http://example.org/a",
        "https://example.org/%40",
        "https://EXAMPLE.org/a",
        "https://example.org./a",
        "https://example.org/a?x",
        "https://example.org/a\n",
    ]:
        assert not pattern.search(url)
