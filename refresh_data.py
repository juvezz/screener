"""Refresh the market database independently of the interactive screener."""

import argparse
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from itertools import islice
from pathlib import Path
from time import monotonic, sleep, time

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from catalog import MARKETS, default_catalog_dir, load_universe
from market_data import MarketStore, Price
from models import number
from provider import CACHE_TTL, MARKET_DB_PATH, DataError, RateLimitError, YahooProvider

PRICE_INTERVAL = 60
FUNDAMENTAL_CHECK_INTERVAL = 900
FUNDAMENTAL_BATCH_SIZE = 200
FAILED_TICKER_RETRY = 86400
WORKERS = 4
FUNDAMENTAL_FIELDS = (
    "quoteType", "longName", "shortName", "fullExchangeName", "exchange", "currency",
    "financialCurrency", "sector", "marketCap", "trailingPE", "priceToBook", "revenueGrowth",
    "returnOnEquity", "profitMargins", "debtToEquity", "trailingAnnualDividendYield", "totalRevenue",
)


def fetch_yahoo_price(symbol: str) -> Price:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="1d", interval="1m", auto_adjust=False, timeout=8)
    if history.empty:
        raise DataError("The provider returned no one-minute prices")
    closes = history["Close"].dropna()
    if closes.empty or closes.index.tz is None:
        raise DataError("The provider returned no price with a trading timestamp")
    value = number(closes.iloc[-1])
    metadata = ticker.get_history_metadata()
    currency = metadata.get("currency")
    if value is None or value <= 0 or not currency:
        raise DataError("The provider returned no price in the listing currency")
    return Price(symbol, value, currency, closes.index[-1].timestamp())


def refresh_fundamentals(store: MarketStore, symbols: list[str]) -> bool:
    now = time()
    due = store.due_fundamentals(symbols, now - CACHE_TTL, now - FAILED_TICKER_RETRY)
    pending = iter(due[:FUNDAMENTAL_BATCH_SIZE])
    saved = failures = 0
    limited = False
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while not limited and (batch := list(islice(pending, WORKERS))):
            futures = {pool.submit(YahooProvider.fetch_remote, symbol): symbol for symbol in batch}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    info, saved_at = future.result()
                except RateLimitError:
                    limited = True
                except DataError as exc:
                    failures += 1
                    store.record_fundamental_failure(symbol, time())
                    logging.warning("%s: %s", symbol, exc)
                else:
                    profile = {key: info[key] for key in FUNDAMENTAL_FIELDS if key in info}
                    store.put(symbol, profile, saved_at)
                    saved += 1
    logging.info("Fundamentals: %d updated, %d initially queued, %d errors", saved, len(due), failures)
    if limited:
        logging.warning("Yahoo rate-limited requests. Remaining tickers will be processed later.")
    return not limited and failures == 0


def refresh_prices(store: MarketStore, symbols: list[str]) -> bool:
    prices = []
    pending = iter(symbols)
    limited = False
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while not limited and (batch := list(islice(pending, WORKERS))):
            futures = {pool.submit(fetch_yahoo_price, symbol): symbol for symbol in batch}
            for future in as_completed(futures):
                try:
                    prices.append(future.result())
                except (RateLimitError, YFRateLimitError):
                    limited = True
                except Exception as exc:
                    logging.warning("%s: %s", futures[future], exc)
    store.save_prices(prices)
    logging.info("Prices: received %d of %d", len(prices), len(symbols))
    if limited:
        logging.warning("Yahoo rate-limited requests. Existing prices will remain in the database.")
    return len(prices) == len(symbols)


def catalog_symbols(countries: list[str] | None) -> list[str]:
    symbols = [symbol for symbol, _ in load_universe(default_catalog_dir(), countries=countries)]
    if not symbols:
        raise ValueError("Specify at least one ticker")
    return symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh screener data in the background")
    parser.add_argument("job", choices=("fundamentals", "prices"))
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--symbols", nargs="+", help="Yahoo Finance ticker symbols")
    scope.add_argument("--country", nargs="+", choices=MARKETS)
    parser.add_argument("--database", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--once", action="store_true", help="Run one refresh cycle and exit")
    args = parser.parse_args(argv)
    if args.job == "prices" and not args.symbols:
        parser.error("Specify --symbols for one-minute updates from Yahoo")
    try:
        symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols)) if args.symbols else (
            catalog_symbols(args.country)
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not symbols or "" in symbols:
        parser.error("Specify at least one ticker")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    yf.set_tz_cache_location(str(args.database.parent / "yfinance"))
    job, interval = (
        (refresh_prices, PRICE_INTERVAL) if args.job == "prices"
        else (refresh_fundamentals, FUNDAMENTAL_CHECK_INTERVAL)
    )
    try:
        while True:
            started = monotonic()
            try:
                with closing(MarketStore(args.database)) as store:
                    success = job(store, symbols)
            except (OSError, sqlite3.Error):
                logging.exception("Could not update the database")
                success = False
            if args.once:
                return 0 if success else 1
            elapsed = monotonic() - started
            if elapsed >= interval and args.job == "prices":
                logging.warning("The price refresh took %.1f s. Check the list size and provider availability.", elapsed)
            sleep(interval - elapsed % interval)
            if args.symbols is None:
                try:
                    symbols = catalog_symbols(args.country)
                except (OSError, ValueError) as exc:
                    logging.warning("Could not reload the catalogs. Using the previous ticker list: %s", exc)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
