"""The Darwin adapter: parsing with a fake runner, and real codesign, ad-hoc only.

Ad-hoc signing needs no key, so these never touch a keychain item. The real
cases prove the step's central refusal on actual bytes: an ad-hoc signature
whose requirement names our pin still passes ``codesign --verify --strict``,
and the step must call it ``not_signed``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from zeo_core.release.signing import INTERNAL, DarwinToolchain, KeychainState, ToolError
from zeo_core.release.signing.identity import designated_requirement, parse_designated
from zeo_core.release.signing.step import _check_signature
from zeo_core.release.signing.toolchain import Completed, SignAnswerLostError

from .conftest import VIRTUALIZATION


class Runner:
    def __init__(self, *answers: Completed | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> Completed:
        self.calls.append(list(argv))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def done(code: int = 0, out: bytes = b"", err: bytes = b"") -> Completed:
    return Completed(code, out, err)


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        (done(0), KeychainState.UNLOCKED),
        (
            done(36, err=b"SecKeychainCopySettings: User interaction is not allowed."),
            KeychainState.LOCKED,
        ),
        (
            done(50, err=b"The specified keychain could not be found."),
            KeychainState.UNKNOWN,
        ),
        (OSError("no security"), KeychainState.UNKNOWN),
    ],
)
def test_keychain_state_is_locked_only_when_established(
    answer: Completed | BaseException, state: KeychainState
) -> None:
    assert DarwinToolchain(Runner(answer)).keychain_state() is state


def test_identities_are_read_as_pins() -> None:
    listing = (
        b'  1) 18518F308DBD22D522C9E89E627278C81D5BC754 "Developer ID Application"\n'
        b'  2) 093E02C58091461DDDC1320EC7F163B9854590B9 "ZEO Runtime Code Signing"\n'
        b"     2 valid identities found\n"
    )
    found = DarwinToolchain(Runner(done(out=listing))).identities()
    assert found == {
        INTERNAL.certificate_sha1,
        "18518F308DBD22D522C9E89E627278C81D5BC754",
    }
    with pytest.raises(ToolError):
        DarwinToolchain(Runner(done(1))).identities()


def _sign(runner: Runner) -> str:
    return DarwinToolchain(runner).sign(
        Path("/x/pomar"),
        identifier="ac.zeo.pomar.pomar",
        identity=INTERNAL,
        requirement="r",
        entitlements=Path("/x/e.plist"),
        hardened=True,
    )


@pytest.mark.parametrize(
    ("answer", "cause"),
    [
        (done(0), "signed"),
        (
            done(
                1, err=b"error: The specified item could not be found in the keychain."
            ),
            "identity_missing",
        ),
        (done(1, err=b"errSecInternalComponent"), "failed_unknown"),
        (done(1, err=b"User interaction is not allowed."), "failed_unknown"),
        (OSError("missing"), "failed_unknown"),
    ],
)
def test_sign_classifies_only_what_is_established(
    answer: Completed | BaseException, cause: str
) -> None:
    runner = Runner(answer)
    assert _sign(runner) == cause
    argv = runner.calls[0]
    assert argv[:4] == [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        INTERNAL.certificate_sha1,
    ]
    assert "=designated => r" in argv
    assert ["--options", "runtime", "--timestamp"] == argv[8:11]
    assert argv[-3:] == ["--entitlements", "/x/e.plist", "/x/pomar"]


def test_a_codesign_timeout_is_a_lost_answer() -> None:
    with pytest.raises(SignAnswerLostError):
        _sign(Runner(subprocess.TimeoutExpired("codesign", 300)))


def test_unreadable_entitlements_are_a_tool_error() -> None:
    runner = Runner(
        done(0, err=b"Identifier=ac.zeo.pomar.pomar\n"),
        done(0, out=b"designated => x"),
        done(1),  # certificate extraction wrote nothing
        done(0, out=b"not a plist"),
    )
    with pytest.raises(ToolError):
        DarwinToolchain(runner).inspect(Path("/x/pomar"))


def test_a_failing_read_is_a_tool_error() -> None:
    with pytest.raises(ToolError):
        DarwinToolchain(Runner(OSError("gone"))).satisfies(Path("/x"), "r")


def test_parse_designated_tells_implied_from_explicit() -> None:
    assert parse_designated('# designated => cdhash H"ab"') == ('cdhash H"ab"', False)
    assert parse_designated('Executable=/x\ndesignated => identifier  "a"') == (
        'identifier "a"',
        True,
    )
    assert parse_designated("nothing") is None


# -- real codesign, ad-hoc ---------------------------------------------------------

darwin = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("cc") is None,
    reason="needs codesign and cc",
)


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n")
    output = tmp_path / "pomar"
    compiler = shutil.which("cc")
    assert compiler is not None
    subprocess.run(  # noqa: S603 - test-only fixed argv
        [compiler, "-o", str(output), str(source)], check=True
    )
    return output


def _adhoc(path: Path, *extra: str) -> None:
    subprocess.run(  # noqa: S603 - test-only fixed argv, ad-hoc: no key
        ["/usr/bin/codesign", "--force", "-s", "-", *extra, str(path)],
        check=True,
        capture_output=True,
    )


@darwin
def test_real_linker_signature_is_not_signed(binary: Path) -> None:
    from zeo_core.release.signing.manifest import Executable

    item = Executable(
        name="pomar",
        path="bin/pomar",
        unsigned_sha256="0" * 64,
        identifier="ac.zeo.pomar.pomar",
        audience="internal",
        platform="darwin",
        entitlements="none",
    )
    seen = DarwinToolchain().inspect(binary)
    assert seen.signed and not seen.designated_explicit and seen.leaf_sha1 is None
    refusal, _ = _check_signature(binary, item, INTERNAL, None, DarwinToolchain())
    assert refusal == "not_signed"


@darwin
def test_real_adhoc_signature_naming_our_pin_is_not_signed(
    binary: Path, tmp_path: Path
) -> None:
    from zeo_core.release.signing.manifest import Executable

    plist = tmp_path / "e.plist"
    import plistlib

    plist.write_bytes(plistlib.dumps(VIRTUALIZATION))
    requirement = designated_requirement("ac.zeo.pomar.pomar", INTERNAL)
    _adhoc(
        binary,
        "-i",
        "ac.zeo.pomar.pomar",
        "--entitlements",
        str(plist),
        "--requirements",
        f"=designated => {requirement}",
    )
    tools = DarwinToolchain()
    seen = tools.inspect(binary)
    # Everything the requirement text says matches; only the certificate is absent.
    assert seen.identifier == "ac.zeo.pomar.pomar"
    assert seen.designated == requirement and seen.designated_explicit
    assert seen.entitlements == VIRTUALIZATION
    assert seen.leaf_sha1 is None
    item = Executable(
        name="pomar",
        path="bin/pomar",
        unsigned_sha256="0" * 64,
        identifier="ac.zeo.pomar.pomar",
        audience="internal",
        platform="darwin",
        entitlements={"path": "e.plist", "sha256": "0" * 64},
    )
    refusal, _ = _check_signature(binary, item, INTERNAL, VIRTUALIZATION, tools)
    assert refusal == "not_signed"


@darwin
def test_real_unsigned_file_is_not_signed(tmp_path: Path) -> None:
    script = tmp_path / "tool"
    script.write_text("#!/bin/sh\n")
    assert DarwinToolchain().inspect(script).signed is False
