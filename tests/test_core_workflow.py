import unittest
from datetime import date
from bond_pricing import DateAwareFixedCouponBond
from main import parse_date, parse_rate
from market_data_loader import TreasuryCurveSnapshot
from risk_analytics import key_rate_dv01_rows
from validation_reports import calibration_report_rows, clean_dirty_accrued_reconciliation_rows
from yield_curve import ZeroCurve


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


if __name__ == "__main__":
    unittest.main()
