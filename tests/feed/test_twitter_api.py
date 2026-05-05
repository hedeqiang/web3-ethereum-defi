from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
import tweepy

from eth_defi.feed import twitter_api
from eth_defi.feed.database import VaultPostDatabase
from eth_defi.feed.twitter_api import TwitterUserCache, compute_handles_hash, sync_x_list_members

LIST_ID = "123"
LIST_MEMBER_PAGE_SIZE = 100


class _RateLimitedResponse:
    """Minimal response object for Tweepy rate-limit exceptions."""

    headers: ClassVar[dict[str, str]] = {"retry-after": "2"}
    reason = "Too Many Requests"
    status_code = 429
    text = "Too Many Requests"

    @staticmethod
    def json() -> dict[str, str]:
        """Return a minimal X API error payload."""

        return {"title": "Too Many Requests"}


def test_sync_x_list_members_waits_on_rate_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Wait for X rate-limit reset and retry the same list member.

    A recoverable rate-limit response should not force the operator to manually
    rerun the sync command when X provides a retry delay.
    """

    monkeypatch.setattr(
        twitter_api,
        "resolve_twitter_handles",
        lambda _handles, _bearer_token, _user_cache: {
            "alice": "1",
            "bob": "2",
        },
    )

    add_calls: list[str] = []
    sleep_calls: list[float] = []

    class FakeClient:
        """Fake Tweepy client for list member reads and writes."""

        def __init__(self, **_kwargs: object):
            pass

        @staticmethod
        def get_list_members(list_id: str, max_results: int, pagination_token: str | None):
            """Return an empty list so every resolved user needs adding."""

            assert list_id == LIST_ID
            assert max_results == LIST_MEMBER_PAGE_SIZE
            assert pagination_token is None
            return SimpleNamespace(data=[], meta={})

        @staticmethod
        def add_list_member(list_id: str, user_id: str) -> None:
            """Rate-limit the first write call, then allow retries."""

            assert list_id == LIST_ID
            add_calls.append(user_id)
            if add_calls == ["1"]:
                raise tweepy.TooManyRequests(_RateLimitedResponse())

    monkeypatch.setattr(twitter_api.tweepy, "Client", FakeClient)
    monkeypatch.setattr(twitter_api.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    db = VaultPostDatabase(tmp_path / "posts.duckdb")
    try:
        added = sync_x_list_members(
            LIST_ID,
            ["alice", "bob"],
            "consumer-key",
            "consumer-secret",
            "access-token",
            "access-token-secret",
            TwitterUserCache(tmp_path / "twitter-users.json"),
            "bearer-token",
            db,
            add_delay_seconds=0,
        )

        assert added == 2
        assert add_calls == ["1", "1", "2"]
        assert sleep_calls == [2]
        assert db.get_sync_state("twitter_handles_hash") == compute_handles_hash(["alice", "bob"])
    finally:
        db.close()
