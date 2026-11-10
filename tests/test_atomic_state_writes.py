"""Regression tests for atomic configuration/state writes.

Audit finding: ``PersistentState.save()`` and ``IniManager.write()`` used to
truncate and rewrite their target file in place.  A crash or power loss midway
through the write could leave ``state.json``/``config.ini`` truncated or
corrupt.  Both now write to a temporary file in the same directory and
``os.replace`` it into position, so the destination is always either the old
complete file or the new complete file.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rmbloat.IniManager import IniManager
from rmbloat.PersistentState import PersistentState


DEFAULTS = {
    'beta': 'default',
    'n': 1,
}


class IniManagerAtomicWriteTests(unittest.TestCase):
    """`IniManager.write()` must never leave a partially written config.ini."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self._tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app_name = 'rmbloat_atomic_test'

    def make_manager(self):
        return IniManager(app_name=self.app_name, **DEFAULTS)

    @staticmethod
    def _temp_files(directory):
        return sorted(
            path for path in Path(directory).iterdir()
            if path.name.endswith('.tmp'))

    def test_write_goes_through_temp_file_then_replace(self):
        manager = self.make_manager()
        config_path = Path(manager.config_file_path)
        original_text = config_path.read_text(encoding='utf-8')
        manager.vals.beta = 'CHANGED'

        seen = {}
        real_replace = os.replace

        def intercepting_replace(src, dst):
            seen['src'] = Path(src)
            seen['src_text'] = Path(src).read_text(encoding='utf-8')
            seen['dst_text'] = Path(dst).read_text(encoding='utf-8')
            real_replace(src, dst)

        with mock.patch('rmbloat.IniManager.os.replace',
                        side_effect=intercepting_replace):
            manager.write()

        # The replacement source is a sibling temp file that already holds the
        # complete new content, while the destination still holds the old one.
        self.assertEqual(seen['src'].parent, config_path.parent)
        self.assertIn('beta = CHANGED', seen['src_text'])
        self.assertEqual(seen['dst_text'], original_text)
        self.assertFalse(seen['src'].exists())

        # Final file is fully updated and no temp files linger.
        self.assertIn('beta = CHANGED',
                      config_path.read_text(encoding='utf-8'))
        self.assertEqual(self._temp_files(config_path.parent), [])

    def test_failed_replace_leaves_original_config_intact(self):
        manager = self.make_manager()
        config_path = Path(manager.config_file_path)
        original_text = config_path.read_text(encoding='utf-8')
        manager.vals.beta = 'CHANGED'

        with mock.patch('rmbloat.IniManager.os.replace',
                        side_effect=OSError('simulated disk failure')):
            manager.write()

        self.assertEqual(config_path.read_text(encoding='utf-8'),
                         original_text)
        self.assertEqual(self._temp_files(config_path.parent), [])


class PersistentStateAtomicWriteTests(unittest.TestCase):
    """`PersistentState.save()` must never truncate `state.json` in place."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_path = Path(self._tmpdir.name) / 'state.json'

    @staticmethod
    def _temp_files(directory):
        return sorted(
            path for path in Path(directory).iterdir()
            if path.name.endswith('.tmp'))

    def test_save_writes_complete_json_and_clears_dirty(self):
        self.state_path.write_text('{"theme": "original"}', encoding='utf-8')
        state = PersistentState(config_path=self.state_path)
        state.set('theme', 'updated')

        state.save()

        self.assertEqual(
            json.loads(self.state_path.read_text(encoding='utf-8')),
            {'theme': 'updated'})
        self.assertFalse(state.dirty)
        self.assertEqual(self._temp_files(self.state_path.parent), [])

    def test_failed_replace_keeps_original_state_and_dirty_flag(self):
        self.state_path.write_text('{"theme": "original"}', encoding='utf-8')
        state = PersistentState(config_path=self.state_path)
        state.set('theme', 'updated')
        original_text = self.state_path.read_text(encoding='utf-8')

        with mock.patch('rmbloat.PersistentState.os.replace',
                        side_effect=OSError('simulated disk failure')):
            state.save()

        self.assertEqual(
            self.state_path.read_text(encoding='utf-8'), original_text)
        self.assertTrue(state.dirty)
        self.assertEqual(self._temp_files(self.state_path.parent), [])


if __name__ == '__main__':
    unittest.main()
