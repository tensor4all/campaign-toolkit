"""Shared reading of a campaign's declarations.

`record_run`, `validate_run` and `report_run` all have to agree on what a suite
declares, on which provider an engine name belongs to and on how a published
page is named. That agreement is exactly what a schema cannot express, so it
lives here rather than being restated in each entry point, where the copies
would drift. Every campaign-specific value is read from the descriptor.
"""
from __future__ import annotations

import pathlib

import yaml

from . import descriptor


def desc() -> descriptor.Descriptor:
    return descriptor.current()


def load(path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text())


def provider_of(engine: str) -> "str | None":
    """What provides an engine name the harness emits."""
    return desc().engines.get(engine)


def find_suite(suite_id: str):
    """The declaration file and body whose `id` is `suite_id`."""
    for f in sorted(desc().suites_dir.glob("*.yaml")):
        data = load(f)
        if data["id"] == suite_id:
            return f, data
    return None, None


def run_spec(suite: dict) -> dict:
    """The suite's declared measurements. One spec per suite, as the model says."""
    return suite["runs"][0]


def populations(suite: dict) -> list:
    """The declared populations with their family groups expanded.

    The expansion is the definition of what the suite covers, so every entry point
    expands it the same way instead of trusting a manifest's own account.
    """
    groups = suite["family_groups"]
    out = []
    for spec in run_spec(suite)["populations"]:
        families = [f for group in spec["groups"] for f in groups[group]]
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
    want = populations(suite)
    got = rs.get("populations", [])
    if not got:
        return "declaration-changed"
    subset = all(population_covers(p, want) for p in got)
    declared_engines = set(engines_for_vendor(suite, arm))
    if not (subset
            and set(manifest["threads"]["counts"]) <= set(spec["threads"])
            and set(rs.get("dtypes", [])) <= set(spec["dtypes"])
            and set(rs.get("engines", [])) <= declared_engines):
        return "declaration-changed"
    full = (same_populations(got, populations(suite))
            and manifest["threads"]["counts"] == spec["threads"]
            and rs.get("dtypes") == spec["dtypes"]
            and rs.get("engines") == engines_for_vendor(suite, arm))
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
    }


def page_relative(profile: str, suite_id: str, manifest: dict) -> str:
    """The published page for a run, relative to the repository root."""
    name = desc().page_name
    for token, value in page_name_values(manifest).items():
        name = name.replace("{" + token + "}", value)
    return f"{desc().data['result_dir']}/{profile}/{suite_id}/{name}.md"


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
