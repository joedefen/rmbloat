#!/usr/bin/env python3
""" Parses VideoFile names to extract Title, etc. """
import os
import re
import string
from io import StringIO
from types import SimpleNamespace
from ruamel.yaml import YAML
# pylint: disable=too-many-statements,too-many-branches,too-many-nested-blocks
# pylint: disable=too-many-locals,broad-exception-caught,too-few-public-methods
# pylint: disable=too-many-instance-attributes,line-too-long,invalid-name

# Initialize the YAML parser for internal use
# Note: ruamel.yaml is imported to keep the regression tests runnable.
yaml = YAML()
yaml.default_flow_style = False

# Simple placeholder for the external function yaml_dump used in run_regressions.
# This assumes the intent was to dump a complex Python structure (like a dict
# containing SimpleNamespaces) to a formatted YAML string/file.
# For simplicity, this uses the ruamel.yaml instance to dump to a string.
def custom_yaml_dump(data, flow_nodes=None, indent=4):
    """
    Placeholder for the original yaml_dump, using ruamel.yaml to format output.
    """
    string_stream = StringIO()
    # Note: flow_nodes and indent control formatting, but a simple dump suffices
    # to demonstrate the current test results.
    yaml.dump(data, string_stream)
    return string_stream.getvalue()


class VideoParser():
    """
    A robust parser for extracting title, season, episode, and year
    from TV show and Movie filenames.

    This class relies heavily on regular expressions to match common naming
    conventions found in media files.
    """

    # --- Dependency Mock-ups / Internal Configuration ---
    # Original: params = ConfigSubshop.get_params()
    # Stubbing out the necessary config variables for the standalone class.
    class _Config:
        def __init__(self):
            # These would typically be loaded from config; default to empty list
            self.tv_root_dirs = []
            self.movie_root_dirs = []
    params = _Config()
    # Original: accumulate_junk = False # for creating 'junk' words list
    accumulate_junk = False # Left for potential future use/debugging
    junk = {}

    # Logging placeholder (Original: lg.trX(...))
    # All logging/trace calls are commented out or replaced with a simple function
    @staticmethod
    def _log(level, *args):
        """Placeholder for original logging calls (lg.trX)."""
        # if level < 5:  # Adjust verbosity if needed for debugging
        #     print(f'[LOG {level}]', *args)
        pass

    # --- Extension and Keyword Definitions ---
    # sets of extensions (note that the leading '.' is not included)
    vid_exts = set(('avi mp4 mov mkv mk3d webm ts mts m2ts ps vob evo mpeg'
                    ' mpg m1v m2p m2v m4v movhd movx qt mxf ogg ogm ogv rm rmvb'
                    ' flv swf asf wm wmv wmx divx x264 xvid').split())
    # for movies w/o a year separator, these words are assumed to be the first
    # non-title work in the junk after the title.
    not_titles = ('1080p 480p 576p 720p'  # actually seen as 1st junk
        ' bd bdrip bluray brrip cd1 cd2'
        ' dvdrip dvdscr extended'
        ' hdrip hdts hdtv japanese korsub'
        ' proper remastered repack web webrip'
        ' bbc 10bit 2hd aaa aac aac2 ac3 afg aks74u amzn bmf'  # junk but NOT seen as 1st junk
        ' cm8 cmrg crazy4ad d3fil3r d3g dd5 drngr dsnp'
        ' evo fgt ftp fum galaxytv gaz'
        ' h264 h265 hevc ion10 it00nz lol m2g'
        ' megusta msd nhanc3 nodlabs notv npw nsd ozlem pdtv'
        ' rarbg rerip rgb sfm shitbox sticky83 stuttershit tbs'
        ' tepes tvv vtv vxt w4f x0r x264 x265 xvid xvidhd yify zmnt'
        .split())
    not_titles_re = rf'{"|".join(not_titles)}' # '1080p|480p|...|webrip'

    oksubs = ('.srt', '.smi', '.ssa', '.ass', '.vtt') # most to least prefered
    sub_exts = {x[1:] for x in oksubs}
    vid_sub_exts = vid_exts | sub_exts

    # compiled patterns to apply to filenames to categorize extension
    vid_ext_pat = re.compile(rf'\.({"|".join(vid_exts)})$', re.IGNORECASE)
    sub_ext_pat = re.compile(rf'\.({"|".join(sub_exts)})$', re.IGNORECASE)
    vid_sub_ext_pat = re.compile(rf'\.({"|".join(vid_sub_exts)})$', re.IGNORECASE)

    # Directory patterns
    title_year_cc_re = re.compile(
        r'^(.*?)\s+\(((?:19|20)\d\d)\)(?:\s+([a-zA-Z]{2}))?$',
        re.IGNORECASE)
    season_regex = r'^season\s*(\d+)$'
    specials_regex = (r'^(:?special|other|trailer|short|scene|interview'
                      +r'|featurette|deleted scene|behind the scene)s?\b')
    seasondir_pat = re.compile(season_regex, re.IGNORECASE)  # season folder pattern
    specialsdir_pat = re.compile(specials_regex, re.IGNORECASE)  # specials folder
    showsubdir_pat = re.compile(rf'{season_regex}|{specials_regex}',
        re.IGNORECASE)  # all 'valid' subdirs of the tv show

    # --- Regex Components ---
    sep = r'[.\-\[\]\s]+'
        # e.g., to trash [*] in '[HorribleSubs] One Piece - 808 [480p].mkv'
    trash_in_front_re = r'^(?:\[[^\]]*\]|)[.\-\s]*'
    hi_ep = r'(?:(?:-e|-|e)(\d{1,3})|)\b'

    # regexes to pull out title, season, episode, episode_hi, year...
    regexes = { 'tv': [
            # title and episode REs $1=title $2=season $3=episode $4=episode_hi
            # 1=strong TV indicator, 0=weak/not TV indicator
        (1, r'(.*?)' + sep + r's(\d{1,3})[.\s]*e(\d{1,3})' + hi_ep), # title S09E02[03]
        (1, r'(.*?)' + sep + r'(\d{1,3})x(\d{1,3})' + hi_ep),  # title 9x02
        (1, r'(.*?)' + sep + r'(\d{1,3})(\d\d)(\d\d)\b'),  # title 90203
        (0, r'(.*?)' + sep + r'(\d{1,3})(\d\d)\b' + hi_ep),  # title 902
        (1, r'()s(\d{1,3})e(\d{1,3})[\s\.]*' + hi_ep), # no-title S09E02
        (1, r'(.*?)' + sep + r's(\d{1,3})[.\s\-]+(\d{1,3})' + hi_ep), # title S09E02[03]
        (0, r'(.*?)' + sep + r'\s*-\s*()(\d{1,3})' + hi_ep),  # title - 02
        (0, r'(.*?)' + sep + 'part()' + sep + r'(\d{1,3})' + hi_ep), # title part 1
        ], 'movie': [
            # title and year REs $1=title $2=year
        (0, r'(.*?)' + sep + r'\(((?:19|20)\d\d)\)'), # title (2020)
        (0, r'(.*)' + sep + r'\b((?:19|20)\d\d)\b'), # title 2020 : look for last
        (0, r'(.*?)' + sep + rf'()(?=\b(?:{not_titles_re})\b)'), # title followed by junk
        ]}

    compiled_regexes = {}

    # --- Static Utility Methods ---

    @staticmethod
    def has_video_ext(path: str) -> bool:
        """Does the video file have a standard video file extension?"""
        return bool(VideoParser.vid_ext_pat.search(path))

    @staticmethod
    def has_subt_ext(path: str) -> bool:
        """Does the subtitle file have a standard subtitle file extension?"""
        return bool(VideoParser.sub_ext_pat.search(path))

    @staticmethod
    def type_hint_by_path(videopath: str) -> SimpleNamespace:
        """ TBD """
        hint = SimpleNamespace(movie=False, tv=False)

        # 1. Check parent folder (e.g., Season 01)
        parent_dir = os.path.basename(os.path.dirname(videopath))
        if VideoParser.seasondir_pat.match(parent_dir):
            hint.tv = True
            return hint # Strong TV hint

        # 2. Check grand-parent folder (e.g., Show Title (2018) US)
        grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(videopath)))

        # Check if grandparent is the Show/Movie Title (YYYY) folder
        title_match = VideoParser.title_year_cc_re.match(grandparent_dir)

        if title_match:
            # If the direct parent was *not* a Season folder, and we found a title/(year) folder
            # we still can't be 100% sure without the root, but the hierarchy suggests it's
            # either a TV show (if the parent was simple like 'disc 1') or a movie.
            # Since you want to be generic, the safest assumption is 'no explicit hint'
            # unless the Season folder was found.
            # However, for TV, the S/E tag is usually enough. For a generic parser,
            # this title_match is useful for *metadata*, but may not be a strong *type hint*.

            # If the parent directory was NOT a season dir, and we find a title/(year) dir
            # two levels up, it suggests TV (since it's a hierarchy).
            if not hint.tv:
                # Since it's nested under a titled folder, assume TV episode is the primary parse target
                hint.tv = True
                return hint

        # If no folder pattern matches, return no hint (leaving the parser to rely on filename regexes)
        return hint

    # --- Public Instance Methods (Post-Initialization) ---

    def is_tv_episode(self) -> bool:
        """Return true if both season and episode are set."""
        return bool(self.episode is not None and self.season is not None)

    def has_tv_sea_or_ep(self) -> bool:
        """Return true if season OR episode is set."""
        return bool(self.episode is not None or self.season is not None)

    def is_movie_year(self) -> bool:
        """Return true if year is set AND it's NOT a TV episode."""
        return bool(self.year is not None and not self.is_tv_episode())

    def is_same_episode(self, rhs) -> bool:
        """Return True if season and epsiode agree (and are non-None)."""
        return (self.is_tv_episode() and rhs.is_tv_episode
                and self.season == rhs.season and self.episode == rhs.episode)

    def is_same_movie_year(self, rhs) -> bool:
        """Return True years agree (and are non-None AND not tv episodes)."""
        return bool(self.is_movie_year() and rhs.is_movie_year() and self.year == rhs.year)

    def is_error(self) -> bool:
        """Cannot parse as tv episode or movie (no regex key was matched)."""
        return not bool(self.re_key)

    def get_essence_dict(self) -> dict:
        """Returns the 'essential' vars that represent the result."""
        rv = {}
        for key in ('title', 'raw_title', 'is_repack', 'year', 'season', 'episode', 'episode_hi'):
            rv[key] = getattr(self, key)
        return rv

    def mini_str(self, verbose: bool = True) -> str:
        """Returns a concise, human-readable summary of the parse result."""
        key = self.re_key if self.re_key else 'n/a'
        hint = self.hint if self.hint else 'any'

        return (f'{key + " " if verbose else ""}{self.episode_key()}'
                + (f' Y={self.year}' if self.year else '')
                + (f'{" special" if self.is_special else ""}')
                + (f'{" repack" if self.is_repack else ""}')
                + (f'{" " + hint if verbose else ""}')
                + (f' {self.ext if self.ext else "n/a"}'))

    def episode_key(self) -> str:
        """Just the title and episode number/range."""
        return (f'"{self.title}"'
                + (f' s{self.season:02d}e{self.episode:02d}' if self.episode is not None else '')
                + (f'-{self.episode_hi:02d}' if self.episode_hi else ''))


    # --- Internal Core Logic ---

    def __init__(self, videopath: str, expect_episode: bool = None, expect_movie: bool = None):
        """
        Initializes the VideoParser and attempts to parse the video file path.

        :param videopath: The full path or filename string to parse.
        :param expect_episode: Hint: If True, prioritize TV parsing.
        :param expect_movie: Hint: If True, prioritize Movie parsing.
        """
        try:
            # Need full path for TV/Movie hint using directory structure
            videopath = os.path.abspath(videopath)
        except Exception:
            pass

        self.videopath = videopath
        self.corename, self.ext = os.path.splitext(os.path.basename(videopath))
        # Handle cases where the 'extension' is part of the core name
        if self.ext and self.ext[1:] not in self.vid_sub_exts:
            self.corename, self.ext = self.corename + self.ext, ''

        self.title, self.raw_title, self.is_repack, self.year = None, None, None, None
        self.season, self.episode, self.episode_hi = None, None, None
        self.re_key, self.hint, self.is_special = '', '', False

        # Determine hint if not explicitly provided
        if expect_episode is None and expect_movie is None:
            hint = self.type_hint_by_path(videopath)
            self._log(9, 'VideoParser type_hint:', vars(hint))
            expect_episode, expect_movie = hint.tv, hint.movie

        self._parse(expect_episode=expect_episode, expect_movie=expect_movie)
        self.check_special(expect_movie, videopath)

    def check_special(self, expect_movie: bool, videopath: str):
        """Check whether this video is likely to be a TV special setting .is_special."""
        self.is_special = False # until proven otherwise
        if self.is_movie_year() or expect_movie:
            return

        if self.season is not None and self.season == 0:  # s00 => special
            self.is_special = True
        elif self.episode is not None and self.episode == 0: # e00 => special
            self.is_special = True
        else:
            parentd = os.path.basename(os.path.dirname(videopath))
            # Check if parent directory is named 'Specials' or similar
            if VideoParser.specialsdir_pat.match(parentd):
                self.is_special = True
            else:
                # Check if parent directory is named 'Season 0' or similar
                mat = VideoParser.seasondir_pat.match(parentd)
                if mat and int(mat.group(1)) == 0:
                    self.is_special = True
                elif VideoParser.specialsdir_pat.match(parentd):
                    self.is_special = True

    def _parse(self, expect_episode: bool = None, expect_movie: bool = None):
        """
        From the regex lists above, assemble and test REs against the
        filename until a match is reached or the lists are exhausted.
        """

        if bool(expect_movie) and not bool(expect_episode):
            cats = ('movie', 'tv')
            self.hint = 'movie'
        elif not bool(expect_movie) and bool(expect_episode):
            cats = ('tv', 'movie')
            self.hint = 'episode'
        else:
            cats = ('movie', 'tv')

        self._log(9, 'DB cats:', cats)

        hits = []
        force_tv = -1 # if set indicates we need to force the tv hit

        for cat in cats:
            for idx in range(len(self.regexes[cat])):
                key = f'{cat[:3]}{idx}'
                self._log(8, 'VideoParser: trying:', key, self.regexes[cat][idx])

                compiled_re = self.compiled_regexes.get(key, None)
                if not compiled_re:
                    pat = self.regexes[cat][idx][1]
                    # Append repack/release group info capture to all patterns
                    compiled_re = re.compile(self.trash_in_front_re + pat
                            + r'.*?(\brepack\b|)', re.IGNORECASE)
                    self.compiled_regexes[key] = compiled_re

                match = compiled_re.match(self.corename)
                self._log(9, 'VideoParser pat:', compiled_re.pattern, self.corename, match,
                        match.groups() if match else '')

                if match:
                    self._log(7, 'VideoParser: matched:', self.regexes[cat][idx])

                    # JUNK ACCUMULATION LOGIC (retained for functionality)
                    if self.accumulate_junk:
                        print('    MATCH:', match.group(0))
                        print('    END:', self.corename[match.end():])
                        words = re.split(r'[\s\.\-=]+', self.corename[match.end():])
                        idx_junk = 0
                        for word in words:
                            if len(word) < 3 or re.match(r'^\d+$', word):
                                continue
                            word = word.lower()
                            count = self.junk.get(word, 0) + 1
                            self.junk[word] = count
                            print('   ' if idx_junk>0 else '', word, self.junk[word])
                            idx_junk += 1

                    hit = SimpleNamespace(season=None, episode=None, episode_hi=None,
                            year=None, is_repack=False, re_key=None, raw_title=None, title=None)

                    hit.raw_title = match.group(1)
                    hit.title = match.group(1).replace('.', ' ')

                    if self.regexes[cat][idx][0]:
                        force_tv = len(hits) # This is a strong TV indicator hit

                    hits.append(hit)

                    # Fill in TV/Movie specific fields based on category
                    if cat == 'tv':
                        # Match groups for TV are: $1=title, $2=season, $3=episode, $4=hi_ep, $5=repack
                        hit.season = match.group(2)
                        hit.season = 1 if hit.season == '' else int(hit.season)
                        hit.episode = int(match.group(3))
                        hit.episode_hi = match.group(4)
                        hit.episode_hi = int(hit.episode_hi) if hit.episode_hi else None
                        hit.is_repack = bool(match.group(5))
                        hit.re_key = key
                        self._log(5, 'VideoParser TV hit:', vars(hit))
                    else: # cat == 'movie'
                        # Match groups for Movie are: $1=title, $2=year, $3=repack
                        hit.year = match.group(2)
                        hit.year = int(hit.year) if hit.year else None
                        # Repack group index is always the last group in the pattern (index -1)
                        hit.is_repack = bool(match.groups()[-1])
                        hit.re_key = key
                        self._log(5, 'VideoParser Movie hit:', vars(hit))

                    break # Stop at the first successful match in this category

        if len(hits) == 0:
            self.raw_title = self.corename
            self.title = self.corename.replace('.', ' ')
            return

        # Ambiguity Resolution Logic
        if len(hits) == 1:
            winner = hits[0]
        elif len(hits) == 2:
            winner, loser = hits[0], hits[1]

            # Prioritize the strong TV match if it occurred
            if force_tv >= 0:
                winner, loser = hits[force_tv], hits[0 if force_tv == 1 else 1]

            # Case 1: TV match looks like a valid year (e.g., S19E45 looks like 1945)
            elif hits[1].year and hits[0].season and hits[0].episode:
                if 1900 <= 100*int(hits[0].season) + int(hits[0].episode) <= 2099:
                    winner, loser = hits[1], hits[0] # Choose Movie
                    self._log(1, 'VideoParser: override as movie')

            # Case 2: Movie match is weak (no year/junk indicator), but TV match is strong
            elif not hits[0].year and hits[1].season and hits[1].episode:
                self._log(1, 'VideoParser: override as tvshow')
                winner, loser = hits[1], hits[0] # Choose TV

            # Attempt to use the shorter title from the loser if the winner was too greedy
            if winner.season and winner.episode:
                if len(loser.title) < len(winner.title):
                    winner.title = loser.title
                    winner.year = loser.year # Jury still out, but retained as requested
        else:
            # Should not happen if the inner 'break' logic is sound
            assert False, "Multiple hits greater than 2 encountered."


        # Set final attributes
        self.title = winner.title
        self.raw_title = winner.raw_title
        self.season = winner.season
        self.episode = winner.episode
        self.episode_hi = winner.episode_hi
        self.year = winner.year
        self.is_repack = winner.is_repack
        self.re_key = winner.re_key
        self._log(1, 'VideoParser: result:', vars(self))


    # --- Regression Testing ---

    @staticmethod
    def run_regressions(verbose: bool = False):
        """
        Runs the internal test suite against the hardcoded YAML data.

        :param verbose: If True, prints all results; otherwise, prints only failures and summary.
        """
        # Load the YAML tests using the internal ruamel.yaml instance
        tests = yaml.load(VideoParser.tests_yaml)
        fail_cnt, results = 0, {}

        # Helper for printing (since lg.pr is no longer available)
        _print = print

        for filename, result_dict in tests.items():
            parsed = VideoParser(filename)
            nres = parsed.mini_str()
            # The structure of the loaded YAML is {'filename': {'result': '...string...'}}
            ores = result_dict.get('result', '')
            ok = 'OK' if nres == ores else 'FAIL'

            if ok != 'OK' or verbose:
                _print(f'{ok:4s}: {filename}')
                _print(f'- nres: {nres}')
                if ok != 'OK':
                    _print(f'- ores: {ores}')
                    fail_cnt += 1
            results[filename] = {'result': nres}

        _print(f'\nTEST Summary: {fail_cnt} failures of {len(tests)} tests')

        if verbose and fail_cnt:
            _print("\nTEST DUMP w new results:\n")
            # Use the custom_yaml_dump placeholder
            _print(custom_yaml_dump(results, flow_nodes=('result',), indent=4))

    @staticmethod
    def parse_file(filename: str, verbose: bool = False) -> bool:
        """
        Test code for actual files (utility for quick testing outside regressions).

        :param filename: The path to a file to parse.
        :param verbose: Prints details of the parse result.
        :returns: True if the file seems to have been parsed correctly based on hints.
        """
        parsed = VideoParser(filename)
        whynot = ''
        has_s_e = bool(parsed.season is not None and parsed.episode is not None)
        has_s_or_e = bool(parsed.season is not None or parsed.episode is not None)
        has_yr = bool(parsed.year is not None)

        # Logic to check if the result seems reasonable based on the hint
        if parsed.hint == 'episode':
            seems_ok = bool(has_s_e or parsed.is_special)
            if not seems_ok:
                whynot = 'expecting TV episode but w/o Season+Episode'
        elif parsed.hint == 'movie':
            seems_ok = not has_s_or_e and not parsed.is_special and has_yr
            if not seems_ok:
                whynot = ('expecting movie but' + (' w Season or Episode' if has_s_or_e else ''
                        ) + ('' if has_yr else ' w/o Year'))
        else:
            seems_ok = (has_s_e and not has_yr) or (not has_s_or_e and has_yr) or parsed.is_special
            if not seems_ok:
                whynot = 'unsure of category but parses neither as TV episode/special nor movie'

        if verbose or not seems_ok:
            dirname, basename = os.path.dirname(filename), os.path.basename(filename)
            print('\n' + basename, f'IN {dirname}' if dirname else '')
            print(f'    {parsed.mini_str(verbose=True)}')
            if not seems_ok:
                print('    ERROR:', whynot)
        return seems_ok


    # --- Test Data ---
    tests_yaml = r"""
    !!omap
    - Yellowstone.2018.S03E08.720p.HEVC.x265-MeGusta.mkv: !!omap
      - result: tv0 "Yellowstone" s3e8 Y=2018 any .mkv
    - Law.and.Order.S18E01-E02.Called.Home.and.Darkness.2008.DVDRip.x264-TVV.mkv: !!omap
      - result: tv0 "Law and Order" s18e1-2 any .mkv
    - The.Amazing.World.of.Gumball.S03E13E14.720p.WEB-DL.AAC2.0.H.264-iT00NZ-7.mkv: !!omap
      - result: tv0 "The Amazing World of Gumball" s3e13-14 any .mkv
    - The Amazing World of Gumball - 132 - The Curse-28.mkv: !!omap
      - result: tv3 "The Amazing World of Gumball" s1e32 any .mkv
    - when.calls.the.heart.s06e07.repack.webrip.x264-tbs.mkv: !!omap
      - result: tv0 "when calls the heart" s6e7 repack any .mkv
    - penny.dreadful.205.hdtv-lol.mp4: !!omap
      - result: tv3 "penny dreadful" s2e5 any .mp4
    - American Experience s24e03-04 The Clinton Years.avi: !!omap
      - result: tv0 "American Experience" s24e3-4 any .avi
    - Homeland S01 E01 - 480p - BRRip - x264 - AAC 5.1 -={SPARROW}=-.mp4: !!omap
      - result: tv0 "Homeland" s1e1 any .mp4
    - Friends - [1x08] - The One where Nana dies Twice.mkv: !!omap
      - result: tv1 "Friends" s1e8 any .mkv
    - Captain Alatriste The Spanish Musketeer (2006).mkv: !!omap
      - result: mov0 "Captain Alatriste The Spanish Musketeer" Y=2006 any .mkv
    - Wonder.Woman.1984.2020.720p.HMAX.WEBRip.AAC2.0.X.264-EVO.mkv: !!omap
      - result: mov1 "Wonder Woman 1984" Y=2020 any .mkv
    - law.and.order.svu.220.dvdrip.avi: !!omap
      - result: tv3 "law and order svu" s2e20 any .avi
    - '[HorribleSubs] One Punch Man S2 - 11 [480p].mkv': !!omap
      - result: tv5 "One Punch Man" s2e11 any .mkv
    - Gone with the Wind.1939: !!omap
      - result: mov1 "Gone with the Wind" Y=1939 any n/a
    - Gone with the Wind.avi: !!omap
      - result: mov2 "Gone with the Wind" any .avi
    - Big.Little.Lies.Part.1.iNTERNAL.720p.HEVC.x265-MeGusta.mkv: !!omap
      - result: tv7 "Big Little Lies" s1e1 any .mkv
    - The.Flash.2014.S06E06.720p.HEVC.x265-MeGusta.mkv: !!omap
      - result: tv0 "The Flash" s6e6 Y=2014 any .mkv
    - Borat.PROPER.DVDRip.XviD-DoNE.avi: !!omap
      - result: mov2 "Borat" any .avi
    - Samurai Jack - 0xSpecial 4 - Genndy\'s Scrapbook.avi: !!omap
      - result: n/a "Samurai Jack - 0xSpecial 4 - Genndy\'s Scrapbook" any .avi
    - American Experience s22e06b Eleanor Roosevelt 2.mp4: !!omap
      - result: n/a "American Experience s22e06b Eleanor Roosevelt 2" any .mp4
    """

class Mangler:
    """ Mangles names with fixed translation table """
    translation_table = None
    title_pat = None

    def __init__(self):
        pass

    @staticmethod
    def mangle(text: str) -> str:
        """
        Mangles a string using a fixed, static character substitution table.
        This function preserves non-alphabetic characters (numbers, spaces, punctuation)
        and maintains the original casing (uppercase maps to uppercase, lowercase to lowercase).
        Args:
            text: The original string (e.g., 'Harry Wild').
        Returns:
            The mangled text (e.g., 'Wqrru Zild').
        """
        if Mangler.translation_table is None:
            # --- 1. Define the Fixed Substitution Mapping ---
            # Standard English alphabet (Source)
            source_lower = string.ascii_lowercase  # 'abcdefghijklmnopqrstuvwxyz'
            source_upper = string.ascii_uppercase  # 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            # Target mangled alphabet (This must be generated ONCE and fixed)
            target_lower = 'qazwsxedcrfvtgbyhnujmikolp'
            target_upper = 'QAZWSXEDCRFVTGBYHNUJMIKOLP'

            # --- 2. Create a Unified Translation Table (Maketrans) ---
            # We combine the source and target alphabets into a single translation table.
            # This is the most efficient way to perform character-by-character substitution in Python.
            Mangler.translation_table = str.maketrans(
                source_lower + source_upper,
                target_lower + target_upper
            )

        # --- 3. Apply the Translation ---
        # The translate method handles the substitution quickly.
        # Any characters not in the source_lower/source_upper strings (like numbers,
        # spaces, dashes, etc.) are automatically preserved.
        mangled_text = text.translate(Mangler.translation_table)

        return mangled_text

    @staticmethod
    def mangle_title(filename: str) -> str:
        """
        Static method to mangle a title without needing to instantiate Mangler.
        Args:
            title: The original title string.
        Returns:
            The mangled title string.
        """
        core, ext = os.path.splitext(filename)
        if not Mangler.title_pat:
            Mangler.title_pat = re.compile(r'(^.*?)\b(s\d+e\d+|\d+x\d+|\d\d\d|\d\d\d\d)', re.IGNORECASE)
        mat = Mangler.title_pat.match(core)
        size = len(mat.group(1)) if mat else 40
        rv = Mangler.mangle(core[:size]) + core[size:] + ext
        return rv


if __name__ == "__main__":
    # Example 1: Run full regression tests (verbose=False by default)
    # This prints only the summary and any failures.

    tests = yaml.load(VideoParser.tests_yaml)
    for video in tests:
        mangled = Mangler.mangle_title(video)
        print(f'{mangled=}    {video=}')


    VideoParser.run_regressions(verbose=False)

    # ---

    # Example 2: Test a specific file with verbose output
    print("\n" + "="*50)
    print("Testing specific file: Yellowstone.2018.S03E08.720p.HEVC.x265-MeGusta.mkv")
    print("="*50)
    VideoParser.parse_file("Yellowstone.2018.S03E08.720p.HEVC.x265-MeGusta.mkv", verbose=True)

    # Example 3: Test a file that fails to parse cleanly
    print("\n" + "="*50)
    print("Testing file that fails to parse as TV/Movie: Gone with the Wind.avi")
    print("="*50)
    VideoParser.parse_file("Gone with the Wind.avi", verbose=True)

    # Example 4: Test a file that is parsed as a movie
    print("\n" + "="*50)
    print("Testing Movie file: Captain Alatriste The Spanish Musketeer (2006).mkv")
    print("="*50)
    VideoParser.parse_file("Captain Alatriste The Spanish Musketeer (2006).mkv", verbose=True)
