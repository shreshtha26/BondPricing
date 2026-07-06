"""
Curve analytics, par-yield bootstrapping, and shared numerical methods.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from scipy.optimize import brentq
from conventions import BASIS_POINT, discount_factor_continuous, validate_compounding_frequency, validate_rate, validate_time_years


def _finite_objective_value(objective: Callable[[float], float], x: float, name: str) -> float:
    value = objective(x)
    if not math.isfinite(value):
        raise ValueError(f"{name} objective value must be finite.")
    return value


def solve_with_expanding_bracket(objective: Callable[[float], float], lower: float = -0.25, upper: float = 0.25,
                                 max_abs_bound: float = 5.0, failure_message: str = "Could not bracket a solution.",
                                 tolerance: float = 1e-14, max_iterations: int = 300,
                                 expand_lower: bool = True, expand_upper: bool = True) -> float:
    """
    Solves a one-dimensional equation by expanding the initial bracket.
    Curve bootstrapping repeatedly solves one unknown zero rate at a time. This
    helper keeps the failure behavior and stressed-rate support consistent
    across par-yield, Treasury-instrument, and SOFR/OIS curve builders.
    """
    if lower >= upper:
        raise ValueError("lower must be less than upper.")
    if not math.isfinite(max_abs_bound) or max_abs_bound <= 0:
        raise ValueError("max_abs_bound must be positive and finite.")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance must be positive and finite.")
    if not expand_lower and not expand_upper:
        raise ValueError("At least one bracket bound must be expandable.")
    lower_value = _finite_objective_value(objective=objective, x=lower, name="lower")
    upper_value = _finite_objective_value(objective=objective, x=upper, name="upper")
    if abs(lower_value) <= tolerance:
        return lower
    if abs(upper_value) <= tolerance:
        return upper
    while lower_value * upper_value > 0:
        if expand_lower:
            lower *= 2
        if expand_upper:
            upper *= 2
        if abs(lower) > max_abs_bound or abs(upper) > max_abs_bound:
            raise ValueError(failure_message)
        lower_value = _finite_objective_value(objective=objective, x=lower, name="lower")
        upper_value = _finite_objective_value(objective=objective, x=upper, name="upper")
        if abs(lower_value) <= tolerance:
            return lower
        if abs(upper_value) <= tolerance:
            return upper
    return brentq(objective, lower, upper, xtol=tolerance, maxiter=max_iterations)


def append_unique_curve_point(maturities: list[float], values: list[float], maturity: float, value: float,
                              duplicate_message: str = "Duplicate curve maturity.", tolerance: float = 1e-12) -> None:
    """
    Appends one curve node after rejecting duplicate maturities.
    Sequential bootstrapping assumes one solved equation per pillar. A duplicate
    maturity means the builder either needs fitting logic or an explicit quote
    selection rule, so this helper fails clearly instead of overwriting a node.
    """
    if len(maturities) != len(values):
        raise ValueError("Curve maturity and value arrays must have the same length.")
    if not math.isfinite(maturity) or not math.isfinite(value):
        raise ValueError("Curve maturity and value must be finite.")
    for existing_maturity in maturities:
        if math.isclose(maturity, existing_maturity, rel_tol=0.0, abs_tol=tolerance):
            raise ValueError(duplicate_message)
    maturities.append(maturity)
    values.append(value)


TOLERANCE = 1e-12


def interpolate_curve_value(target_time: float, times: list[float], values: list[float], allow_left_extrapolation: bool = False,
                            allow_right_extrapolation: bool = False, empty_error: str = "At least one curve point is required for interpolation.",
                            left_error: str = "Interpolation target is outside the curve range.", right_error: str = "Interpolation target is outside the curve range.",
                            single_point_error: str = "A one-point curve cannot interpolate.") -> float:
    """
    Linearly interpolates a curve value from sorted or unsorted curve points.
    This shared helper removes duplicate interpolation code across Treasury,
    SOFR/OIS, and ZeroCurve workflows. Centralizing it matters because every
    curve-based price, forward rate, and risk measure depends on consistent
    treatment of intermediate cashflow dates.
    """
    if len(times) != len(values):
        raise ValueError("Interpolation inputs must have the same length.")
    points = sorted(zip(times, values))
    if not points:
        raise ValueError(empty_error)
    sorted_times = [point[0] for point in points]
    sorted_values = [point[1] for point in points]
    if target_time < sorted_times[0] - TOLERANCE:
        if allow_left_extrapolation:
            return sorted_values[0]
        raise ValueError(left_error)
    if target_time > sorted_times[-1] + TOLERANCE:
        if allow_right_extrapolation:
            return sorted_values[-1]
        raise ValueError(right_error)
    for time, value in points:
        if math.isclose(target_time, time, rel_tol=0.0, abs_tol=TOLERANCE):
            return value
    if len(points) == 1:
        raise ValueError(single_point_error)
    for index in range(len(points) - 1):
        left_time = sorted_times[index]
        right_time = sorted_times[index + 1]
        if left_time - TOLERANCE <= target_time <= right_time + TOLERANCE:
            left_value = sorted_values[index]
            right_value = sorted_values[index + 1]
            if math.isclose(left_time, right_time, rel_tol=0.0, abs_tol=TOLERANCE):
                return left_value
            weight = (target_time - left_time) / (right_time - left_time)
            return left_value + weight * (right_value - left_value)
    return sorted_values[-1]


def partial_curve_discount_factor(time_years: float, solved_maturities: list[float], solved_zero_rates: list[float],
                                  candidate_maturity: float, candidate_zero_rate: float,
                                  allow_left_extrapolation: bool = False, allow_right_extrapolation: bool = False,
                                  empty_error: str = "At least one curve point is required for interpolation.",
                                  left_error: str = "Interpolation target is outside the curve range.",
                                  right_error: str = "Interpolation target is outside the curve range.",
                                  single_point_error: str = "A one-point curve cannot interpolate.") -> float:
    """
    Discounts a cashflow using solved curve nodes plus one candidate node.
    Bootstraps for par yields, Treasury instruments, and OIS all solve one new
    zero rate at a time. This helper keeps that temporary-curve logic in one
    place.
    """
    validate_time_years(time_years)
    if time_years == 0:
        return 1.0
    zero_rate = interpolate_curve_value(target_time=time_years, times=solved_maturities + [candidate_maturity],
                                        values=solved_zero_rates + [candidate_zero_rate],
                                        allow_left_extrapolation=allow_left_extrapolation,
                                        allow_right_extrapolation=allow_right_extrapolation,
                                        empty_error=empty_error, left_error=left_error,
                                        right_error=right_error, single_point_error=single_point_error)
    return discount_factor_continuous(zero_rate, time_years)


def coupon_payment_times(maturity: float, frequency: int = 2) -> list[float]:
    """
    Returns coupon payment times in years, including maturity.

    This helper supports the year-fraction bond examples and bootstrapping
    routine. It gives the curve code a simple cashflow grid before the project
    moves into full date-aware schedules in bond_pricing.py.

    This is a year-fraction schedule, not a calendar/date schedule. For real
    bond settlement, clean/dirty price, accrued interest, holidays, and day
    count conventions should be handled by a date-aware schedule engine.
    """
    validate_time_years(maturity)
    validate_compounding_frequency(frequency)
    if maturity <= 0:
        raise ValueError("maturity must be positive.")
    period = 1 / frequency
    number_of_regular_periods = int(math.floor(maturity * frequency + TOLERANCE))
    payment_times = [i * period for i in range(1, number_of_regular_periods + 1)]
    if not payment_times or abs(payment_times[-1] - maturity) > TOLERANCE:
        payment_times.append(maturity)
    return payment_times


def coupon_accrual_periods(maturity: float, frequency: int = 2) -> list[float]:
    """
    Returns year-fraction accrual periods ending at each coupon payment time.
    Coupon amounts should be coupon_rate times the actual accrual period, not
    blindly coupon_rate / frequency. This matters for short maturities and stub
    periods, and it keeps the par-yield bootstrap internally consistent with
    the par-yield calculation on ZeroCurve.
    """
    payment_times = coupon_payment_times(maturity, frequency)
    previous_time = 0.0
    accrual_periods: list[float] = []
    for payment_time in payment_times:
        accrual_period = payment_time - previous_time
        if accrual_period <= 0:
            raise ValueError("Coupon accrual periods must be positive.")
        accrual_periods.append(accrual_period)
        previous_time = payment_time
    return accrual_periods


def _validated_curve_points(maturities: list[float],zero_rates: list[float]) -> tuple[list[float], list[float]]:
    """
    Validates and sorts the raw curve points before a ZeroCurve is created.
    Curve construction is a high-leverage step: every discount factor, forward
    rate, and bond price depends on these points being finite, positive in
    maturity, unique, and ordered.
    """

    if len(maturities) != len(zero_rates):
        raise ValueError("maturities and zero_rates must have the same length.")
    if not maturities:
        raise ValueError("A curve must contain at least one point.")
    points = sorted(zip(maturities, zero_rates))
    validated_maturities: list[float] = []
    validated_zero_rates: list[float] = []
    previous_maturity: float | None = None
    for maturity, zero_rate in points:
        validate_time_years(maturity)
        validate_rate(zero_rate, "zero_rate")
        if maturity <= 0:
            raise ValueError("Curve maturities must be positive.")
        if previous_maturity is not None and math.isclose(maturity, previous_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            raise ValueError(f"Duplicate curve maturity: {maturity}.")
        validated_maturities.append(float(maturity))
        validated_zero_rates.append(float(zero_rate))
        previous_maturity = maturity
    return validated_maturities, validated_zero_rates


@dataclass
class ZeroCurve:
    """
    Continuously compounded zero-rate curve.
    Rates are stored as decimals, so 4.5% is 0.045. Interpolation is linear on
    continuously compounded zero rates.
    This is the central pricing object of the project. Market data enters as
    par yields, bootstrapping converts those quotes into zero rates, and this
    class turns those zero rates into discount factors, forwards, implied par
    yields, and present values.
    """

    maturities: list[float]
    zero_rates: list[float]

    def __post_init__(self) -> None:
        """
        Normalizes curve inputs immediately after dataclass construction.
        This keeps every downstream method working from a clean curve instead
        of repeatedly defending against unsorted, duplicate, or invalid inputs.
        """
        self.maturities, self.zero_rates = _validated_curve_points(self.maturities, self.zero_rates)

    @property
    def min_maturity(self) -> float:
        """
        Smallest maturity available on the curve.
        Pricing code uses this boundary to decide whether a requested cashflow
        can be interpolated directly or needs an explicit extrapolation policy.
        """
        return self.maturities[0]

    @property
    def max_maturity(self) -> float:
        """
        Largest maturity available on the curve.
        This boundary protects long-dated pricing from silently using a curve
        outside its quoted market range.
        """
        return self.maturities[-1]

    def interpolate_rate(self, target_maturity: float, allow_extrapolation: bool = False) -> float:
        """
        Linearly interpolates the zero rate for a target maturity.
        Interpolation is what lets the curve price cashflows whose dates do not
        land exactly on quoted market tenors. The optional extrapolation flag is
        explicit because using endpoint rates outside the market curve is a
        modeling choice, not a mathematical fact.
        """

        validate_time_years(target_maturity)
        if target_maturity <= 0:
            raise ValueError("target_maturity must be positive.")
        range_error = f"Target maturity is outside the curve range [{self.min_maturity}, {self.max_maturity}]."
        return interpolate_curve_value(target_time=target_maturity, times=self.maturities, values=self.zero_rates, allow_left_extrapolation=allow_extrapolation,
                                       allow_right_extrapolation=allow_extrapolation, left_error=range_error, right_error=range_error)

    def discount_factor(self, maturity: float, allow_extrapolation: bool = False) -> float:
        """
        Calculates a discount factor from the continuously compounded zero rate.
        Discount factors are the object actually used in pricing. A zero rate is
        an interpretable curve quote; the discount factor is what converts a
        future cashflow into present value.
        """
        validate_time_years(maturity)
        if maturity == 0:
            return 1.0
        zero_rate = self.interpolate_rate(target_maturity=maturity, allow_extrapolation=allow_extrapolation)
        return discount_factor_continuous(zero_rate, maturity)

    def discount_factors(self, target_maturities: list[float], allow_extrapolation: bool = False) -> list[float]:
        """
        Calculates discount factors for a list of maturities.
        This batch helper is used when valuing many cashflows or preparing curve
        reports, keeping the discounting convention centralized in ZeroCurve.
        """
        return [self.discount_factor(maturity=maturity, allow_extrapolation=allow_extrapolation) for maturity in target_maturities]

    def forward_rate(self, start_maturity: float, end_maturity: float) -> float:
        """
        Calculates the continuously compounded forward rate between two dates.
        f = -ln(DF(T2) / DF(T1)) / (T2 - T1)
        Forward rates are not separate market inputs in this project. They are
        implied by the discount curve, which makes them a consistency check on
        the bootstrapped curve and useful for derivatives intuition.
        """

        validate_time_years(start_maturity)
        validate_time_years(end_maturity)
        if end_maturity <= start_maturity:
            raise ValueError("end_maturity must be greater than start_maturity.")
        start_df = self.discount_factor(start_maturity)
        end_df = self.discount_factor(end_maturity)
        return -math.log(end_df / start_df) / (end_maturity - start_maturity)

    def par_yield(self, maturity: float, frequency: int = 2) -> float:
        """
        Calculates the annual coupon rate that prices a par bond at 100.
        Coupon amounts use year-fraction accrual periods. The schedule is still
        not date-aware, so settlement, accrued interest, holidays, and day-count
        conventions are outside this class.
        This closes the loop with bootstrapping: if the zero curve was built
        correctly from par yields, asking the curve for a par yield at an input
        maturity should reproduce the original market quote.
        """
        payment_times = coupon_payment_times(maturity, frequency)
        accrual_periods = coupon_accrual_periods(maturity, frequency)
        annuity = sum(accrual_period * self.discount_factor(payment_time) for payment_time, accrual_period in zip(payment_times, accrual_periods))
        if annuity <= 0:
            raise ValueError("Coupon annuity must be positive.")
        final_df = self.discount_factor(maturity)
        return (1 - final_df) / annuity

    def price_cashflows(self, cashflows: list[tuple[float, float]], allow_extrapolation: bool = False) -> float:
        """
        Prices dated cashflows represented as (time_years, amount).
        Bond and derivative valuation eventually reduces to this operation:
        generate future cashflows, get the matching discount factors from the
        curve, and sum the present values.
        """

        price = 0.0
        for payment_time, amount in cashflows:
            validate_time_years(payment_time)
            if not math.isfinite(amount):
                raise ValueError("Cashflow amount must be finite.")
            price += amount * self.discount_factor(maturity=payment_time, allow_extrapolation=allow_extrapolation)
        return price

    def bumped(self, bump_size: float = BASIS_POINT) -> "ZeroCurve":
        """
        Returns a parallel-bumped zero curve.
        A bumped curve is used for risk measures such as curve DV01. Keeping it
        here ensures all instruments apply the same parallel-shift convention.
        """
        validate_rate(bump_size, "bump_size")
        return ZeroCurve(maturities=self.maturities.copy(), zero_rates=[zero_rate + bump_size for zero_rate in self.zero_rates])


def _validated_par_curve_inputs(maturities: list[float], par_yields: list[float],
                                frequency: int) -> tuple[list[float], list[float]]:
    """
    Validates and sorts par-yield curve quotes before bootstrapping.
    Bootstrapping is sequential, so bad ordering, duplicate maturities, or
    missing short-end support can contaminate every later zero rate.
    """
    if len(maturities) != len(par_yields):
        raise ValueError("maturities and par_yields must have the same length.")
    if not maturities:
        raise ValueError("At least one par-yield quote is required.")
    validate_compounding_frequency(frequency)
    points = sorted(zip(maturities, par_yields))
    sorted_maturities: list[float] = []
    sorted_par_yields: list[float] = []
    previous_maturity: float | None = None
    for maturity, par_yield in points:
        validate_time_years(maturity)
        validate_rate(par_yield, "par_yield")
        if maturity <= 0:
            raise ValueError("All maturities must be positive.")
        if previous_maturity is not None and math.isclose(maturity, previous_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            raise ValueError(f"Duplicate maturity: {maturity}.")
        sorted_maturities.append(float(maturity))
        sorted_par_yields.append(float(par_yield))
        previous_maturity = maturity
    first_coupon_time = 1 / frequency
    needs_coupon_dates = any(maturity > first_coupon_time + TOLERANCE for maturity in sorted_maturities)
    if needs_coupon_dates and sorted_maturities[0] > first_coupon_time + TOLERANCE:
        raise ValueError("The first maturity must be no later than the first coupon period "
            "so coupon cashflows can be discounted without extrapolation.")
    return sorted_maturities, sorted_par_yields


def bootstrap_zero_rates_from_par_yields(maturities: list[float], par_yields: list[float],
                                         frequency: int = 2) -> list[float]:
    """
    Bootstraps continuously compounded zero rates from par yields.

    Each par yield is treated as the coupon rate of a par bond with face value
    1. Coupon dates that fall between solved curve nodes are discounted using
    linear interpolation on continuously compounded zero rates.
    """
    sorted_maturities, sorted_par_yields = _validated_par_curve_inputs(maturities=maturities, par_yields=par_yields, frequency=frequency)
    solved_maturities: list[float] = []
    solved_zero_rates: list[float] = []
    for maturity, par_yield in zip(sorted_maturities, sorted_par_yields):
        payment_times = coupon_payment_times(maturity=maturity, frequency=frequency)
        accrual_periods = coupon_accrual_periods(maturity=maturity, frequency=frequency)

        def par_price_error(candidate_zero_rate: float) -> float:
            present_value = 0.0
            for payment_time, accrual_period in zip(payment_times, accrual_periods):
                cashflow = par_yield * accrual_period
                if math.isclose(payment_time, maturity, rel_tol=0.0, abs_tol=TOLERANCE):
                    cashflow += 1.0
                present_value += cashflow * partial_curve_discount_factor(
                    time_years=payment_time,
                    solved_maturities=solved_maturities,
                    solved_zero_rates=solved_zero_rates,
                    candidate_maturity=maturity,
                    candidate_zero_rate=candidate_zero_rate,
                    empty_error="At least one interpolation point is required.",
                )
            return present_value - 1.0

        zero_rate = solve_with_expanding_bracket(par_price_error, failure_message="Could not bracket a zero-rate solution.")
        solved_maturities.append(maturity)
        solved_zero_rates.append(zero_rate)
    return solved_zero_rates


def bootstrap_discount_factors_from_par_yields(maturities: list[float], par_yields: list[float],
                                               frequency: int = 2) -> list[float]:
    """
    Bootstraps discount factors from par yields.
    """
    sorted_maturities, _ = _validated_par_curve_inputs(maturities=maturities, par_yields=par_yields, frequency=frequency)
    zero_rates = bootstrap_zero_rates_from_par_yields(maturities=maturities, par_yields=par_yields, frequency=frequency)
    return [math.exp(-zero_rate * maturity) for maturity, zero_rate in zip(sorted_maturities, zero_rates)]
