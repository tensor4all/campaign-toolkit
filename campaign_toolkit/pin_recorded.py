#!/usr/bin/env python3
"""Fail when a pin names a revision that no publication was measured at.

    campaign-check-pin-recorded                       # the campaign named by CAMPAIGN_ROOT / CAMPAIGN_DESCRIPTOR
    campaign-check-pin-recorded --pin pins/x.rev --evidence 'result/*/*/*.summary.json' \\
        --commit-field library_commit [--time-field timestamp] [--page TEMPLATE]

A pin is what a campaign's pages are named after and what a reader takes as "the revision these
numbers describe". Moving it ahead of the evidence makes every cell `missing` while the numbers
that do exist sit in the history under an older revision, which is the failure this exists to
prevent. So the rule is mechanical and has no exception:

1. at least one publication was measured at the pin (at every recorded identity), and
2. the newest publication was measured at the pin.

The fix is always one of two things: record a publication at the pin, or keep the pin at the last
measured revision. Moving the pin and its publication together, in one change, satisfies both.

Descriptor mode reads a toolkit campaign (the publication pages `gen_index` indexes; a page of a
retired suite does not count). Evidence mode is for a tree the descriptor does not describe: every
file matching `--evidence` (JSON or YAML) is one publication, `--commit-field` and `--time-field`
are dotted keys into it, and `--page`, when given, is a path template (`str.format` over the
file's top-level scalar fields plus `commit`, e.g. `result/{target_profile}/{suite_id}/{commit:.12}.md`)
naming the published page a file must have to count, so a raw run that was never published is
not evidence.
"""
import argparse
import datetime
import glob
import json
import pathlib
import sys

import yaml

from . import descriptor
from .stack import rev_file_value

FIX = ("either record a publication at the pin, or keep the pin at the last measured revision{last}")


def _matches(commit, pin):
    commit, pin = (commit or "").lower(), (pin or "").lower()
    return bool(commit and pin) and (commit.startswith(pin) or pin.startswith(commit))


def _fix(last):
    return FIX.format(last=f" ({last})" if last else "")


def check(publications, pins):
    """Problems, given `publications` and `pins`.

    `publications`: dicts with `name` (what to print), `time` (sortable) and `commits`
    ({identity: commit}). `pins`: {identity: (label, resolved commit)}, the label being what the
    message names (the pin file and its value). An empty `publications` fails too: a pin with
    nothing recorded anywhere is not evidence of anything.
    """
    problems = []
    for identity, (label, commit) in pins.items():
        if not commit:
            problems.append(f"{label} cannot be resolved to a commit, so no publication can be "
                            f"matched to it; fix the pin or materialise its checkout")
    if problems:
        return problems
    names = " and ".join(label for label, _ in pins.values())

    def at_pin(p):
        return all(_matches(p["commits"].get(i), c) for i, (_, c) in pins.items())

    def revisions(p):
        return "/".join((p["commits"].get(i) or "?")[:12] for i in pins)

    if not publications:
        return [f"{names}: no publication is recorded at all, so the pin has no current recording; "
                f"{_fix(None)}"]
    newest = max(publications, key=lambda p: p["time"])
    if not any(at_pin(p) for p in publications):
        problems.append(f"{names}: no current recording - no publication was measured at this "
                        f"revision (newest publication: {newest['name']}, measured at "
                        f"{revisions(newest)}); {_fix(revisions(newest))}")
    elif not at_pin(newest):
        problems.append(f"{names}: the newest publication {newest['name']} was measured at "
                        f"{revisions(newest)}, not at the pin; {_fix(revisions(newest))}")
    return problems


def campaign_problems(found=None):
    """`check` over a toolkit campaign; `found` is `gen_index.collect()` (computed when omitted)."""
    from . import gen_index
    d = descriptor.current()
    found = found if found is not None else gen_index.collect()
    pins = {}
    for identity in d.identities:
        rel = d.pin_file(identity).relative_to(d.root).as_posix()
        pins[identity] = (f"{rel} = {found['references'][identity] or '(empty)'}",
                          found["resolved"][identity])
    pubs = [dict(name=p["page"], time=p["sort_key"],
                 commits={"library": p["library_commit"], "upstream": p["upstream_commit"]})
            for p in found["pages"] if p.get("page") and not p.get("retired")]
    return check(pubs, pins)


def _field(data, dotted):
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _time(value):
    if isinstance(value, datetime.datetime):
        moment = value
    else:
        try:
            moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def evidence_publications(root, patterns, commit_field, time_field, page=None):
    """(publications, problems) of evidence mode. An unreadable file, or one with no commit or no
    time, is a problem, not something to skip: dropping it could hide the newest publication."""
    root = pathlib.Path(root)
    pubs, problems = [], []
    paths = sorted({pathlib.Path(p) for pattern in patterns for p in glob.glob(str(root / pattern))})
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text()
            data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            problems.append(f"{rel}: cannot be read ({exc})")
            continue
        commit, moment = _field(data, commit_field), _time(_field(data, time_field))
        if not commit or moment is None:
            missing = commit_field if not commit else time_field
            problems.append(f"{rel}: has no usable `{missing}`")
            continue
        name = rel
        if page:
            fields = {k: v for k, v in (data or {}).items() if not isinstance(v, (dict, list))}
            try:
                name = page.format(**fields, commit=str(commit))
            except (KeyError, IndexError, ValueError) as exc:
                problems.append(f"{rel}: the page template {page!r} cannot be filled ({exc})")
                continue
            if not (root / name).is_file():
                continue  # a run that was never published is not a publication
        pubs.append(dict(name=name, time=moment, commits={"library": str(commit)}))
    return pubs, problems


def main(argv=None, root=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="the repository root (evidence mode; default: cwd)")
    ap.add_argument("--pin", help="evidence mode: the pin file")
    ap.add_argument("--evidence", action="append", default=[], help="evidence mode: a glob of publications")
    ap.add_argument("--commit-field", help="evidence mode: dotted key of the measured commit")
    ap.add_argument("--time-field", default="timestamp", help="evidence mode: dotted key of the time")
    ap.add_argument("--page", default=None, help="evidence mode: the page a publication must have")
    args = ap.parse_args(argv)
    if args.pin or args.evidence:
        if not (args.pin and args.evidence and args.commit_field):
            ap.error("evidence mode needs --pin, --evidence and --commit-field")
        base = pathlib.Path(args.root or root or ".").resolve()
        value = rev_file_value((base / args.pin).read_text()) if (base / args.pin).is_file() else ""
        pubs, problems = evidence_publications(base, args.evidence, args.commit_field, args.time_field,
                                               args.page)
        problems += check(pubs, {"library": (f"{args.pin} = {value or '(missing)'}", value)})
    else:
        if root is not None:
            descriptor.set_root(root)
        problems = campaign_problems()
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        print("ok   every pin has a current recording, and the newest publication is at the pins")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
