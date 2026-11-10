"""Regression tests for numeric command-line range checks.

``--thread-cnt`` was a plain ``int`` argument.  A value above 256 was accepted
by the parser and only rejected inside ``FfmpegChooser.make_ffmpeg_cmd()``,
after the queue had started; the resulting ``ValueError`` escaped
``advance_jobs()`` and tore down the curses session, so an unattended run died
on a typo.  The numeric options are now validated while the command line is
parsed, which reports the problem and exits with argparse's status 2 before any
window or job exists.  ``-t`` also documents that the VAAPI path ignores it.
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


SAVED_DEFAULTS = {
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


class NumericOptionRangeTests(unittest.TestCase):
    """Out-of-range numbers must be rejected at parse time, not mid-queue."""

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

    def fake_ini(self):
        return SimpleNamespace(
            vals=SimpleNamespace(**SAVED_DEFAULTS),
            config_file_path=os.path.join(self._tmpdir.name, 'config.ini'),
            write=mock.Mock(return_value=True),
        )

    def run_main(self, args):
        """Run ``main(*args*)``, returning (exit_code, opts, Converter mock).

        The exit status is argparse's (2 for a usage error, 0 after ``--help``)
        or ``main()``'s return value.  ``opts`` is ``None`` when main exited
        before constructing the Converter.
        """
        converter = mock.Mock()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                self.rmbloat, 'IniManager', return_value=self.fake_ini()), \
                mock.patch.object(
                    self.rmbloat, 'Converter', return_value=converter) as conv_cls:
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                try:
                    status = self.rmbloat.main(args)
                    opts = conv_cls.call_args.args[0] if conv_cls.called else None
                except SystemExit as exc:
                    status, opts = exc.code, None
        self.stdout = stdout.getvalue()
        self.stderr = stderr.getvalue()
        return status, opts, conv_cls

    def assert_rejected(self, args, message):
        status, opts, conv_cls = self.run_main(args)
        self.assertEqual(status, 2, f'{args} should be a usage error')
        self.assertIsNone(opts)
        conv_cls.assert_not_called()
        self.assertIn(message, self.stderr)

    def test_thread_count_out_of_range_is_rejected(self):
        self.assert_rejected(['-t', '257'],
                             'thread count must be between 0 and 256')
        self.assert_rejected(['-t', '-1'],
                             'thread count must be between 0 and 256')
        self.assert_rejected(['-t', 'many'],
                             'thread count: expected an integer')

    def test_thread_count_upper_bound_is_accepted(self):
        status, opts, conv_cls = self.run_main(['-t', '256'])

        self.assertEqual(status, 0)
        self.assertEqual(opts.thread_cnt, 256)
        conv_cls.assert_called_once()

    def test_quality_above_51_is_rejected(self):
        self.assert_rejected(['-q', '52'],
                             'quality (CRF) must be between 0 and 51')

    def test_max_height_zero_is_rejected(self):
        self.assert_rejected(['-H', '0'],
                             'max height must be between 1 and 65536')

    def test_min_shrink_percent_above_100_is_rejected(self):
        self.assert_rejected(['-m', '101'],
                             'minimum shrink percent must be between 0 and 100')

    def test_thread_count_help_documents_the_hardware_behaviour(self):
        status, _opts, conv_cls = self.run_main(['--help'])

        self.assertEqual(status, 0)
        conv_cls.assert_not_called()
        help_text = ' '.join(self.stdout.split())
        self.assertIn('VAAPI hardware path ignores it', help_text)
        self.assertIn('range=0-256', help_text)


if __name__ == '__main__':
    unittest.main()
