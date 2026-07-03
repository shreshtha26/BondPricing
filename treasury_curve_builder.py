"""
Instrument-level Treasury curve construction from actual bill/note/bond quotes.

FRED CMT data is useful for learning and for a lightweight live curve, but a
market curve used in professional fixed-income work is normally built from
actual traded instruments. This module moves the project in that direction:
Treasury bills contribute zero-coupon discount factors, and Treasury notes and
bonds contribute coupon cashflow pricing equations.
"""

import csv
import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable

from scipy.optimize import brentq

from int_rate_convention import DayCountConvention, year_fraction
from treasury_instruments import TreasuryBill, TreasuryBond, TreasuryNote
from yield_curve import TOLERANCE, ZeroCurve, interpolate_curve_value


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


def _solve_with_expanding_bracket(
    objective,
    lower: float = -0.25,
    upper: float = 0.25,
    max_abs_bound: float = 5.0,
) -> float:
    """
    Solves one bootstrap node while allowing unusual rate environments.

    Negative rates and very high stressed rates are both possible. The bracket
    expands symmetrically until the objective crosses zero or failure is clear.
    """

    lower_value = objective(lower)
    upper_value = objective(upper)

    while lower_value * upper_value > 0:
        lower *= 2
        upper *= 2

        if abs(lower) > max_abs_bound or abs(upper) > max_abs_bound:
            raise ValueError("Could not bracket a Treasury zero-rate solution.")

        lower_value = objective(lower)
        upper_value = objective(upper)

    return brentq(objective, lower, upper)


def _discount_factor_with_candidate(
    time_years: float,
    solved_maturities: list[float],
    solved_zero_rates: list[float],
    candidate_maturity: float,
    candidate_zero_rate: float,
    allow_short_end_extrapolation: bool,
) -> float:
    """
    Discounts one cashflow while solving the current Treasury node.

    The temporary curve consists of completed earlier nodes plus the candidate
    zero rate at the instrument's maturity.
    """

    if time_years == 0:
        return 1.0

    maturities = solved_maturities + [candidate_maturity]
    zero_rates = solved_zero_rates + [candidate_zero_rate]
    zero_rate = interpolate_curve_value(target_time=time_years, times=maturities, values=zero_rates, allow_left_extrapolation=allow_short_end_extrapolation,
                                        empty_error="At least one curve point is required for interpolation.",
                                        left_error="A coupon cashflow occurs before the first solved curve point. Add short Treasury bills/cash instruments, "
                                                   "or explicitly allow short-end extrapolation.",
                                        right_error="Interpolation target is beyond the candidate maturity.", single_point_error="A one-point partial curve cannot interpolate.")

    return math.exp(-zero_rate * time_years)


def _append_curve_point(
    solved_maturities: list[float],
    solved_zero_rates: list[float],
    maturity_years: float,
    zero_rate: float,
) -> None:
    """
    Adds a solved node and rejects duplicate maturities.

    Sequential bootstrapping assumes one market equation per curve node. If two
    instruments have the same maturity, production systems fit or select them;
    this first version raises clearly instead of silently choosing one.
    """

    for existing_maturity in solved_maturities:
        if math.isclose(maturity_years, existing_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            raise ValueError(f"Duplicate Treasury maturity: {maturity_years}.")

    solved_maturities.append(maturity_years)
    solved_zero_rates.append(zero_rate)


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
            present_value += normalized_amount * _discount_factor_with_candidate(
                time_years=time_from_settlement,
                solved_maturities=solved_maturities,
                solved_zero_rates=solved_zero_rates,
                candidate_maturity=maturity_years,
                candidate_zero_rate=candidate_zero_rate,
                allow_short_end_extrapolation=allow_short_end_extrapolation,
            )

        return present_value - normalized_dirty_price

    zero_rate = _solve_with_expanding_bracket(price_error)
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

        _append_curve_point(
            solved_maturities=solved_maturities,
            solved_zero_rates=solved_zero_rates,
            maturity_years=point.maturity_years,
            zero_rate=point.zero_rate,
        )
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


def _parse_optional_float(row: dict[str, str], column: str) -> float | None:
    value = row.get(column, "").strip()

    if value == "":
        return None

    return float(value)


def _parse_decimal_rate(row: dict[str, str], column: str) -> float | None:
    value = _parse_optional_float(row, column)

    if value is None:
        return None

    if abs(value) > 1:
        return value / 100

    return value


def _parse_optional_int(row: dict[str, str], column: str, default: int) -> int:
    value = row.get(column, "").strip()

    if value == "":
        return default

    return int(value)


def _parse_required_date(row: dict[str, str], column: str) -> date:
    value = row.get(column, "").strip()

    if value == "":
        raise ValueError(f"Missing required column value: {column}.")

    return date.fromisoformat(value)


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
            issue_date = _parse_required_date(row, "issue_date")
            maturity_date = _parse_required_date(row, "maturity_date")
            face_value = _parse_optional_float(row, "face_value") or 100.0

            try:
                if instrument_type in {"BILL", "TBILL", "T-BILL"}:
                    instruments.append(
                        TreasuryBill(
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            discount_yield=_parse_decimal_rate(row, "discount_yield"),
                            price=_parse_optional_float(row, "price"),
                            face_value=face_value,
                            settlement_lag_days=_parse_optional_int(row, "settlement_lag_days", 1),
                        )
                    )
                elif instrument_type in {"NOTE", "TNOTE", "T-NOTE"}:
                    coupon_rate = _parse_decimal_rate(row, "coupon_rate")

                    if coupon_rate is None:
                        raise ValueError("coupon_rate is required for notes.")

                    instruments.append(
                        TreasuryNote(
                            coupon_rate=coupon_rate,
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            clean_price=_parse_optional_float(row, "clean_price"),
                            face_value=face_value,
                            frequency=_parse_optional_int(row, "frequency", 2),
                            settlement_lag_days=_parse_optional_int(row, "settlement_lag_days", 1),
                        )
                    )
                elif instrument_type in {"BOND", "TBOND", "T-BOND"}:
                    coupon_rate = _parse_decimal_rate(row, "coupon_rate")

                    if coupon_rate is None:
                        raise ValueError("coupon_rate is required for bonds.")

                    instruments.append(
                        TreasuryBond(
                            coupon_rate=coupon_rate,
                            issue_date=issue_date,
                            maturity_date=maturity_date,
                            clean_price=_parse_optional_float(row, "clean_price"),
                            face_value=face_value,
                            frequency=_parse_optional_int(row, "frequency", 2),
                            settlement_lag_days=_parse_optional_int(row, "settlement_lag_days", 1),
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

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = result.rows()

    with output_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(rows[0]) if rows else []
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return output_path
