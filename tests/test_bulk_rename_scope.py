"""Regression tests for companion-file rename scope (audit low-severity).

``bulk_rename()`` must recurse into directories that share the video's
basename (such as ``movie.cache/``) and rename matching contents there, but it
must not touch unrelated subdirectories.  It must also refuse to overwrite an
existing destination instead of letting ``os.rename()`` clobber it.
"""

import tempfile
import unittest
from pathlib import Path

from rmbloat import FileOps

OLD = 'Dexter.Resurrection.S01E01.A.Beating.Heart.1080p.HEVC.x265-MeGusta'
NEW = 'Dexter.Resurrection.s01e01.1080p.x265-cmf28.recode'


class BulkRenameScopeTests(unittest.TestCase):
    """Companion renames follow matching basenames and never clobber."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.old_video = self.root / f'{OLD}.mkv'
        self.new_video = self.root / f'{NEW}.mkv'
        self.old_video.write_bytes(b'video')

    def touch(self, relative_path, payload=b'x'):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_matching_cache_dir_is_renamed_and_contents_recurse(self):
        cache = self.root / f'{OLD}.cache'
        cache.mkdir()
        self.touch(f'{OLD}.en.srt', b'subs')
        self.touch(f'{OLD}.cache/{OLD}.EMBEDDED.srt', b'embedded')
        self.touch(f'{OLD}.cache/{OLD}.REFERENCE.srt', b'reference')
        self.touch(f'{OLD}.cache/probe-info.yaml', b'probe')
        self.touch(f'{OLD}.cache/quirk.SCORE.03', b'score')

        ops = FileOps.bulk_rename(str(self.old_video), str(self.new_video), set())

        self.assertTrue((self.root / f'{NEW}.en.srt').exists())
        self.assertFalse((self.root / f'{OLD}.en.srt').exists())
        new_cache = self.root / f'{NEW}.cache'
        self.assertTrue(new_cache.is_dir())
        self.assertFalse(cache.exists())
        self.assertEqual((new_cache / f'{NEW}.EMBEDDED.srt').read_bytes(),
                         b'embedded')
        self.assertEqual((new_cache / f'{NEW}.REFERENCE.srt').read_bytes(),
                         b'reference')
        self.assertEqual((new_cache / 'probe-info.yaml').read_bytes(), b'probe')
        self.assertEqual((new_cache / 'quirk.SCORE.03').read_bytes(), b'score')
        self.assertTrue(any(op.startswith('rename ') for op in ops))

    def test_unrelated_subdirectories_are_not_renamed(self):
        unrelated = self.touch(f'unrelated/{OLD}.en.srt', b'unrelated')
        self.touch('other/movie.en.srt', b'other')

        FileOps.bulk_rename(str(self.old_video), str(self.new_video), set())

        self.assertEqual(unrelated.read_bytes(), b'unrelated')
        self.assertFalse((self.root / 'unrelated' / f'{NEW}.en.srt').exists())
        self.assertTrue((self.root / 'other' / 'movie.en.srt').exists())

    def test_nested_matching_directories_recurse(self):
        self.touch(f'{OLD}.cache/{OLD}.cache/{OLD}.EMBEDDED.srt', b'deep')

        FileOps.bulk_rename(str(self.old_video), str(self.new_video), set())

        deep = self.root / f'{NEW}.cache' / f'{NEW}.cache' / f'{NEW}.EMBEDDED.srt'
        self.assertEqual(deep.read_bytes(), b'deep')
        self.assertFalse((self.root / f'{OLD}.cache').exists())

    def test_existing_destination_is_not_overwritten(self):
        source = self.touch(f'{OLD}.en.srt', b'source')
        destination = self.touch(f'{NEW}.en.srt', b'destination')

        ops = FileOps.bulk_rename(str(self.old_video), str(self.new_video), set())

        self.assertEqual(source.read_bytes(), b'source')
        self.assertEqual(destination.read_bytes(), b'destination')
        self.assertTrue(any(op.startswith('ERROR: refusing to overwrite')
                            for op in ops))

    def test_trashed_basename_is_skipped(self):
        source = self.touch(f'{OLD}.en.srt', b'source')

        FileOps.bulk_rename(
            str(self.old_video), str(self.new_video), {source.name})

        self.assertEqual(source.read_bytes(), b'source')
        self.assertFalse((self.root / f'{NEW}.en.srt').exists())

    def test_similar_but_nonmatching_names_are_untouched(self):
        prefixed = self.touch(f'prefix.{OLD}.en.srt', b'prefix')
        suffixed = self.touch(f'{OLD}-extra.en.srt', b'suffix')

        FileOps.bulk_rename(str(self.old_video), str(self.new_video), set())

        self.assertTrue(prefixed.exists())
        self.assertTrue(suffixed.exists())


if __name__ == '__main__':
    unittest.main()
