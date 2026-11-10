"""Regression tests for the ``rmbloatd`` duplicate-instance lock path.

The wrapper used to hardcode ``/tmp/video_probes.json.lock``.  The app now
keeps its private probe cache in ``$XDG_CACHE_HOME/rmbloat`` (``~/.cache`` by
default), so ``rmbloatd`` must derive the lock from that same directory.
These tests pin the shared source of truth.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from rmbloat.IniManager import IniManager
from rmbloat.ProbeCache import ProbeCache, default_cache_dir, default_lock_path

from rmbloat import rmbloatd


class RmbloatdLockPathTests(unittest.TestCase):
    """The lock checked by ``rmbloatd`` must be the lock the app acquires."""

    def _expected_lock(self, home):
        return os.path.join(
            home, '.cache', 'rmbloat', 'video_probes.json.lock')

    def test_default_lock_path_uses_xdg_cache_dir(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}, clear=True):
            self.assertEqual(default_lock_path(), self._expected_lock(home))
            self.assertNotEqual(
                default_lock_path(), '/tmp/video_probes.json.lock')
            self.assertNotEqual(
                default_lock_path(),
                os.path.join(home, '.config', 'rmbloat',
                             'video_probes.json.lock'))

    def test_probe_cache_default_ctor_uses_same_lock_path(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}, clear=True), \
                mock.patch.object(ProbeCache, '_acquire_instance_lock'), \
                mock.patch.object(ProbeCache, 'load'):
            cache = ProbeCache()
        self.assertEqual(cache.lock_path, self._expected_lock(home))

    def test_xdg_cache_home_overrides_config_dir(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as cache_home, \
                mock.patch.dict(os.environ,
                                {'HOME': home,
                                 'XDG_CACHE_HOME': cache_home},
                                clear=True):
            expected = os.path.join(
                cache_home, 'rmbloat', 'video_probes.json.lock')
            self.assertEqual(default_lock_path(), expected)
            self.assertNotEqual(
                default_cache_dir(),
                str(IniManager.default_config_dir('rmbloat')))

    def test_main_exports_lock_path_to_shell_script(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home}, clear=True), \
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
