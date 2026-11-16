"""Regression test for the console-window row-size hints.

console-window 3.x sizes the pads to the viewport and treats ``max_rows`` as
the only content ceiling, so ``head_rows``/``body_rows`` are no longer needed.
A large collection must not reintroduce an upfront row hint; the hard minimum
size is the only geometry the app should pass.
"""

import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies


class WindowRowHintTests(unittest.TestCase):
    """The app passes a minimum size, not advisory row counts."""

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

    def test_window_options_omit_advisory_row_counts(self):
        converter = self.rmbloat.Converter.__new__(self.rmbloat.Converter)
        converter.opts = SimpleNamespace(theme=None)
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
                    return_value='default'), \
                self.assertRaises(StopAfterOptions):
            converter.do_window_mode()

        self.assertNotIn('body_rows', captured)
        self.assertNotIn('head_rows', captured)
        self.assertEqual(captured['min_cols_rows'], (60, 10))


if __name__ == '__main__':
    unittest.main()
