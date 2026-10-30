# ADR 0004: Form 16 versus TaxInput

Status: Proposed

## Context

From Day 3 the engine's input will come from an employee's Form 16 instead of being typed by hand. Day 3 builds synthetic Form 16 PDFs, each with a truth JSON of what a perfect extraction returns. Day 4 extracts fields from the PDFs, and Day 10 measures extraction accuracy field by field against the truth JSON.

The form is Form No. 16 under rule 31(1)(a) of the Income-tax Rules, 1962. It has two parts:

- **Part A**: certificate under section 203. It identifies the employer and employee (names, addresses, PANs, TAN), gives the assessment year and the period of employment, and lists the TDS deducted and deposited each quarter.
- **Part B, Annexure-I**: the employer's computation of salary income for employees taxed under section 192. Annexure-II is a separate layout for specified senior citizens under section 194P.

The version in force for FY 2025-26 (AY 2026-27) is the one under the 1961 Act. Notification 45/2026 (30 March 2026) replaced ITR-1 and ITR-4 for AY 2026-27 and did not change Form 16.

`TaxInput` (`src/itr/domain/inputs.py`) is what `compute` needs. It and Form 16 differ in five ways, each with a financial risk if handled carelessly:

1. **Facts that are not on Form 16 at all.** Basic pay, dearness allowance, HRA received, rent paid, the city of the rented home, savings and other interest, the 80D split between self/family and parents, and the senior-citizen flags. Part B shows only the *results* the employer worked out from some of these, such as the HRA exemption under 10(13A).
2. **Items on Form 16 that `TaxInput` cannot represent.** Other section 10 exemptions (LTA, gratuity, commuted pension, leave encashment, 10(14) allowances), entertainment allowance under 16(ii), 80CCC, 80CCD(1), 80CCD(1B), 80CCD(2), 80E, 80CCH, 80G, other Chapter VI-A deductions, relief under section 89, and salary from other employers. Ignoring a non-zero amount on any of these lines produces a wrong tax figure: usually too high, and for 80CCD(2) a wrong regime recommendation, because 80CCD(2) is allowed in both regimes.
3. **Form 16 is computed under one regime only.** Part B line A says whether the employee opted out of the new regime under section 115BAC(1A). On a new-regime Form 16, the employer does not apply HRA, professional tax, 80C, 80D or 80TTA, so those lines are typically 0 even when the employee paid rent, professional tax or insurance premiums. Copying those zeros into the old-regime computation would overstate old-regime tax and make `compare` favour the new regime when it may not be cheaper.
4. **Form 16 reflects what the employee declared and proved to the employer.** The employee can claim more, or less, when filing the return. Form 16 figures are a starting point, not the final claim.
5. **Some Form 16 figures look like `TaxInput` fields but mean something else.**
   - Part A's employee address is not necessarily where the rented home is.
   - Part B 7(a) is a net figure from house property that can be negative, and does not say whether the property is self-occupied or let out.
   - Part B 7(b) is total income from other sources, not split into savings interest and other interest, so 80TTA cannot be worked out from it.
   - Part B 10(g) is one 80D figure, not split by category.

The HRA exemption is the clearest case of difference 1. `hra_exemption` needs basic pay with qualifying DA, HRA received, rent and city to apply the least-of-three rule, and Form 16 gives only the employer's answer. The exemption also applies only for the period the rented home is occupied, and Rule 2A works it out period by period, so a single annual figure is wrong when rent, city or salary changed during the year. (Rule 2A's text has not been retrieved yet; see `corpus/raw/2026-10-29-hra-rule-2a-access.md`.)

## Options considered

For the HRA inputs specifically:

- **(a) Use the employer's HRA exemption as given.** Simplest. But `TaxInput` has no field for an exemption amount, the engine would stop computing HRA, and any employer error (stale rent declaration, missing receipt, mid-year move) would be copied silently. On a new-regime Form 16 the figure is 0, so the old-regime comparison would lose HRA entirely.
- **(b) Ask the user for rent and city.** Unavoidable for those two facts, since only the employee knows them. On its own it leaves basic pay, DA and HRA received missing.
- **(c) Include a salary breakup annexure in the synthetic Form 16, and ask the user for rent and city.** Many employers attach a breakup listing basic, DA, HRA received and other allowances. It is not part of the statutory form, so real Form 16s may not have one.

For Form 16 lines that `TaxInput` cannot represent (difference 2):

- **(d) Ignore them.** Rejected: silently produces wrong tax.
- **(e) Pass the employer's figures through as opaque deductions.** Rejected for now: several of these have their own limits and regime rules (for example 80CCD(1) is limited to a share of salary, and 80CCD(2) is allowed in the new regime), which would then go unchecked.
- **(f) Refuse to compute, and say which line caused it.** Chosen until each item is modelled properly.

## Decision

### 1. Two models, one pure mapping

- A new domain model, **`Form16`**, holds exactly what is printed on the PDF, line by line, in Form 16's own terms. The truth JSON for each synthetic PDF is a serialized `Form16`. It contains nothing the PDF does not show.
- A separate model, **`UserSupplied`**, holds what the user tells the tool: rent, city, interest, the 80D split, senior-citizen flags, residency, age band, and confirmations.
- A pure domain function, **`to_tax_input(form16, user) -> MappingResult`**, combines the two. It returns either a `TaxInput` or nothing, plus a list of **findings**. It reads no files and makes no network calls, like the rest of the domain.
- Extraction (Day 4) is infrastructure: it turns a PDF into a `Form16`. Accuracy is measured on `Form16`, never on `TaxInput`, so a user-supplied fact is never counted as an extraction error.

### 2. Findings have two severities

- **block**: the tool cannot compute a correct result. `to_tax_input` returns no `TaxInput`, and `compute` does not run. Used whenever a non-zero amount appears on a line the engine cannot represent, or a required fact is missing.
- **warn**: the computation runs, but a cross-check failed or a figure needs the user's attention. Each warning shows the Form 16 line, the Form 16 figure and the engine's figure.

Every finding names its Form 16 line (for example `Part B 2(e)`), so the user can find it on their own document.

A cross-check compares the engine's figure, rounded to whole rupees for the comparison only, with the Form 16 figure. Any difference of ₹1 or more is a warning. This rounding is for comparison and never changes a computed value; ADR 0002's rounding points are unaffected.

### 3. Where each `TaxInput` field comes from

| `TaxInput` field | Source | Notes |
|---|---|---|
| `salary.gross` | Part B 1(d) | Must equal 1(a) + 1(b) + 1(c). Includes perquisites, which are taxable in both regimes. |
| `salary.basic` | Salary breakup annexure, else user | |
| `salary.dearness_allowance` | Annexure, else user | Only DA forming part of retirement benefits. If the annexure does not say which DA qualifies, the user confirms. |
| `salary.hra_received` | Annexure, else user | |
| `salary.other_allowances` | Annexure, else user | |
| `rent_paid` | User | Never inferred. |
| `city` | User | Part A's address is never used as the rent city. |
| `professional_tax` | Part B 4(c) if the Form 16 is old-regime; user if new-regime | A new-regime Form 16 may show 0 because 16(iii) is not allowed there. |
| `self_occupied_home_loan_interest` | User | Pre-filled from Part B 7(a) only when 7(a) is negative, as −7(a), for the user to confirm. |
| `savings_interest`, `other_interest` | User | Bank interest is outside Form 16. |
| `deductions.section_80c` | Part B 10(a), gross amount column, if old-regime; user if new-regime | The user may change it; a figure above Form 16 gives a warning to keep proof. |
| `deductions.section_80d_self_and_family`, `section_80d_parents` | User splits Part B 10(g) | Pre-filled with the whole 10(g) amount in self/family, which the user must confirm or split. |
| `self_or_family_has_senior_citizen`, `parents_include_senior_citizen` | User | Not on Form 16. |

### 4. HRA: option (c) plus (b), with a cross-check

- Basic pay, qualifying DA and HRA received come from the salary breakup annexure. Every synthetic Form 16 that has HRA includes one. When a real Form 16 has no annexure, the user supplies these from a payslip.
- Rent and city come from the user.
- The engine computes the exemption with `hra_exemption`, as now.
- On an old-regime Form 16, Part B 2(e) is compared with the engine's figure. A mismatch is a warning that shows both figures. When the engine's figure is higher than the employer's, the warning also says that the employer allowed less and that rent receipts will be needed to support the higher claim.
- The user is asked whether rent, city and salary stayed the same all year. If not, that is a **block**: period-wise HRA under Rule 2A is not supported yet.

### 5. Interest is always user input

Part B 7(b) can show other-source income the employee reported to the employer, but not its split. The user supplies savings and other interest. If their total is less than 7(b), that is a warning, because it suggests unreported income.

### 6. The regime on the Form 16 changes what can be trusted

Part B line A records the regime the employer used for TDS. The engine still computes both regimes, and the employee may choose either when filing.

- **Old-regime Form 16** (line A is YES): the HRA, professional tax and Chapter VI-A figures are the employer's old-regime view. They pre-fill inputs and serve as cross-checks.
- **New-regime Form 16** (line A is NO): those lines are not evidence of what the employee paid. The user is asked for professional tax, 80C, 80D, rent and city before the old-regime computation runs. If they decline, the old-regime result is shown with a warning that it excludes old-regime deductions.
- Cross-checks of income and tax totals (lines 6, 9, 12 and 13 to 17) run only against the engine's computation for the regime the Form 16 used.
- Part B 4(a) must match the standard deduction for that regime (₹75,000 new, ₹50,000 old, capped at salary). A mismatch is a warning that line A may have been read wrongly.

### 7. Blocking cases for this phase

`to_tax_input` blocks when any of these holds:

- a non-zero amount on Part B 1(e), 2(a), 2(b), 2(c), 2(d), 2(f), 2(g) or 2(h), 4(b), 10(b), 10(c), 10(e), 10(f), 10(h), 10(i), 10(j), 10(k), 10(m) or 18;
- Part B 7(a) is positive (income from a let-out property; only self-occupied interest is supported);
- the document is Annexure-II (specified senior citizen under section 194P);
- more than one Form 16 is supplied for the year;
- the assessment year on Part A differs from the rules file's `assessment_year`;
- the user says they are not resident (87A is for residents only, and non-resident rules are not modelled);
- the user is 60 or older (only the below-60 old-regime slabs are in the rules file, and 80TTB is not modelled);
- rent, city or salary changed during the year while HRA is claimed;
- a required user fact is missing (for example HRA received is non-zero but rent or city is absent).

Each block is lifted only by modelling the item, in its own change with its own tests and rules data.

### 8. Arithmetic checks on the extracted Form 16

These catch both extraction errors and inconsistent documents, and each gives a warning when it fails:

- 1(d) = 1(a) + 1(b) + 1(c)
- 2(h) = sum of the clause-wise lines under 2(g)
- 2(i) = 2(a) + 2(b) + 2(c) + 2(d) + 2(e) + 2(f) + 2(h)
- 3 = 1(d) − 2(i)
- 5 = 4(a) + 4(b) + 4(c)
- 6 = 3 + 1(e) − 5
- 8 = 7(a) + 7(b)
- 9 = 6 + 8
- 10(d) = 10(a) + 10(b) + 10(c), using the deductible column
- 10(n) = sum of the section-wise lines under 10(m)
- 11 = 10(d) + 10(e) + 10(f) + 10(g) + 10(h) + 10(i) + 10(j) + 10(k) + 10(l) + 10(n), all from the deductible column
- 12 = 9 − 11
- 17 = 13 − 14 + 15 + 16

The salary breakup annexure must add up to no more than 1(a). This is the same rule `SalaryInput` already enforces against gross.

### 9. The synthetic dataset

- Each case has three files: the PDF, `truth.json` (a serialized `Form16`) and `user.json` (a serialized `UserSupplied`). Evaluation of extraction uses only the PDF and `truth.json`.
- All amounts in both JSON files are strings, per ADR 0001.
- 7(a) is the only amount that may be negative.
- Every page carries a visible "SYNTHETIC SPECIMEN" watermark.
- Names, addresses, PANs and TANs are generated and fictional. PANs and TANs follow the real format so that extraction is realistic. They are not checked against real numbers, so a collision cannot be ruled out, and the dataset is never published outside the repository.
- The cases cover, at minimum:
  - both values of line A;
  - Form 16s with and without a salary breakup annexure;
  - a negative 7(a);
  - a non-zero 7(b);
  - an 80D amount;
  - a 2(e) that matches the engine and one that does not;
  - at least one case for each blocking line in section 7;
  - at least one PDF whose figures were computed independently of this engine, such as the five golden scenarios, so the dataset is not checked only against itself.

## Ruled out

- Using the employer's HRA exemption in place of computing it (option a).
- Ignoring Form 16 lines that `TaxInput` cannot represent (option d), or passing them through unchecked (option e).
- Treating new-regime Form 16 zeros as the employee's old-regime facts.
- Using Part A's address as the HRA city.
- Splitting Part B 10(g) or 7(b) automatically without the user.
- Putting user-supplied facts into the truth JSON.

## Consequences

- The tool asks the user more questions, especially for a new-regime Form 16. That is the cost of not guessing.
- Many real Form 16s will block until more items are modelled. Each block names its line, so it is clear what to build next. The lines most likely to appear in practice are 10(f) 80CCD(2), 2(a) LTA and 10(e) 80CCD(1B), so they are the first candidates.
- `Form16`, `UserSupplied`, `to_tax_input` and the findings type are new domain code. Each blocking rule and each arithmetic check needs its own test.
- `TaxInput` itself does not change for now. Residency and age are asked so that unsupported cases can block, but they are not yet `TaxInput` fields.
- Day 10's per-field accuracy is measured on `Form16` fields only. A separate end-to-end check can run `to_tax_input` and `compute` on extracted data, but it measures something different.
- The Form 16 layout should be added to `corpus/sources.csv` (for example as `FORM-16-RULE-31`) with an excerpt in `corpus/raw/`, like the other sources.

## Appendix A: every line of Form 16 Part B (Annexure-I)

"Engine figure" means the matching line of `Computation` for the regime the Form 16 used.

| Line | Item | Use |
|---|---|---|
| A | Opting out of 115BAC(1A) (YES/NO) | Decides which cross-checks apply and whether old-regime facts must be asked (section 6). |
| 1(a) | Salary as per 17(1) | Part of gross. Upper bound for the annexure total. |
| 1(b) | Perquisites, 17(2) | Part of gross. |
| 1(c) | Profits in lieu of salary, 17(3) | Part of gross. |
| 1(d) | Total | `salary.gross`. Arithmetic check. |
| 1(e) | Salary from other employers | Block if non-zero. |
| 2(a) | Leave travel concession, 10(5) | Block if non-zero (old regime only). |
| 2(b) | Gratuity, 10(10) | Block if non-zero (allowed in both regimes). |
| 2(c) | Commuted pension, 10(10A) | Block if non-zero (both regimes). |
| 2(d) | Leave encashment, 10(10AA) | Block if non-zero (both regimes). |
| 2(e) | HRA, 10(13A) | Cross-check against the engine's HRA exemption, old-regime Form 16 only. Never an input. |
| 2(f) | Special allowances, 10(14) | Block if non-zero. |
| 2(g), 2(h) | Other section 10 exemptions, and their total | Block if non-zero. |
| 2(i) | Total section 10 exemption | Arithmetic check. |
| 3 | Salary from current employer | Arithmetic check. |
| 4(a) | Standard deduction, 16(ia) | Cross-check, and confirms line A (section 6). |
| 4(b) | Entertainment allowance, 16(ii) | Block if non-zero. |
| 4(c) | Tax on employment, 16(iii) | `professional_tax` if old-regime; otherwise ask. |
| 5 | Total under section 16 | Arithmetic check. |
| 6 | Income under "Salaries" | Arithmetic check. Cross-check against the engine's income from salary. |
| 7(a) | House property offered for TDS | Positive: block. Negative: pre-fill self-occupied interest for confirmation. |
| 7(b) | Other sources offered for TDS | Warn if the user's interest total is less. |
| 8 | Total other income reported | Arithmetic check. |
| 9 | Gross total income | Arithmetic check. Cross-check against the engine's gross total income. |
| 10(a) | 80C | `deductions.section_80c` (gross column) if old-regime; otherwise ask. |
| 10(b) | 80CCC | Block if non-zero. |
| 10(c) | 80CCD(1) | Block if non-zero. |
| 10(d) | Total of 80C, 80CCC, 80CCD(1) | Arithmetic check. |
| 10(e) | 80CCD(1B) | Block if non-zero. |
| 10(f) | 80CCD(2), employer's contribution | Block if non-zero (allowed in both regimes). |
| 10(g) | 80D | User splits it (section 3). Warn if the split total differs. |
| 10(h) | 80E | Block if non-zero. |
| 10(i), 10(j) | 80CCH, employee and Central Government | Block if non-zero. |
| 10(k) | 80G | Block if non-zero. |
| 10(l) | 80TTA | Cross-check only. The engine works 80TTA out from savings interest. |
| 10(m), 10(n) | Other Chapter VI-A, and their total | Block if non-zero. |
| 11 | Aggregate Chapter VI-A | Arithmetic check. Cross-check against the engine's total deductions. |
| 12 | Total taxable income | Cross-check against the engine's total income. |
| 13 | Tax on total income | Cross-check. |
| 14 | Rebate under 87A | Cross-check. |
| 15 | Surcharge | Cross-check. |
| 16 | Health and education cess | Cross-check. |
| 17 | Tax payable | Cross-check against the engine's tax payable. |
| 18 | Relief under section 89 | Block if non-zero. |
| 19, 20 | TDS and TCS as per Form 12BAA | Extracted and kept for a later slice on balance payable. Not used by `compute`. |
| 21 | Net tax payable | Extracted, not used. |

## Appendix B: Part A and the annexure

| Item | Use |
|---|---|
| Employer and employee names and addresses | Extracted. Never used by `compute`. The address is never used as the HRA city. |
| PAN of the employee, PAN and TAN of the deductor | Extracted and format-checked. Personal data: not logged, not sent anywhere the user has not chosen. |
| Assessment year | Must match the rules file, else block. |
| Period with the employer | Extracted. A part-year period is allowed; the standard deduction is still applied once, capped at salary. |
| Quarterly TDS deducted and deposited, receipt numbers, challan details | Extracted for the later balance-payable slice. Not used by `compute`. |
| Salary breakup annexure (basic, DA and whether it counts for retirement, HRA received, other allowances) | Not statutory. When present, it fills the `salary` fields; when absent, the user supplies them. |

## Appendix C: facts `compute` depends on that Form 16 never carries

- Rent paid, and whether it stayed the same all year.
- City of the rented home.
- Savings interest and other interest, separately.
- The 80D split between self/family and parents.
- Whether anyone in each 80D category is a senior citizen.
- Residential status.
- Age (whether 60 or older).
- Self-occupied home-loan interest when the employee did not report it to the employer.
- On a new-regime Form 16: professional tax paid, 80C and 80D claims.

## Sources

- Form No. 16 under rule 31(1)(a), Income-tax Rules, 1962: https://www.incometaxindia.gov.in/documents/d/guest/103120000000007849-pdf-5
- Notification 45/2026 (G.S.R. 226(E), 30 March 2026), which replaced ITR-1 and ITR-4 for AY 2026-27 and left Form 16 unchanged: https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-04/Notification%20No.45_2026.pdf
- Section 115BAC, sub-section (2), the exemptions and deductions not allowed in the new regime: https://www.incometaxindia.gov.in/w/section-115bac-4
- HRA exemption under 10(13A), including that it applies only for the period the rented home is occupied: https://wm.incometaxindia.gov.in/w/deductions/allowances-allowed-to-a-salaried-employee