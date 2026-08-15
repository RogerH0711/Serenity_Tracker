import tempfile
import unittest
from pathlib import Path

from build_site import load_dashboard_data
from prices import build_market_payload, refresh_prices
from storage import connect_db, database_counts, init_db, register_posts, store_analysis


class PriceCacheTest(unittest.TestCase):
    def _project(self, root: Path, *, price_symbol="SIVE.ST"):
        db_path = root / "prices.db"
        aliases_path = root / "aliases.json"
        symbol_json = "null" if price_symbol is None else f'"{price_symbol}"'
        aliases_path.write_text(
            '{"SIVE":{"aliases":["SIVEF"],"company_name":"Sivers",'
            f'"exchange":"STO","price_symbol":{symbol_json},"currency":"SEK"}}}}',
            encoding="utf-8",
        )
        init_db(db_path)
        connection = connect_db(db_path)
        try:
            register_posts(
                connection,
                [
                    {
                        "post_id": "1",
                        "timestamp": "2026-08-01T00:00:00Z",
                        "text": "$SIVE thesis",
                        "url": "https://x.com/example/status/1",
                    },
                    {
                        "post_id": "2",
                        "timestamp": "2026-08-08T00:00:00Z",
                        "text": "$SIVE update",
                        "url": "https://x.com/example/status/2",
                    },
                ],
            )
            store_analysis(
                connection,
                "1",
                [{"ticker": "SIVE", "sentiment": "Bullish", "thesis": "需求增加", "risks": None}],
                "test-v1",
            )
            store_analysis(
                connection,
                "2",
                [{"ticker": "SIVE", "sentiment": "Bearish", "thesis": "短期轉弱", "risks": "需求風險"}],
                "test-v1",
            )
        finally:
            connection.close()
        return db_path, aliases_path

    def test_refreshes_adjusted_prices_and_builds_returns_and_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path, aliases_path = self._project(Path(directory))
            calls = []

            def fake_fetcher(symbol, start_date, end_date):
                calls.append((symbol, start_date, end_date))
                return {
                    "currency": "SEK",
                    "prices": [
                        {"date": "2026-08-01", "adjusted_close": 100},
                        {"date": "2026-08-02", "adjusted_close": 102},
                        {"date": "2026-08-08", "adjusted_close": 110},
                        {"date": "2026-08-31", "adjusted_close": 120},
                        {"date": "2026-10-30", "adjusted_close": 90},
                    ],
                }

            result = refresh_prices(
                db_path=db_path,
                aliases_path=aliases_path,
                fetcher=fake_fetcher,
                max_tickers=1,
                force_tickers=["SIVEF"],
            )
            self.assertEqual(result, {"updated": 1, "failed": 0, "unsupported": 0})
            self.assertEqual(calls[0][0], "SIVE.ST")
            ticker = load_dashboard_data(db_path, aliases_path)["tickers"][0]
            payload = build_market_payload(db_path=db_path, ticker=ticker)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["currency"], "SEK")
            self.assertEqual(payload["returns"]["1"]["return_pct"], 2.0)
            self.assertEqual(payload["returns"]["7"]["return_pct"], 10.0)
            self.assertEqual(payload["returns"]["30"]["return_pct"], 20.0)
            self.assertEqual(payload["returns"]["since_first"]["return_pct"], -10.0)
            self.assertEqual([item["post_id"] for item in payload["markers"]], ["2", "1"])
            self.assertEqual(database_counts(db_path)["price_points"], 5)

            def unexpected_fetch(*_args):
                raise AssertionError("fresh price cache should not refetch")

            self.assertEqual(
                refresh_prices(
                    db_path=db_path,
                    aliases_path=aliases_path,
                    fetcher=unexpected_fetch,
                    max_tickers=1,
                ),
                {"updated": 0, "failed": 0, "unsupported": 0},
            )

    def test_null_price_symbol_is_reviewably_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path, aliases_path = self._project(Path(directory), price_symbol=None)
            result = refresh_prices(
                db_path=db_path,
                aliases_path=aliases_path,
                max_tickers=1,
                force_tickers=["SIVE"],
            )
            self.assertEqual(result, {"updated": 0, "failed": 0, "unsupported": 1})


if __name__ == "__main__":
    unittest.main()
