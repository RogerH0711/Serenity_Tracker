import unittest

from scraper import _extract_post, _failure_details


class FakeTextElement:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self) -> str:
        return self.text


class FakeTimeElement:
    async def get_attribute(self, name: str) -> str:
        self.assert_attribute(name)
        return "2026-08-14T11:29:05.000Z"

    async def evaluate(self, _script: str) -> str:
        return "/aleabitoreddit/status/2088226398708338889"

    @staticmethod
    def assert_attribute(name: str) -> None:
        if name != "datetime":
            raise AssertionError(name)


class FakeArticle:
    def __init__(self, texts: list[str], show_more: bool = False) -> None:
        self.texts = texts
        self.show_more = show_more

    async def query_selector_all(self, selector: str):
        if selector == 'div[data-testid="tweetText"]':
            return [FakeTextElement(text) for text in self.texts]
        return []

    async def query_selector(self, selector: str):
        if selector == "time":
            return FakeTimeElement()
        if selector == '[data-testid="tweet-text-show-more-link"]' and self.show_more:
            return object()
        return None


class ScraperContextTest(unittest.IsolatedAsyncioTestCase):
    def test_classifies_login_and_rate_limit_failures(self) -> None:
        self.assertEqual(
            _failure_details(RuntimeError("X_AUTH_TOKEN 已失效，頁面被導向登入流程")),
            ("auth", None),
        )
        self.assertEqual(
            _failure_details(RuntimeError("X 回傳 HTTP 429")),
            ("rate_limit", 429),
        )

    async def test_extracts_main_post_and_referenced_context_separately(self) -> None:
        post = await _extract_post(
            FakeArticle(
                [
                    "The $SHKY, $SNDK, $MU memory bottleneck never changed",
                    "Yes, I'm still bullish on memory like $MU / Samsung.",
                ]
            )
        )

        self.assertIsNotNone(post)
        self.assertEqual(
            post["text"],
            "The $SHKY, $SNDK, $MU memory bottleneck never changed",
        )
        self.assertEqual(
            post["context"],
            "Yes, I'm still bullish on memory like $MU / Samsung.",
        )
        self.assertEqual(post["post_id"], "2088226398708338889")

    async def test_marks_long_post_for_isolated_full_text_fetch(self) -> None:
        post = await _extract_post(FakeArticle(["$MU truncated"], show_more=True))

        self.assertEqual(post["_needs_full_text"], "1")



if __name__ == "__main__":
    unittest.main()
