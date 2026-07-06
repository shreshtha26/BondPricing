"""
Treasury instruments and instrument-level Treasury curve bootstrapping.
"""

import csv
import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Iterable

from conventions import (BusinessDayConvention, DayCountConvention, DateGenerationRule, MarketCalendar, US_GOVERNMENT_SECURITIES,
                         validate_compounding_frequency, validate_rate, year_fraction)
from curves import TOLERANCE, ZeroCurve, append_unique_curve_point, partial_curve_discount_factor, solve_with_expanding_bracket
from market_data import export_rows_to_csv, parse_decimal_rate, parse_optional_float, parse_optional_int, parse_required_date
from pricing import DateAwareFixedCouponBond, dirty_price_from_clean


@dataclass
class TreasuryBill:
    """
    Short-term zero-coupon Treasury quoted on a bank-discount basis.

    Treasury bills do not pay coupons. Their quote convention differs from
    Treasury notes and bonds, so modeling them separately avoids mixing bill
    discount yields with coupon-bond par yields.
    """

    issue_date: date
    maturity_date: date
    discount_yield: float | None = None
    price: float | None = None
    face_value: float = 100.0
    day_count: DayCountConvention | str = DayCountConvention.ACT_360
    calendar: MarketCalendar = field(default_factory=lambda: US_GOVERNMENT_SECURITIES)
    settlement_lag_days: int = 1

    def __post_init__(self) -> None:
        """
        Validates bill economics before the instrument enters bootstrapping.

        A bill can be supplied either as an actual settlement price or as the
        market's bank-discount yield. The curve builder ultimately needs a
        price/discount factor, so this guard makes sure one source is available.
        """

        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must be before maturity_date.")

        if not math.isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError("face_value must be positive and finite.")

        if self.discount_yield is None and self.price is None:
            raise ValueError("TreasuryBill requires either discount_yield or price.")

        if self.discount_yield is not None:
            validate_rate(self.discount_yield, "discount_yield")

        if self.price is not None and (not math.isfinite(self.price) or self.price <= 0):
            raise ValueError("price must be positive and finite.")

    def settlement_date(self, trade_date: date) -> date:
        """
        Calculates the standard bill settlement date from a trade date.

        Treasury settlement is a calendar problem, not just a number of days.
        Routing this through MarketCalendar keeps bill analytics aligned with
        the broader project convention layer.
        """

        return self.calendar.settlement_date(trade_date=trade_date, settlement_lag_days=self.settlement_lag_days)

    def price_from_discount_yield(self, settlement_date: date) -> float:
        """
        Converts a bank-discount yield quote into a price.

        Bills are often quoted by bank discount yield rather than price or bond
        equivalent yield. This method performs the market-convention conversion
        needed before a bill can contribute a discount factor to the curve.
        """

        if self.discount_yield is None:
            raise ValueError("discount_yield is required to derive bill price.")

        year_frac = year_fraction(start_date=settlement_date, end_date=self.maturity_date, convention=self.day_count)

        return self.face_value * (1 - self.discount_yield * year_frac)

    def market_price(self, settlement_date: date) -> float:
        """
        Returns the bill price used by instrument-level bootstrapping.

        Actual price is preferred when supplied. If only a discount yield is
        available, the method converts it into price using the bill convention.
        """

        if self.price is not None:
            return self.price

        return self.price_from_discount_yield(settlement_date=settlement_date)

    def discount_factor_from_price(self, price: float) -> float:
        """
        Converts bill price into a zero-coupon discount factor.

        Because bills pay a single maturity amount, price divided by face value
        is directly the market discount factor for the bill maturity.
        """

        if not math.isfinite(price) or price <= 0:
            raise ValueError("price must be positive and finite.")

        return price / self.face_value


@dataclass
class TreasuryNote:
    """
    Coupon-bearing Treasury with original maturity typically up to 10 years.

    Notes are represented as date-aware fixed-coupon bonds so they can reuse
    clean/dirty pricing, accrued interest, and curve-based valuation.
    """

    coupon_rate: float
    issue_date: date
    maturity_date: date
    clean_price: float | None = None
    face_value: float = 100.0
    frequency: int = 2
    day_count: DayCountConvention | str = DayCountConvention.ACT_ACT_ICMA
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.UNADJUSTED
    date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD
    end_of_month: bool = False
    calendar: MarketCalendar = field(default_factory=lambda: US_GOVERNMENT_SECURITIES)
    settlement_lag_days: int = 1

    def __post_init__(self) -> None:
        """
        Validates coupon-bearing Treasury economics.

        Instrument-level bootstrapping treats the quoted clean price as a market
        constraint. Validating here keeps bad prices, dates, or coupon settings
        from producing an apparently precise but financially meaningless curve.
        """

        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must be before maturity_date.")

        if not math.isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError("face_value must be positive and finite.")

        validate_rate(self.coupon_rate, "coupon_rate")

        if self.coupon_rate < 0:
            raise ValueError("coupon_rate cannot be negative.")

        validate_compounding_frequency(self.frequency)

        if self.clean_price is not None and (not math.isfinite(self.clean_price) or self.clean_price <= 0):
            raise ValueError("clean_price must be positive and finite.")

    def settlement_date(self, trade_date: date) -> date:
        """
        Calculates standard Treasury note/bond settlement from a trade date.

        This keeps actual market settlement lag and holiday handling outside
        the pricing formula but available to workflows that start from trades.
        """

        return self.calendar.settlement_date(trade_date=trade_date, settlement_lag_days=self.settlement_lag_days)

    def to_bond(self, settlement_date: date) -> DateAwareFixedCouponBond:
        """
        Converts the Treasury instrument into the project's pricing bond class.

        The DateAwareFixedCouponBond already owns coupon schedules, accrued
        interest, clean/dirty conversion, and curve pricing. Reusing it avoids
        two separate implementations of the same fixed-income mechanics.
        """

        holidays = self.calendar.holidays(start_year=self.issue_date.year - 1, end_year=self.maturity_date.year + 1)

        return DateAwareFixedCouponBond(
            face_value=self.face_value,
            coupon_rate=self.coupon_rate,
            issue_date=self.issue_date,
            maturity_date=self.maturity_date,
            settlement_date=settlement_date,
            frequency=self.frequency,
            day_count=self.day_count,
            discount_day_count=self.discount_day_count,
            business_day_convention=self.business_day_convention,
            date_generation_rule=self.date_generation_rule,
            end_of_month=self.end_of_month,
            holidays=holidays,
        )

    def accrued_interest(self, settlement_date: date) -> float:
        """
        Returns accrued interest at settlement.

        Quoted Treasury prices are clean. Bootstrapping needs dirty price, so
        accrued interest is the bridge from quoted market price to present value
        equation.
        """

        return self.to_bond(settlement_date=settlement_date).accrued_interest()

    def dirty_price(self, settlement_date: date) -> float:
        """
        Converts the quoted clean price into dirty settlement price.

        The dirty price is the value matched by discounted future cashflows in
        the instrument-level Treasury bootstrap.
        """

        if self.clean_price is None:
            raise ValueError("clean_price is required for coupon Treasury bootstrap.")

        return dirty_price_from_clean(clean_price=self.clean_price, accrued=self.accrued_interest(settlement_date=settlement_date))

    def future_cashflows(
        self,
        settlement_date: date,
    ) -> list[tuple[date, float, float]]:
        """
        Returns future dated cashflows for curve bootstrapping.

        Each cashflow includes payment date, time from settlement, and amount.
        The curve builder discounts this schedule to force model PV to equal
        the observed dirty price.
        """

        return self.to_bond(settlement_date=settlement_date).future_cashflow_schedule()


@dataclass
class TreasuryBond(TreasuryNote):
    """
    Longer-dated coupon-bearing Treasury.

    Treasury bonds share most mechanics with Treasury notes in this first
    version; the separate class keeps instrument identity explicit for future
    curve bootstrapping and reporting.
    """


TreasuryCurveInstrument = TreasuryBill | TreasuryNote | TreasuryBond


@dataclass
class TreasuryBootstrapPoint:
    """
    Audit row for one instrument used in the Treasury bootstrap.

    Curve construction should be explainable instrument by instrument. These
    rows show which market quote created each curve node, what dirty price was
    matched, and what discount factor / zero rate was implied.
    """

    instrument_type: str
    maturity_date: date
    maturity_years: float
    clean_price: float | None
    dirty_price: float
    accrued_interest: float
    discount_factor: float
    zero_rate: float
    coupon_rate: float | None = None


@dataclass
class TreasuryInstrumentCurveResult:
    """
    Result object for price-based Treasury curve construction.

    The ZeroCurve is the pricing object. The bootstrap points preserve market
    provenance so reports can explain how each curve node was obtained.
    """

    settlement_date: date
    curve: ZeroCurve
    points: list[TreasuryBootstrapPoint]
    source: str = "Treasury bill/note/bond market quotes"
    quote_type: str = "Bill price/discount yield and note/bond clean price"
    curve_build_method: str = "Sequential instrument-level bootstrapping"
    interpolation_method: str = "Linear interpolation on continuously compounded zero rates"
    extrapolation_method: str = "None unless allow_short_end_extrapolation=True"

    def rows(self) -> list[dict[str, float | str | None]]:
        """
        Builds CSV/report rows for the instrument-level curve.

        This mirrors the FRED snapshot reporting shape while keeping extra
        fields that matter for actual priced instruments.
        """

        return [
            {
                "settlement_date": self.settlement_date.isoformat(),
                "source": self.source,
                "quote_type": self.quote_type,
                "curve_build_method": self.curve_build_method,
                "interpolation_method": self.interpolation_method,
                "extrapolation_method": self.extrapolation_method,
                "instrument_type": point.instrument_type,
                "maturity_date": point.maturity_date.isoformat(),
                "maturity_years": point.maturity_years,
                "coupon_rate": point.coupon_rate,
                "clean_price": point.clean_price,
                "dirty_price": point.dirty_price,
                "accrued_interest": point.accrued_interest,
                "discount_factor": point.discount_factor,
                "zero_rate": point.zero_rate,
            }
            for point in self.points
        ]


def _bill_bootstrap_point(
    bill: TreasuryBill,
    settlement_date: date,
    discount_day_count: DayCountConvention | str,
) -> TreasuryBootstrapPoint:
    """
    Converts a Treasury bill into one zero-curve node.

    Bills are zero-coupon instruments, so their price directly implies the
    discount factor at maturity.
    """

    maturity_years = year_fraction(start_date=settlement_date, end_date=bill.maturity_date, convention=discount_day_count)

    if maturity_years <= 0:
        raise ValueError("Bill maturity must be after settlement date.")

    price = bill.market_price(settlement_date=settlement_date)
    discount_factor = bill.discount_factor_from_price(price)

    if discount_factor <= 0:
        raise ValueError("Bill discount factor must be positive.")

    zero_rate = -math.log(discount_factor) / maturity_years

    return TreasuryBootstrapPoint(
        instrument_type="BILL",
        maturity_date=bill.maturity_date,
        maturity_years=maturity_years,
        clean_price=price,
        dirty_price=price,
        accrued_interest=0.0,
        discount_factor=discount_factor,
        zero_rate=zero_rate,
        coupon_rate=None,
    )


def _coupon_treasury_bootstrap_point(
    instrument: TreasuryNote | TreasuryBond,
    settlement_date: date,
    discount_day_count: DayCountConvention | str,
    solved_maturities: list[float],
    solved_zero_rates: list[float],
    allow_short_end_extrapolation: bool,
) -> TreasuryBootstrapPoint:
    """
    Solves the zero rate implied by a coupon-bearing Treasury price.

    The equation is:

    observed dirty price = sum(future cashflow_i * DF_i)

    All earlier discount factors come from previously bootstrapped instruments.
    The current maturity's zero rate is solved so this instrument prices exactly
    to its observed dirty price.
    """

    pricing_instrument = replace(instrument, discount_day_count=discount_day_count)
    cashflows = pricing_instrument.future_cashflows(settlement_date=settlement_date)

    if not cashflows:
        raise ValueError("Coupon Treasury has no future cashflows.")

    maturity_date = cashflows[-1][0]
    maturity_years = cashflows[-1][1]

    if maturity_years <= 0:
        raise ValueError("Coupon Treasury maturity must be after settlement date.")

    dirty_price = pricing_instrument.dirty_price(settlement_date=settlement_date)
    normalized_dirty_price = dirty_price / pricing_instrument.face_value
    normalized_cashflows = [
        (time_from_settlement, amount / pricing_instrument.face_value)
        for _, time_from_settlement, amount in cashflows
    ]

    def price_error(candidate_zero_rate: float) -> float:
        present_value = 0.0

        for time_from_settlement, normalized_amount in normalized_cashflows:
            present_value += normalized_amount * partial_curve_discount_factor(
                time_years=time_from_settlement,
                solved_maturities=solved_maturities,
                solved_zero_rates=solved_zero_rates,
                candidate_maturity=maturity_years,
                candidate_zero_rate=candidate_zero_rate,
                allow_left_extrapolation=allow_short_end_extrapolation,
                empty_error="At least one curve point is required for interpolation.",
                left_error="A coupon cashflow occurs before the first solved curve point. Add short Treasury bills/cash instruments, "
                           "or explicitly allow short-end extrapolation.",
                right_error="Interpolation target is beyond the candidate maturity.",
                single_point_error="A one-point partial curve cannot interpolate.",
            )

        return present_value - normalized_dirty_price

    zero_rate = solve_with_expanding_bracket(price_error, failure_message="Could not bracket a Treasury zero-rate solution.")
    discount_factor = math.exp(-zero_rate * maturity_years)
    accrued = pricing_instrument.accrued_interest(settlement_date=settlement_date)

    return TreasuryBootstrapPoint(
        instrument_type="BOND" if isinstance(instrument, TreasuryBond) else "NOTE",
        maturity_date=maturity_date,
        maturity_years=maturity_years,
        clean_price=pricing_instrument.clean_price,
        dirty_price=dirty_price,
        accrued_interest=accrued,
        discount_factor=discount_factor,
        zero_rate=zero_rate,
        coupon_rate=pricing_instrument.coupon_rate,
    )


def bootstrap_treasury_zero_curve_from_prices(
    instruments: Iterable[TreasuryCurveInstrument],
    settlement_date: date,
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED,
    allow_short_end_extrapolation: bool = False,
) -> TreasuryInstrumentCurveResult:
    """
    Builds a Treasury zero curve from actual bill, note, and bond market quotes.

    Input instruments must normally start with short bills so coupon cashflows
    on notes/bonds can be discounted without extrapolation. Setting
    allow_short_end_extrapolation=True permits a flat short-end assumption, but
    that should be treated as a modeling fallback rather than market data.
    """

    sorted_instruments = sorted(list(instruments), key=lambda instrument: instrument.maturity_date)

    if not sorted_instruments:
        raise ValueError("At least one Treasury instrument is required.")

    solved_maturities: list[float] = []
    solved_zero_rates: list[float] = []
    bootstrap_points: list[TreasuryBootstrapPoint] = []

    for instrument in sorted_instruments:
        if isinstance(instrument, TreasuryBill):
            point = _bill_bootstrap_point(
                bill=instrument,
                settlement_date=settlement_date,
                discount_day_count=discount_day_count,
            )
        else:
            point = _coupon_treasury_bootstrap_point(
                instrument=instrument,
                settlement_date=settlement_date,
                discount_day_count=discount_day_count,
                solved_maturities=solved_maturities,
                solved_zero_rates=solved_zero_rates,
                allow_short_end_extrapolation=allow_short_end_extrapolation,
            )

        append_unique_curve_point(maturities=solved_maturities, values=solved_zero_rates, maturity=point.maturity_years, value=point.zero_rate,
                                  duplicate_message=f"Duplicate Treasury maturity: {point.maturity_years}.", tolerance=TOLERANCE)
        bootstrap_points.append(point)

    curve = ZeroCurve(maturities=solved_maturities, zero_rates=solved_zero_rates)

    return TreasuryInstrumentCurveResult(
        settlement_date=settlement_date,
        curve=curve,
        points=bootstrap_points,
        extrapolation_method=(
            "Flat short-end extrapolation"
            if allow_short_end_extrapolation
            else "None"
        ),
    )


def build_zero_curve_from_treasury_instruments(
    instruments: Iterable[TreasuryCurveInstrument],
    settlement_date: date,
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED,
    allow_short_end_extrapolation: bool = False,
) -> ZeroCurve:
    """
    Convenience wrapper returning only the ZeroCurve.

    Reporting workflows should keep TreasuryInstrumentCurveResult. Quick
    pricing experiments can use this shorter adapter.
    """

    result = bootstrap_treasury_zero_curve_from_prices(instruments=instruments, settlement_date=settlement_date, discount_day_count=discount_day_count,
                                                       allow_short_end_extrapolation=allow_short_end_extrapolation)
    return result.curve


def load_treasury_instruments_from_csv(
    path: str | Path,
) -> list[TreasuryCurveInstrument]:
    """
    Loads Treasury bill/note/bond inputs from a CSV file.

    Required columns:
    instrument_type, issue_date, maturity_date

    Bill rows require either price or discount_yield.
    Note/bond rows require coupon_rate and clean_price.

    Rates may be decimals (0.045) or percentages (4.5). Prices are quoted per
    100 face value unless face_value is supplied.
    """

    instruments: list[TreasuryCurveInstrument] = []

    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row_number, row in enumerate(reader, start=2):
            instrument_type = row.get("instrument_type", "").strip().upper()
            issue_date = parse_required_date(row, "issue_date")
            maturity_date = parse_required_date(row, "maturity_date")
            face_value = parse_optional_float(row, "face_value") or 100.0

            try:
                if instrument_type in {"BILL", "TBILL", "T-BILL"}:
                    instruments.append(
                        TreasuryBill(
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            discount_yield=parse_decimal_rate(row, "discount_yield"),
                            price=parse_optional_float(row, "price"),
                            face_value=face_value,
                            settlement_lag_days=parse_optional_int(row, "settlement_lag_days", 1),
                        )
                    )
                elif instrument_type in {"NOTE", "TNOTE", "T-NOTE"}:
                    coupon_rate = parse_decimal_rate(row, "coupon_rate")

                    if coupon_rate is None:
                        raise ValueError("coupon_rate is required for notes.")

                    instruments.append(
                        TreasuryNote(
                            coupon_rate=coupon_rate,
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            clean_price=parse_optional_float(row, "clean_price"),
                            face_value=face_value,
                            frequency=parse_optional_int(row, "frequency", 2),
                            settlement_lag_days=parse_optional_int(row, "settlement_lag_days", 1),
                        )
                    )
                elif instrument_type in {"BOND", "TBOND", "T-BOND"}:
                    coupon_rate = parse_decimal_rate(row, "coupon_rate")

                    if coupon_rate is None:
                        raise ValueError("coupon_rate is required for bonds.")

                    instruments.append(
                        TreasuryBond(
                            coupon_rate=coupon_rate,
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            clean_price=parse_optional_float(row, "clean_price"),
                            face_value=face_value,
                            frequency=parse_optional_int(row, "frequency", 2),
                            settlement_lag_days=parse_optional_int(row, "settlement_lag_days", 1),
                        )
                    )
                else:
                    raise ValueError(f"Unsupported instrument_type: {instrument_type}.")
            except Exception as error:
                raise ValueError(f"Invalid Treasury CSV row {row_number}: {error}") from error

    return instruments


def export_treasury_bootstrap_report(
    result: TreasuryInstrumentCurveResult,
    output_path: str | Path,
) -> Path:
    """
    Writes an auditable CSV report for an instrument-level Treasury bootstrap.
    """
    return export_rows_to_csv(result.rows(), output_path)
