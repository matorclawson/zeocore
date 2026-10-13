"""Drive comments.list and replies.list: read-only review feedback."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from zeo_core.integrations.core.results import IntegrationResult
from zeo_core.integrations.google.drive import (
    DriveComment,
    DriveCommentAuthor,
    DriveQuotedContent,
    DriveReply,
)
from zeo_core.integrations.google.drive.comments import (
    COMMENT_FIELDS,
    REPLY_FIELDS,
    list_comments,
    list_replies,
)
from zeo_core.integrations.google.drive.service import GoogleDriveService

T1, T2 = "2026-10-12T14:00:00.000Z", "2026-10-12T14:05:00.000Z"
AUTHOR = {"displayName": "Reviewer", "emailAddress": "reviewer@example.org"}


def reply(n: int, **extra: object) -> dict[str, object]:
    return {
        "id": f"r{n}",
        "author": AUTHOR,
        "content": f"reply {n}",
        "createdTime": T1,
        "modifiedTime": T2,
        **extra,
    }


def comment(n: int, **extra: object) -> dict[str, object]:
    return {
        "id": f"c{n}",
        "author": {"displayName": "Editor"},
        "content": f"comment {n}",
        "quotedFileContent": {"mimeType": "text/html", "value": "the anchored text"},
        "anchor": "kix.abc",
        "resolved": False,
        "createdTime": T1,
        "modifiedTime": T2,
        "replies": [reply(n)],
        **extra,
    }


Page = dict[str, object] | Exception


class _Request:
    def __init__(self, answer: Page) -> None:
        self.answer = answer

    def execute(self) -> dict[str, object]:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeDrive:
    """Pages keyed by pageToken (None for the first), and every call made."""

    def __init__(self, pages: dict[str | None, Page]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def comments(self) -> FakeDrive:
        return self

    def replies(self) -> FakeDrive:
        return self

    def list(self, **kwargs: object) -> _Request:
        self.calls.append(dict(kwargs))
        token = kwargs.get("pageToken")
        assert token is None or isinstance(token, str)
        return _Request(self.pages[token])


def test_one_page_of_comments_with_replies_is_read_typed() -> None:
    drive = FakeDrive({None: {"comments": [comment(1)]}})
    result = list_comments(drive, "file1")
    assert result.success, result.error
    assert result.content == (
        DriveComment(
            id="c1",
            author=DriveCommentAuthor(display_name="Editor"),
            content="comment 1",
            quoted_content=DriveQuotedContent(
                mime_type="text/html", value="the anchored text"
            ),
            anchor="kix.abc",
            resolved=False,
            created_time=datetime(2026, 10, 12, 14, 0, tzinfo=UTC),
            modified_time=datetime(2026, 10, 12, 14, 5, tzinfo=UTC),
            replies=(
                DriveReply(
                    id="r1",
                    author=DriveCommentAuthor(
                        display_name="Reviewer", email_address="reviewer@example.org"
                    ),
                    content="reply 1",
                    created_time=datetime(2026, 10, 12, 14, 0, tzinfo=UTC),
                    modified_time=datetime(2026, 10, 12, 14, 5, tzinfo=UTC),
                ),
            ),
        ),
    )
    assert drive.calls == [
        {
            "fileId": "file1",
            "fields": COMMENT_FIELDS,
            "pageSize": 100,
            "includeDeleted": False,
        }
    ]


def test_every_page_is_followed_to_the_end() -> None:
    drive = FakeDrive(
        {
            None: {"comments": [comment(1)], "nextPageToken": "p2"},
            "p2": {"comments": [comment(2)], "nextPageToken": "p3"},
            "p3": {"comments": [comment(3)]},
        }
    )
    result = list_comments(drive, "file1")
    assert result.success, result.error
    assert [c.id for c in result.content or ()] == ["c1", "c2", "c3"]
    assert [call.get("pageToken") for call in drive.calls] == [None, "p2", "p3"]


def test_a_listing_longer_than_the_bound_is_refused_not_truncated() -> None:
    drive = FakeDrive(
        {
            None: {"comments": [comment(1)], "nextPageToken": "p2"},
            "p2": {"comments": [comment(2)], "nextPageToken": "p3"},
        }
    )
    result = list_comments(drive, "file1", max_pages=2)
    assert not result.success
    assert result.content is None
    assert "more than 2 pages" in str(result.error)
    assert "max_pages" in str(result.error)


def test_a_repeated_page_token_is_refused() -> None:
    drive = FakeDrive(
        {
            None: {"comments": [comment(1)], "nextPageToken": "p2"},
            "p2": {"comments": [comment(2)], "nextPageToken": "p2"},
        }
    )
    result = list_comments(drive, "file1")
    assert not result.success
    assert "repeated a page token" in str(result.error)


@pytest.mark.parametrize(
    "item,reason",
    [
        ({**comment(1), "createdTime": None}, "createdTime"),
        ({**comment(1), "author": "x"}, "author"),
        ({**comment(1), "replies": [{**reply(1), "action": "delete"}]}, "action"),
        # An unhashable action must refuse, not escape as a TypeError.
        ({**comment(1), "replies": [{**reply(1), "action": {"x": 1}}]}, "action"),
        ({**comment(1), "anchor": 7}, "anchor"),
        ({**comment(1), "author": {"displayName": "E", "emailAddress": 7}}, "email"),
        ({**comment(1), "quotedFileContent": "text"}, "quotedFileContent"),
        ({**comment(1), "resolved": "yes"}, "resolved"),
        ({**comment(1), "deleted": 1}, "deleted"),
        ({k: v for k, v in comment(1).items() if k != "content"}, "content"),
        ({**comment(1), "createdTime": "not a time"}, "isoformat"),
        ("not an object", "not an object"),
    ],
)
def test_a_malformed_comment_refuses_the_whole_listing(
    item: object, reason: str
) -> None:
    result = list_comments(FakeDrive({None: {"comments": [comment(2), item]}}), "f")
    assert not result.success
    assert reason in str(result.error)


def test_deleted_comments_are_asked_for_only_on_request() -> None:
    deleted = {**comment(1), "deleted": True}
    del deleted["content"]
    drive = FakeDrive({None: {"comments": [deleted]}})
    result = list_comments(drive, "file1", include_deleted=True)
    assert result.success, result.error
    assert drive.calls[0]["includeDeleted"] is True
    only = (result.content or ())[0]
    assert (only.deleted, only.content) == (True, "")


def test_replies_are_listed_with_their_action() -> None:
    drive = FakeDrive(
        {
            None: {"replies": [reply(1)], "nextPageToken": "q"},
            "q": {"replies": [reply(2, action="resolve")]},
        }
    )
    result = list_replies(drive, "file1", "c1")
    assert result.success, result.error
    assert [(r.id, r.action) for r in result.content or ()] == [
        ("r1", None),
        ("r2", "resolve"),
    ]
    assert drive.calls[0] == {
        "fileId": "file1",
        "commentId": "c1",
        "fields": REPLY_FIELDS,
        "pageSize": 100,
        "includeDeleted": False,
    }


@pytest.mark.parametrize(
    "pages,reason",
    [
        ({None: {"replies": [{**reply(1), "action": "delete"}]}}, "action"),
        ({None: {"replies": "x"}}, "without a replies list"),
        (
            {
                None: {"replies": [reply(1)], "nextPageToken": "q"},
                "q": {"replies": [reply(2)], "nextPageToken": "q"},
            },
            "repeated a page token",
        ),
    ],
)
def test_replies_refuse_like_comments(
    pages: dict[str | None, Page], reason: str
) -> None:
    result = list_replies(FakeDrive(pages), "file1", "c1")
    assert not result.success
    assert reason in str(result.error)
    assert "comment c1 on file file1" in str(result.error)


def test_a_reply_listing_past_its_bound_is_refused() -> None:
    drive = FakeDrive(
        {
            None: {"replies": [reply(1)], "nextPageToken": "q"},
            "q": {"replies": [reply(2)], "nextPageToken": "r"},
        }
    )
    result = list_replies(drive, "file1", "c1", max_pages=2)
    assert not result.success
    assert "more than 2 pages" in str(result.error)


def test_the_service_passes_max_pages_through() -> None:
    drive = FakeDrive(
        {
            None: {"comments": [comment(1)], "nextPageToken": "p2"},
            "p2": {"comments": [comment(2)]},
        }
    )
    refused = _service(drive).list_comments("file1", max_pages=1)
    assert not refused.success
    assert "more than 1 pages" in str(refused.error)
    assert _service(drive).list_comments("file1", max_pages=2).success


def test_ids_are_required_and_the_error_says_where_to_get_them() -> None:
    assert "file_id is required" in str(list_comments(FakeDrive({}), "").error)
    error = str(list_replies(FakeDrive({}), "file1", "").error)
    assert "list_comments gives comment ids" in error


def test_an_api_failure_names_the_file_and_operation() -> None:
    failure = HttpError(MagicMock(status=403, reason="Forbidden"), b"denied")
    result = list_comments(FakeDrive({None: failure}), "file9")
    assert not result.success
    assert "file9" in str(result.error)
    assert "comments.list" in str(result.error)


def test_the_fields_mask_names_every_field_read() -> None:
    for field in (
        "nextPageToken",
        "quotedFileContent(mimeType,value)",
        "anchor",
        "resolved",
        "deleted",
        "replies(",
        "author(displayName,emailAddress)",
    ):
        assert field in COMMENT_FIELDS
    assert REPLY_FIELDS.startswith("nextPageToken,replies(")
    assert "action" in REPLY_FIELDS


def _service(drive: object) -> GoogleDriveService:
    service = GoogleDriveService.__new__(GoogleDriveService)
    service._initialized = True
    service.drive_service = drive
    service.logger = MagicMock()
    return service


def test_the_service_methods_delegate_to_the_reads() -> None:
    drive = FakeDrive({None: {"comments": [comment(1)], "replies": [reply(1)]}})
    service = _service(drive)
    assert [c.id for c in service.list_comments("file1").content or ()] == ["c1"]
    assert [r.id for r in service.list_replies("file1", "c1").content or ()] == ["r1"]


def test_the_service_refuses_when_it_cannot_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleDriveService.__new__(GoogleDriveService)
    service._initialized = False
    service.logger = MagicMock()
    failed = IntegrationResult[None].error_result("no Google credentials")
    monkeypatch.setattr(service, "initialize", lambda: failed)
    result = service.list_comments("file1")
    assert not result.success
    assert "no Google credentials" in str(result.error)
