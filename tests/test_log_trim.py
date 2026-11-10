"""Regression tests: log trimming must preserve chronology.

``_trim_log_file()`` selects survivors by *effective* age so ``ERR`` entries are
kept longer.  It then reversed that weighted order before writing, which is not
timestamp order: an old ``ERR`` line could end up after newer ``OK`` lines.
``get_window_of_entries()`` and ``refresh_window()`` assume the file is
chronological, so the History screen and incremental reads broke after a trim.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from rmbloat.StructuredLogger import LogEntry, StructuredLogger


class LogTrimOrderTests(unittest.TestCase):
    """Trimmed logs must stay chronological while still favouring ERR lines."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.logger = StructuredLogger(
            app_name='rmbloat_trim_test', log_dir=Path(self._tmpdir.name))
        self.now = datetime.now()
        self.lines = []

    def add_entry(self, seconds_ago, level):
        entry = LogEntry(
            timestamp=(self.now - timedelta(seconds=seconds_ago)).isoformat(),
            level=level,
            file='f.py',
            line=1,
            function='fn',
            module='m',
            message='x' * 20,
        )
        self.lines.append(json.dumps(entry.to_dict()) + '\n')

    def write_log(self):
        with open(self.logger.log_file, 'w', encoding='utf-8') as handle:
            handle.writelines(self.lines)

    def read_timestamps(self):
        timestamps = []
        with open(self.logger.log_file, 'r', encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    timestamps.append(json.loads(line)['timestamp'])
        return timestamps

    def trim_keeping_four(self):
        self.write_log()
        widest = max(len(line.encode('utf-8')) for line in self.lines)
        self.logger.MAX_LOG_SIZE = widest * 4
        self.logger.TRIM_TO_RATIO = 1.0
        self.logger._trim_log_file()  # pylint: disable=protected-access

    def test_trim_keeps_err_but_writes_chronological_order(self):
        # The oldest entry is an ERR, so weighted selection must keep it while
        # dropping a newer OK entry.
        self.add_entry(2000, 'ERR')
        self.add_entry(1000, 'OK')
        self.add_entry(900, 'OK')
        self.add_entry(800, 'OK')
        self.add_entry(700, 'OK')
        self.add_entry(600, 'OK')
        self.add_entry(500, 'OK')

        self.trim_keeping_four()
        timestamps = self.read_timestamps()
        parsed = [datetime.fromisoformat(ts) for ts in timestamps]

        # Chronological order (the regression wrote oldest ERR lines last).
        self.assertEqual(parsed, sorted(parsed))
        # Weighted selection still retained the oldest entry because it is ERR.
        self.assertIn((self.now - timedelta(seconds=2000)).isoformat(), timestamps)
        # The newest entry survived; the older OK did not fit the target size.
        self.assertIn((self.now - timedelta(seconds=500)).isoformat(), timestamps)
        self.assertNotIn((self.now - timedelta(seconds=1000)).isoformat(), timestamps)
        self.assertLessEqual(len(timestamps), 4)

    def test_window_reads_are_chronological_after_trim(self):
        self.add_entry(2000, 'ERR')
        for seconds_ago in (1000, 900, 800, 700, 600, 500):
            self.add_entry(seconds_ago, 'OK')

        self.trim_keeping_four()
        window, _state = self.logger.get_window_of_entries(window_size=10)
        parsed = [datetime.fromisoformat(ts) for ts in window.keys()]

        self.assertEqual(parsed, sorted(parsed))


if __name__ == '__main__':
    unittest.main()
