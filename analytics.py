"""
Calibration, validation, risk analytics, valuation snapshots, and P&L explain.
"""

import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from conventions import BASIS_POINT, validate_rate
from curves import TOLERANCE, ZeroCurve
from market_data import BondMarketQuote, SecurityMasterRecord, TreasuryCurveSnapshot, export_rows_to_csv
from pricing import DateAwareFixedCouponBond, MarketState, PricingContext, PricingResult, price


@dataclass
class CalibrationRow:
    """
    One instrument-level calibration diagnostic.
    The row answers the desk question: did this curve reproduce the market quote
    within tolerance or bid/ask, and by how much?
    """
    curve_name: str
    instrument_id: str
    maturity: float
    market_quote: float
    model_quote: float
    bid: float | None = None
    ask: float | None = None
    tolerance: float = 1e-10
    weight: float = 1.0

    def __post_init__(self) -> None:
        for field_name, field_value in {"maturity": self.maturity, "market_quote": self.market_quote, "model_quote": self.model_quote, "tolerance": self.tolerance, "weight": self.weight}.items():
            if not math.isfinite(field_value):
                raise ValueError(f"{field_name} must be finite.")
        if self.maturity <= 0:
            raise ValueError("maturity must be positive.")
        if self.tolerance <= 0:
            raise ValueError("tolerance must be positive.")
        if self.bid is not None and not math.isfinite(self.bid):
            raise ValueError("bid must be finite when provided.")
        if self.ask is not None and not math.isfinite(self.ask):
            raise ValueError("ask must be finite when provided.")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot be greater than ask.")

    @property
    def error(self) -> float:
        return self.model_quote - self.market_quote

    @property
    def error_bp(self) -> float:
        return self.error * 10000

    @property
    def inside_bid_ask(self) -> bool | None:
        if self.bid is None or self.ask is None:
            return None
        return self.bid <= self.model_quote <= self.ask

    @property
    def calibration_status(self) -> str:
        if self.inside_bid_ask is True:
            return "PASS_BID_ASK"
        if abs(self.error) <= self.tolerance:
            return "PASS_TOLERANCE"
        return "FAIL"

    def row(self) -> dict[str, float | str | bool | None]:
        return {"curve_name": self.curve_name, "instrument_id": self.instrument_id, "maturity": self.maturity, "market_quote": self.market_quote,
                "model_quote": self.model_quote, "error": self.error, "error_bp": self.error_bp, "bid": self.bid, "ask": self.ask,
                "inside_bid_ask": self.inside_bid_ask, "calibration_status": self.calibration_status, "tolerance": self.tolerance, "weight": self.weight}


def calibration_rows(curve_name: str, maturities: list[float], market_quotes: list[float], model_quotes: list[float],
                     instrument_ids: list[str] | None = None, bids: list[float | None] | None = None, asks: list[float | None] | None = None,
                     tolerance: float = 1e-10) -> list[CalibrationRow]:
    """
    Builds standardized calibration rows from parallel market/model arrays.
    This lets any curve builder produce the same diagnostics without repeating
    row assembly logic.
    """
    if not (len(maturities) == len(market_quotes) == len(model_quotes)):
        raise ValueError("maturities, market_quotes, and model_quotes must have the same length.")
    instrument_ids = instrument_ids or [f"{curve_name}_{maturity:g}Y" for maturity in maturities]
    bids = bids or [None for _ in maturities]
    asks = asks or [None for _ in maturities]
    if not (len(instrument_ids) == len(bids) == len(asks) == len(maturities)):
        raise ValueError("instrument_ids, bids, asks, and maturities must have the same length.")
    return [CalibrationRow(curve_name=curve_name, instrument_id=instrument_id, maturity=maturity, market_quote=market_quote, model_quote=model_quote,
                           bid=bid, ask=ask, tolerance=tolerance)
            for instrument_id, maturity, market_quote, model_quote, bid, ask in zip(instrument_ids, maturities, market_quotes, model_quotes, bids, asks)]


def export_calibration_report(rows: list[CalibrationRow], output_path: str | Path) -> Path:
    return export_rows_to_csv((row.row() for row in rows), output_path)


def bumped_key_rate_curve(curve: ZeroCurve, key_maturity: float, bump_size: float = BASIS_POINT) -> ZeroCurve:
    """
    Returns a curve where one quoted zero-rate node is bumped.
    A key-rate bump isolates sensitivity to one curve maturity instead of
    shifting the whole curve in parallel.
    """
    validate_rate(bump_size, "bump_size")
    if not math.isfinite(key_maturity) or key_maturity <= 0:
        raise ValueError("key_maturity must be positive and finite.")
    bumped_rates = curve.zero_rates.copy()
    for index, maturity in enumerate(curve.maturities):
        if math.isclose(maturity, key_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            bumped_rates[index] += bump_size
            return ZeroCurve(maturities=curve.maturities.copy(), zero_rates=bumped_rates)
    raise ValueError(f"key_maturity {key_maturity} is not an existing curve node.")


def key_rate_dv01_rows(bond: DateAwareFixedCouponBond, curve: ZeroCurve, bump_size: float = BASIS_POINT) -> list[dict[str, float]]:
    """
    Calculates node-by-node key-rate DV01 for a date-aware bond.
    DV01 is calculated as the price gain for a 1 bp decrease in the selected
    zero-rate node using a central difference.
    """
    validate_rate(bump_size, "bump_size")
    if bump_size <= 0:
        raise ValueError("bump_size must be positive.")
    base_dirty_price = bond.dirty_price_from_curve(curve)
    rows: list[dict[str, float]] = []
    for key_maturity in curve.maturities:
        price_down = bond.dirty_price_from_curve(bumped_key_rate_curve(curve, key_maturity, -bump_size))
        price_up = bond.dirty_price_from_curve(bumped_key_rate_curve(curve, key_maturity, bump_size))
        rows.append({
            "key_maturity": key_maturity,
            "base_dirty_price": base_dirty_price,
            "price_down_1bp": price_down,
            "price_up_1bp": price_up,
            "key_rate_dv01": (price_down - price_up) / 2,
            "bump_size": bump_size,
        })
    return rows


def export_key_rate_dv01_report(rows: list[dict[str, float]], output_path: str | Path) -> Path:
    """
    Writes key-rate DV01 rows to CSV.
    """
    return export_rows_to_csv(rows, output_path,
                              fieldnames=["key_maturity", "base_dirty_price", "price_down_1bp", "price_up_1bp", "key_rate_dv01", "bump_size"])


def calibration_report_rows(snapshot: TreasuryCurveSnapshot) -> list[dict[str, float | str]]:
    """
    Compares market par yields with par yields implied by the bootstrapped curve.
    Small residuals show that the bootstrap is internally consistent with the
    market quotes used to build the curve.
    """
    curve = snapshot.to_zero_curve()
    zero_rates = snapshot.zero_rates()
    discount_factors = snapshot.discount_factors()
    rows: list[dict[str, float | str]] = []
    for maturity, market_par_yield, zero_rate, discount_factor in zip(snapshot.maturities, snapshot.par_yields, zero_rates, discount_factors):
        model_par_yield = curve.par_yield(maturity=maturity, frequency=snapshot.frequency)
        calibration_row = CalibrationRow(curve_name="FRED Treasury CMT", instrument_id=f"{maturity:g}Y", maturity=maturity, market_quote=market_par_yield, model_quote=model_par_yield)
        residual = calibration_row.error
        rows.append({
            "valuation_date": snapshot.valuation_date.isoformat(),
            "maturity": maturity,
            "market_par_yield": market_par_yield,
            "model_par_yield": model_par_yield,
            "residual": residual,
            "residual_bp": calibration_row.error_bp,
            "calibration_status": calibration_row.calibration_status,
            "zero_rate": zero_rate,
            "discount_factor": discount_factor,
        })
    return rows


def clean_dirty_accrued_reconciliation_rows(bond: DateAwareFixedCouponBond, curve: ZeroCurve, tolerance: float = 1e-8) -> list[dict[str, float | str | bool]]:
    """
    Checks that clean price plus accrued interest equals dirty price.
    This validates the market quote convention used throughout bond pricing and
    Treasury instrument bootstrapping.
    """
    dirty_price = bond.dirty_price_from_curve(curve)
    accrued_interest = bond.accrued_interest()
    clean_price = bond.clean_price_from_curve(curve)
    reconstructed_dirty_price = clean_price + accrued_interest
    difference = dirty_price - reconstructed_dirty_price
    return [{
        "settlement_date": bond.settlement_date.isoformat(),
        "issue_date": bond.issue_date.isoformat(),
        "maturity_date": bond.maturity_date.isoformat(),
        "clean_price": clean_price,
        "accrued_interest": accrued_interest,
        "dirty_price": dirty_price,
        "clean_plus_accrued": reconstructed_dirty_price,
        "difference": difference,
        "tolerance": tolerance,
        "passed": abs(difference) <= tolerance,
    }]


@dataclass
class BondQuoteValidationRow:
    """
    CUSIP/ISIN-level model-vs-market validation for one bond quote.
    """
    security_id: str
    id_type: str
    valuation_date: date
    instrument_type: str
    quote_source: str
    price_type: str
    market_price: float
    model_price: float
    clean_price: float
    dirty_price: float
    accrued_interest: float
    bid: float | None = None
    ask: float | None = None
    tolerance: float = 0.02
    currency: str = "USD"

    @property
    def price_error(self) -> float:
        return self.model_price - self.market_price

    @property
    def price_error_bp_of_par(self) -> float:
        return self.price_error * 100

    @property
    def inside_bid_ask(self) -> bool | None:
        if self.bid is None or self.ask is None:
            return None
        return self.bid <= self.model_price <= self.ask

    @property
    def validation_status(self) -> str:
        if self.inside_bid_ask is True:
            return "PASS_BID_ASK"
        if abs(self.price_error) <= self.tolerance:
            return "PASS_TOLERANCE"
        return "FAIL"

    def row(self) -> dict[str, float | str | bool | None]:
        return {
            "security_id": self.security_id,
            "id_type": self.id_type,
            "valuation_date": self.valuation_date.isoformat(),
            "instrument_type": self.instrument_type,
            "quote_source": self.quote_source,
            "price_type": self.price_type,
            "market_price": self.market_price,
            "model_price": self.model_price,
            "price_error": self.price_error,
            "price_error_bp_of_par": self.price_error_bp_of_par,
            "bid": self.bid,
            "ask": self.ask,
            "inside_bid_ask": self.inside_bid_ask,
            "validation_status": self.validation_status,
            "clean_price": self.clean_price,
            "dirty_price": self.dirty_price,
            "accrued_interest": self.accrued_interest,
            "tolerance": self.tolerance,
            "currency": self.currency,
        }


def validate_bond_quote(security: SecurityMasterRecord, quote: BondMarketQuote, curve: ZeroCurve,
                        context: PricingContext | None = None, tolerance: float = 0.02) -> BondQuoteValidationRow:
    """
    Prices one security-master record and compares it with one observed quote.
    This is the compact first CUSIP/ISIN-level validation workflow.
    """
    if security.security_id != quote.security_id:
        raise ValueError("security and quote must have the same security_id.")
    if security.currency != quote.currency:
        raise ValueError("security and quote currencies must match.")
    instrument = security.to_instrument_spec()
    market = MarketState(valuation_date=quote.valuation_date, discount_curve=curve, quote_source=quote.quote_source, quote_timestamp=quote.timestamp)
    result: PricingResult = price(instrument, market, context=context)
    model_price = result.clean_price if quote.price_type == "clean" else result.dirty_price
    return BondQuoteValidationRow(
        security_id=security.security_id,
        id_type=security.id_type,
        valuation_date=quote.valuation_date,
        instrument_type=security.instrument_type,
        quote_source=quote.quote_source,
        price_type=quote.price_type,
        market_price=quote.effective_price(),
        model_price=model_price,
        clean_price=result.clean_price,
        dirty_price=result.dirty_price,
        accrued_interest=result.accrued_interest,
        bid=quote.bid,
        ask=quote.ask,
        tolerance=tolerance,
        currency=security.currency,
    )


def validate_bond_quotes(securities: list[SecurityMasterRecord], quotes: list[BondMarketQuote], curve: ZeroCurve,
                         context: PricingContext | None = None, tolerance: float = 0.02) -> list[BondQuoteValidationRow]:
    security_by_id = {security.security_id: security for security in securities}
    rows: list[BondQuoteValidationRow] = []
    for quote in quotes:
        if quote.security_id not in security_by_id:
            raise KeyError(f"Missing security master record for {quote.security_id}.")
        rows.append(validate_bond_quote(security=security_by_id[quote.security_id], quote=quote, curve=curve, context=context, tolerance=tolerance))
    return rows


def export_bond_quote_validation_report(rows: list[BondQuoteValidationRow], output_path: str | Path) -> Path:
    return export_rows_to_csv((row.row() for row in rows), output_path)


def export_report_rows(rows: list[dict[str, float | str | bool]], output_path: str | Path) -> Path:
    """
    Writes validation rows to CSV.
    """
    return export_rows_to_csv(rows, output_path)


@dataclass
class ValuationSnapshot:
    """
    One instrument valuation on one market date.
    This is the bridge from pricing to backtesting: price, accrued interest,
    parallel DV01, key-rate DV01, curve date, and calibration status are stored
    together so realized P&L can be explained later.
    """
    as_of_date: date
    instrument_id: str
    clean_price: float
    dirty_price: float
    accrued_interest: float
    parallel_dv01: float
    key_rate_dv01: dict[float, float]
    curve_date: date
    calibration_status: str = "NOT_RUN"
    currency: str = "USD"

    def key_rate_dv01_text(self) -> str:
        return ";".join(f"{maturity:g}:{dv01:.12g}" for maturity, dv01 in self.key_rate_dv01.items())

    def row(self) -> dict[str, float | str]:
        return {"as_of_date": self.as_of_date.isoformat(), "instrument_id": self.instrument_id, "clean_price": self.clean_price,
                "dirty_price": self.dirty_price, "accrued_interest": self.accrued_interest, "parallel_dv01": self.parallel_dv01,
                "key_rate_dv01": self.key_rate_dv01_text(), "curve_date": self.curve_date.isoformat(),
                "calibration_status": self.calibration_status, "currency": self.currency}


def valuation_snapshot_from_bond_curve(bond: DateAwareFixedCouponBond, curve: ZeroCurve, as_of_date: date | None = None,
                                       instrument_id: str = "BOND", curve_date: date | None = None,
                                       calibration_status: str = "NOT_RUN") -> ValuationSnapshot:
    """
    Prices a date-aware bond and stores the result as a daily valuation snapshot.
    If as_of_date is supplied, the bond's settlement date is replaced for this
    valuation without mutating the original bond template.
    """
    valuation_date = as_of_date or bond.settlement_date
    valuation_bond = replace(bond, settlement_date=valuation_date)
    clean_price = valuation_bond.clean_price_from_curve(curve)
    dirty_price = valuation_bond.dirty_price_from_curve(curve)
    accrued_interest = valuation_bond.accrued_interest()
    parallel_dv01 = valuation_bond.curve_dv01(curve)
    key_rates = {row["key_maturity"]: row["key_rate_dv01"] for row in key_rate_dv01_rows(bond=valuation_bond, curve=curve)}
    return ValuationSnapshot(as_of_date=valuation_date, instrument_id=instrument_id, clean_price=clean_price, dirty_price=dirty_price,
                             accrued_interest=accrued_interest, parallel_dv01=parallel_dv01, key_rate_dv01=key_rates,
                             curve_date=curve_date or valuation_date, calibration_status=calibration_status, currency="USD")


def export_valuation_snapshots(snapshots: list[ValuationSnapshot], output_path: str | Path) -> Path:
    return export_rows_to_csv((snapshot.row() for snapshot in snapshots), output_path)


def curve_zero_rate_moves_bps(start_curve: ZeroCurve, end_curve: ZeroCurve) -> dict[float, float]:
    """
    Measures zero-rate moves at the start curve's maturities in basis points.
    Matching on start-curve pillars lets yesterday's key-rate DV01 explain
    today's realized curve move.
    """
    moves: dict[float, float] = {}
    for maturity, start_rate in zip(start_curve.maturities, start_curve.zero_rates):
        end_rate = end_curve.interpolate_rate(target_maturity=maturity)
        moves[maturity] = (end_rate - start_rate) * 10000
    return moves


def parallel_curve_move_bps(start_curve: ZeroCurve, end_curve: ZeroCurve) -> float:
    moves = curve_zero_rate_moves_bps(start_curve=start_curve, end_curve=end_curve)
    if not moves:
        raise ValueError("At least one curve move is required.")
    return sum(moves.values()) / len(moves)


def _key_lookup(values: dict[float, float], target_maturity: float) -> float:
    for maturity, value in values.items():
        if math.isclose(maturity, target_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            return value
    return 0.0


@dataclass
class PnLExplainRow:
    """
    One next-day P&L explain row.
    Actual P&L is compared with two first-order risk explanations: one using a
    single parallel DV01 and one using node-by-node key-rate DV01.
    """
    start_date: str
    end_date: str
    instrument_id: str
    price_t: float
    price_t_plus_1: float
    actual_pnl: float
    parallel_dv01: float
    parallel_curve_move_bps: float
    estimated_pnl_parallel: float
    estimated_pnl_key_rate: float
    unexplained_pnl_parallel: float
    unexplained_pnl_key_rate: float
    accrued_interest_t: float
    accrued_interest_t_plus_1: float
    accrued_interest_change: float
    which_model_explained_better: str

    def row(self) -> dict[str, float | str]:
        return self.__dict__.copy()


def explain_price_move(start_snapshot: ValuationSnapshot, end_snapshot: ValuationSnapshot, start_curve: ZeroCurve, end_curve: ZeroCurve) -> PnLExplainRow:
    """
    Explains dirty-price movement using yesterday's rate risk and today's curve move.
    DV01 is defined as price gain for a 1 bp rate decrease, so a positive rate
    move produces estimated P&L of -DV01 * move_in_bps.
    """
    if start_snapshot.instrument_id != end_snapshot.instrument_id:
        raise ValueError("Snapshots must refer to the same instrument_id.")
    key_moves = curve_zero_rate_moves_bps(start_curve=start_curve, end_curve=end_curve)
    parallel_move = sum(key_moves.values()) / len(key_moves)
    actual_pnl = end_snapshot.dirty_price - start_snapshot.dirty_price
    estimated_parallel = -start_snapshot.parallel_dv01 * parallel_move
    estimated_key_rate = -sum(_key_lookup(start_snapshot.key_rate_dv01, maturity) * move_bps for maturity, move_bps in key_moves.items())
    unexplained_parallel = actual_pnl - estimated_parallel
    unexplained_key_rate = actual_pnl - estimated_key_rate
    better_model = "key_rate" if abs(unexplained_key_rate) < abs(unexplained_parallel) else "parallel"
    return PnLExplainRow(start_date=start_snapshot.as_of_date.isoformat(), end_date=end_snapshot.as_of_date.isoformat(), instrument_id=start_snapshot.instrument_id,
                         price_t=start_snapshot.dirty_price, price_t_plus_1=end_snapshot.dirty_price, actual_pnl=actual_pnl,
                         parallel_dv01=start_snapshot.parallel_dv01, parallel_curve_move_bps=parallel_move, estimated_pnl_parallel=estimated_parallel,
                         estimated_pnl_key_rate=estimated_key_rate, unexplained_pnl_parallel=unexplained_parallel,
                         unexplained_pnl_key_rate=unexplained_key_rate, accrued_interest_t=start_snapshot.accrued_interest,
                         accrued_interest_t_plus_1=end_snapshot.accrued_interest, accrued_interest_change=end_snapshot.accrued_interest - start_snapshot.accrued_interest,
                         which_model_explained_better=better_model)


def export_pnl_explain_rows(rows: list[PnLExplainRow], output_path: str | Path) -> Path:
    return export_rows_to_csv((row.row() for row in rows), output_path)
