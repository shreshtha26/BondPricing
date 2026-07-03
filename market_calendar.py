"""
Market calendar support for settlement and payment-date handling.
The first version of the project used simple weekend logic. This module makes
calendar handling explicit and reusable: instruments and workflows can ask a
market calendar whether a date is a business day, roll dates, and advance by a
settlement lag.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
import csv
from int_rate_convention import BusinessDayConvention, adjust_business_day, is_business_day


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
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
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
    """
    Approximate US government securities market holidays for V1.
    This covers the core full-day holidays needed for a first version. Early
    closes and emergency market closures still require a vendor or exchange
    calendar.
    """
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
        _observed_fixed_holiday(year, 12, 25)}
    if year >= 2021:
        holidays.add(_observed_fixed_holiday(year, 6, 19))
    return holidays


def new_york_bank_holidays(year: int) -> set[date]:
    """
    Simple New York banking calendar for first-version settlement examples.
    """
    return us_government_securities_holidays(year)


def load_holiday_dates_from_csv(path: str | Path, date_column: str = "date") -> set[date]:
    """
    Loads full-day holiday dates from a vendor or firm-maintained CSV file.
    Production calendars change because of one-off closures, national days of
    mourning, emergency closures, and market-specific decisions. Keeping this
    loader in the calendar layer lets analytics use maintained holiday files
    without changing pricing or bootstrapping code.
    """
    holiday_dates: set[date] = set()
    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if date_column not in (reader.fieldnames or []):
            raise ValueError(f"Holiday CSV must contain '{date_column}' column.")
        for row_number, row in enumerate(reader, start=2):
            value = row.get(date_column, "").strip()
            if value == "":
                continue
            try:
                holiday_dates.add(date.fromisoformat(value))
            except ValueError as error:
                raise ValueError(f"Invalid holiday date on row {row_number}: {value}.") from error
    return holiday_dates


@dataclass
class MarketCalendar:
    """
    Reusable business-day calendar for a market or settlement center.
    """
    name: str
    holiday_provider: Callable[[int], set[date]]
    extra_holidays: set[date] = field(default_factory=set)

    def holidays(self, start_year: int, end_year: int) -> set[date]:
        """
        Returns all full-day holidays in a year range.
        The rule-based provider covers recurring holidays. extra_holidays lets
        users add vendor-maintained exceptions without editing source code.
        """
        holiday_set: set[date] = set(self.extra_holidays)
        for year in range(start_year, end_year + 1):
            holiday_set.update(self.holiday_provider(year))
        return holiday_set

    def with_extra_holidays(self, extra_holidays: set[date], name: str | None = None) -> "MarketCalendar":
        """
        Returns a copy of this calendar with additional full-day closures.
        This is the production-friendly extension point: keep the standard
        recurring holiday rules, then overlay vendor or internal exception
        dates for actual trading/settlement use.
        """
        return MarketCalendar(name=name or self.name, holiday_provider=self.holiday_provider, extra_holidays=self.extra_holidays | extra_holidays)

    def with_extra_holidays_from_csv(self, path: str | Path, date_column: str = "date", name: str | None = None) -> "MarketCalendar":
        """
        Returns a calendar copy enriched with holidays loaded from CSV.
        """
        return self.with_extra_holidays(extra_holidays=load_holiday_dates_from_csv(path=path, date_column=date_column), name=name)

    def is_holiday(self, input_date: date) -> bool:
        """
        Identifies whether a date is a full-day market holiday.
        """
        return input_date in self.holidays(input_date.year - 1, input_date.year + 1)

    def is_business_day(self, input_date: date) -> bool:
        """
        Returns True when the date is neither weekend nor full-day holiday.
        """
        holiday_set = self.holidays(input_date.year - 1, input_date.year + 1)
        return is_business_day(input_date, holiday_set)

    def adjust(self,input_date: date, convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING) -> date:
        """
        Rolls a date according to this market's business-day convention.
        """
        holiday_set = self.holidays(input_date.year - 1, input_date.year + 1)
        return adjust_business_day(input_date=input_date, convention=convention, holidays=holiday_set)

    def advance_business_days(self, input_date: date, business_days: int) -> date:
        """
        Advances a date by a number of business days in this market.
        """
        if business_days < 0:
            raise ValueError("business_days cannot be negative.")
        current_date = input_date
        days_advanced = 0
        while days_advanced < business_days:
            current_date += timedelta(days=1)
            if self.is_business_day(current_date):
                days_advanced += 1
        return current_date

    def settlement_date(self,trade_date: date, settlement_lag_days: int, convention: BusinessDayConvention | str = BusinessDayConvention.FOLLOWING) -> date:
        """
        Calculates settlement date from trade date and settlement lag.
        """
        unadjusted_settlement = self.advance_business_days(input_date=trade_date, business_days=settlement_lag_days)
        return self.adjust(unadjusted_settlement, convention)


US_GOVERNMENT_SECURITIES = MarketCalendar(name="US Government Securities", holiday_provider=us_government_securities_holidays)
NEW_YORK_BANK = MarketCalendar(name="New York Bank", holiday_provider=new_york_bank_holidays)
US_FEDERAL_RESERVE_BANK = MarketCalendar(name="US Federal Reserve Bank", holiday_provider=new_york_bank_holidays)
