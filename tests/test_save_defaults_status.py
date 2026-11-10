"""Regression tests for ``--save-defaults`` reporting whether it persisted.

``main()`` printed the new defaults, called ``ini.write()`` and then
``sys.exit(0)``.  ``IniManager.write()`` swallowed every exception, so a full
disk or a read-only config directory still produced exit status 0 even though
nothing had been saved -- scripts, cron and CI could not tell the difference.
``write()`` now returns a boolean and ``main()`` propagates a failure into the
process exit status.
"""

import contextlib
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rmbloat.IniManager import IniManager

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies

# The configuration fields ``main()`` seeds an IniManager with.  The fake
# manager in the CLI tests must expose the same keys because --save-defaults
# copies every ``vars(vals)`` attribute from the parsed options.
DEFAULTS = {
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


class IniWriteStatusTests(unittest.TestCase):
    """`IniManager.write()` must report success and failure to its caller."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self._tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_manager(self):
        return IniManager(app_name='rmbloat_write_status_test', **DEFAULTS)

    def test_successful_write_returns_true(self):
        manager = self.make_manager()
        manager.vals.quality = 22

        with contextlib.redirect_stdout(io.StringIO()):
            result = manager.write()

        self.assertIs(result, True)
        self.assertIn('quality = 22',
                      Path(manager.config_file_path).read_text(encoding='utf-8'))

    def test_failed_replace_returns_false_and_reports_on_stderr(self):
        manager = self.make_manager()
        manager.vals.quality = 22
        original_text = Path(manager.config_file_path).read_text(encoding='utf-8')

        with mock.patch('rmbloat.IniManager.os.replace',
                        side_effect=OSError('simulated disk failure')):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = manager.write()

        self.assertIs(result, False)
        self.assertIn('Failed to write config file', stderr.getvalue())
        self.assertEqual(
            Path(manager.config_file_path).read_text(encoding='utf-8'),
            original_text)


class SaveDefaultsExitStatusTests(unittest.TestCase):
    """`main(['--save-defaults'])` must fail loudly when nothing was saved."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        home_patch = mock.patch.dict(
            os.environ, {'HOME': self._tmpdir.name}, clear=False)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def fake_ini(self, write_result):
        return SimpleNamespace(
            vals=SimpleNamespace(**DEFAULTS),
            config_file_path=os.path.join(self._tmpdir.name, 'config.ini'),
            write=mock.Mock(return_value=write_result),
        )

    def run_save_defaults(self, write_result):
        ini = self.fake_ini(write_result)
        with mock.patch.object(
                self.rmbloat, 'IniManager', return_value=ini), \
                mock.patch.object(self.rmbloat, 'Converter') as conv_cls:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    self.rmbloat.main(['--save-defaults'])
        return raised.exception.code, ini, conv_cls, stderr.getvalue()

    def test_failed_write_exits_nonzero(self):
        code, ini, conv_cls, stderr = self.run_save_defaults(False)

        self.assertEqual(code, 1)
        ini.write.assert_called_once_with()
        # A failed save must not fall through into a conversion run.
        conv_cls.assert_not_called()
        self.assertIn('not saved', stderr)

    def test_successful_write_exits_zero(self):
        code, ini, conv_cls, stderr = self.run_save_defaults(True)

        self.assertEqual(code, 0)
        ini.write.assert_called_once_with()
        conv_cls.assert_not_called()
        self.assertEqual(stderr, '')


if __name__ == '__main__':
    unittest.main()
