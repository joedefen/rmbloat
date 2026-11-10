"""Regression tests for private log/probe-cache locations.

The structured log and probe cache contain absolute media paths and ffmpeg
output.  They must live in per-user XDG directories and be inaccessible to
other users on the host, even when the process umask is permissive.
"""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rmbloat.ProbeCache import ProbeCache, default_cache_dir
from rmbloat.StructuredLogger import StructuredLogger, default_log_dir


class LogDirectoryPrivacyTests(unittest.TestCase):
    """Structured logs default to the XDG state directory with 0600 files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_default_log_dir_uses_xdg_state_home(self):
        state_home = Path(self._tmpdir.name) / 'state'
        with mock.patch.dict(
                os.environ,
                {'HOME': self._tmpdir.name, 'XDG_STATE_HOME': str(state_home)},
                clear=True):
            self.assertEqual(default_log_dir('rmbloat'), state_home / 'rmbloat')

    def test_default_log_dir_falls_back_to_home_state(self):
        with mock.patch.dict(
                os.environ, {'HOME': self._tmpdir.name}, clear=True):
            self.assertEqual(
                default_log_dir('rmbloat'),
                Path(self._tmpdir.name) / '.local' / 'state' / 'rmbloat')

    def test_log_directory_and_file_are_private(self):
        state_home = Path(self._tmpdir.name) / 'state'
        with mock.patch.dict(
                os.environ,
                {'HOME': self._tmpdir.name, 'XDG_STATE_HOME': str(state_home)},
                clear=True):
            logger = StructuredLogger(app_name='privacy_test')
            logger.event('hello')

        self.assertEqual(logger.log_dir, state_home / 'privacy_test')
        self.assertEqual(stat.S_IMODE(logger.log_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(logger.archive_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(logger.log_file.stat().st_mode), 0o600)
        self.assertIn('hello', logger.log_file.read_text(encoding='utf-8'))

    def test_existing_world_readable_log_is_tightened(self):
        log_dir = Path(self._tmpdir.name) / 'privacy_test'
        log_dir.mkdir(mode=0o755)
        log_file = log_dir / 'events.jsonl'
        log_file.write_text('', encoding='utf-8')
        os.chmod(log_file, 0o644)

        logger = StructuredLogger(
            app_name='privacy_test', log_dir=Path(self._tmpdir.name))
        logger.event('tightened')

        self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)

    def test_max_log_size_comment_matches_value(self):
        self.assertEqual(StructuredLogger.MAX_LOG_SIZE, 50 * 1024 * 1024)


class ProbeCachePrivacyTests(unittest.TestCase):
    """The probe cache defaults to a private XDG cache directory."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def make_cache(self):
        register_patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        register_patch.start()
        self.addCleanup(register_patch.stop)
        cache = ProbeCache()
        self.addCleanup(cache._release_instance_lock)  # pylint: disable=protected-access
        return cache

    def test_default_cache_dir_uses_xdg_cache_home(self):
        cache_home = Path(self._tmpdir.name) / 'cache'
        with mock.patch.dict(
                os.environ,
                {'HOME': self._tmpdir.name, 'XDG_CACHE_HOME': str(cache_home)},
                clear=True):
            self.assertEqual(
                default_cache_dir(), str(cache_home / 'rmbloat'))

    def test_default_cache_dir_falls_back_to_home_cache(self):
        with mock.patch.dict(
                os.environ, {'HOME': self._tmpdir.name}, clear=True):
            self.assertEqual(
                default_cache_dir(),
                os.path.join(self._tmpdir.name, '.cache', 'rmbloat'))

    def test_cache_dir_lock_and_file_are_private(self):
        cache_home = Path(self._tmpdir.name) / 'cache'
        with mock.patch.dict(
                os.environ,
                {'HOME': self._tmpdir.name, 'XDG_CACHE_HOME': str(cache_home)},
                clear=True):
            cache = self.make_cache()
            cache.cache_data = {
                '/videos/a.mkv': {
                    '1': {'last_seen': int(time.time())},
                },
            }
            cache._dirty_count = 1  # pylint: disable=protected-access
            cache.store()

        cache_dir = cache_home / 'rmbloat'
        self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(Path(cache.lock_path).stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(Path(cache.cache_path).stat().st_mode), 0o600)

    def test_existing_world_readable_cache_is_tightened(self):
        cache_home = Path(self._tmpdir.name) / 'cache'
        cache_dir = cache_home / 'rmbloat'
        cache_dir.mkdir(parents=True, mode=0o755)
        cache_file = cache_dir / 'video_probes.json'
        cache_file.write_text('{}', encoding='utf-8')
        os.chmod(cache_file, 0o644)

        with mock.patch.dict(
                os.environ,
                {'HOME': self._tmpdir.name, 'XDG_CACHE_HOME': str(cache_home)},
                clear=True):
            self.make_cache()

        self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
