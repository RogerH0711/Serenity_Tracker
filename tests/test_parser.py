import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from parser import (
    MentionAnalysis,
    TweetAnalysis,
    _normalize_analysis,
    parse_tweets,
)
from storage import database_counts


class FakeModels:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs["contents"])
        return SimpleNamespace(
            parsed=TweetAnalysis(
                mentions=[
                    MentionAnalysis(
                        ticker="SNDK",
                        sentiment="Neutral",
                        sentiment_evidence="$SNDK source text",
                        thesis="source-only analysis",
                        risks=None,
                    )
                ]
            ),
            text="",
        )


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModels()


class BadRequestError(Exception):
    code = 400


class FailingModels:
    def __init__(self) -> None:
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        raise BadRequestError("invalid response schema")


class FailingClient:
    def __init__(self) -> None:
        self.models = FailingModels()


class ParserValidationTest(unittest.TestCase):
    def test_gemini_schema_does_not_emit_unsupported_additional_properties(self) -> None:
        schema = TweetAnalysis.model_json_schema()
        self.assertNotIn("additionalProperties", json.dumps(schema))

    def test_rejects_model_invented_ticker(self) -> None:
        analysis = TweetAnalysis(
            mentions=[
                MentionAnalysis(
                    ticker="WDC",
                    sentiment="Bearish",
                    sentiment_evidence="invented association",
                    thesis="invented association",
                    risks=None,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "未允許"):
            _normalize_analysis(analysis, ["SNDK"], "invented association")

    def test_normalizes_null_like_risk(self) -> None:
        analysis = TweetAnalysis(
            mentions=[
                MentionAnalysis(
                    ticker="$sndk",
                    sentiment="Neutral",
                    sentiment_evidence="Only the source claim",
                    thesis="Only the source claim",
                    risks="未提及",
                )
            ]
        )
        result = _normalize_analysis(
            analysis,
            ["SNDK"],
            "Only the source claim",
        )
        self.assertEqual(result[0]["ticker"], "SNDK")
        self.assertIsNone(result[0]["risks"])

    def test_rejects_sentiment_evidence_not_found_in_source(self) -> None:
        analysis = TweetAnalysis(
            mentions=[
                MentionAnalysis(
                    ticker="MU",
                    sentiment="Bullish",
                    sentiment_evidence="I am bullish",
                    thesis="作者看多記憶體。",
                    risks=None,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "並非輸入原文逐字摘錄"):
            _normalize_analysis(
                analysis,
                ["MU"],
                "$MU memory bottleneck never changed",
            )

    def test_accepts_evidence_with_equivalent_linebreak_and_punctuation_spacing(self) -> None:
        analysis = TweetAnalysis(
            mentions=[
                MentionAnalysis(
                    ticker="MU",
                    sentiment="Bullish",
                    sentiment_evidence="$MU, $SKHY, and Samsung are happy to hear this…",
                    thesis="作者認為非中國記憶體供應商受惠。",
                    risks=None,
                )
            ]
        )
        result = _normalize_analysis(
            analysis,
            ["MU"],
            "$MU\n,\n$SKHY\n, and Samsung are happy to hear this...",
        )
        self.assertEqual(result[0]["sentiment"], "Bullish")

    def test_second_run_does_not_call_model_or_duplicate_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            parsed_path = root / "parsed.json"
            db_path = root / "tracker.db"
            raw_path.write_text(
                json.dumps(
                    [
                        {
                            "post_id": "123",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$SNDK source text",
                            "context": "quoted context",
                            "url": "https://x.com/example/status/123",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            client = FakeClient()

            first = parse_tweets(
                raw_path=raw_path,
                parsed_path=parsed_path,
                db_path=db_path,
                client=client,
                model="fake-model",
            )
            second = parse_tweets(
                raw_path=raw_path,
                parsed_path=parsed_path,
                db_path=db_path,
                client=client,
                model="fake-model",
            )

            self.assertEqual(first["completed"], 1)
            self.assertEqual(second["completed"], 0)
            self.assertEqual(client.models.calls, 1)
            self.assertIn('"referenced_context": "quoted context"', client.models.prompts[0])
            self.assertEqual(database_counts(db_path)["mentions"], 1)

    def test_bad_request_fails_fast_instead_of_reporting_pipeline_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw_path.write_text(
                json.dumps(
                    [
                        {
                            "post_id": str(post_id),
                            "timestamp": f"2026-08-15T00:00:0{post_id}Z",
                            "text": "$SNDK source text",
                            "url": f"https://x.com/example/status/{post_id}",
                        }
                        for post_id in (1, 2)
                    ]
                ),
                encoding="utf-8",
            )
            client = FailingClient()
            db_path = root / "tracker.db"

            with self.assertRaisesRegex(RuntimeError, "請求設定錯誤"):
                parse_tweets(
                    raw_path=raw_path,
                    parsed_path=root / "parsed.json",
                    db_path=db_path,
                    client=client,
                    model="fake-model",
                )

            self.assertEqual(client.models.calls, 1)
            counts = database_counts(db_path)
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(counts["pending"], 1)

    def test_force_reparse_replaces_analysis_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw_path.write_text(
                json.dumps(
                    [
                        {
                            "post_id": "123",
                            "timestamp": "2026-08-15T00:00:00Z",
                            "text": "$SNDK source text",
                            "context": "quoted context",
                            "url": "https://x.com/example/status/123",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            client = FakeClient()
            db_path = root / "tracker.db"
            parse_tweets(
                raw_path=raw_path,
                parsed_path=root / "parsed.json",
                db_path=db_path,
                client=client,
                model="fake-model",
            )
            result = parse_tweets(
                raw_path=raw_path,
                parsed_path=root / "parsed.json",
                db_path=db_path,
                client=client,
                model="fake-model",
                force_post_ids=["123"],
            )

            self.assertEqual(result["completed"], 1)
            self.assertEqual(client.models.calls, 2)
            self.assertEqual(database_counts(db_path)["mentions"], 1)


if __name__ == "__main__":
    unittest.main()
