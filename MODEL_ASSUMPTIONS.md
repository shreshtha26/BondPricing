# Model Assumptions

This document records the main modeling choices used by the first fixed-income workflow.

## Treasury CMT Curve

- FRED Treasury CMT quotes are treated as par-style Treasury yields.
- The live FRED workflow uses those par-style yields as inputs, not as zero rates directly.
- Par yields are bootstrapped into discount factors and continuously compounded zero rates.
- The zero curve uses linear interpolation on zero rates.
- Extrapolation is disabled unless a function explicitly opts into endpoint extrapolation.

## Bond Pricing

- The main example bond is a fixed-coupon, date-aware bond.
- Coupon schedules are generated from issue date, maturity date, coupon frequency, and business-day convention.
- Dirty price is the present value of remaining cashflows.
- Clean price equals dirty price minus accrued interest.
- Accrued interest is calculated from the previous coupon date to settlement using the bond day-count convention.

## Risk

- Parallel curve DV01 bumps every zero-rate node by one basis point.
- Key-rate DV01 bumps one quoted zero-rate node at a time by one basis point.
- Key-rate DV01 is calculated with a central difference: price down one basis point minus price up one basis point, divided by two.

## Calibration

- The calibration report compares market par yields with par yields implied by the bootstrapped zero curve.
- Residuals are reported both in rate units and basis points.
- Small residuals indicate the bootstrap is internally consistent with the input market quotes.

## SOFR/OIS

- The SOFR/OIS module treats SOFR as the overnight starting point for collateralized USD discounting.
- OIS quotes are treated as par fixed rates.
- The first-version OIS equation is: fixed rate times fixed-leg annuity equals one minus the final discount factor.

## Known Limitations

- FRED CMT data is fitted curve data, not a full instrument-level Treasury security feed.
- Holiday calendars cover core full-day rules and support CSV overlays, but they are not vendor-certified early-close calendars.
- Formal market data vendor integration is outside this first version.
