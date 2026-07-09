"""
Calibration, validation, risk analytics, valuation snapshots, and P&L explain.
"""

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from conventions import BASIS_POINT, validate_rate
from curves import TOLERANCE, ZeroCurve
from market_data import BondMarketQuote, CurveMetadata, SecurityMasterRecord, TreasuryCurveSnapshot, export_rows_to_csv
from pricing import (DateAwareFixedCouponBond, MarketState, PricingContext, PricingResult, cashflow_curve_dv01,
                     cashflow_curve_risk,
                     dirty_price_from_clean, future_cashflows_from_instrument, price, solve_cashflow_yield,
                     solve_z_spread)


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
    curve_name: str
    curve_source: str
    curve_type: str
    curve_date: date | None
    curve_build_method: str
    curve_role: str
    discount_curve_used: str
    curve_calibration_status: str
    curve_calibration_residual_bp: float | None
    market_price: float
    model_price: float
    base_model_price: float
    spread_price_impact: float
    pricing_z_spread: float
    pricing_z_spread_bp: float
    clean_price: float
    dirty_price: float
    accrued_interest: float
    market_dirty_price: float
    market_implied_yield: float
    model_implied_yield: float
    yield_error_bp: float
    z_spread: float
    z_spread_bp: float
    parallel_dv01: float
    effective_duration: float
    effective_convexity: float
    residual_explanation: str
    quote_type: str
    quote_timestamp: datetime | None
    quote_age_days: float | None
    quote_stale: bool
    quote_evaluated: bool
    quote_traded: bool
    quote_override: bool
    clean_dirty_mismatch: bool
    data_quality_flags: str
    source_system: str | None
    source_record_id: str | None
    convention_level: str
    convention_warnings: str
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
            "curve_name": self.curve_name,
            "curve_source": self.curve_source,
            "curve_type": self.curve_type,
            "curve_date": self.curve_date.isoformat() if self.curve_date else None,
            "curve_build_method": self.curve_build_method,
            "curve_role": self.curve_role,
            "discount_curve_used": self.discount_curve_used,
            "curve_calibration_status": self.curve_calibration_status,
            "curve_calibration_residual_bp": self.curve_calibration_residual_bp,
            "market_price": self.market_price,
            "model_price": self.model_price,
            "base_model_price": self.base_model_price,
            "price_error": self.price_error,
            "price_error_bp_of_par": self.price_error_bp_of_par,
            "spread_price_impact": self.spread_price_impact,
            "pricing_z_spread": self.pricing_z_spread,
            "pricing_z_spread_bp": self.pricing_z_spread_bp,
            "market_dirty_price": self.market_dirty_price,
            "clean_price": self.clean_price,
            "dirty_price": self.dirty_price,
            "accrued_interest": self.accrued_interest,
            "market_implied_yield": self.market_implied_yield,
            "model_implied_yield": self.model_implied_yield,
            "yield_error_bp": self.yield_error_bp,
            "z_spread": self.z_spread,
            "z_spread_bp": self.z_spread_bp,
            "parallel_dv01": self.parallel_dv01,
            "effective_duration": self.effective_duration,
            "effective_convexity": self.effective_convexity,
            "bid": self.bid,
            "ask": self.ask,
            "inside_bid_ask": self.inside_bid_ask,
            "validation_status": self.validation_status,
            "residual_explanation": self.residual_explanation,
            "quote_type": self.quote_type,
            "quote_timestamp": self.quote_timestamp.isoformat() if self.quote_timestamp else None,
            "quote_age_days": self.quote_age_days,
            "quote_stale": self.quote_stale,
            "quote_evaluated": self.quote_evaluated,
            "quote_traded": self.quote_traded,
            "quote_override": self.quote_override,
            "clean_dirty_mismatch": self.clean_dirty_mismatch,
            "data_quality_flags": self.data_quality_flags,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "convention_level": self.convention_level,
            "convention_warnings": self.convention_warnings,
            "tolerance": self.tolerance,
            "currency": self.currency,
        }


def _market_dirty_price(quote: BondMarketQuote, accrued_interest: float) -> float:
    market_price = quote.effective_price()
    if quote.price_type == "dirty":
        return market_price
    return dirty_price_from_clean(clean_price=market_price, accrued=accrued_interest)


def _inside_bid_ask(model_price: float, bid: float | None, ask: float | None) -> bool | None:
    if bid is None or ask is None:
        return None
    return bid <= model_price <= ask


def _quote_as_of_datetime(quote: BondMarketQuote) -> datetime:
    return datetime.combine(quote.valuation_date, time(hour=23, minute=59, second=59))


def _quote_age_days(quote: BondMarketQuote) -> float | None:
    age_seconds = quote.age_seconds(_quote_as_of_datetime(quote))
    if age_seconds is None:
        return None
    return age_seconds / 86400


def _data_quality_flags(quote: BondMarketQuote, accrued_interest: float, tolerance: float,
                        max_quote_age_days: float | None) -> list[str]:
    flags: list[str] = []
    quote_age_days = _quote_age_days(quote)
    max_age_seconds = None if max_quote_age_days is None else max_quote_age_days * 86400
    if quote.timestamp is None:
        flags.append("MISSING_TIMESTAMP")
    elif quote_age_days is not None and quote_age_days < -1 / 24:
        flags.append("FUTURE_TIMESTAMP")
    if quote.bid is None or quote.ask is None:
        flags.append("MISSING_BID_ASK")
    if quote.observed_price is None:
        flags.append("MISSING_OBSERVED_PRICE")
    if quote.quote_source.strip().upper() == "UNKNOWN":
        flags.append("UNKNOWN_QUOTE_SOURCE")
    if quote.is_stale_as_of(_quote_as_of_datetime(quote), max_age_seconds):
        flags.append("STALE_QUOTE")
    if quote.override_flag:
        flags.append("OVERRIDE_QUOTE")
    if quote.is_evaluated:
        flags.append("EVALUATED_PRICE")
    if quote.is_traded:
        flags.append("TRADED_PRICE")
    if quote.clean_dirty_mismatch(accrued_interest=accrued_interest, tolerance=tolerance):
        flags.append("CLEAN_DIRTY_MISMATCH")
    return flags or ["OK"]


def _has_major_data_quality_issue(flags: list[str] | str) -> bool:
    if isinstance(flags, str):
        flag_set = set(flags.split(";"))
    else:
        flag_set = set(flags)
    return bool(flag_set & {"UNKNOWN_QUOTE_SOURCE", "STALE_QUOTE", "OVERRIDE_QUOTE", "CLEAN_DIRTY_MISMATCH", "FUTURE_TIMESTAMP"})


def _convention_warnings(security: SecurityMasterRecord) -> str:
    warnings = ["DESK_STYLE_CONVENTIONS", "NO_EX_COUPON_RULES", "NO_VENDOR_CERTIFIED_CALENDAR"]
    if security.instrument_type == "fixed_coupon_bond":
        warnings.append("NO_ODD_COUPON_SPECIAL_HANDLING")
    if security.instrument_type == "zero_coupon_bond":
        warnings.append("ZERO_ACCRUAL_USES_CONSTANT_YIELD_WHEN_ISSUE_PRICE_SUPPLIED")
    return ";".join(warnings)


def _residual_explanation(price_error: float, tolerance: float, inside_bid_ask: bool | None,
                          price_type: str, accrued_interest: float, z_spread: float,
                          pricing_z_spread: float,
                          data_quality_flags: list[str],
                          quote_source: str) -> str:
    if inside_bid_ask is True:
        return "PASS_BID_ASK"
    if abs(price_error) <= tolerance:
        return "PASS_TOLERANCE"
    if not quote_source.strip() or quote_source.strip().upper() == "UNKNOWN" or _has_major_data_quality_issue(data_quality_flags):
        return "POSSIBLE_DATA_QUALITY_ISSUE"
    if price_type == "clean" and abs(accrued_interest) > tolerance:
        accrual_gap = abs(abs(price_error) - abs(accrued_interest))
        if accrual_gap <= max(tolerance, 0.10 * abs(accrued_interest)):
            return "POSSIBLE_ACCRUAL_MISMATCH"
    if abs(pricing_z_spread) > 0:
        return "APPLIED_SPREAD_PRICING_EFFECT"
    if abs(z_spread) >= 5 * BASIS_POINT:
        return "POSSIBLE_SPREAD_OR_CREDIT_EFFECT"
    if price_error > 0:
        return "PRICE_ABOVE_MARKET"
    return "PRICE_BELOW_MARKET"


def validate_bond_quote(security: SecurityMasterRecord, quote: BondMarketQuote, curve: ZeroCurve,
                        context: PricingContext | None = None, tolerance: float = 0.02,
                        curve_metadata: CurveMetadata | None = None,
                        max_quote_age_days: float | None = 1.0) -> BondQuoteValidationRow:
    """
    Prices one security-master record and compares it with one observed quote.
    This is the compact first CUSIP/ISIN-level validation workflow.
    """
    if security.security_id != quote.security_id:
        raise ValueError("security and quote must have the same security_id.")
    if security.currency != quote.currency:
        raise ValueError("security and quote currencies must match.")
    metadata = curve_metadata or CurveMetadata.unknown()
    instrument = security.to_instrument_spec()
    market = MarketState(valuation_date=quote.valuation_date, discount_curve=curve, curve_date=metadata.curve_date,
                         quote_source=quote.quote_source, quote_timestamp=quote.timestamp)
    result: PricingResult = price(instrument, market, context=context)
    cashflows = future_cashflows_from_instrument(instrument=instrument, valuation_date=quote.valuation_date)
    market_price = quote.effective_price()
    market_dirty_price = _market_dirty_price(quote=quote, accrued_interest=result.accrued_interest)
    model_price = result.clean_price if quote.price_type == "clean" else result.dirty_price
    base_model_price = float(result.diagnostics["base_clean_price"] if quote.price_type == "clean" else result.diagnostics["base_dirty_price"])
    spread_price_impact = float(result.diagnostics["spread_price_impact"])
    pricing_z_spread = float(result.diagnostics["applied_z_spread"])
    market_implied_yield = solve_cashflow_yield(dirty_price=market_dirty_price, cashflows=cashflows, frequency=security.frequency)
    model_implied_yield = solve_cashflow_yield(dirty_price=result.dirty_price, cashflows=cashflows, frequency=security.frequency)
    yield_error_bp = (model_implied_yield - market_implied_yield) * 10000
    z_spread = solve_z_spread(dirty_price=market_dirty_price, cashflows=cashflows, curve=curve)
    risk_curve = curve.bumped(pricing_z_spread) if pricing_z_spread else curve
    curve_risk = cashflow_curve_risk(cashflows=cashflows, curve=risk_curve)
    data_quality_flags = _data_quality_flags(quote=quote, accrued_interest=result.accrued_interest,
                                             tolerance=tolerance, max_quote_age_days=max_quote_age_days)
    residual_explanation = _residual_explanation(price_error=model_price - market_price, tolerance=tolerance,
                                                 inside_bid_ask=_inside_bid_ask(model_price=model_price, bid=quote.bid, ask=quote.ask),
                                                 price_type=quote.price_type, accrued_interest=result.accrued_interest,
                                                 z_spread=z_spread, pricing_z_spread=pricing_z_spread,
                                                 data_quality_flags=data_quality_flags,
                                                 quote_source=quote.quote_source)
    return BondQuoteValidationRow(
        security_id=security.security_id,
        id_type=security.id_type,
        valuation_date=quote.valuation_date,
        instrument_type=security.instrument_type,
        quote_source=quote.quote_source,
        price_type=quote.price_type,
        curve_name=metadata.curve_name,
        curve_source=metadata.curve_source,
        curve_type=metadata.curve_type,
        curve_date=metadata.curve_date,
        curve_build_method=metadata.curve_build_method,
        curve_role=metadata.curve_role,
        discount_curve_used=metadata.discount_curve_used,
        curve_calibration_status=metadata.calibration_status,
        curve_calibration_residual_bp=metadata.calibration_residual_bp,
        market_price=market_price,
        model_price=model_price,
        base_model_price=base_model_price,
        spread_price_impact=spread_price_impact,
        pricing_z_spread=pricing_z_spread,
        pricing_z_spread_bp=pricing_z_spread * 10000,
        clean_price=result.clean_price,
        dirty_price=result.dirty_price,
        accrued_interest=result.accrued_interest,
        market_dirty_price=market_dirty_price,
        market_implied_yield=market_implied_yield,
        model_implied_yield=model_implied_yield,
        yield_error_bp=yield_error_bp,
        z_spread=z_spread,
        z_spread_bp=z_spread * 10000,
        parallel_dv01=curve_risk["parallel_dv01"],
        effective_duration=curve_risk["effective_duration"],
        effective_convexity=curve_risk["effective_convexity"],
        residual_explanation=residual_explanation,
        quote_type=quote.quote_type,
        quote_timestamp=quote.timestamp,
        quote_age_days=_quote_age_days(quote),
        quote_stale=quote.is_stale_as_of(_quote_as_of_datetime(quote), None if max_quote_age_days is None else max_quote_age_days * 86400),
        quote_evaluated=quote.is_evaluated,
        quote_traded=quote.is_traded,
        quote_override=quote.override_flag,
        clean_dirty_mismatch=quote.clean_dirty_mismatch(accrued_interest=result.accrued_interest, tolerance=tolerance),
        data_quality_flags=";".join(data_quality_flags),
        source_system=quote.source_system,
        source_record_id=quote.source_record_id,
        convention_level="DESK_APPROXIMATION",
        convention_warnings=_convention_warnings(security),
        bid=quote.bid,
        ask=quote.ask,
        tolerance=tolerance,
        currency=security.currency,
    )


def validate_bond_quotes(securities: list[SecurityMasterRecord], quotes: list[BondMarketQuote], curve: ZeroCurve,
                         context: PricingContext | None = None, tolerance: float = 0.02,
                         curve_metadata: CurveMetadata | None = None,
                         max_quote_age_days: float | None = 1.0) -> list[BondQuoteValidationRow]:
    security_by_id = {security.security_id: security for security in securities}
    rows: list[BondQuoteValidationRow] = []
    for quote in quotes:
        if quote.security_id not in security_by_id:
            raise KeyError(f"Missing security master record for {quote.security_id}.")
        rows.append(validate_bond_quote(security=security_by_id[quote.security_id], quote=quote, curve=curve,
                                        context=context, tolerance=tolerance, curve_metadata=curve_metadata,
                                        max_quote_age_days=max_quote_age_days))
    return rows


def export_bond_quote_validation_report(rows: list[BondQuoteValidationRow], output_path: str | Path) -> Path:
    return export_rows_to_csv((row.row() for row in rows), output_path)


def _single_or_multiple(values: list[str | None]) -> str | None:
    unique_values = sorted({value for value in values if value is not None})
    if not unique_values:
        return None
    if len(unique_values) == 1:
        return unique_values[0]
    return "MULTIPLE"


@dataclass
class BondQuoteValidationSummary:
    """
    File-level summary for a batch of bond quote validations.
    """
    valuation_date: str | None
    curve_name: str | None
    curve_date: str | None
    total_bonds: int
    passed: int
    failed: int
    pass_rate: float
    max_abs_price_error: float
    max_abs_yield_error_bp: float
    max_abs_z_spread_bp: float
    max_effective_duration: float
    max_effective_convexity: float
    total_parallel_dv01: float
    largest_residual_security_id: str | None
    data_quality_issue_count: int
    stale_quote_count: int
    evaluated_quote_count: int
    traded_quote_count: int
    clean_dirty_mismatch_count: int
    spread_or_credit_issue_count: int

    def row(self) -> dict[str, float | int | str | None]:
        return {
            "valuation_date": self.valuation_date,
            "curve_name": self.curve_name,
            "curve_date": self.curve_date,
            "total_bonds": self.total_bonds,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "max_abs_price_error": self.max_abs_price_error,
            "max_abs_yield_error_bp": self.max_abs_yield_error_bp,
            "max_abs_z_spread_bp": self.max_abs_z_spread_bp,
            "max_effective_duration": self.max_effective_duration,
            "max_effective_convexity": self.max_effective_convexity,
            "total_parallel_dv01": self.total_parallel_dv01,
            "largest_residual_security_id": self.largest_residual_security_id,
            "data_quality_issue_count": self.data_quality_issue_count,
            "stale_quote_count": self.stale_quote_count,
            "evaluated_quote_count": self.evaluated_quote_count,
            "traded_quote_count": self.traded_quote_count,
            "clean_dirty_mismatch_count": self.clean_dirty_mismatch_count,
            "spread_or_credit_issue_count": self.spread_or_credit_issue_count,
        }


def bond_quote_validation_summary(rows: list[BondQuoteValidationRow]) -> BondQuoteValidationSummary:
    if not rows:
        raise ValueError("At least one bond quote validation row is required.")
    passed = sum(1 for row in rows if row.validation_status != "FAIL")
    failed = len(rows) - passed
    largest_residual = max(rows, key=lambda row: abs(row.price_error))
    return BondQuoteValidationSummary(
        valuation_date=_single_or_multiple([row.valuation_date.isoformat() for row in rows]),
        curve_name=_single_or_multiple([row.curve_name for row in rows]),
        curve_date=_single_or_multiple([row.curve_date.isoformat() if row.curve_date else None for row in rows]),
        total_bonds=len(rows),
        passed=passed,
        failed=failed,
        pass_rate=passed / len(rows),
        max_abs_price_error=max(abs(row.price_error) for row in rows),
        max_abs_yield_error_bp=max(abs(row.yield_error_bp) for row in rows),
        max_abs_z_spread_bp=max(abs(row.z_spread_bp) for row in rows),
        max_effective_duration=max(row.effective_duration for row in rows),
        max_effective_convexity=max(row.effective_convexity for row in rows),
        total_parallel_dv01=sum(row.parallel_dv01 for row in rows),
        largest_residual_security_id=largest_residual.security_id,
        data_quality_issue_count=sum(1 for row in rows if row.residual_explanation == "POSSIBLE_DATA_QUALITY_ISSUE" or _has_major_data_quality_issue(row.data_quality_flags)),
        stale_quote_count=sum(1 for row in rows if row.quote_stale),
        evaluated_quote_count=sum(1 for row in rows if row.quote_evaluated),
        traded_quote_count=sum(1 for row in rows if row.quote_traded),
        clean_dirty_mismatch_count=sum(1 for row in rows if row.clean_dirty_mismatch),
        spread_or_credit_issue_count=sum(1 for row in rows if row.residual_explanation == "POSSIBLE_SPREAD_OR_CREDIT_EFFECT"),
    )


def bond_quote_validation_summary_rows(rows: list[BondQuoteValidationRow]) -> list[dict[str, float | int | str | None]]:
    return [bond_quote_validation_summary(rows).row()]


def export_bond_quote_validation_summary(rows: list[BondQuoteValidationRow], output_path: str | Path) -> Path:
    return export_rows_to_csv(bond_quote_validation_summary_rows(rows), output_path)


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
