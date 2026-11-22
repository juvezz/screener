import contextlib
import csv
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from main import load_universe, main
from models import Filters, Fundamentals
from provider import DataError, InfoCache, YahooProvider, validate_info


# Synthetic values; no market observations or investment recommendations.
INFO = {
    "quoteType": "EQUITY", "longName": "Example Corp", "currency": "USD",
    "financialCurrency": "USD", "marketCap": 2_000_000_000,
    "trailingPE": 15, "priceToBook": 2, "revenueGrowth": 0.12,
    "returnOnEquity": 0.18, "profitMargins": 0.09,
    "debtToEquity": 75, "trailingAnnualDividendYield": 0.025,
}


class FundamentalTests(unittest.TestCase):
    def setUp(self):
        self.stock = Fundamentals.from_info("EXAMPLE", "us", INFO, "2026-01-01T00:00:00+00:00")

    def test_units(self):
        self.assertEqual(self.stock.revenue_growth_pct, 12)
        self.assertEqual(self.stock.roe_pct, 18)
        self.assertEqual(self.stock.net_margin_pct, 9)
        self.assertEqual(self.stock.dividend_yield_pct, 2.5)
        self.assertEqual(self.stock.debt_to_equity, 0.75)

    def test_missing_and_nonfinite_values_are_not_zero(self):
        for value in (None, "N/A", float("nan"), float("inf"), True):
            with self.subTest(value=value):
                info = {**INFO, "trailingPE": value}
                stock = Fundamentals.from_info("EXAMPLE", "us", info, "now")
                self.assertIsNone(stock.pe)
                self.assertTrue(Filters().matches(stock))
                self.assertFalse(Filters(max_pe=20).matches(stock))

    def test_combined_filters_and_boundaries(self):
        filters = Filters(currency="USD", min_market_cap=2e9, max_market_cap=2e9,
                          max_pe=15, max_pb=2, min_revenue_growth=12, min_roe=18,
                          min_net_margin=9, max_debt_to_equity=0.75, min_dividend_yield=2.5)
        self.assertTrue(filters.matches(self.stock))
        self.assertFalse(filters.matches(replace(self.stock, roe_pct=17.9)))
        self.assertFalse(filters.matches(replace(self.stock, currency="HKD")))

    def test_negative_ratios_are_not_cheap_stocks(self):
        for field, constraint in (("pe", Filters(max_pe=20)), ("pb", Filters(max_pb=3)),
                                  ("debt_to_equity", Filters(max_debt_to_equity=1))):
            with self.subTest(field=field):
                self.assertFalse(constraint.matches(replace(self.stock, **{field: -1})))
        self.assertFalse(Filters(max_pe=20).matches(replace(self.stock, pe=0)))

    def test_invalid_filters(self):
        for kwargs in ({"min_market_cap": 1e9}, {"max_pe": 0}, {"min_roe": float("nan")},
                       {"currency": "USD", "min_market_cap": 10, "max_market_cap": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Filters(**kwargs)


class CacheAndProviderTests(unittest.TestCase):
    def test_cache_ttl_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            cache = InfoCache(path)
            self.assertIsNone(cache.get("EXAMPLE", 60))
            cache.put("EXAMPLE", INFO, 100)
            cache.close()
            cache = InfoCache(path)
            try:
                with patch("provider.time.time", return_value=159):
                    self.assertEqual(cache.get("EXAMPLE", 60), (INFO, 100))
                with patch("provider.time.time", return_value=160):
                    self.assertIsNone(cache.get("EXAMPLE", 60))
            finally:
                cache.close()

    def test_incomplete_responses_and_non_equities_are_errors(self):
        for info in ({}, {"quoteType": "EQUITY"}, {**INFO, "quoteType": "ETF"}):
            with self.subTest(info=info), self.assertRaises(DataError):
                validate_info(info)

    def test_provider_cache_refresh_and_no_caching_errors(self):
        with tempfile.TemporaryDirectory() as directory, patch("provider.yf") as yf:
            cache = InfoCache(Path(directory) / "cache.sqlite3")
            provider = YahooProvider(cache, Path(directory), ttl=60)
            yf.Ticker.return_value.get_info.return_value = INFO
            try:
                first = provider.fetch("EXAMPLE")
                self.assertEqual(provider.fetch("EXAMPLE"), first)
                self.assertEqual(yf.Ticker.call_count, 1)
                provider.fetch("EXAMPLE", refresh=True)
                self.assertEqual(yf.Ticker.call_count, 2)
                yf.Ticker.return_value.get_info.return_value = {}
                with self.assertRaises(DataError):
                    provider.fetch("INVALID")
                self.assertIsNone(cache.get("INVALID", 60))
            finally:
                cache.close()


class CliTests(unittest.TestCase):
    def test_offline_listing_and_regions(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.csv"
            path.write_text("symbol,region\n600519.SS,china\n0700.HK,hong_kong\nBABA,china_adr\nAAPL,us\n")
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--list", "--universe", str(path), "--region", "china", "hong_kong", "china_adr"]), 0)
        self.assertIn("600519.SS\tchina", output.getvalue())
        self.assertIn("0700.HK\thong_kong", output.getvalue())
        self.assertIn("BABA\tchina_adr", output.getvalue())
        self.assertNotIn("AAPL", output.getvalue())

    def test_universe_preserves_numeric_symbols_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            path.write_text("symbol,region\n000001.SZ,china\n000001.SZ,china\n", encoding="utf-8")
            self.assertEqual(load_universe(path, None), [("000001.SZ", "china")])
            path.write_text("symbol,region\nEXAMPLE,us\nEXAMPLE,europe\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_universe(path, None)

    def test_partial_failure_csv_and_valid_no_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            argv = ["--symbols", "EXAMPLE", "INVALID", "--cache", str(path / "cache.sqlite3"),
                    "--max-pe", "20", "--output", str(path / "result.csv")]
            with patch("main.YahooProvider") as provider:
                provider.return_value.fetch.side_effect = [(INFO, "now"), DataError("unavailable")]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(argv), 1)
                with (path / "result.csv").open(encoding="utf-8-sig") as source:
                    rows = list(csv.DictReader(source))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["symbol"], "EXAMPLE")
                self.assertEqual(rows[0]["dividend_yield_pct"], "2.5")
                provider.return_value.fetch.side_effect = [(INFO, "now")]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--symbols", "EXAMPLE", "--max-pe", "1",
                                           "--cache", str(path / "cache.sqlite3")]), 0)


if __name__ == "__main__":
    unittest.main()
