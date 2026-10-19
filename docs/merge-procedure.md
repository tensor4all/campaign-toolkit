# Merging a candidate into a measured library's main

The campaign-agnostic part of "a measured change becomes a merged change". Every campaign
instantiates this in its own `docs/merge-procedure.md`: the pins, engines, suites and CPU sets are
its own, the reasoning and the order of operations are not.

## What a merge means

A merge moves the measured library's `main` to a revision that has already been measured *as that
revision*. A merge is not an adoption claim about cells nobody measured, and it is not the moment to
prove the suite is clean.

## Preconditions

1. **The branch is a descendant of `main`.** If it is not, rebase first; never merge a branch whose
   base predates `main`.
2. **The correctness gate passes** - the measured crate's own test target, with the same
   pass/ignore count as `main` plus whatever the change adds. The change's focused tests are part of
   that gate, not a substitute for it.
3. **The measured paths are the merged paths.** `scripts/candidate_compare.py --merged-commit <rev>`
   compares the tree hashes of the campaign's declared measured paths. A merge, rebase or rename
   that leaves them alone keeps the measurement valid; one that changes them invalidates the cells
   that depend on it, and those are re-measured rather than assumed.
4. **A correctness-only change** whose changed code is on no declared benchmark path needs no pair.
   Say which paths it reaches and why no declared cell executes them, and merge on the tests. That
   is a reachability statement, not a performance claim.

## Before the pair: the observer checklist

The recorder refuses to start when the observing side is on a measured core, but that refusal has to
see the whole chain: pinning the recorder does nothing for the agent that launched it, because
affinity is per process and is not inherited backwards. The checklist is short and the recorder
checks all of it:

1. **The agent is pinned off the measured domain.** The recorder samples the ancestor chain and
   refuses any process that still owns a measured CPU and was using at least 1% of a core, naming
   the pid, the comm, the affinity and the rate, and printing the `taskset -pc <free> <pid>` that
   fixes it. Do it before anything else, and again after a restart.
2. **The measured sets come from the suite**, not from memory.
3. **Nothing else is running.** The idle guard checks the whole domain before and after every cell;
   a rejected attempt is archived, but a rejection costs a rebuild and a retry.
4. **The pin is recorded**, in `threads.observer`, so the manifest shows what the check allowed.

## The pair

Declare the target and guard grids from the changed paths before the runs: the affected families at
every declared shape, dtype, order and thread budget, the target cells, and two or three collateral
cells of families the change cannot reach.

1. Build and verify before timing; the recorder holds one campaign-level lock across build, verify
   and timing, so nothing else builds or measures meanwhile.
2. Record the baseline at `main` and the candidate at the branch head, interleaved in time, with the
   campaign's frozen policy.
3. `scripts/candidate_compare.py --baseline <run> --candidate <run> --merged-commit <rev>`. The
   control decision is the unchanged engine row's **median** within 2%; the per-cell tail is printed
   and retained but does not refuse, because the unchanged row moves with the linked binary and a
   cell-by-cell rule refuses even two runs of one revision.
4. A refused comparison means no merge. It is not retried with a relaxed band or a trimmed start.

## What a merge does and does not prove

| question | answered by | cost |
| --- | --- | --- |
| did this change do what it was written for? | the candidate pair on the target cells | about an hour |
| did it break the families it can reach? | the declared guard set in the same pair | included |
| is the machine still the machine? | the unchanged row's median, in the same pair | included |
| did anything regress anywhere else? | the **periodic full declared suite**, run manually at a frozen candidate and published | hours |

A merge proves the cells it measured. It does not prove that no case anywhere regressed, and it is
not supposed to: every number carries a between-start spread, so certifying the whole grid per merge
would cost hours per candidate and still be a distributional claim. A case that no candidate targets
is left to the periodic run; its last published page stands until that run replaces it. The period
is a campaign decision, and its result - not a per-merge argument - is what a suite-wide "no
regression" claim may cite.

## The merge and the record

- Merge the measured revision; fast-forward when the branch is a descendant, so the merged tree is
  the measured tree. Confirm the tree hashes again afterwards.
- Move the campaign's adoption pin to the measured revision when the candidate is adopted: the pin
  names what the campaign measures and the pages quote, so it follows the adoption, not `main`.
- Record in the campaign's ledger and worklog: the measured commit, the tree hashes, the target and
  guard results, the run directories that carry them, and the date. An entry without its run
  directory is not a record.
- Re-recording the declared suite at the new revision is the publication step that follows a merge,
  and it is a separate, deliberately started run.
- Rejected and HOLD branches stay where they are with their records. A merge does not delete the
  branches that lost, and a HOLD candidate is not revived by a neighbouring merge.
