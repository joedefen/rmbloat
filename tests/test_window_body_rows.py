"""Regression test for the console-window body-row hint.

``do_window_mode()`` used to request ``max(10 + len(vids), 10000)`` rows.
In console-window 2.2.2 ``body_rows`` is only an advisory hint: the pads are
viewport-sized and the content model grows on demand up to ``max_rows``.  The
hint should stay small even for collections with thousands of videos.
"""

import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies


class WindowBodyRowsTests(unittest.TestCase):
    """A large collection must not select a large upfront body hint."""

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

    def test_large_collection_uses_small_advisory_hint(self):
        converter = self.rmbloat.Converter.__new__(self.rmbloat.Converter)
        converter.opts = SimpleNamespace(theme=None, no_color=False)
        converter.persistent_state = mock.Mock()
        converter.vids = [SimpleNamespace() for _ in range(5000)]
        captured = {}

        def fake_window_opts(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

        class StopAfterOptions(Exception):
            """Abort do_window_mode() once the window options are built."""

        with mock.patch.object(
                self.rmbloat, 'ConsoleWindowOpts', side_effect=fake_window_opts), \
                mock.patch.object(
                    self.rmbloat, 'ConsoleWindow', side_effect=StopAfterOptions), \
                mock.patch.object(
                    self.rmbloat, 'settle_appearance',
                    return_value=('default', True)), \
                self.assertRaises(StopAfterOptions):
            converter.do_window_mode()

        self.assertEqual(captured['body_rows'], 128)


if __name__ == '__main__':
    unittest.main()
