"""Stack pins, the sync and status commands, hostinfo, the zen2 profile and the generalised pinning.

Nothing here measures, builds or reaches the network: sysfs is a fake tree, `gh` is a stub, and
`pinned.sh` is only ever run with `--dry-run`.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import declarations, descriptor, hostinfo, jsonschema_lite, stack_status  # noqa: E402
from campaign_toolkit import record_run, sync_pins  # noqa: E402
from campaign_toolkit import stack as stackmod  # noqa: E402

SCRIPTS = TOOLKIT / "benchmarks/scripts"
FIXTURE = TOOLKIT / "tests/fixture"
spec = importlib.util.spec_from_file_location("idle_cpus", SCRIPTS / "idle_cpus.py")
idle_cpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idle_cpus)


def schema(name):
    return json.loads((TOOLKIT / "schemas" / name).read_text())


def write(root, rel, text):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_sysfs(root, cpus, offset, l3_groups, nodes, boost="0", smt="on"):
    """A sysfs/proc tree: SMT sibling of n is n+offset, `l3_groups` are first-thread lists."""
    half = offset
    for cpu in range(cpus):
        base = f"sys/devices/system/cpu/cpu{cpu}"
        write(root, f"{base}/topology/thread_siblings_list", f"{cpu % half},{cpu % half + half}\n")
    for group in l3_groups:
        members = hostinfo.parse_cpu_list(group)
        shared = hostinfo.format_cpu_list(members + [m + half for m in members])
        for cpu in members + [m + half for m in members]:
            write(root, f"sys/devices/system/cpu/cpu{cpu}/cache/index3/shared_cpu_list", shared + "\n")
    write(root, "sys/devices/system/cpu/online", f"0-{cpus - 1}\n")
    write(root, "sys/devices/system/cpu/cpufreq/boost", f"{boost}\n")
    write(root, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "performance\n")
    write(root, "sys/devices/system/cpu/amd_pstate/status", "active\n")
    write(root, "sys/firmware/acpi/platform_profile", "performance\n")
    write(root, "sys/devices/system/cpu/smt/active", "1\n")
    write(root, "sys/devices/system/cpu/smt/control", f"{smt}\n")
    for node, cpulist in nodes.items():
        write(root, f"sys/devices/system/node/node{node}/cpulist", cpulist + "\n")
    write(root, "proc/cpuinfo", "processor : 0\nmodel name\t: Fake CPU 9000\n")


ZEN5 = dict(cpus=24, offset=12, l3_groups=["0-3", "4-11"], nodes={0: "0-23"})
ZEN2_NPS1 = dict(cpus=128, offset=64, l3_groups=[f"{4 * k}-{4 * k + 3}" for k in range(16)],
                 nodes={0: "0-63,64-127"})
ZEN2_NPS4 = dict(ZEN2_NPS1, nodes={0: "0-15,64-79", 1: "16-31,80-95", 2: "32-47,96-111", 3: "48-63,112-127"})


class HostinfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_zen5_shape(self):
        fake_sysfs(self.tmp, **ZEN5)
        got = hostinfo.collect(self.tmp)
        self.assertEqual(got["cpu"], "Fake CPU 9000")
        self.assertEqual(got["logical_cpus"], 24)
        self.assertEqual(got["power"], {"cpufreq_boost": "0", "amd_pstate_status": "active", "governor": "performance",
                                        "platform_profile": "performance",
                                        "smt": {"active": "1", "control": "on"}})
        self.assertEqual(got["numa"], {"nodes": 1, "cpulists": {"0": "0-23"}})
        self.assertEqual(got["smt_siblings"][:2], ["0,12", "1,13"])
        self.assertEqual(len(got["smt_siblings"]), 12)
        self.assertEqual(got["l3_domains"], ["0-3,12-15", "4-11,16-23"])

    def test_zen2_shape_with_one_and_four_numa_nodes(self):
        fake_sysfs(self.tmp / "a", **ZEN2_NPS1)
        fake_sysfs(self.tmp / "b", **ZEN2_NPS4)
        one, four = hostinfo.collect(self.tmp / "a"), hostinfo.collect(self.tmp / "b")
        self.assertEqual(one["logical_cpus"], 128)
        self.assertEqual(len(one["smt_siblings"]), 64)
        self.assertEqual(len(one["l3_domains"]), 16)
        self.assertEqual(one["l3_domains"][1], "4-7,68-71")
        self.assertEqual(one["numa"]["nodes"], 1)
        self.assertEqual(four["numa"]["nodes"], 4)
        self.assertEqual(four["numa"]["cpulists"]["3"], "48-63,112-127")

    def test_missing_files_are_null_not_errors(self):
        got = hostinfo.collect(self.tmp / "nothing")
        self.assertIsNone(got["power"])
        self.assertIsNone(got["numa"])
        self.assertIsNone(got["smt_siblings"])
        self.assertIsNone(got["l3_domains"])
        self.assertEqual(hostinfo.power_state(self.tmp / "nothing"), {})

    def test_power_keys_and_string_values_are_unchanged(self):
        fake_sysfs(self.tmp, **ZEN5, boost="0")
        power = hostinfo.power_state(self.tmp)
        self.assertEqual(power["cpufreq_boost"], "0")
        self.assertIsInstance(power["cpufreq_boost"], str)

    def test_toolchain_and_target_cpu(self):
        class Result:
            stdout = "rustc 1.99.0 (abc 2026-09-28)\nrelease: 1.99.0\n"
        run = lambda *a, **k: Result()  # noqa: E731
        got = hostinfo.toolchain({"RUSTFLAGS": "-C opt-level=3 -C target-cpu=znver2"}, run=run)
        self.assertEqual(got["rustc_release"], "1.99.0")
        self.assertEqual(got["target_cpu"], "znver2")
        self.assertIsNone(hostinfo.toolchain({}, run=run)["target_cpu"])
        self.assertEqual(hostinfo.target_cpu("-Ctarget-cpu=native -Ctarget-cpu=znver5"), "znver5")

        def missing(*a, **k):
            raise FileNotFoundError("rustc")
        self.assertIsNone(hostinfo.toolchain({}, run=missing)["rustc"])

    def test_record_run_host_block_carries_smt_and_numa(self):
        profiles = [{"name": "p", "cpu": "c", "logical_cpus": 1, "l3": "l"}]
        from unittest import mock
        with mock.patch.object(hostinfo, "power_state", return_value={"cpufreq_boost": "0", "smt": {"active": "1"}}), \
             mock.patch.object(hostinfo, "numa_layout", return_value={"nodes": 4, "cpulists": {}}):
            host = record_run.host_info("p", profiles)
        self.assertEqual(host["power"]["smt"], {"active": "1"})
        self.assertEqual(host["numa"]["nodes"], 4)

    def test_json_entry_point(self):
        fake_sysfs(self.tmp, **ZEN5)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(hostinfo.main(["--json", "--root", str(self.tmp)]), 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got["numa"]["nodes"], 1)
        self.assertIn("toolchain", got)


class RunSchemaTest(unittest.TestCase):
    """Old manifests keep validating; the new optional fields are accepted and typed."""

    def manifests(self):
        return sorted((FIXTURE / "data/results").glob("*/*/*/run.yaml"))

    def load(self, path):
        """The fixture's manifest with its template placeholders filled in."""
        return yaml.safe_load(path.read_text().replace("@SUITE_SHA@", "0" * 64).replace("@COMMIT@", "1" * 40))

    def test_old_manifests_validate(self):
        runs = self.manifests()
        self.assertTrue(runs)
        for path in runs:
            jsonschema_lite.validate(self.load(path), schema("benchmark-run.schema.json"))

    def test_new_fields_are_accepted(self):
        manifest = self.load(self.manifests()[0])
        manifest["host"].setdefault("power", {})
        manifest["host"]["power"] = dict(manifest["host"]["power"] or {}, cpufreq_boost="0",
                                         smt={"active": "1", "control": "on"})
        manifest["host"]["numa"] = {"nodes": 4, "cpulists": {"0": "0-15,64-79"}}
        manifest["build"] = {"toolchain": {"rustc": "rustc 1.99.0\n", "rustc_release": "1.99.0",
                                           "rustflags": None, "target_cpu": None}}
        jsonschema_lite.validate(manifest, schema("benchmark-run.schema.json"))
        manifest["host"]["numa"] = None
        jsonschema_lite.validate(manifest, schema("benchmark-run.schema.json"))

    def test_new_fields_are_typed(self):
        manifest = self.load(self.manifests()[0])
        manifest["host"]["numa"] = {"nodes": "four"}
        with self.assertRaises(jsonschema_lite.ValidationError):
            jsonschema_lite.validate(manifest, schema("benchmark-run.schema.json"))


class StackFilesTest(unittest.TestCase):
    def test_shipped_stack_is_valid_and_matches_the_root_copy(self):
        stack = stackmod.load()
        self.assertEqual(stackmod.validate(stack), [])
        self.assertEqual((TOOLKIT / "stack/stack-pins.yaml").read_bytes(),
                         (TOOLKIT / "campaign_toolkit/stack/stack-pins.yaml").read_bytes())

    def test_stack_declares_nine_repositories(self):
        names = [r["name"] for r in stackmod.load()["repositories"]]
        self.assertEqual(len(names), 9)
        self.assertIn("campaign-toolkit", names)

    def test_every_commit_is_a_full_sha_and_the_known_pins_are_present(self):
        libs = stackmod.load()["libraries"]
        commits = {k: v["commit"] for k, v in libs.items()}
        self.assertTrue(commits["t4a-faer"].startswith("0e15729"))
        self.assertTrue(commits["tlinalg"].startswith("88a2ffe"))
        self.assertTrue(commits["tcontract"].startswith("71a11a80"))
        self.assertTrue(commits["tcontract-v1"].startswith("8abfa38"))
        self.assertTrue(commits["cpueinsum"].startswith("938a25f"))
        self.assertTrue(commits["einsum-corpus"].startswith("753c0b3"))
        self.assertTrue(commits["tenferro"].startswith("8fec1ca"))
        self.assertTrue(commits["tensor4all-rs"].startswith("a515738"))
        self.assertEqual(libs["faer-upstream"]["tag"], "faer-v0.24.4")

    def test_a_dangling_library_reference_is_refused(self):
        stack = stackmod.load()
        stack["repositories"][0]["consumes"][0]["library"] = "nope"
        self.assertTrue(any("unknown library" in p for p in stackmod.validate(stack)))

    def test_a_short_commit_is_refused_by_the_schema(self):
        stack = stackmod.load()
        stack["libraries"]["tlinalg"]["commit"] = "88a2ffe"
        self.assertTrue(stackmod.validate(stack))


class SyncPinsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stack = stackmod.load()
        self.repo = self.tmp / "faer-benchmark"
        self.t4a = self.stack["libraries"]["t4a-faer"]["commit"]

    def run_sync(self, *args):
        out = io.StringIO()
        code = sync_pins.main([str(self.repo), *args], out=out)
        return code, out.getvalue()

    def matching(self):
        write(self.repo, "pins/t4a-faer.rev", f"# how it was found\n{self.t4a}\n")
        write(self.repo, "pins/upstream-faer.rev", "# tag\nfaer-v0.24.4\n")

    def test_matching_repository_passes_the_check(self):
        self.matching()
        code, out = self.run_sync("--check")
        self.assertEqual(code, 0, out)

    def test_a_differing_pin_fails_the_check_and_is_named(self):
        self.matching()
        write(self.repo, "pins/t4a-faer.rev", "# old\n" + "1" * 40 + "\n")
        code, out = self.run_sync("--check")
        self.assertEqual(code, 1)
        self.assertIn("pins/t4a-faer.rev", out)
        self.assertIn("1" * 40, out)
        self.assertEqual((self.repo / "pins/t4a-faer.rev").read_text(), "# old\n" + "1" * 40 + "\n")  # untouched

    def test_a_missing_file_fails_the_check(self):
        self.matching()
        (self.repo / "pins/upstream-faer.rev").unlink()
        code, out = self.run_sync("--check")
        self.assertEqual(code, 1)
        self.assertIn("pins/upstream-faer.rev: stack says faer-v0.24.4; missing file", out)

    def test_sync_writes_the_value_and_keeps_comments_and_missing_newline(self):
        write(self.repo, "pins/t4a-faer.rev", "# history\n# more\n" + "1" * 40)          # no trailing newline
        code, _ = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual((self.repo / "pins/t4a-faer.rev").read_text(), f"# history\n# more\n{self.t4a}")
        self.assertEqual((self.repo / "pins/upstream-faer.rev").read_text().splitlines()[-1], "faer-v0.24.4")
        self.assertTrue((self.repo / "pins/upstream-faer.rev").read_text().endswith("\n"))
        self.assertEqual(self.run_sync("--check")[0], 0)

    def test_unknown_repository_needs_repo_option(self):
        other = self.tmp / "somewhere"
        other.mkdir()
        self.assertEqual(sync_pins.main([str(other), "--check"], out=io.StringIO()), 2)
        write(other, "pins/tlinalg-rs.rev", self.stack["libraries"]["tlinalg"]["commit"] + "\n")
        self.assertEqual(sync_pins.main([str(other), "--check", "--repo", "tlinalg-benchmark"], out=io.StringIO()), 0)

    def test_workflow_and_cargo_pins_are_checked_not_written(self):
        tenferro = self.tmp / "tenferro-benchmark"
        commit = self.stack["libraries"]["tenferro"]["commit"]
        write(tenferro, ".github/workflows/ci.yml", f"env:\n  TENFERRO_REF: {commit}\n  TENFERRO_REF: {'2' * 40}\n")
        out = io.StringIO()
        self.assertEqual(sync_pins.main([str(tenferro)], out=out), 1)
        self.assertIn("ci.yml", out.getvalue())
        write(tenferro, ".github/workflows/ci.yml", f"env:\n  TENFERRO_REF: {commit}\n")
        self.assertEqual(sync_pins.main([str(tenferro), "--check"], out=io.StringIO()), 0)
        t4a = self.tmp / "tensor4all-benchmark"
        rs = self.stack["libraries"]["tensor4all-rs"]["commit"]
        write(t4a, "Cargo.toml", f'a = {{ git = "https://github.com/tensor4all/tensor4all-rs", rev = "{rs}" }}\n')
        self.assertEqual(sync_pins.main([str(t4a), "--check"], out=io.StringIO()), 0)
        write(t4a, "Cargo.toml", 'a = { git = "https://github.com/tensor4all/tensor4all-rs", rev = "abc" }\n')
        self.assertEqual(sync_pins.main([str(t4a), "--check"], out=io.StringIO()), 1)


class StackStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stack = stackmod.load()

    def table(self, gh):
        out = io.StringIO()
        self.assertEqual(stack_status.main(["--checkouts", str(self.tmp)], out=out, gh=gh), 0)
        return out.getvalue().splitlines()

    def test_one_row_per_repository_with_no_checkouts_and_gh_stubbed(self):
        lines = self.table(lambda name: json.dumps([{"conclusion": "success", "status": "completed",
                                                    "workflowName": "ci"}]))
        rows = lines[2:]
        self.assertEqual(len(rows), 9)
        for repo, row in zip(self.stack["repositories"], rows):
            self.assertTrue(row.startswith(f"| {repo['name']} |"), row)
        self.assertIn("no checkout", rows[0])
        self.assertIn("success (ci)", rows[0])

    def test_pins_runs_and_ci_cells_on_a_fixture_tree(self):
        repo = self.tmp / "tlinalg-benchmark"
        commit = self.stack["libraries"]["tlinalg"]["commit"]
        write(repo, "pins/tlinalg-rs.rev", commit + "\n")
        write(repo, "data/results/p/s/20261001T010101Z-a/run.yaml", "x")
        write(repo, "data/results/p/s/20261027T051928Z-b/run.yaml", "x")
        row = next(r for r in self.table(lambda n: "[]") if r.startswith("| tlinalg-benchmark"))
        self.assertIn("tlinalg-rs.rev 88a2ffe = stack", row)
        self.assertIn("20261027T051928 (data/results/p/s/20261027T051928Z-b)", row)
        self.assertIn("no runs", row)
        write(repo, "pins/tlinalg-rs.rev", "1" * 40 + "\n")
        row = next(r for r in self.table(lambda n: "[]") if r.startswith("| tlinalg-benchmark"))
        self.assertIn("1111111 != stack 88a2ffe", row)

    def test_gh_failures_degrade_per_cell(self):
        def missing(name):
            raise FileNotFoundError("gh")

        def failing(name):
            raise subprocess.CalledProcessError(1, "gh")
        self.assertTrue(all("gh unavailable" in r for r in self.table(missing)[2:]))
        self.assertTrue(all("gh error" in r for r in self.table(failing)[2:]))
        self.assertTrue(all("| no checkout |" in r for r in self.table(failing)[2:-1] if "campaign-toolkit" not in r))

    def test_in_progress_run_shows_its_status(self):
        rows = self.table(lambda n: json.dumps([{"conclusion": "", "status": "in_progress", "workflowName": "w"}]))
        self.assertIn("in_progress (w)", rows[2])


class ProfileTest(unittest.TestCase):
    def test_shared_profiles_pass_the_profile_check(self):
        for path in sorted((TOOLKIT / "campaign_toolkit/profiles").glob("*.yaml")):
            profile = yaml.safe_load(path.read_text())
            self.assertEqual(declarations.check_profile(profile), [], path.name)

    def test_declarations_pass_with_the_new_profiles_named(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "campaign"
        shutil.copytree(FIXTURE, root)
        profiles = yaml.safe_load((root / "benchmarks/profiles.yaml").read_text())
        profiles["profiles"] += ["zen5-cpu", "zen2-cpu"]
        (root / "benchmarks/profiles.yaml").write_text(yaml.safe_dump(profiles))
        out = io.StringIO()
        self.assertEqual(declarations.check(root, out), [], out.getvalue())

    def test_a_bad_row_is_refused(self):
        profile = yaml.safe_load((TOOLKIT / "campaign_toolkit/profiles/zen2-cpu.yaml").read_text())
        profile["thread_rows"][1]["observer"] = "4-63,64-127"             # observer on the measured siblings
        profile["thread_rows"][0]["measured"] = "0,64"                    # two threads of one core
        problems = "\n".join(declarations.check_profile(profile))
        self.assertIn("4T row", problems)
        self.assertIn("1T row", problems)

    def test_zen2_profile_declares_the_decided_rows(self):
        profile = descriptor.shared_profile("zen2-cpu")
        self.assertEqual(profile["role"], "measurement")
        self.assertEqual(profile["logical_cpus"], 128)
        self.assertTrue(profile["record_numa"])
        self.assertEqual([r["threads"] for r in profile["thread_rows"]], [1, 4, 8, 64])

    def test_zen5_role_is_untouched(self):
        self.assertEqual(descriptor.shared_profile("zen5-cpu")["role"], "development")


# The table in the workspace AGENTS.md (the measurement host): threads -> (measured, observer).
ZEN5_TABLE = {1: ("0", "4-11,16-23"), 4: ("0-3", "4-11,16-23"), 8: ("4-11", "0-3,12-15")}


class PinningTest(unittest.TestCase):
    def resolved(self, name, threads):
        res = idle_cpus.resolve(pathlib.Path("/nonexistent"), profile=descriptor.shared_profile(name), threads=threads)
        return {k: idle_cpus.format_cpu_list(v) if isinstance(v, list) else v for k, v in res.items()}

    def test_zen5_rows_reproduce_the_agents_table(self):
        for threads, (measured, observer) in ZEN5_TABLE.items():
            got = self.resolved("zen5-cpu", threads)
            self.assertEqual((got["measured"], got["observer"]), (measured, observer))

    def test_zen5_rows_equal_the_live_topology_derivation(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake_sysfs(tmp, **ZEN5)
        for threads, (measured, observer) in ZEN5_TABLE.items():
            res = idle_cpus.resolve(tmp, cpus=idle_cpus.parse_cpu_list(measured))
            self.assertEqual(idle_cpus.format_cpu_list(res["observer"]), observer)

    def test_zen2_rows(self):
        expect = {1: ("0", "0,64", "4-63,68-127"),
                  4: ("0-3", "0-3,64-67", "4-63,68-127"),
                  8: ("0-7", "0-7,64-71", "8-63,72-127"),
                  64: ("0-63", "0-127", "")}
        for threads, (measured, gate, observer) in expect.items():
            got = self.resolved("zen2-cpu", threads)
            self.assertEqual((got["measured"], got["idle_gate"], got["observer"]), (measured, gate, observer))

    def test_zen2_rows_equal_the_live_topology_derivation_on_128_cpus(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake_sysfs(tmp, **ZEN2_NPS1)
        for row in descriptor.shared_profile("zen2-cpu")["thread_rows"]:
            res = idle_cpus.resolve(tmp, cpus=idle_cpus.parse_cpu_list(row["measured"]))
            self.assertEqual(idle_cpus.format_cpu_list(res["observer"]), row["observer"], row["threads"])

    def test_the_idle_gate_reads_profile_siblings_and_refuses_a_different_live_layout(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake_sysfs(tmp, **ZEN5)                                              # siblings are n+12
        zen2 = descriptor.shared_profile("zen2-cpu")                        # claims n+64
        self.assertTrue(idle_cpus.profile_disagrees(tmp, zen2, [0, 1]))
        self.assertEqual(idle_cpus.profile_disagrees(tmp, descriptor.shared_profile("zen5-cpu"), [0, 1]), [])
        write(tmp, "proc/stat", "cpu 0 0 0 0 0 0 0 0\n" + "".join(f"cpu{i} 0 0 0 100 0 0 0 0\n" for i in range(24)))
        code = idle_cpus.main(["check", "0", "--profile", "zen2-cpu", "--seconds", "0"], root=tmp, sleep=lambda s: None)
        self.assertEqual(code, 4)
        code = idle_cpus.main(["check", "0", "--profile", "zen5-cpu", "--seconds", "0"], root=tmp, sleep=lambda s: None)
        self.assertEqual(code, 0)

    def dry_run(self, *args):
        return subprocess.run([str(SCRIPTS / "pinned.sh"), "--dry-run", *args], capture_output=True, text=True)

    def test_pinned_dry_run_for_each_zen2_row(self):
        expect = {"1": ("0", "0,64", "4-63,68-127"), "4": ("0-3", "0-3,64-67", "4-63,68-127"),
                  "8": ("0-7", "0-7,64-71", "8-63,72-127"), "64": ("0-63", "0-127", "(none on this host)")}
        for threads, (measured, gate, observer) in expect.items():
            r = self.dry_run("--profile", "zen2-cpu", "--threads", threads, "--", "./bench", "run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"taskset -c {measured} ./bench run", r.stdout)
            self.assertIn(f"idle gate:       {gate} ", r.stdout)
            self.assertIn(f"observer cpus:   {observer}", r.stdout)

    def test_pinned_dry_run_for_each_zen5_row(self):
        for threads, (measured, observer) in ZEN5_TABLE.items():
            r = self.dry_run("--profile", "zen5-cpu", "--threads", str(threads))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"taskset -c {measured} COMMAND", r.stdout)
            self.assertIn(f"observer cpus:   {observer}", r.stdout)

    def test_legacy_cpu_list_form_is_still_accepted(self):
        r = self.dry_run("0-3", "--", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("taskset -c 0-3 true", r.stdout)

    def test_pinned_without_cpus_or_with_an_unknown_option_is_refused(self):
        self.assertNotEqual(subprocess.run([str(SCRIPTS / "pinned.sh")], capture_output=True).returncode, 0)
        self.assertEqual(self.dry_run("--bogus").returncode, 64)


if __name__ == "__main__":
    unittest.main()
