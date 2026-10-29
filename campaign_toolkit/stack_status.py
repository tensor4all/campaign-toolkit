#!/usr/bin/env python3
"""One Markdown row per benchmark repository: its pins against the stack, newest run, CI state.

    campaign-stack-status [--checkouts DIR] [--stack FILE]

For the daily patrol, run locally. DIR holds the repositories' checkouts side by side (default:
the directory that holds this toolkit's checkout). A cell that cannot be filled says why ("no
checkout", "gh unavailable") instead of failing the command. The CI cell is the newest workflow
run on the default branch, asked of GitHub with `gh run list`; the call is a parameter so a test
never reaches the network.
"""
import argparse
import glob
import json
import pathlib
import re
import subprocess
import sys

from . import stack as stackmod

OWNER = "tensor4all"
STAMP = re.compile(r"(\d{8})[T_](\d{6})")


def gh_run_list(repo_name):
    """`gh run list` for the default branch of OWNER/repo_name: the JSON text, or an exception."""
    return subprocess.run(
        ["gh", "run", "list", "-R", f"{OWNER}/{repo_name}", "--branch", "main", "--limit", "1",
         "--json", "conclusion,status,workflowName"],
        capture_output=True, text=True, timeout=60, check=True).stdout


def ci_cell(repo_name, gh) -> str:
    try:
        runs = json.loads(gh(repo_name))
    except FileNotFoundError:
        return "gh unavailable"
    except subprocess.TimeoutExpired:
        return "gh timed out"
    except (subprocess.CalledProcessError, OSError, ValueError):
        return "gh error"
    if not runs:
        return "no runs"
    run = runs[0]
    state = run.get("conclusion") or run.get("status") or "unknown"
    return f"{state} ({run.get('workflowName', '?')})"


def pin_cell(stack, repo, root) -> str:
    if not repo["consumes"]:
        return "-"
    parts = []
    for use, want, found, ok in stackmod.compare(stack, repo, root):
        name = pathlib.PurePosixPath(use["file"]).name
        if ok:
            parts.append(f"{name} {stackmod.short(want)} = stack")
        elif found is None:
            parts.append(f"{name} missing (stack {stackmod.short(want)})")
        else:
            have = ", ".join(stackmod.short(v) for v in found) or "none"
            parts.append(f"{name} {have} != stack {stackmod.short(want)}")
    return "; ".join(parts)


def latest_run(repo, root) -> str:
    """The run directory whose name carries the newest timestamp, as `YYYYMMDDTHHMMSS (path)`."""
    if not repo["run_dirs"]:
        return "n/a"
    best = None
    for pattern in repo["run_dirs"]:
        for path in glob.glob(str(pathlib.Path(root) / pattern)):
            match = STAMP.search(pathlib.Path(path).name)
            if match:
                stamp = match.group(1) + "T" + match.group(2)
                if best is None or stamp > best[0]:
                    best = (stamp, pathlib.Path(path).relative_to(root).as_posix())
    return f"{best[0]} ({best[1]})" if best else "none"


def rows(stack, checkouts, gh):
    for repo in stack["repositories"]:
        root = pathlib.Path(checkouts) / repo["name"]
        if root.is_dir():
            pins, run = pin_cell(stack, repo, root), latest_run(repo, root)
        else:
            pins = run = "no checkout"
        yield repo["name"], pins, run, ci_cell(repo["name"], gh)


def render(stack, checkouts, gh=gh_run_list) -> str:
    lines = ["| repository | pins (against the stack) | latest run | CI on main |", "|---|---|---|---|"]
    for cells in rows(stack, checkouts, gh):
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(lines)


def main(argv=None, out=None, gh=gh_run_list) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkouts", default=None,
                    help="the directory holding the repositories' checkouts (default: the toolkit checkout's parent)")
    ap.add_argument("--stack", default=None, help="the stack file (default: the one shipped with the toolkit)")
    args = ap.parse_args(argv)
    try:
        stack = stackmod.load(args.stack)
    except stackmod.StackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    checkouts = args.checkouts or pathlib.Path(__file__).resolve().parent.parent.parent
    print(render(stack, checkouts, gh), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
