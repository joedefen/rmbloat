"""Regression tests for audit finding C7: the original-file swap must be safe.

Previously ``_execute_file_swap()`` trashed or unlinked the original *before*
renaming the transcoded temp file into place. A failing final rename therefore
destroyed the only copy of the original, while the caller still logged the job
as ``OK``. ``send2trash`` failures also silently degraded to a permanent
``os.unlink()``.

These tests exercise the filesystem transaction directly (no ffmpeg/curses) and
verify that:

* a missing/empty temp file and a pre-existing destination abort untouched;
* a failed installation restores the original and keeps the temp output;
* a trash failure never permanently deletes the original;
* ``finish_transcode_job()`` downgrades a failed swap to ``ERR``.
"""

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.runtime_stubs import stubbed_runtime_dependencies

with stubbed_runtime_dependencies():
    from rmbloat import JobHandler as JobHandlerModule
    from rmbloat import rmbloat as RmbloatModule


class FileSwapTests(unittest.TestCase):
    """The swap must preserve data and report failure instead of losing files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.root)

        self.opts = types.SimpleNamespace(
            keep_backup=False,
            use_trash=False,
            sample=False,
            quality=28,
            allowed_codecs='x265',
            min_shrink_pct=10,
        )
        self.probe_cache = mock.Mock()
        self.handler = JobHandlerModule.JobHandler(
            self.opts, chooser=None, probe_cache=self.probe_cache,
            auto_mode_enabled=False)

        self.original_name = 'movie.mkv'
        self.temp_name = 'TEMP.movie.crf28.recode.mkv'
        self.final_name = 'movie.crf28.recode.mkv'

    # ------------------------------------------------------------------ helpers
    def write_file(self, name, payload):
        path = self.root / name
        path.write_bytes(payload)
        return path

    def make_vid(self):
        return types.SimpleNamespace(
            filepath=str(self.root / self.original_name),
            standard_name=self.final_name,
            basename1=None,
            do_rename=False,
            ops=[],
            runs=[types.SimpleNamespace(command='-crf 28', return_code=0, texts=[])],
        )

    def make_job(self):
        return types.SimpleNamespace(
            temp_file=self.temp_name,
            orig_backup_file='ORIG.movie.mkv',
            is_software_fallback=False,
        )

    def rollback_files(self):
        return list(self.root.glob('.rmbloat-rollback-*'))

    # ------------------------------------------------------------------- tests
    def test_success_replaces_original_and_removes_temp(self):
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')

        ok = self.handler._execute_file_swap(self.make_vid(), self.make_job())

        self.assertTrue(ok)
        self.assertEqual((self.root / self.final_name).read_bytes(), b'new-data')
        self.assertFalse((self.root / self.original_name).exists())
        self.assertFalse((self.root / self.temp_name).exists())
        self.assertEqual(self.rollback_files(), [])

    def test_keep_backup_uses_unique_name_without_clobbering(self):
        self.opts.keep_backup = True
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')
        # A previous backup already occupies the normal name.
        self.write_file('ORIG.movie.mkv', b'older-backup')

        ok = self.handler._execute_file_swap(self.make_vid(), self.make_job())

        self.assertTrue(ok)
        self.assertEqual((self.root / self.final_name).read_bytes(), b'new-data')
        self.assertEqual((self.root / 'ORIG.movie.mkv').read_bytes(), b'older-backup')
        self.assertEqual((self.root / 'ORIG.movie.1.mkv').read_bytes(), b'original')

    def test_existing_destination_aborts_without_touching_original(self):
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')
        self.write_file(self.final_name, b'occupied')

        ok = self.handler._execute_file_swap(self.make_vid(), self.make_job())

        self.assertFalse(ok)
        self.assertEqual((self.root / self.original_name).read_bytes(), b'original')
        self.assertEqual((self.root / self.temp_name).read_bytes(), b'new-data')
        self.assertEqual((self.root / self.final_name).read_bytes(), b'occupied')
        self.assertEqual(self.rollback_files(), [])

    def test_swap_uses_video_directory_not_process_cwd(self):
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')
        self.write_file('movie.en.srt', b'subs')

        vid = self.make_vid()
        vid.do_rename = True
        job = self.make_job()
        job.temp_file = str(self.root / self.temp_name)
        job.orig_backup_file = str(self.root / 'ORIG.movie.mkv')

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.addCleanup(os.chdir, str(self.root))
        os.chdir(other.name)

        ok = self.handler._execute_file_swap(vid, job)

        self.assertTrue(ok)
        self.assertEqual((self.root / self.final_name).read_bytes(), b'new-data')
        self.assertFalse((self.root / self.original_name).exists())
        self.assertFalse((self.root / self.temp_name).exists())
        self.assertEqual(
            (self.root / 'movie.crf28.recode.en.srt').read_bytes(), b'subs')
        # The unrelated working directory must remain untouched and current.
        self.assertEqual(list(Path(other.name).iterdir()), [])
        self.assertEqual(os.getcwd(), other.name)

    def test_missing_or_empty_temp_aborts_without_touching_original(self):
        self.write_file(self.original_name, b'original')

        vid = self.make_vid()
        self.assertFalse(self.handler._execute_file_swap(vid, self.make_job()))
        self.assertEqual((self.root / self.original_name).read_bytes(), b'original')

        self.write_file(self.temp_name, b'')
        vid = self.make_vid()
        self.assertFalse(self.handler._execute_file_swap(vid, self.make_job()))
        self.assertEqual((self.root / self.original_name).read_bytes(), b'original')

    def test_failed_install_restores_original_and_keeps_temp(self):
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')
        real_rename = os.rename

        def failing_rename(src, dst):
            if src == self.temp_name:
                raise OSError('simulated disk full')
            return real_rename(src, dst)

        vid = self.make_vid()
        with mock.patch.object(JobHandlerModule.os, 'rename',
                               side_effect=failing_rename), \
                mock.patch.object(JobHandlerModule.os, 'link',
                                  side_effect=OSError('hard links unavailable')):
            ok = self.handler._execute_file_swap(vid, self.make_job())

        self.assertFalse(ok)
        self.assertEqual((self.root / self.original_name).read_bytes(), b'original')
        self.assertEqual((self.root / self.temp_name).read_bytes(), b'new-data')
        self.assertFalse((self.root / self.final_name).exists())
        self.assertEqual(self.rollback_files(), [])
        self.assertTrue(any('ROLLBACK' in op for op in vid.ops))
        self.assertTrue(any('Transcoded output kept' in op for op in vid.ops))

    def test_destination_race_is_not_overwritten(self):
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')

        def racing_link(src, dst):
            Path(dst).write_bytes(b'racer')
            raise FileExistsError(dst)

        vid = self.make_vid()
        with mock.patch.object(JobHandlerModule.os, 'link',
                               side_effect=racing_link):
            ok = self.handler._execute_file_swap(vid, self.make_job())

        self.assertFalse(ok)
        self.assertEqual((self.root / self.original_name).read_bytes(), b'original')
        self.assertEqual((self.root / self.final_name).read_bytes(), b'racer')
        self.assertEqual((self.root / self.temp_name).read_bytes(), b'new-data')

    def test_send2trash_failure_never_permanently_deletes_original(self):
        self.opts.use_trash = True
        self.write_file(self.original_name, b'original')
        self.write_file(self.temp_name, b'new-data')

        vid = self.make_vid()
        with mock.patch.object(JobHandlerModule.send2trash, 'send2trash',
                               side_effect=RuntimeError('trash unavailable')):
            ok = self.handler._execute_file_swap(vid, self.make_job())

        self.assertTrue(ok)
        self.assertEqual((self.root / self.final_name).read_bytes(), b'new-data')
        preserved = self.rollback_files()
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b'original')
        self.assertTrue(any('ERROR during send2trash' in op for op in vid.ops))
        self.assertTrue(any('not permanently deleted' in op for op in vid.ops))

    def test_finish_transcode_job_marks_failed_swap_as_error(self):
        vid = types.SimpleNamespace(
            filepath=str(self.root / self.original_name),
            standard_name=self.final_name,
            basename1=None,
            do_rename=False,
            ops=[],
            gb=1.0,
            net='  - ',
            doit=' OK',
            doit_auto=' OK',
            probe0=types.SimpleNamespace(codec='h264'),
            runs=[types.SimpleNamespace(
                command='-crf 28', return_code=0, texts=[], descr='initial run')],
        )
        job = types.SimpleNamespace(
            vid=vid,
            temp_file=self.temp_name,
            thread=types.SimpleNamespace(is_alive=lambda: False),
        )
        self.probe_cache.get.return_value = types.SimpleNamespace(gb=0.5)
        self.handler._execute_file_swap = mock.Mock(return_value=False)

        probe = self.handler.finish_transcode_job(job)

        self.assertIsNone(probe)
        self.assertEqual(vid.doit, 'ERR')

    def test_advance_jobs_logs_a_failed_swap_as_error(self):
        vid = types.SimpleNamespace(
            filepath=str(self.root / self.original_name),
            doit=' OK',
            doit_auto=' OK',
            probe1=None,
        )
        job = types.SimpleNamespace(vid=vid)
        converter = object.__new__(RmbloatModule.Converter)
        converter.job = job
        converter.job_handler = mock.Mock()
        converter.job_handler.check_job_status.return_value = (None, ' OK', True)

        def finish_failed_swap(_job):
            vid.doit = 'ERR'

        converter.job_handler.finish_transcode_job.side_effect = finish_failed_swap
        converter._log_job_final = mock.Mock()
        converter._manage_queue_and_auto_mode = mock.Mock()
        converter.stack = types.SimpleNamespace(
            curr=types.SimpleNamespace(num=RmbloatModule.CONVERT_ST))

        converter.advance_jobs()

        converter._log_job_final.assert_called_once()
        self.assertEqual(converter._log_job_final.call_args[0][1], 'ERR')
        self.assertEqual(vid.doit, 'ERR')
        self.assertEqual(vid.doit_auto, 'ERR')


class SampleOutputTests(unittest.TestCase):
    """Regression tests for audit finding H4: no hardcoded sample directory."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)

        self.original_name = 'movie.mkv'
        self.standard_name = 'movie.crf28.recode.mkv'
        self.opts = types.SimpleNamespace(
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
        self.probe_cache = mock.Mock()
        self.chooser = mock.Mock()
        self.chooser.use_acceleration = True
        self.chooser.make_namespace.return_value = types.SimpleNamespace()
        self.chooser.make_ffmpeg_cmd.return_value = [
            'ffmpeg', '-i', self.original_name, f'SAMPLE.28.{self.standard_name}']
        self.handler = JobHandlerModule.JobHandler(
            self.opts, chooser=self.chooser, probe_cache=self.probe_cache,
            auto_mode_enabled=False)

    def make_vid(self):
        vid = types.SimpleNamespace(
            filepath=str(self.root / self.original_name),
            standard_name=self.standard_name,
            basename1=None,
            doit=' OK',
            doit_auto=' OK',
            gb=1.0,
            net='  - ',
            ops=[],
            runs=[],
            probe0=types.SimpleNamespace(
                duration=600.0, height=1080, width=1920,
                pix_fmt='yuv420p', codec='h264', streams=[],
                color_spt='bt709,bt709,bt709'),
        )

        def start_new_run(command=None):
            vid.runs.append(types.SimpleNamespace(
                command=command, descr=None, return_code=None, texts=[]))

        vid.start_new_run = start_new_run
        return vid

    def test_sample_output_is_absolute_without_changing_cwd(self):
        vid = self.make_vid()
        cwd_before = os.getcwd()
        with mock.patch.object(JobHandlerModule, 'TranscodeThread'):
            job = self.handler.start_transcode_job(vid)

        expected = str(self.root / f'SAMPLE.28.{self.standard_name}')
        self.assertEqual(job.temp_file, expected)
        self.assertEqual(self.handler.temp_file, expected)
        self.assertTrue(os.path.isabs(job.temp_file))
        self.assertEqual(os.path.dirname(job.temp_file), str(self.root))
        # Job startup must never change process-wide state.
        self.assertEqual(os.getcwd(), cwd_before)
        self.assertEqual(job.orig_backup_file,
                         str(self.root / f'ORIG.{self.original_name}'))
        self.assertEqual(
            self.chooser.make_namespace.call_args.kwargs['input_file'],
            vid.filepath)
        self.assertEqual(
            self.chooser.make_namespace.call_args.kwargs['output_file'],
            expected)

    def test_finish_sample_keeps_only_the_basename_for_display(self):
        vid = self.make_vid()
        # Simulate an absolute temp path anyway: basename1 must never leak it.
        temp_path = str(self.root / f'SAMPLE.28.{self.standard_name}')
        vid.runs = [types.SimpleNamespace(
            command='-crf 28', return_code=0, texts=[], descr='initial run')]
        job = types.SimpleNamespace(
            vid=vid,
            temp_file=temp_path,
            thread=types.SimpleNamespace(is_alive=lambda: False),
        )
        self.probe_cache.get.return_value = types.SimpleNamespace(gb=0.5)

        probe = self.handler.finish_transcode_job(job)

        self.assertIsNotNone(probe)
        self.assertEqual(vid.basename1, f'SAMPLE.28.{self.standard_name}')
        self.assertFalse(os.path.isabs(vid.basename1))


if __name__ == '__main__':
    unittest.main()
