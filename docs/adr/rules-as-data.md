# Tax rules as per-year data

## Context

Slab thresholds, rates, surcharge bands, cess, rebate limits and deduction caps change almost every year through the Finance Act. The calculation logic (apply slabs, compute surcharge with marginal relief, add cess) changes much less often. Past years must stay reproducible: a return for an earlier year has to compute exactly as it did then.

## Options considered

1. **Constants in code, branched by year.** `if year >= ...` checks spread through the calculation. Each Budget means editing logic, and old years can break when someone edits a shared branch.
2. **One code module per year.** Isolates years but duplicates the calculation logic, so a bug fix has to be copied into every module.
3. **A database table of rules.** Adds a runtime dependency and a migration process for data that changes once a year and should live in version control.
4. **One JSON file per year, loaded into a validated domain model.** Logic stays in one place; each year's numbers are a reviewable file.

## Decision

Option 4. Each year's rules live in their own JSON file. On load, the file is parsed into the rules domain model, which rejects it if:

- a required field is missing or has the wrong type;
- a rate or amount is not a string parseable as `Decimal` (no JSON floats, per ADR 0001);
- slab thresholds are not ascending and contiguous, or the top slab is not open-ended;
- rates fall outside `0`–`1`.

The calculation code reads only the domain model, never raw JSON. Each file carries a `source` field citing the Finance Act or notification it was taken from, since JSON has no comments.

Ruled out: year branches in code, per-year code modules, and a database.

### Adding a new year

1. Copy the previous year's file and rename it for the new year.
2. Update the figures from the Finance Act and set `source`.
3. Run the loader; fix any validation errors.
4. Add worked test cases for the new year, ideally checked against the department's calculator or a published example.
5. If the year introduces something structural (a new regime, a new kind of relief), the JSON alone cannot express it: extend the domain model and the calculation code first, in a separate change.

## Consequences

- A Budget that only changes numbers is a data-only change that is easy to review.
- Structural changes still need code, and the model's schema grows over time. Older files must keep validating, so new fields need defaults or a schema version.
- Validation catches malformed files, not wrong figures. Correctness of the numbers still depends on per-year test cases.
