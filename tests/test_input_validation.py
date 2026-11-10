"""Regression tests for audit finding C4.

Startup crashed whenever any argument was not a probeable video: an audio-only
``.mp4``, a text file, or a stale saved default path.  ``batch_get_or_probe()``
returned a ``None`` probe, ``create_video_file_list()`` wrapped it in a
``PathProbePair``, and ``main_loop()`` sorted by ``probe.bloat`` and raised
``AttributeError``.

Three layers are covered here:

* ``ConvertUtils.get_candidate_video_files()`` drops immediate file arguments
  (including ones read from stdin) that are missing, not regular files, or not
  videos.
* ``ProbeCache.batch_get_or_probe()`` omits unprobeable files instead of
  returning ``None`` probes.
* ``Converter.main_loop()`` sorts defensively and ``Converter.process_one_ppp()``
  ignores a pair whose probe is ``None``.
"""

import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class _Dummy:
    """Minimal stand-in for console_window classes."""

    def __init__(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def stubbed_runtime_dependencies():
    """Install fake third-party modules for the duration of a test."""
    previous = {}

    def install(name, **attributes):
        previous[name] = sys.modules.get(name)
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    console_window = install('console_window')
    for name in ('ConsoleWindow', 'OptionSpinner', 'Screen', 'ScreenStack',
                 'ConsoleWindowOpts', 'IncrementalSearchBar', 'Context',
                 'Theme'):
        setattr(console_window, name, type(name, (_Dummy,), {}))

    ruamel = install('ruamel')
    ruamel_yaml = install('ruamel.yaml', YAML=_Dummy)
    ruamel.yaml = ruamel_yaml

    install('send2trash', send2trash=lambda *args, **kwargs: None)

    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


# `rmbloat.ConvertUtils` pulls in `rmbloat.VideoParser`, which imports
# `ruamel.yaml` at module import time.  Stub it so this file's tests run in a
# bare checkout; the stubs are removed again immediately afterwards and the
# module objects stay bound below.
with stubbed_runtime_dependencies():
    from rmbloat import ConvertUtils
    from rmbloat.ProbeCache import Probe, ProbeCache


def purge_rmbloat_modules():
    """Drop cached rmbloat modules so module-level paths use the temp HOME."""
    for name in list(sys.modules):
        if name == 'rmbloat' or name.startswith('rmbloat.'):
            sys.modules.pop(name, None)


class CandidateVideoFileTests(unittest.TestCase):
    """Immediate file arguments must be filtered before probing."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def make_file(self, name, payload=b'x'):
        path = self.root / name
        path.write_bytes(payload)
        return str(path)

    def test_missing_path_is_skipped(self):
        missing = str(self.root / 'gone.mkv')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            paths, read_pipe = ConvertUtils.get_candidate_video_files([missing])
        self.assertEqual(paths, [])
        self.assertFalse(read_pipe)
        self.assertIn('skipping missing path', stderr.getvalue())

    def test_non_video_file_is_skipped(self):
        notes = self.make_file('notes.txt')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            paths, _ = ConvertUtils.get_candidate_video_files([notes])
        self.assertEqual(paths, [])
        self.assertIn('skipping non-video file', stderr.getvalue())

    def test_video_file_is_kept(self):
        movie = self.make_file('movie.mkv')
        paths, _ = ConvertUtils.get_candidate_video_files([movie])
        self.assertEqual(paths, [movie])

    def test_mixed_arguments_keep_only_video_files(self):
        movie = self.make_file('movie.mp4')
        notes = self.make_file('notes.txt')
        missing = str(self.root / 'deleted.mkv')
        with contextlib.redirect_stderr(io.StringIO()):
            paths, _ = ConvertUtils.get_candidate_video_files(
                [missing, notes, movie])
        self.assertEqual(paths, [movie])

    def test_directory_scan_still_filters_by_extension(self):
        movie = self.make_file('a.mkv')
        self.make_file('notes.txt')
        paths, _ = ConvertUtils.get_candidate_video_files([str(self.root)])
        self.assertEqual(paths, [movie])

    def test_skip_prefixes_apply_to_absolute_directory_scan_paths(self):
        # Regression for C6: the prefix filter used to run against the full
        # absolute path, which never starts with 'TEMP.'/'ORIG.'/'SAMPLE.'.
        movie = self.make_file('good.mkv')
        for name in ('ORIG.foo.mkv', 'SAMPLE.foo.mkv', 'TEMP.foo.mkv'):
            self.make_file(name)
        paths, _ = ConvertUtils.get_candidate_video_files([str(self.root)])
        self.assertEqual(paths, [movie])

    def test_skip_prefixes_apply_to_absolute_file_arguments(self):
        movie = self.make_file('good.mkv')
        skipped = [self.make_file(name)
                   for name in ('ORIG.foo.mkv', 'SAMPLE.foo.mkv',
                                'TEMP.foo.mkv')]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            paths, _ = ConvertUtils.get_candidate_video_files(
                skipped + [movie])
        self.assertEqual(paths, [movie])
        self.assertIn('skipping non-video file', stderr.getvalue())

    def test_skip_prefix_check_uses_basename(self):
        self.assertFalse(ConvertUtils.is_valid_video_file(
            os.path.join(self.root, 'TEMP.foo.mkv')))
        self.assertFalse(ConvertUtils.is_valid_video_file(
            os.path.join(self.root, 'ORIG.foo.mkv')))
        self.assertFalse(ConvertUtils.is_valid_video_file(
            os.path.join(self.root, 'SAMPLE.foo.mkv')))
        self.assertTrue(ConvertUtils.is_valid_video_file(
            os.path.join(self.root, 'good.mkv')))
        # Prefixes only matter at the start of the basename.
        self.assertTrue(ConvertUtils.is_valid_video_file(
            os.path.join(self.root, 'TEMP', 'good.mkv')))


class BatchProbeNoneTests(unittest.TestCase):
    """`batch_get_or_probe()` must never hand callers a `None` probe."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache = ProbeCache(cache_file_name='probes.json',
                                cache_dir_name=self._tmpdir.name,
                                chooser=None)

    def test_missing_path_is_omitted_from_results(self):
        missing = os.path.join(self._tmpdir.name, 'audio_only.mp4')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            results = self.cache.batch_get_or_probe([missing])
        self.assertEqual(results, {})
        self.assertNotIn(missing, results)
        self.assertIn('Skipped 1 file(s)', stderr.getvalue())
        self.assertIn(missing, stderr.getvalue())

    def test_failed_probe_does_not_hide_cached_results(self):
        movie = os.path.join(self._tmpdir.name, 'good.mkv')
        with open(movie, 'wb') as handle:
            handle.write(b'data' * 10)
        size = os.path.getsize(movie)
        self.cache._set_cache(movie, Probe(size_bytes=size))  # pylint: disable=protected-access
        missing = os.path.join(self._tmpdir.name, 'gone.mkv')

        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            results = self.cache.batch_get_or_probe([movie, missing])

        self.assertIn(movie, results)
        self.assertIsNotNone(results[movie])
        self.assertNotIn(missing, results)


class MainLoopNoneProbeTests(unittest.TestCase):
    """`main_loop()` must survive a `PathProbePair` with a `None` probe."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        home_patch = mock.patch.dict(
            os.environ, {'HOME': self.temp_dir.name}, clear=False)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

    def make_ppp(self, name, bloat):
        probe = Probe(width=1920, height=1080)
        probe.bloat = bloat
        return self.rmbloat.PathProbePair(name, probe)

    def make_converter(self, ppps, files=()):
        converter = object.__new__(self.rmbloat.Converter)
        converter.opts = types.SimpleNamespace(files=list(files))
        converter.original_cwd = os.getcwd()
        converter.probe_cache = mock.Mock()
        converter.create_video_file_list = mock.Mock(return_value=ppps)
        converter.process_one_ppp = mock.Mock()
        converter.do_window_mode = mock.Mock()
        return converter

    def test_none_probe_does_not_break_sort(self):
        root = self.temp_dir.name
        good_low = self.make_ppp(os.path.join(root, 'low.mkv'), 100)
        good_high = self.make_ppp(os.path.join(root, 'high.mkv'), 900)
        unprobeable = self.rmbloat.PathProbePair(
            os.path.join(root, 'audio_only.mp4'), None)

        converter = self.make_converter([unprobeable, good_low, good_high])
        converter.main_loop()

        converter.do_window_mode.assert_called_once()
        processed = [call.args[0] for call in
                     converter.process_one_ppp.call_args_list]
        self.assertEqual(processed, [good_high, good_low, unprobeable])

    def test_no_probeable_files_reports_and_exits(self):
        converter = self.make_converter([], files=['/tmp/gone.mkv'])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                converter.main_loop()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('No probeable video files found', stderr.getvalue())
        self.assertIn('Usage: rmbloat', stderr.getvalue())

    def test_process_one_ppp_ignores_none_probe(self):
        converter = object.__new__(self.rmbloat.Converter)
        ppp = self.rmbloat.PathProbePair('/does/not/exist.mkv', None)
        converter.process_one_ppp(ppp)  # must not touch probe attributes


if __name__ == '__main__':
    unittest.main()
