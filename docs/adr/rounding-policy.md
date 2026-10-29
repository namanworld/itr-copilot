# Rounding policy (sections 288A and 288B)

## Context

Two provisions of the Income-tax Act, 1961 govern rounding:

- **288A**: total income is rounded to the nearest multiple of ₹10. Paise are ignored first; then if the last digit is 5 or more the amount goes up to the next multiple of ten, otherwise down.
- **288B**: any amount payable, and any refund due, is rounded the same way.

The law says what gets rounded. It does not say intermediate figures (heads of income, deductions, slab-wise tax, surcharge, cess) should be rounded, and rounding them changes the final answer. Rounding each slab's tax, then the surcharge, then the cess, can drift a few rupees from the correct figure.

## Options considered

1. **Round every line to ₹10.** Matches nothing in the Act and compounds error.
2. **Round every line to the nearest rupee.** Common in spreadsheets, but still introduces drift and has no basis in 288A/288B.
3. **Round only at the two points the Act names**, keep everything else at full `Decimal` precision.

## Decision

Option 3. Rounding happens in exactly two places:

1. **Total income → 288A.** After all heads are aggregated and deductions applied, total income is rounded once. Tax is then computed on the rounded figure.
2. **Tax liability and balance payable/refund → 288B.** Slab tax, surcharge, marginal relief, rebate and cess are computed at full precision on the rounded total income. The resulting liability is rounded once. If prepaid taxes (TDS, advance tax) are subtracted, the resulting balance payable or refund is rounded again under 288B.

Each provision is one function, implemented literally: truncate paise, then round half-up to tens on the rupee figure. For a refund, rounding applies to the absolute amount and the sign is restored afterwards.

Intermediate lines are not rounded. Ruled out: per-line rounding to ₹10 and per-line rounding to ₹1.

## Consequences

- Displayed intermediate lines, if shown in whole rupees, may not add up exactly to the totals. Reports should show paise or note that totals are computed from unrounded figures.
- Any export that requires whole-rupee figures (for example, ITR form fields) rounds in the exporter, not in the core.
- Tests need cases at the boundary: last digit 4 vs 5, amounts with paise like `x4.99`, and refunds.
- Section numbers are those of the 1961 Act. If the engine supports tax years under the Income-tax Act, 2025, the equivalent provisions should be cited in that year's rules file (ADR 0003) rather than hard-coded here.
