"""Regression tests for chooser exception handling and test-output safety (M7).

[`_run_command()`](rmbloat/FfmpegChooser.py:93) used to catch only
``TimeoutExpired`` and ``FileNotFoundError``, so a ``PermissionError`` or any
other ``OSError`` from ``subprocess.run()`` escaped and aborted startup
detection.  ``real_world_tests()`` also unlinked a pre-existing
``test_<strategy>.mkv`` in the video directory, which could delete a user file.
"""

# pylint: disable=protected-access,missing-function-docstring,too-few-public-methods

import contextlib
import errno
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from rmbloat.FfmpegChooser import FfmpegChooser


def _result(returncode=0, stdout=b'', stderr=b''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class RunCommandErrorTests(unittest.TestCase):
    """`_run_command()` must turn every OS-level failure into ``None``."""

    @staticmethod
    def make_chooser(quiet=True):
        chooser = FfmpegChooser.__new__(FfmpegChooser)
        chooser.quiet = quiet
        return chooser

    def test_permission_error_returns_none_without_raising(self):
        chooser = self.make_chooser(quiet=False)
        stdout = io.StringIO()
        with mock.patch(
                'rmbloat.FfmpegChooser.subprocess.run',
                side_effect=PermissionError(errno.EACCES, 'Permission denied')), \
                contextlib.redirect_stdout(stdout):
            self.assertIsNone(chooser._run_command(['docker', 'info']))
        self.assertIn('Could not run docker', stdout.getvalue())

    def test_generic_oserror_returns_none(self):
        chooser = self.make_chooser()
        with mock.patch(
                'rmbloat.FfmpegChooser.subprocess.run',
                side_effect=OSError(errno.ENOMEM, 'Cannot allocate memory')):
            self.assertIsNone(chooser._run_command(['ffmpeg', '-version']))

    def test_quiet_mode_suppresses_the_warning(self):
        chooser = self.make_chooser(quiet=True)
        stdout = io.StringIO()
        with mock.patch(
                'rmbloat.FfmpegChooser.subprocess.run',
                side_effect=PermissionError(errno.EACCES, 'Permission denied')), \
                contextlib.redirect_stdout(stdout):
            self.assertIsNone(chooser._run_command(['docker', 'info']))
        self.assertEqual(stdout.getvalue(), '')

    def test_timeout_still_returns_none(self):
        chooser = self.make_chooser()
        with mock.patch(
                'rmbloat.FfmpegChooser.subprocess.run',
                side_effect=subprocess.TimeoutExpired('ffmpeg', 5)):
            self.assertIsNone(chooser._run_command(['ffmpeg', '-version']))

    def test_success_is_passed_through(self):
        chooser = self.make_chooser()
        expected = _result(returncode=0, stdout=b'ok')
        with mock.patch(
                'rmbloat.FfmpegChooser.subprocess.run', return_value=expected):
            self.assertIs(chooser._run_command(['ffmpeg', '-version']), expected)


class _DummyTestChooser:
    """Stand-in for the per-strategy chooser created by ``real_world_tests``."""

    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.namespace = None
        _DummyTestChooser.instances.append(self)

    def make_namespace(self, **kwargs):
        self.namespace = SimpleNamespace(**kwargs)
        return self.namespace

    def make_ffmpeg_cmd(self, params):
        # ffmpeg's last argument is the output file; the fake subprocess
        # writes that path to emulate a successful encode.
        return ['fake-ffmpeg', params.output_file]


def _fake_encode(cmd, **_kwargs):
    Path(cmd[-1]).write_bytes(b'encoded')
    return _result()


def _fake_timeout(cmd, **kwargs):
    Path(cmd[-1]).write_bytes(b'partial')
    raise subprocess.TimeoutExpired(cmd, kwargs.get('timeout', 1))


class RealWorldTestOutputTests(unittest.TestCase):
    """`real_world_tests()` must never delete or reuse an existing file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.video_dir = self.tmp_path / 'videos'
        self.video_dir.mkdir()
        self.output_dir = self.tmp_path / 'outputs'
        self.output_dir.mkdir()
        self.video = self.video_dir / 'movie.mkv'
        self.video.write_bytes(b'input')
        self.preexisting_system = self.output_dir / 'test_system_cpu.mkv'
        self.preexisting_system.write_bytes(b'user system data')
        self.preexisting_docker = self.video_dir / 'test_docker_cpu.mkv'
        self.preexisting_docker.write_bytes(b'user docker data')
        _DummyTestChooser.instances = []

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def make_chooser(**overrides):
        chooser = FfmpegChooser.__new__(FfmpegChooser)
        chooser.image = 'test-image:latest'
        chooser.image_ref = None
        chooser.runtime = None
        chooser.has_docker_acceleration = False
        chooser.system_ffmpeg_path = '/usr/bin/ffmpeg'
        chooser.has_system_acceleration = False
        chooser.strategy = 'system_cpu'
        chooser.quiet = True
        for key, value in overrides.items():
            setattr(chooser, key, value)
        return chooser

    def run_tests(self, chooser, side_effect):
        # Patch the class in the method's own globals rather than by import
        # path: other tests purge and re-import ``rmbloat.FfmpegChooser``, so
        # the module found in ``sys.modules`` may not be the one that defines
        # this bound method.
        chooser_globals = FfmpegChooser.real_world_tests.__globals__
        with mock.patch.dict(
                chooser_globals, {'FfmpegChooser': _DummyTestChooser}), \
                mock.patch('subprocess.run', side_effect=side_effect), \
                contextlib.redirect_stdout(io.StringIO()):
            return chooser.real_world_tests(
                str(self.video), duration=1, output_dir=str(self.output_dir))

    def test_system_strategy_preserves_user_file_and_cleans_its_own(self):
        chooser = self.make_chooser()

        results = self.run_tests(chooser, _fake_encode)

        self.assertTrue(results['system_cpu']['success'])
        self.assertEqual(
            self.preexisting_system.read_bytes(), b'user system data')
        output_file = Path(_DummyTestChooser.instances[-1].namespace.output_file)
        self.assertEqual(output_file.parent, self.output_dir)
        self.assertTrue(output_file.name.startswith('.rmbloat-test-system_cpu-'))
        self.assertNotEqual(output_file, self.preexisting_system)
        self.assertFalse(output_file.exists())

    def test_docker_strategy_writes_beside_video_and_preserves_user_file(self):
        chooser = self.make_chooser(
            runtime='docker', system_ffmpeg_path=None, strategy='docker_cpu')

        results = self.run_tests(chooser, _fake_encode)

        self.assertTrue(results['docker_cpu']['success'])
        self.assertEqual(
            self.preexisting_docker.read_bytes(), b'user docker data')
        output_file = Path(_DummyTestChooser.instances[-1].namespace.output_file)
        self.assertEqual(output_file.parent, self.video_dir)
        self.assertTrue(output_file.name.startswith('.rmbloat-test-docker_cpu-'))
        self.assertNotEqual(output_file, self.preexisting_docker)
        self.assertFalse(output_file.exists())

    def test_partial_output_is_removed_after_timeout(self):
        chooser = self.make_chooser()

        results = self.run_tests(chooser, _fake_timeout)

        self.assertEqual(results['system_cpu']['error'], 'Timeout')
        output_file = Path(_DummyTestChooser.instances[-1].namespace.output_file)
        self.assertFalse(output_file.exists())
        self.assertEqual(
            self.preexisting_system.read_bytes(), b'user system data')


if __name__ == '__main__':
    unittest.main()
