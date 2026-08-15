import unittest

from common import extract_post_id, extract_tickers, is_safe_x_url


class CommonHelpersTest(unittest.TestCase):
    def test_extracts_status_id_from_x_url(self) -> None:
        self.assertEqual(
            extract_post_id("https://x.com/example/status/123456789?ref_src=test"),
            "123456789",
        )
        self.assertIsNone(extract_post_id("https://x.com/example"))

    def test_extracts_and_deduplicates_explicit_cashtags(self) -> None:
        self.assertEqual(
            extract_tickers("$nvda vs $TSM and again $NVDA; local $009150.KS"),
            ["NVDA", "TSM", "009150.KS"],
        )

    def test_excludes_dollar_amounts_but_keeps_numeric_tickers(self) -> None:
        self.assertEqual(
            extract_tickers(
                "$ORCL at $100B, $POET has $830m, $SIVE gets $3.4M, "
                "$NBIS has $8.04B, plus $7 and valid $6976 / $005930.KS"
            ),
            ["ORCL", "POET", "SIVE", "NBIS", "6976", "005930.KS"],
        )

    def test_only_allows_https_x_links(self) -> None:
        self.assertTrue(is_safe_x_url("https://x.com/example/status/123"))
        self.assertTrue(is_safe_x_url("https://mobile.x.com/example/status/123"))
        self.assertFalse(is_safe_x_url("http://x.com/example/status/123"))
        self.assertFalse(is_safe_x_url("https://x.com.evil.example/status/123"))


if __name__ == "__main__":
    unittest.main()
