#!/usr/bin/env python3
""" rmbloat main """
# pylint: disable=too-many-statements
# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return,too-many-branches
# pylint: disable=too-many-return-statements,too-many-instance-attributes
# pylint: disable=consider-using-with,line-too-long,too-many-lines
# pylint: disable=too-many-nested-blocks,try-except-raise,line-too-long
# pylint: disable=too-many-public-methods,invalid-name,multiple-statements


import os
import sys
import base64
import argparse
import traceback
import re
import atexit
import signal
import shutil
import time
import json
import curses
import textwrap
from dataclasses import asdict
from types import SimpleNamespace
from console_window import (ConsoleWindow, OptionSpinner, Screen, ScreenStack,
                            ConsoleWindowOpts, IncrementalSearchBar, Context,
                            Theme, appearance)
from console_window import cli as display_cli
from .ProbeCache import ProbeCache
from .VideoParser import Mangler
from .IniManager import IniManager
from .StructuredLogger import StructuredLogger
from .CpuStatus import CpuStatus
from .FfmpegChooser import FfmpegChooser
from .Models import PathProbePair, Vid
from . import FileOps
from . import ConvertUtils
from . import Sandbox
from . import StatusLine
from .JobHandler import JobHandler
from .PersistentState import PersistentState

lg = StructuredLogger('rmbloat')

# Screen constants
SELECT_ST = 0
CONVERT_ST = 1
HISTORY_ST = 2
HELP_ST = 3
THEME_ST = 4
SCREEN_NAMES = ('SELECT', 'CONVERT', 'HISTORY', 'HELP', 'THEME')

# The theme picker offers the library palettes plus rmbloat's own "mono"
# pseudo-theme.  ``mono`` is not a palette: it turns semantic color off for the
# process (exactly like ``--no-color``) and leaves the installed palette alone.
# Keeping it last makes ``t`` step from the final palette into mono and wrap
# back to the first palette.
MONO_THEME = 'mono'
THEME_CHOICES = (*Theme.list_all(), MONO_THEME)




def settle_appearance(cli_theme=None, no_color=False, saved_theme='',
                      saved_colors=True):
    """Turn the color switches and the remembered choice into process state.

    ``resolve_appearance()`` returns the two halves of the decision -- which
    palette and whether to use it -- from ``--no-color``, ``--theme``, the
    ``NO_COLOR`` convention and the remembered choice.  This helper applies
    that decision to the process-wide switch and records the palette the first
    window should install.  Returns the ``(theme, colors)`` it applied.
    """
    theme, colors = appearance.resolve_appearance(
        cli_theme=cli_theme,
        no_color=no_color,
        saved_theme=saved_theme,
        saved_colors=saved_colors,
    )
    appearance.set_colors_enabled(colors)
    if theme:
        Theme.set_active(theme)
    return theme, colors

# File operation functions moved to FileOps.py
sanitize_file_paths = FileOps.sanitize_file_paths

def store_cache_on_exit():
    """ TBD """
    if Converter.singleton:
        if Converter.singleton.win:
            Converter.singleton.win.stop_curses()
        if Converter.singleton.probe_cache:
            Converter.singleton.probe_cache.store()


def install_exit_handlers(cleanup):
    """Run *cleanup* on normal exit and on service-stop signals.

    ``atexit`` covers normal termination, ``sys.exit()`` and an uncaught
    ``KeyboardInterrupt``; SIGTERM/SIGHUP cover systemd, cron, and ``kill``.
    The cleanup callable must be idempotent because both paths can run.
    """
    atexit.register(cleanup)

    def _handle_termination(signum, _frame):
        cleanup()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, _handle_termination)
        except (OSError, ValueError):
            # Not the main thread, or the platform lacks the signal: the
            # atexit registration still handles normal exits.
            pass

# Data models moved to Models.py
# TranscodeThread class moved to TranscodeThread.py
# Job class moved to Models.py

class Converter:
    """ TBD """
    # --- Conversion Criteria Constants (Customize these) ---
    TARGET_CODECS = ['h265', 'hevc']
    MAX_BITRATE_KBPS = 2100 # about 15MB/min (or 600MB for 40m)
    # Cap ffmpeg output serialized into the job log, independent of the
    # TranscodeThread live buffer.
    MAX_LOGGED_RUN_LINES = 200

    # Constants moved to ConvertUtils.py
    VIDEO_EXTENSIONS = ConvertUtils.VIDEO_EXTENSIONS
    SKIP_PREFIXES = ConvertUtils.SKIP_PREFIXES
    singleton = None

    def __init__(self, opts, cache_dir=None, ini=None):
        assert Converter.singleton is None
        Converter.singleton = self
        self.win = None
        self.ini = ini # IniManager
        self.redraw_mono = time.monotonic()
        self.opts = opts
        self.spinner = None # spinner object
        self.spins = None # spinner values
        self.search_re = None # the "accepted" search
        self.search_re_valid = None # last valid regex pattern
        self.vids = []
        self.todo_vids = []
        self.visible_vids = []
        self.ff_pre_i_opts = []
        self.ff_post_i_opts = []
        self.ff_thread_opts = []
        self.job = None
        self.prev_time_encoded_secs = -1
        # Be quiet if user has already selected a specific strategy
        quiet_chooser = bool(opts.prefer_strategy != 'auto')
        self.chooser = FfmpegChooser(
            force_pull=False,
            prefer_strategy=opts.prefer_strategy,
            quiet=quiet_chooser,
            sandbox=getattr(opts, 'sandbox', 'auto'))
        self.probe_cache = ProbeCache(cache_dir_name=cache_dir, chooser=self.chooser)
        self.probe_cache.load()
        self.probe_cache.store()
        self.start_job_mono = 0
        self.cpu = CpuStatus()
        # self.cgroup_prefix = set_cgroup_cpu_limit(opts.thread_cnt*100)
        atexit.register(store_cache_on_exit)
        # Abort an in-flight transcode and remove TEMP.*/SAMPLE.* files on
        # normal exit and on SIGTERM/SIGHUP.  The transcode thread is a daemon
        # so it cannot perform this cleanup by itself during interpreter
        # shutdown.
        install_exit_handlers(self.emergency_cleanup)

        # Auto mode tracking (separate from JobHandler for overall session tracking)
        self.auto_mode_enabled = bool(opts.auto_hr is not None)
        self.auto_mode_hrs_limit = opts.auto_hr if self.auto_mode_enabled else None
        # Fallback clock anchor for the --auto-hr deadline; the JobHandler's own
        # auto_mode_start_time wins once one exists (see _auto_mode_elapsed_hrs)
        self.auto_mode_start_mono = time.monotonic() if self.auto_mode_enabled else None

        # Job handler (created when entering convert screen, destroyed when leaving)
        self.job_handler = None

        # Session flag: allow re-encoding of already re-encoded files (DUN status)
        self.allow_reencode_dun = False

        # Build options suffix for display
        self.options_suffix = self.build_options_suffix()
        self.screens = None
        self.stack = None
        self.need_freeze_draw = False
        self.persistent_state = PersistentState()
        # The preference to write back.  It stays on the remembered palette
        # when a per-run switch (--no-color / --theme) changes only the display,
        # and follows the picker when the user chooses a palette or mono.
        self.remembered_theme = 'default'

    def build_options_suffix(self):
        """Build the options suffix string for display."""
        parts = []
        parts.append(f'Q={self.opts.quality}')
        parts.append(f'Shr>={self.opts.min_shrink_pct}')
        if self.opts.sample:
            parts.append('SAMPLE')
        if self.opts.keep_backup:
            parts.append('KeepB')
        if self.opts.merge_subtitles:
            parts.append('MrgSrt')
        if self.auto_mode_enabled:
            parts.append(f'Auto={self.opts.auto_hr}hr')
        return ' -- ' + ' '.join(parts)

    def apply_theme_choice(self, choice):
        """Make a theme-picker choice live.

        ``mono`` is a color mode, not a palette: it flips the process-wide
        switch and leaves the installed theme where it was (the same way
        ``--no-color`` keeps ``--theme``).  Choosing a real palette is a request
        for color, so it lifts the switch again and installs the palette.  This
        is the one place the choice becomes window state, whichever screen the
        key was pressed on.
        """
        self.opts.theme = choice
        self.remembered_theme = choice
        if choice == MONO_THEME:
            appearance.set_colors_enabled(False)
            return
        appearance.set_colors_enabled(True)
        if self.win:
            self.win.set_theme(choice)

    def emergency_cleanup(self):
        """Abort an in-flight conversion and remove its temp file.

        Registered with ``atexit`` and the SIGTERM/SIGHUP handlers.  The
        handler owns the active job and its temp path, so delegate to it when
        present; ``self.job`` is aborted as a fallback when the handler was
        torn down first.  Safe to call more than once.
        """
        handler = getattr(self, 'job_handler', None)
        if handler is not None:
            handler.emergency_cleanup()

        job = getattr(self, 'job', None)
        if job is not None:
            try:
                job.abort()
            except Exception as exc:  # noqa: BLE001 - cleanup must never raise
                print(f"Warning: could not abort active job: {exc}")
            self.job = None

    @staticmethod
    def _ops_note(vid, limit=96):
        """Return a compact TUI note for the latest notable Vid operation.

        ``Vid.ops`` records file-swap and cleanup actions. Routine entries
        (rename/link/trash) are ignored, but a swap failure or trash warning
        must be visible in the UI instead of only in the serialized job log.
        """
        prefixes = ('ERROR', 'CRITICAL', 'WARNING', 'REJECTED')
        for op in reversed(getattr(vid, 'ops', ()) or ()):
            text = str(op).strip()
            if text.startswith(prefixes):
                if limit and len(text) > limit:
                    return text[:max(0, limit - 3)] + '...'
                return text
        return ''

    def make_lines(self, doit_skips=None):
        """Generate display lines and stats for video list"""
        lines, self.visible_vids, short_list = [], [], []
        jobcnt, co_wid = 0, len('CODEC')
        stats = SimpleNamespace(total=0, picked=0, done=0, progress_idx=0,
                                gb=0, delta_gb=0)
        is_convert = bool(hasattr(self, 'stack') and self.stack.curr.num == CONVERT_ST)

        # Determine which list to use based on screen
        vid_list = self.todo_vids if is_convert else self.vids

        # Get current filter setting
        filter_mode = self.spins.filter if hasattr(self.spins, 'filter') else 'all'

        for vid in vid_list:
            if doit_skips and vid.doit in doit_skips:
                continue

            # Apply status filter (always show IP - in progress)
            if vid.doit != 'IP ' and filter_mode:
                doit = vid.doit if is_convert else vid.doit_auto
                is_err = bool(doit in ('ERR',) or doit.startswith('Er'))
                # Successful conversions are stored as ' OK' (leading space)
                # and retries as 'OK2'/'OK3', so compare after stripping.
                is_dun = bool(doit == 'DUN' or doit.strip().startswith('OK'))
                is_unchk = vid.doit_auto == '[ ]'

                if filter_mode == '-DUN' and is_dun:
                    continue
                if filter_mode == '-DUN-ERR' and (is_dun or is_err):
                    continue
                if filter_mode == '-DUN-ERR-UnChk' and (is_dun or is_err or is_unchk):
                    continue
                if filter_mode == '+ERR' and not is_err:
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
            note = self._ops_note(vid)
            if note:
                line += f'  [!] {note}'
            if self.search_re:
                pattern = self.search_re
                if self.spins.mangle:
                    pattern = Mangler.mangle(pattern)
                try:
                    match = re.search(pattern, line, re.IGNORECASE)
                    self.search_re_valid = pattern  # remember last valid pattern
                except re.error:
                    # invalid regex, fall back to last valid pattern
                    if self.search_re_valid:
                        match = re.search(self.search_re_valid, line, re.IGNORECASE)
                    else:
                        match = True  # no valid pattern yet, show all
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

    @staticmethod
    def _tail_for_log(texts, limit):
        """Return at most *limit* trailing lines, with an omission marker."""
        texts = list(texts or [])
        if len(texts) <= limit:
            return texts
        omitted = len(texts) - limit
        return [f"... {omitted} earlier line(s) omitted ..."] + texts[-limit:]

    def _log_job_final(self, job, status_label):
        """Standardized logging for finished jobs"""
        clean_label = status_label.strip() # Remove the leading space from " OK"
        dumped = asdict(job.vid)
        runs = dumped.get('runs') or []

        # 'OPT' means the transcode succeeded but was rejected because it did
        # not shrink enough. It is not an error, so log it with the OK stream.
        is_ok = clean_label.startswith('OK') or clean_label == 'OPT'

        if is_ok:
            # Clean up logs for successful runs, including each run's ffmpeg
            # output (asdict embedded runs[*].texts even though vid.texts was
            # cleared).
            dumped['texts'] = []
            for run in runs:
                run['texts'] = []
        else:
            # Failed jobs keep their output for diagnosis, but bounded.
            limit = self.MAX_LOGGED_RUN_LINES
            dumped['texts'] = self._tail_for_log(dumped.get('texts'), limit)
            for run in runs:
                run['texts'] = self._tail_for_log(run.get('texts'), limit)

        title = 'RE-ENCODE-TO-H265'
        if clean_label == 'OK2': title += '-RETRY'
        elif clean_label == 'OK3': title += '-SOFTWARE'
        elif clean_label == 'OPT': title += '-REJECTED'

        # Use the stripped label for the logic check
        lg.put('OK' if is_ok else 'ERR',
               f"{title} ", json.dumps(dumped, indent=4))

    def _auto_mode_elapsed_hrs(self):
        """Hours elapsed since auto mode began converting."""
        start = None
        if self.job_handler is not None:
            start = self.job_handler.auto_mode_start_time
        if start is None:
            start = self.auto_mode_start_mono
        if start is None:
            return 0.0
        return (time.monotonic() - start) / 3600

    def _auto_mode_time_exceeded(self):
        """True once the --auto-hr time budget has been consumed."""
        if not self.auto_mode_enabled or self.auto_mode_hrs_limit is None:
            return False
        return self._auto_mode_elapsed_hrs() >= self.auto_mode_hrs_limit

    def _auto_mode_failure_limit_reached(self):
        """True when the last 10 conversions all failed."""
        return bool(self.job_handler
                    and self.job_handler.consecutive_failures >= 10)

    def _exit_auto_mode(self, reason):
        """Stop a --auto-hr run cleanly: log the reason, report vitals, exit."""
        lg.info(f"Auto mode stopping: {reason}")
        _, stats, _ = self.make_lines()
        self.print_auto_mode_vitals(stats, reason=reason)

    def _manage_queue_and_auto_mode(self):
        """Handles starting the next [X] and checking auto-mode exit"""
        while True:
            # 1. Find the next candidate
            next_vid = next((v for v in self.visible_vids if v and v.doit == '[X]'), None)

            if next_vid:
                if not os.path.isfile(next_vid.filepath):
                    self.vids = [v for v in self.vids if v != next_vid]
                    self.visible_vids = [v for v in self.visible_vids if v != next_vid]
                    continue

                # Auto mode: never start new work once the time budget is spent
                # or the failure ladder tripped.  A job already running is
                # allowed to finish; this check runs again before the next one.
                if self.auto_mode_enabled:
                    if self._auto_mode_time_exceeded():
                        self._exit_auto_mode(
                            f'time limit of {self.auto_mode_hrs_limit} hours reached')
                    if self._auto_mode_failure_limit_reached():
                        self._exit_auto_mode('10 consecutive conversion failures')

                # Start the engine
                self.job = self.job_handler.start_transcode_job(next_vid)
                next_vid.doit = "IP "
                return

            # 2. Queue is Empty - Handle Auto Mode Exit
            # An unattended run has nothing left to do, so terminate instead of
            # idling on the Convert screen until the process is killed.
            if self.auto_mode_enabled:
                self._exit_auto_mode('conversion queue drained')

            # Interactive mode: We only pop the screen if we are currently
            # LOOKING at the Convert screen.  If we are on History/Help, we just
            # stop the engine but stay on the screen.
            self.vids.sort(key=lambda v: (v.all_ok, v.bloat), reverse=True)

            if self.stack.curr.num == CONVERT_ST:
                self.job_handler = None
                self.stack.pop() # Go back to Select screen

            return

    def _conversion_active(self):
        """True while the Convert screen is current or beneath an overlay.

        The Convert screen stays on the back stack when History/Help/Theme is
        pushed on top, so the whole stack is inspected. Any other base screen
        (notably Select) means the conversion engine must stay idle.
        """
        if not self.stack:
            return False
        curr = getattr(self.stack, 'curr', None)
        if curr is not None and curr.num == CONVERT_ST:
            return True
        frames = getattr(self.stack, 'stack', ())
        return any(getattr(frame, 'num', None) == CONVERT_ST for frame in frames)

    def advance_jobs(self):
        """Process ongoing conversion jobs (Threaded Version)"""
        # Safety: If we don't have a handler, we can't do work
        if not self.job_handler:
            return

        # A. Handle Active Job
        # if self.job and self.stack.curr.num in (CONVERT_ST, HELP_ST):
        if self.job: # and self.stack.curr.num in (CONVERT_ST, HELP_ST):
            # The handler now manages the 'is it an int? is it a retry?' logic
            next_job, report, is_done = self.job_handler.check_job_status(self.job)

            if not is_done:
#               # 1. Update UI progress (report is the status_string from the thread)
#               if isinstance(report, str):
#                   self.job.progress = f"{report}{self.job.vid.descr_str()}"

                # 2. If the handler triggered a retry, it returned a NEW job object
                if next_job is not None and next_job != self.job:
                    self.job = next_job
                    self.job.vid.doit = "IP "
            else:
                # 3. Job is finally finished (Final Success or Final Error)
                # 'report' here is the final status code string (e.g., ' OK', 'ERR', 'OK2')
                vid = self.job.vid

                # Final Reap: Clean up files, analyze logs, apply probes
                # success is calculated inside finish_transcode_job now
                probe = self.job_handler.finish_transcode_job(self.job)
                if probe:
                    self.job.vid.probe1 = self.apply_probe(self.job.vid, probe)

                # finish_transcode_job() can downgrade an FFmpeg success to an
                # error (e.g. the original could not be replaced safely) or
                # record a rejection ('OPT' = already optimally sized). Keep
                # both terminal states instead of reporting the prior ' OK'.
                if vid.doit in ('ERR', 'OPT'):
                    report = vid.doit
                vid.doit = vid.doit_auto = report

                # Final Logging (moved from old_advance_jobs)
                self._log_job_final(self.job, report)

                # Clear active job so the queue manager can pick the next one
                self.job = None

        # B. Start New Jobs / Handle Queue
        # Only run the engine while the Convert screen is actually active
        # (current, or under a History/Help/Theme overlay). Requiring the
        # Convert screen specifically - rather than merely "not Select" -
        # stops a stale job_handler from resuming queued conversions after the
        # user has quit back to Select and then opened another screen.
        if not self.job and self._conversion_active():
            self._manage_queue_and_auto_mode()


    def apply_probe(self, vid, probe):
        """ TBD """
        # shorthand
        vid.width = probe.width
        vid.height = probe.height
        vid.codec = probe.codec
        vid.bloat = probe.bloat
        vid.duration = probe.duration
        vid.gb = probe.gb

        vid.codec_ok = JobHandler.is_allowed_codec(self.opts, probe)

        vid.res_ok = bool(vid.height is not None and vid.height <= self.opts.max_height)
        vid.bloat_ok = bool(vid.bloat < self.opts.bloat_thresh)
        vid.all_ok = bool(vid.res_ok and vid.bloat_ok and vid.codec_ok)

        # vid.summary = (f'  {vid.width}x{vid.height}' +
        #               f' {vid.codec} {vid.bloat}b {vid.gb}G')
        return probe

    def append_vid(self, ppp):
        """
        Checks if a video file already meets the updated conversion criteria:
        1. Resolution height is at or below max_height (configurable via --max-height).
        2. Video codec is TARGET_CODECS (e.g., 'h264').
        3. Video "bloat" is below bloat_thresh.

        Args:
            filepath (str): The path to the video file.

        Returns:
            bool: True if the file meets all criteria, False otherwise.
        """

        vid = Vid()
        vid.start_new_run()
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
        return FileOps.bulk_rename(old_file_name, new_file_name, trashes)

    def process_one_ppp(self, ppp):
        """ Handle just one """
        input_file = ppp.video_file
        if ppp.probe is None:
            return  # Cannot evaluate a file that failed to probe
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

        # The directory walk below visits every entry under the given paths.  On
        # a media share with tens of thousands of files that pause can last many
        # seconds, so the scan owns a single status line: it opens with the
        # announcement and is replaced by the final count and duration.  Keeping
        # it to one line means the probe status that follows starts on a fresh
        # row and neither phase can overwrite or hide the other.
        scan_targets = [p for p in (getattr(self.opts, 'files', None) or [])
                        if p != '-']
        if scan_targets:
            label = (scan_targets[0] if len(scan_targets) == 1
                     else f"{len(scan_targets)} paths")
            # Keep the notice after the (stdout) detection summary even when
            # stdout is redirected to a file or pipe.
            sys.stdout.flush()
            StatusLine.begin(f"Scanning {label} for video files"
                             " (large collections can take a while)...")

        # Get candidate video file paths
        scan_start = time.monotonic()
        paths_to_probe, read_pipe = self.get_candidate_video_files(self.opts.files)
        if scan_targets:
            found = len(paths_to_probe)
            StatusLine.end(
                f"Found {found} candidate video{'s' if found != 1 else ''}"
                f" in {time.monotonic() - scan_start:.1f}s")

        # --- Restore TTY Input if needed ---
        if read_pipe:
            try:
                # 2a. Close the current stdin (the pipe). This frees fd 0.
                sys.stdin.close()
                # 2b. Open the TTY device (the actual keyboard/terminal)
                # os.O_RDONLY is read-only access.
                tty_fd = os.open('/dev/tty', os.O_RDONLY)
                # 2c. Replace file descriptor 0 (stdin) with the TTY descriptor.
                # os.open() normally reuses the just-freed fd 0, in which case
                # no dup2 is needed (and closing it below would close stdin).
                if tty_fd != 0:
                    os.dup2(tty_fd, 0)
                # 2d. Re-create the sys.stdin file object for Python's I/O.
                # os.fdopen(0, 'r') creates a new Python file object from FD 0.
                sys.stdin = os.fdopen(0, 'r')
                # 2e. Close the original descriptor only when it is a distinct
                # fd. Closing it when it *is* fd 0 would leave sys.stdin backed
                # by a closed descriptor, breaking the interactive session.
                if tty_fd != 0:
                    os.close(tty_fd)

            except OSError as e:
                # This handles cases where /dev/tty is not available (e.g., some non-interactive environments)
                sys.stderr.write(f"Error reopening TTY: {e}. Cannot enter interactive mode.\n")
                sys.exit(1)

        # 5. Final probing.  ProbeCache owns the single status line for this
        # phase, so the scan above must already have terminated its own.
        results = self.probe_cache.batch_get_or_probe(paths_to_probe)
        for file_path, probe in results.items():
            # batch_get_or_probe() omits files it could not probe (missing,
            # audio-only, ...); never build a PathProbePair with a None probe.
            if probe is None:
                continue
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
        if base.endswith(('.recode.mkv', 'recode.sb.mkv')):
            return 'DUN'

        # Files marked as OK from previous successful conversion or skip
        # OK2/OK3 mean succeeded after retries - still done
        if vid.doit in ('OK', 'OK2', 'OK3', ' OK', '---'):
            return vid.doit if vid.doit != '---' else 'OK'

        # Note: SAMPLE. and TEST. files are now excluded at is_valid_video_file() level

        return None

    def print_auto_mode_vitals(self, stats, reason=None):
        """Print vitals report and exit for auto mode."""
        handler = self.job_handler
        runtime_hrs = self._auto_mode_elapsed_hrs()
        ok_count = handler.ok_count if handler else 0
        error_count = handler.error_count if handler else 0
        consecutive = handler.consecutive_failures if handler else 0

        # Calculate space bloated from remaining TODO items
        space_bloated_gb = 0.0
        for vid in self.vids:
            if vid.doit == '[X]' and vid.probe0:
                space_bloated_gb += vid.probe0.gb

        # Format report
        report = "\n" + "=" * 70 + "\n"
        report += "AUTO MODE VITALS REPORT\n"
        report += "=" * 70 + "\n"
        if reason:
            report += f"Stop reason:          {reason}\n"
        report += f"Runtime:              {runtime_hrs:.2f} hours\n"
        report += f"OK conversions:       {ok_count}\n"
        report += f"Error conversions:    {error_count}"

        if consecutive >= 10:
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
        exit_code = 1 if consecutive >= 10 else 0
        sys.exit(exit_code)

    def do_window_mode(self):
        """Main UI loop using Screen-based architecture"""
        # Capture the explicit switches before the remembered choice is
        # restored: resolve_appearance() gives --no-color and --theme
        # precedence over the saved theme.
        cli_theme = getattr(self.opts, 'theme', None)
        cli_no_color = bool(getattr(self.opts, 'no_color', False))
        self.persistent_state.restore_updated_opts(self.opts)
        saved_theme = getattr(self.opts, 'theme', None) or 'default'
        saved_mono = saved_theme == MONO_THEME
        self.remembered_theme = (MONO_THEME if saved_mono
                                 else Theme.resolve(saved_theme))
        theme, colors = settle_appearance(
            cli_theme=cli_theme,
            no_color=cli_no_color,
            saved_theme='' if saved_mono else saved_theme,
            saved_colors=not saved_mono,
        )
        # The picker names the choice in effect, so color off means mono even
        # when a palette stays installed underneath (the SampleApp convention).
        self.opts.theme = (MONO_THEME if not colors
                           else (theme or Theme.resolve(
                               None if saved_mono else saved_theme)))

        # Create ConsoleWindow
        win_opts = ConsoleWindowOpts(
            head_line=True,
            head_rows=4,
            # console-window 2.2.2 treats body_rows as an advisory hint only:
            # the body grows on demand up to max_rows (32767 by default) and
            # the curses pads stay viewport-sized.  A small hint is enough for
            # collections with several thousand videos.
            body_rows=128,
            min_cols_rows=(60,10),
            ctrl_c_terminates=False,
            theme=(theme or None),
        )
        self.win = win = ConsoleWindow(win_opts)
        # Initialize screens
        self.screens = {
            SELECT_ST: SelectScreen(self),
            CONVERT_ST: ConvertScreen(self),
            HELP_ST: HelpScreen(self),
            HISTORY_ST: HistoryScreen(self),
            THEME_ST: ThemeScreen(self),
        }
        self.stack = ScreenStack(self.win, None, SCREEN_NAMES, self.screens)
        # Setup OptionSpinner
        self.spinner = spin = OptionSpinner(stack=self.stack)
        spin.default_obj = self.opts
        spin.add_key('screen_escape', 'ESC - return to previous screen',
                     keys=27, genre="action")
        spin.add_key('quit', 'q - quit converting OR exit app', genre='action',
                     keys={ord('q'), 0x3})
        spin.add_key('help_mode', '? - Help Screen', genre="action")
        spin.add_key('history', 'h - History Screen', genre='action',
                     scope=[SELECT_ST, CONVERT_ST])
        spin.add_key('theme_screen', 't - Theme Picker', genre='action',
                     scope=[SELECT_ST, CONVERT_ST])

        # On the picker (and HELP) the same key cycles every choice, ending at
        # mono.  A cycle option keeps the spinner's help list truthful.
        spin.add_key('theme', 't - next theme', vals=list(THEME_CHOICES),
                     scope=[THEME_ST, HELP_ST])

        spin.add_key('expand', 'e - expand/collapse log entry', genre='action',
                     scope=HISTORY_ST)
        spin.add_key('copy_log', 'c - copy log entry to clipboard', genre='action',
                     scope=HISTORY_ST)

        spin.add_key('reset_all', 'r - reset all to "[ ]"', genre='action')
        spin.add_key('init_all', 'i - set all automatic state', genre='action')
        spin.add_key('toggle', 'SP - toggle current line state', genre='action',
                     keys={ord(' '), })
        spin.add_key('skip', 's - skip reencoding --> "---"', genre='action')
        spin.add_key('go', 'g - go (start conversions)', genre='action')

        spin.add_key('directory', 'd - show directory', vals=[False, True])
        spin.add_key('filter', 'f - filter status',
                     vals=['', '-DUN', '-DUN-ERR', '-DUN-ERR-UnChk', '+ERR'],
                     scope=[SELECT_ST, CONVERT_ST])
        spin.add_key('search', '/ - incremental search', genre="action",
                      scope=SELECT_ST)
        spin.add_key('hist_search', '/ - search string', genre="action",
                      scope=HISTORY_ST)
        spin.add_key('hist_time_format', 'a - time format',
                     vals=['ago+time', 'ago', 'time'], scope=HISTORY_ST)

        spin.add_key('mangle', 'm - mangle titles', vals=[False, True])
        spin.add_key('fancy_hdr', '_ - header style',
                     vals=['Underline', 'Reverse', 'Off'])
        spin.add_key('freeze', 'p - pause/release screen', vals=[False, True])


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
            if not spins.freeze or self.need_freeze_draw:
                current_screen = self.screens[self.stack.curr.num]
                current_screen.draw_screen()
                redraw = bool(time.monotonic() - self.redraw_mono >= 60) or force_redraw
                self.redraw_mono = time.monotonic() if redraw else self.redraw_mono
                win.render(redraw=redraw)
                force_redraw = False  # Reset after use
                self.need_freeze_draw = False

            # Get user input with screen-specific timeout
            current_screen = self.screens[self.stack.curr.num]
            timeout = current_screen.prompt_timeout
            key = win.prompt(seconds=timeout)

            # Handle IncrementalSearchBar if active on history or select screen
            if key and self.stack.curr.num in (HISTORY_ST, SELECT_ST):
                screen = self.stack.get_curr_obj()
                if screen and screen.search_bar.is_active:
                    if screen.search_bar.handle_key(key):
                        force_redraw = True  # Force full redraw on next iteration
                        key = None

            was_freeze = self.spins.freeze
            previous_theme = self.spins.theme
            # Process spinner keys
            if key in spin.keys:
                spin.do_key(key, win)
            # A theme cycle commits by assigning the option, so the appearance
            # is applied from the committed value here (the seam the library's
            # SampleApp uses for both the picker and HELP).
            if self.spins.theme != previous_theme:
                self.apply_theme_choice(self.spins.theme)

            if self.spins.freeze and not was_freeze:
                self.need_freeze_draw = True

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
                else:
                    # Nothing to convert: an unattended run must exit, not idle
                    self._exit_auto_mode('no videos selected for conversion')

            # Process jobs
            self.advance_jobs()
            # Persist only the remembered choice, not a per-run override such
            # as --no-color (which displays mono but must not become the saved
            # preference).  The picker updates it through apply_theme_choice().
            self.persistent_state.set('theme', self.remembered_theme)
            self.persistent_state.sync()

            # Clear screen for next render
            if not spins.freeze or self.need_freeze_draw:
                win.clear()

    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        ppps = self.create_video_file_list()
        self.probe_cache.store()
        # Defensive: a None probe must never reach the sort key.
        ppps.sort(key=lambda ppp: ppp.probe.bloat if ppp.probe else 0, reverse=True)

        if not ppps:
            if self.opts.files:
                print("No probeable video files found in the given paths.",
                      file=sys.stderr)
            print("Usage: rmbloat {options} {video_file}...", file=sys.stderr)
            sys.exit(1)

        # Every path is absolute from here on: the scan resolves candidates and
        # the parser, swap and rename helpers all work on absolute paths.  The
        # loop therefore does not change the process working directory, which
        # also removes the chance of failing on a directory that disappeared
        # between the scan and this point.
        for ppp in ppps:
            self.process_one_ppp(ppp)
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

    def theme_screen_ACTION(self):
        """t - go to Theme Screen"""
        app = self.app
        if app.stack.curr.num in (SELECT_ST, CONVERT_ST):
            app.stack.push(THEME_ST, app.win.pick_pos)

    def kill_any_job(self):
        """ TBD """
        job = self.app.job
        if job:
            self.app.win.flash('Patiently wait for job to abort...')
            vid = job.vid
            job.abort()
            job = None
            if vid:
                vid.doit = '[X]'
            self.app.job_handler = None
            self.app.job = None

    def get_color_pair(self, line):
        """Attribute for one status row, honoring the no-color switch."""
        wds = line.lstrip().split(maxsplit=1)
        if not wds:
            return None
        word0 = wds[0]
        if word0.startswith('OK') or word0 == 'DUN':
            return appearance.semantic_attr(Theme.OLD_SUCCESS)
        if word0 == 'IP':
            return appearance.semantic_attr(Theme.HOTSWAP)
        if word0.startswith('Er') or word0 == 'ERR':
            return appearance.semantic_attr(Theme.ERROR)
        if word0 == '[X]':
            return appearance.semantic_attr(Theme.INFO)
        if word0 == '---':
            return appearance.semantic_attr(Theme.PROGRESS)
        return None


class SelectScreen(RmbloatScreen):
    """Video selection screen - choose which videos to convert"""

    def __init__(self, app):
        super().__init__(app)
        # Setup incremental search bar
        self.search_bar = IncrementalSearchBar(
            on_change=self._on_search_change,
            on_accept=self._on_search_accept,
            on_cancel=self._on_search_cancel
        )

    def _on_search_change(self, text):
        """Called when search text changes - apply filter incrementally."""
        self.app.search_re = text

    def _on_search_accept(self, text):
        """Called when ENTER pressed in search - keep filter active, exit input mode."""
        self.app.win.passthrough_mode = False
        self.app.search_re = text

    def _on_search_cancel(self, original_text):
        """Called when ESC pressed in search - restore and exit search mode."""
        self.app.search_re = original_text
        self.app.win.passthrough_mode = False

    def draw_screen(self):
        """Draw the video selection screen"""
        app = self.app
        win = app.win
        spins = app.spins

        app.win.set_pick_mode(True)

        # Get video list and stats
        lines, stats, co_wid = app.make_lines()

        # Header line with keys
        head = 'SELECT [r]setAll [i]nit SP:toggle [s]kip [g]o [h]ist [t]heme'
        head += f' [f]ilt={spins.filter} ?:help [q]uit'

        # Show incremental search bar if active, otherwise show current filter
        # Use HOTSWAP color (orange) for entire header when actively editing search
        if self.search_bar.is_active or app.win.passthrough_mode:
            # Add search display to header
            search_display = self.search_bar.get_display_string(prefix=' /', suffix='')
            head += search_display
            # Use plain header (no fancy parsing) with HOTSWAP color for entire line
            win.add_header(head,
                           attr=appearance.semantic_attr(Theme.HOTSWAP) | curses.A_BOLD)
        else:
            # Add static search pattern if present
            if app.search_re:
                shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
                head += f' /{shown}'
            # Use fancy header with normal colors
            win.add_fancy_header(head, app.opts.fancy_hdr)

        # Stats header
        cpu_status = app.cpu.get_status_string()
        win.add_header(f'     Picked={stats.picked}/{stats.total}'
                      f'  GB={stats.gb}({stats.delta_gb})'
                      f'  {cpu_status}'
                      + ('  >>> PAUSED <<<' if spins.freeze else ''))

        # Column headers
        win.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                      f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        # Video list
        for line in lines:
            win.add_body(line, attr=self.get_color_pair(line))

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
                if app.job and app.job.vid == vid:
                    self.kill_any_job()
                    app.job = None # This stops the progress line immediately
                    vid.doit = '---'
                    app.probe_cache.set_anomaly(vid.filepath, '---')
                elif vid.doit.startswith(('[', 'DUN')):
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
        self.kill_any_job()
        sys.exit(0)

    def search_ACTION(self):
        """'/' key - Start incremental search"""
        # Start with current search pattern
        self.search_bar.start(self.app.search_re if self.app.search_re else "")
        self.app.win.passthrough_mode = True


class ConvertScreen(RmbloatScreen):
    """Conversion progress screen - shows encoding progress"""

    def _stop_conversions(self):
        """Abort the running job and tear down the conversion engine.

        Clearing ``job_handler`` is what tells ``advance_jobs()`` to stay
        idle. It must happen even when no job is active (for example, the
        short gap between queued jobs): otherwise merely visiting History or
        Help after leaving Convert would silently resume the queue.
        """
        app = self.app
        self.kill_any_job()
        app.job = None
        app.job_handler = None

        # An interrupted run must not auto-transition straight back into
        # Convert on the next event-loop iteration.
        if app.auto_mode_enabled:
            app.auto_mode_enabled = False
            app.options_suffix = app.build_options_suffix()

    def screen_escape_ACTION(self):
        """ESC key - Return to previous screen (stop conversions)"""
        self._stop_conversions()
        self.app.stack.pop()

    def draw_screen(self):
        """Draw the conversion progress screen"""
        app = self.app
        win = app.win
        spins = app.spins

        app.win.set_pick_mode(False)

        # Get video list and stats
        lines, stats, co_wid = app.make_lines()

        # Header line with keys
        head = f'CONVERT [s]kip [f]ilt={spins.filter}  [h]ist [t]heme ?=help [q]uit'

        if app.search_re:
            shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
            head += f' /{shown}'

        cpu_status = app.cpu.get_status_string()
        head += (f'     ToDo={stats.total-stats.done}/{stats.total}'
                f'  GB={stats.gb}({stats.delta_gb})'
                f'  {cpu_status}'
                + ('  >>> PAUSED <<<' if spins.freeze else ''))
        win.add_fancy_header(head, app.opts.fancy_hdr)

        # Column headers
        win.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                      f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        # Set scroll position to show current job
        win.pick_pos = stats.progress_idx
        win.scroll_pos = stats.progress_idx - win.scroll_view_size + 2

        # Video list
        for line in lines:
            win.add_body(line, attr=self.get_color_pair(line))

    def skip_ACTION(self):
        """'s' key - Skip current conversion job"""
        app = self.app
        if app.job:
            vid = app.job.vid
            app.job.abort()
            app.job = None
            vid.doit = '---'
            app.probe_cache.set_anomaly(vid.filepath, '---')

    def quit_ACTION(self):
        """'q' key - Return to select screen (stop conversions)"""
        self._stop_conversions()
        self.app.stack.pop()


class HelpScreen(RmbloatScreen):
    """Help screen showing keyboard shortcuts"""

    def draw_screen(self):
        """Draw the help screen"""
        app = self.app
        win = app.win
        win.set_pick_mode(on=False)
        app.spinner.show_help_nav_keys(app.win)
        app.spinner.show_help_body(app.win)
        if app.ini:
            win.add_body('--- CLI ARGS ---')
            clis = vars(app.ini.vals)
            for key in clis:
                win.add_body(f'  ● {key}: {clis[key]}')


class ThemeScreen(Theme.create_picker_screen(RmbloatScreen, cycle_key='t')):
    """Theme picker that also offers rmbloat's ``mono`` pseudo-theme.

    The generated picker draws the library palettes and paints its swatches
    through the appearance layer.  rmbloat adds ``mono`` to the cycle on the
    picker itself (the ``theme`` spinner option), so selecting it turns color
    off exactly like ``--no-color`` while the header names that choice instead
    of the palette still installed underneath it.
    """

    def theme_label(self, theme_name):
        if not appearance.colors_enabled():
            return MONO_THEME
        return super().theme_label(theme_name)


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
        self.search_matches = set()  # Set of timestamps with deep-only matches in JSON
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

    def _refresh_entries(self):
        """Refresh cached log entries and re-apply the active search.

        New entries must be filtered as they arrive.  Previously only the
        unfiltered ``entries`` list was refreshed, so an active search kept
        showing a stale ``filtered_entries`` window until the search text was
        edited.
        """
        if self.window_of_logs is None:
            self.window_of_logs, self.window_state = lg.get_window_of_entries(
                window_size=1000)
        else:
            # Refresh window with any new entries
            self.window_of_logs, self.window_state = lg.refresh_window(
                self.window_of_logs, self.window_state, window_size=1000)

        # Convert to list in reverse order (newest first for display)
        self.entries = list(reversed(list(self.window_of_logs.values())))

        # Clean up self.expands: drop timestamps no longer in the window
        valid_timestamps = {entry.timestamp for entry in self.entries}
        self.expands = {ts: state for ts, state in self.expands.items()
                        if ts in valid_timestamps}

        self._filter_entries(self.search_bar.text)

    def _filter_entries(self, search_text):
        """Filter entries based on search text (shallow or deep)."""
        if not search_text:
            self.filtered_entries = self.entries
            self.search_matches = set()
            return

        # Deep search mode if starts with /
        deep_search = search_text.startswith('/')
        pattern = search_text[1:] if deep_search else search_text

        if not pattern:
            self.filtered_entries = self.entries
            self.search_matches = set()
            return

        # Use StructuredLogger's filter method
        self.filtered_entries, self.search_matches = lg.filter_entries(
            self.entries, pattern, deep=deep_search
        )

    def get_mangled_text(self, text):
        """ replace text that looks like video name"""
        extensions = self.app.VIDEO_EXTENSIONS
        # 1. Prepare extensions into an OR group: (?:mp4|mkv|...)
        ext_pattern = '|'.join([ext.lstrip('.') for ext in extensions])

        # 2. The Pattern:
        # (["'])          -> Group 1: The opening quote
        # ([^"']+?)       -> Group 2: The filename/path (non-greedy)
        # \.              -> Literal dot
        # ({ext_pattern}) -> Group 3: The extension
        # \1              -> Matches the same opening quote

        pattern = rf'''(["'])([^"']+?)\.({ext_pattern})\1'''

        def replace(match):
            quote = match.group(1)     # " or '
            name_part = match.group(2) # The file/path
            ext = match.group(3)       # The extension (e.g., mkv)

            mangled_name = Mangler.mangle(name_part)

            # Re-assemble: quote + mangled + . + preserved extension + quote
            return f"{quote}{mangled_name}.{ext}{quote}"

        return re.sub(pattern, replace, text)

    def draw_screen(self):
        """Draw the history screen"""
        app = self.app
        win = app.win
        win.set_pick_mode(True)

        # Refresh the window, cached entries, and the active filter together so
        # a new log entry cannot leave stale search results on screen.
        self._refresh_entries()

        # Count errors vs others in filtered results
        err_count = sum(1 for e in self.filtered_entries if e.level == 'ERR')
        other_count = len(self.filtered_entries) - err_count

        # Build search display string
        search_display = self.search_bar.get_display_string(prefix='', suffix='')

        # Header
        header_line = f'ESC:back [e]xpand [c]opy [a]go {len(self.filtered_entries)}/{len(self.entries)} (ERR:{err_count} oth:{other_count}) /'
        if search_display:
            header_line += f'{search_display}'
        header_line += '  >>> PAUSED <<<' if app.spins.freeze else ''
        win.add_fancy_header(header_line)
        win.add_header(f'Log file: {lg.log_file}')

        # Build display
        self.visible_lines = []
        for entry in self.filtered_entries:
            # Use timestamp as the unique key
            timestamp = entry.timestamp

            # Get display summary from entry
            summary = entry.display_summary

            # Extract JSON string if present (for expansion)
            json_str = None
            if '{' in entry.message:
                try:
                    json_start = entry.message.index('{')
                    json_str = entry.message[json_start:]
                except (ValueError, IndexError):
                    pass

            # Format timestamp based on spinner setting
            time_format = app.spins.hist_time_format if hasattr(app.spins, 'hist_time_format') else 'time'

            if time_format == 'ago':
                timestamp_display = f"{entry.format_ago():>6}"
            elif time_format == 'ago+time':
                ago = entry.format_ago()
                time_str = timestamp[:19]
                timestamp_display = f"{ago:>6} {time_str}"
            else:  # 'time'
                timestamp_display = timestamp[:19]  # Just the date and time part (YYYY-MM-DD HH:MM:SS)

            level = entry.level

            # Add deep match indicator if this entry matched only in JSON
            deep_indicator = " *" if timestamp in self.search_matches else ""

            # Choose color based on log level
            if level == 'ERR':
                level_attr = appearance.semantic_attr(Theme.ERROR) | curses.A_BOLD
            elif level == 'WARN':
                level_attr = appearance.semantic_attr(Theme.WARNING) | curses.A_BOLD
            else:
                level_attr = curses.A_BOLD

            if app.spins.mangle:
                summary = Mangler.mangle_title(summary)
            line = f"{timestamp_display} [{level:>3}] {summary}{deep_indicator}"
            win.add_body(line, attr=level_attr, context=Context("header", timestamp=timestamp))
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
                            display_lines = lines[:3] + ['  ...'*8] + lines[-17:]
                    else:  # expand_state == 2: Full
                        display_lines = lines

                    for line in display_lines:
                        if app.spins.mangle:
                            line = self.get_mangled_text(line)
                        sz = len(line) - len(line.lstrip())
                        wraps = textwrap.wrap(line, width=win.cols-3,
                                      subsequent_indent=' '*(sz+3), max_lines=3)
                        for wrap in wraps:
                            win.add_body(f"  {wrap}", context=
                                         Context("body", timestamp=timestamp))
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
                pos = win.pick_pos
                while pos >= 1:
                    # set position back to header
                    prev = win.body.contexts[pos-1]
                    if prev and getattr(prev, 'timestamp', '') == timestamp:
                        pos -= 1
                        continue
                    break
                win.pick_pos = pos
            else:
                self.expands[timestamp] = next_state

    def copy_log_ACTION(self):
        """'c' key - Copy entire log entry to clipboard (OSC 52) or export to file"""
        app = self.app
        win = app.win
        ctx = win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp

            # Find the entry with this timestamp
            entry = next((e for e in self.filtered_entries if e.timestamp == timestamp), None)

            if entry:
                # 1. Build the full log entry text
                lines = [
                    f"Timestamp: {entry.timestamp}",
                    f"Level: {getattr(entry, 'level', 'N/A')}",
                    f"File: {entry.file}:{entry.line}",
                    f"Function: {entry.function}()",
                    "",
                    "Message:",
                    entry.message or ""
                ]
                text = '\n'.join(lines)

                # 2. Prepare OSC 52 Magic
                success = False
                try:
                    b64_text = base64.b64encode(text.encode('utf-8')).decode('utf-8')
                    osc52 = f"\033]52;c;{b64_text}\a"

                    # Wrap for Tmux if necessary
                    if "TMUX" in os.environ:
                        osc52 = f"\033Ptmux;\033{osc52}\033\\"

                    # Write directly to terminal device to bypass Curses buffer
                    with open('/dev/tty', 'wb') as f:
                        f.write(osc52.encode('utf-8'))
                        f.flush()
                    success = True
                except Exception:
                    success = False

                # 3. Always write a private hard fallback in the XDG cache
                # directory.  The old fixed /tmp path was readable by other
                # users and could be replaced with a symlink.
                export_path = None
                try:
                    export_path = FileOps.write_history_export(text)
                except Exception as exc:
                    if not success:
                        win.alert(message=f'All copy methods failed: {exc}')

                # 4. User Feedback
                if success:
                    message = 'Copied (OSC 52).'
                    if export_path:
                        message += f' Fallback export: {export_path}'
                    win.alert(message=message)
                elif export_path:
                    win.alert(message=f'Clipboard blocked. Exported to {export_path}')

    def hist_search_ACTION(self):
        """ Handle '/' key - start incremental filter search """
        self.search_bar.start("")
        self.app.win.passthrough_mode = True


def bounded_int(low, high, label):
    """Build an ``argparse`` ``type=`` converter with an inclusive range.

    Numeric options used to be plain ``int``, so a value that only the encoder
    objects to (``-t 1000``) was accepted and later raised ``ValueError`` inside
    ``FfmpegChooser.make_ffmpeg_cmd()`` -- after the queue had started, where
    the exception tears down the curses session instead of reporting the bad
    option.  Validating during parsing reports the problem and exits with
    argparse's status 2 before any job or window exists.
    """
    def convert(text):
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f'{label}: expected an integer, got {text!r}') from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f'{label} must be between {low} and {high} (got {value})')
        return value

    return convert


def main(args=None) -> int:
    """
    Convert video files to desired form.

    Returns the process exit status: 0 on success, 1 when initialization or the
    main loop raises an unexpected exception.  Callers (cron, systemd, shell
    scripts) can therefore distinguish a successful run from a failed one.
    """
    try:
        ini = IniManager(app_name='rmbloat',
                               allowed_codecs='x265',
                               bloat_thresh=1600,
                               files=[],  # Default video collection paths
                               full_speed=False,
                               keep_backup=False,
                               max_height=65536,
                               merge_subtitles=False,
                               min_shrink_pct=10,
                               prefer_strategy='auto',
                               quality=28,
                               sandbox='auto',
                               thread_cnt=4,
                               use_trash=False,
                        )
        vals = ini.vals
        parser = argparse.ArgumentParser(
            description="CLI/curses bulk Video converter for media servers")
        # Standard display switches: --ascii/--glyphs, --theme and --no-color.
        # apply_display_arguments() below acts on them before any window exists.
        display_cli.add_display_arguments(parser, prog='rmbloat')
        # config options
        parser.add_argument('-a', '--allowed-codecs',
                    default=vals.allowed_codecs,
                    choices=('x26*', 'x265', 'all'),
                    help=f'allowed codecs [dflt={vals.allowed_codecs}]')
        parser.add_argument('-b', '--bloat-thresh',
                    default=vals.bloat_thresh, type=int,
                    help='bloat threshold to convert'
                        + f' [dflt={vals.bloat_thresh},min=500]')
        # Positive flags always force True (they never toggle a saved default).
        # The paired --no-* flags allow forcing False when the saved default is
        # True; when both forms are given, the last one on the command line
        # wins because both write the same dest.
        parser.add_argument('-F', '--full-speed',
                    dest='full_speed',
                    action='store_true',
                    default=vals.full_speed,
                    help='force full speed: do NOT set nice -n19 and ionice -c3'
                        + f' [dflt={vals.full_speed}]')
        parser.add_argument('--no-full-speed',
                    dest='full_speed',
                    action='store_false',
                    default=argparse.SUPPRESS,
                    help='force nice/ionice even if full speed is the saved default')
        parser.add_argument('-B', '--keep-backup',
                    dest='keep_backup',
                    action='store_true',
                    default=vals.keep_backup,
                    help='force keeping backups: rename to ORIG.{videofile}'
                         + f' [dflt={vals.keep_backup}]')
        parser.add_argument('--no-keep-backup',
                    dest='keep_backup',
                    action='store_false',
                    default=argparse.SUPPRESS,
                    help='force recycling the original even if backups are the saved default')
        parser.add_argument('-M', '--merge-subtitles',
                    dest='merge_subtitles',
                    action='store_true',
                    default=vals.merge_subtitles,
                    help='force merging external .en.srt subtitle files into output'
                    + f' [dflt={vals.merge_subtitles}]')
        parser.add_argument('--no-merge-subtitles',
                    dest='merge_subtitles',
                    action='store_false',
                    default=argparse.SUPPRESS,
                    help='force ignoring external subtitles even if merging is the saved default')
        parser.add_argument('-H', '--max-height',
                    default=vals.max_height,
                    type=bounded_int(1, 65536, 'max height'),
                    help='Maximum video height in pixels; videos taller than this will be scaled down'
                    + f' [dflt={vals.max_height},range=1-65536]')
        parser.add_argument('-m', '--min-shrink-pct',
                    default=vals.min_shrink_pct,
                    type=bounded_int(0, 100, 'minimum shrink percent'),
                    help='minimum conversion reduction percent for replacement'
                    + f' [dflt={vals.min_shrink_pct},range=0-100]')
        parser.add_argument('-p', '--prefer-strategy',
                    choices=FfmpegChooser.STRATEGIES,
                    default=vals.prefer_strategy,
                    help='FFmpeg strategy preference'
                        + f' [dflt={vals.prefer_strategy}]')
        parser.add_argument('-q', '--quality',
                    default=vals.quality,
                    type=bounded_int(0, 51, 'quality (CRF)'),
                    help=f'output quality (CRF, 0-51) [dflt={vals.quality}]')
        saved_sandbox = getattr(vals, 'sandbox', 'auto')
        parser.add_argument('--sandbox',
                    choices=Sandbox.SANDBOX_CHOICES,
                    default=saved_sandbox,
                    help='isolation for local ffmpeg/ffprobe: auto, container, '
                         'bwrap, systemd, or none'
                        + f' [dflt={saved_sandbox}]')
        parser.add_argument('-t', '--thread-cnt',
                    default=vals.thread_cnt,
                    type=bounded_int(0, 256, 'thread count'),
                    help='thread count for ffmpeg conversions (0 = let ffmpeg'
                         ' decide); applies to the software encoders only, the'
                         ' VAAPI hardware path ignores it'
                        + f' [dflt={vals.thread_cnt},range=0-256]')
        parser.add_argument('--use-trash',
                    dest='use_trash',
                    action='store_true',
                    default=vals.use_trash,
                    help='force using the trashcan for converted videos'
                        + f' [dflt={vals.use_trash}]')
        parser.add_argument('--no-use-trash',
                    dest='use_trash',
                    action='store_false',
                    default=argparse.SUPPRESS,
                    help='force deleting the original even if use-trash is the saved default')

        # run-time options
        parser.add_argument('-S', '--save-defaults', action='store_true',
                    help='save the -B/-b/-p/-q/-a/-F/-H/-m/-M options and file paths as defaults')
        parser.add_argument('--auto-hr', type=float, default=None,
                    help='Auto mode: run unattended for specified hours, '
                         'auto-select [X] files and auto-start conversions')
        parser.add_argument('-s', '--sample', action='store_true',
                    help='produce 30s samples named SAMPLE.<crf>.<output> '
                         'next to each input file')
        parser.add_argument('-L', '--logs', action='store_true',
                    help="view the logs (requires 'less')")
        parser.add_argument('-T', '--chooser-tests', action='store_true',
                    help='run tests on ffmpeg choices w 120s cvt of 1st given video')

        # Build help message for files argument showing defaults if set
        files_help = 'Video files and recursively scanned folders w Video files'
        if vals.files:
            files_help += f' [dflt: {", ".join(vals.files)}]'
        parser.add_argument('files', nargs='*', help=files_help)
        opts = parser.parse_args(args)
        # Decide the glyph mode, seed an explicit --theme and honor an explicit
        # --no-color before anything is drawn.  The remembered theme/color
        # decision is settled later in do_window_mode(), where the saved state
        # is available and can be combined with these switches.
        display_cli.apply_display_arguments(opts, prog='rmbloat')
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
            if not ini.write():
                # IniManager already explained the cause on stderr.  Without a
                # non-zero status a script (or cron/CI) could not tell that the
                # defaults it just saw printed were never persisted.
                print('Error: the new defaults were not saved.', file=sys.stderr)
                sys.exit(1)
            sys.exit(0)

        if opts.logs:
            files = [path for path in lg.log_paths if os.path.isfile(path)]
            if not files:
                print(f"No log file found at: {lg.log_file}", file=sys.stderr)
                sys.exit(1)
            # `less` is the viewer: it renders the ANSI sequences embedded in
            # ffmpeg output and can follow appended entries.  Check for it
            # before exec-ing so the requirement is stated plainly instead of
            # surfacing as a bare "Executable ... not found" from the attempt.
            if shutil.which('less') is None:
                print(
                    "Error: 'less' is required to view the logs but was not "
                    "found on PATH.\n"
                    "Install it (for example 'sudo apt install less') or read "
                    f"the log directly: {files[0]}",
                    file=sys.stderr)
                sys.exit(1)
            # -R renders ANSI escape sequences (e.g. ffmpeg output) instead of
            # showing their raw bytes; +F follows appended entries.
            cmd = ['less', '-R', '+F'] + files
            try:
                program = cmd[0]
                # This call replaces the current Python process
                os.execvp(program, cmd)
            except Exception as e:
                # Catch any execution error that survives the PATH check
                print(f"An error occurred during exec: {e}", file=sys.stderr)
                sys.exit(1)
        if opts.chooser_tests:
            chooser = FfmpegChooser(
                force_pull=True, sandbox=getattr(opts, 'sandbox', 'auto'))

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
                duration=120,
                show_test_encode=bool(video_file is None)  # Show example commands if no video
            )
            sys.exit(exit_code)

        opts.bloat_thresh = max(500, opts.bloat_thresh)

        # Let ProbeCache choose its private XDG cache directory.  The cache
        # contains media paths and must not live next to config.ini.
        Converter(opts, ini=ini).main_loop()
        return 0
    except Exception as exc:
        # Note: We no longer call Window.exit_handler(), as ConsoleWindow handles it
        # and there is no guarantee the Window class was ever initialized.
        if Converter.singleton and Converter.singleton.win:
            Converter.singleton.win.stop_curses()

        print("exception:", str(exc), file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    # When the script is run directly, call main and propagate its exit status
    # so failures are visible to shells, cron, systemd, and CI.
    sys.exit(main())
