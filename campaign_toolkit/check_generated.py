"""Check that every committed generated file is what its generator writes from the raw data.

    python -m campaign_toolkit.check_generated [--root <campaign>] [--base <git ref> | --all | --run <dir>...]
        [--no-index]

A contributor commits run directories; everything else that is committed is generated, and this
is the check that nobody edited it by hand:

1. every published page is its source run's `report.md` under the publication header, byte for
   byte (a page that names no readable source run fails: that is the case the check exists for);
2. every run changed since `--base` (or each `--run`, or every run with `--all`) regenerates its
   `report.md` from its CSVs byte for byte. Only changed runs, because a report written by an
   older toolkit is not expected to match today's renderer, and regenerating every run on every
   push would be the expensive part of CI;
3. the index is current (`gen_index --check`).

Charts, comparisons and trends are not committed (the site generates them), so there is nothing
of theirs to compare. Exits 1 and names every difference.
"""
import argparse
import os
import pathlib
import subprocess
import sys

from . import descriptor, gen_index, report_run
from .campaign import load, page_source, record_pages
from .publish_report import page_header


def check_pages(d):
    errors = []
    for page in record_pages(d.root / d.data['result_dir']):
        rel = page.relative_to(d.root)
        source = page_source(page)
        if source is None:
            errors.append(f'{rel}: names no source run (no publication header)')
            continue
        report = source / 'report.md'
        if not report.exists():
            errors.append(f'{rel}: its source {source.relative_to(d.root)}/report.md does not exist')
            continue
        expected = page_header(source.relative_to(d.root)).encode() + report.read_bytes()
        if page.read_bytes() != expected:
            errors.append(f'{rel}: differs from {source.relative_to(d.root)}/report.md under the '
                          f'publication header (re-publish it with publish_report, do not edit it)')
    return errors


def changed_runs(d, base):
    """Run directories with a file changed since `base`, committed, staged, or untracked."""
    data = d.root / d.data['data_dir']
    rel = data.relative_to(d.root).as_posix()

    def names(*args):
        out = subprocess.run(['git', '-C', str(d.root), *args, '--', rel], capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(f'ERROR: git {" ".join(args)} failed: {out.stderr.strip()}')
        return out.stdout.splitlines()

    paths = names('diff', '--name-only', '--relative', base) + names('ls-files', '--others', '--exclude-standard')
    runs = set()
    for name in paths:
        path = d.root / name
        for parent in path.parents:
            if parent == data or data not in parent.parents:
                break
            if (parent / 'run.yaml').exists():
                runs.add(parent)
                break
    return sorted(runs)


def orphaned_runs(d, base):
    """[error] for each run whose `run.yaml` was deleted since `base` while files remain in its
    directory: a report left behind without its manifest can no longer be checked."""
    rel = (d.root / d.data['data_dir']).relative_to(d.root).as_posix()
    out = subprocess.run(['git', '-C', str(d.root), 'diff', '--name-only', '--relative', '--diff-filter=D', base,
                          '--', rel], capture_output=True, text=True)
    errors = []
    for name in out.stdout.splitlines():
        path = d.root / name
        if path.name == 'run.yaml' and path.parent.is_dir() and any(path.parent.iterdir()):
            errors.append(f'{path.parent.relative_to(d.root)}: its run.yaml was deleted but the directory '
                          f'still holds {", ".join(sorted(p.name for p in path.parent.iterdir())[:4])}')
    return errors


def all_runs(d):
    return sorted(m.parent for m in (d.root / d.data['data_dir']).rglob('run.yaml'))


def check_report(d, run_dir):
    """[error] for one run whose `report.md` must regenerate byte for byte."""
    rel = run_dir.relative_to(d.root)
    report = run_dir / 'report.md'
    if not report.exists():
        return []          # a run recorded without a report (raw data only) has nothing to compare
    try:
        manifest = load(run_dir / 'run.yaml')
        suite = load(d.suite_path(manifest['suite_id']))
        rows = report_run.read_rows([run_dir / f for f in manifest.get('result_files') or []])
        text = report_run.render(manifest, suite, rows, run_dir)
    except (SystemExit, Exception) as error:    # every failure is a finding here
        return [f'{rel}/report.md: cannot be regenerated: {error}']
    if report.read_bytes() != text.encode():
        return [f'{rel}/report.md: differs from what report_run writes from its CSVs']
    return []


def main(argv=None, root=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--root', default=None, help='the campaign root (default: CAMPAIGN_ROOT or .)')
    which = ap.add_mutually_exclusive_group()
    which.add_argument('--base', help='regenerate the reports of the runs changed since this git ref')
    which.add_argument('--all', action='store_true', help='regenerate every run\'s report')
    which.add_argument('--run', type=pathlib.Path, action='append', help='regenerate this run\'s report')
    ap.add_argument('--no-index', action='store_true', help='skip the index check (it needs the pinned checkouts)')
    args = ap.parse_args(argv)
    d = descriptor.set_root(root or args.root or os.environ.get('CAMPAIGN_ROOT') or '.')

    errors = check_pages(d)
    if args.all:
        runs = all_runs(d)
    elif args.run:
        runs = [path.resolve() for path in args.run]
    elif args.base:
        runs = changed_runs(d, args.base)
        errors += orphaned_runs(d, args.base)
    else:
        runs = []
    for run in runs:
        errors += check_report(d, run)
    index = 0 if args.no_index else gen_index.main(['--check'], root=d.root)
    if index != 0:
        errors.append('the index is not what gen_index writes (see above)')
    for error in errors:
        print(f'ERROR: {error}')
    print(f'checked {len(record_pages(d.root / d.data["result_dir"]))} pages, {len(runs)} regenerated reports'
          f'{"" if args.no_index else ", the index"}: {"FAILED" if errors else "ok"}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
