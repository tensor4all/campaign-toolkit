# Corpus suites: a short design for tcontract, cpueinsum and the org's other corpus campaigns

## Revised plan (what to do instead)

**Try a thin generated-population adapter before adding a workload grammar.** A corpus campaign keeps
its executable corpus where it already lives (the measured checkout's `corpus.rs`), and a campaign-side
generator writes a *grid-shaped* suite from it:

- the immutable case identity **including the selected path/size variant** goes into the `family` slot,
  because the census's declaration comparison strips `note` - a variant that lives only in `note` can
  disappear from the declared grid without the evidence noticing;
- shapes are emitted as explicit one/two/three-dimension triples where the corpus has genuine matrix
  geometry (and a square `gemm` case keeps its `m x m x m` triple, because the toolkit derives `k = m`
  for that family);
- a whole-program case (the MPS chain: 65 inputs, 64 steps, one timed call) has no equivalent matrix
  shape, so it is emitted with a documented opaque/sentinel shape - the page then says the case is a
  program, not a matrix, and the definition stays in the corpus identified by the recorded revision;
- the adapter emits the toolkit's CSV and census and reuses `report_run`/publication; it does **not**
  keep its own Markdown reporting.

**Admission stays fail-closed**: a requested engine whose build feature is missing (TBLIS) is a
build/admission failure, not an automatic `skipped` row - otherwise a page could claim full coverage
with no independent reference. Only a structurally unsupported case is a declared skip.

**Provenance**: reuse the existing `engines`/`providers` mapping and the campaigns' own TBLIS
collector (tag, commit, bundled BLIS revision, artifact hash); do not add a second engine taxonomy, and
make the provider schema accept the fields the collector already records.

**What this costs, honestly**: the adapter work is small, but every corpus campaign also needs its
harness to speak the v2 contract first (chronological samples, census/status columns, `key=value`
`info`). `tcbench`/`lukbench` today keep best/worst only, print a human-readable `info` and write their
own seconds/GFLOP/s CSV; cpueinsum needs a new harness on top of that (instance loader, path
conversion, reference adapter, verification). No delivery estimate is claimed until those contracts are
settled.

**Fallback if the sentinel-shape compromise is unacceptable**: implement only a *small opaque-case
census extension* - declared case identities and variants, with the executable definition still owned
by the campaign - rather than a generic tensor-program grammar in the toolkit.

### What the pre-review found (blockers that shaped the revision)

1. **The proposed `cases:` record does not describe the real workload.** `per-shape` fixes each case's
   dtype (the harness filters by it), has three-input/two-step cases and 65-input/64-step MPS chains
   whose CSV dimensions are the *final* step's, and `tcbench` resolves its extents from a 16 MiB suite
   knob against a 200 MiB harness default with rank-dependent rounding. A case list works as a census
   of opaque identities, not as their definition.
2. **"The fail-closed checks work unchanged" was false.** The declaration comparison strips `note`, so
   declared *paths* represented only as notes can vanish from both the census and the timing evidence
   without failing; the identity contract must carry declared variants explicitly.
3. The einsum mapping was underspecified (per-instance dtype, the column-major equation/shape pair, and
   the SSA path with `num_inputs - 1` steps that `cpueinsum-rs` requires rather than an `opt_einsum`
   shrink list).
4. Changing the grammar touches the recorder, the suite and manifest schemas (which reject unknown
   fields), coverage and publication, and `jsonschema_lite` supports no `oneOf` - and `per-shape`
   declares a second runner (`lukbench`) that the descriptor's single `measured.binary` cannot select.
5. TBLIS needs *external provenance*, not a new classification: a non-vendor provider is currently
   attributed to the measured revision, `provider_packages` reads Cargo.lock (useless for a native
   install), and the provider schema rejects the `tag`/`sha256` the campaign already records.

### The proposal that was reviewed (kept as the record)

Status: **revised after an independent pre-review (2026-10-17); nothing implemented.** The review
rejected the grammar extension proposed below as the first move and named the two blockers that decide
the shape of the real change. Read this section first: it is the plan; the original proposal is kept
underneath as the record of what was rejected and why.

## What the current grammar covers, and what it cannot

The toolkit models a suite as `populations` of `families` (library API families such as `gemm`, `lu`,
`qr_factor`) over a **shape grid** of `n` strings, with `dtype`, `order` and `batch` as global axes.
`expected_cells` derives the complete cell grid from that declaration, and every campaign that fits it
now works: `faer-benchmark` and `tlinalg-benchmark` both record v2 evidence and publish pages.

Three of the org's campaigns do not fit, and they do not fit for the same reason: their workload is a
**corpus of named cases**, each with its own operands, not a family over a shared shape grid.

| campaign | workload | why the grid model fails |
| --- | --- | --- |
| `tcontract-benchmark` (`tcbench`) | the TCCG 49-case contraction corpus at one nominal size and two widths | a case is an equation over rank-3..6 tensors with its own extents; `sizes_mib` is a knob, not a shape |
| `tcontract-benchmark` (`per-shape`) | 21 named contraction cases with fixed extents | "the cases *are* the shapes", in its own words |
| cpueinsum (new) | the einsum-benchmark instance set (168 problems, 7 categories) | each instance carries its own operand shapes and contraction paths |

The instance schema is already portable and small - this is one real file from
`tenferro-einsum-benchmark/data/instances/bin_matmul_256.json`:

```json
{
  "name": "bin_matmul_256",
  "format_string": "ij,jk->ik",
  "shapes": [[256, 256], [256, 256]],
  "dtype": "float64",
  "num_tensors": 2,
  "paths": {"opt_size": {"path": [[0, 1]], "log2_size": 16.0, "log10_flops": 7.2247},
            "opt_flops": {"path": [[0, 1]], "log2_size": 16.0, "log10_flops": 7.2247}},
  "format_string_colmajor": "ij,jk->ik",
  "shapes_colmajor": [[256, 256], [256, 256]]
}
```

`tcontract`'s harnesses are also corpora already: `tcbench` and `lukbench` each own a `corpus.rs`, an
`engines/` directory (`blas.rs`, `blas.rs`, `tblis.rs`, `ttgt.rs`) and their own `report.rs`, which is
exactly the duplicated reporting the toolkit exists to remove.


## Concrete integration contract: `tcbench` today (surveyed 2026-10-17)

`tcontract-rs` at the campaign's pin `8abfa38` carries the harness as plain modules beside a `main.rs`
(`benchmarks/benchmarks/tcbench/{main.rs,corpus.rs,report.rs,tblis.rs,ttgt.rs,engines/}`). Its CLI is
`tcbench <run|verify|info>` with `--threads`, `--size <MiB>` (default 200), `--reps` (5), `--prime-ms`
(1500, already time-based), `--dtype`, `--case <substr>`, `--engines plan,packed,ttgt,tblis`, `--csv`,
`--stress`. This table is what the next implementation needs; nothing here is done yet.

| the toolkit sends | `tcbench` today | decision for the adapter + harness |
| --- | --- | --- |
| `--threads N` | ✓ same flag, enforced host budget (`BenchThreads`) | as is |
| `--n LIST` | ✗ only `--size <MiB>`; the corpus resolves extents from that knob | the suite declares the *size spec* in `n` (e.g. `16`) and the harness accepts `--n` as that spec, resolving it through the existing corpus sizing; the harness's own default (200 MiB) stays its default when the flag is absent |
| `--batch LIST` | ✗ unit batch | accept it and require `1` (the corpus has unit batch) |
| `--family LIST` | ✗ only `--case <substr>` | `--family` is the list of **case ids** (exact match), because the case identity has to be in `family` for the census; `--case` stays as the diagnostic substring filter |
| `--regime R` | ✗ | accept it; it is the CSV/census label |
| `--rows LIST` | ✗ engine rows are `plan,packed,ttgt,tblis` | accept it, filter to those rows and order them as given |
| `--rotate-rows K` | ✗ | accept it; rotate the declared rows per process start (the v2 census and report depend on it) |
| `--statistic best\|median` | ✗ best/worst only, no chronological samples | keep every call, report best or median of them |
| `verify --csv PATH` census | ✗ `--csv` exists only for `run`, and the CSV is seconds/GFLOP/s with diagnostics | write the census: one row per declared cell with `status`/`note`, including declared skips, and the same rotation the run uses |
| `info` | prints a human-readable environment | add the recorder's `key=value` contract (provider/vendor identity, thread readback), keeping the environment text as extra lines |
| `--dtype` | ✓ same flag (f32,f64,c32,c64) | as is; the campaign's dtypes decide the grid |
| verification success line | prints per-case `OK` lines | add the `all comparisons within tolerance` line the recorder requires |

Two policy points from the pre-review are part of this contract: a case whose dtype the corpus fixes is
*filtered* by dtype rather than multiplied into it (the harness already filters), and a requested engine
whose build feature is missing (`tblis`) is an **admission failure**, not a `skipped` row, because
`tcbench` itself rejects that configuration today.

## The extension: `cases` beside `populations`

A suite may declare its workload as cases instead of populations. Nothing else in the contract moves:
the evidence checks (chronological samples, census, complete process sets), the guard protocol, the
recorder, provenance and publication stay as they are.

```yaml
runs:
  - threads: [1, 4]
    cpu_sets: ["4", "4-7"]
    dtypes: [f64, c64]
    engines: [plan, packed, tblis]
    cases:                       # either inline, or generated from a committed instance set
      - id: bin_matmul_256
        equation: "ij,jk->ik"
        operands: [[256, 256], [256, 256]]
        paths: [opt_flops]        # one row per declared path, like a variant note today
      - id: mps_chain_chi32
        equation: "..."           # whatever the corpus's own definition is
        operands: [[32, 4, 4], [4, 4, 32]]
```

- **Identity**: a cell is `(regime, case id, path, dtype, order, engine row, threads)`. The `case_label`
  template gains `{case}`; the CSV gains a `case` column; `note` keeps carrying a variant (a second
  path, a Householder sequence, and so on).
- **Census**: `expected_cells` derives the grid from the declared cases, so the fail-closed checks work
  unchanged. A case the provider cannot take is a declared `skipped` row, as today.
- **Instance sets**: a campaign whose cases come from `tenferro-einsum-benchmark` (or any other instance
  directory) **generates** its case list from those files into the committed suite, rather than typing
  168 entries; the generator is a script in the campaign and its output is what the declaration gate
  validates. The instance files stay the source of truth for shapes and equations.
- **External engines**: an engine row may declare a provider that is not a Rust crate in the measured
  checkout (`tblis` today). Its provenance block is recorded per run the way `tcontract` already does -
  release tag, commit, bundled BLAS revision, artifact hash - and the page repeats it. This is the same
  shape as the existing `vendor` block, with an extra free-form identity.
- **Feature-gated arms**: a run spec may declare `features: [tblis]`; an engine whose feature is not
  compiled in is emitted as a declared skip rather than silently absent.

## Cost and alternatives

| option | work | consequence |
| --- | --- | --- |
| **A. this design** | one toolkit change (grammar, validator, report, one generator), then the campaigns migrate; ~1-2 days including tests and the two measured records | one system for every campaign the org has; `tcontract`'s own `report.rs` disappears; cpueinsum starts on the same rails |
| **B. per-campaign adapter** | small wrapper per campaign: keep the campaign's own corpus/report code, emit the toolkit's CSV and census, let the toolkit validate and publish | three or four copies of corpus handling stay alive; the evidence contract is shared but the workload model is not |
| **C. defer** | none | `tcontract` and cpueinsum stay outside the system the goal asks for |

Recommendation: **A**, because the corpus shape is the org's common case (four campaigns), and because B
leaves the duplicated reporting that the migration just deleted in `tlinalg`.

## What must not change

- The v2 evidence contract and its fail-closed checks; the idle-guard protocol and the observer rules;
  the frozen timing settings; provenance (library, upstream, harness, toolkit, binary SHA, host block).
- Historical pages: a corpus grammar adds a new declaration shape, it does not reinterpret an existing
  one, and a revised contract still gets a new `-timing-vN` page name.
