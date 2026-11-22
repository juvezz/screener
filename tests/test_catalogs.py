import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog import CATALOG_FIELDS, MARKETS, load_universe
from update_universes import UpdateError, fetch_country, fetch_exchange, fetch_page, main, parse_nasdaq_directory, save_catalog, validate_adrs


def record(symbol: str, region: str = "us", country: str = "us") -> dict:
    return dict(zip(CATALOG_FIELDS, (symbol, region, country, "Example", "NMS", "test", "now")))


def quote(symbol: str) -> dict:
    return {"symbol": symbol, "quoteType": "EQUITY", "exchange": "AMS", "shortName": symbol}


class CatalogTests(unittest.TestCase):
    def test_country_and_region_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_catalog(root / "china.csv", [record("000001.SZ", "china", "cn")])
            save_catalog(root / "usa.csv", [record("EXAMPLE")])
            self.assertEqual(load_universe(root, countries=["cn"]), [("000001.SZ", "china")])
            self.assertEqual(load_universe(root, regions=["us"], countries=["cn"]), [])

    def test_adr_overlap_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_catalog(root / "china_adr.csv", [record("BABA", "china_adr")])
            save_catalog(root / "usa.csv", [record("BABA"), record("EXAMPLE")])
            self.assertEqual(load_universe(root), [("BABA", "china_adr"), ("EXAMPLE", "us")])
            self.assertEqual(load_universe(root, ["us"]), [("BABA", "us"), ("EXAMPLE", "us")])

    def test_missing_country_column_cannot_silently_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.csv"
            path.write_text("symbol,region\nEXAMPLE,us\n")
            with self.assertRaises(ValueError):
                load_universe(path, countries=["us"])

    def test_atomic_replacement_backup_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usa.csv"
            save_catalog(path, [record("OLD")])
            original = path.read_bytes()
            self.assertEqual(save_catalog(path, [record("NEW")], dry_run=True), (1, 1))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(save_catalog(path, [record("NEW")]), (1, 1))
            self.assertEqual((path.parent / ".backups" / path.name).read_bytes(), original)
            self.assertEqual(load_universe(path), [("NEW", "us")])

    def test_invalid_replacement_never_changes_previous_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usa.csv"
            save_catalog(path, [record("OLD")])
            original = path.read_bytes()
            for rows in ([], [record("A"), record("A")], [record("A", "invalid")]):
                with self.subTest(rows=rows), self.assertRaises((UpdateError, ValueError)):
                    save_catalog(path, rows)
                self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class UpdateTests(unittest.TestCase):
    def test_nasdaq_directory_filters_test_issues_funds_and_warrants(self):
        text = (
            "Symbol|Security Name|Market Category|Test Issue|ETF\n"
            "AAPL|Apple Inc. - Common Stock|Q|N|N\n"
            "PFBC|Preferred Bank - Common Stock|Q|N|N\n"
            "ETF|Example Fund|G|N|Y\n"
            "TEST|Test Common Stock|Q|Y|N\n"
            "SPACW|Example - Warrants|G|N|N\n"
            "File Creation Time: 111120261800||||\n"
        )
        self.assertEqual([row["symbol"] for row in parse_nasdaq_directory(text, True)], ["AAPL", "PFBC"])
        with self.assertRaises(UpdateError):
            parse_nasdaq_directory(text.split("File Creation Time:")[0], True)

    def test_other_directory_converts_share_classes_to_yahoo_symbols(self):
        text = (
            "ACT Symbol|Security Name|Exchange|Test Issue|ETF\n"
            "BRK.B|Berkshire Hathaway Common Stock|N|N|N\n"
            "PREF$A|Example Preferred Stock|N|N|N\n"
            "File Creation Time: 111120261800||||\n"
        )
        self.assertEqual(parse_nasdaq_directory(text, False), [
            {"symbol": "BRK-B", "name": "Berkshire Hathaway Common Stock", "exchange": "NYQ"},
        ])

    def test_curated_adr_does_not_depend_on_issuer_domicile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "china_adr.csv"
            save_catalog(path, [record("PDD", "china_adr")])
            with patch("update_universes.yf.Ticker") as ticker:
                ticker.return_value.get_info.return_value = {
                    "quoteType": "EQUITY", "exchange": "NMS", "country": "Ireland",
                }
                self.assertEqual(validate_adrs(path, 0)[0]["symbol"], "PDD")
                ticker.return_value.get_info.return_value = {"quoteType": "ETF", "exchange": "NMS"}
                with self.assertRaises(UpdateError):
                    validate_adrs(path, 0)

    def test_all_pages_are_downloaded_and_offsets_advance(self):
        pages = [
            {"total": 3, "start": 0, "quotes": [quote("A.AS"), quote("B.AS")]},
            {"total": 3, "start": 2, "quotes": [quote("C.AS")]},
        ]
        with patch("update_universes.yf.screen", side_effect=pages) as screen:
            with contextlib.redirect_stderr(io.StringIO()):
                rows = fetch_country("nl", MARKETS["nl"], delay=0, retries=0)
        self.assertEqual([row["symbol"] for row in rows], ["A.AS", "B.AS", "C.AS"])
        self.assertEqual([call.kwargs["offset"] for call in screen.call_args_list], [0, 2])
        self.assertEqual(screen.call_args.kwargs["size"], 250)
        self.assertTrue(all(row["country"] == "nl" and row["updated_at"] for row in rows))

    def test_empty_partial_repeated_or_drifting_results_are_rejected(self):
        first = {"total": 2, "quotes": [quote("A.AS")]}
        cases = [
            [{"total": 0, "quotes": []}],
            [first, {"total": 2, "quotes": []}],
            [first, first],
            [first, {"total": 3, "quotes": [quote("B.AS")]}, {"total": 3, "quotes": []}],
            [first, {"total": 2, "start": 0, "quotes": [quote("B.AS")]}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch("update_universes.yf.screen", side_effect=pages):
                with self.assertRaises(UpdateError), contextlib.redirect_stderr(io.StringIO()):
                    fetch_exchange("nl", "AMS", delay=0, retries=0)

    def test_changed_total_is_allowed_only_with_a_complete_unique_result(self):
        pages = [
            {"total": 2, "quotes": [quote("A.AS")]},
            {"total": 3, "quotes": [quote("B.AS")]},
            {"total": 3, "quotes": [quote("C.AS")]},
        ]
        with patch("update_universes.yf.screen", side_effect=pages):
            with contextlib.redirect_stderr(io.StringIO()):
                rows = fetch_exchange("nl", "AMS", 0, 0)
        self.assertEqual([row["symbol"] for row in rows], ["A.AS", "B.AS", "C.AS"])

    def test_non_equities_are_excluded_without_breaking_pagination(self):
        pages = [
            {"total": 4, "quotes": [{**quote("FUND.AS"), "quoteType": "ETF"}, {"symbol": "STALE.AS"}]},
            {"total": 4, "quotes": [quote("STOCK.AS"), {**quote("FOREIGN.L"), "exchange": "LSE"}]},
        ]
        with patch("update_universes.yf.screen", side_effect=pages):
            with contextlib.redirect_stderr(io.StringIO()):
                rows = fetch_exchange("nl", "AMS", delay=0, retries=0)
        self.assertEqual([row["symbol"] for row in rows], ["STOCK.AS"])

    def test_transfer_between_configured_exchanges_is_kept(self):
        response = {"total": 1, "quotes": [{"symbol": "AEF", "quoteType": "EQUITY", "exchange": "ASE"}]}
        with patch("update_universes.yf.screen", return_value=response):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(fetch_exchange("us", "NYQ", 0, 0)[0]["symbol"], "AEF")

    def test_transient_network_error_is_retried(self):
        response = {"total": 1, "quotes": [quote("A.AS")]}
        with patch("update_universes.yf.screen", side_effect=[OSError("temporary"), response]) as screen:
            with patch("update_universes.time.sleep"):
                self.assertEqual(fetch_page(None, 0, retries=1), response)
        self.assertEqual(screen.call_count, 2)

    def test_one_exchange_failure_preserves_entire_country_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "china.csv"
            save_catalog(path, [record("000001.SZ", "china", "cn")])
            original = path.read_bytes()
            with patch("update_universes.fetch_exchange", side_effect=[[quote("A")], UpdateError("offline")]):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--country", "cn", "--directory", directory, "--delay", "0"]), 1)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
