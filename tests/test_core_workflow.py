import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from pricing import DateAwareFixedCouponBond
from backtesting.risk_backtest import run_bond_risk_backtest
from analytics import CalibrationRow
from market_data import CurveSpec
from main import parse_date, parse_rate
from market_data import MarketDataPoint
from market_data import TreasuryCurveSnapshot
from market_data import BondMarketQuote, SecurityMasterRecord, load_bond_market_quotes_from_csv, load_security_master_from_csv
from market_data import CurveRole, MultiCurveSet, TradeCurveContext
from curves import append_unique_curve_point, solve_with_expanding_bracket
from analytics import explain_price_move
from analytics import key_rate_dv01_rows
from analytics import valuation_snapshot_from_bond_curve
from analytics import calibration_report_rows, clean_dirty_accrued_reconciliation_rows
from analytics import validate_bond_quote, validate_bond_quotes
from curves import ZeroCurve
from pricing import InstrumentSpec, MarketState, PricingContext, PricingResult, price

class CoreWorkflowTests(unittest.TestCase):
    def test_parse_date_accepts_iso_and_missing_values(self) -> None:
        self.assertEqual(parse_date("2026-06-25"), date(2026, 6, 25))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date(""))
        with self.assertRaises(ValueError):
            parse_date("25/06/2026")

    def test_parse_rate_accepts_decimal_percent_and_zero(self) -> None:
        self.assertIsNone(parse_rate(None))
        self.assertEqual(parse_rate(0.0), 0.0)
        self.assertAlmostEqual(parse_rate(0.0525), 0.0525)
        self.assertAlmostEqual(parse_rate(5.25), 0.0525)

    def test_bootstrap_calibration_report_reprices_par_yields(self) -> None:
        snapshot = TreasuryCurveSnapshot(valuation_date=date(2026, 6, 25), maturities=[0.5, 1.0, 2.0, 3.0], par_yields=[0.04, 0.041, 0.043, 0.044])
        rows = calibration_report_rows(snapshot)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(abs(row["residual_bp"]) < 1e-8 for row in rows))

    def test_key_rate_dv01_returns_one_row_per_curve_node(self) -> None:
        bond = DateAwareFixedCouponBond(face_value=100, coupon_rate=0.045, issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), settlement_date=date(2026, 6, 25))
        curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        rows = key_rate_dv01_rows(bond=bond, curve=curve)
        self.assertEqual(len(rows), len(curve.maturities))
        self.assertTrue(any(abs(row["key_rate_dv01"]) > 0 for row in rows))

    def test_clean_dirty_accrued_reconciliation_passes(self) -> None:
        bond = DateAwareFixedCouponBond(face_value=100, coupon_rate=0.045, issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), settlement_date=date(2026, 6, 25))
        curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        row = clean_dirty_accrued_reconciliation_rows(bond=bond, curve=curve)[0]
        self.assertTrue(row["passed"])
        self.assertAlmostEqual(row["dirty_price"], row["clean_plus_accrued"], places=10)

    def test_market_data_point_tracks_mid_staleness_and_flags(self) -> None:
        timestamp = datetime(2026, 6, 25, 15, 30)
        quote = MarketDataPoint(instrument_id="UST10Y", bid=0.0439, ask=0.0441, timestamp=timestamp, source="TEST", quote_type="CMT_PAR_YIELD")
        self.assertAlmostEqual(quote.mid or 0.0, 0.044)
        self.assertAlmostEqual(quote.effective_value(), 0.044)
        self.assertTrue(quote.is_stale_as_of(as_of=timestamp + timedelta(seconds=61), max_age_seconds=60))

    def test_treasury_snapshot_carries_market_data_points(self) -> None:
        quote = MarketDataPoint(instrument_id="DGS1", value=0.04, timestamp=datetime(2026, 6, 25), source="FRED", quote_type="Treasury CMT par-style yield")
        snapshot = TreasuryCurveSnapshot(valuation_date=date(2026, 6, 25), maturities=[1.0], par_yields=[0.04], market_data_points=[quote])
        self.assertEqual(snapshot.quote_by_series_id()["DGS1"].effective_value(), 0.04)
        self.assertEqual(snapshot.quote_rows()[0]["instrument_id"], "DGS1")

    def test_curve_spec_rejects_duplicate_instruments(self) -> None:
        spec = CurveSpec(curve_name="USD SOFR OIS", curve_type="discount", instruments_used=["SOFR_ON", "OIS_1Y"])
        self.assertEqual(spec.currency, "USD")
        with self.assertRaises(ValueError):
            CurveSpec(curve_name="BAD", instruments_used=["OIS_1Y", "OIS_1Y"])

    def test_calibration_row_and_shared_numerical_helpers(self) -> None:
        row = CalibrationRow(curve_name="TEST", instrument_id="2Y", maturity=2.0, market_quote=0.04, model_quote=0.040000000001)
        self.assertEqual(row.calibration_status, "PASS_TOLERANCE")
        self.assertAlmostEqual(solve_with_expanding_bracket(lambda x: x - 0.25), 0.25)
        maturities: list[float] = []
        values: list[float] = []
        append_unique_curve_point(maturities=maturities, values=values, maturity=1.0, value=0.04)
        with self.assertRaises(ValueError):
            append_unique_curve_point(maturities=maturities, values=values, maturity=1.0, value=0.041)

    def test_multi_curve_set_validates_trade_curve_roles(self) -> None:
        curve = ZeroCurve(maturities=[1.0, 2.0], zero_rates=[0.04, 0.041])
        curve_set = MultiCurveSet(currency="USD")
        curve_set.add_curve(role=CurveRole.SOFR_OIS_DISCOUNT, spec=CurveSpec(curve_name="USD SOFR OIS", curve_type="discount"), curve=curve, source="TEST")
        curve_set.add_curve(role=CurveRole.TREASURY_BENCHMARK, spec=CurveSpec(curve_name="USD Treasury", curve_type="benchmark"), curve=curve, source="TEST")
        context = TradeCurveContext(discounting=CurveRole.SOFR_OIS_DISCOUNT, benchmark=CurveRole.TREASURY_BENCHMARK)
        context.validate_against(curve_set)
        self.assertIs(curve_set.get_curve(CurveRole.SOFR_OIS_DISCOUNT), curve)

    def test_valuation_snapshot_and_pnl_explain_use_dv01_sign_correctly(self) -> None:
        bond = DateAwareFixedCouponBond(face_value=100, coupon_rate=0.045, issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), settlement_date=date(2026, 6, 25))
        start_curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        end_curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.0401, 0.0411, 0.0421, 0.0431, 0.0441])
        start = valuation_snapshot_from_bond_curve(bond=bond, curve=start_curve, as_of_date=date(2026, 6, 25), instrument_id="TEST_BOND")
        end = valuation_snapshot_from_bond_curve(bond=bond, curve=end_curve, as_of_date=date(2026, 6, 26), instrument_id="TEST_BOND")
        row = explain_price_move(start_snapshot=start, end_snapshot=end, start_curve=start_curve, end_curve=end_curve)
        self.assertAlmostEqual(row.parallel_curve_move_bps, 1.0, places=10)
        self.assertLess(row.estimated_pnl_parallel, 0.0)

    def test_bond_risk_backtest_returns_valuation_pnl_and_summary(self) -> None:
        bond = DateAwareFixedCouponBond(face_value=100, coupon_rate=0.045, issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), settlement_date=date(2026, 6, 25))
        snapshots = [
            TreasuryCurveSnapshot(valuation_date=date(2026, 6, 25), maturities=[0.5, 1.0, 2.0, 3.0], par_yields=[0.04, 0.041, 0.043, 0.044]),
            TreasuryCurveSnapshot(valuation_date=date(2026, 6, 26), maturities=[0.5, 1.0, 2.0, 3.0], par_yields=[0.0401, 0.0411, 0.0431, 0.0441]),
        ]
        result = run_bond_risk_backtest(bond_template=bond, curve_snapshots=snapshots, instrument_id="TEST_BOND")
        self.assertEqual(len(result.valuation_snapshots), 2)
        self.assertEqual(len(result.pnl_rows), 1)
        self.assertEqual(result.summary()["observations"], 1)

    def test_pricing_types_create_compact_engine_contract(self) -> None:
        curve = ZeroCurve(maturities=[1.0, 2.0], zero_rates=[0.04, 0.041])

        instrument = InstrumentSpec(instrument_id="TEST_BOND",
            instrument_type="fixed_coupon_bond",
            face_value=100.0,
            issue_date=date(2024, 2, 15),
            maturity_date=date(2034, 2, 15),
            coupon_rate=0.045)

        market = MarketState(valuation_date=date(2026, 6, 25),
            discount_curve=curve,
            quote_source="TEST")

        context = PricingContext()

        result = PricingResult(instrument_id=instrument.instrument_id,
            valuation_date=market.valuation_date,
            clean_price=101.0,
            dirty_price=102.0,
            accrued_interest=1.0,
            diagnostics={"quote_source": market.quote_source})

        self.assertEqual(instrument.currency, "USD")
        self.assertEqual(context.pricing.price_type, "dirty")
        self.assertEqual(context.curve.compounding, "continuous")
        self.assertEqual(result.diagnostics["quote_source"], "TEST")

    def test_pricing_types_validate_basic_inputs(self) -> None:
        with self.assertRaises(ValueError):
            InstrumentSpec(instrument_id="", instrument_type="fixed_coupon_bond", face_value=100.0,
                issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15))

        with self.assertRaises(ValueError):
            InstrumentSpec(instrument_id="BAD", instrument_type="fixed_coupon_bond", face_value=-100.0,
                issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15))

        instrument = InstrumentSpec(instrument_id="OK", instrument_type="fixed_coupon_bond", face_value=100.0,
            issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), currency="usd")

        self.assertEqual(instrument.currency, "USD")

    def test_pricing_api_prices_fixed_coupon_bond_with_compact_contract(self) -> None:
        from pricing import price

        curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        instrument = InstrumentSpec(instrument_id="TEST_BOND", instrument_type="fixed_coupon_bond", face_value=100.0,
            issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), coupon_rate=0.045)
        market = MarketState(valuation_date=date(2026, 6, 25), discount_curve=curve, quote_source="TEST")

        result = price(instrument, market)

        self.assertEqual(result.instrument_id, "TEST_BOND")
        self.assertGreater(result.dirty_price, 0.0)
        self.assertAlmostEqual(result.clean_price + result.accrued_interest, result.dirty_price, places=10)
        self.assertEqual(result.diagnostics["instrument_type"], "fixed_coupon_bond")

    def test_cusip_level_quote_validation_prices_fixed_and_zero_coupon_bonds(self) -> None:
        curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        valuation_date = date(2026, 6, 25)
        fixed_security = SecurityMasterRecord(security_id="912TESTFIX", id_type="CUSIP", instrument_type="fixed_coupon_bond",
            issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), face_value=100.0, frequency=2, currency="USD", coupon_rate=0.045)
        zero_security = SecurityMasterRecord(security_id="912TESTZRO", id_type="CUSIP", instrument_type="zero_coupon_bond",
            issue_date=date(2024, 1, 1), maturity_date=date(2028, 1, 1), face_value=100.0, frequency=2, currency="USD", issue_price=85.0)

        fixed_model = price(fixed_security.to_instrument_spec(), MarketState(valuation_date=valuation_date, discount_curve=curve, quote_source="TEST"))
        zero_model = price(zero_security.to_instrument_spec(), MarketState(valuation_date=valuation_date, discount_curve=curve, quote_source="TEST"))
        fixed_quote = BondMarketQuote(security_id="912TESTFIX", valuation_date=valuation_date, observed_price=fixed_model.clean_price,
            price_type="clean", quote_source="TEST", currency="USD")
        zero_quote = BondMarketQuote(security_id="912TESTZRO", valuation_date=valuation_date, observed_price=zero_model.dirty_price,
            price_type="dirty", quote_source="TEST", currency="USD")

        fixed_row = validate_bond_quote(security=fixed_security, quote=fixed_quote, curve=curve, tolerance=1e-10)
        rows = validate_bond_quotes(securities=[fixed_security, zero_security], quotes=[fixed_quote, zero_quote], curve=curve, tolerance=1e-10)

        self.assertEqual(fixed_row.validation_status, "PASS_TOLERANCE")
        self.assertEqual([row.validation_status for row in rows], ["PASS_TOLERANCE", "PASS_TOLERANCE"])
        self.assertEqual(rows[0].price_type, "clean")
        self.assertEqual(rows[1].price_type, "dirty")

    def test_security_master_and_quote_csv_loaders_support_validation(self) -> None:
        curve = ZeroCurve(maturities=[0.1, 1.0, 3.0, 5.0, 10.0], zero_rates=[0.04, 0.041, 0.042, 0.043, 0.044])
        valuation_date = date(2026, 6, 25)
        security = SecurityMasterRecord(security_id="912CSVFIX", instrument_type="fixed_coupon_bond",
            issue_date=date(2024, 2, 15), maturity_date=date(2034, 2, 15), face_value=100.0, frequency=2, currency="USD", coupon_rate=0.045)
        model = price(security.to_instrument_spec(), MarketState(valuation_date=valuation_date, discount_curve=curve, quote_source="CSV_TEST"))

        with TemporaryDirectory() as tmp:
            security_path = Path(tmp) / "security_master.csv"
            quote_path = Path(tmp) / "quotes.csv"
            security_path.write_text(
                "security_id,id_type,instrument_type,issue_date,maturity_date,coupon_rate,face_value,frequency,currency,day_count,discount_day_count,business_day_convention,date_generation_rule\n"
                "912CSVFIX,CUSIP,fixed_coupon_bond,2024-02-15,2034-02-15,4.5,100,2,USD,ACT/ACT ICMA,ACT/365F,UNADJUSTED,BACKWARD\n",
                encoding="utf-8")
            quote_path.write_text(
                "security_id,valuation_date,clean_price,quote_source,price_type,currency\n"
                f"912CSVFIX,2026-06-25,{model.clean_price},CSV_TEST,clean,USD\n",
                encoding="utf-8")

            securities = load_security_master_from_csv(security_path)
            quotes = load_bond_market_quotes_from_csv(quote_path)
            rows = validate_bond_quotes(securities=securities, quotes=quotes, curve=curve, tolerance=1e-10)

        self.assertEqual(len(securities), 1)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(rows[0].validation_status, "PASS_TOLERANCE")
