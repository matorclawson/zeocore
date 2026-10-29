"""Where the bytes of a publish job come from: a local file, or a staging object store.

The executor and the resumable transfer ask a ``MediaSource`` three things: the size,
whether it is still the thing the job recorded, and a range of bytes. They don't care
whether the file is on this disk or in the studio's staging bucket.

* ``FileIdentity`` (``transfer.py``) is the local file: size and modification time.
* ``ObjectSource`` is one object version in a staging store. It reads through an
  injected ``ObjectReader``. This package holds no store client, address or credential:
  the caller supplies the reader, and a job names only a store *label*, an object key
  and the object's version.

A source is "unchanged" while the object's version and size are the ones the job
recorded. The sha256 is checked by the executor by reading the whole source once; an
object may also carry the sha256 the studio stored as metadata, and a mismatch with the
job is refused before any byte is read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

HASH_BLOCK_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ObjectStat:
    """What a store reports about one object version."""

    size_bytes: int
    version: str
    #: The sha256 the studio stored as object metadata, when the store keeps it.
    sha256: str | None = None


class ObjectReader(Protocol):
    """Range reads from a staging store; injected, never constructed here."""

    def stat(self, store: str, key: str, version: str) -> ObjectStat | None:
        """The object version, or ``None`` when it is gone."""
        ...

    def read(
        self, store: str, key: str, version: str, offset: int, length: int
    ) -> bytes:
        """Up to ``length`` bytes from ``offset``; fewer only at the end."""
        ...


@runtime_checkable
class MediaSource(Protocol):
    """The read-only view of one file that a transfer and a hash check need."""

    @property
    def size_bytes(self) -> int: ...

    def matches(self) -> bool:
        """``True`` while the source is still what the job recorded."""
        ...

    def read_range(self, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes at ``offset``; fewer means the source changed."""
        ...

    def sha256(self) -> str:
        """The hex digest of the whole source, read once."""
        ...


def digest_of(source: MediaSource) -> str:
    """Stream ``source`` in blocks and return its sha256 (hex)."""
    digest = hashlib.sha256()
    offset = 0
    while offset < source.size_bytes:
        length = min(HASH_BLOCK_BYTES, source.size_bytes - offset)
        block = source.read_range(offset, length)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ObjectSource:
    """One staging-store object version, read through an ``ObjectReader``."""

    reader: ObjectReader
    store: str
    key: str
    version: str
    size_bytes: int

    def matches(self) -> bool:
        found = self.reader.stat(self.store, self.key, self.version)
        return (
            found is not None
            and found.version == self.version
            and found.size_bytes == self.size_bytes
        )

    def metadata_sha256(self) -> str | None:
        found = self.reader.stat(self.store, self.key, self.version)
        return None if found is None else found.sha256

    def read_range(self, offset: int, length: int) -> bytes:
        return self.reader.read(self.store, self.key, self.version, offset, length)

    def sha256(self) -> str:
        return digest_of(self)


__all__ = [
    "HASH_BLOCK_BYTES",
    "MediaSource",
    "ObjectReader",
    "ObjectSource",
    "ObjectStat",
    "digest_of",
]
