#!/usr/bin/env python3
"""Write a benchmark repository's `pins/*.rev` from the stack, or check that they match it.

    campaign-sync-pins [ROOT] [--stack FILE] [--repo NAME] [--check]

ROOT is the repository's root (default: the current directory) and the repository is looked up
in the stack by its directory name, or by `--repo`. The stack is the one shipped with the
installed toolkit unless `--stack` names another. Only the value of a pin file is the stack's:
the `#` comments a pin file carries to say how a revision was determined are kept, and a missing
file is written with a one-line header. `--check` writes nothing and exits 1 listing every pin
that is missing or differs. Pins the stack declares in a workflow or a Cargo manifest are
checked in both modes and are never rewritten; edit those by hand.
"""
import argparse
import pathlib
import sys

from . import stack as stackmod

HEADER = "# Generated from the stack file by campaign-sync-pins; the last line that is not a comment is the pin.\n"


def with_value(text: str, value: str) -> str:
    """`text` with its pin line (the last that is not a comment) replaced; comments and ending stay."""
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].split("#", 1)[0].strip():
            lines[i] = value + ("\n" if lines[i].endswith("\n") else "")
            return "".join(lines)
    return text + ("" if not text or text.endswith("\n") else "\n") + value + "\n"


def sync(stack, repo, root, check, out) -> int:
    root = pathlib.Path(root)
    bad = 0
    for use, want, found, ok in stackmod.compare(stack, repo, root):
        label = f"{repo['name']}: {use['file']}"
        if ok:
            print(f"ok   {label} = {stackmod.short(want)}", file=out)
            continue
        if use["kind"] == "rev-file" and not check:
            path = root / use["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(with_value(path.read_text(), want) if path.exists() else HEADER + want + "\n")
            print(f"wrote {label} = {stackmod.short(want)}", file=out)
            continue
        bad += 1
        have = "missing file" if found is None else (", ".join(found) or "no value") + " found"
        print(f"FAIL {label}: stack says {want}; {have}", file=out)
    return bad


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".", help="the repository root (default: the current directory)")
    ap.add_argument("--stack", default=None, help="the stack file (default: the one shipped with the toolkit)")
    ap.add_argument("--repo", default=None, help="the repository's name in the stack (default: its directory name)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 listing each pin that differs")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    try:
        stack = stackmod.load(args.stack)
    except stackmod.StackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    name = args.repo or root.name
    repo = stackmod.repository(stack, name)
    if repo is None:
        known = ", ".join(r["name"] for r in stack["repositories"])
        print(f"ERROR: {name!r} is not a repository of the stack (known: {known}); pass --repo", file=sys.stderr)
        return 2
    bad = sync(stack, repo, root, args.check, out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
