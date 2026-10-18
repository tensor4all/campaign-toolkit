import csv
import json
import pathlib
import tempfile
import unittest

import yaml

from campaign_toolkit import candidate

FIELDS = ['regime', 'family', 'dtype', 'order', 'm', 'n', 'k', 'batch', 'threads', 'status', 'row',
          'note', 'samples_ms', 'total_ms', 'per_item_us']


def make_run(root, name, library='base', power=None, fork_ms=10.0, control_ms=20.0,
             cells=(('lu', 'f64', ''),), control_skips=(), aa=2, regime='', batch=1):
    """One complete run: `aa` result files, every declared cell recorded in every file."""
    directory = pathlib.Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        'suite_id': 's', 'suite_sha256': 'x',
        'library': {'commit': library}, 'upstream': {'commit': 'up'},
        'harness': {'commit': 'h'}, 'toolkit': {'commit': 't'},
        'host': {'cpu': 'c', 'logical_cpus': 8, 'l3': 'l', 'l3_domains': 'd', 'hostname': 'n',
                 'arch': 'x', 'power': power or {'cpufreq_boost': '0'}},
        'timing_policy': {'repetitions': 2}, 'threads': {'counts': [1], 'cpu_sets': {'1': '4'}},
        'run_spec': {'aa': aa},
        'result_files': [f'run{repeat}-s-1t.csv' for repeat in range(aa)],
    }
    (directory / 'run.yaml').write_text(yaml.safe_dump(manifest))
    for repeat in range(aa):
        rows = []
        for engine, value in (('faer-fork', fork_ms), ('faer-upstream', control_ms)):
            for family, dtype, note in cells:
                skipped = engine == 'faer-upstream' and family in control_skips
                rows.append({
                    'regime': regime, 'family': family, 'dtype': dtype, 'order': 'c',
                    'm': '32', 'n': '32', 'k': '', 'batch': str(batch), 'threads': '1',
                    'status': 'skipped' if skipped else 'ok', 'row': engine, 'note': note,
                    'samples_ms': '' if skipped else json.dumps([value, value * 1.001]),
                    'total_ms': '' if skipped else str(value),
                    'per_item_us': '' if skipped else str(value * 1000 / batch),
                })
        with (directory / f'run{repeat}-s-1t.csv').open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return directory


def rewrite(path, mutate):
    """Rewrite one recorded CSV through `mutate`; the fixture is never the production evidence."""
    with path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(mutate(rows))


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_identity_differences(self):
        baseline = make_run(self.root, 'baseline')
        self.assertEqual(candidate.identity_differences(baseline, make_run(self.root, 'same')), [])
        candidate_run = make_run(self.root, 'newer-fork', library='candidate')
        self.assertEqual(candidate.identity_differences(baseline, candidate_run), [],
                         'only the measured library commit may differ')
        bad_power = make_run(self.root, 'boost', power={'cpufreq_boost': '1'})
        self.assertIn('power state', ' '.join(candidate.identity_differences(baseline, bad_power)))
        changed_host = make_run(self.root, 'host')
        manifest = yaml.safe_load((changed_host / 'run.yaml').read_text())
        manifest['host']['l3_domains'] = 'other'
        (changed_host / 'run.yaml').write_text(yaml.safe_dump(manifest))
        self.assertIn('host l3_domains', ' '.join(candidate.identity_differences(baseline, changed_host)))

    def test_control_check_passes_on_the_same_run_and_fails_on_a_moved_machine(self):
        baseline = make_run(self.root, 'baseline')
        ok, rows = candidate.control_check(baseline, baseline, 'faer-upstream')
        self.assertTrue(ok)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['ratio'], 1.0, places=12)
        moved = make_run(self.root, 'moved', control_ms=30.0)
        ok, rows = candidate.control_check(baseline, moved, 'faer-upstream')
        self.assertFalse(ok, 'a control row 50% off must refuse the comparison')
        self.assertTrue(rows[0]['outside_baseline_range'])

    def test_unlisted_rejected_csv_cannot_change_candidate_samples(self):
        baseline = make_run(self.root, 'baseline')
        before = candidate.series(baseline, 'faer-fork', 2)
        rejected = make_run(self.root, 'rejected', fork_ms=1e9, control_ms=1e9)
        archive = baseline / 'run0-s-1t.csv.rejected.fixture'
        archive.mkdir()
        (archive / 'result.csv').write_text((rejected / 'run0-s-1t.csv').read_text())
        self.assertEqual(candidate.series(baseline, 'faer-fork', 2), before)
        self.assertTrue(candidate.control_check(baseline, baseline, 'faer-upstream')[0])

    def test_fork_comparison_reports_the_rotation_averaged_ratio(self):
        baseline = make_run(self.root, 'baseline')
        faster = make_run(self.root, 'faster', library='candidate', fork_ms=5.0)
        rows = candidate.fork_comparison(baseline, faster, 'faer-fork')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['cell'][3], 'c', 'the memory order must stay part of the cell key')
        self.assertAlmostEqual(rows[0]['rotation_averaged_ratio'], 0.5, places=9)
        self.assertFalse(candidate.identity_differences(baseline, faster))

    def test_a_consistent_control_skip_is_legitimate_and_a_selection_keeps_it(self):
        cells = (('lu', 'f64', ''), ('gemm', 'f64', ''))
        baseline = make_run(self.root, 'baseline', cells=cells, control_skips=('lu',))
        shifted = make_run(self.root, 'shifted', library='candidate', cells=cells,
                           control_skips=('lu',), fork_ms=5.0)
        ok, rows = candidate.control_check(baseline, shifted, 'faer-upstream', ['lu', 'gemm'])
        self.assertTrue(ok)
        self.assertEqual([row['cell'][1] for row in rows], ['gemm'],
                         'a skipped control cell is recorded, not timed, and the rest still compares')
        rows = candidate.fork_comparison(baseline, shifted, 'faer-fork', ['lu', 'gemm'])
        self.assertEqual([row['cell'][1] for row in rows], ['gemm', 'lu'])
        self.assertTrue(candidate.control_check(baseline, shifted, 'faer-upstream')[0],
                        'no selection means the whole recorded set')
        with self.assertRaisesRegex(ValueError, 'timed no cell'):
            candidate.control_check(baseline, shifted, 'faer-upstream', ['lu'])

    def test_an_explicit_subset_compares_only_itself(self):
        cells = (('lu', 'f64', ''), ('gemm', 'f64', ''))
        baseline = make_run(self.root, 'baseline', cells=cells)
        shifted = make_run(self.root, 'shifted', library='candidate', cells=cells, fork_ms=5.0)
        ok, rows = candidate.control_check(baseline, shifted, 'faer-upstream', ['gemm'])
        self.assertTrue(ok)
        self.assertEqual([row['cell'][1] for row in rows], ['gemm'])
        self.assertEqual([row['cell'][1] for row in
                          candidate.fork_comparison(baseline, shifted, 'faer-fork', ['gemm'])],
                         ['gemm'])

    def test_a_missing_or_unmatched_selection_is_refused(self):
        cells = (('lu', 'f64', ''), ('gemm', 'f64', ''))
        baseline = make_run(self.root, 'baseline', cells=cells)
        shifted = make_run(self.root, 'shifted', library='candidate', cells=cells)
        for selection in (['gemm', '__no_declared_cell__'], ['__no_declared_cell__']):
            with self.assertRaisesRegex(ValueError, 'recorded no .* matching'):
                candidate.control_check(baseline, shifted, 'faer-upstream', selection)
        with self.assertRaisesRegex(ValueError, 'empty token'):
            candidate.fork_comparison(baseline, shifted, 'faer-fork', ['gemm', ''])
        with self.assertRaisesRegex(ValueError, 'empty token'):
            candidate.fork_comparison(baseline, shifted, 'faer-fork', [''])

    def test_a_cell_missing_from_one_run_is_refused_not_intersected(self):
        partial = make_run(self.root, 'partial', library='candidate', cells=(('lu', 'f64', ''),))
        baseline = make_run(self.root, 'baseline',
                            cells=(('lu', 'f64', ''), ('gemm', 'f64', '')))
        with self.assertRaisesRegex(ValueError, 'did not record the same selected cells'):
            candidate.fork_comparison(baseline, partial, 'faer-fork')
        with self.assertRaisesRegex(ValueError, 'did not record the same selected cells'):
            candidate.control_check(baseline, partial, 'faer-upstream')

    def test_missing_skipped_cells_and_duplicate_skips_are_not_silent(self):
        cells = (('lu', 'f64', ''), ('gemm', 'f64', ''))
        baseline = make_run(self.root, 'baseline', cells=cells, control_skips=('lu',))
        partial = make_run(self.root, 'partial', cells=(('gemm', 'f64', ''),))
        with self.assertRaisesRegex(ValueError, 'including skips'):
            candidate.control_check(baseline, partial, 'faer-upstream')
        self.assertTrue(candidate.control_check(baseline, partial, 'faer-upstream', ['gemm'])[0])
        rewrite(baseline / 'run0-s-1t.csv',
                lambda rows: rows + [next(row for row in rows if row['status'] == 'skipped')])
        with self.assertRaisesRegex(ValueError, 'recorded twice'):
            candidate.series(baseline, 'faer-upstream', 2)

    def test_short_and_mismatched_complete_sets_are_refused(self):
        baseline = make_run(self.root, 'baseline', aa=3)
        short = make_run(self.root, 'short', library='candidate', aa=3)
        (short / 'run2-s-1t.csv').unlink()
        manifest = yaml.safe_load((short / 'run.yaml').read_text())
        manifest['result_files'] = manifest['result_files'][:2]
        (short / 'run.yaml').write_text(yaml.safe_dump(manifest))
        with self.assertRaisesRegex(ValueError, 'declared complete-set repeats'):
            candidate.fork_comparison(baseline, short, 'faer-fork')
        fewer = make_run(self.root, 'fewer', library='candidate', aa=2)
        with self.assertRaisesRegex(ValueError, 'complete-set count'):
            candidate.fork_comparison(baseline, fewer, 'faer-fork')

    def test_an_engine_without_timed_cells_is_refused(self):
        baseline = make_run(self.root, 'baseline')
        with self.assertRaisesRegex(ValueError, 'timed no cell'):
            candidate.control_check(baseline, baseline, 'no-such-engine')
        all_skipped = make_run(self.root, 'all-skipped', control_skips=('lu',))
        with self.assertRaisesRegex(ValueError, 'timed no cell'):
            candidate.control_check(all_skipped, all_skipped, 'faer-upstream')

    def test_duplicate_projected_cell_or_repeat_is_refused(self):
        run = make_run(self.root, 'run')
        rewrite(run / 'run0-s-1t.csv', lambda rows: rows + [dict(rows[0])])
        with self.assertRaisesRegex(ValueError, 'recorded twice'):
            candidate.series(run, 'faer-fork', 2)
        aliased = make_run(self.root, 'aliased')
        rewrite(aliased / 'run0-s-1t.csv',
                lambda rows: rows + [dict(rows[0], regime='another-regime')])
        with self.assertRaisesRegex(ValueError, 'share the projected key'):
            candidate.series(aliased, 'faer-fork', 2)

    def test_different_regimes_or_batches_behind_one_key_are_refused(self):
        baseline = make_run(self.root, 'baseline', regime='one')
        for changed in ({'regime': 'two'}, {'batch': 2}):
            other = make_run(self.root, f'other-{list(changed)[0]}', library='candidate',
                             regime=changed.get('regime', 'one'), batch=changed.get('batch', 1))
            with self.assertRaisesRegex(ValueError, 'different recorded cells'):
                candidate.fork_comparison(baseline, other, 'faer-fork')

    def test_variant_notes_stay_distinct_cells(self):
        cells = (('lu', 'f64', ''), ('lu', 'f64', 'variant-a'))
        baseline = make_run(self.root, 'baseline', cells=cells)
        shifted = make_run(self.root, 'shifted', library='candidate', cells=cells, fork_ms=5.0)
        self.assertEqual(len(candidate.series(baseline, 'faer-fork', 2)), 2)
        rows = candidate.fork_comparison(baseline, shifted, 'faer-fork')
        self.assertEqual(sorted(row['cell'][5] for row in rows), ['', 'variant-a'])


if __name__ == '__main__':
    unittest.main()


class SameMeasuredCodeTest(unittest.TestCase):
    fork = pathlib.Path('/home/shinaoka/projects/tensor4all/faer')
    paths = ('faer/src', 'faer/Cargo.toml', 'Cargo.lock')

    def git(self, path, *args):
        import subprocess
        return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_docs_only_revision_is_the_same_code_and_an_older_one_is_not(self):
        if not (self.fork / '.git').exists():
            self.skipTest('fork checkout not present')
        commits = self.git(self.fork, 'log', '--format=%H', '--all', '-60').split()
        if len(commits) < 3:
            self.skipTest('not enough history')
        self.assertEqual(candidate.same_measured_code(self.fork, commits[0], commits[0], self.paths,
                                                      git=self.git), [])
        self.assertTrue(candidate.same_measured_code(self.fork, commits[0], commits[-1], self.paths,
                                                     git=self.git),
                        'two revisions 60 commits apart must differ in the measured paths')
