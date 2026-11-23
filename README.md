# screener

Stock screener for fundamental metrics and prices from Yahoo Finance. Requires Python 3.10 or later.

## Installation

Run these commands from the repository root.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web]'
```

## Web app

```bash
python refresh_data.py fundamentals --symbols AAPL --once
python -m streamlit run streamlit_app.py
```

Open `http://localhost:8501` and choose a market or **All markets**. Optionally enter tickers such as `AAPL`, then click **Show data**.

A fresh checkout includes the US ticker catalog. To use other markets, add local CSV catalogs from an authorized source. See the [catalog setup](universes/README.md).

Use **Previous**, **Next**, or the page number to navigate results. **Rows per page** controls the page size. **Sort all results by** sorts the filtered results across pages. The CSV includes every filtered row.

The table shows a separate price currency when it differs from the listing currency. Use the table's column menu or download the CSV to see the quote timestamp and currency.

The first command creates `.cache/screener.sqlite3` and loads data for AAPL. On a server, the refresh process needs permission to create and write to `.cache/`. Streamlit opens the database in read-only mode and rechecks the selected catalog and stored data every minute. For ongoing updates, run `python refresh_data.py fundamentals` in a separate process from the repository root. This process picks up catalog changes each cycle. The ticker catalogs have their own [refresh command and schedule](universes/README.md).

## CLI

```bash
python main.py --symbols AAPL
```

Run `python main.py --help` for available filters and options.
