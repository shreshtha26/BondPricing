"""
Central configuration for the project.
Only project-wide defaults live here. Bond terms and market quotes still come
from input files at runtime.
"""

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
FRED_CACHE_DIR = DATA_DIR / "fred_cache"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


@dataclass(frozen=True)
class CurveConfig:
    """Curve-building assumptions used in reports and bootstraps."""

    # US Treasury notes and bonds pay semiannual coupons.
    frequency: int = 2
    # CMT par-style yields are bootstrapped into discount factors and zero rates.
    curve_build_method: str = "par-yield bootstrap"
    # Missing maturities are estimated between neighboring zero-rate pillars.
    interpolation_method: str = "linear zero-rate interpolation"
    # Endpoint extrapolation is explicit so short/long cashflows are not hidden assumptions.
    extrapolation_method: str = "flat endpoint only when explicitly enabled"
    compounding: str = "continuous"


@dataclass(frozen=True)
class PricingConfig:
    price_type: str = "dirty"
    use_accrual: bool = True
    use_credit: bool = False
    use_optionality: bool = False
    use_floating_rate: bool = False
    use_inflation: bool = False
    use_liquidity_adjustment: bool = False
    use_market_data_governance: bool = True


@dataclass(frozen=True)
class ModelConfig:
    root_solver: str = "brentq_expanding_bracket"
    lower_rate_bound: float = -0.25
    upper_rate_bound: float = 0.25
    max_abs_rate_bound: float = 5.0
    tolerance: float = 1e-10


@dataclass(frozen=True)
class WorkflowSettings:
    """File locations used by the command-line workflow."""

    output_dir: Path = OUTPUT_DIR
    fred_cache_dir: Path = FRED_CACHE_DIR
    curve_report_path: Path = OUTPUT_DIR / "curve_report.csv"
    bond_quote_validation_report_path: Path = OUTPUT_DIR / "bond_quote_validation_report.csv"
    log_path: Path = OUTPUT_DIR / "run.log"
    default_curve_date: str | None = None
    refresh_market_data_cache: bool = False


CurveBuildSettings = CurveConfig
DEFAULT_CURVE_CONFIG = CurveConfig()
DEFAULT_CURVE_BUILD_SETTINGS = DEFAULT_CURVE_CONFIG
DEFAULT_PRICING_CONFIG = PricingConfig()
DEFAULT_MODEL_CONFIG = ModelConfig()
DEFAULT_WORKFLOW_SETTINGS = WorkflowSettings()
