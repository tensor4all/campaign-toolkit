"""charts.py: the layout checker, log axes with minor ticks, the legend above the panels, and text
that stays readable at the width the site shows a chart."""
import math
import re
import sys
import pathlib
import unittest

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import charts, site, throughput  # noqa: E402

SERIES = [dict(engine='fork', colour='#1f77b4', marker='circle', run=0),
          dict(engine='vendor-a', colour='#2ca02c', marker='triangle', run=0),
          dict(engine='vendor-b', colour='#d62728', marker='diamond', dash='5 3', run=0)]


def cells(sizes=(32, 64, 128, 256, 512, 1024), budgets=(1, 4, 8)):
    out = {}
    for threads in budgets:
        for index, engine in enumerate(s['engine'] for s in SERIES):
            for size in sizes:
                ms = 1e-6 * size ** 3 / (threads * (1 + index))
                out[(threads, 'gemm', size, '', engine)] = {'run0': ms, 'run1': ms * 1.02}
    return out


def throughput_svg(budgets=(1, 4, 8)):
    data = cells(budgets=budgets)
    svg, _ = throughput.render_family('gemm', {'coefficient': 2.0, 'label': '2n^3'}, SERIES,
                                      {s['engine']: data for s in SERIES}, budgets)
    return svg


def minor_lines(svg, axis):
    return re.findall(rf'<line [^>]*class="minor" data-axis="{axis}"', svg)


def panel_rects(svg):
    return [tuple(float(v) for v in m) for m in
            re.findall(r'<rect x="([0-9.]+)" y="([0-9.]+)" width="([0-9.]+)" height="([0-9.]+)" fill="#ffffff" '
                       r'stroke="#d0d0d0"/>', svg)]


class LayoutTest(unittest.TestCase):
    PANEL = (0, 0, 400, 300)

    def test_text_on_different_rows_is_not_an_overlap(self):
        layout = charts.Layout()
        layout.text(10, 20, 20, 'aaaa', panel=self.PANEL)
        layout.text(10, 120, 20, 'bbbb', panel=self.PANEL)     # same column, far below: fine

    def test_overlapping_text_is_refused(self):
        layout = charts.Layout()
        layout.text(10, 20, 20, 'aaaa', panel=self.PANEL)
        with self.assertRaisesRegex(ValueError, 'overlapping text'):
            layout.text(12, 20, 20, 'bbbb', panel=self.PANEL)
        layout.text(12, 200, 20, 'cccc', panel=self.PANEL)       # far away: accepted

    def test_text_in_another_panel_is_not_compared(self):
        layout = charts.Layout()
        layout.text(10, 20, 20, 'aaaa', panel=self.PANEL)
        layout.text(10, 20, 20, 'aaaa', panel=(0, 0, 401, 300))

    def test_text_leaving_its_panel_is_refused(self):
        layout = charts.Layout()
        with self.assertRaisesRegex(ValueError, 'leaves its panel'):
            layout.text(self.PANEL[2] + 40, 30, 20, 'x', panel=self.PANEL)
        with self.assertRaisesRegex(ValueError, 'leaves its panel'):
            layout.text(100, 290, 20, 'a long rotated label', 'end', rotate=True, panel=self.PANEL)

    def test_a_crowded_tick_is_dropped_and_reported(self):
        layout = charts.Layout()
        self.assertTrue(layout.tick(50, 50, 20, '100', 'middle', panel=self.PANEL))
        self.assertEqual(layout.tick(52, 50, 20, '200', 'middle', panel=self.PANEL), '')
        self.assertEqual(layout.dropped, ['200'])


class LogAxisTest(unittest.TestCase):
    def test_decades_and_eight_minors_per_decade(self):
        major, minor = charts.log_ticks(1, 1000)
        self.assertEqual(major, [1, 10, 100, 1000])
        self.assertEqual(len(minor), 3 * 8)
        self.assertEqual(sorted({round(v / 10 ** math.floor(math.log10(v))) for v in minor}), list(range(2, 10)))

    def test_a_narrow_range_is_widened_until_it_holds_two_ticks(self):
        lo, hi = charts.log_range([17.4, 18.5])
        self.assertLessEqual(lo, 17.4)
        self.assertGreaterEqual(hi, 18.5)
        self.assertGreaterEqual(sum(map(len, charts.log_ticks(lo, hi))), 2)

    def test_every_log_axis_of_a_throughput_chart_carries_its_minor_ticks(self):
        svg = throughput_svg()
        data = cells()
        sizes = sorted({k[2] for k in data})
        x_lo, x_hi = charts.log_range(sizes)
        expected_x = len(charts.log_ticks(x_lo, x_hi)[1])
        self.assertGreater(expected_x, 0)
        # one gridline and one short tick per minor value, in each of the three panels
        self.assertEqual(len(minor_lines(svg, 'x')), 3 * 2 * expected_x)
        self.assertGreater(len(minor_lines(svg, 'y')), 0)
        self.assertEqual(len(minor_lines(svg, 'y')) % (3 * 2), 0)

    def test_a_dot_plot_axis_carries_minor_ticks(self):
        values = {('1T', 'a', 'e'): (0.5, 0.01), ('1T', 'b', 'e'): (40.0, 1.0)}
        svg, _ = charts.dot_plot('t', ['a', 'b'], ['e'], values, ['1T'])
        lo, hi = charts.log_range([0.5, 40.0])
        self.assertEqual(len(minor_lines(svg, 'x')), 2 * len(charts.log_ticks(lo, hi)[1]))


class ReadableTextTest(unittest.TestCase):
    def assertReadable(self, svg):
        sizes = charts.effective_text_px(svg)
        self.assertTrue(sizes)
        small = [(cls, text, round(px, 1)) for cls, text, px in sizes if px < charts.MIN_TEXT_PX]
        self.assertEqual(small, [], 'text below 12 px at 72rem')
        self.assertTrue(any(cls == 'tick' for cls, _, _ in sizes))
        self.assertTrue(any(cls == 'legend' for cls, _, _ in sizes))

    def test_display_width_is_72rem(self):
        self.assertEqual(charts.DISPLAY_PX, 72 * 16)

    def test_throughput_ticks_and_legend_are_at_least_12px_at_72rem(self):
        self.assertReadable(throughput_svg())

    def test_four_budgets_wrap_instead_of_shrinking_the_text(self):
        three, four = throughput_svg((1, 4, 8)), throughput_svg((1, 4, 8, 64))
        width = lambda svg: float(re.search(r'<svg[^>]* width="([0-9.]+)"', svg).group(1))  # noqa: E731
        self.assertEqual(width(three), width(four))
        self.assertReadable(four)
        self.assertEqual(len({y for _, y, _, _ in panel_rects(four)}), 2)       # two rows

    def test_dot_plot_with_long_case_labels_is_readable(self):
        cases = [f'a_very_long_contraction_name_number_{i}_with_more/n1/b1 float64' for i in range(6)]
        values = {(p, c, e): (0.1 * (i + 1) * (j + 1), 0.0) for i, c in enumerate(cases)
                  for j, e in enumerate(('eng-a', 'eng-b')) for p in ('1T', '4T', '8T')}
        svg, _ = charts.dot_plot('cases', cases, ['eng-a', 'eng-b'], values, ['1T', '4T', '8T'])
        self.assertReadable(svg)
        self.assertIn('float64', svg)            # shortened in the middle: the dtype stays


class LegendTest(unittest.TestCase):
    def test_the_legend_is_above_every_panel(self):
        svg = throughput_svg()
        legend_y = [float(m) for m in re.findall(r'<text x="[0-9.]+" y="([0-9.]+)"[^>]*class="legend"', svg)]
        self.assertEqual(len(legend_y), len(SERIES))
        top = min(y for _, y, _, _ in panel_rects(svg))
        self.assertLess(max(legend_y), top)


class SiteCssTest(unittest.TestCase):
    def test_charts_are_one_column_full_width(self):
        self.assertIn('grid-template-columns:1fr', charts.CSS)
        self.assertIn('max-width:72rem', charts.CSS)
        self.assertRegex(charts.CSS, r'\.charts img,\.charts svg\{[^}]*width:100%')
        self.assertIn(charts.CSS, site.CSS)


if __name__ == '__main__':
    unittest.main()
