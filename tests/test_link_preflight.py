"""The link preflight against two real shared libraries built here: it passes when both link and
load, and fails -- in seconds, naming the soname, the directories searched and the variable to
set -- when one is hidden. Needs `rustc` and `cc`, which every CI runner and measurement host has."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import link_preflight  # noqa: E402

HAVE_TOOLS = bool(shutil.which("rustc") and shutil.which("cc") and sys.platform.startswith("linux"))


def arm(**over):
    base = {"source": "fixture build.rs", "env": {"PF_ROOT": {"required": True}},
            "search": ["$PF_ROOT/lib"], "libs": ["pfa", "pfb"], "loader_paths": ["$PF_ROOT/lib"]}
    base.update(over)
    return base


@unittest.skipUnless(HAVE_TOOLS, "needs rustc and cc on Linux")
class LinkPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.lib = self.tmp / "prefix" / "lib"
        self.lib.mkdir(parents=True)
        for name in ("pfa", "pfb"):
            src = self.tmp / f"{name}.c"
            src.write_text(f"int {name}_marker(void) {{ return 1; }}\n")
            subprocess.run(["cc", "-shared", "-fPIC", f"-Wl,-soname,lib{name}.so", "-o",
                            str(self.lib / f"lib{name}.so"), str(src)], check=True)
        # The environment the build would see, without anything the host happens to export.
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("LIBRARY_PATH", "LD_LIBRARY_PATH", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")}
        self.env["PF_ROOT"] = str(self.lib.parent)

    def config(self, arms):
        path = self.tmp / "link-preflight.yaml"
        path.write_text(yaml.safe_dump({"arms": arms}))
        return path

    def run_main(self, path, *extra, env=None):
        out = io.StringIO()
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(env or self.env)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = link_preflight.main(["--config", str(path), *extra])
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, out.getvalue()

    def test_both_libraries_present_pass(self):
        report = link_preflight.run(self.config({"vendor": arm()}), environ=self.env)
        self.assertTrue(report["ok"], report["text"])
        self.assertIn("LINK PREFLIGHT OK", report["text"])

    def test_hidden_library_fails_at_link_and_names_it(self):
        (self.lib / "libpfb.so").rename(self.tmp / "libpfb.so.hidden")
        path = self.config({"vendor": arm()})
        start = time.monotonic()
        code, text = self.run_main(path, "--json", str(self.tmp / "out.json"))
        self.assertLess(time.monotonic() - start, 30, "the preflight must fail in seconds")
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL vendor", text)
        self.assertIn("the linker cannot find libpfb.so (-lpfb)", text)
        self.assertNotIn("libpfa.so", text, "only the hidden library is missing")
        self.assertIn(f"searched: {self.lib} ($PF_ROOT/lib)", text)
        self.assertIn("LIBRARY_PATH=(unset)", text)
        self.assertIn("fix:      export LIBRARY_PATH=", text)
        self.assertIn("LINK PREFLIGHT FAILED", text)
        report = json.loads((self.tmp / "out.json").read_text())
        self.assertFalse(report["ok"])
        (missing,) = report["arms"][0]["missing"]
        self.assertEqual((missing["stage"], missing["lib"], missing["soname"]), ("link", "pfb", "libpfb.so"))

    def test_exact_filename_library_is_named_as_linked(self):
        (self.lib / "libpfb.so").rename(self.tmp / "libpfb.so.hidden")
        report = link_preflight.run(self.config({"vendor": arm(libs=["pfa", ":libpfb.so"])}), environ=self.env)
        self.assertFalse(report["ok"])
        self.assertIn("the linker cannot find libpfb.so (-l:libpfb.so)", report["text"])

    def test_hint_directory_turns_into_the_exact_fix(self):
        elsewhere = self.tmp / "elsewhere" / "lib"
        elsewhere.mkdir(parents=True)
        (self.lib / "libpfb.so").rename(elsewhere / "libpfb.so")
        report = link_preflight.run(self.config({"vendor": arm(hints=[str(elsewhere)])}), environ=self.env)
        self.assertFalse(report["ok"])
        (missing,) = report["arms"][0]["missing"]
        self.assertEqual(missing["found"], [str(elsewhere / "libpfb.so")])
        self.assertEqual(missing["fix"], f"export LIBRARY_PATH={elsewhere}${{LIBRARY_PATH:+:$LIBRARY_PATH}}")

    def test_declared_fix_template_wins(self):
        (self.lib / "libpfb.so").rename(self.tmp / "libpfb.so")
        a = arm(hints=[str(self.tmp)], fix={"pfb": "export PFB_LIB={path}"})
        report = link_preflight.run(self.config({"vendor": a}), environ=self.env)
        self.assertEqual(report["arms"][0]["missing"][0]["fix"], f"export PFB_LIB={self.tmp / 'libpfb.so'}")

    def test_library_that_links_but_does_not_load_fails_at_load(self):
        # Linked from the search directory, but the runner arranges no loader path and the build
        # records no rpath: the harness would die with "error while loading shared libraries".
        report = link_preflight.run(self.config({"vendor": arm(loader_paths=[])}), environ=self.env)
        self.assertFalse(report["ok"])
        record = report["arms"][0]
        self.assertEqual(record["stage"], "load")
        self.assertEqual({m["soname"] for m in record["missing"]}, {"libpfa.so", "libpfb.so"})
        self.assertIn("the loader cannot open libpfa.so", report["text"])
        self.assertIn("export LD_LIBRARY_PATH=", report["text"])

    def test_rpath_arm_loads_without_a_loader_path(self):
        report = link_preflight.run(self.config({"vendor": arm(loader_paths=[], rpath=True)}), environ=self.env)
        self.assertTrue(report["ok"], report["text"])

    def test_required_variable_unset_is_named(self):
        env = dict(self.env)
        env.pop("PF_ROOT")
        a = arm(env={"PF_ROOT": {"required": True, "hint_glob": str(self.lib.parent)}})
        report = link_preflight.run(self.config({"vendor": a}), environ=env)
        self.assertFalse(report["ok"])
        self.assertIn("PF_ROOT is unset", report["text"])
        self.assertIn(f"export PF_ROOT={self.lib.parent}", report["text"])

    def test_required_variable_without_a_match_prints_the_declared_fix(self):
        a = {"env": {"PF_ROOT": {"required": True, "hint_glob": str(self.tmp / "nothing-*"),
                                 "fix": "build it with make-pf.sh, then export PF_ROOT=<prefix>"}}}
        _, problems, _ = link_preflight.resolve_env(a, {})
        self.assertEqual(problems[0]["fix"], "build it with make-pf.sh, then export PF_ROOT=<prefix>")

    def test_default_glob_resolves_the_newest_like_the_build_scripts(self):
        for version in ("2025.9", "2026.10", "2026.2"):
            (self.tmp / "vendor" / version).mkdir(parents=True)
        (self.tmp / "vendor" / "latest").symlink_to(self.tmp / "vendor" / "2025.9")
        values, problems, _ = link_preflight.resolve_env(
            {"env": {"X": {"required": True, "default_glob": str(self.tmp / "vendor" / "*")}}}, {})
        self.assertEqual(problems, [])
        self.assertEqual(values["X"], str(self.tmp / "vendor" / "2026.10"))

    def test_optional_search_entry_with_unset_variable_is_dropped(self):
        a = arm(search=["$PF_ROOT/lib", "$NOT_SET_HERE/lib"])
        report = link_preflight.run(self.config({"vendor": a}), environ=self.env)
        self.assertTrue(report["ok"], report["text"])
        self.assertEqual(report["arms"][0]["search"][1]["status"], "skipped: NOT_SET_HERE unset")

    def test_recorder_hook_selects_arms_by_vendor_and_feature(self):
        root = self.tmp / "campaign"
        (root / "benchmarks").mkdir(parents=True)
        self.assertIsNone(link_preflight.for_run(root, vendor="mkl"))
        (root / link_preflight.CONFIG).write_text(yaml.safe_dump({"arms": {
            "mkl": arm(when={"vendor": "mkl"}),
            "ext": arm(when={"feature": "ext"}, libs=["pfa"]),
            "never": arm(when={"vendor": "other"}, libs=["does_not_exist"]),
        }}))
        report = link_preflight.for_run(root, vendor="mkl", features=["ext"], environ=self.env)
        self.assertEqual(sorted(r["arm"] for r in report["arms"]), ["ext", "mkl"])
        self.assertTrue(report["ok"], report["text"])
        report = link_preflight.for_run(root, vendor="openblas", features=[], environ=self.env)
        self.assertEqual(report["arms"], [])

    def test_validate_checks_the_declaration_without_linking(self):
        code, text = self.run_main(self.config({"vendor": arm(libs=["no_such_library_anywhere"])}), "--validate")
        self.assertEqual(code, 0, text)
        self.assertIn("1 arm(s) declared: vendor", text)

    def test_prefix_placeholder_names_the_install_prefix(self):
        (self.lib / "libpfb.so").rename(self.tmp / "libpfb.so")
        moved = self.tmp / "other" / "lib"
        moved.mkdir(parents=True)
        (self.tmp / "libpfb.so").rename(moved / "libpfb.so")
        a = arm(hints=[str(moved)], fix={"pfb": "export PF_ROOT={prefix}"})
        report = link_preflight.run(self.config({"vendor": a}), environ=self.env)
        self.assertEqual(report["arms"][0]["missing"][0]["fix"], f"export PF_ROOT={moved.parent}")

    def test_unusable_declaration_exits_2(self):
        path = self.tmp / "bad.yaml"
        path.write_text(yaml.safe_dump({"arms": {"x": {"libs": ["a"], "serach": []}}}))
        code, _ = self.run_main(path)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
