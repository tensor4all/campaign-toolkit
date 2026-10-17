## Findings by severity

Read-only review completed; no edits, builds, tests, or measurements were run.

Paths below are relative to `campaign-toolkit`. For brevity, **`H/` means `../tcontract-benchmark/extern/tcontract-rs/benchmarks/benchmarks/`**.

### BLOCKER

**1. The proposed case record does not describe the existing per-shape workload.**

The example at `docs/corpus-suites-design.md:54-63` treats dtype as a global product and represents an MPS case with one equation and two operands. The real corpus contradicts both assumptions:

- Each program fixes its dtype: `H/lukbench/corpus.rs:28-39`. The harness **filters cases by their dtype**, rather than running every case at every requested dtype: `H/lukbench/main.rs:154-160`.
- The three-matrix case has three inputs and two explicitly ordered steps: `H/lukbench/corpus.rs:234-240`.
- Each MPS case has **65 inputs and 64 pairwise steps**, with physical dimension **2**, not the example’s two rank-three operands: `H/lukbench/corpus.rs:245-269`.
- Input scaling is part of the fixture, and one timed call executes the whole program: `H/lukbench/corpus.rs:43-50`; `H/lukbench/engines/run.rs:162-203`.

A `cases:` list is sufficient **as a census of opaque workload identities**, but the proposed `equation`/`operands` structure is not sufficient as their definition. Decide which it is. Do not silently replace a whole-chain measurement with one contraction, or manufacture opposite-dtype cases merely to satisfy a Cartesian product.

TCCG has a separate unresolved translation: the suite declares **16 MiB** (`../tcontract-benchmark/benchmarks/suites/tcbench.yaml:82-87`), while the harness defaults to **200 MiB** (`H/tcbench/main.rs:87-89`). Its resolved extents use rank-dependent rounding and a fixed eight-byte sizing element, including for complex runs (`H/tcbench/corpus.rs:39-45,407-432`). No generic size knob is necessary for today’s single-size suite, but the design must say that cases are resolved at 16 MiB and how the harness consumes those exact shapes.

**2. “Fail-closed checks work unchanged” is false; declared path completeness is particularly vulnerable.**

`docs/corpus-suites-design.md:66-70` introduces a different identity while also suggesting that a second path remains in `note`.

Today:

- `BASE` contains neither case nor path; `note` is appended separately: `campaign_toolkit/timing_evidence.py:11-16`.
- The declaration comparison **strips note**: `timing_evidence.py:88-90`.
- Process grouping and rotation use positional tuple fields: `timing_evidence.py:91,106,114-115`.
- Aggregation still requires `batch`: `timing_evidence.py:123-128`.

Consequently, if two declared paths are represented only as notes, removing one path from both verification and timing evidence leaves the same base census. The existing declaration comparison does not establish that both paths were executed. This follows directly from the source; I did not execute a reproducer.

The extension needs one unambiguous identity contract, including declared variants, regime/process grouping, and the meaning of batch. Merely adding a CSV column or modifying `expected_cells` does not preserve the existing guarantees.

### IMPORTANT

**3. The einsum mapping is not yet specified sufficiently to claim coverage of the instance set.**

Even the JSON quoted by the proposal contains information its YAML mapping drops: instance dtype, actual contraction-path steps, and the column-major equation/shape pair (`docs/corpus-suites-design.md:27-36,54-63`).

The cpueinsum API requires actual integer-label inputs and an **SSA path with `num_inputs - 1` steps**. It explicitly distinguishes that representation from an `opt_einsum` shrinking-list positional path: `../cpueinsum-rs/crates/cpueinsum/src/spec.rs:5-20,49-59`.

Thus:

- `paths: [opt_flops]` must resolve to a particular committed sequence, not just a display name.
- C/F variants must select the appropriate equation and operand-axis ordering, not merely receive different `order` labels.
- Instance dtype must be mapped deliberately; globally applying `[f64, c64]` is not equivalent to preserving the quoted `float64` instance.

The full instance checkout is absent locally. I could not verify the 168 count, categories, dtype distribution, operand counts, or whether all paths are binary. The proposal’s single binary-matmul example does not validate those claims.

**4. There are substantial recorder/schema/report changes beyond the proposed census extension.**

The production path assumes populations throughout:

| Boundary | Actual dependency |
|---|---|
| Declaration | Suite schema requires `family_groups`, `workloads`, and run `populations`; rejects unknown fields: `schemas/benchmark-suite.schema.json:6-18,80-87`. |
| Expansion/coverage | `populations`, `same_populations`, and `coverage_of` compare family/shape/batch grids: `campaign_toolkit/campaign.py:52-70,170-200`. |
| Manifest validation | Manifest schema requires populations; declaration validation independently compares them: `schemas/benchmark-run.schema.json:465-488`; `campaign_toolkit/validate_run.py:102-159`. |
| Recorder | Filters populations and invokes `--n --batch --family --rows`; writes populations back into the manifest: `campaign_toolkit/record_run.py:466-486,602-625,627-644,724-734`. |
| Publication | Coverage decides whether a page may replace another: `campaign_toolkit/publish_report.py:54-71`. |

There is also an omitted **runner selection** problem: `per-shape` declares `runner: lukbench` (`../tcontract-benchmark/benchmarks/suites/per-shape.yaml:9`), while toolkit build and execution use one descriptor-wide `measured.binary` (`record_run.py:213-228,557-558`). Adding cases does not select the second harness.

The schema gate needs care too: an ordinary JSON Schema `oneOf` for “cases XOR populations” would not currently be enforced by `jsonschema_lite`, whose supported checks contain no combinator handling (`campaign_toolkit/jsonschema_lite.py:51-108`). Use the existing declaration-check boundary rather than assuming a schema keyword works.

**5. External TBLIS provenance is necessary; a new external-engine classification is not.**

Both tcontract suites explicitly require TBLIS as their independent reference. However, engine-to-provider mappings already accept arbitrary provider strings (`schemas/campaign.schema.json:237-244`); being a Cargo package is not a prerequisite for being an engine.

The actual gaps are narrower:

- A non-vendor provider is currently treated as belonging to the measured revision, with no independent commit: `campaign_toolkit/descriptor.py:206-218`; `campaign_toolkit/record_run.py:356-362`. That would falsely identify TBLIS.
- `provider_packages` reads selected packages from Cargo.lock, so it cannot identify this native installation: `record_run.py:307-339`.
- Reclassifying TBLIS as a `vendor_provider` conflates external provenance with mutually exclusive BLAS/LAPACK symbol ownership and changes arm projection: `schemas/campaign.schema.json:246-257`; `campaign_toolkit/campaign.py:120-137`.
- Conversely, a vendor-free cpueinsum/reference campaign cannot presently pass recorder arm selection: `record_run.py:492-503`.
- Existing tcontract provenance includes `providers[].tag` and `providers[].sha256` (`../tcontract-benchmark/scripts/record_run.py:115-144`), which the toolkit provider schema rejects (`schemas/benchmark-run.schema.json:375-412`).
- Report dependency filtering and the special `lapack-{vendor}` branch do not automatically print TBLIS’s promised identity: `campaign_toolkit/report_run.py:261-262,324-341`.

Preserve the existing TBLIS provenance collector/build recipe. Fix collection, schema compatibility, and presentation; do not introduce a second parallel engine registry or assume a free-form identity is automatically checked.

**6. Skipping a required engine because its feature was not built weakens the campaign.**

The current suites request `features: [tblis]`, and the campaign builder forwards that selection (`../tcontract-benchmark/benchmarks/suites/tcbench.yaml:33-34`; `../tcontract-benchmark/scripts/record_run.py:43-56`).

More importantly, **tcbench deliberately rejects an unavailable requested engine**, explaining that silently missing rows would otherwise produce apparently successful runs (`H/tcbench/main.rs:145-157`). The proposal reverses that policy (`docs/corpus-suites-design.md:79-80`).

Today, `ok` and `skipped` census cells are both accepted, while full coverage is computed from declared axes—not from whether the reference produced measurements (`timing_evidence.py:81-90`; `record_run.py:730-734`). Automatically converting a missing TBLIS build into skips could therefore publish a full-coverage page with no independent reference.

A structurally unsupported case and a missing required build feature are different events. Keep the latter a build/admission failure.

**7. Reporting needs a real identity migration, not just `{case}` support.**

The descriptor already permits arbitrary `case_axes`, and `campaign.case_label` already supplies their values as template tokens (`campaign_toolkit/descriptor.py:184-190`; `campaign_toolkit/campaign.py:161-167`). But **the report table does not use that template**:

- `case_name` hardcodes family/dimensions/batch and, for v2, note: `campaign_toolkit/report_run.py:47-50`.
- `best_per_case` groups using that name: `report_run.py:67-85`.
- `render` iterates populations and filters cases by the family prefix: `report_run.py:385-406`.

Adding a case column without changing those keys can merge distinct corpus operations or omit them from tables.

Also, do not copy tcontract’s existing `notes` directly into toolkit `note`: the former contains engine-specific strategy and numerical error diagnostics (`H/lukbench/engines/run.rs:122,145,157`), whereas the latter is **operation identity**. That would split engines performing the same workload into different report rows and can mismatch verification against timing.

Historical preservation is feasible, but not automatic. Making `case` globally required breaks old CSV readers (`report_run.py:32-41`); rerendering old runs also reads today’s workload/engine prose (`report_run.py:310-327`). Preserve historical pages and old identity handling rather than bulk “upgrading” their evidence.

**8. The 1–2 day estimate is not supported by the current integration state.**

The existing tcontract timer retains only best/worst and scatter, not chronological samples (`H/tcbench/engines/mod.rs:156-173`). Its CSV is seconds/GFLOP/s/diagnostics, without the toolkit census/status/sample columns (`H/tcbench/report.rs:51-79`). Its `info` output is human-readable rather than the recorder’s `key=value` vendor contract (`H/tcbench/report.rs:189-198`; `campaign_toolkit/record_run.py:277-304`).

Both tcontract harnesses therefore need substantive v2 work irrespective of suite grammar. Cpueinsum also needs the new instance loader, path conversion, reference adapter, verification, and recorder-facing harness. Toolkit work does not deliver those.

“Two measured records” additionally obscures that there are two tcontract suites plus the new cpueinsum campaign. This is not a defensible fixed delivery estimate until their scope and executable contracts are settled.

### MINOR

**9. The design misidentifies some existing structure and duplicated work.**

- Batch is population-local, not global: `campaign_toolkit/campaign.py:60-63`, contrary to `docs/corpus-suites-design.md:9-10`.
- Rust `report.rs` includes the evidence serializer and environment output, not just duplicated publication formatting (`H/tcbench/report.rs:51-81`). Moving Markdown publication does not make that entire file unnecessary.
- The implementation uses packaged schemas preferentially (`campaign_toolkit/descriptor.py:79-93`), so changing only the top-level `schemas/` files would miss the active copies under `campaign_toolkit/schemas/`.

## Smaller path and its fidelity cost

The design’s option B is unnecessarily expensive as described: **an adapter need not retain campaign-specific Markdown reporting**. It can retain corpus execution, emit toolkit-compatible evidence, and use the shared report and publication path.

A generated-population adapter deserves a concrete trial before adding a second workload grammar:

- Encode the immutable case identity—including selected path/size variant—in `family`.
- Keep equations, operand layouts and whole-program definitions in the committed corpus identified by the recorded source revision.
- Use genuine equivalent GEMM dimensions where defined.

But the suggested “operand extents in `n`” is **not generally representable**: the schema allows only one to three scalar dimensions, and `expected_cells` interprets them as matrix dimensions (`schemas/benchmark-suite.schema.json:130-135`; `timing_evidence.py:35-43`). Also, renaming square `gemm` to a case ID changes expected `k` from `m` to empty; emit an explicit triple when preserving GEMM geometry.

For chains there is no single equivalent shape—the existing corpus explicitly calls its CSV dimensions the **final step’s** (`H/lukbench/corpus.rs:103-108`). An opaque/sentinel shape encoding could buy common-system pages, but costs self-describing shape fidelity and must not be presented as actual matrix geometry.

If that compromise is unacceptable, retain a **small opaque-case census extension**, not a generic tensor-program grammar. The campaign already owns the executable definitions; the toolkit needs their declared identities, variants, coverage, and evidence—not another implementation of contraction semantics.

## Delete list

1. `docs/corpus-suites-design.md:57-63`: **yagni:** cut toolkit-owned equation/program modelling; reuse committed campaign corpus definitions.
2. `docs/corpus-suites-design.md:66-68`: **shrink:** cut competing path-versus-note identity conventions; use one declared variant identity.
3. `docs/corpus-suites-design.md:75-78`: **reuse:** cut a parallel external-engine taxonomy; reuse `engines`/`providers` and the existing TBLIS provenance collector.
4. `docs/corpus-suites-design.md:79-80`: **delete:** cut automatic skips for missing required build features; retain admission failure.
5. `docs/corpus-suites-design.md:87`: **delete:** cut the requirement that adapters keep their own publication/report implementation; reuse `report_run`.
6. `docs/corpus-suites-design.md:86`: **delete:** cut the unconditional 1–2 day estimate and promise that all of Rust `report.rs` disappears.

**net: approximately −20 proposal lines possible; implementation savings cannot yet be quantified.**

## Unchecked scope

- The actual einsum instance repository is not cloned here; its complete data, generators, paths, layouts, licenses, and categories were not inspected.
- No runtime reproducer, schema validation, tests, builds, measurements, installed TBLIS artifact inspection, or GPU checks were performed.
- No full historical-page/manifest inventory or hosted CI review.
- Reviewed the working-tree proposal and source; did not independently verify the Git object for `c5e586e`.

**Verdict: change the design—try a thin generated-population adapter first; if its fidelity loss is unacceptable, implement only an opaque-case census extension with explicit variants, while preserving required-engine admission and existing provenance checks.**
