"""
Compact pricing API, pricing types, and bond pricing engines.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from config import CurveConfig, ModelConfig, PricingConfig
from conventions import (BASIS_POINT, BusinessDayConvention, CompoundingConvention, DayCountConvention, DateGenerationRule,
                         discount_factor_from_rate, generate_coupon_schedule, validate_compounding_frequency, validate_discrete_rate, validate_rate, year_fraction)
from curves import ZeroCurve, coupon_accrual_periods, coupon_payment_times, solve_with_expanding_bracket


def _validate_positive(value: float, name: str) -> None:
    """
    Shared input guard for prices, notionals, and maturities.
    Negative or non-finite values can make risk and pricing formulas look valid
    while producing nonsense results, so validation is centralized here.
    """
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    if value <= 0:
        raise ValueError(f"{name} must be positive.")


def accrued_interest(face_value: float, coupon_rate: float, accrual_start_date: date, settlement_date: date,
                     next_coupon_date: date, day_count: DayCountConvention | str = DayCountConvention.ACT_ACT_ICMA, frequency: int = 2) -> float:
    """
    Calculates accrued interest from the previous coupon date to settlement.
    Accrued interest connects date conventions to market bond quoting. Traders
    usually quote clean prices, but settlement uses dirty price, so this helper
    is necessary for moving between quoted and cash-settled values.
    """
    _validate_positive(face_value, "face_value")
    validate_rate(coupon_rate, "coupon_rate")
    validate_compounding_frequency(frequency)
    if coupon_rate < 0:
        raise ValueError("coupon_rate cannot be negative.")
    if not (accrual_start_date <= settlement_date <= next_coupon_date):
        raise ValueError("settlement_date must be between accrual_start_date and next_coupon_date.")
    accrual_fraction = year_fraction(start_date=accrual_start_date, end_date=settlement_date, convention=day_count,
                                     frequency=frequency, coupon_start_date=accrual_start_date, coupon_end_date=next_coupon_date)
    return face_value * coupon_rate * accrual_fraction


def dirty_price_from_clean(clean_price: float, accrued: float) -> float:
    """
    Converts a quoted clean price into a settlement dirty price.
    This is a small but important market convention: clean price excludes
    accrued interest, while dirty price includes it.
    """
    if not math.isfinite(clean_price) or not math.isfinite(accrued):
        raise ValueError("clean_price and accrued must be finite.")
    return clean_price + accrued


def clean_price_from_dirty(dirty_price: float, accrued: float) -> float:
    """
    Converts a settlement dirty price back into a quoted clean price.
    This lets curve-based present values be reported in the same clean-price
    language used by bond markets.
    """
    if not math.isfinite(dirty_price) or not math.isfinite(accrued):
        raise ValueError("dirty_price and accrued must be finite.")
    return dirty_price - accrued


def simple_fixed_coupon_cashflow_schedule(face_value: float, coupon_rate: float, maturity_years: float,
                                          frequency: int = 2) -> list[tuple[float, float]]:
    """
    Builds a year-fraction cashflow schedule for compact examples.
    Date-aware production-style pricing uses DateAwareFixedCouponBond; this
    helper keeps the small educational wrappers without a second bond class.
    """
    _validate_positive(face_value, "face_value")
    _validate_positive(maturity_years, "maturity_years")
    validate_compounding_frequency(frequency)
    validate_rate(coupon_rate, "coupon_rate")
    if coupon_rate < 0:
        raise ValueError("coupon_rate cannot be negative.")
    payment_times = coupon_payment_times(maturity=maturity_years, frequency=frequency)
    accrual_periods = coupon_accrual_periods(maturity=maturity_years, frequency=frequency)
    cashflows = [face_value * coupon_rate * accrual_period for accrual_period in accrual_periods]
    cashflows[-1] += face_value
    return list(zip(payment_times, cashflows))


@dataclass
class DateAwareFixedCouponBond:
    """
    Date-aware fixed-coupon bond for the live-curve workflow.
    This class connects market conventions to pricing. It uses real dates for
    coupon schedules, accrued interest, clean/dirty price, and curve-based
    valuation from the live FRED Treasury ZeroCurve.
    """
    face_value: float
    coupon_rate: float
    issue_date: date
    maturity_date: date
    settlement_date: date
    frequency: int = 2
    day_count: DayCountConvention | str = DayCountConvention.ACT_ACT_ICMA
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.UNADJUSTED
    date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD
    end_of_month: bool = False
    holidays: set[date] | None = None

    def __post_init__(self) -> None:
        """
        Validates real bond dates and economics.
        Date-aware pricing depends on settlement being inside the bond life and
        on coupon/notional inputs being economically meaningful.
        """
        _validate_positive(self.face_value, "face_value")
        validate_rate(self.coupon_rate, "coupon_rate")
        validate_compounding_frequency(self.frequency)
        if self.coupon_rate < 0:
            raise ValueError("coupon_rate cannot be negative.")
        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must be before maturity_date.")
        if not (self.issue_date <= self.settlement_date < self.maturity_date):
            raise ValueError("settlement_date must be on or after issue_date and before maturity_date.")

    def coupon_dates(self) -> list[date]:
        """
        Generates adjusted coupon dates for the bond.
        This is where calendar conventions enter instrument pricing. The output
        dates drive accrued interest and future cashflow generation.
        """
        return generate_coupon_schedule(start_date=self.issue_date, maturity_date=self.maturity_date, frequency=self.frequency,
                                        business_day_convention=self.business_day_convention, date_generation_rule=self.date_generation_rule, end_of_month=self.end_of_month, holidays=self.holidays)

    def surrounding_coupon_dates(self) -> tuple[date, date]:
        """
        Finds the coupon period containing the settlement date.
        Accrued interest is measured from the previous coupon date to settlement
        relative to the next coupon date, so this period is required for
        clean/dirty price conversion.
        """
        schedule = self.coupon_dates()
        previous_coupon_date = schedule[0]
        for next_coupon_date in schedule[1:]:
            if previous_coupon_date <= self.settlement_date <= next_coupon_date:
                return previous_coupon_date, next_coupon_date
            previous_coupon_date = next_coupon_date
        raise ValueError("Could not locate settlement date in coupon schedule.")

    def accrued_interest(self) -> float:
        """
        Calculates accrued interest for this bond at settlement.
        This converts the generic accrued_interest() helper into an instrument
        method using the bond's own schedule and day-count convention.
        """
        previous_coupon_date, next_coupon_date = self.surrounding_coupon_dates()
        return accrued_interest(face_value=self.face_value, coupon_rate=self.coupon_rate, accrual_start_date=previous_coupon_date,
                                settlement_date=self.settlement_date, next_coupon_date=next_coupon_date, day_count=self.day_count, frequency=self.frequency)

    def future_cashflow_schedule(self) -> list[tuple[date, float, float]]:
        """
        Returns future cashflows as (payment_date, time_from_settlement, amount).
        This is the date-aware equivalent of cashflow_schedule(). It turns the
        bond contract into dated amounts and year fractions that a ZeroCurve can
        discount.
        """
        schedule = self.coupon_dates()
        future_cashflows: list[tuple[date, float, float]] = []
        for previous_coupon_date, payment_date in zip(schedule[:-1], schedule[1:]):
            if payment_date <= self.settlement_date:
                continue
            accrual_fraction = year_fraction(start_date=previous_coupon_date, end_date=payment_date, convention=self.day_count,
                                             frequency=self.frequency, coupon_start_date=previous_coupon_date, coupon_end_date=payment_date)
            amount = self.face_value * self.coupon_rate * accrual_fraction
            if payment_date == schedule[-1]:
                amount += self.face_value
            time_from_settlement = year_fraction(start_date=self.settlement_date, end_date=payment_date, convention=self.discount_day_count, frequency=self.frequency)
            future_cashflows.append((payment_date, time_from_settlement, amount))
        return future_cashflows

    def dirty_price_from_curve(self, curve: ZeroCurve, allow_curve_extrapolation: bool = True) -> float:
        """
        Prices future cashflows from a ZeroCurve and returns dirty price.
        Dirty price is the true present value of remaining cashflows. The
        extrapolation flag is explicit because short first cashflows may occur
        before the first quoted curve tenor.
        """
        cashflows = [(time_from_settlement, amount) for _, time_from_settlement, amount in self.future_cashflow_schedule()]
        return curve.price_cashflows(cashflows, allow_extrapolation=allow_curve_extrapolation)

    def clean_price_from_curve(self, curve: ZeroCurve, allow_curve_extrapolation: bool = True) -> float:
        """
        Prices from the curve and subtracts accrued interest to report clean price.
        Clean price is the market quote format, so this method makes curve-based
        valuation comparable with quoted bond prices.
        """
        return clean_price_from_dirty(dirty_price=self.dirty_price_from_curve(curve=curve, allow_curve_extrapolation=allow_curve_extrapolation), accrued=self.accrued_interest())

    def dirty_price_from_yield(self, yield_rate: float, compounding: CompoundingConvention | str = CompoundingConvention.COMPOUNDED) -> float:
        """
        Prices future cashflows from a single quoted yield.
        This gives the date-aware bond a traditional YTM-style valuation path,
        which is useful for comparing against curve-based pricing.
        """
        validate_rate(yield_rate, "yield_rate")
        dirty_price = 0.0
        for _, time_from_settlement, amount in self.future_cashflow_schedule():
            dirty_price += amount * discount_factor_from_rate(rate=yield_rate, time_years=time_from_settlement, compounding=compounding, compounding_frequency=self.frequency)
        return dirty_price

    def clean_price_from_yield(self, yield_rate: float, compounding: CompoundingConvention | str = CompoundingConvention.COMPOUNDED) -> float:
        """
        Converts the flat-yield dirty price into a clean price.
        This mirrors market quoting conventions for the yield-based pricing
        route.
        """
        return clean_price_from_dirty(dirty_price=self.dirty_price_from_yield(yield_rate=yield_rate, compounding=compounding), accrued=self.accrued_interest())

    def curve_dv01(self, curve: ZeroCurve, bump_size: float = BASIS_POINT, allow_curve_extrapolation: bool = True) -> float:
        """
        Calculates curve DV01 for the date-aware bond.
        This is the live-curve risk measure used in main.py: the whole zero
        curve is shifted up and down by one basis point, then the bond is
        repriced.
        """
        validate_rate(bump_size, "bump_size")
        if bump_size <= 0:
            raise ValueError("bump_size must be positive.")
        price_down = self.dirty_price_from_curve(curve.bumped(-bump_size), allow_curve_extrapolation=allow_curve_extrapolation)
        price_up = self.dirty_price_from_curve(curve.bumped(bump_size), allow_curve_extrapolation=allow_curve_extrapolation)
        return (price_down - price_up) / 2


def price_from_yield(face_value: float, coupon_rate: float, maturity_years: float, yield_rate: float, frequency: int = 2) -> float:
    """
    Convenience wrapper for flat-YTM pricing of a simple fixed-coupon bond.
    This is kept as a compact educational wrapper; date-aware pricing should use
    DateAwareFixedCouponBond or price(...).
    """
    validate_discrete_rate(yield_rate, frequency)
    base = 1 + yield_rate / frequency
    return sum(cashflow / (base ** (frequency * payment_time))
               for payment_time, cashflow in simple_fixed_coupon_cashflow_schedule(face_value, coupon_rate, maturity_years, frequency))


def price_from_zero_curve(face_value: float, coupon_rate: float, maturity_years: float, curve: ZeroCurve, frequency: int = 2) -> float:
    """
    Convenience wrapper for pricing a simple bond from a ZeroCurve.
    This keeps the project examples concise when demonstrating curve-based
    pricing.
    """
    return curve.price_cashflows(simple_fixed_coupon_cashflow_schedule(face_value, coupon_rate, maturity_years, frequency))


def solve_ytm(market_price: float, face_value: float, coupon_rate: float, maturity_years: float, frequency: int = 2) -> float:
    """
    Finds the flat quoted yield that matches a market price.
    Solving YTM is the inverse of flat-yield pricing. It answers: what single
    yield would reproduce this observed bond price?
    """
    _validate_positive(market_price, "market_price")
    validate_compounding_frequency(frequency)
    def objective(yield_guess: float) -> float:
        theoretical_price = price_from_yield(face_value=face_value, coupon_rate=coupon_rate, maturity_years=maturity_years, yield_rate=yield_guess, frequency=frequency)
        return theoretical_price - market_price
    return solve_with_expanding_bracket(objective, lower=-frequency + 1e-10, upper=0.25, max_abs_bound=10.0,
                                        failure_message="Could not bracket a yield-to-maturity solution.",
                                        expand_lower=False, expand_upper=True)


@dataclass(frozen=True)
class InstrumentSpec:
    """
    Bond terms needed by the first pricing workflow.

    Later layers can add callable, floating-rate, inflation-linked, credit, and
    securitized fields without changing the public price(...) shape.
    """
    instrument_id: str
    instrument_type: str
    face_value: float
    issue_date: date
    maturity_date: date
    coupon_rate: float | None = None
    frequency: int = 2
    currency: str = "USD"
    issue_price: float | None = None
    day_count: DayCountConvention | str = DayCountConvention.ACT_ACT_ICMA
    discount_day_count: DayCountConvention | str = DayCountConvention.ACT_365_FIXED
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.UNADJUSTED
    date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD
    end_of_month: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id is required.")
        if not self.instrument_type.strip():
            raise ValueError("instrument_type is required.")
        if not math.isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError("face_value must be positive and finite.")
        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must be before maturity_date.")
        if self.coupon_rate is not None and not math.isfinite(self.coupon_rate):
            raise ValueError("coupon_rate must be finite when provided.")
        if self.coupon_rate is not None and self.coupon_rate < 0:
            raise ValueError("coupon_rate cannot be negative.")
        if self.frequency <= 0:
            raise ValueError("frequency must be positive.")
        if not self.currency.strip():
            raise ValueError("currency is required.")
        if self.issue_price is not None and (not math.isfinite(self.issue_price) or self.issue_price <= 0):
            raise ValueError("issue_price must be positive and finite when provided.")
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True)
class MarketState:
    """
    Market inputs available at one valuation date.

    The first engine requires a discount curve. Projection curves, spread
    curves, volatility surfaces, repo curves, and richer quote metadata can be
    attached as later product layers.
    """
    valuation_date: date
    discount_curve: ZeroCurve
    curve_date: date | None = None
    quote_source: str = "unknown"
    quote_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not self.quote_source.strip():
            raise ValueError("quote_source is required.")



@dataclass(frozen=True)
class PricingContext:
    """
    Curve, pricing, and model settings for one valuation run.

    Feature switches live here so price(...) can stay compact as the engine
    grows.
    """
    curve: CurveConfig = field(default_factory=CurveConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    model: ModelConfig = field(default_factory=ModelConfig)


@dataclass(frozen=True)
class PricingResult:
    """
    Standard pricing output returned by price(...).

    Diagnostics hold the curve, model, spread, adjustment, and data-quality
    details needed to explain a residual.
    """
    instrument_id: str
    valuation_date: date
    clean_price: float
    dirty_price: float
    accrued_interest: float
    currency: str = "USD"
    diagnostics: dict[str, float | str | bool | None] = field(default_factory=dict)


def run_pricing_pipeline(instrument: InstrumentSpec, market: MarketState, context: PricingContext) -> PricingResult:
    if instrument.instrument_type == "fixed_coupon_bond":
        return _price_fixed_coupon_bond(instrument=instrument, market=market, context=context)
    if instrument.instrument_type == "zero_coupon_bond":
        return _price_zero_coupon_bond(instrument=instrument, market=market, context=context)
    raise NotImplementedError(f"Unsupported instrument type: {instrument.instrument_type}, pricing is not implemented yet")


def _price_fixed_coupon_bond(instrument: InstrumentSpec, market: MarketState, context: PricingContext) -> PricingResult:
    if instrument.coupon_rate is None:
        raise ValueError("coupon_rate is required for fixed_coupon_bond.")

    bond = DateAwareFixedCouponBond(
        face_value=instrument.face_value,
        coupon_rate=instrument.coupon_rate,
        issue_date=instrument.issue_date,
        maturity_date=instrument.maturity_date,
        settlement_date=market.valuation_date,
        frequency=instrument.frequency,
        day_count=instrument.day_count,
        discount_day_count=instrument.discount_day_count,
        business_day_convention=instrument.business_day_convention,
        date_generation_rule=instrument.date_generation_rule,
        end_of_month=instrument.end_of_month)

    dirty_price = bond.dirty_price_from_curve(market.discount_curve)
    accrued_interest = bond.accrued_interest()
    clean_price = dirty_price - accrued_interest

    return PricingResult(
        instrument_id=instrument.instrument_id,
        valuation_date=market.valuation_date,
        clean_price=clean_price,
        dirty_price=dirty_price,
        accrued_interest=accrued_interest,
        currency=instrument.currency,
        diagnostics={"instrument_type": instrument.instrument_type,
            "price_type": context.pricing.price_type,
            "quote_source": market.quote_source,
            "curve_date": market.curve_date.isoformat() if market.curve_date else None})


def _price_zero_coupon_bond(instrument: InstrumentSpec, market: MarketState, context: PricingContext) -> PricingResult:
    if not (instrument.issue_date <= market.valuation_date < instrument.maturity_date):
        raise ValueError("valuation_date must be on or after issue_date and before maturity_date.")

    time_to_maturity = year_fraction(start_date=market.valuation_date, end_date=instrument.maturity_date, convention=instrument.discount_day_count)
    dirty_price = instrument.face_value * market.discount_curve.discount_factor(maturity=time_to_maturity, allow_extrapolation=True)
    accrued_interest = _zero_coupon_accrued_interest(instrument=instrument, market=market)
    clean_price = dirty_price - accrued_interest

    return PricingResult(
        instrument_id=instrument.instrument_id,
        valuation_date=market.valuation_date,
        clean_price=clean_price,
        dirty_price=dirty_price,
        accrued_interest=accrued_interest,
        currency=instrument.currency,
        diagnostics={"instrument_type": instrument.instrument_type,
            "price_type": context.pricing.price_type,
            "quote_source": market.quote_source,
            "curve_date": market.curve_date.isoformat() if market.curve_date else None,
            "time_to_maturity": time_to_maturity,
            "accrual_method": "constant_yield_accretion" if instrument.issue_price is not None else "none"})


def _zero_coupon_accrued_interest(instrument: InstrumentSpec, market: MarketState) -> float:
    if instrument.issue_price is None:
        return 0.0

    total_life = year_fraction(start_date=instrument.issue_date, end_date=instrument.maturity_date, convention=instrument.discount_day_count)
    elapsed = year_fraction(start_date=instrument.issue_date, end_date=market.valuation_date, convention=instrument.discount_day_count)
    issue_yield = -math.log(instrument.issue_price / instrument.face_value) / total_life
    accreted_value = instrument.issue_price * math.exp(issue_yield * elapsed)
    return accreted_value - instrument.issue_price


def price(instrument: InstrumentSpec, market:MarketState, *, context:PricingContext|None=None) -> PricingResult:
    pricing_context = context or PricingContext()
    return run_pricing_pipeline(instrument=instrument, market=market, context=pricing_context)
