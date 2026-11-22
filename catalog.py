"""Country listing catalogs, shared by screening and catalog updates."""

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

REGIONS = ("us", "europe", "china", "hong_kong", "china_adr")


@dataclass(frozen=True)
class Market:
    filename: str
    region: str
    exchanges: tuple[str, ...]


# Country means the location of the listing, not the issuer's incorporation.
# Restrict discovery to these exchanges: this is not a worldwide security master.
MARKETS = {
    "us": Market("usa.csv", "us", ("NYQ", "NMS", "NGM", "NCM", "ASE")),
    "de": Market("germany.csv", "europe", ("GER",)),
    "fr": Market("france.csv", "europe", ("PAR",)),
    "nl": Market("netherlands.csv", "europe", ("AMS",)),
    "ch": Market("switzerland.csv", "europe", ("EBS",)),
    "gb": Market("uk.csv", "europe", ("LSE",)),
    "cn": Market("china.csv", "china", ("SHH", "SHZ")),
    "hk": Market("hong_kong.csv", "hong_kong", ("HKG",)),
}
CATALOG_FIELDS = ("symbol", "region", "country", "name", "exchange", "source", "updated_at")


def default_catalog_dir() -> Path:
    local = Path(__file__).with_name("universes")
    if local.is_dir():
        return local
    return Path(sys.prefix) / "share" / "global-fundamental-screener" / "universes"


def load_universe(
    path: Path, regions: list[str] | None = None, countries: list[str] | None = None
) -> list[tuple[str, str]]:
    paths = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    if not paths:
        raise ValueError(f"No stock catalog CSV files found in {path}")
    result = {}
    for source_path in paths:
        with source_path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not {"symbol", "region"}.issubset(reader.fieldnames or []):
                raise ValueError(f"{source_path}: the symbol and region columns are required")
            if countries and "country" not in (reader.fieldnames or []):
                raise ValueError(f"{source_path}: --country requires a country column")
            for line, row in enumerate(reader, 2):
                symbol = (row.get("symbol") or "").strip().upper()
                region = (row.get("region") or "").strip()
                country = (row.get("country") or "").strip().lower()
                if not symbol or region not in REGIONS or (country and country not in MARKETS):
                    raise ValueError(f"{source_path}:{line}: invalid ticker, region, or country")
                if (regions and region not in regions) or (countries and country not in countries):
                    continue
                previous = result.get(symbol)
                if previous and previous != region:
                    # The curated ADR list overlaps with the US exchange catalog.
                    if {previous, region} == {"us", "china_adr"}:
                        region = "china_adr"
                    else:
                        raise ValueError(f"Ticker {symbol} is listed in multiple regions")
                result[symbol] = region
    return sorted(result.items())
