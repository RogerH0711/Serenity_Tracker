"""SQLite schema, migrations, and persistence operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from common import PROJECT_DIR, extract_post_id


DB_PATH = PROJECT_DIR / "serenity.db"
VALID_SENTIMENTS = {"Bullish", "Bearish", "Neutral"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL UNIQUE,
            parse_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (parse_status IN ('pending', 'completed', 'failed')),
            parse_error TEXT,
            parse_attempts INTEGER NOT NULL DEFAULT 0 CHECK (parse_attempts >= 0),
            parsed_at TEXT,
            analysis_version TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            sentiment TEXT NOT NULL
                CHECK (sentiment IN ('Bullish', 'Bearish', 'Neutral')),
            sentiment_evidence TEXT NOT NULL DEFAULT '',
            thesis TEXT NOT NULL,
            risks TEXT,
            analysis_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(post_id) ON DELETE CASCADE,
            UNIQUE (post_id, ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mention_overrides (
            post_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            sentiment TEXT NOT NULL
                CHECK (sentiment IN ('Bullish', 'Bearish', 'Neutral')),
            sentiment_evidence TEXT NOT NULL DEFAULT '',
            thesis TEXT NOT NULL,
            risks TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT 'manual',
            reviewed_at TEXT NOT NULL,
            PRIMARY KEY (post_id, ticker),
            FOREIGN KEY (post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'success', 'partial', 'failed')),
            current_stage TEXT NOT NULL DEFAULT 'database',
            scraped_count INTEGER NOT NULL DEFAULT 0 CHECK (scraped_count >= 0),
            long_posts_attempted INTEGER NOT NULL DEFAULT 0
                CHECK (long_posts_attempted >= 0),
            long_posts_succeeded INTEGER NOT NULL DEFAULT 0
                CHECK (long_posts_succeeded >= 0),
            long_posts_failed INTEGER NOT NULL DEFAULT 0
                CHECK (long_posts_failed >= 0),
            parsed_count INTEGER NOT NULL DEFAULT 0 CHECK (parsed_count >= 0),
            parse_failed INTEGER NOT NULL DEFAULT 0 CHECK (parse_failed >= 0),
            mentions_written INTEGER NOT NULL DEFAULT 0
                CHECK (mentions_written >= 0),
            summaries_updated INTEGER NOT NULL DEFAULT 0
                CHECK (summaries_updated >= 0),
            summaries_failed INTEGER NOT NULL DEFAULT 0
                CHECK (summaries_failed >= 0),
            prices_updated INTEGER NOT NULL DEFAULT 0
                CHECK (prices_updated >= 0),
            prices_failed INTEGER NOT NULL DEFAULT 0
                CHECK (prices_failed >= 0),
            alias_candidates_found INTEGER NOT NULL DEFAULT 0
                CHECK (alias_candidates_found >= 0),
            latest_source_timestamp TEXT,
            failed_stage TEXT,
            failure_kind TEXT,
            failure_code INTEGER,
            error_message TEXT,
            site_generated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ticker_snapshots (
            ticker TEXT PRIMARY KEY,
            source_fingerprint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL
                CHECK (status IN ('completed', 'failed')),
            latest_sentiment TEXT
                CHECK (latest_sentiment IN ('Bullish', 'Bearish', 'Neutral')),
            summary TEXT,
            evolution_json TEXT NOT NULL DEFAULT '[]',
            key_points_json TEXT NOT NULL DEFAULT '[]',
            risks_json TEXT NOT NULL DEFAULT '[]',
            source_post_ids_json TEXT NOT NULL DEFAULT '[]',
            covered_post_ids_json TEXT NOT NULL DEFAULT '[]',
            analysis_version TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error TEXT,
            generated_at TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ticker_alias_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_ticker TEXT NOT NULL,
            alias TEXT NOT NULL,
            reason TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            reviewed_at TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            UNIQUE (canonical_ticker, alias)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ticker_price_profiles (
            ticker TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'yfinance',
            provider_symbol TEXT NOT NULL,
            currency TEXT,
            first_mention_date TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('completed', 'failed', 'unsupported')),
            last_attempt_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            ticker TEXT NOT NULL,
            price_date TEXT NOT NULL,
            adjusted_close REAL NOT NULL CHECK (adjusted_close > 0),
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (ticker, price_date),
            FOREIGN KEY (ticker) REFERENCES ticker_price_profiles(ticker)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS qa_cache (
            question_hash TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            answer_json TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_posts_status_timestamp
            ON posts(parse_status, timestamp DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_mentions_ticker
            ON mentions(ticker)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_overrides_reviewed_at
            ON mention_overrides(reviewed_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_status
            ON ticker_snapshots(status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_alias_candidates_status_last_seen
            ON ticker_alias_candidates(status, last_seen_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_price_profiles_status_attempt
            ON ticker_price_profiles(status, last_attempt_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_qa_cache_accessed
            ON qa_cache(last_accessed_at DESC)
        """,
    )
    for statement in statements:
        connection.execute(statement)
    connection.execute("PRAGMA optimize")


def _upgrade_current_schema(
    connection: sqlite3.Connection,
    db_path: Path,
    backup_dir: Path | None,
) -> Path | None:
    """Add non-destructive columns required by newer application versions."""
    posts_columns = _table_columns(connection, "posts")
    mentions_columns = _table_columns(connection, "mentions")
    needs_post_context = bool(posts_columns) and "context" not in posts_columns
    needs_sentiment_evidence = (
        bool(mentions_columns)
        and "post_id" in mentions_columns
        and "sentiment_evidence" not in mentions_columns
    )
    pipeline_columns = _table_columns(connection, "pipeline_runs")
    needs_failure_kind = (
        bool(pipeline_columns) and "failure_kind" not in pipeline_columns
    )
    needs_failure_code = (
        bool(pipeline_columns) and "failure_code" not in pipeline_columns
    )
    needs_prices_updated = (
        bool(pipeline_columns) and "prices_updated" not in pipeline_columns
    )
    needs_prices_failed = (
        bool(pipeline_columns) and "prices_failed" not in pipeline_columns
    )
    if not any(
        (
            needs_post_context,
            needs_sentiment_evidence,
            needs_failure_kind,
            needs_failure_code,
            needs_prices_updated,
            needs_prices_failed,
        )
    ):
        return None

    backup_path = _backup_database(db_path, backup_dir)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if needs_post_context:
            connection.execute(
                "ALTER TABLE posts ADD COLUMN context TEXT NOT NULL DEFAULT ''"
            )
        if needs_sentiment_evidence:
            connection.execute(
                """
                ALTER TABLE mentions
                ADD COLUMN sentiment_evidence TEXT NOT NULL DEFAULT ''
                """
            )
        if needs_failure_kind:
            connection.execute("ALTER TABLE pipeline_runs ADD COLUMN failure_kind TEXT")
        if needs_failure_code:
            connection.execute("ALTER TABLE pipeline_runs ADD COLUMN failure_code INTEGER")
        if needs_prices_updated:
            connection.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN prices_updated "
                "INTEGER NOT NULL DEFAULT 0 CHECK (prices_updated >= 0)"
            )
        if needs_prices_failed:
            connection.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN prices_failed "
                "INTEGER NOT NULL DEFAULT 0 CHECK (prices_failed >= 0)"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return backup_path


def _backup_database(db_path: Path, backup_dir: Path | None = None) -> Path:
    backup_dir = backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}-pre-migration-{stamp}.db"
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / (
            f"{db_path.stem}-pre-migration-{stamp}-{counter}.db"
        )
        counter += 1
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def _migrate_legacy_mentions(
    connection: sqlite3.Connection,
    db_path: Path,
    backup_dir: Path | None,
) -> tuple[int, Path]:
    """Migrate the original flat mentions table and keep its earliest analysis."""
    backup_path = _backup_database(db_path, backup_dir)
    legacy_rows = connection.execute(
        """
        SELECT id, timestamp, ticker, sentiment, thesis, risks, url
        FROM mentions
        ORDER BY id ASC
        """
    ).fetchall()

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE mentions RENAME TO mentions_legacy")
        _create_schema(connection)
        now = utc_now()
        seen: set[tuple[str, str]] = set()

        for row in legacy_rows:
            post_id = extract_post_id(row["url"] or "")
            ticker = str(row["ticker"] or "").strip().upper()
            sentiment = str(row["sentiment"] or "").strip().title()
            if not post_id or not ticker or sentiment not in VALID_SENTIMENTS:
                continue

            connection.execute(
                """
                INSERT OR IGNORE INTO posts (
                    post_id, timestamp, text, url, parse_status, parse_error,
                    parse_attempts, parsed_at, analysis_version, created_at, updated_at
                ) VALUES (?, ?, '', ?, 'completed', NULL, 1, ?, 'legacy-v1', ?, ?)
                """,
                (post_id, row["timestamp"] or "", row["url"], now, now, now),
            )

            identity = (post_id, ticker)
            if identity in seen:
                continue
            seen.add(identity)
            connection.execute(
                """
                INSERT INTO mentions (
                    post_id, ticker, sentiment, thesis, risks,
                    analysis_version, created_at
                ) VALUES (?, ?, ?, ?, ?, 'legacy-v1', ?)
                """,
                (
                    post_id,
                    ticker,
                    sentiment,
                    str(row["thesis"] or "").strip(),
                    row["risks"],
                    now,
                ),
            )

        connection.execute("DROP TABLE mentions_legacy")
        _create_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return len(legacy_rows) - len(seen), backup_path


def init_db(
    db_path: Path = DB_PATH,
    *,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    """Create the current schema or safely migrate the original flat schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_db(db_path)
    migration: dict[str, object] = {
        "migrated": False,
        "schema_updated": False,
        "removed_duplicates": 0,
    }
    try:
        mentions_columns = _table_columns(connection, "mentions")
        if mentions_columns and "post_id" not in mentions_columns:
            removed, backup_path = _migrate_legacy_mentions(
                connection, db_path, backup_dir
            )
            migration.update(
                migrated=True,
                removed_duplicates=removed,
                backup_path=backup_path,
            )
        else:
            _create_schema(connection)
            connection.commit()
            backup_path = _upgrade_current_schema(connection, db_path, backup_dir)
            if backup_path is not None:
                migration.update(schema_updated=True, backup_path=backup_path)
    finally:
        connection.close()
    return migration


def register_posts(
    connection: sqlite3.Connection,
    posts: Iterable[Mapping[str, str]],
) -> int:
    """Upsert source fields and return the number of processed post records."""
    now = utc_now()
    processed = 0
    for post in posts:
        post_id = str(post["post_id"])
        connection.execute(
            """
            INSERT INTO posts (
                post_id, timestamp, text, context, url, parse_status, parse_error,
                parse_attempts, parsed_at, analysis_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, 0, NULL, NULL, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                timestamp = excluded.timestamp,
                text = excluded.text,
                context = excluded.context,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (
                post_id,
                post["timestamp"],
                post["text"],
                post.get("context", ""),
                post["url"],
                now,
                now,
            ),
        )
        processed += 1
    connection.commit()
    return processed


def pending_posts(
    connection: sqlite3.Connection,
    limit: int,
    post_ids: Sequence[str] | None = None,
) -> list[sqlite3.Row]:
    post_filter = ""
    parameters: list[object] = []
    if post_ids:
        placeholders = ",".join("?" for _ in post_ids)
        post_filter = f" AND post_id IN ({placeholders})"
        parameters.extend(post_ids)
    parameters.append(limit)
    return connection.execute(
        f"""
        SELECT post_id, timestamp, text, context, url, parse_attempts
        FROM posts
        WHERE parse_status IN ('pending', 'failed')
        {post_filter}
        ORDER BY timestamp DESC, post_id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def mark_posts_pending(
    connection: sqlite3.Connection,
    post_ids: Sequence[str],
) -> int:
    """Queue selected posts without deleting their last successful analysis."""
    unique_ids = list(dict.fromkeys(post_ids))
    if not unique_ids:
        return 0
    placeholders = ",".join("?" for _ in unique_ids)
    cursor = connection.execute(
        f"""
        UPDATE posts
        SET parse_status = 'pending', parse_error = NULL, updated_at = ?
        WHERE post_id IN ({placeholders})
        """,
        [utc_now(), *unique_ids],
    )
    connection.commit()
    return int(cursor.rowcount)


def set_mention_override(
    connection: sqlite3.Connection,
    *,
    post_id: str,
    ticker: str,
    sentiment: str,
    sentiment_evidence: str,
    thesis: str,
    risks: str | None,
    review_note: str,
    reviewer: str = "manual",
) -> None:
    """Persist a human-reviewed effective analysis without changing model output."""
    normalized_ticker = ticker.strip().upper()
    normalized_sentiment = sentiment.strip().title()
    normalized_thesis = thesis.strip()
    if normalized_sentiment not in VALID_SENTIMENTS:
        raise ValueError(f"無效 sentiment：{sentiment}")
    if not normalized_thesis:
        raise ValueError("人工覆核 thesis 不可為空")
    identity = connection.execute(
        "SELECT 1 FROM mentions WHERE post_id = ? AND ticker = ?",
        (post_id, normalized_ticker),
    ).fetchone()
    if identity is None:
        raise ValueError(f"找不到可覆核的分析：{post_id}/{normalized_ticker}")
    connection.execute(
        """
        INSERT INTO mention_overrides (
            post_id, ticker, sentiment, sentiment_evidence, thesis, risks,
            review_note, reviewer, reviewed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id, ticker) DO UPDATE SET
            sentiment = excluded.sentiment,
            sentiment_evidence = excluded.sentiment_evidence,
            thesis = excluded.thesis,
            risks = excluded.risks,
            review_note = excluded.review_note,
            reviewer = excluded.reviewer,
            reviewed_at = excluded.reviewed_at
        """,
        (
            post_id,
            normalized_ticker,
            normalized_sentiment,
            sentiment_evidence.strip(),
            normalized_thesis,
            risks.strip() if risks else None,
            review_note.strip(),
            reviewer.strip() or "manual",
            utc_now(),
        ),
    )
    connection.commit()


def delete_mention_override(
    connection: sqlite3.Connection,
    post_id: str,
    ticker: str,
) -> bool:
    cursor = connection.execute(
        "DELETE FROM mention_overrides WHERE post_id = ? AND ticker = ?",
        (post_id, ticker.strip().upper()),
    )
    connection.commit()
    return bool(cursor.rowcount)


def list_mention_overrides(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            o.post_id, o.ticker, o.sentiment, o.sentiment_evidence,
            o.thesis, o.risks, o.review_note, o.reviewer, o.reviewed_at,
            p.timestamp, p.url
        FROM mention_overrides AS o
        JOIN posts AS p ON p.post_id = o.post_id
        ORDER BY o.reviewed_at DESC, o.post_id DESC, o.ticker ASC
        """
    ).fetchall()


def start_pipeline_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        """
        INSERT INTO pipeline_runs (started_at, status, current_stage)
        VALUES (?, 'running', 'database')
        """,
        (utc_now(),),
    )
    connection.commit()
    return int(cursor.lastrowid)


def mark_abandoned_pipeline_runs(connection: sqlite3.Connection) -> int:
    """Close runs left open after an OS kill or interrupted terminal session."""
    cursor = connection.execute(
        """
        UPDATE pipeline_runs
        SET finished_at = ?, status = 'failed', failed_stage = current_stage,
            failure_kind = 'interrupted',
            error_message = '前次程序在完成前中斷', current_stage = 'finished'
        WHERE status = 'running'
        """,
        (utc_now(),),
    )
    connection.commit()
    return int(cursor.rowcount)


def update_pipeline_stage(
    connection: sqlite3.Connection,
    run_id: int,
    stage: str,
) -> None:
    connection.execute(
        "UPDATE pipeline_runs SET current_stage = ? WHERE id = ?",
        (stage, run_id),
    )
    connection.commit()


def record_pipeline_scrape(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    scraped_count: int,
    long_posts_attempted: int,
    long_posts_succeeded: int,
    long_posts_failed: int,
    latest_source_timestamp: str | None,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET scraped_count = ?, long_posts_attempted = ?,
            long_posts_succeeded = ?, long_posts_failed = ?,
            latest_source_timestamp = ?
        WHERE id = ?
        """,
        (
            scraped_count,
            long_posts_attempted,
            long_posts_succeeded,
            long_posts_failed,
            latest_source_timestamp,
            run_id,
        ),
    )
    connection.commit()


def record_pipeline_parse(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    parsed_count: int,
    parse_failed: int,
    mentions_written: int,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET parsed_count = ?, parse_failed = ?, mentions_written = ?
        WHERE id = ?
        """,
        (parsed_count, parse_failed, mentions_written, run_id),
    )
    connection.commit()


def record_pipeline_summaries(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    updated: int,
    failed: int,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET summaries_updated = ?, summaries_failed = ?
        WHERE id = ?
        """,
        (updated, failed, run_id),
    )
    connection.commit()


def record_pipeline_alias_candidates(
    connection: sqlite3.Connection,
    run_id: int,
    found: int,
) -> None:
    connection.execute(
        "UPDATE pipeline_runs SET alias_candidates_found = ? WHERE id = ?",
        (found, run_id),
    )
    connection.commit()


def record_pipeline_prices(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    updated: int,
    failed: int,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET prices_updated = ?, prices_failed = ?
        WHERE id = ?
        """,
        (updated, failed, run_id),
    )
    connection.commit()


def record_pipeline_issue(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    stage: str,
    failure_kind: str,
    error_message: str,
    failure_code: int | None = None,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET failed_stage = ?, failure_kind = ?, failure_code = ?,
            error_message = ?
        WHERE id = ?
        """,
        (
            stage,
            failure_kind,
            failure_code,
            error_message[:1000],
            run_id,
        ),
    )
    connection.commit()


def finish_pipeline_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    failed_stage: str | None = None,
    failure_kind: str | None = None,
    failure_code: int | None = None,
    error_message: str | None = None,
    site_generated_at: str | None = None,
) -> None:
    if status not in {"success", "partial", "failed"}:
        raise ValueError(f"無效 pipeline status：{status}")
    connection.execute(
        """
        UPDATE pipeline_runs
        SET finished_at = ?, status = ?, current_stage = 'finished',
            failed_stage = COALESCE(?, failed_stage),
            failure_kind = COALESCE(?, failure_kind),
            failure_code = COALESCE(?, failure_code),
            error_message = COALESCE(?, error_message),
            site_generated_at = COALESCE(?, site_generated_at)
        WHERE id = ?
        """,
        (
            utc_now(),
            status,
            failed_stage,
            failure_kind,
            failure_code,
            (error_message or "")[:1000] or None,
            site_generated_at,
            run_id,
        ),
    )
    connection.commit()


def get_pipeline_run(
    connection: sqlite3.Connection,
    run_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM pipeline_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def recent_pipeline_runs(
    connection: sqlite3.Connection,
    limit: int = 5,
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_ticker_snapshots(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM ticker_snapshots ORDER BY ticker"
    ).fetchall()


def get_price_profiles(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM ticker_price_profiles ORDER BY ticker"
    ).fetchall()


def save_market_prices(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    provider_symbol: str,
    currency: str | None,
    first_mention_date: str,
    prices: Iterable[Mapping[str, object]],
) -> int:
    normalized_ticker = ticker.strip().upper()
    normalized_symbol = provider_symbol.strip().upper()
    now = utc_now()
    rows = []
    for item in prices:
        price_date = str(item["date"])
        adjusted_close = float(item["adjusted_close"])
        if adjusted_close <= 0:
            continue
        rows.append((normalized_ticker, price_date, adjusted_close, now))
    if not rows:
        raise ValueError(f"{normalized_ticker} 沒有有效價格資料")

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO ticker_price_profiles (
                ticker, provider, provider_symbol, currency,
                first_mention_date, status, last_attempt_at,
                last_success_at, last_error
            ) VALUES (?, 'yfinance', ?, ?, ?, 'completed', ?, ?, NULL)
            ON CONFLICT(ticker) DO UPDATE SET
                provider = 'yfinance',
                provider_symbol = excluded.provider_symbol,
                currency = excluded.currency,
                first_mention_date = excluded.first_mention_date,
                status = 'completed',
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_error = NULL
            """,
            (
                normalized_ticker,
                normalized_symbol,
                currency.strip().upper() if currency else None,
                first_mention_date,
                now,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO market_prices (
                ticker, price_date, adjusted_close, fetched_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker, price_date) DO UPDATE SET
                adjusted_close = excluded.adjusted_close,
                fetched_at = excluded.fetched_at
            """,
            rows,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(rows)


def mark_price_refresh_failed(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    provider_symbol: str,
    first_mention_date: str,
    error: str,
    unsupported: bool = False,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO ticker_price_profiles (
            ticker, provider, provider_symbol, first_mention_date,
            status, last_attempt_at, last_error
        ) VALUES (?, 'yfinance', ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            provider = 'yfinance',
            provider_symbol = excluded.provider_symbol,
            first_mention_date = excluded.first_mention_date,
            status = excluded.status,
            last_attempt_at = excluded.last_attempt_at,
            last_error = excluded.last_error
        """,
        (
            ticker.strip().upper(),
            provider_symbol.strip().upper(),
            first_mention_date,
            "unsupported" if unsupported else "failed",
            now,
            error[:1000],
        ),
    )
    connection.commit()


def get_market_price_data(
    connection: sqlite3.Connection,
    ticker: str,
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    normalized = ticker.strip().upper()
    profile = connection.execute(
        "SELECT * FROM ticker_price_profiles WHERE ticker = ?",
        (normalized,),
    ).fetchone()
    prices = connection.execute(
        """
        SELECT price_date, adjusted_close
        FROM market_prices
        WHERE ticker = ?
        ORDER BY price_date
        """,
        (normalized,),
    ).fetchall()
    return profile, prices


def get_cached_qa(
    connection: sqlite3.Connection,
    *,
    question_hash: str,
    context_fingerprint: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT * FROM qa_cache
        WHERE question_hash = ? AND context_fingerprint = ?
        """,
        (question_hash, context_fingerprint),
    ).fetchone()
    if row is not None:
        connection.execute(
            """
            UPDATE qa_cache
            SET last_accessed_at = ?, hit_count = hit_count + 1
            WHERE question_hash = ?
            """,
            (utc_now(), question_hash),
        )
        connection.commit()
    return row


def save_qa_cache(
    connection: sqlite3.Connection,
    *,
    question_hash: str,
    question: str,
    context_fingerprint: str,
    answer_json: str,
    model: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO qa_cache (
            question_hash, question, context_fingerprint, answer_json,
            model, created_at, last_accessed_at, hit_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(question_hash) DO UPDATE SET
            question = excluded.question,
            context_fingerprint = excluded.context_fingerprint,
            answer_json = excluded.answer_json,
            model = excluded.model,
            created_at = excluded.created_at,
            last_accessed_at = excluded.last_accessed_at,
            hit_count = 0
        """,
        (
            question_hash,
            question,
            context_fingerprint,
            answer_json,
            model,
            now,
            now,
        ),
    )
    connection.commit()


def save_ticker_snapshot(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    source_fingerprint: str,
    latest_sentiment: str,
    summary: str,
    evolution_json: str,
    key_points_json: str,
    risks_json: str,
    source_post_ids_json: str,
    covered_post_ids_json: str,
    analysis_version: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO ticker_snapshots (
            ticker, source_fingerprint, status, latest_sentiment, summary,
            evolution_json, key_points_json, risks_json,
            source_post_ids_json, covered_post_ids_json, analysis_version,
            attempts, last_error, generated_at, updated_at
        ) VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            source_fingerprint = excluded.source_fingerprint,
            status = 'completed',
            latest_sentiment = excluded.latest_sentiment,
            summary = excluded.summary,
            evolution_json = excluded.evolution_json,
            key_points_json = excluded.key_points_json,
            risks_json = excluded.risks_json,
            source_post_ids_json = excluded.source_post_ids_json,
            covered_post_ids_json = excluded.covered_post_ids_json,
            analysis_version = excluded.analysis_version,
            attempts = ticker_snapshots.attempts + 1,
            last_error = NULL,
            generated_at = excluded.generated_at,
            updated_at = excluded.updated_at
        """,
        (
            ticker.strip().upper(),
            source_fingerprint,
            latest_sentiment,
            summary.strip(),
            evolution_json,
            key_points_json,
            risks_json,
            source_post_ids_json,
            covered_post_ids_json,
            analysis_version,
            now,
            now,
        ),
    )
    connection.commit()


def mark_ticker_snapshot_failed(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    error: str,
    analysis_version: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO ticker_snapshots (
            ticker, status, analysis_version, attempts, last_error, updated_at
        ) VALUES (?, 'failed', ?, 1, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            status = 'failed',
            attempts = ticker_snapshots.attempts + 1,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (ticker.strip().upper(), analysis_version, error[:1000], now),
    )
    connection.commit()


def upsert_alias_candidate(
    connection: sqlite3.Connection,
    *,
    canonical_ticker: str,
    alias: str,
    reason: str,
    confidence: float,
) -> bool:
    now = utc_now()
    existing = connection.execute(
        """
        SELECT id FROM ticker_alias_candidates
        WHERE canonical_ticker = ? AND alias = ?
        """,
        (canonical_ticker, alias),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO ticker_alias_candidates (
            canonical_ticker, alias, reason, confidence,
            status, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(canonical_ticker, alias) DO UPDATE SET
            reason = excluded.reason,
            confidence = excluded.confidence,
            last_seen_at = excluded.last_seen_at
        """,
        (canonical_ticker, alias, reason, confidence, now, now),
    )
    connection.commit()
    return existing is None


def list_alias_candidates(
    connection: sqlite3.Connection,
    status: str | None = "pending",
) -> list[sqlite3.Row]:
    if status is None:
        return connection.execute(
            "SELECT * FROM ticker_alias_candidates ORDER BY last_seen_at DESC, id"
        ).fetchall()
    return connection.execute(
        """
        SELECT * FROM ticker_alias_candidates
        WHERE status = ?
        ORDER BY confidence DESC, last_seen_at DESC, id
        """,
        (status,),
    ).fetchall()


def review_alias_candidate(
    connection: sqlite3.Connection,
    candidate_id: int,
    *,
    status: str,
    note: str = "",
) -> sqlite3.Row | None:
    if status not in {"approved", "rejected"}:
        raise ValueError(f"無效 alias review status：{status}")
    candidate = connection.execute(
        "SELECT * FROM ticker_alias_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        return None
    connection.execute(
        """
        UPDATE ticker_alias_candidates
        SET status = ?, reviewed_at = ?, review_note = ?
        WHERE id = ?
        """,
        (status, utc_now(), note.strip(), candidate_id),
    )
    connection.commit()
    return candidate


def store_analysis(
    connection: sqlite3.Connection,
    post_id: str,
    mentions: Sequence[Mapping[str, str | None]],
    analysis_version: str,
) -> None:
    """Atomically replace one post's analysis and mark it complete."""
    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM mentions WHERE post_id = ?", (post_id,))
        for mention in mentions:
            connection.execute(
                """
                INSERT INTO mentions (
                    post_id, ticker, sentiment, sentiment_evidence, thesis, risks,
                    analysis_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    mention["ticker"],
                    mention["sentiment"],
                    mention.get("sentiment_evidence", ""),
                    mention["thesis"],
                    mention.get("risks"),
                    analysis_version,
                    now,
                ),
            )
        connection.execute(
            """
            UPDATE posts
            SET parse_status = 'completed', parse_error = NULL,
                parse_attempts = parse_attempts + 1, parsed_at = ?,
                analysis_version = ?, updated_at = ?
            WHERE post_id = ?
            """,
            (now, analysis_version, now, post_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def mark_parse_failed(
    connection: sqlite3.Connection,
    post_id: str,
    error: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        UPDATE posts
        SET parse_status = 'failed', parse_error = ?,
            parse_attempts = parse_attempts + 1, updated_at = ?
        WHERE post_id = ?
        """,
        (error[:1000], now, post_id),
    )
    connection.commit()


def database_counts(db_path: Path = DB_PATH) -> dict[str, int]:
    connection = connect_db(db_path)
    try:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM posts) AS posts,
                (SELECT COUNT(*) FROM mentions) AS mentions,
                (SELECT COUNT(*) FROM posts WHERE parse_status = 'pending') AS pending,
                (SELECT COUNT(*) FROM posts WHERE parse_status = 'failed') AS failed,
                (SELECT COUNT(*) FROM mention_overrides) AS overrides,
                (SELECT COUNT(*) FROM pipeline_runs) AS pipeline_runs,
                (SELECT COUNT(*) FROM ticker_snapshots
                    WHERE summary IS NOT NULL) AS snapshots,
                (SELECT COUNT(*) FROM ticker_alias_candidates
                    WHERE status = 'pending') AS alias_candidates,
                (SELECT COUNT(*) FROM ticker_price_profiles
                    WHERE status = 'completed') AS price_tickers,
                (SELECT COUNT(*) FROM market_prices) AS price_points,
                (SELECT COUNT(*) FROM qa_cache) AS qa_cache
            """
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}
    finally:
        connection.close()
