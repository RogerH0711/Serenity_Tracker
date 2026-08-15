import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qa import (
    AnswerCitation,
    GroundedAnswer,
    _validate_answer,
    answer_question,
    select_question_context,
)
from storage import connect_db, database_counts, init_db, register_posts, store_analysis


class FakeModels:
    def __init__(self, citation_post_id="2"):
        self.calls = 0
        self.citation_post_id = citation_post_id

    def generate_content(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed=GroundedAnswer(
                answer=(
                    "作者目前對 SIVE 偏多，主要依據是需求增加"
                    "（補充說明）。 (post_id \"2\", ticker \"SIVE\", "
                    "claim \"需求增加\")"
                ),
                citations=[
                    AnswerCitation(
                        ticker="SIVE",
                        post_id=self.citation_post_id,
                        claim="支持目前偏多與需求增加的整理",
                    )
                ],
                limitations=["只反映已收錄的公開貼文。"],
            ),
            text="",
        )


class FakeClient:
    def __init__(self, citation_post_id="2"):
        self.models = FakeModels(citation_post_id)


class GroundedQuestionTest(unittest.TestCase):
    def _project(self, root: Path):
        db_path = root / "qa.db"
        aliases_path = root / "aliases.json"
        aliases_path.write_text(
            '{"SIVE":{"aliases":["SIVEF"],"company_name":"Sivers"}}',
            encoding="utf-8",
        )
        init_db(db_path)
        connection = connect_db(db_path)
        try:
            register_posts(
                connection,
                [
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
                "2",
                [{"ticker": "SIVE", "sentiment": "Bullish", "thesis": "需求增加", "risks": None}],
                "test-v1",
            )
        finally:
            connection.close()
        return db_path, aliases_path

    def test_answer_is_cited_and_second_call_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path, aliases_path = self._project(Path(directory))
            client = FakeClient()
            first = answer_question(
                "為什麼看多 SIVEF？",
                db_path=db_path,
                aliases_path=aliases_path,
                client=client,
                model="fake",
            )
            second = answer_question(
                "為什麼看多 SIVEF？",
                db_path=db_path,
                aliases_path=aliases_path,
                client=client,
                model="fake",
            )
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(client.models.calls, 1)
            self.assertEqual(first["question"], "為什麼看多 SIVEF？")
            self.assertNotIn("post_id", first["answer"])
            self.assertNotIn('ticker "SIVE"', first["answer"])
            self.assertEqual(first["citations"][0]["url"], "https://x.com/example/status/2")
            self.assertEqual(database_counts(db_path)["qa_cache"], 1)

    def test_rejects_invented_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path, aliases_path = self._project(Path(directory))
            with self.assertRaisesRegex(ValueError, "未允許引用"):
                answer_question(
                    "為什麼看多 SIVE？",
                    db_path=db_path,
                    aliases_path=aliases_path,
                    client=FakeClient("999"),
                    model="fake",
                )

    def test_context_excludes_unsafe_legacy_metadata_and_snapshot(self) -> None:
        dashboard = {
            "tickers": [
                {
                    "ticker": "SNDK",
                    "aliases": ["SNDK"],
                    "company_name": "SanDisk",
                    "latest_sentiment": "Bullish",
                    "latest_date": "2026-08-15",
                    "latest_thesis": "記憶體需求仍強",
                    "semantic_snapshot": {
                        "summary": "股票代碼已不再活躍",
                        "is_stale": False,
                        "source_quality_counts": {
                            "manual": 1,
                            "verified": 0,
                            "legacy": 1,
                            "unverified": 0,
                        },
                    },
                    "posts": [
                        {
                            "post_id": "new",
                            "date": "2026-08-15",
                            "sentiment": "Bullish",
                            "thesis": "記憶體需求仍強",
                            "risks": None,
                            "quality_status": "manual",
                        },
                        {
                            "post_id": "old",
                            "date": "2026-01-01",
                            "sentiment": "Bearish",
                            "thesis": "SanDisk 已於2016年被 Western Digital 收購",
                            "risks": "股票代碼已不再活躍",
                            "quality_status": "legacy",
                        },
                    ],
                }
            ]
        }

        context = select_question_context("分析 SNDK", dashboard)

        self.assertIsNone(context[0]["latest_summary"])
        self.assertEqual(
            [post["post_id"] for post in context[0]["posts"]],
            ["new"],
        )

    def test_rejects_unsupported_corporate_status_in_answer(self) -> None:
        context = [
            {
                "ticker": "SNDK",
                "posts": [
                    {
                        "post_id": "new",
                        "quality_status": "manual",
                        "thesis": "記憶體需求仍強",
                        "risks": None,
                    }
                ],
            }
        ]
        answer = GroundedAnswer(
            answer="SNDK 股票代碼已不再活躍。",
            citations=[
                AnswerCitation(ticker="SNDK", post_id="new", claim="公司狀態")
            ],
        )

        with self.assertRaisesRegex(ValueError, "公司狀態"):
            _validate_answer(
                answer,
                context,
                {
                    ("SNDK", "new"): {
                        "date": "2026-08-15",
                        "url": "https://x.com/example/status/new",
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
