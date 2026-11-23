"""The `vs` verdict: a difference is annotated only when it beats the spread the run recorded.

The page tells its reader that a cell whose `±` is as large as the difference is not evidence; these
cases hold the verdict to that sentence in the cell's own unit, not only in the relative band.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from campaign_toolkit import report_run  # noqa: E402

KEY = ('gemm', 'gemm/4096x4096x4096/b1', 'f64', 'f', 64)
ROWS = ['faer-fork', 'lapack-mkl']


def verdict(fork, other, w_fork=None, w_other=None):
    best = {KEY: {'faer-fork': fork, 'lapack-mkl': other}}
    within = {KEY: {row: [w] for row, w in zip(ROWS, (w_fork, w_other)) if w is not None}}
    return report_run.comparison_verdict(best, within, KEY, ROWS)['lapack-mkl']


def two_starts(median, sd):
    """The two process medians a page prints as `median ± sd` (sample SD of two values)."""
    half = sd / 2 ** 0.5
    return [median - half, median + half]


class Verdict(unittest.TestCase):
    def test_spread_as_large_as_the_difference_is_not_a_verdict(self):
        # colmat-benchmark#121: ohtaka-fat gemm 4096^3 64T, the process medians of each arm.
        # MKL 306.38 +/- 239.68 against faer 68.74 +/- 1.93: the 237.64 ms difference is smaller
        # than the MKL `±`, although |log ratio| 1.49 clears the relative band (0.78).
        self.assertIs(verdict([67.376864, 70.106223], [136.900354, 475.861514]), False)

    def test_a_difference_beyond_both_spreads_is_reported(self):
        ratio, _, _ = verdict([104.326464, 104.238772], [177.970707, 158.141467])
        self.assertLess(ratio, 1)          # faer faster: 104.28 vs 168.06 +/- 14.02

    def test_a_difference_equal_to_the_spread_is_not_a_verdict(self):
        # The other row's `±` is exactly 100 and so is the difference: "as large as" is not
        # "larger than". |log 101| = 4.6 clears the relative band (0.99), so only the absolute
        # rule decides; shifting the row by 0.5 keeps its SD and makes the difference 100.5.
        self.assertIs(verdict([1.0, 1.0, 1.0], [1.0, 101.0, 201.0]), False)
        ratio, _, _ = verdict([1.0, 1.0, 1.0], [1.5, 101.5, 201.5])
        self.assertLess(ratio, 1)

    def test_a_difference_inside_the_sum_of_the_printed_spreads_is_not_a_verdict(self):
        # colmat-benchmark zen5 candidate-tall-guard-mkl qr/4096x256 4T: 33.4992 +/- 2.79222 vs
        # 30.1599 +/- 1.90694. The 3.339 ms difference beats each printed `±` but not their sum
        # (4.699): the two median ± SD intervals overlap, so it is no longer a verdict
        # (colmat-benchmark#102 review replaced the larger-of-the-two rule with the sum).
        f, o = 33.4992, 30.1599
        self.assertIs(verdict([f - 2.79222, f, f + 2.79222], [o - 1.90694, o, o + 1.90694]), False)

    def test_ohtaka_small_cholesky_64t_overlaps(self):
        # colmat-benchmark#102 small cholesky 2048 64T: 21.1907 +/- 2.5658 vs 24.2012 +/- 0.450257
        # printed "1.14x faster". 3.0105 ms difference, `±` sum 3.0161: not a verdict.
        self.assertIs(verdict(two_starts(21.1907, 2.5658), two_starts(24.2012, 0.450257)), False)

    def test_a_w_as_large_as_the_difference_is_unresolved(self):
        # colmat-benchmark#102 small lu 2048 128T: faer 62.8526 +/- 0.505 (W 0.905) vs OpenBLAS
        # 783.744 +/- 72.44 (W 1071.67). The ± sum (72.9) is far below the 720.9 ms difference, but
        # the OpenBLAS calls inside one process disagree by more than the difference: no ratio.
        self.assertEqual(verdict(two_starts(62.8526, 0.505171), two_starts(783.744, 72.4423),
                                 0.905395, 1071.67), 'unresolved')
        self.assertEqual(report_run.verdict_text('unresolved', 62.0, 'lapack-openblas'),
                         "`?` not evidence vs `lapack-openblas`: a row's `W` is as large as the "
                         "difference (rerun)")
        # The fork's W counts the same way (mirror).
        self.assertEqual(verdict(two_starts(783.744, 72.4423), two_starts(62.8526, 0.505171),
                                 1071.67, 0.905395), 'unresolved')

    def test_a_clear_verdict_is_not_marginal(self):
        # colmat-benchmark#102 small qr_factor 2048 16T: 39.02 +/- 0.46 (W 1.04) vs 442.9 +/- 4.18.
        ratio, _, marginal = verdict(two_starts(39.0216, 0.461818), two_starts(442.917, 4.1759),
                                     1.04074, 2.90573)
        self.assertLess(ratio, 1)
        self.assertIsNone(marginal)

    def test_a_spread_near_the_difference_is_marginal(self):
        # colmat-benchmark#102 small cholesky 2048 32T: 19.0237 +/- 0.0854 (W 1.43338) vs
        # 20.8068 +/- 0.0952: the 1.78 ms difference clears every rule, but the fork's W is 0.80
        # of it, so the page says marginal instead of printing a bare 1.09x.
        ratio, _, marginal = verdict(two_starts(19.0237, 0.0854086), two_starts(20.8068, 0.0951808),
                                     1.43338, 0.283342)
        self.assertLess(ratio, 1)
        self.assertAlmostEqual(marginal, 1.43338 / 1.7831, places=3)
        self.assertIn('(marginal: spread 0.80 of the difference)',
                      report_run.verdict_text((ratio, 0.05, marginal), 19.0, 'lapack-mkl'))

    def test_a_noisy_fork_row_is_checked_too(self):
        # The fork's own `±` counts: zen5 325e482 OpenBLAS gemm/64x64x64 8T printed "89x slower"
        # for a fork row of 1.504 +/- 2.444 ms. Mirror of the boundary case above, fork side.
        self.assertIs(verdict([1.0, 101.0, 201.0], [1.0, 1.0, 1.0]), False)

    def test_the_relative_floor_still_applies(self):
        # Tight rows 3% apart: beyond the absolute spread, inside the 5% floor.
        self.assertIs(verdict([100.0, 100.01], [103.0, 103.01]), False)

    def test_a_row_without_two_starts_has_no_verdict(self):
        self.assertIsNone(verdict([1.0, 1.0], [2.0]))


if __name__ == '__main__':
    unittest.main()
