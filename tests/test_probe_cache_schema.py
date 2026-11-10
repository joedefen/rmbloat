"""Regression tests for probe-cache schema upgrades.

The write-only ``Probe.customs`` field was removed from the on-disk schema.
Existing caches still contain that key, so loading must drop obsolete fields
instead of rejecting the whole entry and forcing every video to be reprobed.
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from rmbloat.ProbeCache import ProbeCache


class ProbeCacheSchemaUpgradeTests(unittest.TestCase):
    """Obsolete cache keys must not invalidate otherwise-valid probes."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        # Suppress the atexit store/lock callbacks: they would run after the
        # temporary directory has been removed and print spurious errors.
        register_patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        register_patch.start()
        self.addCleanup(register_patch.stop)

    def _entry(self, size_bytes):
        return {
            'folder': self._tmpdir.name + '/',
            'last_seen': int(time.time()),
            'anomaly': None,
            'width': 1920,
            'height': 1080,
            'codec': 'h264',
            'bitrate': 5000,
            'fps': 23.976,
            'duration': 120.0,
            'size_bytes': size_bytes,
            'color_spt': 'bt709,~,~',
            'pix_fmt': 'yuv420p',
            'streams': [],
        }

    def test_obsolete_customs_key_is_dropped_and_entry_preserved(self):
        video_path = os.path.join(self._tmpdir.name, 'movie.mp4')
        with open(video_path, 'wb') as handle:
            handle.write(b'x' * 32)
        size = os.path.getsize(video_path)

        entry = self._entry(size)
        entry['customs'] = {'drop_subs': [1]}
        cache_path = os.path.join(self._tmpdir.name, 'probes.json')
        with open(cache_path, 'w', encoding='utf-8') as handle:
            json.dump({video_path: {str(size): entry}}, handle)

        cache = ProbeCache(cache_file_name='probes.json',
                           cache_dir_name=self._tmpdir.name,
                           chooser=None)
        self.addCleanup(cache._release_instance_lock)  # pylint: disable=protected-access

        key = os.path.normpath(video_path)
        self.assertIn(key, cache.cache_data)
        stored = cache.cache_data[key][str(size)]
        self.assertNotIn('customs', stored)
        self.assertEqual(stored['width'], 1920)

        # The retained entry must still deserialize into a usable Probe.
        probe = cache._load_probe_data(key, str(size))  # pylint: disable=protected-access
        self.assertEqual(probe.codec, 'h264')
        self.assertGreater(probe.bloat, 0)


if __name__ == '__main__':
    unittest.main()
