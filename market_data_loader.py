"""
Market-data loading and live Treasury curve construction.
This module connects the analytics code to real market quotes. FRED Treasury
CMT rates are loaded as par-style yields, stored in an auditable snapshot, then
passed into the bootstrapping layer to create the ZeroCurve used for reporting,
charting, and bond pricing.
"""
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
import pandas as pd
from pandas import Timestamp
from pandas._libs import NaTType
from bootstrapping import bootstrap_discount_factors_from_par_yields, bootstrap_zero_rates_from_par_yields, write_interactive_curve_html
from config import DEFAULT_CURVE_BUILD_SETTINGS, FRED_CACHE_DIR
from yield_curve import ZeroCurve


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
    curve_build_method: str = DEFAULT_CURVE_BUILD_SETTINGS.curve_build_method
    interpolation_method: str = DEFAULT_CURVE_BUILD_SETTINGS.interpolation_method
    extrapolation_method: str = DEFAULT_CURVE_BUILD_SETTINGS.extrapolation_method

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
        a ZeroCurve, bond pricing, forwards, par yields, and charting all use a
        consistent set of bootstrapped zero rates.
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

    def write_plot(self, output_path: str | Path = "curve_plot.html") -> Path:
        """
        Writes the live Treasury curve chart for this exact market snapshot.
        The generated HTML is an output artifact, while this method is the
        reproducible source path that ties the chart back to its valuation date
        and market quotes.
        """
        return write_interactive_curve_html(maturities=self.maturities, par_yields=self.par_yields,frequency=self.frequency,
                                output_path=output_path, title=f"Live {self.source} Treasury Curve - {self.valuation_date}")


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
    # FRED sometimes uses "." for missing values.
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
    return TreasuryCurveSnapshot(valuation_date=selected_date.date(),maturities=maturities,par_yields=par_yields,
        downloaded_at=datetime.now(timezone.utc),source_urls={series_id: FRED_GRAPH_URL_TEMPLATE.format(series_id=series_id)
            for series_id in FRED_TREASURY_SERIES}, series_ids=list(FRED_TREASURY_SERIES))


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


def write_fred_treasury_curve_plot(date: str | None = None,frequency: int = 2,output_path: str | Path = "curve_plot.html",
                                   cache_dir: str | Path = FRED_CACHE_DIR, use_cache: bool = True,refresh_cache: bool = False) -> Path:
    """
    Writes an interactive plot using live FRED Treasury par-yield data.
    This function backs the user's live chart workflow: FRED data is downloaded,
    bootstrapped, and visualized without relying on sample data from bootstrapping.py.
    """
    snapshot = load_fred_treasury_curve_snapshot(date=date,frequency=frequency,cache_dir=cache_dir,use_cache=use_cache,refresh_cache=refresh_cache)
    return snapshot.write_plot(output_path=output_path)


if __name__ == "__main__":
    snapshot = load_fred_treasury_curve_snapshot()
    print(f"Downloaded Treasury Par Yield Curve from FRED for {snapshot.valuation_date}")
    print("-" * 50)
    for maturity, par_yield in zip(snapshot.maturities, snapshot.par_yields):
        print(f"{maturity:>6.3f}Y | Par yield: {par_yield:.4%}")
    print()
    print("Using Bootstrapped FRED ZeroCurve")
    print("-" * 50)
    curve = snapshot.to_zero_curve()
    target_maturity = 4.0
    interpolated_rate = curve.interpolate_rate(target_maturity)
    discount_factor = curve.discount_factor(target_maturity)
    print(f"Target maturity: {target_maturity}Y")
    print(f"Interpolated rate: {interpolated_rate:.4%}")
    print(f"Discount factor: {discount_factor:.6f}")
    print()
    print("Forward Rate Example")
    print("-" * 50)
    start_maturity = 2.0
    end_maturity = 5.0
    forward_rate = curve.forward_rate(start_maturity=start_maturity,end_maturity=end_maturity)
    print(f"Forward rate from {start_maturity}Y to {end_maturity}Y: {forward_rate:.4%}")
    print()
    print("Interactive Live FRED Curve Plot")
    print("-" * 50)
    output_file = snapshot.write_plot(output_path="curve_plot.html")
    print(f"Wrote live interactive chart to: {output_file}")
