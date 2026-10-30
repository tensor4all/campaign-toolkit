"""Trend points and the trend chart: one representative case's median across library revisions.

    python -m campaign_toolkit.trend --out <dir> [--root <campaign>] [--points points.json]...
        [--runs <run dir>...] [--axis pin|week] [--write-points points.json]

A trend point is one number a campaign published: the median time of one case, engine and
thread budget in one recorded run, with the conditions it was measured under. Points are the
exchange format (`schemas/trend-points.schema.json`): a toolkit campaign derives them from its
run directories, another repository writes them from its own records and passes `--points`,
and this module draws them for both.

Which points are joined by a line is the point of the module. A line joins only points of the
same campaign, suite, case, engine and thread budget that were also measured under the same
conditions: hardware profile, timing policy, power state (boost, governor) and vendor arm (an
engine measured in two arms' binaries is two lines). A v1 and a v2 point, or a boost-off and a
boost-on point, are two lines, never one, and the legend names the conditions of each. A run
that recorded no power state is its own condition (`unrecorded`).

The x axis is the pin (`--axis pin`, the default): one position per library commit, labelled by
its version (and commit, when one version spans several commits) or by the commit alone. When
every pin has a version they are in version order, otherwise in the order they were first
measured. The same pin measured again keeps only its newest point. `--axis week` puts every
point at its ISO week instead, the newest point of a week standing for it, so the week-to-week
spread at one pin is visible.

By default the runs are the ones behind the campaign's published pages; experiments that were
never published stay off the chart. The representative cases are declared in the descriptor's
`trend:` list (suite, case key, engines, thread budgets).
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import statistics
import sys

from . import charts, descriptor, jsonschema_lite
from .campaign import load, page_source, record_pages

SCHEMA = 'trend-points.schema.json'
# What a line is drawn under, besides its engine and thread budget: points that differ in any of
# these are never joined.
CONDITIONS = ('campaign', 'profile', 'timing_policy', 'power', 'vendor')
# How each recorded power control reads in the power string; every recorded control is kept, so two
# runs that differ in any of them never share a line.
POWER_TEXT = (('cpufreq_boost', {'0': 'boost off', '1': 'boost on'}, 'boost {}'),
              ('intel_no_turbo', {'1': 'turbo off', '0': 'turbo on'}, 'no_turbo {}'),
              ('governor', {}, '{}'),
              ('energy_performance_preference', {}, 'epp {}'),
              ('scaling_max_freq_khz', {}, 'max {} kHz'),
              ('amd_pstate_status', {}, 'amd-pstate {}'),
              ('platform_profile', {}, 'platform {}'))


def power_of(manifest):
    """Every recorded power control as one comparable, readable string, or `unrecorded`."""
    power = (manifest.get('host') or {}).get('power') or {}
    known = {name for name, _, _ in POWER_TEXT}
    parts = [names.get(str(power[name]), template.format(power[name]))
             for name, names, template in POWER_TEXT if power.get(name) not in (None, '')]
    parts += [f'{name} {power[name]}' for name in sorted(power) if name not in known and power[name] not in (None, '')]
    return ', '.join(parts) or 'unrecorded'


def instant(timestamp):
    """An ISO 8601 timestamp as an aware datetime (a naive one is read as UTC)."""
    stamp = datetime.datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)


def timing_policy_of(manifest):
    return f'v{(manifest.get("timing_policy") or {}).get("version", 1)}'


def case_key(row, v2):
    """A case as its report keys a cell: `<regime>:<family>/<dims>/b<batch>[ [note]] <dtype>[ <axis>...]`.

    The regime (the report's section), the declared case axes and, under v2, the order column the
    report always separates are all in it, so two cells the report keeps apart never share a key.
    """
    from .report_run import case_name
    axes = list(descriptor.current().case_axes)
    if v2 and 'order' not in axes:
        axes.append('order')
    extra = ''.join(f' {row[a]}' for a in axes if row.get(a))
    return f'{row["regime"]}:{case_name(row, variants=v2)} {row["dtype"]}{extra}'


def run_cells(run_dir):
    """(manifest, {(case key, engine, threads): [process medians, ms]}) for the ok rows of a run.

    Under v2 each process's median is re-derived from its raw samples; under v1 a process
    contributes the time it recorded.
    """
    from .timing_evidence import samples
    run_dir = pathlib.Path(run_dir)
    manifest = load(run_dir / 'run.yaml')
    policy = manifest.get('timing_policy') or {}
    v2 = policy.get('version') == 2
    import csv
    cells = {}
    for name in manifest.get('result_files') or []:
        with (run_dir / name).open(newline='') as handle:
            for row in csv.DictReader(handle):
                if row.get('status') != 'ok':
                    continue
                value = (statistics.median(samples(row, policy['repetitions'])) if v2
                         else float(row['total_ms']))
                cells.setdefault((case_key(row, v2), row['row'], int(row['threads'])), []).append(value)
    return manifest, cells


def summary(values):
    """(median, between-start sample SD) of process medians; the SD is 0 for one process."""
    return statistics.median(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def points_from_run(run_dir):
    """Every trend point of one run, as schema documents."""
    d = descriptor.current()
    manifest, cells = run_cells(run_dir)
    library = manifest.get('library') or {}
    try:
        run_id = pathlib.Path(run_dir).resolve().relative_to(d.root / d.data['data_dir']).as_posix()
    except ValueError:
        run_id = pathlib.Path(run_dir).name
    out = []
    for (case, engine, threads), values in sorted(cells.items()):
        median, spread = summary(values)
        out.append(dict(campaign=d.data['id'], suite=manifest['suite_id'], case_key=case, engine=engine,
                        threads=threads, profile=manifest['target_profile'],
                        library_version=library.get('version'), library_commit=str(library.get('commit')),
                        timing_policy=timing_policy_of(manifest), power=power_of(manifest),
                        vendor=(manifest.get('vendor') or {}).get('name'), median=median, spread=spread,
                        unit='ms', run_id=run_id, timestamp=str(manifest['timestamp'])))
    return out


def published_runs():
    """The run directories behind the campaign's published pages, oldest first."""
    d = descriptor.current()
    sources = {page_source(page) for page in record_pages(d.root / d.data['result_dir'])}
    return sorted(s for s in sources if s is not None and (s / 'run.yaml').exists())


def read_points(path):
    """The points of a trend-points document, validated against the schema."""
    document = json.loads(pathlib.Path(path).read_text())
    schema = json.loads((descriptor.schemas_dir() / SCHEMA).read_text())
    jsonschema_lite.validate(document, schema)
    for point in document['points']:
        if point['median'] <= 0:
            raise ValueError(f'{path}: a median of {point["median"]} cannot be drawn on a log axis '
                             f'({point["run_id"]}, {point["case_key"]}, {point["engine"]})')
    return document['points']


def points_document(points):
    return {'schema_version': 1, 'points': points}


def _version_key(version):
    parts = re.findall(r'\d+', version or '')
    return tuple(int(p) for p in parts) if parts else None


def week_of(timestamp):
    year, week, _ = instant(timestamp).astimezone(datetime.timezone.utc).isocalendar()
    return f'{year}-W{week:02d}'


def select(points, entry):
    """The points one `trend:` entry draws."""
    return [p for p in points
            if p['suite'] == entry['suite'] and p['case_key'] == entry['case']
            and p['engine'] in entry['engines'] and p['threads'] in entry['threads']
            and ('vendor' not in entry or p.get('vendor') == entry['vendor'])]


def series(points, axis='pin'):
    """{(engine, threads, *CONDITIONS): {x: point}}, one point per position.

    A position is a library commit (`pin`) or an ISO week (`week`); the newest point measured
    at a position stands for it.
    """
    out = {}
    for point in sorted(points, key=lambda p: instant(p['timestamp'])):
        key = (point['engine'], point['threads'], *(point.get(c) for c in CONDITIONS))
        x = point['library_commit'] if axis == 'pin' else week_of(point['timestamp'])
        out.setdefault(key, {})[x] = point          # later (newer) points replace earlier ones
    return out


def positions(points, axis='pin'):
    """[(x, label)] in axis order."""
    if axis == 'week':
        return [(w, w) for w in sorted({week_of(p['timestamp']) for p in points})]
    first, version = {}, {}
    for point in points:
        commit = point['library_commit']
        first[commit] = min(first.get(commit, instant(point['timestamp'])), instant(point['timestamp']))
        version[commit] = point.get('library_version')
    commits = list(first)
    keys = {c: _version_key(version[c]) for c in commits}
    if all(keys.values()):
        commits.sort(key=lambda c: (keys[c], first[c]))
    else:
        commits.sort(key=lambda c: first[c])
    shared = {v for v in version.values() if v and list(version.values()).count(v) > 1}
    labels = []
    for commit in commits:
        v = version[commit]
        labels.append((commit, f'{v} {commit[:7]}' if v in shared else (v or commit[:12])))
    return labels


def condition_text(conditions, shown=CONDITIONS):
    """The named conditions of a line, as the legend and the table print them."""
    values = dict(zip(CONDITIONS, conditions))
    parts = {'campaign': values['campaign'], 'profile': values['profile'],
             'timing_policy': f'timing {values["timing_policy"]}', 'power': values['power'],
             'vendor': f'{values["vendor"]} arm' if values['vendor'] else 'no vendor arm'}
    return ', '.join(parts[name] for name in shown)


def render(entry, points, axis='pin'):
    """(svg, markdown, dropped tick labels) for one `trend:` entry."""
    chosen = select(points, entry)
    units = sorted({p['unit'] for p in chosen})
    if len(units) > 1:
        raise ValueError(f'{entry["suite"]} {entry["case"]}: points in several units {units}; convert them first')
    flat = [p for p in chosen if p['median'] <= 0]
    if flat:
        raise ValueError(f'{entry["suite"]} {entry["case"]}: a median of {flat[0]["median"]} cannot be drawn on '
                         f'a log axis ({flat[0]["run_id"]})')
    xs = positions(chosen, axis)
    index = {x: i for i, (x, _) in enumerate(xs)}
    lines = series(chosen, axis)
    # The newest condition draws solid; an older one (a v1 contract, a different power state) dashed.
    newest = {}
    for key, by_x in lines.items():
        cond = key[2:]
        stamp = max(instant(p['timestamp']) for p in by_x.values())
        newest[cond] = max(newest.get(cond, stamp), stamp)
    conditions = sorted(newest, key=lambda c: newest[c], reverse=True)
    engines = list(entry['engines'])
    unit = units[0] if units else 'ms'
    values = [v for by_x in lines.values() for p in by_x.values()
              for v in (p['median'], p['median'] + p['spread'], p['median'] - p['spread']) if v > 0]
    y_range = charts.log_range(values) if values else (1.0, 10.0)
    # Conditions every line shares go in one line under the title; the legend names the rest.
    varying = [name for i, name in enumerate(CONDITIONS) if len({c[i] for c in conditions}) > 1]
    common = [name for name in CONDITIONS if name not in varying]
    subtitle = ('all lines: ' + condition_text(conditions[0], common)) if conditions and common else ''
    entries = []
    for key in sorted(lines, key=lambda k: (engines.index(k[0]), conditions.index(k[2:]))):
        if any(e['key'][0] == key[0] and e['key'][2:] == key[2:] for e in entries):
            continue
        label = key[0] + (f' ({condition_text(key[2:], varying)})' if varying else '')
        entries.append(dict(key=key, label=label,
                            colour=charts.PALETTE[engines.index(key[0]) % len(charts.PALETTE)],
                            dash=charts.DASHES[conditions.index(key[2:]) % len(charts.DASHES)],
                            marker=('circle', 'triangle', 'diamond', 'square')[engines.index(key[0]) % 4]))
    panels = []
    for threads in entry['threads']:
        drawn = []
        for spec in entries:
            key = (spec['key'][0], threads, *spec['key'][2:])
            by_x = lines.get(key, {})
            drawn.append(dict(spec, points=[(index[x], p['median'], p['spread']) for x, p in by_x.items()]))
        panels.append(charts.xy_panel(f'{threads}T', {'label': 'pin' if axis == 'pin' else 'week',
                                                      'categories': [label for _, label in xs]},
                                      {'label': f'median time ({unit})', 'range': y_range}, drawn))
    title = entry.get('title') or f'{entry["suite"]}: {entry["case"]}'
    longest = max((len(label) for _, label in xs), default=1)
    panel_h = 360 + 0.62 * charts.FONT['tick'] * longest
    svg, dropped = charts.figure(title, entries, panels, panel_h=panel_h, subtitle=subtitle)
    return svg, markdown(title, entry, xs, lines, entries, axis, subtitle), dropped


def markdown(title, entry, xs, lines, entries, axis, subtitle=''):
    out = [f'# {title} - plotted data', '', *([f'Conditions of {subtitle}.', ''] if subtitle else []),
           f'Median time per {"pin (library commit)" if axis == "pin" else "ISO week"}, with the between-start '
           'spread (`+-`) and the run it came from. A column joins only points measured under the same '
           'campaign, profile, timing policy, power state and vendor arm; a dot means that line has no point '
           'there.', '']
    for threads in entry['threads']:
        columns = [(spec, (spec['key'][0], threads, *spec['key'][2:])) for spec in entries]
        out += [f'## {threads}T', '', f'| {"pin" if axis == "pin" else "week"} | '
                + ' | '.join(spec['label'] for spec, _ in columns) + ' |', '|---|' + '---|' * len(columns)]
        for x, label in xs:
            cells = []
            for _, key in columns:
                point = lines.get(key, {}).get(x)
                cells.append(f'{point["median"]:.4g} +- {point["spread"]:.2g} (`{point["run_id"]}`)'
                             if point else '.')
            out.append(f'| {label} | ' + ' | '.join(cells) + ' |')
        out.append('')
    return '\n'.join(out)


def slug(entry):
    """A file name stem for one entry: readable, and distinct for entries that differ in anything
    (vendor, engines, threads), so two declarations never write the same file."""
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:8]
    return re.sub(r'[^A-Za-z0-9.]+', '-', f'{entry["suite"]}-{entry["case"]}').strip('-') + '-' + digest


def write(entries, points, out_dir, axis='pin'):
    """Write `trend-<slug>[-week].svg` and `.md` per entry; returns [(entry, svg path, md path)]."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in entries:
        svg, text, _ = render(entry, points, axis)
        stem = f'trend-{slug(entry)}' + ('-week' if axis == 'week' else '')
        (out_dir / f'{stem}.svg').write_text(svg)
        (out_dir / f'{stem}.md').write_text(text)
        written.append((entry, out_dir / f'{stem}.svg', out_dir / f'{stem}.md'))
    return written


def main(argv=None, root=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--root', default=None, help='the campaign root (default: CAMPAIGN_ROOT or .)')
    ap.add_argument('--out', type=pathlib.Path, help='where the charts and tables go')
    ap.add_argument('--points', type=pathlib.Path, action='append', default=[],
                    help='a trend-points document to draw (repeatable); replaces the run directories')
    ap.add_argument('--runs', type=pathlib.Path, nargs='+', default=None,
                    help='run directories to draw instead of the ones behind the published pages')
    ap.add_argument('--axis', choices=('pin', 'week'), default='pin')
    ap.add_argument('--write-points', type=pathlib.Path, help='also write the points as one document')
    args = ap.parse_args(argv)
    d = descriptor.set_root(root or args.root or os.environ.get('CAMPAIGN_ROOT') or '.')
    if args.points:
        points = [p for path in args.points for p in read_points(path)]
    else:
        points = [p for run in (args.runs or published_runs()) for p in points_from_run(run)]
    if args.write_points:
        args.write_points.write_text(json.dumps(points_document(points), indent=1) + '\n')
    entries = d.data.get('trend') or []
    if args.out is None and args.write_points:
        return 0            # only the points were asked for
    if not entries:
        print('the descriptor declares no `trend:` entries; nothing to draw')
        return 0
    if args.out is None:
        ap.error('--out is required to draw the declared trends')
    for entry, svg, _ in write(entries, points, args.out, args.axis):
        count = len(select(points, entry))
        print(f'wrote {svg} ({count} points)' + ('' if count else
              f'; WARNING: no point has case key {entry["case"]!r} in suite {entry["suite"]!r}'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
