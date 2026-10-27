"""Write the browser landing page for a campaign's recorded results.

    python -m campaign_toolkit.site --root <campaign> --site <dir> [--pin pins/x.rev]...

A result page is Markdown; a browser shows it as text. This writes the one HTML page a
campaign's site serves: the pages it has, grouped by whether they were recorded at the
current pin, the throughput charts when the campaign has them, and whatever extra section
the campaign passes (a joined comparison, for instance).

It is a view over committed artifacts. It does not measure, and it does not decide
anything: a page it lists is a page that exists under the site root.
"""
import argparse
import html
import pathlib
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
    pages = _pages(site_root)
    charts = _charts(site_root)

    def at_pin(page):
        return any(page.name.startswith(rev[:12]) for rev in revisions)

    pages.sort(key=lambda p: (not at_pin(p), -p.stat().st_mtime))
    items = []
    for page in pages:
        rel = page.relative_to(site_root)
        if 'github.com' in blob:
            href = f'{blob}/blob/{repo_ref}/{page.relative_to(root)}'
            note = ' <span class="muted">(Markdown, rendered by GitHub)</span>'
        else:
            href, note = str(rel), ''
        mark = '' if at_pin(page) else ' <span class="muted">(superseded)</span>'
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(str(rel))}</a>{note}{mark}</li>')

    heading = title or 'Benchmark results'
    intro = intro or ('One page per `(suite, hardware profile, revision, vendor arm)`, generated '
                      'from the recorded data. The pages at the pinned revision come first; the '
                      'rest are kept as history and marked.')
    pin_lines = ''.join(f'<li>pinned revision: <code>{html.escape(rev)}</code></li>' for rev in revisions)
    (site_root / 'index.html').write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(heading)}</title>
<style>{CSS}</style></head><body>
<h1>{html.escape(heading)}</h1>
<p>{intro}</p>
<ul>{pin_lines}</ul>
{extra_html}
{chart_grid(site_root, charts, blob, repo_ref, root)}
<h2>Pages</h2><ul>{''.join(items) or '<li class="muted">no pages yet</li>'}</ul>
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
