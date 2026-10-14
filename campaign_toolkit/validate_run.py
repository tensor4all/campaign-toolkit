#!/usr/bin/env python3
"""Validate one run manifest against the schema and the declared profiles/suites.

Schema validation is only half of it. The rest is the part a schema cannot
express: that the profile and suite are declared, that `cpu_sets` lines up with
`threads`, and that the recorded commit actually exists in the checkout it
names -- a manifest that points at a commit nobody can resolve is not evidence.
"""
import argparse
import csv
import json
import pathlib
import subprocess
import sys

from . import descriptor, jsonschema_lite
from .campaign import (engines_for_vendor, find_suite, load, populations,
                       same_populations, vendor_engines, vendor_of_engines)


def known_profiles():
    return {p["name"] for p in load(descriptor.current().profiles_file)["profiles"]}


def declaration_drifted(manifest, suite_file):
    """True when this run was taken under a different suite declaration.

    Evidence is immutable and declarations are not: adding an engine to a suite
    must not retroactively invalidate a manifest that was correct when it was
    written, and reconstructing the old declaration is impossible. A manifest
    with no recorded hash predates the field, so it is treated the same way.
    """
    recorded = manifest.get("suite_sha256")
    if not recorded:
        return True
    import hashlib
    current = hashlib.sha256(pathlib.Path(suite_file).read_bytes()).hexdigest()
    return recorded != current


def check_vendor(manifest, errors):
    """The recorded vendor library has to be the one the declared engines name.

    Checked against the manifest's *own* recorded engines rather than against the
    current declaration: this has to hold for a manifest measured under a declaration
    that has since moved, and it is the part that decides whether a page may name its
    vendor at all. One binary links one vendor library, so a run carries one arm.
    """
    named = vendor_engines(manifest.get("run_spec", {}).get("engines", []))
    vendor = manifest.get("vendor") or {}
    if len(named) > 1:
        errors.append(f"run_spec.engines declares the vendors {named}: one binary links one vendor "
                      f"library, so a run measures one arm")
        return
    if not named:
        if vendor:
            errors.append(f"the manifest records vendor {vendor.get('name')!r}, but no declared "
                          f"engine is backed by a vendor library")
        return
    if not vendor:
        errors.append(f"run_spec.engines declares the vendor arm {named[0]!r}, but the manifest "
                      f"records no vendor identity; nothing is published from an arm whose library "
                      f"cannot be named")
    elif vendor.get("name") != named[0]:
        errors.append(f"the manifest records vendor {vendor.get('name')!r}, but its declared engines "
                      f"name {named[0]!r}")


def check_result_rows(path, manifest, errors):
    """Every timed row has to be a measurement, not a failure.

    A family can pass verification and still fail while timing. The harness records that
    in its CSV as `failed` and still exits zero, and the report drops such a row silently,
    so without this a run could be published at full coverage with measurements missing.
    """
    run_dir = pathlib.Path(path).parent
    failures = []
    names = manifest.get("result_files") or []
    if not names:
        errors.append("result_files is empty: a run has to name the files that hold what it "
                      "measured, or the report and the validator are reading different evidence")
    for name in names:
        csv_path = run_dir / name
        if not csv_path.exists():
            errors.append(f"result file {name} is not in the run directory")
            continue
        with open(csv_path) as handle:
            for row in csv.DictReader(handle):
                if row.get("status") not in ("ok", "skipped"):
                    failures.append(f"{name}: {row.get('family')}/{row.get('row')} "
                                    f"{row.get('status')!r} {row.get('note', '')}".strip())
    if failures:
        errors.append(f"{len(failures)} row(s) were recorded as failed rather than measured: "
                      + "; ".join(failures[:3])
                      + (" ..." if len(failures) > 3 else ""))


def spec_of(suite):
    return suite["runs"][0]


def check_declaration(manifest, suite, spec, errors):
    """The checks that only make sense against the declaration as it stands today.

    A manifest was correct under the declaration it was written against and that declaration
    cannot be reconstructed, so these are skipped once the suite has moved -- but only these.
    What a manifest says about itself, what it recorded and what it linked is checked either way.
    """
    declared = {p["regime"]: p for p in populations(suite)}
    rs = manifest.get("run_spec", {})
    # A partial run is legitimate -- the campaign is not run in full on every
    # profile -- but for this vendor arm it may only be a subset of what the suite
    # declares, and the manifest has to say whether it covered the declaration.
    for t in manifest["threads"]["counts"]:
        if t not in spec["threads"]:
            errors.append(f"threads.counts contains {t}, which the suite does not declare")
        if str(t) not in manifest["threads"]["cpu_sets"]:
            errors.append(f"threads.cpu_sets is missing an entry for {t}T")
    covered = rs.get("populations", [])
    if not covered:
        errors.append("run_spec.populations is empty: a run states what it covered")
    for population in covered:
        want = declared.get(population.get("regime"))
        if want is None:
            errors.append(f"run_spec.populations names regime {population.get('regime')!r}, "
                          f"which the suite does not declare")
            continue
        for family in population.get("families", []):
            if family not in want["families"]:
                errors.append(f"run_spec.populations[{population['regime']}] contains family "
                              f"{family!r}, which the suite does not declare for that regime")
        for n in population.get("n", []):
            if n not in want["n"]:
                errors.append(f"run_spec.populations[{population['regime']}] contains n {n!r}, "
                              f"which the suite does not declare for that regime")
        for batch in population.get("batch", []):
            if batch not in want["batch"]:
                errors.append(f"run_spec.populations[{population['regime']}] contains batch "
                              f"{batch!r}, which the suite does not declare for that regime")
    for key, declared_values in (("dtypes", spec["dtypes"]),
                                 ("engines", engines_for_vendor(suite,
                                                                 vendor_of_engines(rs.get("engines", []))
                                                                 or ""),)):
        for v in rs.get(key, []):
            if v not in declared_values:
                errors.append(f"run_spec.{key} contains {v!r}, which the suite does not declare "
                              f"for a run of this vendor arm")
    covers = (same_populations(covered, populations(suite))
              and manifest["threads"]["counts"] == spec["threads"]
              and rs.get("dtypes") == spec["dtypes"]
              and rs.get("engines") == engines_for_vendor(suite, vendor_of_engines(rs.get("engines", []))))
    if rs.get("covers_declared_suite") != covers:
        errors.append(f"run_spec.covers_declared_suite should be {covers}")


def check_commit(identity, info, errors):
    """The recorded commit has to resolve in a checkout that exists here.

    Resolved in a checkout that exists *here*, never in the path recorded by the
    measuring host: a committed manifest has to be verifiable in any checkout, or it
    is not evidence.
    """
    commit = (info or {}).get("commit")
    if not commit:
        return
    d = descriptor.current()
    candidates = [d.checkout_dir(identity)]
    measured = (info or {}).get("measured_path")
    if measured and pathlib.Path(measured, ".git").exists():
        candidates.insert(0, pathlib.Path(measured))
    for directory in candidates:
        if not (directory / ".git").exists():
            continue
        r = subprocess.run(["git", "-C", str(directory), "cat-file", "-e", f"{commit}^{{commit}}"],
                           capture_output=True)
        if r.returncode == 0:
            return
    tried = ", ".join(str(c) for c in candidates)
    errors.append(f"{identity}.commit {commit} could not be resolved in any available "
                  f"checkout ({tried})")


def validate(path, check_git=True):
    errors = []
    manifest = load(path)
    schema = json.loads((descriptor.schemas_dir() / "benchmark-run.schema.json").read_text())
    try:
        jsonschema_lite.validate(manifest, schema)
    except jsonschema_lite.ValidationError as e:
        return [f"schema: {e.json_path}: {e.message}"]

    if manifest["target_profile"] not in known_profiles():
        errors.append(f"target_profile {manifest['target_profile']!r} is not in "
                      f"{descriptor.current().data['profiles_file']}")

    suite_file, suite = find_suite(manifest["suite_id"])
    if suite is None:
        errors.append(f"suite_id {manifest['suite_id']!r} has no {descriptor.current().data['suites_dir']}"
                      f"/*.yaml declaration")
    else:
        declared = pathlib.Path(manifest["suite_file"])
        if not declared.is_absolute():
            # A committed manifest names the declaration relative to the campaign
            # root; resolving against the process's working directory would make
            # validation depend on where it happens to be run from.
            declared = descriptor.root() / declared
        if declared.resolve() != suite_file.resolve():
            errors.append(f"suite_file {declared} is not the declaration for {manifest['suite_id']!r} ({suite_file})")
        check_vendor(manifest, errors)
        if manifest["target_profile"] not in suite["required_profiles"] + suite.get("optional_profiles", []):
            errors.append(f"suite {manifest['suite_id']!r} does not list profile {manifest['target_profile']!r}")
        # Only the declaration-derived checks are skipped when the declaration has moved: a
        # manifest was correct under the declaration it was written against, and reconstructing
        # that declaration is impossible. Everything that stands on its own -- the commit it
        # names, the rows it recorded, the vendor it linked -- is still checked, so a later suite
        # edit cannot switch those off for every manifest in the tree.
        drifted = declaration_drifted(manifest, suite_file)
        if drifted:
            print(f"note {path}: measured under a different declaration than the current one "
                  f"(recorded suite_sha256 {manifest.get('suite_sha256') or 'absent'}); "
                  f"declaration-derived checks skipped", file=sys.stderr)
        else:
            check_declaration(manifest, suite, spec_of(suite), errors)

    if check_git:
        check_commit("library", manifest.get("library") or {}, errors)
        if manifest.get("upstream"):
            check_commit("upstream", manifest["upstream"], errors)
    return errors


def main(argv=None, root=None):
    if root is not None:
        descriptor.set_root(root)
    descriptor.current()
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="+")
    ap.add_argument("--no-git", action="store_true")
    args = ap.parse_args(argv)
    failed = False
    for m in args.manifest:
        errors = validate(m, check_git=not args.no_git)
        if errors:
            failed = True
            print(f"FAIL {m}")
            for e in errors:
                print(f"  {e}")
        else:
            print(f"ok   {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
