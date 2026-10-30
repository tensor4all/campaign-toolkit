"""trend.py, check_generated.py and the site's figure hook, against the fixture campaign.

The fixture's two runs are given two library revisions and published, so the campaign has two
pins; a trend over them is one line. Conditions that must never share a line (timing contract,
power state, vendor arm) are exercised through trend points directly, the exchange format another
repository hands the toolkit.
"""
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import check_generated, descriptor, publish_report, report_run, site, trend  # noqa: E402

FIXTURE = TOOLKIT / 'tests/fixture'
RUNS = ['data/results/fixture-cpu/fixture-kernels/20260101T000000Z',
        'data/results/fixture-cpu/fixture-kernels/20260102T000000Z']
ENTRY = {'suite': 'fixture-kernels', 'case': 'small:mul/n8/b1 f64', 'engines': ['algo-lane', 'blas-openblas'],
         'threads': [1]}


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
               GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
    return subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def polylines(svg):
    return re.findall(r'<polyline points="([^"]+)"', svg)


def point(commit, timestamp, **changes):
    base = dict(campaign='c', suite='fixture-kernels', case_key='small:mul/n8/b1 f64', engine='algo-lane', threads=1,
                profile='zen5-cpu', library_version=None, library_commit=commit, timing_policy='v2',
                power='boost off, performance', vendor=None, median=1.0, spread=0.01, unit='ms',
                run_id=f'run-{commit}-{timestamp}', timestamp=timestamp)
    base.update(changes)
    return base


class TwoPinCampaign(unittest.TestCase):
    """The fixture with its two runs at two library revisions, both published."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / 'campaign'
        shutil.copytree(FIXTURE, self.root)
        checkout = self.root / 'extern/fixture-rs'
        checkout.mkdir(parents=True)
        git(checkout, 'init', '-q')
        revs = []
        for name in ('one', 'two'):
            (checkout / 'README').write_text(name + '\n')
            git(checkout, 'add', 'README')
            git(checkout, 'commit', '-q', '-m', name)
            revs.append(git(checkout, 'rev-parse', 'HEAD'))
        sha = hashlib.sha256((self.root / 'benchmarks/suites/fixture-kernels.yaml').read_bytes()).hexdigest()
        for path in self.root.rglob('*'):
            if path.suffix in ('.yaml', '.rev'):
                path.write_text(path.read_text().replace('@SUITE_SHA@', sha).replace('@COMMIT@', revs[1]))
        first = self.root / RUNS[0] / 'run.yaml'
        first.write_text(first.read_text().replace(revs[1], revs[0]))
        self.revs = revs
        descriptor.set_root(self.root)
        for run in RUNS:
            self.assertEqual(report_run.main(['--run-dir', str(self.root / run)], root=self.root), 0)
            self.assertEqual(publish_report.main([str(self.root / run)], root=self.root), 0)
        descriptor.set_root(self.root)


class TrendFromRunsTest(TwoPinCampaign):
    def test_the_published_runs_are_the_points_and_two_pins_make_one_line(self):
        runs = trend.published_runs()
        self.assertEqual([r.relative_to(self.root).as_posix() for r in runs], RUNS)
        points = [p for run in runs for p in trend.points_from_run(run)]
        chosen = trend.select(points, ENTRY)
        self.assertEqual({p['library_commit'] for p in chosen}, set(self.revs))
        self.assertEqual({p['power'] for p in chosen}, {'unrecorded'})
        self.assertEqual({p['timing_policy'] for p in chosen}, {'v1'})
        out = pathlib.Path(self.tmp.name) / 'out'
        self.assertEqual(trend.main(['--out', str(out)], root=self.root), 0)
        stem = 'trend-' + trend.slug(ENTRY)
        self.assertTrue(stem.startswith('trend-fixture-kernels-small-mul-n8-b1-f64-'))
        svg = (out / f'{stem}.svg').read_text()
        lines = polylines(svg)
        self.assertEqual(len(lines), 2)                      # one per engine, each across both pins
        self.assertTrue(all(len(line.split()) == 2 for line in lines))
        table = (out / f'{stem}.md').read_text()
        self.assertIn('| pin |', table)
        self.assertIn(self.revs[0][:12], table)

    def test_points_round_trip_through_the_schema(self):
        out = pathlib.Path(self.tmp.name) / 'points.json'
        self.assertEqual(trend.main(['--write-points', str(out)], root=self.root), 0)
        points = trend.read_points(out)
        self.assertTrue(points)
        document = json.loads(out.read_text())
        document['points'][0]['power'] = ''
        out.write_text(json.dumps(document))
        with self.assertRaises(Exception):
            trend.read_points(out)


class SiteFiguresTest(TwoPinCampaign):
    def test_the_site_generates_the_declared_charts_and_lists_them(self):
        out = site.write_site(self.root / 'result', root=self.root).read_text()
        figures = self.root / 'result/figures'
        stem = 'trend-' + trend.slug(ENTRY)
        self.assertTrue((figures / 'cases/cases-fixture-cpu-fixture-kernels-openblas.svg').exists())
        self.assertTrue((figures / f'trend/{stem}.svg').exists())
        self.assertTrue((figures / f'trend/{stem}-week.svg').exists())
        self.assertIn('Time per case', out)
        self.assertIn('Trend across pins', out)
        self.assertIn(f'src="figures/trend/{stem}.svg"', out)
        # a rebuild does not take the chart tables for result pages
        again = site.write_site(self.root / 'result', root=self.root).read_text()
        self.assertNotIn('figures/cases/cases-fixture-cpu-fixture-kernels-openblas.md"', again.split('Time per case')[0])

    def test_a_chart_of_an_earlier_build_is_never_shown_again(self):
        site.write_site(self.root / 'result', root=self.root)
        stale = self.root / 'result/figures/throughput/throughput-old-v2.svg'
        stale.parent.mkdir(parents=True)
        stale.write_text('<svg/>')
        out = site.write_site(self.root / 'result', root=self.root).read_text()
        self.assertFalse(stale.exists())
        self.assertNotIn('throughput-old-v2.svg', out)


class CheckGeneratedTest(TwoPinCampaign):
    def setUp(self):
        super().setUp()
        git(self.root, 'init', '-q')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'campaign')
        self.page = sorted((self.root / 'result/fixture-cpu/fixture-kernels').glob('*.md'))[0]
        self.source = trend.page_source(self.page)

    def check(self, *args):
        return check_generated.main(list(args), root=self.root)

    def flip(self, path, at=-2):
        data = bytearray(path.read_bytes())
        data[at] = ord('X') if data[at] != ord('X') else ord('Y')
        path.write_bytes(bytes(data))

    def test_the_published_campaign_passes(self):
        self.assertEqual(self.check('--all'), 0)

    def test_one_byte_in_a_page_fails(self):
        self.flip(self.page)
        self.assertEqual(self.check(), 1)

    def test_a_page_without_its_header_fails(self):
        self.page.write_text(self.page.read_text().split('\n', 2)[2])
        self.assertEqual(self.check(), 1)

    def test_one_byte_in_a_report_fails_when_its_run_changed(self):
        # The same byte in the report and its page: the page check passes, the regeneration does not.
        self.flip(self.source / 'report.md')
        self.flip(self.page)
        self.assertEqual(check_generated.check_pages(descriptor.current()), [])
        self.assertEqual(self.check('--base', 'HEAD'), 1)
        self.assertEqual([r.relative_to(self.root).as_posix()
                          for r in check_generated.changed_runs(descriptor.current(), 'HEAD')],
                         [self.source.relative_to(self.root).as_posix()])
        # an unchanged run is not regenerated: against a base that includes the edit, it passes
        git(self.root, 'commit', '-q', '-am', 'edit')
        self.assertEqual(self.check('--base', 'HEAD'), 0)

    def test_a_carriage_return_in_a_report_fails_the_page_check(self):
        report = self.source / 'report.md'
        data = report.read_bytes()
        at = data.index(b'\n')
        report.write_bytes(data[:at] + b'\r' + data[at:])
        self.assertTrue(check_generated.check_pages(descriptor.current()))

    def test_a_deleted_manifest_beside_its_report_fails(self):
        (self.source / 'run.yaml').unlink()
        self.assertTrue(check_generated.orphaned_runs(descriptor.current(), 'HEAD'))
        self.assertEqual(self.check('--base', 'HEAD', '--no-index'), 1)

    def test_one_byte_in_the_index_fails(self):
        self.flip(self.root / 'result/INDEX.md')
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.check('--no-index'), 0)


class TrendLinesTest(unittest.TestCase):
    def test_two_pins_under_one_condition_are_one_line(self):
        svg, _, _ = trend.render(ENTRY, [point('a' * 40, '2026-10-01T00:00:00Z', median=1.0),
                                         point('b' * 40, '2026-10-08T00:00:00Z', median=0.9)])
        self.assertEqual(len(polylines(svg)), 1)

    def test_v1_and_v2_are_never_on_one_line(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', timing_policy='v1'),
                  point('b' * 40, '2026-10-08T00:00:00Z', timing_policy='v1'),
                  point('b' * 40, '2026-10-09T00:00:00Z', timing_policy='v2'),
                  point('c' * 40, '2026-10-15T00:00:00Z', timing_policy='v2')]
        lines = trend.series(trend.select(points, ENTRY))
        policy = 2 + trend.CONDITIONS.index('timing_policy')
        self.assertEqual(sorted(k[policy] for k in lines), ['v1', 'v2'])
        for key, by_x in lines.items():
            self.assertEqual({p['timing_policy'] for p in by_x.values()}, {key[policy]})
        svg, table, _ = trend.render(ENTRY, points)
        self.assertEqual(len(polylines(svg)), 2)
        self.assertIn('timing v1', table)
        self.assertIn('timing v2', table)

    def test_power_states_and_vendor_arms_are_separate_lines(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', power='unrecorded'),
                  point('b' * 40, '2026-10-08T00:00:00Z'),
                  point('b' * 40, '2026-10-08T01:00:00Z', vendor='mkl'),
                  point('c' * 40, '2026-10-15T00:00:00Z', power='boost on, performance')]
        self.assertEqual(len(trend.series(points)), 4)
        self.assertEqual(polylines(trend.render(ENTRY, points)[0]), [])
        only_mkl = dict(ENTRY, vendor='mkl')
        self.assertEqual(len(trend.select(points, only_mkl)), 1)

    def test_the_same_pin_measured_twice_keeps_the_newest(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', median=2.0),
                  point('a' * 40, '2026-10-08T00:00:00Z', median=1.0)]
        (by_x,) = trend.series(points).values()
        self.assertEqual(by_x['a' * 40]['median'], 1.0)
        by_week = list(trend.series(points, 'week').values())[0]
        self.assertEqual(sorted(by_week), ['2026-W40', '2026-W41'])

    def test_pins_are_in_version_order_when_every_pin_has_one(self):
        points = [point('a' * 40, '2026-10-09T00:00:00Z', library_version='0.25.0'),
                  point('b' * 40, '2026-10-01T00:00:00Z', library_version='0.24.10'),
                  point('c' * 40, '2026-10-02T00:00:00Z', library_version='0.24.4')]
        self.assertEqual([label for _, label in trend.positions(points)], ['0.24.4', '0.24.10', '0.25.0'])
        unversioned = [dict(p, library_version=None) for p in points]
        self.assertEqual([x[0] for x, _ in trend.positions(unversioned)], ['b', 'c', 'a'])

    def test_a_version_shared_by_two_pins_names_the_commit(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', library_version='0.24.4'),
                  point('b' * 40, '2026-10-02T00:00:00Z', library_version='0.24.4')]
        self.assertEqual([label for _, label in trend.positions(points)], ['0.24.4 aaaaaaa', '0.24.4 bbbbbbb'])

    def test_every_recorded_power_control_is_a_condition(self):
        on = {'host': {'power': {'intel_no_turbo': '0', 'governor': 'performance'}}}
        off = {'host': {'power': {'intel_no_turbo': '1', 'governor': 'performance'}}}
        self.assertNotEqual(trend.power_of(on), trend.power_of(off))
        self.assertEqual(trend.power_of({'host': {}}), 'unrecorded')
        capped = {'host': {'power': {'cpufreq_boost': '0', 'governor': 'performance',
                                     'scaling_max_freq_khz': '2000000'}}}
        self.assertEqual(trend.power_of(capped), 'boost off, performance, max 2000000 kHz')

    def test_newest_is_by_instant_not_by_text(self):
        older = point('a' * 40, '2026-10-08T01:00:00+02:00', median=2.0)     # 2026-10-07T23:00Z
        newer = point('a' * 40, '2026-10-08T00:30:00Z', median=1.0)
        (by_x,) = trend.series([newer, older]).values()
        self.assertEqual(by_x['a' * 40]['median'], 1.0)

    def test_campaigns_are_separate_lines_and_mixed_units_are_refused(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z'), point('b' * 40, '2026-10-08T00:00:00Z', campaign='d')]
        self.assertEqual(len(trend.series(points)), 2)
        with self.assertRaisesRegex(ValueError, 'units'):
            trend.render(ENTRY, [point('a' * 40, '2026-10-01T00:00:00Z'),
                                 point('b' * 40, '2026-10-08T00:00:00Z', unit='s', median=0.001)])

    def test_a_zero_median_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'log axis'):
            trend.render(ENTRY, [point('a' * 40, '2026-10-01T00:00:00Z', median=0.0)])

    def test_entries_differing_only_in_vendor_write_different_files(self):
        self.assertNotEqual(trend.slug(dict(ENTRY, vendor='mkl')), trend.slug(dict(ENTRY, vendor='openblas')))

    def test_shared_conditions_go_under_the_title_and_the_legend_names_the_rest(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', timing_policy='v1'),
                  point('b' * 40, '2026-10-08T00:00:00Z')]
        svg, _, _ = trend.render(ENTRY, points)
        self.assertIn('class="subtitle">all lines: c, zen5-cpu, boost off, performance, no vendor arm<', svg)
        self.assertIn('>algo-lane (timing v2)<', svg)

    def test_the_chart_and_table_carry_the_same_numbers(self):
        points = [point('a' * 40, '2026-10-01T00:00:00Z', median=1.234),
                  point('b' * 40, '2026-10-08T00:00:00Z', median=0.987)]
        _, table, _ = trend.render(ENTRY, copy.deepcopy(points))
        self.assertIn('1.234 +- 0.01', table)
        self.assertIn('0.987 +- 0.01', table)


if __name__ == '__main__':
    unittest.main()
