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

It returns model price, observed price, clean/dirty split, accrued interest, and a validation status.

## Curve

- FRED Treasury CMT rates are treated as par-style Treasury yields.
- The FRED workflow does not use CMT rates as zero rates directly.
- Par-style yields are bootstrapped into discount factors and continuously compounded zero rates.
- Interpolation is linear on zero rates.
- Extrapolation is disabled unless a function explicitly allows endpoint extrapolation.

## Bond Pricing

- Fixed-coupon bonds use dated coupon schedules.
- Dirty price is the present value of remaining cashflows.
- Clean price is dirty price minus accrued interest.
- Accrued interest is calculated from the previous coupon date to settlement using the bond day-count convention.
- Zero-coupon bonds discount the maturity principal.
- If a zero-coupon bond has an `issue_price`, accrued interest is reported as constant-yield accretion from issue date to valuation date.

## Quote Validation

- Runtime bond terms and quotes must come from supplied CSV files.
- Sample CSVs are allowed for demos because they are explicit inputs, not hardcoded bonds inside the engine.
- The validation residual is `model_price - market_price`.
- A row passes if the residual is inside the configured tolerance or, when bid/ask are supplied, the model price falls inside the bid/ask range.

## Risk And Explain

- Parallel DV01 bumps every zero-rate node by one basis point.
- Key-rate DV01 bumps one quoted zero-rate node at a time.
- DV01 is reported as the price gain for a one basis point rate decrease.
- P&L explain is first-order: it compares actual price movement with DV01-based curve-move estimates.

## Data Quality

- Quote source, quote type, currency, bid, ask, timestamp, stale flag, and override flag are part of the market-data layer.
- The first version records these fields, but it does not yet enforce full vendor-style data governance.
- Missing FRED observations are dropped, and the curve loader requires a complete tenor set for the selected date.

## Limits

- Precision target is desk-style approximation, not audit-grade production valuation.
- The built-in calendars are compact rule-based calendars, not vendor-certified holiday and early-close calendars.
- TRACE, EMMA, Bloomberg-style, broker-run, and evaluated-price feeds are not integrated directly yet.
- Callable, puttable, FRN, TIPS, credit-risky, IR derivative, option, and securitized-product models remain future layers.
