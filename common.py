"""Shared helpers for stable post identities and atomic file writes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
POST_ID_PATTERN = re.compile(r"/status/(\d+)")
CASHTAG_PATTERN = re.compile(
    r"(?<![\w$])\$([A-Za-z0-9][A-Za-z0-9.-]{0,14})(?![A-Za-z0-9.-])"
)
MONEY_TOKEN_PATTERN = re.compile(r"^\d+(?:\.\d+)?[KMBT]$", re.IGNORECASE)
DECIMAL_AMOUNT_PATTERN = re.compile(r"^\d+\.\d+$")


def extract_post_id(url: str) -> str | None:
    """Return the numeric X status id from a canonical or query-string URL."""
    match = POST_ID_PATTERN.search(url or "")
    return match.group(1) if match else None


def canonical_x_url(post_id: str, account: str) -> str:
    return f"https://x.com/{account}/status/{post_id}"


def extract_tickers(text: str) -> list[str]:
    """Extract explicit cashtags while excluding common dollar amounts."""
    seen: set[str] = set()
    tickers: list[str] = []
    for match in CASHTAG_PATTERN.finditer(text or ""):
        ticker = match.group(1).upper()
        if MONEY_TOKEN_PATTERN.fullmatch(ticker):
            continue
        if DECIMAL_AMOUNT_PATTERN.fullmatch(ticker):
            continue
        if ticker.isdigit() and len(ticker) < 4:
            continue
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def is_safe_x_url(url: str) -> bool:
    """Allow only HTTPS links to x.com and its subdomains."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "x.com" or host.endswith(".x.com"))


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file only after the full new contents are on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
