"""The charts a campaign site generates at deploy time, from its recorded runs.

Charts are not committed: the Pages workflow writes them from `data/` every time it builds the
site, so a chart can never drift from the runs it claims to show. The descriptor declares which
charts a campaign has:

* `figures.throughput`: size-against-throughput charts (`throughput`), the chart config plus the
  (suite, vendor arm) whose current page's run fills each run position the config names;
* `figures.cases`: suites whose current pages each get a case dot plot (`charts.dot_plot`),
  for suites whose cases are not a size sweep;
* `trend`: the representative cases followed across pins (`trend`), drawn once by pin and once
  by week.

`site_figures` is the hook `site.write_site` calls; `grid_html` is the block it prints. A
campaign that declares nothing gets None, and the site keeps showing the charts it finds committed
under its result directory. Charts are drawn per hardware profile: a run of one profile is never
drawn with, or filed under the name of, a run of another.
"""
import html
import pathlib
import shutil

import yaml

from . import charts, descriptor, throughput, trend
from .campaign import load, page_source

OUT_DIR = 'figures'
SECTIONS = (('throughput', 'Throughput against size',
             'One algorithmic operation count per family: a line is comparable across sizes within its own chart, '
             'not across charts.'),
            ('cases', 'Time per case',
             'Median time of every case in the current page\'s run, one marker per engine, on a log axis; the bar is '
             'the between-start spread.'),
            ('trend', 'Trend across pins',
             'The representative cases, one point per pin. A line joins only points measured under the same '
             'profile, timing policy and power state; the legend names them.'))


def current_runs(pages):
    """[(run dir, manifest)] behind the given pages, in page order; unreadable sources are skipped."""
    out = []
    for page in pages:
        source = page_source(page)
        if source is not None and (source / 'run.yaml').exists():
            out.append((source, load(source / 'run.yaml')))
    return out


def _vendor(manifest):
    return (manifest.get('vendor') or {}).get('name')


def by_profile(runs):
    """{profile: [(run dir, manifest)]} in first-seen order."""
    out = {}
    for run, manifest in runs:
        out.setdefault(manifest['target_profile'], []).append((run, manifest))
    return out


def throughput_figures(spec, runs, out_dir):
    d = descriptor.current()
    config = yaml.safe_load(d.resolve(spec['config']).read_text())
    version = config.get('version', 'v1')
    out = []
    for profile, group in by_profile(runs).items():
        run_dirs = []
        for want in spec['runs']:
            match = [run for run, manifest in group if manifest['suite_id'] == want['suite']
                     and (want.get('vendor') is None or _vendor(manifest) == want['vendor'])]
            run_dirs.append(match[-1] if match else None)
        if not any(run_dirs):
            continue
        target = out_dir / 'throughput' / profile
        written, _ = throughput.write_report(run_dirs, config, target,
                                             budgets=tuple(config.get('budgets', (1, 4, 8))))
        out += [dict(kind='throughput', title=f'{profile} {family}: nominal GFLOP/s vs n',
                     svg=target / f'throughput-{family}-{version}.svg',
                     data=target / f'throughput-{family}-{version}.md')
                for family in written]
    return out


def case_figures(suites, runs, out_dir):
    target = out_dir / 'cases'
    target.mkdir(parents=True, exist_ok=True)
    out = []
    for run, manifest in runs:
        if manifest['suite_id'] not in suites:
            continue
        _, cells = trend.run_cells(run)
        cases = list(dict.fromkeys(case for case, _, _ in cells))
        engines = list(dict.fromkeys(engine for _, engine, _ in cells))
        panels = [f'{t}T' for t in sorted({threads for _, _, threads in cells})]
        values = {(f'{threads}T', case, engine): trend.summary(times)
                  for (case, engine, threads), times in cells.items()}
        arm = _vendor(manifest)
        profile = manifest['target_profile']
        title = f'{profile} {manifest["suite_id"]}{" " + arm if arm else ""}: median time per case'
        svg, _ = charts.dot_plot(title, cases, engines, values, panels)
        stem = f'cases-{profile}-{manifest["suite_id"]}{"-" + arm if arm else ""}'
        (target / f'{stem}.svg').write_text(svg)
        (target / f'{stem}.md').write_text(charts.dot_plot_markdown(title, cases, engines, values, panels) + '\n')
        out.append(dict(kind='cases', title=title, svg=target / f'{stem}.svg', data=target / f'{stem}.md'))
    return out


def trend_figures(entries, out_dir):
    points = [p for run in trend.published_runs() for p in trend.points_from_run(run)]
    by_pin = trend.write(entries, points, out_dir / 'trend', 'pin')
    by_week = trend.write(entries, points, out_dir / 'trend', 'week')
    return [dict(kind='trend', title=entry.get('title') or f'{entry["suite"]}: {entry["case"]}', svg=svg, data=md,
                 alternate=week_svg)
            for (entry, svg, md), (_, week_svg, _) in zip(by_pin, by_week)]


def site_figures(root, site_root, pages):
    """Write the declared charts under `<site_root>/figures/` and return them (possibly none, when
    no current run has data), or None when the campaign declares no charts.

    The directory is emptied first, so a chart of an earlier build can never be shown again.
    """
    if not descriptor.descriptor_path(root).exists():
        return None
    d = descriptor.set_root(root)
    declared = d.data.get('figures') or {}
    entries = d.data.get('trend') or []
    if not declared and not entries:
        return None
    out_dir = pathlib.Path(site_root) / OUT_DIR
    shutil.rmtree(out_dir, ignore_errors=True)
    runs = current_runs(pages)
    found = []
    if 'throughput' in declared:
        found += throughput_figures(declared['throughput'], runs, out_dir)
    if declared.get('cases'):
        found += case_figures(declared['cases'], runs, out_dir)
    if entries:
        found += trend_figures(entries, out_dir)
    return found


def grid_html(site_root, found):
    """The chart block of the landing page: a section per kind, one full-width chart per row."""
    site_root = pathlib.Path(site_root)
    rel = lambda path: html.escape(path.relative_to(site_root).as_posix())   # noqa: E731
    blocks = []
    for kind, heading, note in SECTIONS:
        figures = [f for f in found if f['kind'] == kind]
        if not figures:
            continue
        items = []
        for f in figures:
            links = f'<a href="{rel(f["data"])}">data</a>'
            if f.get('alternate'):
                links += f', <a href="{rel(f["alternate"])}">by week</a>'
            items.append(f'<figure><a href="{rel(f["svg"])}"><img src="{rel(f["svg"])}" '
                         f'alt="{html.escape(f["title"])}" loading="lazy"></a>'
                         f'<figcaption><strong>{html.escape(f["title"])}</strong> ({links})</figcaption></figure>')
        blocks.append(f'<h2>{heading}</h2>\n<p class="muted">{note}</p>\n<div class="charts">{"".join(items)}</div>')
    return '\n'.join(blocks)
