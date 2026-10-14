#!/usr/bin/env python3
"""Compare two recorded runs of the same suite: what changed between two revisions.

    compare_runs.py <before-run-dir> <after-run-dir> [--families a,b] [--threads 1]

Both directories are raw run directories, and both must have been produced by the
harness over overlapping cells **of the same vendor arm**: one binary links one
vendor library, so a run of one arm shares no engine row with a run of another,
and the two are compared by reading their pages side by side rather than here.
For every cell the table reports the ratio `after / before` of the best wall time,
so a value below 1 means the change made that cell faster.

What this is and is not:

* it is a **scan** over whatever the two runs covered. A cell that only one run
  contains is reported as such and contributes no ratio, because a difference
  between two different populations is not a comparison;
* a ratio from a single complete set is not a performance claim. The timing policy
  requires complete-set repeats for that, and this prints the A/A spread when the
  runs have them, so a reader can see whether the difference is above the noise;
* it says nothing about a family neither run measured.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import statistics
import sys

from . import descriptor
from .campaign import case_label, cell_of


def columns():
    return descriptor.current().csv_columns


def read(run_dir: pathlib.Path):
    """(cell -> [ms per repeat]), the repeat count, and the manifest.

    Only the files the manifest names: that is what the validator checked, and a ratio built from
    anything else would compare numbers nothing gated.
    """
    import yaml
    cols = columns()
    manifest_path = run_dir / "run.yaml"
    if not manifest_path.exists():
        sys.exit(f"{run_dir} has no run.yaml: a comparison needs to know what was measured")
    manifest = yaml.safe_load(manifest_path.read_text())
    listed = manifest.get("result_files") or []
    if not listed:
        sys.exit(f"{manifest_path} names no result files")
    absent = [f for f in listed if not (run_dir / f).exists()]
    if absent:
        sys.exit(f"{run_dir}: result files named by the manifest are absent: {absent}")
    cells: dict[tuple, list[float]] = {}
    repeats = set()
    for path in [run_dir / f for f in listed]:
        repeats.add(path.stem.split("-")[0])
        with open(path) as handle:
            for row in csv.DictReader(handle):
                missing = [c for c in cols if c not in row]
                if missing:
                    sys.exit(f"{path}: missing columns {missing}")
                if row["status"] != "ok":
                    continue
                cells.setdefault(cell_of(row), []).append(float(row["total_ms"]))
    return cells, len(repeats), manifest


def shape(key) -> str:
    return case_label(key)


def aggregate(pairs):
    """(family, engine row, threads, dtype) -> ratios, for the family-level table.

    The engine stays in the key. A median over these ratios would otherwise pool the measured
    provider's rows with the vendor row and call the result a family statistic, and a change that
    moves one of them would be hidden by the other.
    """
    groups: dict[tuple, list[float]] = {}
    for key, ratio in pairs:
        groups.setdefault((key.family, key.row, key.threads, key.dtype), []).append(ratio)
    return groups


def main(argv=None, root=None) -> int:
    if root is not None:
        descriptor.set_root(root)
    descriptor.current()
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--families", default=None, help="only these families (comma separated)")
    ap.add_argument("--threads", default=None, help="only this thread count")
    args = ap.parse_args(argv)

    before_dir, after_dir = pathlib.Path(args.before), pathlib.Path(args.after)
    before, before_repeats, before_manifest = read(before_dir)
    after, after_repeats, after_manifest = read(after_dir)

    # One binary links one vendor library, so two runs of different arms have no engine row in
    # common except the measured provider's rows, which are the same code on both sides and say
    # nothing about either vendor. Comparing them here would produce a table of those ratios and
    # read as a vendor comparison; the vendor comparison is two pages read side by side.
    before_arm = (before_manifest.get("vendor") or {}).get("name")
    after_arm = (after_manifest.get("vendor") or {}).get("name")
    if before_arm != after_arm:
        sys.exit(f"ERROR: these are two vendor arms ({before_arm} and {after_arm}), which is not a "
                 f"revision comparison: the two binaries share no engine row but the measured "
                 f"provider's rows, and those are identical code on both sides. Compare each arm "
                 f"with the previous revision recorded for the same arm, and compare the arms by "
                 f"reading their pages side by side.")

    # A ratio is only a revision comparison when the thing that changed is the thing under
    # test. The measured library is expected to differ; if two runs were taken against
    # different baselines, or by different harnesses, the change measured is a different one
    # and the table would silently attribute it to the measured library.
    for what in ("upstream", "harness"):
        b = (before_manifest.get(what) or {}).get("commit")
        a = (after_manifest.get(what) or {}).get("commit")
        if b != a:
            sys.exit(f"ERROR: the two runs were taken against different {what} revisions "
                     f"({b!r} and {a!r}); that difference is part of what the ratio would "
                     f"measure, so it is not a one-variable comparison.")

    wanted = set(args.families.split(",")) if args.families else None
    keys = sorted(set(before) & set(after))
    if wanted:
        keys = [k for k in keys if k.family in wanted]
    if args.threads:
        keys = [k for k in keys if k.threads == int(args.threads)]

    print(f"# {args.before} -> {args.after}")
    print()
    print(f"- vendor arm: `{before_arm}` on both sides")
    print(f"- complete-set repeats: before {before_repeats}, after {after_repeats}")
    only_before = len(set(before) - set(after))
    only_after = len(set(after) - set(before))
    print(f"- cells only in one run (no ratio): {only_before} before, {only_after} after")
    if min(before_repeats, after_repeats) < 2:
        print("- this is a **scan**, not a paired comparison: at least one side has a single")
        print("  complete set, so the ratios carry no A/A spread")
    print()

    pairs = []
    print("## Per cell")
    print()
    print("| cell | before (ms) | after (ms) | after/before |")
    print("|---|---|---|---|")
    for key in keys:
        b, a = min(before[key]), min(after[key])
        pairs.append((key, a / b))
        mark = "" if 0.95 <= a / b <= 1.05 else (" **faster**" if a < b else " slower")
        print(f"| {shape(key)} | {b:.6g} | {a:.6g} | {a / b:.3f}{mark} |")

    print()
    print("## Per family")
    print()
    print("| family | row | threads | dtype | cells | median after/before | min | max |")
    print("|---|---|---|---|---|---|---|---|")
    for (family, row, threads, dtype), ratios in sorted(aggregate(pairs).items()):
        print(f"| `{family}` | `{row}` | {threads}T | {dtype} | {len(ratios)} | "
              f"{statistics.median(ratios):.3f} | {min(ratios):.3f} | {max(ratios):.3f} |")

    if min(before_repeats, after_repeats) >= 2:
        print()
        print("## A/A spread within each run")
        print()
        for name, cells in (("before", before), ("after", after)):
            spreads = [max(v) / min(v) for v in cells.values() if len(v) > 1 and min(v) > 0]
            if spreads:
                print(f"- {name}: median max/min {statistics.median(spreads):.3f}, "
                      f"worst {max(spreads):.3f} over {len(spreads)} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
