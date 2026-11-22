"""Persisted market data read by the screener and written by the refresh jobs."""

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from itertools import islice
from pathlib import Path
from typing import TextIO

from models import Fundamentals
from provider import InfoCache, timestamp_iso


@dataclass(frozen=True)
class Price:
    symbol: str
    value: float
    currency: str
    quoted_at: float


@dataclass(frozen=True)
class MarketRow:
    fundamentals: Fundamentals
    price: Price | None

    def as_record(self) -> dict:
        row = asdict(self.fundamentals)
        row["price"] = self.price.value if self.price else None
        row["price_currency"] = self.price.currency if self.price else None
        row["price_at"] = timestamp_iso(self.price.quoted_at) if self.price else None
        return row


class MarketStore(InfoCache):
    def __init__(self, path: Path, *, readonly: bool = False):
        if readonly:
            self.connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            return
        super().__init__(path)
        try:
            if self.connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS latest_prices "
                "(symbol TEXT PRIMARY KEY, price REAL NOT NULL, currency TEXT NOT NULL, quoted_at REAL NOT NULL)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS failed_fundamental_updates "
                "(symbol TEXT PRIMARY KEY, attempted_at REAL NOT NULL)"
            )
        except Exception:
            self.close()
            raise

    def read_market(self, universe: list[tuple[str, str]]) -> list[MarketRow]:
        records = {}
        symbols = iter(symbol for symbol, _ in universe)
        with self.connection:
            self.connection.execute("BEGIN")
            has_prices = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'latest_prices'",
            ).fetchone()
            while batch := tuple(islice(symbols, 500)):
                placeholders = ",".join("?" for _ in batch)
                if has_prices:
                    prices = self.connection.execute(
                        f"SELECT symbol, price, currency, quoted_at FROM latest_prices WHERE symbol IN ({placeholders})",
                        batch,
                    )
                    for symbol, value, currency, quoted_at in prices:
                        records[symbol] = ({}, None, Price(symbol, value, currency, quoted_at))
                profiles = self.connection.execute(
                    f"SELECT symbol, saved_at, payload FROM fundamentals WHERE symbol IN ({placeholders})", batch,
                )
                for symbol, saved_at, payload in profiles:
                    _, _, price = records.get(symbol, ({}, None, None))
                    records[symbol] = (json.loads(payload), timestamp_iso(saved_at), price)

        rows = []
        for symbol, region in universe:
            if symbol in records:
                info, fetched_at, price = records[symbol]
                if price and not info.get("currency"):
                    info = {**info, "currency": price.currency}
                rows.append(MarketRow(Fundamentals.from_info(symbol, region, info, fetched_at), price))
        return rows

    def due_fundamentals(self, symbols: list[str], refresh_before: float, retry_before: float) -> list[str]:
        fresh = set(self.connection.execute(
            "SELECT symbol FROM fundamentals WHERE saved_at > ?", (refresh_before,),
        ))
        deferred = set(self.connection.execute(
            "SELECT symbol FROM failed_fundamental_updates WHERE attempted_at > ?", (retry_before,),
        ))
        return [symbol for symbol in symbols if (symbol,) not in fresh and (symbol,) not in deferred]

    def record_fundamental_failure(self, symbol: str, attempted_at: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO failed_fundamental_updates VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET attempted_at = max(attempted_at, excluded.attempted_at)",
                (symbol, attempted_at),
            )

    def save_prices(self, prices: list[Price]) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO latest_prices VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET price = excluded.price, currency = excluded.currency, "
                "quoted_at = excluded.quoted_at "
                "WHERE excluded.quoted_at >= latest_prices.quoted_at",
                ((price.symbol, price.value, price.currency, price.quoted_at) for price in prices),
            )


def write_market_csv(target: TextIO, rows: list[MarketRow]) -> None:
    names = [field.name for field in fields(Fundamentals)] + ["price", "price_currency", "price_at"]
    writer = csv.DictWriter(target, fieldnames=names)
    writer.writeheader()
    writer.writerows(row.as_record() for row in rows)
