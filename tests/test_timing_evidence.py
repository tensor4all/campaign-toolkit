import copy
import csv
import json
import pathlib
import shutil
import tempfile
import unittest

import yaml

from campaign_toolkit import campaign, compare_runs, descriptor, report_run, timing_evidence


class TimingEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.engines = ['candidate', 'baseline', 'vendor']
        self.manifest = {
            **{name: {'commit': 'abc', 'dirty': False}
               for name in ('library', 'harness', 'toolkit')},
            'binary': {'path': '/synthetic/not-executed', 'sha256': '0' * 64},
            'timing_policy': {'version': 2, 'statistic': 'median', 'repetitions': 2,
                              'row_rotation_step': 1},
            'run_spec': {'aa': 3, 'dtypes': ['f64'], 'order': ['c'],
                         'engines': self.engines,
                         'populations': [{'regime': 'small', 'families': ['mul'],
                                          'n': ['2'], 'batch': [1]}]},
            'threads': {'counts': [1]},
            'verification_files': ['verify-small-1t.csv'],
            'result_files': [f'run{i}-small-1t.csv' for i in range(3)],
        }
        census = []
        for engine in self.engines:
            for variant in ('block', 'sequence'):
                census.append(dict(zip(timing_evidence.BASE,
                              ('small', 'mul', 'f64', 'c', '2', '2', '', '1', engine, '1')))
                              | {'note': variant, 'status': 'ok', 'samples_ms': '',
                                 'total_ms': '', 'per_item_us': '',
                                 'rayon_threads': '1', 'vendor_threads': '1'})
        self.write('verify-small-1t.csv', census)
        for repeat, name in enumerate(self.manifest['result_files']):
            order = self.engines[repeat:] + self.engines[:repeat]
            rows = []
            for engine in order:
                for original in census:
                    if original['row'] == engine:
                        rows.append(original | {'samples_ms': json.dumps([1 + repeat, 3 + repeat]),
                                                'total_ms': str(2 + repeat),
                                                'per_item_us': str((2 + repeat) * 1000)})
            self.write(name, rows)

    def write(self, name, rows):
        with (self.root / name).open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def rows(self, name):
        with (self.root / name).open(newline='') as handle:
            return list(csv.DictReader(handle))

    def validate(self, manifest=None):
        timing_evidence.validate(manifest or self.manifest, self.root)

    def test_complete_process_sets_preserve_variants(self):
        self.validate()
        self.assertEqual(timing_evidence.samples(self.rows('run0-small-1t.csv')[0], 2), [1, 3])

    def test_v2_report_uses_process_medians_and_distinct_variants(self):
        # The fixture deliberately has no configured case_axes. Validated C/F
        # cells must still have separate report keys, tables and process SDs.
        self.manifest['run_spec']['order'].append('f')
        for name in self.manifest['verification_files'] + self.manifest['result_files']:
            rows = self.rows(name)
            other_order = []
            for row in rows:
                other = row | {'order': 'f'}
                if row['status'] == 'ok' and row['samples_ms']:
                    other |= {'samples_ms': json.dumps([v + 10 for v in json.loads(row['samples_ms'])]),
                              'total_ms': str(float(row['total_ms']) + 10),
                              'per_item_us': str(float(row['per_item_us']) + 10000)}
                other_order.append(other)
            self.write(name, rows + other_order)
        self.validate()
        fixture = pathlib.Path(__file__).parent / 'fixture'
        shutil.copytree(fixture / 'benchmarks', self.root / 'benchmarks')
        descriptor.set_root(self.root)
        source = next((fixture / 'data/results').glob('*/*/*/run.yaml'))
        manifest = yaml.safe_load(source.read_text())
        manifest.update({key: value for key, value in self.manifest.items()
                         if key not in ('library', 'harness', 'threads')})
        manifest['timing_policy']['minimum_untimed_priming_ms'] = 3000
        suite = yaml.safe_load((fixture / 'benchmarks/suites/fixture-kernels.yaml').read_text())
        rows = report_run.read_rows([self.root / name for name in self.manifest['result_files']])
        best, _, within, series = report_run.best_per_case(rows, manifest['timing_policy'])
        self.assertEqual(len(best), 4)  # Two variants, two orders.
        self.assertEqual(best[('small', 'mul/n2/b1 [block]', 'f64', 'c', 1)]['candidate'], [2, 3, 4])
        self.assertEqual(best[('small', 'mul/n2/b1 [block]', 'f64', 'f', 1)]['candidate'], [12, 13, 14])
        self.assertEqual(len(within[('small', 'mul/n2/b1 [block]', 'f64', 'c', 1)]['candidate']), 3)
        key = ('small', 'mul/n2/b1 [block]', 'f64', 'c', 1)
        ratios, detail = report_run.rotation_ratios(series, key, self.engines, 'candidate')
        self.assertEqual(ratios['baseline'], 1.0)      # Synthetic rows differ only by rotation.
        self.assertEqual(detail['baseline'], '1.000 1.000 1.000')
        balanced, between, position = report_run.cell_statistics(series, key, self.engines)
        self.assertTrue(balanced)                       # All three declared engines have rows.
        self.assertGreater(between, 0.05)               # Each engine moves 2/3/4 ms across starts.
        self.assertEqual(position, 1.0)                 # Identical engines carry no position signal.
        text = report_run.render(manifest, suite, rows, self.root)
        self.assertIn('3 ± 1', text)
        self.assertIn('13 ± 1', text)
        self.assertIn('vs candidate', text)
        self.assertIn('c order', text)
        self.assertIn('f order', text)
        self.assertIn('[block]', text)
        self.assertIn('[sequence]', text)
        self.assertNotIn('A comparison is made from paired repeats', text)
        self.assertEqual(report_run.report_axes(rows, {'version': 1}), ())
        orderless = [{k: v for k, v in row.items() if k != 'order'} for row in rows]
        self.assertEqual(report_run.report_axes(orderless, manifest['timing_policy']), ())
        self.assertEqual(campaign.page_name_values(manifest)['timing'], '-timing-v2')
        manifest['timing_policy']['version'] = 1
        self.assertEqual(campaign.page_name_values(manifest)['timing'], '')

    def test_single_engine_still_requires_two_processes(self):
        for name in self.manifest['verification_files'] + self.manifest['result_files']:
            self.write(name, [row for row in self.rows(name) if row['row'] == 'candidate'])
        manifest = copy.deepcopy(self.manifest)
        manifest['run_spec']['engines'] = ['candidate']
        manifest['run_spec']['aa'] = 1
        manifest['result_files'] = manifest['result_files'][:1]
        with self.assertRaisesRegex(ValueError, 'at least two complete process sets'):
            self.validate(manifest)
        manifest['run_spec']['aa'] = 2
        manifest['result_files'] = self.manifest['result_files'][:2]
        self.validate(manifest)

    def test_legacy_revision_comparison_refuses_v2(self):
        (self.root / 'run.yaml').write_text(yaml.safe_dump(self.manifest))
        with self.assertRaisesRegex(SystemExit, 'not yet supported'):
            compare_runs.read(self.root)

    def test_missing_repeat_and_missing_cell_are_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['result_files'].pop()
        with self.assertRaisesRegex(ValueError, 'missing complete-set'):
            self.validate(manifest)
        name = 'run0-small-1t.csv'
        self.write(name, self.rows(name)[:-1])
        with self.assertRaisesRegex(ValueError, 'missing, unexpected'):
            self.validate()

    def test_failed_check_and_duplicate_cell_are_rejected(self):
        name = 'run0-small-1t.csv'
        rows = self.rows(name)
        rows[0]['status'] = 'failed'
        self.write(name, rows)
        with self.assertRaisesRegex(ValueError, 'failed timed'):
            self.validate()
        rows[0]['status'] = 'ok'
        rows.append(rows[0])
        self.write(name, rows)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.validate()

    def test_samples_aggregate_and_thread_readback_are_checked(self):
        name = 'run0-small-1t.csv'
        original = self.rows(name)
        for key, value, error in [('samples_ms', '[NaN, 3]', 'finite'),
                                  ('samples_ms', '[1]', 'count'),
                                  ('total_ms', '1', 'aggregate'),
                                  ('per_item_us', '1', 'aggregate'),
                                  ('rayon_threads', '24', 'thread budgets')]:
            with self.subTest(key=key, value=value):
                rows = copy.deepcopy(original)
                rows[0][key] = value
                self.write(name, rows)
                with self.assertRaisesRegex(ValueError, error):
                    self.validate()

    def _rename_budget_column(self, new):
        for name in self.manifest['result_files'] + self.manifest['verification_files']:
            rows = [{(new if k == 'rayon_threads' else k): v for k, v in row.items()}
                    for row in self.rows(name)]
            self.write(name, rows)

    def test_pool_threads_column_is_the_pool_budget(self):
        """tlinalg 0.6.0 writes pool_threads; a wrong value is still refused."""
        self._rename_budget_column('pool_threads')
        self.validate()
        name = 'run0-small-1t.csv'
        rows = self.rows(name)
        rows[0]['pool_threads'] = '24'
        self.write(name, rows)
        with self.assertRaisesRegex(ValueError, 'thread budgets'):
            self.validate()

    def test_rayon_threads_column_still_accepted(self):
        """Historical runs and harnesses that still write rayon_threads (fbench, tcbench, cpueinsum)."""
        self.assertIn('rayon_threads', self.rows('run0-small-1t.csv')[0])
        self.validate()

    def test_schedule_dirty_source_and_wrong_grid_are_checked(self):
        name = 'run1-small-1t.csv'
        self.write(name, self.rows(name)[2:] + self.rows(name)[:2])
        with self.assertRaisesRegex(ValueError, 'engine order'):
            self.validate()
        manifest = copy.deepcopy(self.manifest)
        manifest['harness']['dirty'] = True
        with self.assertRaisesRegex(ValueError, 'clean committed harness'):
            self.validate(manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest['run_spec']['populations'][0]['n'].append('4')
        with self.assertRaisesRegex(ValueError, 'declared run grid'):
            self.validate(manifest)
