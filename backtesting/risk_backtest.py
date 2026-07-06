"""
Historical bond risk backtesting.
This module runs the existing curve, pricing, DV01, and key-rate DV01 engine
over multiple market dates, then checks how well yesterday's risk explains the
next market day's realized dirty-price movement.
"""

from dataclasses import dataclass
from pathlib import Path
from pricing import DateAwareFixedCouponBond
from config import FRED_CACHE_DIR
from market_data import TreasuryCurveSnapshot, export_rows_to_csv, load_fred_treasury_curve_snapshot
from analytics import PnLExplainRow, explain_price_move
from analytics import calibration_report_rows
from analytics import ValuationSnapshot, export_valuation_snapshots, valuation_snapshot_from_bond_curve
from curves import ZeroCurve
from backtesting.metrics import summarize_pnl_backtest


@dataclass
class HistoricalRiskBacktestResult:
    """
    Container for historical valuation snapshots and P&L explain rows.
    The valuation snapshots are the daily model outputs; the P&L explain rows
    compare each date with the next available date.
    """
    valuation_snapshots: list[ValuationSnapshot]
    curves: list[ZeroCurve]
    pnl_rows: list[PnLExplainRow]

    def summary(self) -> dict[str, float | str]:
        return summarize_pnl_backtest(self.pnl_rows)

    def export_reports(self, output_dir: str | Path) -> dict[str, Path]:
        output_dir = Path(output_dir)
        valuation_path = export_valuation_snapshots(self.valuation_snapshots, output_dir / "historical_valuations.csv")
        pnl_path = export_pnl_backtest_report(self.pnl_rows, output_dir / "pnl_backtest.csv")
        summary_path = export_backtest_summary(self.summary(), output_dir / "backtest_summary.csv")
        return {"historical_valuations": valuation_path, "pnl_backtest": pnl_path, "backtest_summary": summary_path}


def _snapshot_calibration_status(snapshot: TreasuryCurveSnapshot, tolerance_bp: float = 1e-6) -> str:
    rows = calibration_report_rows(snapshot)
    if all(abs(row["residual_bp"]) <= tolerance_bp for row in rows):
        return "PASS"
    return "FAIL"


def run_bond_risk_backtest(bond_template: DateAwareFixedCouponBond, curve_snapshots: list[TreasuryCurveSnapshot],
                           instrument_id: str = "BOND") -> HistoricalRiskBacktestResult:
    """
    Runs a bond valuation and risk explain backtest from supplied curve snapshots.
    Supplying snapshots keeps the function testable and also supports cached or
    custom historical market data sources.
    """
    if len(curve_snapshots) < 2:
        raise ValueError("At least two curve snapshots are required for backtesting.")
    sorted_snapshots = sorted(curve_snapshots, key=lambda snapshot: snapshot.valuation_date)
    valuation_snapshots: list[ValuationSnapshot] = []
    curves: list[ZeroCurve] = []
    for snapshot in sorted_snapshots:
        curve = snapshot.to_zero_curve()
        curves.append(curve)
        valuation_snapshots.append(valuation_snapshot_from_bond_curve(bond=bond_template, curve=curve, as_of_date=snapshot.valuation_date,
                                   instrument_id=instrument_id, curve_date=snapshot.valuation_date, calibration_status=_snapshot_calibration_status(snapshot)))
    pnl_rows = [explain_price_move(start_snapshot=valuation_snapshots[index], end_snapshot=valuation_snapshots[index + 1],
                                   start_curve=curves[index], end_curve=curves[index + 1]) for index in range(len(valuation_snapshots) - 1)]
    return HistoricalRiskBacktestResult(valuation_snapshots=valuation_snapshots, curves=curves, pnl_rows=pnl_rows)


def run_fred_bond_risk_backtest(bond_template: DateAwareFixedCouponBond, dates: list[str], instrument_id: str = "BOND",
                                frequency: int = 2, cache_dir: str | Path | None = None, use_cache: bool = True,
                                refresh_cache: bool = False) -> HistoricalRiskBacktestResult:
    """
    Loads FRED Treasury curves for specific historical dates and runs the bond risk backtest.
    Passing explicit dates avoids accidental changes when new FRED observations
    are published.
    """
    snapshots = [load_fred_treasury_curve_snapshot(date=curve_date, frequency=frequency, cache_dir=cache_dir if cache_dir is not None else FRED_CACHE_DIR,
                 use_cache=use_cache, refresh_cache=refresh_cache) for curve_date in dates]
    return run_bond_risk_backtest(bond_template=bond_template, curve_snapshots=snapshots, instrument_id=instrument_id)


def export_pnl_backtest_report(rows: list[PnLExplainRow], output_path: str | Path) -> Path:
    return export_rows_to_csv((row.row() for row in rows), output_path)


def export_backtest_summary(summary: dict[str, float | str], output_path: str | Path) -> Path:
    return export_rows_to_csv([summary], output_path, fieldnames=list(summary))
