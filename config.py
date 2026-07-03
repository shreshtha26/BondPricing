"""
Central configuration for the project.
This module keeps paths, defaults, and runtime settings in one place so the
main workflow stays focused on execution logic. Centralizing these values makes
the project easier to configure, review, and maintain across different environments.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
FRED_CACHE_DIR = DATA_DIR / "fred_cache"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


# That means it is a lightweight class used to store data. frozen=True means once created, the settings should not be
# changed accidentally. So this class is mainly for: configuration / documentation / reporting / avoiding hardcoded assumptions
@dataclass(frozen=True)
# Assumptions used when turning market quotes into a zero curve.
# This class stores the default assumptions for building the interest-rate curve.
class CurveBuildSettings:
    frequency: int = 2 # semiannual coupons is standard for US Treasury notes and bonds

    # The project starts with par yields -> bootstraps -> discount factors -> zero rates
    # par-yield bootstrapping uses coupon cashflows. If a bond pays twice per year, the bootstrap assumes coupon periods every 6 months.
    curve_build_method: str = "par-yield bootstrap"

    # If the exact maturity is missing, the code estimates it by linear interpolation between the nearest available zero rates.
    interpolation_method: str = "linear zero-rate interpolation"

    # If the requested maturity is beyond the curve’s last point, the code treats it as extrapolation and only allows it when
    # explicitly enabled, using the nearest endpoint rate.
    extrapolation_method: str = "flat endpoint only when explicitly enabled"


@dataclass(frozen=True)
# Default date-aware bond used by main.py for the live-curve demo.
class ExampleBondSettings:
    face_value: float = 100.0
    coupon_rate: float = 0.045
    issue_date: date = date(2024, 2, 15)
    maturity_date: date = date(2034, 2, 15)
    frequency: int = 2


@dataclass(frozen=True)
# Output and runtime settings for the end-to-end workflow.
class WorkflowSettings:
    output_dir: Path = OUTPUT_DIR
    fred_cache_dir: Path = FRED_CACHE_DIR
    curve_plot_path: Path = OUTPUT_DIR / "curve_plot.html"
    curve_report_path: Path = OUTPUT_DIR / "curve_report.csv"
    bond_report_path: Path = OUTPUT_DIR / "bond_report.csv"
    bond_cashflows_path: Path = OUTPUT_DIR / "bond_cashflows.csv"
    log_path: Path = OUTPUT_DIR / "run.log"
    default_curve_date: str | None = None
    refresh_market_data_cache: bool = False


DEFAULT_CURVE_BUILD_SETTINGS = CurveBuildSettings()
DEFAULT_BOND_SETTINGS = ExampleBondSettings()
DEFAULT_WORKFLOW_SETTINGS = WorkflowSettings()
