"""Data loading and CSV export shared by the CLI and web app."""

import csv
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields
from typing import TextIO

from models import Fundamentals
from provider import DataError, YahooProvider


def load_fundamentals(
    universe: Sequence[tuple[str, str]],
    provider: YahooProvider,
    refresh: bool = False,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> tuple[list[Fundamentals], list[str]]:
    stocks = []
    failures = []
    for index, (symbol, region) in enumerate(universe, 1):
        if on_progress:
            on_progress(index, len(universe), symbol)
        try:
            info, fetched_at = provider.fetch(symbol, refresh)
            stocks.append(Fundamentals.from_info(symbol, region, info, fetched_at))
        except DataError as exc:
            failures.append(symbol)
            if on_error:
                on_error(symbol, str(exc))
    return stocks, failures


def write_csv(target: TextIO, stocks: Sequence[Fundamentals]) -> None:
    writer = csv.DictWriter(target, fieldnames=[field.name for field in fields(Fundamentals)])
    writer.writeheader()
    writer.writerows(asdict(stock) for stock in stocks)
