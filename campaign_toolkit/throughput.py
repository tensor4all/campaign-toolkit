"""Throughput charts from recorded runs: nominal GFLOP/s against problem size.

The package knows nothing about any campaign's workloads: the operation count for each family, the
series (which engine comes from which recorded run, its colour and marker) and the family list all
arrive as a small config document, so the same code serves any suite that records square problems.

One SVG per family (one panel per thread budget, side by side) plus a markdown file of the same
name holding the plotted numbers, so a reader - or a model - can read the trend without a
rasteriser. The drawing is `charts`: log axes with minor ticks, the legend above the panels, and
the layout checker that fails the render when a label would overlap another or leave its panel.
"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

import yaml

from . import charts


def load_run(run_dir, dtype='f64', order='c'):
    """{(threads, family, size, note, engine): {repeat: median ms}} for square, ok rows."""
    run_dir = Path(run_dir)
    manifest = yaml.safe_load((run_dir / 'run.yaml').read_text())
    cells = defaultdict(dict)
    for name in manifest['result_files']:
        repeat = name.split('-')[0]
        with (run_dir / name).open(newline='') as handle:
            for row in csv.DictReader(handle):
                if row['status'] != 'ok' or row['dtype'] != dtype or row['order'] != order:
                    continue
                if row['m'] != row['n']:
                    continue
                cells[(int(row['threads']), row['family'], int(row['n']), row['note'], row['row'])][repeat] = \
                    statistics.median(json.loads(row['samples_ms']))
    return cells


def points(cells, threads, family, engine, coefficient, complex_dtype=False):
    """[(n, nominal GFLOP/s, between-start sample SD)] for one series in one panel."""
    out = []
    for (t, f, size, note, row), medians in sorted(cells.items()):
        if t != threads or f != family or row != engine or note:
            continue
        values = [ms for ms in medians.values() if ms > 0]
        if len(values) < 2:
            continue
        ms = statistics.median(values)
        gflops = coefficient * (4 if complex_dtype else 1) * size ** 3 / (ms * 1e6)
        out.append((size, gflops, gflops * statistics.stdev(values) / ms))
    return out


def render_family(family, family_spec, series, cells_by_engine, budgets, dtype='f64'):
    """One SVG: a panel per thread budget, side by side, log size against log throughput.

    Returns (svg, dropped tick labels) so a caller can report a crowded axis.
    """
    blocks = {threads: {spec['engine']: points(cells_by_engine[spec['engine']], threads, family, spec['engine'],
                                               family_spec['coefficient'], complex_dtype=dtype == 'c64')
                        for spec in series}
              for threads in budgets}
    data = [p for per_engine in blocks.values() for pts in per_engine.values() for p in pts]
    x_range = charts.log_range([p[0] for p in data]) if data else (10.0, 100.0)
    y_range = charts.log_range([max(p[1], 1e-6) for p in data]) if data else (1.0, 10.0)
    entries = [dict(label=spec['engine'], colour=spec['colour'], marker=spec.get('marker', 'circle'),
                    dash=spec.get('dash'), width=spec.get('width', 1.6), size=spec.get('size', 3.5))
               for spec in series]
    panels = [charts.xy_panel(f'{threads}T', {'label': 'n (matrix size)', 'range': x_range},
                              {'label': 'nominal GFLOP/s', 'range': y_range},
                              [dict(entry, points=blocks[threads][entry['label']]) for entry in entries])
              for threads in budgets]
    return charts.figure(f'{family}: nominal GFLOP/s vs n', entries, panels)


def data_markdown(family, family_spec, series, cells_by_engine, budgets, dtype='f64'):
    lines = [f'# {family} - plotted data (nominal GFLOP/s)', '',
             f'Nominal operation count: `{family_spec["label"]}`. Each value is the median of the process',
             'medians for that cell, `+-` is the between-start sample SD, a dot means that row is not',
             'measured in that arm, and the chart of the same name holds the same numbers.', '']
    for threads in budgets:
        table = {spec['engine']: {x: (y, err) for x, y, err in
                                  points(cells_by_engine[spec['engine']], threads, family, spec['engine'],
                                         family_spec['coefficient'], complex_dtype=dtype == 'c64')}
                 for spec in series}
        sizes = sorted({x for rows in table.values() for x in rows})
        if not sizes:
            continue
        lines += [f'## {threads}T', '', '| n | ' + ' | '.join(spec['engine'] for spec in series) + ' |',
                  '|---|' + '---|' * len(series)]
        for size in sizes:
            lines.append(f'| {size} | ' + ' | '.join(
                f'{table[spec["engine"]][size][0]:.3g} +- {table[spec["engine"]][size][1]:.2g}'
                if size in table[spec['engine']] else '.'
                for spec in series) + ' |')
        lines.append('')
    return '\n'.join(lines) + '\n'


def revisions_line(run_dirs, labels=None):
    """One line naming, per run, its role, vendor arm and the revisions it was measured at, read from
    each run's `run.yaml`; a `None` entry is a run that does not exist and is named as missing."""
    parts, missing = [], []
    for index, run_dir in enumerate(run_dirs):
        if run_dir is None:
            missing.append((labels[index] if labels and index < len(labels) else f'run {index}'))
            continue
        manifest = yaml.safe_load((Path(run_dir) / 'run.yaml').read_text())
        role = 'large tier' if 'large' in str(manifest.get('suite_id', '')) else 'daily'
        short = lambda block: str((manifest.get(block) or {}).get('commit') or 'none')[:7]  # noqa: E731
        parts.append(f'{role} {manifest.get("vendor", {}).get("name", "?")}: fork `{short("library")}`, '
                     f'upstream `{short("upstream")}`, harness `{short("harness")}`')
    line = 'Revisions: ' + '; '.join(parts) + '.'
    if missing:
        line += (' No run was given for ' + ', '.join(missing) + ' (none recorded at this fork revision, and '
                 'an older recording is not mixed in), so the series that would use it stops at the sizes '
                 'of its other run.')
    return line


def page_markdown(config, families, budgets, dtype='f64', revisions=None):
    """The page that links the charts: a table of contents, then one section per family."""
    page = config.get('page', {})
    version = config.get('version', 'v1')
    lines = [f'# {page.get("title", "Throughput charts")}', '']
    lines += [line for line in page.get('notes', [])]
    if revisions:
        lines.append(revisions)
    lines += ['', '## Contents', '',
              '| family (chart, data) | nominal flops |', '|---|---|']
    for family in families:
        spec = config['families'][family]
        lines.append(f'| [{family}](throughput-{family}-{version}.svg) '
                     f'([data](throughput-{family}-{version}.md)) | `{spec["label"]}` |')
    lines.append('')
    for family in families:
        lines += [f'## {family}', '', f'![{family}](throughput-{family}-{version}.svg)', '',
                  f'Plotted numbers: [throughput-{family}-{version}.md](throughput-{family}-{version}.md). '
                  f'Nominal operation count `{config["families"][family]["label"]}`.', '']
    return '\n'.join(lines) + '\n'


def write_report(run_dirs, config, out_dir, budgets=(1, 4, 8), dtype='f64', order='c'):
    """Write `throughput-<family>-<version>.svg` and `.md` for every family in the config."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A run that was not recorded is `None`: its cells are empty and the page says so.
    cells_by_run = [load_run(run_dir, dtype=dtype, order=order) if run_dir is not None else {}
                    for run_dir in run_dirs]
    # A series may name several runs (a daily suite and the large-tier suite, say): the cells are
    # merged, and a size measured in both keeps the later run's value.
    cells_by_series = {}
    for spec in config['series']:
        indexes = spec['run'] if isinstance(spec['run'], list) else [spec['run']]
        merged = {}
        for index in indexes:
            merged.update(cells_by_run[index])
        cells_by_series[spec['engine']] = merged
    written, dropped = [], []
    for family, family_spec in config['families'].items():
        svg, skipped = render_family(family, family_spec, config['series'], cells_by_series, budgets, dtype)
        dropped += [f'{family}:{label}' for label in skipped]
        version = config.get('version', 'v1')
        (out_dir / f'throughput-{family}-{version}.svg').write_text(svg)
        (out_dir / f'throughput-{family}-{version}.md').write_text(
            data_markdown(family, family_spec, config['series'], cells_by_series, budgets, dtype))
        written.append(family)
    (out_dir / 'README.md').write_text(page_markdown(
        config, written, budgets, dtype,
        revisions_line(run_dirs, config.get('page', {}).get('run_labels'))))
    return written, dropped
