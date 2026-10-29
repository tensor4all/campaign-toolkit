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
`campaign-validate-declarations`, `campaign-union-index`) are declared in
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
| `campaign_toolkit/schemas/` | the packaged copies of the schemas, used at runtime and by an installed wheel |
| `campaign_toolkit/profiles/` | the shared hardware profile library a campaign opts into by name |
| `schemas/` | the same schemas for review; a test fails if the two copies differ |
| `benchmarks/scripts/` | the pinned, idle-checked timing protocol, copied from the measured library |
| `tests/fixture/` | a synthetic campaign: declarations and two runs, no build |
| `docs/extraction.md` | provenance: what was taken from where, at which commit |

## CI

`.github/workflows/ci.yml` is content-only — the measurement itself runs on the
host that owns the numbers. It validates the descriptor and suite schemas
against the fixture, compiles every module, and runs the self-test in `tests/`.
