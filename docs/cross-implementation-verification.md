# Cross-implementation verification

A campaign page says "this implementation is faster than that one". That sentence is only worth
something if both implementations were shown to compute **the same thing**. Today every campaign
gets that from a comparison *inside one run*: the suite carries two rows and the harness compares
them. Once a table's rows are independent implementations - einsum against OMEinsum.jl, tlinalg
against torch - the rows live in different processes and that comparison does not exist.

This document is the contract for the substitute. It is what the Julia arms
(`cpueinsum-benchmark#3`) and the torch arm (`tlinalg-benchmark#17`) will emit, and what
`campaign-toolkit#29` implements.

## The five columns

Each arm computes these from its **own output buffer**, outside the timed region, and writes them
to `verify-*.csv` (never to the timing CSV):

| column | definition | what it catches | what it misses |
| --- | --- | --- | --- |
| `sum` | `Σ x_i` | missing terms, scale errors, NaN/Inf | any permutation of the output |
| `sum_sq` | `Σ x_i²` (Frobenius² for a matrix) | the same, without cancellation | any permutation |
| `max_abs` | `max |x_i|` | uninitialised or extreme entries | small errors |
| `weighted` | `Σ w_i x_i` | **element position**: transposes, stride and padding bugs | - |
| `status` | `ok` / `skipped: <reason>` | a case that did not run | - |

`w_i` is a fixed weight in `[0, 1)` derived from the **linear index** `i` in row-major order:

```
h = splitmix64(i * 0x9E3779B97F4A7C15)
w = (h >> 11) * 2^-53
```

The constants and the shift are part of the contract: every arm must produce the same `w_i` for
the same `i`, or the comparison is meaningless. Position matters here precisely because these
benchmarks are about strides, layouts, batch axes and transposes - a plain `sum` is invariant
under exactly the mistakes the benchmarks are looking for.

## Agreement

```
|a - b| / max(|a|, |b|, 1) <= 1e-9      for each of the four values
```

Any disagreement fails the verify, and the recorder names the value and the cell. The `1` in the
denominator keeps a near-zero value from making the tolerance infinite.

## The full comparison, for small outputs

Four scalars are a diagnostic, not a proof: an error that preserves them all (a permutation that
happens to preserve the weighted sum, a sign pattern) passes. So for outputs below a declared size
the arms additionally dump the full tensor in **row-major order** and compare it entry by entry at
`rtol = 1e-8`, `atol = 1e-10`.

`run.yaml` records what was done:

```yaml
verification:
  diagnostics: [sum, sum_sq, max_abs, weighted]
  tolerance: 1.0e-9
  full_tensor_below: 65536
```

## What a page may claim

* every timed cell matched a successful verification census **within its own arm** - the existing
  claim, unchanged;
* a page may add that an **independent implementation** agreed on the four diagnostics only when
  the other arm's values were actually compared. The page must not say "verified against
  OMEinsum.jl" when OMEinsum's output was never checked;
* when a case used the full comparison rather than the scalars, the page says so per cell rather
  than in a footnote.

## Why not a hash

Julia, Rust and torch do not produce bit-identical floats for the same contraction, so a hash of
the output is either too strict (exact bytes) or fragile (quantised bytes near a rounding
boundary). Sums with a relative tolerance are order-independent, which is the property a
cross-language comparison needs.

## Test the contract itself

`tests/test_toolkit.py` carries a unit test: two fake arms whose outputs differ by a permutation
must agree on `sum` and `sum_sq` and disagree on `weighted`. A contract that cannot fail is not a
check.

## The verify grid (#28)

A verify recomputes every case it covers, so its cost is a real constraint on a publication: at
batch 1024 one shape took forty-five minutes on this host, which is longer than the timing pass it
guards and enough to make a whole campaign's shape grid look unaffordable. A suite can therefore
declare the grid its verify runs on, separately from the grid it times:

```yaml
verify:
  n: ["4", "8", "16"]
```

The block **narrows** the declared grid - the recorder refuses a value the declaration itself does
not cover - and the validator compares the census with the cells the run claims to have checked
rather than with the whole timing grid. Only the shape axis can be narrowed: a timed cell at a
budget the verify skipped would be an unchecked number on a page, so `verify.threads` is refused
outright. A population the block does not name (a canary with one shape) keeps its own. `run.yaml` records the grid under
`verification.grid`, so a page can say what was checked instead of implying it checked everything.
Leaving the block out keeps the old behaviour, which is what every campaign did before.
