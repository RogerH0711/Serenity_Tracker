"""Inspect and manage persistent human overrides for parsed stock mentions."""

from __future__ import annotations

import argparse

from storage import (
    DB_PATH,
    connect_db,
    delete_mention_override,
    init_db,
    list_mention_overrides,
    set_mention_override,
)


def _show_post(connection, post_id: str, ticker: str | None) -> int:
    parameters: list[str] = [post_id]
    ticker_filter = ""
    if ticker:
        ticker_filter = " AND m.ticker = ?"
        parameters.append(ticker.strip().upper())
    rows = connection.execute(
        f"""
        SELECT
            p.timestamp, p.url, m.post_id, m.ticker,
            m.sentiment AS model_sentiment,
            m.sentiment_evidence AS model_evidence,
            m.thesis AS model_thesis,
            m.risks AS model_risks,
            m.analysis_version,
            o.sentiment AS override_sentiment,
            o.sentiment_evidence AS override_evidence,
            o.thesis AS override_thesis,
            o.risks AS override_risks,
            o.review_note, o.reviewer, o.reviewed_at
        FROM mentions AS m
        JOIN posts AS p ON p.post_id = m.post_id
        LEFT JOIN mention_overrides AS o
          ON o.post_id = m.post_id AND o.ticker = m.ticker
        WHERE m.post_id = ? {ticker_filter}
        ORDER BY m.ticker
        """,
        parameters,
    ).fetchall()
    if not rows:
        print(f"找不到分析：{post_id}{f'/{ticker}' if ticker else ''}")
        return 1
    print(f"來源：{rows[0]['url']} ({rows[0]['timestamp']})")
    for row in rows:
        print(f"\n[{row['ticker']}] model {row['analysis_version']}")
        print(f"  sentiment: {row['model_sentiment']}")
        print(f"  evidence:  {row['model_evidence'] or '未提供'}")
        print(f"  thesis:    {row['model_thesis']}")
        print(f"  risks:     {row['model_risks'] or '未提及'}")
        if row["override_sentiment"]:
            print(f"  override:  {row['override_sentiment']} ({row['reviewer']})")
            print(f"  evidence:  {row['override_evidence'] or '未提供'}")
            print(f"  thesis:    {row['override_thesis']}")
            print(f"  risks:     {row['override_risks'] or '未提及'}")
            print(f"  note:      {row['review_note'] or '未填寫'}")
            print(f"  reviewed:  {row['reviewed_at']}")
    return 0


def _list_overrides(connection) -> int:
    rows = list_mention_overrides(connection)
    if not rows:
        print("目前沒有人工覆核。")
        return 0
    for row in rows:
        print(
            f"{row['reviewed_at']}  {row['post_id']}/{row['ticker']}  "
            f"{row['sentiment']}  {row['review_note'] or '-'}"
        )
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="查看模型結果與既有人工覆核")
    show.add_argument("post_id")
    show.add_argument("--ticker")

    listing = subparsers.add_parser("list", help="列出全部人工覆核")
    listing.set_defaults(command="list")

    setting = subparsers.add_parser("set", help="建立或更新人工覆核")
    setting.add_argument("post_id")
    setting.add_argument("ticker")
    setting.add_argument(
        "--sentiment",
        required=True,
        choices=["Bullish", "Bearish", "Neutral"],
    )
    setting.add_argument("--thesis", required=True)
    setting.add_argument("--evidence", default="")
    setting.add_argument("--risks", default=None)
    setting.add_argument("--note", required=True)
    setting.add_argument("--reviewer", default="manual")

    deleting = subparsers.add_parser("delete", help="刪除人工覆核並恢復模型結果")
    deleting.add_argument("post_id")
    deleting.add_argument("ticker")
    return parser


def main() -> int:
    arguments = build_argument_parser().parse_args()
    init_db(DB_PATH)
    connection = connect_db(DB_PATH)
    try:
        if arguments.command == "show":
            return _show_post(connection, arguments.post_id, arguments.ticker)
        if arguments.command == "list":
            return _list_overrides(connection)
        if arguments.command == "set":
            set_mention_override(
                connection,
                post_id=arguments.post_id,
                ticker=arguments.ticker,
                sentiment=arguments.sentiment,
                sentiment_evidence=arguments.evidence,
                thesis=arguments.thesis,
                risks=arguments.risks,
                review_note=arguments.note,
                reviewer=arguments.reviewer,
            )
            print(
                f"人工覆核已儲存：{arguments.post_id}/"
                f"{arguments.ticker.strip().upper()}。請重新執行 build_site.py。"
            )
            return 0
        deleted = delete_mention_override(
            connection,
            arguments.post_id,
            arguments.ticker,
        )
        if not deleted:
            print("找不到指定人工覆核。")
            return 1
        print("人工覆核已刪除；建站時將恢復模型結果。")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
