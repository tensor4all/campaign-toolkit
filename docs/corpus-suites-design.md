# Corpus suites: a short design for tcontract, cpueinsum and the org's other corpus campaigns

Status: **proposal, not implemented.** Needs a decision because it changes the suite grammar; the
review rule for this kind of change is a short design plus an independent pre-review before the
implementation lands.

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
