#!/usr/bin/env python3
"""Validate a campaign's declarations: the descriptor and every suite.

This is the content check a campaign (and the toolkit itself) runs in CI. It is
the schema check plus the cross-references a schema cannot express: a population
names a declared family group, a suite names declared profiles, and every engine
a suite may measure is named in the descriptor and documented in the suite.
"""
import argparse
import json
import os
import pathlib
import sys

from . import descriptor, jsonschema_lite
from .campaign import load


def check(root, out=None) -> list:
    out = out or sys.stdout
    d = descriptor.set_root(root)
    problems = []
    schema = json.loads((descriptor.schemas_dir() / "benchmark-suite.schema.json").read_text())
    profiles = set(d.profile_names())
    engines = set(d.engines)
    suites = sorted(d.suites_dir.glob("*.yaml"))
    if not suites:
        problems.append(f"{d.suites_dir} declares no suites")
    for path in suites:
        suite = load(path)
        try:
            jsonschema_lite.validate(suite, schema)
        except jsonschema_lite.ValidationError as exc:
            problems.append(f"{path}: {exc.json_path}: {exc.message}")
            continue
        groups = set(suite["family_groups"])
        for spec in suite["runs"]:
            for population in spec["populations"]:
                unknown = set(population["groups"]) - groups
                if unknown:
                    problems.append(f"{path}: population names unknown groups {sorted(unknown)}")
            for engine in spec["engines"]:
                if engine not in engines:
                    problems.append(f"{path}: engine {engine!r} is not declared by the descriptor")
                elif engine not in suite["engines_doc"]:
                    problems.append(f"{path}: engine {engine!r} has no engines_doc entry")
        for profile in suite["required_profiles"] + suite.get("optional_profiles", []):
            if profile not in profiles:
                problems.append(f"{path}: unknown profile {profile!r}")
    if not problems:
        print(f"declarations ok: {len(suites)} suite(s) under {d.root}", file=out)
    for problem in problems:
        print(f"FAIL {problem}", file=out)
    return problems


def main(argv=None, root=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None, help="the campaign root (default: $CAMPAIGN_ROOT or cwd)")
    args = ap.parse_args(argv)
    target = root or args.root or os.environ.get("CAMPAIGN_ROOT") or pathlib.Path.cwd()
    problems = check(target)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
