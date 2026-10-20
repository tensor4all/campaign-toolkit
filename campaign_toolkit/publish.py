#!/usr/bin/env python3
"""Refresh every campaign the same way, from one plan file.

    python -m campaign_toolkit.publish --plan publish.yaml [--only NAME] [--dry-run]

The plan names, per campaign, the repository, the publication commands (in order) and the
site step. The routine is deliberately boring:

1. **Preflight** before anything is measured: no campaign holds `target/campaign.lock`, every
   tree is clean (a dirty tree is refused by the recorder anyway, but after a build), the
   toolkit is clean, and the machine is in the state the timing policy wants (no turbo, the
   observer off the measured CPUs). A failure here costs seconds; the same failure later
   costs a run.
2. **Record** each command as given. A non-zero exit stops the whole plan: publishing the
   rest would leave the set inconsistent.
3. **Commit** what the run wrote, so the next campaign's recorder sees a clean tree.
4. **Site**: regenerate the landing page and commit it.
5. **Push**, so the Pages workflow picks it up.

Nothing here decides what to measure; that stays in each campaign's descriptor.
"""
import argparse
import pathlib
import subprocess
import sys

import yaml


def sh(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), text=True)
    if check and r.returncode != 0:
        sys.exit(f'ERROR: {cmd} failed in {cwd} (exit {r.returncode})')
    return r.returncode


def git(root, *args, check=True):
    return sh(['git', '-C', str(root), *args], cwd=root, check=check)


def dirty(root):
    return bool(subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                               capture_output=True, text=True).stdout.strip())


def preflight(campaigns, toolkit):
    problems = []
    for c in campaigns:
        lock = pathlib.Path(c['root']) / 'target' / 'campaign.lock'
        if lock.exists():
            held = subprocess.run(['flock', '-n', str(lock), 'true']).returncode != 0
            if held:
                problems.append(f"{c['name']}: a measurement holds {lock}")
        if dirty(c['root']):
            problems.append(f"{c['name']}: the tree is dirty; commit or clean it first")
    if dirty(toolkit):
        problems.append(f'toolkit {toolkit} is dirty; the recorder refuses a dirty toolkit')
    if problems:
        for p in problems:
            print('preflight:', p, file=sys.stderr)
        sys.exit(1)
    print('preflight: ok')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', default='publish.yaml')
    ap.add_argument('--only', action='append', default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    plan = yaml.safe_load(pathlib.Path(args.plan).read_text())
    toolkit = pathlib.Path(plan['toolkit']).resolve()
    campaigns = plan['campaigns']
    if args.only:
        campaigns = [c for c in campaigns if c['name'] in set(args.only)]
    for c in campaigns:
        c['root'] = str(pathlib.Path(c['root']).resolve())
    preflight(campaigns, toolkit)
    if args.dry_run:
        for c in campaigns:
            steps = len(c.get('record') or []) + (1 if c.get('publish') else 0)
            print(f"{c['name']}: {steps} step(s), site: {'yes' if c.get('site') not in (None, 'none') else 'inside the recipe'}")
        return 0
    for c in campaigns:
        root = c['root']
        print(f"== {c['name']}")
        for cmd in c['record']:
            print('  record:', cmd)
            sh(cmd, root)
        if c.get('publish'):
            print('  publish:', c['publish'])
            sh(c['publish'], root)
        if dirty(root):
            git(root, 'add', '-A')
            git(root, '-c', 'user.name=tensor4all agent',
                '-c', 'user.email=agent@tensor4all.invalid',
                'commit', '-q', '-m', c.get('message', 'publish: refresh the recorded pages'))
        else:
            print('  nothing new to commit')
        if c.get('site') and c['site'] != 'none':
            print('  site:', c['site'])
            sh(c['site'], root)
            if dirty(root):
                git(root, 'add', '-A')
                git(root, '-c', 'user.name=tensor4all agent',
                    '-c', 'user.email=agent@tensor4all.invalid',
                    'commit', '-q', '-m', 'site: regenerate the landing page')
        git(root, 'push', 'origin', 'HEAD')
        print(f"  pushed {c['name']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
