"""Regression tests: reopening the TTY must not close stdin.

``create_video_file_list()`` closes the piped ``sys.stdin`` before reopening
``/dev/tty``.  Closing stdin frees file descriptor 0, and ``os.open('/dev/tty')``
normally returns that same descriptor.  The old code then called
``os.close(tty_fd)`` unconditionally, which closed fd 0 and left ``sys.stdin``
backed by a closed descriptor.

These tests verify that fd 0 is never closed after the reopen, whether the TTY
lands on fd 0 (the common case) or on a distinct descriptor.
"""

import types
import unittest
from unittest import mock

from tests.runtime_stubs import stubbed_runtime_dependencies

with stubbed_runtime_dependencies():
    from rmbloat import rmbloat as RmbloatModule


class StdinRestoreTests(unittest.TestCase):
    """Reopening /dev/tty must leave fd 0 usable for the TUI."""

    def make_converter(self):
        converter = object.__new__(RmbloatModule.Converter)
        converter.opts = types.SimpleNamespace(files=['-'])
        converter.get_candidate_video_files = mock.Mock(return_value=([], True))
        converter.probe_cache = mock.Mock()
        converter.probe_cache.batch_get_or_probe.return_value = {}
        return converter

    def run_reopen(self, tty_fd):
        """Run the TTY-reopen path with *tty_fd* returned by os.open()."""
        converter = self.make_converter()
        fake_stdin = mock.Mock()
        fake_new_stdin = mock.Mock()
        with mock.patch.object(self._sys(), 'stdin', fake_stdin), \
                mock.patch.object(RmbloatModule.os, 'open',
                                  return_value=tty_fd) as open_mock, \
                mock.patch.object(RmbloatModule.os, 'dup2') as dup2_mock, \
                mock.patch.object(RmbloatModule.os, 'fdopen',
                                  return_value=fake_new_stdin) as fdopen_mock, \
                mock.patch.object(RmbloatModule.os, 'close') as close_mock:
            result = converter.create_video_file_list()
        return result, open_mock, dup2_mock, fdopen_mock, close_mock

    @staticmethod
    def _sys():
        return RmbloatModule.sys

    def test_tty_reusing_fd_zero_is_not_closed(self):
        result, open_mock, dup2_mock, fdopen_mock, close_mock = self.run_reopen(0)

        self.assertEqual(result, [])
        open_mock.assert_called_once_with('/dev/tty', RmbloatModule.os.O_RDONLY)
        dup2_mock.assert_not_called()
        fdopen_mock.assert_called_once_with(0, 'r')
        # The regression: the old code closed fd 0 here, breaking sys.stdin.
        close_mock.assert_not_called()

    def test_distinct_tty_fd_is_duplicated_then_closed(self):
        result, _open_mock, dup2_mock, fdopen_mock, close_mock = self.run_reopen(7)

        self.assertEqual(result, [])
        dup2_mock.assert_called_once_with(7, 0)
        fdopen_mock.assert_called_once_with(0, 'r')
        close_mock.assert_called_once_with(7)


if __name__ == '__main__':
    unittest.main()
