# Extraction provenance

This repository adopts the shared tooling that, until now, each tensor4all
benchmark campaign carried its own copy of. This document records what was
taken from which file, at which commit of which repository, and the basis for
doing so. It follows the model of `faer-benchmark/docs/provenance.md`.

## Authorization

Copied and adapted content here is the work of the same organization and the
same author as the source (`tensor4all`, maintained by Hiroshi Shinaoka), taken
from repositories that are `MIT`-licensed:

- `tlinalg-benchmark` carries no `LICENSE` file in its tree, but every script
  and document it holds derives from `tlinalg-rs` and `tprims-rs`, which are
  `MIT` (`tlinalg-rs/LICENSE-MIT`, and the notices in the copies of
  `benchmarks/scripts/*.py`, `MIT, Copyright (c) 2026 Lukas Devos and tensor4all
  contributors`).
- Nothing here redistributes any measured library's source.

Copied files keep their in-file attribution. No third-party license is
reinterpreted: the code ported into the measured libraries from Eigen, LAPACK
and SuiteSparse keeps its own terms, and none of that code is copied here.

## Copied from `tlinalg-benchmark`

At commit `50e27c9be9ff86a700ac17dbd81da484a3a869dd`
(`git -C tlinalg-benchmark rev-parse HEAD`, "Record both sides at the fork's
compact-factor region floor"). This is the reference campaign: the most mature
of the five, with a real `result/INDEX.md` and 20+ recorded runs.

| file here | source | note |
|---|---|---|
| `campaign_toolkit/gen_index.py` | `scripts/gen_index.py` | the index generator, verbatim logic; `ROOT`, the pin file and the printed generator/pin strings now come from the descriptor; the checkout and reference are read for each declared identity |
| `campaign_toolkit/validate_run.py` | `scripts/validate_run.py` | verbatim logic; `jsonschema` replaced by the built-in `jsonschema_lite`, the schema loaded from the toolkit, commits resolved in the descriptor's checkout, the second identity checked when present |
| `campaign_toolkit/record_run.py` | `scripts/record_run.py` | verbatim logic; the build script, binary, feature per arm, pin variable names and protocol location come from the descriptor |
| `campaign_toolkit/report_run.py` | `scripts/report_run.py` | verbatim logic; the header labels, CSV columns and provider packages come from the descriptor |
| `campaign_toolkit/publish_report.py` | `scripts/publish_report.py` | verbatim logic; the page path comes from the descriptor's `page_name` |
| `campaign_toolkit/compare_runs.py` | `scripts/compare_runs.py` | verbatim; it names no campaign, and its output is byte-identical to the source (see below) |
| `campaign_toolkit/campaign.py` | `scripts/campaign.py` | the engine -> provider map, vendor providers, provider packages and page-name rule now come from the descriptor; `population_covers` and `declared_for` are the generalization `faer-benchmark` had already made for a regime declared more than once |
| `schemas/benchmark-run.schema.json` | same path | copied, then extended (see below) |
| `schemas/benchmark-suite.schema.json` | same path | copied unchanged |
| `.github/workflows/ci.yml` | same path | the same one content-only job, retargeted at the fixture campaign and the package |

## Copied from `tlinalg-rs`

At commit `e6ee2d3304e042926c77cda7b81e58c871a98919`
(`git -C extern/tlinalg-rs rev-parse HEAD`, the campaign's own reference pin).
The timing protocol does not live in the campaign repository; it lives in the
measured library, and the reference campaign takes it from the measured
checkout. The toolkit carries the same two files so a campaign that owns its
harness (as `faer-benchmark` does) has them.

| file here | source | note |
|---|---|---|
| `benchmarks/scripts/pinned.sh` | `benchmarks/scripts/pinned.sh` | copied verbatim, including its in-file attribution |
| `benchmarks/scripts/idle_cpus.py` | `benchmarks/scripts/idle_cpus.py` | copied verbatim, including its in-file attribution |

Both are themselves adapted from `tensor4all/tprims-rs`
(`MIT, Copyright (c) 2026 Lukas Devos and tensor4all contributors`), which is
where the `pinned.sh`/`idle_cpus.py` pattern originated. That attribution is
kept in the files.

## Determined, not copied

The descriptor schema, `schemas/campaign.schema.json`, `campaign_toolkit/descriptor.py`,
`campaign_toolkit/declarations.py`, `campaign_toolkit/jsonschema_lite.py`, the
fixture campaign and this document are written for this repository; they are not
copied from anywhere.

## Deliberate extensions of the copied schemas

Neither extension removes a check.

- `benchmark-run.schema.json`: `upstream` is added as an **optional** second
  recorded library identity. The reference campaign records one; `faer-benchmark`
  records two (a baseline). The toolkit takes the descriptor's `identities` to
  decide, so the property is allowed rather than required — a manifest with no
  `upstream` still validates, and one with it is validated and its commit
  resolved the same way as `library`.

## Proof of the extraction

The migration of `tlinalg-benchmark` (branch `toolkit`) is the acceptance test:

- `python3 scripts/gen_index.py --check` exits 0 and reproduces
  `result/INDEX.md` byte for byte (the strongest check: the generated index is
  compared to the committed one);
- `python3 scripts/validate_run.py` prints `ok` for every recorded manifest;
- `python3 scripts/compare_runs.py A B` is byte-identical to the pre-migration
  script, run from a pristine `git show main:scripts/compare_runs.py`;
- no recorded `data/results/**` or `result/**` file changed.
