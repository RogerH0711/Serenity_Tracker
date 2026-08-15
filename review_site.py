"""Serve Serenity Tracker locally with a safe browser-based review API."""

from __future__ import annotations

import argparse
from collections import deque
import json
import secrets
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from build_site import (
    ALIASES_PATH,
    OUTPUT_PATH,
    TEMPLATE_PATH,
    build_static_site,
    load_dashboard_data,
    load_ticker_aliases,
)
from common import PROJECT_DIR
from parser import _error_code
from prices import build_market_payload, refresh_prices
from qa import answer_question
from storage import (
    DB_PATH,
    connect_db,
    delete_mention_override,
    init_db,
    set_mention_override,
)


MAX_REQUEST_BYTES = 64 * 1024
VALID_SENTIMENTS = {"Bullish", "Bearish", "Neutral"}
AI_REQUESTS_PER_MINUTE = 6


class LocalRateLimitError(RuntimeError):
    status_code = 429


def _find_ticker(
    raw_ticker: str,
    *,
    db_path: Path,
    aliases_path: Path,
) -> dict[str, object]:
    requested = raw_ticker.strip().upper().removeprefix("$")
    if not requested:
        raise ValueError("ticker 不可為空")
    alias_map, _profiles = load_ticker_aliases(aliases_path)
    canonical = alias_map.get(requested, requested)
    dashboard = load_dashboard_data(db_path, aliases_path)
    ticker = next(
        (item for item in dashboard["tickers"] if item["ticker"] == canonical),
        None,
    )
    if ticker is None:
        raise LookupError(f"找不到 {requested} 的研究資料")
    return ticker


def market_payload(
    raw_ticker: str,
    *,
    db_path: Path = DB_PATH,
    aliases_path: Path = ALIASES_PATH,
) -> dict[str, object]:
    ticker = _find_ticker(
        raw_ticker,
        db_path=db_path,
        aliases_path=aliases_path,
    )
    return build_market_payload(db_path=db_path, ticker=ticker)


def _required_text(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 不可為空")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{key} 超過 {maximum} 字")
    return normalized


def _optional_text(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} 必須是文字")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{key} 超過 {maximum} 字")
    return normalized


def _review_identity(payload: dict[str, Any]) -> tuple[str, str]:
    post_id = _required_text(payload, "post_id", 40)
    ticker = _required_text(payload, "ticker", 40).upper()
    if not post_id.isdigit():
        raise ValueError("post_id 格式無效")
    if any(ord(character) < 32 for character in ticker):
        raise ValueError("ticker 格式無效")
    return post_id, ticker


def save_review(
    payload: dict[str, Any],
    *,
    db_path: Path = DB_PATH,
    template_path: Path = TEMPLATE_PATH,
    aliases_path: Path = ALIASES_PATH,
    output_path: Path = OUTPUT_PATH,
) -> None:
    post_id, ticker = _review_identity(payload)
    sentiment = _required_text(payload, "sentiment", 20).title()
    if sentiment not in VALID_SENTIMENTS:
        raise ValueError("sentiment 必須是 Bullish、Bearish 或 Neutral")
    thesis = _required_text(payload, "thesis", 2000)
    evidence = _optional_text(payload, "sentiment_evidence", 1000)
    risks = _optional_text(payload, "risks", 2000)
    note = _required_text(payload, "review_note", 1000)
    reviewer = _optional_text(payload, "reviewer", 100) or "local-web"

    init_db(db_path)
    connection = connect_db(db_path)
    try:
        set_mention_override(
            connection,
            post_id=post_id,
            ticker=ticker,
            sentiment=sentiment,
            sentiment_evidence=evidence,
            thesis=thesis,
            risks=risks or None,
            review_note=note,
            reviewer=reviewer,
        )
    finally:
        connection.close()
    build_static_site(
        db_path=db_path,
        template_path=template_path,
        aliases_path=aliases_path,
        output_path=output_path,
    )


def remove_review(
    payload: dict[str, Any],
    *,
    db_path: Path = DB_PATH,
    template_path: Path = TEMPLATE_PATH,
    aliases_path: Path = ALIASES_PATH,
    output_path: Path = OUTPUT_PATH,
) -> None:
    post_id, ticker = _review_identity(payload)
    init_db(db_path)
    connection = connect_db(db_path)
    try:
        deleted = delete_mention_override(connection, post_id, ticker)
    finally:
        connection.close()
    if not deleted:
        raise LookupError("找不到這筆人工覆核")
    build_static_site(
        db_path=db_path,
        template_path=template_path,
        aliases_path=aliases_path,
        output_path=output_path,
    )


@dataclass(frozen=True)
class ReviewApplication:
    db_path: Path
    template_path: Path
    aliases_path: Path
    output_path: Path
    token: str
    ai_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    ai_requests: deque[float] = field(default_factory=deque, compare=False)

    def allow_ai_request(self) -> bool:
        now = time.monotonic()
        with self.ai_lock:
            while self.ai_requests and now - self.ai_requests[0] >= 60:
                self.ai_requests.popleft()
            if len(self.ai_requests) >= AI_REQUESTS_PER_MINUTE:
                return False
            self.ai_requests.append(now)
            return True


class ReviewHTTPServer(ThreadingHTTPServer):
    application: ReviewApplication

    def __init__(
        self,
        server_address: tuple[str, int],
        application: ReviewApplication,
    ) -> None:
        self.application = application
        super().__init__(server_address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, format_string: str, *arguments: object) -> None:
        print(f"[review] {self.address_string()} {format_string % arguments}")

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; frame-ancestors 'none'")

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _allowed_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        port = self.server.server_port
        return origin in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _authorized(self) -> bool:
        return (
            self._allowed_origin()
            and self.headers.get("X-Review-Token", "")
            == self.server.application.token
            and self.headers.get_content_type() == "application/json"
        )

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length 格式無效") from error
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise ValueError("請求內容大小無效")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("請求不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("請求必須是 JSON 物件")
        return payload

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        if path == "/api/review/status":
            self._send_json(
                HTTPStatus.OK,
                {
                    "enabled": True,
                    "token": self.server.application.token,
                    "capabilities": {
                        "review": True,
                        "market": True,
                        "ai": bool(os.getenv("GEMINI_API_KEY", "").strip()),
                    },
                },
            )
            return
        if path == "/api/market":
            try:
                ticker = parse_qs(parsed_url.query).get("ticker", [""])[0]
                payload = market_payload(
                    ticker,
                    db_path=self.server.application.db_path,
                    aliases_path=self.server.application.aliases_path,
                )
            except LookupError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except Exception as error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"讀取行情失敗：{error}"},
                )
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path not in {"/", "/index.html"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到頁面"})
            return
        try:
            body = self.server.application.output_path.read_bytes()
        except FileNotFoundError:
            build_static_site(
                db_path=self.server.application.db_path,
                template_path=self.server.application.template_path,
                aliases_path=self.server.application.aliases_path,
                output_path=self.server.application.output_path,
            )
            body = self.server.application.output_path.read_bytes()
        self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {
            "/api/reviews",
            "/api/reviews/delete",
            "/api/market/refresh",
            "/api/ask",
        }:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到 API"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "本機請求驗證失敗"})
            return
        try:
            payload = self._read_json()
            response_payload: dict[str, object]
            if path == "/api/reviews":
                save_review(
                    payload,
                    db_path=self.server.application.db_path,
                    template_path=self.server.application.template_path,
                    aliases_path=self.server.application.aliases_path,
                    output_path=self.server.application.output_path,
                )
                message = "人工覆核已儲存"
                response_payload = {"ok": True, "message": message}
            elif path == "/api/reviews/delete":
                remove_review(
                    payload,
                    db_path=self.server.application.db_path,
                    template_path=self.server.application.template_path,
                    aliases_path=self.server.application.aliases_path,
                    output_path=self.server.application.output_path,
                )
                message = "人工覆核已移除"
                response_payload = {"ok": True, "message": message}
            elif path == "/api/market/refresh":
                ticker = _required_text(payload, "ticker", 40).upper()
                refresh_prices(
                    db_path=self.server.application.db_path,
                    aliases_path=self.server.application.aliases_path,
                    max_tickers=1,
                    force_tickers=[ticker],
                )
                response_payload = market_payload(
                    ticker,
                    db_path=self.server.application.db_path,
                    aliases_path=self.server.application.aliases_path,
                )
            else:
                question = _required_text(payload, "question", 500)
                if not self.server.application.allow_ai_request():
                    raise LocalRateLimitError(
                        f"本機 AI 每分鐘最多 {AI_REQUESTS_PER_MINUTE} 次，請稍後再試"
                    )
                response_payload = answer_question(
                    question,
                    db_path=self.server.application.db_path,
                    aliases_path=self.server.application.aliases_path,
                )
        except LookupError as error:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except Exception as error:
            error_code = _error_code(error)
            status = (
                HTTPStatus.TOO_MANY_REQUESTS
                if error_code == 429
                else HTTPStatus.BAD_GATEWAY
                if error_code is not None and error_code >= 500
                else HTTPStatus.INTERNAL_SERVER_ERROR
            )
            self._send_json(
                status,
                {"error": f"本機操作失敗：{error}"},
            )
            return
        self._send_json(HTTPStatus.OK, response_payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("port 必須介於 1 到 65535")

    load_dotenv(PROJECT_DIR / ".env")
    init_db(DB_PATH)
    build_static_site()
    application = ReviewApplication(
        db_path=DB_PATH,
        template_path=TEMPLATE_PATH,
        aliases_path=ALIASES_PATH,
        output_path=OUTPUT_PATH,
        token=secrets.token_urlsafe(32),
    )
    server = ReviewHTTPServer(("127.0.0.1", arguments.port), application)
    print(f"Serenity 本機研究站：http://127.0.0.1:{arguments.port}")
    print("已啟用人工覆核與本機行情；設定 GEMINI_API_KEY 後可使用 AI 問答。")
    print("只有這個本機網址可以寫入資料庫；按 Ctrl+C 關閉。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSerenity 本機研究站已關閉。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
