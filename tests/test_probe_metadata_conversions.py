"""Regression tests for ffprobe metadata conversion handling in ProbeCache.

ffprobe reports odd metadata for unusual/truncated files: numbers may arrive as
strings, be missing entirely, or be placeholders such as ``'N/A'``.  Those
values must not escape as ``TypeError``/``ValueError`` from
``_get_metadata_with_ffprobe()``; the probe should either succeed with a
sensible default or fall through to the normal probe-failure bookkeeping.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from rmbloat import Sandbox
from rmbloat.ProbeCache import ProbeCache, Probe
from rmbloat.Sandbox import CappedResult


def _ffprobe_result(payload):
    """Build a stand-in for ``Sandbox.run_capped()``'s result."""
    return CappedResult(
        returncode=0,
        stdout=json.dumps(payload).encode('utf-8'),
        stderr=b'',
    )


class ProbeMetadataConversionTests(unittest.TestCase):
    """Unconvertible metadata must not crash the probe."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        # Suppress the atexit store/lock callbacks: they would run after the
        # temporary directory has been removed and print spurious errors.
        register_patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        register_patch.start()
        self.addCleanup(register_patch.stop)
        self.cache = ProbeCache(cache_file_name='probes.json',
                                cache_dir_name=self._tmpdir.name,
                                chooser=None)
        self.addCleanup(self.cache._release_instance_lock)  # pylint: disable=protected-access
        self.video = os.path.join(self._tmpdir.name, 'odd.mp4')
        with open(self.video, 'wb') as handle:
            handle.write(b'data' * 10)

    def _probe(self, payload):
        with mock.patch.object(Sandbox, 'run_capped',
                               return_value=_ffprobe_result(payload)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return self.cache._get_metadata_with_ffprobe(self.video)  # pylint: disable=protected-access

    @staticmethod
    def _payload(video=None, fmt=None):
        stream = {'codec_type': 'video', 'codec_name': 'h264'}
        stream.update(video or {})
        meta_format = {'format_name': 'mov,mp4'}
        meta_format.update(fmt or {})
        return {'streams': [stream], 'format': meta_format}

    def test_placeholder_and_missing_numbers_default_to_zero(self):
        """``'N/A'``/``None``/missing fields probe successfully as 0."""
        meta = self._probe(self._payload(
            video={'width': 'N/A', 'height': None},
            fmt={'bit_rate': 'N/A'},
        ))
        self.assertIsInstance(meta, Probe)
        self.assertIsNone(meta.anomaly)
        self.assertEqual(meta.width, 0)
        self.assertEqual(meta.height, 0)
        self.assertEqual(meta.bitrate, 0)
        self.assertEqual(meta.duration, 0.0)

    def test_numeric_strings_and_float_strings_are_converted(self):
        """Digit strings (and float-shaped strings) parse instead of raising."""
        meta = self._probe(self._payload(
            video={'width': '1920', 'height': '1080.0',
                   'r_frame_rate': '24000/1001'},
            fmt={'bit_rate': '4000000', 'duration': '123.456'},
        ))
        self.assertIsInstance(meta, Probe)
        self.assertIsNone(meta.anomaly)
        self.assertEqual(meta.width, 1920)
        self.assertEqual(meta.height, 1080)
        self.assertEqual(meta.bitrate, 4000)
        self.assertAlmostEqual(meta.duration, 123.456, places=3)
        self.assertAlmostEqual(meta.fps, 23.976, places=3)

    def test_numeric_json_types_are_accepted(self):
        """Real numbers and integers (not just strings) convert cleanly."""
        meta = self._probe(self._payload(
            video={'width': 640, 'height': 360.0},
            fmt={'bit_rate': 500000, 'duration': 12},
        ))
        self.assertIsInstance(meta, Probe)
        self.assertEqual(meta.width, 640)
        self.assertEqual(meta.height, 360)
        self.assertEqual(meta.bitrate, 500)
        self.assertEqual(meta.duration, 12.0)

    def test_unexpected_metadata_shape_uses_failure_path(self):
        """A non-iterable ``streams`` value records a probe failure, not a crash."""
        payload = self._payload()
        payload['streams'] = 5  # not a list -> ValueError inside the probe
        meta = self._probe(payload)
        self.assertIsInstance(meta, Probe)
        self.assertEqual(meta.anomaly, '?P1')
        self.assertEqual(meta.width, 0)
        self.assertEqual(meta.size_bytes, os.path.getsize(self.video))

    def test_non_object_metadata_uses_failure_path(self):
        """A JSON array where an object was expected must not raise."""
        meta = self._probe([{'codec_type': 'video'}])
        self.assertIsInstance(meta, Probe)
        self.assertEqual(meta.anomaly, '?P1')

    def test_malformed_stream_entries_are_ignored(self):
        """Non-dict stream entries are skipped instead of breaking the probe."""
        payload = self._payload(video={'width': 1280, 'height': 720})
        payload['streams'] = [None, 'bogus', payload['streams'][0]]
        meta = self._probe(payload)
        self.assertIsInstance(meta, Probe)
        self.assertIsNone(meta.anomaly)
        self.assertEqual(meta.width, 1280)
        self.assertEqual(meta.height, 720)
        self.assertEqual(meta.streams, ['video:h264'])

    def test_helpers_are_total_functions(self):
        """`_as_int`/`_as_float` never raise, even on hostile input."""
        for value in ('N/A', '', None, b'\x00', object(), [1]):
            self.assertEqual(ProbeCache._as_int(value), 0)  # pylint: disable=protected-access
            self.assertEqual(ProbeCache._as_float(value), 0.0)  # pylint: disable=protected-access
        self.assertEqual(ProbeCache._as_int('7.9'), 7)  # pylint: disable=protected-access
        self.assertEqual(ProbeCache._as_int(None, 5), 5)  # pylint: disable=protected-access
        self.assertEqual(ProbeCache._as_float('1.5', 2.0), 1.5)  # pylint: disable=protected-access
        self.assertEqual(ProbeCache._as_float('nope', 2.0), 2.0)  # pylint: disable=protected-access


if __name__ == '__main__':
    unittest.main()
