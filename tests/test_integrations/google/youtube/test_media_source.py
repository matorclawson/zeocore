"""A job's bytes may come from a staging store as well as from a local file."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from zeo_core.integrations.google.youtube.job import JobDirectory, MediaFile, ObjectRef
from zeo_core.integrations.google.youtube.links import InMemoryUploadLinkStore
from zeo_core.integrations.google.youtube.media_source import (
    ObjectSource,
    ObjectStat,
    digest_of,
)
from zeo_core.integrations.google.youtube.publish import (
    EXIT_DONE,
    EXIT_HELD,
    PublishExecutor,
)
from zeo_core.integrations.google.youtube.transfer import (
    CHUNK_UNIT,
    ResumableTransfer,
    TransferState,
)

from .fakes import FakeBroker, FakeYouTube
from .helpers import make_job


class FakeObjectStore:
    """A staging store that keeps object versions in memory (an ``ObjectReader``)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], tuple[bytes, str | None]] = {}
        self.reads: list[tuple[int, int]] = []
        #: Called with the read number before each read; lets a test replace the object.
        self.before_read: Callable[[int], None] | None = None

    def put(
        self,
        store: str,
        key: str,
        version: str,
        data: bytes,
        *,
        sha256: str | None = None,
    ) -> None:
        self.objects[(store, key, version)] = (data, sha256)

    def stat(self, store: str, key: str, version: str) -> ObjectStat | None:
        found = self.objects.get((store, key, version))
        if found is None:
            return None
        return ObjectStat(len(found[0]), version, found[1])

    def read(
        self, store: str, key: str, version: str, offset: int, length: int
    ) -> bytes:
        if self.before_read is not None:
            self.before_read(len(self.reads))
        self.reads.append((offset, length))
        found = self.objects.get((store, key, version))
        if found is None:
            return b""
        return found[0][offset : offset + length]


def make_staged_job(
    root: Path, store: FakeObjectStore, *, store_sha256: bool = True
) -> Path:
    """A ``make_job`` job whose video is an object in ``store``, not a local file."""
    directory = make_job(root, authorize=False)
    raw = json.loads((directory / "job.json").read_text())
    local = Path(raw["video"]["path"])
    data = local.read_bytes()
    key = "episodes/take-1.mp4"
    store.put(
        "staging",
        key,
        "v1",
        data,
        sha256=raw["video"]["sha256"] if store_sha256 else None,
    )
    local.unlink()  # nothing may read the local copy
    raw["video"] = {
        "object": {"store": "staging", "key": key, "version": "v1"},
        "size_bytes": len(data),
        "sha256": raw["video"]["sha256"],
        "mime_type": "video/mp4",
    }
    job_bytes = json.dumps(raw, sort_keys=True).encode()
    (directory / "job.json").write_bytes(job_bytes)
    (directory / "authorization.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": raw["job_id"],
                "trust_root": "OPERATOR_DEVICE_EVENT",
                "authorized_at": datetime.now(UTC).isoformat(),
                "job_sha256": hashlib.sha256(job_bytes).hexdigest(),
            }
        )
    )
    return directory


DATA = bytes((7 + i) % 251 for i in range(5 * CHUNK_UNIT + 777))
SHA = hashlib.sha256(DATA).hexdigest()


def _source(store: FakeObjectStore, version: str = "v1") -> ObjectSource:
    return ObjectSource(store, "staging", "episodes/a.mp4", version, len(DATA))


def _store(**kw: object) -> FakeObjectStore:
    store = FakeObjectStore()
    store.put("staging", "episodes/a.mp4", "v1", DATA, **kw)  # type: ignore[arg-type]
    return store


def test_an_object_source_reads_ranges_and_hashes_the_whole_object() -> None:
    source = _source(_store())
    assert source.read_range(10, 5) == DATA[10:15]
    assert source.read_range(len(DATA) - 3, 100) == DATA[-3:]
    assert source.sha256() == SHA == digest_of(source)


def test_an_object_source_notices_a_replaced_or_missing_object() -> None:
    store = _store()
    assert _source(store).matches()
    assert not _source(store, "v2").matches(), "no such version"
    store.put("staging", "episodes/a.mp4", "v1", DATA + b"x")
    assert not _source(store).matches(), "same version, other size"
    assert not ObjectSource(store, "staging", "gone.mp4", "v1", 1).matches()


@pytest.mark.parametrize(
    "key", ["/abs.mp4", "a/../b.mp4", "..", "a\nb.mp4", ""], ids=repr
)
def test_an_object_key_cannot_escape_or_hide_a_line(key: str) -> None:
    with pytest.raises(ValidationError):
        ObjectRef(store="staging", key=key, version="v1")


def test_a_media_file_names_exactly_one_location(tmp_path: Path) -> None:
    ref = {"store": "staging", "key": "a.mp4", "version": "v1"}
    base = {"size_bytes": 5, "sha256": "0" * 64, "mime_type": "video/mp4"}
    MediaFile.model_validate({**base, "object": ref})
    MediaFile.model_validate({**base, "path": str(tmp_path / "a.mp4")})
    with pytest.raises(ValidationError):
        MediaFile.model_validate({**base})
    with pytest.raises(ValidationError):
        MediaFile.model_validate({**base, "object": ref, "path": str(tmp_path)})


def _transfer(yt: FakeYouTube, url: str, source: ObjectSource) -> ResumableTransfer:
    return ResumableTransfer(
        url=url,
        file=source,
        mime_type="video/mp4",
        http=yt,
        chunk_bytes=2 * CHUNK_UNIT,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )


def _session(yt: FakeYouTube) -> str:
    return yt.open(
        "videos",
        len(DATA),
        {"metadata": {"title": "t"}, "status": {"privacy": "private"}},
    )


def test_a_transfer_completes_from_a_staged_object() -> None:
    yt, store = FakeYouTube(), _store()
    url = _session(yt)
    out = _transfer(yt, url, _source(store)).run()
    assert out.state is TransferState.COMPLETED
    assert yt.sessions[url].data == bytearray(DATA)
    assert yt.puts[0][1] == f"bytes */{len(DATA)}", "the probe still comes first"
    assert all(length <= 2 * CHUNK_UNIT for _o, length in store.reads)


def test_a_transfer_resumes_at_the_stored_byte_from_an_object() -> None:
    yt, store = FakeYouTube(), _store()
    url = _session(yt)
    yt.drop_after = CHUNK_UNIT + 1000  # part of the first chunk lands, the line drops
    out = _transfer(yt, url, _source(store)).run()
    assert out.state is TransferState.COMPLETED
    assert yt.sessions[url].data == bytearray(DATA)
    resumed = [r for _u, r in yt.puts if r.startswith(f"bytes {CHUNK_UNIT + 1000}-")]
    assert resumed, "the next chunk starts exactly where YouTube's Range ended"


def test_an_object_replaced_mid_transfer_stops_the_transfer() -> None:
    yt, store = FakeYouTube(), _store()
    url = _session(yt)

    def replace(number: int) -> None:
        if number == 1:  # the studio replaced the object after the first chunk
            del store.objects[("staging", "episodes/a.mp4", "v1")]

    store.before_read = replace
    out = _transfer(yt, url, _source(store)).run()
    assert out.state is TransferState.FILE_CHANGED


# --- the executor ---------------------------------------------------------


def _executor(
    directory: Path, yt: FakeYouTube, store: FakeObjectStore | None
) -> PublishExecutor:
    return PublishExecutor(
        directory,
        broker=FakeBroker(yt),
        links=InMemoryUploadLinkStore(),
        http=yt,
        sleep=lambda _s: None,
        chunk_bytes=CHUNK_UNIT,
        approval_wait=0,
        object_reader=store,
    )


def test_the_executor_publishes_a_video_staged_in_the_store(tmp_path: Path) -> None:
    yt, store = FakeYouTube(), FakeObjectStore()
    directory = make_staged_job(tmp_path, store)
    result = _executor(directory, yt, store).run()
    assert result.exit_code == EXIT_DONE, result.status
    job = JobDirectory(directory).job()
    (video,) = yt.videos.values()
    assert job.video.object is not None and job.video.path is None
    assert video.content_sha256 == job.video.sha256
    assert not (tmp_path / "files" / "take-1.mp4").exists(), "no local copy was read"


def test_a_job_that_names_an_object_needs_an_object_store(tmp_path: Path) -> None:
    yt, store = FakeYouTube(), FakeObjectStore()
    directory = make_staged_job(tmp_path, store)
    held = _executor(directory, yt, None).run()
    assert held.exit_code == EXIT_HELD and held.status["reason"] == "no_object_store"
    assert not yt.sessions


def test_staged_bytes_that_differ_from_the_job_are_held(tmp_path: Path) -> None:
    yt, store = FakeYouTube(), FakeObjectStore()
    directory = make_staged_job(tmp_path, store, store_sha256=False)
    (key,) = store.objects
    data, _sha = store.objects[key]
    store.objects[key] = (bytes([data[0] ^ 0xFF]) + data[1:], None)
    held = _executor(directory, yt, store).run()
    assert held.exit_code == EXIT_HELD and held.status["reason"] == "file_changed"
    assert not yt.sessions, "nothing is uploaded from bytes the operator didn't approve"


def test_a_stored_sha256_that_differs_from_the_job_is_held_before_reading(
    tmp_path: Path,
) -> None:
    yt, store = FakeYouTube(), FakeObjectStore()
    directory = make_staged_job(tmp_path, store)
    (key,) = store.objects
    store.objects[key] = (store.objects[key][0], "f" * 64)
    held = _executor(directory, yt, store).run()
    assert held.exit_code == EXIT_HELD and held.status["reason"] == "file_changed"
    assert store.reads == [], "the metadata check comes before any byte is read"


def test_a_staged_job_file_is_plain_json_with_no_address(tmp_path: Path) -> None:
    store = FakeObjectStore()
    directory = make_staged_job(tmp_path, store)
    raw = json.loads((directory / "job.json").read_text())
    assert raw["video"]["object"] == {
        "store": "staging",
        "key": "episodes/take-1.mp4",
        "version": "v1",
    }
    assert "path" not in raw["video"]
