"""
Backtest summary metrics.
These metrics compare actual P&L with risk-estimated P&L and summarize whether
parallel DV01 or key-rate DV01 produced smaller unexplained residuals.
"""

import math
from analytics import PnLExplainRow


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _rmse(values: list[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def summarize_pnl_backtest(rows: list[PnLExplainRow]) -> dict[str, float | str]:
    """
    Summarizes risk explain quality across backtest observations.
    Lower mean absolute unexplained P&L and RMSE indicate a better first-order
    risk explanation.
    """
    actual = [row.actual_pnl for row in rows]
    parallel_errors = [row.unexplained_pnl_parallel for row in rows]
    key_rate_errors = [row.unexplained_pnl_key_rate for row in rows]
    parallel_abs = [abs(value) for value in parallel_errors]
    key_rate_abs = [abs(value) for value in key_rate_errors]
    key_rate_wins = sum(1 for row in rows if row.which_model_explained_better == "key_rate")
    parallel_wins = len(rows) - key_rate_wins
    return {"observations": len(rows), "mean_actual_pnl": _mean(actual), "mean_abs_unexplained_parallel": _mean(parallel_abs),
            "mean_abs_unexplained_key_rate": _mean(key_rate_abs), "rmse_unexplained_parallel": _rmse(parallel_errors),
            "rmse_unexplained_key_rate": _rmse(key_rate_errors), "parallel_win_count": parallel_wins, "key_rate_win_count": key_rate_wins,
            "key_rate_win_ratio": key_rate_wins / len(rows) if rows else 0.0,
            "better_model": "key_rate" if _mean(key_rate_abs) < _mean(parallel_abs) else "parallel"}
