"""Regression tests for audit finding H10.

``main()`` used to catch every ``Exception``, print a traceback, and return
normally, so the process always exited with status 0.  Shell scripts, cron,
systemd units, and CI could not tell a successful run from a failed
initialization.

``main()`` now returns an exit status (0 on success, 1 on failure) and the
``__main__`` entry point forwards it through ``sys.exit()``.
"""

import contextlib
import importlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import (purge_rmbloat_modules,
                                 stubbed_runtime_dependencies)


class MainExitStatusTests(unittest.TestCase):
    """`main()` must report failures to the shell as a non-zero status."""

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

        # Isolate class-level singleton state between tests.
        previous_singleton = self.rmbloat.Converter.singleton
        self.rmbloat.Converter.singleton = None
        self.addCleanup(
            setattr, self.rmbloat.Converter, 'singleton', previous_singleton)

    def _fake_ini(self):
        vals = SimpleNamespace(
            allowed_codecs='x265',
            bloat_thresh=1600,
            files=[],
            full_speed=False,
            keep_backup=False,
            max_height=65536,
            merge_subtitles=False,
            min_shrink_pct=10,
            prefer_strategy='auto',
            quality=28,
            thread_cnt=4,
            use_trash=False,
        )
        return SimpleNamespace(
            vals=vals,
            config_file_path=os.path.join(self.temp_dir.name, 'config.ini'),
            write=mock.Mock(),
        )

    def test_successful_run_returns_zero(self):
        converter = mock.Mock()
        ini = self._fake_ini()
        with mock.patch.object(
                self.rmbloat, 'IniManager', return_value=ini), \
             mock.patch.object(
                self.rmbloat, 'Converter', return_value=converter) as conv_cls:
            status = self.rmbloat.main([])

        self.assertEqual(status, 0)
        converter.main_loop.assert_called_once_with()
        # ProbeCache must choose its private XDG directory, not the config dir.
        self.assertIsNone(conv_cls.call_args.kwargs.get('cache_dir'))
        self.assertIs(conv_cls.call_args.kwargs.get('ini'), ini)

    def test_unexpected_exception_returns_one_and_reports_traceback(self):
        stderr = io.StringIO()
        with mock.patch.object(
                self.rmbloat, 'IniManager',
                side_effect=RuntimeError('initialization exploded')), \
             contextlib.redirect_stderr(stderr), \
             contextlib.redirect_stdout(io.StringIO()):
            status = self.rmbloat.main([])

        self.assertEqual(status, 1)
        printed = stderr.getvalue()
        self.assertIn('initialization exploded', printed)
        self.assertIn('Traceback', printed)

    def test_exception_path_stops_curses_window(self):
        win = SimpleNamespace(stop_curses=mock.Mock())
        self.rmbloat.Converter.singleton = SimpleNamespace(win=win)

        with mock.patch.object(
                self.rmbloat, 'IniManager',
                side_effect=RuntimeError('boom')), \
             contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            status = self.rmbloat.main([])

        self.assertEqual(status, 1)
        win.stop_curses.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
