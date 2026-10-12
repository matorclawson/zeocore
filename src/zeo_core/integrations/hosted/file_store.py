"""A product service user's device grant in one named file (operator, 2026-10-12).

The operator's ruling: a product's service user (``zs-<product>``) keeps its
ZEOconnect grant in a named file, 0600, in a 0700 directory owned by that
user. It is **opt-in, never a fallback**: it is used only when
``ZEOCORE_SESSION_FILE`` names it, and a Keychain that can't be read stays a
refusal (client-access proposal §2 and §9, option A). ZEO-RT qualifies it on
the real account.

Every load, save and delete checks, and refuses with ``SecureStoreError``
otherwise:
- the path is absolute, with no symbolic link anywhere in it;
- the directory is a directory owned by this user, with no group or other
  access (0700);
- no directory above it is writable by another user, unless it is sticky,
  and each is owned by this user or root;
- the file, checked on the open descriptor so it can't be swapped in between,
  is a regular file owned by this user with no group or other access.

Writes go to a fresh 0600 file in the same directory, then an atomic rename,
then the directory is flushed. The file records the Broker origin it was
paired with, so a session is never sent to another Broker.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from zeo_core.integrations.hosted.pairing import DeviceSession, SecureStoreError

SESSION_FILE_ENV: Final = "ZEOCORE_SESSION_FILE"
_PRODUCTION: Final = "production"
_MAX_BYTES: Final = 64 * 1024
_FORMAT: Final = 1


class FileSecureSessionStore:
    def __init__(
        self, path: str | Path, *, origin: str | None = None, uid: int | None = None
    ) -> None:
        self._path = Path(path)
        self._origin = origin or _PRODUCTION
        self._uid = os.getuid() if uid is None else uid

    # -- the checks ------------------------------------------------------------

    def _check_directory(self) -> None:
        path = self._path
        if not path.is_absolute() or path.name in ("", ".", ".."):
            raise SecureStoreError("the session file must be an absolute path")
        directory = path.parent
        if os.path.realpath(directory) != str(directory):
            raise SecureStoreError("the session file's path has a symbolic link")
        try:
            info = os.lstat(directory)
        except OSError:
            raise SecureStoreError("the session file's directory is missing") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self._uid:
            raise SecureStoreError(
                "the session file's directory must be this user's own directory"
            )
        if info.st_mode & 0o077:
            raise SecureStoreError(
                "the session file's directory must allow no group or other access"
            )
        for ancestor in directory.parents:
            info = os.lstat(ancestor)
            if info.st_uid not in (0, self._uid):
                raise SecureStoreError(
                    "a directory above the session file belongs to another user"
                )
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise SecureStoreError(
                    "a directory above the session file is writable by others"
                )

    def _check_file(self, descriptor: int) -> None:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid:
            raise SecureStoreError("the session file must be this user's own file")
        if info.st_mode & 0o077:
            raise SecureStoreError(
                "the session file must allow no group or other access"
            )
        if info.st_size > _MAX_BYTES:
            raise SecureStoreError("the session file is too large")

    # -- the store -------------------------------------------------------------

    def load(self) -> DeviceSession | None:
        self._check_directory()
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            raise SecureStoreError("the session file can't be opened") from None
        try:
            # Checked on the descriptor itself, before anything wraps it, so a
            # directory or a swapped file is refused rather than read.
            self._check_file(descriptor)
        except SecureStoreError:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "rb") as handle:
            material = handle.read(_MAX_BYTES + 1)
        try:
            payload = json.loads(material)
            if payload["format"] != _FORMAT:
                raise ValueError
            origin = payload["origin"]
            session = DeviceSession(
                device_id=payload["device_id"],
                access_token=SecretStr(payload["access_token"]),
                refresh_token=SecretStr(payload["refresh_token"]),
                access_expires_at=payload["access_expires_at"],
                refresh_expires_at=payload["refresh_expires_at"],
            )
        except Exception:
            raise SecureStoreError("the session file is invalid") from None
        finally:
            del material
        if origin != self._origin:
            raise SecureStoreError("the session file was paired with another Broker")
        return session

    def save(self, session: DeviceSession) -> None:
        self._check_directory()
        material = json.dumps(
            {
                "format": _FORMAT,
                "origin": self._origin,
                "device_id": session.device_id,
                "access_token": session.access_token.get_secret_value(),
                "refresh_token": session.refresh_token.get_secret_value(),
                "access_expires_at": session.access_expires_at.isoformat(),
                "refresh_expires_at": session.refresh_expires_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        temporary = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.write(descriptor, material)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._path)
            self._flush_directory()
        except OSError:
            temporary.unlink(missing_ok=True)
            raise SecureStoreError("the session file can't be written") from None
        finally:
            del material

    def delete(self) -> None:
        self._check_directory()
        try:
            self._path.unlink(missing_ok=True)
            self._flush_directory()
        except OSError:
            raise SecureStoreError("the session file can't be removed") from None

    def _flush_directory(self) -> None:
        descriptor = os.open(self._path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
