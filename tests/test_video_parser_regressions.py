"""Regression tests for VideoParser episode/special handling.

Two parser regressions were fixed here: ``is_same_episode()`` tested the
bound method instead of calling it, and ``check_special()`` contained a
duplicate ``specialsdir_pat`` branch while the specials regex used ``(:?``
instead of the non-capturing ``(?:``.
"""

import unittest
from unittest import mock

from rmbloat.VideoParser import VideoParser


def _parser(season=None, episode=None, year=None):
    parser = VideoParser.__new__(VideoParser)
    parser.season = season
    parser.episode = episode
    parser.year = year
    parser.is_special = False
    return parser


class SameEpisodeTests(unittest.TestCase):
    """``is_same_episode()`` must actually call ``rhs.is_tv_episode()``."""

    def test_matching_episode_requires_rhs_to_be_a_tv_episode(self):
        parser = _parser(season=1, episode=2)
        rhs = mock.Mock()
        rhs.season = 1
        rhs.episode = 2
        rhs.is_tv_episode.return_value = False

        # Before the fix the bound method object was truthy and the matching
        # season/episode made this return True without ever calling it.
        self.assertFalse(parser.is_same_episode(rhs))
        rhs.is_tv_episode.assert_called_once_with()

    def test_matching_tv_episodes_compare_equal(self):
        parser = _parser(season=1, episode=2)
        rhs = mock.Mock()
        rhs.season = 1
        rhs.episode = 2
        rhs.is_tv_episode.return_value = True

        self.assertTrue(parser.is_same_episode(rhs))

    def test_different_episode_is_not_the_same(self):
        parser = _parser(season=1, episode=2)
        rhs = mock.Mock()
        rhs.season = 1
        rhs.episode = 3
        rhs.is_tv_episode.return_value = True

        self.assertFalse(parser.is_same_episode(rhs))


class SpecialDetectionTests(unittest.TestCase):
    """Specials folders, S00/E00, and the corrected regex."""

    def test_specials_directory_marks_special(self):
        parser = _parser(season=1, episode=2)
        parser.check_special(False, '/tv/Show/Specials/Show.S01E02.mkv')
        self.assertTrue(parser.is_special)

    def test_season_zero_marks_special(self):
        parser = _parser(season=0, episode=2)
        parser.check_special(False, '/tv/Show/Season 0/Show.S00E02.mkv')
        self.assertTrue(parser.is_special)

    def test_episode_zero_marks_special(self):
        parser = _parser(season=1, episode=0)
        parser.check_special(False, '/tv/Show/Season 1/Show.S01E00.mkv')
        self.assertTrue(parser.is_special)

    def test_season_zero_directory_marks_special(self):
        parser = _parser(season=1, episode=2)
        parser.check_special(False, '/tv/Show/Season 0/Show.S01E02.mkv')
        self.assertTrue(parser.is_special)

    def test_regular_season_is_not_special(self):
        parser = _parser(season=1, episode=2)
        parser.check_special(False, '/tv/Show/Season 1/Show.S01E02.mkv')
        self.assertFalse(parser.is_special)

    def test_movie_year_is_not_special(self):
        parser = _parser(year=2020)
        parser.check_special(False, '/movies/Specials/Movie.2020.mkv')
        self.assertFalse(parser.is_special)

    def test_specials_regex_rejects_leading_colon(self):
        # The old ``(:?`` pattern matched ':special'; ``(?:`` must not.
        self.assertIsNone(VideoParser.specialsdir_pat.match(':special'))
        self.assertIsNotNone(VideoParser.specialsdir_pat.match('Specials'))
        self.assertIsNotNone(VideoParser.specialsdir_pat.match('Deleted Scene'))


if __name__ == '__main__':
    unittest.main()
