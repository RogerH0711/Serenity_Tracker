import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import (
    connect_db,
    database_counts,
    init_db,
    mark_posts_pending,
    pending_posts,
    register_posts,
    store_analysis,
)


class StorageMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.db_path = self.root / "legacy.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            CREATE TABLE mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                sentiment TEXT,
                thesis TEXT,
                risks TEXT,
                url TEXT
            )
            """
        )
        rows = [
            (
                "2026-08-14T11:29:05.000Z",
                "SNDK",
                "Bearish",
                "first analysis",
                None,
                "https://x.com/example/status/123",
            ),
            (
                "2026-08-14T11:29:05.000Z",
                "SNDK",
                "Bearish",
                "duplicate analysis",
                "invented risk",
                "https://x.com/example/status/123",
            ),
        ]
        connection.executemany(
            """
            INSERT INTO mentions (timestamp, ticker, sentiment, thesis, risks, url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migrates_and_keeps_one_identity(self) -> None:
        result = init_db(self.db_path, backup_dir=self.root / "backups")
        self.assertTrue(result["migrated"])
        self.assertEqual(result["removed_duplicates"], 1)
        self.assertTrue(Path(result["backup_path"]).exists())

        connection = connect_db(self.db_path)
        try:
            mention = connection.execute(
                "SELECT post_id, ticker, thesis FROM mentions"
            ).fetchone()
            self.assertEqual(dict(mention), {
                "post_id": "123",
                "ticker": "SNDK",
                "thesis": "first analysis",
            })
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO mentions (
                        post_id, ticker, sentiment, thesis, risks,
                        analysis_version, created_at
                    ) VALUES ('123', 'SNDK', 'Neutral', 'again', NULL, 'v2', 'now')
                    """
                )
        finally:
            connection.close()

    def test_existing_post_is_not_automatically_requeued(self) -> None:
        init_db(self.db_path, backup_dir=self.root / "backups")
        post = {
            "post_id": "123",
            "timestamp": "2026-08-14T11:29:05.000Z",
            "text": "$SNDK source text",
            "url": "https://x.com/example/status/123",
        }
        connection = connect_db(self.db_path)
        try:
            register_posts(connection, [post])
            self.assertEqual(pending_posts(connection, 10), [])
            store_analysis(
                connection,
                "123",
                [
                    {
                        "ticker": "SNDK",
                        "sentiment": "Neutral",
                        "thesis": "source-only analysis",
                        "risks": None,
                    }
                ],
                "explicit-v2",
            )
            register_posts(connection, [post])
            self.assertEqual(pending_posts(connection, 10), [])
        finally:
            connection.close()

        counts = database_counts(self.db_path)
        self.assertEqual(counts["posts"], 1)
        self.assertEqual(counts["mentions"], 1)
        self.assertEqual(counts["pending"], 0)

    def test_explicit_reparse_preserves_last_analysis_until_replacement(self) -> None:
        init_db(self.db_path, backup_dir=self.root / "backups")
        connection = connect_db(self.db_path)
        try:
            self.assertEqual(mark_posts_pending(connection, ["123"]), 1)
            queue = pending_posts(connection, 10, ["123"])
            self.assertEqual([row["post_id"] for row in queue], ["123"])
            mention = connection.execute(
                "SELECT thesis FROM mentions WHERE post_id = '123'"
            ).fetchone()
            self.assertEqual(mention["thesis"], "first analysis")
        finally:
            connection.close()

    def test_upgrades_v2_schema_with_context_and_evidence_columns(self) -> None:
        current_path = self.root / "current-v2.db"
        connection = sqlite3.connect(current_path)
        connection.executescript(
            """
            CREATE TABLE posts (
                post_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL UNIQUE,
                parse_status TEXT NOT NULL DEFAULT 'pending',
                parse_error TEXT,
                parse_attempts INTEGER NOT NULL DEFAULT 0,
                parsed_at TEXT,
                analysis_version TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                thesis TEXT NOT NULL,
                risks TEXT,
                analysis_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (post_id, ticker)
            );
            """
        )
        connection.commit()
        connection.close()

        result = init_db(current_path, backup_dir=self.root / "backups")
        self.assertTrue(result["schema_updated"])
        self.assertTrue(Path(result["backup_path"]).exists())
        connection = connect_db(current_path)
        try:
            post_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(posts)")
            }
            mention_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(mentions)")
            }
            self.assertIn("context", post_columns)
            self.assertIn("sentiment_evidence", mention_columns)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
