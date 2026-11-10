"""Regression tests for ``LogEntry.format_time_delta()``.

The docstring promises compact forms such as ``5d`` and ``2h30m``, but exact
units used to render as ``1m0s`` / ``1d0h`` because the zero minor unit was
always appended.
"""

import unittest

from rmbloat.StructuredLogger import LogEntry


class FormatTimeDeltaTests(unittest.TestCase):
    """Exact units stay compact; partial units keep both components."""

    def test_zero(self):
        self.assertEqual(LogEntry.format_time_delta(0), '0s')

    def test_seconds_only(self):
        self.assertEqual(LogEntry.format_time_delta(45), '45s')

    def test_exact_minute_omits_zero_seconds(self):
        self.assertEqual(LogEntry.format_time_delta(60), '1m')

    def test_minutes_and_seconds(self):
        self.assertEqual(LogEntry.format_time_delta(90), '1m30s')

    def test_exact_hour_omits_zero_minutes(self):
        self.assertEqual(LogEntry.format_time_delta(3600), '1h')

    def test_hours_and_minutes(self):
        self.assertEqual(LogEntry.format_time_delta(5400), '1h30m')

    def test_exact_day_omits_zero_hours(self):
        self.assertEqual(LogEntry.format_time_delta(86400), '1d')

    def test_days_and_hours(self):
        self.assertEqual(LogEntry.format_time_delta(97200), '1d3h')

    def test_exact_week(self):
        self.assertEqual(LogEntry.format_time_delta(604800), '1w')

    def test_exact_year(self):
        self.assertEqual(LogEntry.format_time_delta(31536000), '1y')

    def test_negative_values_are_signed(self):
        self.assertEqual(LogEntry.format_time_delta(-90, signed=True), '-1m30s')
        self.assertEqual(LogEntry.format_time_delta(-3600, signed=True), '-1h')

    def test_positive_values_have_no_plus_sign(self):
        self.assertEqual(LogEntry.format_time_delta(90, signed=True), '1m30s')

    def test_sub_second_rounds_down_to_zero(self):
        self.assertEqual(LogEntry.format_time_delta(0.4), '0s')
        self.assertEqual(LogEntry.format_time_delta(-0.4, signed=True), '0s')


if __name__ == '__main__':
    unittest.main()
