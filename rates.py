"""
SOFR/OIS and interest-rate curve construction.
"""

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from config import FRED_CACHE_DIR
from conventions import (BusinessDayConvention, DateGenerationRule, DayCountConvention, MarketCalendar, NEW_YORK_BANK,
                         add_months, generate_coupon_schedule, validate_compounding_frequency, validate_rate, year_fraction)
from curves import TOLERANCE, ZeroCurve, append_unique_curve_point, partial_curve_discount_factor, solve_with_expanding_bracket
from market_data import export_rows_to_csv, parse_decimal_rate, parse_optional_int


@dataclass
class OISQuote:
    """
    Par fixed-rate quote for an overnight index swap.

    The quote supplies one market equation for the SOFR/OIS bootstrap. The
    fixed rate is the coupon rate that makes the fixed leg equal the overnight
    floating leg under the discount curve being solved.
    """

    fixed_rate: float
    tenor_months: int | None = None
    maturity_date: date | None = None
    fixed_leg_frequency: int = 1
    fixed_leg_day_count: DayCountConvention | str = DayCountConvention.ACT_360
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.MODIFIED_FOLLOWING

    def __post_init__(self) -> None:
        """
        Validates quote economics before bootstrapping.

        A usable OIS quote must define a positive maturity either by tenor or by
        explicit maturity date. Rates may be negative in stressed markets, so
        the validation only requires finite rates.
        """

        validate_rate(self.fixed_rate, "fixed_rate")
        validate_compounding_frequency(self.fixed_leg_frequency)

        if self.tenor_months is None and self.maturity_date is None:
            raise ValueError("OISQuote requires tenor_months or maturity_date.")

        if self.tenor_months is not None and self.tenor_months <= 0:
            raise ValueError("tenor_months must be positive.")

    def resolved_maturity_date(
        self,
        effective_date: date,
        calendar: MarketCalendar = NEW_YORK_BANK,
    ) -> date:
        """
        Resolves the quote maturity from tenor or explicit maturity date.

        This connects market quote tenors such as 3M or 5Y to actual payment
        dates that can be discounted by the curve.
        """

        if self.maturity_date is not None:
            return calendar.adjust(input_date=self.maturity_date, convention=self.business_day_convention)

        maturity = add_months(effective_date, self.tenor_months or 0)

        return calendar.adjust(input_date=maturity, convention=self.business_day_convention)

    def fixed_leg_schedule(
        self,
        effective_date: date,
        calendar: MarketCalendar = NEW_YORK_BANK,
    ) -> list[date]:
        """
        Generates fixed-leg payment dates for this OIS quote.

        OIS valuation is a cashflow problem just like bond pricing: generate
        accrual periods, discount each payment, and solve the curve node that
        makes the par swap equation hold.
        """

        maturity = self.resolved_maturity_date(effective_date=effective_date, calendar=calendar)
        holidays = calendar.holidays(start_year=effective_date.year - 1, end_year=maturity.year + 1)

        return generate_coupon_schedule(
            start_date=effective_date,
            maturity_date=maturity,
            frequency=self.fixed_leg_frequency,
            business_day_convention=self.business_day_convention,
            date_generation_rule=DateGenerationRule.BACKWARD,
            holidays=holidays,
        )


@dataclass
class OISBootstrapPoint:
    """
    Audit row for one SOFR/OIS bootstrap node.
    """

    quote_type: str
    maturity_date: date
    maturity_years: float
    fixed_rate: float
    discount_factor: float
    zero_rate: float
    fixed_leg_annuity: float


@dataclass
class SOFROISCurveResult:
    """
    Result object for SOFR/OIS curve construction.

    The curve can be used wherever collateralized USD discount factors are
    needed, while the points preserve the overnight and OIS quotes used to
    build it.
    """

    effective_date: date
    curve: ZeroCurve
    points: list[OISBootstrapPoint]
    source: str = "SOFR fixing and OIS par quotes"
    quote_type: str = "SOFR overnight fixing plus par OIS fixed rates"
    curve_build_method: str = "Sequential SOFR/OIS bootstrapping"
    interpolation_method: str = "Linear interpolation on continuously compounded zero rates"

    def rows(self) -> list[dict[str, float | str]]:
        """
        Builds report-ready rows for the SOFR/OIS curve.
        """

        return [
            {
                "effective_date": self.effective_date.isoformat(),
                "source": self.source,
                "quote_type": self.quote_type,
                "curve_build_method": self.curve_build_method,
                "interpolation_method": self.interpolation_method,
                "node_type": point.quote_type,
                "maturity_date": point.maturity_date.isoformat(),
                "maturity_years": point.maturity_years,
                "fixed_rate": point.fixed_rate,
                "discount_factor": point.discount_factor,
                "zero_rate": point.zero_rate,
                "fixed_leg_annuity": point.fixed_leg_annuity,
            }
            for point in self.points
        ]


def _overnight_point(
    effective_date: date,
    overnight_rate: float,
    calendar: MarketCalendar,
    overnight_day_count: DayCountConvention | str,
    discount_day_count: DayCountConvention | str,
) -> OISBootstrapPoint:
    """
    Converts the one-day SOFR fixing into the first discount factor.

    The overnight fixing accrues using money-market day count, then is converted
    into a continuously compounded zero rate for the project ZeroCurve.
    """

    validate_rate(overnight_rate, "overnight_rate")
    next_business_day = calendar.advance_business_days(effective_date, 1)
    accrual = year_fraction(start_date=effective_date, end_date=next_business_day, convention=overnight_day_count)
    maturity_years = year_fraction(start_date=effective_date, end_date=next_business_day, convention=discount_day_count)

    if accrual <= 0 or maturity_years <= 0:
        raise ValueError("Overnight accrual period must be positive.")

    discount_factor = 1 / (1 + overnight_rate * accrual)
    zero_rate = -math.log(discount_factor) / maturity_years

    return OISBootstrapPoint(
        quote_type="SOFR_OVERNIGHT",
        maturity_date=next_business_day,
        maturity_years=maturity_years,
        fixed_rate=overnight_rate,
        discount_factor=discount_factor,
        zero_rate=zero_rate,
        fixed_leg_annuity=accrual,
    )


def _ois_payment_grid(
    quote: OISQuote,
    effective_date: date,
    calendar: MarketCalendar,
    discount_day_count: DayCountConvention | str,
) -> tuple[date, float, list[tuple[float, float]]]:
    """
    Converts an OIS quote into payment times and fixed-leg accruals.
    """

    schedule = quote.fixed_leg_schedule(effective_date=effective_date, calendar=calendar)
    payment_grid: list[tuple[float, float]] = []

    for accrual_start, payment_date in zip(schedule[:-1], schedule[1:]):
        accrual = year_fraction(start_date=accrual_start, end_date=payment_date, convention=quote.fixed_leg_day_count, frequency=quote.fixed_leg_frequency,
                                coupon_start_date=accrual_start, coupon_end_date=payment_date)
        payment_time = year_fraction(start_date=effective_date, end_date=payment_date, convention=discount_day_count, frequency=quote.fixed_leg_frequency)

        if accrual <= 0 or payment_time <= 0:
            raise ValueError("OIS payment accruals and times must be positive.")

        payment_grid.append((payment_time, accrual))

    if not payment_grid:
        raise ValueError("OIS quote generated no fixed-leg payments.")

    maturity_date = schedule[-1]
    maturity_years = payment_grid[-1][0]

    return maturity_date, maturity_years, payment_grid


def bootstrap_sofr_ois_curve(
    effective_date: date,
    overnight_rate: float,
    ois_quotes: list[OISQuote],
    calendar: MarketCalendar = NEW_YORK_BANK,
    overnight_day_count: DayCountConvention | str = DayCountConvention.ACT_360,
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED,
) -> SOFROISCurveResult:
    """
    Bootstraps a continuously compounded SOFR/OIS zero curve.

    For a spot-starting par OIS, the first-version equation is:

    fixed_rate * sum(accrual_i * DF_i) = 1 - DF_T

    Rearranged for root solving:

    fixed_rate * annuity + DF_T - 1 = 0

    This is a collateralized discount curve, conceptually separate from a
    Treasury curve because OIS reflects overnight funding/collateral economics
    rather than Treasury bond supply, liquidity, and term-premium effects.
    """

    if not ois_quotes:
        raise ValueError("At least one OIS quote is required.")

    overnight = _overnight_point(
        effective_date=effective_date,
        overnight_rate=overnight_rate,
        calendar=calendar,
        overnight_day_count=overnight_day_count,
        discount_day_count=discount_day_count,
    )

    solved_maturities = [overnight.maturity_years]
    solved_zero_rates = [overnight.zero_rate]
    bootstrap_points = [overnight]

    sorted_quotes = sorted(ois_quotes, key=lambda quote: quote.resolved_maturity_date(effective_date=effective_date, calendar=calendar))

    for quote in sorted_quotes:
        maturity_date, maturity_years, payment_grid = _ois_payment_grid(
            quote=quote,
            effective_date=effective_date,
            calendar=calendar,
            discount_day_count=discount_day_count,
        )

        def ois_par_error(candidate_zero_rate: float) -> float:
            annuity = 0.0

            for payment_time, accrual in payment_grid:
                annuity += accrual * partial_curve_discount_factor(
                    time_years=payment_time,
                    solved_maturities=solved_maturities,
                    solved_zero_rates=solved_zero_rates,
                    candidate_maturity=maturity_years,
                    candidate_zero_rate=candidate_zero_rate,
                    allow_left_extrapolation=True,
                    empty_error="At least one curve point is required.",
                    right_error="Interpolation target is beyond the candidate maturity.",
                )

            final_discount_factor = partial_curve_discount_factor(
                time_years=maturity_years,
                solved_maturities=solved_maturities,
                solved_zero_rates=solved_zero_rates,
                candidate_maturity=maturity_years,
                candidate_zero_rate=candidate_zero_rate,
                allow_left_extrapolation=True,
                empty_error="At least one curve point is required.",
                right_error="Interpolation target is beyond the candidate maturity.",
            )

            return quote.fixed_rate * annuity + final_discount_factor - 1.0

        zero_rate = solve_with_expanding_bracket(ois_par_error, failure_message="Could not bracket a SOFR/OIS zero-rate solution.")
        discount_factor = math.exp(-zero_rate * maturity_years)
        fixed_leg_annuity = sum(
            accrual
            * partial_curve_discount_factor(
                time_years=payment_time,
                solved_maturities=solved_maturities,
                solved_zero_rates=solved_zero_rates,
                candidate_maturity=maturity_years,
                candidate_zero_rate=zero_rate,
                allow_left_extrapolation=True,
                empty_error="At least one curve point is required.",
                right_error="Interpolation target is beyond the candidate maturity.",
            )
            for payment_time, accrual in payment_grid
        )

        append_unique_curve_point(maturities=solved_maturities, values=solved_zero_rates, maturity=maturity_years, value=zero_rate,
                                  duplicate_message=f"Duplicate SOFR/OIS maturity: {maturity_years}.", tolerance=TOLERANCE)
        bootstrap_points.append(
            OISBootstrapPoint(
                quote_type="OIS",
                maturity_date=maturity_date,
                maturity_years=maturity_years,
                fixed_rate=quote.fixed_rate,
                discount_factor=discount_factor,
                zero_rate=zero_rate,
                fixed_leg_annuity=fixed_leg_annuity,
            )
        )

    curve = ZeroCurve(maturities=solved_maturities, zero_rates=solved_zero_rates)

    return SOFROISCurveResult(
        effective_date=effective_date,
        curve=curve,
        points=bootstrap_points,
    )

def load_ois_quotes_from_csv(path: str | Path) -> list[OISQuote]:
    """
    Loads OIS par quotes from CSV.

    Required column:
    fixed_rate

    Provide either tenor_months or maturity_date. Rates may be decimal or
    percentage. Optional columns: fixed_leg_frequency, fixed_leg_day_count.
    """

    quotes: list[OISQuote] = []

    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row_number, row in enumerate(reader, start=2):
            try:
                maturity_text = row.get("maturity_date", "").strip()
                maturity = date.fromisoformat(maturity_text) if maturity_text else None
                tenor_months = parse_optional_int(row, "tenor_months")
                fixed_leg_frequency = parse_optional_int(row, "fixed_leg_frequency") or 1
                fixed_leg_day_count = row.get("fixed_leg_day_count", "").strip() or DayCountConvention.ACT_360

                quotes.append(
                    OISQuote(
                        fixed_rate=parse_decimal_rate(row, "fixed_rate", required=True),
                        tenor_months=tenor_months,
                        maturity_date=maturity,
                        fixed_leg_frequency=fixed_leg_frequency,
                        fixed_leg_day_count=fixed_leg_day_count,
                    )
                )
            except Exception as error:
                raise ValueError(f"Invalid OIS CSV row {row_number}: {error}") from error

    return quotes


def load_latest_sofr_fixing_from_fred(
    date_value: str | date | None = None,
    cache_dir: str | Path = FRED_CACHE_DIR,
    use_cache: bool = True,
    refresh_cache: bool = False,
) -> tuple[date, float]:
    """
    Loads the latest SOFR fixing from FRED.

    FRED provides the overnight SOFR time series. OIS par swap quotes are not
    supplied by FRED in this project, so they should come from a vendor export
    or CSV and then be passed to bootstrap_sofr_ois_curve().
    """

    import pandas as pd

    from market_data import download_fred_series_with_cache

    series_id = "SOFR"
    df = download_fred_series_with_cache(
        series_id=series_id,
        cache_dir=cache_dir,
        use_cache=use_cache,
        refresh_cache=refresh_cache,
    ).dropna(subset=[series_id])

    if df.empty:
        raise ValueError("No SOFR fixings found from FRED.")

    if date_value is None:
        selected = df.loc[df["observation_date"].idxmax()]
    else:
        selected_date = pd.to_datetime(date_value)
        matches = df[df["observation_date"] == selected_date]

        if matches.empty:
            available_start = df["observation_date"].min().date()
            available_end = df["observation_date"].max().date()
            raise ValueError(
                f"No SOFR fixing found for {selected_date.date()}. "
                f"Available range: {available_start} to {available_end}."
            )

        selected = matches.iloc[0]

    return selected["observation_date"].date(), float(selected[series_id]) / 100


def export_sofr_ois_curve_report(
    result: SOFROISCurveResult,
    output_path: str | Path,
) -> Path:
    """
    Writes an auditable CSV report for a SOFR/OIS bootstrap.
    """

    return export_rows_to_csv(result.rows(), output_path)
