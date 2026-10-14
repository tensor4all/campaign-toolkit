#!/usr/bin/env python3
"""Union index: read several campaigns' published indexes into one table.

Where a campaign's `result/INDEX.md` answers "is this cell current in *this*
campaign", this answers the question a reader asks across campaigns: which
cells exist, at which revisions, and what their currency is. It is cheap
because each campaign's index already carries exactly those columns and the
currency was already computed, per campaign, against that campaign's own pins
and its own `invalidated_by` paths -- nothing here re-derives a number.

An input is a campaign root or the path to one of its `result/INDEX.md` files;
a root is read through its own published index, so both inputs take the same
code path. One campaign is the same code path as several: the table simply has
one campaign in it.

The output is keyed by `(campaign, suite, profile, revision, vendor)` and each
row links back to the campaign's page and to the campaign's own index.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

import yaml

PAGE_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
REFERENCE = re.compile(r"^- Reference (upstream )?revision \(`([^`]+)`\): `([^`]+)`")


def index_path_of(path) -> pathlib.Path:
    """The published `result/INDEX.md` an input names.

    A campaign root resolves to `<root>/result/INDEX.md`; a file is taken as it
    is. `result_dir` is the convention this reads; an explicit file path always
    works when a campaign has moved it.
    """
    path = pathlib.Path(path)
    if path.is_dir():
        return path / "result/INDEX.md"
    return path


def campaign_id(index: pathlib.Path) -> str:
    """The `id` a campaign declares, or the repository directory's name.

    A published index can outlive the tree it was generated from, so the
    descriptor is used when it is there and the directory name when it is not.
    """
    root = index.resolve().parent.parent
    descriptor = root / "benchmarks/campaign.yaml"
    if descriptor.exists():
        try:
            data = yaml.safe_load(descriptor.read_text())
            if isinstance(data, dict) and data.get("id"):
                return data["id"]
        except yaml.YAMLError:
            pass
    return root.name


def parse_table(lines: list) -> list:
    """The rows of one markdown table, as column-name -> cell text."""
    rows, header = [], None
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def parse_index(index: pathlib.Path) -> dict:
    """One published index: its campaign, its reference revisions, its page rows.

    Only the `## All pages` table is read: it is the one table that carries
    every page, and the `Cells` table is a projection of it that this command
    re-derives by key.
    """
    campaign = campaign_id(index)
    reference = {"library": "", "upstream": ""}
    table, section, capture = [], "", False
    for line in index.read_text().splitlines():
        match = REFERENCE.match(line)
        if match:
            reference["upstream" if match.group(1) else "library"] = match.group(3)
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            capture = section == "All pages"
            continue
        if capture:
            table.append(line)
    rows = []
    for raw in parse_table(table):
        if "suite" not in raw or "page" not in raw:
            continue
        link = PAGE_LINK.search(raw["page"])
        target = (index.parent / link.group(2)).resolve() if link else None
        revision = raw.get("revision", raw.get("fork/upstream", "")).strip("`")
        upstream = raw.get("upstream", "").strip("`")
        if "/" in revision:
            revision, _, embedded = revision.partition("/")
            upstream = embedded if embedded != "-" else ""
        rows.append(dict(
            campaign=campaign, index=index.resolve(),
            reference=reference,
            suite=raw.get("suite", "").strip("`"),
            profile=raw.get("profile", "").strip("`"),
            vendor=("" if raw.get("vendor", "-") == "-" else raw.get("vendor", "").strip("`")),
            revision=revision, upstream="" if upstream == "-" else upstream,
            version=("" if raw.get("version", "-") == "-" else raw.get("version", "")),
            date=raw.get("date", ""), coverage=raw.get("coverage", ""),
            status=raw.get("status", ""),
            page=link.group(1) if link else raw["page"],
            relative=link.group(2) if link else raw["page"],
            target=target,
        ))
    return dict(campaign=campaign, index=index.resolve(), reference=reference, rows=rows)


def key_of(row: dict):
    """What makes a row a row: the campaign, the suite, the profile, the revision, the vendor arm."""
    return (row["campaign"], row["suite"], row["profile"], row["revision"], row["vendor"])


def link_to(target, relative, out_dir=None):
    """A target that resolves from the union index's own directory.

    A union index written beside the campaigns links into them by relative path;
    one written elsewhere (a Pages site) falls back to the campaign-relative path
    the source index printed, which stays readable even when the campaign tree is
    not beside the reader.
    """
    if out_dir is not None and target is not None:
        try:
            return os.path.relpath(target, out_dir)
        except ValueError:
            pass
    return relative


def render(campaigns: list, rows: list, out_dir) -> str:
    out = [
        "# Union index",
        "",
        "Generated by `campaign-union-index`. **Do not edit by hand**; rerun it over the",
        "input indexes. Every row is one campaign's own page, with the currency that",
        "campaign computed against its own pins; nothing here re-derives a number.",
        "",
        "## Campaigns",
        "",
    ]
    for campaign in campaigns:
        ref = campaign["reference"]
        pins = f"reference `{ref['library']}`" if ref["library"] else "no library reference"
        if ref["upstream"]:
            pins += f", upstream `{ref['upstream']}`"
        root = campaign["index"].parent.parent
        relative = f"{root.name}/{campaign['index'].parent.name}/{campaign['index'].name}"
        out.append(f"- `{campaign['campaign']}` ({pins}) -> "
                   f"[{relative}]({link_to(campaign['index'], relative, out_dir)})")
    out += [
        "",
        "## Pages",
        "",
        "Every published page, keyed by `(campaign, suite, profile, revision, vendor)`.",
        "",
        "| campaign | suite | profile | vendor | revision | upstream | version | date | coverage | status | page |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (key_of(r), r["date"], r["upstream"])):
        vendor = f"`{row['vendor']}`" if row["vendor"] else "-"
        page = f"[{row['page']}]({link_to(row['target'], row['relative'], out_dir)})"
        out.append(
            f"| `{row['campaign']}` | `{row['suite']}` | `{row['profile']}` | {vendor} | "
            f"`{row['revision']}` | `{row['upstream'] or '-'}` | {row['version'] or '-'} | "
            f"{row['date']} | {row['coverage']} | {row['status']} | {page} |")
    out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Write one index over several campaigns' published result indexes.")
    ap.add_argument("path", nargs="+",
                    help="a campaign root, or the path to its result/INDEX.md")
    ap.add_argument("--out", default=None,
                    help="where to write the union index (default: stdout)")
    args = ap.parse_args(argv)

    parsed, rows = [], []
    for path in args.path:
        index = index_path_of(path)
        if not index.exists():
            print(f"ERROR: {index} not found; name a campaign root or its result/INDEX.md",
                  file=sys.stderr)
            return 1
        campaign = parse_index(index)
        parsed.append(campaign)
        rows.extend(campaign["rows"])

    # One row per key, newest first when a campaign somehow published two.
    newest = {}
    for row in rows:
        current = newest.get(key_of(row))
        if current is None or (row["date"], row["upstream"]) > (current["date"], current["upstream"]):
            newest[key_of(row)] = row
    text = render(parsed, list(newest.values()),
                  pathlib.Path(args.out).resolve().parent if args.out else None)

    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(newest)} page(s) from {len(parsed)} campaign(s))")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
