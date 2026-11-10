"""Regression tests for audit finding C5: ``--auto-hr`` must actually stop.

Previously the auto-mode time limit was stored but never read, the vitals
report was never printed, and an empty queue simply returned, leaving an
unattended run idling forever (so cron / ``rmbloatd`` maintenance windows never
terminated).

These tests drive [`Converter._manage_queue_and_auto_mode()`] directly with a
partially-constructed ``Converter`` so no curses UI or real encoding is needed.
"""

import contextlib
import importlib
import io
import os
import sys
import tempfile
import time
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


class AutoModeStopTests(unittest.TestCase):
    """Auto mode must terminate instead of idling forever."""

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

    def make_converter(self):
        """Build a Converter without running __init__ (no curses, no probes)."""
        converter = object.__new__(self.rmbloat.Converter)
        converter.auto_mode_enabled = True
        converter.auto_mode_hrs_limit = 1.0
        converter.auto_mode_start_mono = time.monotonic()
        converter.job = None
        converter.job_handler = mock.Mock()
        converter.job_handler.auto_mode_start_time = time.monotonic()
        converter.job_handler.consecutive_failures = 0
        converter.job_handler.ok_count = 3
        converter.job_handler.error_count = 1
        converter.vids = []
        converter.visible_vids = []
        converter.todo_vids = []
        converter.win = None
        converter.search_re = None
        converter.spins = types.SimpleNamespace(
            mangle=False, directory=False, filter='')
        converter.opts = types.SimpleNamespace(sample=False)
        converter.stack = types.SimpleNamespace(
            curr=types.SimpleNamespace(num=self.rmbloat.CONVERT_ST))
        return converter

    def make_vid(self, path):
        return types.SimpleNamespace(
            doit='[X]', doit_auto='[X]', codec='h264', basename1=None,
            filepath=path, height=1080, res_ok=False, bloat_ok=False,
            codec_ok=False, duration=120.0, net='  - ', bloat=2000, gb=1.5,
            probe0=None, all_ok=False)

    def make_video_path(self, name='movie.mkv'):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, 'wb') as handle:
            handle.write(b'x')
        return path

    def run_queue_manager(self, converter):
        """Run the queue manager, returning (exit_code_or_None, stdout)."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            try:
                converter._manage_queue_and_auto_mode()  # pylint: disable=protected-access
            except SystemExit as exc:
                return exc.code, stdout.getvalue()
        return None, stdout.getvalue()

    def test_queue_drained_exits_cleanly(self):
        """An empty queue must end the run, not idle forever."""
        converter = self.make_converter()
        code, output = self.run_queue_manager(converter)

        self.assertEqual(code, 0)
        self.assertIn('AUTO MODE VITALS REPORT', output)
        self.assertIn('conversion queue drained', output)

    def test_queue_drained_without_job_handler_exits_cleanly(self):
        """Auto mode with nothing selected has no handler yet; still exits."""
        converter = self.make_converter()
        converter.job_handler = None
        code, output = self.run_queue_manager(converter)

        self.assertEqual(code, 0)
        self.assertIn('AUTO MODE VITALS REPORT', output)
        self.assertIn('OK conversions:       0', output)

    def test_time_limit_exceeded_stops_before_starting_next_job(self):
        """A spent --auto-hr budget must stop the run and report vitals."""
        converter = self.make_converter()
        vid = self.make_vid(self.make_video_path())
        converter.vids = [vid]
        converter.visible_vids = [vid]
        converter.todo_vids = [vid]
        # Started two hours ago with a one-hour budget.
        converter.job_handler.auto_mode_start_time = time.monotonic() - 7200

        code, output = self.run_queue_manager(converter)

        self.assertEqual(code, 0)
        self.assertIn('time limit of 1.0 hours reached', output)
        converter.job_handler.start_transcode_job.assert_not_called()
        self.assertIsNone(converter.job)

    def test_within_time_budget_starts_the_next_job(self):
        """Inside the budget the queue manager still launches work."""
        converter = self.make_converter()
        vid = self.make_vid(self.make_video_path())
        converter.vids = [vid]
        converter.visible_vids = [vid]
        converter.todo_vids = [vid]
        sentinel = object()
        converter.job_handler.start_transcode_job.return_value = sentinel

        code, _ = self.run_queue_manager(converter)

        self.assertIsNone(code)
        self.assertIs(converter.job, sentinel)
        self.assertEqual(vid.doit, 'IP ')
        converter.job_handler.start_transcode_job.assert_called_once_with(vid)

    def test_consecutive_failure_limit_exits_nonzero(self):
        """Ten failed conversions in a row end the run with status 1."""
        converter = self.make_converter()
        vid = self.make_vid(self.make_video_path())
        converter.vids = [vid]
        converter.visible_vids = [vid]
        converter.todo_vids = [vid]
        converter.job_handler.consecutive_failures = 10

        code, output = self.run_queue_manager(converter)

        self.assertEqual(code, 1)
        self.assertIn('10 consecutive conversion failures', output)
        self.assertIn('early termination', output)

    def test_time_limit_ignored_when_auto_mode_disabled(self):
        """`_auto_mode_time_exceeded()` stays False without --auto-hr."""
        converter = self.make_converter()
        converter.auto_mode_enabled = False
        converter.auto_mode_start_mono = time.monotonic() - 7200
        self.assertFalse(converter._auto_mode_time_exceeded())  # pylint: disable=protected-access

    def test_missing_file_is_pruned_then_run_ends(self):
        """A queued file that vanished mid-run is dropped, then the run ends."""
        converter = self.make_converter()
        vid = self.make_vid(os.path.join(self.temp_dir.name, 'gone.mkv'))
        converter.vids = [vid]
        converter.visible_vids = [vid]
        converter.todo_vids = [vid]

        code, output = self.run_queue_manager(converter)

        self.assertEqual(code, 0)
        # The vanished entry is pruned from the master list and never started.
        self.assertEqual(converter.vids, [])
        converter.job_handler.start_transcode_job.assert_not_called()
        self.assertIn('conversion queue drained', output)


if __name__ == '__main__':
    unittest.main()
