"""Collect recent posts from an X profile with stable status identities."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, ElementHandle, Page, async_playwright

from common import (
    PROJECT_DIR,
    atomic_write_json,
    canonical_x_url,
    extract_post_id,
    is_safe_x_url,
)


RAW_TWEETS_PATH = PROJECT_DIR / "raw_tweets.json"
DEFAULT_TARGET_ACCOUNT = "aleabitoreddit"


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必須是整數，目前為 {raw_value!r}") from error
    if value < 1:
        raise ValueError(f"{name} 必須大於 0")
    return value


async def _extract_post(article: ElementHandle) -> dict[str, str] | None:
    text_elements = await article.query_selector_all('div[data-testid="tweetText"]')
    time_element = await article.query_selector("time")
    if not text_elements or not time_element:
        return None

    text = (await text_elements[0].inner_text()).strip()
    context_parts: list[str] = []
    seen_context: set[str] = set()
    for context_element in text_elements[1:]:
        context_text = (await context_element.inner_text()).strip()
        normalized = " ".join(context_text.split())
        if not normalized or normalized == " ".join(text.split()):
            continue
        if normalized in seen_context:
            continue
        seen_context.add(normalized)
        context_parts.append(context_text)
    timestamp = (await time_element.get_attribute("datetime") or "").strip()
    href = await time_element.evaluate(
        "element => element.closest('a')?.getAttribute('href') || ''"
    )
    post_id = extract_post_id(href)
    if not text or not timestamp or not post_id:
        return None

    path_parts = [part for part in urlparse(href).path.split("/") if part]
    author = path_parts[0] if path_parts else DEFAULT_TARGET_ACCOUNT
    url = canonical_x_url(post_id, author)
    if not is_safe_x_url(url):
        return None

    show_more = await article.query_selector(
        '[data-testid="tweet-text-show-more-link"]'
    )

    return {
        "post_id": post_id,
        "timestamp": timestamp,
        "text": text,
        "context": "\n\n".join(context_parts),
        "url": url,
        "_needs_full_text": "1" if show_more else "",
    }


async def _find_post_on_page(
    page: Page,
    post_id: str,
) -> dict[str, str] | None:
    articles = await page.query_selector_all('article[data-testid="tweet"]')
    for article in articles:
        candidate = await _extract_post(article)
        if candidate and candidate["post_id"] == post_id:
            return candidate
    return None


async def _hydrate_long_posts(
    context: BrowserContext,
    posts: list[dict[str, str]],
) -> None:
    """Read truncated posts on an isolated detail page without navigating profile."""
    targets = [post for post in posts if post.pop("_needs_full_text", "")]
    if not targets:
        return
    detail_page = await context.new_page()
    try:
        for post in targets:
            try:
                await detail_page.goto(
                    post["url"],
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await detail_page.wait_for_selector(
                    'article[data-testid="tweet"]',
                    timeout=15_000,
                )
                full_post = await _find_post_on_page(detail_page, post["post_id"])
                if full_post and len(full_post["text"]) > len(post["text"]):
                    post["text"] = full_post["text"]
                    post["context"] = full_post["context"]
            except Exception as error:
                print(
                    f"警告：無法取得長貼文 {post['post_id']} 全文，"
                    f"將使用時間軸文字：{error}"
                )
    finally:
        await detail_page.close()


async def _collect_visible_posts(
    page: Page,
    collected: dict[str, dict[str, str]],
) -> int:
    articles = await page.query_selector_all('article[data-testid="tweet"]')
    added = 0
    for article in articles:
        try:
            post = await _extract_post(article)
        except Exception as error:
            print(f"警告：略過一則無法解析的貼文元件：{error}")
            continue
        if post and post["post_id"] not in collected:
            collected[post["post_id"]] = post
            added += 1
    return added


async def scrape_tweets(
    *,
    auth_token: str,
    target_account: str,
    output_path: Path = RAW_TWEETS_PATH,
    scroll_rounds: int = 3,
    max_posts: int = 40,
) -> list[dict[str, str]]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [
                    {
                        "name": "auth_token",
                        "value": auth_token,
                        "domain": ".x.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = await context.new_page()
            profile_url = f"https://x.com/{target_account}"
            print(f"正在前往 {profile_url} ...")
            response = await page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            if response and response.status >= 400:
                raise RuntimeError(f"X 回傳 HTTP {response.status}")
            if "/login" in page.url or "/i/flow/login" in page.url:
                raise RuntimeError("X_AUTH_TOKEN 已失效，頁面被導向登入流程")

            await page.wait_for_selector(
                'article[data-testid="tweet"]', timeout=15_000
            )

            collected: dict[str, dict[str, str]] = {}
            stable_rounds = 0
            for round_index in range(scroll_rounds):
                added = await _collect_visible_posts(page, collected)
                stable_rounds = stable_rounds + 1 if added == 0 else 0
                if len(collected) >= max_posts or stable_rounds >= 2:
                    break
                if round_index + 1 < scroll_rounds:
                    await page.mouse.wheel(0, 2600)
                    await page.wait_for_timeout(1_200)

            tweets = sorted(
                collected.values(), key=lambda post: post["timestamp"], reverse=True
            )[:max_posts]
            if not tweets:
                raise RuntimeError("沒有取得任何有效貼文；保留既有 raw_tweets.json")

            await _hydrate_long_posts(context, tweets)
            for tweet in tweets:
                tweet.pop("_needs_full_text", None)

            atomic_write_json(output_path, tweets)
            print(f"抓取完成：原子寫入 {len(tweets)} 則貼文至 {output_path.name}")
            return tweets
        finally:
            await browser.close()


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    auth_token = os.getenv("X_AUTH_TOKEN", "").strip()
    if not auth_token:
        raise SystemExit("錯誤：找不到 X_AUTH_TOKEN，請檢查 .env 設定。")

    target_account = os.getenv("X_TARGET_ACCOUNT", DEFAULT_TARGET_ACCOUNT).strip()
    try:
        asyncio.run(
            scrape_tweets(
                auth_token=auth_token,
                target_account=target_account,
                scroll_rounds=_positive_int("SCRAPE_SCROLL_ROUNDS", 3),
                max_posts=_positive_int("SCRAPE_MAX_POSTS", 40),
            )
        )
    except Exception as error:
        raise SystemExit(f"爬蟲失敗：{error}") from error


if __name__ == "__main__":
    main()
