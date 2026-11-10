"""Regression tests: ffmpeg log capture must stay bounded.

Two places previously grew without limit:

* ``TranscodeThread.run_ffmpeg()`` appended every non-progress stderr line to
  ``info.texts``, so corrupt media could exhaust memory during one job.
* ``Converter._log_job_final()`` serialized ``asdict(vid)``, which embeds
  ``runs[*].texts``.  It cleared only ``vid.texts`` for successful jobs, so
  successful runs still wrote huge JSON log entries.
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.runtime_stubs import stubbed_runtime_dependencies

with stubbed_runtime_dependencies():
    from rmbloat.Models import PathProbePair, Vid
    from rmbloat.ProbeCache import Probe
    from rmbloat.TranscodeThread import TranscodeThread

    from rmbloat import rmbloat as RmbloatModule


class TranscodeLogBufferTests(unittest.TestCase):
    """The live ffmpeg output buffer must stay bounded."""

    def make_thread(self):
        info = SimpleNamespace(texts=[], return_code=None)
        return TranscodeThread(
            cmd=[], run_info=info, job=None, progress_secs_max=1), info

    def test_line_count_is_bounded_and_newest_is_kept(self):
        thread, info = self.make_thread()
        thread.MAX_LOG_LINES = 10
        thread.LOG_TRIM_TARGET = 0.5
        thread.MAX_LOG_CHARS = 10 ** 9

        for index in range(25):
            thread._record_text(f'line-{index}')  # pylint: disable=protected-access

        self.assertLessEqual(len(info.texts), thread.MAX_LOG_LINES)
        self.assertEqual(info.texts[-1], 'line-24')
        self.assertNotIn('line-0', info.texts)

    def test_char_count_is_bounded_and_newest_is_kept(self):
        thread, info = self.make_thread()
        thread.MAX_LOG_LINES = 10 ** 6
        thread.LOG_TRIM_TARGET = 0.5
        thread.MAX_LOG_CHARS = 100

        for index in range(20):
            thread._record_text(f'{index:03d}' + 'x' * 27)  # 30 chars

        self.assertLessEqual(sum(len(text) for text in info.texts),
                             thread.MAX_LOG_CHARS)
        self.assertEqual(info.texts[-1], '019' + 'x' * 27)

    def test_oversized_single_line_is_truncated(self):
        thread, info = self.make_thread()
        thread.MAX_MESSAGE_CHARS = 10

        thread._record_text('abcdefghijklmno')

        self.assertEqual(len(info.texts), 1)
        self.assertTrue(info.texts[0].startswith('abcdefghij'))
        self.assertTrue(info.texts[0].endswith('... [truncated]'))
        self.assertLess(len(info.texts[0]), len('abcdefghijklmno') + 30)


class FinalJobLogBoundsTests(unittest.TestCase):
    """_log_job_final must not serialize unbounded run output."""

    def make_vid(self, run_lines):
        probe = Probe(width=1920, height=1080)
        ppp = PathProbePair('/tmp/movie.mkv', probe)
        ppp.standard_name = 'movie.recode.mkv'
        vid = Vid()
        vid.post_init(ppp)
        vid.start_new_run()
        vid.runs[-1].texts = list(run_lines)
        vid.texts = ['top-level'] * len(run_lines)
        return vid

    def run_log(self, vid, status):
        converter = object.__new__(RmbloatModule.Converter)
        converter.MAX_LOGGED_RUN_LINES = 5
        job = SimpleNamespace(vid=vid)
        with mock.patch.object(RmbloatModule, 'lg') as logger:
            converter._log_job_final(job, status)  # pylint: disable=protected-access
        payload = logger.put.call_args[0][2]
        return json.loads(payload)

    def test_successful_job_clears_run_texts(self):
        vid = self.make_vid([f'line-{index}' for index in range(300)])

        dumped = self.run_log(vid, ' OK')

        self.assertEqual(dumped['texts'], [])
        self.assertEqual(dumped['runs'][0]['texts'], [])

    def test_failed_job_keeps_bounded_tail(self):
        vid = self.make_vid([f'line-{index}' for index in range(300)])

        dumped = self.run_log(vid, 'ERR')

        run_texts = dumped['runs'][0]['texts']
        self.assertEqual(len(run_texts), 6)  # 5 kept + omission marker
        self.assertIn('295 earlier line(s) omitted', run_texts[0])
        self.assertEqual(run_texts[-1], 'line-299')
        self.assertEqual(len(dumped['texts']), 6)


if __name__ == '__main__':
    unittest.main()
