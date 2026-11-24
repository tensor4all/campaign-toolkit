"""Shared reading of a campaign's declarations.

`record_run`, `validate_run` and `report_run` all have to agree on what a suite
declares, on which provider an engine name belongs to and on how a published
page is named. That agreement is exactly what a schema cannot express, so it
lives here rather than being restated in each entry point, where the copies
would drift. Every campaign-specific value is read from the descriptor.
"""
from __future__ import annotations

import collections
import pathlib

import yaml

from . import descriptor


# One measured cell, as the harness's CSV and the report and comparison read it. A
# `Cell` is hashable and sorts in this field order, so it is the key of a comparison.
# `axes` holds the values of the descriptor's `case_axes`, in declaration order.
Cell = collections.namedtuple("Cell", "regime family dtype axes m n k batch row threads")


def desc() -> descriptor.Descriptor:
    return descriptor.current()


def load(path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text())


def provider_of(engine: str) -> "str | None":
    """What provides an engine name the harness emits, under its current or a historical name.

    A historical engine name resolves to the provider its current name declares, so a run
    recorded before a rename still has a vendor arm and a measured provider.
    """
    engines = desc().engines
    return engines.get(engine) or engines.get(desc().canonical_engine(engine))


def same_name(recorded: "str | None", current: "str | None") -> bool:
    """True when two engine or provider names are one name, either side possibly historical."""
    if recorded is None or current is None:
        return recorded == current
    d = desc()
    return d.canonical_engine(recorded) == d.canonical_engine(current)


def canonical_engines(engines) -> list:
    """An engine list with every historical name replaced by its current one, order kept."""
    return [desc().canonical_engine(e) for e in engines]


def suite_of(manifest: dict) -> str:
    """The current id of the suite a manifest was recorded under."""
    return desc().canonical_suite(manifest["suite_id"])


def find_suite(suite_id: str):
    """The declaration file and body of a suite, named by its current or a historical id."""
    want = desc().canonical_suite(suite_id)
    for f in sorted(desc().suites_dir.glob("*.yaml")):
        data = load(f)
        if data["id"] == want:
            return f, data
    return None, None


def run_spec(suite: dict) -> dict:
    """The suite's declared measurements. One spec per suite, as the model says."""
    return suite["runs"][0]


def populations(suite: dict, vendor: "str | None" = None) -> list:
    """The declared populations with their family groups expanded.

    The expansion is the definition of what the suite covers, so every entry point
    expands it the same way instead of trusting a manifest's own account.

    A run spec may narrow one vendor arm with `vendor_families: {<provider>: [family, ...]}`:
    that arm is then declared, and so expected to cover, only those families (a population left
    with none is dropped). Every other arm keeps the full set. The narrowing is part of the
    declaration, so coverage, validation and the recorder all read it through here.
    """
    groups = suite["family_groups"]
    only = run_spec(suite).get("vendor_families", {}).get(vendor) if vendor else None
    out = []
    for spec in run_spec(suite)["populations"]:
        families = [f for group in spec["groups"] for f in groups[group]]
        if only is not None:
            families = [f for f in families if f in only]
            if not families:
                continue
        out.append({"regime": spec["regime"], "families": families,
                    "n": [str(n) for n in spec["n"]], "batch": [int(b) for b in spec["batch"]]})
    return out


def same_populations(covered: list, declared: list) -> bool:
    def key(p):
        return (p["regime"], tuple(p["families"]), tuple(p["n"]), tuple(p["batch"]))
    return sorted(key(p) for p in covered) == sorted(key(p) for p in declared)


def declared_for(suite: dict, regime: str) -> list:
    """Every population the suite declares under one regime.

    A regime may be declared more than once, because a population carries one shape
    grid for a set of families, so "which declaration does this population belong
    to" is a list, not one entry.
    """
    return [p for p in populations(suite) if p["regime"] == regime]


def population_covers(covered: dict, declared: list) -> bool:
    """True when a covered population is a subset of what is declared for its regime.

    The comparison is against the union of everything declared for that regime,
    because a regime can carry more than one population. `same_populations` remains
    the exact check that decides full coverage; this only decides whether a covered
    grid is *part of* the declaration.
    """
    candidates = [p for p in declared if p["regime"] == covered.get("regime")]
    if not candidates:
        return False
    return (set(covered.get("families", [])) <= {f for p in candidates for f in p["families"]}
            and set(covered.get("n", [])) <= {n for p in candidates for n in p["n"]}
            and set(covered.get("batch", [])) <= {b for p in candidates for b in p["batch"]})


def vendor_engines(engines) -> list:
    """The vendor libraries a declared engine list measures against, in order.

    One binary links one vendor library, so this is a list only to catch the case a
    declaration asks for two: a run cannot carry more than one of them.
    """
    providers = desc().vendor_providers
    out = []
    for engine in engines:
        provider = provider_of(engine)
        if provider in providers and provider not in out:
            out.append(provider)
    return out


def vendor_of_engines(engines) -> "str | None":
    """The one vendor a run measures against, or None when it declares none."""
    vendors = vendor_engines(engines)
    return vendors[0] if len(vendors) == 1 else None


def _cpu_count(cpus) -> int:
    return sum((int(p.split("-")[1]) - int(p.split("-")[0]) + 1) if "-" in str(p) else 1
               for p in str(cpus).split(","))


def _check_pairs(threads, sets, where) -> None:
    if len(threads) != len(sets):
        raise SystemExit(f"ERROR: this suite declares {len(threads)} thread budgets {threads} but "
                         f"{len(sets)} cpu sets {sets}{where}; they are paired by position")
    for budget, cpus in zip(threads, sets):
        if _cpu_count(cpus) < int(budget):
            raise SystemExit(f"ERROR: the {budget}-thread budget is paired with cpu set {cpus!r}"
                             f"{where}, which holds {_cpu_count(cpus)} cpu(s); threads on fewer cpus "
                             f"than the budget measure something other than the declared budget")


def check_cpu_sets(suite: dict) -> None:
    """Refuse a declaration whose CPU sets cannot hold the thread budgets it pairs them with.

    A suite declares `threads` and `cpu_sets` positionally: the budget at index i is pinned to
    the set at index i. Two mistakes are silent without this check and both produce a run that
    measures less than it claims - a list shorter than the budgets (an index error, or the wrong
    budget pinned), and a set with fewer CPUs than the budget it is paired with, which is what an
    8-thread arm pinned to one core looks like in the manifest. A per-profile mapping is checked
    list by list.
    """
    spec = run_spec(suite)
    threads, sets = spec.get("threads", []), spec.get("cpu_sets", [])
    if isinstance(sets, dict):
        for profile, listed in sets.items():
            _check_pairs(threads, listed, f" for {profile}")
    else:
        _check_pairs(threads, sets, "")


# The CPU set an unpinned profile records: a marker, never a CPU list the process did not run on.
UNPINNED = "unpinned"


def cpu_sets_for(suite: dict, profile: dict, threads) -> tuple:
    """`({str(threads): measured CPUs}, source)` for a run of `suite` on `profile`.

    The machine decides where a thread row runs, so a profile that declares `thread_rows` pins
    the rows by itself. The suite may still say otherwise for one profile: a mapping entry named
    after the profile wins. A suite that declares one list for every profile (the form written
    before profiles carried rows) keeps it on every profile, so no recorded campaign moves its
    CPUs when the toolkit does; `declarations` asks such a suite to name its profile once it
    lists more than one. `source` says which of the three decided.

    A profile declaring `pinned: false` has no CPUs to choose: every row is recorded as
    `UNPINNED`, and a suite list (written for some other machine) is not borrowed for it.
    """
    if profile.get("pinned") is False:
        return {str(t): UNPINNED for t in threads}, f"{profile['name']} is unpinned (pinned: false)"
    spec = run_spec(suite)
    declared, sets = list(spec["threads"]), spec.get("cpu_sets", [])
    if isinstance(sets, dict) and profile["name"] in sets:
        listed = sets[profile["name"]]
        return ({str(t): listed[declared.index(t)] for t in threads},
                f"the suite's cpu_sets for {profile['name']}")
    if not isinstance(sets, dict):
        return {str(t): sets[declared.index(t)] for t in threads}, "the suite's cpu_sets"
    rows = {row["threads"]: row["measured"] for row in profile.get("thread_rows", [])}
    if not rows:
        raise SystemExit(f"ERROR: the suite's cpu_sets name no list for {profile['name']!r}, and that "
                         f"profile declares no thread_rows; add {profile['name']} to the suite's "
                         f"cpu_sets or rows to the profile")
    missing = [t for t in threads if t not in rows]
    if missing:
        raise SystemExit(f"ERROR: profile {profile['name']!r} declares thread rows for "
                         f"{sorted(rows)} but not for {missing}; a row the machine does not declare "
                         f"has no CPUs to run on")
    return {str(t): rows[t] for t in threads}, f"the thread_rows of {profile['name']}"


def engines_for_vendor(suite: dict, vendor: str) -> list:
    """The engines one vendor arm of this suite is expected to measure.

    A binary links one vendor library, so an arm cannot measure another vendor's
    rows. A vendor cell is therefore the measured provider's rows plus that
    vendor's own row, and `covers_declared_suite` is measured against this
    projection rather than against the whole declaration -- otherwise no
    single-vendor run could ever cover the suite, and every cell would be partial
    for ever. The recorder and the validator both call this, so the two cannot
    disagree about what coverage means.
    """
    providers = desc().vendor_providers
    out = []
    for engine in run_spec(suite)["engines"]:
        provider = provider_of(engine)
        if provider not in providers or provider == vendor:
            out.append(engine)
    return out


def cell_of(row: dict) -> Cell:
    """One CSV row as a cell key.

    `k` is read whenever the descriptor declares the column, so a three-index shape is
    distinguished from a two-index one; it is empty for every other family. The engine is keyed
    by its current name, so a run recorded under a historical name compares with a new one.
    """
    d = desc()
    k = row.get("k", "") if "k" in d.csv_columns else ""
    return Cell(row["regime"], row["family"], row["dtype"],
                tuple(row[axis] for axis in d.case_axes),
                int(row["m"]), int(row["n"]), k,
                int(row["batch"]), d.canonical_engine(row["row"]), int(row["threads"]))


def case_dims(m: int, n: int, k: str = "") -> str:
    """A shape as a reader writes it: `N` square, `MxN` rectangular, `MxKxN` three-index."""
    if k:
        return f"{m}x{k}x{n}"
    return f"n{n}" if m == n else f"{m}x{n}"


def case_label(cell: Cell) -> str:
    """One cell as the descriptor's `case_label` template renders it."""
    tokens = {"regime": cell.regime, "family": cell.family, "dtype": cell.dtype,
              "dims": case_dims(cell.m, cell.n, cell.k), "batch": cell.batch,
              "row": cell.row, "threads": cell.threads}
    tokens.update(zip(desc().case_axes, cell.axes))
    return desc().case_label.format(**tokens)


def below_timing_baseline(manifest: dict, baseline: dict | None = None,
                          version: int | None = None) -> list:
    """Why a run's timing policy falls short of the campaign's frozen baseline, or `[]`.

    The baseline (`timing.baseline` in the descriptor) is the priming and the repetition count the
    campaign froze for its timing-policy version after calibrating them. A run recorded under that
    version with less of either is still a valid run, but not one to read against the runs that met
    them: too little priming reads a few percent fast (colmat-benchmark's 150 ms probe), which is the
    size of the ratios a page would otherwise call faster or slower. Only runs of the baseline's own
    version are compared; a run of another version already has a page of its own.
    """
    if baseline is None or version is None:
        d = descriptor.current()
        baseline = d.timing_baseline if baseline is None else baseline
        version = (d.get("timing") or {}).get("version") if version is None else version
    policy = manifest.get("timing_policy") or {}
    if not baseline or policy.get("version") != version:
        return []
    reasons = []
    prime, reps = policy.get("minimum_untimed_priming_ms"), policy.get("repetitions")
    if prime is not None and prime < baseline["prime_ms"]:
        reasons.append(f"priming {prime} ms < {baseline['prime_ms']} ms")
    if reps is not None and reps < baseline["reps"]:
        reasons.append(f"{reps} reps < {baseline['reps']}")
    return reasons


def coverage_of(suite: dict, manifest: dict) -> str:
    """How a run relates to the *current* declaration, by content rather than by hash.

    `full` when the run covered the whole declared grid for its vendor arm, `partial`
    when it covered a subset of it, and `declaration-changed` when what it covered is
    not a subset of what is declared today, so the two have to be compared by hand.
    The grid is what a reader needs to know about; a hash would have called an edited
    sentence a moved declaration. The publish rule and the index both call this, so
    they cannot disagree about which pages are full.
    """
    spec = run_spec(suite)
    rs = manifest.get("run_spec", {})
    arm = vendor_of_engines(rs.get("engines", []))
    want = populations(suite, arm)
    got = rs.get("populations", [])
    if not got:
        return "declaration-changed"
    subset = all(population_covers(p, want) for p in got)
    # A run recorded under a historical engine name covers the engine of its current name.
    recorded = canonical_engines(rs.get("engines", []))
    declared_engines = canonical_engines(engines_for_vendor(suite, arm))
    if not (subset
            and set(manifest["threads"]["counts"]) <= set(spec["threads"])
            and set(rs.get("dtypes", [])) <= set(spec["dtypes"])
            and set(rs.get("order", [])) <= set(spec.get("order", []))
            and set(recorded) <= set(declared_engines)):
        return "declaration-changed"
    full = (same_populations(got, want)
            and manifest["threads"]["counts"] == spec["threads"]
            and rs.get("dtypes") == spec["dtypes"]
            and rs.get("order", []) == spec.get("order", [])
            and recorded == declared_engines)
    return "full" if full else "partial"


def page_name_values(manifest: dict) -> dict:
    """The tokens a campaign's `page_name` template may use.

    The template is part of the descriptor because the name of a page is the name
    of a *measurement*: a single measured revision names the page, and a campaign
    that records a second revision (a baseline) puts both in the name.
    """
    library = manifest["library"]
    upstream = manifest.get("upstream") or {}
    vendor = (manifest.get("vendor") or {}).get("name")
    return {
        "library12": library["commit"][:12],
        "upstream12": (upstream.get("commit") or "")[:12],
        "version": f"-{library['version']}" if library.get("version") else "",
        "vendor": f"-{vendor}" if vendor else "",
        "dirty": "-dirty" if (library.get("dirty") or upstream.get("dirty")) else "",
        "timing": (f"-timing-v{manifest['timing_policy']['version']}"
                   if manifest.get('timing_policy', {}).get('version', 1) > 1 else ''),
    }


def page_relative(profile: str, suite_id: str, manifest: dict) -> str:
    """The published page for a run, relative to the repository root."""
    name = desc().page_name
    for token, value in page_name_values(manifest).items():
        name = name.replace("{" + token + "}", value)
    return f"{desc().data['result_dir']}/{profile}/{suite_id}/{name}.md"


# Generated files under a result directory that are not record pages, and the chart directories
# (committed throughput charts, deploy-time figures) whose Markdown tables belong to their charts.
GENERATED_PAGES = ("INDEX.md", "HISTORY.md", "README.md")
CHART_DIRS = ("throughput", "figures")


def record_pages(site_root):
    """The record pages under a result directory: every `.md` except the generated index files and
    the chart tables. The site, the trend and the generated-file check all list pages with this."""
    return sorted(p for p in pathlib.Path(site_root).rglob("*.md")
                  if p.name not in GENERATED_PAGES and not p.name.startswith("index")
                  and not set(CHART_DIRS) & set(p.parts))


def page_source(page):
    """The run directory a page was generated from, read out of the page itself, or None.

    Publication refuses a partial run over a full-coverage page, so the newest
    manifest that would have written a path is not the same thing as the run whose
    numbers the page holds. The page names the report it was copied from; nothing
    has to guess.
    """
    import re
    with open(page) as handle:
        first = handle.readline()
    found = re.match(r"<!-- generated from (\S+)/report\.md by ", first)
    return descriptor.root() / found.group(1) if found else None


def main(argv=None, root=None):
    """Summarise the descriptor. Shared declaration reading has no other CLI."""
    if root is not None:
        descriptor.set_root(root)
    d = desc()
    print(f"{d['id']}: identities={d.identities} vendor_providers={list(d.vendor_providers)}")
    for engine, provider in sorted(d.engines.items()):
        print(f"  {engine} -> {provider}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
