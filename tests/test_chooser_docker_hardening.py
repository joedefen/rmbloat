"""Regression tests for Docker hardening/portability (audit finding M6).

The container invocation used ``--net=host --ipc=host``, bind mounts lacked an
SELinux ``:z`` label, and the image was pulled automatically during startup.
All three have been removed: local files need neither host namespace, the
mounts are labelled, and pulling is now an explicit action.  The mutable image
tag is also resolved to its content digest (or image ID) before any probe or
encode runs.
"""

# pylint: disable=protected-access,missing-function-docstring,too-few-public-methods

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rmbloat.FfmpegChooser import FfmpegChooser


def _result(returncode=0, stdout='', stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRun:
    """Return queued results and record every command."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, timeout=5, text_output=False):
        self.calls.append(cmd)
        if not self.results:
            return _result(returncode=1)
        return self.results.pop(0)


class DockerHardeningTests(unittest.TestCase):
    """Container commands must avoid host namespaces and pin the image."""

    def make_chooser(self, **overrides):
        chooser = FfmpegChooser.__new__(FfmpegChooser)
        chooser.image = 'test-image:latest'
        chooser.image_ref = None
        chooser.runtime = 'docker'
        chooser.quiet = True
        chooser.render_device = None
        chooser.use_docker = True
        chooser.use_acceleration = False
        chooser.vaapi_supports_10bit = False
        chooser.system_ffmpeg_path = None
        chooser.has_system_acceleration = False
        chooser.has_docker_acceleration = True
        chooser.prefer_strategy = 'auto'
        chooser.strategy = 'docker_cpu'
        for key, value in overrides.items():
            setattr(chooser, key, value)
        return chooser

    def test_real_encode_drops_host_namespaces_and_labels_mount(self):
        chooser = self.make_chooser()
        params = chooser.make_namespace(
            input_file='input.mp4',
            output_file='output.mkv',
            validate_input_exists=False,
        )

        cmd = chooser.make_ffmpeg_cmd(params)

        self.assertNotIn('--net=host', cmd)
        self.assertNotIn('--ipc=host', cmd)
        workdir = str(Path('input.mp4').resolve().parent)
        self.assertEqual(cmd[cmd.index('-v') + 1], f'{workdir}:{workdir}:z')
        self.assertIn('test-image:latest', cmd)

    def test_ffprobe_mount_is_read_only_and_labelled(self):
        chooser = self.make_chooser(image_ref='registry/repo@sha256:abc')

        cmd = chooser.make_ffprobe_cmd('input.mp4')

        self.assertNotIn('--net=host', cmd)
        self.assertNotIn('--ipc=host', cmd)
        mount = cmd[cmd.index('-v') + 1]
        self.assertTrue(mount.endswith(':ro,z'))
        self.assertIn('registry/repo@sha256:abc', cmd)
        self.assertNotIn('test-image:latest', cmd)

    def test_missing_image_is_not_pulled_on_normal_startup(self):
        chooser = self.make_chooser()
        fake_run = _FakeRun([_result(returncode=1)])

        with mock.patch.object(chooser, '_run_command', side_effect=fake_run):
            chooser._ensure_image(force_pull=False)

        self.assertIsNone(chooser.runtime)
        self.assertFalse(
            any('pull' in call for call in fake_run.calls),
            f'unexpected pull: {fake_run.calls}')

    def test_force_pull_then_pins_registry_digest(self):
        chooser = self.make_chooser()
        digest = 'registry.example/repo@sha256:' + 'a' * 64
        fake_run = _FakeRun([
            _result(returncode=1),                  # not present locally
            _result(returncode=0),                  # pull succeeds
            _result(returncode=0, stdout=digest),   # RepoDigest
        ])

        with mock.patch.object(chooser, '_run_command', side_effect=fake_run):
            chooser._ensure_image(force_pull=True)

        self.assertEqual(chooser.image_ref, digest)
        self.assertTrue(any(
            call[:2] == ['docker', 'pull'] for call in fake_run.calls))

        cmd = chooser.make_ffmpeg_cmd(chooser.make_namespace(
            input_file='input.mp4', output_file='output.mkv',
            validate_input_exists=False))
        self.assertIn(digest, cmd)
        self.assertNotIn('test-image:latest', cmd)

    def test_falls_back_to_image_id_when_repo_digest_is_missing(self):
        chooser = self.make_chooser()
        fake_run = _FakeRun([
            _result(returncode=0),                         # image exists
            _result(returncode=0, stdout='<no value>\n'),  # no RepoDigest
            _result(returncode=0, stdout='sha256:deadbeef\n'),
        ])

        with mock.patch.object(chooser, '_run_command', side_effect=fake_run):
            chooser._ensure_image(force_pull=False)

        self.assertEqual(chooser.image_ref, 'sha256:deadbeef')
        self.assertFalse(
            any(call[:2] == ['docker', 'pull'] for call in fake_run.calls))


if __name__ == '__main__':
    unittest.main()
