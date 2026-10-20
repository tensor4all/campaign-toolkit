"""Version-2 raw timing evidence: complete verification census and process sets."""
import csv
import itertools
import json
import math
import pathlib
import re
import statistics


BASE = ('regime', 'family', 'dtype', 'order', 'm', 'n', 'k', 'batch', 'row', 'threads')


def cell(row):
    # Notes distinguish separately timed variants; never pool different operations.
    return tuple(row.get(k, '') for k in BASE) + (row.get('note', ''),)


def samples(row, repetitions):
    values = json.loads(row.get('samples_ms') or 'null')
    if not isinstance(values, list) or len(values) != repetitions:
        raise ValueError('missing raw samples or repetition count mismatch')
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('samples must be finite nonnegative numbers')
    return values


def expected_cells(manifest):
    spec = manifest['run_spec']
    expected = set()
    for pop in spec['populations']:
        for family, dtype, order, shape, batch, engine, threads in itertools.product(
                pop['families'], spec['dtypes'], spec.get('order', ['']), pop['n'],
                pop['batch'], spec['engines'], manifest['threads']['counts']):
            dims = str(shape).split('x')
            if len(dims) == 1:
                m = n = dims[0]
                k = m if family == 'gemm' else ''
            elif len(dims) == 2:
                m, n = dims
                k = ''
            else:
                m, k, n = dims
            expected.add(tuple(map(str, (pop['regime'], family, dtype, order, m, n, k,
                                         batch, engine, threads))))
    return expected


def validate(manifest, run_dir):
    """Raise ValueError for incomplete, duplicate, failed or inconsistent v2 evidence."""
    policy = manifest['timing_policy']
    for identity in ('library', 'upstream', 'harness', 'toolkit'):
        if identity == 'upstream' and identity not in manifest:
            continue
        source = manifest.get(identity) or {}
        if not source.get('commit') or source.get('dirty') is not False:
            raise ValueError(f'v2 requires clean committed {identity} identity')
    binary = manifest.get('binary') or {}
    if not binary.get('path') or not re.fullmatch(r'[0-9a-f]{64}', binary.get('sha256', '')):
        raise ValueError('v2 requires the measured binary path and SHA-256')
    spec = manifest['run_spec']
    repeats = spec['aa']
    repetitions = policy['repetitions']
    engines = spec['engines']
    if policy['statistic'] != 'median' or repetitions < 2:
        raise ValueError('v2 requires median and at least two timed calls per row')
    if repeats < 2:
        raise ValueError('v2 requires at least two complete process sets')
    if not engines or repeats % len(engines) or policy.get('row_rotation_step') != 1:
        raise ValueError('v2 requires complete engine-order rotation cycles')
    run_dir = pathlib.Path(run_dir)

    def read(name):
        if pathlib.Path(name).name != name:
            raise ValueError('evidence filenames must be basenames')
        with (run_dir / name).open(newline='') as handle:
            return list(csv.DictReader(handle))

    census = {}
    if not manifest.get('verification_files'):
        raise ValueError('v2 requires the successful verification census')
    for name in manifest['verification_files']:
        for row in read(name):
            key = cell(row)
            if key in census or row.get('status') not in ('ok', 'skipped'):
                raise ValueError(f'{name}: duplicate or failed verification cell {key}')
            census[key] = row['status']
    bases = {key[:-1] for key in census}
    expected = expected_cells(manifest)
    # A suite may narrow the grid its verify runs on: the check is stated by the run rather than
    # implied by the timing grid, because recomputing every case can cost more than the run it
    # guards. A narrowed grid must be a subset of what the declaration covers - the recorder
    # refuses one that is not - so the census is compared with the cells it claims to have checked.
    grid = (manifest.get('verification') or {}).get('grid') or {}
    narrowed = {str(v) for v in grid.get('n', [])}
    narrow_threads = {str(v) for v in grid.get('threads', [])}
    checked = expected
    if narrowed:
        # The recorder narrows a population only if the narrowed shapes name some of it; a
        # population the grid does not name keeps its own (a canary with one shape), so the same
        # rule has to be applied here or the census it produced looks incomplete.
        by_regime = {}
        for k in expected:
            by_regime.setdefault(k[0], set()).add(k[BASE.index('n')])
        checked = {
            k for k in expected
            if k[BASE.index('n')] in narrowed
            or not (by_regime[k[0]] & narrowed)
        }
    if bases != checked:
        raise ValueError('verification census does not match the declared run grid')
    # The process groups are the *declared* ones: the verify grid may be narrower than the timing
    # grid, and a run must still carry a timing file for every regime at every declared budget.
    groups = {(str(pop['regime']), str(t))
              for pop in manifest['run_spec']['populations']
              for t in manifest['threads']['counts']}
    seen_groups = set()
    for name in manifest['result_files']:
        match = re.match(r'^run(\d+)-', name)
        if not match or int(match[1]) not in range(repeats):
            raise ValueError(f'{name}: invalid complete-set repeat identity')
        repeat = int(match[1])
        rows = read(name)
        present_groups = {(r.get('regime'), r.get('threads')) for r in rows}
        if len(present_groups) != 1:
            raise ValueError(f'{name}: expected one population/thread process')
        group = next(iter(present_groups))
        if (repeat, group) in seen_groups or group not in groups:
            raise ValueError(f'{name}: duplicate or unexpected process group')
        seen_groups.add((repeat, group))
        # The declared cells of this process group, with the census's status where the verify
        # covered the cell. A cell the narrowed grid did not check (a larger shape of a family
        # that was checked below it) is still a declared cell that must appear and succeed; what
        # it does not get is a status comparison, because there is no verified status to compare
        # with.
        expected = {key: census.get(key, 'ok')
                    for key in expected_cells(manifest) if (key[0], key[9]) == group}
        actual = {}
        order_by_case = {}
        for row in rows:
            key = cell(row)
            if key in actual or row.get('status') not in ('ok', 'skipped'):
                raise ValueError(f'{name}: duplicate or failed timed cell {key}')
            actual[key] = row['status']
            case = key[:8] + key[9:10]
            order = order_by_case.setdefault(case, [])
            if not order or order[-1] != row['row']:
                order.append(row['row'])
            budget = row['threads']
            if row.get('rayon_threads') != budget or row.get('vendor_threads') != budget:
                raise ValueError(f'{name}: observed thread budgets differ from requested {budget}')
            if row['status'] == 'ok':
                values = samples(row, repetitions)
                total = statistics.median(values)
                aggregate = float(row['total_ms'])
                per_item = float(row['per_item_us'])
                if (not math.isfinite(aggregate) or not math.isfinite(per_item)
                        or total <= 0 or abs(aggregate - total) > 1e-6
                        or abs(per_item - total * 1000 / int(row['batch'])) > 1e-6):
                    raise ValueError(f'{name}: aggregate is inconsistent with chronological samples')
            elif row.get('samples_ms'):
                raise ValueError(f'{name}: skipped row carries timing samples')
        if actual != expected:
            raise ValueError(f'{name}: missing, unexpected or status-changed timed cells')
        rotation = repeat % len(engines)
        wanted_order = engines[rotation:] + engines[:rotation]
        if any(order != wanted_order for order in order_by_case.values()):
            raise ValueError(f'{name}: engine order does not match recorded rotation')
    if seen_groups != set(itertools.product(range(repeats), groups)):
        raise ValueError('missing complete-set process repeat')
