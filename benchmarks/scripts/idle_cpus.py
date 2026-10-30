#!/usr/bin/env python3
"""Pick idle CPUs inside one L3 domain, or check that given CPUs are idle.

Adapted from `tensor4all/tprims-rs` `benchmarks/scripts/idle_cpus.py`
(MIT, Copyright (c) 2026 Lukas Devos and tensor4all contributors). The one
deliberate difference is in `check`: it expands the given CPUs with their SMT
siblings, because a busy sibling contends with the physical core that is being
measured. Upstream checked only the listed logical CPUs.

PERFORMANCE_TIPS.md (Performance-Sensitive Tests And Benchmarks) requires the
measured process to be pinned to cores of one L3 domain that are idle
immediately before and after each measurement; "idle" is a `/proc/stat` busy
fraction over a few seconds. This script implements that observation on
Linux with the standard library only.

    idle_cpus.py pick N [--seconds S] [--max-busy F]   # prints e.g. 8,9,10,11
    idle_cpus.py check CPUS [--seconds S] [--max-busy F] [--profile P] [--dry-run]
    idle_cpus.py resolve (CPUS | --profile P --threads N) [--json]

`pick` exits 2 when no L3 domain has N idle CPUs; `check` exits 1 when a CPU
or one of its siblings is busy. Both exit 3 off Linux (CPU affinity is
Linux-only): record that pinning was unavailable instead.

SMT siblings and the observer's CPUs are never hard-coded. They come from the
hardware profile when one is given (`--profile NAME`, resolved from the toolkit's
`campaign_toolkit/profiles/`, or a YAML path): `smt.sibling_offset` says CPU n's
siblings are n+offset, n+2*offset..., and a thread row's `observer` is the CPU set
to keep the rest of the machine on. Without a profile they come from the live
topology (`thread_siblings_list`, `cache/index3/shared_cpu_list`): the observer
is every CPU outside the L3 domains the measured CPUs touch. `check --profile`
also compares the profile's siblings with the live ones where the host exposes
them and exits 4 on a difference. `resolve` and `check --dry-run` only print what
would be measured, checked and left to the observer, and read neither /proc/stat
nor anything that needs Linux.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_SECONDS = 3.0
DEFAULT_MAX_BUSY = 0.05


def parse_cpu_list(text: str) -> list[int]:
    """`0-3,8,10-11` -> [0, 1, 2, 3, 8, 10, 11]."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-")
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return cpus


def read_times(root: Path) -> dict[int, tuple[int, int]]:
    """Per-CPU (busy, total) jiffies from `proc/stat`."""
    out: dict[int, tuple[int, int]] = {}
    for line in (root / "proc/stat").read_text().splitlines():
        head, *fields = line.split()
        if not head.startswith("cpu") or head == "cpu":
            continue
        v = [int(x) for x in fields]
        idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
        total = sum(v[:8])  # guest time is already inside user/nice
        out[int(head[3:])] = (total - idle, total)
    return out


def busy_fractions(root: Path, seconds: float, sleep=time.sleep) -> dict[int, float]:
    a = read_times(root)
    sleep(seconds)
    b = read_times(root)
    frac: dict[int, float] = {}
    for cpu, (busy1, tot1) in b.items():
        busy0, tot0 = a.get(cpu, (busy1, tot1))
        dt = tot1 - tot0
        frac[cpu] = (busy1 - busy0) / dt if dt > 0 else 0.0
    return frac


def l3_domains(root: Path, cpus: list[int]) -> list[list[int]]:
    """Distinct L3 domains (cache index3 shared_cpu_list), lowest CPU first."""
    seen: dict[tuple[int, ...], None] = {}
    for cpu in cpus:
        p = root / f"sys/devices/system/cpu/cpu{cpu}/cache/index3/shared_cpu_list"
        dom = tuple(parse_cpu_list(p.read_text())) if p.exists() else (cpu,)
        seen.setdefault(dom, None)
    return sorted((list(d) for d in seen), key=lambda d: d[0])


def format_cpu_list(cpus) -> str:
    """[0, 1, 2, 3, 12] -> `0-3,12`."""
    out: list[list[int]] = []
    for cpu in sorted(set(cpus)):
        if out and cpu == out[-1][1] + 1:
            out[-1][1] = cpu
        else:
            out.append([cpu, cpu])
    return ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in out)


def find_profile(spec: str) -> Path:
    """A profile by YAML path, or by name from the toolkit's library."""
    if os.sep in spec or spec.endswith((".yaml", ".yml")):
        return Path(spec)
    dirs = [Path(d) for d in os.environ.get("IDLE_PROFILE_DIRS", "").split(os.pathsep) if d]
    dirs.append(Path(__file__).resolve().parent.parent.parent / "campaign_toolkit" / "profiles")
    try:
        from campaign_toolkit import descriptor
        dirs.append(descriptor.profiles_dir())
    except ImportError:
        pass
    for d in dirs:
        if (d / f"{spec}.yaml").exists():
            return d / f"{spec}.yaml"
    raise SystemExit(f"idle_cpus: no profile {spec!r} (looked in {', '.join(map(str, dirs))})")


def load_profile(spec: str) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("idle_cpus: reading a profile needs pyyaml")
    return yaml.safe_load(find_profile(spec).read_text())


def profile_siblings(profile: dict, cpu: int) -> list[int]:
    """CPU `cpu` and its SMT siblings by the profile's `smt: {threads_per_core, sibling_offset}`."""
    smt = profile.get("smt")
    if not smt:
        return [cpu]
    offset = smt["sibling_offset"]
    return [cpu % offset + k * offset for k in range(smt["threads_per_core"])]


def profile_row(profile: dict, threads: int) -> dict:
    for row in profile.get("thread_rows") or []:
        if row["threads"] == threads:
            return row
    raise SystemExit(f"idle_cpus: profile {profile.get('name')!r} has no {threads}-thread row")


def online_cpus(root: Path) -> list[int]:
    p = root / "sys/devices/system/cpu/online"
    return parse_cpu_list(p.read_text()) if p.exists() else []


def live_observer(root: Path, measured: list[int]) -> list[int]:
    """Every online CPU outside the L3 domains (and SMT siblings) the measured CPUs touch."""
    touched: set[int] = set(with_siblings(root, measured))
    for dom in l3_domains(root, measured):
        touched.update(dom)
    return [c for c in online_cpus(root) if c not in touched]


def resolve(root: Path, cpus: list[int] | None = None, profile: dict | None = None,
            threads: int | None = None) -> dict:
    """What a pinned run measures, what its idle gate checks and where the observer belongs."""
    if profile is not None and threads is not None:
        row = profile_row(profile, threads)
        cpus = parse_cpu_list(row["measured"])
        observer = parse_cpu_list(row.get("observer", ""))
    elif profile is not None:
        observer = None
    else:
        observer = live_observer(root, cpus)
    checked = with_siblings(root, cpus, profile)
    if observer is None:
        observer = [c for c in range(profile["logical_cpus"]) if c not in checked]
    return {"measured": cpus, "siblings_checked": [c for c in checked if c not in cpus],
            "idle_gate": checked, "observer": observer,
            "source": f"profile {profile['name']}" if profile is not None else "live topology"}


def print_resolution(res: dict, out=sys.stdout) -> None:
    print(f"measured cpus:   {format_cpu_list(res['measured'])}", file=out)
    print(f"idle gate:       {format_cpu_list(res['idle_gate'])} "
          f"(SMT siblings checked: {format_cpu_list(res['siblings_checked']) or 'none'})", file=out)
    print(f"observer cpus:   {format_cpu_list(res['observer']) or '(none on this host)'}", file=out)
    print(f"derived from:    {res['source']}", file=out)


def siblings(root: Path, cpu: int) -> list[int]:
    """The hardware threads sharing `cpu`'s physical core, `cpu` itself included."""
    p = root / f"sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
    return parse_cpu_list(p.read_text()) if p.exists() else [cpu]


def with_siblings(root: Path, cpus: list[int], profile: dict | None = None) -> list[int]:
    """`cpus` expanded with every sibling, so a busy sibling is not invisible.

    The siblings are the profile's when one is given, else the live topology's."""
    out: set[int] = set()
    for cpu in cpus:
        out.update(profile_siblings(profile, cpu) if profile is not None else siblings(root, cpu))
    return sorted(out)


def profile_disagrees(root: Path, profile: dict, cpus: list[int]) -> list[str]:
    """CPUs whose live siblings differ from the profile's (CPUs the host does not expose are skipped)."""
    bad = []
    for cpu in cpus:
        p = root / f"sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
        if p.exists() and sorted(parse_cpu_list(p.read_text())) != sorted(profile_siblings(profile, cpu)):
            bad.append(f"cpu{cpu}: profile {format_cpu_list(profile_siblings(profile, cpu))}, "
                       f"host {p.read_text().strip()}")
    return bad


def primary_threads(root: Path, cpus: list[int]) -> list[int]:
    """One hardware thread per physical core (the lowest sibling)."""
    keep = []
    for cpu in cpus:
        p = root / f"sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
        sib = parse_cpu_list(p.read_text()) if p.exists() else [cpu]
        if cpu == min(sib):
            keep.append(cpu)
    return keep


def pick(root: Path, n: int, frac: dict[int, float], max_busy: float) -> list[int] | None:
    """The first n idle primary CPUs of the L3 domain with the most idle ones."""
    best: list[int] | None = None
    for dom in l3_domains(root, sorted(frac)):
        idle = [c for c in primary_threads(root, dom) if frac.get(c, 1.0) <= max_busy]
        if len(idle) >= n and (best is None or len(idle) > len(best)):
            best = idle
    return None if best is None else best[:n]


def main(argv: list[str] | None = None, root: Path = Path("/"), sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pick", "check"):
        p = sub.add_parser(name)
        p.add_argument("arg", help="number of CPUs (pick) or a CPU list (check)")
        p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
        p.add_argument("--max-busy", type=float, default=DEFAULT_MAX_BUSY)
        if name == "check":
            p.add_argument("--profile", help="take SMT siblings from this hardware profile, not the live topology")
            p.add_argument("--dry-run", action="store_true", help="only print what would be checked")
    p = sub.add_parser("resolve", help="print the measured, checked and observer CPU sets without measuring")
    p.add_argument("arg", nargs="?", help="a CPU list (live topology, or the profile's siblings with --profile)")
    p.add_argument("--profile")
    p.add_argument("--threads", type=int, help="with --profile: the thread row to resolve")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "resolve":
        if a.arg is None and not (a.profile and a.threads):
            ap.error("resolve needs a CPU list, or --profile with --threads")
        profile = load_profile(a.profile) if a.profile else None
        res = resolve(root, parse_cpu_list(a.arg) if a.arg else None, profile, None if a.arg else a.threads)
        if a.json:
            print(json.dumps({k: (format_cpu_list(v) if isinstance(v, list) else v) for k, v in res.items()}))
        else:
            print_resolution(res)
        return 0
    if a.cmd == "check" and a.dry_run:
        profile = load_profile(a.profile) if a.profile else None
        print_resolution(resolve(root, parse_cpu_list(a.arg), profile))
        return 0
    if not (root / "proc/stat").exists() or (root == Path("/") and not sys.platform.startswith("linux")):
        print("idle_cpus: not Linux; CPU pinning is unavailable on this host", file=sys.stderr)
        return 3
    frac = busy_fractions(root, a.seconds, sleep)
    if a.cmd == "pick":
        got = pick(root, int(a.arg), frac, a.max_busy)
        if got is None:
            print(f"idle_cpus: no L3 domain has {a.arg} idle CPUs (max busy {a.max_busy})", file=sys.stderr)
            return 2
        print(",".join(map(str, got)))
        return 0
    requested = parse_cpu_list(a.arg)
    profile = load_profile(a.profile) if a.profile else None
    if profile is not None:
        disagree = profile_disagrees(root, profile, requested)
        if disagree:
            print("idle_cpus: the profile's SMT siblings differ from this host's:\n  " + "\n  ".join(disagree),
                  file=sys.stderr)
            return 4
    cpus = with_siblings(root, requested, profile)
    busy = {c: frac.get(c, 1.0) for c in cpus if frac.get(c, 1.0) > a.max_busy}
    for c in cpus:
        mark = "" if c in requested else " (sibling)"
        print(f"cpu{c} busy={frac.get(c, 1.0):.3f}{mark}", file=sys.stderr)
    if busy:
        print(f"idle_cpus: busy CPUs {sorted(busy)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
