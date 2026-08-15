"""Detect, review, and approve conservative ticker alias suggestions."""

from __future__ import annotations

import argparse
import json
import os
import re

from build_site import ALIASES_PATH, load_ticker_aliases
from common import atomic_write_json
from storage import (
    DB_PATH,
    connect_db,
    init_db,
    list_alias_candidates,
    record_pipeline_alias_candidates,
    review_alias_candidate,
    upsert_alias_candidate,
)


EXCHANGE_SUFFIX = re.compile(r"^([A-Z0-9-]+)\.([A-Z]{1,4})$")


def _suggest_pairs(tickers: set[str], alias_map: dict[str, str]):
    suggestions: dict[tuple[str, str], tuple[str, float]] = {}
    for ticker in sorted(tickers):
        match = EXCHANGE_SUFFIX.fullmatch(ticker)
        if match and match.group(1) in tickers:
            canonical, alias = ticker, match.group(1)
            if alias_map.get(alias, alias) != alias_map.get(canonical, canonical):
                suggestions[(canonical, alias)] = (
                    f"{alias} 與交易所後綴代碼 {canonical} 同時出現",
                    0.98,
                )

        if ticker.endswith("F") and len(ticker) > 2 and ticker[:-1] in tickers:
            canonical, alias = ticker[:-1], ticker
            if alias_map.get(alias, alias) != alias_map.get(canonical, canonical):
                suggestions[(canonical, alias)] = (
                    f"{alias} 可能是 {canonical} 的海外／OTC F-share 代碼",
                    0.72,
                )
    return suggestions


def scan_candidates(db_path=DB_PATH, aliases_path=ALIASES_PATH) -> dict[str, int]:
    init_db(db_path)
    alias_map, _profiles = load_ticker_aliases(aliases_path)
    connection = connect_db(db_path)
    try:
        tickers = {
            str(row["ticker"]).strip().upper()
            for row in connection.execute("SELECT DISTINCT ticker FROM mentions")
        }
        suggestions = _suggest_pairs(tickers, alias_map)
        created = 0
        for (canonical, alias), (reason, confidence) in suggestions.items():
            created += int(
                upsert_alias_candidate(
                    connection,
                    canonical_ticker=canonical,
                    alias=alias,
                    reason=reason,
                    confidence=confidence,
                )
            )
        run_id = os.getenv("SERENITY_PIPELINE_RUN_ID", "").strip()
        if run_id.isdigit():
            record_pipeline_alias_candidates(connection, int(run_id), created)
        pending = len(list_alias_candidates(connection, "pending"))
    finally:
        connection.close()
    print(f"Alias 掃描完成：新增 {created} 筆候選，目前待審核 {pending} 筆。")
    return {"created": created, "pending": pending}


def _load_alias_payload(path=ALIASES_PATH) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} 最外層必須是物件")
    return payload


def approve_candidate(
    candidate_id: int,
    *,
    company_name: str = "",
    exchange: str = "",
    note: str = "",
    db_path=DB_PATH,
    aliases_path=ALIASES_PATH,
) -> bool:
    init_db(db_path)
    connection = connect_db(db_path)
    try:
        candidate = connection.execute(
            "SELECT * FROM ticker_alias_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            return False
        canonical = str(candidate["canonical_ticker"]).upper()
        alias = str(candidate["alias"]).upper()
        payload = _load_alias_payload(aliases_path)
        alias_map, _profiles = load_ticker_aliases(aliases_path)
        canonical_owner = alias_map.get(canonical)
        if canonical_owner and canonical_owner != canonical:
            raise RuntimeError(
                f"候選 canonical {canonical} 目前已屬於 {canonical_owner}"
            )
        owner = alias_map.get(alias)
        if owner and owner not in {canonical, alias}:
            raise RuntimeError(f"{alias} 已經屬於 {owner}，不能再指向 {canonical}")

        canonical_profile = payload.setdefault(
            canonical,
            {"aliases": [], "company_name": "", "exchange": ""},
        )
        raw_aliases = canonical_profile.setdefault("aliases", [])
        if not isinstance(raw_aliases, list):
            raise RuntimeError(f"{canonical} 的 aliases 必須是陣列")
        if alias != canonical and alias not in raw_aliases:
            raw_aliases.append(alias)

        if alias in payload and alias != canonical:
            alias_profile = payload.pop(alias)
            for nested_alias in alias_profile.get("aliases", []):
                normalized = str(nested_alias).strip().upper()
                if normalized and normalized != canonical and normalized not in raw_aliases:
                    raw_aliases.append(normalized)
            if not canonical_profile.get("company_name"):
                canonical_profile["company_name"] = alias_profile.get("company_name", "")
            if not canonical_profile.get("exchange"):
                canonical_profile["exchange"] = alias_profile.get("exchange", "")

        if company_name.strip():
            canonical_profile["company_name"] = company_name.strip()
        if exchange.strip():
            canonical_profile["exchange"] = exchange.strip()
        atomic_write_json(aliases_path, payload)
        load_ticker_aliases(aliases_path)
        review_alias_candidate(
            connection,
            candidate_id,
            status="approved",
            note=note,
        )
    finally:
        connection.close()
    print(f"已核准 alias：{alias} → {canonical}，並更新 {aliases_path.name}。")
    return True


def reject_candidate(
    candidate_id: int,
    *,
    note: str = "",
    db_path=DB_PATH,
) -> bool:
    init_db(db_path)
    connection = connect_db(db_path)
    try:
        candidate = review_alias_candidate(
            connection,
            candidate_id,
            status="rejected",
            note=note,
        )
    finally:
        connection.close()
    if candidate is None:
        return False
    print(f"已拒絕 alias 候選：{candidate['alias']} → {candidate['canonical_ticker']}。")
    return True


def print_candidates(status: str | None, db_path=DB_PATH) -> None:
    init_db(db_path)
    connection = connect_db(db_path)
    try:
        rows = list_alias_candidates(connection, status)
    finally:
        connection.close()
    if not rows:
        print("目前沒有符合條件的 alias 候選。")
        return
    for row in rows:
        print(
            f"#{row['id']}  {row['alias']} → {row['canonical_ticker']}  "
            f"{row['confidence']:.0%}  [{row['status']}]\n"
            f"    {row['reason']}"
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan", help="掃描新 alias 候選")

    listing = commands.add_parser("list", help="列出 alias 候選")
    listing.add_argument(
        "--status",
        choices=["pending", "approved", "rejected", "all"],
        default="pending",
    )

    approve = commands.add_parser("approve", help="核准候選並更新 JSON")
    approve.add_argument("candidate_id", type=int)
    approve.add_argument("--company-name", default="")
    approve.add_argument("--exchange", default="")
    approve.add_argument("--note", default="")

    reject = commands.add_parser("reject", help="拒絕候選並避免重複提示")
    reject.add_argument("candidate_id", type=int)
    reject.add_argument("--note", default="")
    return parser


def main() -> int:
    arguments = build_argument_parser().parse_args()
    if arguments.command == "scan":
        scan_candidates()
        return 0
    if arguments.command == "list":
        print_candidates(None if arguments.status == "all" else arguments.status)
        return 0
    if arguments.command == "approve":
        approved = approve_candidate(
            arguments.candidate_id,
            company_name=arguments.company_name,
            exchange=arguments.exchange,
            note=arguments.note,
        )
        if not approved:
            print("找不到指定 alias 候選。")
            return 1
        return 0
    rejected = reject_candidate(arguments.candidate_id, note=arguments.note)
    if not rejected:
        print("找不到指定 alias 候選。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
