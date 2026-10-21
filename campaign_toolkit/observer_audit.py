"""Who, besides the measurement, is allowed to run on the measured cores.

`pinned.sh` decides after the fact whether a window was quiet, and the recorder refuses when
*it* is on the wrong side of the split. Neither names the process that spoiled the window, and
neither sees a task that was already busy before the run started, nor one the recorder never
spawned - which is what actually spoiled windows here: an unpinned `dockerd` worker at 13% on a
measured SMT sibling, and a leftover busy loop of the operator's. So the operator learned
"cpus 4-11 busy before run" and had to go and look.

This module asks the same question directly: sample every task on the machine, and report the
processes that consumed CPU while the affinity of any of their threads reached outside the CPUs
the observing side owns. A thread whose mask reaches a measured core may have run there, and
"may have" is not evidence.

Three limits, stated rather than papered over:

- `/proc` shows only the current PID namespace. Inside a container this audit sees the
  container and nothing else, so it warns when it is namespaced; the launcher must audit the
  host side in that case.
- A process owned by another user can be reported but not confined, and one that is busy while
  reaching a measured core makes a run unprovable rather than safe. It is reported as foreign.
- Sampling is a window. A task asleep for the whole window is not seen at all; catching that one
  waking up is `pinned.sh`'s idle gate, not this.
"""

import argparse
import collections
import os
import pathlib
import subprocess
import sys
import time

PF_KTHREAD = 0x00200000
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")

# Daemons a measurement never needs on a measured core. `--confine` moves them whether or not
# they were busy during the sample window: they are idle between builds and wake up during one,
# and the window only sees the ones that happened to be awake.
DAEMONS = ("dockerd", "containerd", "rootlesskit", "buildkitd", "kache")


def _snapshot(root):
    """`{task: (comm, flags, busy_ticks)}` for every task, and how many could not be read."""
    found, unreadable = {}, 0
    for task in (root / "").glob("[0-9]*/task/[0-9]*"):
        try:
            raw = (task / "stat").read_text()
        except FileNotFoundError:
            continue                        # it exited between enumeration and reading
        except OSError:
            unreadable += 1                 # hidepid, a locked /proc, ...
            continue
        try:
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            fields = raw[raw.rindex(")") + 2:].split()
            found[str(task)] = (comm, int(fields[6]), int(fields[11]) + int(fields[12]))
        except (ValueError, IndexError):
            unreadable += 1
    return found, unreadable


def _status_field(task, name):
    try:
        for line in (pathlib.Path(task) / "status").read_text().splitlines():
            if line.startswith(name):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def parse_cpu_list(spec):
    """`"0-3,12-15"` -> `[0, 1, 2, 3, 12, 13, 14, 15]`."""
    out = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-")
            out.extend(range(int(low), int(high) + 1))
        else:
            out.append(int(part))
    return out


def namespaced(root=pathlib.Path("/proc")):
    """True when this `/proc` is a nested PID namespace, so the audit cannot see the host."""
    nspid = _status_field(root / "self", "NSpid")
    return bool(nspid) and len(nspid.split()) > 1


def offenders(observer, seconds=2.0, share=0.05, root=pathlib.Path("/proc")):
    """Processes that used CPU while a thread's mask reached outside `observer`.

    Returns `(bad, foreign, uninspected)`: processes this user can confine and should, processes
    busy with the same property but owned by another user (reportable, not movable), and the
    number of tasks whose affinity could not be read. The test is per *process*, not per thread:
    two dozen workers at 0.8% each spoil a core while no single thread crosses any threshold.

    `share` defaults to the 5% of a core that `pinned.sh` itself treats as busy, so the audit
    reports what could actually move the idle gate and does not refuse a machine over a daemon
    that used 1% of one core.
    """
    observer = set(observer)
    first, unreadable = _snapshot(root)
    time.sleep(seconds)
    second, more_unreadable = _snapshot(root)
    unreadable += more_unreadable

    busy = collections.Counter()
    threads = collections.defaultdict(list)
    comm = {}
    for task, (name, flags, ticks) in second.items():
        if flags & PF_KTHREAD:
            continue
        pid = int(pathlib.Path(task).parts[-3])
        comm.setdefault(pid, name)
        # A task that did not exist in the first pass has spent all of its time in this window,
        # so its total is its share of it; ignoring it would hide a worker spawned mid-window.
        spent = ticks - first.get(task, (name, flags, 0))[2]
        if spent > 0:
            busy[pid] += spent
            threads[pid].append(task)
        elif task not in first:
            threads[pid].append(task)

    threshold = share * seconds * CLOCK_TICKS
    bad, foreign = [], []
    for pid, ticks in busy.items():
        if ticks < threshold:
            continue
        masks: dict[str, int] = {}
        unknown = 0
        for task in threads[pid]:
            allowed = _status_field(task, "Cpus_allowed_list")
            if allowed is None:
                unknown += 1
            else:
                masks[allowed] = masks.get(allowed, 0) + 1
        reaching = [m for m in masks if not set(parse_cpu_list(m)) <= observer]
        if not reaching:
            continue
        uid = _owner(pid, root)
        entry = {"pid": pid, "comm": comm[pid], "uid": uid,
                 "cores": round(ticks / CLOCK_TICKS / seconds, 2),
                 "tasks": len(threads[pid]),
                 "allowed": reaching[0] if reaching else sorted(masks)[0],
                 "unknown_tasks": unknown}
        (bad if uid == os.geteuid() else foreign).append(entry)
    return bad, foreign, unreadable


def _owner(pid, root=pathlib.Path("/proc")):
    """The real uid of a process, or None when it cannot be read."""
    uid = _status_field(pathlib.Path(root) / str(pid), "Uid")
    try:
        return int(uid.split()[0]) if uid else None
    except ValueError:
        return None


def _comm(pid):
    try:
        return pathlib.Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def _outside(pid, cpus):
    """How many of `pid`'s tasks are not provably confined to `cpus`."""
    want = set(parse_cpu_list(cpus))
    left = 0
    for task in pathlib.Path(f"/proc/{pid}/task").glob("[0-9]*"):
        allowed = _status_field(task, "Cpus_allowed_list")
        if allowed is None or not set(parse_cpu_list(allowed)) <= want:
            left += 1
    return left


def confine(cpus, entries=(), names=DAEMONS):
    """Move every task of `entries` and of each daemon in `names` onto `cpus`.

    `taskset -a` and not `-p`: a daemon's compiling threads are not its main thread, and moving
    the main one alone leaves the rest where they were. Only processes this user owns are
    touched, and the command name is re-checked immediately before the move so that a recycled
    pid cannot get an unrelated process confined.
    """
    targets = [(f"pid {e['pid']} ({e['comm']})", e["pid"], e["comm"]) for e in entries]
    for name in names:
        for pid in subprocess.run(["pgrep", "-x", name], capture_output=True,
                                  text=True).stdout.split():
            targets.append((name, int(pid), name))
    moved, failed, skipped = [], [], []
    for label, pid, expect in targets:
        if _owner(pid) != os.geteuid():
            skipped.append((label, pid))
            continue
        if _comm(pid) != expect:
            skipped.append((label, pid))
            continue
        subprocess.run(["taskset", "-apc", cpus, str(pid)], capture_output=True, text=True)
        left = _outside(pid, cpus)
        (failed if left else moved).append((label, pid, left))
    return moved, failed, skipped


def report(bad, foreign, unreadable, observer):
    if not bad and not foreign:
        print(f"observer-audit: nothing used CPU outside the observing side {observer}")
    if bad:
        print(f"observer-audit: {len(bad)} process(es) used CPU while a thread was allowed "
              f"outside {observer}", file=sys.stderr)
        print(f"  {'pid':>7s} {'tasks':>5s} {'core%':>6s}  {'command':22s} allowed", file=sys.stderr)
        for e in sorted(bad, key=lambda e: -e["cores"]):
            print(f"  {e['pid']:7d} {e['tasks']:5d} {100 * e['cores']:5.1f}%  "
                  f"{e['comm'][:22]:22s} {e['allowed']}", file=sys.stderr)
    if foreign:
        print(f"observer-audit: {len(foreign)} process(es) of another user were busy with the same "
              f"property and cannot be confined by this user; the run cannot be shown clean "
              f"while they are:", file=sys.stderr)
        for e in sorted(foreign, key=lambda e: -e["cores"]):
            print(f"  uid {e['uid']} pid {e['pid']} {e['cores'] * 100:.1f}% {e['comm']} "
                  f"{e['allowed']}", file=sys.stderr)
    if unreadable:
        print(f"observer-audit: {unreadable} task(s) could not be inspected at all (hidepid?); "
              f"whether they used a measured core is not known", file=sys.stderr)
    if namespaced():
        print("observer-audit: this /proc is a nested PID namespace, so only this namespace was "
              "audited; the host side must be audited separately", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--observer", required=True,
                    help="the CPUs the observing side owns, e.g. 0-3,12-15")
    ap.add_argument("--seconds", type=float, default=2.0, help="sample window (default 2)")
    ap.add_argument("--share", type=float, default=0.05,
                    help="fraction of a core over the window that counts as busy (default 0.05, "
                         "the fraction pinned.sh's idle gate calls busy)")
    ap.add_argument("--confine", action="store_true",
                    help="move the offenders and the known daemons onto --observer, then recheck")
    args = ap.parse_args(argv)

    observer = parse_cpu_list(args.observer)
    bad, foreign, unreadable = offenders(observer, args.seconds, args.share)
    report(bad, foreign, unreadable, args.observer)
    if not args.confine:
        return 1 if bad else 0

    moved, failed, skipped = confine(args.observer, bad)
    for label, pid, _ in moved:
        print(f"observer-audit: confined {label} to {args.observer}")
    for label, pid, left in failed:
        print(f"observer-audit: could not confine {label} ({pid}); {left} task(s) left outside "
              f"{args.observer}", file=sys.stderr)
    for label, pid in skipped:
        print(f"observer-audit: left {label} ({pid}) alone: not this user's process", file=sys.stderr)
    remaining, foreign, unreadable = offenders(observer, args.seconds, args.share)
    if remaining or failed:
        report(remaining, foreign, unreadable, args.observer)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
