import pathlib
import statistics
import unittest
from unittest import mock

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

    def test_one_point_per_size_when_two_notes_carry_the_same_cell(self):
        cells = {(1, 'gemm', 32, '', 'faer-fork'): {'run0': 1.0, 'run1': 1.0},
                 (1, 'gemm', 32, 'api-inclusive', 'faer-fork'): {'run0': 2.0, 'run1': 2.0}}
        out = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][1], 2.0 * 32 ** 3 / (1.0 * 1e6), places=6)

    def test_api_inclusive_rows_are_plotted_and_variant_notes_are_skipped(self):
        cells = {(1, 'gemm', 32, 'api-inclusive', 'lapack-mkl'): {'run0': 1.0, 'run1': 1.0},
                 (1, 'gemm', 64, 'variant-a', 'lapack-mkl'): {'run0': 1.0, 'run1': 1.0}}
        out = throughput.points(cells, 1, 'gemm', 'lapack-mkl', coefficient=2.0)
        self.assertEqual([size for size, _, _ in out], [32])

    def test_c64_counts_four_times_the_real_operations(self):
        cells = {(1, 'gemm', 32, '', 'faer-fork'): {'run0': 1.0, 'run1': 1.0}}
        real = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0, complex_dtype=False)[0][1]
        cplx = throughput.points(cells, 1, 'gemm', 'faer-fork', coefficient=2.0, complex_dtype=True)[0][1]
        self.assertAlmostEqual(cplx, 4 * real, places=6)


class RenderFamilyLabelTest(unittest.TestCase):
    def test_a_series_label_replaces_the_engine_name_in_the_legend_and_the_points_still_load(self):
        cells = {(1, 'gemm', 32, '', 'faer-fork'): {'run0': 1.0, 'run1': 1.1},
                 (1, 'gemm', 64, '', 'faer-fork'): {'run0': 0.9, 'run1': 0.9}}
        series = [{'engine': 'faer-fork', 'label': 'colmat', 'colour': '#1f77b4', 'run': [0]}]
        svg, _dropped = throughput.render_family('gemm', {'coefficient': 2.0}, series,
                                                 {'faer-fork': cells}, [1])
        self.assertIn('colmat', svg)
        self.assertNotIn('faer-fork', svg)
        self.assertIn('<polyline points=', svg)   # the labeled series' points are plotted


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



class ThroughputFiguresVariantsTest(unittest.TestCase):
    """`figures.site_figures` writes one chart set per declared (dtype, order) variant, each
    plotted from that variant's own recorded cells."""

    CAMPAIGN = {
        'schema_version': 1, 'id': 'variants-campaign',
        'measured': {'name': 'fixture-rs', 'url': 'https://example.invalid/fixture-rs',
                     'pin_file': 'pins/fixture-rs.rev', 'extern_dir': 'extern/fixture-rs',
                     'build_script': 'scripts/build_fixture_rev.sh', 'binary': 'fixturebench',
                     'pin_env': {'rev': 'FIXTURE_REV', 'dirty': 'FIXTURE_DIRTY', 'dir': 'FIXTURE_DIR'}},
        'identities': ['library'], 'profiles_file': 'benchmarks/profiles.yaml',
        'suites_dir': 'benchmarks/suites', 'data_dir': 'data/results', 'result_dir': 'result',
        'engines': {'faer-fork': 'fork'}, 'vendor_providers': ['mkl'],
        'page_name': '{library12}', 'default_invalidated_by': ['fixture/**'],
        'figures': {'throughput': {'config': 'benchmarks/throughput.yaml',
                                   'runs': [{'suite': 's', 'vendor': 'mkl'}]}},
        'report': {'library_label': 'fixture-rs commit', 'provider_label': 'fork',
                   'harness_label': 'harness commit', 'index_generator': 'scripts/gen_index.py',
                   'setup_script': 'scripts/setup_extern_deps.sh'},
    }
    SERIES = [{'engine': 'faer-fork', 'label': 'fork', 'colour': '#111', 'run': [0]}]
    FAMILIES = {'gemm': {'coefficient': 2.0, 'label': '2n^3'}}

    def _site(self, tmp, config):
        import yaml
        from campaign_toolkit import descriptor, figures
        root = pathlib.Path(tmp) / 'campaign'
        run = root / 'data' / 'results' / 'run'
        (root / 'benchmarks').mkdir(parents=True)
        run.mkdir(parents=True)
        (root / 'result').mkdir()
        (root / 'benchmarks' / 'campaign.yaml').write_text(yaml.safe_dump(self.CAMPAIGN))
        (root / 'benchmarks' / 'throughput.yaml').write_text(yaml.safe_dump(config))
        # One recorded run: gemm at n = 32 and 64, both repeats, every (dtype, order) cell.
        # The c64 samples are three times the f64 ones, so a chart set that quietly plotted the
        # f64 cells under a c64 name is distinguishable from a correctly generated one.
        lines = ['status,dtype,order,m,n,threads,family,note,row,samples_ms\n']
        for dtype, scale in (('f64', 1.0), ('c64', 3.0)):
            for order in ('f', 'c'):
                for n in (32, 64):
                    lines.append(f'ok,{dtype},{order},{n},{n},1,gemm,,faer-fork,'
                                 f'"[{scale * 1.0}, {scale * 1.2}]"\n')
        for repeat in ('run0', 'run1'):
            (run / f'{repeat}-gemm-1t.csv').write_text(''.join(lines))
        (run / 'run.yaml').write_text(yaml.safe_dump({
            'result_files': ['run0-gemm-1t.csv', 'run1-gemm-1t.csv'],
            'target_profile': 'p', 'suite_id': 's', 'vendor': {'name': 'mkl'}}))
        page = root / 'result' / 'page.md'
        page.write_text('<!-- generated from data/results/run/report.md by the recorder -->\n')
        descriptor.set_root(root)
        return root, figures.site_figures(root, root / 'result', [page])

    def test_one_chart_set_per_variant_from_that_variants_own_cells(self):
        import re
        import tempfile
        config = {'version': 'v9', 'budgets': [1],
                  'variants': [{'dtype': 'f64', 'order': 'f'}, {'dtype': 'f64', 'order': 'c'},
                               {'dtype': 'c64', 'order': 'f'}, {'dtype': 'c64', 'order': 'c'}],
                  'page': {'title': 'throughput', 'notes': []},
                  'series': self.SERIES, 'families': self.FAMILIES}
        with tempfile.TemporaryDirectory() as tmp:
            root, found = self._site(tmp, config)
            self.assertEqual(len(found), 4)   # one chart per (family, variant)
            self.assertEqual([(f['dtype'], f['order']) for f in found],
                             [('f64', 'f'), ('f64', 'c'), ('c64', 'f'), ('c64', 'c')])
            for f in found:
                self.assertIn(f"({f['dtype']}, {f['order'].upper()} order)", f['title'])
                self.assertIn('daily mkl', f['revisions'])   # the runs behind the charts
            # One directory per variant, and the README of each set names its dataset.
            per_variant = root / 'result' / 'figures' / 'throughput' / 'p'
            self.assertEqual(sorted(p.name for p in per_variant.iterdir()),
                             ['c64-c', 'c64-f', 'f64-c', 'f64-f'])
            for variant in ('c64-c', 'c64-f', 'f64-c', 'f64-f'):
                readme = (per_variant / variant / 'README.md').read_text()
                self.assertIn(f'# throughput ({variant[0:3]}, {variant[-1].upper()} order)', readme)
            # The c64 set is plotted from the recorded c64 cells with the complex x4 count:
            # 2 * 4 * 64^3 / (median([3.0, 3.6]) ms). An f64 copy, or a forgotten x4, fails this.
            table = (per_variant / 'c64-f' / 'throughput-gemm-v9.md').read_text()
            value = float(re.search(r'\| 64 \| ([0-9.e+-]+) ', table).group(1))
            expected = 2.0 * 4 * 64 ** 3 / (statistics.median([3.0, 3.6]) * 1e6)
            self.assertAlmostEqual(value / expected, 1.0, places=2)
            # The same x4 must reach the drawn chart, not only the table: the renderer has to
            # ask `points` for the complex count when it draws a c64 chart. An auto-scaled log
            # axis hides a dropped multiplier (every value shifts by the same factor), so the
            # contract is asserted at the call the renderer makes.
            cells = throughput.load_run(root / 'data' / 'results' / 'run', dtype='c64', order='f')
            asked = []
            real_points = throughput.points

            def spy(*args, **kwargs):
                asked.append(kwargs.get('complex_dtype'))
                return real_points(*args, **kwargs)

            with mock.patch.object(throughput, 'points', side_effect=spy):
                svg, _ = throughput.render_family('gemm', self.FAMILIES['gemm'], self.SERIES,
                                                  {'faer-fork': cells}, [1], 'c64')
            self.assertEqual(asked, [True])
            self.assertIn('<polyline points=', svg)   # the complex-scaled points are plotted

    def test_the_orders_form_stays_one_f64_chart_set_in_a_flat_directory(self):
        import tempfile
        config = {'version': 'v9', 'budgets': [1], 'orders': ['f'],
                  'page': {'title': 'throughput', 'notes': []},
                  'series': self.SERIES, 'families': self.FAMILIES}
        with tempfile.TemporaryDirectory() as tmp:
            root, found = self._site(tmp, config)
            self.assertEqual(len(found), 1)
            self.assertEqual((found[0]['dtype'], found[0]['order']), ('f64', 'f'))
            self.assertIn('(f64, F order)', found[0]['title'])
            self.assertTrue((root / 'result' / 'figures' / 'throughput' / 'p' /
                             'throughput-gemm-v9.svg').is_file())
if __name__ == '__main__':
    unittest.main()
