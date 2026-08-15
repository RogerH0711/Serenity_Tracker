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
        CREATE INDEX IF NOT EXISTS idx_posts_status_timestamp
            ON posts(parse_status, timestamp DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_mentions_ticker
            ON mentions(ticker)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _upgrade_current_schema(
    connection: sqlite3.Connection,
    db_path: Path,
    backup_dir: Path | None,
) -> Path | None:
    """Add non-destructive context/evidence columns to an existing v2 database."""
    posts_columns = _table_columns(connection, "posts")
    mentions_columns = _table_columns(connection, "mentions")
    needs_post_context = bool(posts_columns) and "context" not in posts_columns
    needs_sentiment_evidence = (
        bool(mentions_columns)
        and "post_id" in mentions_columns
        and "sentiment_evidence" not in mentions_columns
    )
    if not needs_post_context and not needs_sentiment_evidence:
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
                (SELECT COUNT(*) FROM posts WHERE parse_status = 'failed') AS failed
            """
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}
    finally:
        connection.close()
