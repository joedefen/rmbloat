"""Regression tests for predictable temp/copy paths (audit finding M5).

The probe cache used to write to ``<cache>.json.tmp`` and the History clipboard
fallback always wrote ``/tmp/rmbloat-export.txt``.  Both names are predictable
and, on a multi-user host, could be pre-created or replaced with a symlink.
They now use ``tempfile.mkstemp`` (mode 0600), and the History export lives in
the per-user XDG cache directory instead of shared ``/tmp``.
"""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rmbloat import FileOps
from rmbloat.ProbeCache import ProbeCache


class ProbeCacheSecureTempFileTests(unittest.TestCase):
    """Probe-cache writes must use a unique private sibling temp file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        register_patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        register_patch.start()
        self.addCleanup(register_patch.stop)
        self.cache = ProbeCache(cache_file_name='probes.json',
                                cache_dir_name=self._tmpdir.name,
                                chooser=None)
        self.addCleanup(self.cache._release_instance_lock)  # pylint: disable=protected-access

    def _temp_files(self):
        return sorted(Path(self._tmpdir.name).glob('.probes.json.*.tmp'))

    def test_store_uses_unique_0600_temp_file_in_cache_dir(self):
        seen = {}
        real_replace = os.replace

        def intercepting_replace(src, dst):
            seen['src'] = Path(src)
            seen['mode'] = stat.S_IMODE(Path(src).stat().st_mode)
            seen['content'] = json.loads(Path(src).read_text(encoding='utf-8'))
            real_replace(src, dst)

        now = int(time.time())
        expected = {'/videos/a.mkv': {'1': {'last_seen': now}}}
        self.cache.cache_data = {'/videos/a.mkv': {'1': {'last_seen': now}}}
        self.cache._dirty_count = 1  # pylint: disable=protected-access

        with mock.patch('rmbloat.ProbeCache.os.replace',
                        side_effect=intercepting_replace):
            self.cache.store()

        self.assertEqual(seen['src'].parent, Path(self._tmpdir.name))
        self.assertTrue(seen['src'].name.startswith('.probes.json.'))
        self.assertNotEqual(seen['src'].name, 'probes.json.tmp')
        self.assertEqual(seen['mode'], 0o600)
        self.assertEqual(seen['content'], expected)
        self.assertFalse(seen['src'].exists())
        self.assertEqual(self._temp_files(), [])
        self.assertTrue(Path(self.cache.cache_path).exists())

    def test_failed_replace_keeps_old_cache_and_cleans_temp(self):
        cache_path = Path(self.cache.cache_path)
        cache_path.write_text('{"old": {"1": {}}}', encoding='utf-8')
        self.cache.cache_data = {'/videos/b.mkv': {'1': {'last_seen': 1}}}
        self.cache._dirty_count = 1  # pylint: disable=protected-access

        with mock.patch('rmbloat.ProbeCache.os.replace',
                        side_effect=OSError('simulated disk failure')):
            self.cache.store()

        self.assertEqual(cache_path.read_text(encoding='utf-8'),
                         '{"old": {"1": {}}}')
        self.assertEqual(self._temp_files(), [])


class HistoryExportSecureFileTests(unittest.TestCase):
    """History copy exports must use a private XDG cache file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = mock.patch.dict(
            os.environ, {'XDG_CACHE_HOME': self._tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_export_is_unique_private_and_under_xdg_cache(self):
        first = FileOps.write_history_export('first entry')
        second = FileOps.write_history_export('second entry')

        expected_dir = Path(self._tmpdir.name) / 'rmbloat'
        self.assertEqual(Path(first).parent, expected_dir)
        self.assertEqual(Path(second).parent, expected_dir)
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).name.startswith('rmbloat-export-'))
        self.assertEqual(
            stat.S_IMODE(Path(first).stat().st_mode), 0o600)
        self.assertEqual(Path(first).read_text(encoding='utf-8'), 'first entry')
        self.assertEqual(Path(second).read_text(encoding='utf-8'),
                         'second entry')

    def test_export_dir_falls_back_to_home_cache(self):
        with mock.patch.dict(os.environ, {'HOME': self._tmpdir.name}):
            os.environ.pop('XDG_CACHE_HOME', None)
            self.assertEqual(
                FileOps.history_export_dir(),
                os.path.join(self._tmpdir.name, '.cache', 'rmbloat'))


if __name__ == '__main__':
    unittest.main()
