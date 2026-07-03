"""
End-to-end live fixed-income analytics workflow.
This script is the command-line entry point. It loads Treasury market data,
builds the bootstrapped zero curve, exports reports, writes the live chart, and
prices a date-aware example bond from the same curve.
"""

import argparse
import csv
import logging
import math
from datetime import date
from pathlib import Path
from bond_pricing import DateAwareFixedCouponBond
from config import DEFAULT_BOND_SETTINGS, DEFAULT_CURVE_BUILD_SETTINGS,DEFAULT_WORKFLOW_SETTINGS
from market_data_loader import TreasuryCurveSnapshot, load_fred_treasury_curve_snapshot
from risk_analytics import export_key_rate_dv01_report, key_rate_dv01_rows
from sofr_ois import bootstrap_sofr_ois_curve, export_sofr_ois_curve_report, load_latest_sofr_fixing_from_fred,load_ois_quotes_from_csv
from treasury_curve_builder import TreasuryInstrumentCurveResult,bootstrap_treasury_zero_curve_from_prices,export_treasury_bootstrap_report,load_treasury_instruments_from_csv
from validation_reports import calibration_report_rows, clean_dirty_accrued_reconciliation_rows, export_report_rows
from yield_curve import ZeroCurve


# It expects ISO format: YYYY-MM-DD
def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid date '{value}'. Please use YYYY-MM-DD format.") from error


# Accepts either decimal rates or percent-style rates from the command line.
def parse_rate(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"Invalid rate '{value}'. Please use a finite number.")
    if abs(value) > 1:
        return value / 100
    return value


# This creates a log file inside an output folder to write log messages here + terminal
def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir/DEFAULT_WORKFLOW_SETTINGS.log_path.name
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",handlers=[logging.FileHandler(log_path), logging.StreamHandler()])


def build_arg_parser() -> argparse.ArgumentParser:
    # python main.py --help
    parser = argparse.ArgumentParser(description="Build a live Treasury zero curve and price an example bond.")

    parser.add_argument("--date", default=DEFAULT_WORKFLOW_SETTINGS.default_curve_date,
                        help="FRED curve date in YYYY-MM-DD format. Defaults to latest complete date.")

    parser.add_argument("--output-dir",default=str(DEFAULT_WORKFLOW_SETTINGS.output_dir),
                        help="Directory for chart, CSV reports, and logs.")

    parser.add_argument("--frequency", type=int, default=DEFAULT_CURVE_BUILD_SETTINGS.frequency,
                        help="Coupon frequency used for par-yield bootstrapping.")

    parser.add_argument("--refresh-cache", action="store_true", default=DEFAULT_WORKFLOW_SETTINGS.refresh_market_data_cache,
                        help="Force a fresh FRED download instead of using cached CSV files.")

    parser.add_argument("--no-cache", action="store_true", help="Disable FRED cache reads and writes for this run.")

    parser.add_argument("--coupon-rate", type=float, default=DEFAULT_BOND_SETTINGS.coupon_rate,
                        help="Example bond annual coupon rate as a decimal.")

    parser.add_argument("--issue-date", default=DEFAULT_BOND_SETTINGS.issue_date.isoformat(),
                        help="Example bond issue date in YYYY-MM-DD format.")

    parser.add_argument("--maturity-date", default=DEFAULT_BOND_SETTINGS.maturity_date.isoformat(),
                        help="Example bond maturity date in YYYY-MM-DD format.")

    parser.add_argument("--settlement-date", default=None, help="Example bond settlement date. Defaults to the curve valuation date.")

    parser.add_argument("--treasury-instruments-csv", default=None,
                        help="Optional CSV of actual Treasury bill/note/bond quotes for instrument-level curve bootstrapping.")

    parser.add_argument("--allow-short-end-extrapolation", action="store_true",
                        help="Allow flat short-end extrapolation when an early coupon cashflow falls before the first Treasury instrument curve point.")

    parser.add_argument("--ois-quotes-csv", default=None, help="Optional CSV of OIS par fixed-rate quotes for SOFR/OIS bootstrapping.")

    parser.add_argument("--sofr-rate", type=float, default=None,
                        help="SOFR overnight fixing for OIS bootstrapping. Accepts decimal 0.0525 or percent 5.25. If omitted, FRED SOFR is used.")

    parser.add_argument("--sofr-date", default=None,
                        help="SOFR fixing date in YYYY-MM-DD format. Defaults to the settlement date when --ois-quotes-csv is supplied and --sofr-rate is omitted.")

    return parser


#  Prints the live curve transformation in table form.
def print_curve_report(snapshot: TreasuryCurveSnapshot) -> None:
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file,
            fieldnames=["valuation_date","source","quote_type",
                "curve_build_method","interpolation_method","extrapolation_method","maturity",
                "par_yield","zero_rate","discount_factor","forward_start","forward_end","forward_rate"])
        writer.writeheader()
        for row in snapshot.rows():
            writer.writerow({"valuation_date": snapshot.valuation_date, "source": snapshot.source,
                    "quote_type": snapshot.quote_type, "curve_build_method": snapshot.curve_build_method,
                    "interpolation_method": snapshot.interpolation_method, "extrapolation_method": snapshot.extrapolation_method, **row})


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


def build_example_bond(settlement_date: date, coupon_rate: float, issue_date: date, maturity_date: date) -> DateAwareFixedCouponBond:
    return DateAwareFixedCouponBond(face_value=DEFAULT_BOND_SETTINGS.face_value, coupon_rate=coupon_rate,
        issue_date=issue_date, maturity_date=maturity_date, settlement_date=settlement_date, frequency=DEFAULT_BOND_SETTINGS.frequency)


def bond_report_rows(bond: DateAwareFixedCouponBond, curve: ZeroCurve) -> tuple[dict[str, float | str], list[dict[str, float | str]]]:
    dirty_price = bond.dirty_price_from_curve(curve)
    accrued = bond.accrued_interest()
    clean_price = bond.clean_price_from_curve(curve)
    curve_dv01 = bond.curve_dv01(curve)
    summary = {"settlement_date": bond.settlement_date.isoformat(), "issue_date": bond.issue_date.isoformat(),
        "maturity_date": bond.maturity_date.isoformat(), "face_value": bond.face_value,
        "coupon_rate": bond.coupon_rate, "frequency": bond.frequency, "accrued_interest": accrued,
        "dirty_price_from_curve": dirty_price, "clean_price_from_curve": clean_price, "curve_dv01": curve_dv01}
    cashflow_rows = [{"payment_date": payment_date.isoformat(), "time_from_settlement": time_from_settlement,
            "cashflow": amount} for payment_date, time_from_settlement, amount in bond.future_cashflow_schedule()]
    return summary, cashflow_rows


def print_bond_report(bond: DateAwareFixedCouponBond, curve: ZeroCurve) -> None:
    """
    Prints date-aware bond valuation from the live zero curve.
    """
    summary, cashflow_rows = bond_report_rows(bond=bond, curve=curve)
    print()
    print("Date-Aware Bond Priced From Live Zero Curve")
    print("-" * 96)
    print(f"Settlement date:        {summary['settlement_date']}")
    print(f"Issue date:             {summary['issue_date']}")
    print(f"Maturity date:          {summary['maturity_date']}")
    print(f"Coupon rate:            {summary['coupon_rate']:.4%}")
    print(f"Coupon frequency:       {summary['frequency']}")
    print(f"Accrued interest:       {summary['accrued_interest']:.6f}")
    print(f"Dirty price from curve: {summary['dirty_price_from_curve']:.6f}")
    print(f"Clean price from curve: {summary['clean_price_from_curve']:.6f}")
    print(f"Curve DV01:             {summary['curve_dv01']:.6f}")
    print()
    print("Next Cashflows")
    print("-" * 96)
    print(f"{'Payment Date':>14} | {'Years':>8} | {'Cashflow':>12}")
    print("-" * 96)
    for row in cashflow_rows[:8]:
        print(f"{row['payment_date']:>14} | "f"{row['time_from_settlement']:>8.4f} | "f"{row['cashflow']:>12.6f}")


def export_bond_reports(bond: DateAwareFixedCouponBond, curve: ZeroCurve, summary_path: Path, cashflows_path: Path) -> None:
    summary, cashflow_rows = bond_report_rows(bond=bond, curve=curve)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    cashflows_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary))
        writer.writeheader()
        writer.writerow(summary)
    with cashflows_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file,fieldnames=["payment_date", "time_from_settlement", "cashflow"])
        writer.writeheader()
        writer.writerows(cashflow_rows)


def main() -> None:
    """
    Runs the full live-data workflow.
    """
    args = build_arg_parser().parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    logging.info("Starting live fixed-income analytics workflow.")
    curve_report_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.curve_report_path.name
    bond_report_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.bond_report_path.name
    cashflows_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.bond_cashflows_path.name
    chart_path = output_dir / DEFAULT_WORKFLOW_SETTINGS.curve_plot_path.name
    treasury_instrument_report_path = output_dir / "treasury_instrument_curve_report.csv"
    sofr_ois_report_path = output_dir / "sofr_ois_curve_report.csv"
    calibration_report_path = output_dir / "calibration_report.csv"
    key_rate_dv01_report_path = output_dir / "key_rate_dv01_report.csv"
    price_reconciliation_report_path = output_dir / "price_reconciliation_report.csv"
    snapshot = load_fred_treasury_curve_snapshot(date=args.date, frequency=args.frequency,
        cache_dir=DEFAULT_WORKFLOW_SETTINGS.fred_cache_dir, use_cache=not args.no_cache, refresh_cache=args.refresh_cache)
    curve = snapshot.to_zero_curve()
    settlement_date = parse_date(args.settlement_date) or snapshot.valuation_date
    bond = build_example_bond(settlement_date=settlement_date, coupon_rate=args.coupon_rate,
        issue_date=parse_date(args.issue_date), maturity_date=parse_date(args.maturity_date))
    snapshot.write_plot(output_path=chart_path)
    export_curve_report(snapshot=snapshot, output_path=curve_report_path)
    export_bond_reports(bond=bond, curve=curve, summary_path=bond_report_path, cashflows_path=cashflows_path)
    calibration_rows = calibration_report_rows(snapshot)
    key_rate_rows = key_rate_dv01_rows(bond=bond, curve=curve)
    reconciliation_rows = clean_dirty_accrued_reconciliation_rows(bond=bond, curve=curve)
    export_report_rows(rows=calibration_rows, output_path=calibration_report_path)
    export_key_rate_dv01_report(rows=key_rate_rows, output_path=key_rate_dv01_report_path)
    export_report_rows(rows=reconciliation_rows, output_path=price_reconciliation_report_path)
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
    print(f"Wrote live curve chart to: {chart_path}")
    print(f"Wrote curve report to:     {curve_report_path}")
    print(f"Wrote bond report to:      {bond_report_path}")
    print(f"Wrote cashflow report to:  {cashflows_path}")
    print(f"Wrote calibration report to:        {calibration_report_path}")
    print(f"Wrote key-rate DV01 report to:      {key_rate_dv01_report_path}")
    print(f"Wrote price reconciliation to:      {price_reconciliation_report_path}")
    if treasury_result is not None:
        print(f"Wrote Treasury instrument curve report to: {treasury_instrument_report_path}")
    if sofr_ois_rows is not None:
        print(f"Wrote SOFR/OIS curve report to:            {sofr_ois_report_path}")
    print_bond_report(bond=bond, curve=curve)
    print(f"Clean/dirty/accrued reconciliation passed: {reconciliation_rows[0]['passed']}")
    if treasury_result is not None:
        print_treasury_instrument_curve_report(treasury_result)
    if sofr_ois_rows is not None:
        print_sofr_ois_curve_report(sofr_ois_rows)
    logging.info("Selected curve valuation date: %s", snapshot.valuation_date)
    logging.info("Wrote chart: %s", chart_path)
    logging.info("Wrote curve report: %s", curve_report_path)
    logging.info("Wrote bond report: %s", bond_report_path)
    logging.info("Wrote calibration report: %s", calibration_report_path)
    logging.info("Wrote key-rate DV01 report: %s", key_rate_dv01_report_path)
    logging.info("Wrote price reconciliation report: %s", price_reconciliation_report_path)
    if treasury_result is not None:
        logging.info("Wrote Treasury instrument report: %s", treasury_instrument_report_path)
    if sofr_ois_rows is not None:
        logging.info("Wrote SOFR/OIS report: %s", sofr_ois_report_path)
    logging.info("Workflow completed successfully.")


if __name__ == "__main__":
    main()
