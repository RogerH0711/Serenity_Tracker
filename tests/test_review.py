import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import review
from storage import connect_db, init_db, register_posts, store_analysis


class ReviewCliTest(unittest.TestCase):
    def test_set_list_and_delete_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "review.db"
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                register_posts(
                    connection,
                    [
                        {
                            "post_id": "123",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$MU source",
                            "url": "https://x.com/example/status/123",
                        }
                    ],
                )
                store_analysis(
                    connection,
                    "123",
                    [
                        {
                            "ticker": "MU",
                            "sentiment": "Neutral",
                            "sentiment_evidence": "$MU source",
                            "thesis": "模型結果",
                            "risks": None,
                        }
                    ],
                    "test-v1",
                )
            finally:
                connection.close()

            output = io.StringIO()
            with (
                patch.object(review, "DB_PATH", db_path),
                patch(
                    "sys.argv",
                    [
                        "review.py",
                        "set",
                        "123",
                        "MU",
                        "--sentiment",
                        "Bullish",
                        "--thesis",
                        "人工看多",
                        "--evidence",
                        "$MU source",
                        "--note",
                        "人工確認",
                    ],
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(review.main(), 0)
            self.assertIn("人工覆核已儲存", output.getvalue())

            output = io.StringIO()
            with (
                patch.object(review, "DB_PATH", db_path),
                patch("sys.argv", ["review.py", "list"]),
                redirect_stdout(output),
            ):
                self.assertEqual(review.main(), 0)
            self.assertIn("123/MU", output.getvalue())

            with (
                patch.object(review, "DB_PATH", db_path),
                patch("sys.argv", ["review.py", "delete", "123", "MU"]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(review.main(), 0)


if __name__ == "__main__":
    unittest.main()
