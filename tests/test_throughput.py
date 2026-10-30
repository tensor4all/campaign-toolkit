import unittest

from campaign_toolkit import throughput


class PointsTest(unittest.TestCase):
    def test_points_scale_with_the_operation_count_and_report_spread(self):
        cells = {(1, 'gemm', 32, '', 'faer-fork'): {'run0': 1.0, 'run1': 1.1}}
        out = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0)
        self.assertEqual(len(out), 1)
        size, gflops, err = out[0]
        self.assertEqual(size, 32)
        self.assertAlmostEqual(gflops, 2.0 * 32 ** 3 / (1.05 * 1e6), places=6)
        self.assertGreater(err, 0)

    def test_c64_counts_four_times_the_real_operations(self):
        cells = {(1, 'gemm', 32, '', 'faer-fork'): {'run0': 1.0, 'run1': 1.0}}
        real = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0, complex_dtype=False)[0][1]
        cplx = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0, complex_dtype=True)[0][1]
        self.assertAlmostEqual(cplx, 4 * real, places=6)


class RevisionsLineTest(unittest.TestCase):
    def test_names_role_vendor_and_revisions_and_the_missing_run(self):
        import tempfile
        import yaml
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'run.yaml').write_text(yaml.safe_dump({
                'suite_id': 'faer-kernels-large', 'vendor': {'name': 'mkl'},
                'library': {'commit': 'a' * 40}, 'upstream': {'commit': 'b' * 40},
                'harness': {'commit': 'c' * 40}}))
            line = throughput.revisions_line([run, None], ['large mkl', 'large openblas'])
        self.assertIn('large tier mkl: fork `aaaaaaa`, upstream `bbbbbbb`, harness `ccccccc`', line)
        self.assertIn('No run was given for large openblas', line)


if __name__ == '__main__':
    unittest.main()
