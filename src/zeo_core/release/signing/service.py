"""``zeocore release serve``: matormini's signing service (ZEOCORE-SOW-24).

It follows zmaild's pattern. It listens only on a tailnet ``100.x`` address,
is never funnelled, and takes the caller's node from ``tailscale whois`` on
the peer address, never from anything the caller sends. One request is
handled at a time.

``POST /v1/sign`` takes a request pack and answers with the response pack, or
with a JSON refusal. ``GET /v1/sign/<request id>`` repeats a settled answer.
A settled request gets the same answer again and is never signed twice. Every
request, admitted or refused, is journaled durably with its reason.

Only a signed answer is stored. A refusal is worked out again each time,
because what it rests on can change: a manifest refused as unreviewed is
admitted once its commit merges.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

from .admission import ALLOWLIST, GitHubReads, Grant, Limits, admit
from .pack import MAX_PACK_BYTES, PackError, create_response, open_request
from .step import WorkDir, sign
from .toolchain import Runner, Toolchain, run_tool

TAILSCALE: Final = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_TAILNET: Final = ipaddress.ip_network("100.64.0.0/10")
_REQUEST_ID: Final = re.compile(r"^[0-9a-f]{64}$")
_STATUS: Final = {
    "invalid": HTTPStatus.BAD_REQUEST,
    "refused": HTTPStatus.FORBIDDEN,
    "held": HTTPStatus.SERVICE_UNAVAILABLE,
    "ambiguous": HTTPStatus.CONFLICT,
}

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Reply:
    status: HTTPStatus
    body: bytes
    content_type: str = "application/json"
    request_id: str | None = None


def _json_reply(
    status: str, reason: str, message: str, request_id: str | None
) -> Reply:
    body: dict[str, Any] = {
        "ok": False,
        "status": status,
        "outcome": reason,
        "message": message,
    }
    if request_id is not None:
        body["request_id"] = request_id
    return Reply(_STATUS[status], json.dumps(body).encode(), request_id=request_id)


class RequestJournal:
    """Every request, admitted or refused, appended and fsynced (§1 g)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def recent(self, node: str, since: datetime) -> int:
        """How many requests ``node`` made since ``since``, refused ones too."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        for line in lines:
            record = json.loads(line)
            if (
                record.get("node") == node
                and datetime.fromisoformat(record["at"]) >= since
            ):
                count += 1
        return count


class SigningService:
    """The transport-free core, so every refusal has a fixture."""

    def __init__(
        self,
        state: Path,
        toolchain: Toolchain,
        github: GitHubReads,
        signing_host: str | None,
        *,
        allowlist: dict[str, dict[str, Grant]] | None = None,
        limits: Limits | None = None,
        now: Clock = _now,
    ) -> None:
        self._state = state
        self._toolchain = toolchain
        self._github = github
        self._host = signing_host
        self._allowlist = ALLOWLIST if allowlist is None else allowlist
        self._limits = Limits() if limits is None else limits
        self._now = now
        self._lock = threading.Lock()
        for name in ("packs", "opened", "work", "responses"):
            (state / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        self._journal = RequestJournal(state / "requests.jsonl")
        self._held = os.open(state / "service.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._held)
            raise RuntimeError(f"{state} is held by another signing service") from None

    def close(self) -> None:
        os.close(self._held)

    def _settled(self, request_id: str) -> Reply | None:
        response = self._state / "responses" / f"{request_id}.tar"
        if response.is_file():
            return Reply(
                HTTPStatus.OK, response.read_bytes(), "application/x-tar", request_id
            )
        return None

    def _settle(self, request_id: str, status: str, reason: str, message: str) -> Reply:
        return _json_reply(status, reason, message, request_id)

    def status(self, node: str, request_id: str) -> Reply:
        if node not in self._allowlist:
            return _json_reply(
                "refused", "caller_unadmitted", "this node is not allowed", None
            )
        if not _REQUEST_ID.match(request_id):
            return _json_reply("invalid", "request_invalid", "not a request id", None)
        with self._lock:
            settled = self._settled(request_id)
        return settled or _json_reply(
            "held",
            "request_unsettled",
            "no settled answer for this request",
            request_id,
        )

    def handle(self, node: str, pack: bytes) -> Reply:
        with self._lock:
            reply = self._handle(node, pack)
            self._journal.append(
                {
                    "at": self._now().isoformat(),
                    "node": node,
                    "request_id": reply.request_id,
                    "http_status": int(reply.status),
                    "outcome": (
                        "signed"
                        if reply.status is HTTPStatus.OK
                        else json.loads(reply.body)["outcome"]
                    ),
                }
            )
            return reply

    def _handle(self, node: str, pack: bytes) -> Reply:
        if node not in self._allowlist:
            return _json_reply(
                "refused", "caller_unadmitted", "this node is not allowed", None
            )
        since = self._now() - timedelta(hours=1)
        if self._journal.recent(node, since) >= self._limits.max_requests_per_hour:
            return _json_reply(
                "held", "rate_limited", "too many requests this hour", None
            )
        if len(pack) > MAX_PACK_BYTES:
            return _json_reply(
                "invalid", "request_invalid", "the pack is too large", None
            )
        request_id = hashlib.sha256(pack).hexdigest()
        settled = self._settled(request_id)
        if settled is not None:
            return settled
        stored = self._state / "packs" / f"{request_id}.tar"
        stored.write_bytes(pack)
        opened_dir = self._state / "opened" / f"{request_id}-{uuid.uuid4().hex}"
        try:
            opened = open_request(stored, opened_dir)
        except PackError as error:
            status = "invalid" if error.reason == "request_invalid" else "refused"
            return self._settle(request_id, status, error.reason, str(error))
        refusal = admit(opened, node, self._github, self._allowlist, self._limits)
        if refusal is not None:
            return self._settle(
                request_id, refusal.status, refusal.reason, refusal.message
            )
        work = WorkDir(self._state / "work" / request_id)
        outcome = sign(opened.manifest, work, self._toolchain, self._host)
        if outcome.status != "done":
            # A locked keychain is held, never unlocked (§1 h).
            status = "held" if outcome.status == "waiting" else outcome.status
            return self._settle(request_id, status, outcome.reason, outcome.message)
        partial = self._state / "responses" / f".{request_id}.partial"
        create_response(opened.manifest, work, partial)
        partial.replace(self._state / "responses" / f"{request_id}.tar")
        shutil.rmtree(opened_dir, ignore_errors=True)
        settled = self._settled(request_id)
        if settled is None:  # pragma: no cover - the response was just written
            raise RuntimeError("the response pack vanished")
        return settled


class Whois:
    """The caller's tailnet node, from ``tailscale whois``, cached for a minute."""

    def __init__(
        self, run: Runner = run_tool, cli: str = TAILSCALE, now: Clock = _now
    ) -> None:
        self._run = run
        self._cli = cli
        self._now = now
        self._cache: dict[str, tuple[str, datetime]] = {}

    def __call__(self, address: str) -> str | None:
        hit = self._cache.get(address)
        if hit is not None and self._now() - hit[1] < timedelta(minutes=1):
            return hit[0]
        try:
            done = self._run([self._cli, "whois", "--json", address])
            name = (
                json.loads(done.stdout)["Node"]["Name"] if done.returncode == 0 else ""
            )
        except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:
            return None
        if not isinstance(name, str) or not name:
            return None
        node = name.rstrip(".")
        self._cache[address] = (node, self._now())
        return node


def tailnet_address(listen: str) -> tuple[str, int]:
    """``100.x.y.z:port``, refused unless the address is on the tailnet."""
    host, _, port = listen.rpartition(":")
    try:
        address = ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise ValueError("listen must be a tailnet 100.x address and a port") from None
    if address not in _TAILNET or not 0 < number < 65536:
        raise ValueError("listen must be a tailnet 100.x address and a port")
    return host, number


class _Handler(BaseHTTPRequestHandler):
    server_version = "zeocore-signing/1"
    service: SigningService
    whois: Callable[[str], str | None]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # the request journal is the record

    def _send(self, reply: Reply) -> None:
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        if reply.request_id is not None:
            self.send_header("X-Zeo-Request-Id", reply.request_id)
        self.end_headers()
        self.wfile.write(reply.body)

    def _node(self) -> str | None:
        node = self.whois(self.client_address[0])
        if node is None:
            self._send(
                _json_reply("refused", "caller_unadmitted", "unknown caller", None)
            )
        return node

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        node = self._node()
        if node is None:
            return
        if self.path != "/v1/sign":
            self._send(_json_reply("invalid", "request_invalid", "unknown path", None))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_PACK_BYTES:
            self._send(_json_reply("invalid", "request_invalid", "bad length", None))
            return
        self._send(self.service.handle(node, self.rfile.read(length)))

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        node = self._node()
        if node is None:
            return
        prefix = "/v1/sign/"
        if not self.path.startswith(prefix):
            self._send(_json_reply("invalid", "request_invalid", "unknown path", None))
            return
        self._send(self.service.status(node, self.path.removeprefix(prefix)))


def make_handler(
    service: SigningService, whois: Callable[[str], str | None]
) -> type[BaseHTTPRequestHandler]:
    return type(
        "SigningHandler",
        (_Handler,),
        {"service": service, "whois": staticmethod(whois)},
    )


def serve(
    listen: str, service: SigningService, whois: Callable[[str], str | None]
) -> None:
    """Run until interrupted. Never bound to anything but the tailnet."""
    server = ThreadingHTTPServer(tailnet_address(listen), make_handler(service, whois))
    try:
        server.serve_forever()
    finally:
        server.server_close()
