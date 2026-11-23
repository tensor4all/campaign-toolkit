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


def verdict(fork, other):
    best = {KEY: {'faer-fork': fork, 'lapack-mkl': other}}
    return report_run.comparison_verdict(best, {}, KEY, ROWS)['lapack-mkl']


class Verdict(unittest.TestCase):
    def test_spread_as_large_as_the_difference_is_not_a_verdict(self):
        # colmat-benchmark#121: ohtaka-fat gemm 4096^3 64T, the process medians of each arm.
        # MKL 306.38 +/- 239.68 against faer 68.74 +/- 1.93: the 237.64 ms difference is smaller
        # than the MKL `±`, although |log ratio| 1.49 clears the relative band (0.78).
        self.assertIs(verdict([67.376864, 70.106223], [136.900354, 475.861514]), False)

    def test_a_difference_beyond_both_spreads_is_reported(self):
        ratio, _ = verdict([104.326464, 104.238772], [177.970707, 158.141467])
        self.assertLess(ratio, 1)          # faer faster: 104.28 vs 168.06 +/- 14.02

    def test_a_difference_equal_to_the_spread_is_not_a_verdict(self):
        # The other row's `±` is exactly 100 and so is the difference: "as large as" is not
        # "larger than". |log 101| = 4.6 clears the relative band (0.99), so only the absolute
        # rule decides; shifting the row by 0.5 keeps its SD and makes the difference 100.5.
        self.assertIs(verdict([1.0, 1.0, 1.0], [1.0, 101.0, 201.0]), False)
        ratio, _ = verdict([1.0, 1.0, 1.0], [1.5, 101.5, 201.5])
        self.assertLess(ratio, 1)

    def test_a_difference_beyond_each_printed_spread_is_reported(self):
        # colmat-benchmark zen5 candidate-tall-guard-mkl qr/4096x256 4T: 33.4992 +/- 2.79222 vs
        # 30.1599 +/- 1.90694. The 3.339 ms difference beats both printed `±`, so the page's own
        # sentence calls it evidence (their hypot, 3.381, would not have).
        f, o = 33.4992, 30.1599
        ratio, _ = verdict([f - 2.79222, f, f + 2.79222], [o - 1.90694, o, o + 1.90694])
        self.assertGreater(ratio, 1)

    def test_the_relative_floor_still_applies(self):
        # Tight rows 3% apart: beyond the absolute spread, inside the 5% floor.
        self.assertIs(verdict([100.0, 100.01], [103.0, 103.01]), False)

    def test_a_row_without_two_starts_has_no_verdict(self):
        self.assertIsNone(verdict([1.0, 1.0], [2.0]))


if __name__ == '__main__':
    unittest.main()
