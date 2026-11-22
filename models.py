"""Fundamental indicators and screening filters."""

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def scaled(info: Mapping[str, Any], key: str, factor: float = 1) -> float | None:
    value = number(info.get(key))
    return None if value is None else value * factor


@dataclass(frozen=True)
class Fundamentals:
    symbol: str
    name: str
    region: str
    exchange: str
    currency: str
    financial_currency: str
    sector: str
    market_cap: float | None
    pe: float | None
    pb: float | None
    revenue_growth_pct: float | None
    roe_pct: float | None
    net_margin_pct: float | None
    debt_to_equity: float | None
    dividend_yield_pct: float | None
    fetched_at: str | None

    @classmethod
    def from_info(
        cls, symbol: str, region: str, info: Mapping[str, Any], fetched_at: str | None
    ) -> "Fundamentals":
        return cls(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName") or symbol,
            region=region,
            exchange=info.get("fullExchangeName") or info.get("exchange") or "",
            currency=info.get("currency") or "",
            financial_currency=info.get("financialCurrency") or "",
            sector=info.get("sector") or "",
            market_cap=scaled(info, "marketCap"),
            pe=scaled(info, "trailingPE"),
            pb=scaled(info, "priceToBook"),
            revenue_growth_pct=scaled(info, "revenueGrowth", 100),
            roe_pct=scaled(info, "returnOnEquity", 100),
            net_margin_pct=scaled(info, "profitMargins", 100),
            debt_to_equity=scaled(info, "debtToEquity", 0.01),
            dividend_yield_pct=scaled(info, "trailingAnnualDividendYield", 100),
            fetched_at=fetched_at,
        )


@dataclass(frozen=True)
class Filters:
    currency: str | None = None
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    max_pe: float | None = None
    max_pb: float | None = None
    min_revenue_growth: float | None = None
    min_roe: float | None = None
    min_net_margin: float | None = None
    max_debt_to_equity: float | None = None
    min_dividend_yield: float | None = None

    def __post_init__(self) -> None:
        for key, value in vars(self).items():
            if key != "currency" and value is not None and number(value) is None:
                raise ValueError(f"{key}: expected a finite number")
        if (self.min_market_cap is not None or self.max_market_cap is not None) and not self.currency:
            raise ValueError("Specify --currency (USD, EUR, CNY, etc.) when filtering by market cap")
        if (
            self.min_market_cap is not None
            and self.max_market_cap is not None
            and self.min_market_cap > self.max_market_cap
        ):
            raise ValueError("Minimum market cap exceeds maximum market cap")
        for key in ("min_market_cap", "max_market_cap", "max_debt_to_equity", "min_dividend_yield"):
            value = getattr(self, key)
            if value is not None and value < 0:
                raise ValueError(f"{key}: value must be >= 0")
        for key in ("max_pe", "max_pb"):
            value = getattr(self, key)
            if value is not None and value <= 0:
                raise ValueError(f"{key}: value must be > 0")

    def matches(self, stock: Fundamentals) -> bool:
        if self.currency and stock.currency != self.currency:
            return False
        checks = (
            (stock.market_cap, self.min_market_cap, self.max_market_cap),
            (stock.pe, None, self.max_pe),
            (stock.pb, None, self.max_pb),
            (stock.revenue_growth_pct, self.min_revenue_growth, None),
            (stock.roe_pct, self.min_roe, None),
            (stock.net_margin_pct, self.min_net_margin, None),
            (stock.debt_to_equity, None, self.max_debt_to_equity),
            (stock.dividend_yield_pct, self.min_dividend_yield, None),
        )
        for value, lower, upper in checks:
            if lower is None and upper is None:
                continue
            if value is None or not isfinite(value):
                return False
            if lower is not None and value < lower:
                return False
            if upper is not None and value > upper:
                return False
        # Loss-making companies must not appear as cheap stocks; negative book
        # equity must not pass P/B or debt/equity constraints either.
        if self.max_pe is not None and stock.pe <= 0:
            return False
        if self.max_pb is not None and stock.pb <= 0:
            return False
        if self.max_debt_to_equity is not None and stock.debt_to_equity < 0:
            return False
        return True
