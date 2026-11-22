import io
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from operator import itemgetter

import streamlit as st

from catalog import default_catalog_dir, load_universe
from market_data import MarketRow, MarketStore, write_market_csv
from models import Filters, Fundamentals
from provider import MARKET_DB_PATH

CACHE_PATH = MARKET_DB_PATH
MARKET_LABELS = {
    "all": "All markets",
    "us": "United States",
    "europe": "Europe · all available exchanges",
    "de": "Germany",
    "fr": "France",
    "nl": "Netherlands",
    "ch": "Switzerland",
    "gb": "United Kingdom",
    "cn": "Mainland China",
    "hk": "Hong Kong",
    "china_adr": "Chinese ADRs",
    "custom": "Custom tickers",
}


def select_universe(market: str, symbols: tuple[str, ...]) -> list[tuple[str, str]]:
    if market == "custom":
        return [(symbol, "custom") for symbol in symbols]

    catalog = default_catalog_dir()
    if market == "all":
        universe = load_universe(catalog)
    elif market in ("europe", "china_adr"):
        universe = load_universe(catalog, regions=[market])
    else:
        universe = load_universe(catalog, countries=[market])
    if not symbols:
        return universe

    by_symbol = dict(universe)
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        raise ValueError("Tickers missing from the selected market catalog: " + ", ".join(missing))
    return [(symbol, by_symbol[symbol]) for symbol in symbols]


def filter_panel(stocks: list[Fundamentals]) -> Filters:
    currencies = sorted({stock.currency for stock in stocks if stock.currency})
    cap_help = "Select a listing currency. A value of 1 represents 1 billion in that currency."
    with st.container(border=True):
        st.subheader("Filters", anchor=False)
        pe, roe, cap, fx = st.columns(4)
        max_pe = pe.number_input("Maximum P/E", value=None, step=0.5, placeholder="Any")
        min_roe = roe.number_input("Minimum ROE, %", value=None, step=1.0, placeholder="Any")
        min_cap = cap.number_input(
            "Minimum market cap, billions", value=None, min_value=0.0, step=0.1,
            placeholder="Any", help=cap_help,
        )
        currency = fx.selectbox(
            "Listing currency", [None, *currencies], format_func=lambda value: value or "All currencies",
            key="filter-currency",
        )
        with st.expander("Additional filters"):
            cap, pb, growth = st.columns(3)
            max_cap = cap.number_input(
                "Maximum market cap, billions", value=None, min_value=0.0, step=0.1,
                placeholder="Any", help=cap_help,
            )
            max_pb = pb.number_input("Maximum P/B", value=None, step=0.5, placeholder="Any")
            min_growth = growth.number_input(
                "Minimum revenue growth, %", value=None, step=1.0, placeholder="Any",
            )
            margin, debt, dividend = st.columns(3)
            min_margin = margin.number_input(
                "Minimum net margin, %", value=None, step=1.0, placeholder="Any",
            )
            max_debt = debt.number_input(
                "Maximum debt/equity, ×", value=None, min_value=0.0, step=0.1, placeholder="Any",
            )
            min_dividend = dividend.number_input(
                "Minimum dividend yield, %", value=None, min_value=0.0, step=0.5,
                placeholder="Any",
            )

    if currency is None and (min_cap is not None or max_cap is not None):
        raise ValueError("Select a listing currency to filter by market cap.")
    if any(value is not None and value <= 0 for value in (max_pe, max_pb)):
        raise ValueError("P/E and P/B limits must be greater than zero.")
    return Filters(
        currency=currency,
        min_market_cap=None if min_cap is None else min_cap * 1e9,
        max_market_cap=None if max_cap is None else max_cap * 1e9,
        max_pe=max_pe,
        max_pb=max_pb,
        min_revenue_growth=min_growth,
        min_roe=min_roe,
        min_net_margin=min_margin,
        max_debt_to_equity=max_debt,
        min_dividend_yield=min_dividend,
    )


def set_results_page(page: int) -> None:
    st.session_state["results-page"] = page


def results_page(rows: list[dict], columns: dict, market: str, filters: Filters) -> list[dict]:
    sort, order, size = st.columns([2, 1, 1])
    sort_column = sort.selectbox(
        "Sort all results by", list(columns), format_func=lambda name: columns[name]["label"],
        key="results-sort-by",
    )
    direction = order.selectbox("Order", ["Ascending", "Descending"], key="results-order")
    page_size = size.selectbox("Rows per page", [10, 25, 50], key="results-page-size")
    page_count = (len(rows) + page_size - 1) // page_size

    scope = (market, st.session_state.get("market_selection"), filters, sort_column, direction, page_size)
    previous_page = st.session_state.get("results-page", 1)
    page = min(previous_page, page_count) if st.session_state.get("results-page-scope") == scope else 1
    st.session_state["results-page-scope"] = scope
    if page != previous_page:
        set_results_page(page)

    start = (page - 1) * page_size
    end = min(start + page_size, len(rows))
    if page_count > 1:
        previous, jump, following, status = st.columns([1, 1, 1, 2], vertical_alignment="bottom")
        previous.button(
            "Previous", key="results-previous", width="stretch", disabled=page == 1,
            on_click=set_results_page, args=(page - 1,),
        )
        jump.number_input("Page", min_value=1, step=1, key="results-page")
        following.button(
            "Next", key="results-next", width="stretch", disabled=page == page_count,
            on_click=set_results_page, args=(page + 1,),
        )
        status.caption(f"Page {page} of {page_count} · Rows {start + 1}–{end} of {len(rows)}")

    present, missing = [], []
    for row in rows:
        bucket = missing if row[sort_column] in (None, "") else present
        bucket.append(row)
    present.sort(key=itemgetter(sort_column), reverse=direction == "Descending")
    return (present + missing)[start:end]


def show_results(entries: list[MarketRow], market: str, total: int) -> None:
    try:
        filters = filter_panel([entry.fundamentals for entry in entries])
    except ValueError as exc:
        st.warning(str(exc))
        return
    matches = [entry for entry in entries if filters.matches(entry.fundamentals)]

    heading, download = st.columns([3, 1], vertical_alignment="center")
    with heading:
        st.subheader("Results", anchor=False)
        st.caption(f"{MARKET_LABELS[market]} · Data for {len(entries)} of {total} tickers · {len(matches)} matches")
    if not matches:
        st.info("No results match these filters. Try adjusting the limits.")
        return

    csv_data = io.StringIO(newline="")
    write_market_csv(csv_data, matches)
    download.download_button(
        "Download CSV", csv_data.getvalue().encode("utf-8-sig"), f"screener-{market}.csv",
        mime="text/csv", on_click="ignore", icon=":material/download:", width="stretch",
        help="All filtered rows with all fields, sorted by ticker.",
    )

    column = st.column_config
    columns = {
        "symbol": column.TextColumn("Ticker", pinned=True),
        "name": column.TextColumn("Company", width="medium"),
        "currency": column.TextColumn("Currency"),
        "price": column.NumberColumn("Price", format="localized"),
        "price_currency": column.TextColumn("Price currency"),
        "price_at": column.DatetimeColumn(
            "Price as of (UTC)", format="DD.MM.YYYY HH:mm", timezone="UTC",
            help="Timestamp of the provider's latest available one-minute bar. The price may be delayed.",
        ),
        "market_cap": column.NumberColumn(
            "Market cap", format="compact", width=100,
            help="Shown in the listing currency. Select a single currency to compare market caps.",
        ),
        "pe": column.NumberColumn("P/E", format="%.2f"),
        "roe_pct": column.NumberColumn("ROE, %", format="%.2f"),
        "pb": column.NumberColumn("P/B", format="%.2f"),
        "revenue_growth_pct": column.NumberColumn("Rev. growth, %", format="%.2f", width=95, help="Revenue growth"),
        "net_margin_pct": column.NumberColumn("Margin, %", format="%.2f"),
        "debt_to_equity": column.NumberColumn("Debt/equity, ×", format="%.2f"),
        "dividend_yield_pct": column.NumberColumn("Dividend yield, %", format="%.2f"),
        "fetched_at": column.DatetimeColumn("Fundamentals updated (UTC)", format="DD.MM.YYYY HH:mm", timezone="UTC"),
    }
    hidden_columns = {"price_at"}
    if all(entry.price is None or entry.price.currency == entry.fundamentals.currency for entry in matches):
        hidden_columns.add("price_currency")
    rows = results_page([entry.as_record() for entry in matches], columns, market, filters)
    for row in rows:
        for field in ("fetched_at", "price_at"):
            if row[field]:
                row[field] = datetime.fromisoformat(row[field])

    row_height = 32
    st.dataframe(
        rows, column_config=columns, column_order=[name for name in columns if name not in hidden_columns],
        hide_index=True, width="stretch", height=50 + row_height * len(rows), row_height=row_height,
    )
    st.caption("Data source · Yahoo Finance")


def show_market(universe: list[tuple[str, str]], market: str) -> None:
    try:
        rows = []
        if CACHE_PATH.is_file():
            with closing(MarketStore(CACHE_PATH, readonly=True)) as store:
                rows = store.read_market(universe)
    except (OSError, sqlite3.Error, ValueError):
        logging.exception("Failed to read market database")
        st.error("Could not read data from the database.")
        return
    if rows:
        show_results(rows, market, len(universe))
    else:
        st.info("No stored data for this selection yet. Results will appear after a background refresh.")


@st.fragment(run_every=60)
def market_panel() -> None:
    with st.container(border=True):
        market_column, ticker_column, action = st.columns([2, 3, 1.7], vertical_alignment="bottom")
        markets = list(MARKET_LABELS)
        market = market_column.selectbox(
            "Market", markets, index=markets.index("us"), format_func=MARKET_LABELS.__getitem__,
        )
        custom = market == "custom"
        entered = ticker_column.text_input(
            "Tickers" if custom else "Tickers (optional)", key=f"tickers-{market}",
            placeholder="e.g., AAPL, SAP.DE, 0700.HK" if custom else "Leave blank to show the entire market",
            help="Separate tickers with spaces or commas.",
        )
        symbols = tuple(sorted(set(re.findall(r"[^,;\s]+", entered.upper()))))
        try:
            if entered.strip() and not symbols:
                raise ValueError("Enter at least one ticker.")
            universe = select_universe(market, symbols)
            if not universe and not custom:
                raise ValueError("The selected market catalog has no tickers.")
        except (ValueError, OSError) as exc:
            universe = []
            st.warning(str(exc))
        show = action.button("Show data", type="primary", width="stretch", disabled=not universe)
        if universe:
            count = f"{len(universe):,}".replace(",", " ")
            st.caption(f"Tickers in selection · {count}")

    if not universe:
        return

    selection = (market, symbols)
    if show:
        st.session_state.market_selection = selection
    if st.session_state.get("market_selection") == selection:
        show_market(universe, market)
    else:
        st.info("Open the selection to view stored data and apply filters.")


def main() -> None:
    st.set_page_config(page_title="Screener · Fundamental Stock Screener", layout="wide")
    st.html("""
        <style>
          .stMainBlockContainer { max-width: 88rem; padding-top: 3.5rem; }
          .st-key-screener-heading {
            background: linear-gradient(115deg, #dfeeea, #f0f6f8 80%);
            border: 1px solid #d5e4e4;
            border-left: 4px solid #09665b;
            border-radius: 0.9rem;
            padding: 1.3rem 1.65rem;
          }
          .st-key-screener-heading h1 { color: #142d38; padding: 0; }
          .st-key-screener-heading p { color: #405963; }
          @media (max-width: 640px) {
            .stMainBlockContainer { padding-top: 3rem; }
            .st-key-screener-heading { padding: 1rem 1.15rem; }
            .st-key-screener-heading h1 { font-size: 1.8rem; }
          }
        </style>
    """)
    with st.container(key="screener-heading"):
        st.title("Fundamental Stock Screener", anchor=False)
        st.caption("Compare stocks by market cap, valuation, and profitability.")
    market_panel()


if __name__ == "__main__":
    main()
