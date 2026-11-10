"""Regression tests for the `rmbloat --logs` command.

The original handler assumed ``StructuredLogger.log_paths`` returned at least
two paths and unconditionally accessed ``files[1]``.  The logger actually
returns a single path, so ``rmbloat --logs`` always raised ``IndexError``.

These tests exercise the real ``main(['--logs'])`` path.  Third-party runtime
dependencies are stubbed so the tests run in a bare checkout, and ``HOME`` is
redirected to a temporary directory so no user configuration or logs are
touched.
"""

import contextlib
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies


class LogsCommandRegressionTests(unittest.TestCase):
    """`rmbloat --logs` must page the one existing log file, never crash."""

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

        # Import after HOME and the dependency stubs are in place: the module
        # creates its StructuredLogger at import time.
        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def test_logs_pages_the_single_existing_log_file(self):
        self.rmbloat.lg.info('regression entry for --logs')

        paths = self.rmbloat.lg.log_paths
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths, [str(self.rmbloat.lg.log_file)])
        self.assertTrue(os.path.isfile(paths[0]))

        # Pretend `less` is installed so the test does not depend on the host.
        with mock.patch.object(self.rmbloat.shutil, 'which',
                               return_value='/usr/bin/less'), \
                mock.patch('os.execvp', side_effect=SystemExit(0)) as execvp:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.rmbloat.main(['--logs'])

        self.assertEqual(raised.exception.code, 0)
        execvp.assert_called_once_with(
            'less', ['less', '-R', '+F', paths[0]])

    def test_logs_reports_missing_log_file_instead_of_raising_indexerror(self):
        log_path = str(self.rmbloat.lg.log_file)
        self.assertFalse(os.path.isfile(log_path))

        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch('os.execvp') as execvp:
            with contextlib.redirect_stderr(stderr), \
                    contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    self.rmbloat.main(['--logs'])

        self.assertEqual(raised.exception.code, 1)
        execvp.assert_not_called()
        self.assertIn('No log file found at:', stderr.getvalue())
        self.assertIn(log_path, stderr.getvalue())

    def test_logs_only_passes_existing_paths_to_less(self):
        self.rmbloat.lg.info('path-filtering regression entry')
        existing = str(self.rmbloat.lg.log_file)
        missing = str(Path(existing).with_name('missing.jsonl'))
        self.assertFalse(os.path.isfile(missing))

        with mock.patch.object(
                self.rmbloat.StructuredLogger, 'log_paths',
                new_callable=mock.PropertyMock,
                return_value=[existing, missing]):
            with mock.patch.object(self.rmbloat.shutil, 'which',
                                   return_value='/usr/bin/less'), \
                    mock.patch('os.execvp',
                               side_effect=SystemExit(0)) as execvp:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.rmbloat.main(['--logs'])

        # The missing second path must be filtered out, not indexed blindly.
        execvp.assert_called_once_with(
            'less', ['less', '-R', '+F', existing])

    def test_logs_requires_less_and_says_so(self):
        """A missing `less` must be reported clearly, not exec-attempted."""
        self.rmbloat.lg.info('entry that must not be paged')
        log_path = str(self.rmbloat.lg.log_file)

        stderr = io.StringIO()
        with mock.patch.object(self.rmbloat.shutil, 'which',
                               return_value=None), \
                mock.patch('os.execvp') as execvp, \
                contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.rmbloat.main(['--logs'])

        self.assertEqual(raised.exception.code, 1)
        execvp.assert_not_called()
        self.assertIn("'less' is required to view the logs", stderr.getvalue())
        self.assertIn('sudo apt install less', stderr.getvalue())
        self.assertIn(log_path, stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
