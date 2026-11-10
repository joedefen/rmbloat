"""Regression tests for the medium-severity audit finding:

    Incomplete recycle-directory exclusion: only ``.Trash*``, ``Trash`` and
    ``lost+found`` were skipped, so Synology ``#recycle``, QNAP ``@Recycle``,
    ``.recycle`` and similar stores were traversed and their (deleted) media
    could be converted.

``ConvertUtils.is_excluded_directory()`` now centralises the check and
``get_candidate_video_files()`` uses it to prune ``os.walk()``.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _stub_runtime_dependencies():
    """Install minimal ``ruamel.yaml`` stubs needed to import ConvertUtils."""
    ruamel = types.ModuleType('ruamel')
    ruamel_yaml = types.ModuleType('ruamel.yaml')
    ruamel_yaml.YAML = lambda *args, **kwargs: types.SimpleNamespace()
    ruamel.yaml = ruamel_yaml
    return {'ruamel': ruamel, 'ruamel.yaml': ruamel_yaml}


with mock.patch.dict(sys.modules, _stub_runtime_dependencies()):
    from rmbloat import ConvertUtils


class ExcludedDirectoryNameTests(unittest.TestCase):
    """Direct unit tests for the name predicate."""

    EXCLUDED = (
        '.Trash',
        '.Trash-1000',
        '.Trashes',
        'Trash',
        'trash',
        'lost+found',
        '#recycle',
        '#RECYCLE',
        '#snapshot',
        '@Recycle',
        '@recycle',
        '@Recently-Snapshot',
        '@eaDir',
        '.@__thumb',
        '.recycle',
        '$RECYCLE.BIN',
        '$Recycle.Bin',
        'RECYCLER',
        'System Volume Information',
        '.snapshot',
    )

    INCLUDED = (
        'Movies',
        'TV Shows',
        'recycled-but-not-a-store',
        'MyTrash',
        'trashy',
        'lost+found-backup',
        '@eaDirBackup',
        'recycle',
    )

    def test_known_recycle_and_system_dirs_are_excluded(self):
        for name in self.EXCLUDED:
            with self.subTest(name=name):
                self.assertTrue(ConvertUtils.is_excluded_directory(name))

    def test_regular_dirs_are_not_excluded(self):
        for name in self.INCLUDED:
            with self.subTest(name=name):
                self.assertFalse(ConvertUtils.is_excluded_directory(name))


class WalkPruningTests(unittest.TestCase):
    """End-to-end check that pruned directories contribute no candidates."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _touch(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'')
        return str(path)

    def test_recycle_stores_are_not_traversed(self):
        wanted = self._touch('Movies', 'keep.mkv')
        for store in ('#recycle', '@Recycle', '.recycle', '$RECYCLE.BIN',
                      '.Trash-1000', 'lost+found', '@eaDir', '#snapshot'):
            self._touch(store, 'deleted.mkv')
        # And nested inside a normal tree, where os.walk would otherwise reach it
        self._touch('Movies', '#recycle', 'nested-deleted.mkv')
        self._touch('Movies', 'Sub', 'also-keep.mp4')

        paths, _ = ConvertUtils.get_candidate_video_files([str(self.root)])

        self.assertIn(wanted, paths)
        self.assertIn(str(self.root / 'Movies' / 'Sub' / 'also-keep.mp4'), paths)
        self.assertFalse(
            [p for p in paths if 'recycle' in p.lower() or '/@eaDir' in p
             or 'lost+found' in p or '$RECYCLE.BIN' in p
             or '.Trash' in p or '#snapshot' in p],
            msg=f'unexpected candidates: {paths}',
        )

    def test_case_variants_are_excluded(self):
        self._touch('Movies', 'keep.mkv')
        for store in ('#RECYCLE', '@RECYCLE', '$Recycle.Bin', '.tRaSh'):
            self._touch(store, 'deleted.mkv')

        paths, _ = ConvertUtils.get_candidate_video_files([str(self.root)])

        self.assertEqual(paths, [str(self.root / 'Movies' / 'keep.mkv')])


if __name__ == '__main__':
    unittest.main()
