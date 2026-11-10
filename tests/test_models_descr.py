"""Regression tests for ``Vid.descr_str()`` (audit finding M3).

``start_new_run()`` creates an ``FfmpegRun`` with ``descr=None``; only later
does ``JobHandler`` assign ``'initial run'`` or a retry description.  The
progress decoration must therefore cope with a missing/empty description (and
with no runs at all) instead of raising ``TypeError``/``IndexError``.
"""

import types
import unittest

from rmbloat.Models import Job, Vid


def _make_vid():
    vid = Vid()
    vid.post_init(types.SimpleNamespace(
        video_file='movie.mkv', standard_name='movie.mkv', do_rename=False))
    return vid


class VidDescrStrTests(unittest.TestCase):
    """`descr_str()` must be safe before a description is assigned."""

    def setUp(self):
        self.vid = _make_vid()

    def test_no_runs_returns_empty_string(self):
        self.assertEqual(self.vid.runs, [])
        self.assertEqual(self.vid.descr_str(), '')

    def test_fresh_run_without_description_returns_empty_string(self):
        """The exact case produced by ``start_new_run()``."""
        self.vid.start_new_run(command='-crf 28')
        self.assertIsNone(self.vid.runs[-1].descr)
        self.assertEqual(self.vid.descr_str(), '')

    def test_explicit_none_description_returns_empty_string(self):
        self.vid.runs.append(types.SimpleNamespace(descr=None))
        self.assertEqual(self.vid.descr_str(), '')

    def test_empty_description_returns_empty_string(self):
        self.vid.runs.append(types.SimpleNamespace(descr=''))
        self.assertEqual(self.vid.descr_str(), '')

    def test_initial_run_is_not_decorated(self):
        self.vid.runs.append(types.SimpleNamespace(descr='initial run'))
        self.assertEqual(self.vid.descr_str(), '')

    def test_retry_description_is_decorated(self):
        self.vid.runs.append(types.SimpleNamespace(descr='retry w S/W convert'))
        self.assertEqual(self.vid.descr_str(), '  [retry w S/W convert]')

    def test_non_string_description_does_not_raise(self):
        self.vid.runs.append(types.SimpleNamespace(descr=7))
        self.assertEqual(self.vid.descr_str(), '  [7]')

    def test_job_progress_survives_missing_description(self):
        """``Job.progress`` calls ``descr_str()`` while a run is in flight."""
        self.vid.start_new_run(command='-crf 28')
        self.vid.runs[-1].return_code = None
        job = types.SimpleNamespace(
            vid=self.vid,
            thread=types.SimpleNamespace(
                is_finished=False,
                status_string=' encoding  50%',
                info=types.SimpleNamespace(return_code=None),
            ),
        )
        # Bind the real property getter to the stand-in instance.
        self.assertEqual(Job.progress.fget(job), ' encoding  50%')


if __name__ == '__main__':
    unittest.main()
