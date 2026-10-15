import csv
import json
import pathlib
import tempfile
import unittest

import yaml

from campaign_toolkit import candidate


def make_run(root, name, library='base', power=None, fork_ms=10.0, control_ms=20.0):
    directory = pathlib.Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        'suite_id': 's', 'suite_sha256': 'x',
        'library': {'commit': library}, 'upstream': {'commit': 'up'},
        'harness': {'commit': 'h'}, 'toolkit': {'commit': 't'},
        'host': {'cpu': 'c', 'logical_cpus': 8, 'l3': 'l', 'l3_domains': 'd', 'hostname': 'n',
                 'arch': 'x', 'power': power or {'cpufreq_boost': '0'}},
        'timing_policy': {'repetitions': 2}, 'threads': {'counts': [1], 'cpu_sets': {'1': '4'}},
        'run_spec': {'aa': 3}, 'result_files': ['run0-s-1t.csv', 'run1-s-1t.csv'],
    }
    (directory / 'run.yaml').write_text(yaml.safe_dump(manifest))
    for repeat in range(2):
        rows = []
        for engine, value in (('faer-fork', fork_ms), ('faer-upstream', control_ms)):
            rows.append({'family': 'lu', 'dtype': 'f64', 'order': 'c', 'm': '32', 'n': '32', 'k': '',
                         'threads': '1', 'status': 'ok', 'row': engine, 'note': '',
                         'samples_ms': json.dumps([value, value * 1.001]),
                         'total_ms': str(value), 'per_item_us': str(value * 1000)})
        with (directory / f'run{repeat}-s-1t.csv').open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return directory


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

    def test_fork_comparison_reports_the_rotation_averaged_ratio(self):
        baseline = make_run(self.root, 'baseline')
        faster = make_run(self.root, 'faster', library='candidate', fork_ms=5.0)
        rows = candidate.fork_comparison(baseline, faster, 'faer-fork')
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['rotation_averaged_ratio'], 0.5, places=9)
        self.assertFalse(candidate.identity_differences(baseline, faster))


if __name__ == '__main__':
    unittest.main()
