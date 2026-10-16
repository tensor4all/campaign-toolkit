#!/usr/bin/env python3
"""Turn one run's harness CSVs into the tracked report for a (profile, suite) cell.

Only the numbers that came from a verified run are published: every row here was
gated by the harness's verify pass before the timing pass started. The report
repeats the run's provenance in its own header, because a table that is copied
into a discussion without its commit is the thing this repository exists to stop.
"""
import argparse
import csv
import itertools
import math
import pathlib
import statistics
import sys

from . import descriptor
from .campaign import case_dims, load, provider_of


def columns():
    return descriptor.current().csv_columns


def read_rows(csv_paths):
    """Rows from the run CSVs, tagged with the repeat they came from.

    A repeat is one complete pass over the declared populations. Legacy rows
    contain a best time; v2 retains chronological calls whose per-process median
    is re-derived during reporting.
    """
    cols = columns()
    rows = []
    for path in csv_paths:
        repeat = pathlib.Path(path).stem.split("-")[0]  # run0-small-8t -> run0
        with open(path) as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                missing = [c for c in cols if c not in row]
                if missing:
                    raise SystemExit(f"{path}: missing columns {missing}")
                row["repeat"] = repeat
                rows.append(row)
    return rows


def case_name(row, variants=False):
    shape = case_dims(int(row["m"]), int(row["n"]), row.get("k", ""))
    name = f"{row['family']}/{shape}/b{row['batch']}"
    return name + (f" [{row['note']}]" if variants and row.get('note') else '')


def report_axes(rows, policy):
    axes = descriptor.current().case_axes
    # V2 validates order even when the campaign omitted it from its display axes.
    if policy and policy['version'] == 2 and 'order' not in axes and any(r.get('order') for r in rows):
        return [*axes, 'order']
    return axes


def best_per_case(rows, policy=None):
    """(regime, case, dtype, *axes, threads) -> engine row -> times, and skipped cases.

    A declared case axis is part of the key, so a cell of one order is never pooled with
    the same shape of another.
    """
    axes = report_axes(rows, policy)
    best, skipped, within, series = {}, {}, {}, {}
    v2 = policy is not None and policy['version'] == 2
    for row in rows:
        key = (row["regime"], case_name(row, variants=v2), row["dtype"],
               *(row[a] for a in axes), int(row["threads"]))
        if row["status"] == "skipped":
            skipped.setdefault((row["regime"], int(row["threads"])), set()).add(
                f"{row['family']}/{row['m']}x{row['n']}")
            continue
        if row["status"] != "ok":
            continue
        times = best.setdefault(key, {}).setdefault(row["row"], [])
        if v2:
            from .timing_evidence import samples
            values = samples(row, policy['repetitions'])
            times.append(statistics.median(values))
            within.setdefault(key, {}).setdefault(row['row'], []).append(statistics.stdev(values))
            # Kept by explicit repeat identity: a ratio joins two engines of one start, never two
            # rows that merely happen to sit next to each other in a list.
            series.setdefault((key, row['row']), {})[row['repeat']] = times[-1]
        else:
            times.append(float(row["total_ms"]))
    return best, skipped, within, series


def rotation_ratios(series, key, engines, fork):
    """Rotation-averaged engine/fork ratio for one cell, and whether it is position-balanced.

    The per-start ratios are averaged in log space, so the result is the geometric mean of the
    ratios measured inside each process start. It removes a position effect only when every
    declared engine has a measured row (each engine then visits every row position once) and only
    under a position effect shared by the engines and stable across starts; the caller must keep
    that qualifier and the per-start ratios visible.
    """
    ratios, detail = {}, {}
    for engine in engines:
        if engine == fork:
            continue
        first, second = series.get((key, fork)) or {}, series.get((key, engine)) or {}
        shared = sorted(set(first) & set(second))
        if len(shared) < 2:
            ratios[engine] = None
            continue
        logs = [math.log(second[r]) - math.log(first[r]) for r in shared]
        ratios[engine] = math.exp(statistics.fmean(logs))
        detail[engine] = ' '.join(f'{math.exp(value):.3f}' for value in logs)
    return ratios, detail


def cell_statistics(series, key, engines):
    """(position-balanced?, worst between-start relative range, position-effect ratio or None).

    `between` is the largest relative range of a *single* engine's own per-start medians, so it
    measures variation between process starts rather than the difference between engines.
    `position` is exp(max - min) of the per-position mean log medians, and is defined only where
    every declared engine has a measured row in every start (a real Latin square) and only for the
    shared, start-invariant position model; it is None otherwise.
    """
    per_start = {engine: series.get((key, engine)) or {} for engine in engines}
    balanced = all(per_start.values())
    between = max(((max(values.values()) - min(values.values())) / statistics.median(values.values())
                   for values in per_start.values()
                   if len(values) > 1 and statistics.median(values.values()) > 0), default=0.0)
    position = None
    if balanced and len(engines) > 1 and len(per_start[engines[0]]) > 1:
        means = {}
        for repeat in per_start[engines[0]]:
            try:
                rotation = int(repeat[3:]) % len(engines)
            except ValueError:
                return balanced, between, None
            order = engines[rotation:] + engines[:rotation]
            for spot, engine in enumerate(order):
                means.setdefault(spot, []).append(math.log(per_start[engine][repeat]))
        if len(means) == len(engines):
            values = [statistics.fmean(positions) for positions in means.values()]
            position = math.exp(max(values) - min(values))
    return balanced, between, position


def spread_guide(best, within):
    """Run-level noise guide computed from the run's own rows: what counts as spread here.

    `within` is a descriptive spread read off the chronological calls, not a confidence interval,
    and the between-start range includes the rotating-order schedule, so this is a reading aid that
    lets a reader tell "this cell moved" from "this cell is noisy" without a model.
    """
    within_rel, between_rel, signal_rel = [], [], []
    for key, per_engine in best.items():
        for engine, medians in per_engine.items():
            center = statistics.median(medians) if medians else 0.0
            if center > 0:
                sds = within.get(key, {}).get(engine, [])
                within_rel += [sd / m for sd, m in zip(sds, medians) if m > 0]
                if len(medians) > 1:
                    between_rel.append((max(medians) - min(medians)) / center)
                    if center >= 1e-3:      # >= 1 us: below that a row is timer/scheduler jitter
                        signal_rel.append((max(medians) - min(medians)) / center)
    return {
        "within_median": statistics.median(within_rel) if within_rel else None,
        "between_median": statistics.median(between_rel) if between_rel else None,
        "between_worst": max(between_rel) if between_rel else None,
        "between_worst_signal": max(signal_rel) if signal_rel else None,
        "between_flagged": sum(1 for r in between_rel if r > 0.05),
        "between_cells": len(between_rel),
    }


def provider_text(prov):
    """One provider's identity as a report fragment."""
    text = f"`{prov['name']}`"
    if prov.get("version"):
        text += f" {prov['version']}"
    if prov.get("commit"):
        text += f" @ `{prov['commit']}`"
    if prov.get("note"):
        text += f" ({prov['note']})"
    return text


VERDICT_FLOOR = 0.05        # a difference below this is not reported even when the starts agreed
DISPATCH_US = 0.02          # below ~20 us/call the multi-thread rows pay thread-region start-up


def comparison_verdict(best, within, key, row_names):
    """One verdict per other engine: `(direction, ratio)` when the difference beats the band.

    The band is the combined between-start spread of the two rows (sqrt of the two relative
    sample SDs) floored at 5%, so a cell is only annotated when the difference is larger than
    the spread the run itself recorded. `within` holds each row's per-process relative SDs.
    """
    fork = best.get(key, {}).get(row_names[0])
    if not fork or len(fork) < 2:
        return {}
    out = {}
    for engine in row_names[1:]:
        other = best.get(key, {}).get(engine)
        if not other or len(other) < 2:
            out[engine] = None
            continue
        fork_median, other_median = statistics.median(fork), statistics.median(other)
        if fork_median <= 0 or other_median <= 0:
            out[engine] = None
            continue
        band = max(math.hypot(statistics.stdev(fork) / fork_median,
                              statistics.stdev(other) / other_median), VERDICT_FLOOR)
        ratio = fork_median / other_median
        out[engine] = (ratio, band) if abs(math.log(ratio)) > band else False
    return out


def verdict_text(verdict, fork_us, other):
    """`🔵 fork 2.5x faster than <row>` / `🔴 fork 2.5x slower than <row>` / `·` / `-`.

    The subject is the fork and the object is named in the cell, so a BLAS row and an upstream row
    can never be confused when the columns are read apart: blue means the fork is faster than that
    named row, red means it is slower.
    """
    if verdict is None:
        return '-'
    if verdict is False:
        return '·'
    ratio, _ = verdict
    mark = '🔴' if ratio > 1 else '🔵'
    return (f'{mark} fork {max(ratio, 1 / ratio):.2f}× ' + ('slower' if ratio > 1 else 'faster')
            + f' than `{other}`')


def spread_guide_text(best, within):
    """One line telling a reader what spread looks like in this run, plus how to read it."""
    guide = spread_guide(best, within)
    if guide["between_cells"] < 1:
        return ("Spread guide: this run kept one process set per cell, so no between-start spread is "
                "available; the within-process SD of each row is still in its `W` value.")
    worst = (f", worst {guide['between_worst_signal']:.2%} among cells of 1 us or more "
             f"(a sub-microsecond row is timer and scheduler jitter, not kernel time)"
             if guide.get("between_worst_signal") is not None else "")
    return (
        f"Spread guide (computed from this run's own rows): per-engine between-start range median "
        f"{guide['between_median']:.2%}{worst}; "
        f"{guide['between_flagged']} of {guide['between_cells']} engine cells exceed 5% and carry `!`; "
        f"median within-process relative SD {guide['within_median']:.2%}. A cell whose `±`, `!` or `p` "
        f"is as large as the difference you are reading is not evidence of a regression."
    )


def render(manifest, suite, rows, run_dir):
    d = descriptor.current()
    axes = report_axes(rows, manifest['timing_policy'])
    report = d.report
    vendor = manifest.get("vendor") or {}
    library = manifest["library"]
    upstream = manifest.get("upstream") or {}
    deps = ([p for p in manifest.get("providers", []) if p["name"] in d.provider_packages]
            or [p for p in manifest.get("providers", []) if p["name"] in d.measured_providers])
    engines = manifest["run_spec"]["engines"]
    # The columns are the engines this run recorded measuring, in the declaration's order: a page
    # prints the arms it has, and a vendor arm has one LAPACK row because one binary links one
    # vendor library.
    row_names = list(engines)
    v2 = manifest['timing_policy']['version'] == 2
    if v2:
        from .timing_evidence import validate
        validate(manifest, run_dir)
    best, skipped, within, series = best_per_case(rows, manifest['timing_policy'])
    out = [
        f"# `{manifest['suite_id']}` on `{manifest['target_profile']}`",
        "",
        f"- {report['library_label']}: `{library['commit']}`"
        + (" **(dirty)**" if library["dirty"] else ""),
        # A campaign that records a baseline says so above its tables: a page naming one
        # revision of a two-library comparison cannot be read as a one-variable result.
        *([f"- {report['library_version_label']}: `{library['version'] or '-'}`"]
          if report.get("library_version_label") else []),
        *([f"- {report['upstream_label']}: `{upstream.get('commit') or '-'}`"
           + (" **(dirty)**" if upstream.get("dirty") else ""),
           f"- {report['upstream_ref_label']}: `{upstream.get('ref') or '-'}` / "
           f"`{upstream.get('version') or '-'}`"]
          if report.get("upstream_label") else []),
        f"- features: `{', '.join(library['features']) or 'default'}`",
        *([f"- {report['provider_label']}: " + "; ".join(provider_text(p) for p in deps)] if deps else []),
        f"- {report['harness_label']}: `{manifest['harness']['commit']}`",
        *([f"- toolkit commit: `{manifest['toolkit']['commit']}`",
           f"- measured binary SHA-256: `{manifest['binary']['sha256']}`"] if v2 else []),
        f"- hardware profile: `{manifest['target_profile']}`",
        f"- host role: `{(manifest.get('host') or {}).get('role', 'development')}`"
        + (" — **provisional**: recorded on a development host, superseded by the measurement"
           " host's dataset" if (manifest.get('host') or {}).get('role', 'development') != 'measurement'
           else " — published dataset host"),
        f"- timestamp: `{manifest['timestamp']}`",
        f"- timing policy: v{manifest['timing_policy']['version']}, "
        f"{manifest['timing_policy']['statistic']} of {manifest['timing_policy']['repetitions']} reps, "
        f"priming {manifest['timing_policy']['minimum_untimed_priming_ms']} ms "
        f"(time-based, identical on every row)",
        f"- guards: idle window {manifest['guards']['idle_window_seconds']} s, "
        f"max busy {manifest['guards']['max_busy_percent']}% including SMT siblings, "
        f"{manifest['guards']['attempts']} attempts",
        f"- command: `{manifest['command']}`",
        f"- raw data: `{run_dir.relative_to(d.root)}/`",
        "",
        "## What was measured",
        "",
        f"- **Workload** — {suite['workloads']['description']}",
        f"  Source: {suite['workloads']['source']}",
    ]
    for spot in suite["workloads"].get("known_blind_spots", []):
        out.append(f"  Known blind spot: {spot}")
    out += ["- **Rows** — one row per engine in every table below:"]
    for engine in row_names:
        doc = suite["engines_doc"].get(engine)
        if doc is None:
            out.append(f"  - `{engine}` — (undocumented in the suite declaration)")
            continue
        out.append(f"  - `{engine}` — {doc['summary']}")
        out.append(f"    Identity: {doc['identity']}")
        if doc.get("caveat"):
            out.append(f"    Caveat: {doc['caveat']}")
        if provider_of(engine) in d.measured_providers and deps:
            own = [p for p in deps if p["name"] == provider_of(engine)]
            out.append("    As measured: " + "; ".join(provider_text(p) for p in (own or deps)))
        if engine == f"lapack-{vendor.get('name')}":
            # From the identity the harness read out of the library it linked, never from the arm
            # that was requested.
            parts = [f"linked {vendor.get('linkage', '?')}", str(vendor.get("name"))]
            if vendor.get("version"):
                parts[-1] += f" {vendor['version']}"
            parts.append(f"kernel dispatched to `{vendor['corename']}`" if vendor.get("corename")
                         else "kernel not reported by the vendor")
            if vendor.get("parallel"):
                parts.append(f"{vendor['parallel']} threading")
            if vendor.get("config"):
                parts.append(f"config `{vendor['config']}`")
            out.append("    As measured: " + "; ".join(parts))
    out += [
        "",
        "## Hardware",
        "",
        f"- CPU: `{manifest['host']['cpu']}`",
        f"- logical CPUs: `{manifest['host']['logical_cpus']}`",
        f"- L3: `{manifest['host']['l3']}`",
        f"- L3 domains: `{manifest['host'].get('l3_domains') or manifest['host']['l3']}`",
        f"- OS / arch: `{manifest['host']['os']}` / `{manifest['host']['arch']}`",
        f"- hostname: `{manifest['host']['hostname']}`",
        "- CPU sets: " + ", ".join(f"{t}T -> `{c}`"
                                   for t, c in sorted(manifest["threads"]["cpu_sets"].items(),
                                                      key=lambda kv: int(kv[0]))),
        "",
        *(d.verify_note().replace("{binary}", d.measured["binary"]).splitlines() if not v2 else [
            'Every timed cell matches a successful verification census, including operation variants.',
            'Each cell is median of process medians ± sample SD of those medians; W is the median',
            'within-process sample SD. All values are milliseconds. These are descriptive spreads,',
            'not confidence intervals. The between-process spread includes the rotating-order schedule.',
            f"Each process retains {manifest['timing_policy']['repetitions']} chronological calls; "
            f"{manifest['run_spec']['aa']} complete process sets are recorded.",
            spread_guide_text(best, within),
            f"The `vs {row_names[0]}` column is the rotation-averaged ratio exp(mean over starts of",
            f"log(engine median / {row_names[0]} median)), with the per-start ratios beside it: `R > 1`",
            'means the engine was slower than that first-declared row in that start. It removes a',
            'row-position',
            'effect only under one position effect shared by the engines and stable across starts,',
            'and only where every declared engine has a measured row (`*` marks a cell where one has',
            'no row, so no ratio there is position-balanced). `!` marks a cell where some engine\'s own',
            'per-start median range exceeds 5% of its median, and `p` a balanced cell whose estimated',
            'position-effect ratio exceeds 1.05. Flagged cells are retained, not discarded; the',
            'between-start variation can exceed the difference being compared.',
            'The `vs` columns mark only differences larger than the spread this run recorded for those',
            'two rows (combined between-start SD, floored at 5%), always with the fork as the subject:',
            '`🔵 fork X faster than <row>` / `🔴 fork X slower than <row>` (the compared row is named in',
            'the cell, not only in the column header), `·` inside the band, `-` that row is not',
            'measured in this arm. `notes` carries the cell warnings (`*` a declared row has no measurement,',
            '`!` between-start range over 5%, `p` position effect over 5%, dispatch-dominated means the',
            'row is under 20 us so 4T/8T pay thread-region start-up). Per-start ratios and every',
            'chronological sample are in the run directory named at the top of this page (`run*.csv`).',
            'These describe the recorded revisions; they are not a regression claim.',
        ]),
        "",
    ]
    for population in manifest["run_spec"]["populations"]:
        regime = population["regime"]
        # A regime can carry more than one population (two shape grids cannot share one), and
        # both are labelled with the same regime in the CSV, so the cases are narrowed to this
        # population's families rather than to the whole regime.
        families = set(population["families"])
        # A declared axis is taken from what the run recorded; a manifest that predates the
        # axis still gets its tables split by what its rows actually carry, so two values of
        # an axis are never pooled into one table.
        axis_values_lists = [(manifest["run_spec"].get(axis) or sorted({row[axis] for row in rows}))
                             for axis in axes]
        for dtype in manifest["run_spec"]["dtypes"]:
            for axis_values in itertools.product(*axis_values_lists):
                for threads in manifest["threads"]["counts"]:
                    section = ", ".join([regime, dtype]
                                        + [f"{v} {a}" for v, a in zip(axis_values, axes)]
                                        + [f"{threads}T"])
                    head = 3 + len(axis_values)
                    cases = sorted({k[1] for k in best
                                    if k[0] == regime and k[2] == dtype
                                    and k[3:head] == axis_values and k[head] == threads
                                    and k[1].split("/")[0] in families})
                    if not cases:
                        continue
                    out.append(f"## {section} "
                               f"(CPU {manifest['threads']['cpu_sets'].get(str(threads), '?')})")
                    out.append("")
                    # v2 drops the raw ratio column - the per-start ratios and every chronological
                    # sample are in the run's own CSVs - and keeps one verdict column per other row
                    # plus a separate notes column for the cell's warnings.
                    out.append("| case | " + " | ".join(f"{e} (ms)" for e in row_names) + " |"
                               + ("" if not v2 else "".join(f" vs `{e}` |" for e in row_names[1:]))
                               + (" notes |" if v2 else " |"))
                    out.append("|" + "---|" * (len(row_names) + (2 if not v2 else 2 + len(row_names) - 1)))
                    for case in cases:
                        cells = []
                        key = (regime, case, dtype, *axis_values, threads)
                        for engine in row_names:
                            times = best.get(key, {}).get(engine, [])
                            if times and v2:
                                cells.append(f"{statistics.median(times):.6g} ± {statistics.stdev(times):.6g} "
                                             f"(W {statistics.median(within[key][engine]):.6g})")
                            else:
                                cells.append(f"{min(times):.4f}" if times else "-")
                        if v2:
                            balanced, between, position = cell_statistics(series, key, row_names)
                            forks = best.get(key, {}).get(row_names[0]) or []
                            fork_us = statistics.median(forks) if forks else None
                            verdicts = comparison_verdict(best, within, key, row_names)
                            for engine in row_names[1:]:
                                cells.append(verdict_text(verdicts.get(engine), fork_us, engine))
                            notes = []
                            if not balanced:
                                notes.append('`*` one row has no measurement here')
                            if between > 0.05:
                                notes.append('`!` between-start range over 5%')
                            if position and position > 1.05:
                                notes.append('`p` position effect over 5%')
                            if fork_us is not None and fork_us < DISPATCH_US:
                                notes.append('dispatch-dominated (row under 20 us)')
                            cells.append('; '.join(notes) if notes else '')
                        out.append(f"| `{case}` | " + " | ".join(cells) + " |")
                    out.append("")
                    if not population.get("skipped") and threads in [t for (r, t) in skipped
                                                                     if r == regime]:
                        cases_skipped = sorted(skipped[(regime, threads)])
                        out.append(f"Not applicable to these shapes and reported as skipped: "
                                   f"{', '.join('`' + c + '`' for c in cases_skipped)}.")
                        out.append("")
        # A run that recorded its skips records them once for the population, and that record is
        # what the page quotes: it is what the harness reported, not a re-reading of its CSVs.
        if population.get("skipped"):
            out.append(f"Not applicable to this population's shapes and reported as skipped: "
                       f"{', '.join('`' + c + '`' for c in population['skipped'])}.")
            out.append("")
    if v2:
        out.append('Repeated process sets describe observed repeatability under the recorded order '
                   'schedule; they are not a paired baseline/candidate experiment and do not '
                   'establish equal performance between engines.')
        out.append('')
    elif manifest["run_spec"]["aa"] > 1:
        spreads = []
        for key, per_row in best.items():
            for engine, times in per_row.items():
                if len(times) > 1 and min(times) > 0:
                    spreads.append(max(times) / min(times))
        if spreads:
            out.append(f"A/A across {manifest['run_spec']['aa']} complete set repeats: "
                       f"median max/min {statistics.median(spreads):.3f}, "
                       f"worst {max(spreads):.3f}. A comparison is made from paired repeats, "
                       f"not from one table read twice.")
            out.append("")
    else:
        out.append("This run made a single complete set, so it carries no A/A: it is a scan, "
                   "not a paired comparison.")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv=None, root=None):
    if root is not None:
        descriptor.set_root(root)
    d = descriptor.current()
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args(argv)
    run_dir = pathlib.Path(args.run_dir).resolve()
    manifest = load(run_dir / "run.yaml")
    suite = load(d.suite_path(manifest["suite_id"]))
    # Exactly the files the manifest names, not every CSV that happens to be in the directory:
    # the manifest is what the validator checked, and a report built from anything else would
    # publish numbers nothing gated.
    listed = manifest.get("result_files") or []
    if not listed:
        raise SystemExit(f"{run_dir}/run.yaml names no result files")
    missing = [f for f in listed if not (run_dir / f).exists()]
    if missing:
        raise SystemExit(f"{run_dir}: result files named by the manifest are absent: {missing}")
    rows = read_rows([run_dir / f for f in listed])
    (run_dir / "report.md").write_text(render(manifest, suite, rows, run_dir))
    print(f"wrote {run_dir}/report.md from {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
