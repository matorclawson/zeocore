"""The zeo-release App: its identity, and the one tag call it makes."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from zeo_core.release.app_tokens import GITHUB_API, AppTokenError
from zeo_core.release.pypi import ReleaseApp
from zeo_core.release.pypi.reads import TagAnswerLostError, TargetUnreadableError

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
REPO = "zeroemployeeorg/zeocore"
COMMIT = "a" * 40
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


class Key:
    def load(self) -> SecretStr:
        return SecretStr(PEM)


class Locked:
    def load(self) -> SecretStr:
        raise AppTokenError("refused", "the App key is not in this user's Keychain")


def _token() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": "ghs_secret",
            "expires_at": "2026-10-11T13:00:00Z",
            "permissions": {"contents": "write", "metadata": "read"},
            "repositories": [{"full_name": REPO}],
        },
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _app(
    handler: Handler, key: object = None
) -> tuple[ReleaseApp, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(base_url=GITHUB_API, transport=httpx.MockTransport(record))
    app = ReleaseApp(
        app_id=4242,
        key_source=key or Key(),  # type: ignore[arg-type]
        http_client=client,
        clock=lambda: NOW,
    )
    return app, seen


def _github(tag_status: int | Exception = 201) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": 4242, "slug": "zeo-release"})
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77})
        if path == "/app/installations/77/access_tokens":
            return _token()
        if path == "/installation/token":
            return httpx.Response(204)
        if path == f"/repos/{REPO}/git/refs":
            if isinstance(tag_status, Exception):
                raise tag_status
            return httpx.Response(tag_status, json={})
        return httpx.Response(404)

    return handler


def test_identity_is_the_apps_own() -> None:
    app, seen = _app(_github())
    assert app.identity() == (4242, "zeo-release")
    assert seen[0].headers["authorization"].startswith("Bearer ")


def test_the_tag_is_made_with_a_one_repository_token_then_revoked() -> None:
    app, seen = _app(_github())
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "created"
    paths = [request.url.path for request in seen]
    assert paths == [
        f"/repos/{REPO}/installation",
        "/app/installations/77/access_tokens",
        f"/repos/{REPO}/git/refs",
        "/installation/token",
    ]
    asked = json.loads(seen[1].content)
    assert asked == {"repositories": ["zeocore"], "permissions": {"contents": "write"}}
    tag = seen[2]
    assert json.loads(tag.content) == {"ref": "refs/tags/v0.13.0", "sha": COMMIT}
    assert tag.headers["authorization"] == "token ghs_secret"
    assert seen[3].method == "DELETE"


@pytest.mark.parametrize(
    ("status", "result"), [(422, "exists"), (403, "rejected"), (404, "rejected")]
)
def test_the_tag_answer_is_classified(status: int, result: str) -> None:
    app, _ = _app(_github(status))
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == result


@pytest.mark.parametrize(
    "failure", [502, httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")]
)
def test_a_lost_tag_answer_is_never_guessed(failure: int | Exception) -> None:
    app, seen = _app(_github(failure))
    with pytest.raises(TagAnswerLostError):
        app.create_tag(REPO, "v0.13.0", COMMIT)
    assert seen[-1].url.path == "/installation/token"  # revoked all the same


def test_a_tag_request_that_never_connected_was_not_sent() -> None:
    app, _ = _app(_github(httpx.ConnectError("refused")))
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "not_sent"


def test_no_installation_or_no_token_means_nothing_was_sent() -> None:
    def no_installation(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    app, seen = _app(no_installation)
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "not_sent"

    def no_id(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    app, _ = _app(no_id)
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "not_sent"

    def no_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(403)

    app, seen = _app(no_token)
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "not_sent"
    assert all(request.url.path != f"/repos/{REPO}/git/refs" for request in seen)


def test_a_locked_key_is_unreadable_never_a_guess() -> None:
    app, seen = _app(_github(), key=Locked())
    with pytest.raises(TargetUnreadableError):
        app.identity()
    assert app.create_tag(REPO, "v0.13.0", COMMIT) == "not_sent"
    assert seen == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[1]),
        httpx.Response(200, json={"id": "x", "slug": 3}),
    ],
)
def test_an_unreadable_identity(answer: httpx.Response) -> None:
    app, _ = _app(lambda _r: answer)
    with pytest.raises(TargetUnreadableError):
        app.identity()


def test_an_unreachable_github_is_unreadable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app, _ = _app(down)
    with pytest.raises(TargetUnreadableError):
        app.identity()


def test_the_app_id_is_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        ReleaseApp(app_id=0, key_source=Key())
