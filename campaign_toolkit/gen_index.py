#!/usr/bin/env python3
"""Generate the campaign's result index: a table of contents over the pages.

Each result page is one `(suite, hardware profile, measured revision, vendor
arm)`. The index has two tables: the at-a-glance state of every cell, and the
full list of published pages keyed by revision and hardware.

The failure this exists to prevent: with one tracked report per `(profile,
suite)`, a cell that has not been re-measured keeps its old file, and a reader
sees a commit hash and a table without being able to tell whether that cell
reflects the current library or something weeks old. Reports were
self-describing; the *set* of them was not, so staleness leaked into prose. Here
it is structural and machine-checkable:

  status      meaning
  ----------  ---------------------------------------------------------------
  missing     no run recorded for this cell
  current     measured at the reference revision, or at a descendant
  N behind    N commits touching the suite's `invalidated_by` paths lie between
              the measured revision and the campaign's reference pin
  stale       as `behind`, but N exceeds the suite's `stale_after`
  dirty       the measured checkout had uncommitted changes; never comparable
  diverged    the measured revision is not an ancestor of the reference

`behind` counts invalidating commits, not raw distance: a suite that only
touches `crates/` is not out of date because a doc changed.
"""
import argparse
import pathlib
import re
import subprocess
import sys

from . import descriptor
from .campaign import coverage_of, load, page_relative, page_source, run_spec, vendor_engines


def git(checkout, *args):
    out = subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {checkout}: {out.stderr.strip()}")
    return out.stdout.strip()


def reference_rev(pin):
    """The reference revision named by a pin file.

    `#` comments are allowed and ignored, so a pin can say how the revision was
    determined without a second document to drift from it. The last non-comment
    line is the value.
    """
    value = ""
    for line in pathlib.Path(pin).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            value = line
    return value


def invalidating_distance(checkout, measured, reference, paths):
    """Commits touching `paths` that are in `reference` but not in `measured`.

    A measured revision that is a *descendant* of the reference is current rather
    than diverged: it contains everything the reference names, so nothing the
    suite declares invalidating lies between them. Only a revision that is on
    neither side of the reference is diverged.
    """
    if measured == reference:
        return 0
    try:
        git(checkout, "merge-base", "--is-ancestor", measured, reference)
    except RuntimeError:
        try:
            git(checkout, "merge-base", "--is-ancestor", reference, measured)
        except RuntimeError:
            return None
        return 0
    return int(git(checkout, "rev-list", "--count", f"{measured}..{reference}", "--", *paths) or 0)


def status_of(suite, manifest, references, checkouts, have_checkouts):
    """(status, invalidating_commits) for one recorded run, over every recorded identity.

    A campaign may record more than one library revision; a run's currency is then
    the worse of the two, and the counts add because both are part of the same
    comparison and either one moving means the comparison has to be re-taken.
    """
    upstream = manifest.get("upstream") or {}
    if manifest["library"]["dirty"] or upstream.get("dirty"):
        return "dirty", None
    if not have_checkouts:
        return "unknown", None
    total = 0
    for identity in descriptor.current().identities:
        measured = (manifest["library"]["commit"] if identity == "library"
                    else upstream.get("commit"))
        reference = references.get(identity)
        if not measured or not reference:
            return "unknown", None
        distance = invalidating_distance(checkouts[identity], measured, reference,
                                         suite["invalidated_by"])
        if distance is None:
            return "diverged", None
        total += distance
    if total == 0:
        return "current", 0
    if total > suite["stale_after"]:
        return "stale", total
    return f"{total} behind", total


def collect():
    """The published pages, plus the cells that have no run at all.

    A cell is `(suite, profile, vendor arm)`: one binary links one vendor library,
    so an arm is a separate run at the same revision, and an arm with no run has to
    appear as `missing` rather than be implied by the other arm's page.
    """
    d = descriptor.current()
    profiles = d.profile_names()
    suites = [load(f) for f in sorted(d.suites_dir.glob("*.yaml"))]
    references = {identity: reference_rev(d.pin_file(identity)) for identity in d.identities}
    checkouts = {identity: d.checkout_dir(identity) for identity in d.identities}
    have_checkouts = all((checkout / ".git").exists() for checkout in checkouts.values())
    pages, missing = [], []
    for suite in suites:
        arms = vendor_engines(run_spec(suite)["engines"]) or [None]
        wanted = list(suite["required_profiles"]) + [p for p in suite.get("optional_profiles", [])]
        for profile in wanted:
            required = profile in suite["required_profiles"]
            cell = d.data_dir / profile / suite["id"]
            manifests = sorted(cell.glob("*/run.yaml")) if cell.is_dir() else []
            # One page per revision per arm: keep the run whose numbers the page actually holds.
            per_page = {}
            for mf in manifests:
                m = load(mf)
                rel = page_relative(profile, suite["id"], m)
                page = d.root / rel
                if not page.exists():
                    continue
                source = page_source(page)
                if source is not None and source.resolve() != mf.parent.resolve():
                    continue
                # No readable header means the page cannot be attributed to a run. It is still
                # indexed -- silently dropping it would hide a page -- but the index says so.
                status, distance = status_of(suite, m, references, checkouts, have_checkouts)
                arm = (m.get("vendor") or {}).get("name")
                row = dict(
                    suite=suite["id"], profile=profile, vendor=arm, required=required,
                    commit=m["library"]["commit"][:12],
                    upstream=(m.get("upstream") or {}).get("commit", "")[:12],
                    version=m["library"].get("version"),
                    upstream_version=(m.get("upstream") or {}).get("version"),
                    date=m["timestamp"][:10], timestamp=m["timestamp"],
                    coverage=coverage_of(suite, m),
                    status=status, behind=distance, stale_after=suite["stale_after"],
                    page=rel, unattributed=source is None,
                    sort_key=(m["timestamp"], m["library"]["commit"][:12],
                              (m.get("upstream") or {}).get("commit", "")[:12]),
                )
                if rel not in per_page or row["sort_key"] > per_page[rel]["sort_key"]:
                    per_page[rel] = row
            published_arms = {r["vendor"] for r in per_page.values()}
            for arm in arms:
                if arm not in published_arms:
                    missing.append(dict(suite=suite["id"], profile=profile, vendor=arm,
                                        required=required))
            pages.extend(per_page.values())
    return pages, missing, references, profiles, [s["id"] for s in suites], have_checkouts


def cells(pages, missing):
    """(suite, profile, vendor) groups, newest page first, including cells with no run."""
    order, by = [], {}
    for r in missing:
        key = (r["suite"], r["profile"], r["vendor"])
        by[key] = {"suite": r["suite"], "profile": r["profile"], "vendor": r["vendor"],
                   "required": r["required"], "runs": []}
        order.append(key)
    for r in pages:
        key = (r["suite"], r["profile"], r["vendor"])
        if key not in by:
            by[key] = {"suite": r["suite"], "profile": r["profile"], "vendor": r["vendor"],
                       "required": r["required"], "runs": []}
            order.append(key)
        by[key]["runs"].append(r)
    out = []
    for key in order:
        cell = by[key]
        cell["runs"].sort(key=lambda r: r["sort_key"], reverse=True)
        full = [r for r in cell["runs"] if r["coverage"] == "full"]
        cell["newest"] = (full or cell["runs"])[0] if cell["runs"] else None
        out.append(cell)
    return out


def link(page):
    """A markdown link to a page, with a target that resolves from the index directory.

    The index lives in the result directory, so the target must be relative to
    that directory while the visible text stays the repository-relative path.
    Using the repository-relative path for both produced links to
    `result/result/<profile>/...` that 404.
    """
    d = descriptor.current()
    target = pathlib.Path(page)
    try:
        target = target.relative_to(d.index_path.parent.relative_to(d.root))
    except ValueError:
        pass
    return f"[{page}]({target})"


def revision_of(page):
    """The two revisions a page carries, as `library/upstream`."""
    return f"{page['commit']}/{page['upstream'] or '-'}"


def render(pages, missing, references, generated):
    d = descriptor.current()
    dual = d.has_upstream
    pin_rel = d.measured["pin_file"]
    revision_label = "fork/upstream" if dual else "revision"
    out = [
        "# Result index",
        "",
        f"Generated by `{d.report_field('index_generator')}`. **Do not edit by hand**; CI checks that",
        "this file is what the generator produces.",
        "",
        f"- Reference revision (`{pin_rel}`): `{references.get('library')}`",
    ]
    if dual:
        out.append(f"- Reference upstream revision (`{d.upstream['pin_file']}`): "
                   f"`{references.get('upstream')}`")
        out += [
            f"- Newest recorded run: `{generated}`",
            "- Every page below is one `(suite, hardware profile, library revision, upstream",
            "  revision, vendor arm)` and carries its commits, its hardware and the vendor library",
            "  it linked itself.",
        ]
    else:
        out += [
            f"- Newest recorded run: `{generated}`",
            "- Every page below is one `(suite, hardware profile, revision, vendor arm)` and carries",
            "  its commit, its hardware and the vendor library it linked itself.",
        ]
    out += [
        "",
        "## Cells",
        "",
        "The newest full-coverage page for each cell, and whether it is current. One binary links",
        "one vendor library, so an arm is its own cell: an arm with no run appears as `missing`",
        "rather than being implied by the other arm's page.",
        "",
        f"| suite | profile | vendor | {revision_label} | version | date | coverage | status | page |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in cells(pages, missing):
        mark = "" if cell["required"] else " (optional)"
        r = cell["newest"]
        vendor = f"`{cell['vendor']}`" if cell["vendor"] else "-"
        if r is None:
            out.append(f"| `{cell['suite']}` | `{cell['profile']}`{mark} | {vendor} | - | - | - | - "
                       f"| missing | - |")
        else:
            cell_link = link(r["page"]) if r.get("page") else "(no page)"
            revision = f"`{revision_of(r)}`" if dual else f"`{r['commit']}`"
            out.append(f"| `{cell['suite']}` | `{cell['profile']}`{mark} | {vendor} | {revision} | "
                       f"{r['version'] or '-'} | {r['date']} | {r['coverage']} | {r['status']} | {cell_link} |")
    out += [
        "",
        "`missing` is a normal cell, not an error: the campaign is not run on every",
        "profile for every commit. It becomes an error only for a profile a suite lists",
        "as `required`. `coverage` relates a page to the *current* declaration: `full`",
        "when it was measured over the whole spec and the declaration has not moved since,",
        "`partial` when it covered a subset, and `declaration-changed` when the declaration",
        "itself changed after the measurement, so the two have to be compared by hand. A",
        "partial or declaration-changed page never displaces a full-coverage one.",
        "",
        "## All pages",
        "",
        f"| suite | profile | vendor | {revision_label} | version | date | coverage | status | page |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(pages, key=lambda r: (r["suite"], r["profile"], r["sort_key"]), reverse=True):
        page_link = link(r["page"]) if r.get("page") else "(no page)"
        vendor = f"`{r['vendor']}`" if r["vendor"] else "-"
        revision = f"`{revision_of(r)}`" if dual else f"`{r['commit']}`"
        out.append(f"| `{r['suite']}` | `{r['profile']}` | {vendor} | {revision} | "
                   f"{r['version'] or '-'} | {r['date']} | {r['coverage']} | {r['status']} | {page_link} |")
    out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def broken_links(text):
    """Every markdown target in the index must name a file that exists."""
    missing = []
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        if not (descriptor.current().index_path.parent / target).exists():
            missing.append(target)
    return missing


def main(argv=None, root=None):
    if root is not None:
        descriptor.set_root(root)
    d = descriptor.current()
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed file differs, or a required cell is missing/stale")
    ap.add_argument("--fail-on-stale", action="store_true",
                    help="also fail when a required cell is stale")
    ap.add_argument("--write", action="store_true", help="write result/INDEX.md")
    args = ap.parse_args(argv)

    pages, missing, references, _profiles, _suites, have_checkouts = collect()
    published = [p for p in pages if p["page"]]
    stamps = [p["timestamp"] for p in published]
    text = render(published, missing, references, max(stamps) if stamps else "none")

    problems = []
    if not have_checkouts:
        dirs = ", ".join(sorted(c for c in (d.identity(i)["extern_dir"] for i in d.identities)))
        problems.append(f"{dirs} is absent, so currency cannot be computed "
                        f"(run {d.report_field('setup_script')})")
    for cell in missing:
        if cell["required"]:
            arm = f"/{cell['vendor']}" if cell["vendor"] else ""
            problems.append(f"required cell {cell['suite']}/{cell['profile']}{arm} has no run")
    for cell in cells(pages, missing):
        # The gate is the cell's own newest page, not every page ever published for it: a page
        # kept for the record must not keep a cell failing after the cell has been re-measured.
        r = cell["newest"]
        if not cell["required"] or r is None:
            continue
        arm = f"/{r['vendor']}" if r["vendor"] else ""
        if r.get("unattributed"):
            problems.append(f"{r['page']} carries no readable provenance header, so which run it "
                            f"holds is unknown")
        if r["status"] == "dirty":
            problems.append(f"required cell {r['suite']}/{r['profile']}{arm} was measured from a dirty checkout")
        elif r["status"] == "diverged":
            problems.append(f"required cell {r['suite']}/{r['profile']}{arm} is not an ancestor of the reference")
        elif r["status"] == "stale" and args.fail_on_stale:
            problems.append(f"required cell {r['suite']}/{r['profile']}{arm} is stale "
                            f"({r['behind']} invalidating commits > {r['stale_after']})")

    index_rel = d.index_path.relative_to(d.root)
    if args.write:
        d.index_path.parent.mkdir(parents=True, exist_ok=True)
        d.index_path.write_text(text)
        print(f"wrote {index_rel}")
        return 0
    for target in broken_links(text):
        problems.append(f"generated index links to {target}, which does not exist")

    if args.check:
        if not d.index_path.exists() or d.index_path.read_text() != text:
            problems.append(f"{index_rel} is out of date; run "
                            f"{d.report_field('index_generator')} --write")
        for p in problems:
            print(f"FAIL {p}", file=sys.stderr)
        return 1 if problems else 0
    print(text)
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
