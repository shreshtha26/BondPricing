"""
Command-line workflow for curve building and bond quote validation.
"""

import argparse
import logging
import math
from datetime import date
from pathlib import Path
from config import DEFAULT_CURVE_BUILD_SETTINGS, DEFAULT_WORKFLOW_SETTINGS
from market_data import (TreasuryCurveSnapshot, export_rows_to_csv, load_bond_market_quotes_from_csv,
                         load_fred_treasury_curve_snapshot, load_security_master_from_csv)
from rates import bootstrap_sofr_ois_curve, export_sofr_ois_curve_report, load_latest_sofr_fixing_from_fred,load_ois_quotes_from_csv
from treasury import TreasuryInstrumentCurveResult,bootstrap_treasury_zero_curve_from_prices,export_treasury_bootstrap_report,load_treasury_instruments_from_csv
from analytics import calibration_report_rows, export_bond_quote_validation_report, export_report_rows, validate_bond_quotes


def parse_date(value: str | None) -> date | None:
    """Parse an optional CLI date in YYYY-MM-DD format."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid date '{value}'. Please use YYYY-MM-DD format.") from error


def parse_rate(value: float | None) -> float | None:
    """Accept decimal rates or percent-style rates from the CLI."""

    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"Invalid rate '{value}'. Please use a finite number.")
    if abs(value) > 1:
        return value / 100
    return value


def setup_logging(output_dir: Path) -> None:
    """Send workflow logs to both the terminal and outputs/run.log."""

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir/DEFAULT_WORKFLOW_SETTINGS.log_path.name
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",handlers=[logging.FileHandler(log_path), logging.StreamHandler()])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a live Treasury zero curve and validate supplied bond quotes.")

    parser.add_argument("--date", default=DEFAULT_WORKFLOW_SETTINGS.default_curve_date,
                        help="FRED curve date in YYYY-MM-DD format. Defaults to latest complete date.")

    parser.add_argument("--output-dir",default=str(DEFAULT_WORKFLOW_SETTINGS.output_dir),
                        help="Directory for chart, CSV reports, and logs.")

    parser.add_argument("--frequency", type=int, default=DEFAULT_CURVE_BUILD_SETTINGS.frequency,
                        help="Coupon frequency used for par-yield bootstrapping.")

    parser.add_argument("--refresh-cache", action="store_true", default=DEFAULT_WORKFLOW_SETTINGS.refresh_market_data_cache,
                        help="Force a fresh FRED download instead of using cached CSV files.")

    parser.add_argument("--no-cache", action="store_true", help="Disable FRED cache reads and writes for this run.")

    parser.add_argument("--settlement-date", default=None,
                        help="Settlement/effective date for optional Treasury/OIS workflows. Defaults to the curve valuation date.")

    parser.add_argument("--treasury-instruments-csv", default=None,
                        help="Optional CSV of actual Treasury bill/note/bond quotes for instrument-level curve bootstrapping.")

    parser.add_argument("--allow-short-end-extrapolation", action="store_true",
                        help="Allow flat short-end extrapolation when an early coupon cashflow falls before the first Treasury instrument curve point.")

    parser.add_argument("--ois-quotes-csv", default=None, help="Optional CSV of OIS par fixed-rate quotes for SOFR/OIS bootstrapping.")

    parser.add_argument("--sofr-rate", type=float, default=None,
                        help="SOFR overnight fixing for OIS bootstrapping. Accepts decimal 0.0525 or percent 5.25. If omitted, FRED SOFR is used.")

    parser.add_argument("--sofr-date", default=None,
                        help="SOFR fixing date in YYYY-MM-DD format. Defaults to the settlement date when --ois-quotes-csv is supplied and --sofr-rate is omitted.")

    parser.add_argument("--security-master-csv", default=None,
                        help="Optional CSV of CUSIP/ISIN bond terms for quote validation.")

    parser.add_argument("--bond-quotes-csv", default=None,
                        help="Optional CSV of observed clean/dirty bond prices for quote validation.")

    parser.add_argument("--bond-quote-tolerance", type=float, default=0.02,
                        help="Allowed model-vs-market price difference per 100 face value.")

    return parser


def print_curve_report(snapshot: TreasuryCurveSnapshot) -> None:
    """Print the market quote to zero-curve transformation."""

    print(f"Live Treasury Curve Snapshot: {snapshot.valuation_date}")
    print(f"Source: {snapshot.source}")
    print(f"Quote type: {snapshot.quote_type}")
    print(f"Curve build method: {snapshot.curve_build_method}")
    print(f"Interpolation: {snapshot.interpolation_method}")
    print(f"Extrapolation: {snapshot.extrapolation_method}")
    print("-" * 104)
    print(f"{'Maturity':>10} | {'Par Yield':>10} | {'Zero Rate':>10} | {'Discount':>10} | {'Forward Period':>18} | {'Forward':>10}")
    print("-" * 104)
    for row in snapshot.rows():
        forward_period = f"{row['forward_start']:.3f}Y->{row['forward_end']:.3f}Y"
        print(f"{row['maturity']:>9.3f}Y | {row['par_yield']:>9.4%} | {row['zero_rate']:>9.4%} | {row['discount_factor']:>10.6f} | {forward_period:>18} | {row['forward_rate']:>9.4%}")


def export_curve_report(snapshot: TreasuryCurveSnapshot, output_path: Path) -> None:
    rows = [{"valuation_date": snapshot.valuation_date, "source": snapshot.source, "quote_type": snapshot.quote_type,
             "curve_build_method": snapshot.curve_build_method, "interpolation_method": snapshot.interpolation_method,
             "extrapolation_method": snapshot.extrapolation_method, **row} for row in snapshot.rows()]
    export_rows_to_csv(rows, output_path,
                       fieldnames=["valuation_date", "source", "quote_type", "curve_build_method", "interpolation_method",
                                   "extrapolation_method", "maturity", "par_yield", "zero_rate", "discount_factor",
                                   "forward_start", "forward_end", "forward_rate"])


def print_treasury_instrument_curve_report(result: TreasuryInstrumentCurveResult) -> None:
    """
    Prints the instrument-level Treasury bootstrap.
    """
    print()
    print("Instrument-Level Treasury Bootstrap")
    print("-" * 112)
    print(f"Settlement date: {result.settlement_date}")
    print(f"Source: {result.source}")
    print(f"Quote type: {result.quote_type}")
    print("-" * 112)
    print(f"{'Type':>6} | "f"{'Maturity':>12} | "f"{'Years':>8} | "f"{'Coupon':>9} | "f"{'Clean':>10} | "f"{'Dirty':>10} | "f"{'Discount':>10} | "f"{'Zero':>9}")
    print("-" * 112)
    for point in result.points:
        coupon = "" if point.coupon_rate is None else f"{point.coupon_rate:.4%}"
        clean = "" if point.clean_price is None else f"{point.clean_price:.6f}"
        print(f"{point.instrument_type:>6} | "f"{point.maturity_date.isoformat():>12} | "f"{point.maturity_years:>8.4f} | "f"{coupon:>9} | "f"{clean:>10} | "f""
              f""f"{point.dirty_price:>10.6f} | "f"{point.discount_factor:>10.6f} | "f"{point.zero_rate:>8.4%}")


def print_sofr_ois_curve_report(rows: list[dict[str, float | str]]) -> None:
    """
    Prints the SOFR/OIS bootstrap output.
    """
    if not rows:
        return
    print()
    print("SOFR/OIS Bootstrap")
    print("-" * 104)
    print(f"Effective date: {rows[0]['effective_date']}")
    print(f"Source: {rows[0]['source']}")
    print(f"Quote type: {rows[0]['quote_type']}")
    print("-" * 104)
    print(f"{'Node':>14} | "f"{'Maturity':>12} | "f"{'Years':>8} | "f"{'Fixed Rate':>10} | "f"{'Discount':>10} | "f"{'Zero':>9} | "f"{'Annuity':>10}")
    print("-" * 104)
    for row in rows:
        print(f"{row['node_type']:>14} | "f"{row['maturity_date']:>12} | "f"{row['maturity_years']:>8.4f} | "f"{row['fixed_rate']:>9.4%} | "
            f"{row['discount_factor']:>10.6f} | "f"{row['zero_rate']:>8.4%} | "f"{row['fixed_leg_annuity']:>10.6f}")


def main() -> None:
    """
    Runs the full live-data workflow.
    """
    args = build_arg_parser().parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    logging.info("Starting live fixed-income analytics workflow.")
    curve_report_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.curve_report_path.name
    treasury_instrument_report_path = output_dir / "treasury_instrument_curve_report.csv"
    sofr_ois_report_path = output_dir / "sofr_ois_curve_report.csv"
    calibration_report_path = output_dir / "calibration_report.csv"
    bond_quote_validation_report_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.bond_quote_validation_report_path.name
    snapshot = load_fred_treasury_curve_snapshot(date=args.date, frequency=args.frequency,
        cache_dir=DEFAULT_WORKFLOW_SETTINGS.fred_cache_dir, use_cache=not args.no_cache, refresh_cache=args.refresh_cache)
    curve = snapshot.to_zero_curve()
    settlement_date = parse_date(args.settlement_date) or snapshot.valuation_date
    export_curve_report(snapshot=snapshot, output_path=curve_report_path)
    calibration_rows = calibration_report_rows(snapshot)
    export_report_rows(rows=calibration_rows, output_path=calibration_report_path)
    bond_quote_validation_rows = None
    if args.security_master_csv is not None or args.bond_quotes_csv is not None:
        if args.security_master_csv is None or args.bond_quotes_csv is None:
            raise ValueError("--security-master-csv and --bond-quotes-csv must be supplied together.")
        securities = load_security_master_from_csv(args.security_master_csv)
        quotes = load_bond_market_quotes_from_csv(args.bond_quotes_csv)
        bond_quote_validation_rows = validate_bond_quotes(securities=securities, quotes=quotes, curve=curve, tolerance=args.bond_quote_tolerance)
        export_bond_quote_validation_report(rows=bond_quote_validation_rows, output_path=bond_quote_validation_report_path)
    treasury_result = None
    if args.treasury_instruments_csv is not None:
        treasury_instruments = load_treasury_instruments_from_csv(args.treasury_instruments_csv)
        treasury_result = bootstrap_treasury_zero_curve_from_prices(instruments=treasury_instruments,
            settlement_date=settlement_date, allow_short_end_extrapolation=args.allow_short_end_extrapolation)
        export_treasury_bootstrap_report(result=treasury_result,output_path=treasury_instrument_report_path)
    sofr_ois_rows = None
    if args.ois_quotes_csv is not None:
        ois_quotes = load_ois_quotes_from_csv(args.ois_quotes_csv)
        sofr_rate = parse_rate(args.sofr_rate)
        if sofr_rate is None:
            sofr_date = parse_date(args.sofr_date) or settlement_date
            _, sofr_rate = load_latest_sofr_fixing_from_fred(date_value=sofr_date,cache_dir=DEFAULT_WORKFLOW_SETTINGS.fred_cache_dir,
                                                             use_cache=not args.no_cache,refresh_cache=args.refresh_cache)
        sofr_ois_result = bootstrap_sofr_ois_curve(effective_date=settlement_date,overnight_rate=sofr_rate,ois_quotes=ois_quotes)
        export_sofr_ois_curve_report(result=sofr_ois_result,output_path=sofr_ois_report_path)
        sofr_ois_rows = sofr_ois_result.rows()
    print_curve_report(snapshot)
    print()
    print(f"Wrote curve report to:     {curve_report_path}")
    print(f"Wrote calibration report to:        {calibration_report_path}")
    if treasury_result is not None:
        print(f"Wrote Treasury instrument curve report to: {treasury_instrument_report_path}")
    if sofr_ois_rows is not None:
        print(f"Wrote SOFR/OIS curve report to:            {sofr_ois_report_path}")
    if bond_quote_validation_rows is not None:
        failed = sum(1 for row in bond_quote_validation_rows if row.validation_status == "FAIL")
        print(f"Wrote bond quote validation report to:     {bond_quote_validation_report_path}")
        print(f"Bond quote validations: {len(bond_quote_validation_rows)} total, {failed} failed")
    if treasury_result is not None:
        print_treasury_instrument_curve_report(treasury_result)
    if sofr_ois_rows is not None:
        print_sofr_ois_curve_report(sofr_ois_rows)
    logging.info("Selected curve valuation date: %s", snapshot.valuation_date)
    logging.info("Wrote curve report: %s", curve_report_path)
    logging.info("Wrote calibration report: %s", calibration_report_path)
    if treasury_result is not None:
        logging.info("Wrote Treasury instrument report: %s", treasury_instrument_report_path)
    if sofr_ois_rows is not None:
        logging.info("Wrote SOFR/OIS report: %s", sofr_ois_report_path)
    if bond_quote_validation_rows is not None:
        logging.info("Wrote bond quote validation report: %s", bond_quote_validation_report_path)
    logging.info("Workflow completed successfully.")


if __name__ == "__main__":
    main()
