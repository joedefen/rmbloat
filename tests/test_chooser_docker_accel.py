"""Regression tests for Docker acceleration parity.

The acceleration probe, the 10-bit probe, and real Docker encodes must all
run with the same ``--user uid:gid`` identity and the same supplementary
render/video groups.  Previously the probe ran as root, so it could pass while
real encodes failed on ``/dev/dri`` permissions, and the Docker path never
detected 10-bit VAAPI support at all.
"""

# pylint: disable=protected-access,missing-function-docstring,too-few-public-methods

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from rmbloat.FfmpegChooser import FfmpegChooser


def _group_lookup(values):
    """Build a fake ``grp.getgrnam`` that only knows the given groups."""

    def lookup(name):
        if name not in values:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=values[name])

    return lookup


class _FakeRun:
    """Record every probe command and return a successful result."""

    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, timeout=5, text_output=False):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.returncode, stdout='', stderr='')


def _group_add_values(cmd):
    return [cmd[i + 1] for i, value in enumerate(cmd) if value == '--group-add']


class DockerAccelerationParityTests(unittest.TestCase):
    """Probes and encodes must share identity, device, and 10-bit handling."""

    def make_chooser(self, **overrides):
        """Create a chooser without running hardware/runtime detection."""
        chooser = FfmpegChooser.__new__(FfmpegChooser)
        chooser.image = 'test-image:latest'
        chooser.runtime = 'docker'
        chooser.quiet = True
        chooser.render_device = '/dev/dri/renderD128'
        chooser.use_docker = True
        chooser.use_acceleration = True
        chooser.vaapi_supports_10bit = True
        chooser.system_ffmpeg_path = None
        chooser.has_system_acceleration = False
        chooser.has_docker_acceleration = True
        chooser.prefer_strategy = 'auto'
        chooser.strategy = 'docker_accel'
        for key, value in overrides.items():
            setattr(chooser, key, value)
        return chooser

    def test_group_args_include_render_video_and_device_owner(self):
        chooser = self.make_chooser()
        groups = {'render': 993, 'video': 44}
        with mock.patch('grp.getgrnam', side_effect=_group_lookup(groups)):
            args = chooser._docker_user_and_group_args('/dev/dri/renderD128')

        self.assertEqual(args[0], '--user')
        self.assertEqual(args[1], f'{os.getuid()}:{os.getgid()}')
        added = _group_add_values(args)
        self.assertIn('993', added)
        self.assertIn('44', added)

    def test_real_encode_uses_identity_groups_and_10bit(self):
        chooser = self.make_chooser()
        params = chooser.make_namespace(
            input_file='input.mp4',
            output_file='output.mkv',
            use_10bit=True,
            validate_input_exists=False,
        )
        groups = {'render': 993, 'video': 44}
        with mock.patch('grp.getgrnam', side_effect=_group_lookup(groups)):
            cmd = chooser.make_ffmpeg_cmd(params)

        self.assertIn('--user', cmd)
        self.assertIn('--device=/dev/dri:/dev/dri', cmd)
        self.assertIn('--group-add', cmd)
        self.assertIn('993', _group_add_values(cmd))
        self.assertEqual(cmd[cmd.index('-profile:v') + 1], 'main10')
        self.assertIn('format=p010le,hwupload', cmd)

    def test_container_10bit_probe_uses_user_and_groups(self):
        chooser = self.make_chooser(vaapi_supports_10bit=False)
        fake_run = _FakeRun()
        groups = {'render': 993, 'video': 44}

        with mock.patch('grp.getgrnam', side_effect=_group_lookup(groups)):
            with mock.patch.object(chooser, '_run_command', side_effect=fake_run):
                supported = chooser._detect_vaapi_10bit_support(container=True)

        self.assertTrue(supported)
        cmd = fake_run.calls[-1]
        self.assertEqual(cmd[0], 'docker')
        self.assertIn('--user', cmd)
        self.assertIn('--group-add', cmd)
        self.assertIn('--device=/dev/dri:/dev/dri', cmd)
        self.assertEqual(cmd[cmd.index('-profile:v') + 1], 'main10')

    def test_acceleration_probe_matches_real_encode_identity(self):
        chooser = self.make_chooser(
            vaapi_supports_10bit=False,
            has_docker_acceleration=False,
        )
        fake_run = _FakeRun()
        groups = {'render': 993, 'video': 44}

        with mock.patch('pathlib.Path.exists', return_value=True):
            with mock.patch.object(
                    chooser, '_find_render_device',
                    return_value='/dev/dri/renderD128'):
                with mock.patch('grp.getgrnam', side_effect=_group_lookup(groups)):
                    with mock.patch.object(
                            chooser, '_run_command', side_effect=fake_run):
                        chooser._test_docker_acceleration()

        self.assertTrue(chooser.has_docker_acceleration)
        # The probe returned rc=0, so the container 10-bit probe also runs
        # and succeeds.
        self.assertTrue(chooser.vaapi_supports_10bit)

        accel_cmd, ten_bit_cmd = fake_run.calls[0], fake_run.calls[1]
        self.assertIn('--user', accel_cmd)
        self.assertIn('--group-add', accel_cmd)
        self.assertIn('--user', ten_bit_cmd)
        self.assertIn('--group-add', ten_bit_cmd)
        self.assertEqual(ten_bit_cmd[ten_bit_cmd.index('-profile:v') + 1], 'main10')


    def test_acceleration_probe_skips_10bit_when_system_cpu_wins(self):
        chooser = self.make_chooser(
            vaapi_supports_10bit=False,
            has_docker_acceleration=False,
            system_ffmpeg_path='/usr/bin/ffmpeg',
            has_system_acceleration=False,
        )
        fake_run = _FakeRun()

        with mock.patch('pathlib.Path.exists', return_value=True):
            with mock.patch.object(
                    chooser, '_find_render_device',
                    return_value='/dev/dri/renderD128'):
                with mock.patch.object(
                        chooser, '_run_command', side_effect=fake_run):
                    chooser._test_docker_acceleration()

        self.assertTrue(chooser.has_docker_acceleration)
        # Docker acceleration cannot win while system ffmpeg exists, so the
        # extra 10-bit container probe must not run.
        self.assertFalse(chooser.vaapi_supports_10bit)
        self.assertEqual(len(fake_run.calls), 1)


if __name__ == '__main__':
    unittest.main()
