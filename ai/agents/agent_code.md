# Screener coding guidelines

## Language

Write all code comments and docstrings in English. Write all project output and error messages in English, including CLI help and output, log messages, exception messages, and Streamlit labels, warnings, and status messages. Write every README, agent instruction file, and skill document in English. Preserve ticker symbols, company names, and other source data as provided.

## Project layout

| Path | Responsibility |
| --- | --- |
| `main.py` | CLI entry point. Parses ticker selection and filters, lists symbols, runs screening, and prints or exports results. The installed command is `screener`. |
| `streamlit_app.py` and `.streamlit/config.toml` | Web interface and theme. Handles ticker selection, reads stored market data, and displays or exports filtered results. |
| `models.py` | Converts Yahoo Finance payloads to `Fundamentals` and defines filter validation and matching in `Filters`. |
| `catalog.py` and `universes/` | Defines supported listing countries, exchanges, and regions. Loads and validates the CSV ticker catalogs. |
| `provider.py` | Retrieves and validates Yahoo Finance fundamentals through `yfinance`. Defines the default SQLite path and the `InfoCache` store for fundamentals. |
| `screening.py` | Loads fundamentals for the CLI and exports the CLI's CSV results. |
| `market_data.py` | Defines `MarketStore`, which reads market rows and stores fundamental payloads, latest prices, and failed refresh attempts in SQLite. Exports the web app's CSV results. |
| `refresh_data.py` | Runs background jobs to fetch fundamentals and latest prices and write them to SQLite. The installed command is `refresh-market-data`. |
| `update_universes.py` | Updates ticker catalogs from Nasdaq Trader for US listings and from Yahoo Finance for other supported markets. The optional ADR check validates the curated Chinese ADR list. The installed command is `update-universes`. |
| `pyproject.toml` and `requirements.lock` | Define Python compatibility, dependencies, packaging, command entry points, and pinned versions. |
| `tests/` | Contains `unittest` coverage for screening, the provider and SQLite stores, refresh jobs, the CLI, and catalog loading and updates. |
| `README.md` and `universes/README.md` | Document setup, usage, metric units, catalog sources, and catalog maintenance. |
| `ai/agents/` and `ai/skills/` | Hold project agent instructions and reusable project skills. |

## How screening works

The CLI and web app accept explicit ticker symbols or load a selection from the CSV catalogs. A catalog country identifies the listing venue. A ticker present in both the US and curated Chinese ADR lists is assigned to `china_adr` when both records are selected.

The CLI loads fundamentals sequentially using a seven-day cache lifetime by default. It can change the lifetime with `--cache-hours` or bypass the cache with `--refresh`. The `--list` flag prints selected tickers without fetching fundamentals. CLI progress and diagnostics go to stderr. Exit codes are 0 for success, 1 for ticker failures, and 2 for invalid arguments or storage errors.

The web app reads stored fundamentals and the latest available prices, then refreshes the open selection from SQLite every minute. Both entry points use `Fundamentals` to normalize metrics and `Filters` to match rows. The CLI exports fundamentals with `write_csv`, and the web app uses `write_market_csv` to include price data.

## SQLite initialization and refresh

The default database is `.cache/screener.sqlite3`, resolved relative to the process working directory. On a fresh server, the first run of `refresh_data.py` opens `MarketStore` in write mode and creates the parent directory, database file, and tables. The refresh process needs write access to the database directory. SQLite runs in the Python process without a separate database service.

`InfoCache` creates the `fundamentals` table. `MarketStore` also creates `latest_prices` and `failed_fundamental_updates` and enables write-ahead logging. The Streamlit app opens the store in read-only mode and shows an empty state until rows are available. Run the web app and jobs from the same working directory to use the same default path. Git ignores `.cache/`. Use persistent storage for it when deployments replace the filesystem.

Run this command from the repository root to initialize the database and fetch fundamentals for a single ticker.

```bash
python refresh_data.py fundamentals --symbols AAPL --once
```

The fundamentals job checks every 15 minutes for missing records or records at least seven days old and processes up to 200 tickers per cycle. The prices job polls an explicit ticker list every minute and retains the last available price when a fetch fails.

## Coding rules

- Support Python 3.10 and newer. Use descriptive names, type hints on function interfaces, and focused comments that explain decisions or constraints.
- Keep argument parsing and UI rendering in their entry points. Reuse the shared catalog, model, and provider code when changing behavior used by both interfaces.
- Keep metric normalization in `Fundamentals.from_info` and filter rules in `Filters`. Store growth, margins, return on equity, and dividend yield as percentage values where 10 represents 10%. Store debt to equity as a ratio. Apply market capitalization bounds only with an explicit quote currency.
- Represent missing or non-finite metrics with `None`. An active filter rejects rows with missing or non-finite values for that metric. When the corresponding filters are active, require positive P/E and P/B and nonnegative debt to equity.
- Preserve CLI flags, exit codes, CSV columns, and compatibility with existing catalog and database files unless the requested change requires a contract change.
- Keep provider calls in the CLI loading flow and refresh jobs. Keep Streamlit database access read-only. Preserve bounded concurrency and rate-limit handling in the refresh jobs. Keep successful data when a ticker fails, protect newer stored values from older updates, and close SQLite connections after use.
- Validate catalog data before replacing a working CSV. Keep dry runs free of catalog writes and retain backups and atomic replacement when saving updates.
- Use mocked provider responses and temporary files for automated tests that involve network or storage. Update tests and the relevant README when behavior or usage changes.

## Local verification

Run these commands from the repository root in the project environment.

```bash
python -m unittest discover -s tests -v
python main.py --list --country us
python -m streamlit run streamlit_app.py
```

Install the optional web dependencies with `python -m pip install -e '.[web]'` before running Streamlit. Use `python update_universes.py --country us --dry-run` to check a catalog refresh without replacing the CSV. This command contacts the configured data sources.
