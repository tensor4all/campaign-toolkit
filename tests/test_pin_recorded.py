"""The pin-recorded guard and the stack's `measured` hold. Nothing is built or measured."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import pin_recorded, stack_status, sync_pins  # noqa: E402
from campaign_toolkit import stack as stackmod  # noqa: E402

A, B, C = "a" * 40, "b" * 40, "c" * 40
FIX = "either record a publication at the pin, or keep the pin at the last measured revision"


def pub(name, time, commit):
    return dict(name=name, time=time, commits={"library": commit})


class CheckTest(unittest.TestCase):
    def pins(self, commit):
        return {"library": (f"pins/x.rev = {commit}", commit)}

    def test_newest_publication_at_the_pin_passes(self):
        pubs = [pub("old.md", 1, A), pub("new.md", 2, B)]
        self.assertEqual(pin_recorded.check(pubs, self.pins(B)), [])
        self.assertEqual(pin_recorded.check(pubs, self.pins(B[:12])), [])

    def test_a_pin_with_no_recording_fails_and_names_the_way_out(self):
        problems = pin_recorded.check([pub("old.md", 1, A), pub("new.md", 2, B)], self.pins(C))
        self.assertEqual(len(problems), 1)
        self.assertIn(f"pins/x.rev = {C}", problems[0])
        self.assertIn("no current recording", problems[0])
        self.assertIn("new.md", problems[0])
        self.assertIn(FIX + f" ({B[:12]})", problems[0])

    def test_a_pin_behind_the_newest_publication_fails(self):
        problems = pin_recorded.check([pub("old.md", 1, A), pub("new.md", 2, B)], self.pins(A))
        self.assertEqual(len(problems), 1)
        self.assertIn("newest publication new.md", problems[0])
        self.assertIn(FIX, problems[0])

    def test_nothing_recorded_fails(self):
        problems = pin_recorded.check([], self.pins(A))
        self.assertEqual(len(problems), 1)
        self.assertIn("no publication is recorded at all", problems[0])

    def test_an_unresolved_pin_fails(self):
        problems = pin_recorded.check([pub("p.md", 1, A)], {"library": ("pins/x.rev = v1", "")})
        self.assertIn("cannot be resolved", problems[0])

    def test_every_identity_must_match(self):
        p = dict(name="p.md", time=1, commits={"library": A, "upstream": B})
        pins = {"library": ("l", A), "upstream": ("u", C)}
        self.assertIn("no current recording", pin_recorded.check([p], pins)[0])
        self.assertEqual(pin_recorded.check([p], dict(pins, upstream=("u", B))), [])


class EvidenceModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "pins").mkdir()
        for name, commit, stamp in (("a", A, "2026-01-01T00:00:00Z"), ("b", B, "2026-01-02T00:00:00+00:00")):
            path = self.root / "result" / "p" / name / "s.summary.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"meta": {"commit": commit}, "timestamp": stamp}))

    def run_main(self, pin, *extra):
        (self.root / "pins/x.rev").write_text(f"# comment\n{pin}\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = pin_recorded.main(["--root", str(self.root), "--pin", "pins/x.rev", "--evidence",
                                      "result/*/*/*.summary.json", "--commit-field", "meta.commit", *extra])
        return code, err.getvalue()

    def test_pin_at_the_newest_evidence_passes(self):
        self.assertEqual(self.run_main(B), (0, ""))

    def test_pin_at_older_or_unrecorded_revision_fails(self):
        for pin, text in ((A, "newest publication"), (C, "no current recording")):
            with self.subTest(pin=pin):
                code, err = self.run_main(pin)
                self.assertEqual(code, 1)
                self.assertIn(text, err)
                self.assertIn(FIX, err)

    def test_evidence_without_a_commit_is_a_failure_not_skipped(self):
        bad = self.root / "result/p/c/s.summary.json"
        bad.parent.mkdir(parents=True)
        bad.write_text(json.dumps({"timestamp": "2026-01-03T00:00:00Z"}))
        code, err = self.run_main(B)
        self.assertEqual(code, 1)
        self.assertIn("has no usable `meta.commit`", err)

    def test_page_template_counts_only_published_runs(self):
        run = self.root / "data/prof/suite/20260105T000000Z/run.yaml"
        run.parent.mkdir(parents=True)
        run.write_text(f"target_profile: prof\nsuite_id: suite\ntimestamp: '2026-01-05T00:00:00Z'\nlib:\n  commit: {C}\n")
        args = ["--evidence", "data/*/*/*/run.yaml", "--commit-field", "lib.commit",
                "--page", "result/{target_profile}/{suite_id}/{commit:.12}.md"]

        def run_main(pin):
            (self.root / "pins/y.rev").write_text(pin + "\n")
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                code = pin_recorded.main(["--root", str(self.root), "--pin", "pins/y.rev", *args])
            return code, err.getvalue()

        # The run has no page: nothing is published, so there is no recording at all.
        self.assertIn("no publication is recorded at all", run_main(C)[1])
        page = self.root / f"result/prof/suite/{C[:12]}.md"
        page.parent.mkdir(parents=True)
        page.write_text("page\n")
        self.assertEqual(run_main(C)[0], 0)
        self.assertEqual(run_main(A)[0], 1)


class MeasuredHoldTest(unittest.TestCase):
    def setUp(self):
        self.stack = copy.deepcopy(stackmod.load())
        self.repo = stackmod.repository(self.stack, "tlinalg-benchmark")
        self.use = self.repo["consumes"][0]
        self.target = stackmod.target(self.stack, self.use)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "pins").mkdir()

    def hold(self, value=A, note="measured at A; the target is not recorded yet"):
        self.use["measured"] = value
        if note is not None:
            self.use["note"] = note

    def test_a_hold_replaces_the_expected_pin_and_is_printed(self):
        self.hold()
        self.assertEqual(stackmod.validate(self.stack), [])
        (self.root / self.use["file"]).write_text(A + "\n")
        out = io.StringIO()
        self.assertEqual(sync_pins.sync(self.stack, self.repo, self.root, True, out), 0)
        self.assertIn("held at the measured revision", out.getvalue())
        self.assertIn(stackmod.short(self.target), out.getvalue())
        self.assertIn("measured hold", stack_status.pin_cell(self.stack, self.repo, self.root))
        # The target itself no longer passes: the pin is the hold until the hold is dropped.
        (self.root / self.use["file"]).write_text(self.target + "\n")
        self.assertEqual(sync_pins.sync(self.stack, self.repo, self.root, True, io.StringIO()), 1)

    def test_a_hold_without_a_note_or_equal_to_the_target_is_refused(self):
        self.hold(note=None)
        self.assertTrue(any("carries a `note`" in p for p in stackmod.validate(self.stack)))
        self.hold(value=self.target)
        self.assertTrue(any("drop the hold" in p for p in stackmod.validate(self.stack)))

    def test_a_hold_on_a_tag_pin_or_a_short_sha_is_refused(self):
        faer = stackmod.repository(self.stack, "colmat-benchmark")
        tag_use = next(u for u in faer["consumes"] if u["field"] == "tag")
        tag_use.update(measured=A, note="n")
        self.assertTrue(any("not a tag pin" in p for p in stackmod.validate(self.stack)))
        tag_use.pop("measured")
        self.hold(value="abc1234")
        self.assertTrue(stackmod.validate(self.stack))

    def test_shipped_holds_are_the_measured_revisions(self):
        stack = stackmod.load()
        held = {u["file"]: u["measured"] for r in stack["repositories"] for u in r["consumes"]
                if "measured" in u}
        # No shipped hold: every pinned campaign has a publication at the
        # stack's commit (the three v2 campaigns were recorded at a045446).
        self.assertEqual(held, {})


if __name__ == "__main__":
    unittest.main()
