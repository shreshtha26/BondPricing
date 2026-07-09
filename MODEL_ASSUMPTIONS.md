# Model Assumptions

This file is the model contract for the current version of BondPricing. It records what the engine assumes today, so a quote-validation result can be explained without guessing what the code did.

## Product Decision

The first complete workflow is fixed-coupon and zero-coupon bond quote validation.

The model takes:

- supplied bond terms,
- an observed market quote,
- a selected curve,
- convention settings,
- and a tolerance.

It returns model price, observed price, curve metadata, clean/dirty split, accrued interest, implied yield diagnostics, Z-spread, duration, convexity, DV01, a residual explanation label, and a validation status.

## Curve

- FRED Treasury CMT rates are treated as par-style Treasury yields.
- The FRED workflow does not use CMT rates as zero rates directly.
- Par-style yields are bootstrapped into discount factors and continuously compounded zero rates.
- Bond quote validation can use the FRED CMT zero curve, a Treasury instrument price-based zero curve, or a SOFR/OIS curve when the matching market inputs are supplied.
- Interpolation is linear on zero rates.
- Extrapolation is disabled unless a function explicitly allows endpoint extrapolation.

## Bond Pricing

- Fixed-coupon bonds use dated coupon schedules.
- Dirty price is the present value of remaining cashflows.
- Clean price is dirty price minus accrued interest.
- Accrued interest is calculated from the previous coupon date to settlement using the bond day-count convention.
- Zero-coupon bonds discount the maturity principal.
- If a zero-coupon bond has an `issue_price`, accrued interest is reported as constant-yield accretion from issue date to valuation date.
- A supplied validation Z-spread is applied as a constant additive spread to every zero-rate node before pricing.
- Pricing diagnostics preserve the base-curve price, the spread-adjusted price, the applied spread in basis points, and the price impact of the spread.

## Quote Validation

- Runtime bond terms and quotes must come from supplied CSV files.
- Sample CSVs are allowed for demos because they are explicit inputs, not hardcoded bonds inside the engine.
- The validation residual is `model_price - market_price`.
- The validation row records the curve name, source, type, date, build method, role, and discount curve used.
- Curve calibration status and maximum absolute calibration residual are reported with the bond validation row.
- Market-implied yield is the flat yield that reproduces the observed dirty price.
- Model-implied yield is the flat yield that reproduces the model dirty price.
- Z-spread is the constant spread over the selected zero curve that reproduces the observed dirty price.
- The applied validation spread and the solved market-implied Z-spread are separate fields. The applied spread changes model price; the solved Z-spread explains the observed market quote.
- If an applied validation spread creates a material model-vs-market residual, the residual explanation can report `APPLIED_SPREAD_PRICING_EFFECT`.
- A row passes if the residual is inside the configured tolerance or, when bid/ask are supplied, the model price falls inside the bid/ask range.
- The file-level validation summary rolls up total bonds, pass/fail count, pass rate, maximum absolute price/yield/Z-spread residuals, maximum duration and convexity, total parallel DV01, largest residual security, and basic residual explanation counts.

## Risk And Explain

- Parallel DV01 bumps every zero-rate node by one basis point.
- Key-rate DV01 bumps one quoted zero-rate node at a time.
- DV01 is reported as the price gain for a one basis point rate decrease.
- Effective duration uses the same parallel curve bump as DV01: `(price_down - price_up) / (2 * dirty_price * bump_size)`.
- Effective convexity uses the same parallel curve bump: `(price_down + price_up - 2 * dirty_price) / (dirty_price * bump_size^2)`.
- P&L explain is first-order: it compares actual price movement with DV01-based curve-move estimates.

## Data Quality

- Quote source, quote type, currency, bid, ask, timestamp, stale flag, and override flag are part of the market-data layer.
- Quote validation flags missing timestamps, missing bid/ask, missing observed prices, stale timestamps, explicit stale flags, override flags, evaluated prices, traded prices, future timestamps, and suspicious clean/dirty mismatches.
- Evaluated quotes are not automatically rejected. They are flagged so residuals can be interpreted differently from traded quotes.
- Direct TRACE, EMMA, Bloomberg, broker-run, and security-master connectors are not embedded. Their exports should be supplied through the CSV input contract unless licensed integrations are added later.
- Missing FRED observations are dropped, and the curve loader requires a complete tenor set for the selected date.

## Limits

- Precision target is desk-style approximation, not audit-grade production valuation.
- The built-in calendars are compact rule-based calendars, not vendor-certified holiday and early-close calendars.
- Validation rows explicitly report `DESK_APPROXIMATION` convention level and warnings for missing vendor-certified calendars, ex-coupon rules, and special odd-coupon handling.
- TRACE, EMMA, Bloomberg-style, broker-run, and evaluated-price feeds are not integrated directly yet.
- Callable, puttable, FRN, TIPS, credit-risky, IR derivative, option, and securitized-product models remain future layers.
