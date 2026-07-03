"""
Risk analytics for curve-based fixed-income valuation.
This module keeps risk calculations separate from product definitions. Products
generate cashflows, curves discount cashflows, and this layer applies market
shocks such as key-rate bumps to measure sensitivity.
"""

import csv
import math
from pathlib import Path

from bond_pricing import DateAwareFixedCouponBond
from int_rate_convention import BASIS_POINT, validate_rate
from yield_curve import TOLERANCE, ZeroCurve


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
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = ["key_maturity", "base_dirty_price", "price_down_1bp", "price_up_1bp", "key_rate_dv01", "bump_size"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_path
