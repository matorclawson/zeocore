"""The named-file grant store for a product's service user (operator, 2026-10-12)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from zeo_core.integrations.hosted.file_store import FileSecureSessionStore
from zeo_core.integrations.hosted.pairing import DeviceSession, SecureStoreError

NOW = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)


def _session() -> DeviceSession:
    return DeviceSession(
        device_id="dev_newsroom_intake",
        access_token=SecretStr("access-canary"),
        refresh_token=SecretStr("refresh-canary"),
        access_expires_at=NOW + timedelta(minutes=15),
        refresh_expires_at=NOW + timedelta(days=30),
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    directory = Path(os.path.realpath(tmp_path)) / "zeocore"
    directory.mkdir(mode=0o700)
    return directory


def test_a_grant_round_trips_through_its_named_file(home: Path) -> None:
    store = FileSecureSessionStore(home / "grant.json")
    assert store.load() is None
    store.save(_session())
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token.get_secret_value() == "refresh-canary"
    mode = stat.S_IMODE(os.stat(home / "grant.json").st_mode)
    assert mode == 0o600
    assert [p.name for p in home.iterdir()] == ["grant.json"]  # no temporary left
    store.delete()
    assert store.load() is None


def test_saving_again_replaces_the_grant(home: Path) -> None:
    store = FileSecureSessionStore(home / "grant.json")
    store.save(_session())
    rotated = _session().model_copy(update={"access_token": SecretStr("rotated")})
    store.save(rotated)
    loaded = store.load()
    assert loaded is not None and loaded.access_token.get_secret_value() == "rotated"


def test_a_grant_is_never_sent_to_another_broker(home: Path) -> None:
    FileSecureSessionStore(home / "grant.json").save(_session())
    other = FileSecureSessionStore(home / "grant.json", origin="https://dev.example")
    with pytest.raises(SecureStoreError, match="another Broker"):
        other.load()


def test_the_file_never_holds_a_token_in_its_name_or_mode(home: Path) -> None:
    FileSecureSessionStore(home / "grant.json").save(_session())
    payload = json.loads((home / "grant.json").read_text())
    assert payload["format"] == 1 and payload["origin"] == "production"


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o777])
def test_a_directory_open_to_others_is_refused(home: Path, mode: int) -> None:
    home.chmod(mode)
    with pytest.raises(SecureStoreError, match="no group or other access"):
        FileSecureSessionStore(home / "grant.json").load()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666])
def test_a_file_open_to_others_is_refused(home: Path, mode: int) -> None:
    store = FileSecureSessionStore(home / "grant.json")
    store.save(_session())
    (home / "grant.json").chmod(mode)
    with pytest.raises(SecureStoreError, match="no group or other access"):
        store.load()


def test_a_symlinked_file_is_refused(home: Path) -> None:
    real = home / "real.json"
    FileSecureSessionStore(real).save(_session())
    (home / "grant.json").symlink_to(real)
    with pytest.raises(SecureStoreError, match="can't be opened"):
        FileSecureSessionStore(home / "grant.json").load()


def test_a_symlinked_directory_is_refused(home: Path, tmp_path: Path) -> None:
    link = Path(os.path.realpath(tmp_path)) / "link"
    link.symlink_to(home)
    with pytest.raises(SecureStoreError, match="symbolic link"):
        FileSecureSessionStore(link / "grant.json").load()


def test_another_users_directory_or_file_is_refused(home: Path) -> None:
    FileSecureSessionStore(home / "grant.json").save(_session())
    stranger = FileSecureSessionStore(home / "grant.json", uid=os.getuid() + 4242)
    with pytest.raises(SecureStoreError, match="own directory"):
        stranger.load()


def test_a_writable_ancestor_is_refused_unless_sticky(tmp_path: Path) -> None:
    shared = Path(os.path.realpath(tmp_path)) / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    home = shared / "zeocore"
    home.mkdir(mode=0o700)
    with pytest.raises(SecureStoreError, match="writable by others"):
        FileSecureSessionStore(home / "grant.json").load()
    shared.chmod(0o1777)  # like /tmp: sticky, so others can't replace our entry
    assert FileSecureSessionStore(home / "grant.json").load() is None


@pytest.mark.parametrize("path", ["grant.json", "relative/grant.json"])
def test_only_an_absolute_path_is_accepted(path: str) -> None:
    with pytest.raises(SecureStoreError, match="absolute path"):
        FileSecureSessionStore(path).load()


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    missing = Path(os.path.realpath(tmp_path)) / "absent" / "grant.json"
    with pytest.raises(SecureStoreError, match="missing"):
        FileSecureSessionStore(missing).save(_session())


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[]", b'{"format": 2}', json.dumps({"format": 1}).encode()],
)
def test_an_invalid_file_is_refused(home: Path, content: bytes) -> None:
    target = home / "grant.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(descriptor, content)
    os.close(descriptor)
    with pytest.raises(SecureStoreError, match="invalid"):
        FileSecureSessionStore(target).load()


def test_an_oversized_file_is_refused(home: Path) -> None:
    target = home / "grant.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(descriptor, b"x" * (64 * 1024 + 1))
    os.close(descriptor)
    with pytest.raises(SecureStoreError, match="too large"):
        FileSecureSessionStore(target).load()


def test_a_directory_in_place_of_the_file_is_refused(home: Path) -> None:
    (home / "grant.json").mkdir(mode=0o700)
    with pytest.raises(SecureStoreError):
        FileSecureSessionStore(home / "grant.json").load()


def test_a_failed_write_leaves_no_temporary(home: Path) -> None:
    (home / "grant.json").mkdir(mode=0o700)  # the rename onto it fails
    with pytest.raises(SecureStoreError, match="can't be written"):
        FileSecureSessionStore(home / "grant.json").save(_session())
    assert sorted(p.name for p in home.iterdir()) == ["grant.json"]


def test_a_short_write_is_completed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zeo_core.integrations.hosted import file_store

    real_write = os.write
    calls: list[int] = []

    def short(descriptor: int, data: bytes | memoryview) -> int:
        chunk = bytes(data)[:7]  # never more than 7 bytes at a time
        calls.append(len(chunk))
        return real_write(descriptor, chunk)

    monkeypatch.setattr(file_store.os, "write", short)
    store = FileSecureSessionStore(home / "grant.json")
    store.save(_session())
    monkeypatch.setattr(file_store.os, "write", real_write)
    loaded = store.load()
    assert loaded is not None and loaded.device_id == "dev_newsroom_intake"
    assert len(calls) > 1


def test_a_write_that_makes_no_progress_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zeo_core.integrations.hosted import file_store

    monkeypatch.setattr(file_store.os, "write", lambda descriptor, data: 0)
    with pytest.raises(SecureStoreError, match="can't be written"):
        FileSecureSessionStore(home / "grant.json").save(_session())
    assert list(home.iterdir()) == []
