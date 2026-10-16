# Migrating an existing campaign onto the toolkit (and starting a new one)

Survey of the sibling campaigns on 2026-10-16, and the steps that bring each of them onto the same
timing-v2 system as `faer-benchmark`. Written so a fresh session can execute it without re-surveying.

## Where the sibling campaigns are today

| campaign | harness | scripts | toolkit | newest page |
| --- | --- | --- | --- | --- |
| `faer-benchmark` | `fbench` **in the campaign**, links both libraries by path | thin shims over `campaign_toolkit` | pinned in `requirements.txt` | 2026-10-16, timing v2 |
| `tlinalg-benchmark` | `tlbench` **in the measured repo** (`tlinalg-rs/crates/tlinalg-bench`), vendor chosen with `BENCH_VENDOR` | its own copies of `record_run.py`, `report_run.py`, `validate_run.py`, `gen_index.py`, `compare_runs.py`, `publish_report.py`, `campaign.py` | none (`requirements.txt` = jsonschema, pyyaml) | 2026-10-11, v1 |
| `tcontract-benchmark` | same shape (`build_for_tprims_rev.sh`, harness in `tcontract-rs`) | same copied scripts, smaller set | none | 2026-10-09, v1 |
| `cpueinsum-rs` | none yet | none | none | none |
| `tenferro-benchmark`, `tenferro-einsum-benchmark`, `strided-rs-benchmark-suite` | exist on GitHub, not cloned here | own pipelines | none | - |

Both sibling campaigns already declare suites in the shape the toolkit's suite schema expects
(`schema_version`, `id`, `invalidated_by`, `stale_after`, `required_profiles`, `family_groups`,
`workloads`, `runs[0].populations`), because they were written from the same template. What they lack is
the descriptor, the toolkit pin, the v2 evidence contract and the measurement preconditions.

## Steps per campaign

1. **Descriptor** `benchmarks/campaign.yaml` (model: `faer-benchmark`): `measured` (name, url, pin file,
   extern dir, build script, binary, features or build env, `build_argv`, `pin_env`, `protocol`),
   `identities`, `profiles_file`, `suites_dir`, `data_dir`, `result_dir`, `engines`, `vendor_providers`,
   `vendor_env`, `csv_columns`, `case_axes`/`case_label`, `page_name` (add `{timing}`), `report`,
   `default_invalidated_by`, `timing: {version: 2}`.
2. **Shims**: replace each copied script with a three-line shim over the toolkit, as
   `faer-benchmark/scripts/*.py` does (`from campaign_toolkit.record_run import main`), and keep the
   campaign's own `benchmarks/scripts/{pinned.sh,idle_cpus.py}` (the toolkit looks for the protocol in
   the measured checkout or the campaign through `descriptor.measured.protocol`).
3. **Pin** the toolkit in `requirements.txt`
   (`-e git+https://github.com/tensor4all/campaign-toolkit@<commit>#egg=campaign-toolkit`).
4. **Admission and measurement**: the toolkit already derives the measured CPU sets from sysfs, refuses
   a build whose path patch was ignored, refuses to record with CPU boost on or with the observer pinned
   onto the measured L3 domains, and prints the page-to-raw-data mapping. Nothing machine-specific has to
   be copied into the campaign; the profile states the machine facts.
5. **Record the latest commit** of the measured library, both arms where the campaign has vendor arms,
   with the frozen contract (`--prime-ms 250 --reps 5 --aa 3 --statistic median`), then publish. The
   pages carry `-timing-v2`, so the v1 history stays where it is.
6. **CI**: declarations, `validate_run.py` over every manifest, `gen_index.py --check`, and the
   measured repo's own tests.
7. **PR, hosted CI green, merge** - per repository, batching the whole migration of that repository into
   one pull request.

## Toolkit gaps to close first (small, and they belong in the toolkit)

- **Vendor by environment**: `tlinalg` and `tcontract` select the vendor arm with `BENCH_VENDOR` rather
  than a cargo feature. Give `measured` an optional `build_env: {BENCH_VENDOR: "{vendor}"}` that the
  recorder exports for the build, instead of adapting each build script.
- **Harness in the measured checkout**: both campaigns build the harness out of the measured checkout;
  `measured.protocol: measured` already covers the protocol lookup, but the build-argv template should be
  exercised once per campaign in a dry run (`record_run --no-publish`) before the real run.
- **Instances instead of a hand-written grid** (cpueinsum): the suite schema takes an explicit shape
  list, so a campaign whose cases are `tenferro-einsum-benchmark`'s portable JSON instances (168 einsum
  problems, 7 categories, shapes/dtypes/contraction paths, tensors generated zero-filled at run time)
  needs either a generator that writes the population list from those files or a small suite-level
  include. Decide once, then reuse for `strided-rs-benchmark-suite`-style campaigns.

## cpueinsum

`cpueinsum-rs` has no campaign. Reuse `tenferro-einsum-benchmark`'s instances as the workload set, add
`cpueinsum-rs` as the measured library and one reference implementation (tenferro-einsum or strided-rs)
as the second engine row, and follow the steps above. The benchmark suite is defined by the instance
files, so the suite declaration should be generated from them and committed, not hand-typed.

## Fresh-session reproducibility (the part that must not rely on this session's memory)

- CPU sets come from `idle_cpus.py`/`descriptor` (the L3 domain of the measured CPUs) and the observer's
  affinity is checked, so a new session only has to launch the recorder pinned off the measured domain
  (`taskset -c <free cpus>`) and the toolkit refuses when it is not.
- `host.power` records boost/governor/EPP; the recorder refuses to measure with boost on.
- Every campaign's `AGENTS.md` points at its `docs/rules.md` equivalent, and `faer-benchmark/docs/rules.md`
  is the model for the runbook text.
