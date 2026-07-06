"""
Compact interest-rate conventions used by the current pricing stack.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Callable


BASIS_POINT = 0.0001


class DayCountConvention(str, Enum):
    ACT_360 = "ACT/360"
    ACT_365_FIXED = "ACT/365F"
    ACT_ACT_ICMA = "ACT/ACT ICMA"


class BusinessDayConvention(str, Enum):
    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED FOLLOWING"
    PRECEDING = "PRECEDING"


class CompoundingConvention(str, Enum):
    SIMPLE = "SIMPLE"
    COMPOUNDED = "COMPOUNDED"
    CONTINUOUS = "CONTINUOUS"


class DateGenerationRule(str, Enum):
    FORWARD = "FORWARD"
    BACKWARD = "BACKWARD"


DAY_COUNT_ALIASES = {
    "ACT/360": DayCountConvention.ACT_360,
    "A/360": DayCountConvention.ACT_360,
    "ACTUAL/360": DayCountConvention.ACT_360,
    "ACT/365": DayCountConvention.ACT_365_FIXED,
    "ACT/365F": DayCountConvention.ACT_365_FIXED,
    "ACTUAL/365": DayCountConvention.ACT_365_FIXED,
    "ACTUAL/365F": DayCountConvention.ACT_365_FIXED,
    "ACT/ACT ICMA": DayCountConvention.ACT_ACT_ICMA,
    "ACT/ACT ISMA": DayCountConvention.ACT_ACT_ICMA,
    "ACTUAL/ACTUAL ICMA": DayCountConvention.ACT_ACT_ICMA,
}


def validate_compounding_frequency(compounding_frequency: int) -> None:
    if not isinstance(compounding_frequency, int):
        raise TypeError("compounding_frequency must be an integer.")
    if compounding_frequency <= 0:
        raise ValueError("compounding_frequency must be positive.")


def validate_time_years(time_years: float) -> None:
    if not math.isfinite(time_years):
        raise ValueError("time_years must be finite.")
    if time_years < 0:
        raise ValueError("time_years cannot be negative.")


def validate_rate(rate: float, name: str = "rate") -> None:
    if not math.isfinite(rate):
        raise ValueError(f"{name} must be finite.")


def validate_discrete_rate(rate: float, compounding_frequency: int) -> None:
    validate_rate(rate)
    validate_compounding_frequency(compounding_frequency)
    if 1 + rate / compounding_frequency <= 0:
        raise ValueError("Discrete-compounded rate denominator must be positive.")


def _normalize_day_count(convention: DayCountConvention | str) -> DayCountConvention:
    if isinstance(convention, DayCountConvention):
        return convention
    normalized = convention.strip().upper()
    if normalized not in DAY_COUNT_ALIASES:
        raise ValueError(f"Unsupported day-count convention: {convention}.")
    return DAY_COUNT_ALIASES[normalized]


def _normalize_business_day_convention(convention: BusinessDayConvention | str) -> BusinessDayConvention:
    if isinstance(convention, BusinessDayConvention):
        return convention
    normalized = convention.strip().upper().replace("_", " ")
    for member in BusinessDayConvention:
        if normalized == member.value:
            return member
    raise ValueError(f"Unsupported business-day convention: {convention}.")


def _normalize_compounding(convention: CompoundingConvention | str) -> CompoundingConvention:
    if isinstance(convention, CompoundingConvention):
        return convention
    normalized = convention.strip().upper().replace("_", " ")
    for member in CompoundingConvention:
        if normalized == member.value:
            return member
    raise ValueError(f"Unsupported compounding convention: {convention}.")


def _normalize_date_generation_rule(rule: DateGenerationRule | str) -> DateGenerationRule:
    if isinstance(rule, DateGenerationRule):
        return rule
    normalized = rule.strip().upper().replace("_", " ")
    for member in DateGenerationRule:
        if normalized == member.value:
            return member
    raise ValueError(f"Unsupported date generation rule: {rule}.")


def _validate_date_range(start_date: date, end_date: date) -> None:
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        raise TypeError("start_date and end_date must be datetime.date objects.")
    if end_date < start_date:
        raise ValueError("end_date cannot be before start_date.")


def last_day_of_month(year: int, month: int) -> int:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def is_end_of_month(input_date: date) -> bool:
    return input_date.day == last_day_of_month(input_date.year, input_date.month)


def add_months(input_date: date, months: int, end_of_month: bool = False) -> date:
    month_index = input_date.year * 12 + input_date.month - 1 + months
    target_year = month_index // 12
    target_month = month_index % 12 + 1
    target_day = last_day_of_month(target_year, target_month) if end_of_month and is_end_of_month(input_date) else input_date.day
    return date(target_year, target_month, min(target_day, last_day_of_month(target_year, target_month)))


def actual_days(start_date: date, end_date: date) -> int:
    _validate_date_range(start_date, end_date)
    return (end_date - start_date).days


def is_business_day(input_date: date, holidays: set[date] | None = None) -> bool:
    return input_date.weekday() < 5 and input_date not in (holidays or set())


def adjust_business_day(input_date: date, convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING,
                        holidays: set[date] | None = None) -> date:
    convention = _normalize_business_day_convention(convention)
    if convention == BusinessDayConvention.UNADJUSTED or is_business_day(input_date, holidays):
        return input_date

    def following() -> date:
        adjusted = input_date
        while not is_business_day(adjusted, holidays):
            adjusted += timedelta(days=1)
        return adjusted

    def preceding() -> date:
        adjusted = input_date
        while not is_business_day(adjusted, holidays):
            adjusted -= timedelta(days=1)
        return adjusted

    if convention == BusinessDayConvention.FOLLOWING:
        return following()
    if convention == BusinessDayConvention.PRECEDING:
        return preceding()
    if convention == BusinessDayConvention.MODIFIED_FOLLOWING:
        adjusted = following()
        return preceding() if adjusted.month != input_date.month else adjusted
    raise ValueError(f"Unsupported business-day convention: {convention}.")


def generate_coupon_schedule(start_date: date, maturity_date: date, frequency: int = 2,
                             business_day_convention: BusinessDayConvention | str = BusinessDayConvention.UNADJUSTED,
                             date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD,
                             end_of_month: bool = False, holidays: set[date] | None = None) -> list[date]:
    _validate_date_range(start_date, maturity_date)
    validate_compounding_frequency(frequency)
    if 12 % frequency != 0:
        raise ValueError("Coupon schedule frequency must divide 12.")

    rule = _normalize_date_generation_rule(date_generation_rule)
    months_per_period = 12 // frequency

    if rule == DateGenerationRule.FORWARD:
        dates = [start_date]
        current = start_date
        while current < maturity_date:
            current = add_months(current, months_per_period, end_of_month=end_of_month)
            dates.append(maturity_date if current >= maturity_date else current)
    else:
        dates = [maturity_date]
        current = maturity_date
        while current > start_date:
            current = add_months(current, -months_per_period, end_of_month=end_of_month)
            dates.append(start_date if current <= start_date else current)
        dates.reverse()

    return [adjust_business_day(input_date=item, convention=business_day_convention, holidays=holidays) for item in dates]


def year_fraction(start_date: date, end_date: date,
                  convention: DayCountConvention | str = DayCountConvention.ACT_365_FIXED,
                  frequency: int | None = None, coupon_start_date: date | None = None,
                  coupon_end_date: date | None = None, maturity_date: date | None = None,
                  holidays: set[date] | None = None) -> float:
    _validate_date_range(start_date, end_date)
    convention = _normalize_day_count(convention)
    days = actual_days(start_date, end_date)
    if days == 0:
        return 0.0

    if convention == DayCountConvention.ACT_360:
        return days / 360
    if convention == DayCountConvention.ACT_365_FIXED:
        return days / 365
    if convention == DayCountConvention.ACT_ACT_ICMA:
        if frequency is None or coupon_start_date is None or coupon_end_date is None:
            raise ValueError("frequency, coupon_start_date, and coupon_end_date are required for ACT/ACT ICMA.")
        validate_compounding_frequency(frequency)
        _validate_date_range(coupon_start_date, coupon_end_date)
        coupon_days = actual_days(coupon_start_date, coupon_end_date)
        if coupon_days == 0:
            raise ValueError("ACT/ACT ICMA coupon period cannot be zero days.")
        return days / (frequency * coupon_days)

    raise ValueError(f"Unsupported day-count convention: {convention}.")


def discount_factor_discrete(rate: float, time_years: float, compounding_frequency: int) -> float:
    validate_discrete_rate(rate, compounding_frequency)
    validate_time_years(time_years)
    return 1 / ((1 + rate / compounding_frequency) ** (compounding_frequency * time_years))


def discount_factor_continuous(rate: float, time_years: float) -> float:
    validate_rate(rate)
    validate_time_years(time_years)
    return math.exp(-rate * time_years)


def discount_factor_from_rate(rate: float, time_years: float,
                              compounding: CompoundingConvention | str = CompoundingConvention.COMPOUNDED,
                              compounding_frequency: int = 1) -> float:
    compounding = _normalize_compounding(compounding)
    validate_rate(rate)
    validate_time_years(time_years)
    if time_years == 0:
        return 1.0
    if compounding == CompoundingConvention.SIMPLE:
        denominator = 1 + rate * time_years
        if denominator <= 0:
            raise ValueError("Simple-compounded discount denominator must be positive.")
        return 1 / denominator
    if compounding == CompoundingConvention.COMPOUNDED:
        return discount_factor_discrete(rate=rate, time_years=time_years, compounding_frequency=compounding_frequency)
    if compounding == CompoundingConvention.CONTINUOUS:
        return discount_factor_continuous(rate, time_years)
    raise ValueError(f"Unsupported compounding convention: {compounding}.")


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_government_securities_holidays(year: int) -> set[date]:
    holidays = {
        _observed_fixed_holiday(year, 1, 1),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _observed_fixed_holiday(year, 11, 11),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    if year >= 2021:
        holidays.add(_observed_fixed_holiday(year, 6, 19))
    return holidays


def new_york_bank_holidays(year: int) -> set[date]:
    return us_government_securities_holidays(year)


@dataclass
class MarketCalendar:
    name: str
    holiday_provider: Callable[[int], set[date]]
    extra_holidays: set[date] = field(default_factory=set)

    def holidays(self, start_year: int, end_year: int) -> set[date]:
        holiday_set = set(self.extra_holidays)
        for year in range(start_year, end_year + 1):
            holiday_set.update(self.holiday_provider(year))
        return holiday_set

    def is_business_day(self, input_date: date) -> bool:
        holiday_set = self.holidays(input_date.year - 1, input_date.year + 1)
        return is_business_day(input_date, holiday_set)

    def adjust(self, input_date: date, convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING) -> date:
        holiday_set = self.holidays(input_date.year - 1, input_date.year + 1)
        return adjust_business_day(input_date=input_date, convention=convention, holidays=holiday_set)

    def advance_business_days(self, input_date: date, business_days: int) -> date:
        if business_days < 0:
            raise ValueError("business_days cannot be negative.")
        current = input_date
        advanced = 0
        while advanced < business_days:
            current += timedelta(days=1)
            if self.is_business_day(current):
                advanced += 1
        return current

    def settlement_date(self, trade_date: date, settlement_lag_days: int,
                        convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING) -> date:
        unadjusted = self.advance_business_days(input_date=trade_date, business_days=settlement_lag_days)
        return self.adjust(unadjusted, convention)


US_GOVERNMENT_SECURITIES = MarketCalendar(name="US Government Securities", holiday_provider=us_government_securities_holidays)
NEW_YORK_BANK = MarketCalendar(name="New York Bank", holiday_provider=new_york_bank_holidays)
