"""Write the browser landing page for a campaign's recorded results.

    python -m campaign_toolkit.site --root <campaign> --site <dir> [--pin pins/x.rev]...

A result page is Markdown; a browser shows it as text. This writes the one HTML page a
campaign's site serves: the latest generation of results only, the throughput charts when
the campaign has them, and whatever extra section the campaign passes (a joined
comparison, for instance).

Latest generation: for each (suite, vendor arm) the one page recorded at the campaign's
pin on the newest hardware profile. A (suite, arm) with no page at the pin is not linked at
all: the page names it in one plain line. Older pages are never linked from the landing
page; its one link to them is `history.html` (see `history_pages`). A campaign with no pin file has no notion of "at the pin", so
every (suite, arm) shows its newest page.

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


def _pages(site_root):
    pages = [p for p in site_root.rglob('*.md')
             if p.name not in ('INDEX.md', 'README.md') and not p.name.startswith('index')
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
    """Latest generation: ([page, ...], [missing]) one page per (suite, arm), sorted by path.

    Newest profile = the profile of the newest recorded page on the site. Within a
    (suite, arm) that has pages on it, only that profile is considered; the page at a
    pinned revision is the one linked, the newest recording breaking a remaining tie. A
    (suite, arm) with no page at the pin is returned in `missing` as (suite name, arm).
    With no pinned revision at all, the newest page of each (suite, arm) is linked.
    """
    def at_pin(page):
        return not revisions or any(page.name.startswith(rev[:12]) for rev in revisions)

    entries = []
    for page in _pages(site_root):
        parts = page.relative_to(site_root).parts
        profile = parts[-3] if len(parts) >= 3 else ''
        suite = parts[:-3] + parts[-2:-1] if len(parts) >= 3 else parts[:-1]
        stamp = (_recorded(page), page.stat().st_mtime)
        entries.append((page, (suite, _arm(page)), profile, at_pin(page), stamp))
    if not entries:
        return [], []
    newest_profile = max(entries, key=lambda e: e[4])[2]
    groups = {}
    for entry in entries:
        groups.setdefault(entry[1], []).append(entry)
    chosen, missing = [], []
    for (suite, arm), group in sorted(groups.items()):
        pinned = [e for e in group if e[3]]
        pool = [e for e in pinned if e[2] == newest_profile] or pinned
        if pool:
            chosen.append(max(pool, key=lambda e: e[4])[0])
        else:
            missing.append((suite[-1] if suite else '', arm))
    return sorted(chosen), missing


def _is_record(rel):
    """Whether a path (relative to the site root) is a record page: the same filter as `_pages`,
    plus the comparison HTML pages."""
    path = pathlib.PurePosixPath(rel)
    if 'throughput' in path.parts or path.name in ('INDEX.md', 'README.md'):
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


def write_history(root, site_root, title):
    """Write `<site_root>/history.html`; returns its path, or None with no publication history."""
    site_rel = site_root.relative_to(root).as_posix()
    blob = _blob_base(root)
    entries = history_pages(root, site_rel)
    if not entries:
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
{''.join(sections)}
</body></html>
""")
    print(f'wrote {out} ({len(entries)} publication commits)')
    return out


def _missing_line(missing):
    by_suite = {}
    for suite, arm in missing:
        by_suite.setdefault(suite, []).append(arm)
    names = [f"{suite} ({', '.join(a for a in arms if a)})" if any(arms) else suite
             for suite, arms in sorted(by_suite.items())]
    return 'Not recorded at the current pin: ' + ', '.join(names)


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
    """Write `<site_root>/index.html` from the artifacts under it."""
    site_root = pathlib.Path(site_root).resolve()
    root = pathlib.Path(root).resolve() if root else site_root
    while not (root / '.git').exists() and root != root.parent:
        root = root.parent
    blob = _blob_base(root)
    revisions = _pin_revisions(root, pins)
    pages, missing = current_pages(site_root, revisions)
    charts = _charts(site_root)

    items = []
    for page in pages:
        rel = page.relative_to(site_root)
        if 'github.com' in blob:
            href = f'{blob}/blob/{repo_ref}/{page.relative_to(root)}'
            note = ' <span class="muted">(Markdown, rendered by GitHub)</span>'
        else:
            href, note = str(rel), ''
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(str(rel))}</a>{note}</li>')
    missing_html = f'<p class="muted">{html.escape(_missing_line(missing))}</p>' if missing else ''

    heading = title or 'Benchmark results'
    intro = intro or ('The latest generation of results: for each suite and vendor arm, the page '
                      'recorded at the pinned revision.')
    history = write_history(root, site_root, title or 'Benchmark results')
    history_link = '<p><a href="history.html">History of published records</a></p>' if history else ''
    pin_lines = ''.join(f'<li>pinned revision: <code>{html.escape(rev)}</code></li>' for rev in revisions)
    (site_root / 'index.html').write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(heading)}</title>
<style>{CSS}</style></head><body>
<h1>{html.escape(heading)}</h1>
<p>{intro}</p>
{history_link}
<ul>{pin_lines}</ul>
{extra_html}
{chart_grid(site_root, charts, blob, repo_ref, root)}
<h2>Pages</h2><ul>{''.join(items) or '<li class="muted">no pages yet</li>'}</ul>
{missing_html}
</body></html>
""")
    print(f'wrote {site_root / "index.html"} ({len(pages)} pages, {len(charts)} charts)')
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
