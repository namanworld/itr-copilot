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
- an unknown or misspelled field is present;
- a rate or amount is not a string parseable as `Decimal` (no JSON floats, per ADR 0001);
- amounts are negative, rates fall outside `0`–`1`, or rates decrease across bands;
- slab or surcharge bands are empty, invalid, non-contiguous, or not open-ended at the top;
- surcharge bands are defined separately for each tax regime;
- 80D age-based category limits and the preventive-check-up sublimit are explicit;
- HRA identifies its three-limit formula, salary components, and metro cities;
- the 87A rebate exceeds its income threshold, or deduction codes are duplicated;
- a source ID is missing, empty, or absent from the source catalog.

The calculation code reads only the domain model, never raw JSON. Rules and nested values carry `source_ids` that refer to `corpus/sources.csv`, since JSON has no comments. The catalog records each source's title, URL, retrieval date, local evidence path, governing Act or Rule, document type, and verification notes. Store concise relevant excerpts or access-attempt notes in `corpus/raw/`; never mark a blocked or redirected source as downloaded or verified.

Ruled out: year branches in code, per-year code modules, and a database.

### Adding a new year

1. Copy the previous year's file and rename it for the new year.
2. Update the figures from the Finance Act and set `source_ids`.
3. Add or update the source catalog rows and save dated source excerpts in `corpus/raw/`. Record failed access attempts explicitly; they are not source evidence.
4. Run the loader; fix any validation errors.
5. Add worked test cases for the new year, ideally checked against the department's calculator or a published example.
6. If the year introduces something structural (a new regime, a new kind of relief), the JSON alone cannot express it: extend the domain model and the calculation code first, in a separate change.

## Consequences

- A Budget that only changes numbers is a data-only change that is easy to review.
- Structural changes still need code, and the model's schema grows over time. Older files must keep validating, so new fields need defaults or a schema version.
- Validation catches malformed files, not wrong figures. Correctness of the numbers still depends on per-year test cases.
