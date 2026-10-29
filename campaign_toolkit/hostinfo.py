#!/usr/bin/env python3
"""What this host says about itself that decides whether two measurements are comparable.

    python -m campaign_toolkit.hostinfo --json

Boost, governor, the amd-pstate mode, the platform profile, SMT, the NUMA layout (a Zen 2 host's
NPS setting shows up as the node count), the SMT sibling map and the L3 domains are read from
sysfs; the toolchain is read from the environment and `rustc -vV`. Every sysfs read goes through
a root path, so a test runs against a fake tree, and a file that is absent is `null`, never an
error: the same call runs on a laptop, in a container and on macOS, where it reports almost
nothing. `record_run` takes the manifest's `host.power`, `host.numa` and `build.toolchain` from
here, and other repositories that write a run manifest can call it for the same block.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import re
import subprocess
import sys

CPU = "sys/devices/system/cpu"

# manifest key -> sysfs file below the root. The names are the ones `host.power` has always used.
POWER_FILES = (
    ("cpufreq_boost", f"{CPU}/cpufreq/boost"),
    ("intel_no_turbo", f"{CPU}/intel_pstate/no_turbo"),
    ("amd_pstate_status", f"{CPU}/amd_pstate/status"),
    ("platform_profile", "sys/firmware/acpi/platform_profile"),
    ("governor", f"{CPU}/cpu0/cpufreq/scaling_governor"),
    ("energy_performance_preference", f"{CPU}/cpu0/cpufreq/energy_performance_preference"),
    ("scaling_max_freq_khz", f"{CPU}/cpu0/cpufreq/scaling_max_freq"),
)


def parse_cpu_list(text: str) -> list[int]:
    """`0-3,8,10-11` -> [0, 1, 2, 3, 8, 10, 11]."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-")
            cpus.extend(range(int(low), int(high) + 1))
        else:
            cpus.append(int(part))
    return cpus


def format_cpu_list(cpus) -> str:
    """`[0, 1, 2, 3, 12, 13]` -> `0-3,12-13`."""
    out: list[list[int]] = []
    for cpu in sorted(set(cpus)):
        if out and cpu == out[-1][1] + 1:
            out[-1][1] = cpu
        else:
            out.append([cpu, cpu])
    return ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in out)


def read(root, rel: str) -> str | None:
    """The stripped text of `rel` below `root`, or None when it is absent or unreadable."""
    try:
        node = pathlib.Path(root) / rel
        return node.read_text().strip() if node.exists() else None
    except (OSError, UnicodeDecodeError):
        return None


def power_state(root="/") -> dict:
    """`host.power`: the frequency and power controls that are exposed, raw strings as read."""
    state = {}
    for name, rel in POWER_FILES:
        value = read(root, rel)
        if value is not None:
            state[name] = value
    smt = {key: value for key, value in (("active", read(root, f"{CPU}/smt/active")),
                                         ("control", read(root, f"{CPU}/smt/control")))
           if value is not None}
    if smt:
        state["smt"] = smt
    return state


def numa_layout(root="/") -> dict | None:
    """`host.numa`: the node count and each node's CPUs; None when the host exposes no nodes."""
    base = pathlib.Path(root) / "sys/devices/system/node"
    try:
        nodes = sorted((int(p.name[4:]) for p in base.glob("node[0-9]*") if p.name[4:].isdigit()))
    except OSError:
        return None
    if not nodes:
        return None
    cpulists = {}
    for node in nodes:
        text = read(root, f"sys/devices/system/node/node{node}/cpulist")
        if text is not None:
            cpulists[str(node)] = text
    return {"nodes": len(nodes), "cpulists": cpulists}


def logical_cpu_ids(root="/") -> list[int]:
    """The CPU numbers sysfs lists (`cpuN` directories), sorted; empty without sysfs."""
    base = pathlib.Path(root) / CPU
    try:
        return sorted(int(p.name[3:]) for p in base.glob("cpu[0-9]*") if p.name[3:].isdigit())
    except OSError:
        return []


def sibling_groups(root="/") -> list[str]:
    """The distinct SMT sibling sets (`thread_siblings_list`), lowest CPU first, e.g. `0,64`."""
    groups = {}
    for cpu in logical_cpu_ids(root):
        text = read(root, f"{CPU}/cpu{cpu}/topology/thread_siblings_list")
        if text is not None:
            groups[tuple(parse_cpu_list(text))] = text
    return [groups[key] for key in sorted(groups)]


def l3_domains(root="/") -> list[str]:
    """The distinct L3 sharing sets (`cache/index3/shared_cpu_list`), lowest CPU first."""
    domains = {}
    for cpu in logical_cpu_ids(root):
        text = read(root, f"{CPU}/cpu{cpu}/cache/index3/shared_cpu_list")
        if text is not None:
            domains[tuple(parse_cpu_list(text))] = text
    return [domains[key] for key in sorted(domains)]


def cpu_model(root="/") -> str | None:
    """The CPU model string from `/proc/cpuinfo`, or the platform's own account off Linux."""
    text = read(root, "proc/cpuinfo")
    for line in (text or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or None


def collect(root="/") -> dict:
    """Everything above as one JSON-able record; the root is a parameter so a test can fake it."""
    ids = logical_cpu_ids(root)
    return {
        "cpu": cpu_model(root),
        "logical_cpus": len(ids) or None,
        "power": power_state(root) or None,
        "numa": numa_layout(root),
        "smt_siblings": sibling_groups(root) or None,
        "l3_domains": l3_domains(root) or None,
    }


def target_cpu(rustflags: str | None) -> str | None:
    """The last `-C target-cpu=X` (or `-Ctarget-cpu=X`) in a RUSTFLAGS string, else None."""
    found = re.findall(r"-C\s*target-cpu=(\S+)", rustflags or "")
    return found[-1] if found else None


def toolchain(env=None, run=subprocess.run) -> dict:
    """`build.toolchain`: the compiler and the flags in force where this is called.

    This is what the recorder's own environment says, which is what the build script inherits;
    flags set only in a `.cargo/config.toml` are not visible here. `target_cpu` is derived from
    RUSTFLAGS and is None when the flags do not name one.
    """
    env = os.environ if env is None else env
    try:
        verbose = run(["rustc", "-vV"], capture_output=True, text=True, env=dict(env)).stdout or None
    except OSError:
        verbose = None
    release = None
    for line in (verbose or "").splitlines():
        if line.startswith("release:"):
            release = line.split(":", 1)[1].strip()
    rustflags = env.get("CARGO_ENCODED_RUSTFLAGS", "").replace("\x1f", " ") or env.get("RUSTFLAGS") or None
    return {"rustc": verbose, "rustc_release": release, "rustflags": rustflags,
            "target_cpu": target_cpu(rustflags)}


def main(argv=None, root="/") -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print one JSON object (the default is a short summary)")
    ap.add_argument("--root", default=root, help="filesystem root to read sysfs/proc from (default /)")
    args = ap.parse_args(argv)
    record = collect(args.root)
    record["toolchain"] = toolchain()
    if args.json:
        print(json.dumps(record, indent=2))
        return 0
    for key, value in record.items():
        if key != "toolchain":
            print(f"{key}: {value}")
    print(f"toolchain: {record['toolchain']['rustc_release']} target-cpu={record['toolchain']['target_cpu']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
