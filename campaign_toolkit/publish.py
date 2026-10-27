#!/usr/bin/env python3
"""Refresh every campaign the same way, from one plan file.

    python -m campaign_toolkit.publish --plan publish.yaml [--only NAME] [--dry-run] [--no-merge]

The plan names, per campaign, the repository, the publication commands (in order) and the
site step. The routine is deliberately boring:

1. **Preflight** before anything is measured: no campaign holds `target/campaign.lock`, every
   tree is clean (a dirty tree is refused by the recorder anyway, but after a build), every
   campaign is on `main` level with `origin/main`, `gh` is logged in, the toolkit is clean,
   and the machine is in the state the timing policy wants (no turbo, the
   observer off the measured CPUs). A failure here costs seconds; the same failure later
   costs a run.
2. **Record** each command as given. A non-zero exit stops the whole plan: publishing the
   rest would leave the set inconsistent.
3. **Commit** what the run wrote onto a branch `publish/<campaign>-<UTC stamp>`, so the next
   campaign's recorder sees a clean tree.
4. **Site**: regenerate the landing page and commit it on the same branch.
5. **Pull request**: push the branch, open a PR against `main`, wait for its checks (a failing
   check stops the plan and leaves the PR open), then merge with a merge commit, never squash,
   rebase or `--admin`: publish commit SHAs are quoted in handoffs and must stay reachable.
   Nothing reaches `main` by a direct push. `--no-merge` stops after opening the PR.

Nothing here decides what to measure; that stays in each campaign's descriptor.
"""
import argparse
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

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


def out(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def gh(root, *args, check=True):
    code, text = out(['gh', *args], cwd=root)
    if check and code != 0:
        sys.exit(f'ERROR: gh {" ".join(args)} failed in {root} (exit {code}): {text.strip()}')
    return code, text


def git_out(root, *args):
    return out(['git', '-C', str(root), *args])[1].strip()


def check_remote(root):
    """Return a problem string unless `root` is on main, level with origin/main."""
    code, text = out(['git', '-C', str(root), 'fetch', 'origin'])
    if code != 0:
        return f'git fetch origin failed: {text.strip()}'
    branch = git_out(root, 'branch', '--show-current')
    if branch != 'main':
        return f'on branch {branch or "(detached)"}, not main'
    counts = git_out(root, 'rev-list', '--left-right', '--count', 'HEAD...origin/main').split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (-1, -1)
    if (ahead, behind) != (0, 0):
        return f'main is {ahead} ahead, {behind} behind origin/main; make it equal first'
    return None


def preflight(campaigns, toolkit):
    problems = []
    if out(['gh', 'auth', 'status'])[0] != 0:
        problems.append('gh is not available or not logged in (gh auth status fails)')
    for c in campaigns:
        problem = check_remote(c['root'])
        if problem:
            problems.append(f"{c['name']}: {problem}")
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
    ap.add_argument('--no-merge', action='store_true',
                    help='open the pull request and stop after printing its URL')
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
            print(f"{c['name']}: {steps} step(s), site: {'yes' if c.get('site') not in (None, 'none') else 'inside the recipe'}"
                  f", then branch publish/{c['name']}-<UTC>, PR to main, checks, merge"
                  f"{' (--no-merge: stop after opening the PR)' if args.no_merge else ''}")
        return 0
    for c in campaigns:
        root = c['root']
        print(f"== {c['name']}")
        for cmd in c.get('record') or []:
            print('  record:', cmd)
            sh(cmd, root)
        if c.get('publish'):
            print('  publish:', c['publish'])
            sh(c['publish'], root)
        branch = f"publish/{c['name']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        git(root, 'switch', '-q', '-c', branch)
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
        open_pr(c, root, branch, merge=not args.no_merge)
    return 0


def wait_for_checks(root, pr, url, attempts=12, delay=10):
    """Block until the PR's checks pass; exit on a failure. No checks for ~2 minutes: proceed."""
    for i in range(attempts):
        code, text = gh(root, 'pr', 'checks', pr, '--watch', '--fail-fast', check=False)
        if text.strip():
            print(text.rstrip())
        if code == 0:
            return
        if 'no checks reported' in text:
            if i + 1 < attempts:
                time.sleep(delay)
            continue
        sys.exit(f'ERROR: a check failed on {url}; the branch and PR are left open')
    print('  no checks reported; proceeding')


def open_pr(c, root, branch, merge=True):
    if int(git_out(root, 'rev-list', '--count', 'origin/main..HEAD') or 0) == 0:
        git(root, 'switch', '-q', 'main')
        git(root, 'branch', '-D', branch)
        print(f"  nothing new to publish ({c['name']})")
        return
    subjects = git_out(root, 'log', '--reverse', '--format=%s', 'origin/main..HEAD').splitlines()
    body = ('Opened by campaign_toolkit.publish. Commits:\n\n'
            + '\n'.join(f'- {s}' for s in subjects)
            + '\n\n\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)')
    git(root, 'push', '-q', '-u', 'origin', branch)
    _, text = gh(root, 'pr', 'create', '--base', 'main', '--head', branch,
                 '--title', subjects[0], '--body', body)
    url = text.strip().splitlines()[-1]
    print(f'  opened {url}')
    if not merge:
        git(root, 'switch', '-q', 'main')
        print(f"  --no-merge: left {c['name']} on main; PR {url}")
        return
    wait_for_checks(root, url, url)
    gh(root, 'pr', 'merge', url, '--merge', '--delete-branch')
    git(root, 'switch', '-q', 'main')
    git(root, 'pull', '-q', '--ff-only', 'origin', 'main')
    _, state = gh(root, 'pr', 'view', url, '--json', 'state', '-q', '.state', check=False)
    print(f"  merged {c['name']}: {url} ({state.strip() or 'unknown'})")


if __name__ == '__main__':
    sys.exit(main())
