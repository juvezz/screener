import csv
import io
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from market_data import MarketStore, Price, write_market_csv
from provider import CACHE_TTL, InfoCache, RateLimitError
from refresh_data import main, refresh_fundamentals, refresh_prices


class MarketDataTests(unittest.TestCase):
    def test_fundamentals_worker_reloads_catalog_between_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "usa.csv"
            catalog.write_text("symbol,region,country\nAAA,us,us\n")
            observed = []

            def finish_cycle(_seconds):
                if len(observed) == 2:
                    raise KeyboardInterrupt
                catalog.write_text("symbol,region,country\nBBB,us,us\n")

            with patch("refresh_data.default_catalog_dir", return_value=root), patch(
                "refresh_data.yf.set_tz_cache_location",
            ), patch("refresh_data.sleep", side_effect=finish_cycle), patch(
                "refresh_data.refresh_fundamentals", side_effect=lambda _store, symbols: observed.append(symbols),
            ):
                self.assertEqual(main(["fundamentals", "--database", str(root / "market.sqlite3")]), 0)
            self.assertEqual(observed, [["AAA"], ["BBB"]])

    def test_existing_database_and_quote_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            with closing(InfoCache(path)) as legacy:
                legacy.put("AAA", {"trailingPE": 20, "currentPrice": 99}, 100)
            with closing(MarketStore(path, readonly=True)) as reader:
                self.assertIsNone(reader.read_market([("AAA", "us")])[0].price)

            with closing(MarketStore(path)) as writer, closing(MarketStore(path, readonly=True)) as reader:
                writer.save_prices([Price("AAA", 42, "USD", 150), Price("0700.HK", 100, "HKD", 200)])
                writer.save_prices([Price("AAA", 40, "USD", 140)])
                universe = [("0700.HK", "hong_kong"), ("AAA", "us"), ("MISSING", "us")]
                rows = reader.read_market(universe)
                self.assertEqual([row.fundamentals.symbol for row in rows], ["0700.HK", "AAA"])
                self.assertIsNone(rows[0].fundamentals.fetched_at)
                self.assertEqual(rows[0].fundamentals.currency, "HKD")
                self.assertEqual(rows[1].fundamentals.pe, 20)
                self.assertEqual(rows[1].price.value, 42)
                target = io.StringIO()
                write_market_csv(target, rows)
                export = list(csv.DictReader(io.StringIO(target.getvalue())))
                self.assertEqual(export[1]["price_currency"], "USD")
                self.assertEqual(export[1]["price_at"], "1970-01-01T00:02:30+00:00")

    def test_fundamental_refresh_uses_weekly_cutoff_and_leaves_prices_alone(self):
        now = CACHE_TTL + 1000
        info = {"quoteType": "EQUITY", "trailingPE": 15, "currentPrice": 99}
        with tempfile.TemporaryDirectory() as directory, closing(MarketStore(Path(directory) / "market")) as store:
            store.put("FRESH", info, 1001)
            store.put("DUE", info, 999)
            store.save_prices([Price("DUE", 42, "USD", now)])
            with patch("refresh_data.time", return_value=now), patch(
                "refresh_data.YahooProvider.fetch_remote", return_value=(info, now),
            ) as fetch:
                self.assertTrue(refresh_fundamentals(store, ["DUE", "FRESH"]))
                fetch.assert_called_once_with("DUE")
            saved, at = store.get("DUE", ttl=None)
            self.assertEqual(at, now)
            self.assertNotIn("currentPrice", saved)
            self.assertEqual(store.read_market([("DUE", "us")])[0].price.value, 42)

    def test_rate_limited_work_keeps_old_data_and_stops_requests(self):
        with tempfile.TemporaryDirectory() as directory, closing(MarketStore(Path(directory) / "market")) as store:
            store.save_prices([Price("AAA", 42, "USD", 100)])
            with patch("refresh_data.WORKERS", 1), patch(
                "refresh_data.fetch_yahoo_price", side_effect=RateLimitError("busy"),
            ) as fetch:
                self.assertFalse(refresh_prices(store, ["AAA", "BBB"]))
                fetch.assert_called_once_with("AAA")
            self.assertEqual(store.read_market([("AAA", "us")])[0].price.value, 42)
