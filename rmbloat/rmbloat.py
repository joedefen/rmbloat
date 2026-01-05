#!/usr/bin/env python3
# pylint: disable=too-many-statements
"""
TODO:
- make max height an option
- error strategy
  - to the probe cache, add an "exception" field
  - For errors in transcoding, I was thinking of exception values "Er1" ... "Er9" where the number is
    the number of prior failures capped at 9.   When automatically selecting videos, Er1 would be ignored,
    in effect, and Er2 or higher would be blocked showing "Er2" instead of a checkbox ... but it
    would act as unchecked "[ ]" so it could be manually checked showing [X].
 - Similarly, if there was not enough space reduction, the exception value would be "OPT"
   (already space optimized ... a better designation?), and again those act as if unchecked and
   can be manually checked (for re-encoding ... presumably because the CMF option was changed
   or something that might allow the encoding to succeed.)
- V2.0
  - fully automated daemon (still running as curses app)
  - runs during certain hours ... restarts itself to freshly read
    disk

- V3.0
  - merge in missing subtitles
    ffmpeg -i video.mkv -i video.en.srt
    -map 0 -map 1:s:0
    -c copy   # Copy all streams, no re-encoding
    -c:s srt   # Only subtitle needs codec
    -metadata:s:s:0 language=eng
    video.sb.mkv
"""

# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return,too-many-branches
# pylint: disable=too-many-return-statements,too-many-instance-attributes
# pylint: disable=consider-using-with,line-too-long,too-many-lines
# pylint: disable=too-many-nested-blocks,try-except-raise,line-too-long
# pylint: disable=too-many-public-methods,invalid-name

import sys
import os
import argparse
import traceback
import atexit
import re
import time
import json
import curses
from dataclasses import asdict
from types import SimpleNamespace
from console_window import (ConsoleWindow, OptionSpinner, Screen, ScreenStack,
                            ConsoleWindowOpts, IncrementalSearchBar, Context)
from .ProbeCache import ProbeCache
from .VideoParser import Mangler
from .IniManager import IniManager
from .StructuredLogger import StructuredLogger
from .CpuStatus import CpuStatus
from .FfmpegChooser import FfmpegChooser
from .Models import PathProbePair, Vid
from . import FileOps
from . import ConvertUtils
from .JobHandler import JobHandler
from .SystemDiscovery import get_system_discovery

lg = StructuredLogger('rmbloat')

# Screen constants
SELECT_ST = 0
CONVERT_ST = 1
HISTORY_ST = 2
HELP_ST = 3
SCREEN_NAMES = ('SELECT', 'CONVERT', 'HISTORY', 'HELP')

# File operation functions moved to FileOps.py
sanitize_file_paths = FileOps.sanitize_file_paths

def store_cache_on_exit():
    """ TBD """
    if Converter.singleton:
        if Converter.singleton.win:
            Converter.singleton.win.stop_curses()
        if Converter.singleton.probe_cache:
            Converter.singleton.probe_cache.store()

# Data models moved to Models.py
# FfmpegMon class moved to FfmpegMon.py
# Job class moved to Models.py

class Converter:
    """ TBD """
    # --- Conversion Criteria Constants (Customize these) ---
    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    TARGET_CODECS = ['h265', 'hevc']
    MAX_BITRATE_KBPS = 2100 # about 15MB/min (or 600MB for 40m)

    # Constants moved to ConvertUtils.py
    VIDEO_EXTENSIONS = ConvertUtils.VIDEO_EXTENSIONS
    SKIP_PREFIXES = ConvertUtils.SKIP_PREFIXES
    singleton = None

    def __init__(self, opts, cache_dir='/tmp'):
        assert Converter.singleton is None
        Converter.singleton = self
        self.win = None
        self.redraw_mono = time.monotonic()
        self.opts = opts
        self.spinner = None # spinner object
        self.spins = None # spinner values
        self.search_re = None # the "accepted" search
        self.vids = []
        self.todo_vids = []
        self.visible_vids = []
        self.original_cwd = os.getcwd()
        self.ff_pre_i_opts = []
        self.ff_post_i_opts = []
        self.ff_thread_opts = []
        self.state = 'probe' # 'select', 'convert'
        self.job = None
        self.prev_time_encoded_secs = -1
        # Be quiet if user has already selected a specific strategy
        quiet_chooser = bool(opts.prefer_strategy != 'auto')
        self.chooser = FfmpegChooser(force_pull=False, prefer_strategy=opts.prefer_strategy, quiet=quiet_chooser)
        self.probe_cache = ProbeCache(cache_dir_name=cache_dir, chooser=self.chooser)
        self.probe_cache.load()
        self.probe_cache.store()
        self.start_job_mono = 0
        self.cpu = CpuStatus()
        # self.cgroup_prefix = set_cgroup_cpu_limit(opts.thread_cnt*100)
        atexit.register(store_cache_on_exit)

        # Auto mode tracking (separate from JobHandler for overall session tracking)
        self.auto_mode_enabled = bool(opts.auto_hr is not None)
        self.auto_mode_hrs_limit = opts.auto_hr if self.auto_mode_enabled else None

        # Job handler (created when entering convert screen, destroyed when leaving)
        self.job_handler = None

        # Session flag: allow re-encoding of already re-encoded files (DUN status)
        self.allow_reencode_dun = False

        # Build options suffix for display
        self.options_suffix = self.build_options_suffix()

    def build_options_suffix(self):
        """Build the options suffix string for display."""
        parts = []
        parts.append(f'Q={self.opts.quality}')
        parts.append(f'Shr>={self.opts.min_shrink_pct}')
        if self.opts.dry_run:
            parts.append('DRYRUN')
        if self.opts.sample:
            parts.append('SAMPLE')
        if self.opts.keep_backup:
            parts.append('KeepB')
        if self.opts.merge_subtitles:
            parts.append('MrgSrt')
        if self.auto_mode_enabled:
            parts.append(f'Auto={self.opts.auto_hr}hr')
        return ' -- ' + ' '.join(parts)

    def make_lines(self, doit_skips=None):
        """Generate display lines and stats for video list"""
        lines, self.visible_vids, short_list = [], [], []
        jobcnt, co_wid = 0, len('CODEC')
        stats = SimpleNamespace(total=0, picked=0, done=0, progress_idx=0,
                                gb=0, delta_gb=0)

        # Determine which list to use based on screen
        vid_list = self.todo_vids if (hasattr(self, 'stack') and
                                      self.stack.curr.num == CONVERT_ST) else self.vids

        for vid in vid_list:
            if doit_skips and vid.doit in doit_skips:
                continue
            short_list.append(vid)
            co_wid = max(co_wid, len(vid.codec))

        for vid in short_list:
            basename = vid.basename1 if vid.basename1 else os.path.basename(vid.filepath)
            dirname = os.path.dirname(vid.filepath)
            if self.spins.mangle:
                basename = Mangler.mangle_title(basename)
                dirname = Mangler.mangle(dirname)
            res = f'{vid.height}p'
            ht_over = ' ' if vid.res_ok else '^'
            br_over = ' ' if vid.bloat_ok else '^'
            co_over = ' ' if vid.codec_ok else '^'
            mins = int(round(vid.duration / 60))
            line = f'{vid.doit:>3} {vid.net} {vid.bloat:5}{br_over} {res:>5}{ht_over}'
            line += f' {vid.codec:>{co_wid}}{co_over} {mins:>4} {1024*vid.gb:>6.0f}'
            line += f'   {basename}'
            if self.spins.directory:
                line += f' ---> {dirname}'
            if self.spins.search:
                pattern = self.spins.search
                if self.spins.mangle:
                    pattern = Mangler.mangle(pattern)
                match = re.search(pattern, line, re.IGNORECASE)
                if not match:
                    continue
            if vid.doit == '[X]':
                stats.picked += 1
            if vid.doit not in ('[X]', '[ ]', 'IP '):
                stats.done += 1

            if vid.probe0:
                gb, delta_gb = vid.probe0.gb, 0
                if vid.probe1 and not self.opts.sample:
                    delta_gb = vid.probe1.gb - gb
                stats.gb += gb
                stats.delta_gb += delta_gb
            lines.append(line)
            self.visible_vids.append(vid)
            if self.job and self.job.vid == vid:
                jobcnt += 1
                lines.append(f'-----> {self.job.progress}')
                stats.progress_idx = len(self.visible_vids)
                self.visible_vids.append(None)
                if self.win.pick_mode:
                    stats.progress_idx -= 1
                    self.win.set_pick_mode(False)

        stats.total = len(self.visible_vids) - jobcnt
        stats.gb = round(stats.gb, 1)
        stats.delta_gb = round(stats.delta_gb, 1)
        return lines, stats, co_wid

    def toggle_doit(self, vid):
        """Toggle the doit status of a video"""
        if vid.doit == '[X]':
            vid.doit = vid.doit_auto if vid.doit_auto != '[X]' else '[ ]'
        elif vid.doit == 'DUN':
            # First time toggling DUN status - prompt user
            if not self.allow_reencode_dun:
                answer = self.win.answer("Enable re-encoding of already re-encoded files for this session? (y/n): ")
                if answer and answer.lower() == 'y':
                    self.allow_reencode_dun = True
                    vid.doit = '[X]'
                # else: leave as DUN
            else:
                # Already allowed in this session
                vid.doit = '[X]'
        elif not vid.doit.startswith('?') and not self.dont_doit(vid):
            vid.doit = '[X]'

    def advance_jobs(self):
        """Process ongoing conversion jobs"""
        # Handle running job progress
        if self.job and self.stack.curr.num in (CONVERT_ST, HELP_ST):
            while True:
                if self.opts.dry_run:
                    delta = time.monotonic() - self.job.start_mono
                    got = 0 if delta >= 3 else f'{delta=}'
                else:
                    got = self.get_job_progress(self.job)
                if isinstance(got, str):
                    self.job.progress = got
                elif isinstance(got, int):
                    # Check if we should retry with error tolerance
                    should_retry = (got != 0 and
                                  (not hasattr(self.job, 'is_retry') or not self.job.is_retry) and
                                  self._should_retry_with_error_tolerance(self.job.vid))

                    if should_retry:
                        # Retry with error tolerance
                        lg.put('WARN', 'RETRY-WITH-ERROR-TOLERANCE',
                            f'Retrying {self.job.vid.filebase} with error tolerance flags')
                        vid = self.job.vid
                        # Mark the error in probe cache before retrying
                        self.probe_cache.set_anomaly(vid.filepath, 'Err')
                        vid.doit = '[X]'  # Reset for retry
                        self.job = self.start_transcode_job(vid, retry_with_error_tolerance=True)
                        vid.doit = 'IP '
                        break  # Continue with retry job

                    # Job finished (either success or final failure after retry)
                    self.job.vid.doit = ' OK' if got == 0 else 'ERR'
                    self.job.vid.doit_auto = self.job.vid.doit
                    self.finish_transcode_job(
                        success=bool(got == 0), job=self.job)
                    dumped = asdict(self.job.vid)
                    # asdict() automatically handles nested Probe dataclasses
                    if got == 0:
                        dumped['texts'] = []

                    if self.opts.sample:
                        title = 'SAMPLE'
                    elif self.opts.dry_run:
                        title = 'DRY-RUN'
                    elif hasattr(self.job, 'is_retry') and self.job.is_retry:
                        title = 'RE-ENCODE-TO-H265-RETRY'
                    else:
                        title = 'RE-ENCODE-TO-H265'

                    lg.put('OK' if got == 0 else 'ERR',
                        title + ' ', json.dumps(dumped, indent=4))
                    self.job = None
                    break  # finished job
                else:
                    break  # no progress on job

        # Start new jobs if on convert screen
        if self.stack.curr.num == CONVERT_ST and not self.job:
            gonners = []
            for vid in self.visible_vids:
                if not vid:
                    continue
                if vid.doit == '[X]':
                    if not os.path.isfile(vid.filepath):
                        gonners.append(vid)
                        continue
                    if not self.job:  # start only one job
                        self.prev_time_encoded_secs = -1
                        self.job = self.start_transcode_job(vid)
                        vid.doit = 'IP '
            if gonners:  # any disappearing files?
                vids = []
                for vid in self.vids:
                    if vid not in gonners:
                        vids.append(vid)
                self.vids = vids  # pruned list
                # Convert Vid dataclass objects to dicts for JSON serialization
                gonners_data = [asdict(v) for v in gonners]
                lg.err('videos disappeared before conversion:\n'
                    + json.dumps(gonners_data, indent=4))

            if not self.job:
                # Check auto mode exit conditions
                if self.auto_mode_enabled:
                    # Calculate current stats for vitals report
                    _, stats, _ = self.make_lines()

                    # Check exit conditions
                    time_exceeded = False
                    if self.job_handler.auto_mode_start_time and self.auto_mode_hrs_limit:
                        runtime_hrs = (time.monotonic() - self.job_handler.auto_mode_start_time) / 3600
                        time_exceeded = runtime_hrs >= self.auto_mode_hrs_limit

                    no_more_todo = (stats.total - stats.done) == 0
                    too_many_failures = self.job_handler.consecutive_failures >= 10

                    if time_exceeded or no_more_todo or too_many_failures:
                        self.print_auto_mode_vitals(stats)
                        # print_auto_mode_vitals exits, so we never reach here

                # Destroy JobHandler when leaving convert screen (resets counters)
                self.job_handler = None
                self.vids.sort(key=lambda vid: (vid.all_ok, vid.bloat), reverse=True)
                self.stack.pop()  # Return to select screen

    def is_allowed_codec(self, probe):
        """ Return whether the codec is 'allowed' """
        if not probe:
            return True
        if not re.match(r'^[a-z]\w*$', probe.codec, re.IGNORECASE):
            # if not a codec name (e.g., "---"), then it is OK
            # in the sense we will not choose it as an exception
            return True
        codec_ok = bool(self.opts.allowed_codecs == 'all')
        if self.opts.allowed_codecs == 'x265':
            codec_ok = bool(probe.codec in ('hevc',))
        if self.opts.allowed_codecs == 'x26*':
            codec_ok = bool(probe.codec in ('hevc','h264'))
        return codec_ok

    def apply_probe(self, vid, probe):
        """ TBD """
        # shorthand
        vid.width = probe.width
        vid.height = probe.height
        vid.codec = probe.codec
        vid.bloat = probe.bloat
        vid.duration = probe.duration
        vid.gb = probe.gb

        vid.codec_ok = self.is_allowed_codec(probe)

        vid.res_ok = bool(vid.height is not None and vid.height <= self.TARGET_HEIGHT)
        vid.bloat_ok = bool(vid.bloat < self.opts.bloat_thresh)
        vid.all_ok = bool(vid.res_ok and vid.bloat_ok and vid.codec_ok)

        # vid.summary = (f'  {vid.width}x{vid.height}' +
        #               f' {vid.codec} {vid.bloat}b {vid.gb}G')
        return probe

    def append_vid(self, ppp):
        """
        Checks if a video file already meets the updated conversion criteria:
        1. Resolution is at least TARGET_WIDTH x TARGET_HEIGHT.
        2. Video codec is TARGET_CODECS (e.g., 'h264').
        3. Video "bloat" is below bloat_thresh.

        Args:
            filepath (str): The path to the video file.

        Returns:
            bool: True if the file meets all criteria, False otherwise.
        """

        vid = Vid()
        vid.post_init(ppp)
        vid.probe0 = self.apply_probe(vid, ppp.probe)
        self.vids.append(vid)

        anomaly = vid.probe0.anomaly # shorthand
        if anomaly and anomaly not in ('Er1', ):
            vid.doit = anomaly
        else:
            # Check if file should be excluded from encoding
            exclusion_status = self.dont_doit(vid)
            if exclusion_status:
                vid.doit = exclusion_status  # 'DUN', 'OK', etc.
            elif vid.all_ok:
                vid.doit = '[ ]'
            else:
                vid.doit = '[X]'
        vid.doit_auto = vid.doit # auto value of doit saved for ease of re-init

    # Utility functions moved to ConvertUtils.py
    bash_quote = staticmethod(ConvertUtils.bash_quote)

    # Job handling methods delegated to JobHandler
    def start_transcode_job(self, vid):
        """Delegate to JobHandler"""
        return self.job_handler.start_transcode_job(vid, self.bash_quote)

    def monitor_transcode_progress(self, job):
        """Delegate to JobHandler"""
        return self.job_handler.monitor_transcode_progress(job)

    def get_job_progress(self, job):
        """Delegate to JobHandler"""
        return self.job_handler.get_job_progress(job)

    def finish_transcode_job(self, success, job):
        """Delegate to JobHandler and apply probe if returned"""
        probe = self.job_handler.finish_transcode_job(success, job, self.is_allowed_codec)
        # Apply probe if one was returned
        if probe:
            job.vid.probe1 = self.apply_probe(job.vid, probe)

    # Utility methods delegated to ConvertUtils
    human_readable_size = staticmethod(ConvertUtils.human_readable_size)
    is_valid_video_file = staticmethod(ConvertUtils.is_valid_video_file)
    get_candidate_video_files = staticmethod(ConvertUtils.get_candidate_video_files)

    def standard_name(self, pathname: str, height: int) -> tuple[bool, str]:
        """
        Delegates to ConvertUtils.standard_name() with quality from opts.

        Returns: Whether changed and filename string with x265 codec.
        """
        return ConvertUtils.standard_name(pathname, height, self.opts.quality)

    def bulk_rename(self, old_file_name: str, new_file_name: str,
                    trashes: set):
        """
        Renames files and directories in the current working directory (CWD).

        Delegates to FileOps.bulk_rename() for the actual operation.
        """
        return FileOps.bulk_rename(old_file_name, new_file_name, trashes, self.opts.dry_run)

    def process_one_ppp(self, ppp):
        """ Handle just one """
        input_file = ppp.video_file
        if not self.is_valid_video_file(input_file):
            return  # Skip to the next file in the loop

        # --- File names for the safe replacement process ---
        do_rename, standard_name = self.standard_name(input_file, ppp.probe.height)

        ppp.do_rename = do_rename
        ppp.standard_name = standard_name

        self.append_vid(ppp)

    def create_video_file_list(self):
        """ TBD """
        ppps = []

        # Get candidate video file paths
        paths_to_probe, read_pipe = self.get_candidate_video_files(self.opts.files)

        # --- Restore TTY Input if needed ---
        if read_pipe:
            try:
                # 2a. Close the current stdin (the pipe)
                sys.stdin.close()
                # 2b. Open the TTY device (the actual keyboard/terminal)
                # os.O_RDONLY is read-only access.
                tty_fd = os.open('/dev/tty', os.O_RDONLY)
                # 2c. Replace file descriptor 0 (stdin) with the TTY descriptor
                # os.dup2(old_fd, new_fd) copies the old_fd to the new_fd (FD 0).
                os.dup2(tty_fd, 0)
                # 2d. Re-create the sys.stdin file object for Python's I/O
                # os.fdopen(0, 'r') creates a new Python file object from FD 0.
                sys.stdin = os.fdopen(0, 'r')
                # 2e. Close the original file descriptor variable (tty_fd)
                os.close(tty_fd)

            except OSError as e:
                # This handles cases where /dev/tty is not available (e.g., some non-interactive environments)
                sys.stderr.write(f"Error reopening TTY: {e}. Cannot enter interactive mode.\n")
                sys.exit(1)

        # 5. Final Probing and Progress Indicator 🎬
        total_files = len(paths_to_probe)
#       probe_count = 0
#       update_interval = 10  # Update the line every 10 probes

        if total_files > 0:
            # Print the initial line to start the progress bar
            sys.stderr.write(f"probing: 0% 0 of {total_files}\r")
            sys.stderr.flush()

        results = self.probe_cache.batch_get_or_probe(paths_to_probe)
        for file_path, probe in results.items():
            ppp = PathProbePair(file_path, probe)
            ppps.append(ppp)

        return ppps

    def dont_doit(self, vid):
        """
        Check if video should be excluded from re-encoding.

        Returns:
            str or None: Status string if should be excluded ('DUN', 'OK'), None otherwise
        """
        base = os.path.basename(vid.filepath).lower()

        # Already re-encoded files get "DUN" (done) status
        if base.endswith('.recode.mkv'):
            return 'DUN'

        # Files marked as OK from previous successful conversion or skip
        if vid.doit in ('OK', '---'):
            return 'OK'

        # Note: SAMPLE. and TEST. files are now excluded at is_valid_video_file() level

        return None

    def print_auto_mode_vitals(self, stats):
        """Print vitals report and exit for auto mode."""
        runtime_hrs = (time.monotonic() - self.job_handler.auto_mode_start_time) / 3600

        # Calculate space bloated from remaining TODO items
        space_bloated_gb = 0.0
        for vid in self.vids:
            if vid.doit == '[X]' and vid.probe0:
                space_bloated_gb += vid.probe0.gb

        # Format report
        report = "\n" + "=" * 70 + "\n"
        report += "AUTO MODE VITALS REPORT\n"
        report += "=" * 70 + "\n"
        report += f"Runtime:              {runtime_hrs:.2f} hours\n"
        report += f"OK conversions:       {self.job_handler.ok_count}\n"
        report += f"Error conversions:    {self.job_handler.error_count}"

        if self.job_handler.consecutive_failures >= 10:
            report += " (early termination: 10 consecutive failures)\n"
        else:
            report += "\n"

        report += f"Remaining TODO:       {stats.total - stats.done}\n"
        report += f"Space saved:          {abs(stats.delta_gb):.2f} GB\n"
        report += f"Space still bloated:  {space_bloated_gb:.2f} GB\n"
        report += "=" * 70 + "\n"

        # Print to screen
        if self.win:
            self.win.stop_curses()
        print(report)

        # Log to file
        lg.lg(report)

        # Exit with appropriate code
        exit_code = 1 if self.job_handler.consecutive_failures >= 10 else 0
        sys.exit(exit_code)

    def handle_search_change(self):
        """Handle search pattern changes"""
        if self.spins.search != self.search_re:
            valid = True
            if self.stack.curr.num != SELECT_ST:
                self.win.alert(message='Cannot change search unless in select screen')
                valid = False
            try:
                re.compile(self.spins.search)
            except Exception as exc:
                self.win.alert(message=f'Ignoring invalid search: {exc}')
                valid = False
            if valid:
                self.search_re = self.spins.search
            else:  # ignore pattern changes unless in select or if won't compile
                self.spins.search = self.search_re

    def do_window_mode(self):
        """Main UI loop using Screen-based architecture"""
        # Create ConsoleWindow
        win_opts = ConsoleWindowOpts(
            head_line=True,
            head_rows=4,
            body_rows=10+len(self.vids),
            min_cols_rows=(60,10),
            ctrl_c_terminates=True,
        )
        self.win = win = ConsoleWindow(win_opts)
        # Initialize screens
        self.screens = {
            SELECT_ST: SelectScreen(self),
            CONVERT_ST: ConvertScreen(self),
            HELP_ST: HelpScreen(self),
            HISTORY_ST: HistoryScreen(self),
        }
        self.stack = ScreenStack(self.win, None, SCREEN_NAMES, self.screens)
        # Setup OptionSpinner
        self.spinner = spin = OptionSpinner(stack=self.stack)
        spin.default_obj = self.opts
        spin.add_key('help_mode', '? - help screen', genre="action")
        spin.add_key('screen_escape', 'ESC - return to previous screen',
                     keys=27, genre="action")
        spin.add_key('history', 'h - go to History Screen', genre='action',
                     scope=[SELECT_ST, CONVERT_ST])
        spin.add_key('expand', 'e - expand/collapse log entry', genre='action')
        spin.add_key('copy_log', 'c - copy log entry to clipboard', genre='action',
                     scope=HISTORY_ST)
        spin.add_key('reset_all', 'r - reset all to "[ ]"', genre='action')
        spin.add_key('init_all', 'i - set all automatic state', genre='action')
        spin.add_key('toggle', 'SP - toggle current line state', genre='action',
                     keys={ord(' '), })
        spin.add_key('skip', 's - skip reencoding --> "---"', genre='action')
        spin.add_key('go', 'g - begin conversions', genre='action')
        spin.add_key('quit', 'q - quit converting OR exit app', genre='action',
                     keys={ord('q'), 0x3})
        spin.add_key('freeze', 'p - pause/release screen', vals=[False, True])
        spin.add_key('directory', 'd - show directory', vals=[False, True])
        spin.add_key('search', '/ - search string',
                      prompt='Set search string, then Enter', scope=SELECT_ST)
        spin.add_key('hist_search', '/ - search string', genre="action",
                      scope=HISTORY_ST)
        spin.add_key('mangle', 'm - mangle titles', vals=[False, True])
        # NOTE: With relax_handled_keys=True (default in console_window),
        # we no longer need to call set_handled_keys() - all non-navigation
        # keys are automatically passed to the app
        # other = {curses.KEY_ENTER, 10}
        # self.win.set_handled_keys(spin.keys | other)

        self.spins = spins = spin.default_obj

        curses.intrflush(False)


        # Start in select screen
        win.set_pick_mode(True, 1)

        # Main event loop
        force_redraw = False
        while True:
            # Draw current screen
            if not spins.freeze:
                current_screen = self.screens[self.stack.curr.num]
                current_screen.draw_screen()
                redraw = bool(time.monotonic() - self.redraw_mono >= 60) or force_redraw
                self.redraw_mono = time.monotonic() if redraw else self.redraw_mono
                win.render(redraw=redraw)
                force_redraw = False  # Reset after use

            # Get user input with screen-specific timeout
            current_screen = self.screens[self.stack.curr.num]
            timeout = current_screen.prompt_timeout
            key = win.prompt(seconds=timeout)

            # Handle IncrementalSearchBar if active on history screen
            if key and self.stack.curr.num == HISTORY_ST:
                screen = self.stack.get_curr_obj()
                if screen and screen.search_bar.is_active:
                    if screen.search_bar.handle_key(key):
                        force_redraw = True  # Force full redraw on next iteration
                        key = None

            # Process spinner keys
            if key in spin.keys:
                spin.do_key(key, win)

            # Handle search pattern changes
            if self.stack.curr.num == SELECT_ST:
                self.handle_search_change()

            # Let ScreenStack perform any pending actions
            self.stack.perform_actions(spin)

            # Auto-transition to convert state if auto mode enabled
            if self.auto_mode_enabled and self.stack.curr.num == SELECT_ST and not self.job:
                # Collect checked videos
                self.todo_vids = [v for v in self.vids if v.doit == '[X]']
                if self.todo_vids:
                    # Create JobHandler when entering convert screen
                    self.job_handler = JobHandler(
                        self.opts,
                        self.chooser,
                        self.probe_cache,
                        auto_mode_enabled=self.auto_mode_enabled
                    )
                    self.stack.push(CONVERT_ST, win.pick_pos)

            # Process jobs
            self.advance_jobs()

            # Clear screen for next render
            if not spins.freeze:
                win.clear()

    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        ppps = self.create_video_file_list()
        self.probe_cache.store()
        ppps.sort(key=lambda vid: vid.probe.bloat, reverse=True)

        if not ppps:
            print("Usage: rmbloat {options} {video_file}...")
            sys.exit(1)

        # --- The main loop change is here ---
        for ppp in ppps:
            input_file_path_str = ppp.video_file
            file_dir, _ = os.path.split(input_file_path_str)
            if not file_dir:
                file_dir = os.path.abspath(os.path.dirname(input_file_path_str))

            # Use a try...finally block to ensure you always change back.
            try:
                os.chdir(file_dir)
                self.process_one_ppp(ppp)

            except Exception:
                raise
                # print(f"An error occurred while processing {file_basename}: {e}")
            finally:
                os.chdir(self.original_cwd)
        self.do_window_mode()


# ============================================================================
# Screen Classes
# ============================================================================

class RmbloatScreen(Screen):
    """Base screen class for rmbloat video converter"""
    app: 'Converter'  # Type hint for IDE
    prompt_timeout = 3.0  # Default timeout in seconds for win.prompt()

    def screen_escape_ACTION(self):
        """ESC key - Return to previous screen"""
        if self.app.stack.curr.num != SELECT_ST:
            self.app.stack.pop()
    
    def help_mode_ACTION(self):
        """? - enter help mode"""
        app = self.app
        if app.stack.curr.num != HELP_ST:
            app.stack.push(HELP_ST, app.win.pick_pos)
    
    def history_ACTION(self):
        """h - go to History Screen"""
        app = self.app
        if app.stack.curr.num in (SELECT_ST, CONVERT_ST):
            app.stack.push(HISTORY_ST)



class SelectScreen(RmbloatScreen):
    """Video selection screen - choose which videos to convert"""

    def draw_screen(self):
        """Draw the video selection screen"""
        app = self.app
        win = app.win
        spins = app.spins

        app.win.set_pick_mode(True)

        # Get video list and stats
        lines, stats, co_wid = app.make_lines()

        # Header line with keys
        head = '[r]setAll [i]nit SP:toggle [s]kip [g]o [h]ist ?:help [q]uit'
        if app.search_re:
            shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
            head += f' /{shown}'
        win.add_fancy_header(head, "Underline")

        # Stats header
        cpu_status = app.cpu.get_status_string()
        win.add_header(f'     Picked={stats.picked}/{stats.total}'
                      f'  GB={stats.gb}({stats.delta_gb})'
                      f'  {cpu_status}')

        # Column headers
        win.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                      f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        # Video list
        for line in lines:
            win.add_body(line)

    def reset_all_ACTION(self):
        """'r' key - Reset all checkboxes"""
        app = self.app
        for vid in app.visible_vids:
            if vid and vid.doit_auto.startswith('['):
                vid.doit = '[ ]'

    def init_all_ACTION(self):
        """'i' key - Initialize all to auto state"""
        app = self.app
        for vid in app.visible_vids:
            if vid:
                vid.doit = vid.doit_auto

    def toggle_ACTION(self):
        """Space key - Toggle current line state"""
        app = self.app
        idx = app.win.pick_pos
        if 0 <= idx < len(app.visible_vids):
            vid = app.visible_vids[idx]
            if vid:
                app.toggle_doit(vid)
                app.win.pick_pos += 1

    def skip_ACTION(self):
        """'s' key - Skip/unskip current video"""
        app = self.app
        win = app.win
        idx = win.pick_pos
        if 0 <= idx < len(app.visible_vids):
            vid = app.visible_vids[idx]
            if vid:
                if vid.doit.startswith(('[', 'DUN')):
                    vid.doit = '---'
                    app.probe_cache.set_anomaly(vid.filepath, '---')
                elif vid.doit == '---':
                    if vid.doit_auto == '---':
                        vid.doit_auto = '[ ]'
                    vid.doit = vid.doit_auto
                    app.probe_cache.set_anomaly(vid.filepath, None)
                win.pick_pos += 1

    def go_ACTION(self):
        """'g' key - Go to convert screen"""
        app = self.app

        # Collect checked videos
        app.todo_vids = []
        for vid in app.vids:
            if vid.doit == '[X]':
                app.todo_vids.append(vid)

        if app.todo_vids:
            # Create JobHandler when entering convert screen
            app.job_handler = JobHandler(
                app.opts,
                app.chooser,
                app.probe_cache,
                auto_mode_enabled=app.auto_mode_enabled
            )
            app.stack.push(CONVERT_ST, app.win.pick_pos)

    def quit_ACTION(self):
        """'q' key - Quit application"""
        sys.exit(0)


class ConvertScreen(RmbloatScreen):
    """Conversion progress screen - shows encoding progress"""

    def draw_screen(self):
        """Draw the conversion progress screen"""
        app = self.app
        win = app.win
        spins = app.spins

        app.win.set_pick_mode(False)

        # Get video list and stats
        lines, stats, co_wid = app.make_lines()

        # Header line with keys
        head = ' [s]kip ?=help [q]uit'
        if app.search_re:
            shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
            head += f' /{shown}'

        cpu_status = app.cpu.get_status_string()
        head += (f'     ToDo={stats.total-stats.done}/{stats.total}'
                f'  GB={stats.gb}({stats.delta_gb})'
                f'  {cpu_status}')
        win.add_header(head)

        # Column headers
        win.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                      f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        # Set scroll position to show current job
        win.pick_pos = stats.progress_idx
        win.scroll_pos = stats.progress_idx - win.scroll_view_size + 2

        # Video list
        for line in lines:
            win.add_body(line)

    def skip_ACTION(self):
        """'s' key - Skip current conversion job"""
        app = self.app
        if app.job:
            vid = app.job.vid
            app.job.ffsubproc.stop()
            app.job = None
            vid.doit = '---'
            app.probe_cache.set_anomaly(vid.filepath, '---')

    def quit_ACTION(self):
        """'q' key - Return to select screen (stop conversions)"""
        app = self.app
        if app.job:
            app.job.ffsubproc.stop()
            app.job.vid.doit = '[X]'
            app.job = None

        # Disable auto mode when user interrupts
        if app.auto_mode_enabled:
            app.auto_mode_enabled = False
            app.options_suffix = app.build_options_suffix()

        app.stack.pop()


class HelpScreen(RmbloatScreen):
    """Help screen showing keyboard shortcuts"""

    def draw_screen(self):
        """Draw the help screen"""
        app = self.app
        app.spinner.show_help_nav_keys(app.win)
        app.spinner.show_help_body(app.win)

class HistoryScreen(RmbloatScreen):
    """History screen showing log entries with expand/collapse functionality"""
    prompt_timeout = 60.0  # Slower refresh for reading/copying logs

    def __init__(self, app):
        super().__init__(app)
        self.expands = {}  # Maps timestamp -> expansion state (0=collapsed, 1=partial, 2=full)
        self.entries = []  # Cached log entries (all entries before filtering)
        self.filtered_entries = []  # Entries after search filtering
        self.visible_lines = []  # Maps display line index -> timestamp (None for expanded/blank lines)
        self.window_of_logs = None  # Window of log entries (OrderedDict)
        self.window_state = None  # Window state for incremental reads
        self.search_matches = {}  # Maps timestamp -> True if deep match found in JSON
        self.prev_filter = ''

        # Setup search bar
        self.search_bar = IncrementalSearchBar(
            on_change=self._on_search_change,
            on_accept=self._on_search_accept,
            on_cancel=self._on_search_cancel
        )

    def _on_search_change(self, text):
        """Called when search text changes - filter entries incrementally."""
        self._filter_entries(text)

    def _on_search_accept(self, text):
        """Called when ENTER pressed in search - keep filter active, exit input mode."""
        self.app.win.passthrough_mode = False
        # Filter remains active - just exit typing mode
        self.prev_filter = text

    def _on_search_cancel(self, original_text):
        """Called when ESC pressed in search - restore and exit search mode."""
        self._filter_entries(original_text)
        self.app.win.passthrough_mode = False

    def _filter_entries(self, search_text):
        """Filter entries based on search text (shallow or deep)."""
        if not search_text:
            self.filtered_entries = self.entries
            self.search_matches = {}
            return

        # Deep search mode if starts with /
        deep_search = search_text.startswith('/')
        pattern = search_text[1:] if deep_search else search_text

        if not pattern:
            self.filtered_entries = self.entries
            self.search_matches = {}
            return

        # Case-insensitive search
        pattern_lower = pattern.lower()

        filtered = []
        matches = {}

        for entry in self.entries:
            # Extract visible text (what shows in collapsed view)
            timestamp = entry.timestamp[:19]
            level = entry.level if hasattr(entry, 'level') else "???"

            # Get filebase
            filebase = None
            json_str = None
            try:
                msg = entry.message if entry.message else ""
                if '{' in msg:
                    json_start = msg.index('{')
                    json_str = msg[json_start:]
                    data = json.loads(json_str)
                    filebase = data.get('filebase', data.get('file', None))
            except (json.JSONDecodeError, ValueError, AttributeError, KeyError):
                pass

            if not filebase:
                if entry.message:
                    filebase = entry.message[:70]
                else:
                    filebase = f"{entry.file}:{entry.line} {entry.function}()"

            # Shallow search: check visible text
            visible_text = f"{timestamp} {level} {filebase}".lower()
            shallow_match = pattern_lower in visible_text

            # Deep search: also check JSON content
            deep_match = False
            if deep_search and json_str:
                deep_match = pattern_lower in json_str.lower()

            if shallow_match or deep_match:
                filtered.append(entry)
                if deep_match and not shallow_match:
                    matches[entry.timestamp] = True  # Mark as deep-only match using timestamp

        self.filtered_entries = filtered
        self.search_matches = matches

    def draw_screen(self):
        """Draw the history screen"""
        app = self.app
        win = app.win
        win.set_pick_mode(True)

        # Get window of log entries (chronological order - eldest to youngest)
        if self.window_of_logs is None:
            self.window_of_logs, self.window_state = lg.get_window_of_entries(window_size=1000)
        else:
            # Refresh window with any new entries
            self.window_of_logs, self.window_state = lg.refresh_window(self.window_of_logs, self.window_state, window_size=1000)

        # Convert to list in reverse order (newest first for display)
        self.entries = list(reversed(list(self.window_of_logs.values())))

        # Clean up self.expands: remove any timestamps that are no longer in entries
        valid_timestamps = {entry.timestamp for entry in self.entries}
        self.expands = {ts: state for ts, state in self.expands.items() if ts in valid_timestamps}

        # Apply search filter if active
        if not self.search_bar.text:
            self.filtered_entries = self.entries
            self.search_matches = {}

        # Count errors vs others in filtered results
        err_count = sum(1 for e in self.filtered_entries if e.level == 'ERR')
        other_count = len(self.filtered_entries) - err_count

        # Build search display string
        search_display = self.search_bar.get_display_string(prefix='', suffix='')

        # Header
        header_line = f'ESC:back [e]xpand [c]opy/export {len(self.filtered_entries)}/{len(self.entries)} (ERR:{err_count} oth:{other_count}) /'
        if search_display:
            header_line += f'{search_display}'
        win.add_header(header_line)
        win.add_header(f'Log file: {lg.log_file}')

        # Build display
        self.visible_lines = []
        for entry in self.filtered_entries:
            # Use timestamp as the unique key
            timestamp = entry.timestamp

            # Extract filebase from message JSON
            filebase = None
            json_str = None

            # Parse the JSON from the message
            try:
                # The message may contain "RE-ENCODE-TO-H265 {json...}" or similar
                # Extract just the JSON part
                msg = entry.message if entry.message else ""
                if '{' in msg:
                    json_start = msg.index('{')
                    json_str = msg[json_start:]
                    data = json.loads(json_str)
                    filebase = data.get('filebase', data.get('file', None))
            except (json.JSONDecodeError, ValueError, AttributeError, KeyError):
                pass

            # If no filebase from JSON, use the message itself (truncated)
            if not filebase:
                if entry.message:
                    filebase = entry.message[:70]
                else:
                    filebase = f"{entry.file}:{entry.line} {entry.function}()"

            # Show timestamp, level, and filebase
            timestamp_display = timestamp[:19]  # Just the date and time part (YYYY-MM-DD HH:MM:SS)
            level = entry.level if hasattr(entry, 'level') else "???"

            # Add deep match indicator if this entry matched only in JSON
            deep_indicator = " *" if timestamp in self.search_matches else ""

            line = f"{timestamp_display} [{level:>3}] {filebase}{deep_indicator}"
            win.add_body(line, attr=curses.A_BOLD, context=Context("header", timestamp=timestamp))
            self.visible_lines.append(timestamp)

            # Handle expansion
            expand_state = self.expands.get(timestamp, 0)
            if expand_state > 0 and json_str:
                # Format JSON nicely
                try:
                    data = json.loads(json_str)
                    formatted = json.dumps(data, indent=2)
                    lines = formatted.split('\n')

                    if expand_state == 1:  # Partial: first 10 and last 10
                        if len(lines) <= 20:
                            display_lines = lines
                        else:
                            display_lines = lines[:10] + ['  ...'] + lines[-10:]
                    else:  # expand_state == 2: Full
                        display_lines = lines

                    for line in display_lines:
                        win.add_body(f"  {line}", context=Context("body", timestamp=timestamp))
                        self.visible_lines.append(None)  # Placeholder for expanded lines

                except (json.JSONDecodeError, ValueError):
                    win.add_body("  (invalid JSON)")
                    self.visible_lines.append(None)

            # Empty line between entries
            win.add_body("", context=Context("DECOR"))
            self.visible_lines.append(None)

    def expand_ACTION(self):
        """'e' key - Expand/collapse current entry"""
        app = self.app
        win = app.win
        ctx = win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp

            # Cycle through expansion states: 0 (collapsed) -> 1 (partial) -> 2 (full) -> 0
            current = self.expands.get(timestamp, 0)
            next_state = (current + 1) % 3
            if next_state == 0:
                # Back to collapsed, remove from dict
                if timestamp in self.expands:
                    del self.expands[timestamp]
            else:
                self.expands[timestamp] = next_state

    def copy_log_ACTION(self):
        """'c' key - Copy entire log entry to clipboard or export to file"""
        app = self.app
        win = app.win
        ctx = win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp

            # Find the entry with this timestamp
            entry = None
            for e in self.filtered_entries:
                if e.timestamp == timestamp:
                    entry = e
                    break

            if entry:
                # Build the full log entry text
                lines = []
                lines.append(f"Timestamp: {entry.timestamp}")
                lines.append(f"Level: {entry.level if hasattr(entry, 'level') else 'N/A'}")
                lines.append(f"File: {entry.file}:{entry.line}")
                lines.append(f"Function: {entry.function}()")
                lines.append("")

                # Add the full message (including JSON if present)
                if entry.message:
                    lines.append("Message:")
                    lines.append(entry.message)

                    # Try to pretty-print JSON if it exists
                    try:
                        msg = entry.message
                        if '{' in msg:
                            json_start = msg.index('{')
                            json_str = msg[json_start:]
                            data = json.loads(json_str)
                            lines.append("")
                            lines.append("JSON (formatted):")
                            lines.append(json.dumps(data, indent=2))
                    except (json.JSONDecodeError, ValueError, AttributeError):
                        pass

                text = '\n'.join(lines)

                # Try clipboard first using SystemDiscovery
                sys_disc = get_system_discovery()
                success, error = sys_disc.copy_to_clipboard(text)

                if success:
                    win.alert(message='Log entry copied to clipboard')
                else:
                    # Clipboard failed - fall back to file export
                    try:
                        export_path = '/tmp/rmbloat-export.txt'
                        with open(export_path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        win.alert(message=f'Exported to {export_path}')
                    except Exception as exc:
                        win.alert(message=f'Export failed: {exc}')

    def hist_search_ACTION(self):
        """ Handle '/' key - start incremental filter search """
        self.search_bar.start("")
        self.app.win.passthrough_mode = True


def main(args=None):
    """
    Convert video files to desired form
    """
    try:
        cfg = IniManager(app_name='rmbloat',
                               allowed_codecs='x265',
                               bloat_thresh=1600,
                               files=[],  # Default video collection paths
                               full_speed=False,
                               keep_backup=False,
                               merge_subtitles=False,
                               min_shrink_pct=10,
                               prefer_strategy='auto',
                               quality=28,
                               thread_cnt=4,
                        )
        vals = cfg.vals
        parser = argparse.ArgumentParser(
            description="CLI/curses bulk Video converter for media servers")
        # config options
        parser.add_argument('-a', '--allowed-codecs',
                    default=vals.allowed_codecs,
                    choices=('x26*', 'x265', 'all'),
                    help=f'allowed codecs [dflt={vals.allowed_codecs}]')
        parser.add_argument('-b', '--bloat-thresh',
                    default=vals.bloat_thresh, type=int,
                    help='bloat threshold to convert'
                        + f' [dflt={vals.bloat_thresh},min=500]')
        parser.add_argument('-F', '--full-speed',
                    action='store_false' if vals.full_speed else 'store_true',
                    help='if true, do NOT set nice -n19 and ionice -c3'
                        + f' [dflt={vals.full_speed}]')
        parser.add_argument('-B', '--keep-backup',
                    action='store_false' if vals.keep_backup else 'store_true',
                    help='if true, rename to ORIG.{videofile} rather than recycle'
                         + f' [dflt={vals.keep_backup}]')
        parser.add_argument('-M', '--merge-subtitles',
                    action='store_false' if vals.merge_subtitles else 'store_true',
                    help='Merge external .en.srt subtitle files into output'
                    + f' [dflt={vals.merge_subtitles}]')
        parser.add_argument('-m', '--min-shrink-pct',
                    default=vals.min_shrink_pct, type=int,
                    help='minimum conversion reduction percent for replacement'
                    + f' [dflt={vals.min_shrink_pct}]')
        parser.add_argument('-p', '--prefer-strategy',
                    choices=FfmpegChooser.STRATEGIES,
                    default=vals.prefer_strategy,
                    help='FFmpeg strategy preference'
                        + f' [dflt={vals.prefer_strategy}]')
        parser.add_argument('-q', '--quality',
                    default=vals.quality, type=int,
                    help=f'output quality (CRF) [dflt={vals.quality}]')
        parser.add_argument('-t', '--thread-cnt',
                    default=vals.thread_cnt, type=int,
                    help='thread count for ffmpeg conversions'
                        + f' [dflt={vals.thread_cnt}]')

        # run-time options
        parser.add_argument('-S', '--save-defaults', action='store_true',
                    help='save the -B/-b/-p/-q/-a/-F/-m/-M options and file paths as defaults')
        parser.add_argument('--auto-hr', type=float, default=None,
                    help='Auto mode: run unattended for specified hours, '
                         'auto-select [X] files and auto-start conversions')
        parser.add_argument('-n', '--dry-run', action='store_true',
                    help='Perform a trial run with no changes made.')
        parser.add_argument('-s', '--sample', action='store_true',
                    help='produce 30s samples called SAMPLE.{input-file}')
        parser.add_argument('-L', '--logs', action='store_true',
                    help='view the logs')
        parser.add_argument('-T', '--chooser-tests', action='store_true',
                    help='run tests on ffmpeg choices w 30s cvt of 1st given video')

        # Build help message for files argument showing defaults if set
        files_help = 'Video files and recursively scanned folders w Video files'
        if vals.files:
            files_help += f' [dflt: {", ".join(vals.files)}]'
        parser.add_argument('files', nargs='*', help=files_help)
        opts = parser.parse_args(args)
            # Fake as option ... if this needs tuning (which I doubt)
            # then make it an actual option.  It is the max time allowed
            # between progress updates when converting a video
        opts.progress_secs_max = 30

        # Use default files if none provided on command line
        # (but not for --chooser-tests, where no files means detection-only mode)
        if not opts.files and vals.files and not opts.chooser_tests:
            opts.files = vals.files
            print('Using default video collection paths from config:')
            for path in opts.files:
                print(f'  {path}')

        if opts.save_defaults:
            print('Setting new defaults:')
            for key in vars(vals):
                new_value = getattr(opts, key)
                # Special handling for files: sanitize paths
                if key == 'files':
                    new_value = sanitize_file_paths(new_value)
                    print(f'- {key} (sanitized):')
                    for path in new_value:
                        print(f'    {path}')
                else:
                    print(f'- {key} {new_value}')
                setattr(vals, key, new_value)
            cfg.write()
            sys.exit(0)

        if opts.logs:
            files = lg.log_paths
            cmd = ['less', '+F', files[0]]
            if os.path.isfile(files[1]):
                cmd.append(files[1])
            try:
                program = cmd[0]
                # This call replaces the current Python process
                os.execvp(program, cmd)
            except FileNotFoundError:
                print(f"Error: Executable '{program}' not found.", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                # Catch any other execution errors
                print(f"An error occurred during exec: {e}", file=sys.stderr)
                sys.exit(1)
        if opts.chooser_tests:
            chooser = FfmpegChooser(force_pull=True)

            video_file = None
            if opts.files:
                # Get first video file from arguments
                paths, _ = Converter.get_candidate_video_files(opts.files)
                if paths:
                    video_file = paths[0]
                    print(f"\nTesting with video: {video_file}")
                else:
                    print("\nWarning: No valid video files found, running basic tests only")

            # Run tests (real-world if video_file provided, basic otherwise)
            exit_code = chooser.run_tests(
                video_file=video_file,
                duration=30,
                show_test_encode=bool(video_file is None)  # Show example commands if no video
            )
            sys.exit(exit_code)

        if opts.sample:
            opts.dry_run = False # cannot have both
        opts.bloat_thresh = max(500, opts.bloat_thresh)

        Converter(opts, os.path.dirname(cfg.config_file_path)).main_loop()
    except Exception as exc:
        # Note: We no longer call Window.exit_handler(), as ConsoleWindow handles it
        # and there is no guarantee the Window class was ever initialized.
        if Converter.singleton and Converter.singleton.win:
            Converter.singleton.win.stop_curses()

        print("exception:", str(exc))
        print(traceback.format_exc())


if __name__ == '__main__':
    # When the script is run directly, call main
    # Pass sys.argv[1:] to main, but it's cleaner to let argparse
    # handle reading from sys.argv directly, as done above.
    main()
