"""The signing service and its admission rule (ZEOCORE-SOW-24 §1 and §2)."""

from __future__ import annotations

import io
import json
import tarfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from zeo_core.cli.release import make_command
from zeo_core.release.signing import (
    KeychainState,
    LoadedManifest,
    SigningRequest,
    create_request,
    open_request,
)
from zeo_core.release.signing.admission import (
    MAX_REQUESTS_PER_HOUR,
    GhCli,
    Grant,
    ReviewUnreadableError,
    admit,
)
from zeo_core.release.signing.service import (
    SigningService,
    Whois,
    make_handler,
    tailnet_address,
)
from zeo_core.release.signing.toolchain import Completed

from .conftest import HOST, FakeToolchain

Release = Callable[..., LoadedManifest]
NODE = "eng-uk-0006.tail29b7b0.ts.net"
REPO = "zeroemployeeorg/pomar"
COMMIT = "c" * 40
GRANTS = {
    NODE: {
        "pomar": Grant(
            REPO, frozenset({"ac.zeo.pomar.pomar", "ac.zeo.pomar.pomar-host"})
        )
    }
}


@dataclass
class FakeGitHub:
    files: dict[tuple[str, str, str], bytes] = field(default_factory=dict)
    merged: set[str] = field(default_factory=set)
    broken: bool = False
    reads: int = 0

    def default_branch(self, repo: str) -> str:
        self.reads += 1
        if self.broken:
            raise ReviewUnreadableError("down")
        return "main"

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        return self.files.get((repo, commit, path))

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        return commit in self.merged


def _pack(
    release: Release,
    tmp_path: Path,
    *specs: dict[str, object],
    repo: str = REPO,
    product: str = "pomar",
) -> tuple[Path, LoadedManifest]:
    built = release(*(specs or ({"name": "pomar"},)), product=product)
    request = SigningRequest(
        pack_version=1,
        product=product,
        repo=repo,
        commit=COMMIT,
        manifest_path="release.yaml",
    )
    out = tmp_path / f"request-{len(list(tmp_path.glob('request-*')))}.tar"
    create_request(built, request, out)
    return out, built


def _reviewed(built: LoadedManifest, *, merged: bool = True) -> FakeGitHub:
    github = FakeGitHub()
    github.files[(REPO, COMMIT, "release.yaml")] = built.manifest_path.read_bytes()
    if merged:
        github.merged.add(COMMIT)
    return github


# -- admission -------------------------------------------------------------------


def _admit(
    release: Release,
    tmp_path: Path,
    *,
    node: str = NODE,
    specs: tuple[dict[str, object], ...] = (),
    repo: str = REPO,
    product: str = "pomar",
) -> str | None:
    pack, built = _pack(release, tmp_path, *specs, repo=repo, product=product)
    opened = open_request(pack, tmp_path / f"in-{pack.stem}")
    refusal = admit(opened, node, _reviewed(built), GRANTS)
    return None if refusal is None else refusal.reason


def test_a_reviewed_request_from_its_node_is_admitted(
    release: Release, tmp_path: Path
) -> None:
    assert _admit(release, tmp_path) is None


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"node": "rodimac.tail29b7b0.ts.net"}, "caller_unadmitted"),
        ({"product": "runtime", "specs": ({"name": "zeo"},)}, "caller_unadmitted"),
        ({"specs": ({"name": "pomar-hygiene"},)}, "identifier_undeclared"),
        (
            {"specs": ({"name": "pomar", "audience": "distributed"},)},
            "audience_not_unattended",
        ),
        ({"repo": "someone/else"}, "manifest_unreviewed"),
    ],
)
def test_admission_refuses(
    release: Release, tmp_path: Path, kw: dict[str, object], reason: str
) -> None:
    assert _admit(release, tmp_path, **kw) == reason  # type: ignore[arg-type]


def test_an_unmerged_commit_is_unreviewed(release: Release, tmp_path: Path) -> None:
    pack, built = _pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "in")
    refusal = admit(opened, NODE, _reviewed(built, merged=False), GRANTS)
    assert refusal is not None and refusal.reason == "manifest_unreviewed"


def test_a_manifest_that_differs_from_the_merged_one_is_unreviewed(
    release: Release, tmp_path: Path
) -> None:
    pack, _ = _pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "in")
    github = FakeGitHub(merged={COMMIT})
    github.files[(REPO, COMMIT, "release.yaml")] = b"signing: other\n"
    refusal = admit(opened, NODE, github, GRANTS)
    assert refusal is not None and refusal.reason == "manifest_unreviewed"


def test_an_unreadable_github_holds(release: Release, tmp_path: Path) -> None:
    pack, _ = _pack(release, tmp_path)
    refusal = admit(
        open_request(pack, tmp_path / "in"), NODE, FakeGitHub(broken=True), GRANTS
    )
    assert refusal is not None and (refusal.status, refusal.reason) == (
        "held",
        "review_unreadable",
    )


def test_too_many_executables_are_refused(release: Release, tmp_path: Path) -> None:
    specs: tuple[dict[str, object], ...] = tuple(
        {"name": f"tool-{i}"} for i in range(17)
    )
    pack, built = _pack(release, tmp_path, *specs)
    grants = {
        NODE: {
            "pomar": Grant(REPO, frozenset(f"ac.zeo.pomar.tool-{i}" for i in range(17)))
        }
    }
    refusal = admit(open_request(pack, tmp_path / "in"), NODE, _reviewed(built), grants)
    assert refusal is not None and refusal.reason == "request_invalid"


def test_the_shipped_allowlist_names_pomars_seven_identifiers() -> None:
    from zeo_core.release.signing.admission import ALLOWLIST

    grant = ALLOWLIST[NODE]["pomar"]
    assert grant.repo == REPO and len(grant.identifiers) == 7
    assert "ac.zeo.pomar.pomar-host" in grant.identifiers


# -- the service core ------------------------------------------------------------


@pytest.fixture
def clock() -> list[datetime]:
    return [datetime(2026, 10, 11, 9, 0, tzinfo=UTC)]


def _service(
    tmp_path: Path, github: FakeGitHub, tools: FakeToolchain, clock: list[datetime]
) -> SigningService:
    return SigningService(
        tmp_path / "state", tools, github, HOST, allowlist=GRANTS, now=lambda: clock[0]
    )


def _journal(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "state" / "requests.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_the_service_signs_once_and_repeats_the_answer(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> None:
    pack, built = _pack(release, tmp_path)
    tools = FakeToolchain()
    service = _service(tmp_path, _reviewed(built), tools, clock)
    try:
        first = service.handle(NODE, pack.read_bytes())
        again = service.handle(NODE, pack.read_bytes())
        status = service.status(NODE, first.request_id or "")
    finally:
        service.close()
    assert first.status is HTTPStatus.OK and first.content_type == "application/x-tar"
    assert again.body == first.body == status.body
    assert tools.sign_calls == ["ac.zeo.pomar.pomar"]
    with tarfile.open(fileobj=io.BytesIO(first.body)) as tar:
        assert sorted(tar.getnames()) == ["receipts/sign-pomar.json", "signed/pomar"]
    assert [entry["outcome"] for entry in _journal(tmp_path)] == ["signed", "signed"]


def test_every_refusal_is_journaled_and_not_cached(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> None:
    pack, built = _pack(release, tmp_path)
    github = _reviewed(built, merged=False)
    service = _service(tmp_path, github, FakeToolchain(), clock)
    try:
        refused = service.handle(NODE, pack.read_bytes())
        github.merged.add(COMMIT)  # the PR merges
        admitted = service.handle(NODE, pack.read_bytes())
    finally:
        service.close()
    assert refused.status is HTTPStatus.FORBIDDEN
    assert json.loads(refused.body)["outcome"] == "manifest_unreviewed"
    assert admitted.status is HTTPStatus.OK
    assert [entry["outcome"] for entry in _journal(tmp_path)] == [
        "manifest_unreviewed",
        "signed",
    ]


@pytest.mark.parametrize(
    ("node", "body", "status", "outcome"),
    [
        ("rodimac.tail29b7b0.ts.net", b"x", HTTPStatus.FORBIDDEN, "caller_unadmitted"),
        (NODE, b"not a tar file" * 50, HTTPStatus.BAD_REQUEST, "request_invalid"),
    ],
)
def test_the_service_refuses_bad_callers_and_packs(
    tmp_path: Path,
    clock: list[datetime],
    node: str,
    body: bytes,
    status: HTTPStatus,
    outcome: str,
) -> None:
    service = _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    try:
        reply = service.handle(node, body)
    finally:
        service.close()
    assert (reply.status, json.loads(reply.body)["outcome"]) == (status, outcome)
    assert _journal(tmp_path)[0]["node"] == node


def test_a_pack_with_a_changed_file_is_refused(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> None:
    pack, built = _pack(release, tmp_path)
    with tarfile.open(pack) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar}  # type: ignore[union-attr]
    members["bin/pomar"] = b"swapped"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    service = _service(tmp_path, _reviewed(built), FakeToolchain(), clock)
    try:
        reply = service.handle(NODE, buffer.getvalue())
    finally:
        service.close()
    assert (reply.status, json.loads(reply.body)["outcome"]) == (
        HTTPStatus.FORBIDDEN,
        "input_mismatch",
    )


def test_a_locked_keychain_is_held_never_unlocked(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> None:
    pack, built = _pack(release, tmp_path)
    tools = FakeToolchain(keychain=KeychainState.LOCKED)
    service = _service(tmp_path, _reviewed(built), tools, clock)
    try:
        reply = service.handle(NODE, pack.read_bytes())
    finally:
        service.close()
    answer = json.loads(reply.body)
    assert (reply.status, answer["status"], answer["outcome"]) == (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "held",
        "signing_keychain_locked",
    )
    assert tools.sign_calls == []


def test_a_node_is_rate_limited_per_hour(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> None:
    service = _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    try:
        for _ in range(MAX_REQUESTS_PER_HOUR):
            service.handle(NODE, b"junk")
        limited = service.handle(NODE, b"junk")
        clock[0] += timedelta(hours=1, seconds=1)
        later = service.handle(NODE, b"junk")
    finally:
        service.close()
    assert json.loads(limited.body)["outcome"] == "rate_limited"
    assert json.loads(later.body)["outcome"] == "request_invalid"


def test_status_needs_an_allowed_node_and_a_request_id(
    tmp_path: Path, clock: list[datetime]
) -> None:
    service = _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    try:
        assert (
            json.loads(service.status("other", "a" * 64).body)["outcome"]
            == "caller_unadmitted"
        )
        assert (
            json.loads(service.status(NODE, "nope").body)["outcome"]
            == "request_invalid"
        )
        assert (
            json.loads(service.status(NODE, "a" * 64).body)["outcome"]
            == "request_unsettled"
        )
    finally:
        service.close()


def test_one_service_per_state_directory(tmp_path: Path, clock: list[datetime]) -> None:
    first = _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    try:
        with pytest.raises(RuntimeError, match="held by another"):
            _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    finally:
        first.close()


# -- HTTP, on loopback ---------------------------------------------------------------


@pytest.fixture
def server(
    release: Release, tmp_path: Path, clock: list[datetime]
) -> Iterator[tuple[str, Path, SigningService]]:
    pack, built = _pack(release, tmp_path)
    service = _service(tmp_path, _reviewed(built), FakeToolchain(), clock)
    callers = {"127.0.0.1": NODE}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, callers.get))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", pack, service
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.close()


def test_http_sign_then_status(server: tuple[str, Path, SigningService]) -> None:
    base, pack, _ = server
    signed = httpx.post(f"{base}/v1/sign", content=pack.read_bytes())
    assert signed.status_code == 200
    request_id = signed.headers["x-zeo-request-id"]
    again = httpx.get(f"{base}/v1/sign/{request_id}")
    assert again.content == signed.content


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("POST", "/v2/sign", b"x", 400),
        ("GET", "/elsewhere", b"", 400),
        ("POST", "/v1/sign", b"", 400),
    ],
)
def test_http_refuses_unknown_paths_and_empty_bodies(
    server: tuple[str, Path, SigningService],
    method: str,
    path: str,
    body: bytes,
    status: int,
) -> None:
    base, _, _ = server
    assert httpx.request(method, f"{base}{path}", content=body).status_code == status


def test_http_refuses_an_unknown_caller(tmp_path: Path, clock: list[datetime]) -> None:
    service = _service(tmp_path, FakeGitHub(), FakeToolchain(), clock)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(service, lambda _a: None)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert httpx.post(f"{base}/v1/sign", content=b"x").status_code == 403
        assert httpx.get(f"{base}/v1/sign/{'a' * 64}").status_code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.close()


# -- whois, the tailnet address and GitHub reads -------------------------------------


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


def test_whois_reads_the_node_and_caches_it() -> None:
    runner = Runner(
        Completed(0, json.dumps({"Node": {"Name": NODE + "."}}).encode(), b"")
    )
    whois = Whois(runner, cli="/ts")
    assert whois("100.1.2.3") == NODE
    assert whois("100.1.2.3") == NODE
    assert runner.calls == [["/ts", "whois", "--json", "100.1.2.3"]]


@pytest.mark.parametrize(
    "answer",
    [
        Completed(1, b"", b"no"),
        Completed(0, b"not json", b""),
        Completed(0, b'{"Node": {"Name": ""}}', b""),
        OSError("x"),
    ],
)
def test_whois_without_a_node_is_none(answer: Completed | BaseException) -> None:
    assert Whois(Runner(answer))("100.1.2.3") is None


@pytest.mark.parametrize(
    "listen",
    ["127.0.0.1:7744", "100.123.206.49:0", "100.123.206.49", "nope:1", "8.8.8.8:80"],
)
def test_only_a_tailnet_address_is_served(listen: str) -> None:
    with pytest.raises(ValueError):
        tailnet_address(listen)


def test_a_tailnet_address_is_accepted() -> None:
    assert tailnet_address("100.123.206.49:7744") == ("100.123.206.49", 7744)


def test_gh_reads() -> None:
    runner = Runner(
        Completed(0, b'{"default_branch": "main"}', b""),
        Completed(0, b"manifest bytes", b""),
        Completed(1, b"", b"gh: Not Found (HTTP 404)"),
        Completed(0, b'{"status": "behind"}', b""),
        Completed(0, b'{"status": "ahead"}', b""),
        Completed(1, b"", b"gh: Not Found (HTTP 404)"),
    )
    gh = GhCli(runner, gh="gh")
    assert gh.default_branch(REPO) == "main"
    assert gh.file_at(REPO, COMMIT, "release.yaml") == b"manifest bytes"
    assert gh.file_at(REPO, COMMIT, "absent.yaml") is None
    assert gh.is_ancestor(REPO, COMMIT, "main") is True
    assert gh.is_ancestor(REPO, COMMIT, "main") is False
    assert gh.is_ancestor(REPO, COMMIT, "main") is False
    assert runner.calls[1][2:4] == ["-H", "Accept: application/vnd.github.raw"]


@pytest.mark.parametrize(
    ("answer", "call"),
    [
        (Completed(1, b"", b"HTTP 500"), "default_branch"),
        (OSError("no gh"), "default_branch"),
        (Completed(0, b"[]", b""), "default_branch"),
        (Completed(0, b"nope", b""), "default_branch"),
        (Completed(0, b"{}", b""), "default_branch"),
        (Completed(0, b'{"status": "weird"}', b""), "is_ancestor"),
    ],
)
def test_gh_failures_are_unreadable(
    answer: Completed | BaseException, call: str
) -> None:
    gh = GhCli(Runner(answer))
    with pytest.raises(ReviewUnreadableError):
        if call == "default_branch":
            gh.default_branch(REPO)
        else:
            gh.is_ancestor(REPO, COMMIT, "main")


# -- send and serve -------------------------------------------------------------------


def _body(status: str, outcome: str) -> bytes:
    answer = {"ok": False, "status": status, "outcome": outcome, "message": "m"}
    return json.dumps(answer).encode()


def _send(
    tmp_path: Path, post: Callable[[str, bytes], tuple[int, str, bytes]], **fields: str
) -> tuple[int, dict[str, object]]:
    (tmp_path / "r.tar").write_bytes(b"pack")
    request = {
        "pack": str(tmp_path / "r.tar"),
        "service": "100.123.206.49:7744",
        "out": str(tmp_path / "back.tar"),
        **fields,
    }
    return make_command(FakeToolchain, {}, post=post)(
        ["send"], json.dumps(request).encode()
    )


def test_send_writes_the_response(tmp_path: Path) -> None:
    urls: list[str] = []

    def post(url: str, body: bytes) -> tuple[int, str, bytes]:
        urls.append(url)
        return 200, "application/x-tar", b"response"

    status, answer = _send(tmp_path, post)
    assert (status, answer["outcome"]) == (0, "signed")
    assert (tmp_path / "back.tar").read_bytes() == b"response"
    assert urls == ["http://100.123.206.49:7744/v1/sign"]


@pytest.mark.parametrize(
    ("reply", "status", "outcome"),
    [
        (
            (
                403,
                "application/json",
                _body("refused", "manifest_unreviewed"),
            ),
            20,
            "manifest_unreviewed",
        ),
        (
            (
                400,
                "application/json",
                _body("invalid", "request_invalid"),
            ),
            2,
            "request_invalid",
        ),
        (
            (
                409,
                "application/json",
                _body("ambiguous", "signing_attempt_unreconciled"),
            ),
            13,
            "signing_attempt_unreconciled",
        ),
        ((502, "text/html", b"<html>"), 13, "service_answer_unreadable"),
    ],
)
def test_send_relays_the_services_answer(
    tmp_path: Path, reply: tuple[int, str, bytes], status: int, outcome: str
) -> None:
    got, answer = _send(tmp_path, lambda _u, _b: reply)
    assert (got, answer["outcome"]) == (status, outcome)
    assert not (tmp_path / "back.tar").exists()


def test_send_holds_when_the_service_is_unreachable(tmp_path: Path) -> None:
    def post(url: str, body: bytes) -> tuple[int, str, bytes]:
        raise httpx.ConnectError("refused")

    assert _send(tmp_path, post)[1]["outcome"] == "service_unreachable"


@pytest.mark.parametrize(
    "fields",
    [{"service": "127.0.0.1:7744"}, {"pack": "/absent.tar"}],
)
def test_send_refuses_a_bad_request(tmp_path: Path, fields: dict[str, str]) -> None:
    assert _send(tmp_path, lambda _u, _b: (200, "", b""), **fields)[0] == 2


def test_send_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "back.tar").write_bytes(b"earlier")
    assert _send(tmp_path, lambda _u, _b: (200, "application/x-tar", b"x"))[0] == 2


def test_serve_needs_a_tailnet_address_and_the_signing_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[tuple[str, Path, str]] = []

    def serve_with(listen: str, state: Path, host: str) -> None:
        served.append((listen, state, host))

    request = {"listen": "100.123.206.49:7744", "state_dir": str(tmp_path / "s")}
    command = make_command(
        FakeToolchain, {"ZEOCORE_SIGNING_HOST": HOST}, serve_with=serve_with
    )
    assert command(["serve"], json.dumps(request).encode()) == (
        0,
        {"ok": True, "outcome": "stopped"},
    )
    assert served == [("100.123.206.49:7744", tmp_path / "s", HOST)]
    assert json.loads(capsys.readouterr().out)["event"] == "serving"

    unconfigured = make_command(FakeToolchain, {}, serve_with=serve_with)
    assert (
        unconfigured(["serve"], json.dumps(request).encode())[1]["outcome"]
        == "signing_host_unconfigured"
    )
    loopback = {**request, "listen": "127.0.0.1:7744"}
    assert command(["serve"], json.dumps(loopback).encode())[0] == 2
