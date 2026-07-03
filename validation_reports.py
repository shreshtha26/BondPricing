"""
Validation and reconciliation reports for the fixed-income workflow.
These checks turn pricing assumptions into auditable rows: the curve should
reproduce its input par yields, and clean price plus accrued interest should
reconcile back to dirty price.
"""

import csv
from pathlib import Path

from bond_pricing import DateAwareFixedCouponBond
from market_data_loader import TreasuryCurveSnapshot
from yield_curve import ZeroCurve


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
        residual = model_par_yield - market_par_yield
        rows.append({
            "valuation_date": snapshot.valuation_date.isoformat(),
            "maturity": maturity,
            "market_par_yield": market_par_yield,
            "model_par_yield": model_par_yield,
            "residual": residual,
            "residual_bp": residual * 10000,
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


def export_report_rows(rows: list[dict[str, float | str | bool]], output_path: str | Path) -> Path:
    """
    Writes validation rows to CSV.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(rows[0]) if rows else []
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_path
