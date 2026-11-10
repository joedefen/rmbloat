"""Regression tests for the status filter and History search refresh (M8/M9).

``make_lines()`` compared the raw status to ``'OK'``, but successful jobs are
stored as ``' OK'`` (leading space), so the ``-DUN`` filters never hid them.
``HistoryScreen.draw_screen()`` refreshed ``entries`` but only recomputed
``filtered_entries`` when the search box was empty, so an active search kept
showing a stale window when new log entries arrived.
"""

# pylint: disable=protected-access,missing-function-docstring

import importlib
import unittest
from collections import OrderedDict
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


class StatusFilterTests(RmbloatModuleTestCase):
    """``-DUN`` must hide every successful status, including ``' OK'``."""

    @staticmethod
    def make_vid(doit):
        return SimpleNamespace(
            doit=doit,
            doit_auto=doit,
            codec='h264',
            basename1=f'{doit.strip() or "blank"}.mkv',
            filepath=f'/videos/{doit.strip() or "blank"}.mkv',
            height=1080,
            res_ok=True,
            bloat_ok=False,
            codec_ok=False,
            duration=60.0,
            net='0%',
            bloat=1800,
            gb=1.0,
            probe0=SimpleNamespace(gb=1.0),
            probe1=None,
        )

    def make_converter(self, vids, filter_mode):
        converter = self.rmbloat.Converter.__new__(self.rmbloat.Converter)
        converter.vids = vids
        converter.todo_vids = []
        converter.job = None
        converter.search_re = None
        converter.spins = SimpleNamespace(
            filter=filter_mode, mangle=False, directory=False)
        converter.opts = SimpleNamespace(sample=False)
        return converter

    def test_dun_filter_hides_leading_space_success(self):
        vids = [
            self.make_vid(' OK'),
            self.make_vid('OK2'),
            self.make_vid('DUN'),
            self.make_vid('[X]'),
            self.make_vid('ERR'),
        ]
        converter = self.make_converter(vids, '-DUN')

        converter.make_lines()

        self.assertEqual(
            [vid.doit for vid in converter.visible_vids], ['[X]', 'ERR'])

    def test_dun_err_filter_hides_success_and_error(self):
        vids = [
            self.make_vid(' OK'),
            self.make_vid('OK3'),
            self.make_vid('ERR'),
            self.make_vid('[X]'),
        ]
        converter = self.make_converter(vids, '-DUN-ERR')

        converter.make_lines()

        self.assertEqual(
            [vid.doit for vid in converter.visible_vids], ['[X]'])

    def test_dun_err_unchecked_drops_everything_but_checked_or_progress(self):
        vids = [
            self.make_vid(' OK'),
            self.make_vid('ERR'),
            self.make_vid('[ ]'),
            self.make_vid('[X]'),
        ]
        converter = self.make_converter(vids, '-DUN-ERR-UnChk')

        converter.make_lines()

        self.assertEqual(
            [vid.doit for vid in converter.visible_vids], ['[X]'])


class HistorySearchRefreshTests(RmbloatModuleTestCase):
    """A window refresh must re-run the active search filter."""

    def make_screen(self, search_text):
        screen = self.rmbloat.HistoryScreen.__new__(self.rmbloat.HistoryScreen)
        screen.window_of_logs = None
        screen.window_state = None
        screen.entries = []
        screen.filtered_entries = []
        screen.search_matches = set()
        screen.expands = {}
        screen.search_bar = SimpleNamespace(text=search_text)
        return screen

    @staticmethod
    def fake_filter(entries, pattern, deep=False):
        matched = [entry for entry in entries if pattern in entry.message]
        matches = {entry.timestamp for entry in matched} if deep else set()
        return matched, matches

    def test_new_entries_are_filtered_while_search_is_active(self):
        old = SimpleNamespace(timestamp='2026-01-01T00:00:00', message='old')
        new = SimpleNamespace(timestamp='2026-01-02T00:00:00', message='needle')
        first_window = OrderedDict([(old.timestamp, old)])
        second_window = OrderedDict([(old.timestamp, old), (new.timestamp, new)])
        screen = self.make_screen('needle')
        screen.expands = {old.timestamp: 2, 'gone': 1}

        with mock.patch.object(
                self.rmbloat.lg, 'get_window_of_entries',
                return_value=(first_window, 'state1')), \
                mock.patch.object(
                    self.rmbloat.lg, 'refresh_window',
                    return_value=(second_window, 'state2')), \
                mock.patch.object(
                    self.rmbloat.lg, 'filter_entries',
                    side_effect=self.fake_filter):
            screen._refresh_entries()
            self.assertEqual(screen.filtered_entries, [])

            screen._refresh_entries()

        self.assertEqual(
            [entry.message for entry in screen.filtered_entries], ['needle'])
        self.assertEqual(screen.expands, {old.timestamp: 2})

    def test_cleared_search_shows_every_entry_after_refresh(self):
        entry = SimpleNamespace(timestamp='2026-01-01T00:00:00', message='old')
        window = OrderedDict([(entry.timestamp, entry)])
        screen = self.make_screen('')

        with mock.patch.object(
                self.rmbloat.lg, 'get_window_of_entries',
                return_value=(window, 'state')):
            screen._refresh_entries()

        self.assertEqual(screen.filtered_entries, [entry])
        self.assertEqual(screen.search_matches, set())


if __name__ == '__main__':
    unittest.main()
