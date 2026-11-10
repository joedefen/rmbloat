"""Regression test for the documented `rmbloat --chooser-tests` command (C2).

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
import sys
import tempfile
import types
import unittest
from unittest import mock


class _Dummy:
    """Minimal stand-in for console_window classes."""

    def __init__(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def stubbed_runtime_dependencies():
    """Install fake third-party modules for the duration of a test."""
    previous = {}

    def install(name, **attributes):
        previous[name] = sys.modules.get(name)
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    console_window = install('console_window')
    for name in ('ConsoleWindow', 'OptionSpinner', 'Screen', 'ScreenStack',
                 'ConsoleWindowOpts', 'IncrementalSearchBar', 'Context',
                 'Theme'):
        setattr(console_window, name, type(name, (_Dummy,), {}))

    ruamel = install('ruamel')
    ruamel_yaml = install('ruamel.yaml', YAML=_Dummy)
    ruamel.yaml = ruamel_yaml

    install('send2trash', send2trash=lambda *args, **kwargs: None)

    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def purge_rmbloat_modules():
    """Drop cached rmbloat modules so module-level paths use the temp HOME."""
    for name in list(sys.modules):
        if name == 'rmbloat' or name.startswith('rmbloat.'):
            sys.modules.pop(name, None)


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
