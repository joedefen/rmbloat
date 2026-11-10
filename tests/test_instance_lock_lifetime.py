"""Regression tests: the single-instance lock must outlive its owner.

``_release_instance_lock()`` used to unlock and then ``os.remove()`` the lock
path.  A process that had already opened the same inode could take a lock on
the now-unlinked file while a third process created a fresh lock file and
locked *that*, so two instances could run at once despite the documented
single-instance policy.  The ``flock`` is the lock and the file carries no
data, so releasing leaves the lock file in place -- which is also the state the
``rmbloatd`` staleness check expects.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from rmbloat.ProbeCache import ProbeCache


class InstanceLockLifetimeTests(unittest.TestCase):
    """Releasing the lock must not delete the lock file."""

    CACHE_NAME = 'probes.json'

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        # Keep atexit from holding the lock (and the temp dir) after the test.
        patcher = mock.patch('rmbloat.ProbeCache.atexit.register')
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_cache(self):
        cache = ProbeCache(cache_file_name=self.CACHE_NAME,
                           cache_dir_name=self._tmpdir.name,
                           chooser=None)
        self.addCleanup(cache._release_instance_lock)  # pylint: disable=protected-access
        return cache

    def test_release_leaves_the_lock_file_in_place(self):
        cache = self.make_cache()
        lock_path = cache.lock_path
        inode = os.stat(lock_path).st_ino
        self.assertTrue(os.path.exists(lock_path))

        cache._release_instance_lock()  # pylint: disable=protected-access

        # Same path, same inode: nothing unlinked it and nothing re-created it.
        self.assertTrue(os.path.exists(lock_path))
        self.assertEqual(os.stat(lock_path).st_ino, inode)

    def test_second_instance_is_refused_while_the_lock_is_held(self):
        first = self.make_cache()
        self.assertTrue(os.path.exists(first.lock_path))

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as raised:
                ProbeCache(cache_file_name=self.CACHE_NAME,
                           cache_dir_name=self._tmpdir.name,
                           chooser=None)

        self.assertEqual(raised.exception.code, 1)
        self.assertIn('Another rmbloat instance is already running',
                      stdout.getvalue())
        first._release_instance_lock()  # pylint: disable=protected-access

    def test_lock_file_is_reusable_after_release(self):
        first = self.make_cache()
        lock_path = first.lock_path
        first._release_instance_lock()  # pylint: disable=protected-access

        second = self.make_cache()

        self.assertEqual(second.lock_path, lock_path)
        self.assertTrue(os.path.exists(lock_path))

    def test_release_is_idempotent(self):
        cache = self.make_cache()

        cache._release_instance_lock()  # pylint: disable=protected-access
        cache._release_instance_lock()  # pylint: disable=protected-access

        self.assertTrue(os.path.exists(cache.lock_path))


if __name__ == '__main__':
    unittest.main()
