import argparse
import sqlite3
import sys
from dataclasses import fields
from math import isfinite
from pathlib import Path

from catalog import MARKETS, REGIONS, default_catalog_dir, load_universe
from models import Filters, Fundamentals
from provider import CACHE_TTL, MARKET_DB_PATH, InfoCache, YahooProvider
from screening import load_fundamentals, write_csv


def nonnegative(value: str) -> float:
    result = float(value)
    if not isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("Expected a finite number >= 0")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Fundamental stock screener")
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--symbols", nargs="+", help="Yahoo Finance ticker symbols separated by spaces")
    selection.add_argument("--region", nargs="+", choices=REGIONS)
    result.add_argument("--country", nargs="+", choices=MARKETS, help="ISO code of the listing country")
    result.add_argument("--universe", type=Path, help="CSV file or directory of CSV files (default: universes/)")
    result.add_argument("--list", action="store_true", help="List tickers without making requests")
    result.add_argument("--currency", type=str.upper, help="Market cap currency, such as USD")
    result.add_argument("--min-market-cap", type=float, help="Market cap in units of --currency")
    result.add_argument("--max-market-cap", type=float)
    result.add_argument("--max-pe", type=float, help="Maximum positive trailing P/E")
    result.add_argument("--max-pb", type=float, help="Maximum positive P/B")
    result.add_argument("--min-revenue-growth", type=float, help="Revenue growth, %% (10 = 10%%)")
    result.add_argument("--min-roe", type=float, help="ROE, %%")
    result.add_argument("--min-net-margin", type=float, help="Net profit margin, %%")
    result.add_argument("--max-debt-to-equity", type=float, help="Debt-to-equity ratio (1 = 100%%)")
    result.add_argument("--min-dividend-yield", type=float, help="Trailing annual dividend yield, %%")
    result.add_argument("--cache", type=Path, default=MARKET_DB_PATH)
    result.add_argument("--cache-hours", type=nonnegative, default=CACHE_TTL / 3600)
    result.add_argument("--refresh", action="store_true", help="Fetch fresh data and bypass the cache")
    result.add_argument("--output", type=Path, help="Write the full table to a CSV file")
    return result


def display(stocks: list[Fundamentals]) -> None:
    columns = ("symbol", "currency", "market_cap", "pe", "pb", "roe_pct", "revenue_growth_pct",
               "net_margin_pct", "debt_to_equity", "dividend_yield_pct")
    headers = ("Ticker", "Currency", "Market Cap", "P/E", "P/B", "ROE %", "Revenue Δ %",
               "Margin %", "Debt/Equity", "Div. Yield %")
    rows = [headers]
    for stock in stocks:
        row = []
        for column in columns:
            value = getattr(stock, column)
            if value is None:
                row.append("—")
            elif isinstance(value, float):
                row.append(f"{value:,.0f}" if column == "market_cap" else f"{value:.2f}")
            else:
                row.append(value)
        rows.append(row)
    widths = [max(len(row[i]) for row in rows) for i in range(len(columns))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        filters = Filters(**{field.name: getattr(args, field.name) for field in fields(Filters)})
        if args.symbols:
            if args.universe or args.country:
                raise ValueError("Use --symbols separately from --universe or --country")
            universe = list(dict.fromkeys((symbol.strip().upper(), "custom") for symbol in args.symbols))
            if any(not symbol for symbol, _ in universe):
                raise ValueError("Empty ticker symbol")
        else:
            universe = load_universe(args.universe or default_catalog_dir(), args.region, args.country)
        if not universe:
            raise ValueError("The ticker list is empty")
    except (ValueError, OSError) as exc:
        argument_parser.error(str(exc))
    if args.list:
        for symbol, region in universe:
            print(f"{symbol}\t{region}")
        return 0

    def report_progress(index: int, total: int, symbol: str) -> None:
        print(f"[{index}/{total}] {symbol}", file=sys.stderr)

    def report_error(symbol: str, message: str) -> None:
        print(f"  {symbol}: {message}", file=sys.stderr)

    cache = None
    try:
        cache = InfoCache(args.cache)
        provider = YahooProvider(cache, args.cache.parent, args.cache_hours * 3600)
        stocks, failures = load_fundamentals(
            universe, provider, args.refresh, on_progress=report_progress, on_error=report_error,
        )
        matches = [stock for stock in stocks if filters.matches(stock)]
        display(matches)
        print(
            f"Loaded: {len(stocks)}; matched: {len(matches)}; errors: {len(failures)}.",
            file=sys.stderr,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8-sig", newline="") as target:
                write_csv(target, matches)
        # Partial results remain useful but must be distinguishable in automation.
        return 1 if failures else 0
    except (OSError, sqlite3.Error) as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        return 2
    finally:
        if cache is not None:
            cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
