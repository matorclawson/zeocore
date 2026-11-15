"""GoogleSheetsService.from_credentials: in-memory credentials, no files."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zeo_core.core.errors import ZeoApiError
from zeo_core.integrations.google.sheets import GoogleSheetsService


def _fake_api() -> MagicMock:
    api = MagicMock()
    get = api.spreadsheets.return_value.values.return_value.get
    get.return_value.execute.return_value = {
        "range": "A1:B1",
        "majorDimension": "ROWS",
        "values": [["asset", "owner"]],
    }
    return api


def test_builds_client_from_credentials_and_reads_without_any_file() -> None:
    credentials = MagicMock(name="fake-credentials")
    api = _fake_api()
    with patch("googleapiclient.discovery.build", return_value=api) as build:
        service = GoogleSheetsService.from_credentials(credentials)
        result = service.get_values("sheet-id", "A1:B1")
    build.assert_called_once_with("sheets", "v4", credentials=credentials)
    assert service.is_available()
    assert result.success
    assert result.content is not None and result.content["values"] == [
        ["asset", "owner"]
    ]


def test_does_not_touch_config_or_auth_providers() -> None:
    with patch("googleapiclient.discovery.build", return_value=_fake_api()):
        with patch(
            "zeo_core.integrations.google.sheets.service.GoogleAuthProvider"
        ) as auth:
            service = GoogleSheetsService.from_credentials(MagicMock())
            service.get_values("sheet-id", "A1:B1")
    auth.assert_not_called()


def test_build_failure_raises_api_error() -> None:
    with patch("googleapiclient.discovery.build", side_effect=RuntimeError("boom")):
        with pytest.raises(ZeoApiError):
            GoogleSheetsService.from_credentials(MagicMock())
