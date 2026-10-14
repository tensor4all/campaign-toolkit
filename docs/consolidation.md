# Where the benchmarks live: one results home, or one campaign per library?

This design note lives with the shared tooling because the answer decides how much
of it is shared. It is a decision request for the maintainer, not a record of a
change; the campaigns' own `docs/` hold their results.

Status: design, for the maintainer's decision. Nothing here is implemented except
the parts named in *What exists today*.

## What exists today

tensor4all has seven benchmark repositories:

| repository | kind |
|---|---|
| `tensor4all-benchmark`, `tenferro-benchmark`, `tenferro-einsum-benchmark`, `strided-rs-benchmark-suite` | one-off suites for one question |
| `tlinalg-benchmark`, `tcontract-benchmark`, `faer-benchmark` | *campaigns*: one results repository per measured library, results keyed by library commit x hardware profile, with pins, a generated staleness index and a timing policy |

The three campaigns carry **the same tooling**: `record_run.py`, `validate_run.py`,
`gen_index.py`, `publish_report.py`, `compare_runs.py`, `campaign.py`,
`report_run.py`, `pinned.sh`, `idle_cpus.py` — about 1,800 lines per repository,
already drifted (`record_run.py` is 469 / 380 / 495 lines in the three copies;
`campaign.py` and `compare_runs.py` do not exist in `tcontract-benchmark` at all).
The `zen5-cpu` hardware profile is copied into each repository verbatim.

## What actually differs between campaigns

This is the whole parameterisation surface, and it is small:

| | where it differs |
|---|---|
| measured library | pin file, checkout, revision, dirty state |
| second identity | `faer-benchmark` records an `upstream` faer revision beside the fork; the others record one |
| harness | crate and binary name (`tlbench`, `fbench`), and the vendor-arm feature names |
| engine names | which row names exist and which provider each belongs to |
| page identity | `result/<profile>/<suite>/<commit12>[-vendor].md` vs `<fork12>-<upstream12>[-vendor].md` |
| staleness | the `invalidated_by` paths of the *measured* library and its `stale_after` |
| report header | the fields a page prints above its tables |

Everything else is shared and mechanical: the manifest schema, the run directory
layout, the coverage/currency computation, the one-vendor-per-binary rule, the
timing protocol, the index.

## Options

**A — one campaign repository per measured library, shared tooling.** Status quo
plus the extraction that is being done now (`campaign-toolkit`).

**B — one results repository with a directory per library** (`tensor4all/benchmark`,
`tlinalg/`, `faer/`, `tcontract/`, ...), the toolkit consumed once.

**C — hybrid: per-library recording, one generated reading surface.** Campaigns keep
recording (pins, declarations, CI, git history of pages); a generated aggregate
repository or Pages site carries the union index across campaigns, with back-links.

## Recommendation

**Phase 1 (now): A.** The page identity is a property of the measured library, and
so are the pin, the CI, and the history that a page's git blame explains. The
campaigns are also read by different people: a tlinalg question is answered from
`tlinalg-benchmark`'s index, and nothing is gained by making them share a
directory tree. What *is* gained, and is the reason this design exists, is one
copy of the tooling: `campaign-toolkit`, with each campaign reduced to its
declaration, its pins, its pages and thin shims.

**Phase 2 (when a reader needs two campaigns at once): C.** Generate the union
index; never hand-maintain it. The building block is a `campaign-union-index`
command in the toolkit that reads several campaign roots and writes one index
keyed by (campaign, suite, profile, revision, vendor). It is cheap because each
campaign's `result/INDEX.md` already carries exactly those columns, and it keeps
each number owned by the repository that recorded it. Its existence is also what
decides whether Phase 3 is ever needed — if two campaigns are read side by side,
the reader will say so.

**Phase 3 (only if the organisation wants one home for recording): B.** It is
purely mechanical *once every campaign has a complete `campaign.yaml`*: the
declarations, pins, `data/` and `result/` move unchanged, the page names do not
change (they are computed from the descriptor), and CI becomes one workflow with a
matrix over campaigns. The precondition is the point: doing it before the
descriptor is complete in every campaign means doing the parameterisation twice.

**Do not put the harness in the measured fork.** `faer/AGENTS.md` states the reason
directly: "Do not carry unrelated formatting churn or dependency bumps. Every
extra diff is a rebase cost against upstream." A benchmark crate in the fork's
workspace is a permanent rebase cost, it would appear in the published `t4a-faer`
tree, and campaign infrastructure is not an experiment. A harness that links the
checkout *by path* (what `fbench` does) needs nothing from the fork.

**Do not fold a library's rows into another library's campaign.**
`tlinalg-benchmark` measures faer *through* tlinalg: its numbers carry tlinalg's
packing, batch loop and lane policy. A faer-level claim cannot be evidenced by a
page whose identity is a tlinalg commit, and a page carrying two identities is
exactly the opacity the per-page header exists to remove.

**Upstream faer's own bench page is not a substitute either** (`faer/examples/bench.rs`
+ `bench.toml`, source of truth `codeberg.org/sarah-quinones/faer`). It has vendor
arms, but no revision x host keying, no resolved vendor identity, no staleness, and
it restores inputs inside the clock. The campaigns already treat upstream as an
*arm* (`faer-upstream`, in the same binary as the fork) — that is the useful form
of the comparison, and it needs no permission from upstream. The fork's own rules
require the maintainer's explicit permission before an upstream issue or PR is even
prepared, so an upstream contribution is a separate decision with a separate owner.

## What the toolkit must therefore do

1. Resolve every path and identity from `benchmarks/campaign.yaml`; the package
   contains no campaign's name, pin, binary or engine.
2. Support one *or more* measured identities, with the page name computed from a
   pattern over them, and refuse a run whose declared identity has no resolved
   revision.
3. Own the shared **hardware profile library** (`zen5-cpu` and its L3-domain
   wording currently exist as three verbatim copies) with a campaign opting into
   the profiles it measures. A profile is a fact about a machine, not about a
   campaign.
4. Provide `campaign-union-index` (Phase 2's building block) even while only one
   campaign uses it: it is the command that makes the consolidation question
   answerable from data instead of taste.

## Decision points for the maintainer

1. Is a second, generated repository for *reading* wanted (Phase 2), or is
   per-campaign reading enough?
2. Is the eventual move to a single recording repository (Phase 3) accepted in
   principle, knowing it is mechanical once `campaign.yaml` is complete everywhere?
3. Does the toolkit own the hardware profiles (recommended) or does each campaign
   keep its own copy?
4. Which campaign migrates second? `faer-benchmark` is the natural candidate (it has
   the second identity and no recorded run yet, so nothing can be invalidated by the
   migration), and `tcontract-benchmark` the largest win (its three missing scripts
   come for free).
