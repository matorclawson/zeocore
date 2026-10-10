"""sign and verify against SOW-19 §3, SOW-20 §4, SOW-21 §2 and SOW-22 §2."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from zeo_core.release.signing import (
    DISTRIBUTED,
    INTERNAL,
    Inspection,
    JournalHeldError,
    KeychainState,
    LoadedManifest,
    SigningJournal,
    WorkDir,
    sign,
    verify,
)

from .conftest import HOST, VIRTUALIZATION, FakeToolchain, signed_as

Release = Callable[..., LoadedManifest]


@pytest.fixture
def work(tmp_path: Path) -> WorkDir:
    return WorkDir(tmp_path / "work")


def test_signs_a_separate_candidate_and_verify_passes(
    release: Release, work: WorkDir
) -> None:
    loaded = release(
        {"name": "pomar"}, {"name": "pomar-host", "entitlements": VIRTUALIZATION}
    )
    tools = FakeToolchain()

    signed = sign(loaded, work, tools, HOST)

    assert (signed.status, signed.reason) == ("done", "signed")
    assert tools.sign_calls == ["ac.zeo.pomar.pomar", "ac.zeo.pomar.pomar-host"]
    receipt = signed.receipts[1]
    assert receipt["certificate_sha1"] == INTERNAL.certificate_sha1
    assert receipt["signed_sha256"] != receipt["unsigned_sha256"]
    assert receipt["designated_requirement"] == (
        'identifier "ac.zeo.pomar.pomar-host" and certificate root = '
        'H"093e02c58091461dddc1320ec7f163b9854590b9"'
    )
    # The unsigned original is kept as it was.
    assert (loaded.root / "bin" / "pomar").read_bytes() == b"binary pomar"

    checked = verify(loaded, work, tools)
    assert (checked.status, checked.reason) == ("done", "verified")
    assert {r["step"] for r in checked.receipts} == {"verify"}


def test_signing_again_returns_the_receipts_without_a_second_signature(
    release: Release, work: WorkDir
) -> None:
    loaded = release()
    tools = FakeToolchain()
    first = sign(loaded, work, tools, HOST)
    again = sign(loaded, work, tools, HOST)
    assert again.status == "done"
    assert again.receipts == first.receipts
    assert tools.sign_calls == ["ac.zeo.pomar.pomar"]


@pytest.mark.parametrize(
    ("configured", "reason"),
    [(None, "signing_host_unconfigured"), ("eng-uk", "signing_host_mismatch")],
)
def test_sign_holds_off_the_configured_host(
    release: Release, work: WorkDir, configured: str | None, reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(release(), work, tools, configured)
    assert (outcome.status, outcome.reason) == ("held", reason)
    assert tools.sign_calls == []
    assert not work.root.exists()


def test_the_configured_host_is_matched_without_case(
    release: Release, work: WorkDir
) -> None:
    assert sign(release(), work, FakeToolchain(), "MatorMini").status == "done"


@pytest.mark.parametrize(
    ("spec", "status", "reason"),
    [
        ({"platform": "linux"}, "refused", "platform_signature_unadopted"),
        (
            {
                "policy": "legacy-runtime-installed",
                "identifier": "org.zeroemployee.zeo",
            },
            "refused",
            "legacy_policy_verify_only",
        ),
        ({"audience": "distributed"}, "held", "notarisation_unbuilt"),
    ],
)
def test_sign_refuses_what_is_not_built_before_any_signer(
    release: Release, work: WorkDir, spec: dict[str, str], status: str, reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(release({"name": "pomar", **spec}), work, tools, HOST)
    assert (outcome.status, outcome.reason) == (status, reason)
    assert tools.sign_calls == []


def test_a_tampered_input_is_refused_before_signing(
    release: Release, work: WorkDir
) -> None:
    loaded = release(
        {"name": "pomar"}, {"name": "pomar-host", "entitlements": VIRTUALIZATION}
    )
    (loaded.root / "pomar-host.entitlements").write_bytes(b"changed")
    tools = FakeToolchain()
    outcome = sign(loaded, work, tools, HOST)
    assert (outcome.reason, outcome.executable) == ("input_mismatch", "pomar-host")
    # Refused as a whole: the first executable was not signed either.
    assert tools.sign_calls == []


def test_a_locked_keychain_waits_with_the_plain_message(
    release: Release, work: WorkDir
) -> None:
    tools = FakeToolchain(keychain=KeychainState.LOCKED)
    outcome = sign(release(), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("waiting", "signing_keychain_locked")
    assert outcome.message == "unlock the login keychain"
    assert tools.sign_calls == []


def test_an_unknown_keychain_state_is_never_reported_as_locked(
    release: Release, work: WorkDir
) -> None:
    tools = FakeToolchain(keychain=KeychainState.UNKNOWN, sign_cause="failed_unknown")
    outcome = sign(release(), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("held", "signing_failed_unknown")


@pytest.mark.parametrize(
    ("tools", "reason"),
    [
        (
            FakeToolchain(available=frozenset({DISTRIBUTED.certificate_sha1})),
            "signing_identity_missing",
        ),
        (FakeToolchain(identities_fail=True), "signing_failed_unknown"),
        (FakeToolchain(sign_cause="identity_missing"), "signing_identity_missing"),
        (FakeToolchain(sign_cause="failed_unknown"), "signing_failed_unknown"),
    ],
)
def test_signing_failures_are_held_and_close_the_intent(
    release: Release, work: WorkDir, tools: FakeToolchain, reason: str
) -> None:
    loaded = release()
    outcome = sign(loaded, work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("held", reason)
    assert work.read_receipt("pomar") is None
    # A definite failure is not an open attempt: a later run may try again.
    retry = sign(loaded, work, FakeToolchain(), HOST)
    assert retry.status == "done"


def test_a_lost_answer_is_ambiguous_and_never_signed_again(
    release: Release, work: WorkDir
) -> None:
    loaded = release()
    lost = sign(loaded, work, FakeToolchain(lose_answer=True), HOST)
    assert (lost.status, lost.reason) == ("ambiguous", "signing_attempt_unreconciled")

    tools = FakeToolchain()
    again = sign(loaded, work, tools, HOST)
    assert again.status == "ambiguous"
    assert tools.sign_calls == []


def test_a_lost_answer_whose_candidate_verifies_is_reconciled(
    release: Release, work: WorkDir
) -> None:
    loaded = release()
    sign(loaded, work, FakeToolchain(lose_answer=True), HOST)
    tools = FakeToolchain()
    tools.inspections[work.candidate("pomar")] = signed_as("ac.zeo.pomar.pomar")

    outcome = sign(loaded, work, tools, HOST)

    assert outcome.status == "done"
    assert outcome.receipts[0]["reconciled"] is True
    assert tools.sign_calls == []


def test_a_bad_reconciliation_read_stays_ambiguous(
    release: Release, work: WorkDir
) -> None:
    from zeo_core.release.signing import ToolError

    loaded = release()
    sign(loaded, work, FakeToolchain(lose_answer=True), HOST)

    class Unreadable(FakeToolchain):
        def inspect(self, path: Path) -> Inspection:
            raise ToolError("unreadable")

    assert sign(loaded, work, Unreadable(), HOST).status == "ambiguous"


def _forged(**changes: object) -> Callable[[str, Path | None], object]:
    return lambda identifier, _entitlements: signed_as(identifier, **changes)


@pytest.mark.parametrize(
    ("after_sign", "reason"),
    [
        # An ad-hoc signature, even one whose requirement names our pin.
        (_forged(leaf_sha1=None), "not_signed"),
        (_forged(designated='cdhash H"2abf"', designated_explicit=False), "not_signed"),
        (_forged(designated='identifier "ac.zeo.pomar.pomar"'), "not_signed"),
        (_forged(signed=False), "not_signed"),
        (_forged(identifier="ac.zeo.pomar.other"), "identity_mismatch"),
        (_forged(leaf_sha1="AB" * 20), "identity_mismatch"),
        (
            lambda identifier, _e: signed_as(identifier, DISTRIBUTED),
            "wrong_audience",
        ),
        (_forged(entitlements=VIRTUALIZATION), "entitlements_mismatch"),
    ],
)
def test_a_signed_candidate_that_isnt_the_binding_is_refused(
    release: Release, work: WorkDir, after_sign: Callable[..., object], reason: str
) -> None:
    tools = FakeToolchain(after_sign=after_sign)  # type: ignore[arg-type]
    outcome = sign(release(), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert work.read_receipt("pomar") is None


def test_a_requirement_codesign_rejects_is_not_signed(
    release: Release, work: WorkDir
) -> None:
    outcome = sign(release(), work, FakeToolchain(satisfied=False), HOST)
    assert outcome.reason == "not_signed"


def test_dropped_entitlements_are_refused(release: Release, work: WorkDir) -> None:
    tools = FakeToolchain(after_sign=_forged(entitlements=None))  # type: ignore[arg-type]
    loaded = release({"name": "pomar-host", "entitlements": VIRTUALIZATION})
    assert sign(loaded, work, tools, HOST).reason == "entitlements_mismatch"


def test_a_receipt_from_another_manifest_is_refused(
    release: Release, work: WorkDir
) -> None:
    sign(release(), work, FakeToolchain(), HOST)
    other = release({"name": "pomar", "content": b"rebuilt"})
    assert sign(other, work, FakeToolchain(), HOST).reason == "receipt_mismatch"
    assert verify(other, work, FakeToolchain()).reason == "receipt_mismatch"


def test_a_held_journal_refuses_a_second_run(release: Release, work: WorkDir) -> None:
    work.prepare()
    holder = SigningJournal(work.root / "journal.jsonl")
    try:
        with pytest.raises(JournalHeldError):
            sign(release(), work, FakeToolchain(), HOST)
    finally:
        holder.close()


# -- verify ----------------------------------------------------------------------


def _signed(
    release: Release, work: WorkDir, *specs: dict[str, object]
) -> tuple[LoadedManifest, FakeToolchain]:
    loaded = release(*specs)
    tools = FakeToolchain()
    assert sign(loaded, work, tools, HOST).status == "done"
    return loaded, tools


def test_verify_runs_on_any_host(release: Release, work: WorkDir) -> None:
    loaded, tools = _signed(release, work)
    assert verify(loaded, work, replace(tools, host_name="eng-uk")).status == "done"


def test_verify_refuses_an_unsigned_release(release: Release, work: WorkDir) -> None:
    assert verify(release(), work, FakeToolchain()).reason == "not_signed"


def test_verify_refuses_a_missing_companion(release: Release, work: WorkDir) -> None:
    loaded, tools = _signed(release, work, {"name": "pomar"}, {"name": "pomar-host"})
    work.receipt_path("pomar-host").unlink()
    assert verify(loaded, work, tools).reason == "companion_unsigned"


def test_verify_refuses_a_changed_signed_file(release: Release, work: WorkDir) -> None:
    loaded, tools = _signed(release, work)
    work.candidate("pomar").write_bytes(b"swapped")
    assert verify(loaded, work, tools).reason == "signed_digest_mismatch"


def test_verify_refuses_a_changed_original(release: Release, work: WorkDir) -> None:
    loaded, tools = _signed(release, work)
    (loaded.root / "bin" / "pomar").write_bytes(b"swapped")
    assert verify(loaded, work, tools).reason == "input_mismatch"


def test_verify_refuses_a_signature_that_changed(
    release: Release, work: WorkDir
) -> None:
    loaded, tools = _signed(release, work)
    tools.inspections[work.candidate("pomar")] = signed_as(
        "ac.zeo.pomar.pomar", leaf_sha1=None
    )
    assert verify(loaded, work, tools).reason == "not_signed"


def test_verify_holds_when_the_signature_is_unreadable(
    release: Release, work: WorkDir
) -> None:
    from zeo_core.release.signing import ToolError

    loaded, tools = _signed(release, work)

    class Unreadable(FakeToolchain):
        def inspect(self, path: Path) -> Inspection:
            raise ToolError("unreadable")

    outcome = verify(loaded, work, Unreadable())
    assert (outcome.status, outcome.reason) == ("held", "verify_unreadable")


@pytest.mark.parametrize(
    ("spec", "status", "reason"),
    [
        ({"platform": "linux"}, "refused", "platform_signature_unadopted"),
        (
            {
                "policy": "legacy-releasesupport-public",
                "identifier": "com.zeroemployeeorg.zero-employee.zeo",
            },
            "held",
            "legacy_policy_unpinned",
        ),
        ({"audience": "distributed"}, "held", "notarisation_unbuilt"),
    ],
)
def test_verify_holds_what_is_not_built(
    release: Release, work: WorkDir, spec: dict[str, str], status: str, reason: str
) -> None:
    outcome = verify(release({"name": "zeo", **spec}), work, FakeToolchain())
    assert (outcome.status, outcome.reason) == (status, reason)


def test_receipts_are_written_once(release: Release, work: WorkDir) -> None:
    loaded, _ = _signed(release, work)
    receipt = json.loads(work.receipt_path("pomar").read_text())
    with pytest.raises(FileExistsError):
        work.write_receipt("pomar", receipt)


def test_an_unreadable_journal_line_refuses_to_open(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"key": "k", "action": "guess"}\n')
    with pytest.raises(ValueError, match="line 1"):
        SigningJournal(path)


def test_the_journal_reopens_with_open_intents(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    path = tmp_path / "journal.jsonl"
    journal = SigningJournal(path)
    journal.append("a", "intent", datetime.now(UTC))
    journal.append("b", "intent", datetime.now(UTC))
    journal.append("b", "failed", datetime.now(UTC))
    journal.close()
    reopened = SigningJournal(path)
    assert reopened.is_open("a") and not reopened.is_open("b")
    reopened.close()


# -- corrupt records are classified, never an internal error -----------------------


def test_a_corrupt_receipt_is_refused_by_sign_and_verify(
    release: Release, work: WorkDir
) -> None:
    loaded, tools = _signed(release, work)
    for content in ("not json", "[1, 2]"):
        work.receipt_path("pomar").write_text(content)
        assert sign(loaded, work, tools, HOST).reason == "receipt_invalid"
        assert verify(loaded, work, tools).reason == "receipt_invalid"


def test_a_corrupt_companion_receipt_is_refused_by_verify(
    release: Release, work: WorkDir
) -> None:
    loaded, tools = _signed(release, work, {"name": "pomar"}, {"name": "pomar-host"})
    work.receipt_path("pomar").unlink()
    work.receipt_path("pomar-host").write_text("not json")
    assert verify(loaded, work, tools).reason == "receipt_invalid"


def test_a_reviewed_entitlements_file_that_isnt_a_plist_is_refused(
    release: Release, work: WorkDir, tmp_path: Path
) -> None:
    import hashlib

    import yaml

    loaded = release({"name": "pomar-host", "entitlements": VIRTUALIZATION})
    bad = b"not a property list"
    (loaded.root / "pomar-host.entitlements").write_bytes(bad)
    document = yaml.safe_load(loaded.manifest_path.read_text())
    entry = document["signing"]["executables"][0]
    entry["entitlements"]["sha256"] = hashlib.sha256(bad).hexdigest()
    loaded.manifest_path.write_text(yaml.safe_dump(document))
    from zeo_core.release.signing import load

    relabelled = load(loaded.manifest_path)
    tools = FakeToolchain()
    outcome = sign(relabelled, work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", "entitlements_invalid")
    assert tools.sign_calls == []


def test_an_unreadable_journal_holds_signing(release: Release, work: WorkDir) -> None:
    work.prepare()
    (work.root / "journal.jsonl").write_text("not json\n")
    tools = FakeToolchain()
    outcome = sign(release(), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("held", "journal_unreadable")
    assert tools.sign_calls == []


def test_a_requirement_tool_error_holds_rather_than_refuses(
    release: Release, work: WorkDir
) -> None:
    from zeo_core.release.signing import ToolError

    class Erroring(FakeToolchain):
        def satisfies(self, path: Path, requirement: str) -> bool:
            raise ToolError("syntax")

    outcome = sign(release(), work, Erroring(), HOST)
    assert (outcome.status, outcome.reason) == ("held", "signing_failed_unknown")
