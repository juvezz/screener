"""Refresh listing catalogs from Nasdaq Symbol Directory and Yahoo Finance."""

import argparse
import csv
import io
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import requests
import yfinance as yf

from catalog import CATALOG_FIELDS, MARKETS, Market, default_catalog_dir, load_universe


class UpdateError(Exception):
    """A catalog could not be downloaded completely and validated."""


NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
NON_STOCK_NAME = re.compile(
    r"\b(?:warrants?|rights|debentures)\b|"
    r"\b(?:preferred|preference)\s+(?:stock|shares|securities)\b|"
    r"\b(?:senior|subordinated|unsecured)\s+notes\b|"
    r"(?: - Units\b|\bUnits,\s*each)", re.IGNORECASE,
)


def parse_nasdaq_directory(text: str, nasdaq: bool) -> list[dict]:
    lines = text.strip().splitlines()
    if len(lines) < 3 or not lines[-1].startswith("File Creation Time:"):
        raise UpdateError("The Nasdaq directory is incomplete because the footer is missing")
    reader = csv.DictReader(io.StringIO("\n".join(lines[:-1])), delimiter="|")
    symbol_field = "Symbol" if nasdaq else "ACT Symbol"
    exchange_field = "Market Category" if nasdaq else "Exchange"
    required = {symbol_field, exchange_field, "Security Name", "ETF", "Test Issue"}
    if not required.issubset(reader.fieldnames or []):
        raise UpdateError("The Nasdaq directory format has changed")
    exchange_map = {"Q": "NMS", "G": "NGM", "S": "NCM"} if nasdaq else {"N": "NYQ", "A": "ASE"}
    result = []
    seen = set()
    for row in reader:
        symbol = (row.get(symbol_field) or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or not name or row.get("ETF") not in ("Y", "N") or row.get("Test Issue") not in ("Y", "N"):
            raise UpdateError("Incomplete entry in the Nasdaq directory")
        if symbol in seen:
            raise UpdateError(f"Duplicate ticker in the Nasdaq directory: {symbol}")
        seen.add(symbol)
        exchange = exchange_map.get(row.get(exchange_field))
        if (not exchange or row["ETF"] == "Y" or row["Test Issue"] == "Y"
                or row.get("NextShares") == "Y" or NON_STOCK_NAME.search(name)):
            continue
        # Share classes use a dash at Yahoo (BRK.B -> BRK-B). Other special
        # suffixes need explicit mapping and are excluded from this catalog.
        if not re.fullmatch(r"[A-Z]+(?:\.[A-Z])?", symbol):
            continue
        result.append({"symbol": symbol.replace(".", "-"), "name": name, "exchange": exchange})
    if not result:
        raise UpdateError("No eligible stocks remain in the Nasdaq directory")
    return result


def fetch_us(retries: int) -> list[dict]:
    result = {}
    for filename in ("nasdaqlisted.txt", "otherlisted.txt"):
        for attempt in range(retries + 1):
            try:
                response = requests.get(NASDAQ_URL + filename, timeout=(10, 45))
                response.raise_for_status()
                rows = parse_nasdaq_directory(response.text, filename == "nasdaqlisted.txt")
                break
            except (requests.RequestException, UpdateError) as exc:
                if attempt == retries:
                    raise UpdateError(f"Could not download {filename}: {exc}") from exc
                time.sleep(2 ** attempt)
        for row in rows:
            if row["symbol"] in result:
                raise UpdateError(f"Duplicate ticker across Nasdaq directories: {row['symbol']}")
            result[row["symbol"]] = row
        print(f"  us/{filename}: {len(rows)} instruments after filtering", file=sys.stderr)
    updated_at = datetime.now(timezone.utc).isoformat()
    return [dict(row, region="us", country="us", source="nasdaqtrader:symbol-directory:filtered", updated_at=updated_at)
            for symbol, row in sorted(result.items())]


def fetch_page(query: yf.EquityQuery, offset: int, retries: int) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            response = yf.screen(query, offset=offset, size=250, sortField="ticker", sortAsc=True)
            if not isinstance(response, dict):
                raise UpdateError("Yahoo returned an unrecognized response format")
            return response
        except Exception as exc:
            if attempt == retries:
                raise UpdateError(f"Could not load page offset={offset}: {exc}") from exc
            time.sleep(2 ** attempt)
    raise AssertionError("Unreachable")


def fetch_exchange(country: str, exchange: str, delay: float, retries: int) -> list[dict]:
    query = yf.EquityQuery("and", [
        yf.EquityQuery("eq", ["region", country]),
        yf.EquityQuery("eq", ["exchange", exchange]),
        yf.EquityQuery("gt", ["intradaymarketcap", 0]),
    ])
    quotes = {}
    offset = 0
    total = None
    while True:
        response = fetch_page(query, offset, retries)
        page_total = response.get("total")
        page = response.get("quotes")
        if type(page_total) is not int or page_total <= 0 or not isinstance(page, list):
            raise UpdateError(f"{exchange}: empty or invalid response. The previous file was kept")
        if total is not None and page_total != total:
            print(f"  {country}/{exchange}: total changed from {total} to {page_total}. "
                  "The completeness check continues using the latest total", file=sys.stderr)
        total = page_total
        if response.get("start", offset) != offset:
            raise UpdateError(f"{exchange}: the provider ignored the page offset")
        if not page or len(page) > 250:
            raise UpdateError(f"{exchange}: incomplete results ({len(quotes)} of {total})")
        for quote in page:
            if not isinstance(quote, dict):
                raise UpdateError(f"{exchange}: invalid entry")
            symbol = quote.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise UpdateError(f"{exchange}: entry is missing a ticker")
            symbol = symbol.strip().upper()
            if symbol in quotes:
                raise UpdateError(f"{exchange}: duplicate ticker {symbol} indicates unreliable pagination")
            quotes[symbol] = {**quote, "symbol": symbol}
        offset += len(page)
        print(f"  {country}/{exchange}: {offset}/{total}", file=sys.stderr)
        if offset == total:
            # Yahoo can return ETFs/funds even for an EQUITY query. Count every
            # row for pagination, then retain only instruments typed EQUITY.
            # The screener's exchange index can lag behind a listing transfer
            # (e.g. an ASE stock returned by the NYQ query). Trust the quote's
            # actual venue if it belongs to the configured country universe.
            equities = [quote for quote in quotes.values()
                        if quote.get("quoteType") == "EQUITY"
                        and quote.get("exchange") in MARKETS[country].exchanges]
            excluded = len(quotes) - len(equities)
            if excluded:
                print(f"  {country}/{exchange}: excluded {excluded} entries from other markets or asset types", file=sys.stderr)
            return equities
        if offset > total:
            raise UpdateError(f"{exchange}: result count exceeds total")
        time.sleep(delay)


def fetch_country(country: str, market: Market, delay: float, retries: int) -> list[dict]:
    quotes = {}
    for index, exchange in enumerate(market.exchanges):
        if index:
            time.sleep(delay)
        for quote in fetch_exchange(country, exchange, delay, retries):
            quotes[quote["symbol"]] = quote
    updated_at = datetime.now(timezone.utc).isoformat()
    return [
        {
            "symbol": symbol, "region": market.region, "country": country,
            "name": quote.get("longName") or quote.get("shortName") or symbol,
            "exchange": quote["exchange"], "source": "yahoo:equity-screener:market-cap-positive",
            "updated_at": updated_at,
        }
        for symbol, quote in sorted(quotes.items())
    ]


def validate_adrs(path: Path, delay: float) -> list[dict]:
    # Yahoo's exchange-region query cannot discover issuer nationality. Keep
    # curated membership explicit rather than labelling every US ADR Chinese.
    symbols = [symbol for symbol, _ in load_universe(path, ["china_adr"])]
    if not symbols:
        raise UpdateError("The Chinese ADR list is empty")
    result = []
    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(delay)
        try:
            info = yf.Ticker(symbol).get_info()
        except Exception as exc:
            raise UpdateError(f"Could not validate ADR {symbol}: {exc}") from exc
        if (
            not isinstance(info, dict)
            or info.get("quoteType") != "EQUITY"
            or info.get("exchange") not in MARKETS["us"].exchanges
        ):
            raise UpdateError(f"{symbol}: Yahoo could not confirm a stock listing on a supported US exchange")
        result.append({
            "symbol": symbol, "region": "china_adr", "country": "us",
            "name": info.get("longName") or info.get("shortName") or symbol,
            "exchange": info["exchange"], "source": "yahoo:validated-curated-list",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    return result


def save_catalog(path: Path, rows: list[dict], dry_run: bool = False) -> tuple[int, int]:
    symbols = [row["symbol"] for row in rows]
    if not symbols or len(symbols) != len(set(symbols)):
        raise UpdateError("Save canceled because the list is empty or contains duplicate tickers")
    previous = {symbol for symbol, _ in load_universe(path)} if path.exists() else set()
    current = set(symbols)
    changes = len(current - previous), len(previous - current)
    if dry_run:
        return changes
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.stem}-", suffix=".tmp", delete=False,
        ) as target:
            temporary_path = Path(target.name)
            writer = csv.DictWriter(target, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            target.flush()
            os.fsync(target.fileno())
        # Validate the serialized file before replacing a working catalog.
        load_universe(temporary_path)
        if path.exists():
            backup = path.parent / ".backups" / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return changes


def nonnegative_delay(value: str) -> float:
    result = float(value)
    if not isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("Delay must be a finite number >= 0")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update stock catalogs by listing country")
    parser.add_argument("--country", nargs="+", choices=MARKETS, help="Defaults to all countries")
    parser.add_argument("--directory", type=Path, default=default_catalog_dir())
    parser.add_argument("--dry-run", action="store_true", help="Download and show changes without writing files")
    parser.add_argument("--check-adrs", action="store_true", help="Also validate the existing ADR list")
    parser.add_argument("--delay", type=nonnegative_delay, default=0.5, help="Delay between pages in seconds")
    parser.add_argument("--retries", type=int, default=2, help="Retries after a network error while fetching a page")
    args = parser.parse_args(argv)
    if not 0 <= args.retries <= 5:
        parser.error("--retries must be between 0 and 5")
    yf.set_tz_cache_location(str(Path(".cache/yfinance")))
    failures = 0
    countries = list(dict.fromkeys(args.country or MARKETS))
    for index, country in enumerate(countries):
        if index:
            time.sleep(args.delay)
        market = MARKETS[country]
        path = args.directory / market.filename
        try:
            rows = fetch_us(args.retries) if country == "us" else fetch_country(country, market, args.delay, args.retries)
            added, removed = save_catalog(path, rows, args.dry_run)
            print(f"{path.name}: {len(rows)} stocks; +{added} / -{removed}"
                  + (" (dry run)" if args.dry_run else ""))
        except (UpdateError, OSError, ValueError) as exc:
            failures += 1
            print(f"{country}: {exc}", file=sys.stderr)
    if args.check_adrs:
        path = args.directory / "china_adr.csv"
        try:
            rows = validate_adrs(path, args.delay)
            save_catalog(path, rows, args.dry_run)
            print(f"china_adr.csv: validated {len(rows)} tickers"
                  + (" (dry run)" if args.dry_run else ""))
        except (UpdateError, OSError, ValueError) as exc:
            failures += 1
            print(f"china_adr: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
