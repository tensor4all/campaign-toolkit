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


if __name__ == '__main__':
    unittest.main()
