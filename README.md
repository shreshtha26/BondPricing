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

## Real Data Inputs

For quote validation, provide both files:

```bash
poetry run python main.py \
  --security-master-csv data/security_master.csv \
  --bond-quotes-csv data/bond_quotes.csv
```

The engine does not create a default bond inside `main.py`. Bond terms and observed prices must come from files you supply.

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

| security_id | price_type | market_price | model_price | residual | accrued_interest | status |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| SAMPLEFIXED1 | clean | 100.10 | 100.1009 | 0.0009 | 1.5295 | PASS_TOLERANCE |
| SAMPLEZERO1 | dirty | 85.73 | 85.7275 | -0.0025 | 4.7100 | PASS_TOLERANCE |

The residual is:

```text
model_price - market_price
```

A positive residual means the model price is above the observed quote. A negative residual means the model price is below the observed quote.

## Interpreting Residuals

The first version explains a residual through the fields it can observe directly:

- `clean_price`, `dirty_price`, and `accrued_interest` show whether the difference is coming from quote convention or settlement accrual.
- `price_type` shows whether the market quote was compared on a clean or dirty basis.
- `quote_source`, `bid`, `ask`, and `tolerance` show whether the quote is a point estimate, a range, or a tolerance-based validation.
- The curve report and calibration report show whether the selected Treasury curve is internally consistent with the bootstrapped zero curve.

Large residuals are not automatically errors. They may come from curve choice, stale quotes, missing credit spread, liquidity premium, tax treatment, embedded optionality, or security terms that do not match the supplied CSV.

## Outputs

Generated files are written to `outputs/`:

```text
curve_report.csv
calibration_report.csv
bond_quote_validation_report.csv
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
security_id,valuation_date,clean_price,dirty_price,bid,ask,quote_source,price_type,currency
SAMPLEFIXED1,2024-06-25,100.10,,,,SAMPLE_FILE,clean,USD
SAMPLEZERO1,2024-06-25,,85.73,,,SAMPLE_FILE,dirty,USD
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
- Holiday calendars are compact rule-based US government securities/New York bank calendars, not vendor-certified calendars.

## Current Limits

This is a desk-style approximation engine, not an audit-grade valuation library.

The main gaps are:

- No licensed CUSIP/ISIN terms feed is included.
- TRACE, EMMA, Bloomberg, broker-run, and evaluated-price data must be supplied as files for now.
- Callable, puttable, floating-rate, inflation-linked, credit-risky, option, swap, and securitized-product logic are planned layers, not the first completed workflow.
- Data-quality diagnostics are basic: quote source, currency, bid/ask, tolerance, and model-vs-market residual.

## Roadmap

The build order is deliberately layered:

1. Fixed-coupon and zero-coupon bond quote validation.
2. Yield, spread, Z-spread, and DV01 diagnostics.
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
