import tempfile
import unittest
from pathlib import Path

from alias_review import approve_candidate, scan_candidates
from build_site import load_ticker_aliases
from storage import connect_db, init_db, list_alias_candidates, register_posts, store_analysis


class AliasReviewTest(unittest.TestCase):
    def test_detects_and_approves_f_share_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "aliases.db"
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
                            "text": "$TEST $TESTF",
                            "url": "https://x.com/example/status/1",
                        }
                    ],
                )
                store_analysis(
                    connection,
                    "1",
                    [
                        {"ticker": "TEST", "sentiment": "Neutral", "thesis": "base", "risks": None},
                        {"ticker": "TESTF", "sentiment": "Neutral", "thesis": "foreign", "risks": None},
                    ],
                    "test-v1",
                )
            finally:
                connection.close()

            result = scan_candidates(db_path, aliases_path)
            self.assertEqual(result, {"created": 1, "pending": 1})
            connection = connect_db(db_path)
            try:
                candidate = list_alias_candidates(connection)[0]
                candidate_id = candidate["id"]
                self.assertEqual(candidate["alias"], "TESTF")
            finally:
                connection.close()

            self.assertTrue(
                approve_candidate(
                    candidate_id,
                    company_name="Test Company",
                    db_path=db_path,
                    aliases_path=aliases_path,
                )
            )
            alias_map, profiles = load_ticker_aliases(aliases_path)
            self.assertEqual(alias_map["TESTF"], "TEST")
            self.assertEqual(profiles["TEST"]["company_name"], "Test Company")
            self.assertEqual(scan_candidates(db_path, aliases_path)["pending"], 0)


if __name__ == "__main__":
    unittest.main()
