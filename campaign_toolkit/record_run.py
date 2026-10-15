#!/usr/bin/env python3
"""Record one (profile, suite) run: build at a revision, verify, measure, publish.

Order matters and is enforced here:

1. one campaign-level admission lock is taken first and held across build,
   verify and timing. The campaign is one measurement at a time and no build
   while a measurement runs; a lock inside a per-revision target directory
   would let a second recorder build another revision during this one;
2. the harness is built out of the measured checkout (or this repository, when
   the campaign owns the harness), into a target directory keyed by revision;
3. the harness's `verify` runs over every population and thread budget before any
   timing, and a mismatch aborts the run (nothing is published from an
   unverified run);
4. each measurement goes through the pinned, idle-checked protocol script;
5. the manifest is validated against its schema *before* it is written, and the
   recorded commit must resolve in the checkout it names.

The harness's `info` output and its CSV columns are its contract with the
recorder: `info` prints one `key=value` per line (the vendor identity is read
back from the binary rather than inferred from the arm requested), and `run
--csv` writes the columns the report reads. Both are declared by the campaign's
descriptor and by the harness, not restated here.
"""
import argparse
import csv
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import platform
import shlex
import subprocess
import sys

import yaml

from . import descriptor
from .campaign import (engines_for_vendor, populations, provider_of, same_populations,
                       vendor_engines)


def sh(cmd, **kw):
    print(f"+ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run([str(c) for c in cmd], check=True, text=True,
                          capture_output=True, **kw)


def git(dir_, *args):
    return subprocess.run(["git", "-C", str(dir_), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


def parse_cpu_list(text):
    cpus = set()
    for part in str(text).split(","):
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-")
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


def sysfs_cpu_list(path):
    return parse_cpu_list(path.read_text()) if path.exists() else set()


def l3_domain(cpu):
    """The CPUs sharing `cpu`'s last-level cache, `cpu` included."""
    return sysfs_cpu_list(pathlib.Path(f"/sys/devices/system/cpu/cpu{cpu}/cache/index3/shared_cpu_list")) or {cpu}


def online_cpus():
    return sysfs_cpu_list(pathlib.Path("/sys/devices/system/cpu/online")) or set(os.sched_getaffinity(0))


def measured_cpus(cpu_sets):
    """The declared measured CPUs plus their SMT siblings: a busy sibling contends for the core."""
    measured = set()
    for spec in cpu_sets.values():
        for cpu in parse_cpu_list(spec):
            measured |= sysfs_cpu_list(pathlib.Path(
                f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")) or {cpu}
    return measured


def observer_cpus(cpu_sets):
    """CPUs outside every L3 domain a measured CPU belongs to: where the observing side belongs."""
    domains = set()
    for spec in cpu_sets.values():
        for cpu in parse_cpu_list(spec):
            domains |= l3_domain(cpu)
    return online_cpus() - domains


def warn_if_measured_cpus_span_l3_domains(cpu_sets):
    """A measured set may span L3 domains; say so, because every touched domain must stay quiet.

    Legitimate measurements can need more cores than one last-level cache serves, so spanning is
    not refused. What must not happen silently is a set that fills only part of several domains:
    the idle guard watches every domain the measurement touches (cache peers as well as SMT
    siblings), and unrelated work on the free cores of a touched domain would contend for the same
    cache. This is a warning to the operator, not a validity rule.
    """
    for threads, spec in cpu_sets.items():
        domains = {frozenset(l3_domain(cpu)) for cpu in parse_cpu_list(spec)}
        if len(domains) > 1:
            print(f"warning: the {threads}T CPU set {spec!r} spans the L3 domains "
                  f"{sorted(sorted(d) for d in domains)}; every CPU in those domains is watched by "
                  f"the idle guard, so the whole domains - not only the measured cores - must stay "
                  f"idle for the run to be admitted", file=sys.stderr)


def require_observer_off_measured_cpus(cpu_sets):
    """Refuse to record from a process that may be scheduled onto a measured CPU.

    Keeping unrelated work off the measured cores is an operator precondition, not something the
    idle guard can repair after the fact: this process is the observer, and its children inherit
    its affinity, so an unpinned recorder can land on the very cores it measures. Fail before the
    build rather than record a number nobody can trust. The suggested pin is derived from this
    machine's topology, not from a hardcoded CPU list.
    """
    measured = measured_cpus(cpu_sets)
    free_cpus = observer_cpus(cpu_sets)
    mine = set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else set()
    shared = sorted(mine & measured)
    if shared:
        free = ", ".join(f"{low}" if high == low else f"{low}-{high}"
                         for low, high in ranges(sorted(free_cpus)))
        sys.exit(f"ERROR: this recording process may run on measured CPUs {shared} "
                 f"(measured set with siblings: {sorted(measured)}); pin the observing side off the "
                 f"measured L3 domain, for example `taskset -c {free or '?'} python3 <recorder> ...`")
    if not free_cpus and os.environ.get("FBENCH_OBSERVER_SHARES_L3") != "1":
        sys.exit("ERROR: the measured CPUs cover every L3 domain on this host, so the observing side "
                 "cannot be placed off the measured cache. Park the observing side for the "
                 "measurement, or set FBENCH_OBSERVER_SHARES_L3=1 to record the residual sharing "
                 "explicitly instead of leaving it implicit")
    if not free_cpus:
        print("warning: FBENCH_OBSERVER_SHARES_L3=1 - the observing side shares an L3 domain with the "
              "measurement; the residual contention is recorded, not removed", file=sys.stderr)


def ranges(cpus):
    out = []
    for cpu in cpus:
        if out and cpu == out[-1][1] + 1:
            out[-1][1] = cpu
        else:
            out.append([cpu, cpu])
    return out


def boost_state():
    """The boost/turbo switches this host exposes, read as `{name: raw value}`."""
    state = {}
    for name, path in (("cpufreq_boost", "/sys/devices/system/cpu/cpufreq/boost"),
                       ("intel_pstate_no_turbo", "/sys/devices/system/cpu/intel_pstate/no_turbo")):
        node = pathlib.Path(path)
        try:
            if node.exists():
                state[name] = node.read_text().strip()
        except OSError:
            pass
    return state


def require_boost_disabled(state=None):
    """Refuse to measure while a boost switch is on: frequency is part of the measurement.

    A boost that is on for one process start and throttled for the next moves the level between
    starts. On this campaign's host the process-median range of `cholesky` 512^2 f64 8T collapsed
    from 40-73% to 1.5-5% once boost was disabled, so a run with boost on is not comparable with a
    run without it. `FBENCH_ALLOW_BOOST=1` records the deviation instead of refusing, for hosts
    where the switch cannot be changed.
    """
    state = boost_state() if state is None else state
    on = [f"{name}={value}" for name, value in sorted(state.items())
          if (name == "cpufreq_boost" and value == "1") or (name == "intel_pstate_no_turbo" and value == "0")]
    if not on:
        return
    if os.environ.get("FBENCH_ALLOW_BOOST") != "1":
        sys.exit("ERROR: CPU boost/turbo is enabled (" + ", ".join(on) + "); disable it before "
                 "measuring, for example `echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost`, "
                 "or set FBENCH_ALLOW_BOOST=1 to record the deviation explicitly")
    print(f"warning: FBENCH_ALLOW_BOOST=1 - boost is on ({', '.join(on)}); the frequency level is "
          f"not fixed, and these numbers are not comparable with boost-off runs", file=sys.stderr)


def toolkit_identity(root):
    """V2 needs this package's own Git checkout, not an enclosing campaign repo."""
    root = pathlib.Path(root).resolve()
    try:
        own_root = pathlib.Path(git(root, 'rev-parse', '--show-toplevel')).resolve()
    except subprocess.CalledProcessError:
        own_root = None
    if own_root != root:
        sys.exit('ERROR: v2 recording requires an editable toolkit install from its own '
                 'Git checkout; an installed distribution has no usable source identity')
    return {'commit': git(root, 'rev-parse', 'HEAD'),
            'dirty': bool(git(root, 'status', '--porcelain', '--untracked-files=normal')),
            'measured_path': str(root)}


def build(checkout, vendor, upstream=None):
    d = descriptor.current()
    env_file = d.root / "target" / f"pin-{vendor}.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    template = (d.measured.get("build_argv")
                or ["{build_script}", "{checkout}", "{out}", "{binary}"])
    tokens = {
        "build_script": str(d.root / d.measured["build_script"]),
        "checkout": str(checkout),
        "upstream": str(upstream) if upstream else "",
        "out": str(env_file),
        "binary": d.measured["binary"],
        "vendor": vendor,
    }
    sh([token.format(**tokens) for token in template],
       env=dict(os.environ, BENCH_VENDOR=vendor))
    pin = {}
    for line in env_file.read_text().splitlines():
        if "=" not in line:
            continue
        # The builder writes shell assignments with `printf %q`, so they are read back as shell
        # words: `.strip("\'\"")` leaves the backslash `%q` puts before a space.
        (assignment,) = shlex.split(line)
        k, _, v = assignment.partition("=")
        pin[k] = v
    return pin


def host_info(profile_name, profiles):
    profile = next(p for p in profiles if p["name"] == profile_name)
    lscpu = subprocess.run(["lscpu"], capture_output=True, text=True).stdout
    logical = next((int(line.split(":")[1].strip()) for line in lscpu.splitlines()
                    if line.startswith("CPU(s):")), 0)
    l3 = next((line.split(":", 1)[1].strip() for line in lscpu.splitlines()
               if line.startswith("L3 cache:")), None)
    power = {}
    for name, path in (("cpufreq_boost", "/sys/devices/system/cpu/cpufreq/boost"),
                       ("intel_no_turbo", "/sys/devices/system/cpu/intel_pstate/no_turbo"),
                       ("amd_pstate_status", "/sys/devices/system/cpu/amd_pstate/status"),
                       ("platform_profile", "/sys/firmware/acpi/platform_profile"),
                       ("governor", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
                       ("energy_performance_preference",
                        "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
                       ("scaling_max_freq_khz", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")):
        node = pathlib.Path(path)
        try:
            if node.exists():
                power[name] = node.read_text().strip()
        except OSError:
            pass
    return {
        "power": power or None,
        "hostname": platform.node(),
        "cpu": profile["cpu"],
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "logical_cpus": logical or profile["logical_cpus"],
        "l3": l3 or profile.get("l3"),
        "l3_domains": profile.get("l3"),
        "notes": profile.get("description"),
    }


def vendor_identity(binary, env, threads=None):
    """`<binary> info` -> the vendor's own account of what was linked.

    Read from the binary that will be measured, executed directly the way the
    recorder executes it, so a loader or link problem shows up here rather than
    halfway through a timing pass.
    """
    command = [str(binary), 'info']
    if threads is not None:
        command += ['--threads', str(threads)]
    out = subprocess.run(command, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        sys.exit(f"ERROR: {binary} info failed: {out.stderr.strip()}")
    fields = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


def lock_providers(lock_path):
    """The measured provider's package dependencies, from the checkout's own `Cargo.lock`.

    Which dependency revisions were linked is provenance: their patches can move
    numerical behaviour, so a page that names the measured commit but not the
    provider it was built against cannot be compared with one built against a
    different provider. A registry dependency records its name and version; a git
    dependency records its revision in `commit`. Each entry keeps the package's own
    name, never the engine's provider name, so it cannot collide with a provider.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # py3.10 has no tomllib; the lock is provenance, not a gate
        return []

    d = descriptor.current()
    packages_of_interest = d.provider_packages
    lock = pathlib.Path(lock_path)
    if not lock.exists():
        return []
    packages = tomllib.loads(lock.read_text()).get("package", [])
    out = []
    for pkg in packages:
        if pkg.get("name") not in packages_of_interest:
            continue
        source = pkg.get("source") or ""
        commit = source.rsplit("#", 1)[-1] if source.startswith("git+") and "#" in source else None
        note = "linked dependency of the measured provider"
        if source:
            note += f"; source {source}"
        out.append({"name": pkg["name"], "version": pkg.get("version"),
                    "commit": commit, "path": None, "note": note})
    return out


def providers_for(engines, vendor, deps=()):
    """The providers and vendor library this run was measured against.

    One entry per measured provider the run's engines name, then the measured provider's linked
    dependencies, then the vendor. A campaign that records a baseline has two measured
    providers, and a manifest that named only one of them would lose that library's identity.
    The vendor entry is written from the identity the harness read out of the linked library,
    never from the arm that was requested: the point of reading it back is that the manifest
    records what was linked rather than what was asked for.
    """
    d = descriptor.current()
    out = []
    names = {provider_of(e) for e in engines}
    where = "library and upstream" if d.has_upstream else "library"
    for measured_name in d.measured_providers:
        if measured_name not in names:
            continue
        out.append({"name": measured_name, "version": None, "commit": None, "path": None,
                    "note": f"the provider in the measured revision; see {where} above"})
    if set(d.measured_providers) & names:
        out.extend(deps)
    for provider in vendor_engines(engines):
        if vendor.get("vendor.name") != provider:
            continue
        note = f"linked {vendor.get('vendor.linkage', '?')}"
        if vendor.get("vendor.corename"):
            note += f", kernel dispatched to {vendor['vendor.corename']}"
        elif provider == "mkl":
            note += ", kernel not reported by the vendor"
        if vendor.get("vendor.config"):
            note += f", config {vendor['vendor.config']}"
        out.append({"name": provider, "version": vendor.get("vendor.version") or None,
                    "commit": None, "path": None, "note": note})
    return out


def vendor_block(vendor, build_env):
    """The structured vendor identity, or None when no vendor arm was declared."""
    if not vendor.get("vendor.name") or vendor["vendor.name"] == "none":
        return None
    return {
        "name": vendor["vendor.name"],
        "linkage": vendor.get("vendor.linkage") or "none",
        "version": vendor.get("vendor.version") or None,
        "config": vendor.get("vendor.config") or None,
        "corename": vendor.get("vendor.corename") or None,
        "parallel": vendor.get("vendor.parallel") or None,
        "procs": int(vendor["vendor.procs"]) if vendor.get("vendor.procs") else None,
        "build_env": build_env or None,
    }


def skipped_of(csv_paths):
    """`family/shape` pairs the harness reported as not applicable, per population.

    Recorded rather than inferred: a family the provider rejects is coverage the
    run did not have, and a page that claimed the whole grid would be wrong.
    """
    seen = {}
    for path in csv_paths:
        with open(path) as handle:
            for row in csv.DictReader(handle):
                if row.get("status") != "skipped":
                    continue
                key = row["regime"]
                shape = row["n"] if row["m"] == row["n"] else f"{row['m']}x{row['n']}"
                seen.setdefault(key, set()).add(f"{row['family']}/{shape}")
    return seen


def protocol_script(checkout, name):
    """The timing protocol's location, from the descriptor.

    A campaign whose harness lives in the measured checkout takes the protocol from
    that checkout; one whose harness lives in the campaign repository owns it here.
    """
    d = descriptor.current()
    where = d.measured.get("protocol", "measured")
    base = (d.root / "benchmarks/scripts") if where == "campaign" else (checkout / "benchmarks/scripts")
    return base / name


def main(argv=None, root=None):
    if root is not None:
        descriptor.set_root(root)
    d = descriptor.current()
    pin_env = d.measured["pin_env"]
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("suite_id")
    ap.add_argument("--checkout", default=str(d.checkout_dir("library")))
    if d.has_upstream:
        ap.add_argument("--upstream", default=str(d.checkout_dir("upstream")),
                        help="the baseline checkout the harness links by path")
    timing = d.get('timing') or {}
    v2 = timing.get('version') == 2
    ap.add_argument("--reps", type=int, default=timing.get('reps', 5))
    ap.add_argument("--aa", type=int, default=timing.get('aa', 1), help="number of complete process-set repeats")
    ap.add_argument("--prime-ms", type=int, default=timing.get('prime_ms', 500))
    ap.add_argument("--regimes", default=None, help="override, comma separated")
    ap.add_argument("--families", default=None,
                    help="override: measure only these families (comma separated). The run records the "
                         "subset it covered, so a partial population is visible rather than implied.")
    ap.add_argument("--threads", default=None, help="override, comma separated")
    ap.add_argument("--dtypes", default=None)
    ap.add_argument("--engines", default=None,
                    help="which engine rows to declare as this run's own, for a report. It does not "
                         "select what the harness measures: the binary measures the rows it was "
                         "built with, whatever this says.")
    ap.add_argument("--vendor", default=None,
                    help="which vendor arm to record: one binary links one vendor library, so an arm "
                         "is its own run. Required when the suite declares more than one.")
    ap.add_argument("--label", default=None, help="suffix for the run directory")
    ap.add_argument('--no-publish', action='store_true',
                    help='retain and validate run artifacts without publishing a result page')
    args = ap.parse_args(argv)

    profiles = d.profiles()
    suite = yaml.safe_load(d.suite_path(args.suite_id).read_text())
    spec = suite["runs"][0]
    declared = populations(suite)
    covered = declared
    if args.regimes:
        wanted = set(args.regimes.split(","))
        covered = [p for p in declared if p["regime"] in wanted]
    if args.families:
        wanted = set(args.families.split(","))
        declared_families = {f for p in declared for f in p["families"]}
        unknown = wanted - declared_families
        if unknown:
            sys.exit(f"ERROR: {sorted(unknown)} are not declared by this suite")
        covered = [dict(p, families=[f for f in p["families"] if f in wanted]) for p in covered]
        covered = [p for p in covered if p["families"]]
    threads = [int(t) for t in args.threads.split(",")] if args.threads else spec["threads"]
    dtypes = args.dtypes.split(",") if args.dtypes else spec["dtypes"]
    # A declared case axis -- the memory order, in the campaigns that declare one. It is part
    # of a cell's identity, so it is recorded and it is part of the coverage comparison.
    order = spec.get("order", [])
    arms = vendor_engines(spec["engines"])
    if args.vendor:
        arm = args.vendor
    elif len(arms) == 1:
        arm = arms[0]
    else:
        sys.exit(f"ERROR: this suite declares the vendor arms {arms}, and one binary links one vendor "
                 f"library, so one run records one of them: pass --vendor")
    if arm not in arms:
        sys.exit(f"ERROR: --vendor {arm!r} is not one of the arms this suite declares ({arms})")
    if arm not in d.vendor_providers:
        sys.exit(f"ERROR: unknown vendor arm {arm!r}; this campaign knows {list(d.vendor_providers)}")
    # The engines this arm is expected to measure: the measured provider's rows every arm carries,
    # plus this arm's own vendor row. Anything else is another binary's to measure.
    engines = args.engines.split(",") if args.engines else engines_for_vendor(suite, arm)
    cpu_sets = {str(t): spec["cpu_sets"][spec["threads"].index(t)] for t in threads}
    warn_if_measured_cpus_span_l3_domains(cpu_sets)
    require_observer_off_measured_cpus(cpu_sets)
    require_boost_disabled()
    if args.reps < 1 or args.aa < 1 or args.prime_ms < 1:
        sys.exit('ERROR: repetitions, process sets and priming must be positive')
    toolkit = None
    harness = {'commit': git(d.root, 'rev-parse', 'HEAD'),
               'dirty': bool(git(d.root, 'status', '--porcelain', '--untracked-files=no'))}
    if v2:
        if args.reps < 2 or args.aa < 2 or not engines or args.aa % len(engines):
            sys.exit('ERROR: v2 needs >=2 calls, >=2 process sets and complete engine-order rotation cycles')
        toolkit_root = pathlib.Path(__file__).resolve().parent.parent
        toolkit = toolkit_identity(toolkit_root)
        if toolkit['dirty'] or harness['dirty']:
            sys.exit('ERROR: v2 requires clean, committed toolkit and harness source')

    # One measurement at a time, and no build while a measurement runs. Taken
    # before the build and held for the whole run.
    lock_path = d.root / "target/campaign.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("ERROR: another campaign run holds target/campaign.lock; "
                 "the campaign is one measurement at a time")

    checkout = pathlib.Path(args.checkout)
    upstream = pathlib.Path(args.upstream) if d.has_upstream else None
    pin = build(checkout, arm, upstream)
    rev, dirty = pin[pin_env["rev"]], pin[pin_env["dirty"]] == "true"
    if v2 and (dirty or (d.has_upstream and pin[d.upstream['pin_env']['dirty']] == 'true')):
        sys.exit('ERROR: v2 requires clean committed library checkouts')
    if dirty:
        print("WARNING: the measured checkout is dirty; this cell will be marked dirty", file=sys.stderr)
    upstream_block = None
    if d.has_upstream:
        up_env = d.upstream["pin_env"]
        upstream_block = {
            "url": d.upstream["url"],
            "commit": pin[up_env["rev"]],
            "version": pin.get(up_env.get("version", "")) or None,
            "ref": pin.get(up_env.get("ref", "")) or None,
            "dirty": pin[up_env["dirty"]] == "true",
            "measured_path": pin[up_env["dir"]],
        }

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{args.label}" if args.label else stamp
    run_dir = d.data_dir / args.profile / args.suite_id / name
    run_dir.mkdir(parents=True, exist_ok=True)

    bin_dir = pathlib.Path(pin[pin_env.get("bin_dir", "BIN_DIR")])
    binary = bin_dir / d.measured["binary"]
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest() if v2 else None
    if v2:
        (run_dir / 'build-info.json').write_text(json.dumps({
            'pin': pin,
            'admission': {'harness': harness, 'toolkit': toolkit, 'argv': list(sys.argv)},
            'binary': {'path': str(binary), 'sha256': binary_sha256},
            'rustc': sh(['rustc', '-vV']).stdout,
            'cargo': sh(['cargo', '-vV']).stdout,
            'env': {key: value for key, value in os.environ.items()
                    if key in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CARGO_BUILD_TARGET', 'CC', 'CFLAGS')
                    or key.startswith('CARGO_PROFILE_')},
        }, indent=2) + '\n')
    pinned = protocol_script(checkout, "pinned.sh")
    idle = protocol_script(checkout, "idle_cpus.py")
    if not pinned.exists() or not idle.exists():
        sys.exit(f"ERROR: {pinned} or {idle} missing; the timing protocol must be present")

    # The thread budget is the measurement's, not the environment's. The vendor
    # library is set from the row's budget by the harness and read back, so an
    # ambient value must not be inherited as if it were a declaration.
    env = dict(os.environ)
    for variable in d.vendor_env:
        env.pop(variable, None)
    env.setdefault("CARGO_BUILD_JOBS", str(max(1, (os.cpu_count() or 4) // 4)))

    # What the harness reports it linked decides the manifest, not what was asked for: the arm
    # and the binary have to agree, or the run is not recorded at all.
    identity = vendor_identity(binary, env, threads[0] if v2 else None)
    if v2:
        (run_dir / 'binary-info.txt').write_text('\n'.join(f'{k}={v}' for k, v in identity.items()) + '\n')
    if identity.get("vendor.name") != arm:
        sys.exit(f"ERROR: this run records the {arm} arm, but the harness built for it reports "
                 f"{identity.get('vendor.name')!r}; a page may not name a vendor the binary did "
                 f"not link")

    guards = []
    csvs = []
    verification_files = []

    # 1. correctness first, for the whole covered grid, at every budget.
    for population in covered:
        for t in threads:
            stem = f"verify-{population['regime']}-{t}t"
            out = run_dir / f"{stem}.txt"
            guard = run_dir / f"{stem}.guard"
            r = subprocess.run([str(pinned), cpu_sets[str(t)], "--", str(binary), "verify",
                                "--threads", str(t),
                                "--n", ",".join(population["n"]),
                                "--batch", ",".join(str(b) for b in population["batch"]),
                                "--dtype", ",".join(dtypes),
                                "--family", ",".join(population["families"]),
                                *(['--regime', population['regime'], '--csv', str(run_dir / f'{stem}.csv')]
                                  if v2 else [])],
                               env=env, capture_output=True, text=True)
            out.write_text(r.stdout)
            guard.write_text(r.stderr)
            guards.append(guard.name)
            if r.returncode != 0 or "MISMATCH" in r.stdout:
                sys.exit(f"ERROR: verify failed for {population['regime']} {t}T (see {out}); nothing published")
            if "all comparisons within tolerance" not in r.stdout:
                sys.exit(f"ERROR: verify did not report a clean grid for {population['regime']} {t}T")
            if v2:
                verification_files.append(f'{stem}.csv')

    # 2. timing, one CSV per (repeat, population, threads).
    for rep in range(args.aa):
        for population in covered:
            for t in threads:
                stem = f"run{rep}-{population['regime']}-{t}t"
                r = subprocess.run([str(pinned), cpu_sets[str(t)], "--", str(binary), "run",
                                    "--threads", str(t),
                                    "--n", ",".join(population["n"]),
                                    "--batch", ",".join(str(b) for b in population["batch"]),
                                    "--dtype", ",".join(dtypes),
                                    "--family", ",".join(population["families"]),
                                    "--regime", population["regime"],
                                    "--reps", str(args.reps), "--prime-ms", str(args.prime_ms),
                                    *(['--statistic', 'median', '--rotate-rows', str(rep % len(engines))]
                                      if v2 else []),
                                    "--csv", str(run_dir / f"{stem}.csv")],
                                   env=env, capture_output=True, text=True)
                (run_dir / f"{stem}.guard").write_text(r.stderr)
                guards.append(f"{stem}.guard")
                if r.returncode != 0:
                    sys.exit(f"ERROR: run failed for {population['regime']} {t}T (repeat {rep}): {r.stderr[-2000:]}")
                if not (run_dir / f"{stem}.csv").exists():
                    sys.exit(f"ERROR: no CSV produced for {population['regime']} {t}T")
                csvs.append(str(run_dir / f"{stem}.csv"))

    skipped = skipped_of(csvs)
    recorded = []
    for population in covered:
        entry = dict(population)
        if skipped.get(population["regime"]):
            entry["skipped"] = sorted(skipped[population["regime"]])
        recorded.append(entry)

    if v2:
        if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_sha256:
            sys.exit('ERROR: measured binary changed during recording')
        sources = [(d.root, harness['commit']), (toolkit_root, toolkit['commit']),
                   (pin[pin_env['dir']], rev)]
        if upstream_block:
            sources.append((upstream_block['measured_path'], upstream_block['commit']))
        for source, commit in sources:
            if (git(source, 'rev-parse', 'HEAD') != commit
                    or git(source, 'status', '--porcelain', '--untracked-files=no')):
                sys.exit(f'ERROR: measured source changed during recording: {source}')
    manifest = {
        "schema_version": 1,
        "target_profile": args.profile,
        "suite_id": args.suite_id,
        "suite_file": f"{d.data['suites_dir']}/{args.suite_id}.yaml",
        "suite_sha256": hashlib.sha256(d.suite_path(args.suite_id).read_bytes()).hexdigest(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "command": " ".join(sys.argv),
        "library": {
            "url": d.measured["url"],
            "commit": rev,
            "dirty": dirty,
            "features": [f for f in pin[pin_env["features"]].split(",") if f],
            "measured_path": pin[pin_env["dir"]],
            "version": (pin.get(pin_env["version"]) if pin_env.get("version") else None) or None,
        },
        **({"upstream": upstream_block} if upstream_block else {}),
        "vendor": vendor_block(identity, pin.get(pin_env.get("env", "BUILD_ENV"))),
        "harness": harness,
        **({'toolkit': toolkit, 'verification_files': verification_files,
            'binary': {'path': str(binary), 'sha256': binary_sha256}} if v2 else {}),
        "host": host_info(args.profile, profiles),
        "threads": {
            "counts": threads,
            "cpu_sets": cpu_sets,
            "env": {k: os.environ.get(k) for k in d.vendor_env},
            "note": "the vendor's budget is set by the harness from the row's thread count and "
                    "read back before each measurement; these variables were removed from the "
                    "measured process's environment so it cannot inherit a different one",
        },
        "timing_policy": {
            "version": 2 if v2 else 1,
            "minimum_untimed_priming_ms": args.prime_ms,
            "statistic": "median" if v2 else "best",
            **({'row_rotation_step': 1} if v2 else {}),
            "repetitions": args.reps,
            "notes": ("descriptive process medians under a rotating engine-order schedule; "
                      "raw samples and the verification census are retained" if v2 else
                      "priming is time-based, not a call count: after an idle gate this host "
                      "reads low for the first second or two of sustained work"),
        },
        "guards": {
            "idle_window_seconds": int(os.environ.get("PINNED_IDLE_SECONDS", 3)),
            "max_busy_percent": round(float(os.environ.get("PINNED_MAX_BUSY", 0.05)) * 100, 3),
            "attempts": int(os.environ.get("PINNED_RETRIES", 3)),
            "sibling_aware": True,
            "logs": sorted(set(guards)),
        },
        "providers": providers_for(engines, identity,
                                   lock_providers(pathlib.Path(pin[pin_env["dir"]]) / "Cargo.lock")),
        "invalidated_by": suite.get("invalidated_by", d.default_invalidated_by),
        "result_files": [pathlib.Path(c).name for c in csvs],
        "run_spec": {
            "populations": recorded,
            "dtypes": dtypes,
            **({"order": order} if "order" in spec else {}),
            "engines": engines,
            "aa": args.aa,
            "covers_declared_suite": (threads == spec["threads"]
                                      and dtypes == spec["dtypes"]
                                      and order == spec.get("order", [])
                                      and engines == engines_for_vendor(suite, arm)
                                      and same_populations(covered, declared)),
        },
    }

    manifest_path = run_dir / "run.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    v = descriptor.run_module("validate_run", [str(manifest_path)], root=d.root, capture=True)
    print(v.stdout + v.stderr, file=sys.stderr)
    if v.returncode != 0:
        sys.exit("ERROR: run.yaml does not validate; nothing published")

    subprocess.run([sys.executable, "-m", "campaign_toolkit.report_run",
                    "--run-dir", str(run_dir)],
                   env=dict(os.environ, CAMPAIGN_ROOT=str(d.root)),
                   cwd=str(pathlib.Path(__file__).resolve().parent.parent), check=True)
    code = 0
    if not args.no_publish:
        code = descriptor.run_module("publish_report", [str(run_dir)], root=d.root).returncode
    lock.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
