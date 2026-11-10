"""Regression tests for the 10-bit output policy.

``should_use_10bit()`` used to branch over several codec/pixel-format cases
that all returned ``False``.  The simplified policy is intentionally narrow:
preserve 10-bit sources, and encode everything else as 8-bit for VAAPI safety.
"""

import unittest

from rmbloat.JobHandler import JobHandler


class ShouldUse10BitTests(unittest.TestCase):
    """Only sources that are already 10-bit should produce 10-bit output."""

    def test_ten_bit_sources_are_preserved(self):
        for pix_fmt in ('yuv420p10le', 'yuv422p10be', 'p010le', 'p210le'):
            with self.subTest(pix_fmt=pix_fmt):
                self.assertTrue(JobHandler.should_use_10bit(pix_fmt, 'hevc'))

    def test_eight_bit_and_unknown_sources_stay_eight_bit(self):
        for pix_fmt in ('yuv420p', 'yuv422p', 'yuv444p', 'nv12', 'nv21',
                        'yuvj420p', 'bgr24', 'rgb24', 'unknown', '', None):
            with self.subTest(pix_fmt=pix_fmt):
                self.assertFalse(JobHandler.should_use_10bit(pix_fmt, 'h264'))

    def test_codec_no_longer_changes_the_decision(self):
        self.assertFalse(JobHandler.should_use_10bit('yuv420p', 'mpeg4'))
        self.assertTrue(JobHandler.should_use_10bit('yuv420p10le', 'mpeg4'))


if __name__ == '__main__':
    unittest.main()
