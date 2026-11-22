import tempfile
import unittest
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from market_data import MarketStore, Price


@unittest.skipUnless(find_spec("streamlit"), "Streamlit is an optional dependency")
class StreamlitMarketTests(unittest.TestCase):
    def test_results_paginate_after_sorting_and_reset_when_filters_change(self):
        import streamlit_app
        from streamlit.testing.v1 import AppTest

        script = """
from market_data import MarketRow
from models import Fundamentals
from streamlit_app import show_results

entries = [
    MarketRow(Fundamentals.from_info(f"STOCK{n:02}", "us", {"trailingPE": n if n < 26 else None}, None), None)
    for n in range(1, 27)
]
show_results(entries, "us", len(entries))
"""
        with patch.object(streamlit_app, "write_market_csv", wraps=streamlit_app.write_market_csv) as export:
            app = AppTest.from_string(script).run()
            self.assertEqual(len(app.dataframe[0].value), 10)
            self.assertEqual(app.dataframe[0].value["symbol"].iloc[0], "STOCK01")

            app.button(key="results-next").click().run()
            self.assertEqual(app.dataframe[0].value["symbol"].iloc[0], "STOCK11")
            self.assertEqual(len(export.call_args.args[1]), 26)
            app.run()
            self.assertEqual(app.number_input(key="results-page").value, 2)

            app.number_input(key="results-page").set_value(4).run()
            self.assertEqual(app.number_input(key="results-page").value, 3)
            self.assertEqual(len(app.dataframe[0].value), 6)
            self.assertTrue(app.button(key="results-next").proto.disabled)

            app.selectbox(key="results-sort-by").set_value("pe").run()
            app.selectbox(key="results-order").set_value("Descending").run()
            self.assertEqual(app.number_input(key="results-page").value, 1)
            self.assertEqual(app.dataframe[0].value["symbol"].iloc[0], "STOCK25")
            app.button(key="results-next").click().run()
            self.assertEqual(app.dataframe[0].value["symbol"].iloc[0], "STOCK15")

            app.selectbox(key="results-page-size").set_value(25).run()
            self.assertEqual(len(app.dataframe[0].value), 25)
            app.button(key="results-next").click().run()
            self.assertEqual(app.dataframe[0].value["symbol"].tolist(), ["STOCK26"])
            app.number_input[0].set_value(5.0).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value["symbol"].tolist(), ["STOCK05", "STOCK04", "STOCK03", "STOCK02", "STOCK01"])
            self.assertEqual(len(export.call_args.args[1]), 5)

    def test_quote_currency_is_shown_only_when_it_differs_from_listing_currency(self):
        from streamlit.testing.v1 import AppTest

        script = """
import streamlit as st
from market_data import MarketRow, Price
from models import Fundamentals
from streamlit_app import show_results

currency = st.selectbox("Test quote currency", ["GBP", "GBp"])
stock = Fundamentals.from_info("EXAMPLE.L", "europe", {"currency": "GBP"}, None)
show_results([MarketRow(stock, Price("EXAMPLE.L", 100, currency, 100))], "gb", 1)
"""
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertIn("price", app.dataframe[0].proto.column_order)
        self.assertNotIn("price_at", app.dataframe[0].proto.column_order)
        self.assertNotIn("price_currency", app.dataframe[0].proto.column_order)

        app.selectbox[0].set_value("GBp").run()
        self.assertFalse(app.exception)
        self.assertIn("price_currency", app.dataframe[0].proto.column_order)
        self.assertNotIn("price_at", app.dataframe[0].proto.column_order)

    def test_all_markets_combines_catalogs_and_accepts_explicit_tickers(self):
        import streamlit_app
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory)
            (catalog / "usa.csv").write_text("symbol,region,country\nAAPL,us,us\nBABA,us,us\n")
            (catalog / "china_adr.csv").write_text("symbol,region,country\nBABA,china_adr,us\n")
            (catalog / "germany.csv").write_text("symbol,region,country\nSAP.DE,europe,de\n")
            database = catalog / "screener.sqlite3"
            with closing(MarketStore(database)) as store:
                store.save_prices([
                    Price("AAPL", 200, "USD", 100),
                    Price("BABA", 100, "USD", 100),
                    Price("SAP.DE", 220, "EUR", 100),
                ])

            with patch.object(streamlit_app, "default_catalog_dir", return_value=catalog), patch.object(
                streamlit_app, "CACHE_PATH", database,
            ):
                app = AppTest.from_string("from streamlit_app import main\nmain()").run()
                self.assertEqual(app.selectbox[0].options[:2], ["All markets", "United States"])
                self.assertEqual(app.selectbox[0].value, "us")
                app.selectbox[0].set_value("all").run()
                app.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertEqual(app.dataframe[0].value["symbol"].tolist(), ["AAPL", "BABA", "SAP.DE"])
                self.assertEqual(app.dataframe[0].value["region"].tolist(), ["us", "china_adr", "europe"])

                app.text_input[0].set_value("AAPL, SAP.DE").run()
                app.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertEqual(app.dataframe[0].value["symbol"].tolist(), ["AAPL", "SAP.DE"])
