"""Regression tests for ``Job``'s retry-ladder state.

[`check_job_status()`](../rmbloat/JobHandler.py:201) and
[`_execute_file_swap()`](../rmbloat/JobHandler.py:654) read ``is_retry``,
``is_software_fallback``, and ``temp_dir`` unconditionally.  Those attributes
used to be injected by ``start_transcode_job()`` *after* construction, so a
plain ``Job(...)`` (a test, a future call site, a replayed job) raised
``AttributeError`` instead of simply looking like a first attempt.

They are now constructor parameters with safe defaults, and the construction
site passes them explicitly rather than mutating the instance afterwards.
"""

import os
import tempfile
import types
import unittest
from unittest import mock

from tests.runtime_stubs import stubbed_runtime_dependencies

with stubbed_runtime_dependencies():
    from rmbloat.Models import Job

    from rmbloat import JobHandler as JobHandlerModule


def make_vid(texts=()):
    """A minimal ``Vid``-shaped object for the job paths under test."""
    vid = types.SimpleNamespace(
        filepath='/media/movie.mkv',
        standard_name='/media/movie.crf28.mkv',
        basename1=None,
        do_rename=False,
        ops=[],
        runs=[types.SimpleNamespace(command='-crf 28', return_code=1,
                                    texts=list(texts))],
    )

    def start_new_run(command=None):
        vid.runs.append(types.SimpleNamespace(
            command=command, return_code=None, texts=[], descr=None))

    vid.start_new_run = start_new_run
    return vid


class JobRetryStateTests(unittest.TestCase):
    """The retry-ladder flags are part of the declared job state."""

    def test_constructor_defines_retry_state_defaults(self):
        job = Job(make_vid(), 'ORIG.movie.mkv', 'TEMP.movie.mkv', 60, None)

        self.assertFalse(job.is_retry)
        self.assertFalse(job.is_software_fallback)
        self.assertIsNone(job.temp_dir)

    def test_constructor_accepts_retry_state(self):
        job = Job(make_vid(), 'ORIG.movie.mkv', 'TEMP.movie.mkv', 60, None,
                  is_retry=True, is_software_fallback=True,
                  temp_dir='/media/.rmbloat-abcd')

        self.assertTrue(job.is_retry)
        self.assertTrue(job.is_software_fallback)
        self.assertEqual(job.temp_dir, '/media/.rmbloat-abcd')

    def test_state_is_normalised_to_bool(self):
        job = Job(make_vid(), 'ORIG.mkv', 'TEMP.mkv', 60, None,
                  is_retry=1, is_software_fallback='yes')

        self.assertIs(job.is_retry, True)
        self.assertIs(job.is_software_fallback, True)


class CheckJobStatusOnPlainJobTests(unittest.TestCase):
    """A bare ``Job(...)`` must survive ``check_job_status()``."""

    def make_handler(self, return_code):
        handler = object.__new__(JobHandlerModule.JobHandler)
        handler.get_job_progress = mock.Mock(return_value=return_code)
        return handler

    def test_first_failure_enters_the_tolerant_retry(self):
        handler = self.make_handler(1)
        vid = make_vid(['Error reinitializing filters'])
        job = Job(vid, 'ORIG.movie.mkv', 'TEMP.movie.mkv', 60, None)

        with mock.patch.object(handler, 'start_transcode_job',
                               return_value='next-job') as start:
            next_job, report, done = handler.check_job_status(job)

        self.assertEqual(
            (next_job, report, done),
            ('next-job', 'RETRYING (Tolerant)...', False))
        start.assert_called_once_with(vid, retry_with_error_tolerance=True)

    def test_plain_failure_without_retry_signals_is_an_error(self):
        handler = self.make_handler(1)
        job = Job(make_vid(), 'ORIG.movie.mkv', 'TEMP.movie.mkv', 60, None)

        self.assertEqual(handler.check_job_status(job), (None, 'ERR', True))

    def test_success_reports_plain_ok(self):
        handler = self.make_handler(0)
        job = Job(make_vid(), 'ORIG.movie.mkv', 'TEMP.movie.mkv', 60, None)

        self.assertEqual(handler.check_job_status(job), (None, ' OK', True))

    def test_retry_state_selects_the_reported_status(self):
        handler = self.make_handler(0)
        retried = Job(make_vid(), 'ORIG.mkv', 'TEMP.mkv', 60, None,
                      is_retry=True)
        fallback = Job(make_vid(), 'ORIG.mkv', 'TEMP.mkv', 60, None,
                       is_retry=True, is_software_fallback=True)

        self.assertEqual(handler.check_job_status(retried)[1], 'OK2')
        self.assertEqual(handler.check_job_status(fallback)[1], 'OK3')


class StartTranscodeJobStateTests(unittest.TestCase):
    """``start_transcode_job()`` must not rely on post-construction injection."""

    def test_job_is_constructed_with_its_retry_state(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        work_dir = os.path.join(tmpdir.name, 'work')
        os.mkdir(work_dir)

        handler = object.__new__(JobHandlerModule.JobHandler)
        handler.temp_dir = work_dir
        handler.opts = types.SimpleNamespace(
            quality=28, sample=False, merge_subtitles=False, max_height=1080,
            full_speed=True, thread_cnt=2, progress_secs_max=5,
            allowed_codecs='x265')
        handler.chooser = mock.Mock()
        handler.chooser.make_namespace.return_value = types.SimpleNamespace()
        handler.chooser.make_ffmpeg_cmd.return_value = ['ffmpeg', '-i', 'in']
        handler.chooser.child_env.return_value = {}
        handler.chooser.use_acceleration = True
        handler.sample_seconds = 30

        vid = make_vid()
        vid.probe0 = types.SimpleNamespace(
            duration=60.0, pix_fmt='yuv420p10le', codec='hevc', height=1080,
            width=1920, color_spt='bt709,bt709,bt709', streams=[])

        with mock.patch.object(JobHandlerModule, 'Sandbox') as sandbox, \
                mock.patch.object(JobHandlerModule, 'TranscodeThread'):
            sandbox.create_private_work_dir.return_value = work_dir
            job = handler.start_transcode_job(
                vid, retry_with_error_tolerance=True, force_software=True)

        self.assertTrue(job.is_retry)
        self.assertTrue(job.is_software_fallback)
        self.assertEqual(job.temp_dir, work_dir)
        self.assertEqual(os.path.dirname(job.temp_file), work_dir)


if __name__ == '__main__':
    unittest.main()
