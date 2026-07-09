# BondPricing

BondPricing is a compact fixed-income valuation and quote-validation engine.

The first workflow answers one practical question:

> Given bond terms, an observed quote, and a selected curve, what is the model price, what is the residual, and what explains it?

The current build is focused on fixed-coupon and zero-coupon bonds. It loads a Treasury curve, bootstraps discount factors and zero rates, prices supplied bond records, and compares model clean/dirty prices with observed quotes.

The project is intentionally written for explanation. A result should be traceable from:

```text
market data -> curve -> cashflows -> accrued interest -> price -> risk -> residual
```

## Current Workflow

```text
FRED Treasury CMT data
  -> Treasury par-style yield snapshot
  -> par-yield bootstrap
  -> discount factors and zero rates
  -> calibration report
  -> optional fixed/zero bond quote validation from CSV
```

Optional curve paths are also available:

```text
Treasury bill/note/bond quotes
  -> clean/dirty conversion
  -> instrument cashflows
  -> price-based Treasury zero curve

SOFR fixing + OIS par rates
  -> OIS fixed-leg schedules
  -> SOFR/OIS discount curve
```

In this repo, `spot rate` and `zero rate` mean the same thing.

## Quick Start

Install dependencies with Poetry:

```bash
poetry install
```

Build the Treasury CMT-based zero curve:

```bash
poetry run python main.py
```

Run a specific curve date:

```bash
poetry run python main.py --date 2024-06-25
```

Run the fixed/zero bond validation path using explicit sample CSV files:

```bash
poetry run python main.py \
  --date 2024-06-25 \
  --security-master-csv examples/security_master_sample.csv \
  --bond-quotes-csv examples/bond_quotes_sample.csv
```

Those sample files are not hidden defaults. They are ordinary input files that exercise the same ingestion path used for real broker, vendor, TRACE-style, or internally prepared data.

Choose the curve used for validation:

```bash
poetry run python main.py \
  --date 2024-06-25 \
  --security-master-csv examples/security_master_sample.csv \
  --bond-quotes-csv examples/bond_quotes_sample.csv \
  --validation-curve cmt
```

Available validation curves:

```text
cmt                  FRED Treasury CMT bootstrapped zero curve
treasury-instruments Actual Treasury bill/note/bond price bootstrap, requires --treasury-instruments-csv
sofr-ois             SOFR/OIS discount curve, requires --ois-quotes-csv
```

Apply an explicit constant spread while pricing the validation file:

```bash
poetry run python main.py \
  --date 2024-06-25 \
  --security-master-csv examples/security_master_sample.csv \
  --bond-quotes-csv examples/bond_quotes_sample.csv \
  --validation-z-spread-bp 75
```

With a supplied spread, the validation report shows both the base-curve model price and the spread-adjusted model price.

## Real Data Inputs

For quote validation, provide both files:

```bash
poetry run python main.py \
  --security-master-csv data/security_master.csv \
  --bond-quotes-csv data/bond_quotes.csv
```

The engine does not create a default bond inside `main.py`. Bond terms and observed prices must come from files you supply.

For real CUSIP/ISIN validation, the first source of truth is still a supplied file. TRACE, EMMA, Bloomberg-style exports, broker runs, and internal marks should be normalized into the security-master and quote CSV schemas. Direct licensed/live connectors are intentionally not hidden inside the workflow.

Optional Treasury instrument bootstrapping:

```bash
poetry run python main.py --treasury-instruments-csv data/treasury_quotes.csv
```

Optional SOFR/OIS bootstrapping:

```bash
poetry run python main.py --ois-quotes-csv data/ois_quotes.csv --sofr-rate 5.25
```

If `--sofr-rate` is omitted, the workflow tries to load SOFR from FRED for `--sofr-date` or the settlement date.

## Sample Output

The sample validation run writes `outputs/bond_quote_validation_report.csv`. A passing run looks like this:

| security_id | curve_name | curve_date | price_type | market_price | model_price | residual | yield_error_bp | z_spread_bp | duration | convexity | parallel_dv01 | explanation |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| SAMPLEFIXED1 | FRED Treasury CMT | 2024-06-25 | clean | 100.10 | 100.1009 | 0.0009 | -0.0135 | 0.0132 | 6.5172 | 47.1881 | 0.0662 | PASS_TOLERANCE |
| SAMPLEZERO1 | FRED Treasury CMT | 2024-06-25 | dirty | 85.73 | 85.7275 | -0.0025 | 0.0845 | -0.0827 | 3.5589 | 12.6658 | 0.0305 | PASS_TOLERANCE |

The same run also writes `outputs/bond_quote_validation_summary.csv`:

| valuation_date | curve_name | total_bonds | passed | failed | pass_rate | max_abs_price_error | max_duration | max_convexity | total_parallel_dv01 | data_quality_issue_count | stale_quote_count | largest_residual_security_id |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2024-06-25 | FRED Treasury CMT | 2 | 2 | 0 | 1.00 | 0.0025 | 6.5172 | 47.1881 | 0.0967 | 0 | 0 | SAMPLEZERO1 |

The residual is:

```text
model_price - market_price
```

A positive residual means the model price is above the observed quote. A negative residual means the model price is below the observed quote.

## Interpreting Residuals

The first version explains a residual through the fields it can observe directly:

- `curve_name`, `curve_source`, `curve_date`, `curve_build_method`, `curve_role`, and `discount_curve_used` show which curve produced the model price.
- `curve_calibration_status` and `curve_calibration_residual_bp` show whether that curve reproduced its own input market quotes.
- `clean_price`, `dirty_price`, and `accrued_interest` show whether the difference is coming from quote convention or settlement accrual.
- `price_type` shows whether the market quote was compared on a clean or dirty basis.
- `base_model_price`, `pricing_z_spread_bp`, and `spread_price_impact` show whether an explicit spread was applied to the selected curve before pricing.
- `market_implied_yield`, `model_implied_yield`, and `yield_error_bp` translate the price residual into flat-yield terms.
- `z_spread` and `z_spread_bp` show the constant spread over the selected zero curve that would match the observed dirty price.
- `parallel_dv01` shows the dirty-price sensitivity to a one basis point parallel curve move.
- `effective_duration` and `effective_convexity` are central-difference curve risk measures from the same parallel zero-curve bump used for DV01.
- `quote_source`, `bid`, `ask`, and `tolerance` show whether the quote is a point estimate, a range, or a tolerance-based validation.
- `quote_type`, `quote_timestamp`, `quote_age_days`, `quote_stale`, `quote_evaluated`, `quote_traded`, `quote_override`, `clean_dirty_mismatch`, and `data_quality_flags` explain whether the market quote itself is clean enough to trust.
- `convention_level` and `convention_warnings` state that this is desk-style convention handling, not audit-grade vendor settlement logic.
- The curve report and calibration report show whether the selected Treasury curve is internally consistent with the bootstrapped zero curve.

Large residuals are not automatically errors. They may come from curve choice, stale quotes, missing credit spread, liquidity premium, tax treatment, embedded optionality, or security terms that do not match the supplied CSV.

If `--validation-z-spread-bp` is supplied and the spread-adjusted model price misses the observed quote, the residual label can be `APPLIED_SPREAD_PRICING_EFFECT`. That means the residual is coming from the explicit spread assumption, not from the base curve alone.

## Outputs

Generated files are written to `outputs/`:

```text
curve_report.csv
calibration_report.csv
bond_quote_validation_report.csv
bond_quote_validation_summary.csv
treasury_instrument_curve_report.csv
sofr_ois_curve_report.csv
run.log
```

FRED downloads are cached in:

```text
data/fred_cache/
```

## Main Files

- `main.py`: command-line workflow for curve building and quote validation.
- `pricing.py`: fixed-coupon and zero-coupon pricing types plus the compact `price(...)` API.
- `curves.py`: zero curves, discount factors, forwards, par-yield bootstrapping, and root solving.
- `conventions.py`: day counts, business-day rolling, schedules, and discount factors.
- `market_data.py`: FRED loading, CSV loaders, market-data containers, and curve metadata.
- `analytics.py`: calibration rows, quote-validation rows, risk rows, valuation snapshots, and P&L explain.
- `treasury.py`: price-based Treasury bootstrapping from bill/note/bond CSV inputs.
- `rates.py`: SOFR fixing loading, OIS quote loading, and SOFR/OIS curve bootstrapping.
- `backtesting/`: historical valuation and P&L explain helpers.
- `tests/`: deterministic tests using synthetic fixtures.

## CSV Schemas

Security master:

```text
security_id,id_type,issuer,instrument_type,issue_date,maturity_date,coupon_rate,face_value,frequency,currency,issue_price,day_count,discount_day_count,business_day_convention,date_generation_rule,end_of_month
SAMPLEFIXED1,SAMPLE,Demo issuer,fixed_coupon_bond,2022-02-15,2032-02-15,4.25,100,2,USD,,ACT/ACT ICMA,ACT/365F,UNADJUSTED,BACKWARD,false
SAMPLEZERO1,SAMPLE,Demo issuer,zero_coupon_bond,2023-01-15,2028-01-15,,100,2,USD,82.50,ACT/ACT ICMA,ACT/365F,UNADJUSTED,BACKWARD,false
```

Bond quotes:

```text
security_id,valuation_date,clean_price,dirty_price,bid,ask,quote_source,price_type,quote_type,timestamp,stale_flag,override_flag,source_system,source_record_id,currency
SAMPLEFIXED1,2024-06-25,100.10,,,,SAMPLE_FILE,clean,market_price,,,,,USD
SAMPLEZERO1,2024-06-25,,85.73,,,SAMPLE_FILE,dirty,market_price,,,,,USD
```

Treasury instrument quotes:

```text
instrument_type,issue_date,maturity_date,price,discount_yield,coupon_rate,clean_price,face_value,frequency
BILL,2024-05-28,2024-07-25,99.70,,,,100,
NOTE,2022-08-15,2028-08-15,,,4.00,99.20,100,2
BOND,2020-02-15,2034-02-15,,,4.50,99.10,100,2
```

OIS quotes:

```text
tenor_months,maturity_date,fixed_rate,fixed_leg_frequency,fixed_leg_day_count
1,,0.0410,1,ACT/360
12,,0.0385,1,ACT/360
24,,0.0375,1,ACT/360
```

Rates can be supplied as decimals such as `0.045` or percentages such as `4.5`.

## Financial Assumptions

- FRED Treasury CMT rates are treated as Treasury par-style yields.
- The CMT workflow bootstraps continuously compounded zero rates.
- Curve interpolation is linear on zero rates.
- Endpoint extrapolation is only used where the code explicitly opts into it.
- Fixed-coupon bonds use generated coupon schedules, accrued interest, clean price, and dirty price.
- Zero-coupon bonds discount principal and, when `issue_price` is supplied, report simple constant-yield accretion as accrued interest.
- The quote-validation report compares supplied observed clean/dirty prices with model clean/dirty prices.
- The validation curve can be selected with `--validation-curve cmt`, `--validation-curve treasury-instruments`, or `--validation-curve sofr-ois`.
- `--validation-z-spread-bp` applies a constant spread to the selected curve before model pricing.
- Holiday calendars are compact rule-based US government securities/New York bank calendars, not vendor-certified calendars.

## Current Limits

This is a desk-style approximation engine, not an audit-grade valuation library.

The main gaps are:

- No licensed CUSIP/ISIN terms feed is included.
- TRACE, EMMA, Bloomberg, broker-run, and evaluated-price data must be supplied as files for now.
- Callable, puttable, floating-rate, inflation-linked, credit-risky, option, swap, and securitized-product logic are planned layers, not the first completed workflow.
- Data-quality diagnostics flag timestamp gaps, stale quotes, evaluated/traded quote type, overrides, bid/ask availability, and suspicious clean/dirty mismatches.

## Roadmap

The build order is deliberately layered:

1. Fixed-coupon and zero-coupon bond quote validation.
2. Yield, spread, Z-spread, duration, convexity, and DV01 diagnostics.
3. Floating-rate note pricing.
4. Inflation-linked bond pricing.
5. Callable and puttable bond logic.
6. Credit-risky bond pricing.
7. OIS, swaps, and multi-curve rate workflows.
8. Caps, floors, swaptions, and other fixed-income options.
9. Securitized-product cashflows, prepayment, and waterfall logic.

## Tests

Run the test suite:

```bash
poetry run python -m unittest discover -s tests
```
