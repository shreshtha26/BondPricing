"""
Interest-rate, date, and cashflow timing conventions.
This module is the foundation under curve construction and bond pricing. Fixed
income analytics are highly sensitive to how dates are rolled, how year
fractions are measured, and how rates compound. The rest of the project uses
these helpers so bootstrapping, accrued interest, discounting, and present value
calculations share the same convention logic.
"""

import math
from datetime import date, timedelta
from enum import Enum


BASIS_POINT = 0.0001


class DayCountConvention(str, Enum):
    ACT_360 = "ACT/360"
    ACT_364 = "ACT/364"
    ACT_365_FIXED = "ACT/365F"
    ACT_365_25 = "ACT/365.25"
    ACT_365L = "ACT/365L"
    ACT_ACT_ISDA = "ACT/ACT ISDA"
    ACT_ACT_ICMA = "ACT/ACT ICMA"
    ACT_ACT_AFB = "ACT/ACT AFB"
    NL_365 = "NL/365"
    BUSINESS_252 = "BUS/252"
    THIRTY_360_US = "30/360 US"
    THIRTY_360_BOND_BASIS = "30/360 BOND BASIS"
    THIRTY_E_360 = "30E/360"
    THIRTY_E_360_ISDA = "30E/360 ISDA"
    THIRTY_360_ITALIAN = "30/360 ITALIAN"
    ONE_ONE = "1/1"


class BusinessDayConvention(str, Enum):
    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED FOLLOWING"
    PRECEDING = "PRECEDING"
    MODIFIED_PRECEDING = "MODIFIED PRECEDING"
    HALF_MONTH_MODIFIED_FOLLOWING = "HALF-MONTH MODIFIED FOLLOWING"
    NEAREST = "NEAREST"


class CompoundingConvention(str, Enum):
    SIMPLE = "SIMPLE"
    COMPOUNDED = "COMPOUNDED"
    CONTINUOUS = "CONTINUOUS"
    SIMPLE_THEN_COMPOUNDED = "SIMPLE THEN COMPOUNDED"


class DateGenerationRule(str, Enum):
    FORWARD = "FORWARD"
    BACKWARD = "BACKWARD"


DAY_COUNT_ALIASES = {
    "ACT/360": DayCountConvention.ACT_360,
    "A/360": DayCountConvention.ACT_360,
    "ACTUAL/360": DayCountConvention.ACT_360,
    "MONEY MARKET": DayCountConvention.ACT_360,
    "ACT/364": DayCountConvention.ACT_364,
    "ACTUAL/364": DayCountConvention.ACT_364,
    "ACT/365": DayCountConvention.ACT_365_FIXED,
    "ACT/365F": DayCountConvention.ACT_365_FIXED,
    "ACTUAL/365 FIXED": DayCountConvention.ACT_365_FIXED,
    "ACTUAL/365F": DayCountConvention.ACT_365_FIXED,
    "ENGLISH": DayCountConvention.ACT_365_FIXED,
    "ACT/365.25": DayCountConvention.ACT_365_25,
    "ACT/365L": DayCountConvention.ACT_365L,
    "ACTUAL/365L": DayCountConvention.ACT_365L,
    "ISMA-YEAR": DayCountConvention.ACT_365L,
    "ACT/ACT": DayCountConvention.ACT_ACT_ISDA,
    "ACT/ACT ISDA": DayCountConvention.ACT_ACT_ISDA,
    "ACTUAL/ACTUAL ISDA": DayCountConvention.ACT_ACT_ISDA,
    "ACT/ACT ICMA": DayCountConvention.ACT_ACT_ICMA,
    "ACT/ACT ISMA": DayCountConvention.ACT_ACT_ICMA,
    "ACTUAL/ACTUAL ICMA": DayCountConvention.ACT_ACT_ICMA,
    "ACT/ACT AFB": DayCountConvention.ACT_ACT_AFB,
    "ACTUAL/ACTUAL AFB": DayCountConvention.ACT_ACT_AFB,
    "NL/365": DayCountConvention.NL_365,
    "NO LEAP/365": DayCountConvention.NL_365,
    "BUS/252": DayCountConvention.BUSINESS_252,
    "BUSINESS/252": DayCountConvention.BUSINESS_252,
    "30/360": DayCountConvention.THIRTY_360_US,
    "30/360 US": DayCountConvention.THIRTY_360_US,
    "30U/360": DayCountConvention.THIRTY_360_US,
    "30/360 BOND BASIS": DayCountConvention.THIRTY_360_BOND_BASIS,
    "30A/360": DayCountConvention.THIRTY_360_BOND_BASIS,
    "30E/360": DayCountConvention.THIRTY_E_360,
    "30/360 ICMA": DayCountConvention.THIRTY_E_360,
    "EUROBOND BASIS": DayCountConvention.THIRTY_E_360,
    "30E/360 ISDA": DayCountConvention.THIRTY_E_360_ISDA,
    "GERMAN": DayCountConvention.THIRTY_E_360_ISDA,
    "30/360 ITALIAN": DayCountConvention.THIRTY_360_ITALIAN,
    "ITALIAN": DayCountConvention.THIRTY_360_ITALIAN,
    "1/1": DayCountConvention.ONE_ONE,
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
        raise ValueError(
            "Discrete-compounded rate is invalid because "
            "1 + rate / compounding_frequency must be positive."
        )


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


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_year(year: int) -> int:
    return 366 if is_leap_year(year) else 365


def last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    return (next_month - timedelta(days=1)).day


def is_end_of_month(input_date: date) -> bool:
    return input_date.day == last_day_of_month(input_date.year, input_date.month)


def is_last_day_of_february(input_date: date) -> bool:
    return input_date.month == 2 and is_end_of_month(input_date)


def add_months(input_date: date, months: int, end_of_month: bool = False) -> date:
    month_index = input_date.year * 12 + input_date.month - 1 + months
    target_year = month_index // 12
    target_month = month_index % 12 + 1
    target_last_day = last_day_of_month(target_year, target_month)

    if end_of_month and is_end_of_month(input_date):
        target_day = target_last_day
    else:
        target_day = min(input_date.day, target_last_day)

    return date(target_year, target_month, target_day)


def actual_days(start_date: date, end_date: date) -> int:
    _validate_date_range(start_date, end_date)

    return (end_date - start_date).days


def _contains_feb_29(
    start_date: date,
    end_date: date,
    include_start: bool,
    include_end: bool,
) -> bool:
    for year in range(start_date.year, end_date.year + 1):
        if not is_leap_year(year):
            continue

        feb_29 = date(year, 2, 29)
        starts_before = start_date < feb_29 or (include_start and start_date == feb_29)
        ends_after = feb_29 < end_date or (include_end and feb_29 == end_date)

        if starts_before and ends_after:
            return True

    return False


def _count_feb_29_days(start_date: date, end_date: date) -> int:
    return sum(
        1
        for year in range(start_date.year, end_date.year + 1)
        if is_leap_year(year) and start_date <= date(year, 2, 29) < end_date
    )


def is_business_day(input_date: date, holidays: set[date] | None = None) -> bool:
    holiday_set = holidays or set()

    return input_date.weekday() < 5 and input_date not in holiday_set


def business_days_between(
    start_date: date,
    end_date: date,
    holidays: set[date] | None = None,
) -> int:
    """
    Counts business days from start_date inclusive to end_date exclusive.
    """

    _validate_date_range(start_date, end_date)

    count = 0
    current_date = start_date

    while current_date < end_date:
        if is_business_day(current_date, holidays):
            count += 1

        current_date += timedelta(days=1)

    return count


def adjust_business_day(
    input_date: date,
    convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING,
    holidays: set[date] | None = None,
) -> date:
    """
    Applies a business-day rolling convention.
    """

    convention = _normalize_business_day_convention(convention)

    if convention == BusinessDayConvention.UNADJUSTED or is_business_day(input_date, holidays):
        return input_date

    def following() -> date:
        adjusted_date = input_date

        while not is_business_day(adjusted_date, holidays):
            adjusted_date += timedelta(days=1)

        return adjusted_date

    def preceding() -> date:
        adjusted_date = input_date

        while not is_business_day(adjusted_date, holidays):
            adjusted_date -= timedelta(days=1)

        return adjusted_date

    if convention == BusinessDayConvention.FOLLOWING:
        return following()

    if convention == BusinessDayConvention.PRECEDING:
        return preceding()

    if convention == BusinessDayConvention.MODIFIED_FOLLOWING:
        adjusted = following()

        if adjusted.month != input_date.month:
            return preceding()

        return adjusted

    if convention == BusinessDayConvention.MODIFIED_PRECEDING:
        adjusted = preceding()

        if adjusted.month != input_date.month:
            return following()

        return adjusted

    if convention == BusinessDayConvention.HALF_MONTH_MODIFIED_FOLLOWING:
        adjusted = following()

        if adjusted.month != input_date.month or (input_date.day <= 15 < adjusted.day):
            return preceding()

        return adjusted

    if convention == BusinessDayConvention.NEAREST:
        offset = 1

        while True:
            forward_date = input_date + timedelta(days=offset)
            backward_date = input_date - timedelta(days=offset)

            if is_business_day(forward_date, holidays):
                return forward_date

            if is_business_day(backward_date, holidays):
                return backward_date

            offset += 1

    raise ValueError(f"Unsupported business-day convention: {convention}.")


def generate_coupon_schedule(
    start_date: date,
    maturity_date: date,
    frequency: int = 2,
    business_day_convention: BusinessDayConvention | str = (
        BusinessDayConvention.UNADJUSTED
    ),
    date_generation_rule: DateGenerationRule | str = DateGenerationRule.BACKWARD,
    end_of_month: bool = False,
    holidays: set[date] | None = None,
) -> list[date]:
    """
    Generates a simple coupon schedule including start and maturity dates.

    For production bond systems, keep both unadjusted accrual dates and adjusted
    payment dates. This helper returns adjusted dates only.
    """

    _validate_date_range(start_date, maturity_date)
    validate_compounding_frequency(frequency)

    if 12 % frequency != 0:
        raise ValueError("Coupon schedule frequency must divide 12.")

    rule = _normalize_date_generation_rule(date_generation_rule)
    months_per_period = 12 // frequency

    if rule == DateGenerationRule.FORWARD:
        unadjusted_dates = [start_date]
        current_date = start_date

        while current_date < maturity_date:
            current_date = add_months(current_date, months_per_period, end_of_month=end_of_month)

            if current_date >= maturity_date:
                unadjusted_dates.append(maturity_date)
                break

            unadjusted_dates.append(current_date)
    else:
        unadjusted_dates = [maturity_date]
        current_date = maturity_date

        while current_date > start_date:
            current_date = add_months(current_date, -months_per_period, end_of_month=end_of_month)

            if current_date <= start_date:
                unadjusted_dates.append(start_date)
                break

            unadjusted_dates.append(current_date)

        unadjusted_dates.reverse()

    return [
        adjust_business_day(
            input_date=unadjusted_date,
            convention=business_day_convention,
            holidays=holidays,
        )
        for unadjusted_date in unadjusted_dates
    ]


def _day_count_30_360(
    start_date: date,
    end_date: date,
    convention: DayCountConvention,
    maturity_date: date | None = None,
) -> int:
    y1, m1, d1 = start_date.year, start_date.month, start_date.day
    y2, m2, d2 = end_date.year, end_date.month, end_date.day

    if convention == DayCountConvention.THIRTY_360_US:
        start_is_last_feb = is_last_day_of_february(start_date)
        end_is_last_feb = is_last_day_of_february(end_date)

        if start_is_last_feb and end_is_last_feb:
            d2 = 30

        if start_is_last_feb:
            d1 = 30

        if d2 == 31 and d1 >= 30:
            d2 = 30

        if d1 == 31:
            d1 = 30

    elif convention == DayCountConvention.THIRTY_360_BOND_BASIS:
        d1 = min(d1, 30)

        if d1 >= 30:
            d2 = min(d2, 30)

    elif convention == DayCountConvention.THIRTY_E_360:
        d1 = min(d1, 30)
        d2 = min(d2, 30)

    elif convention == DayCountConvention.THIRTY_E_360_ISDA:
        if is_end_of_month(start_date):
            d1 = 30

        is_feb_maturity = (
            maturity_date is not None
            and end_date == maturity_date
            and end_date.month == 2
        )

        if is_end_of_month(end_date) and not is_feb_maturity:
            d2 = 30

    elif convention == DayCountConvention.THIRTY_360_ITALIAN:
        if m1 == 2 and d1 > 27:
            d1 = 30
        elif d1 == 31:
            d1 = 30

        if m2 == 2 and d2 > 27:
            d2 = 30
        elif d2 == 31:
            d2 = 30

    else:
        raise ValueError(f"Unsupported 30/360 convention: {convention}.")

    return 360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)


def year_fraction(
    start_date: date,
    end_date: date,
    convention: DayCountConvention | str = DayCountConvention.ACT_365_FIXED,
    frequency: int | None = None,
    coupon_start_date: date | None = None,
    coupon_end_date: date | None = None,
    maturity_date: date | None = None,
    holidays: set[date] | None = None,
) -> float:
    """
    Calculates the year fraction between two dates under a day-count convention.
    """

    _validate_date_range(start_date, end_date)
    convention = _normalize_day_count(convention)
    days = actual_days(start_date, end_date)

    if days == 0:
        return 0.0

    if convention == DayCountConvention.ACT_360:
        return days / 360

    if convention == DayCountConvention.ACT_364:
        return days / 364

    if convention == DayCountConvention.ACT_365_FIXED:
        return days / 365

    if convention == DayCountConvention.ACT_365_25:
        return days / 365.25

    if convention == DayCountConvention.ACT_365L:
        if frequency is None:
            raise ValueError("frequency is required for ACT/365L.")

        validate_compounding_frequency(frequency)

        if frequency == 1:
            denominator = 366 if _contains_feb_29(
                start_date,
                end_date,
                include_start=False,
                include_end=True,
            ) else 365
        else:
            denominator = days_in_year(end_date.year)

        return days / denominator

    if convention == DayCountConvention.ACT_ACT_ISDA:
        total = 0.0
        current_date = start_date

        while current_date < end_date:
            next_year = date(current_date.year + 1, 1, 1)
            period_end = min(end_date, next_year)
            total += (period_end - current_date).days / days_in_year(current_date.year)
            current_date = period_end

        return total

    if convention == DayCountConvention.ACT_ACT_ICMA:
        if frequency is None or coupon_start_date is None or coupon_end_date is None:
            raise ValueError(
                "frequency, coupon_start_date, and coupon_end_date are required "
                "for ACT/ACT ICMA."
            )

        validate_compounding_frequency(frequency)
        _validate_date_range(coupon_start_date, coupon_end_date)

        coupon_days = actual_days(coupon_start_date, coupon_end_date)

        if coupon_days == 0:
            raise ValueError("ACT/ACT ICMA coupon period cannot be zero days.")

        return days / (frequency * coupon_days)

    if convention == DayCountConvention.ACT_ACT_AFB:
        whole_years = 0
        current_end = end_date

        while True:
            previous_anniversary = add_months(current_end, -12)

            if previous_anniversary < start_date:
                break

            whole_years += 1
            current_end = previous_anniversary

        denominator = 366 if _contains_feb_29(
            start_date,
            current_end,
            include_start=True,
            include_end=False,
        ) else 365

        return whole_years + actual_days(start_date, current_end) / denominator

    if convention == DayCountConvention.NL_365:
        return (days - _count_feb_29_days(start_date, end_date)) / 365

    if convention == DayCountConvention.BUSINESS_252:
        return business_days_between(start_date, end_date, holidays) / 252

    if convention in {
        DayCountConvention.THIRTY_360_US,
        DayCountConvention.THIRTY_360_BOND_BASIS,
        DayCountConvention.THIRTY_E_360,
        DayCountConvention.THIRTY_E_360_ISDA,
        DayCountConvention.THIRTY_360_ITALIAN,
    }:
        return _day_count_30_360(
            start_date=start_date,
            end_date=end_date,
            convention=convention,
            maturity_date=maturity_date,
        ) / 360

    if convention == DayCountConvention.ONE_ONE:
        return 1.0

    raise ValueError(f"Unsupported day-count convention: {convention}.")


def effective_annual_rate(quoted_rate: float, compounding_frequency: int) -> float:
    """
    Converts a quoted annual discrete-compounded rate into an effective annual rate.
    """

    validate_discrete_rate(quoted_rate, compounding_frequency)

    return (1 + quoted_rate / compounding_frequency) ** compounding_frequency - 1


def discount_factor_discrete(
    rate: float,
    time_years: float,
    compounding_frequency: int,
) -> float:
    """
    Calculates a discount factor using discrete compounding.

    DF = 1 / (1 + r / m) ** (m * T)
    """

    validate_discrete_rate(rate, compounding_frequency)
    validate_time_years(time_years)

    return 1 / ((1 + rate / compounding_frequency) ** (compounding_frequency * time_years))


def discount_factor_continuous(rate: float, time_years: float) -> float:
    """
    Calculates a discount factor using continuous compounding.

    DF = exp(-r * T)
    """

    validate_rate(rate)
    validate_time_years(time_years)

    return math.exp(-rate * time_years)


def discount_factor_from_rate(
    rate: float,
    time_years: float,
    compounding: CompoundingConvention | str = CompoundingConvention.COMPOUNDED,
    compounding_frequency: int = 1,
) -> float:
    """
    Calculates a discount factor using a named compounding convention.
    """

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
        return discount_factor_discrete(
            rate=rate,
            time_years=time_years,
            compounding_frequency=compounding_frequency,
        )

    if compounding == CompoundingConvention.CONTINUOUS:
        return discount_factor_continuous(rate, time_years)

    if compounding == CompoundingConvention.SIMPLE_THEN_COMPOUNDED:
        validate_compounding_frequency(compounding_frequency)

        if time_years <= 1 / compounding_frequency:
            return discount_factor_from_rate(
                rate=rate,
                time_years=time_years,
                compounding=CompoundingConvention.SIMPLE,
                compounding_frequency=compounding_frequency,
            )

        return discount_factor_discrete(
            rate=rate,
            time_years=time_years,
            compounding_frequency=compounding_frequency,
        )

    raise ValueError(f"Unsupported compounding convention: {compounding}.")


def continuous_rate_from_discount_factor(
    discount_factor: float,
    time_years: float,
) -> float:
    """
    Converts a discount factor into a continuously compounded zero rate.
    """

    if not math.isfinite(discount_factor):
        raise ValueError("discount_factor must be finite.")

    if discount_factor <= 0:
        raise ValueError("discount_factor must be positive.")

    validate_time_years(time_years)

    if time_years == 0:
        raise ValueError("time_years must be positive for rate conversion.")

    return -math.log(discount_factor) / time_years


def rate_from_discount_factor(
    discount_factor: float,
    time_years: float,
    compounding: CompoundingConvention | str = CompoundingConvention.COMPOUNDED,
    compounding_frequency: int = 1,
) -> float:
    """
    Converts a discount factor into a rate under a named compounding convention.
    """

    compounding = _normalize_compounding(compounding)

    if not math.isfinite(discount_factor):
        raise ValueError("discount_factor must be finite.")

    if discount_factor <= 0:
        raise ValueError("discount_factor must be positive.")

    validate_time_years(time_years)

    if time_years == 0:
        raise ValueError("time_years must be positive for rate conversion.")

    if compounding == CompoundingConvention.SIMPLE:
        return (1 / discount_factor - 1) / time_years

    if compounding == CompoundingConvention.COMPOUNDED:
        validate_compounding_frequency(compounding_frequency)

        return compounding_frequency * (
            discount_factor ** (-1 / (compounding_frequency * time_years)) - 1
        )

    if compounding == CompoundingConvention.CONTINUOUS:
        return continuous_rate_from_discount_factor(discount_factor, time_years)

    if compounding == CompoundingConvention.SIMPLE_THEN_COMPOUNDED:
        validate_compounding_frequency(compounding_frequency)

        if time_years <= 1 / compounding_frequency:
            return rate_from_discount_factor(
                discount_factor=discount_factor,
                time_years=time_years,
                compounding=CompoundingConvention.SIMPLE,
                compounding_frequency=compounding_frequency,
            )

        return rate_from_discount_factor(
            discount_factor=discount_factor,
            time_years=time_years,
            compounding=CompoundingConvention.COMPOUNDED,
            compounding_frequency=compounding_frequency,
        )

    raise ValueError(f"Unsupported compounding convention: {compounding}.")


def convert_discrete_to_continuous(
    discrete_rate: float,
    compounding_frequency: int,
) -> float:
    """
    Converts a discrete-compounded quoted rate into the equivalent continuous rate.
    """

    validate_discrete_rate(discrete_rate, compounding_frequency)

    return compounding_frequency * math.log(1 + discrete_rate / compounding_frequency)


def convert_continuous_to_discrete(
    continuous_rate: float,
    compounding_frequency: int,
) -> float:
    """
    Converts a continuously compounded rate into an equivalent discrete quote.
    """

    validate_rate(continuous_rate, "continuous_rate")
    validate_compounding_frequency(compounding_frequency)

    return compounding_frequency * (math.exp(continuous_rate / compounding_frequency) - 1)


def present_value_discrete(
    cashflow: float,
    rate: float,
    time_years: float,
    compounding_frequency: int,
) -> float:
    """
    Present value of a future cashflow using discrete compounding.
    """

    if not math.isfinite(cashflow):
        raise ValueError("cashflow must be finite.")

    return cashflow * discount_factor_discrete(
        rate=rate,
        time_years=time_years,
        compounding_frequency=compounding_frequency,
    )


def present_value_continuous(
    cashflow: float,
    rate: float,
    time_years: float,
) -> float:
    """
    Present value of a future cashflow using continuous compounding.
    """

    if not math.isfinite(cashflow):
        raise ValueError("cashflow must be finite.")

    return cashflow * discount_factor_continuous(rate=rate, time_years=time_years)


if __name__ == "__main__":
    quoted_rate = 0.10
    time_years = 1.0

    print("Interest Rate Convention Examples")
    print("-" * 45)
    print("Quoted annual rate:", f"{quoted_rate:.2%}")
    print()

    for frequency in [1, 2, 4, 12, 360, 365]:
        ear = effective_annual_rate(quoted_rate, frequency)
        df = discount_factor_discrete(quoted_rate, time_years, frequency)

        print(f"Compounding frequency: {frequency}")
        print(f"Effective annual rate: {ear:.4%}")
        print(f"Discount factor for 1 year: {df:.6f}")
        print()

    continuous_rate = convert_discrete_to_continuous(
        discrete_rate=quoted_rate,
        compounding_frequency=2,
    )

    print("Semiannual 10% converted to continuous:")
    print(f"Continuous rate: {continuous_rate:.4%}")

    back_to_semiannual = convert_continuous_to_discrete(
        continuous_rate=continuous_rate,
        compounding_frequency=2,
    )

    print("Converted back to semiannual quoted rate:")
    print(f"Semiannual quoted rate: {back_to_semiannual:.4%}")

    print()
    print("Continuous discount factor for 1 year:")
    print(discount_factor_continuous(continuous_rate, 1.0))

    print()
    print("Present Value Example")
    print("-" * 45)

    future_cashflow = 100.0
    pv_discrete = present_value_discrete(
        cashflow=future_cashflow,
        rate=quoted_rate,
        time_years=time_years,
        compounding_frequency=2,
    )

    pv_continuous = present_value_continuous(
        cashflow=future_cashflow,
        rate=continuous_rate,
        time_years=time_years,
    )

    print(f"Future cashflow: {future_cashflow}")
    print(f"Quoted rate: {quoted_rate:.2%}")
    print(f"Time: {time_years} year")
    print("Frequency: 2")
    print(f"PV using discrete compounding: {pv_discrete:.4f}")
    print(f"Equivalent continuous rate: {continuous_rate:.4%}")
    print(f"PV using continuous compounding: {pv_continuous:.4f}")
