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

The index is centred on the current pins. Its one table lists, per cell, the page measured
at the reference revisions (every recorded identity: the library commit, and for a campaign
that records a baseline the upstream commit or tag); every other page is listed in the
generated `HISTORY.md` beside it, newest first and grouped by the revisions it measured.
"At the pin" decides what is shown; currency (`status`) still decides the `--check` gate, so
moving a pin before re-measuring keeps CI green while the page moves to the history.

Currency needs the measured checkouts. An absent one is reported only when there
is a recorded page whose currency it would decide; with nothing recorded there is
nothing to compute, and failing on it would be noise on a repository that has not
measured anything yet.
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


def _unique(items):
    """`items` without repeats, first occurrence first."""
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def collect():
    """The published pages, plus the declared cells.

    A cell is `(suite, profile, vendor arm)`: one binary links one vendor library,
    so an arm is a separate run at the same revision, and an arm with no run has to
    appear as `missing` rather than be implied by the other arm's page.

    Pages are keyed by their path, so a page is listed once even when a profile is
    named both as required and as optional. A profile directory that a suite no
    longer declares is still read: its pages are history, not gone.
    """
    d = descriptor.current()
    profiles = d.profile_names()
    suites = [load(f) for f in sorted(d.suites_dir.glob("*.yaml"))]
    references = {identity: reference_rev(d.pin_file(identity)) for identity in d.identities}
    checkouts = {identity: d.checkout_dir(identity) for identity in d.identities}
    have_checkouts = all((checkout / ".git").exists() for checkout in checkouts.values())
    pages, declared = {}, []
    for suite in suites:
        arms = vendor_engines(run_spec(suite)["engines"]) or [None]
        wanted = _unique(list(suite["required_profiles"]) + list(suite.get("optional_profiles", [])))
        on_disk = sorted(p.parent.name for p in d.data_dir.glob(f"*/{suite['id']}") if p.is_dir())
        for profile in _unique(wanted + on_disk):
            required = profile in suite["required_profiles"]
            if profile in wanted:
                declared.extend(dict(suite=suite["id"], profile=profile, vendor=arm, required=required)
                                for arm in arms)
            cell = d.data_dir / profile / suite["id"]
            manifests = sorted(cell.glob("*/run.yaml")) if cell.is_dir() else []
            # One page per revision per arm: keep the run whose numbers the page actually holds.
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
                upstream = m.get("upstream") or {}
                row = dict(
                    suite=suite["id"], profile=profile, vendor=arm, required=required,
                    commit=m["library"]["commit"][:12], library_commit=m["library"]["commit"],
                    upstream=upstream.get("commit", "")[:12], upstream_commit=upstream.get("commit", ""),
                    upstream_ref=upstream.get("ref") or "",
                    version=m["library"].get("version"),
                    upstream_version=upstream.get("version"),
                    date=m["timestamp"][:10], timestamp=m["timestamp"],
                    coverage=coverage_of(suite, m),
                    status=status, behind=distance, stale_after=suite["stale_after"],
                    page=rel, unattributed=source is None, run_dir=mf.parent,
                    sort_key=(m["timestamp"], m["library"]["commit"][:12], upstream.get("commit", "")[:12]),
                )
                if rel not in pages or row["sort_key"] > pages[rel]["sort_key"]:
                    pages[rel] = row
    # Pages of a suite whose declaration was removed: history, never a cell.
    declared_suites = {s["id"] for s in suites}
    for mf in sorted(d.data_dir.glob("*/*/*/run.yaml")):
        profile, suite_id = mf.parent.parent.parent.name, mf.parent.parent.name
        if suite_id in declared_suites:
            continue
        m = load(mf)
        try:
            rel = page_relative(profile, suite_id, m)
        except (KeyError, TypeError, ValueError):
            continue
        page = d.root / rel
        if not page.exists():
            continue
        source = page_source(page)
        if source is not None and source.resolve() != mf.parent.resolve():
            continue
        upstream = m.get("upstream") or {}
        row = dict(
            suite=suite_id, profile=profile, vendor=(m.get("vendor") or {}).get("name"), required=False,
            commit=m["library"]["commit"][:12], library_commit=m["library"]["commit"],
            upstream=upstream.get("commit", "")[:12], upstream_commit=upstream.get("commit", ""),
            upstream_ref=upstream.get("ref") or "", version=m["library"].get("version"),
            upstream_version=upstream.get("version"), date=m["timestamp"][:10], timestamp=m["timestamp"],
            coverage="suite-removed", status="retired", behind=None, stale_after=None,
            page=rel, unattributed=source is None, run_dir=mf.parent, retired=True,
            sort_key=(m["timestamp"], m["library"]["commit"][:12], upstream.get("commit", "")[:12]),
        )
        if rel not in pages or row["sort_key"] > pages[rel]["sort_key"]:
            pages[rel] = row
    resolved = {identity: resolve_pin(references[identity], checkouts[identity],
                                      d.identity(identity).get("url"))
                for identity in d.identities}
    return dict(pages=list(pages.values()), declared=declared, references=references, resolved=resolved,
                profiles=profiles, suites=[s["id"] for s in suites], have_checkouts=have_checkouts)


def unmeasured(pages, declared):
    """Declared cells with no page at any revision (the `--check` gate's `missing`)."""
    published = {(r["suite"], r["profile"], r["vendor"]) for r in pages}
    return [c for c in declared if (c["suite"], c["profile"], c["vendor"]) not in published]


_HEX = re.compile(r"[0-9a-fA-F]{7,40}")


def resolve_pin(pin, checkout, url=None):
    """The commit a pin names: the pin itself when it is a commit, else the tag resolved in the
    checkout, else through `git ls-remote` on the repository URL; '' when it cannot be resolved.

    A tag name in a manifest is not evidence of identity (a recorder may write the nearest
    preceding tag), so a tag pin is compared by the commit it names.
    """
    pin = (pin or "").strip()
    if not pin or _HEX.fullmatch(pin):
        return pin
    if checkout is not None and (pathlib.Path(checkout) / ".git").exists():
        try:
            return git(checkout, "rev-parse", f"{pin}^{{commit}}")
        except RuntimeError:
            pass
    if url:
        out = subprocess.run(["git", "ls-remote", url, f"refs/tags/{pin}", f"refs/tags/{pin}^{{}}"],
                             capture_output=True, text=True)
        lines = dict(reversed(line.split("\t", 1)) for line in out.stdout.splitlines() if "\t" in line)
        return lines.get(f"refs/tags/{pin}^{{}}") or lines.get(f"refs/tags/{pin}") or ""
    return ""


def at_pin(row, resolved):
    """Whether a page was measured at every reference revision the campaign records.

    `resolved` maps each identity to the commit its pin names (`resolve_pin`). An identity whose
    pin could not be resolved matches no page: nothing is shown as current on a guess.
    """
    commits = {"library": row.get("library_commit") or row.get("commit") or "",
               "upstream": row.get("upstream_commit") or row.get("upstream") or ""}
    for identity, pin in resolved.items():
        commit = commits.get(identity, "")
        if not pin or not commit or not (commit.lower().startswith(pin.lower())
                                         or pin.lower().startswith(commit.lower())):
            return False
    return True


def split_pages(pages, declared, resolved):
    """(current cells, history pages): the landing's and the index's one definition of "current".

    A cell is current when it has a page at the pins: the newest full-coverage one, else the
    newest. A declared cell without one is `missing`. Every page not selected for a cell is
    history, newest first (a second page at the pins, a page of a retired suite included).
    `resolved` maps each identity to the commit its pin names (`resolve_pin`).
    """
    order, cells = [], {}
    for c in declared:
        key = (c["suite"], c["profile"], c["vendor"])
        if key not in cells:
            cells[key] = dict(suite=c["suite"], profile=c["profile"], vendor=c["vendor"],
                              required=c["required"], page=None)
            order.append(key)
    pinned = {}
    for r in pages:
        if at_pin(r, resolved) and not r.get("retired"):
            pinned.setdefault((r["suite"], r["profile"], r["vendor"]), []).append(r)
    for key, rows in pinned.items():
        rows.sort(key=lambda r: r["sort_key"], reverse=True)
        full = [r for r in rows if r["coverage"] == "full"]
        if key not in cells:
            cells[key] = dict(suite=key[0], profile=key[1], vendor=key[2], required=False, page=None)
            order.append(key)
        cells[key]["page"] = (full or rows)[0]
    current = [cells[k] for k in order]
    shown = {c["page"]["page"] for c in current if c["page"]}
    history = sorted((r for r in pages if r["page"] not in shown), key=lambda r: r["sort_key"], reverse=True)
    return current, history


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


HISTORY_NAME = "HISTORY.md"
TABLE_HEADER = "| suite | profile | vendor | {revision} | version | date | coverage | status | page |"
TABLE_RULE = "|---|---|---|---|---|---|---|---|---|"


def history_path():
    """`HISTORY.md` beside the index."""
    return descriptor.current().index_path.parent / HISTORY_NAME


def _row(r, dual, profile_mark=""):
    page_link = link(r["page"]) if r.get("page") else "(no page)"
    vendor = f"`{r['vendor']}`" if r["vendor"] else "-"
    revision = f"`{revision_of(r)}`" if dual else f"`{r['commit']}`"
    return (f"| `{r['suite']}` | `{r['profile']}`{profile_mark} | {vendor} | {revision} | "
            f"{r['version'] or '-'} | {r['date']} | {r['coverage']} | {r['status']} | {page_link} |")


def _references_lines(references, generated, dual):
    d = descriptor.current()
    if dual:
        return [
            f"- Reference fork revision (`{d.measured['pin_file']}`): `{references.get('library')}`",
            f"- Reference upstream revision (`{d.upstream['pin_file']}`): "
            f"`{references.get('upstream')}`",
            f"- Newest recorded run: `{generated}`",
            "- Every page below is one `(suite, hardware profile, fork revision, upstream revision,",
            "  vendor arm)` and carries its commits, its hardware and the vendor library it linked.",
        ]
    return [
        f"- Reference revision (`{d.measured['pin_file']}`): `{references.get('library')}`",
        f"- Newest recorded run: `{generated}`",
        "- Every page below is one `(suite, hardware profile, revision, vendor arm)` and carries",
        "  its commit, its hardware and the vendor library it linked itself.",
    ]


def render(pages, declared, references, generated, resolved=None):
    """`INDEX.md`: the cells at the current pins, and one line to the history."""
    d = descriptor.current()
    dual = d.has_upstream
    revision_label = "fork/upstream" if dual else "revision"
    current, history = split_pages(pages, declared, resolved if resolved is not None else references)
    out = [
        "# Result index",
        "",
        f"Generated by `{d.report_field('index_generator')}`. **Do not edit by hand**; CI checks that",
        "this file is what the generator produces.",
        "",
        f"Older pages ({len(history)}): [{HISTORY_NAME}]({HISTORY_NAME}).",
        "",
    ]
    out += _references_lines(references, generated, dual)
    out += [
        "",
        "## Cells",
        "",
        "The page measured at the reference revisions for each cell. One binary links one vendor",
        "library, so an arm is its own cell: an arm with no page at the pins appears as `missing`",
        "rather than being implied by the other arm's page.",
        "",
        TABLE_HEADER.format(revision=revision_label),
        TABLE_RULE,
    ]
    for cell in current:
        mark = "" if cell["required"] else " (optional)"
        r = cell["page"]
        vendor = f"`{cell['vendor']}`" if cell["vendor"] else "-"
        if r is None:
            out.append(f"| `{cell['suite']}` | `{cell['profile']}`{mark} | {vendor} | - | - | - | - "
                       f"| missing | - |")
        else:
            out.append(_row(r, dual, mark))
    out += [
        "",
        "`missing` is a normal cell, not an error: the campaign is not run on every",
        "profile for every commit. A cell is listed here only with a page measured at the",
        "reference revisions; every other page is in the history. `status` is currency: a page",
        "measured before the pin moved can still be `current` (no invalidating commit since), and",
        "the `--check` gate follows currency, so moving a pin before re-measuring does not fail CI.",
        "`coverage` relates a page to the *current* declaration: `full` when it was measured over",
        "the whole spec and the declaration has not moved since, `partial` when it covered a",
        "subset, and `declaration-changed` when the declaration itself changed after the",
        "measurement. A partial or declaration-changed page never displaces a full-coverage one.",
    ]
    return "\n".join(out).rstrip("\n") + "\n"


def render_history(pages, declared, references, generated, resolved=None):
    """`HISTORY.md`: every page not shown in the index, newest first, by measured revisions."""
    d = descriptor.current()
    dual = d.has_upstream
    revision_label = "fork/upstream" if dual else "revision"
    _current, history = split_pages(pages, declared, resolved if resolved is not None else references)
    out = [
        "# Result history",
        "",
        f"Generated by `{d.report_field('index_generator')}`. **Do not edit by hand**; CI checks that",
        "this file is what the generator produces.",
        "",
        "Every page not selected for the index's Cells table, newest first and grouped by the",
        "revisions it measured. The current pages are in [INDEX.md](INDEX.md).",
        "",
    ]
    out += _references_lines(references, generated, dual)
    out += ["", "## Older pages", ""]
    groups, order = {}, []
    for r in history:
        key = (r["commit"], r["upstream"] if dual else "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    if not order:
        out.append("No older pages.")
    for key in order:
        rows = groups[key]
        head = rows[0]
        version = head["version"] or "-"
        if dual:
            upstream = head["upstream_ref"] or head["upstream_version"] or "-"
            title = f"`{revision_of(head)}`: version {version}, upstream {upstream}"
        else:
            title = f"`{head['commit']}`: version {version}"
        out += [f"### {title}", "", TABLE_HEADER.format(revision=revision_label), TABLE_RULE]
        out += [_row(r, dual) for r in sorted(rows, key=lambda r: (r["suite"], r["profile"], r["vendor"] or ""))]
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

    found = collect()
    declared, references, resolved = found["declared"], found["references"], found["resolved"]
    have_checkouts = found["have_checkouts"]
    pages = found["pages"]
    published = [p for p in pages if p["page"]]
    missing = unmeasured(published, declared)
    stamps = [p["timestamp"] for p in published]
    generated = max(stamps) if stamps else "none"
    text = render(published, declared, references, generated, resolved)
    history_text = render_history(published, declared, references, generated, resolved)

    problems = []
    if not have_checkouts and published:
        # Currency is a property of recorded evidence. With nothing recorded there is
        # nothing to compute, so an absent checkout is only a note; with a page in the
        # tree it is the reason the index cannot say whether that page is current.
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

    outputs = [(d.index_path, text), (history_path(), history_text)]
    if args.write:
        d.index_path.parent.mkdir(parents=True, exist_ok=True)
        for path, body in outputs:
            path.write_text(body)
            print(f"wrote {path.relative_to(d.root)}")
        return 0
    for target in broken_links(text) + broken_links(history_text):
        if target in (HISTORY_NAME, "INDEX.md"):
            continue  # the pair link each other; --check below compares both
        problems.append(f"generated index links to {target}, which does not exist")

    if args.check:
        for path, body in outputs:
            if not path.exists() or path.read_text() != body:
                problems.append(f"{path.relative_to(d.root)} is out of date; run "
                                f"{d.report_field('index_generator')} --write")
        for p in problems:
            print(f"FAIL {p}", file=sys.stderr)
        return 1 if problems else 0
    print(text)
    print(history_text)
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
