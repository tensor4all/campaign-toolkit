"""Write the browser landing page for a campaign's recorded results.

    python -m campaign_toolkit.site --root <campaign> --site <dir> [--pin pins/x.rev]...

A result page is Markdown; a browser shows it as text. This writes the campaign's site: an
HTML copy of every result page (converted with the `markdown` package, beside the `.md`, with
the GitHub blob as its "source" link), the landing page `index.html`, and `history.html`.

The landing page has one section per hardware profile, in the order of the campaign's
`benchmarks/profiles.yaml`. A section opens with one summary line (the pinned revisions, the
measurement dates, the power state and the toolchain of its pages' runs), then links the page
of each (suite, vendor arm) recorded at the pins, then names in one line the cells that have
no page at the pins. Which page is current is the result index's own rule
(`gen_index.split_pages`): the site and `INDEX.md` agree by construction, and every other page
is listed in `history.html` exactly as `HISTORY.md` lists it.

A site root that is not a campaign's result directory (no descriptor) falls back to file
names: a page is at the pin when its name starts with the pinned revision, and each
(profile, suite, arm) links its newest such page. With no pin file every (profile, suite, arm)
shows its newest page.

It is a view over committed artifacts. It does not measure, and it does not decide
anything: a page it lists is a page that exists under the site root.
"""
import argparse
import datetime
import html
import pathlib
import re
import subprocess
import sys

CSS = """
body{font:14px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;max-width:72rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
h1{font-size:1.45rem}h2{font-size:1.1rem;margin-top:2rem}
code{background:#f2f2f2;padding:0 .2rem}
.muted{color:#666;font-size:.9em}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.2rem;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid #e3e3e3;padding:.25rem .4rem;text-align:left}
th{background:#f5f5f5}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.2rem}
.charts figure{margin:0}
.charts img{width:100%;height:auto;border:1px solid #eee;background:#fff}
.charts figcaption{font-size:.85rem;color:#444;margin-top:.25rem}
.summary{background:#f7f7f7;padding:.4rem .6rem;border-left:3px solid #ccc}
nav{font-size:.9em;margin-bottom:1rem}
"""


def _git(root, *args):
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    return out.stdout.strip()


def _blob_base(root):
    remote = _git(root, 'remote', 'get-url', 'origin')
    remote = remote.removesuffix('.git')
    for prefix in ('ssh://git@github.com/', 'git@github.com:'):
        if remote.startswith(prefix):
            return 'https://github.com/' + remote[len(prefix):]
    return remote


def _pin_revisions(root, pins):
    revisions = []
    for pin in pins or []:
        path = pathlib.Path(pin)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                revisions.append(line)
    return revisions


GENERATED = ('INDEX.md', 'HISTORY.md', 'README.md')


def _pages(site_root):
    pages = [p for p in site_root.rglob('*.md')
             if p.name not in GENERATED and not p.name.startswith('index')
             # chart data tables belong to their chart, not to the page list
             and 'throughput' not in p.parts]
    return sorted(pages)


_REV_PREFIX = re.compile(r'^(?:[0-9a-f]{12}-)+')
_TIMESTAMP = re.compile(r'^- timestamp: `([^`]+)`', re.M)


def _arm(page):
    """The vendor arm in a page name: `<rev>[-<upstream>]-<arm>[-timing-v2].md` (may be '')."""
    rest = _REV_PREFIX.sub('', page.stem + '-', count=1)
    rest = re.sub(r'-?timing-v2-?$', '', rest)
    return rest.strip('-')


def _recorded(page):
    """ISO timestamp the page records (`- timestamp: ...`), '' when it has none."""
    m = _TIMESTAMP.search(page.read_text(errors='replace')[:6000])
    return m.group(1) if m else ''


def current_pages(site_root, revisions):
    """File-name fallback: ([page, ...], [missing]) one page per (profile, suite, arm).

    The page at a pinned revision is the one linked, the newest recording breaking a tie. A
    (profile, suite, arm) with no page at the pin is returned in `missing` as
    (profile, suite name, arm). With no pinned revision at all, the newest page of each
    (profile, suite, arm) is linked.
    """
    def at_pin(page):
        return not revisions or any(page.name.startswith(rev[:12]) for rev in revisions)

    groups = {}
    for page in _pages(site_root):
        parts = page.relative_to(site_root).parts
        profile = parts[-3] if len(parts) >= 3 else ''
        suite = parts[:-3] + parts[-2:-1] if len(parts) >= 3 else parts[:-1]
        stamp = (_recorded(page), page.stat().st_mtime)
        groups.setdefault((profile, suite, _arm(page)), []).append((page, at_pin(page), stamp))
    chosen, missing = [], []
    for (profile, suite, arm), group in sorted(groups.items()):
        pinned = [e for e in group if e[1]]
        if pinned:
            chosen.append(max(pinned, key=lambda e: e[2])[0])
        else:
            missing.append((profile, suite[-1] if suite else '', arm))
    return sorted(chosen), missing


def _is_record(rel):
    """Whether a path (relative to the site root) is a record page: the same filter as `_pages`,
    plus the comparison HTML pages."""
    path = pathlib.PurePosixPath(rel)
    if 'throughput' in path.parts or path.name in GENERATED:
        return False
    if path.name.startswith(('index', 'history')):
        return False
    return path.suffix == '.md' or (path.suffix == '.html' and 'comparison' in path.name)


def _publication_commits(root, site_rel):
    """[(sha, epoch, subject)] newest first: first-parent commits that touched the site root."""
    out = _git(root, 'log', '--first-parent', '--format=%H%x09%ct%x09%s', '--', site_rel)
    commits = []
    for line in out.splitlines():
        sha, epoch, subject = line.split('\t', 2)
        commits.append((sha, int(epoch), subject))
    return commits


def _records_at(root, sha, site_rel):
    """{path relative to the repository root: blob id} of the record pages at a commit."""
    out = subprocess.run(['git', '-C', str(root), 'ls-tree', '-r', '-z', sha, '--', site_rel],
                         capture_output=True, text=True).stdout
    records = {}
    for entry in out.split('\0'):
        if not entry:
            continue
        meta, path = entry.split('\t', 1)
        if _is_record(pathlib.PurePosixPath(path).relative_to(site_rel).as_posix()):
            records[path] = meta.split()[2]
    return records


def _pins_at(root, sha):
    names = _git(root, 'ls-tree', '--name-only', sha, 'pins/').splitlines()
    pins = []
    for name in names:
        if not name.endswith('.rev'):
            continue
        for line in _git(root, 'show', f'{sha}:{name}').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                pins.append((pathlib.PurePosixPath(name).stem, line[:12]))
                break
    return pins


def history_pages(root, site_rel):
    """[{sha, epoch, subject, pins, records, new, removed}] newest first.

    Only commits that added, changed or removed a record page are kept: a commit that touched
    the site root without changing a record (a chart, an index) has nothing to list.
    """
    commits = _publication_commits(root, site_rel)
    states = [_records_at(root, sha, site_rel) for sha, _, _ in commits]
    entries = []
    for i, (sha, epoch, subject) in enumerate(commits):
        before = states[i + 1] if i + 1 < len(commits) else {}
        now = states[i]
        new = {p for p, blob in now.items() if before.get(p) != blob}
        removed = set(before) - set(now)
        if new or removed:
            entries.append(dict(sha=sha, epoch=epoch, subject=subject, pins=_pins_at(root, sha),
                                records=sorted(now), new=new, removed=removed))
    return entries


def _record_link(blob, sha, path, site_rel):
    rel = pathlib.PurePosixPath(path).relative_to(site_rel).as_posix()
    return f'<li><a href="{html.escape(blob)}/blob/{sha}/{html.escape(path)}">{html.escape(rel)}</a></li>'


def _group_by_suite(paths, site_rel):
    groups = {}
    for path in paths:
        parts = pathlib.PurePosixPath(path).relative_to(site_rel).parts
        suite = '/'.join(parts[:-3] + parts[-2:-1]) if len(parts) >= 3 else '/'.join(parts[:-1])
        groups.setdefault(suite or '.', []).append(path)
    return sorted(groups.items())


def _record_lists(blob, sha, paths, site_rel):
    return ''.join(
        f'<p class="muted">{html.escape(suite)} ({len(group)})</p><ul>'
        + ''.join(_record_link(blob, sha, p, site_rel) for p in group) + '</ul>'
        for suite, group in _group_by_suite(paths, site_rel))


def _older_section(site_root, older):
    """The pages of the tree that the landing does not show, as links to their HTML copies."""
    if not older:
        return ''
    items = []
    for page in older:
        rel = page.relative_to(site_root)
        items.append(f'<li><a href="{html.escape(str(rel.with_suffix(".html")))}">'
                     f'{html.escape(str(rel))}</a></li>')
    return (f'<h2 class="older">Older pages in this tree ({len(older)})</h2>\n'
            '<p class="muted">Every result page in the tree that the landing page does not show, newest '
            'first: the same list as <code>HISTORY.md</code>.</p>\n'
            f'<ul class="older">{"".join(items)}</ul>\n')


def write_history(root, site_root, title, older=()):
    """Write `<site_root>/history.html`; returns its path, or None with nothing to list.

    `older` is the list of pages in the tree the landing does not show (newest first); it
    comes first, then the log of publication commits.
    """
    site_rel = site_root.relative_to(root).as_posix()
    blob = _blob_base(root)
    entries = history_pages(root, site_rel)
    if not entries and not older:
        return None
    sections = []
    for e in entries:
        sha = e['sha']
        when = datetime.datetime.fromtimestamp(e['epoch'], datetime.timezone.utc)
        pins = ''.join(f' <span class="muted">{html.escape(n)} <code>{html.escape(r)}</code></span>'
                       for n, r in e['pins'])
        carried = [p for p in e['records'] if p not in e['new']]
        parts = [f'<h2>{when:%Y-%m-%d %H:%M} UTC '
                 f'<a href="{html.escape(blob)}/commit/{sha}"><code>{sha[:10]}</code></a></h2>',
                 f'<p>{html.escape(e["subject"])}{pins}</p>']
        if e['new']:
            parts.append(f'<p><strong>New or changed ({len(e["new"])})</strong></p>'
                         + _record_lists(blob, sha, sorted(e['new']), site_rel))
        if e['removed']:
            parts.append(f'<p class="muted">Removed in this commit: {len(e["removed"])} page(s).</p>')
        if carried:
            parts.append(f'<details><summary>Carried over ({len(carried)})</summary>'
                         + _record_lists(blob, sha, carried, site_rel) + '</details>')
        sections.append('\n'.join(parts))
    out = site_root / 'history.html'
    out.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}: history</title>
<style>{CSS}</style></head><body>
<h1>History of published records</h1>
<p class="muted">Each entry is a publication commit on the main branch, newest first, with the
records present at that commit. Links are permanent: they point at the commit.
<a href="index.html">Back to the current results</a>.</p>
{_older_section(site_root, older)}
{''.join(sections)}
</body></html>
""")
    print(f'wrote {out} ({len(entries)} publication commits)')
    return out


def _missing_line(missing):
    """One line naming the (suite, arm) cells with no page at the pin."""
    by_suite = {}
    for suite, arm in missing:
        by_suite.setdefault(suite, []).append(arm)
    names = [f"{suite} ({', '.join(a for a in arms if a)})" if any(arms) else suite
             for suite, arms in sorted(by_suite.items())]
    return 'Not recorded at the current pin: ' + ', '.join(names)


def _load_yaml(path):
    import yaml
    try:
        return yaml.safe_load(pathlib.Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _toolchain(run_dir):
    """The first line of the `rustc` the run's build recorded, '' when it has none."""
    import json
    try:
        info = json.loads((pathlib.Path(run_dir) / 'build-info.json').read_text())
    except (OSError, ValueError):
        return ''
    rustc = info.get('rustc') if isinstance(info, dict) else None
    return rustc.splitlines()[0].strip() if isinstance(rustc, str) and rustc.strip() else ''


def _power(run_dir):
    """`boost off, governor performance` from the manifest's `host.power`, '' when unrecorded."""
    power = ((_load_yaml(pathlib.Path(run_dir) / 'run.yaml').get('host') or {}).get('power') or {})
    parts = []
    boost = power.get('cpufreq_boost')
    if boost is not None:
        parts.append({'0': 'boost off', '1': 'boost on'}.get(str(boost), f'boost {boost}'))
    if power.get('governor'):
        parts.append(f"governor {power['governor']}")
    return ', '.join(parts)


def _joined(values, none='not recorded'):
    values = sorted({v for v in values if v})
    return '; '.join(values) if values else none


def profile_summary(rows, dual):
    """One line: pinned revisions, measurement dates, power state, toolchain of `rows`' runs."""
    if not rows:
        return 'Nothing recorded at the current pin on this profile.'
    pins = []
    for r in rows:
        pin = f"{r['version'] or '-'} <code>{html.escape(r['commit'])}</code>"
        if dual:
            upstream = r.get('upstream_ref') or r.get('upstream_version') or '-'
            pin += f" (upstream {html.escape(str(upstream))} <code>{html.escape(r['upstream'] or '-')}</code>)"
        pins.append(pin)
    dates = sorted(r['date'] for r in rows)
    when = dates[0] if dates[0] == dates[-1] else f'{dates[0]} to {dates[-1]}'
    power = _joined(_power(r['run_dir']) for r in rows if r.get('run_dir'))
    toolchain = _joined(_toolchain(r['run_dir']) for r in rows if r.get('run_dir'))
    return (f"pin: {'; '.join(sorted(set(pins)))} &middot; measured: {when} &middot; "
            f"power: {html.escape(power)} &middot; toolchain: {html.escape(toolchain)}")


def campaign_root(start):
    """The campaign whose descriptor exists at or above `start` (`descriptor.descriptor_path`, so
    `CAMPAIGN_DESCRIPTOR` applies), or None for a site root that belongs to no campaign."""
    from . import descriptor
    start = pathlib.Path(start).resolve()
    for candidate in [start, *start.parents]:
        if descriptor.descriptor_path(candidate).exists():
            return candidate
    return None


def campaign_view(root, site_root):
    """The result index's own view of a campaign site, or None when `site_root` is not one.

    {profiles: [name, ...], current: [cell, ...], history: [row, ...], dual: bool}: the cells
    and history of `gen_index.split_pages`, with the profiles in the descriptor's order
    followed by any other profile a page sits on.
    """
    from . import descriptor, gen_index
    if root is None:
        return None
    d = descriptor.set_root(root)  # an invalid descriptor is an error, not a reason to fall back
    if d.result_dir.resolve() != site_root.resolve():
        return None
    found = gen_index.collect()
    current, history = gen_index.split_pages(found["pages"], found["declared"], found["resolved"])
    seen = list(found["profiles"])
    for cell in current:
        if cell['profile'] not in seen:
            seen.append(cell['profile'])
    return dict(profiles=seen, current=current, history=history, dual=d.has_upstream)


def render_page(page, site_root, root, blob, repo_ref, title):
    """Write `<page>.html`: the Markdown page as HTML, with a back link and its source link."""
    import markdown
    out = page.with_suffix('.html')
    body = markdown.markdown(page.read_text(errors='replace'), extensions=['tables', 'fenced_code'])
    up = pathlib.Path(*(['..'] * (len(page.relative_to(site_root).parts) - 1)))
    rel = page.relative_to(root)
    source = (f'<a href="{html.escape(blob)}/blob/{html.escape(repo_ref)}/{html.escape(str(rel))}">source</a>'
              if 'github.com' in blob else f'<a href="{html.escape(page.name)}">source</a>')
    out.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}: {html.escape(page.stem)}</title>
<style>{CSS}</style></head><body>
<nav><a href="{html.escape(str(up / 'index.html'))}">current results</a> &middot;
<a href="{html.escape(str(up / 'history.html'))}">history</a> &middot; {source} (<code>{html.escape(str(rel))}</code>)</nav>
{body}
</body></html>
""")
    return out


def _page_item(page, site_root, root, blob, repo_ref, extra=''):
    rel = page.relative_to(site_root)
    source = (f' <span class="muted">(<a href="{html.escape(blob)}/blob/{html.escape(repo_ref)}/'
              f'{html.escape(str(page.relative_to(root)))}">source</a>)</span>' if 'github.com' in blob else '')
    return (f'<li><a href="{html.escape(str(rel.with_suffix(".html")))}">{html.escape(str(rel))}</a>'
            f'{extra}{source}</li>')


def _charts(site_root):
    return sorted(site_root.rglob('throughput-*-v2.svg'))


def chart_grid(site_root, charts, blob, repo_ref, root=None):
    root = pathlib.Path(root) if root else site_root
    if not charts:
        return ''
    figures = []
    for svg in charts:
        family = svg.stem.removeprefix('throughput-').removesuffix('-v2')
        rel = svg.relative_to(site_root)
        table = svg.with_suffix('.md').relative_to(root)  # blob links are repository-root relative
        figures.append(
            f'<figure><a href="{html.escape(str(rel))}"><img src="{html.escape(str(rel))}" '
            f'alt="{html.escape(family)} throughput chart" loading="lazy"></a>'
            f'<figcaption><strong>{html.escape(family)}</strong> '
            f'<a href="{blob}/blob/{repo_ref}/{html.escape(str(table))}">(data)</a></figcaption></figure>')
    return ('<h2>Throughput against size</h2>\n'
            '<p class="muted">One algorithm operation count per family; a line is comparable across '
            'sizes within its own chart and not across charts. Click a chart for the full size.</p>\n'
            f'<div class="charts">{"".join(figures)}</div>')


def write_site(site_root, pins=(), repo_ref='main', title=None, intro=None, extra_html='',
               root=None):
    """Write `<site_root>/index.html`, `history.html` and an HTML copy of every result page."""
    site_root = pathlib.Path(site_root).resolve()
    given = pathlib.Path(root).resolve() if root else site_root
    root = given
    while not (root / '.git').exists() and root != root.parent:
        root = root.parent
    if not (root / '.git').exists():
        root = given  # not in a repository: links stay relative to the campaign
    blob = _blob_base(root)
    heading = title or 'Benchmark results'
    charts = _charts(site_root)
    view = campaign_view(campaign_root(given), site_root)

    sections, older, shown = [], [], 0
    if view is not None:
        campaign = campaign_root(given)
        by_profile = {}
        for cell in view['current']:
            by_profile.setdefault(cell['profile'], []).append(cell)
        for profile in view['profiles']:
            cells = by_profile.get(profile, [])
            rows = [c['page'] for c in cells if c['page']]
            items = []
            for c in cells:
                if c['page']:
                    page = campaign / c['page']['page']
                    render_page(page, site_root, root, blob, repo_ref, heading)
                    arm = f" <span class=\"muted\">{html.escape(c['vendor'])}</span>" if c['vendor'] else ''
                    items.append(_page_item(page, site_root, root, blob, repo_ref, arm))
            missing = [(c['suite'], c['vendor'] or '') for c in cells if not c['page']]
            shown += len(items)
            sections.append(
                f'<h2 id="{html.escape(profile)}">{html.escape(profile)}</h2>\n'
                f'<p class="summary">{profile_summary(rows, view["dual"])}</p>\n'
                + (f'<ul>{"".join(items)}</ul>\n' if items else '')
                + (f'<p class="muted">{html.escape(_missing_line(missing))}</p>\n' if missing else ''))
        for r in view['history']:
            page = campaign / r['page']
            if page.exists():
                render_page(page, site_root, root, blob, repo_ref, heading)
                older.append(page)
    else:
        revisions = _pin_revisions(root, pins)
        pages, missing = current_pages(site_root, revisions)
        by_profile = {}
        for page in pages:
            by_profile.setdefault(page.relative_to(site_root).parts[0], [[], []])[0].append(page)
        for profile, suite, arm in missing:
            by_profile.setdefault(profile, [[], []])[1].append((suite, arm))
        pin_line = ', '.join(f'<code>{html.escape(r[:12])}</code>' for r in revisions) or 'none'
        for profile, (chosen, absent) in sorted(by_profile.items()):
            for page in chosen:
                render_page(page, site_root, root, blob, repo_ref, heading)
            shown += len(chosen)
            sections.append(
                f'<h2 id="{html.escape(profile)}">{html.escape(profile)}</h2>\n'
                f'<p class="summary">pin: {pin_line}</p>\n'
                + (f'<ul>{"".join(_page_item(p, site_root, root, blob, repo_ref) for p in chosen)}</ul>\n'
                   if chosen else '')
                + (f'<p class="muted">{html.escape(_missing_line(absent))}</p>\n' if absent else ''))
        current = set(pages)
        older = sorted((p for p in _pages(site_root) if p not in current),
                       key=lambda p: (_recorded(p), p.stat().st_mtime), reverse=True)
        for page in older:
            render_page(page, site_root, root, blob, repo_ref, heading)

    intro = intro or ('The results at the pinned revisions, one section per hardware profile. Each page '
                      'is shown here as HTML; its Markdown source is linked beside it.')
    history = write_history(root, site_root, heading, older)
    history_link = (f'<p><a href="history.html">History of published records</a> '
                    f'<span class="muted">({len(older)} older page(s) in this tree, and the publication '
                    f'log)</span></p>' if history else '')
    (site_root / 'index.html').write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(heading)}</title>
<style>{CSS}</style></head><body>
<h1>{html.escape(heading)}</h1>
<p>{intro}</p>
{history_link}
{''.join(sections) or '<p class="muted">no pages yet</p>'}
{extra_html}
{chart_grid(site_root, charts, blob, repo_ref, root)}
</body></html>
""")
    print(f'wrote {site_root / "index.html"} ({shown} current pages, {len(older)} older, {len(charts)} charts)')
    return site_root / 'index.html'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--site', default=None, help='the site root (default: the descriptor result_dir)')
    ap.add_argument('--pin', action='append', default=None)
    ap.add_argument('--repo-ref', default='main')
    ap.add_argument('--title')
    ap.add_argument('--intro')
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    site_root = pathlib.Path(args.site).resolve() if args.site else None
    pins = args.pin
    if site_root is None or pins is None:
        from . import descriptor
        d = descriptor.set_root(root)
        site_root = site_root or (root / d.data['result_dir'])
        pins = pins or [d.data['measured']['pin_file']]
    write_site(site_root, pins=pins, repo_ref=args.repo_ref, title=args.title,
               intro=args.intro, root=root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
