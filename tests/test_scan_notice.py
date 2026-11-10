"""Regression tests: the startup directory scan must announce itself.

``create_video_file_list()`` walks every entry under the requested paths before
any probe runs.  On a media share with tens of thousands of files that walk can
take 20 seconds or more, so the terminal used to sit silent between the FFmpeg
summary and ``Starting concurrent ffprobe...``.  The scan now says what it is
doing and reports how long it took and what it found.
"""

# pylint: disable=protected-access,missing-function-docstring

import contextlib
import importlib
import io
import types
import unittest
from unittest import mock

from tests.runtime_stubs import (purge_rmbloat_modules,
                                 stubbed_runtime_dependencies)


class ScanNoticeTests(unittest.TestCase):
    """The pre-probe directory walk is visible on stderr."""

    def setUp(self):
        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def run_scan(self, files, candidates):
        """Call ``create_video_file_list()`` and return (result, stderr text)."""
        converter = object.__new__(self.rmbloat.Converter)
        converter.opts = types.SimpleNamespace(files=list(files))
        converter.get_candidate_video_files = mock.Mock(
            return_value=(list(candidates), False))
        converter.probe_cache = mock.Mock()
        converter.probe_cache.batch_get_or_probe.return_value = {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = converter.create_video_file_list()
        return result, err.getvalue()

    def test_directory_scan_is_announced_and_reported(self):
        _result, err = self.run_scan(['/heap/Videos'], ['/heap/Videos/a.mkv'])

        self.assertIn('Scanning /heap/Videos for video files', err)
        # The wording must set an expectation for a slow share.
        self.assertIn('large collections can take a while', err)
        self.assertRegex(err, r'Found 1 candidate video in \d+\.\ds\n')
        # Ordering: announcement, then result, then the probe progress line.
        self.assertLess(err.index('Scanning'), err.index('Found'))
        self.assertLess(err.index('Found'), err.index('probing: 0% 0 of 1'))

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
