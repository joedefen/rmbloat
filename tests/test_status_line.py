"""Regression tests for the single-line startup status output.

The scan and probe phases each own one status line.  On a terminal the line is
rewritten in place so only its final state remains; when stderr is redirected
the intermediate updates are dropped and the opening and final states become
ordinary lines.  Either way the phase must finish with a terminated line, so a
later phase cannot land on the same row and neither status can hide the other.
"""

import io
import os
import sys
import unittest
from unittest import mock

from rmbloat import StatusLine


class _Stream:
    """``stderr`` stand-in whose ``isatty()`` result is chosen by the test."""

    def __init__(self, tty):
        self._tty = tty
        self.chunks = []
        self.flushes = 0

    def isatty(self):
        return self._tty

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        self.flushes += 1

    @property
    def text(self):
        return ''.join(self.chunks)


class StatusLineTests(unittest.TestCase):
    """One line per phase, ending on its final state."""

    def run_calls(self, stream, *calls):
        with mock.patch.object(sys, 'stderr', stream):
            for function, text in calls:
                function(text)
        return stream

    def test_tty_rewrites_a_single_terminated_line(self):
        stream = self.run_calls(
            _Stream(True),
            (StatusLine.begin, 'Scanning /media ...'),
            (StatusLine.update, 'Scanning /media ... 25%'),
            (StatusLine.end, 'Found 7550 candidate videos in 13.9s'))

        # Exactly one newline: the final state, and nothing to wrap onto.
        self.assertEqual(stream.text.count('\n'), 1)
        self.assertTrue(stream.text.endswith(
            '\rFound 7550 candidate videos in 13.9s\033[K\n'))
        # Every write rewinds the cursor and clears the rest of the row.
        self.assertTrue(all(chunk.startswith('\r') for chunk in stream.chunks))
        self.assertEqual(stream.text.count('\033[K'), 3)

    def test_tty_updates_replace_rather_than_append(self):
        stream = self.run_calls(
            _Stream(True),
            (StatusLine.begin, 'probing: 0% 0 of 2'),
            (StatusLine.update, 'probing: 50% 1 of 2'),
            (StatusLine.end, 'probing: 100% 2 of 2 in 1.2s'))

        self.assertIn('\rprobing: 50% 1 of 2\033[K', stream.text)
        self.assertNotIn('probing: 0% 0 of 2\n', stream.text)

    def test_tty_truncates_long_text_instead_of_wrapping(self):
        long_text = 'Scanning ' + '/very/long/path' * 20
        stream = _Stream(True)
        with mock.patch.object(
                StatusLine.shutil, 'get_terminal_size',
                return_value=os.terminal_size((60, 24))):
            self.run_calls(stream, (StatusLine.begin, long_text))

        payload = stream.text.lstrip('\r').removesuffix('\033[K')
        self.assertLess(len(payload), 60)
        self.assertTrue(payload.endswith('...'))

    def test_redirected_stderr_keeps_only_begin_and_end(self):
        stream = self.run_calls(
            io.StringIO(),
            (StatusLine.begin, 'Scanning /media ...'),
            (StatusLine.update, 'Scanning /media ... 25%'),
            (StatusLine.end, 'Found 2 candidate videos in 0.4s'))

        self.assertEqual(stream.getvalue(),
                         'Scanning /media ...\n'
                         'Found 2 candidate videos in 0.4s\n')
        self.assertNotIn('\r', stream.getvalue())
        self.assertNotIn('\033[K', stream.getvalue())

    def test_stream_without_isatty_is_treated_as_redirected(self):
        class _NoIsatty:
            def __init__(self):
                self.chunks = []

            def write(self, text):
                self.chunks.append(text)

        stream = _NoIsatty()
        self.run_calls(stream, (StatusLine.end, 'probing: 100% 2 of 2'))
        self.assertEqual(stream.chunks, ['probing: 100% 2 of 2\n'])

    def test_every_write_is_flushed(self):
        stream = self.run_calls(
            _Stream(True),
            (StatusLine.begin, 'a'),
            (StatusLine.update, 'b'),
            (StatusLine.end, 'c'))

        self.assertEqual(stream.flushes, 3)


if __name__ == '__main__':
    unittest.main()
