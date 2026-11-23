# Stock catalogs

Files are grouped by **the country where the exchange is located**. Hong Kong
is treated as a separate market. You can edit the CSV files manually. The next
refresh will replace the catalog for the corresponding country.

The repository and install package include the US catalog. Yahoo-sourced CSVs
are local files. Add them to this directory only if you have permission to use
the data. Git and the install package exclude these local catalogs.

| File | Code | Exchanges included in automatic updates |
| --- | --- | --- |
| usa.csv | us | NYSE, Nasdaq (NMS/NGM/NCM), NYSE American |
| germany.csv | de | Xetra |
| france.csv | fr | Euronext Paris |
| netherlands.csv | nl | Euronext Amsterdam |
| switzerland.csv | ch | SIX Swiss Exchange |
| uk.csv | gb | London Stock Exchange |
| china.csv | cn | Shanghai, Shenzhen |
| hong_kong.csv | hk | Hong Kong Stock Exchange |
| china_adr.csv | us | Separate, manually curated list of Chinese ADRs in the US |

`source=seed` identifies a starter sample. A successful update records the source
as `nasdaqtrader:symbol-directory:filtered` for the US or
`yahoo:equity-screener:market-cap-positive` for other countries. It also records
the download time in UTC as `updated_at`. Tickers are stored as strings to
preserve leading zeros.

Run these commands from the project root.

```bash
python update_universes.py --country de fr nl --dry-run
python update_universes.py --country de fr nl
python update_universes.py
```

The US catalog uses `nasdaqlisted.txt` and `otherlisted.txt` from the official
[Nasdaq Symbol Directory](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs).
The updater downloads both files and checks their columns, the `File Creation Time`
footer, and ticker uniqueness. It excludes ETFs, test issues, NextShares, and
names that indicate warrants, rights, preferred stock, bonds, or SPAC units.
It limits listings to the exchanges in the table and maps the Nasdaq Q/G/S
categories to NMS/NGM/NCM. It converts share class symbols to Yahoo's format
(`BRK.B` → `BRK-B`) and excludes other special suffixes without an explicit mapping.
The name filter is heuristic and may misclassify instruments. The directories
lack market capitalization data, so the updater skips that check. This source
avoids Yahoo's unreliable US pagination.

Other countries use the [Yahoo Equity Screener through yfinance.screen](https://ranaroussi.github.io/yfinance/reference/api/yfinance.screen.html).
The updater requests instruments with positive market capitalization
(`intradaymarketcap > 0`). This filters out many ETPs, bonds, and warrants that
Yahoo misclassifies as equities and excludes instruments with unavailable market
capitalization. It queries each exchange separately in ticker order, with up to
250 instruments per page. It checks `total`, page offsets, duplicates, exchange,
and instrument type. It reports how many entries it excludes because they lack
the `EQUITY` type, including incomplete source records, or belong to an exchange
outside the selected country. If a ticker transfers between supported exchanges
in the same country, the updater uses the actual exchange in the quote even when
the screener index is stale. If `total` changes between pages, the updater logs a
warning and requires the unique record count to match the latest `total`.
Incomplete results, network errors, and empty responses leave the previous file
in place. Each country updates independently. The process exits with code 1 if a
country fails and keeps the successful updates.

The updater validates each file and replaces it with an atomic rename. The prior
version stays in `.backups/<filename>` until the next update. Git excludes these
backups. The updater reports how many tickers were added and removed. `--dry-run`
leaves the CSV files and backups untouched, although yfinance may update its cache.

Catalogs outside the US contain instruments with positive market capitalization
that Yahoo classifies as `EQUITY` on the listed exchanges. They may include
preferred shares, REITs, and ETPs misclassified by the provider. Coverage of the
official common-stock listings may be incomplete. The catalogs currently exclude
OTC listings, secondary German exchanges, and other European countries. The
supported exchanges are defined in `catalog.py`.

Chinese ADRs appear in both `usa.csv` and `china_adr.csv`. The screener removes
duplicates. Use `--country cn` for mainland exchanges and `--region china_adr`
for ADRs, whose listing country is `us`. When all files are loaded, Chinese ADRs
are assigned to `china_adr`.

The Yahoo query used here lacks an issuer-country filter, so new Chinese ADRs
are added to `china_adr.csv` manually. Run this command to validate existing
tickers and update their names and validation dates.

```bash
python update_universes.py --country us --check-adrs
```

Validation checks the equity type and US exchange in Yahoo. The curated list
defines which issuers are Chinese. Yahoo may list Ireland as the country of
incorporation for PDD, for example. Legal ADR status requires separate
verification. If any ticker fails validation, the previous ADR file stays in place.

For regular updates, manually install a weekly cron job. This example runs every
Sunday at 08:00 in the machine's time zone. Set `SCREENER_DIR` above the job in
your crontab to the absolute path of your checkout.

```cron
0 8 * * 0 cd "$SCREENER_DIR" && .venv/bin/python update_universes.py >> .cache/universes-update.log 2>&1
```

Create `.cache/` with `mkdir -p .cache` before installing the cron job. Check the
exit code and the log. If Yahoo returns errors, the saved catalogs can become
stale.
