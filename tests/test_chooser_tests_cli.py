"""Regression test for the documented `rmbloat --chooser-tests` command.

When no video file is supplied, ``main()`` asks ``FfmpegChooser.run_tests()``
to print example encode commands.  Those examples use the illustrative path
``input.mp4``, which normally does not exist, so building the namespace used
to raise ``FileNotFoundError`` and crash the very first Quick Start command.

These tests exercise the real ``main(['--chooser-tests'])`` path.  FFmpeg
runtime detection is stubbed so the test never pulls a container image, and
``HOME`` is redirected to a temporary directory so no user configuration or
logs are touched.
"""

import contextlib
import importlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tests.runtime_stubs import (purge_rmbloat_modules,
                                 stubbed_runtime_dependencies)


class ChooserTestsCommandRegressionTests(unittest.TestCase):
    """`rmbloat --chooser-tests` must print examples without a video file."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        home_patch = mock.patch.dict(
            os.environ, {'HOME': self.temp_dir.name}, clear=False)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def _stub_detection(self):
        """Force a system-ffmpeg/CPU strategy without touching Docker."""
        def fake_system_detection(chooser):
            chooser.system_ffmpeg_path = '/usr/bin/ffmpeg'
            chooser.has_system_acceleration = False
            chooser.vaapi_supports_10bit = False

        def fake_runtime_detection(chooser):
            chooser.runtime = None

        return [
            mock.patch.object(
                self.rmbloat.FfmpegChooser, '_detect_system_ffmpeg',
                autospec=True, side_effect=fake_system_detection),
            mock.patch.object(
                self.rmbloat.FfmpegChooser, '_detect_runtime',
                autospec=True, side_effect=fake_runtime_detection),
        ]

    def test_chooser_tests_without_video_prints_examples(self):
        patches = self._stub_detection()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                self.rmbloat.main(['--chooser-tests'])

        self.assertEqual(raised.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn('Example FFmpeg Command', output)
        self.assertIn('Example FFprobe Command', output)
        self.assertIn("-i input.mp4", output)


if __name__ == '__main__':
    unittest.main()
