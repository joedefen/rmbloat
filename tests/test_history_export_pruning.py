"""Regression tests: the History clipboard fallback must not grow forever.

``write_history_export()`` wrote a new uniquely named ``rmbloat-export-*.txt``
into the cache directory on every copy and nothing ever removed the previous
ones.  Each file holds at least one full log entry -- media paths and ffmpeg
output -- so a session spent inspecting History accumulated them without bound.
Every new export now prunes all but the newest few, and anything older than a
week, without following or deleting anything it did not create.
"""

# pylint: disable=protected-access,missing-function-docstring

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rmbloat import FileOps


class HistoryExportPruningTests(unittest.TestCase):
    """A copy must stay possible while old exports are still cleaned up."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patch = mock.patch.dict(
            os.environ,
            {'HOME': self._tmpdir.name, 'XDG_CACHE_HOME': self._tmpdir.name},
            clear=False)
        patch.start()
        self.addCleanup(patch.stop)

    @property
    def export_dir(self):
        return Path(FileOps.history_export_dir())

    def exports(self):
        prefix, suffix = FileOps.HISTORY_EXPORT_PREFIX, FileOps.HISTORY_EXPORT_SUFFIX
        return sorted(path.name for path in self.export_dir.iterdir()
                      if path.name.startswith(prefix) and path.name.endswith(suffix))

    def write(self, text='entry'):
        return FileOps.write_history_export(text)

    def test_exports_are_bounded_and_the_newest_survives(self):
        limit = FileOps.HISTORY_EXPORT_MAX_FILES
        paths = [self.write(f'entry {index}') for index in range(limit + 5)]

        self.assertEqual(len(self.exports()), limit)
        self.assertTrue(os.path.exists(paths[-1]),
                        'the export just written must never be pruned')

    def test_stale_exports_are_removed_even_when_the_count_is_small(self):
        stale = Path(self.write('old entry'))
        old = time.time() - FileOps.HISTORY_EXPORT_MAX_AGE_SECS - 60
        os.utime(stale, (old, old))

        fresh = Path(self.write('new entry'))

        self.assertFalse(stale.exists(), 'an export older than max_age must go')
        self.assertTrue(fresh.exists())

    def test_unrelated_entries_are_never_removed(self):
        self.write('first')
        notes = self.export_dir / 'notes.txt'
        notes.write_text('keep me', encoding='utf-8')
        other_suffix = self.export_dir / 'rmbloat-export-keep.json'
        other_suffix.write_text('keep me too', encoding='utf-8')
        directory = self.export_dir / (
            f'{FileOps.HISTORY_EXPORT_PREFIX}dir{FileOps.HISTORY_EXPORT_SUFFIX}')
        directory.mkdir()

        for _ in range(FileOps.HISTORY_EXPORT_MAX_FILES + 3):
            self.write('entry')

        self.assertTrue(notes.exists())
        self.assertTrue(other_suffix.exists())
        self.assertTrue(directory.is_dir())

    def test_a_symlink_is_not_followed_or_deleted(self):
        self.write('first')
        target = self.export_dir / 'plain-target.txt'
        target.write_text('original', encoding='utf-8')
        link = self.export_dir / (
            f'{FileOps.HISTORY_EXPORT_PREFIX}link{FileOps.HISTORY_EXPORT_SUFFIX}')
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are unavailable here')

        for _ in range(FileOps.HISTORY_EXPORT_MAX_FILES + 3):
            self.write('entry')

        self.assertTrue(link.is_symlink(), 'a planted symlink must be left alone')
        self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_pruning_failure_never_breaks_a_copy(self):
        for _ in range(FileOps.HISTORY_EXPORT_MAX_FILES + 3):
            self.write('entry')

        with mock.patch.object(FileOps.os, 'unlink', side_effect=OSError('denied')):
            path = self.write('last entry')

        self.assertTrue(os.path.exists(path))
        self.assertEqual(Path(path).read_text(encoding='utf-8'), 'last entry')

    def test_prune_reports_how_many_files_it_removed(self):
        for _ in range(4):
            self.write('entry')

        removed = FileOps._prune_history_exports(
            self.export_dir, keep=1, max_age=10 ** 9)

        self.assertEqual(removed, 3)
        self.assertEqual(len(self.exports()), 1)


if __name__ == '__main__':
    unittest.main()
