# Follow-ups after the timing-v2 rollout (2026-10-17)

The rollout is finished: `faer-benchmark`/`t4a-faer`, `tlinalg-benchmark`, `tcontract-benchmark`
and `cpueinsum-benchmark` all record with the v2 contract and publish pages at their latest measured
commit, and the procedure is reproducible from a fresh session. What follows is what was deliberately
left open, with the file that holds the evidence, so a later session starts from facts instead of
reconstructing them.

## 1. cpueinsum: one corpus instance is excluded from the declaration

`str_mps_varying_inner_product_200` fails the harness's own verification (`opt_flops`, `rel_err = NaN`,
deterministic across thread budgets and row orders; probes show agreement at some extents and `NaN` at
others, so it is not a structural disagreement of the two paths). The declaration therefore covers 13 of
14 instances, and the exclusion is stated in the suite, the page and the campaign docs.

- Evidence: `cpueinsum-benchmark/docs/verify-refusal-str_mps-varying-inner-product-200.md`,
  plus `AGENTS.md`/`README.md`/`benchmarks/suites/*.yaml`/`scripts/gen_suite_from_census.py` there.
- Tracked as **tensor4all/cpueinsum-rs#30**.
- Next: root-cause it in `cpueinsum-rs` (the corpus's own runner generates zero-filled tensors and never
  compared values, so its pipeline could not have seen this). Then delete the exclusion entry and let the
  generator re-declare all 14.

## 2. Toolkit: the recorder cannot pass a harness *input path* — FIXED (2026-10-18)

`cpueinsum-bench` requires `--instances DIR`, and the recorder's harness command line has no field for it.
The campaign works around this with `scripts/harness_launcher.sh`, which adds the flag and is what
`measured.binary` names - so `binary.sha256` is the launcher's, with the cargo artifact's hash one file
away in `binary-info.txt`.

- **Done:** the descriptor field `measured.harness_args` now exists (campaign-toolkit #23, with the test
  fix in #24), the campaign declares `--instances <corpus>` there, `scripts/harness_launcher.sh` is
  deleted, the build script installs the artifact itself, and the campaign was re-recorded (5m41s,
  `coverage=full`) so `binary.sha256` is the artifact's own hash `08d31475…` - the same hash the launcher
  used to report in `binary-info.txt`.

## 3. faer: a small-shape threshold guard for the adopted row-major work

The acceptance run for the adopted row-major QR/LU change found the 1T target cells clean (8 beyond-band
improvements, 0 regressions) but **43 beyond-band regressions at 4T/8T, all small shapes** (e.g.
`qr_factor 32x32` f64 at 1.83x). The change is merged; the follow-up is the guard.

- Tracked as **tensor4all/faer#18**.
- Evidence: `faer-benchmark/docs/reviews/20261016-acceptance-run.md` (the "next step" line), the run
  dirs it cites, and `faer-benchmark/docs/rules.md` for the acceptance protocol to re-run afterwards.
- Next: add the shape threshold (or route small shapes to the previous path), then re-accept.

## 4. tlinalg: an optional further trim

The suite was trimmed on 2026-10-17 to the batched small regime plus three canaries (5,004 rows per arm,
about 40-45 minutes each, down from 13,392 rows and 114/108 minutes). If it should be cheaper still, the
`heavy` group (svd_full/eig/eigvals/full_piv_lu/full_piv_lu_solve) is the next cut, about -25%; it costs
the batched behaviour of the expensive decompositions at tiny sizes.

- Evidence: `tlinalg-benchmark/benchmarks/suites/tlinalg-kernels.yaml` (the trim comment and the
  updated blind spots) and `result/zen5-cpu/tlinalg-kernels/*-timing-v2.md`.

## 5. tcbench and lukbench disagree about unknown options — FIXED (2026-10-17)

`lukbench` refuses an unknown option with exit 2; the merged `tcbench` still only warns and continues. The
fail-closed rule says they should agree.

- **Done:** `tcontract-rs` #130 makes `tcbench` exit 2 with `unknown option …`, matching `lukbench`;
  verified with fmt/clippy/tests and the binary itself. No evidence-format change, so recorded pages stay
  valid.
