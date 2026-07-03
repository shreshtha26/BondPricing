"""
Bond pricing utilities built on top of the zero-curve engine.
This module shows how the bootstrapped curve becomes useful for instruments.
It supports simple year-fraction examples for learning and date-aware fixed
coupon bonds for a more realistic workflow with accrued interest, clean/dirty
prices, and curve DV01.
"""

import math
from dataclasses import dataclass
from datetime import date
from scipy.optimize import brentq
from int_rate_convention import (BASIS_POINT, BusinessDayConvention, CompoundingConvention, DayCountConvention, DateGenerationRule,
                                 discount_factor_from_rate, generate_coupon_schedule, validate_compounding_frequency, validate_discrete_rate, validate_rate, year_fraction)
from yield_curve import ZeroCurve, coupon_accrual_periods, coupon_payment_times


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


@dataclass
class FixedCouponBond:
    """
    Year-fraction fixed-coupon bond used for compact analytics examples.
    This class is intentionally simpler than DateAwareFixedCouponBond. It keeps
    the early learning path easy: specify maturity in years, generate cashflows,
    solve YTM, and compare flat-yield pricing with curve-based pricing.
    """
    face_value: float
    coupon_rate: float
    maturity_years: float
    yield_rate: float | None = None
    frequency: int = 2

    def __post_init__(self) -> None:
        """
        Validates the simple bond inputs before any analytics are run.
        The pricing formulas assume positive face value/maturity and a valid
        discrete yield, so this prevents invalid examples from propagating.
        """
        _validate_positive(self.face_value, "face_value")
        _validate_positive(self.maturity_years, "maturity_years")
        validate_compounding_frequency(self.frequency)
        validate_rate(self.coupon_rate, "coupon_rate")
        if self.coupon_rate < 0:
            raise ValueError("coupon_rate cannot be negative.")
        if self.yield_rate is not None:
            validate_discrete_rate(self.yield_rate, self.frequency)

    def periods(self) -> int:
        """
        Returns the number of coupon periods in the simplified schedule.
        This is mainly a convenience method for examples and reporting.
        """
        return len(self.cashflow_times())

    def cashflow_times(self) -> list[float]:
        """
        Returns coupon payment times measured in years from today.
        These times are the bridge between the simple bond object and ZeroCurve,
        which discounts cashflows by year-fraction maturity.
        """
        return coupon_payment_times(maturity=self.maturity_years, frequency=self.frequency)

    def coupon_per_period(self) -> float:
        """
        Returns the regular coupon amount for a non-stub period.
        It is kept for readability in simple examples; cashflows() uses accrual
        periods so stubs remain consistent with the curve code.
        """
        return self.face_value * self.coupon_rate / self.frequency

    def yield_per_period(self) -> float:
        """
        Converts the annual quoted YTM into a per-period yield.
        Flat-yield analytics use this to discount every cashflow by the same
        yield, which contrasts with curve pricing where each date has its own
        discount factor.
        """
        if self.yield_rate is None:
            raise ValueError("yield_rate is required for yield-based analytics.")
        return self.yield_rate / self.frequency

    def cashflows(self) -> list[float]:
        """
        Generates coupon and principal cashflows for the simplified bond.
        This turns bond terms into the cashflow vector that both flat-yield and
        curve-based pricing consume.
        """
        accrual_periods = coupon_accrual_periods(maturity=self.maturity_years, frequency=self.frequency)
        flows = [self.face_value * self.coupon_rate * accrual_period for accrual_period in accrual_periods]
        flows[-1] += self.face_value
        return flows

    def cashflow_schedule(self) -> list[tuple[float, float]]:
        """
        Pairs each cashflow amount with its payment time in years.
        The schedule is the common input shape for ZeroCurve.price_cashflows().
        """
        return list(zip(self.cashflow_times(), self.cashflows()))

    def price(self) -> float:
        """
        Prices the bond from its flat quoted yield using discrete compounding.
        This reproduces traditional YTM pricing, where one yield discounts all
        cashflows. It is useful for comparison with the more realistic
        curve-based method.
        """
        if self.yield_rate is None:
            raise ValueError("yield_rate is required for yield-based pricing.")
        validate_discrete_rate(self.yield_rate, self.frequency)
        base = 1 + self.yield_rate / self.frequency
        return sum(cashflow / (base ** (self.frequency * payment_time)) for payment_time, cashflow in self.cashflow_schedule())

    def price_from_curve(self, curve: ZeroCurve) -> float:
        """
        Prices the bond by discounting each cashflow on a zero curve.
        This is the simplified example of the project's main valuation idea:
        once a zero curve exists, each cashflow receives its own market-implied
        discount factor.
        """
        return curve.price_cashflows(self.cashflow_schedule())

    def macaulay_duration(self) -> float:
        """
        Calculates Macaulay duration under the flat-yield assumption.
        Duration summarizes the weighted-average timing of cashflows and is the
        foundation for first-order interest-rate risk intuition.
        """
        price = self.price()
        if price <= 0:
            raise ValueError("Price must be positive to calculate duration.")
        y = self.yield_per_period()
        weighted_time = sum(payment_time * cashflow / ((1 + y) ** (self.frequency * payment_time)) for payment_time, cashflow in self.cashflow_schedule())
        return weighted_time / price

    def modified_duration(self) -> float:
        """
        Calculates modified duration from Macaulay duration.
        Modified duration links a small yield move to approximate price change,
        making it the flat-yield counterpart to DV01.
        """
        return self.macaulay_duration() / (1 + self.yield_per_period())

    def convexity(self) -> float:
        """
        Calculates flat-yield convexity.
        Convexity captures the curvature missed by duration and explains why
        bond price changes are not perfectly linear in yield changes.
        """
        price = self.price()
        if price <= 0:
            raise ValueError("Price must be positive to calculate convexity.")
        base = 1 + self.yield_per_period()
        convexity_sum = 0.0
        for payment_time, cashflow in self.cashflow_schedule():
            periods_to_payment = self.frequency * payment_time
            convexity_sum += cashflow * periods_to_payment * (periods_to_payment + 1) / (self.frequency ** 2) / (base ** (periods_to_payment + 2))
        return convexity_sum / price

    def dv01(self) -> float:
        """
        Approximate price gain for a 1 bp decrease in the flat yield.
        DV01 is the practical risk number used to describe how much money a
        bond gains or loses for a one-basis-point rate move.
        """
        return self.modified_duration() * self.price() * BASIS_POINT

    def curve_dv01(self, curve: ZeroCurve, bump_size: float = BASIS_POINT) -> float:
        """
        Central-difference DV01 from a parallel zero-curve bump.
        This connects the bond to the bootstrapped curve: instead of bumping a
        single YTM, it bumps every zero rate and reprices cashflows from the
        shifted curve.
        """
        validate_rate(bump_size, "bump_size")
        if bump_size <= 0:
            raise ValueError("bump_size must be positive.")
        price_down = self.price_from_curve(curve.bumped(-bump_size))
        price_up = self.price_from_curve(curve.bumped(bump_size))
        return (price_down - price_up) / 2


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
    Scripts can call this without manually constructing FixedCouponBond.
    """
    bond = FixedCouponBond(face_value=face_value, coupon_rate=coupon_rate, maturity_years=maturity_years, yield_rate=yield_rate, frequency=frequency)
    return bond.price()


def price_from_zero_curve(face_value: float, coupon_rate: float, maturity_years: float, curve: ZeroCurve, frequency: int = 2) -> float:
    """
    Convenience wrapper for pricing a simple bond from a ZeroCurve.
    This keeps the project examples concise when demonstrating curve-based
    pricing.
    """
    bond = FixedCouponBond(face_value=face_value, coupon_rate=coupon_rate, maturity_years=maturity_years, frequency=frequency)
    return bond.price_from_curve(curve)


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
    lower = -frequency + 1e-10
    upper = 0.25
    lower_value = objective(lower)
    upper_value = objective(upper)
    while lower_value * upper_value > 0:
        upper *= 2
        if upper > 10:
            raise ValueError("Could not bracket a yield-to-maturity solution.")
        upper_value = objective(upper)
    return brentq(objective, lower, upper)


if __name__ == "__main__":
    bond = FixedCouponBond(face_value=100, coupon_rate=0.05, maturity_years=5, yield_rate=0.045, frequency=2)
    print("Fixed Coupon Bond Example")
    print("-" * 35)
    print(f"Face Value:          {bond.face_value}")
    print(f"Coupon Rate:         {bond.coupon_rate:.2%}")
    print(f"Yield Rate:          {bond.yield_rate:.2%}")
    print(f"Maturity:            {bond.maturity_years} years")
    print(f"Coupon Frequency:    {bond.frequency} times per year")
    print()
    print("Cashflow schedule:")
    print(bond.cashflow_schedule())
    print()
    print(f"Bond Price:          {bond.price():.4f}")
    print(f"Macaulay Duration:   {bond.macaulay_duration():.4f} years")
    print(f"Modified Duration:   {bond.modified_duration():.4f}")
    print(f"Convexity:           {bond.convexity():.4f}")
    print(f"DV01:                {bond.dv01():.6f}")
    print()
    market_price = 102
    implied_ytm = solve_ytm(market_price=market_price, face_value=100, coupon_rate=0.05, maturity_years=5, frequency=2)
    print(f"If market price is {market_price}, implied YTM is: {implied_ytm:.4%}")
