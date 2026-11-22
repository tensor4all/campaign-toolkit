"""Historical names: a campaign renames a suite and an engine without rewriting its recordings.

The fixture campaign is copied and renamed the way a real campaign would rename itself: the suite
`fixture-kernels` becomes `fixture-core` and the engine `algo-lane` becomes `algo-line`, with
`aliases:` in the descriptor. The recorded runs stay where and as they were recorded. Every reader
that looks a name up must still find the declaration and the provider, and every reader that
aggregates must put the old runs under the new names.
"""
import contextlib
import hashlib
import io
import pathlib
import shutil
import sys
import unittest

import yaml

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))
sys.path.insert(0, str(TOOLKIT / "tests"))

from campaign_toolkit import (  # noqa: E402
    campaign, candidate, declarations, descriptor, gen_index, publish_report, report_run, site, trend, validate_run,
)
import test_toolkit  # noqa: E402

RUN_A_REL = test_toolkit.RUN_A_REL


class AliasedCampaignTest(unittest.TestCase):
    # The fixture's own setUp (a copied campaign, a checkout its commits resolve in); its tests are
    # not inherited, because they assert the names this test renames.
    tearDown = test_toolkit.FixtureCampaignTest.tearDown

    def setUp(self):
        test_toolkit.FixtureCampaignTest.setUp(self)
        # Publish run A under the original names first, as the campaign did before its rename.
        run_a = self.root / RUN_A_REL
        self.assertEqual(report_run.main(["--run-dir", str(run_a)], root=self.root), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_report.main([str(run_a)], root=self.root), 0)
        self.report_before = (run_a / "report.md").read_bytes()
        self.rename()

    def rename(self):
        suites = self.root / "benchmarks/suites"
        old = suites / "fixture-kernels.yaml"
        text = old.read_text().replace("algo-lane", "algo-line")
        data = yaml.safe_load(text)
        assert data["id"] == "fixture-kernels"
        text = text.replace("id: fixture-kernels", "id: fixture-core", 1)
        (suites / "fixture-core.yaml").write_text(text)
        old.unlink()
        path = self.root / "benchmarks/campaign.yaml"
        desc = yaml.safe_load(path.read_text())
        desc["engines"] = {("algo-line" if k == "algo-lane" else k): v for k, v in desc["engines"].items()}
        desc["aliases"] = {"suites": {"fixture-kernels": "fixture-core"}, "engines": {"algo-lane": "algo-line"}}
        if "trend" in desc:
            del desc["trend"]
        path.write_text(yaml.safe_dump(desc, sort_keys=False))
        descriptor.set_root(self.root)

    def test_declarations_accept_the_aliases(self):
        self.assertEqual(declarations.check(self.root, out=io.StringIO()), [])

    def test_an_alias_to_nothing_is_refused(self):
        path = self.root / "benchmarks/campaign.yaml"
        desc = yaml.safe_load(path.read_text())
        desc["aliases"]["suites"]["gone"] = "no-such-suite"
        desc["aliases"]["engines"]["algo-pool"] = "algo-line"
        path.write_text(yaml.safe_dump(desc, sort_keys=False))
        problems = declarations.check(self.root, out=io.StringIO())
        self.assertTrue(any("no-such-suite" in p for p in problems), problems)
        self.assertTrue(any("'algo-pool' is both" in p for p in problems), problems)

    def test_a_suite_still_listing_the_historical_engine_is_accepted(self):
        suite = self.root / "benchmarks/suites/fixture-core.yaml"
        data = yaml.safe_load(suite.read_text())
        data["runs"][0]["engines"] = ["algo-lane" if e == "algo-line" else e for e in data["runs"][0]["engines"]]
        suite.write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertEqual(declarations.check(self.root, out=io.StringIO()), [])

    def test_a_chart_series_naming_the_historical_engine_draws(self):
        from campaign_toolkit import throughput
        # `load_run` keys cells by the current engine name; a series config written under the
        # historical name must draw that line, not an empty one.
        cells = {(1, "mul", 64, "", "algo-line"): {"run0": 1.0, "run1": 1.1}}
        line = throughput.points(cells, 1, "mul", "algo-line", 2.0)
        self.assertEqual(len(line), 1)
        self.assertEqual(throughput.points(cells, 1, "mul", "algo-lane", 2.0), line)

    def test_the_recorder_covers_a_suite_still_listing_the_historical_engine(self):
        from campaign_toolkit import record_run
        suite = yaml.safe_load((self.root / "benchmarks/suites/fixture-core.yaml").read_text())
        suite["runs"][0]["engines"] = ["algo-lane" if e == "algo-line" else e for e in suite["runs"][0]["engines"]]
        recorded = ["algo-line" if e == "algo-lane" else e for e in campaign.engines_for_vendor(suite, "openblas")]
        self.assertIn("algo-line", recorded)
        self.assertTrue(record_run.covers_declared_engines(recorded, suite, "openblas"))
        self.assertFalse(record_run.covers_declared_engines(recorded[:-1], suite, "openblas"))

    def test_trend_points_written_under_old_names_are_selected_and_drawn(self):
        points = [dict(suite="fixture-kernels", case_key="c", engine="algo-lane", threads=1)]
        entry = dict(suite="fixture-core", case="c", engines=["algo-line"], threads=[1])
        self.assertEqual(trend.select(points, entry), points)
        # and drawn: render indexes the series by the entry's (current) engine names
        run = (self.root / RUN_A_REL).resolve()
        old = [dict(p, suite="fixture-kernels", engine="algo-lane") for p in trend.points_from_run(run)]
        entry = dict(suite="fixture-core", case=old[0]["case_key"], engines=["algo-line"],
                     threads=[old[0]["threads"]])
        svg, data = trend.render(entry, old)[:2]
        self.assertIn("algo-line", svg)

    def test_a_baseline_under_old_engine_names_pairs_with_a_candidate_under_new_ones(self):
        import test_candidate
        old = test_candidate.make_run(self.root / "probe", "old")
        new = test_candidate.make_run(self.root / "probe", "new")
        for path in new.glob("*.csv"):
            test_candidate.rewrite(path, lambda rows: [
                {**r, "row": "algo-line" if r["row"] == "faer-fork" else r["row"]} for r in rows])
            test_candidate.rewrite(old / path.name, lambda rows: [
                {**r, "row": "algo-lane" if r["row"] == "faer-fork" else r["row"]} for r in rows])
        # The cell identities (and the timings) are one series under either engine name.
        self.assertEqual(candidate._series(old, "algo-lane", 2), candidate._series(new, "algo-line", 2))

    def test_the_file_name_fallback_lists_one_cell_per_suite(self):
        site_root = self.root / "result"
        old = next((site_root / "fixture-cpu/fixture-kernels").glob("*.md"))
        new_dir = site_root / "fixture-cpu/fixture-core"
        new_dir.mkdir(parents=True)
        shutil.copy(old, new_dir / old.name)
        pages, _missing = site.current_pages(site_root, [])
        self.assertEqual(len([p for p in pages if p.parent.parent.name == "fixture-cpu"]), 1)

    def test_lookups_accept_either_name(self):
        d = descriptor.current()
        self.assertEqual(d.suite_path("fixture-kernels").name, "fixture-core.yaml")
        self.assertEqual(campaign.find_suite("fixture-kernels")[1]["id"], "fixture-core")
        self.assertEqual(campaign.provider_of("algo-lane"), "algo")
        self.assertEqual(campaign.provider_of("algo-line"), "algo")
        self.assertEqual(d.suite_names("fixture-core"), ["fixture-core", "fixture-kernels"])

    def declare_names_only(self):
        """State that the rename changed only names: the old declaration hash is the new file's."""
        old = yaml.safe_load((self.root / RUN_A_REL / "run.yaml").read_text())["suite_sha256"]
        new = hashlib.sha256((self.root / "benchmarks/suites/fixture-core.yaml").read_bytes()).hexdigest()
        path = self.root / "benchmarks/campaign.yaml"
        desc = yaml.safe_load(path.read_text())
        desc["aliases"]["suite_sha256"] = {old: new}
        path.write_text(yaml.safe_dump(desc, sort_keys=False))
        descriptor.set_root(self.root)
        return old, new

    def test_a_names_only_rename_keeps_the_declaration_checks(self):
        manifest = str(self.root / RUN_A_REL / "run.yaml")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(validate_run.validate(manifest), [])
        self.assertIn("declaration-derived checks skipped", err.getvalue())   # without the entry
        old, new = self.declare_names_only()
        self.assertEqual(declarations.check(self.root, out=io.StringIO()), [])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(validate_run.validate(manifest), [])
        self.assertNotIn("declaration-derived checks skipped", err.getvalue())
        # The declaration checks really run: a thread count the suite does not declare is refused.
        data = yaml.safe_load(pathlib.Path(manifest).read_text())
        data["threads"]["counts"] = [99]
        pathlib.Path(manifest).write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertTrue(any("99" in e for e in validate_run.validate(manifest)))

    def test_the_entry_lapses_when_the_renamed_file_is_edited(self):
        self.declare_names_only()
        suite = self.root / "benchmarks/suites/fixture-core.yaml"
        suite.write_text(suite.read_text() + "\n# an edit after the rename\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            validate_run.validate(str(self.root / RUN_A_REL / "run.yaml"))
        self.assertIn("declaration-derived checks skipped", err.getvalue())

    def test_a_candidate_under_the_renamed_declaration_compares_with_an_old_baseline(self):
        old, new = self.declare_names_only()
        baseline = self.root / RUN_A_REL
        renamed = self.root / "data/results/fixture-cpu/fixture-core/20270101T000000Z"
        shutil.copytree(baseline, renamed)
        data = yaml.safe_load((renamed / "run.yaml").read_text())
        data.update(suite_id="fixture-core", suite_file="benchmarks/suites/fixture-core.yaml", suite_sha256=new)
        (renamed / "run.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        differences = candidate.identity_differences(baseline, renamed)
        self.assertNotIn("the suite declaration changed between the runs", differences)
        self.assertFalse(any(d.startswith("suite:") for d in differences), differences)

    def test_old_manifests_still_validate(self):
        for manifest in sorted((self.root / "data/results").rglob("run.yaml")):
            self.assertEqual(validate_run.validate(str(manifest)), [], f"{manifest}")

    def test_a_suite_file_naming_another_suite_is_still_refused(self):
        manifest = self.root / RUN_A_REL / "run.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["suite_file"] = "benchmarks/suites/fixture-external.yaml"
        manifest.write_text(yaml.safe_dump(data, sort_keys=False))
        errors = validate_run.validate(str(manifest))
        self.assertTrue(any("is not the declaration" in e for e in errors), errors)

    def test_the_old_report_regenerates_byte_for_byte(self):
        run_a = (self.root / RUN_A_REL).resolve()
        manifest = campaign.load(run_a / "run.yaml")
        suite = campaign.load(descriptor.current().suite_path(manifest["suite_id"]))
        rows = report_run.read_rows([run_a / f for f in manifest["result_files"]])
        # The record keeps the names it was recorded under; the documentation of the engine is
        # found under its current name, so the rows stay documented.
        text = report_run.render(manifest, suite, rows, run_a)
        self.assertIn("`algo-lane`", text)
        self.assertNotIn("(undocumented in the suite declaration)", text)
        self.assertEqual(text.encode(), self.report_before)

    def test_the_index_puts_the_old_page_under_the_new_suite(self):
        found = gen_index.collect()
        pages = [p for p in found["pages"] if p["page"]]
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["suite"], "fixture-core")
        self.assertEqual(pages[0]["page"].split("/")[2], "fixture-kernels")  # the file did not move
        self.assertNotEqual(pages[0]["coverage"], "declaration-changed")
        self.assertFalse(pages[0].get("retired"))
        current, _history = gen_index.split_pages(found["pages"], found["declared"], found["resolved"])
        self.assertTrue(any(c["suite"] == "fixture-core" and c["page"] for c in current))
        self.assertFalse(any(c["suite"] == "fixture-kernels" for c in current))

    def test_aggregating_readers_speak_the_current_names(self):
        run_a = self.root / RUN_A_REL
        _manifest, cells = trend.run_cells(run_a)
        engines = {engine for _, engine, _ in cells}
        self.assertIn("algo-line", engines)
        self.assertNotIn("algo-lane", engines)
        rows = [{"regime": "small", "family": "mul", "dtype": "f64", "m": "4", "n": "4", "k": "",
                 "batch": "1", "row": "algo-lane", "threads": "1"}]
        self.assertEqual(campaign.cell_of(rows[0]).row, "algo-line")
        points = trend.points_from_run(run_a)
        self.assertTrue(points and all(p["suite"] == "fixture-core" for p in points))

    def test_the_site_labels_the_cell_and_keys_the_old_record(self):
        out = site.write_site(self.root / "result", root=self.root, title="fixture")
        index = (self.root / "result/index.html").read_text()
        self.assertIn("fixture-cpu/fixture-core/", index)
        page = next((self.root / "result/fixture-cpu/fixture-kernels").glob("*.html"))
        text = page.read_text()
        self.assertIn("Recorded under historical names", text)
        self.assertIn("<code>algo-lane</code> is <code>algo-line</code>", text)
        self.assertTrue(out is None or out)


if __name__ == "__main__":
    unittest.main()
