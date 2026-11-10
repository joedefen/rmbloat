"""Regression tests: files with no video stream must be probed only once.

ffprobe succeeds on an audio-only ``.mp4``/``.mkv`` but reports no video
stream.  The file must still be omitted from the selection list, but the
verdict has to be *cached*: re-running ffprobe for every audio file on every
scan (each run carrying a 30-second timeout) made mixed music/video shares
painfully slow.  The stored verdict must also stay out of the caller's results,
so a non-video never becomes a row.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from rmbloat.ProbeCache import NOT_A_VIDEO_ANOMALY, ProbeCache
from rmbloat.Sandbox import CappedResult

from rmbloat import Sandbox

AUDIO_ONLY = json.dumps({
    'streams': [{'codec_type': 'audio', 'codec_name': 'aac'}],
    'format': {'format_name': 'mov,mp4', 'bit_rate': '128000', 'duration': '180.0'},
}).encode('utf-8')

VIDEO = json.dumps({
    'streams': [{
        'codec_type': 'video', 'codec_name': 'h264', 'width': 640,
        'height': 480, 'pix_fmt': 'yuv420p', 'r_frame_rate': '24/1',
    }],
    'format': {'format_name': 'matroska,webm', 'bit_rate': '900000',
               'duration': '60.0'},
}).encode('utf-8')


class NegativeProbeCacheTests(unittest.TestCase):
    """A "no video stream" verdict is durable, silent, and never a row."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        # Suppress the atexit store/lock callbacks: they would run after the
        # temporary directory has been removed and print spurious errors.
        register_patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        register_patch.start()
        self.addCleanup(register_patch.stop)
        self.cache = self.make_cache()

    def make_cache(self):
        cache = ProbeCache(cache_file_name='probes.json',
                           cache_dir_name=self._tmpdir.name,
                           chooser=None)
        self.addCleanup(cache._release_instance_lock)  # pylint: disable=protected-access
        return cache

    def write(self, basename, payload=b'data' * 16):
        path = os.path.join(self._tmpdir.name, basename)
        with open(path, 'wb') as handle:
            handle.write(payload)
        return path

    def run_ffprobe(self, payload, callable_):
        """Run *callable_* with ffprobe stubbed, returning (result, invocations)."""
        invocations = []

        def fake_run_capped(argv, **kwargs):
            invocations.append(argv)
            return CappedResult(returncode=0, stdout=payload, stderr=b'')

        with mock.patch.object(Sandbox, 'run_capped',
                               side_effect=fake_run_capped), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = callable_()
        return result, invocations

    def test_repeated_get_probes_an_audio_only_file_once(self):
        """The audit's case: two `get()` calls, one ffprobe invocation."""
        song = self.write('song.mp4')
        first, first_calls = self.run_ffprobe(
            AUDIO_ONLY, lambda: self.cache.get(song))
        second, second_calls = self.run_ffprobe(
            AUDIO_ONLY, lambda: self.cache.get(song))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(first_calls), 1)
        self.assertEqual(second_calls, [], 'the cached verdict must skip ffprobe')

    def test_negative_verdict_is_terminal_in_the_validator(self):
        """The lookup hits the cache, yet the entry is not a usable probe."""
        song = self.write('song.mp4')
        self.run_ffprobe(AUDIO_ONLY, lambda: self.cache.get(song))

        entry = self.cache._get_valid_entry(song)  # pylint: disable=protected-access
        self.assertIsNotNone(entry, 'the verdict must be served, not retried')
        self.assertEqual(entry.anomaly, NOT_A_VIDEO_ANOMALY)
        self.assertEqual(entry.width, 0)
        self.assertIsNone(self.cache.get(song))

    def test_negative_verdict_is_persisted_for_the_next_run(self):
        """A new process must honour the verdict written by the previous one."""
        song = self.write('song.mp4')
        self.run_ffprobe(AUDIO_ONLY, lambda: self.cache.get(song))
        self.cache.store()
        self.cache._release_instance_lock()  # pylint: disable=protected-access

        next_cache = self.make_cache()
        _, invocations = self.run_ffprobe(
            AUDIO_ONLY, lambda: next_cache.get(song))
        self.assertEqual(invocations, [])
        self.assertIsNone(next_cache.get(song))

    def test_changed_file_size_reprobes(self):
        """Replacing the file with different content must be re-examined."""
        song = self.write('song.mp4')
        self.run_ffprobe(AUDIO_ONLY, lambda: self.cache.get(song))
        with open(song, 'ab') as handle:
            handle.write(b'more')

        _, invocations = self.run_ffprobe(
            AUDIO_ONLY, lambda: self.cache.get(song))
        self.assertEqual(len(invocations), 1)

    def test_batch_omits_cached_non_videos_and_counts_them(self):
        """Cached skips are summarised once, never listed per file."""
        song = self.write('song.mp4')
        movie = self.write('movie.mkv')
        self.run_ffprobe(AUDIO_ONLY, lambda: self.cache.get(song))

        invocations = []

        def fake_run_capped(argv, **kwargs):
            invocations.append(argv)
            return CappedResult(returncode=0, stdout=VIDEO, stderr=b'')

        stderr = io.StringIO()
        with mock.patch.object(Sandbox, 'run_capped',
                               side_effect=fake_run_capped), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            results = self.cache.batch_get_or_probe([movie, song])

        self.assertEqual(list(results), [movie])
        self.assertIsNone(results[movie].anomaly)
        self.assertEqual(len(invocations), 1, 'only the video may be probed')
        report = stderr.getvalue()
        self.assertIn('already known to have no video stream', report)
        self.assertNotIn(song, report, 'cached skips must not be listed by path')

    def test_video_without_format_data_is_not_cached_as_missing_a_stream(self):
        """Only a genuine "no video stream" earns the durable verdict.

        A video stream without format data usually means a truncated or
        still-being-written file, which must stay undecided so a later run can
        succeed once the copy finishes.
        """
        movie = self.write('truncated.mkv')
        payload = json.dumps({
            'streams': [{'codec_type': 'video', 'codec_name': 'h264'}],
        }).encode('utf-8')

        self.run_ffprobe(payload, lambda: self.cache.get(movie))

        self.assertEqual(self.cache.cache_data, {})
