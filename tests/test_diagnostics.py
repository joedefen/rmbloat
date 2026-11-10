"""Regression tests for process state and transcode/probe diagnostics.

Each class pins one defect:

* ``Converter.main_loop()`` changed the process working directory for every
  file even though every downstream path is absolute.
* Probe failures were printed to stdout, where they were overwritten by the
  in-place probe status line and interleaved with redirected output.  They now
  go to stderr, like the batch summary.
* ``TranscodeThread`` hid the real exception text behind a literal ``str(...)``
  wrapper, and dropped a final stderr line that arrived without a newline.
* Dead attributes, a no-op branch, and an unused helper named by the audit
  must not reappear.
"""

import contextlib
import importlib
import inspect
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import stubbed_runtime_dependencies


with stubbed_runtime_dependencies():
    from rmbloat import Sandbox
    from rmbloat.Models import PathProbePair, Probe
    from rmbloat.PersistentState import PersistentState
    from rmbloat.ProbeCache import ProbeCache
    from rmbloat.TranscodeThread import TranscodeThread


class MainLoopCwdTests(unittest.TestCase):
    """``main_loop()`` must not change the process working directory."""

    def setUp(self):
        with stubbed_runtime_dependencies():
            self.rmbloat = importlib.import_module('rmbloat.rmbloat')
        self._saved_cwd = os.getcwd()
        self.addCleanup(os.chdir, self._saved_cwd)
        self.video_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.video_dir.cleanup)
        self.work_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.work_dir.cleanup)

    def make_converter(self, ppps):
        converter = object.__new__(self.rmbloat.Converter)
        converter.opts = SimpleNamespace(files=[])
        converter.probe_cache = mock.Mock()
        converter.create_video_file_list = mock.Mock(return_value=ppps)
        converter.do_window_mode = mock.Mock()
        return converter

    def test_main_loop_never_enters_the_video_directory(self):
        video = Path(self.video_dir.name) / 'movie.mkv'
        video.write_bytes(b'x')
        probe = Probe(width=1920, height=1080)
        probe.bloat = 100
        converter = self.make_converter([PathProbePair(str(video), probe)])

        os.chdir(self.work_dir.name)
        observed = []
        converter.process_one_ppp = mock.Mock(
            side_effect=lambda _ppp: observed.append(os.getcwd()))

        converter.main_loop()

        self.assertEqual(observed, [self.work_dir.name])
        self.assertEqual(os.getcwd(), self.work_dir.name)
        converter.do_window_mode.assert_called_once_with()


class ProbeErrorStreamTests(unittest.TestCase):
    """Probe failures belong on stderr, beside the probe status line."""

    def test_missing_file_error_goes_to_stderr(self):
        # The missing-file path returns before any cache state is touched, so
        # an uninitialised instance is enough to exercise it.
        cache = ProbeCache.__new__(ProbeCache)
        cache.chooser = None

        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'gone.mkv')
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                result = cache._get_metadata_with_ffprobe(  # pylint: disable=protected-access
                    missing)

        self.assertIsNone(result)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('File not found', stderr.getvalue())


class TranscodeThreadDiagnosticsTests(unittest.TestCase):
    """The job log must keep the real error text."""

    def make_thread(self):
        info = SimpleNamespace(texts=[], return_code=None)
        return TranscodeThread(
            cmd=[], run_info=info, job=None, progress_secs_max=1), info

    def test_run_reports_the_exception_message_not_a_str_wrapper(self):
        thread, info = self.make_thread()
        with mock.patch.object(thread, 'run_ffmpeg',
                               side_effect=RuntimeError('boom')):
            thread.run()

        self.assertEqual(thread.status_string, 'JobERR: boom')
        self.assertNotIn('str(', thread.status_string)
        self.assertEqual(info.return_code, 127)
        self.assertTrue(thread.is_finished)

    def test_final_unterminated_stderr_line_is_kept(self):
        thread, info = self.make_thread()

        thread._flush_partial_line(  # pylint: disable=protected-access
            b'[libx265 @ 0x1] Error while opening encoder')

        self.assertEqual(
            info.texts[-1], '[libx265 @ 0x1] Error while opening encoder')

    def test_leftover_progress_fragment_is_not_recorded(self):
        thread, info = self.make_thread()

        thread._flush_partial_line(  # pylint: disable=protected-access
            b'frame=  50 fps=0.0 q=-0.0')

        self.assertEqual(info.texts, [])


class VestigialCodeTests(unittest.TestCase):
    """The dead state and no-op branches must stay removed."""

    def test_main_loop_does_not_change_directory(self):
        with stubbed_runtime_dependencies():
            module = importlib.import_module('rmbloat.rmbloat')
        source = inspect.getsource(module.Converter.main_loop)
        self.assertNotIn('chdir', source)

    def test_converter_keeps_no_unused_process_state(self):
        with stubbed_runtime_dependencies():
            module = importlib.import_module('rmbloat.rmbloat')
        source = inspect.getsource(module.Converter.__init__)
        self.assertNotIn('original_cwd', source)
        self.assertNotIn("self.state =", source)

    def test_persistent_state_has_no_unused_save_updated_opts(self):
        self.assertFalse(hasattr(PersistentState, 'save_updated_opts'))

    def test_harden_container_args_has_no_noop_acceleration_branch(self):
        source = inspect.getsource(Sandbox.harden_container_args)
        body = source.split('"""')[-1]
        self.assertNotIn('pass', body)
