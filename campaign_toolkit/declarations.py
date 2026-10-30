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
from .campaign import check_cpu_sets, load


def cpu_set(text) -> set:
    cpus = set()
    for part in str(text).split(","):
        if part.strip():
            low, _, high = part.partition("-")
            cpus.update(range(int(low), int(high or low) + 1))
    return cpus


def check_profile(profile) -> list:
    """Problems with one hardware profile: the schema, then its thread rows against its SMT layout.

    A row must name as many measured CPUs as it has threads, never two hardware threads of one
    core, and an observer that shares no CPU with a measured core or its SMT siblings.
    """
    schema = json.loads((descriptor.schemas_dir() / "profile.schema.json").read_text())
    name = profile.get("name", "?")
    try:
        jsonschema_lite.validate(profile, schema)
    except jsonschema_lite.ValidationError as exc:
        return [f"profile {name}: {exc.json_path}: {exc.message}"]
    smt = profile.get("smt") or {"threads_per_core": 1, "sibling_offset": profile["logical_cpus"]}
    offset, everything = smt["sibling_offset"], set(range(profile["logical_cpus"]))
    problems = []
    for row in profile.get("thread_rows", []):
        measured, observer = cpu_set(row["measured"]), cpu_set(row["observer"])
        where = f"profile {name}: {row['threads']}T row"
        cores = [cpu % offset for cpu in measured]
        if len(measured) != row["threads"]:
            problems.append(f"{where}: {len(measured)} measured CPUs for {row['threads']} threads")
        if len(set(cores)) != len(cores):
            problems.append(f"{where}: two hardware threads of one core are measured")
        if not (measured | observer) <= everything:
            problems.append(f"{where}: a CPU is outside 0-{profile['logical_cpus'] - 1}")
        checked = {c % offset + k * offset for c in measured for k in range(smt["threads_per_core"])}
        if observer & checked:
            problems.append(f"{where}: the observer shares CPUs {sorted(observer & checked)} with the "
                            f"measured cores or their SMT siblings")
    return problems


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
            # A regime is a *population's* identity, not a bare shape class: the harness's raw-data
            # file names, its skip keys and the report's tables are all keyed by it, so two
            # populations under one label would overwrite each other's evidence.
            regimes = [p["regime"] for p in spec["populations"]]
            duplicates = sorted({r for r in regimes if regimes.count(r) > 1})
            if duplicates:
                problems.append(f"{path}: regime label(s) {duplicates} name more than one "
                                f"population; a regime is a population identity")
            for population in spec["populations"]:
                unknown = set(population["groups"]) - groups
                if unknown:
                    problems.append(f"{path}: population names unknown groups {sorted(unknown)}")
            for engine in spec["engines"]:
                if engine not in engines:
                    problems.append(f"{path}: engine {engine!r} is not declared by the descriptor")
                elif engine not in suite["engines_doc"]:
                    problems.append(f"{path}: engine {engine!r} has no engines_doc entry")
        named = list(dict.fromkeys(suite["required_profiles"] + suite.get("optional_profiles", [])))
        for profile in named:
            if profile not in profiles:
                problems.append(f"{path}: unknown profile {profile!r}")
        sets = suite["runs"][0]["cpu_sets"]
        if isinstance(sets, dict):
            for profile in sets:
                if profile not in named:
                    problems.append(f"{path}: cpu_sets names {profile!r}, which the suite does not list")
        elif len(named) > 1:
            # One list for several machines puts one machine's CPU numbers on the others: the
            # list was written for one of them, so the suite has to say which.
            problems.append(f"{path}: cpu_sets is one list for the profiles {named}; name the profile "
                            f"it was written for (`cpu_sets: {{{named[0]}: {sets}}}`), and the others "
                            f"take their own thread_rows")
        try:
            check_cpu_sets(suite)
        except SystemExit as exc:
            problems.append(f"{path}: {str(exc).removeprefix('ERROR: ')}")
    for profile in d.profiles():
        problems.extend(check_profile(profile))
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
