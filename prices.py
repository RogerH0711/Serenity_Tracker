"""Incrementally cache adjusted daily prices for locally reviewed tickers."""

from __future__ import annotations

import argparse
import math
import os
import re
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from build_site import ALIASES_PATH, load_dashboard_data, load_ticker_aliases
from parser import _positive_int
from storage import (
    DB_PATH,
    connect_db,
    get_market_price_data,
    get_price_profiles,
    init_db,
    mark_price_refresh_failed,
    record_pipeline_prices,
    save_market_prices,
)


PRICE_PROVIDER = "yfinance"
PRICE_REFRESH_HOURS = 18
FAILED_RETRY_HOURS = 6
MAX_CHART_POINTS = 420
VALID_PROVIDER_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.^=_-]{0,24}$")


def _default_currency(symbol: str) -> str:
    suffixes = {
        ".KS": "KRW",
        ".KQ": "KRW",
        ".SZ": "CNY",
        ".SS": "CNY",
        ".T": "JPY",
        ".TW": "TWD",
        ".TWO": "TWD",
        ".ST": "SEK",
        ".L": "GBP",
        ".TO": "CAD",
        ".AX": "AUD",
    }
    return next(
        (currency for suffix, currency in suffixes.items() if symbol.endswith(suffix)),
        "USD",
    )


def fetch_yfinance_prices(
    symbol: str,
    start_date: date,
    end_date: date,
) -> dict[str, object]:
    try:
        import yfinance as yf
    except ImportError as error:
        raise RuntimeError("尚未安裝 yfinance，請重新安裝 requirements.txt") from error

    ticker = yf.Ticker(symbol)
    history = ticker.history(
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="1d",
        auto_adjust=True,
        actions=False,
        repair=False,
        timeout=20,
    )
    prices = []
    for timestamp, row in history.iterrows():
        close = float(row["Close"])
        if not math.isfinite(close) or close <= 0:
            continue
        price_date = timestamp.date().isoformat()
        prices.append({"date": price_date, "adjusted_close": close})
    metadata = getattr(ticker, "history_metadata", {}) or {}
    currency = str(metadata.get("currency") or _default_currency(symbol)).upper()
    return {"currency": currency, "prices": prices}


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _needs_refresh(profile: object | None, provider_symbol: str, now: datetime) -> bool:
    if profile is None or profile["provider_symbol"] != provider_symbol:
        return True
    if profile["status"] == "unsupported":
        return False
    reference = _parse_utc(
        profile["last_success_at"]
        if profile["status"] == "completed"
        else profile["last_attempt_at"]
    )
    maximum_age = (
        timedelta(hours=PRICE_REFRESH_HOURS)
        if profile["status"] == "completed"
        else timedelta(hours=FAILED_RETRY_HOURS)
    )
    return reference is None or now - reference >= maximum_age


def refresh_prices(
    *,
    db_path: Path = DB_PATH,
    aliases_path: Path = ALIASES_PATH,
    fetcher: Callable[[str, date, date], dict[str, object]] = fetch_yfinance_prices,
    max_tickers: int = 5,
    force_tickers: Sequence[str] = (),
) -> dict[str, int]:
    init_db(db_path)
    data = load_dashboard_data(db_path, aliases_path)
    alias_map, _profiles = load_ticker_aliases(aliases_path)
    requested = {
        alias_map.get(ticker.strip().upper(), ticker.strip().upper())
        for ticker in force_tickers
        if ticker.strip()
    }
    now = datetime.now(timezone.utc)
    connection = connect_db(db_path)
    updated = failed = unsupported = 0
    try:
        existing = {row["ticker"]: row for row in get_price_profiles(connection)}
        candidates = []
        for ticker in data["tickers"]:
            canonical = ticker["ticker"]
            if requested and canonical not in requested:
                continue
            provider_symbol = ticker.get("price_symbol")
            if requested or (
                isinstance(provider_symbol, str)
                and _needs_refresh(existing.get(canonical), provider_symbol, now)
            ):
                candidates.append(ticker)
        candidates.sort(
            key=lambda ticker: (
                ticker["ticker"] in requested,
                ticker["latest_date"],
            ),
            reverse=True,
        )
        limit = max(max_tickers, len(requested))
        candidates = candidates[:limit]
        print(f"行情快取：{len(candidates)} 組需要更新。")

        for index, ticker in enumerate(candidates, start=1):
            canonical = str(ticker["ticker"])
            provider_symbol = ticker.get("price_symbol")
            first_mention = date.fromisoformat(str(ticker["first_date"]))
            if (
                not isinstance(provider_symbol, str)
                or not VALID_PROVIDER_SYMBOL.fullmatch(provider_symbol)
            ):
                unsupported += 1
                mark_price_refresh_failed(
                    connection,
                    ticker=canonical,
                    provider_symbol=str(provider_symbol or canonical),
                    first_mention_date=first_mention.isoformat(),
                    error="尚未設定有效的 yfinance price_symbol",
                    unsupported=True,
                )
                print(f"[{index}/{len(candidates)}] {canonical} 缺少有效行情代碼")
                continue
            start_date = first_mention - timedelta(days=7)
            latest_research = date.fromisoformat(str(ticker["latest_date"]))
            end_date = max(date.today(), latest_research) + timedelta(days=2)
            try:
                result = fetcher(provider_symbol, start_date, end_date)
                prices = result.get("prices")
                if not isinstance(prices, list) or not prices:
                    raise ValueError("行情來源沒有回傳價格")
                save_market_prices(
                    connection,
                    ticker=canonical,
                    provider_symbol=provider_symbol,
                    currency=str(
                        result.get("currency")
                        or ticker.get("price_currency")
                        or _default_currency(provider_symbol)
                    ),
                    first_mention_date=first_mention.isoformat(),
                    prices=prices,
                )
                updated += 1
                print(
                    f"[{index}/{len(candidates)}] {canonical} "
                    f"快取 {len(prices)} 個交易日"
                )
            except Exception as error:
                failed += 1
                mark_price_refresh_failed(
                    connection,
                    ticker=canonical,
                    provider_symbol=provider_symbol,
                    first_mention_date=first_mention.isoformat(),
                    error=str(error),
                )
                print(f"[{index}/{len(candidates)}] {canonical} 行情失敗：{error}")

        run_id = os.getenv("SERENITY_PIPELINE_RUN_ID", "").strip()
        if run_id.isdigit():
            record_pipeline_prices(
                connection,
                int(run_id),
                updated=updated,
                failed=failed + unsupported,
            )
    finally:
        connection.close()
    print(
        f"行情快取完成：更新 {updated}、失敗 {failed}、"
        f"未支援 {unsupported}。"
    )
    return {"updated": updated, "failed": failed, "unsupported": unsupported}


def _first_price_on_or_after(
    dates: list[str],
    prices: list[float],
    target: str,
) -> tuple[str, float] | None:
    index = bisect_left(dates, target)
    if index >= len(dates):
        return None
    return dates[index], prices[index]


def _return_payload(
    baseline: float,
    observation: tuple[str, float] | None,
) -> dict[str, object] | None:
    if observation is None:
        return None
    observed_date, observed_price = observation
    return {
        "date": observed_date,
        "price": round(observed_price, 4),
        "return_pct": round((observed_price / baseline - 1) * 100, 2),
    }


def build_market_payload(
    *,
    db_path: Path,
    ticker: dict[str, object],
) -> dict[str, object]:
    connection = connect_db(db_path)
    try:
        profile, rows = get_market_price_data(connection, str(ticker["ticker"]))
    finally:
        connection.close()
    if profile is None:
        return {"status": "missing", "ticker": ticker["ticker"]}
    if not rows:
        return {
            "status": profile["status"],
            "ticker": ticker["ticker"],
            "provider_symbol": profile["provider_symbol"],
            "error": profile["last_error"],
        }

    dates = [str(row["price_date"]) for row in rows]
    closes = [float(row["adjusted_close"]) for row in rows]
    baseline_observation = _first_price_on_or_after(
        dates,
        closes,
        str(ticker["first_date"]),
    )
    if baseline_observation is None:
        return {
            "status": "failed",
            "ticker": ticker["ticker"],
            "provider_symbol": profile["provider_symbol"],
            "error": "首次提及後沒有價格資料",
        }
    baseline_date, baseline = baseline_observation
    returns = {}
    first_date = date.fromisoformat(str(ticker["first_date"]))
    last_price_date = date.fromisoformat(dates[-1])
    for horizon in (1, 7, 30, 90):
        target = first_date + timedelta(days=horizon)
        returns[str(horizon)] = (
            _return_payload(
                baseline,
                _first_price_on_or_after(dates, closes, target.isoformat()),
            )
            if target <= last_price_date
            else None
        )
    returns["since_first"] = _return_payload(
        baseline,
        (dates[-1], closes[-1]),
    )

    markers = []
    for post in ticker["posts"]:
        observation = _first_price_on_or_after(dates, closes, str(post["date"]))
        if observation is None:
            continue
        marker_date, marker_price = observation
        markers.append(
            {
                "date": marker_date,
                "price": round(marker_price, 4),
                "sentiment": post["sentiment"],
                "post_date": post["date"],
                "post_id": post["post_id"],
                "url": post["url"],
            }
        )

    step = max(1, math.ceil(len(rows) / MAX_CHART_POINTS))
    sampled = [
        {"date": dates[index], "close": round(closes[index], 4)}
        for index in range(0, len(rows), step)
    ]
    if sampled[-1]["date"] != dates[-1]:
        sampled.append({"date": dates[-1], "close": round(closes[-1], 4)})
    return {
        "status": "completed",
        "ticker": ticker["ticker"],
        "provider": profile["provider"],
        "provider_symbol": profile["provider_symbol"],
        "currency": profile["currency"] or _default_currency(profile["provider_symbol"]),
        "first_mention_date": ticker["first_date"],
        "baseline_date": baseline_date,
        "last_price_date": dates[-1],
        "last_success_at": profile["last_success_at"],
        "series": sampled,
        "markers": markers,
        "returns": returns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="指定需要更新的 ticker；可重複使用",
    )
    arguments = parser.parse_args()
    refresh_prices(
        max_tickers=_positive_int("PRICE_MAX_TICKERS", 5),
        force_tickers=arguments.ticker,
    )


if __name__ == "__main__":
    main()
