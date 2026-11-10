"""Regression tests: the startup directory scan must announce itself.

``create_video_file_list()`` walks every entry under the requested paths before
any probe runs.  On a media share with tens of thousands of files that walk can
take 20 seconds or more, so the scan now owns one status line: it opens with
``Scanning <path> ...`` and is replaced by ``Found <n> candidate videos in Xs``.

The scan must also leave the probe phase its own row.  The probe status is a
separate single-line status owned by :mod:`rmbloat.ProbeCache`, so the scan has
to finish with a terminated line and must never print a ``probing:`` line of
its own.
"""

# pylint: disable=protected-access,missing-function-docstring

import contextlib
import importlib
import io
import sys
import types
import unittest
from unittest import mock

from tests.runtime_stubs import (purge_rmbloat_modules,
                                 stubbed_runtime_dependencies)


class _TtyStream:
    """``stderr`` stand-in that reports itself as an interactive terminal."""

    def __init__(self):
        self.chunks = []

    def isatty(self):
        return True

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return ''.join(self.chunks)


class ScanNoticeTests(unittest.TestCase):
    """The pre-probe directory walk is visible on stderr."""

    def setUp(self):
        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def make_converter(self, files, candidates):
        converter = object.__new__(self.rmbloat.Converter)
        converter.opts = types.SimpleNamespace(files=list(files))
        converter.get_candidate_video_files = mock.Mock(
            return_value=(list(candidates), False))
        converter.probe_cache = mock.Mock()
        converter.probe_cache.batch_get_or_probe.return_value = {}
        return converter

    def run_scan(self, files, candidates):
        """Run the scan with a redirected (non-TTY) stderr."""
        converter = self.make_converter(files, candidates)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = converter.create_video_file_list()
        return result, err.getvalue()

    def test_redirected_scan_reports_announcement_and_result(self):
        _result, err = self.run_scan(['/heap/Videos'], ['/heap/Videos/a.mkv'])

        self.assertIn('Scanning /heap/Videos for video files', err)
        # The wording must set an expectation for a slow share.
        self.assertIn('large collections can take a while', err)
        self.assertRegex(err, r'Found 1 candidate video in \d+\.\ds\n')
        self.assertLess(err.index('Scanning'), err.index('Found'))
        # Probing status belongs to ProbeCache; the scan must not print one.
        self.assertNotIn('probing:', err)

    def test_tty_scan_keeps_a_single_line_and_shows_the_final_state(self):
        converter = self.make_converter(['/heap/Videos'],
                                        ['/heap/Videos/a.mkv'])
        stream = _TtyStream()
        with mock.patch.object(sys, 'stderr', stream):
            converter.create_video_file_list()

        # In place: the announcement is rewritten, not appended, and the line
        # is terminated so the probe status starts on the next row.
        self.assertEqual(stream.text.count('\n'), 1)
        self.assertTrue(stream.text.endswith('\033[K\n'))
        self.assertRegex(stream.text,
                         r'\rFound 1 candidate video in \d+\.\ds\033\[K\n$')
        self.assertTrue(stream.chunks[0].startswith('\rScanning '))
        self.assertNotIn('probing:', stream.text)

    def test_multiple_targets_are_summarised(self):
        _result, err = self.run_scan(['/a', '/b'], ['/a/1.mkv', '/b/2.mkv'])

        self.assertIn('Scanning 2 paths for video files', err)
        self.assertRegex(err, r'Found 2 candidate videos in \d+\.\ds\n')

    def test_stdin_only_run_has_no_scan_notice(self):
        _result, err = self.run_scan(['-'], [])

        self.assertNotIn('Scanning', err)
        self.assertNotIn('Found', err)


if __name__ == '__main__':
    unittest.main()
