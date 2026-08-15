import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

from build_site import TEMPLATE_PATH, load_dashboard_data
from review_site import (
    AI_REQUESTS_PER_MINUTE,
    ReviewApplication,
    ReviewRequestHandler,
    remove_review,
    save_review,
)
from storage import connect_db, init_db, register_posts, store_analysis


class ReviewSiteTest(unittest.TestCase):
    def test_review_save_and_remove_rebuild_the_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "review.db"
            aliases_path = root / "aliases.json"
            output_path = root / "index.html"
            aliases_path.write_text("{}", encoding="utf-8")
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

            payload = {
                "post_id": "123",
                "ticker": "MU",
                "sentiment": "Bullish",
                "thesis": "人工確認看多",
                "sentiment_evidence": "$MU source",
                "risks": "供應風險",
                "review_note": "已閱讀原文",
                "reviewer": "roger",
            }
            save_review(
                payload,
                db_path=db_path,
                template_path=TEMPLATE_PATH,
                aliases_path=aliases_path,
                output_path=output_path,
            )
            self.assertTrue(output_path.exists())

            data = load_dashboard_data(db_path, aliases_path)
            post = data["tickers"][0]["posts"][0]
            self.assertTrue(post["has_override"])
            self.assertEqual(post["sentiment"], "Bullish")
            self.assertEqual(post["reviewer"], "roger")

            remove_review(
                payload,
                db_path=db_path,
                template_path=TEMPLATE_PATH,
                aliases_path=aliases_path,
                output_path=output_path,
            )
            data = load_dashboard_data(db_path, aliases_path)
            self.assertFalse(data["tickers"][0]["posts"][0]["has_override"])

    def test_review_request_requires_same_origin_and_session_token(self) -> None:
        application = ReviewApplication(
            db_path=Path("review.db"),
            template_path=TEMPLATE_PATH,
            aliases_path=Path("aliases.json"),
            output_path=Path("index.html"),
            token="test-token",
        )
        handler = object.__new__(ReviewRequestHandler)
        handler.server = SimpleNamespace(server_port=8765, application=application)
        headers = Message()
        headers["Origin"] = "http://127.0.0.1:8765"
        headers["Content-Type"] = "application/json"
        headers["X-Review-Token"] = "test-token"
        handler.headers = headers
        self.assertTrue(handler._authorized())

        headers.replace_header("X-Review-Token", "wrong-token")
        self.assertFalse(handler._authorized())

    def test_local_ai_rate_limit_is_bounded(self) -> None:
        application = ReviewApplication(
            db_path=Path("review.db"),
            template_path=TEMPLATE_PATH,
            aliases_path=Path("aliases.json"),
            output_path=Path("index.html"),
            token="test-token",
        )
        self.assertTrue(
            all(application.allow_ai_request() for _ in range(AI_REQUESTS_PER_MINUTE))
        )
        self.assertFalse(application.allow_ai_request())


if __name__ == "__main__":
    unittest.main()
