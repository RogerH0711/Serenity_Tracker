import unittest

from scraper import _extract_post


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
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    async def query_selector_all(self, selector: str):
        if selector == 'div[data-testid="tweetText"]':
            return [FakeTextElement(text) for text in self.texts]
        return []

    async def query_selector(self, selector: str):
        if selector == "time":
            return FakeTimeElement()
        return None


class ScraperContextTest(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
