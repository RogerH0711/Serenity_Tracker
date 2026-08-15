import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storage import connect_db, get_ticker_snapshots, init_db, register_posts, store_analysis
from summarize import (
    EvolutionSummary,
    KeyPointSummary,
    RiskSummary,
    TickerSnapshotAnalysis,
    summarize_changed_tickers,
)


class FakeModels:
    def __init__(self) -> None:
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed=TickerSnapshotAnalysis(
                summary="作者最新維持偏多，需求論點獲得強化。",
                summary_source_post_ids=["2"],
                evolution=[
                    EvolutionSummary(
                        change_type="reinforced",
                        summary="最新貼文再次強化需求成長論點。",
                        source_post_ids=["1", "2"],
                    )
                ],
                key_points=[
                    KeyPointSummary(
                        title="需求成長",
                        thesis="作者認為需求持續增加。",
                        why="這是最新偏多立場的主要依據。",
                        source_post_ids=["2"],
                    )
                ],
                risks=[
                    RiskSummary(
                        name="供應限制",
                        summary="作者曾提到供應能力可能限制成長。",
                        source_post_ids=["1"],
                    )
                ],
            ),
            text="",
        )


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModels()


class FailingModels:
    def generate_content(self, **_kwargs):
        raise RuntimeError("temporary Gemini failure")


class FailingClient:
    def __init__(self) -> None:
        self.models = FailingModels()


class IncrementalSummaryTest(unittest.TestCase):
    def test_only_changed_ticker_is_summarized_and_sources_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "summary.db"
            aliases_path = root / "aliases.json"
            aliases_path.write_text("{}", encoding="utf-8")
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                register_posts(
                    connection,
                    [
                        {
                            "post_id": "1",
                            "timestamp": "2026-08-14T00:00:00Z",
                            "text": "$SIVE supply",
                            "url": "https://x.com/example/status/1",
                        },
                        {
                            "post_id": "2",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$SIVE demand",
                            "url": "https://x.com/example/status/2",
                        },
                    ],
                )
                store_analysis(
                    connection,
                    "1",
                    [{"ticker": "SIVE", "sentiment": "Neutral", "thesis": "供應限制", "risks": "供應限制"}],
                    "legacy-v1",
                )
                store_analysis(
                    connection,
                    "2",
                    [{"ticker": "SIVE", "sentiment": "Bullish", "thesis": "需求增加", "risks": None}],
                    "legacy-v1",
                )
            finally:
                connection.close()

    def test_failed_refresh_keeps_the_last_completed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "summary.db"
            aliases_path = root / "aliases.json"
            aliases_path.write_text("{}", encoding="utf-8")
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                register_posts(
                    connection,
                    [
                        {
                            "post_id": "1",
                            "timestamp": "2026-08-14T00:00:00Z",
                            "text": "$SIVE supply",
                            "url": "https://x.com/example/status/1",
                        },
                        {
                            "post_id": "2",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$SIVE demand",
                            "url": "https://x.com/example/status/2",
                        }
                    ],
                )
                store_analysis(
                    connection,
                    "1",
                    [
                        {
                            "ticker": "SIVE",
                            "sentiment": "Neutral",
                            "thesis": "供應限制",
                            "risks": "供應限制",
                        }
                    ],
                    "legacy-v1",
                )
                store_analysis(
                    connection,
                    "2",
                    [
                        {
                            "ticker": "SIVE",
                            "sentiment": "Bullish",
                            "thesis": "需求增加",
                            "risks": None,
                        }
                    ],
                    "legacy-v1",
                )
            finally:
                connection.close()

            self.assertEqual(
                summarize_changed_tickers(
                    db_path=db_path,
                    aliases_path=aliases_path,
                    client=FakeClient(),
                    model="fake",
                    max_tickers=1,
                ),
                {"updated": 1, "failed": 0},
            )

            connection = connect_db(db_path)
            try:
                register_posts(
                    connection,
                    [
                        {
                            "post_id": "3",
                            "timestamp": "2026-08-16T00:00:00Z",
                            "text": "$SIVE follow-up",
                            "url": "https://x.com/example/status/3",
                        }
                    ],
                )
                store_analysis(
                    connection,
                    "3",
                    [
                        {
                            "ticker": "SIVE",
                            "sentiment": "Neutral",
                            "thesis": "等待後續驗證",
                            "risks": None,
                        }
                    ],
                    "legacy-v1",
                )
            finally:
                connection.close()

            self.assertEqual(
                summarize_changed_tickers(
                    db_path=db_path,
                    aliases_path=aliases_path,
                    client=FailingClient(),
                    model="fake",
                    max_tickers=1,
                ),
                {"updated": 0, "failed": 1},
            )
            connection = connect_db(db_path)
            try:
                snapshot = get_ticker_snapshots(connection)[0]
                self.assertEqual(snapshot["status"], "failed")
                self.assertEqual(
                    snapshot["summary"],
                    "作者最新維持偏多，需求論點獲得強化。",
                )
                self.assertIn("temporary Gemini failure", snapshot["last_error"])
            finally:
                connection.close()

            client = FakeClient()
            first = summarize_changed_tickers(
                db_path=db_path,
                aliases_path=aliases_path,
                client=client,
                model="fake",
                max_tickers=2,
            )
            second = summarize_changed_tickers(
                db_path=db_path,
                aliases_path=aliases_path,
                client=client,
                model="fake",
                max_tickers=2,
            )

            self.assertEqual(first, {"updated": 1, "failed": 0})
            self.assertEqual(second, {"updated": 0, "failed": 0})
            self.assertEqual(client.models.calls, 1)
            connection = connect_db(db_path)
            try:
                snapshot = get_ticker_snapshots(connection)[0]
                self.assertEqual(snapshot["status"], "completed")
                self.assertEqual(
                    json.loads(snapshot["source_post_ids_json"]),
                    ["1", "2"],
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
