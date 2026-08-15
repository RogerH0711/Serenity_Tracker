"""Build a searchable static research dashboard from validated SQLite records."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from common import PROJECT_DIR, atomic_write_text, is_safe_x_url
from storage import DB_PATH, connect_db, init_db, utc_now


TEMPLATE_PATH = PROJECT_DIR / "template.html"
ALIASES_PATH = PROJECT_DIR / "ticker_aliases.json"
OUTPUT_PATH = PROJECT_DIR / "index.html"

PERIODS = {
    "day": {"label": "日", "title": "Daily Watchlist", "days": 1},
    "week": {"label": "週", "title": "Weekly Watchlist", "days": 7},
    "month": {"label": "月", "title": "Monthly Watchlist", "days": 28},
    "quarter": {"label": "季", "title": "Quarterly Watchlist", "days": 90},
}


def load_ticker_aliases(
    path: Path = ALIASES_PATH,
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    """Load a reviewable alias map whose canonical symbols are unique."""
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{path.name} 不是有效 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} 最外層必須是物件")

    alias_map: dict[str, str] = {}
    profiles: dict[str, dict[str, object]] = {}
    for raw_canonical, raw_profile in payload.items():
        canonical = str(raw_canonical).strip().upper()
        if not canonical or not isinstance(raw_profile, dict):
            raise RuntimeError(f"{path.name} 包含無效 canonical ticker")
        raw_aliases = raw_profile.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise RuntimeError(f"{canonical} 的 aliases 必須是陣列")
        aliases = list(
            dict.fromkeys(
                str(alias).strip().upper()
                for alias in raw_aliases
                if str(alias).strip()
            )
        )
        for alias in [canonical, *aliases]:
            existing = alias_map.get(alias)
            if existing and existing != canonical:
                raise RuntimeError(
                    f"ticker alias {alias} 同時指向 {existing} 與 {canonical}"
                )
            alias_map[alias] = canonical
        profiles[canonical] = {
            "company_name": str(raw_profile.get("company_name", "")).strip(),
            "exchange": str(raw_profile.get("exchange", "")).strip(),
            "aliases": aliases,
            "price_symbol": (
                None
                if raw_profile.get("price_symbol", canonical) is None
                else str(raw_profile.get("price_symbol", canonical)).strip().upper()
            ),
            "currency": str(raw_profile.get("currency", "")).strip().upper(),
        }
    return alias_map, profiles


def _normalized_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value)


def _similarity(left: str, right: str) -> float:
    normalized_left = _normalized_text(left)
    normalized_right = _normalized_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return min(len(normalized_left), len(normalized_right)) / max(
            len(normalized_left), len(normalized_right)
        )
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _split_risks(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip(" -•\t") for part in re.split(r"[；;\n]+", value) if part.strip()]


def _aggregate_risks(posts: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    for post in posts:
        for risk in _split_risks(post.get("risks")):
            matched = None
            for group in groups:
                if _similarity(risk, str(group["risk"])) >= 0.74:
                    matched = group
                    break
            occurrence = {
                "date": post["date"],
                "post_id": post["post_id"],
                "url": post["url"],
            }
            if matched is None:
                groups.append(
                    {
                        "risk": risk,
                        "count": 1,
                        "first_date": post["date"],
                        "last_date": post["date"],
                        "occurrences": [occurrence],
                    }
                )
            else:
                matched["count"] = int(matched["count"]) + 1
                matched["first_date"] = min(str(matched["first_date"]), post["date"])
                matched["last_date"] = max(str(matched["last_date"]), post["date"])
                matched["occurrences"].append(occurrence)
    return sorted(
        groups,
        key=lambda group: (int(group["count"]), str(group["last_date"])),
        reverse=True,
    )


def _build_evolution(posts: list[dict[str, object]]) -> list[dict[str, object]]:
    evolution: list[dict[str, object]] = []
    previous: dict[str, object] | None = None
    previous_risks: set[str] = set()
    for post in reversed(posts):
        current_risks = {_normalized_text(risk) for risk in _split_risks(post.get("risks"))}
        if previous is None:
            change_type = "首次提及"
            change_summary = f"首次記錄為 {post['sentiment']}"
        elif post["sentiment"] != previous["sentiment"]:
            change_type = "立場轉折"
            change_summary = f"由 {previous['sentiment']} 轉為 {post['sentiment']}"
        elif current_risks - previous_risks:
            change_type = "新增風險"
            change_summary = "在既有論點上新增風險因素"
        elif _similarity(str(post["thesis"]), str(previous["thesis"])) >= 0.72:
            change_type = "論點延續"
            change_summary = f"延續 {post['sentiment']} 立場"
        else:
            change_type = "論點更新"
            change_summary = f"維持 {post['sentiment']}，但更新判斷依據"
        evolution.append(
            {
                "change_type": change_type,
                "change_summary": change_summary,
                **post,
            }
        )
        previous = post
        previous_risks |= current_risks
    return list(reversed(evolution))


def _select_key_points(posts: list[dict[str, object]]) -> list[dict[str, object]]:
    if not posts:
        return []
    selected: list[tuple[int, str]] = [(0, "最新立場")]
    if len(posts) > 1:
        selected.append((len(posts) - 1, "論點起點"))

    candidates: list[tuple[int, int, str]] = []
    chronological = list(reversed(posts))
    for chronological_index, post in enumerate(chronological[1:-1], start=1):
        previous = chronological[chronological_index - 1]
        descending_index = len(posts) - 1 - chronological_index
        if post["sentiment"] != previous["sentiment"]:
            candidates.append((3, descending_index, "立場轉折"))
        elif post.get("risks"):
            candidates.append((2, descending_index, "關鍵風險"))
        else:
            novelty = 1 if _similarity(str(post["thesis"]), str(previous["thesis"])) < 0.6 else 0
            candidates.append((novelty, descending_index, "代表論點"))
    if candidates and len(selected) < 3:
        _, index, reason = max(candidates, key=lambda item: (item[0], len(str(posts[item[1]]["thesis"]))))
        selected.append((index, reason))

    unique: dict[int, str] = {}
    for index, reason in selected:
        unique.setdefault(index, reason)
    return [
        {"why": reason, **posts[index]}
        for index, reason in list(unique.items())[:3]
    ]


def _prefer_post(
    current: dict[str, object] | None,
    candidate: dict[str, object],
    canonical: str,
) -> dict[str, object]:
    if current is None:
        return candidate
    if candidate["source_ticker"] == canonical and current["source_ticker"] != canonical:
        return candidate
    return current


def ticker_fingerprint(ticker: dict[str, object]) -> str:
    """Hash only effective research inputs so unchanged tickers stay cached."""
    payload = [
        {
            "post_id": post["post_id"],
            "sentiment": post["sentiment"],
            "thesis": post["thesis"],
            "risks": post.get("risks"),
            "quality_status": post["quality_status"],
            "reviewed_at": post.get("reviewed_at"),
        }
        for post in ticker["posts"]
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_list(value: str | None) -> list[dict[str, object]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _source_references(
    post_ids: object,
    posts_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(post_ids, list):
        return []
    references = []
    for post_id in dict.fromkeys(str(value) for value in post_ids):
        post = posts_by_id.get(post_id)
        if post:
            references.append(
                {
                    "post_id": post_id,
                    "date": post["date"],
                    "url": post["url"],
                    "quality_status": post["quality_status"],
                }
            )
    return references


def _semantic_snapshot(
    row: object | None,
    ticker: dict[str, object],
) -> dict[str, object] | None:
    if row is None or not row["summary"]:
        return None
    posts_by_id = {str(post["post_id"]): post for post in ticker["posts"]}

    def enrich(items: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                **item,
                "sources": _source_references(
                    item.get("source_post_ids"),
                    posts_by_id,
                ),
            }
            for item in items
        ]

    try:
        source_ids = json.loads(row["source_post_ids_json"] or "[]")
    except json.JSONDecodeError:
        source_ids = []
    sources = _source_references(source_ids, posts_by_id)
    source_quality_counts = {
        status: sum(1 for source in sources if source["quality_status"] == status)
        for status in ("manual", "verified", "legacy", "unverified")
    }
    return {
        "summary": row["summary"],
        "sources": sources,
        "source_quality_counts": source_quality_counts,
        "evolution": enrich(_json_list(row["evolution_json"])),
        "key_points": enrich(_json_list(row["key_points_json"])),
        "risks": enrich(_json_list(row["risks_json"])),
        "analysis_version": row["analysis_version"],
        "generated_at": row["generated_at"],
        "status": row["status"],
        "is_stale": (
            row["status"] == "failed"
            or row["source_fingerprint"] != ticker_fingerprint(ticker)
        ),
    }


def load_dashboard_data(
    db_path: Path = DB_PATH,
    aliases_path: Path = ALIASES_PATH,
) -> dict[str, object]:
    init_db(db_path)
    alias_map, alias_profiles = load_ticker_aliases(aliases_path)
    connection = connect_db(db_path)
    try:
        rows = connection.execute(
            """
            SELECT
                p.post_id, p.timestamp, p.url,
                m.ticker, m.analysis_version,
                m.sentiment AS model_sentiment,
                m.sentiment_evidence AS model_sentiment_evidence,
                m.thesis AS model_thesis,
                m.risks AS model_risks,
                o.post_id AS override_post_id,
                o.sentiment AS override_sentiment,
                o.sentiment_evidence AS override_sentiment_evidence,
                o.thesis AS override_thesis,
                o.risks AS override_risks,
                o.review_note, o.reviewer, o.reviewed_at
            FROM mentions AS m
            JOIN posts AS p ON p.post_id = m.post_id
            LEFT JOIN mention_overrides AS o
              ON o.post_id = m.post_id AND o.ticker = m.ticker
            ORDER BY p.timestamp DESC, p.post_id DESC, m.id ASC
            """
        ).fetchall()
        snapshot_rows = connection.execute(
            "SELECT * FROM ticker_snapshots ORDER BY ticker"
        ).fetchall()
        pipeline_rows = connection.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
        pending_alias_candidates = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM ticker_alias_candidates
                WHERE status = 'pending'
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    snapshots_by_ticker = {row["ticker"]: row for row in snapshot_rows}
    grouped: dict[str, dict[str, object]] = {}
    all_dates: list[date] = []
    for row in rows:
        source_ticker = str(row["ticker"]).strip().upper()
        canonical = alias_map.get(source_ticker, source_ticker)
        post_date = date.fromisoformat(row["timestamp"][:10])
        all_dates.append(post_date)
        ticker_data = grouped.setdefault(
            canonical,
            {
                "ticker": canonical,
                "profile": alias_profiles.get(
                    canonical,
                    {
                        "company_name": "",
                        "exchange": "",
                        "aliases": [],
                        "price_symbol": canonical,
                        "currency": "",
                    },
                ),
                "source_tickers": set(),
                "posts_by_id": {},
            },
        )
        ticker_data["source_tickers"].add(source_ticker)
        has_override = bool(row["override_post_id"])
        sentiment = (
            row["override_sentiment"] if has_override else row["model_sentiment"]
        )
        sentiment_evidence = (
            row["override_sentiment_evidence"]
            if has_override
            else row["model_sentiment_evidence"]
        )
        thesis = row["override_thesis"] if has_override else row["model_thesis"]
        risks = row["override_risks"] if has_override else row["model_risks"]
        analysis_version = str(row["analysis_version"] or "")
        if has_override or analysis_version.startswith("manual-"):
            quality_status = "manual"
        elif sentiment_evidence:
            quality_status = "verified"
        elif analysis_version == "legacy-v1":
            quality_status = "legacy"
        else:
            quality_status = "unverified"
        candidate = {
            "post_id": row["post_id"],
            "timestamp": row["timestamp"],
            "date": row["timestamp"][:10],
            "source_ticker": source_ticker,
            "sentiment": sentiment,
            "sentiment_evidence": sentiment_evidence or None,
            "thesis": thesis,
            "risks": risks or None,
            "quality_status": quality_status,
            "analysis_version": analysis_version,
            "has_override": has_override,
            "review_note": row["review_note"] if has_override else None,
            "reviewer": row["reviewer"] if has_override else None,
            "reviewed_at": row["reviewed_at"] if has_override else None,
            "model_sentiment": row["model_sentiment"],
            "url": row["url"] if is_safe_x_url(row["url"]) else None,
        }
        ticker_data["posts_by_id"][row["post_id"]] = _prefer_post(
            ticker_data["posts_by_id"].get(row["post_id"]),
            candidate,
            canonical,
        )

    tickers: list[dict[str, object]] = []
    for canonical, ticker_data in grouped.items():
        posts = sorted(
            ticker_data["posts_by_id"].values(),
            key=lambda post: (post["timestamp"], post["post_id"]),
            reverse=True,
        )
        latest = posts[0]
        sentiment_counts = {"Bullish": 0, "Bearish": 0, "Neutral": 0}
        quality_counts = {"manual": 0, "verified": 0, "legacy": 0, "unverified": 0}
        for post in posts:
            sentiment_counts[post["sentiment"]] += 1
            quality_counts[post["quality_status"]] += 1
        profile = ticker_data["profile"]
        configured_aliases = [canonical, *profile["aliases"]]
        ticker_record = {
                "ticker": canonical,
                "company_name": profile["company_name"],
                "exchange": profile["exchange"],
                "price_symbol": profile["price_symbol"],
                "price_currency": profile["currency"],
                "aliases": list(dict.fromkeys(configured_aliases)),
                "source_tickers": sorted(ticker_data["source_tickers"]),
                "latest_date": latest["date"],
                "latest_sentiment": latest["sentiment"],
                "latest_thesis": latest["thesis"],
                "latest_quality_status": latest["quality_status"],
                "mention_count": len(posts),
                "sentiment_counts": sentiment_counts,
                "quality_counts": quality_counts,
                "risk_count": sum(1 for post in posts if post["risks"]),
                "first_date": posts[-1]["date"],
                "posts": posts,
                "evolution": _build_evolution(posts),
                "key_points": _select_key_points(posts),
                "risk_groups": _aggregate_risks(posts),
            }
        ticker_record["semantic_snapshot"] = _semantic_snapshot(
            snapshots_by_ticker.get(canonical),
            ticker_record,
        )
        tickers.append(ticker_record)

    tickers.sort(key=lambda item: (item["latest_date"], item["ticker"]), reverse=True)
    dataset_end = max(all_dates) if all_dates else date.today()
    dataset_start = min(all_dates) if all_dates else dataset_end
    quality_totals = {"manual": 0, "verified": 0, "legacy": 0, "unverified": 0}
    for ticker in tickers:
        for key, value in ticker["quality_counts"].items():
            quality_totals[key] += value
    period_data: dict[str, dict[str, object]] = {}
    for key, definition in PERIODS.items():
        start = dataset_end - timedelta(days=int(definition["days"]) - 1)
        period_post_ids: set[str] = set()
        period_tickers = 0
        period_mentions = 0
        for ticker in tickers:
            matching = [
                post
                for post in ticker["posts"]
                if start.isoformat() <= post["date"] <= dataset_end.isoformat()
            ]
            if matching:
                period_tickers += 1
                period_mentions += len(matching)
                period_post_ids.update(post["post_id"] for post in matching)
        period_data[key] = {
            **definition,
            "start": start.isoformat(),
            "end": dataset_end.isoformat(),
            "ticker_count": period_tickers,
            "mention_count": period_mentions,
            "post_count": len(period_post_ids),
        }

    recent_runs = [dict(row) for row in pipeline_rows]
    latest_run = recent_runs[0] if recent_runs else None
    if latest_run is None:
        health_status = "unknown"
    elif latest_run["status"] == "running":
        health_status = "running"
    elif latest_run["status"] == "failed":
        health_status = "failed"
    elif latest_run["status"] == "partial":
        health_status = "partial"
    else:
        finished_at = latest_run.get("finished_at")
        try:
            finished = datetime.fromisoformat(finished_at) if finished_at else None
        except ValueError:
            finished = None
        if finished and finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        health_status = (
            "stale"
            if finished and datetime.now(timezone.utc) - finished > timedelta(hours=3)
            else "healthy"
        )
    semantic_total = sum(
        1 for ticker in tickers if ticker["semantic_snapshot"] is not None
    )
    semantic_fresh = sum(
        1
        for ticker in tickers
        if ticker["semantic_snapshot"] is not None
        and not ticker["semantic_snapshot"]["is_stale"]
    )

    return {
        "generated_at": utc_now(),
        "dataset_start": dataset_start.isoformat(),
        "dataset_end": dataset_end.isoformat(),
        "quality_totals": quality_totals,
        "health": {
            "status": health_status,
            "latest_run": latest_run,
            "recent_runs": recent_runs,
            "semantic_total": semantic_total,
            "semantic_fresh": semantic_fresh,
            "semantic_pending": max(0, len(tickers) - semantic_fresh),
            "alias_candidates_pending": pending_alias_candidates,
        },
        "periods": period_data,
        "tickers": tickers,
    }


def _safe_embedded_json(data: object) -> str:
    """Prevent JSON data from terminating its script element."""
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_static_site(
    *,
    db_path: Path = DB_PATH,
    template_path: Path = TEMPLATE_PATH,
    aliases_path: Path = ALIASES_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, object]:
    data = load_dashboard_data(db_path, aliases_path)
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise RuntimeError(f"找不到模板：{template_path}") from error

    placeholder = "{{ DYNAMIC_JSON_DATA }}"
    if template.count(placeholder) != 1:
        raise RuntimeError(f"模板必須且只能包含一個 {placeholder}")
    html_output = template.replace(placeholder, _safe_embedded_json(data))
    atomic_write_text(output_path, html_output)
    print(
        f"網頁生成成功：{len(data['tickers'])} 組股票，"
        f"原子寫入 {output_path.name}。"
    )
    return data


if __name__ == "__main__":
    try:
        build_static_site()
    except Exception as error:
        raise SystemExit(f"建站失敗：{error}") from error
