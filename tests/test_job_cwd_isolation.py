"""Regression tests: job startup must not call ``chdir``.

``start_transcode_job()`` used to change the process-wide working directory to
the source video's folder and never restore it.  Consumers therefore relied on
matching relative paths, which is fragile in a threaded TUI+ffmpeg program.
Job paths are now absolute and the swap/bulk-rename helpers operate on those
paths directly, so the process CWD is never changed.
"""

# pylint: disable=protected-access,missing-function-docstring

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies


class RmbloatModuleTestCase(unittest.TestCase):
    """Import the real module against hermetic third-party stubs."""

    def setUp(self):
        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')
        self.job_handler_module = importlib.import_module('rmbloat.JobHandler')
        previous_singleton = self.rmbloat.Converter.singleton
        self.rmbloat.Converter.singleton = None
        self.addCleanup(
            setattr, self.rmbloat.Converter, 'singleton', previous_singleton)


class JobHandlerFixtureTestCase(RmbloatModuleTestCase):
    """Shared media directory, options, chooser, and handler setup."""

    def setUp(self):
        super().setUp()
        self.video_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.video_dir.cleanup)
        self.other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.other_dir.cleanup)

        self.original_name = 'movie.mkv'
        self.standard_name = 'movie.crf28.recode.mkv'
        self.opts = SimpleNamespace(
            keep_backup=False,
            use_trash=False,
            sample=True,
            merge_subtitles=False,
            quality=28,
            allowed_codecs='x265',
            min_shrink_pct=10,
            full_speed=False,
            thread_cnt=0,
            max_height=1080,
            progress_secs_max=5,
        )
        self.chooser = mock.Mock()
        self.chooser.use_acceleration = False
        self.chooser.make_namespace.return_value = SimpleNamespace()
        self.chooser.make_ffmpeg_cmd.return_value = [
            'ffmpeg', '-y', self.standard_name]
        self.probe_cache = mock.Mock()
        self.handler = self.rmbloat.JobHandler(
            self.opts, chooser=self.chooser, probe_cache=self.probe_cache,
            auto_mode_enabled=False)

    def make_vid(self, do_rename=False):
        source = Path(self.video_dir.name) / self.original_name
        source.write_bytes(b'original')
        vid = SimpleNamespace(
            filepath=str(source),
            standard_name=self.standard_name,
            basename1=None,
            do_rename=do_rename,
            doit=' OK',
            doit_auto=' OK',
            gb=1.0,
            net='  - ',
            ops=[],
            runs=[],
            probe0=SimpleNamespace(
                duration=600.0, height=1080, width=1920,
                pix_fmt='yuv420p', codec='h264', streams=[],
                color_spt='bt709,bt709,bt709'),
        )

        def start_new_run(command=None):
            vid.runs.append(SimpleNamespace(
                command=command, descr=None, return_code=None, texts=[]))

        vid.start_new_run = start_new_run
        return vid


class JobCwdIsolationTests(JobHandlerFixtureTestCase):
    """Transcodes must use absolute paths and leave the process CWD alone."""

    def test_start_transcode_job_keeps_cwd_and_builds_absolute_paths(self):
        vid = self.make_vid()
        cwd_before = os.getcwd()

        with mock.patch.object(
                self.job_handler_module, 'TranscodeThread') as thread_cls:
            job = self.handler.start_transcode_job(vid)

        temp_path = Path(job.temp_file)
        self.assertEqual(os.getcwd(), cwd_before)
        self.assertEqual(temp_path.name, f'SAMPLE.28.{self.standard_name}')
        self.assertEqual(temp_path.parent.parent, Path(self.video_dir.name))
        self.assertTrue(temp_path.parent.name.startswith('.rmbloat-tmp-'))
        self.assertEqual(self.handler.temp_file, job.temp_file)
        self.assertTrue(os.path.isabs(job.temp_file))
        self.assertEqual(
            job.orig_backup_file,
            str(Path(self.video_dir.name) / f'ORIG.{self.original_name}'))
        self.assertEqual(
            self.chooser.make_namespace.call_args.kwargs['input_file'],
            str(Path(self.video_dir.name) / self.original_name))
        self.assertEqual(
            self.chooser.make_namespace.call_args.kwargs['output_file'],
            job.temp_file)
        self.assertEqual(thread_cls.call_args.kwargs['temp_file'], job.temp_file)

    def test_file_swap_and_companion_rename_ignore_process_cwd(self):
        vid = self.make_vid(do_rename=True)
        vid.start_new_run('-crf 28')
        video_dir = Path(self.video_dir.name)
        (video_dir / 'movie.en.srt').write_bytes(b'subs')
        temp_path = video_dir / f'SAMPLE.28.{self.standard_name}'
        temp_path.write_bytes(b'new-data')
        job = SimpleNamespace(
            temp_file=str(temp_path),
            orig_backup_file=str(video_dir / f'ORIG.{self.original_name}'),
            is_software_fallback=False,
        )

        cwd_before = os.getcwd()
        os.chdir(self.other_dir.name)
        self.addCleanup(os.chdir, cwd_before)
        try:
            ok = self.handler._execute_file_swap(vid, job)

            self.assertTrue(ok)
            final_path = video_dir / self.standard_name
            self.assertEqual(final_path.read_bytes(), b'new-data')
            self.assertFalse((video_dir / self.original_name).exists())
            self.assertFalse(temp_path.exists())
            renamed_subtitle = video_dir / 'movie.crf28.recode.en.srt'
            self.assertEqual(renamed_subtitle.read_bytes(), b'subs')
            # The unrelated working directory is untouched and still current.
            self.assertEqual(list(Path(self.other_dir.name).iterdir()), [])
            self.assertEqual(os.getcwd(), self.other_dir.name)
            self.assertEqual(vid.basename1, self.standard_name)
        finally:
            os.chdir(cwd_before)


class RetryWorkDirCleanupTests(JobHandlerFixtureTestCase):
    """A retry must not leak the previous attempt's private work directory."""

    @staticmethod
    def _work_dirs(video_dir):
        return [path for path in Path(video_dir).iterdir()
                if path.name.startswith('.rmbloat-tmp-')]

    def test_retry_discards_the_previous_private_work_directory(self):
        vid = self.make_vid()

        with mock.patch.object(
                self.job_handler_module, 'TranscodeThread') as thread_cls:
            first_job = self.handler.start_transcode_job(vid)
            first_temp = Path(first_job.temp_file)
            first_dir = first_temp.parent
            first_temp.write_bytes(b'partial encode')
            self.assertTrue(first_dir.is_dir())

            # The retry ladder re-enters start_transcode_job(); that attempt's
            # partial output and directory must be gone, so exactly one
            # private directory remains for the new attempt.
            second_job = self.handler.start_transcode_job(vid)

        self.assertEqual(thread_cls.call_count, 2)
        self.assertFalse(first_temp.exists())
        self.assertFalse(first_dir.exists())
        work_dirs = self._work_dirs(self.video_dir.name)
        self.assertEqual(len(work_dirs), 1)
        self.assertEqual(work_dirs[0], Path(second_job.temp_file).parent)

    def test_cleanup_after_a_skipped_job_removes_its_work_directory(self):
        vid = self.make_vid()

        with mock.patch.object(self.job_handler_module, 'TranscodeThread'):
            job = self.handler.start_transcode_job(vid)
            temp_path = Path(job.temp_file)
            temp_path.write_bytes(b'partial encode')
            self.assertTrue(temp_path.parent.is_dir())

            # Skipping a conversion tears the handler down without calling
            # finish_transcode_job(); exit cleanup must still remove both the
            # partial file and its private directory.
            self.handler.emergency_cleanup()

        self.assertFalse(temp_path.exists())
        self.assertFalse(self._work_dirs(self.video_dir.name))


if __name__ == '__main__':
    unittest.main()
