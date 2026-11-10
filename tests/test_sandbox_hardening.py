"""Regression tests for the ffmpeg/ffprobe sandbox and hardening layer."""

import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rmbloat import Sandbox
from rmbloat.FfmpegChooser import FfmpegChooser
from rmbloat.ProbeCache import ProbeCache, Probe
from tests.runtime_stubs import stubbed_runtime_dependencies


def _make_chooser(**overrides):
    """Build a chooser without running runtime/GPU detection."""
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
    chooser.sandbox_preference = 'auto'
    chooser.sandbox_mode = 'container'
    chooser.strategy = 'docker_cpu'
    for key, value in overrides.items():
        setattr(chooser, key, value)
    return chooser


class ContainerHardeningTests(unittest.TestCase):
    """Every container invocation must drop privileges and the network."""

    # The only IPC modes in which ``--shm-size`` has any effect.  ``none``
    # mounts no shared memory, so pairing it with ``--shm-size`` silently
    # throws the allowance away.
    SHM_COMPATIBLE_IPC_MODES = ('private', 'shareable', 'host')

    def ipc_mode(self, cmd):
        if '--ipc' in cmd:
            return cmd[cmd.index('--ipc') + 1]
        for arg in cmd:
            if arg.startswith('--ipc='):
                return arg.split('=', 1)[1]
        return None

    def assert_hardened(self, cmd):
        self.assertIn('--network', cmd)
        self.assertEqual(cmd[cmd.index('--network') + 1], 'none')
        self.assertIn('--cap-drop', cmd)
        self.assertEqual(cmd[cmd.index('--cap-drop') + 1], 'ALL')
        self.assertIn('no-new-privileges', cmd)
        self.assertIn('--read-only', cmd)
        self.assertIn('--pids-limit', cmd)
        self.assertIn('--shm-size', cmd)
        self.assertNotIn('--net=host', cmd)
        self.assertNotIn('--ipc=host', cmd)
        # ``--shm-size`` is only honoured by modes that keep a private shm
        # segment; ``none`` would contradict it.
        self.assertIn(self.ipc_mode(cmd), self.SHM_COMPATIBLE_IPC_MODES)
        self.assertNotEqual(self.ipc_mode(cmd), 'host')

    def test_shm_size_is_paired_with_a_compatible_ipc_mode(self):
        """``--ipc none`` used to discard the ``--shm-size`` allowance."""
        args = Sandbox.harden_container_args()
        self.assertIn('--shm-size', args)
        self.assertEqual(self.ipc_mode(args), 'private')
        self.assertNotEqual(self.ipc_mode(args), 'none')

    def test_ipc_namespace_stays_private_and_never_host(self):
        """``private`` isolates IPC exactly like ``none`` but keeps shm."""
        for use_acceleration in (False, True):
            args = Sandbox.harden_container_args(
                use_acceleration=use_acceleration)
            self.assertEqual(self.ipc_mode(args), 'private')
            self.assertNotIn('host', (self.ipc_mode(args),))

    def test_ipc_mode_can_be_overridden_without_losing_shm_size(self):
        args = Sandbox.harden_container_args(ipc_mode='shareable')
        self.assertEqual(self.ipc_mode(args), 'shareable')
        self.assertIn('--shm-size', args)

    def test_container_flags_are_shared(self):
        args = Sandbox.harden_container_args()
        self.assert_hardened(args)

    def test_probe_is_hardened_and_read_only(self):
        chooser = _make_chooser()
        cmd = chooser.make_ffprobe_cmd('input.mp4')
        self.assert_hardened(cmd)
        self.assertIn('file,pipe', cmd)
        mount = cmd[cmd.index('-v') + 1]
        self.assertTrue(mount.endswith(':ro,z'))

    def test_encode_mounts_media_read_only_and_private_dir_writable(self):
        chooser = _make_chooser()
        workdir = str(Path('input.mp4').resolve().parent)
        temp_dir = os.path.join(workdir, '.rmbloat-tmp-test')
        params = chooser.make_namespace(
            input_file='input.mp4',
            output_file=os.path.join(temp_dir, 'out.mkv'),
            temp_dir=temp_dir,
            validate_input_exists=False,
        )

        with mock.patch('grp.getgrnam', side_effect=KeyError):
            cmd = chooser.make_ffmpeg_cmd(params)

        self.assert_hardened(cmd)
        self.assertIn(f'{workdir}:{workdir}:ro,z', cmd)
        self.assertIn(f'{temp_dir}:{temp_dir}:z', cmd)
        self.assertIn('-nostdin', cmd)
        self.assertIn('file,pipe', cmd)
        # The output path is relative to the container working directory.
        self.assertTrue(cmd[-1].endswith('out.mkv'))
        self.assertFalse(os.path.isabs(cmd[-1]))


class LocalSandboxTests(unittest.TestCase):
    """The host-ffmpeg path gets a scrubbed env, limits, and no network."""

    def test_local_encode_has_no_network_and_scrubbed_limits(self):
        chooser = _make_chooser(
            use_docker=False,
            runtime=None,
            sandbox_mode='none',
            strategy='system_cpu',
        )
        params = chooser.make_namespace(
            input_file='input.mp4',
            output_file='output.mkv',
            validate_input_exists=False,
        )
        cmd = chooser.make_ffmpeg_cmd(params)

        self.assertIn('-nostdin', cmd)
        self.assertIn('-protocol_whitelist', cmd)
        self.assertIn(Sandbox.PROTOCOL_WHITELIST, cmd)
        # Resource limits run in a separate helper, not preexec_fn.
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith('exec_limits.py'))

    def test_local_env_strips_dangerous_variables_and_keeps_home(self):
        with mock.patch.dict(
                os.environ,
                {'FFREPORT': '/tmp/report', 'LD_PRELOAD': '/evil.so',
                 'HOME': '/home/example'},
                clear=True):
            env = Sandbox.local_env(tmpdir='/tmp/rmbloat-work')
        self.assertNotIn('FFREPORT', env)
        self.assertNotIn('LD_PRELOAD', env)
        self.assertEqual(env['HOME'], '/home/example')
        self.assertEqual(env['TMPDIR'], '/tmp/rmbloat-work')

    def test_child_env_is_inherited_for_container_only(self):
        container = _make_chooser()
        local = _make_chooser(use_docker=False)
        self.assertIsNone(container.child_env())
        self.assertIsInstance(local.child_env(), dict)

    def test_detect_local_sandbox_none(self):
        self.assertEqual(Sandbox.detect_local_sandbox('none'), 'none')


class PrivateWorkDirTests(unittest.TestCase):
    """The per-job directory stays private and same-filesystem."""

    def test_work_dir_is_0700_and_removed_only_when_empty(self):
        with tempfile.TemporaryDirectory() as parent:
            work = Sandbox.create_private_work_dir(parent)
            self.assertIsNotNone(work)
            self.assertEqual(Path(work).parent, Path(parent))
            self.assertEqual(stat.S_IMODE(os.stat(work).st_mode), 0o700)

            # A non-empty directory is preserved (it may hold the only output).
            Path(work, 'keep.mkv').write_bytes(b'x')
            Sandbox.remove_private_work_dir(work)
            self.assertTrue(os.path.isdir(work))

            os.unlink(Path(work, 'keep.mkv'))
            Sandbox.remove_private_work_dir(work)
            self.assertFalse(os.path.exists(work))


class CappedRunnerTests(unittest.TestCase):
    """ffprobe output and runtime are bounded."""

    @unittest.skipUnless(sys.platform != 'win32', 'POSIX process groups')
    def test_timeout_kills_the_process(self):
        result = Sandbox.run_capped(
            [sys.executable, '-c', 'import time; time.sleep(10)'],
            timeout=0.3)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)

    def test_stdout_limit_is_enforced(self):
        result = Sandbox.run_capped(
            [sys.executable, '-c',
             'import sys; sys.stdout.write("x" * 200000)'],
            timeout=5,
            stdout_limit=1000)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 1000)

    def test_successful_output_is_returned(self):
        result = Sandbox.run_capped(
            [sys.executable, '-c', 'print("ok")'], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b'ok')

    def test_limit_helper_execs_the_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            argv = Sandbox.apply_limits(
                [sys.executable, '-c', 'print("limited")'],
                fsize=1024 * 1024)
            result = subprocess.run(
                argv, cwd=tmpdir, capture_output=True, text=True,
                check=False, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('limited', result.stdout)


class ProbeInputValidationTests(unittest.TestCase):
    """ffprobe must only receive regular files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._patch = mock.patch('rmbloat.ProbeCache.atexit.register')
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.cache = ProbeCache(
            cache_file_name='probes.json',
            cache_dir_name=self._tmpdir.name,
            chooser=None)
        self.addCleanup(self.cache._release_instance_lock)

    def test_fifo_is_rejected_without_running_ffprobe(self):
        fifo = os.path.join(self._tmpdir.name, 'pipe.mp4')
        os.mkfifo(fifo)
        with mock.patch.object(Sandbox, 'run_capped') as run, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            probe = self.cache._get_metadata_with_ffprobe(fifo)
        self.assertIsInstance(probe, Probe)
        run.assert_not_called()

    def test_regular_file_probe_requests_only_used_fields(self):
        video = os.path.join(self._tmpdir.name, 'ok.mp4')
        Path(video).write_bytes(b'data' * 10)
        payload = (b'{"streams":[{"codec_type":"video","codec_name":"h264",'
                   b'"width":1920,"height":1080}],'
                   b'"format":{"duration":"10.0","bit_rate":"1000000"}}')
        result = Sandbox.CappedResult(returncode=0, stdout=payload, stderr=b'')
        with mock.patch.object(Sandbox, 'run_capped', return_value=result) as run, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            probe = self.cache._get_metadata_with_ffprobe(video)
        self.assertIsInstance(probe, Probe)
        command = run.call_args.args[0]
        self.assertIn('-show_entries', command)
        self.assertIn(Sandbox.PROTOCOL_WHITELIST, command)
        self.assertNotIn('-show_format', command)
        self.assertNotIn('-show_streams', command)


class ScannerExclusionTests(unittest.TestCase):
    """rmbloat's own work files must never be scanned."""

    def setUp(self):
        # ConvertUtils imports VideoParser, whose YAML dependency is optional
        # in a bare checkout; the shared stubs keep this test hermetic.
        deps = stubbed_runtime_dependencies()
        deps.__enter__()
        self.addCleanup(deps.__exit__, None, None, None)
        from rmbloat import ConvertUtils  # pylint: disable=import-outside-toplevel
        self.convert_utils = ConvertUtils

    def test_private_work_directory_is_excluded(self):
        self.assertTrue(
            self.convert_utils.is_excluded_directory('.rmbloat-tmp-abc'))
        self.assertTrue(
            self.convert_utils.is_excluded_directory('.rmbloat-anything'))
        self.assertFalse(self.convert_utils.is_excluded_directory('Season 01'))

    def test_private_rollback_file_is_not_a_video(self):
        self.assertFalse(self.convert_utils.is_valid_video_file(
            '/media/.rmbloat-rollback-abc.mkv'))
        self.assertTrue(self.convert_utils.is_valid_video_file('/media/movie.mkv'))


if __name__ == '__main__':
    unittest.main()
