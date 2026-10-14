#!/usr/bin/env python3
"""Publish one recorded run as the cell's report, then regenerate the index.

This is the only place the publish rule lives, so `record_run` and a manual
re-publish after a report-format change cannot disagree:

* a run that did not cover the suite's declared spec never displaces the
  published report;
* the published file is a copy of the run's own report, so the report and the
  raw data it came from cannot drift apart.
"""
import argparse
import pathlib
import sys

from . import descriptor
from .campaign import coverage_of, find_suite, load, page_relative, page_source


def page_path(manifest):
    """The page this run would be published to. The rule lives in `campaign.py`, with the
    index that has to agree with it.
    """
    return descriptor.root() / page_relative(manifest["target_profile"], manifest["suite_id"], manifest)


def published_full(report, suite):
    """Whether the page already published for this revision covers the whole declaration.

    Read from the run the page itself names, and judged by the same coverage rule the index
    uses. A page whose provenance cannot be read is not full: unknown is not a licence to
    overwrite it, and it is not a licence to protect it either -- it is a reason to look.
    """
    source = page_source(report)
    manifest = source / "run.yaml" if source else None
    if manifest is None or not manifest.exists():
        return None
    return coverage_of(suite, load(manifest)) == "full"


def publish(run_dir):
    d = descriptor.current()
    manifest = load(run_dir / "run.yaml")
    _suite_file, suite = find_suite(manifest["suite_id"])
    if suite is None:
        sys.exit(f"ERROR: {manifest['suite_id']!r} has no declaration; nothing to publish against")
    # The recorder validates before it publishes, but a hand-run publication has to meet the same
    # bar: publishing is what makes a number citable, so it is not a step that may skip the gate.
    check = descriptor.run_module("validate_run", [str(run_dir / "run.yaml")],
                                  root=d.root, capture=True)
    if check.returncode != 0:
        sys.exit(f"ERROR: {run_dir}/run.yaml does not validate; nothing published\n"
                 f"{check.stdout}{check.stderr}")
    report = page_path(manifest)
    report.parent.mkdir(parents=True, exist_ok=True)
    covers = coverage_of(suite, manifest) == "full"
    header = (f"<!-- generated from {run_dir.relative_to(d.root)}/report.md "
              f"by scripts/publish_report.py; the report below is the source of truth -->\n\n")
    if covers or not report.exists():
        report.write_text(header + (run_dir / "report.md").read_text())
        print(f"published {report.relative_to(d.root)} from {run_dir.relative_to(d.root)} "
              f"(coverage={'full' if covers else 'partial'})")
    elif published_full(report, suite):
        print(f"kept the existing {report.relative_to(d.root)}: this run is partial and the page "
              f"already there covers the whole declaration")
    elif published_full(report, suite) is None:
        print(f"kept the existing {report.relative_to(d.root)}: its provenance cannot be read, so "
              f"this partial run will not overwrite it. Re-publish it from its own run, or say "
              f"in the commit message why it is being replaced.")
    else:
        report.write_text(header + (run_dir / "report.md").read_text())
        print(f"replaced the partial {report.relative_to(d.root)} with this run "
              f"(coverage=partial): the page it held did not cover the whole declaration")
    descriptor.run_module("gen_index", ["--write"], root=d.root)


def main(argv=None, root=None):
    if root is not None:
        descriptor.set_root(root)
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    args = ap.parse_args(argv)
    publish(pathlib.Path(args.run_dir).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
