"""Regression tests for ``IniManager.write()`` persisting current values.

``write()`` used to iterate the constructor-supplied
``_default_values`` instead of the live ``self.vals`` attributes, so
``rmbloat --save-defaults`` printed "new defaults" that were never stored.
The same bug meant that any schema-mismatch rewrite (a new field added in a
later release) reset every existing user setting to its constructor default.

``HOME`` is redirected to a temporary directory for every test so no real
user configuration is read or written.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rmbloat.IniManager import IniManager


DEFAULTS = {
    'alpha': 'orig',
    'beta': 'default',
    'n': 1,
    'ratio': 1.5,
    'flag': False,
    'files': [],
}


class IniManagerWriteTests(unittest.TestCase):
    """`write()` must serialize the current `self.vals` state."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self._tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app_name = 'rmbloat_ini_test'

    def make_manager(self, **overrides):
        values = dict(DEFAULTS)
        values.update(overrides)
        return IniManager(app_name=self.app_name, **values)

    def test_write_persists_mutated_values(self):
        """Mutating `vals` then calling `write()` must hit the file."""
        manager = self.make_manager()
        manager.vals.beta = 'CHANGED'
        manager.vals.n = 42
        manager.vals.ratio = 2.25
        manager.vals.flag = True
        manager.vals.files = ['/videos/a.mkv', '/videos/b.mkv']

        manager.write()

        reloaded = self.make_manager()
        self.assertEqual(reloaded.vals.beta, 'CHANGED')
        self.assertEqual(reloaded.vals.n, 42)
        self.assertAlmostEqual(reloaded.vals.ratio, 2.25)
        self.assertIs(reloaded.vals.flag, True)
        self.assertEqual(reloaded.vals.files, ['/videos/a.mkv', '/videos/b.mkv'])

    def test_write_file_content_uses_current_values(self):
        """The on-disk INI must contain the new value, not the default."""
        manager = self.make_manager()
        manager.vals.beta = 'CHANGED'
        manager.write()

        text = Path(manager.config_file_path).read_text(encoding='utf-8')
        self.assertIn('beta = CHANGED', text)
        self.assertNotIn('beta = default', text)

    def test_write_includes_runtime_added_attributes(self):
        """Attributes added to `vals` at runtime are persisted too."""
        manager = self.make_manager()
        manager.vals.new_field = 'added'
        manager.write()

        reloaded = IniManager(
            app_name=self.app_name, new_field='fallback', **DEFAULTS)
        # Constructor does not know the field, but it is in the file.
        text = Path(reloaded.config_file_path).read_text(encoding='utf-8')
        self.assertIn('new_field = added', text)

    def test_schema_mismatch_rewrite_keeps_user_settings(self):
        """Adding a new constructor field must not reset existing values."""
        manager = self.make_manager()
        manager.vals.beta = 'CHANGED'
        manager.vals.n = 42
        manager.write()

        # Simulate a release that introduces one more config field.
        upgraded = IniManager(
            app_name=self.app_name, gamma='new-default', **DEFAULTS)

        self.assertEqual(upgraded.vals.beta, 'CHANGED')
        self.assertEqual(upgraded.vals.n, 42)
        self.assertEqual(upgraded.vals.gamma, 'new-default')

        # The rewrite should have persisted both old and new values.
        text = Path(upgraded.config_file_path).read_text(encoding='utf-8')
        self.assertIn('beta = CHANGED', text)
        self.assertIn('n = 42', text)
        self.assertIn('gamma = new-default', text)

    def test_schema_mismatch_drops_extra_file_keys(self):
        """Unknown keys in the file are dropped by the expected rewrite."""
        manager = self.make_manager()
        manager.write()

        config_path = Path(manager.config_file_path)
        text = config_path.read_text(encoding='utf-8')
        config_path.write_text(text + 'obsolete = 1\n', encoding='utf-8')

        rewritten = self.make_manager()
        self.assertFalse(hasattr(rewritten.vals, 'obsolete'))
        new_text = config_path.read_text(encoding='utf-8')
        self.assertNotIn('obsolete', new_text)


if __name__ == '__main__':
    unittest.main()
