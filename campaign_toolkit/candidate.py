"""Candidate mode: compare a candidate-revision run against a recorded baseline run.

The procedure is fixed in the campaign's protocol document; this module implements it without knowing
which campaign it serves:

1. the two runs must describe the same machine and the same comparison - same suite declaration,
   same upstream/harness/toolkit identities, same host block (CPU, cache domains, power state), same
   CPU sets and timing policy - only the measured library commit may differ;
2. a predeclared set of cells is checked in-run: the candidate binary still carries the
   unchanged upstream row, so if that row no longer reproduces the baseline within the spread the
   baseline recorded, the machine is not in the baseline state and the comparison is refused;
3. only then is the measured row compared, cell by cell, as the rotation-averaged ratio of the
   candidate row to the recorded baseline row.

Everything here is descriptive: a ratio with the recorded spread beside it, no confidence interval,
no equality claim. Pairing is a precondition rather than a report: a selection the two runs did not
time as the same complete cells is refused instead of compared over an intersection.
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


def same_measured_code(checkout, measured_commit, merged_commit, paths, git=None):
    """The measured paths whose tree hashes differ between two revisions ([] means identical code).

    Measuring a candidate and then measuring it again after merging it into the fork's main branch is
    wasted machine time when the merge did not touch the code; this is the check that says whether it
    did, by comparing the recorded commit's tree hashes with the merged commit's for every measured
    path. A merge that leaves them equal keeps the original measurement valid; a merge that changes one
    invalidates only the cells that depend on it.
    """
    if git is None:
        import subprocess

        def git(path, *args):
            return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True,
                                  text=True).stdout.strip()

    differences = []
    for path in paths:
        try:
            before = git(checkout, 'rev-parse', f'{measured_commit}:{path}' if path != 'Cargo.lock'
                         else f'{measured_commit}:{path}')
            after = git(checkout, 'rev-parse', f'{merged_commit}:{path}' if path != 'Cargo.lock'
                        else f'{merged_commit}:{path}')
        except Exception:                                   # noqa: BLE001 - a missing path is a difference
            differences.append(f'{path}: not present in both revisions')
            continue
        if before != after:
            differences.append(f'{path}: {before[:12]} vs {after[:12]}')
    return differences


def _series(run_dir, engine, repetitions):
    """The public row projection and the full identity of every recorded row of one engine.

    Returns ``(rows, identities)``. ``rows`` is what :func:`series` returns: only rows that were
    timed. ``identities`` maps the same projection to ``timing_evidence.cell`` for every row the
    run recorded, including legitimate skips, so a caller can tell "recorded" from "timed".

    Two rows that alias one projected key are refused rather than silently overwritten: either the
    same repeat was recorded twice, or distinct full identities (a regime or batch the public key
    does not carry) collapsed onto it.
    """
    run_dir = pathlib.Path(run_dir)
    manifest = _manifest(run_dir)
    rows, identities, seen = {}, {}, set()
    for name in manifest['result_files']:
        repeat = name.split('-')[0]
        with (run_dir / name).open(newline='') as handle:
            for row in csv.DictReader(handle):
                if row['row'] != engine:
                    continue
                # The memory order is part of a cell's identity: an optimization that only
                # changes the row-major path must never be averaged with the column-major one.
                key = (int(row['threads']), row['family'], row['dtype'], row['order'],
                       f"{row['m']}x{row['n']}" + (f"x{row['k']}" if row['k'] else ''), row['note'])
                identity = timing_evidence.cell(row)
                if identities.setdefault(key, identity) != identity:
                    raise ValueError(f'{engine}: distinct recorded cells share the projected key '
                                     f'{key}: {identities[key]} vs {identity}')
                if (key, repeat) in seen:
                    raise ValueError(f'{engine}: {repeat} was recorded twice for cell {key}')
                seen.add((key, repeat))
                if row['status'] != 'ok':
                    continue
                rows.setdefault(key, {})[repeat] = statistics.median(
                    timing_evidence.samples(row, repetitions))
    return rows, identities


def series(run_dir, engine, repetitions):
    """{(threads, family, dtype, order, shape, note): {repeat: median ms}} for one engine row."""
    return _series(run_dir, engine, repetitions)[0]


def _paired_series(baseline_run, candidate_run, engine, cells=None):
    """The selected timed rows of one engine, paired across the two runs and complete.

    The selection is the requested cell substrings; every one of them has to match a cell the run
    recorded, skips included, and both runs have to have timed exactly the same selected cells, with
    matching full identities and the declared complete-set repeats present on both sides. Anything
    else raises ValueError: an intersection or a short series would silently shrink the comparison.
    """
    baseline, candidate = _manifest(baseline_run), _manifest(candidate_run)
    repeats = baseline['run_spec'].get('aa')
    if repeats != candidate['run_spec'].get('aa') or not isinstance(repeats, int) or repeats < 2:
        raise ValueError('the runs do not declare the same complete-set count of at least two')
    tokens = [str(cell).strip() for cell in (cells or [])]
    if any(not token for token in tokens):
        raise ValueError('the cell selection contains an empty token')
    base_rows, base_ids = _series(baseline_run, engine, baseline['timing_policy']['repetitions'])
    cand_rows, cand_ids = _series(candidate_run, engine, candidate['timing_policy']['repetitions'])

    def selected(identities):
        if not tokens:
            return set(identities)
        return {key for key in identities
                if any(token in part for token in tokens for part in map(str, key))}

    if tokens:
        for side, identities in (('baseline', base_ids), ('candidate', cand_ids)):
            for token in tokens:
                if not any(token in part for key in identities for part in map(str, key)):
                    raise ValueError(f'the {side} run recorded no {engine} cell matching {token!r}')
    base_selected, cand_selected = selected(base_ids), selected(cand_ids)
    if base_selected != cand_selected:
        raise ValueError(f'{engine}: the runs did not record the same selected cells (including skips)')
    base_timed = {key: base_rows[key] for key in base_selected if key in base_rows}
    cand_timed = {key: cand_rows[key] for key in cand_selected if key in cand_rows}
    if set(base_timed) != set(cand_timed):
        raise ValueError(f'{engine}: the runs did not time the same selected cells: '
                         f'baseline only {sorted(set(base_timed) - set(cand_timed))[:3]}, '
                         f'candidate only {sorted(set(cand_timed) - set(base_timed))[:3]}')
    if not base_timed:
        raise ValueError(f'{engine}: the selection timed no cell in either run')
    for key in sorted(base_selected):
        if base_ids[key] != cand_ids[key]:
            raise ValueError(f'{engine}: cell {key} names different recorded cells in the two runs: '
                             f'{base_ids[key]} vs {cand_ids[key]}')
    expected = {f'run{repeat}' for repeat in range(repeats)}
    for key in sorted(base_timed):
        for side, timed in (('baseline', base_timed), ('candidate', cand_timed)):
            if set(timed[key]) != expected:
                raise ValueError(f'the {side} run timed {sorted(timed[key])} for cell {key}, not the '
                                 f'{repeats} declared complete-set repeats')
    return sorted(base_timed), base_timed, cand_timed


def control_check(baseline_run, candidate_run, engine, cells=None):
    """Compare the unchanged row between the two runs, cell by cell.

    Returns (ok, rows) where rows holds the baseline median, the candidate median, the ratio and
    whether the candidate left the baseline's recorded between-start range. The engine row is the
    unchanged one the candidate binary still carries, so a moved machine shows up here before any
    fork number is read. Raises ValueError when the two runs cannot be paired.
    """
    keys, baseline_series, candidate_series = _paired_series(baseline_run, candidate_run, engine,
                                                             cells)
    rows, ok = [], True
    for key in keys:
        base = baseline_series[key]
        cand = candidate_series[key]
        base_median, cand_median = statistics.median(base.values()), statistics.median(cand.values())
        low, high = min(base.values()), max(base.values())
        outside = not (low * 0.95 <= cand_median <= high * 1.05)
        rows.append({'cell': key, 'baseline_ms': base_median, 'candidate_ms': cand_median,
                     'ratio': cand_median / base_median, 'baseline_range_ms': (low, high),
                     'outside_baseline_range': outside})
        ok = ok and not outside
    return ok, rows


def fork_comparison(baseline_run, candidate_run, fork_engine, cells=None):
    """Rotation-averaged candidate/baseline ratio per cell, with the candidate's spread beside it.

    Raises ValueError when the two runs cannot be paired.
    """
    keys, baseline_series, candidate_series = _paired_series(baseline_run, candidate_run,
                                                             fork_engine, cells)
    rows = []
    for key in keys:
        base, cand = baseline_series[key], candidate_series[key]
        shared = sorted(cand)
        base_median = statistics.median(base[r] for r in shared)
        cand_median = statistics.median(cand[r] for r in shared)
        ratio = math.exp(statistics.fmean(math.log(cand[r]) - math.log(base[r]) for r in shared))
        rows.append({'cell': key, 'baseline_fork_ms': base_median, 'candidate_fork_ms': cand_median,
                     'rotation_averaged_ratio': ratio,
                     'candidate_between_start_sd_ms': statistics.stdev([cand[r] for r in shared])})
    return rows
