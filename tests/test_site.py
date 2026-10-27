"""site.py: GitHub blob links are repository-root relative; remotes normalise to https."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import site  # noqa: E402


def _repo(tmp, remote):
    root = pathlib.Path(tmp) / 'repo'
    sub = root / 'result' / 'zen5-cpu' / 'k' / 'throughput'
    sub.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', remote], check=True)
    (sub / 'throughput-cholesky-v2.svg').write_text('<svg/>')
    (sub / 'throughput-cholesky-v2.md').write_text('| a |\n')
    page = root / 'result' / 'zen5-cpu' / 'k' / 'abc123456789-x.md'
    page.write_text('# page\n')
    return root


class BlobBase(unittest.TestCase):
    def test_remote_forms(self):
        want = 'https://github.com/org/repo'
        for remote in ('git@github.com:org/repo.git', 'git@github.com:org/repo',
                       'ssh://git@github.com/org/repo.git', 'https://github.com/org/repo.git',
                       'https://github.com/org/repo'):
            with tempfile.TemporaryDirectory() as tmp:
                root = _repo(tmp, remote)
                self.assertEqual(site._blob_base(root), want, remote)


class RepoRootLinks(unittest.TestCase):
    def test_links_relative_to_repo_root(self):
        for remote in ('git@github.com:org/repo.git', 'https://github.com/org/repo.git'):
            with tempfile.TemporaryDirectory() as tmp:
                root = _repo(tmp, remote)
                out = site.write_site(root / 'result').read_text()
                base = 'https://github.com/org/repo/blob/main/result/zen5-cpu/k'
                self.assertIn(f'href="{base}/throughput/throughput-cholesky-v2.md"', out)
                self.assertIn(f'href="{base}/abc123456789-x.md"', out)
                self.assertNotIn('github.com:', out)
                self.assertNotIn('blob/main/zen5-cpu', out)


NEW, OLD = 'bbbbbbbbbbbb', 'aaaaaaaaaaaa'


def _stamped(path, stamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'# page\n\n- timestamp: `{stamp}`\n')
    return path


def _campaign(tmp, pin):
    root = pathlib.Path(tmp) / 'camp'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin',
                    'git@github.com:org/camp.git'], check=True)
    (root / 'pins').mkdir()
    (root / 'pins' / 'lib.rev').write_text(f'# comment\n{pin}0000000000000000000000000000\n')
    return root, root / 'result' / 'zen5-cpu' / 'kern'


def _site(root):
    return site.write_site(root / 'result', pins=['pins/lib.rev']).read_text()


class LatestGeneration(unittest.TestCase):
    def test_two_pins_only_the_pinned_page_is_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, suite = _campaign(tmp, NEW)
            _stamped(suite / f'{OLD}-mkl-timing-v2.md', '2026-10-01T00:00:00Z')
            _stamped(suite / f'{NEW}-mkl-timing-v2.md', '2026-10-20T00:00:00Z')
            out = _site(root)
            self.assertIn(f'{NEW}-mkl-timing-v2.md"', out)
            self.assertNotIn(OLD, out)
            self.assertNotIn('not at the pinned revision', out)
            self.assertNotIn('superseded', out)

    def test_two_vendor_arms_at_the_pin_are_both_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, suite = _campaign(tmp, NEW)
            for arm in ('mkl', 'openblas'):
                _stamped(suite / f'{OLD}-{arm}-timing-v2.md', '2026-10-01T00:00:00Z')
                _stamped(suite / f'{NEW}-{arm}-timing-v2.md', '2026-10-20T00:00:00Z')
            out = _site(root)
            self.assertIn(f'{NEW}-mkl-timing-v2.md"', out)
            self.assertIn(f'{NEW}-openblas-timing-v2.md"', out)
            self.assertNotIn(OLD, out)

    def test_no_page_at_the_pin_is_named_not_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, suite = _campaign(tmp, NEW)
            _stamped(suite / f'{NEW}-mkl-timing-v2.md', '2026-10-20T00:00:00Z')
            other = root / 'result' / 'zen5-cpu' / 'other'
            _stamped(other / f'{OLD}-mkl-timing-v2.md', '2026-10-01T00:00:00Z')
            _stamped(other / 'cccccccccccc-openblas-timing-v2.md', '2026-10-05T00:00:00Z')
            out = _site(root)
            self.assertIn(f'kern/{NEW}-mkl-timing-v2.md"', out)
            self.assertNotIn('other/', out.replace('Not recorded at the current pin: other (mkl, openblas)', ''))
            self.assertNotIn(OLD, out)
            self.assertNotIn('cccccccccccc', out)
            self.assertIn('Not recorded at the current pin: other (mkl, openblas)', out)


if __name__ == '__main__':
    unittest.main()
