import json
import tempfile
import unittest
from pathlib import Path

from build_site import TEMPLATE_PATH, build_static_site, load_dashboard_data
from storage import (
    connect_db,
    init_db,
    mark_parse_failed,
    register_posts,
    set_mention_override,
    store_analysis,
)


class StaticSiteSafetyTest(unittest.TestCase):
    def test_external_content_cannot_close_json_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "site.db"
            output_path = root / "index.html"
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                register_posts(
                    connection,
                    [
                        {
                            "post_id": "999",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$NVDA test",
                            "url": "https://x.com/example/status/999",
                        }
                    ],
                )
                store_analysis(
                    connection,
                    "999",
                    [
                        {
                            "ticker": "NVDA",
                            "sentiment": "Neutral",
                            "thesis": "</script><script>alert(1)</script>",
                            "risks": "<img src=x onerror=alert(2)>",
                        }
                    ],
                    "test-v1",
                )
                mark_parse_failed(connection, "999", "temporary API failure")
            finally:
                connection.close()

            build_static_site(
                db_path=db_path,
                template_path=TEMPLATE_PATH,
                output_path=output_path,
            )
            html = output_path.read_text(encoding="utf-8")
            self.assertNotIn("</script><script>alert(1)</script>", html)
            self.assertNotIn("<img src=x onerror=alert(2)>", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertNotIn("innerHTML", html)

    def test_periods_aliases_and_research_summaries_are_precomputed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "site.db"
            aliases_path = root / "aliases.json"
            aliases_path.write_text(
                json.dumps(
                    {
                        "SIVE": {
                            "aliases": ["SIVEF"],
                            "company_name": "Sivers Semiconductors",
                            "exchange": ""
                        }
                    }
                ),
                encoding="utf-8",
            )
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                posts = [
                    {
                        "post_id": "3",
                        "timestamp": "2026-08-14T12:00:00Z",
                        "text": "$SIVEF latest",
                        "url": "https://x.com/example/status/3",
                    },
                    {
                        "post_id": "2",
                        "timestamp": "2026-08-10T12:00:00Z",
                        "text": "$SIVE earlier",
                        "url": "https://x.com/example/status/2",
                    },
                    {
                        "post_id": "1",
                        "timestamp": "2026-07-01T12:00:00Z",
                        "text": "$AXTI old",
                        "url": "https://x.com/example/status/1",
                    },
                ]
                register_posts(connection, posts)
                store_analysis(
                    connection,
                    "3",
                    [{"ticker": "SIVEF", "sentiment": "Bullish", "thesis": "需求增強", "risks": "供應風險"}],
                    "test-v1",
                )
                store_analysis(
                    connection,
                    "2",
                    [{"ticker": "SIVE", "sentiment": "Neutral", "sentiment_evidence": "$SIVE earlier", "thesis": "等待驗證", "risks": None}],
                    "context-aware-v3",
                )
                store_analysis(
                    connection,
                    "1",
                    [{"ticker": "AXTI", "sentiment": "Bearish", "thesis": "舊論點", "risks": None}],
                    "legacy-v1",
                )
                set_mention_override(
                    connection,
                    post_id="3",
                    ticker="SIVEF",
                    sentiment="Neutral",
                    sentiment_evidence="人工查核原文",
                    thesis="人工覆核後維持觀望",
                    risks="供應風險",
                    review_note="原模型過度推論看多",
                    reviewer="tester",
                )
            finally:
                connection.close()

            data = load_dashboard_data(db_path, aliases_path)
            self.assertEqual(len(data["tickers"]), 2)
            sive = next(ticker for ticker in data["tickers"] if ticker["ticker"] == "SIVE")
            self.assertEqual(sive["mention_count"], 2)
            self.assertEqual(sive["source_tickers"], ["SIVE", "SIVEF"])
            self.assertEqual(sive["latest_sentiment"], "Neutral")
            self.assertEqual(sive["latest_thesis"], "人工覆核後維持觀望")
            self.assertEqual(sive["latest_quality_status"], "manual")
            self.assertEqual(sive["posts"][0]["model_sentiment"], "Bullish")
            self.assertTrue(sive["posts"][0]["has_override"])
            self.assertEqual(sive["posts"][0]["review_note"], "原模型過度推論看多")
            self.assertEqual(sive["risk_groups"][0]["risk"], "供應風險")
            self.assertEqual(sive["evolution"][0]["change_type"], "新增風險")
            self.assertEqual(len(sive["key_points"]), 2)
            self.assertEqual(data["quality_totals"]["manual"], 1)
            self.assertEqual(data["quality_totals"]["verified"], 1)
            self.assertEqual(data["quality_totals"]["legacy"], 1)
            self.assertEqual(data["quality_totals"]["unverified"], 0)
            self.assertEqual(data["periods"]["day"]["ticker_count"], 1)
            self.assertEqual(data["periods"]["week"]["ticker_count"], 1)
            self.assertEqual(data["periods"]["quarter"]["ticker_count"], 2)


if __name__ == "__main__":
    unittest.main()
