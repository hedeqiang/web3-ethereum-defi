"""Backfill recent tweets for Twitter/X handles not yet seen in the database.

Handles tracked in the feeder YAML files but not yet present in the
``tracked_sources`` table, or present but with ``last_post_published_at``
still ``NULL``, are fetched individually via the X API and their posts
are stored.

This is a one-shot companion to the normal list-timeline scan.  The
list-based collector only falls back to individual timelines for new handles
within a regular scan cycle; this script covers an initial bulk backfill
after adding a large batch of new feeder YAML files.

Run in Docker using the ``post-scanner`` service environment:

.. code-block:: shell

    docker compose build vault-scanner
    docker compose run --rm -T --no-deps --entrypoint python post-scanner scripts/feed/backfill-twitter-handles.py

Required environment variables:

- ``TWITTER_BEARER_TOKEN``

Optional environment variables:

- ``DB_PATH``: DuckDB path, default
  ``~/.tradingstrategy/vaults/vault-post-database.duckdb``
- ``MAPPINGS_DIR``: feeder YAML root, default ``eth_defi/data/feeds``
- ``LOG_LEVEL``: logging level, default ``info``
- ``MAX_TWEETS_PER_HANDLE``: tweets to fetch per handle, default ``20``
- ``DELAY_BETWEEN_HANDLES``: seconds to sleep between X API calls, default ``1``
"""

import logging
import os
import time
from pathlib import Path

from tabulate import tabulate

from eth_defi.compat import native_datetime_utc_now
from eth_defi.feed.database import DEFAULT_VAULT_POST_DATABASE, VaultPostDatabase
from eth_defi.feed.sources import FEEDS_DATA_DIR, load_post_sources
from eth_defi.feed.twitter_api import TwitterUserCache, XApiError, fetch_user_tweets, resolve_twitter_handles
from eth_defi.utils import setup_console_logging

logger = logging.getLogger(__name__)


def _get_required_env(name: str) -> str:
    """Read a required environment variable.

    :param name:
        Environment variable name.

    :return:
        Environment variable value.

    :raise RuntimeError:
        If the environment variable is not set.
    """

    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_db_path() -> Path:
    """Read the configured feed database path.

    :return:
        DuckDB database path.
    """

    db_path = os.environ.get("DB_PATH")
    return Path(db_path).expanduser() if db_path else DEFAULT_VAULT_POST_DATABASE


def _get_mappings_dir() -> Path:
    """Read the configured feeder YAML directory.

    :return:
        Feeder YAML root directory.
    """

    mappings_dir = os.environ.get("MAPPINGS_DIR")
    return Path(mappings_dir).expanduser() if mappings_dir else FEEDS_DATA_DIR


def main() -> None:
    """Backfill recent tweets for all handles with no stored last_post_published_at."""

    setup_console_logging(
        default_log_level=os.environ.get("LOG_LEVEL", "info"),
        log_file=Path("logs/backfill-twitter-handles.log"),
    )

    bearer_token = _get_required_env("TWITTER_BEARER_TOKEN")
    db_path = _get_db_path()
    mappings_dir = _get_mappings_dir()
    max_tweets = int(os.environ.get("MAX_TWEETS_PER_HANDLE", "20"))
    delay_between_handles = float(os.environ.get("DELAY_BETWEEN_HANDLES", "1"))

    sources, feeders_skipped, aliases = load_post_sources(mappings_dir)
    twitter_sources = [s for s in sources if s.source_type == "twitter"]

    if not twitter_sources:
        print("No Twitter/X sources found in YAML files — nothing to backfill.")
        return

    logger.info("Loaded %d Twitter sources from %s (%d feeders skipped, %d aliases)", len(twitter_sources), mappings_dir, feeders_skipped, len(aliases))

    # Resolve handles to user IDs so we can call the timeline endpoint
    user_cache = TwitterUserCache()
    handles = [s.source_key for s in twitter_sources]
    logger.info("Resolving %d Twitter handles via X API…", len(handles))
    try:
        handle_to_id = resolve_twitter_handles(handles, bearer_token, user_cache)
    except XApiError as e:
        logger.error("Failed to resolve Twitter handles: %s", e)
        raise SystemExit(1) from None

    resolved_sources = [s for s in twitter_sources if s.source_key in handle_to_id]
    unresolved = len(twitter_sources) - len(resolved_sources)
    if unresolved:
        logger.warning("%d handles could not be resolved (suspended or deleted) and will be skipped", unresolved)

    with VaultPostDatabase(db_path) as db:
        source_ids = db.upsert_tracked_sources(resolved_sources)
        stored_timestamps = db.get_source_last_post_timestamps(source_ids.values())

        # Only backfill sources with no stored last_post_published_at
        to_backfill = [s for s in resolved_sources if stored_timestamps.get(source_ids[s.get_logical_key()]) is None]

        logger.info(
            "%d of %d resolved sources need backfill (NULL last_post_published_at)",
            len(to_backfill),
            len(resolved_sources),
        )

        if not to_backfill:
            print("All handles already have a stored last_post_published_at — nothing to backfill.")
            return

        rows = []
        for source in to_backfill:
            source_id = source_ids[source.get_logical_key()]
            user_id = handle_to_id[source.source_key]
            checked_at = native_datetime_utc_now()

            try:
                posts = fetch_user_tweets(
                    user_id,
                    bearer_token,
                    source.source_key,
                    max_tweets=max_tweets,
                )
            except XApiError as e:
                logger.warning("Failed to fetch tweets for @%s: %s", source.source_key, e)
                db.mark_source_failure(source_id, str(e), checked_at=checked_at)
                rows.append([source.feeder_id, f"@{source.source_key}", "failed", 0, 0, str(e)[:60]])
                continue

            latest_post_at = max((p.published_at for p in posts if p.published_at is not None), default=None)
            inserted = db.insert_posts(source_id, posts)
            db.mark_source_success(source_id, checked_at=checked_at, last_post_published_at=latest_post_at)

            last_str = latest_post_at.isoformat(sep=" ", timespec="seconds") if latest_post_at else "-"
            rows.append([source.feeder_id, f"@{source.source_key}", "ok", len(posts), inserted, last_str])
            logger.info("@%s: fetched %d, inserted %d, last=%s", source.source_key, len(posts), inserted, last_str)

            if delay_between_handles > 0:
                time.sleep(delay_between_handles)

        db.save()

    ok_count = sum(1 for r in rows if r[2] == "ok")
    fail_count = sum(1 for r in rows if r[2] == "failed")
    total_inserted = sum(r[4] for r in rows if r[2] == "ok")

    print()
    print(tabulate(rows, headers=["Feeder", "Handle", "Status", "Fetched", "Inserted", "Last post / Error"], tablefmt="fancy_grid"))
    print(f"\nBackfill complete: {ok_count} handles populated, {fail_count} failed, {total_inserted} posts inserted.")


if __name__ == "__main__":
    main()
