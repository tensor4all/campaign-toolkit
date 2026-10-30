# `campaign-toolkit`

Shared tooling for the tensor4all benchmark campaigns. Five campaigns each
carried their own copy of the same scripts (`record_run.py`, `validate_run.py`,
`gen_index.py`, `publish_report.py`, `compare_runs.py`, `campaign.py`,
`report_run.py`) and the copies had drifted. This package holds that logic once,
parameterised by a per-campaign **descriptor**; a campaign keeps only a thin
`scripts/*.py` shim per entry point, a `benchmarks/campaign.yaml`, and its data.

## Install

```bash
pip install -e /path/to/campaign-toolkit          # local development
# or, from a campaign's requirements.txt, pinned by revision:
#   campaign-toolkit @ git+https://github.com/tensor4all/campaign-toolkit@<rev>
```

The runtime dependencies are `pyyaml` and `markdown` (the site's page conversion).
JSON Schemas are checked by the small built-in validator (`campaign_toolkit.jsonschema_lite`), not by `jsonschema`.

## The descriptor

Every campaign-specific value lives in `benchmarks/campaign.yaml` at the campaign
root, validated against `schemas/campaign.schema.json`. It declares the measured
project (name, url, pin file, checkout, build script, binary, the feature per
vendor arm), which revisions a run records (`identities: [library]` or
`[library, upstream]`), the profiles/suites/data/result directories, the engine
name -> provider map, the vendor providers (mutually exclusive BLAS/LAPACK arms) and the
external providers (a baseline linked against a separately built library), the published-page
name template, the default invalidated paths, the CSV columns (and, for a campaign whose harness measures
one, the extra `case_axes` that identify a cell and the `case_label` a report and a
comparison print it as), and the strings the index and report headers print. The package
itself holds no campaign's specifics.

A campaign's entry points are its `scripts/*.py` shims:

```python
from campaign_toolkit.gen_index import main
if __name__ == "__main__":
    sys.exit(main(root=pathlib.Path(__file__).resolve().parent.parent))
```

Each is also runnable directly, with `CAMPAIGN_ROOT` naming the campaign:

```bash
CAMPAIGN_ROOT=/path/to/campaign python3 -m campaign_toolkit.gen_index --check
```

Console entry points (`campaign-record-run`, `campaign-validate-run`,
`campaign-gen-index`, `campaign-publish-report`, `campaign-compare-runs`,
`campaign-report-run`, `campaign-descriptor`,
`campaign-validate-declarations`, `campaign-union-index`, `campaign-sync-pins`,
`campaign-stack-status`, `campaign-trend`, `campaign-check-generated`) are declared in
`pyproject.toml`.

## Recording a run

A campaign records a run through `record_run` (its `scripts/record_run.py` shim). A suite that
declares no vendor engine is recorded without a vendor selection, and its manifest omits the
`vendor` block entirely (absence, not null). A suite that measures an engine whose provider is
neither the measured library nor a vendor library is refused unless
`--external-provider-json PATH` supplies that provider's collected identity with a non-empty
`commit`: nothing is published from an arm whose revision cannot be stated. The entries are a JSON
list of `{name, version, commit, path, note, tag, sha256}`, merged into the manifest's `providers`
and printed on the page's provenance section independently of Cargo-dependency provenance, so an
external baseline's revision is visible even when it is not a Cargo dependency.

### Which CPUs a thread row runs on

A suite's `cpu_sets` pairs each of its `threads` with the measured CPUs, by position. It is
either one list or a mapping from a profile name to a list:

```yaml
runs:
  - threads: [1, 4, 8]
    cpu_sets:
      zen5-cpu: ["4", "4-7", "4-11"]   # this suite's choice on zen5-cpu
    # zen2-cpu is not named, so its own thread_rows decide: 1T 0, 4T 0-3, 8T 0-7
```

`record_run` takes, for the run's profile, the suite's entry for that profile; else, when
`cpu_sets` is one list, that list (on every profile, as before mappings existed); else the
profile's `thread_rows`, and a thread count the profile has no row for is refused. It prints
which of the three decided. One list cannot be right on two machines, so `declarations`
fails a suite whose single list serves more than one profile, and names the mapping to write
instead: the list goes under the profile it was written for, and the CPUs it records there do
not change.

### Before recording on a new host

Three things the first `zen2-cpu` smoke (tensor4all-meta#76, 3.3) had to learn:

- **Confine the observing side, every thread of it.** The recorder refuses an observing
  process that may run inside a measured L3 domain. `taskset -pc <observer> <pid>` moves one
  thread only: an agent's other threads stay on the measured cores and the audit names them.
  Use `taskset -apc <observer> <pid>` (all threads) on the driving agent, then
  `python3 -m campaign_toolkit.observer_audit --observer <observer> --confine` for the rest.
- **Timing v2 needs `--aa` at least the engine count, and a multiple of it.** One process set
  per engine-order rotation: with three engine rows (`--vendor openblas` in faer) `--aa 2` is
  refused and `--aa 3` passes; with two rows, `--aa 2` or `4`.
- **`--shapes` must cover the suite's `verify` grid.** A suite's `verify.n` may narrow the
  shapes the verify runs on, never widen them, so a `--shapes` filter that leaves out a shape
  `verify.n` names is refused. A smoke asks for the `verify.n` shapes (tlinalg: `4,8,16`), not
  for one shape.

## Hardware profiles

A profile is a fact about a machine, not about a campaign, so the library lives
here (`campaign_toolkit/profiles/<name>.yaml`) and a campaign names the profiles
it measures in `benchmarks/profiles.yaml`:

```yaml
schema_version: 1
profiles:
  - zen5-cpu                 # resolved from the toolkit
  - name: some-one-off       # or declared inline, for a machine only this campaign uses
    cpu: ...
    logical_cpus: 8
    l3: "0-7: 8 MiB shared"
```

Editing a shared profile therefore reaches every campaign that measures it; the
run manifest still records what the host actually reported, so a recorded run is
unaffected by a later profile edit.

A profile may also declare `smt` (how SMT siblings are numbered) and `thread_rows` (per
thread count, the measured CPUs and the observer's CPUs). `zen5-cpu` and `zen2-cpu` (EPYC
7702P, 64C/128T, measurement role) do. `campaign-validate-declarations` checks the rows
(a measured core appears once; the observer shares no CPU with a measured core or its SMT
siblings), and the idle gate, `pinned.sh` and `record_run` (for a suite that does not name
the profile in its `cpu_sets`, above) read them:

```bash
benchmarks/scripts/pinned.sh --dry-run --profile zen2-cpu --threads 4   # prints, runs nothing
benchmarks/scripts/pinned.sh --profile zen2-cpu --threads 4 -- COMMAND  # the measured set from the row
```

The dry run prints the `taskset` line, the measured CPUs, the CPUs the idle gate checks (SMT
siblings included) and the observer's CPUs. Without `--profile` the siblings and the observer
come from the live topology, and `pinned.sh CPUS -- COMMAND` works as before.

## The stack

`stack/stack-pins.yaml` says which revision of each measured library the campaigns measure
(`{crate, version, tag, commit}`, git dependencies by commit) and which benchmark repository
carries which pin. A repository's `pins/*.rev` are generated from it, and CI checks them:

```bash
campaign-sync-pins                # write pins/*.rev of the repository in the cwd (comments are kept)
campaign-sync-pins --check        # write nothing; exit 1 naming each pin that is missing or differs
campaign-stack-status             # the daily patrol: one Markdown row per repository (pins, newest run, CI)
```

`campaign_toolkit/stack/stack-pins.yaml` is the copy an installed toolkit reads (`--stack`
overrides it); a test fails if it differs from `stack/stack-pins.yaml`, which is the one to edit.
`campaign-stack-status` looks for the checkouts beside the toolkit checkout (`--checkouts DIR`),
asks `gh` for the CI state of `main`, and fills a cell it cannot with a reason.

## Host information

`python -m campaign_toolkit.hostinfo --json` prints what decides whether two measurements are
comparable: boost, governor, amd-pstate mode, platform profile, SMT, the NUMA layout (a Zen 2
host's NPS setting), the SMT sibling map, the L3 domains, and the toolchain. A missing sysfs
file is `null`. `record_run` writes `host.power.smt`, `host.numa` and `build.toolchain` from it
into every new manifest; they are optional in the schema, so older manifests still validate.

## The result index, the history and the site

`campaign-gen-index --write` writes two files into the result directory:

- `INDEX.md`: one row per `(suite, profile, vendor arm)` with the page measured at the
  reference revisions (every recorded identity: the library pin, and for a campaign that
  records a baseline the upstream commit or tag), or `missing`. A page is listed once, keyed
  by its path.
- `HISTORY.md`: every other page, newest first, grouped by the revisions it measured.
  `INDEX.md` links it in one line at the top.

`--check` checks both files. The gate follows *currency* (`status`), not the pin: a page
measured before the pin moved can still be `current`, so moving a pin before re-measuring
keeps CI green while its page moves to the history.

`python -m campaign_toolkit.site --root <campaign>` writes the campaign's site into the
result directory: `index.html` with one section per hardware profile (in the order of
`benchmarks/profiles.yaml`; each section opens with one line naming the pin, the measurement
dates, the power state and the toolchain of its runs), an HTML copy of every result page
beside its `.md` (linked from the landing page and the history; the GitHub blob is its
"source" link), and `history.html`, whose list of older pages is the same set as
`HISTORY.md`, followed by the log of publication commits. The current page of a cell is the
index's own rule (`gen_index.split_pages`), so the site and the index agree. The conversion
uses the `markdown` package.

## Charts, trends and the generated-file check

Charts are generated when the site is built, from the runs behind the current pages, and are
not committed. The descriptor declares them (all optional):

```yaml
figures:
  throughput:                      # size against nominal GFLOP/s (campaign_toolkit.throughput)
    config: benchmarks/throughput.yaml
    runs:                          # the run positions the config's series name
      - {suite: faer-kernels, vendor: mkl}
      - {suite: faer-kernels, vendor: openblas}
  cases: [tcbench]                 # one dot plot per current page: cases x engines, log time
trend:                             # representative cases followed across pins
  - suite: faer-kernels-large
    case: gemm-large:gemm/1024x1024x1024/b1 f64 c   # the case key: regime, case name, dtype, axes
    vendor: mkl                    # optional: the points of one vendor arm only
    engines: [faer-fork, lapack-mkl]
    threads: [4, 8]
```

`site.write_site` empties `<result>/figures/`, writes them there per hardware profile (a run of
one profile is never drawn with another's), and shows one full-width chart per row.
Every chart is drawn by `campaign_toolkit.charts`: log axes always carry minor ticks (2..9 times
each decade), panels sit side by side (three to a row), the legend sits above the panels, and
every tick and legend label is at least 12 px when the chart is shown 72rem wide (a test checks
the emitted SVG). Each chart has a Markdown table of the same numbers beside it.

`campaign-trend --out <dir>` draws the declared trends without the site. Its input is
**trend points** (`schemas/trend-points.schema.json`): one point is the median of one case,
engine and thread budget in one run, with its pin, profile, timing policy, power state and
vendor arm (every recorded power control is part of the power string). By default they come from the runs behind the published pages; `--runs` names run
directories instead, and `--points file.json` draws points another repository wrote (`--write-points`
writes them). A line joins only points whose profile, timing policy, power state and vendor
arm (and campaign) are equal, so a v1 and a v2 point, or a boost-off and a boost-on point, are
never on one line; the conditions all lines share are printed under the title and the legend
names the rest. Points of different units, or a median of 0, are refused. The x axis is the pin (`--axis week`: the ISO
week); a pin measured twice keeps its newest point.

`campaign-check-generated` checks that nothing generated was edited by hand: every published
page is its source run's `report.md` under the publication header, byte for byte; the runs
changed since `--base <ref>` (or `--run DIR`, or `--all`) regenerate their `report.md` from
their CSVs byte for byte (a deleted `run.yaml` beside remaining files fails too); and `gen_index --check` passes (`--no-index` skips it). Only changed
runs are regenerated, because a report written by an older toolkit need not match today's
renderer.

## The union index

`campaign-union-index` reads several campaigns and writes one index across them,
one row per `(campaign, page)`:

```bash
campaign-union-index /path/to/tlinalg-benchmark /path/to/faer-benchmark --out union.md
campaign-union-index /path/to/faer-benchmark/result/INDEX.md --out union.md
```

Each input is a campaign root or the path to its published `result/INDEX.md`; a
root is read through its own index, so both take the same code path, and one
campaign is the same code path as several. The currency in the output is the
currency each campaign already computed against *its own* pins and
`invalidated_by` paths — this command re-derives no number. Each row links back
to the campaign's page and to the campaign's own index. A campaign's pages are read
from its `INDEX.md` and the `HISTORY.md` beside it, or from the single `All pages`
table of an index generated before the history file existed.

## Version-2 timing evidence

A campaign can opt into `timing: {version: 2}` and optionally declare validated
`aa`, `reps`, and `prime_ms` defaults. Its harness must implement median/raw-sample
output, explicit row rotation, observed thread-budget columns, and a verification
CSV census. The recorder requires clean committed library/harness/toolkit source.
V2 validation rejects incomplete or duplicate process/cell/variant coverage and
inconsistent aggregates. At least two process sets and complete engine-rotation
cycles are required, even for a single engine. Reports always separate C/F order
in v2, including when omitted from a campaign's display axes. They show medians
with separate within-process and scheduled between-process sample SD, not
best-of-repeats or confidence intervals.
`{timing}` in `page_name` keeps revised-contract pages separate from historical v1.
For v2 recording, install the toolkit **editable from its own Git checkout**
(e.g. `pip install -e ../campaign-toolkit`). A wheel or ordinary non-editable VCS
installation does not retain the required source identity; an enclosing campaign
Git repository is not a substitute. A pinned pip VCS requirement can use
`-e git+https://github.com/tensor4all/campaign-toolkit@COMMIT#egg=campaign-toolkit`.
V1 readers/recorders do not acquire this Git-checkout prerequisite. The early
`build-info.json` retains admission identities/argv and binary SHA-256 even when
verification or timing subsequently fails.

Existing campaigns/manifests without this opt-in retain v1 rendering. The legacy
revision-comparison command explicitly refuses v2 rather than pool variants or
select minima; v2 candidate comparison is deferred until that next phase.
Use `record_run --no-publish` for validated smoke/control artifacts without
publishing a numerical page.

## Layout

| path | what |
|---|---|
| `campaign_toolkit/` | the package: the entry points and `descriptor.py`, `campaign.py`, `jsonschema_lite.py`, `declarations.py` |
| `campaign_toolkit/charts.py`, `figures.py`, `trend.py` | the chart parts, the site's chart hook, and the trend points and chart |
| `campaign_toolkit/schemas/` | the packaged copies of the schemas, used at runtime and by an installed wheel |
| `campaign_toolkit/profiles/` | the shared hardware profile library a campaign opts into by name |
| `schemas/` | the same schemas for review; a test fails if the two copies differ |
| `stack/` | the stack pins (`stack-pins.yaml`); `campaign_toolkit/stack/` is the packaged copy, kept equal by a test |
| `benchmarks/scripts/` | the pinned, idle-checked timing protocol, copied from the measured library |
| `tests/fixture/` | a synthetic campaign: declarations and two runs, no build |
| `docs/extraction.md` | provenance: what was taken from where, at which commit |

## CI

`.github/workflows/ci.yml` is content-only — the measurement itself runs on the
host that owns the numbers. It validates the descriptor and suite schemas
against the fixture, validates the stack file, compiles every module, resolves every profile
row with `pinned.sh --dry-run`, and runs the self-test in `tests/`.
