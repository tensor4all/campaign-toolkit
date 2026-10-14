"""Self-test for the toolkit, against a synthetic fixture campaign in tests/fixture.

It never builds or measures anything: it exercises the content paths only --
descriptor validation, manifest validation, the report, publication and the
generated index, and the run comparison. A throwaway git repository stands in for
the measured checkout so the recorded commit resolves the way it does in a real
campaign.
"""
import contextlib
import hashlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import (  # noqa: E402
    compare_runs, descriptor, gen_index, publish_report, report_run, union_index,
    validate_run,
)

FIXTURE = TOOLKIT / "tests/fixture"
SUITE_REL = "benchmarks/suites/fixture-kernels.yaml"
RUN_A_REL = "data/results/fixture-cpu/fixture-kernels/20260101T000000Z"
RUN_B_REL = "data/results/fixture-cpu/fixture-kernels/20260102T000000Z"

# The toolkit must hold no campaign's specifics: everything comes from the
# descriptor. A campaign's name here would mean something leaked.
FORBIDDEN_IN_PACKAGE = ("tlinalg", "faer", "tlbench", "fbench")


def git(cwd, *args, **env):
    full = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid", **env)
    return subprocess.run(["git", *args], cwd=cwd, env=full, check=True,
                          capture_output=True, text=True).stdout.strip()


class FixtureCampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "campaign"
        shutil.copytree(FIXTURE, self.root)

        # A checkout the recorded commit can resolve in, standing in for the
        # measured project.
        self.checkout = self.root / "extern/fixture-rs"
        self.checkout.mkdir(parents=True)
        git(self.checkout, "init", "-q")
        (self.checkout / "README").write_text("fixture\n")
        git(self.checkout, "add", "README")
        git(self.checkout, "commit", "-q", "-m", "fixture")
        rev = git(self.checkout, "rev-parse", "HEAD")

        suite = self.root / SUITE_REL
        sha = hashlib.sha256(suite.read_bytes()).hexdigest()
        for path in self.root.rglob("*"):
            if path.suffix in (".yaml", ".rev"):
                text = path.read_text().replace("@COMMIT@", rev).replace("@SUITE_SHA@", sha)
                path.write_text(text)
        descriptor.set_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_descriptor_loads(self):
        d = descriptor.load_descriptor(self.root)
        self.assertEqual(d["id"], "fixture-campaign")
        self.assertEqual(d.identities, ["library"])
        self.assertEqual(d.engines["algo-lane"], "algo")
        self.assertEqual(d.vendor_providers, ("openblas",))
        self.assertEqual(d.measured_providers, ("algo",))
        self.assertEqual(len(d.csv_columns), 12)

    def test_descriptor_path_can_be_named(self):
        alt = self.root / "alt-campaign.yaml"
        shutil.copy(self.root / "benchmarks/campaign.yaml", alt)
        os.environ["CAMPAIGN_DESCRIPTOR"] = "alt-campaign.yaml"
        try:
            d = descriptor.load_descriptor(self.root)
        finally:
            del os.environ["CAMPAIGN_DESCRIPTOR"]
        self.assertEqual(d["id"], "fixture-campaign")
        self.assertEqual(d.path.resolve(), alt.resolve())

    def test_every_manifest_validates(self):
        manifests = sorted((self.root / "data/results").rglob("run.yaml"))
        self.assertEqual(len(manifests), 2)
        for manifest in manifests:
            self.assertEqual(validate_run.validate(str(manifest)), [], f"{manifest}")

    def test_report_publish_and_index(self):
        run_a = self.root / RUN_A_REL
        self.assertEqual(report_run.main(["--run-dir", str(run_a)], root=self.root), 0)
        self.assertTrue((run_a / "report.md").exists())

        self.assertEqual(publish_report.main([str(run_a)], root=self.root), 0)
        index = self.root / "result/INDEX.md"
        self.assertTrue(index.exists())
        self.assertIn("fixture-kernels", index.read_text())
        # The generator reproduces what publication just wrote, byte for byte.
        self.assertEqual(gen_index.main(["--check"], root=self.root), 0)

        # And rewriting it changes nothing.
        before = index.read_bytes()
        self.assertEqual(gen_index.main(["--write"], root=self.root), 0)
        self.assertEqual(index.read_bytes(), before)

    def test_index_reports_absent_checkout_only_when_something_is_recorded(self):
        # A campaign that has measured nothing has no currency to compute, so an absent
        # checkout is a note rather than a failure; once a page exists it is the reason
        # the index cannot say whether that page is current.
        root = pathlib.Path(self.tmp.name) / "empty"
        shutil.copytree(FIXTURE, root)
        shutil.rmtree(root / "extern", ignore_errors=True)
        shutil.rmtree(root / "data", ignore_errors=True)
        suite = root / SUITE_REL
        suite.write_text(suite.read_text().replace(
            "required_profiles:\n  - fixture-cpu\noptional_profiles: []",
            "required_profiles: []\noptional_profiles:\n  - fixture-cpu"))
        self.assertEqual(gen_index.main(["--write"], root=root), 0)
        self.assertEqual(gen_index.main(["--check"], root=root), 0)

    def test_dual_identity_index_names_both_revisions(self):
        # A campaign that records a baseline prints the fork identity in every line that
        # carries the revision identity, so two revisions cannot be read as one.
        import yaml as _yaml
        path = self.root / "benchmarks/campaign.yaml"
        data = _yaml.safe_load(path.read_text())
        data["identities"] = ["library", "upstream"]
        data["upstream"] = {
            "name": "fixture-upstream", "url": "https://example.invalid/upstream",
            "pin_file": "pins/fixture-rs.rev", "extern_dir": "extern/upstream",
            "pin_env": {"rev": "UP_REV", "dirty": "UP_DIRTY", "dir": "UP_DIR"},
        }
        data["page_name"] = "{library12}-{upstream12}{vendor}{dirty}"
        path.write_text(_yaml.safe_dump(data, sort_keys=False))
        descriptor.set_root(self.root)
        text = gen_index.render([], [], {"library": "a" * 40, "upstream": "b" * 40}, "none")
        self.assertIn("- Reference fork revision", text)
        self.assertIn("fork revision, upstream revision,", text)
        self.assertNotIn("library revision", text)

    def test_compare_runs(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = compare_runs.main([str(self.root / RUN_A_REL), str(self.root / RUN_B_REL)],
                                   root=self.root)
        self.assertEqual(rc, 0)
        out = buffer.getvalue()
        self.assertIn(" -> ", out)
        self.assertIn("- vendor arm: `openblas` on both sides", out)
        self.assertIn("## Per cell", out)
        self.assertIn("## Per family", out)

    def test_compare_refuses_two_vendor_arms(self):
        other = self.root / RUN_B_REL / "run.yaml"
        other.write_text(other.read_text().replace("name: openblas", "name: mkl", 1))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit):
            compare_runs.main([str(self.root / RUN_A_REL), str(self.root / RUN_B_REL)], root=self.root)

    def test_bad_descriptor_is_refused(self):
        bad = pathlib.Path(self.tmp.name) / "bad"
        shutil.copytree(FIXTURE, bad)
        text = (bad / "benchmarks/campaign.yaml").read_text()
        text = "\n".join(line for line in text.splitlines() if not line.startswith("page_name:"))
        (bad / "benchmarks/campaign.yaml").write_text(text)
        with self.assertRaises(SystemExit):
            descriptor.load_descriptor(bad)


    def test_profile_reference_resolves_and_is_used(self):
        # A campaign opts into a shared profile by naming it; the body comes from
        # the toolkit, so a corrected profile reaches every campaign that measures it.
        (self.root / "benchmarks/profiles.yaml").write_text(
            "schema_version: 1\nprofiles:\n  - zen5-cpu\n")
        d = descriptor.load_descriptor(self.root)
        self.assertEqual(d.profile_names(), ["zen5-cpu"])
        self.assertEqual(d.profiles()[0]["cpu"], "AMD Ryzen AI 9 HX 470")

    def test_union_index_over_one_and_two_campaigns(self):
        def publish(root):
            run_a = root / RUN_A_REL
            self.assertEqual(report_run.main(["--run-dir", str(run_a)], root=root), 0)
            self.assertEqual(publish_report.main([str(run_a)], root=root), 0)
            self.assertEqual(gen_index.main(["--write"], root=root), 0)

        publish(self.root)
        second = pathlib.Path(self.tmp.name) / "campaign2"
        shutil.copytree(self.root, second)
        campaign = second / "benchmarks/campaign.yaml"
        campaign.write_text(campaign.read_text().replace("fixture-campaign", "second-campaign"))
        publish(second)

        out = pathlib.Path(self.tmp.name) / "union.md"
        # A campaign root and an already-published INDEX.md are both inputs.
        rc = union_index.main([str(self.root), str(second / "result/INDEX.md"),
                               "--out", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text()
        self.assertIn("| campaign | suite | profile |", text)
        self.assertIn("| `fixture-campaign` | `fixture-kernels` |", text)
        self.assertIn("| `second-campaign` | `fixture-kernels` |", text)

        # One campaign is the same code path, not a special case.
        single = pathlib.Path(self.tmp.name) / "single.md"
        self.assertEqual(union_index.main([str(self.root), "--out", str(single)]), 0)
        text = single.read_text()
        self.assertIn("| `fixture-campaign` |", text)
        self.assertNotIn("second-campaign", text)


class ToolkitRepositoryTest(unittest.TestCase):
    def test_shared_profile_library(self):
        profile = descriptor.shared_profile("zen5-cpu")
        self.assertEqual(profile["name"], "zen5-cpu")
        self.assertEqual(profile["logical_cpus"], 24)
        self.assertTrue(descriptor.profiles_dir().is_dir())
        with self.assertRaises(SystemExit):
            descriptor.shared_profile("no-such-profile")

    def test_union_index_keeps_both_identities_of_a_dual_page(self):
        # A dual-identity campaign names a page by `fork/upstream`; two pages that
        # share the fork and the vendor differ only by upstream, so the whole string
        # is the identity and neither page may be dropped.
        root = pathlib.Path(tempfile.mkdtemp()) / "dual"
        index = root / "result/INDEX.md"
        index.parent.mkdir(parents=True)
        index.write_text(
            "# Result index\n\n"
            "- Reference fork revision (`pins/fork.rev`): `aaaaaaaaaaaa`\n"
            "- Reference upstream revision (`pins/up.rev`): `v1`\n"
            "- Newest recorded run: `2026-01-01`\n\n"
            "## All pages\n\n"
            "| suite | profile | vendor | fork/upstream | version | date | coverage | status | page |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| `kernels` | `cpu` | `mkl` | `aaaaaaaaaaaa/bbbbbbbbbbbb` | - | 2026-01-01 "
            "| full | current | [result/cpu/kernels/a-b-mkl.md](cpu/kernels/a-b-mkl.md) |\n"
            "| `kernels` | `cpu` | `mkl` | `aaaaaaaaaaaa/cccccccccccc` | - | 2026-01-01 "
            "| full | current | [result/cpu/kernels/a-c-mkl.md](cpu/kernels/a-c-mkl.md) |\n")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = union_index.main([str(index)])
        self.assertEqual(rc, 0)
        text = buffer.getvalue()
        self.assertIn("reference `aaaaaaaaaaaa`, upstream `v1`", text)
        self.assertIn("aaaaaaaaaaaa/bbbbbbbbbbbb", text)
        self.assertIn("aaaaaaaaaaaa/cccccccccccc", text)

    def test_package_holds_no_campaign_names(self):
        for path in sorted((TOOLKIT / "campaign_toolkit").rglob("*")):
            if path.suffix not in (".py", ".json"):
                continue
            text = path.read_text()
            for name in FORBIDDEN_IN_PACKAGE:
                self.assertNotIn(name, text, f"{path} mentions {name!r}")

    def test_top_level_schemas_match_the_packaged_copies(self):
        for packaged in sorted((TOOLKIT / "campaign_toolkit/schemas").glob("*.json")):
            top = TOOLKIT / "schemas" / packaged.name
            self.assertTrue(top.exists(), f"{top} is missing")
            self.assertEqual(top.read_bytes(), packaged.read_bytes(),
                             f"{top} has drifted from {packaged}")

    def test_schemas_are_valid_documents(self):
        from campaign_toolkit import jsonschema_lite
        for path in sorted((TOOLKIT / "campaign_toolkit/schemas").glob("*.json")):
            import json
            jsonschema_lite.check_schema(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
