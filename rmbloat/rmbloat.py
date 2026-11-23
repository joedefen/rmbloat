#!/usr/bin/env python3
""" rmbloat main """

import argparse
import atexit
import base64
import curses
import json
import os
import re
import shutil
import signal
import sys
import textwrap
import time
import traceback
from dataclasses import asdict
from types import SimpleNamespace

from console_window import (
    Appearance,
    BasicHelpScreen,
    ConsoleWindow,
    ConsoleWindowOpts,
    Context,
    Glyphs,
    IncrementalSearchBar,
    OptionSpinner,
    Screen,
    ScreenStack,
    Theme,
)
from console_window import cli as display_cli

from . import ConvertUtils, FileOps, Sandbox, StatusLine, __version__
from .CpuStatus import CpuStatus
from .FfmpegChooser import FfmpegChooser
from .IniManager import IniManager
from .JobHandler import JobHandler
from .Models import PathProbePair, Vid
from .PersistentState import PersistentState
from .ProbeCache import ProbeCache
from .StructuredLogger import StructuredLogger
from .VideoParser import Mangler

VERSION_BANNER = f'rmbloat {__version__}'

lg = StructuredLogger('rmbloat')

SELECT_ST = 0
CONVERT_ST = 1
HISTORY_ST = 2
HELP_ST = 3
THEME_ST = 4
SCREEN_NAMES = ('SELECT', 'CONVERT', 'HISTORY', 'HELP', 'THEME')

THEME_CHOICES = tuple(Theme.list_all())




def settle_appearance(cli_theme=None, saved_theme=''):
    """Turn the theme switches and the remembered choice into process state."""
    theme = Appearance.resolve_appearance(
        cli_theme=cli_theme,
        saved_theme=saved_theme,
    )
    if theme:
        Theme.set_active(theme)
    return theme

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

    ``atexit`` covers normal termination,
    ``sys.exit()`` and an uncaught ``KeyboardInterrupt``; SIGTERM/SIGHUP cover systemd,
    cron, and ``kill``.  The cleanup callable must be idempotent because both paths can run.
    """
    atexit.register(cleanup)

    def _handle_termination(signum, _frame):
        cleanup()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, _handle_termination)
        except (OSError, ValueError):
            # Not the main thread, or the platform lacks the signal: the atexit
            # registration still handles normal exits.
            pass


class Converter:
    """ TBD """
    TARGET_CODECS = ['h265', 'hevc']
    MAX_BITRATE_KBPS = 2100 # about 15MB/min (or 600MB for 40m)
    MAX_LOGGED_RUN_LINES = 200

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
        atexit.register(store_cache_on_exit)
        install_exit_handlers(self.emergency_cleanup)

        self.auto_mode_enabled = bool(opts.auto_hr is not None)
        self.auto_mode_hrs_limit = opts.auto_hr if self.auto_mode_enabled else None
        self.auto_mode_start_mono = time.monotonic() if self.auto_mode_enabled else None

        self.job_handler = None

        self.allow_reencode_dun = False

        self.options_suffix = self.build_options_suffix()
        self.screens = None
        self.stack = None
        self.need_freeze_draw = False
        self.persistent_state = PersistentState()
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
        """Make a theme-picker choice live."""
        self.opts.theme = choice
        self.remembered_theme = choice
        if self.win:
            self.win.set_theme(choice)

    def emergency_cleanup(self):
        """Abort an in-flight conversion and remove its temp file.

        Registered with ``atexit`` and the SIGTERM/SIGHUP handlers.
        The handler owns the active job and its temp path, so delegate to it when present;
        ``self.job`` is aborted as a fallback when the handler was torn down first.
        Safe to call more than once.
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

        ``Vid.ops`` records file-swap and cleanup actions.
        Routine entries (rename/link/trash) are ignored,
        but a swap failure or trash warning must be visible
        in the UI instead of only in the serialized job log.
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

        vid_list = self.todo_vids if is_convert else self.vids

        filter_mode = self.spins.filter if hasattr(self.spins, 'filter') else 'all'

        for vid in vid_list:
            if doit_skips and vid.doit in doit_skips:
                continue

            # Apply status filter (always show IP - in progress)
            if vid.doit != 'IP ' and filter_mode:
                doit = vid.doit if is_convert else vid.doit_auto
                is_err = bool(doit in ('ERR',) or doit.startswith('Er'))
                # Successful conversions are stored as ' OK'
                # (leading space) and retries as 'OK2'/'OK3', so compare after stripping.
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
                # A two-cell attention emoji, chosen through the glyph registry so the row
                # still degrades to its two-cell ASCII fallback.
                line += f'  {Glyphs.get("star_glow")} {note}'
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

        # 'OPT' means the transcode succeeded but did not shrink enough.
        is_ok = clean_label.startswith('OK') or clean_label == 'OPT'

        if is_ok:
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
        if clean_label == 'OK2':
            title += '-RETRY'
        elif clean_label == 'OK3':
            title += '-SOFTWARE'
        elif clean_label == 'OPT':
            title += '-REJECTED'

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
            next_vid = next((v for v in self.visible_vids if v and v.doit == '[X]'), None)

            if next_vid:
                if not os.path.isfile(next_vid.filepath):
                    self.vids = [v for v in self.vids if v != next_vid]
                    self.visible_vids = [v for v in self.visible_vids if v != next_vid]
                    continue

                if self.auto_mode_enabled:
                    if self._auto_mode_time_exceeded():
                        self._exit_auto_mode(
                            f'time limit of {self.auto_mode_hrs_limit} hours reached')
                    if self._auto_mode_failure_limit_reached():
                        self._exit_auto_mode('10 consecutive conversion failures')

                self.job = self.job_handler.start_transcode_job(next_vid)
                next_vid.doit = "IP "
                return

            # Queue is Empty - Handle Auto Mode Exit
            # An unattended run has nothing left to do,
            # so terminate instead of idling on the
            # Convert screen until the process is killed.
            if self.auto_mode_enabled:
                self._exit_auto_mode('conversion queue drained')

            # Interactive mode: We only pop the screen if we
            # are currently LOOKING at the Convert screen.
            # If we are on History/Help, we just stop the engine but stay on the screen.
            self.vids.sort(key=lambda v: (v.all_ok, v.bloat), reverse=True)

            if self.stack.curr.num == CONVERT_ST:
                self.job_handler = None
                self.stack.pop() # Go back to Select screen

            return

    def _conversion_active(self):
        """True while the Convert screen is current or beneath an overlay."""
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

        if self.job: # and self.stack.curr.num in (CONVERT_ST, HELP_ST):
            next_job, report, is_done = self.job_handler.check_job_status(self.job)

            if not is_done:

                if next_job is not None and next_job != self.job:
                    self.job = next_job
                    self.job.vid.doit = "IP "
            else:
                vid = self.job.vid

                probe = self.job_handler.finish_transcode_job(self.job)
                if probe:
                    self.job.vid.probe1 = self.apply_probe(self.job.vid, probe)

                if vid.doit in ('ERR', 'OPT'):
                    report = vid.doit
                vid.doit = vid.doit_auto = report

                self._log_job_final(self.job, report)

                self.job = None

        if not self.job and self._conversion_active():
            self._manage_queue_and_auto_mode()


    def apply_probe(self, vid, probe):
        """ TBD """
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

        return probe

    def append_vid(self, ppp):
        """Checks if a video file already meets the updated conversion criteria:
        - Resolution height is at or below max_height (configurable via --max-height).
        - Video codec is TARGET_CODECS (e.g., 'h264').
        - Video "bloat" is below bloat_thresh.

        Args: filepath (str): The path to the video file.

        Returns: bool: True if the file meets all criteria, False otherwise.
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
            exclusion_status = self.dont_doit(vid)
            if exclusion_status:
                vid.doit = exclusion_status  # 'DUN', 'OK', etc.
            elif vid.all_ok:
                vid.doit = '[ ]'
            else:
                vid.doit = '[X]'
        vid.doit_auto = vid.doit # auto value of doit saved for ease of re-init

    human_readable_size = staticmethod(ConvertUtils.human_readable_size)
    is_valid_video_file = staticmethod(ConvertUtils.is_valid_video_file)
    get_candidate_video_files = staticmethod(ConvertUtils.get_candidate_video_files)

    def standard_name(self, pathname: str, height: int) -> tuple[bool, str]:
        """Delegates to ConvertUtils.standard_name() with quality from opts.

        Returns: Whether changed and filename string with x265 codec.
        """
        return ConvertUtils.standard_name(pathname, height, self.opts.quality)

    def bulk_rename(self, old_file_name: str, new_file_name: str,
                    trashes: set):
        """Renames files and directories in the current working directory (CWD)."""
        return FileOps.bulk_rename(old_file_name, new_file_name, trashes)

    def process_one_ppp(self, ppp):
        """ Handle just one """
        input_file = ppp.video_file
        if ppp.probe is None:
            return  # Cannot evaluate a file that failed to probe
        if not self.is_valid_video_file(input_file):
            return  # Skip to the next file in the loop

        do_rename, standard_name = self.standard_name(input_file, ppp.probe.height)

        ppp.do_rename = do_rename
        ppp.standard_name = standard_name

        self.append_vid(ppp)

    def create_video_file_list(self):
        """ TBD """
        ppps = []

        # The directory walk below visits every entry under the given paths.
        # On a media share with tens of thousands of files that pause can last many seconds,
        # so the scan owns a single status line: it opens with the
        # announcement and is replaced by the final count and duration.
        # Keeping it to one line means the probe status that follows starts
        # on a fresh row and neither phase can overwrite or hide the other.
        scan_targets = [p for p in (getattr(self.opts, 'files', None) or [])
                        if p != '-']
        if scan_targets:
            label = (scan_targets[0] if len(scan_targets) == 1
                     else f"{len(scan_targets)} paths")
            # Keep the notice after the (stdout) detection summary
            # even when stdout is redirected to a file or pipe.
            sys.stdout.flush()
            StatusLine.begin(f"Scanning {label} for video files"
                             " (large collections can take a while)...")

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
                # Close the current stdin (the pipe). This frees fd 0.
                sys.stdin.close()
                # Open the TTY device (the actual keyboard/terminal)
                # os.O_RDONLY is read-only access.
                tty_fd = os.open('/dev/tty', os.O_RDONLY)
                # Replace file descriptor 0 (stdin) with the TTY descriptor.
                # os.open() normally reuses the just-freed fd 0,
                # in which case no dup2 is needed (and closing it below would close stdin).
                if tty_fd != 0:
                    os.dup2(tty_fd, 0)
                # Re-create the sys.stdin file object for Python's I/O.
                # os.fdopen(0, 'r') creates a new Python file object from FD 0.
                sys.stdin = os.fdopen(0, 'r')
                # Close the original descriptor only when it is a distinct fd.
                # Closing it when it *is* fd 0 would leave
                # sys.stdin backed by a closed descriptor, breaking the interactive session.
                if tty_fd != 0:
                    os.close(tty_fd)

            except OSError as e:
                # This handles cases where /dev/tty is not available (e.g., some non-interactive environments)
                sys.stderr.write(f"Error reopening TTY: {e}. Cannot enter interactive mode.\n")
                sys.exit(1)

        # Final probing.  ProbeCache owns the single status line for this
        # phase, so the scan above must already have terminated its own.
        results = self.probe_cache.batch_get_or_probe(paths_to_probe)
        for file_path, probe in results.items():
            # batch_get_or_probe() omits files it could not probe (missing, audio-only,
            # ...); never build a PathProbePair with a None probe.
            if probe is None:
                continue
            ppp = PathProbePair(file_path, probe)
            ppps.append(ppp)

        return ppps

    def dont_doit(self, vid):
        """Check if video should be excluded from re-encoding.

        Returns:
            str or None: Status string if should be excluded ('DUN', 'OK'), None otherwise
        """
        base = os.path.basename(vid.filepath).lower()

        # Already re-encoded files get "DUN" (done) status
        if base.endswith(('.recode.mkv', 'recode.sb.mkv')):
            return 'DUN'

        # Files marked as OK from previous successful conversion
        # or skip OK2/OK3 mean succeeded after retries - still done
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

        space_bloated_gb = 0.0
        for vid in self.vids:
            if vid.doit == '[X]' and vid.probe0:
                space_bloated_gb += vid.probe0.gb

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

        if self.win:
            self.win.stop_curses()
        print(report)

        lg.lg(report)

        exit_code = 1 if consecutive >= 10 else 0
        sys.exit(exit_code)

    def do_window_mode(self):
        """Main UI loop using Screen-based architecture"""
        cli_theme = getattr(self.opts, 'theme', None)
        self.persistent_state.restore_updated_opts(self.opts)
        saved_theme = getattr(self.opts, 'theme', None) or 'default'
        self.remembered_theme = Theme.resolve(saved_theme)
        theme = settle_appearance(cli_theme=cli_theme, saved_theme=saved_theme)
        self.opts.theme = theme or Theme.resolve(saved_theme)

        win_opts = ConsoleWindowOpts(
            head_line=True,
            min_cols_rows=(60,10),
            ctrl_c_terminates=False,
            theme=(theme or None),
        )
        self.win = win = ConsoleWindow(win_opts)
        self.screens = {
            SELECT_ST: SelectScreen(self),
            CONVERT_ST: ConvertScreen(self),
            HELP_ST: HelpScreen(self),
            HISTORY_ST: HistoryScreen(self),
            THEME_ST: ThemeScreen(self),
        }
        self.stack = ScreenStack(self.win, None, SCREEN_NAMES, self.screens)
        self.spinner = spin = OptionSpinner(stack=self.stack, escape_keys=(27,))
        spin.default_obj = self.opts
        spin.add_key('help_mode', '? - Help Screen', genre="action")
        spin.add_key('history', 'h - History Screen', genre='action',
                     scope=[SELECT_ST, CONVERT_ST])
        spin.add_key('theme_screen', 't - Theme Picker', genre='action',
                     scope=[SELECT_ST, CONVERT_ST])

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

        self.spins = spins = spin.default_obj
        curses.intrflush(False)

        win.set_pick_mode(True)

        force_redraw = False
        while True:
            if not spins.freeze or self.need_freeze_draw:
                current_screen = self.screens[self.stack.curr.num]
                current_screen.clear()
                current_screen.draw_screen()
                redraw = bool(time.monotonic() - self.redraw_mono >= 60) or force_redraw
                self.redraw_mono = time.monotonic() if redraw else self.redraw_mono
                win.render(redraw=redraw)
                force_redraw = False  # Reset after use
                self.need_freeze_draw = False

            current_screen = self.screens[self.stack.curr.num]
            timeout = current_screen.prompt_timeout
            key = win.prompt(seconds=timeout)

            if key and self.stack.curr.num in (HISTORY_ST, SELECT_ST):
                screen = self.stack.get_curr_obj()
                if screen and screen.search_bar.is_active:
                    if screen.search_bar.handle_key(key):
                        force_redraw = True  # Force full redraw on next iteration
                        key = None

            was_freeze = self.spins.freeze
            previous_theme = self.spins.theme
            if key in spin.keys:
                spin.do_key(key, win)
            if self.spins.theme != previous_theme:
                self.apply_theme_choice(self.spins.theme)

            if self.spins.freeze and not was_freeze:
                self.need_freeze_draw = True

            self.stack.perform_actions(spin)

            if self.auto_mode_enabled and self.stack.curr.num == SELECT_ST and not self.job:
                self.todo_vids = [v for v in self.vids if v.doit == '[X]']
                if self.todo_vids:
                    self.job_handler = JobHandler(
                        self.opts,
                        self.chooser,
                        self.probe_cache,
                        auto_mode_enabled=self.auto_mode_enabled
                    )
                    self.stack.push(CONVERT_ST)
                else:
                    self._exit_auto_mode('no videos selected for conversion')

            self.advance_jobs()
            self.persistent_state.set('theme', self.remembered_theme)
            self.persistent_state.sync()

    def main_loop(self):
        """ TBD """
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

        for ppp in ppps:
            self.process_one_ppp(ppp)
        self.do_window_mode()


# ============================================================================ Screen
# Classes ============================================================================

class RmbloatScreen(Screen):
    """Base screen class for rmbloat video converter"""
    app: 'Converter'  # Type hint for IDE
    prompt_timeout = 3.0  # Default timeout in seconds for win.prompt()

    def escape_ACTION(self):
        """ESC key - Return to previous screen (built-in escape action)."""
        if self.app.stack.curr.num != SELECT_ST:
            self.app.stack.pop()

    def help_mode_ACTION(self):
        """? - enter help mode"""
        app = self.app
        if app.stack.curr.num != HELP_ST:
            app.stack.push(HELP_ST)

    def history_ACTION(self):
        """h - go to History Screen"""
        app = self.app
        if app.stack.curr.num in (SELECT_ST, CONVERT_ST):
            app.stack.push(HISTORY_ST)

    def theme_screen_ACTION(self):
        """t - go to Theme Screen"""
        app = self.app
        if app.stack.curr.num in (SELECT_ST, CONVERT_ST):
            app.stack.push(THEME_ST)

    def kill_any_job(self):
        """ TBD """
        job = self.app.job
        if job:
            self.win.flash('Patiently wait for job to abort...')
            vid = job.vid
            job.abort()
            job = None
            if vid:
                vid.doit = '[X]'
            self.app.job_handler = None
            self.app.job = None

    def get_color_pair(self, line):
        """Attribute for one status row, honoring the active theme."""
        wds = line.lstrip().split(maxsplit=1)
        if not wds:
            return None
        word0 = wds[0]
        if word0.startswith('OK') or word0 == 'DUN':
            return Appearance.semantic_attr(Theme.OLD_SUCCESS)
        if word0 == 'IP':
            return Appearance.semantic_attr(Theme.HOTSWAP)
        if word0.startswith('Er') or word0 == 'ERR':
            return Appearance.semantic_attr(Theme.ERROR)
        if word0 == '[X]':
            return Appearance.semantic_attr(Theme.INFO)
        if word0 == '---':
            return Appearance.semantic_attr(Theme.PROGRESS)
        return None


class SelectScreen(RmbloatScreen):
    """Video selection screen - choose which videos to convert"""

    def __init__(self, app):
        super().__init__(app)
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
        self.win.passthrough_mode = False
        self.app.search_re = text

    def _on_search_cancel(self, original_text):
        """Called when ESC pressed in search - restore and exit search mode."""
        self.app.search_re = original_text
        self.win.passthrough_mode = False

    def draw_screen(self):
        """Draw the video selection screen"""
        app = self.app
        spins = app.spins

        self.set_pick_mode(True)

        lines, stats, co_wid = app.make_lines()

        head = 'SELECT [r]setAll [i]nit SP:toggle [s]kip [g]o [h]ist [t]heme'
        head += f' [f]ilt={spins.filter} ?:help [q]uit'

        if self.search_bar.is_active or self.win.passthrough_mode:
            search_display = self.search_bar.get_display_string(prefix=' /', suffix='')
            head += search_display
            self.add_header(head,
                            attr=Appearance.semantic_attr(Theme.HOTSWAP) | curses.A_BOLD)
        else:
            if app.search_re:
                shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
                head += f' /{shown}'
            self.add_fancy_header(head, app.opts.fancy_hdr)

        cpu_status = app.cpu.get_status_string()
        self.add_header(f'     Picked={stats.picked}/{stats.total}'
                       f'  GB={stats.gb}({stats.delta_gb})'
                       f'  {cpu_status}'
                       + ('  >>> PAUSED <<<' if spins.freeze else ''))

        self.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                       f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        for line in lines:
            self.add_body(line, attr=self.get_color_pair(line))

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
        idx = self.pick_pos
        if 0 <= idx < len(app.visible_vids):
            vid = app.visible_vids[idx]
            if vid:
                app.toggle_doit(vid)
                self.pick_pos += 1

    def skip_ACTION(self):
        """'s' key - Skip/unskip current video"""
        app = self.app
        idx = self.pick_pos
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
                self.pick_pos += 1

    def go_ACTION(self):
        """'g' key - Go to convert screen"""
        app = self.app

        app.todo_vids = []
        for vid in app.vids:
            if vid.doit == '[X]':
                app.todo_vids.append(vid)

        if app.todo_vids:
            app.job_handler = JobHandler(
                app.opts,
                app.chooser,
                app.probe_cache,
                auto_mode_enabled=app.auto_mode_enabled
            )
            app.stack.push(CONVERT_ST)

    def quit_ACTION(self):
        """'q' key - Quit application"""
        self.kill_any_job()
        sys.exit(0)

    def search_ACTION(self):
        """'/' key - Start incremental search"""
        self.search_bar.start(self.app.search_re if self.app.search_re else "")
        self.win.passthrough_mode = True


class ConvertScreen(RmbloatScreen):
    """Conversion progress screen - shows encoding progress"""

    def _stop_conversions(self):
        """Abort the running job and tear down the conversion engine.

        Clearing ``job_handler`` is what tells ``advance_jobs()`` to stay idle.
        It must happen even when no job is active (for example,
        the short gap between queued jobs): otherwise merely visiting History
        or Help after leaving Convert would silently resume the queue.
        """
        app = self.app
        self.kill_any_job()
        app.job = None
        app.job_handler = None

        # An interrupted run must not auto-transition straight
        # back into Convert on the next event-loop iteration.
        if app.auto_mode_enabled:
            app.auto_mode_enabled = False
            app.options_suffix = app.build_options_suffix()

    def escape_ACTION(self):
        """ESC key - Return to previous screen (stop conversions)"""
        self._stop_conversions()
        self.app.stack.pop()

    def draw_screen(self):
        """Draw the conversion progress screen"""
        app = self.app
        spins = app.spins

        self.set_pick_mode(False)

        lines, stats, co_wid = app.make_lines()

        head = f'CONVERT [s]kip [f]ilt={spins.filter}  [h]ist [t]heme ?=help [q]uit'

        if app.search_re:
            shown = Mangler.mangle(app.search_re) if spins.mangle else app.search_re
            head += f' /{shown}'

        cpu_status = app.cpu.get_status_string()
        head += (f'     ToDo={stats.total-stats.done}/{stats.total}'
                f'  GB={stats.gb}({stats.delta_gb})'
                f'  {cpu_status}'
                + ('  >>> PAUSED <<<' if spins.freeze else ''))
        self.add_fancy_header(head, app.opts.fancy_hdr)

        self.add_header(f'CVT {"NET":>4} {"BLOAT":>{co_wid}}  {"RES":>5}  '
                       f'{"CODEC":>5}  {"MINS":>4} {"MB":>6}   VIDEO{app.options_suffix}')

        self.pick_pos = stats.progress_idx
        self.scroll_pos = stats.progress_idx - self.win.scroll_view_size + 2

        for line in lines:
            self.add_body(line, attr=self.get_color_pair(line))

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


class HelpScreen(BasicHelpScreen, RmbloatScreen):
    """Help page: navigation, the spinner body, notes, fonts and CLI args.

    ``BasicHelpScreen`` supplies the 3.x help shape (nav block,
    keys for the pushed screen, instructions, and the built-in one-time Fonts check);
    this subclass only appends rmbloat's saved CLI arguments.
    """

    def draw_screen(self):
        """Draw the standard help page, then the saved CLI arguments."""
        super().draw_screen()
        app = self.app
        if not app.ini:
            return
        self.add_body('--- CLI ARGS ---')
        bullet = Glyphs.get('circle_filled')
        clis = vars(app.ini.vals)
        for key, value in clis.items():
            self.add_body(f'  {bullet} {key}: {value}')


class ThemeScreen(Theme.create_picker_screen(RmbloatScreen, cycle_key='t')):
    """Theme picker over the library palettes, including ``mono``.

    ``mono`` is a real theme in 3.x, so the generated picker previews it and names it
    in the header without any application pseudo-theme.
    """


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
        self.win.passthrough_mode = False
        # Filter remains active - just exit typing mode
        self.prev_filter = text

    def _on_search_cancel(self, original_text):
        """Called when ESC pressed in search - restore and exit search mode."""
        self._filter_entries(original_text)
        self.win.passthrough_mode = False

    def _refresh_entries(self):
        """Refresh cached log entries and re-apply the active search.

        New entries are filtered as they arrive,
        so an active search never keeps showing a stale
        ``filtered_entries`` window until the search text is edited.
        """
        if self.window_of_logs is None:
            self.window_of_logs, self.window_state = lg.get_window_of_entries(
                window_size=1000)
        else:
            # Refresh window with any new entries
            self.window_of_logs, self.window_state = lg.refresh_window(
                self.window_of_logs, self.window_state, window_size=1000)

        self.entries = list(reversed(list(self.window_of_logs.values())))

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

        # The Pattern:
        # (["']) -> Group 1: The opening quote ([^"']+?)
        # -> Group 2: The filename/path (non-greedy) \.
        # -> Literal dot ({ext_pattern}) -> Group 3: The
        # extension \1 -> Matches the same opening quote

        pattern = rf'''(["'])([^"']+?)\.({ext_pattern})\1'''

        def replace(match):
            quote = match.group(1)     # " or '
            name_part = match.group(2) # The file/path
            ext = match.group(3)       # The extension (e.g., mkv)

            mangled_name = Mangler.mangle(name_part)

            return f"{quote}{mangled_name}.{ext}{quote}"

        return re.sub(pattern, replace, text)

    def draw_screen(self):
        """Draw the history screen"""
        app = self.app
        win = self.win
        self.set_pick_mode(True)

        self._refresh_entries()

        err_count = sum(1 for e in self.filtered_entries if e.level == 'ERR')
        other_count = len(self.filtered_entries) - err_count

        search_display = self.search_bar.get_display_string(prefix='', suffix='')

        header_line = f'ESC:back [e]xpand [c]opy [a]go {len(self.filtered_entries)}/{len(self.entries)} (ERR:{err_count} oth:{other_count}) /'
        if search_display:
            header_line += f'{search_display}'
        header_line += '  >>> PAUSED <<<' if app.spins.freeze else ''
        self.add_fancy_header(header_line)
        self.add_header(f'Log file: {lg.log_file}')

        self.visible_lines = []
        for entry in self.filtered_entries:
            timestamp = entry.timestamp

            summary = entry.display_summary

            json_str = None
            if '{' in entry.message:
                try:
                    json_start = entry.message.index('{')
                    json_str = entry.message[json_start:]
                except (ValueError, IndexError):
                    pass

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

            if level == 'ERR':
                level_attr = Appearance.semantic_attr(Theme.ERROR) | curses.A_BOLD
            elif level == 'WARN':
                level_attr = Appearance.semantic_attr(Theme.WARNING) | curses.A_BOLD
            else:
                level_attr = curses.A_BOLD

            if app.spins.mangle:
                summary = Mangler.mangle_title(summary)
            line = f"{timestamp_display} [{level:>3}] {summary}{deep_indicator}"
            self.add_body(line, attr=level_attr,
                          context=Context("header", timestamp=timestamp))
            self.visible_lines.append(timestamp)

            expand_state = self.expands.get(timestamp, 0)
            if expand_state > 0 and json_str:
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
                            self.add_body(f"  {wrap}", context=
                                          Context("body", timestamp=timestamp))
                        self.visible_lines.append(None)  # Placeholder for expanded lines

                except (json.JSONDecodeError, ValueError):
                    self.add_body("  (invalid JSON)")
                    self.visible_lines.append(None)

            self.add_body("", context=Context("DECOR"))
            self.visible_lines.append(None)

    def expand_ACTION(self):
        """'e' key - Expand/collapse current entry"""
        ctx = self.win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp

            # Cycle through expansion states: 0 (collapsed) -> 1 (partial) -> 2 (full) -> 0
            current = self.expands.get(timestamp, 0)
            next_state = (current + 1) % 3
            if next_state == 0:
                if timestamp in self.expands:
                    del self.expands[timestamp]
                pos = self.pick_pos
                while pos >= 1:
                    prev = self.body.contexts[pos-1]
                    if prev and getattr(prev, 'timestamp', '') == timestamp:
                        pos -= 1
                        continue
                    break
                self.pick_pos = pos
            else:
                self.expands[timestamp] = next_state

    def copy_log_ACTION(self):
        """'c' key - Copy entire log entry to clipboard (OSC 52) or export to file"""
        win = self.win
        ctx = win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp

            entry = next((e for e in self.filtered_entries if e.timestamp == timestamp), None)

            if entry:
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
                # directory.
                export_path = None
                try:
                    export_path = FileOps.write_history_export(text)
                except Exception as exc:
                    if not success:
                        win.alert(message=f'All copy methods failed: {exc}')

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
        self.win.passthrough_mode = True


def bounded_int(low, high, label):
    """Build an ``argparse`` ``type=`` converter with an inclusive range."""
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
    """Convert video files to desired form.

    Returns the process exit status: 0 on success,
    1 when initialization or the main loop raises an unexpected exception.
    """
    argv = list(sys.argv[1:] if args is None else args)
    if '--version' in argv:
        print(VERSION_BANNER)
        return 0
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
        parser.add_argument('--version', action='version',
                    version=VERSION_BANNER,
                    help='show the release number and exit')
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

        files_help = 'Video files and recursively scanned folders w Video files'
        if vals.files:
            files_help += f' [dflt: {", ".join(vals.files)}]'
        parser.add_argument('files', nargs='*', help=files_help)
        opts = parser.parse_args(args)
        display_cli.apply_display_arguments(opts, prog='rmbloat')
            # Fake as option ...
            # if this needs tuning (which I doubt) then make it an actual option.
            # It is the max time allowed between progress updates when converting a video
        opts.progress_secs_max = 30

        # Use default files if none provided on command line (but not for --chooser-tests,
        # where no files means detection-only mode)
        if not opts.files and vals.files and not opts.chooser_tests:
            opts.files = vals.files
            print('Using default video collection paths from config:')
            for path in opts.files:
                print(f'  {path}')

        if opts.save_defaults:
            print('Setting new defaults:')
            for key in vars(vals):
                new_value = getattr(opts, key)
                if key == 'files':
                    new_value = sanitize_file_paths(new_value)
                    print(f'- {key} (sanitized):')
                    for path in new_value:
                        print(f'    {path}')
                else:
                    print(f'- {key} {new_value}')
                setattr(vals, key, new_value)
            if not ini.write():
                # IniManager already explained the cause on stderr.
                print('Error: the new defaults were not saved.', file=sys.stderr)
                sys.exit(1)
            sys.exit(0)

        if opts.logs:
            files = [path for path in lg.log_paths if os.path.isfile(path)]
            if not files:
                print(f"No log file found at: {lg.log_file}", file=sys.stderr)
                sys.exit(1)
            # `less` is the viewer: it renders the ANSI sequences
            # embedded in ffmpeg output and can follow appended entries.
            # Check for it before exec-ing so the requirement is stated
            # plainly instead of surfacing as a bare "Executable ...
            # not found" from the attempt.
            if shutil.which('less') is None:
                print(
                    "Error: 'less' is required to view the logs but was not "
                    "found on PATH.\n"
                    "Install it (for example 'sudo apt install less') or read "
                    f"the log directly: {files[0]}",
                    file=sys.stderr)
                sys.exit(1)
            # -R renders ANSI escape sequences (e.g.
            # ffmpeg output) instead of showing their raw bytes;
            # +F follows appended entries.
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
                paths, _ = Converter.get_candidate_video_files(opts.files)
                if paths:
                    video_file = paths[0]
                    print(f"\nTesting with video: {video_file}")
                else:
                    print("\nWarning: No valid video files found, running basic tests only")

            exit_code = chooser.run_tests(
                video_file=video_file,
                duration=120,
                show_test_encode=bool(video_file is None)  # Show example commands if no video
            )
            sys.exit(exit_code)

        opts.bloat_thresh = max(500, opts.bloat_thresh)

        # Let ProbeCache choose its private XDG cache directory.
        # The cache contains media paths and must not live next to config.ini.
        Converter(opts, ini=ini).main_loop()
        return 0
    except Exception as exc:
        if Converter.singleton and Converter.singleton.win:
            Converter.singleton.win.stop_curses()

        print("exception:", str(exc), file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
