"""
Treasury instrument definitions for industry-style curve inputs.

FRED CMT data is useful for a first live curve, but institutional Treasury
curves are normally built from actual instruments. This module introduces
separate bill, note, and bond objects so the project can evolve from fitted CMT
quotes toward instrument-by-instrument bootstrapping.
"""

import math
from dataclasses import dataclass, field
from datetime import date

from bond_pricing import DateAwareFixedCouponBond, dirty_price_from_clean
from int_rate_convention import (
    BusinessDayConvention,
    DayCountConvention,
    DateGenerationRule,
    validate_compounding_frequency,
    validate_rate,
    year_fraction,
)
from market_calendar import MarketCalendar, US_GOVERNMENT_SECURITIES


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
