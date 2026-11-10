"""Regression tests for the ``rmbloatd`` duplicate-instance lock path.

The wrapper used to hardcode ``/tmp/video_probes.json.lock`` while the app
constructs ``ProbeCache`` with its config directory, producing
``~/.config/rmbloat/video_probes.json.lock``.  ``rmbloatd start`` therefore
never saw a running instance.  These tests pin the shared source of truth.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from rmbloat import rmbloatd
from rmbloat.IniManager import IniManager
from rmbloat.ProbeCache import ProbeCache, default_cache_dir, default_lock_path


class RmbloatdLockPathTests(unittest.TestCase):
    """The lock checked by ``rmbloatd`` must be the lock the app acquires."""

    def _expected_lock(self, home):
        return os.path.join(
            home, '.config', 'rmbloat', 'video_probes.json.lock')

    def test_default_lock_path_uses_config_dir(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}):
            self.assertEqual(default_lock_path(), self._expected_lock(home))
            self.assertNotEqual(
                default_lock_path(), '/tmp/video_probes.json.lock')

    def test_probe_cache_default_ctor_uses_same_lock_path(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}), \
                mock.patch.object(ProbeCache, '_acquire_instance_lock'), \
                mock.patch.object(ProbeCache, 'load'):
            cache = ProbeCache()
        self.assertEqual(cache.lock_path, self._expected_lock(home))

    def test_ini_manager_and_probe_cache_agree_on_directory(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}):
            self.assertEqual(
                str(IniManager.default_config_dir('rmbloat')),
                default_cache_dir())

    def test_main_exports_lock_path_to_shell_script(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}), \
                mock.patch.object(sys, 'argv', ['rmbloatd', 'start']), \
                mock.patch.object(rmbloatd.os, 'execve') as execve:
            rmbloatd.main()

        self.assertTrue(execve.called)
        child_env = execve.call_args.args[2]
        self.assertEqual(
            child_env['RMBLOAT_LOCK_FILE'], self._expected_lock(home))

    def test_shell_script_uses_injected_lock_path(self):
        script = rmbloatd.BASH_SCRIPT_CONTENT
        self.assertIn('RMBLOAT_LOCK_FILE', script)
        self.assertNotIn('/tmp/video_probes.json.lock', script)


if __name__ == '__main__':
    unittest.main()
