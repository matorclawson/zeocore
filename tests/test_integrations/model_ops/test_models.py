"""Inputs are closed and bounded, with no credential or dispatch surface."""

import hashlib
import json
import unittest
from pathlib import Path

import pytest
from pydantic import ValidationError

from zeo_core.integrations.hosted.client import HostedClientError, HostedExpectedBinding
from zeo_core.integrations.model_ops import (
    CataloguedModel,
    DraftModelOperationClient,
    ModelCatalogue,
    ModelMessage,
    TextCompletionRequest,
    parse_model_request_json,
)
from zeo_core.integrations.model_ops.models import Provider


class ModelOperationTests(unittest.TestCase):
    def catalogue(self, provider: Provider = "anthropic") -> ModelCatalogue:
        return ModelCatalogue(
            provider=provider,
            models=(
                CataloguedModel(
                    model="synthetic-snapshot-20261013",
                    max_output_tokens=128,
                    accepts_temperature=True,
                ),
            ),
            connector_revision="synthetic-connector-v1",
            proposed_contract_sha256="a" * 64,
        )

    def request(self) -> TextCompletionRequest:
        return TextCompletionRequest(
            model="synthetic-snapshot-20261013",
            messages=(ModelMessage(role="user", text="Fictional test input"),),
            max_output_tokens=64,
        )

    def test_closed_input_does_not_accept_transport_or_key_fields(self) -> None:
        for key in ("api_key", "authorization", "url", "tools"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                TextCompletionRequest.model_validate(
                    {**self.request().model_dump(), key: "synthetic-invalid-field"}
                )

    def test_byte_bound_not_character_bound(self) -> None:
        with self.assertRaises(ValidationError):
            TextCompletionRequest(
                model="synthetic-snapshot-20261013",
                messages=(ModelMessage(role="user", text="界" * 90_000),),
                max_output_tokens=64,
            )

    def test_no_blank_message_or_surrogate_text(self) -> None:
        with self.assertRaises(ValidationError):
            ModelMessage(role="user", text="  ")
        with self.assertRaises(ValidationError):
            TextCompletionRequest(
                model="synthetic-snapshot-20261013",
                messages=(ModelMessage(role="user", text="\ud800"),),
                max_output_tokens=1,
            )

    def test_integer_output_cap_is_required_and_strict(self) -> None:
        for cap in (True, 0, -1, "10"):
            with self.subTest(cap=cap), self.assertRaises(ValidationError):
                TextCompletionRequest.model_validate(
                    {**self.request().model_dump(), "max_output_tokens": cap}
                )

    def test_duplicate_ids_and_known_aliases_refused(self) -> None:
        for ids in (("a", "a"), ("latest",), ("model-latest",), ("default",)):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                ModelCatalogue.model_validate(
                    {
                        **self.catalogue().model_dump(),
                        "models": [
                            {"model": model, "max_output_tokens": 128} for model in ids
                        ],
                    }
                )

    def test_exact_provider_operations_and_expected_binding(self) -> None:
        operations: dict[Provider, str] = {
            "anthropic": "anthropic.messages.create",
            "openai": "openai.responses.create",
            "google": "google.gemini.generate",
        }
        for provider, operation in operations.items():
            with self.subTest(provider=provider):
                binding = HostedExpectedBinding(
                    external_identity="synthetic-project", connection_revision="1"
                )
                client = DraftModelOperationClient(
                    connection_output_token_limit=128,
                    catalogue=self.catalogue(provider),
                    connection_id="synthetic-connection",
                    expected_binding=binding,
                )
                prepared = client.prepare(self.request(), idempotency_key="same-act")
                self.assertEqual(prepared.operation_id, operation)
                self.assertEqual(prepared.expect, binding)
                self.assertEqual(prepared.idempotency_key, "same-act")
                self.assertEqual(client.capability, "text_completion")
                self.assertFalse(hasattr(client, "invoke"))
                replay = client.prepare(self.request(), idempotency_key="same-act")
                self.assertEqual(prepared, replay)

    def test_unknown_model_fails_without_echoing_input(self) -> None:
        client = DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=self.catalogue(),
            connection_id="synthetic-connection",
            expected_binding=HostedExpectedBinding(
                external_identity="synthetic-project", connection_revision="1"
            ),
        )
        req = self.request().model_copy(update={"model": "unknown-model"})
        with self.assertRaisesRegex(
            HostedClientError, "select an owner-pinned catalogue model"
        ):
            client.prepare(req, idempotency_key="same-act")

    def test_prompt_is_excluded_from_repr(self) -> None:
        self.assertNotIn("Fictional test input", repr(self.request()))

    def test_prepare_revalidates_a_model_copy_before_output(self) -> None:
        client = DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=self.catalogue(),
            connection_id="synthetic-connection",
            expected_binding=HostedExpectedBinding(
                external_identity="synthetic-project", connection_revision="1"
            ),
        )
        bypassed = self.request().model_copy(update={"max_output_tokens": -1})
        with self.assertRaises(ValidationError):
            client.prepare(bypassed, idempotency_key="same-act")

    def test_validation_text_does_not_echo_prompt(self) -> None:
        value = {**self.request().model_dump(), "max_output_tokens": 0}
        with self.assertRaises(ValidationError) as failure:
            TextCompletionRequest.model_validate(value)
        self.assertNotIn("Fictional test input", str(failure.exception))

    def test_temperature_is_bounded_finite_and_not_a_boolean(self) -> None:
        for value in (-0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TextCompletionRequest.model_validate(
                    {**self.request().model_dump(), "temperature": value}
                )

    def test_system_and_temperature_survive_preparation(self) -> None:
        client = DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=self.catalogue(),
            connection_id="synthetic-connection",
            expected_binding=HostedExpectedBinding(
                external_identity="synthetic-project", connection_revision="1"
            ),
        )
        request = TextCompletionRequest.model_validate(
            {
                **self.request().model_dump(),
                "system": "Fictional system",
                "temperature": 0.5,
            }
        )
        prepared = client.prepare(request, idempotency_key="same-act")
        self.assertEqual(prepared.arguments["temperature"], 0.5)
        self.assertEqual(prepared.arguments["system"], "Fictional system")
        self.assertEqual(
            set(prepared.arguments),
            {"model", "messages", "max_output_tokens", "system", "temperature"},
        )

    def test_catalogue_output_ceiling_is_model_specific(self) -> None:
        client = DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=self.catalogue(),
            connection_id="synthetic-connection",
            expected_binding=HostedExpectedBinding(
                external_identity="synthetic-project", connection_revision="1"
            ),
        )
        request = self.request().model_copy(update={"max_output_tokens": 129})
        with self.assertRaisesRegex(HostedClientError, "reduce max_output_tokens"):
            client.prepare(request, idempotency_key="same-act")

    def test_json_escaping_and_structure_count_towards_byte_limit(self) -> None:
        # Raw text fits256KiB, but escaped JSON string bytes do not.
        with self.assertRaises(ValidationError):
            TextCompletionRequest(
                model="synthetic-snapshot-20261013",
                messages=(ModelMessage(role="user", text="\x01" * 50_000),),
                max_output_tokens=64,
            )


def test_output_tokens_fit_canonical_json() -> None:
    for model_type in (TextCompletionRequest, CataloguedModel):
        values: dict[str, object] = {
            "model": "synthetic-model",
            "max_output_tokens": 1 << 53,
        }
        if model_type is TextCompletionRequest:
            values["messages"] = [{"role": "user", "text": "Synthetic."}]
        with pytest.raises(ValidationError):
            model_type.model_validate(values)


@pytest.mark.parametrize("model_type", [TextCompletionRequest, CataloguedModel])
def test_output_tokens_accept_canonical_json_upper_bound(
    model_type: type[TextCompletionRequest] | type[CataloguedModel],
) -> None:
    values: dict[str, object] = {
        "model": "synthetic-model",
        "max_output_tokens": (1 << 53) - 1,
    }
    if model_type is TextCompletionRequest:
        values["messages"] = [{"role": "user", "text": "Synthetic."}]
    assert model_type.model_validate(values).max_output_tokens == (1 << 53) - 1


def test_configuration_is_validated_before_preparation() -> None:
    catalogue = ModelCatalogue(
        provider="anthropic",
        models=(CataloguedModel(model="synthetic", max_output_tokens=10),),
        connector_revision="draft-test",
        proposed_contract_sha256="a" * 64,
    )
    binding = HostedExpectedBinding(
        external_identity="synthetic", connection_revision="1"
    )
    with pytest.raises(HostedClientError, match="fresh connection binding"):
        DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=catalogue,
            connection_id="",
            expected_binding=binding,
        )
    invalid = binding.model_copy(update={"connection_revision": "0"})
    with pytest.raises(HostedClientError, match="fresh connection binding"):
        DraftModelOperationClient(
            connection_output_token_limit=128,
            catalogue=catalogue,
            connection_id="synthetic",
            expected_binding=invalid,
        )


def test_caller_retains_rfc8785_bytes_and_digest() -> None:
    request = TextCompletionRequest(
        model="synthetic",
        messages=(ModelMessage(role="user", text="é"),),
        max_output_tokens=10,
        temperature=1.0,
    )
    expected = (
        '{"max_output_tokens":10,"messages":[{"role":"user","text":"é"}],'
        '"model":"synthetic","temperature":1}'
    ).encode()
    assert request.canonical_bytes() == expected
    assert request.request_sha256() == "sha256:" + hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("temperature", [0.001, 0.555])
def test_temperature_precision_is_limited(temperature: float) -> None:
    with pytest.raises(ValidationError):
        TextCompletionRequest(
            model="synthetic",
            messages=(ModelMessage(role="user", text="test"),),
            max_output_tokens=1,
            temperature=temperature,
        )


def test_connection_ceiling_and_temperature_catalogue_flag() -> None:
    catalogue = ModelCatalogue(
        provider="anthropic",
        models=(CataloguedModel(model="synthetic", max_output_tokens=10),),
        connector_revision="draft-test",
        proposed_contract_sha256="a" * 64,
    )
    client = DraftModelOperationClient(
        catalogue=catalogue,
        connection_id="synthetic",
        connection_output_token_limit=5,
        expected_binding=HostedExpectedBinding(
            external_identity="synthetic", connection_revision="1"
        ),
    )
    request = TextCompletionRequest(
        model="synthetic",
        messages=(ModelMessage(role="user", text="test"),),
        max_output_tokens=6,
    )
    with pytest.raises(HostedClientError, match="reduce max_output_tokens"):
        client.prepare(request, idempotency_key="synthetic")
    request = request.model_copy(update={"max_output_tokens": 5, "temperature": 0.5})
    with pytest.raises(HostedClientError, match="omit temperature"):
        client.prepare(request, idempotency_key="synthetic")


@pytest.mark.parametrize(
    "filename,digest",
    [
        (
            "request-vectors-base.json",
            "0f4c199a873e0282779edb50669ce5f4470789c7d41349d75bc2d8a9a9a4a85a",
        ),
        (
            "request-vectors-extra.json",
            "1af60fe28860848bc5f24427face7ee749c5c7ba222ab4779e2053b7f6de1f98",
        ),
    ],
)
def test_owner_request_vectors_match_canonical_bytes_and_refusals(
    filename: str, digest: str
) -> None:
    path = Path(__file__).parent / "fixtures" / filename
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    vectors = json.loads(raw)
    for item in vectors["valid"]:
        request = parse_model_request_json(item.get("raw", json.dumps(item["request"])))
        assert request.canonical_bytes().decode() == item["canonical"], item["name"]
        assert len(request.canonical_bytes()) == item["request_size_bytes"], item[
            "name"
        ]
        assert request.request_sha256() == item["request_sha256"], item["name"]
    for item in vectors["refused"]:
        text = item["raw"]
        rejected = bytes.fromhex(text[4:]) if text.startswith("hex:") else text
        if item.get("expected") == "model_not_allowed":
            request = parse_model_request_json(rejected)
            catalogue = ModelCatalogue(
                provider="anthropic",
                models=(CataloguedModel(model="synthetic", max_output_tokens=128),),
                connector_revision="synthetic",
                proposed_contract_sha256="a" * 64,
            )
            client = DraftModelOperationClient(
                catalogue=catalogue,
                connection_id="synthetic",
                connection_output_token_limit=128,
                expected_binding=HostedExpectedBinding(
                    external_identity="synthetic", connection_revision="1"
                ),
            )
            with pytest.raises(HostedClientError):
                client.prepare(request, idempotency_key="synthetic")
        else:
            with pytest.raises(HostedClientError):
                parse_model_request_json(rejected)


@pytest.mark.parametrize(
    "raw",
    [
        '\ufeff{"model":"m","messages":[{"role":"user","text":"x"}],"max_output_tokens":1}',
        '{"model":"m","messages":[{"role":"user","text":"x"}],'
        '"max_output_tokens":1} extra',
        "[" * 2000 + "0" + "]" * 2000,
    ],
)
def test_raw_json_rejects_bom_trailing_data_and_nesting(raw: str) -> None:
    with pytest.raises(HostedClientError):
        parse_model_request_json(raw)


def test_oversized_string_is_refused_before_encoding() -> None:
    class MustNotEncode(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("encoding must not run")

    with pytest.raises(HostedClientError):
        parse_model_request_json(MustNotEncode("x" * (512 * 1024 + 1)))


def test_negative_zero_temperature_is_valid_and_canonicalises_to_zero() -> None:
    raw = (
        '{"model":"m","messages":[{"role":"user","text":"x"}],'
        '"max_output_tokens":1,"temperature":-0.0}'
    )
    request = parse_model_request_json(raw)
    assert request.temperature == 0.0
    assert b'"temperature":0' in request.canonical_bytes()


@pytest.mark.parametrize("codepoint", range(0x1C, 0x20))
def test_python_extra_whitespace_is_valid_message_text(codepoint: int) -> None:
    assert ModelMessage(role="user", text=chr(codepoint)).text == chr(codepoint)


@pytest.mark.parametrize("text", ["\u0085\u00a0", "\u2000\u200a\u3000", "\u0009\u2028"])
def test_contract_unicode_whitespace_only_text_is_refused(text: str) -> None:
    with pytest.raises(ValidationError):
        ModelMessage(role="user", text=text)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"synthetic","messages":[{"role":"user","text":"synthetic-private"}],"max_output_tokens":1,"temperature":1e-2}',
        b'{"model":"synthetic","messages":[{"role":"user","text":"synthetic-private"}],"max_output_tokens":1,"temperature":NaN}',
        b"\xff",
        b" " * (512 * 1024 + 1),
    ],
)
def test_raw_json_refusal_does_not_echo_input(raw: bytes) -> None:
    with pytest.raises(HostedClientError) as error:
        parse_model_request_json(raw)
    assert "synthetic-private" not in str(error.value)


@pytest.mark.parametrize("field", ["system", "temperature"])
def test_explicit_null_optional_request_is_not_silently_dropped(field: str) -> None:
    values = {
        "model": "synthetic",
        "messages": [{"role": "user", "text": "test"}],
        "max_output_tokens": 1,
        field: None,
    }
    with pytest.raises(ValidationError):
        TextCompletionRequest.model_validate(values)
