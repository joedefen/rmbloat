"""Regression tests for exit cleanup and CLI boolean semantics.

``JobHandler.emergency_cleanup()`` was never registered, and the transcode
thread is a daemon, so a SIGTERM/crash could orphan an ffmpeg child and leave
``TEMP.*``/``SAMPLE.*`` files behind.  The cleanup is now wired to ``atexit``
and SIGTERM/SIGHUP.

``-F/-B/-M/--use-trash`` used to switch between ``store_true`` and
``store_false`` based on the saved default, so passing a flag *toggled* the
setting instead of forcing it.  Positive flags now always force True, and
paired ``--no-*`` flags force False.
"""

# pylint: disable=protected-access,missing-function-docstring

import importlib
import os
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies


class RmbloatModuleTestCase(unittest.TestCase):
    """Import the real module against hermetic third-party stubs."""

    def setUp(self):
        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')
        previous_singleton = self.rmbloat.Converter.singleton
        self.rmbloat.Converter.singleton = None
        self.addCleanup(
            setattr, self.rmbloat.Converter, 'singleton', previous_singleton)


class ExitCleanupTests(RmbloatModuleTestCase):
    """Exit/signal cleanup must abort the job before deleting its temp file."""

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_job_handler_cleanup_aborts_then_removes_temp(self):
        handler = self.rmbloat.JobHandler.__new__(self.rmbloat.JobHandler)
        job = mock.Mock()
        temp_path = Path(self.temp_dir.name) / 'TEMP.movie.mkv'
        temp_path.write_bytes(b'partial encode')
        handler.job = job
        handler.temp_file = str(temp_path)

        handler.emergency_cleanup()

        job.abort.assert_called_once_with()
        self.assertFalse(temp_path.exists())
        self.assertIsNone(handler.job)
        self.assertIsNone(handler.temp_file)
        # atexit and a signal handler may both run; the second call is a no-op.
        handler.emergency_cleanup()

    def test_converter_cleanup_delegates_and_aborts_active_job(self):
        converter = self.rmbloat.Converter.__new__(self.rmbloat.Converter)
        job = mock.Mock()
        handler = mock.Mock()
        converter.job = job
        converter.job_handler = handler

        converter.emergency_cleanup()

        handler.emergency_cleanup.assert_called_once_with()
        job.abort.assert_called_once_with()
        self.assertIsNone(converter.job)

    def test_exit_handlers_cover_atexit_and_service_signals(self):
        cleanup = mock.Mock()
        with mock.patch.object(self.rmbloat.atexit, 'register') as register, \
                mock.patch.object(self.rmbloat.signal, 'signal') as signal_mock:
            self.rmbloat.install_exit_handlers(cleanup)

        register.assert_called_once_with(cleanup)
        handlers = {
            call.args[0]: call.args[1]
            for call in signal_mock.call_args_list
        }
        self.assertIn(signal.SIGTERM, handlers)
        self.assertIn(signal.SIGHUP, handlers)

        with self.assertRaises(SystemExit) as raised:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        cleanup.assert_called_once_with()


class CliBooleanFlagTests(RmbloatModuleTestCase):
    """Positive flags force True; --no-* flags force False."""

    BOOLEAN_ATTRS = ('full_speed', 'keep_backup', 'merge_subtitles', 'use_trash')

    def setUp(self):
        super().setUp()
        self.config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)

    def fake_ini(self, **overrides):
        values = {
            'allowed_codecs': 'x265',
            'bloat_thresh': 1600,
            'files': [],
            'full_speed': False,
            'keep_backup': False,
            'max_height': 65536,
            'merge_subtitles': False,
            'min_shrink_pct': 10,
            'prefer_strategy': 'auto',
            'quality': 28,
            'thread_cnt': 4,
            'use_trash': False,
        }
        values.update(overrides)
        vals = SimpleNamespace(**values)
        return SimpleNamespace(
            vals=vals,
            config_file_path=os.path.join(self.config_dir.name, 'config.ini'),
            write=mock.Mock(),
        )

    def run_main(self, args, saved):
        converter = mock.Mock()
        with mock.patch.object(
                self.rmbloat, 'IniManager', return_value=self.fake_ini(**saved)), \
                mock.patch.object(
                    self.rmbloat, 'Converter', return_value=converter) as conv_cls:
            status = self.rmbloat.main(args)
        self.assertEqual(converter.main_loop.call_count, 1)
        return status, conv_cls.call_args.args[0]

    @staticmethod
    def all_true():
        return {attr: True for attr in CliBooleanFlagTests.BOOLEAN_ATTRS}

    def assert_booleans(self, opts, expected):
        for attr, value in expected.items():
            self.assertIs(getattr(opts, attr), value, attr)

    def test_positive_flags_force_true_when_saved_default_is_true(self):
        status, opts = self.run_main(
            ['-F', '-B', '-M', '--use-trash'], self.all_true())

        self.assertEqual(status, 0)
        self.assert_booleans(opts, self.all_true())

    def test_positive_flags_force_true_when_saved_default_is_false(self):
        status, opts = self.run_main(
            ['-F', '-B', '-M', '--use-trash'], {})

        self.assertEqual(status, 0)
        self.assert_booleans(opts, self.all_true())

    def test_negative_flags_override_saved_true(self):
        status, opts = self.run_main(
            ['--no-full-speed', '--no-keep-backup',
             '--no-merge-subtitles', '--no-use-trash'], self.all_true())

        self.assertEqual(status, 0)
        self.assert_booleans(opts, {attr: False for attr in self.BOOLEAN_ATTRS})

    def test_no_flags_keeps_saved_defaults(self):
        saved = {
            'full_speed': True,
            'keep_backup': False,
            'merge_subtitles': True,
            'use_trash': False,
        }
        status, opts = self.run_main([], saved)

        self.assertEqual(status, 0)
        self.assert_booleans(opts, saved)

    def test_last_flag_on_the_command_line_wins(self):
        status, opts = self.run_main(
            ['-F', '--no-full-speed', '-F'], {'full_speed': False})

        self.assertEqual(status, 0)
        self.assertIs(opts.full_speed, True)


if __name__ == '__main__':
    unittest.main()
