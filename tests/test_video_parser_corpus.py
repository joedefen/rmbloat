"""Regression gate for the embedded ``VideoParser`` corpus.

``VideoParser.run_regressions()`` used to print its failures and return
``None``, so ``python -m rmbloat.VideoParser`` (and anything that wrapped it)
always exited 0.  It now returns the failure count and the module entry point
exits non-zero when a case regressed.

While turning that into a gate, the shipped corpus turned out to have drifted:
``episode_key()`` zero pads season/episode numbers, twelve stored expectations
still used the old unpadded form, and a bare movie name without a year or a
release/junk token is now reported as unparsed (``n/a``).  The corpus also
revealed a real parser bug: every compiled pattern ends with a
``(\\brepack\\b|)`` capture, and the empty alternative matched immediately, so
``is_repack`` was never true.  The group is now ``(?:.*?(\\brepack\\b)|)``.
"""

import contextlib
import importlib
import io
import unittest
from unittest import mock

from tests.runtime_stubs import purge_rmbloat_modules

# Test modules that import ``rmbloat.*`` inside ``stubbed_runtime_dependencies()``
# leave modules behind that were built against the ``ruamel.yaml`` dummy, so a
# plain import here can pick up a ``yaml`` object with no ``load()``.  Purge
# first so this module always sees the real dependency.
purge_rmbloat_modules()
from rmbloat.VideoParser import VideoParser  # noqa: E402

video_parser_module = importlib.import_module('rmbloat.VideoParser')


def run_quietly():
    """Run the embedded corpus, returning ``(failures, captured stdout)``."""
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        failures = VideoParser.run_regressions(verbose=False)
    return failures, stdout.getvalue()


class ShippedCorpusTests(unittest.TestCase):
    """The hardcoded corpus must pass so CI can gate on it."""

    def test_shipped_corpus_has_no_failures(self):
        failures, output = run_quietly()
        self.assertEqual(failures, 0, output)
        self.assertIn('0 failures of', output)

    def test_summary_counts_every_case(self):
        corpus = video_parser_module.yaml.load(VideoParser.tests_yaml)
        _, output = run_quietly()
        self.assertIn(f'0 failures of {len(corpus)} tests', output)


class FailureCountingTests(unittest.TestCase):
    """A regression must be reported as a non-zero count."""

    def test_failing_case_is_counted_and_named(self):
        fake_yaml = mock.Mock()
        fake_yaml.load.return_value = {
            'Show.S01E02.720p.mkv': {'result': 'tv0 "Show" s01e02 any .mkv'},
            'Show.S03E04.720p.mkv': {'result': 'tv0 "Show" s99e99 any .mkv'},
            'Movie.2020.1080p.mkv': {'result': 'mov1 "Movie" Y=2020 any .mkv'},
        }

        with mock.patch.object(video_parser_module, 'yaml', fake_yaml):
            failures, output = run_quietly()

        self.assertEqual(failures, 1)
        self.assertIn('FAIL: Show.S03E04.720p.mkv', output)
        self.assertIn('TEST Summary: 1 failures of 3 tests', output)

    def test_passing_case_returns_zero(self):
        fake_yaml = mock.Mock()
        fake_yaml.load.return_value = {
            'Show.S01E02.720p.mkv': {'result': 'tv0 "Show" s01e02 any .mkv'},
        }

        with mock.patch.object(video_parser_module, 'yaml', fake_yaml):
            failures, _ = run_quietly()

        self.assertEqual(failures, 0)


class RepackAndFormatTests(unittest.TestCase):
    """Behaviour the refreshed corpus encodes."""

    def test_repack_token_is_detected(self):
        parsed = VideoParser(
            'when.calls.the.heart.s06e07.repack.webrip.x264-tbs.mkv')
        self.assertTrue(parsed.is_repack)
        self.assertIn(' repack ', parsed.mini_str())

    def test_ordinary_release_is_not_flagged_as_repack(self):
        parsed = VideoParser(
            'Yellowstone.2018.S03E08.720p.HEVC.x265-MeGusta.mkv')
        self.assertFalse(parsed.is_repack)

    def test_episode_numbers_are_zero_padded(self):
        parsed = VideoParser('penny.dreadful.205.hdtv-lol.mp4')
        self.assertEqual(parsed.episode_key(), '"penny dreadful" s02e05')

    def test_bare_movie_name_is_reported_as_unparsed(self):
        parsed = VideoParser('Gone with the Wind.avi')
        self.assertTrue(parsed.is_error())
        self.assertEqual(parsed.mini_str(), 'n/a "Gone with the Wind" any .avi')
