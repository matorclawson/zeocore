"""The ``zeo-release`` GitHub App: the only identity that creates release tags.

Its private key stays in the signing host's Keychain (``zeo-release-app-key``)
and is read only to sign the App's own short JWT. Each tag is created with a
fresh installation token scoped to one repository and ``contents: write``,
revoked once the call is answered. No token or key is printed, logged or
returned.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import httpx

from zeo_core.release.app_tokens import (
    GITHUB_API,
    AppKeySource,
    AppTokenError,
    InstallationTokens,
    app_jwt,
)

from .reads import TagAnswerLostError, TagResult, TargetUnreadableError

_API_VERSION: Final = "2022-11-28"


def _headers(scheme: str, credential: str) -> dict[str, str]:
    return {
        "Authorization": f"{scheme} {credential}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": "zeocore-release",
    }


class ReleaseApp:
    def __init__(
        self,
        *,
        app_id: int,
        key_source: AppKeySource,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if app_id <= 0:
            raise ValueError("the App id is a positive integer")
        self.app_id = app_id
        self._key_source = key_source
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = http_client or httpx.Client(
            base_url=GITHUB_API,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _jwt(self) -> str:
        try:
            return app_jwt(self.app_id, self._key_source.load(), self._clock())
        except AppTokenError as error:
            raise TargetUnreadableError(
                f"the release App can't sign: {error}"
            ) from None

    def _read(self, path: str) -> dict[str, object]:
        try:
            response = self._http.get(path, headers=_headers("Bearer", self._jwt()))
        except httpx.TransportError:
            raise TargetUnreadableError("GitHub could not be reached") from None
        if response.status_code != 200:
            raise TargetUnreadableError(f"GitHub answered {response.status_code}")
        try:
            value = response.json()
        except ValueError:
            raise TargetUnreadableError("GitHub answered with something else") from None
        if not isinstance(value, dict):
            raise TargetUnreadableError("GitHub answered with something else")
        return value

    def identity(self) -> tuple[int, str]:
        """The App's own id and slug, as GitHub reports them for this key."""
        answer = self._read("/app")
        app_id, slug = answer.get("id"), answer.get("slug")
        if not isinstance(app_id, int) or not isinstance(slug, str):
            raise TargetUnreadableError("GitHub's App answer is unreadable")
        return app_id, slug

    def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
        """One ``POST /git/refs`` with a one-repository token, sent once."""
        try:
            installation = self._read(f"/repos/{repo}/installation").get("id")
        except TargetUnreadableError:
            return "not_sent"
        if not isinstance(installation, int):
            return "not_sent"
        tokens = InstallationTokens(
            app_id=self.app_id,
            installation_id=installation,
            key_source=self._key_source,
            http_client=self._http,
            clock=self._clock,
        )
        try:
            minted = tokens.token(repo, {"contents": "write"})
        except AppTokenError:
            # No tag request was sent: a token that may have been issued
            # expires on its own, and nothing else happened.
            return "not_sent"
        try:
            response = self._http.post(
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/tags/{tag}", "sha": commit},
                headers=_headers("token", minted.token.get_secret_value()),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return "not_sent"
        except httpx.TransportError:
            raise TagAnswerLostError("GitHub did not answer the tag request") from None
        finally:
            tokens.revoke(minted.token)
        if response.status_code == 201:
            return "created"
        if response.status_code == 422:
            return "exists"
        if 400 <= response.status_code < 500:
            return "rejected"
        raise TagAnswerLostError(f"GitHub answered {response.status_code}")
