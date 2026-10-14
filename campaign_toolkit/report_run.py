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
import pathlib
import statistics
import sys

from . import descriptor
from .campaign import case_dims, load, provider_of


def columns():
    return descriptor.current().csv_columns


def read_rows(csv_paths):
    """Rows from the run CSVs, tagged with the repeat they came from.

    A repeat is one complete pass over the declared populations; the harness
    already reduced `--reps` timed iterations to a best time, so a repeat holds
    one number per (case, engine row).
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


def case_name(row):
    shape = case_dims(int(row["m"]), int(row["n"]), row.get("k", ""))
    return f"{row['family']}/{shape}/b{row['batch']}"


def best_per_case(rows):
    """(regime, case, dtype, *axes, threads) -> engine row -> times, and skipped cases.

    A declared case axis is part of the key, so a cell of one order is never pooled with
    the same shape of another.
    """
    axes = descriptor.current().case_axes
    best, skipped = {}, {}
    for row in rows:
        key = (row["regime"], case_name(row), row["dtype"],
               *(row[a] for a in axes), int(row["threads"]))
        if row["status"] == "skipped":
            skipped.setdefault((row["regime"], int(row["threads"])), set()).add(
                f"{row['family']}/{row['m']}x{row['n']}")
            continue
        if row["status"] != "ok":
            continue
        times = best.setdefault(key, {}).setdefault(row["row"], [])
        times.append(float(row["total_ms"]))
    return best, skipped


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


def render(manifest, suite, rows, run_dir):
    d = descriptor.current()
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
    best, skipped = best_per_case(rows)
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
        f"- hardware profile: `{manifest['target_profile']}`",
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
        f"Every row below passed `{d.measured['binary']} verify` on the same grid before timing: the",
        "providers' factorizations are checked against each provider's documented convention",
        "(reconstruction, orthonormality, residuals), not against each other's floating-point",
        "output. Times are milliseconds, the best wall time per case and engine row; with more",
        "than one complete set repeat the table shows the best repeat, and the A/A spread is",
        "reported at its foot.",
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
                             for axis in d.case_axes]
        for dtype in manifest["run_spec"]["dtypes"]:
            for axis_values in itertools.product(*axis_values_lists):
                for threads in manifest["threads"]["counts"]:
                    section = ", ".join([regime, dtype]
                                        + [f"{v} {a}" for v, a in zip(axis_values, d.case_axes)]
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
                    out.append("| case | " + " | ".join(f"{e} (ms)" for e in row_names) + " |")
                    out.append("|" + "---|" * (len(row_names) + 1))
                    for case in cases:
                        cells = []
                        for engine in row_names:
                            key = (regime, case, dtype, *axis_values, threads)
                            times = best.get(key, {}).get(engine, [])
                            cells.append(f"{min(times):.4f}" if times else "-")
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
    if manifest["run_spec"]["aa"] > 1:
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
