import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

CACHE_TTL = 7 * 86400
MARKET_DB_PATH = Path(".cache/screener.sqlite3")


class DataError(Exception):
    """A ticker could not be loaded or has no usable equity fundamentals."""


class RateLimitError(DataError):
    """Yahoo Finance requested that the caller stop sending requests."""


def timestamp_iso(saved_at: float) -> str:
    return datetime.fromtimestamp(saved_at, timezone.utc).isoformat()


class InfoCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS fundamentals "
            "(symbol TEXT PRIMARY KEY, saved_at REAL NOT NULL, payload TEXT NOT NULL)"
        )

    def get(self, symbol: str, ttl: float | None) -> tuple[dict[str, Any], float] | None:
        row = self.connection.execute(
            "SELECT payload, saved_at FROM fundamentals WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None or (ttl is not None and time.time() - row[1] >= ttl):
            return None
        return json.loads(row[0]), row[1]

    def put(self, symbol: str, info: dict[str, Any], saved_at: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO fundamentals VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET saved_at = excluded.saved_at, payload = excluded.payload "
                "WHERE excluded.saved_at >= fundamentals.saved_at",
                (symbol, saved_at, json.dumps(info)),
            )

    def close(self) -> None:
        self.connection.close()


def validate_info(info: Any) -> None:
    if not isinstance(info, dict) or info.get("quoteType") != "EQUITY":
        raise DataError("The response contained no stock data (quoteType=EQUITY)")
    fields = ("marketCap", "trailingPE", "priceToBook", "totalRevenue", "returnOnEquity")
    if not any(info.get(field) is not None for field in fields):
        raise DataError("The response contained no fundamental metrics")


class YahooProvider:
    def __init__(self, cache: InfoCache, cache_dir: Path, ttl: float = CACHE_TTL):
        yf.set_tz_cache_location(str(cache_dir / "yfinance"))
        self.cache = cache
        self.ttl = ttl

    @staticmethod
    def fetch_remote(symbol: str) -> tuple[dict[str, Any], float]:
        try:
            info = yf.Ticker(symbol).get_info()
            validate_info(info)
        except YFRateLimitError as exc:
            raise RateLimitError(f"Yahoo Finance error: {exc}") from exc
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"Yahoo Finance error: {exc}") from exc
        return info, time.time()

    def fetch(self, symbol: str, refresh: bool = False) -> tuple[dict[str, Any], str]:
        cached = None if refresh else self.cache.get(symbol, self.ttl)
        if cached is not None:
            info, saved_at = cached
            validate_info(info)
        else:
            info, saved_at = self.fetch_remote(symbol)
            self.cache.put(symbol, info, saved_at)
        return info, timestamp_iso(saved_at)
