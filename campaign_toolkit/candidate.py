"""Candidate mode: compare a candidate-revision run run against a recorded baseline run.

The procedure is fixed in the campaign's protocol document; this module implements it without knowing
which campaign it serves:

1. the two runs must describe the same machine and the same comparison - same suite declaration,
   same upstream/harness/toolkit identities, same host block (CPU, cache domains, power state), same
   CPU sets and timing policy - only the measured library commit may differ;
2. a predeclared overlap set of cells is checked in-run: the candidate binary still carries the
   unchanged upstream row, so if that row no longer reproduces the baseline within the spread the
   baseline recorded, the machine is not in the baseline state and the comparison is refused;
3. only then is the measured row compared, cell by cell, as the rotation-averaged ratio of the candidate
   fork to the recorded baseline fork.

Everything here is descriptive: a ratio with the recorded spread beside it, no confidence interval,
no equality claim, and a cell that was not measured in both runs is reported as missing.
"""
import csv
import math
import pathlib
import statistics

import yaml

from . import timing_evidence

IDENTITY_KEYS = ('upstream', 'harness', 'toolkit')
HOST_KEYS = ('cpu', 'logical_cpus', 'l3', 'l3_domains', 'hostname', 'arch')


def _manifest(run_dir):
    return yaml.safe_load((pathlib.Path(run_dir) / 'run.yaml').read_text())


def identity_differences(baseline, candidate):
    """[] when the candidate run may be compared with the baseline; a list of reasons otherwise."""
    baseline, candidate = _manifest(baseline), _manifest(candidate)
    out = []
    if baseline.get('suite_id') != candidate.get('suite_id'):
        out.append(f"suite: {baseline.get('suite_id')!r} vs {candidate.get('suite_id')!r}")
    if baseline.get('suite_sha256') != candidate.get('suite_sha256'):
        out.append('the suite declaration changed between the runs')
    for key in IDENTITY_KEYS:
        one, two = baseline.get(key) or {}, candidate.get(key) or {}
        if key == 'upstream' and not one and not two:
            continue
        if one.get('commit') != two.get('commit'):
            out.append(f'{key} commit: {one.get("commit")} vs {two.get("commit")}')
    for key in HOST_KEYS:
        one, two = (baseline.get('host') or {}), (candidate.get('host') or {})
        if one.get(key) != two.get(key):
            out.append(f'host {key}: {one.get(key)!r} vs {two.get(key)!r}')
    if (baseline.get('host') or {}).get('power') != (candidate.get('host') or {}).get('power'):
        out.append('host power state differs (boost/governor/epp), which is not comparable')
    if baseline['timing_policy'] != candidate['timing_policy']:
        out.append('timing policy differs')
    if baseline['threads']['cpu_sets'] != candidate['threads']['cpu_sets']:
        out.append('CPU sets differ')
    if baseline['run_spec'].get('aa') != candidate['run_spec'].get('aa'):
        out.append('complete-set count differs')
    return out


def series(run_dir, engine, repetitions):
    """{(threads, family, dtype, shape, note): {repeat: median ms}} for one engine row."""
    run_dir = pathlib.Path(run_dir)
    manifest = _manifest(run_dir)
    rows = {}
    for name in manifest['result_files']:
        repeat = name.split('-')[0]
        with (run_dir / name).open(newline='') as handle:
            for row in csv.DictReader(handle):
                if row['status'] != 'ok' or row['row'] != engine:
                    continue
                key = (int(row['threads']), row['family'], row['dtype'],
                       f"{row['m']}x{row['n']}" + (f"x{row['k']}" if row['k'] else ''), row['note'])
                rows.setdefault(key, {})[repeat] = statistics.median(
                    timing_evidence.samples(row, repetitions))
    return rows


def control_check(baseline_run, candidate_run, engine, cells=None):
    """Compare the unchanged row between the two runs, cell by cell.

    Returns (ok, rows) where rows holds the baseline median, the candidate median, the ratio and
    whether the candidate left the baseline's recorded between-start range. The engine row is the
    unchanged one the candidate binary still carries, so a moved machine shows up here before any
    fork number is read.
    """
    baseline, candidate = _manifest(baseline_run), _manifest(candidate_run)
    baseline_series = series(baseline_run, engine, baseline['timing_policy']['repetitions'])
    candidate_series = series(candidate_run, engine, candidate['timing_policy']['repetitions'])
    keys = sorted(set(baseline_series) & set(candidate_series))
    if cells:
        keys = [key for key in keys
                if any(cell.strip() in part for cell in cells for part in map(str, key))]
    rows, ok = [], True
    for key in keys:
        base = baseline_series[key]
        cand = candidate_series[key]
        if len(base) < 2 or len(cand) < 2:
            continue
        base_median, cand_median = statistics.median(base.values()), statistics.median(cand.values())
        low, high = min(base.values()), max(base.values())
        outside = not (low * 0.95 <= cand_median <= high * 1.05)
        rows.append({'cell': key, 'baseline_ms': base_median, 'candidate_ms': cand_median,
                     'ratio': cand_median / base_median, 'baseline_range_ms': (low, high),
                     'outside_baseline_range': outside})
        ok = ok and not outside
    return ok, rows


def fork_comparison(baseline_run, candidate_run, fork_engine, cells=None):
    """Rotation-averaged candidate/baseline ratio per cell, with the candidate's spread beside it."""
    baseline, candidate = _manifest(baseline_run), _manifest(candidate_run)
    baseline_series = series(baseline_run, fork_engine, baseline['timing_policy']['repetitions'])
    candidate_series = series(candidate_run, fork_engine, candidate['timing_policy']['repetitions'])
    rows = []
    for key in sorted(set(baseline_series) & set(candidate_series)):
        if cells and not any(cell.strip() in part for cell in cells for part in map(str, key)):
            continue
        base, cand = baseline_series[key], candidate_series[key]
        shared = sorted(set(base) & set(cand))
        if len(shared) < 2:
            continue
        base_median = statistics.median(base[r] for r in shared)
        cand_median = statistics.median(cand[r] for r in shared)
        ratio = math.exp(statistics.fmean(math.log(cand[r]) - math.log(base[r]) for r in shared))
        rows.append({'cell': key, 'baseline_fork_ms': base_median, 'candidate_fork_ms': cand_median,
                     'rotation_averaged_ratio': ratio,
                     'candidate_between_start_sd_ms': statistics.stdev([cand[r] for r in shared])})
    return rows
