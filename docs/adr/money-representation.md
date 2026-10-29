# Money representation

## Context

The engine computes Indian income tax. Inputs arrive with paise (salary slips, bank interest, TDS certificates). The computation multiplies amounts by fractional rates: slab rates, surcharge, 4% cess, special rates on capital gains. Section 288A and 288B rounding only happens at specific points (see ADR 0002), so every value before those points has to be held exactly.

Binary floats are not an option: `0.1 + 0.2 != 0.3`, and a rupee off in a tax figure is a bug a user will notice and report.

## Options considered

1. **`int` rupees.** Simple, fast, no fractional arithmetic. But paise in the inputs are thrown away at entry, before the law says they may be, and `amount * rate` still produces fractions that need a rounding rule at every multiplication.
2. **`int` paise.** Exact for addition and subtraction, and a common choice for payments systems. But every rate multiplication still yields a fractional paise value, so each call site needs its own rounding decision. The code fills up with `// 100` and `round()` calls that are easy to get subtly wrong.
3. **`Decimal`.** Exact base-10 arithmetic, explicit control of rounding mode and precision, and rates such as `"0.04"` are represented exactly.

## Decision

All monetary amounts and rates are `decimal.Decimal`.

- Amounts and rates are constructed from strings or ints, never from `float`. JSON is loaded with `parse_float=Decimal`.
- Intermediate values are not quantized. Rounding happens only where ADR 0002 says it does.
- Quantizing for display (to paise or whole rupees) is a presentation concern and stays out of the computation code.

Ruled out: `int` rupees, because it discards paise earlier than the law allows; `int` paise, because it pushes a rounding decision onto every rate multiplication; `float`, entirely.

## Consequences

- A single stray `float` breaks exactness silently. Tests should assert that computed values are `Decimal`, and a lint or type check should reject `float` in the domain package.
- Serialization writes amounts as strings so precision survives round trips.
- `Decimal` is slower than `int`. For a per-return calculation this does not matter.
- Comparisons with ints work, but comparisons with floats are a trap; callers must convert at the boundary.
