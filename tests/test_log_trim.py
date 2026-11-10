"""Regression tests: log trimming must preserve chronology.

``_trim_log_file()`` selects survivors by *effective* age so ``ERR`` entries are
kept longer.  It then reversed that weighted order before writing, which is not
timestamp order: an old ``ERR`` line could end up after newer ``OK`` lines.
``get_window_of_entries()`` and ``refresh_window()`` assume the file is
chronological, so the History screen and incremental reads broke after a trim.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

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

    def test_failed_replace_during_trim_leaves_the_original_intact(self):
        """A failed atomic install must not truncate the existing history."""
        self.add_entry(2000, 'ERR')
        for seconds_ago in (1000, 900, 800, 700, 600, 500):
            self.add_entry(seconds_ago, 'OK')
        self.write_log()
        original = self.logger.log_file.read_bytes()

        widest = max(len(line.encode('utf-8')) for line in self.lines)
        self.logger.MAX_LOG_SIZE = widest * 4
        self.logger.TRIM_TO_RATIO = 1.0
        stderr = io.StringIO()
        with mock.patch(
                'rmbloat.StructuredLogger.os.replace',
                side_effect=OSError('simulated full disk')), \
                contextlib.redirect_stderr(stderr):
            self.logger._trim_log_file()  # pylint: disable=protected-access

        self.assertEqual(self.logger.log_file.read_bytes(), original)
        self.assertIn('TRIM ERROR', stderr.getvalue())
        # The temporary sibling was cleaned up.
        self.assertEqual(list(Path(self._tmpdir.name).rglob('*.tmp')), [])

    def test_trim_preserves_unparseable_lines(self):
        self.add_entry(2000, 'ERR')
        for seconds_ago in (1000, 900, 800, 700, 600, 500):
            self.add_entry(seconds_ago, 'OK')
        self.lines.append('{ this is not json }\n')

        self.trim_keeping_four()

        contents = self.logger.log_file.read_text(encoding='utf-8')
        self.assertIn('{ this is not json }', contents)

    def test_purge_preserves_unparseable_lines(self):
        self.add_entry(10, 'OK')
        self.lines.append('{ this is not json }\n')
        self.write_log()

        self.logger.purge_at_or_before(datetime.now().isoformat())

        lines = [line for line in
                 self.logger.log_file.read_text(encoding='utf-8').splitlines()
                 if line.strip()]
        self.assertEqual(lines, ['{ this is not json }'])


if __name__ == '__main__':
    unittest.main()
