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

A campaign's pages are read from its index (the pages at its pins) and the
`HISTORY.md` beside it (every other page), or from an older index's single
`All pages` table. The output has one row per `(campaign, page)` and each row
links back to the campaign's page and to the campaign's own index.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

from . import descriptor

PAGE_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# "fork revision" is the wording a dual-identity campaign publishes for its
# measured revision; "upstream revision" is its baseline.
REFERENCE = re.compile(r"^- Reference (?:(fork|upstream) )?revision \(`([^`]+)`\): `([^`]+)`")


def descriptor_of(path) -> "descriptor.Descriptor | None":
    """The descriptor of the campaign an input names, when it is still there.

    A published index can outlive the tree it was generated from, so both the
    index path and the campaign's `id` come from the descriptor when it exists
    and from the conventional layout when it does not.
    """
    path = pathlib.Path(path).resolve()
    for root in ([path] if path.is_dir() else path.parents):
        if (root / "benchmarks/campaign.yaml").exists():
            try:
                return descriptor.load_descriptor(root)
            except SystemExit:
                return None
    return None


def index_path_of(path) -> pathlib.Path:
    """The published `result/INDEX.md` an input names.

    A campaign root resolves through its descriptor's `result_dir`; a file is
    taken as it is. When the descriptor is absent, `<root>/result/INDEX.md` is
    the convention, and an explicit file path always works.
    """
    path = pathlib.Path(path)
    if path.is_dir():
        campaign = descriptor_of(path)
        if campaign is not None:
            return campaign.index_path
        return path / "result/INDEX.md"
    return path


def campaign_id(index: pathlib.Path) -> str:
    """The `id` a campaign declares, or the repository directory's name."""
    campaign = descriptor_of(index)
    if campaign is not None:
        return campaign["id"]
    return index.resolve().parent.parent.name


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


def _sections(path: pathlib.Path) -> tuple:
    """(reference revisions, {section title: [lines]}) of one generated markdown file."""
    reference = {"library": "", "upstream": ""}
    sections, current = {}, None
    for line in path.read_text().splitlines():
        match = REFERENCE.match(line)
        if match:
            reference["upstream" if match.group(1) == "upstream" else "library"] = match.group(3)
            continue
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
            continue
        if current is not None:
            current.append(line)
    return reference, sections


def _tables(lines: list) -> list:
    """Every markdown table in `lines` (a section may hold several, one per subsection)."""
    tables, table = [], []
    for line in lines:
        if line.startswith("|"):
            table.append(line)
        elif table:
            tables.append(table)
            table = []
    if table:
        tables.append(table)
    return [row for t in tables for row in parse_table(t)]


def parse_index(index: pathlib.Path) -> dict:
    """One published index: its campaign, its reference revisions, its page rows.

    A campaign index lists the pages at its pins in `## Cells` and every other page in the
    `HISTORY.md` beside it (`## Older pages`); both are read, so the union keeps every page.
    An index published before the history file existed carries every page in `## All pages`,
    which is read instead. `missing` cells name no page and are not rows here.
    """
    campaign = campaign_id(index)
    reference, sections = _sections(index)
    if "All pages" in sections:
        raws = _tables(sections["All pages"])
    else:
        raws = _tables(sections.get("Cells", []))
        history = index.parent / "HISTORY.md"
        if history.exists():
            raws += _tables(_sections(history)[1].get("Older pages", []))
    rows = []
    for raw in raws:
        if "suite" not in raw or "page" not in raw:
            continue
        link = PAGE_LINK.search(raw["page"])
        if link is None:
            continue  # a `missing` cell: no page to list
        target = (index.parent / link.group(2)).resolve()
        # A dual-identity campaign publishes its revision as `fork/upstream`; that
        # whole string is the page's identity, so it stays one value here.
        revision = raw.get("revision", raw.get("fork/upstream", "")).strip("`")
        rows.append(dict(
            campaign=campaign,
            suite=raw.get("suite", "").strip("`"),
            # the Cells table marks an optional profile after the name: `zen5-cpu` (optional)
            profile=raw.get("profile", "").split(" (", 1)[0].strip("`"),
            vendor=("" if raw.get("vendor", "-") == "-" else raw.get("vendor", "").strip("`")),
            revision=revision,
            version=("" if raw.get("version", "-") == "-" else raw.get("version", "")),
            date=raw.get("date", ""), coverage=raw.get("coverage", ""),
            status=raw.get("status", ""),
            page=link.group(1),
            relative=link.group(2),
            target=target,
        ))
    return dict(campaign=campaign, index=index.resolve(), reference=reference, rows=rows)


def key_of(row: dict):
    """What makes a row a row: the campaign and the page. Two pages can share a revision and an
    arm (two timing contracts), so the page path is the identity."""
    return (row["campaign"], row["page"])


def sort_key(row: dict):
    return (row["campaign"], row["suite"], row["profile"], row["revision"], row["vendor"], row["date"], row["page"])


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
        "Every published page, keyed by `(campaign, page)` and sorted by suite, profile, revision",
        "and vendor arm.",
        "",
        "| campaign | suite | profile | vendor | revision | version | date | coverage | status | page |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=sort_key):
        vendor = f"`{row['vendor']}`" if row["vendor"] else "-"
        page = f"[{row['page']}]({link_to(row['target'], row['relative'], out_dir)})"
        out.append(
            f"| `{row['campaign']}` | `{row['suite']}` | `{row['profile']}` | {vendor} | "
            f"`{row['revision']}` | {row['version'] or '-'} | "
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

    # One row per page; an index and its history never list a page twice, but the same
    # campaign may be named twice on the command line.
    newest = {}
    for row in rows:
        current = newest.get(key_of(row))
        if current is None or (row["date"], row["revision"]) > (current["date"], current["revision"]):
            newest[key_of(row)] = row
    # A stdout index is linked as if it had been saved in the working directory,
    # which is where a redirected one lands.
    base = pathlib.Path(args.out).resolve().parent if args.out else pathlib.Path.cwd()
    text = render(parsed, list(newest.values()), base)

    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(newest)} page(s) from {len(parsed)} campaign(s))")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
