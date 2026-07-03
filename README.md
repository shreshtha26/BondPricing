# Derivatives Fixed-Income Analytics

This is an early version of a fixed-income analytics project built in Python.
The project currently focuses on US Treasury market data. It pulls Treasury yield data from FRED, builds a zero curve, 
calculates discount factors and forward rates, and prices a fixed-coupon bond using proper date and cashflow handling.

It also generates basic reports so the results can be reviewed easily.
The project includes some additional experimental modules for working with Treasury bill/note/bond prices 
and SOFR/OIS-style curve inputs.

## What It Does

The main workflow is:

```text
FRED Treasury CMT data
  -> Treasury par-style yield snapshot
  -> par-yield bootstrap
  -> discount factors
  -> zero / spot curve
  -> forward rates
  -> bond pricing and risk
  -> CSV reports and interactive chart
```

In this project, `spot rate` and `zero rate` mean the same thing.

The additional curve paths are:

```text
Treasury bill/note/bond market prices
  -> clean/dirty price conversion
  -> instrument cashflows
  -> price-based Treasury bootstrap
  -> Treasury zero curve

SOFR overnight fixing + OIS par fixed rates
  -> OIS fixed-leg schedules
  -> OIS discount-factor bootstrap
  -> collateralized SOFR/OIS zero curve
```

## How To Run

Run the full live workflow:

```bash
python main.py
```

Useful options:

```bash
python main.py --date 2026-06-25
python main.py --refresh-cache
python main.py --output-dir outputs
python main.py --coupon-rate 0.0475
python main.py --issue-date 2024-02-15 --maturity-date 2034-02-15
```

Run optional instrument-level Treasury bootstrapping:

```bash
python main.py --treasury-instruments-csv path/to/treasury_quotes.csv
```

Run optional SOFR/OIS bootstrapping:

```bash
python main.py --ois-quotes-csv path/to/ois_quotes.csv --sofr-rate 5.25
```

If `--sofr-rate` is omitted, the workflow tries to load the SOFR fixing from
FRED for `--sofr-date` or the settlement date.

## Outputs

The workflow writes generated files into `outputs/`:

```text
outputs/curve_plot.html
outputs/curve_report.csv
outputs/bond_report.csv
outputs/bond_cashflows.csv
outputs/calibration_report.csv
outputs/key_rate_dv01_report.csv
outputs/price_reconciliation_report.csv
outputs/treasury_instrument_curve_report.csv
outputs/sofr_ois_curve_report.csv
outputs/run.log
```

FRED CSV files are cached in:

```text
data/fred_cache/
```

## Main Files

- `int_rate_convention.py`: day counts, business-day rolling, schedules,
  compounding, discount factors, present value helpers.
- `market_calendar.py`: reusable market calendars, settlement-date helpers,
  and CSV-loaded holiday overlays for vendor or firm-maintained exceptions.
- `market_data_loader.py`: live FRED loading, local caching, Treasury curve
  snapshots, provenance, live chart creation.
- `bootstrapping.py`: par-yield bootstrap into discount factors and zero rates,
  plus interactive chart rendering.
- `yield_curve.py`: `ZeroCurve`, interpolation, discount factors, forward rates,
  implied par yields, cashflow pricing, curve bumps.
- `bond_pricing.py`: simple and date-aware fixed-coupon bond pricing, accrued
  interest, clean/dirty prices, duration, convexity, DV01.
- `risk_analytics.py`: key-rate DV01 and curve shock risk reports.
- `validation_reports.py`: calibration report rows and clean/dirty/accrued
  reconciliation checks.
- `treasury_instruments.py`: Treasury bill/note/bond instrument objects with
  bill price conversion, accrued interest, dirty price, and future cashflows.
- `treasury_curve_builder.py`: instrument-level Treasury bootstrapping from
  actual bill prices/discount yields and note/bond clean prices.
- `sofr_ois.py`: SOFR fixing loading, OIS quote loading, and SOFR/OIS curve
  bootstrapping.
- `main.py`: command-line workflow that ties everything together.
- `MODEL_ASSUMPTIONS.md`: model assumptions, calibration logic, risk
  definitions, and known limitations.
- `tests/`: unit tests for parsing, calibration, key-rate DV01, and price
  reconciliation.

## Tests

Run the unit tests:

```bash
python -m unittest discover
```

## CSV Input Schemas

Treasury instrument CSV:

```text
instrument_type,issue_date,maturity_date,price,discount_yield,coupon_rate,clean_price,face_value,frequency
BILL,2026-05-28,2026-07-28,99.70,,,,100,
NOTE,2024-08-15,2028-08-15,,,0.040,99.20,100,2
BOND,2024-02-15,2036-02-15,,,0.045,99.10,100,2
```

For bills, provide either `price` or `discount_yield`. For notes and bonds,
provide `coupon_rate` and `clean_price`. Rates can be decimals such as `0.045`
or percentages such as `4.5`.

OIS quote CSV:

```text
tenor_months,maturity_date,fixed_rate,fixed_leg_frequency,fixed_leg_day_count
1,,0.0410,1,ACT/360
12,,0.0385,1,ACT/360
24,,0.0375,1,ACT/360
```

Provide either `tenor_months` or `maturity_date`.

## Financial Assumptions

- FRED Treasury CMT rates are treated as Treasury par-style yields.
- The bootstrapped zero curve uses continuously compounded zero rates.
- The bootstrap uses linear interpolation on zero rates.
- Endpoint extrapolation is only used where code explicitly opts into it.
- The default live chart is based on fitted FRED CMT yields. Instrument-level
  Treasury bootstrapping is available when actual bill/note/bond quotes are
  supplied by CSV.
- Treasury notes and bonds are bootstrapped from dirty price, where dirty price
  equals quoted clean price plus accrued interest.
- The SOFR/OIS curve uses the par OIS equation
  `fixed_rate * fixed_leg_annuity = 1 - final_discount_factor`.
- Key-rate DV01 bumps one zero-curve node at a time and reprices the bond with
  a central difference.
- The calibration report compares market par yields with par yields implied by
  the bootstrapped curve.
- The price reconciliation report checks `dirty price = clean price + accrued`.
- Holiday calendars include rule-based US government securities/New York bank
  full-day holidays and support CSV overlays for vendor-maintained exceptions.

## Current Limitations

This is an industry-style first version, not a production trading system.

