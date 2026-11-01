import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from campaign_toolkit.provenance import ProvenanceError, check_revision, check_stack_revision


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q")
        git(self.checkout, "checkout", "-q", "-b", "main")
        self.base = self.commit("base")
        self.release = self.commit("release")
        git(self.checkout, "tag", "v1", self.release)
        self.tip = self.commit("after release")
        git(self.checkout, "checkout", "-q", "-b", "unmerged", self.base)
        self.diverged = self.commit("unmerged")
        git(self.checkout, "checkout", "-q", "main")

    def commit(self, message):
        git(self.checkout, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.checkout, "rev-parse", "HEAD")

    def test_ancestry_and_equality(self):
        for revision, on_main, in_release in (
            (self.base, True, True),
            (self.release, True, True),
            (self.tip, True, False),
            (self.diverged, False, False),
        ):
            with self.subTest(revision=revision):
                result = check_revision(self.checkout, revision, "refs/heads/main", self.release)
                self.assertIs(result.on_main, on_main)
                self.assertIs(result.in_release, in_release)

    def test_stack_release_is_used(self):
        with mock.patch('campaign_toolkit.provenance.stack.load', return_value={
                'libraries': {'fixture': {'commit': self.release, 'tag': 'v1'},
                              'unreleased': {'commit': self.tip, 'tag': None, 'version': None}}}):
            self.assertTrue(check_stack_revision(self.checkout, self.base, 'refs/heads/main',
                                                 'fixture').in_release)
            self.assertFalse(check_stack_revision(self.checkout, self.tip, 'refs/heads/main',
                                                  'fixture').in_release)
            with self.assertRaises(ProvenanceError):
                check_stack_revision(self.checkout, self.base, 'refs/heads/main', 'missing')
            with self.assertRaisesRegex(ProvenanceError, 'no release'):
                check_stack_revision(self.checkout, self.tip, 'refs/heads/main', 'unreleased')

    def test_unknown_revision_or_ref_fails_closed(self):
        for revision, main_ref, release_revision in (
            ("f" * 40, "refs/heads/main", self.release),
            (self.base, "refs/heads/missing", self.release),
            (self.base, "refs/heads/main", "f" * 40),
        ):
            with self.subTest(revision=revision, main_ref=main_ref,
                              release_revision=release_revision):
                with self.assertRaises(ProvenanceError):
                    check_revision(self.checkout, revision, main_ref, release_revision)

    def test_shallow_repository_fails_closed(self):
        shallow = self.root / "shallow"
        git(self.root, "clone", "-q", "--depth", "1", self.checkout.as_uri(), str(shallow))
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        with self.assertRaises(ProvenanceError):
            check_revision(shallow, self.tip, "refs/heads/main", self.release)


if __name__ == "__main__":
    unittest.main()
