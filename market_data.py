"""
Market data containers, curve specifications, FRED Treasury data loading, and multi-curve roles.
"""

import csv
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError

import pandas as pd
from pandas import Timestamp
from pandas._libs import NaTType

from config import DEFAULT_CURVE_BUILD_SETTINGS, FRED_CACHE_DIR
from conventions import BusinessDayConvention, DateGenerationRule, DayCountConvention
from curves import ZeroCurve, bootstrap_discount_factors_from_par_yields, bootstrap_zero_rates_from_par_yields


def parse_optional_float(row: Mapping[str, str], column: str) -> float | None:
    value = row.get(column, "").strip()
    if value == "":
        return None
    return float(value)


def parse_decimal_rate(row: Mapping[str, str], column: str, required: bool = False) -> float | None:
    value = parse_optional_float(row, column)
    if value is None:
        if required:
            raise ValueError(f"{column} is required.")
        return None
    if abs(value) > 1:
        return value / 100
    return value


def parse_optional_int(row: Mapping[str, str], column: str, default: int | None = None) -> int | None:
    value = row.get(column, "").strip()
    if value == "":
        return default
    return int(value)


def parse_required_float(row: Mapping[str, str], column: str) -> float:
    value = parse_optional_float(row, column)
    if value is None:
        raise ValueError(f"{column} is required.")
    return value


def parse_required_int(row: Mapping[str, str], column: str) -> int:
    value = parse_optional_int(row, column)
    if value is None:
        raise ValueError(f"{column} is required.")
    return value


def parse_optional_bool(row: Mapping[str, str], column: str, default: bool = False) -> bool:
    value = row.get(column, "").strip().lower()
    if value == "":
        return default
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{column} must be a boolean value.")


def parse_required_date(row: Mapping[str, str], column: str) -> Date:
    value = row.get(column, "").strip()
    if value == "":
        raise ValueError(f"Missing required column value: {column}.")
    return Date.fromisoformat(value)


def export_rows_to_csv(rows: Iterable[Mapping[str, object]], output_path: str | Path,
                       fieldnames: Sequence[str] | None = None) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    materialized_rows = [dict(row) for row in rows]
    selected_fieldnames = list(fieldnames) if fieldnames is not None else list(materialized_rows[0]) if materialized_rows else []
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=selected_fieldnames)
        writer.writeheader()
        writer.writerows(materialized_rows)
    return output_path


@dataclass
class SecurityMasterRecord:
    """
    Bond terms keyed by a traded identifier such as CUSIP or ISIN.
    This is the bridge from real market identifiers into InstrumentSpec.
    """
    security_id: str
    instrument_type: str
    issue_date: Date
    maturity_date: Date
    face_value: float
    frequency: int
    currency: str
    coupon_rate: float | None = None
    id_type: str = "CUSIP"
    issuer: str | None = None
    issue_price: float | None = None
    day_count: DayCountConvention | str = DayCountConvention.ACT_ACT_ICMA
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.UNADJUSTED
    date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD
    end_of_month: bool = False

    def __post_init__(self) -> None:
        if not self.security_id.strip():
            raise ValueError("security_id is required.")
        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must be before maturity_date.")
        if not math.isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError("face_value must be positive and finite.")
        if self.frequency <= 0:
            raise ValueError("frequency must be positive.")
        if self.coupon_rate is not None and (not math.isfinite(self.coupon_rate) or self.coupon_rate < 0):
            raise ValueError("coupon_rate must be non-negative and finite when provided.")
        if self.issue_price is not None and (not math.isfinite(self.issue_price) or self.issue_price <= 0):
            raise ValueError("issue_price must be positive and finite when provided.")
        self.instrument_type = self.instrument_type.strip().lower()
        if self.instrument_type not in {"fixed_coupon_bond", "zero_coupon_bond"}:
            raise ValueError(f"Unsupported security instrument_type: {self.instrument_type}.")
        if self.instrument_type == "fixed_coupon_bond" and self.coupon_rate is None:
            raise ValueError("coupon_rate is required for fixed_coupon_bond.")
        if not self.currency.strip():
            raise ValueError("currency is required.")
        self.security_id = self.security_id.strip()
        self.id_type = self.id_type.strip().upper() or "CUSIP"
        self.currency = self.currency.strip().upper()

    def to_instrument_spec(self):
        from pricing import InstrumentSpec

        return InstrumentSpec(
            instrument_id=self.security_id,
            instrument_type=self.instrument_type,
            face_value=self.face_value,
            issue_date=self.issue_date,
            maturity_date=self.maturity_date,
            coupon_rate=self.coupon_rate,
            frequency=self.frequency,
            currency=self.currency,
            issue_price=self.issue_price,
            day_count=self.day_count,
            discount_day_count=self.discount_day_count,
            business_day_convention=self.business_day_convention,
            date_generation_rule=self.date_generation_rule,
            end_of_month=self.end_of_month,
        )

    def row(self) -> dict[str, float | str | bool | None]:
        return {
            "security_id": self.security_id,
            "id_type": self.id_type,
            "issuer": self.issuer,
            "instrument_type": self.instrument_type,
            "issue_date": self.issue_date.isoformat(),
            "maturity_date": self.maturity_date.isoformat(),
            "coupon_rate": self.coupon_rate,
            "face_value": self.face_value,
            "frequency": self.frequency,
            "currency": self.currency,
            "issue_price": self.issue_price,
            "day_count": str(self.day_count),
            "discount_day_count": str(self.discount_day_count),
            "business_day_convention": str(self.business_day_convention),
            "date_generation_rule": str(self.date_generation_rule),
            "end_of_month": self.end_of_month,
        }


@dataclass
class BondMarketQuote:
    """
    Observed clean or dirty price for a traded bond identifier.
    Prices are assumed to be quoted per 100 face value unless the security terms
    use a different face_value scale.
    """
    security_id: str
    valuation_date: Date
    price_type: str
    quote_source: str
    currency: str
    observed_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    quote_type: str = "market_price"
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not self.security_id.strip():
            raise ValueError("security_id is required.")
        self.security_id = self.security_id.strip()
        if not self.currency.strip():
            raise ValueError("currency is required.")
        self.currency = self.currency.strip().upper()
        self.price_type = self.price_type.strip().lower()
        if self.price_type not in {"clean", "dirty"}:
            raise ValueError("price_type must be 'clean' or 'dirty'.")
        if not self.quote_source.strip():
            raise ValueError("quote_source is required.")
        for field_name, field_value in {"observed_price": self.observed_price, "bid": self.bid, "ask": self.ask}.items():
            if field_value is not None and (not math.isfinite(field_value) or field_value <= 0):
                raise ValueError(f"{field_name} must be positive and finite when provided.")
        if self.observed_price is None and self.bid is None and self.ask is None:
            raise ValueError("At least one of observed_price, bid, or ask is required.")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot be greater than ask.")

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    def effective_price(self, prefer_mid: bool = True) -> float:
        if prefer_mid and self.mid is not None:
            return self.mid
        if self.observed_price is not None:
            return self.observed_price
        if self.bid is not None:
            return self.bid
        if self.ask is not None:
            return self.ask
        raise ValueError("No usable quote price is available.")

    def inside_bid_ask(self, model_price: float) -> bool | None:
        if self.bid is None or self.ask is None:
            return None
        return self.bid <= model_price <= self.ask

    def row(self) -> dict[str, float | str | bool | None]:
        return {
            "security_id": self.security_id,
            "valuation_date": self.valuation_date.isoformat(),
            "observed_price": self.observed_price,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "effective_price": self.effective_price(),
            "price_type": self.price_type,
            "quote_source": self.quote_source,
            "quote_type": self.quote_type,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "currency": self.currency,
        }


def load_security_master_from_csv(path: str | Path) -> list[SecurityMasterRecord]:
    records: list[SecurityMasterRecord] = []
    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row_number, row in enumerate(reader, start=2):
            try:
                currency = row.get("currency", "").strip()
                day_count = row.get("day_count", "").strip()
                discount_day_count = row.get("discount_day_count", "").strip()
                business_day_convention = row.get("business_day_convention", "").strip()
                date_generation_rule = row.get("date_generation_rule", "").strip()
                for column, value in {
                    "currency": currency,
                    "day_count": day_count,
                    "discount_day_count": discount_day_count,
                    "business_day_convention": business_day_convention,
                    "date_generation_rule": date_generation_rule,
                }.items():
                    if value == "":
                        raise ValueError(f"{column} is required.")
                records.append(SecurityMasterRecord(
                    security_id=row.get("security_id", "").strip(),
                    id_type=row.get("id_type", "CUSIP").strip() or "CUSIP",
                    issuer=row.get("issuer", "").strip() or None,
                    instrument_type=row.get("instrument_type", "").strip(),
                    issue_date=parse_required_date(row, "issue_date"),
                    maturity_date=parse_required_date(row, "maturity_date"),
                    coupon_rate=parse_decimal_rate(row, "coupon_rate"),
                    face_value=parse_required_float(row, "face_value"),
                    frequency=parse_required_int(row, "frequency"),
                    currency=currency,
                    issue_price=parse_optional_float(row, "issue_price"),
                    day_count=day_count,
                    discount_day_count=discount_day_count,
                    business_day_convention=business_day_convention,
                    date_generation_rule=date_generation_rule,
                    end_of_month=parse_optional_bool(row, "end_of_month"),
                ))
            except Exception as error:
                raise ValueError(f"Invalid security master row {row_number}: {error}") from error
    return records


def load_bond_market_quotes_from_csv(path: str | Path) -> list[BondMarketQuote]:
    quotes: list[BondMarketQuote] = []
    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row_number, row in enumerate(reader, start=2):
            try:
                timestamp_text = row.get("timestamp", "").strip()
                quote_source = row.get("quote_source", row.get("source", "")).strip()
                currency = row.get("currency", "").strip()
                if quote_source == "":
                    raise ValueError("quote_source is required.")
                if currency == "":
                    raise ValueError("currency is required.")
                dirty_price = parse_optional_float(row, "dirty_price")
                clean_price = parse_optional_float(row, "clean_price")
                raw_price_type = row.get("price_type", "").strip().lower()
                if dirty_price is not None:
                    observed_price = dirty_price
                    price_type = "dirty"
                elif clean_price is not None:
                    observed_price = clean_price
                    price_type = "clean"
                else:
                    observed_price = parse_optional_float(row, "price")
                    if observed_price is not None and raw_price_type == "":
                        raise ValueError("price_type is required when using generic price.")
                    price_type = raw_price_type or "clean"
                quotes.append(BondMarketQuote(
                    security_id=row.get("security_id", "").strip(),
                    valuation_date=parse_required_date(row, "valuation_date"),
                    observed_price=observed_price,
                    bid=parse_optional_float(row, "bid"),
                    ask=parse_optional_float(row, "ask"),
                    price_type=price_type,
                    quote_source=quote_source,
                    quote_type=row.get("quote_type", "market_price").strip() or "market_price",
                    timestamp=datetime.fromisoformat(timestamp_text) if timestamp_text else None,
                    currency=currency,
                ))
            except Exception as error:
                raise ValueError(f"Invalid bond quote row {row_number}: {error}") from error
    return quotes


@dataclass
class MarketDataPoint:
    """
    Represents one observable market quote used by a curve or pricer.
    The larger workflow can use this object as the bridge between raw loaders
    and curve builders, making market-data assumptions explicit and auditable.
    """
    instrument_id: str
    value: float | None = None
    bid: float | None = None
    ask: float | None = None
    timestamp: datetime | None = None
    source: str = "unknown"
    quote_type: str = "unknown"
    currency: str = "USD"
    stale_flag: bool = False
    override_flag: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id is required.")
        if not self.source.strip():
            raise ValueError("source is required.")
        if not self.quote_type.strip():
            raise ValueError("quote_type is required.")
        if not self.currency.strip():
            raise ValueError("currency is required.")
        self.currency = self.currency.upper()
        for field_name, field_value in {"value": self.value, "bid": self.bid, "ask": self.ask}.items():
            if field_value is not None and not math.isfinite(field_value):
                raise ValueError(f"{field_name} must be finite when provided.")
        if self.value is None and self.bid is None and self.ask is None:
            raise ValueError("At least one of value, bid, or ask is required.")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot be greater than ask.")

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    def effective_value(self, prefer_mid: bool = True) -> float:
        if prefer_mid and self.mid is not None:
            return self.mid
        if self.value is not None:
            return self.value
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        if self.bid is not None:
            return self.bid
        if self.ask is not None:
            return self.ask
        raise ValueError("No usable market value is available.")

    def is_usable(self, allow_stale: bool = False, allow_override: bool = True) -> bool:
        if self.stale_flag and not allow_stale:
            return False
        if self.override_flag and not allow_override:
            return False
        return True

    def age_seconds(self, as_of: datetime) -> float | None:
        if self.timestamp is None:
            return None
        return (as_of - self.timestamp).total_seconds()

    def is_stale_as_of(self, as_of: datetime, max_age_seconds: float) -> bool:
        if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative and finite.")
        age = self.age_seconds(as_of)
        return age is not None and age > max_age_seconds

    def row(self) -> dict[str, float | str | bool | None]:
        return {"instrument_id": self.instrument_id, "value": self.value, "bid": self.bid, "ask": self.ask, "mid": self.mid,
                "effective_value": self.effective_value(), "timestamp": self.timestamp.isoformat() if self.timestamp else None,
                "source": self.source, "quote_type": self.quote_type, "currency": self.currency, "stale_flag": self.stale_flag,
                "override_flag": self.override_flag}


@dataclass
class CurveSpec:
    """
    Describes how a curve should be built and audited.
    Curve builders can carry this object into reports so users can explain not
    just the output curve, but the construction policy that produced it.
    """
    curve_name: str
    currency: str = "USD"
    curve_type: str = "zero"
    discount_curve: str | None = None
    projection_curve: str | None = None
    day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED
    interpolation_method: str = "linear zero-rate interpolation"
    extrapolation_method: str = "no extrapolation unless explicitly enabled"
    calibration_tolerance: float = 1e-10
    instruments_used: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.curve_name.strip():
            raise ValueError("curve_name is required.")
        if not self.currency.strip():
            raise ValueError("currency is required.")
        if not self.curve_type.strip():
            raise ValueError("curve_type is required.")
        if not math.isfinite(self.calibration_tolerance) or self.calibration_tolerance <= 0:
            raise ValueError("calibration_tolerance must be positive and finite.")
        self.currency = self.currency.upper()
        if len(set(self.instruments_used)) != len(self.instruments_used):
            raise ValueError("instruments_used cannot contain duplicates.")

    def row(self) -> dict[str, float | str | None]:
        return {"curve_name": self.curve_name, "currency": self.currency, "curve_type": self.curve_type, "discount_curve": self.discount_curve,
                "projection_curve": self.projection_curve, "day_count": str(self.day_count), "interpolation_method": self.interpolation_method,
                "extrapolation_method": self.extrapolation_method, "calibration_tolerance": self.calibration_tolerance,
                "instruments_used": ",".join(self.instruments_used)}

    def with_instruments(self, instruments: list[str]) -> "CurveSpec":
        return CurveSpec(curve_name=self.curve_name, currency=self.currency, curve_type=self.curve_type, discount_curve=self.discount_curve,
                         projection_curve=self.projection_curve, day_count=self.day_count, interpolation_method=self.interpolation_method,
                         extrapolation_method=self.extrapolation_method, calibration_tolerance=self.calibration_tolerance, instruments_used=instruments)


FRED_TREASURY_SERIES = {
    "DGS1MO": 1 / 12,
    "DGS3MO": 3 / 12,
    "DGS6MO": 0.5,
    "DGS1": 1.0,
    "DGS2": 2.0,
    "DGS3": 3.0,
    "DGS5": 5.0,
    "DGS7": 7.0,
    "DGS10": 10.0,
    "DGS20": 20.0,
    "DGS30": 30.0,
}
FRED_GRAPH_URL_TEMPLATE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


@dataclass
class TreasuryCurveSnapshot:
    """
    Auditable container for one Treasury curve observation.
    The larger project should avoid passing anonymous lists of maturities and
    rates once data comes from the market. This object keeps the valuation date,
    source, quote type, and curve assumptions together so downstream analytics
    can explain exactly what market snapshot produced a price or chart.
    """
    valuation_date: Date
    maturities: list[float]
    par_yields: list[float]
    source: str = "FRED"
    quote_type: str = "Treasury CMT par-style yield"
    frequency: int = 2
    downloaded_at: datetime | None = None
    source_urls: dict[str, str] = field(default_factory=dict)
    series_ids: list[str] = field(default_factory=list)
    market_data_points: list[MarketDataPoint] = field(default_factory=list)
    curve_build_method: str = DEFAULT_CURVE_BUILD_SETTINGS.curve_build_method
    interpolation_method: str = DEFAULT_CURVE_BUILD_SETTINGS.interpolation_method
    extrapolation_method: str = DEFAULT_CURVE_BUILD_SETTINGS.extrapolation_method

    def __post_init__(self) -> None:
        if self.market_data_points and len(self.market_data_points) != len(self.maturities):
            raise ValueError("market_data_points must match the number of maturities when provided.")
        if self.market_data_points and any(not point.is_usable() for point in self.market_data_points):
            raise ValueError("TreasuryCurveSnapshot contains unusable market data points.")

    def quote_rows(self) -> list[dict[str, float | str | bool | None]]:
        """
        Returns source quote metadata used to build this Treasury curve.
        The normal rows() method stays focused on curve analytics; this method
        exposes the market-data audit trail separately.
        """
        return [point.row() | {"valuation_date": self.valuation_date.isoformat(), "maturity": maturity} for point, maturity in zip(self.market_data_points, self.maturities)]

    def quote_by_series_id(self) -> dict[str, MarketDataPoint]:
        return {point.instrument_id: point for point in self.market_data_points}

    def zero_rates(self) -> list[float]:
        """
        Bootstraps spot/zero rates from the snapshot's par-yield quotes.
        This method is the live-data version of the core project transformation:
        market par yields become zero rates that can drive discounting.
        """
        return bootstrap_zero_rates_from_par_yields(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def discount_factors(self) -> list[float]:
        """
        Bootstraps discount factors from the snapshot's par-yield quotes.
        Discount factors are the direct pricing inputs for future cashflows, so
        this method exposes the market-implied present-value curve.
        """
        return bootstrap_discount_factors_from_par_yields(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def to_zero_curve(self) -> ZeroCurve:
        """
        Converts the market snapshot into the project's central ZeroCurve.
        This is the handoff from market data into pricing: once the snapshot is
        a ZeroCurve, bond pricing, forwards, and par yields all use a consistent
        set of bootstrapped zero rates.
        """
        return ZeroCurve(maturities=self.maturities, zero_rates=self.zero_rates())

    def forward_rates(self) -> list[tuple[float, float, float]]:
        """
        Calculates adjacent-tenor forward rates implied by the snapshot.
        These forwards help diagnose the shape of the live curve and connect the
        bootstrapped discount factors to derivatives intuition.
        """
        curve = self.to_zero_curve()
        forwards = []
        start_maturity = 0.0
        for end_maturity in self.maturities:
            forward_rate = curve.forward_rate(start_maturity=start_maturity, end_maturity=end_maturity)
            forwards.append((start_maturity, end_maturity, forward_rate))
            start_maturity = end_maturity
        return forwards

    def rows(self) -> list[dict[str, float]]:
        """
        Builds report-ready rows for par, zero, discount, and forward data.
        main.py uses this to print a compact live-curve report without
        duplicating curve calculations or formatting assumptions.
        """
        zero_rates = self.zero_rates()
        discount_factors = self.discount_factors()
        forward_rates = self.forward_rates()
        return [{"maturity": maturity, "par_yield": par_yield, "zero_rate": zero_rate, "discount_factor": discount_factor,
            "forward_start": forward_start, "forward_end": forward_end, "forward_rate": forward_rate}
            for (maturity, par_yield, zero_rate, discount_factor, (forward_start, forward_end, forward_rate))
            in zip(self.maturities, self.par_yields, zero_rates, discount_factors, forward_rates)]

def download_fred_series(series_id: str) -> pd.DataFrame:
    """
    Downloads one FRED time series using FRED's public CSV graph endpoint.
    Example:
    DGS10 = 10-year Treasury constant maturity rate
    This is the lowest-level market-data function. Higher-level functions use
    it repeatedly to assemble a complete Treasury curve across maturities.
    """
    return download_fred_series_with_cache(series_id=series_id)


def _fred_cache_path(series_id: str, cache_dir: str | Path) -> Path:
    return Path(cache_dir) / f"{series_id}.csv"


def download_fred_series_with_cache(series_id: str, cache_dir: str | Path = FRED_CACHE_DIR, use_cache: bool = True,
            refresh_cache: bool = False, max_retries: int = 3, retry_delay_seconds: float = 1.0) -> pd.DataFrame:
    """
    Downloads one FRED series and caches the CSV locally.
    Caching makes the workflow faster and more reproducible. If the network is
    unavailable, previously cached data can still support local reports.
    """
    url = FRED_GRAPH_URL_TEMPLATE.format(series_id=series_id)
    cache_path = _fred_cache_path(series_id=series_id, cache_dir=cache_dir)
    if use_cache and cache_path.exists() and not refresh_cache:
        df = pd.read_csv(cache_path)
    else:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        last_error: HTTPError | URLError | None = None
        for attempt in range(1, max_retries + 1):
            try:
                df = pd.read_csv(url)
                break
            except (HTTPError, URLError) as error:
                last_error = error
                if attempt < max_retries:
                    sleep(retry_delay_seconds)
        else:
            if use_cache and cache_path.exists():
                df = pd.read_csv(cache_path)
            else:
                raise ValueError(f"Could not download FRED series {series_id} from {url}: "f"{last_error}") from last_error
        df.to_csv(cache_path, index=False)
    df["observation_date"] = pd.to_datetime(df["observation_date"])
    # FRED publishes some missing observations as ".".
    df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
    return df


def load_treasury_curve_snapshot_from_fred(date: str | None = None, cache_dir: str | Path = FRED_CACHE_DIR,
                                           use_cache: bool = True, refresh_cache: bool = False) -> TreasuryCurveSnapshot:
    """
    Downloads multiple Treasury constant maturity rates from FRED
    and returns selected date + maturities + par yields.

    Rates from FRED are in percentage form. Example: 4.25 means 4.25%
    We convert: 4.25 -> 0.0425

    The function selects a complete curve date because bootstrapping requires a
    coherent set of tenors. Partial market data would create a curve whose shape
    is driven by missing-data artifacts rather than market information.
    """
    series_frames = []
    for series_id, maturity in FRED_TREASURY_SERIES.items():
        df = download_fred_series_with_cache(series_id=series_id, cache_dir=cache_dir, use_cache=use_cache,refresh_cache=refresh_cache)
        df = df.rename(columns={series_id: "rate"})
        df["series_id"] = series_id
        df["maturity"] = maturity
        series_frames.append(df)
    all_data = pd.concat(series_frames, ignore_index=True)
    clean_data = all_data.dropna(subset=["rate"])
    expected_series_count = len(FRED_TREASURY_SERIES)
    if date is None:
        complete_dates = (clean_data.groupby("observation_date")["series_id"].nunique().loc[lambda counts: counts == expected_series_count])
        if complete_dates.empty:
            raise ValueError("No complete FRED Treasury curve found.")
        selected_date = complete_dates.index.max()
    else:
        selected_date = pd.to_datetime(date)
    selected_data = clean_data[clean_data["observation_date"] == selected_date]
    if len(selected_data) != expected_series_count:
        available_series = set(selected_data["series_id"])
        missing_series = [series_id for series_id in FRED_TREASURY_SERIES if series_id not in available_series]
        available_start = all_data["observation_date"].min().date()
        available_end = all_data["observation_date"].max().date()
        raise ValueError(
            f"Complete FRED Treasury curve not found for date: {selected_date.date()}. "
            f"Missing series: {', '.join(missing_series)}. "
            f"Available range: {available_start} to {available_end}. "
            f"Try a business day, not a weekend/holiday.")
    selected_data = selected_data.sort_values("maturity")
    maturities = selected_data["maturity"].tolist()
    par_yields = (selected_data["rate"] / 100).tolist()
    quote_timestamp = selected_date.to_pydatetime().replace(tzinfo=timezone.utc)
    market_data_points = [MarketDataPoint(instrument_id=row["series_id"], value=row["rate"] / 100, timestamp=quote_timestamp, source="FRED",
        quote_type="Treasury CMT par-style yield", currency="USD") for _, row in selected_data.iterrows()]
    return TreasuryCurveSnapshot(valuation_date=selected_date.date(),maturities=maturities,par_yields=par_yields,
        downloaded_at=datetime.now(timezone.utc),source_urls={series_id: FRED_GRAPH_URL_TEMPLATE.format(series_id=series_id)
            for series_id in FRED_TREASURY_SERIES}, series_ids=list(FRED_TREASURY_SERIES), market_data_points=market_data_points)


def download_treasury_curve_snapshot_from_fred(date: str | None = None) -> tuple[Timestamp | NaTType, list[float], list[float]]:
    """
    Backward-compatible tuple API for selected date + maturities + par yields.
    Existing notebooks or scripts may still expect the old tuple shape. Keeping
    this wrapper lets the project evolve toward TreasuryCurveSnapshot without
    breaking earlier examples immediately.
    """
    snapshot = load_treasury_curve_snapshot_from_fred(date=date)
    return (pd.Timestamp(snapshot.valuation_date),snapshot.maturities,snapshot.par_yields)


def download_treasury_curve_from_fred(date: str | None = None) -> tuple[list[float], list[float]]:
    """
    Downloads one FRED Treasury par-yield curve and returns maturities + yields.
    This compact API is useful for quick experiments where metadata is not
    needed, but production-style workflows should prefer TreasuryCurveSnapshot.
    """
    _, maturities, par_yields = download_treasury_curve_snapshot_from_fred(date=date)
    return maturities, par_yields


def build_fred_treasury_curve(date: str | None = None,frequency: int = 2) -> ZeroCurve:
    """
    Builds a bootstrapped ZeroCurve from FRED Treasury constant maturity rates.
    FRED Treasury constant maturity rates are par-style market yields. This
    function bootstraps continuously compounded zero rates before creating the
    ZeroCurve, so downstream discount factors are based on zero rates rather
    than raw par yields.
    This is the shortest path from live market data to the project's pricing
    engine.
    """
    snapshot = load_treasury_curve_snapshot_from_fred(date=date)
    snapshot.frequency = frequency
    return snapshot.to_zero_curve()


def load_fred_treasury_curve_snapshot(date: str | None = None, frequency: int = 2, cache_dir: str | Path = FRED_CACHE_DIR,
                                    use_cache: bool = True,refresh_cache: bool = False) -> TreasuryCurveSnapshot:
    """
    Public convenience wrapper for loading a configured FRED curve snapshot.
    This is the preferred live-data entry point for scripts such as main.py
    because it returns both the market metadata and the analytics methods.
    """
    snapshot = load_treasury_curve_snapshot_from_fred(date=date, cache_dir=cache_dir, use_cache=use_cache, refresh_cache=refresh_cache)
    snapshot.frequency = frequency
    return snapshot


def build_zero_curve_from_snapshot(snapshot: TreasuryCurveSnapshot) -> ZeroCurve:
    """
    Converts any TreasuryCurveSnapshot into a ZeroCurve.
    Keeping this adapter explicit makes it clear where market-data containers
    cross into pricing-curve objects.
    """
    return snapshot.to_zero_curve()


class CurveRole(StrEnum):
    SOFR_OIS_DISCOUNT = "sofr_ois_discount"
    SOFR_PROJECTION = "sofr_projection"
    TREASURY_BENCHMARK = "treasury_benchmark"
    FED_FUNDS = "fed_funds"
    TERM_SOFR = "term_sofr"
    CROSS_CURRENCY_BASIS = "cross_currency_basis"
    CREDIT = "credit"
    REPO_FUNDING = "repo_funding"
    XVA_CREDIT = "xva_credit"
    XVA_FUNDING = "xva_funding"
    XVA_COLLATERAL = "xva_collateral"


ROLE_PURPOSES = {
    CurveRole.SOFR_OIS_DISCOUNT: "Discount collateralized USD cashflows.",
    CurveRole.SOFR_PROJECTION: "Project future SOFR-indexed floating coupons.",
    CurveRole.TREASURY_BENCHMARK: "Measure benchmark Treasury rates and spreads.",
    CurveRole.FED_FUNDS: "Represent Fed Funds-linked projection or funding assumptions.",
    CurveRole.TERM_SOFR: "Represent forward-looking Term SOFR tenors where contractually applicable.",
    CurveRole.CROSS_CURRENCY_BASIS: "Adjust cross-currency discounting and projection relationships.",
    CurveRole.CREDIT: "Apply issuer, sector, or counterparty credit spread assumptions.",
    CurveRole.REPO_FUNDING: "Represent repo, securities financing, and collateral funding economics.",
    CurveRole.XVA_CREDIT: "Support counterparty credit exposure and CVA-style calculations.",
    CurveRole.XVA_FUNDING: "Support funding valuation adjustment calculations.",
    CurveRole.XVA_COLLATERAL: "Support collateral and margin valuation assumptions.",
}


@dataclass
class CurveDefinition:
    """
    Binds a curve role to its construction spec and optional ZeroCurve object.
    This keeps curve selection explicit: a Treasury curve should not silently be
    used as an OIS discount curve unless the caller deliberately assigns it.
    """
    role: CurveRole
    spec: CurveSpec
    curve: ZeroCurve | None = None
    source: str = "internal"

    def row(self) -> dict[str, float | str | None]:
        spec_row = self.spec.row()
        return {"role": self.role.value, "purpose": ROLE_PURPOSES[self.role], "source": self.source, "has_curve": self.curve is not None, **spec_row}


@dataclass
class MultiCurveSet:
    """
    Registry of curves by role for pricing and risk workflows.
    The first version is intentionally small: it records which curve is meant
    for which purpose and gives pricers one place to request the right curve.
    """
    currency: str = "USD"
    definitions: dict[CurveRole, CurveDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.currency.strip():
            raise ValueError("currency is required.")
        self.currency = self.currency.upper()
        for definition in self.definitions.values():
            if definition.spec.currency != self.currency:
                raise ValueError("All curve definitions must match the MultiCurveSet currency.")

    def add_curve(self, role: CurveRole, spec: CurveSpec, curve: ZeroCurve | None = None, source: str = "internal") -> None:
        if spec.currency != self.currency:
            raise ValueError("CurveSpec currency must match the MultiCurveSet currency.")
        self.definitions[role] = CurveDefinition(role=role, spec=spec, curve=curve, source=source)

    def has_curve(self, role: CurveRole) -> bool:
        return role in self.definitions and self.definitions[role].curve is not None

    def get_curve(self, role: CurveRole) -> ZeroCurve:
        if role not in self.definitions:
            raise KeyError(f"Curve role is not registered: {role.value}.")
        curve = self.definitions[role].curve
        if curve is None:
            raise ValueError(f"Curve role is registered without a curve object: {role.value}.")
        return curve

    def require_roles(self, roles: list[CurveRole]) -> None:
        missing = [role.value for role in roles if not self.has_curve(role)]
        if missing:
            raise ValueError(f"Missing required curve roles: {', '.join(missing)}.")

    def rows(self) -> list[dict[str, float | str | None]]:
        return [definition.row() for definition in self.definitions.values()]


@dataclass
class TradeCurveContext:
    """
    Describes which curve roles a trade should use.
    This is the policy layer behind the industry statement: the curve depends
    on collateral agreement, floating index, benchmark, funding, credit, and XVA.
    """
    discounting: CurveRole = CurveRole.SOFR_OIS_DISCOUNT
    projection: CurveRole | None = None
    benchmark: CurveRole | None = CurveRole.TREASURY_BENCHMARK
    credit: CurveRole | None = None
    funding: CurveRole | None = None
    xva: list[CurveRole] = field(default_factory=list)

    def required_roles(self) -> list[CurveRole]:
        roles = [self.discounting]
        roles.extend(role for role in [self.projection, self.benchmark, self.credit, self.funding] if role is not None)
        roles.extend(self.xva)
        return list(dict.fromkeys(roles))

    def validate_against(self, curve_set: MultiCurveSet) -> None:
        curve_set.require_roles(self.required_roles())
