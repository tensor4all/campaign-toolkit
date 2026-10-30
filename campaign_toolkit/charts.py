"""Shared chart parts: the layout checker, log axes with minor ticks, a legend above the plot,
panels side by side, and the case dot plot.

Every chart a campaign site shows is drawn here, so the rules hold for all of them:

* a log axis always carries minor ticks: a gridline and a short tick at 2..9 times each decade,
  so a reader can tell 2 from 5 without counting pixels;
* panels sit side by side, at most `PANELS_PER_ROW` to a row, so one thread budget per panel
  reads across instead of down;
* the legend sits above the panels, outside every plot area, so it never hides a point;
* text is sized for the page: the site shows a chart at `DISPLAY_PX` wide (`CSS`), and every
  label is at least `MIN_TEXT_PX` there. `effective_text_px` is what the test checks.

A label that would overlap another, or leave its panel, fails the render (`Layout`): a chart
that cannot be checked is a chart that ships broken. A crowded tick label is dropped instead,
and the caller can report it; its gridline stays.
"""
import html
import math
import re
from dataclasses import dataclass, field

DISPLAY_PX = 72 * 16     # the figure width on the page: 72rem at the default 16 px root size
MIN_TEXT_PX = 12         # the smallest text a reader is asked to read at that width
PANELS_PER_ROW = 3
PANEL_W, PANEL_H = 560, 440
GAP = 24                 # between panels, and around the figure
FONT = {'title': 26, 'panel': 22, 'tick': 20, 'axis': 20, 'legend': 20}
PALETTE = ('#1f77b4', '#d62728', '#2ca02c', '#9467bd', '#ff7f0e', '#8c564b', '#e377c2', '#17becf')
DASHES = (None, '7 4', '2 3', '10 3 2 3')
CASE_LABEL_CHARS = 40    # a longer case label is shortened on the chart; the data table keeps it
# The widest figure whose smallest text (ticks, legend) still shows at MIN_TEXT_PX at DISPLAY_PX.
MAX_WIDTH = DISPLAY_PX * min(FONT['tick'], FONT['legend']) / MIN_TEXT_PX

# The site's chart block: one chart per row at the full content width (72rem), never a grid of
# thumbnails, so the text keeps the size `MIN_TEXT_PX` is computed for.
CSS = """
.charts{display:grid;grid-template-columns:1fr;gap:1.6rem;max-width:72rem}
.charts figure{margin:0}
.charts img,.charts svg{display:block;width:100%;height:auto;border:1px solid #eee;background:#fff}
.charts figcaption{font-size:.9rem;color:#444;margin-top:.3rem}
"""


@dataclass
class Layout:
    """Text bounding boxes, so a layout bug is an assertion rather than a surprise on the page.

    `panel` is the rectangle `(x, y, width, height)` the text belongs to: text must stay inside
    it, and only text of the same rectangle is checked for overlap.
    """
    boxes: list = field(default_factory=list)
    dropped: list = field(default_factory=list)

    def tick(self, x, y, size, content, anchor='start', panel=None, rotate=False, cls='tick'):
        """A tick label, dropped when it would collide: a crowded axis is unreadable, and the
        gridline stays. Returns the label or an empty string."""
        try:
            return self.text(x, y, size, content, anchor, rotate=rotate, panel=panel, cls=cls)
        except ValueError:
            self.dropped.append(content)
            return ''

    def text(self, x, y, size, content, anchor='start', rotate=False, panel=None, cls=None):
        length = 0.62 * size * len(content)
        if rotate:                      # turned -90 degrees: the text runs up the page
            x0, x1 = x - size * 0.9, x + size * 0.25
            y0, y1 = {'middle': (y - length / 2, y + length / 2), 'end': (y, y + length),
                      'start': (y - length, y)}[anchor]
        else:
            x0, x1 = {'middle': (x - length / 2, x + length / 2), 'end': (x - length, x),
                      'start': (x, x + length)}[anchor]
            y0, y1 = y - size * 0.9, y + size * 0.25
        box = (x0, y0, x1, y1, panel, content)
        for other in self.boxes:
            if other[4] != panel:
                continue
            if box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]:
                raise ValueError(f'overlapping text: {content!r} {[round(v, 1) for v in box[:4]]} '
                                 f'with {other[5]!r} {[round(v, 1) for v in other[:4]]}')
        if panel:
            px, py, pw, ph = panel
            if x0 < px - 1 or x1 > px + pw + 1 or y0 < py - 1 or y1 > py + ph + 1:
                raise ValueError(f'text {content!r} leaves its panel')
        self.boxes.append(box)
        transform = f' transform="rotate(-90 {x:.1f} {y:.1f})"' if rotate else ''
        klass = f' class="{cls}"' if cls else ''
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}"{klass}{transform}>'
                f'{html.escape(content)}</text>')


def effective_text_px(svg):
    """[(class, content, px)]: each text's font size as shown at `DISPLAY_PX` wide."""
    width = float(re.search(r'<svg[^>]* width="([0-9.]+)"', svg).group(1))
    out = []
    for match in re.finditer(r'<text [^>]*font-size="([0-9.]+)"[^>]*>([^<]*)</text>', svg):
        klass = re.search(r'class="([^"]+)"', match.group(0))
        out.append((klass.group(1) if klass else '', html.unescape(match.group(2)),
                    float(match.group(1)) * DISPLAY_PX / width))
    return out


def log_ticks(lo, hi):
    """([major], [minor]) inside [lo, hi]: the decades, and 2..9 times each decade."""
    major, minor = [], []
    for power in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for mantissa in range(1, 10):
            value = mantissa * 10.0 ** power
            if lo * (1 - 1e-9) <= value <= hi * (1 + 1e-9):
                (major if mantissa == 1 else minor).append(value)
    return major, minor


def log_range(values, pad=0.08):
    """(lo, hi) of a log axis over positive values: padded, and widened about its centre until it
    holds at least two ticks, so even a nearly flat trend has a scale to be read against."""
    values = [v for v in values if v > 0]
    lo, hi = math.log10(min(values)) - pad, math.log10(max(values)) + pad
    while sum(map(len, log_ticks(10 ** lo, 10 ** hi))) < 2:
        lo, hi = lo - 0.05, hi + 0.05
    return 10 ** lo, 10 ** hi


def tick_text(value):
    return f'{value:g}'


def log_axis(layout, axis, lo, hi, plot, panel, font=FONT['tick']):
    """Gridlines, short ticks and labels of one log axis over the plot area `plot`.

    Majors are always labelled; when that leaves fewer than two labels, the 2x and 5x minors are
    labelled too, then every minor, so a narrow axis still names its scale. Minor gridlines and ticks
    carry `class="minor"` and `data-axis`, so a test can count them.
    """
    x0, y0, width, height = plot
    span = math.log10(hi) - math.log10(lo)
    if axis == 'x':
        pos = lambda v: x0 + (math.log10(v) - math.log10(lo)) / span * width   # noqa: E731
    else:
        pos = lambda v: y0 + height - (math.log10(v) - math.log10(lo)) / span * height   # noqa: E731
    major, minor = log_ticks(lo, hi)
    out = []
    for value, kind in [*((v, 'major') for v in major), *((v, 'minor') for v in minor)]:
        p = pos(value)
        tick = 8 if kind == 'major' else 4
        colour = '#dddddd' if kind == 'major' else '#f0f0f0'
        attrs = f' class="{kind}" data-axis="{axis}"'
        if axis == 'x':
            out.append(f'<line x1="{p:.1f}" y1="{y0}" x2="{p:.1f}" y2="{y0 + height}" stroke="{colour}"{attrs}/>')
            out.append(f'<line x1="{p:.1f}" y1="{y0 + height}" x2="{p:.1f}" y2="{y0 + height + tick}" '
                       f'stroke="#888888"{attrs}/>')
        else:
            out.append(f'<line x1="{x0}" y1="{p:.1f}" x2="{x0 + width}" y2="{p:.1f}" stroke="{colour}"{attrs}/>')
            out.append(f'<line x1="{x0 - tick}" y1="{p:.1f}" x2="{x0}" y2="{p:.1f}" stroke="#888888"{attrs}/>')
    labelled = list(major)
    if len(labelled) < 2:
        labelled += [v for v in minor if round(v / 10 ** math.floor(math.log10(v))) in (2, 5)]
    if len(labelled) < 2:
        labelled += [v for v in minor if v not in labelled]
    for value in labelled:
        p = pos(value)
        if axis == 'x':
            out.append(layout.tick(p, y0 + height + 12 + font, font, tick_text(value), 'middle', panel=panel))
        else:
            out.append(layout.tick(x0 - 12, p + font * 0.32, font, tick_text(value), 'end', panel=panel))
    return out, pos


def category_positions(count, start, length):
    """Evenly spaced centres for `count` categories over [start, start + length]."""
    step = length / max(count, 1)
    return [start + step * (index + 0.5) for index in range(count)]


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


def legend(layout, entries, x, y, width, font=FONT['legend']):
    """Legend entries in equal columns across `width` starting at (x, y), outside every plot area.

    Each entry is a dict with `label`, `colour` and optionally `marker`, `dash`, `width`,
    `size`. Returns (svg parts, height used).
    """
    row_h = 1.7 * font
    fit = int((width - 72) / (0.62 * font))          # the longest label one column can hold
    entries = [dict(entry, label=short_label(entry['label'], fit)) for entry in entries]
    column_w = max(44 + 0.62 * font * len(entry['label']) + 28 for entry in entries)
    columns = max(1, min(len(entries), int(width // column_w)))
    rows = -(-len(entries) // columns)
    height = rows * row_h + row_h * 0.2
    box = (x, y, width, height)
    out = []
    for index, entry in enumerate(entries):
        ex, ey = x + (index % columns) * column_w, y + (index // columns + 0.6) * row_h
        dash = f' stroke-dasharray="{entry["dash"]}"' if entry.get('dash') else ''
        out.append(f'<line x1="{ex:.1f}" y1="{ey:.1f}" x2="{ex + 34:.1f}" y2="{ey:.1f}" '
                   f'stroke="{entry["colour"]}" stroke-width="{entry.get("width", 1.6) + 0.6}"{dash}/>')
        out.append(marker(entry.get('marker', 'circle'), ex + 17, ey, entry['colour'],
                          max(entry.get('size', 3.5), 3.5) * 1.3))
        out.append(layout.text(ex + 44, ey + font * 0.35, font, entry['label'], panel=box, cls='legend'))
    return out, height


def grid(count):
    """[(row, column)] of `count` panels laid out side by side, `PANELS_PER_ROW` to a row."""
    return [(index // PANELS_PER_ROW, index % PANELS_PER_ROW) for index in range(count)]


def svg_document(width, height, parts):
    return '\n'.join([f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
                      f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="sans-serif">',
                      f'<rect width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>',
                      *parts, '</svg>']) + '\n'


def figure(title, entries, panels, panel_w=PANEL_W, panel_h=PANEL_H, lead_w=0, lead=None, subtitle=''):
    """One SVG: title (and an optional subtitle line), legend above, then the panels side by side.

    `panels` is a list of callables `draw(layout, rect) -> [svg parts]`, one per panel;
    `lead`, when given, draws a column `lead_w` wide at the left of every row (a dot plot's case
    labels), called as `lead(layout, rect)`. Returns (svg, dropped tick labels).
    """
    layout = Layout()
    cells = grid(len(panels))
    columns = min(len(panels), PANELS_PER_ROW) or 1
    width = GAP + lead_w + columns * panel_w + (columns - 1) * GAP + GAP
    # Room for the widest legend entry, the title and the subtitle on one line each, within the
    # readable width; what still does not fit is shortened below.
    widest = max([72 + 0.62 * FONT['legend'] * len(entry['label']) + lead_w for entry in entries]
                 + [0.62 * FONT['title'] * len(title), 0.62 * FONT['legend'] * len(subtitle)])
    width = max(width, min(MAX_WIDTH, 2 * GAP + widest + 4))
    # The title and subtitle are fitted to the figure (shortened in the middle), never clipped.
    header = (0, 0, width, GAP + FONT['title'] + 14 + (1.5 * FONT['legend'] if subtitle else 0))
    fit = lambda text, size: short_label(text, int((width - 2 * GAP) / (0.62 * size)))   # noqa: E731
    parts = [layout.text(GAP, GAP + FONT['title'], FONT['title'], fit(title, FONT['title']), panel=header,
                         cls='title')]
    if subtitle:
        parts.append(layout.text(GAP, header[3] - 6, FONT['legend'], fit(subtitle, FONT['legend']), panel=header,
                                 cls='subtitle'))
    legend_parts, legend_h = legend(layout, entries, GAP + lead_w, header[3],
                                    width - 2 * GAP - lead_w) if entries else ([], 0)
    parts += legend_parts
    top = header[3] + legend_h + 10
    rows = max((row for row, _ in cells), default=0) + 1
    for row in range(rows):
        if lead is not None:
            parts += lead(layout, (GAP, top + row * (panel_h + GAP), lead_w, panel_h))
    for (row, column), draw in zip(cells, panels):
        rect = (GAP + lead_w + column * (panel_w + GAP), top + row * (panel_h + GAP), panel_w, panel_h)
        parts += draw(layout, rect)
    height = top + rows * panel_h + (rows - 1) * GAP + GAP
    if width > MAX_WIDTH + 0.5:
        raise ValueError(f'a figure {width:.0f} wide would show its text below {MIN_TEXT_PX} px')
    return svg_document(width, height, parts), layout.dropped


def panel_frame(layout, rect, title):
    x, y, w, h = rect
    return [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#ffffff" stroke="#d0d0d0"/>',
            layout.text(x + 14, y + 10 + FONT['panel'], FONT['panel'], title, panel=rect, cls='panel')]


def xy_panel(title, x_axis, y_axis, series):
    """A panel drawer: log y against a log x (`x_axis['log']`) or against categories.

    `x_axis` / `y_axis` are dicts: `label`, and either `range` (lo, hi) for a log axis or
    `categories` (labels in order) for a category axis (x only). `series` is a list of dicts:
    the legend fields plus `points` [(x, y, err)], x a value or a category index, and `line`
    (draw the polyline, default true).
    """
    def draw(layout, rect):
        x, y, w, h = rect
        categories = x_axis.get('categories')
        bottom = 80
        if categories is not None:
            bottom = 34 + 0.62 * FONT['tick'] * max((len(c) for c in categories), default=1) + 34
        plot = (x + 104, y + 24 + FONT['panel'], w - 104 - 18, h - 24 - FONT['panel'] - bottom)
        out = panel_frame(layout, rect, title)
        y_lo, y_hi = y_axis['range']
        y_parts, ypos = log_axis(layout, 'y', y_lo, y_hi, plot, rect)
        out += y_parts
        px, py, pw, ph = plot
        if categories is not None:
            centres = category_positions(len(categories), px, pw)
            xpos = lambda i: centres[i]   # noqa: E731
            for index, label in enumerate(categories):
                out.append(f'<line x1="{centres[index]:.1f}" y1="{py}" x2="{centres[index]:.1f}" '
                           f'y2="{py + ph}" stroke="#f0f0f0"/>')
                out.append(layout.tick(centres[index] + FONT['tick'] * 0.32, py + ph + 12, FONT['tick'], label,
                                       'end', panel=rect, rotate=True))
        else:
            x_parts, xpos = log_axis(layout, 'x', *x_axis['range'], plot, rect)
            out += x_parts
        out.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="none" stroke="#c8c8c8"/>')
        out.append(layout.text(px + pw / 2, y + h - 12, FONT['axis'], x_axis['label'], 'middle',
                               panel=rect, cls='axis'))
        out.append(layout.text(x + 12 + FONT['axis'] * 0.9, py + ph / 2, FONT['axis'], y_axis['label'], 'middle',
                               rotate=True, panel=rect, cls='axis'))
        for spec in series:
            pts = sorted(spec['points'])
            if not pts:
                continue
            dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get('dash') else ''
            if spec.get('line', True) and len(pts) > 1:
                out.append('<polyline points="' + ' '.join(f'{xpos(a):.1f},{ypos(b):.1f}' for a, b, _ in pts)
                           + f'" fill="none" stroke="{spec["colour"]}" stroke-width="{spec.get("width", 1.6)}"'
                           f'{dash}/>')
            for a, b, err in pts:
                if err:
                    low, high = max(b - err, y_lo), min(b + err, y_hi)     # clipped to the plot
                    out.append(f'<line x1="{xpos(a):.1f}" y1="{ypos(low):.1f}" x2="{xpos(a):.1f}" '
                               f'y2="{ypos(high):.1f}" stroke="{spec["colour"]}" stroke-width="1.1"/>')
                out.append(marker(spec.get('marker', 'circle'), xpos(a), ypos(b), spec['colour'],
                                  spec.get('size', 3.5)))
        return out
    return draw


def short_label(label, limit=CASE_LABEL_CHARS):
    """A label cut in the middle, so both its start (the case) and its end (the dtype) stay."""
    if len(label) <= limit:
        return label
    head = (limit - 1) * 3 // 5
    return label[:head] + '…' + label[len(label) - (limit - 1 - head):]


def dot_plot(title, cases, engines, values, panels, unit='ms'):
    """Cases (rows) x engines (markers) on a log value axis, one panel per entry of `panels`.

    `values[(panel, case, engine)] = (median, spread)`; `panels` are the panel titles (thread
    budgets, say). Returns (svg, dropped tick labels).
    """
    font = FONT['tick']
    row_h = 1.6 * font
    labels = [short_label(c) for c in cases]
    lead_w = 0.62 * font * max((len(label) for label in labels), default=1) + 20
    top_pad, bottom = 24 + FONT['panel'], 80
    panel_h = top_pad + row_h * max(len(cases), 1) + bottom
    data = [v[0] for v in values.values() if v[0] > 0]
    lo, hi = log_range(data) if data else (1.0, 10.0)
    entries = [dict(label=engine, colour=PALETTE[i % len(PALETTE)], marker=('circle', 'triangle', 'diamond',
                                                                             'square')[i % 4])
               for i, engine in enumerate(engines)]

    def lead(layout, rect):
        x, y, w, h = rect
        rows = category_positions(len(cases), y + top_pad, row_h * len(cases))
        return [layout.tick(x + w - 8, centre + font * 0.32, font, label, 'end', panel=rect, cls='tick')
                for centre, label in zip(rows, labels)]

    def panel(name):
        def draw(layout, rect):
            x, y, w, h = rect
            plot = (x + 16, y + top_pad, w - 32, row_h * len(cases))
            out = panel_frame(layout, rect, name)
            x_parts, xpos = log_axis(layout, 'x', lo, hi, plot, rect)
            out += x_parts
            px, py, pw, ph = plot
            rows = category_positions(len(cases), py, ph)
            for centre in rows:
                out.append(f'<line x1="{px}" y1="{centre:.1f}" x2="{px + pw}" y2="{centre:.1f}" '
                           f'stroke="#f4f4f4"/>')
            out.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="none" stroke="#c8c8c8"/>')
            out.append(layout.text(px + pw / 2, y + h - 12, FONT['axis'], f'time ({unit}, log)', 'middle',
                                   panel=rect, cls='axis'))
            for case, centre in zip(cases, rows):
                for index, entry in enumerate(entries):
                    if (name, case, entry['label']) not in values:
                        continue
                    median, spread = values[(name, case, entry['label'])]
                    if median <= 0:
                        continue
                    offset = (index - (len(entries) - 1) / 2) * min(4.0, row_h / (2 * len(entries)))
                    if spread:
                        out.append(f'<line x1="{xpos(max(median - spread, lo)):.1f}" y1="{centre + offset:.1f}" '
                                   f'x2="{xpos(min(median + spread, hi)):.1f}" y2="{centre + offset:.1f}" '
                                   f'stroke="{entry["colour"]}" stroke-width="1.1"/>')
                    out.append(marker(entry['marker'], xpos(median), centre + offset, entry['colour'], 5))
            return out
        return draw

    columns = min(len(panels), PANELS_PER_ROW) or 1
    panel_w = min(440, (MAX_WIDTH - 2 * GAP - lead_w - (columns - 1) * GAP) // columns)
    return figure(title, entries, [panel(name) for name in panels], panel_w=panel_w, panel_h=panel_h,
                  lead_w=lead_w, lead=lead)


def dot_plot_markdown(title, cases, engines, values, panels, unit='ms'):
    """The numbers of `dot_plot`, as a Markdown table per panel."""
    lines = [f'# {title} - plotted data', '',
             f'Each value is the median time in {unit} with its between-start spread (`+-`); '
             'a dot means the engine has no row for that case.', '']
    for name in panels:
        lines += [f'## {name}', '', '| case | ' + ' | '.join(engines) + ' |', '|---|' + '---|' * len(engines)]
        for case in cases:
            cells = []
            for engine in engines:
                value = values.get((name, case, engine))
                cells.append(f'{value[0]:.4g} +- {value[1]:.2g}' if value else '.')
            lines.append(f'| `{case}` | ' + ' | '.join(cells) + ' |')
        lines.append('')
    return '\n'.join(lines)
