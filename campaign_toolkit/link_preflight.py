#!/usr/bin/env python3
"""Fail in seconds, before any build or timing, when a library a benchmark links cannot be linked or loaded.

    campaign-link-preflight                                   # every arm of benchmarks/link-preflight.yaml
    campaign-link-preflight --arm mkl --arm tblis             # only these arms
    campaign-link-preflight --config PATH --json OUT --log OUT --profile zen5-cpu

A campaign that discovers a missing vendor library at its own build step has already spent the
hours before that step: a refresh reached a follow-up harness's build and stopped on
`rust-lld: error: unable to find library -liomp5`, exit 101. `ldconfig -p` would not have caught it,
because the question is not whether the library is installed but whether *this* link line, with
*these* search directories, finds it, and whether the loader then finds it again at run time.

So the check builds the link itself. For each arm (one link line of the campaign: a vendor arm, an
external baseline) it compiles an empty `fn main() {}` with the same `rustc`, `RUSTFLAGS` and
linker the build uses, the arm's `-L` directories and its libraries forced into `DT_NEEDED`
(`--no-as-needed`, as the campaigns' build scripts do), then asks the loader to resolve the result
under the run-time `LD_LIBRARY_PATH` the runner arranges (or the baked `-rpath`, for arms whose
build records one). A link the probe cannot make is one the build cannot make; a probe the loader
cannot start is a harness the runner cannot start.

The arms are the campaign's declaration, `benchmarks/link-preflight.yaml`:

```yaml
arms:
  mkl:
    source: harness/build.rs (feature link-mkl)  # what this arm mirrors; printed with every failure
    when: {vendor: mkl}                         # record_run runs it for this vendor arm / feature
    env:                                        # variables the build resolves, and how
      MKLROOT: {required: true, default_glob: "/opt/intel/oneapi/mkl/*"}   # or hint_glob: suggest only
      MKL_THREADING_LIB: {required: true, default_glob: "/opt/intel/oneapi/compiler/*/lib/libiomp5.so"}
    search: ["$MKLROOT/lib", "dirname:$MKL_THREADING_LIB"]   # the -L directories, in order
    libs: [mkl_intel_lp64, mkl_intel_thread, mkl_core, iomp5]
    rpath: true                                 # the build bakes the search directories as -rpath
    loader_paths: []                            # directories the runner prepends to LD_LIBRARY_PATH
    fix:                                        # the variable that places a library, per library
      iomp5: "export MKL_THREADING_LIB={path}"  # {path} the file found, {dir} its directory, {prefix} the parent
```

A search entry whose variable is unset is dropped, as the build scripts drop it, and a directory
that does not exist is recorded as absent rather than passed on. `LIBRARY_PATH` (link time) and
`LD_LIBRARY_PATH` (run time) are inherited, because the build and the runner inherit them.

Exit codes: 0 every arm links and loads; 1 an arm failed (each missing library is named with the
directories searched and the variable to set); 2 the declaration itself is unusable.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import yaml

CONFIG = "benchmarks/link-preflight.yaml"
ARM_KEYS = {"source", "when", "env", "search", "libs", "rpath", "loader_paths", "fix", "hints"}
ENV_KEYS = {"required", "default_glob", "hint_glob", "realpath", "fix"}
# Where a missing library is looked for when the arm names no `hints`, to say where it *is*.
DEFAULT_HINTS = ["/opt/intel/oneapi/*/*/lib", "/opt/intel/oneapi/*/lib", "/opt/intel/oneapi/*/*/lib/intel64",
                 "/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/usr/local/lib", "/usr/local/lib64",
                 "/opt/*/lib", "/opt/*/lib64"]
VAR = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
LINK_MISSING = [re.compile(r"unable to find library -l(\S+)"),        # rust-lld / ld.lld
                re.compile(r"cannot find -l(\S+?)(?::|\s|$)")]          # GNU ld, gold
LOAD_MISSING = re.compile(r"^\s*(\S+) => not found", re.M)
LOAD_ERROR = re.compile(r"error while loading shared libraries: ([^:]+):")


class ConfigError(SystemExit):
    def __init__(self, message):
        super().__init__(2)
        self.message = message


def _version_key(path):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path)]


def newest(pattern):
    """The newest match of `pattern` by version order, skipping `latest` symlinks: a moving
    symlink records nothing a reader can act on, the same rule the build scripts follow."""
    matches = [m for m in glob.glob(pattern) if "/latest" not in m]
    return sorted(matches, key=_version_key)[-1].rstrip("/") if matches else None


def soname(lib):
    """`iomp5` -> `libiomp5.so`; `:libiomp5.so` (an exact `-l:` name) -> `libiomp5.so`."""
    return lib[1:] if lib.startswith(":") else f"lib{lib}.so"


def lib_of(name, libs):
    """The declared library a linker or loader message names (`iomp5`, `libiomp5.so`, `libmkl_core.so.2`)."""
    for lib in libs:
        base = soname(lib)
        if name in (lib, lib.lstrip(":"), base) or name.startswith(base + "."):
            return lib
    return None


def load_config(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise ConfigError(f"no link-preflight declaration at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    arms = data.get("arms")
    if not isinstance(arms, dict) or not arms:
        raise ConfigError(f"{path}: `arms` must map an arm name to its link line")
    for name, arm in arms.items():
        if not isinstance(arm, dict):
            raise ConfigError(f"{path}: arm {name!r} must be a mapping")
        unknown = set(arm) - ARM_KEYS
        if unknown:
            raise ConfigError(f"{path}: arm {name!r} has unknown keys {sorted(unknown)} (known: {sorted(ARM_KEYS)})")
        if not arm.get("libs") or not all(isinstance(lib, str) and lib for lib in arm["libs"]):
            raise ConfigError(f"{path}: arm {name!r} must list at least one library in `libs`")
        for var, spec in (arm.get("env") or {}).items():
            if not isinstance(spec, dict) or set(spec) - ENV_KEYS:
                raise ConfigError(f"{path}: arm {name!r} env {var!r} must be a mapping of {sorted(ENV_KEYS)}")
        when = arm.get("when") or {}
        if set(when) - {"vendor", "feature"}:
            raise ConfigError(f"{path}: arm {name!r} `when` takes `vendor` and/or `feature`")
    return data


def resolve_env(arm, environ):
    """(values, problems, notes): each declared variable as the build resolves it."""
    values, problems, notes = {}, [], []
    for var, spec in (arm.get("env") or {}).items():
        value = environ.get(var) or None
        if value is None and spec.get("default_glob"):
            value = newest(spec["default_glob"])
            if value:
                notes.append(f"{var} unset; resolved to {value} from {spec['default_glob']}")
        if value and spec.get("realpath", True):
            value = os.path.realpath(value)
        if value is None and spec.get("required"):
            # `hint_glob` only suggests a value: a build that requires the variable (no default) still fails.
            hint = newest(spec.get("hint_glob") or spec.get("default_glob") or "") if (
                spec.get("hint_glob") or spec.get("default_glob")) else None
            problems.append({"stage": "env", "variable": var,
                             "message": f"{var} is unset" + (f" and nothing matches {spec['default_glob']}"
                                                              if spec.get("default_glob") else ""),
                             "fix": f"export {var}={hint}" if hint else spec.get("fix") or f"export {var}=<path>"})
        values[var] = value
    return values, problems, notes


def expand(entry, values, environ):
    """A search/loader entry with `$VAR` expanded, or None when a variable it names is unset."""
    dirname = entry.startswith("dirname:")
    text = entry[len("dirname:"):] if dirname else entry
    missing = []

    def sub(m):
        var = m.group(1) or m.group(2)
        value = values.get(var) if var in values else environ.get(var)
        if not value:
            missing.append(var)
            return ""
        return value
    out = VAR.sub(sub, text)
    if missing:
        return None, missing
    return (os.path.dirname(out) if dirname else out), []


def rustflags(environ):
    """The flags cargo would hand rustc: CARGO_ENCODED_RUSTFLAGS wins over RUSTFLAGS, as in cargo."""
    encoded = environ.get("CARGO_ENCODED_RUSTFLAGS")
    if encoded:
        return [f for f in encoded.split("\x1f") if f]
    return environ.get("RUSTFLAGS", "").split()


def candidates(lib, hints):
    """Files that would satisfy `lib`, found under the hint directories (realpath-deduplicated)."""
    name = soname(lib)
    seen, out = set(), []
    for pattern in hints:
        for d in sorted(glob.glob(pattern), key=_version_key, reverse=True):
            for f in sorted(glob.glob(os.path.join(d, name)) + glob.glob(os.path.join(d, name + ".*"))):
                real = os.path.realpath(f)
                if real in seen or "/latest" in f:
                    continue
                seen.add(real)
                out.append(f)
    return out


def fix_for(lib, stage, arm, found):
    """The one command that makes `lib` reachable at `stage` (link or load)."""
    path = found[0] if found else None
    directory = os.path.dirname(path) if path else f"<directory holding {soname(lib)}>"
    template = (arm.get("fix") or {}).get(lib) or (arm.get("fix") or {}).get(lib.lstrip(":"))
    if template:
        return template.format(path=path or f"<path to {soname(lib)}>", dir=directory,
                               prefix=os.path.dirname(directory) if path else "<install prefix>")
    # Link time: the build's -L list is fixed by its build script, so the variable that adds a
    # directory without editing it is the compiler driver's LIBRARY_PATH. Load time: the runner's
    # LD_LIBRARY_PATH (an rpath arm misses only when the link found the library outside its rpath,
    # through LIBRARY_PATH, and the same variable at run time fixes that too).
    var = "LIBRARY_PATH" if stage == "link" else "LD_LIBRARY_PATH"
    return f"export {var}={directory}${{{var}:+:${var}}}"


def probe_arm(name, arm, environ, workdir, extra_loader_paths=()):
    """Link and load one arm. Returns its JSON-able record."""
    start = time.monotonic()
    libs = list(arm["libs"])
    values, problems, notes = resolve_env(arm, environ)
    search, searched = [], []
    for entry in arm.get("search") or []:
        path, missing = expand(entry, values, environ)
        if path is None:
            searched.append({"entry": entry, "path": None, "status": f"skipped: {', '.join(missing)} unset"})
        elif not os.path.isdir(path):
            searched.append({"entry": entry, "path": path, "status": "absent"})
        else:
            searched.append({"entry": entry, "path": path, "status": "searched"})
            search.append(path)
    loader = []
    for entry in list(arm.get("loader_paths") or []) + list(extra_loader_paths):
        path, _ = expand(entry, values, environ)
        if path:
            loader.append(path)
    record = {"arm": name, "source": arm.get("source"), "libs": libs, "env": values, "env_notes": notes,
              "search": searched, "library_path": environ.get("LIBRARY_PATH"), "rpath": bool(arm.get("rpath")),
              "loader_paths": loader, "ld_library_path": None, "missing": [], "ok": False,
              "stage": None, "command": None, "stderr": None, "seconds": None}
    if problems:
        record.update(stage="env", missing=problems, seconds=round(time.monotonic() - start, 3))
        return record

    rustc = environ.get("RUSTC") or "rustc"
    if not shutil.which(rustc):
        raise ConfigError(f"no `{rustc}` on PATH: the link probe needs the compiler the build uses")
    src = pathlib.Path(workdir) / f"{name}.rs"
    src.write_text("fn main() {}\n")
    exe = pathlib.Path(workdir) / f"probe-{name}"
    cmd = [rustc, "--edition", "2021", "-C", "opt-level=0", "-C", "debuginfo=0", *rustflags(environ)]
    linker = environ.get("CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER")
    if linker:
        cmd += ["-C", f"linker={linker}"]
    for d in search:
        cmd += ["-L", f"native={d}"]
    cmd += ["-C", "link-arg=-Wl,--push-state,--no-as-needed"]
    cmd += [x for lib in libs for x in ("-C", f"link-arg=-l{lib}")]
    cmd += ["-C", "link-arg=-Wl,--pop-state"]
    if arm.get("rpath"):
        cmd += [x for d in search for x in ("-C", f"link-arg=-Wl,-rpath,{d}")]
    cmd += [str(src), "-o", str(exe)]
    record["command"] = shlex.join(cmd)
    hints = list(arm.get("hints") or []) + DEFAULT_HINTS
    link = subprocess.run(cmd, capture_output=True, text=True, env=environ, cwd=workdir)
    if link.returncode != 0:
        names = []
        for pattern in LINK_MISSING:
            names += [m.group(1) for m in pattern.finditer(link.stderr)]
        record["stage"] = "link"
        record["stderr"] = link.stderr[-2000:]
        for n in dict.fromkeys(names):
            lib = lib_of(n, libs) or n
            found = candidates(lib, hints)
            record["missing"].append({"stage": "link", "lib": lib, "soname": soname(lib), "found": found,
                                      "fix": fix_for(lib, "link", arm, found)})
        if not record["missing"]:
            record["missing"].append({"stage": "link", "lib": None, "soname": None, "found": [],
                                      "fix": "the link failed for a reason other than a missing library; "
                                             "see stderr"})
        record["seconds"] = round(time.monotonic() - start, 3)
        return record

    run_env = dict(environ)
    inherited = environ.get("LD_LIBRARY_PATH")
    if loader:
        run_env["LD_LIBRARY_PATH"] = os.pathsep.join(loader + ([inherited] if inherited else []))
    record["ld_library_path"] = run_env.get("LD_LIBRARY_PATH")
    names = []
    if shutil.which("ldd"):
        ldd = subprocess.run(["ldd", str(exe)], capture_output=True, text=True, env=run_env)
        names = LOAD_MISSING.findall(ldd.stdout + ldd.stderr)
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=run_env)
    if run.returncode != 0:
        names += LOAD_ERROR.findall(run.stderr)
    if names or run.returncode != 0:
        record["stage"] = "load"
        record["stderr"] = run.stderr[-2000:]
        for n in dict.fromkeys(names):
            lib = lib_of(n, libs) or (":" + n)
            found = candidates(lib, hints)
            record["missing"].append({"stage": "load", "lib": lib, "soname": n, "found": found,
                                      "fix": fix_for(lib, "load", arm, found)})
        if not record["missing"]:
            record["missing"].append({"stage": "load", "lib": None, "soname": None, "found": [],
                                      "fix": f"the probe exited {run.returncode}; see stderr"})
    else:
        record["ok"] = True
    record["seconds"] = round(time.monotonic() - start, 3)
    return record


def describe_search(record):
    parts = []
    for s in record["search"]:
        if s["status"] == "searched":
            parts.append(f"{s['path']} ({s['entry']})")
        elif s["status"] == "absent":
            parts.append(f"{s['path']} ({s['entry']}, absent)")
        else:
            parts.append(f"{s['entry']} ({s['status']})")
    parts.append(f"LIBRARY_PATH={record['library_path'] or '(unset)'}")
    parts.append("the linker's default directories")
    return "; ".join(parts)


def format_record(record, label):
    head = f"{'OK  ' if record['ok'] else 'FAIL'} {label}{record['arm']}"
    if record.get("source"):
        head += f"  [{record['source']}]"
    lines = [head + f"  ({record['seconds']} s)"]
    for note in record.get("env_notes") or []:
        lines.append(f"     note:     {note}")
    shown = set()
    for m in record["missing"]:
        if m["stage"] == "env":
            lines.append(f"     missing:  {m['message']}")
            lines.append(f"     fix:      {m['fix']}")
            continue
        if m["stage"] not in shown:
            # Where it was looked for, once per stage: the same list for every library of the arm.
            shown.add(m["stage"])
            if m["stage"] == "link":
                lines.append(f"     searched: {describe_search(record)}")
            else:
                rpath = ", ".join(s["path"] for s in record["search"] if s["status"] == "searched")
                lines.append("     searched: " + (f"the rpath ({rpath}); " if record["rpath"] else "")
                             + f"LD_LIBRARY_PATH={record['ld_library_path'] or '(unset)'}; the loader's cache")
        what = f"{m['soname']} (-l{m['lib']})" if m.get("lib") else "(unparsed; see stderr)"
        verb = "the linker cannot find" if m["stage"] == "link" else "the loader cannot open"
        lines.append(f"     missing:  {verb} {what}")
        if m.get("found"):
            lines.append(f"     found:    {', '.join(m['found'][:3])}")
        lines.append(f"     fix:      {m['fix']}")
        if not m.get("lib") and record.get("stderr"):
            lines.append("     stderr:   " + record["stderr"].strip().splitlines()[-1][:300])
    return lines


def selected_arms(config, names=None, vendor=None, features=None):
    """The arms to check: `names` if given, else (from the recorder) the arms whose `when` matches."""
    arms = config["arms"]
    if names:
        unknown = [n for n in names if n not in arms]
        if unknown:
            raise ConfigError(f"unknown arm(s) {unknown}; declared: {sorted(arms)}")
        return {n: arms[n] for n in names}
    if vendor is None and features is None:
        return dict(arms)
    out = {}
    for n, arm in arms.items():
        when = arm.get("when") or {}
        if not when:
            out[n] = arm
        elif when.get("vendor") and when["vendor"] == vendor:
            out[n] = arm
        elif when.get("feature") and when["feature"] in (features or ()):
            out[n] = arm
    return out


def run(config_path, names=None, vendor=None, features=None, environ=None, extra_loader_paths=(), label="",
        profile=None):
    """Check the selected arms. Returns the report (JSON-able); `report['ok']` is the verdict."""
    environ = dict(os.environ if environ is None else environ)
    config = load_config(config_path)
    arms = selected_arms(config, names, vendor, features)
    text = pathlib.Path(config_path).read_bytes()
    rustc = environ.get("RUSTC") or "rustc"
    version = subprocess.run([rustc, "-vV"], capture_output=True, text=True).stdout if shutil.which(rustc) else ""
    report = {
        "schema": "link-preflight/1",
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(), "profile": profile,
        "config": {"path": str(config_path), "sha256": hashlib.sha256(text).hexdigest()},
        "rustc": version.strip(), "rustflags": rustflags(environ),
        "arms": [], "ok": True,
    }
    with tempfile.TemporaryDirectory(prefix="link-preflight-") as work:
        for name, arm in arms.items():
            record = probe_arm(name, arm, environ, work, extra_loader_paths)
            report["arms"].append(record)
            report["ok"] = report["ok"] and record["ok"]
    report["text"] = format_report(report, label)
    return report


def format_report(report, label=""):
    lines = [f"link preflight on {report['host']}"
             + (f" ({report['profile']})" if report.get("profile") else "")
             + f": {report['config']['path']}"]
    for record in report["arms"]:
        lines += ["  " + line for line in format_record(record, label)]
    if not report["arms"]:
        lines.append("  (no arm applies)")
    lines.append("LINK PREFLIGHT OK" if report["ok"] else
                 "LINK PREFLIGHT FAILED: a required library cannot be linked or loaded; "
                 "nothing may be built or measured until the fix lines above are applied.")
    return "\n".join(lines)


def for_run(root, vendor=None, features=(), loader_paths=(), environ=None):
    """The recorder's hook: the report for this run's arms, or None when the campaign declares none."""
    path = pathlib.Path(root) / CONFIG
    if not path.is_file():
        return None
    return run(path, vendor=vendor, features=list(features), environ=environ, extra_loader_paths=loader_paths)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help=f"the declaration (default: $CAMPAIGN_ROOT/{CONFIG}, else ./{CONFIG})")
    ap.add_argument("--arm", action="append", help="check only this arm (repeatable); default: every arm")
    ap.add_argument("--profile", help="the measurement profile this host serves; recorded in the report")
    ap.add_argument("--json", help="write the report as JSON here (the evidence a campaign keeps)")
    ap.add_argument("--log", help="write the text report here as well as to stdout")
    ap.add_argument("--validate", action="store_true",
                    help="check the declaration only (CI without the vendor libraries); link nothing")
    args = ap.parse_args(argv)
    config = args.config or str(pathlib.Path(os.environ.get("CAMPAIGN_ROOT", ".")) / CONFIG)
    try:
        if args.validate:
            arms = selected_arms(load_config(config), args.arm)
            print(f"{config}: {len(arms)} arm(s) declared: {', '.join(sorted(arms))}")
            return 0
        report = run(config, names=args.arm, profile=args.profile)
    except ConfigError as e:
        print(f"ERROR: {e.message}", file=sys.stderr)
        return 2
    print(report["text"])
    if args.log:
        pathlib.Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.log).write_text(report["text"] + "\n")
    if args.json:
        pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
