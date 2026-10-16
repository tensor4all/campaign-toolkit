"""Throughput charts from recorded runs: nominal GFLOP/s against problem size.

The package knows nothing about any campaign's workloads: the operation count for each family, the
series (which engine comes from which recorded run, its colour and marker) and the family list all
arrive as a small config document, so the same code serves any suite that records square problems.

One SVG per family (three panels, one per thread budget) plus a markdown file of the same name
holding the plotted numbers, so a reader - or a model - can read the trend without a rasteriser.
A layout checker fails the render when a label would overlap another or leave its panel: a chart
that cannot be checked is a chart that ships broken.
"""
import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PANEL_W, PANEL_H = 620, 480
PAD_L, PAD_R, PAD_T, PAD_B = 168, 24, 56, 44
FONT = {'title': 26, 'panel': 24, 'tick': 17, 'axis': 19, 'legend': 16}


@dataclass
class Layout:
    """Text bounding boxes, so a layout bug is an assertion rather than a surprise on the page."""
    boxes: list = field(default_factory=list)
    dropped: list = field(default_factory=list)

    def tick(self, x, y, size, content, anchor='start', panel=None):
        """A tick label, dropped when it would collide: a crowded axis is unreadable, and the
        gridline stays. Returns the label or an empty string."""
        try:
            return self.text(x, y, size, content, anchor, panel=panel)
        except ValueError:
            self.dropped.append(content)
            return ''

    def text(self, x, y, size, content, anchor='start', rotate=False, panel=None):
        width = 0.62 * size * len(content)
        if rotate:                      # the text runs along the vertical axis
            x0, x1 = x - size * 0.25, x + size * 0.9
            width = size
        elif anchor == 'middle':
            x0, x1 = x - width / 2, x + width / 2
        elif anchor == 'end':
            x0, x1 = x - width, x
        else:
            x0, x1 = x, x + width
        if rotate:
            half = 0.62 * size * len(content) / 2
            box = (x0, y - half, x1, y + half, panel, content)
        else:
            box = (*((x0, y - size * 0.9, x1, y + size * 0.25)), panel, content)
        for other in self.boxes:
            if other[4] is not panel:
                continue
            if box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]:
                raise ValueError(f'overlapping text: {content!r} {[round(v, 1) for v in box[:4]]} '
                                 f'with {other[5]!r} {[round(v, 1) for v in other[:4]]}')
        if panel:
            px, py = panel
            if (box[0] < px - 1 or box[2] > px + PANEL_W + 1
                    or box[1] < py - 1 or box[3] > py + PANEL_H + 1):
                raise ValueError(f'text {content!r} leaves its panel')
        self.boxes.append(box)
        transform = f' transform="rotate(-90 {x:.1f} {y:.1f})"' if rotate else ''
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}"{transform}>'
                f'{content}</text>')


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


def marker(kind, x, y, colour, size):
    if kind == 'triangle':
        return (f'<polygon points="{x:.1f},{y - size:.1f} {x - size:.1f},{y + size * 0.8:.1f} '
                f'{x + size:.1f},{y + size * 0.8:.1f}" fill="{colour}"/>')
    if kind == 'diamond':
        return (f'<polygon points="{x:.1f},{y - size:.1f} {x - size:.1f},{y:.1f} {x:.1f},{y + size:.1f} '
                f'{x + size:.1f},{y:.1f}" fill="{colour}"/>')
    if kind == 'square':
        return (f'<rect x="{x - size:.1f}" y="{y - size:.1f}" width="{2 * size:.1f}" height="{2 * size:.1f}" '
                f'fill="{colour}"/>')
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" fill="{colour}"/>'


def render_panel(layout, family, threads, blocks, coefficient, origin, series):
    ox, oy = origin
    x0, y0 = ox + 150, oy + 32
    width, height = PANEL_W - 174, PANEL_H - 100
    data = [p for pts in blocks.values() for p in pts]
    panel_key = (ox, oy)
    if not data:
        return f'<g><text x="{x0}" y="{y0 + 20}" font-size="{FONT["panel"]}">no data</text></g>'
    xs = [p[0] for p in data]
    ys = [max(p[1], 1e-6) for p in data]
    lx0, lx1 = math.log10(min(xs)), math.log10(max(xs)) + 0.02
    ly0, ly1 = math.log10(min(ys)) - 0.25, math.log10(max(ys)) + 0.25
    scale_x = lambda v: x0 + (math.log10(v) - lx0) / (lx1 - lx0) * width        # noqa: E731
    scale_y = lambda v: y0 + height - (math.log10(max(v, 1e-6)) - ly0) / (ly1 - ly0) * height  # noqa: E731
    out = [f'<rect x="{ox + 6}" y="{oy + 6}" width="{PANEL_W - 12}" height="{PANEL_H - 12}" fill="#ffffff" '
           f'stroke="#d0d0d0"/>', layout.text(ox + 16, oy + 32, FONT['panel'], f'{threads}T')]
    for size in sorted(set(xs)):
        out.append(f'<line x1="{scale_x(size):.1f}" y1="{y0}" x2="{scale_x(size):.1f}" y2="{y0 + height}" '
                   f'stroke="#f2f2f2"/>')
        out.append(layout.tick(scale_x(size), y0 + height + 26, FONT['tick'], f'{size}', 'middle',
                               panel=panel_key))
    for power in range(math.ceil(ly0), math.floor(ly1) + 1):
        value = 10.0 ** power
        out.append(f'<line x1="{x0}" y1="{scale_y(value):.1f}" x2="{x0 + width}" y2="{scale_y(value):.1f}" '
                   f'stroke="#f2f2f2"/>')
        out.append(layout.tick(x0 - 10, scale_y(value) + 6, FONT['tick'], f'{value:g}', 'end',
                               panel=panel_key))
    out.append(f'<rect x="{x0}" y="{y0}" width="{width}" height="{height}" fill="none" stroke="#c8c8c8"/>')
    out.append(layout.text(ox + 150, oy + PANEL_H - 14, FONT['axis'], 'n (matrix size)', 'start', panel=panel_key))
    out.append(layout.text(ox + 30, oy + PANEL_H / 2, FONT['axis'], 'nominal GFLOP/s', 'middle', rotate=True,
                           panel=panel_key))
    for spec in series:
        pts = blocks.get(spec['engine']) or []
        if not pts:
            continue
        dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get('dash') else ''
        out.append('<polyline points="' + ' '.join(f'{scale_x(x):.1f},{scale_y(y):.1f}' for x, y, _ in pts)
                   + f'" fill="none" stroke="{spec["colour"]}" stroke-width="{spec.get("width", 1.3)}"{dash}/>')
        for x, y, err in pts:
            out.append(f'<line x1="{scale_x(x):.1f}" y1="{scale_y(y - err):.1f}" x2="{scale_x(x):.1f}" '
                       f'y2="{scale_y(y + err):.1f}" stroke="{spec["colour"]}" stroke-width="1.1"/>')
            out.append(marker(spec.get('marker', 'circle'), scale_x(x), scale_y(y), spec['colour'],
                              spec.get('size', 3)))
    legend_row = 2.1 * FONT['legend']
    legend_w = 13.6 * FONT['legend']
    legend_x = x0 + width - legend_w + 8
    legend_y = y0 + height - 10 - legend_row * len(series)
    out.append(f'<rect x="{legend_x - 8}" y="{legend_y - 12}" width="{legend_w:.0f}" '
               f'height="{legend_row * len(series) + 16:.0f}" fill="#ffffff" fill-opacity="0.86" '
               f'stroke="#e0e0e0"/>')
    for index, spec in enumerate(series):
        row_y = legend_y + index * legend_row
        dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get('dash') else ''
        out.append(f'<line x1="{legend_x}" y1="{row_y}" x2="{legend_x + 30}" y2="{row_y}" '
                   f'stroke="{spec["colour"]}" stroke-width="{spec.get("width", 1.3)}"{dash}/>')
        out.append(marker(spec.get('marker', 'circle'), legend_x + 15, row_y, spec['colour'],
                          spec.get('size', 3) * 1.5))
        out.append(layout.text(legend_x + 38, row_y + 5, FONT['legend'], spec['engine'], panel=panel_key))
    return '<g>' + ''.join(out) + '</g>'


def render_family(family, family_spec, series, cells_by_engine, budgets, dtype='f64'):
    """Returns (svg, dropped_tick_labels) so a caller can report a crowded axis."""
    """One SVG: a panel per thread budget, stacked so a page does not have to scale the text down."""
    layout = Layout()
    width = PAD_L + PANEL_W + PAD_R
    height = PAD_T + len(budgets) * PANEL_H + PAD_B
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           layout.text(12, 30, FONT['title'], f'{family}: nominal GFLOP/s vs n')]
    for index, threads in enumerate(budgets):
        blocks = {spec['engine']: points(cells_by_engine[spec['engine']], threads, family, spec['engine'],
                                         family_spec['coefficient'], complex_dtype=dtype == 'c64')
                  for spec in series}
        svg.append(render_panel(layout, family, threads, blocks, family_spec['coefficient'],
                                (PAD_L, PAD_T + index * PANEL_H), series))
    svg.append('</svg>')
    return '\n'.join(svg) + '\n', layout.dropped


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


def page_markdown(config, families, budgets, dtype='f64'):
    """The page that links the charts: a table of contents, then one section per family."""
    page = config.get('page', {})
    version = config.get('version', 'v1')
    lines = [f'# {page.get("title", "Throughput charts")}', '']
    lines += [line for line in page.get('notes', [])]
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
    cells_by_run = [load_run(run_dir, dtype=dtype, order=order) for run_dir in run_dirs]
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
    (out_dir / 'README.md').write_text(page_markdown(config, written, budgets, dtype))
    return written, dropped
