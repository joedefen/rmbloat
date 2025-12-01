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
import random
import curses
from pathlib import Path
from dataclasses import asdict
from types import SimpleNamespace
from datetime import timedelta
import send2trash
from console_window import ConsoleWindow, OptionSpinner
from .ProbeCache import ProbeCache
from .VideoParser import Mangler
from .IniManager import IniManager
from .RotatingLogger import RotatingLogger
from .CpuStatus import CpuStatus
from .FfmpegChooser import FfmpegChooser
from .Models import PathProbePair, Vid, Job
from . import FileOps
from . import ConvertUtils

lg = RotatingLogger('rmbloat')

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

#       # Regex to find FFmpeg progress lines (from stderr)
#       # Looks for 'frame=  XXXXX' and 'time=00:00:00.00' and 'speed=XX.XXx'
#       "frame= 2091 fps= 26 q=35.9 size=   11264KiB time=00:01:27.12 bitrate=1059.1kbits/s speed= 1.1x    \r",
    PROGRESS_RE = re.compile(
        # 1. Mandatory Frame Number (Group 1)
        # The \s* at the beginning accounts for possible leading whitespace
        r"\s*frame[=\s]+(\d+)\s+"

        # 2. Time Section (Optional, Strict Numerical Capture)
        # Looks for 'time=', then attempts to capture the precise HH:MM:SS.cs format (G2-G5).
        r"(?:.*?time[=\s]+(\d{2}):(\d{2}):(\d{2})\.(\d{2}))?"

        # 3. Speed Section (Optional, Strict Numerical Capture)
        # Looks for 'speed=', then captures the float (G6).
        r"(?:.*?speed[=\s]+(\d+\.\d+)x)?",

        re.IGNORECASE
    )

    # Constants moved to ConvertUtils.py
    VIDEO_EXTENSIONS = ConvertUtils.VIDEO_EXTENSIONS
    SKIP_PREFIXES = ConvertUtils.SKIP_PREFIXES
    sample_seconds = 30
    singleton = None

    def __init__(self, opts, cache_dir='/tmp'):
        assert Converter.singleton is None
        Converter.singleton = self
        self.win = None
        self.redraw_mono = time.monotonic()
        self.opts = opts
        self.spins = None # spinner values
        self.search_re = None # the "accepted" search
        self.vids = []
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
        self.progress_line_mono = 0
        self.start_job_mono = 0
        self.cpu = CpuStatus()
        # self.cgroup_prefix = set_cgroup_cpu_limit(opts.thread_cnt*100)
        atexit.register(store_cache_on_exit)

        # Auto mode tracking
        self.auto_mode_enabled = bool(opts.auto_hr is not None)
        self.auto_mode_start_time = time.monotonic() if self.auto_mode_enabled else None
        self.auto_mode_hrs_limit = opts.auto_hr if self.auto_mode_enabled else None
        self.consecutive_failures = 0
        self.ok_count = 0
        self.error_count = 0

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
            vid.doit = '[ ]' if vid.all_ok or self.dont_doit(vid) else '[X]'
        vid.doit_auto = vid.doit # auto value of doit saved for ease of re-init

    # Utility functions moved to ConvertUtils.py
    bash_quote = staticmethod(ConvertUtils.bash_quote)


    def generate_taskset_core_list(self, desired_cores: int = 3) -> str:
        """
        Generates a comma-separated list of core indices for taskset, prioritizing
        physically separate cores when utilization is low (<= 50% of capacity).
        Args:
            total_logical_cores (int): Total logical cores (e.g., 16 for 8 cores with HT).
            desired_cores (int): The number of cores to select (e.g., 3).
        Returns:
            str: A comma-separated string of unique core indices (e.g., "0,4,8").
        """
        total_logical_cores = self.cpu.core_count
        desired_cores = max(1, min(total_logical_cores, desired_cores))
        selected_cores = set()
        mask = 0

        # 1. Determine the step size based on utilization
        # Half the total logical cores (e.g., 16 / 2 = 8). This is the number of physical cores.
        step = 2 if desired_cores <= total_logical_cores // 2  else 1

        # 2. Choose a random starting core
        current_core = random.randrange(desired_cores)

        # 3. Select the cores
        for _ in range(desired_cores):
            # Add the core, then use the step and modulo operation to find the next one
            selected_cores.add(current_core)
            mask += 1<<current_core
            current_core = (current_core + step) % total_logical_cores

        # 4. Format and return
        # Sort for cleaner display, then convert to string
        return f'{mask:#x}'
        # return '--cpu-set " + ",".join(map(str, sorted(list(selected_cores))))

    def make_color_opts(self, color_spt):
        """ TBD """
        spt_parts = color_spt.split(',')

        # 1. Reconstruct the three full, original values (can contain 'unknown')
        space_orig = spt_parts[0]
        primaries_orig = spt_parts[1] if spt_parts[1] != "~" else space_orig
        trc_orig = spt_parts[2] if spt_parts[2] != "~" else primaries_orig

        # 2. Define the final, valid FFmpeg values using fallback logic

        # Use BT.709 as the default standard for all three components
        DEFAULT_SPACE = 'bt709'
        DEFAULT_PRIMARIES = 'bt709'
        DEFAULT_TRC = '709' # Note: TRC often uses '709' instead of 'bt709' string

        # Check and replace 'unknown' or invalid values with the safe default

        # Color Space:
        if space_orig == 'unknown':
            space = DEFAULT_SPACE
        else:
            space = space_orig

        # Color Primaries:
        if primaries_orig == 'unknown':
            primaries = DEFAULT_PRIMARIES
        else:
            primaries = primaries_orig

        # Color TRC:
        if trc_orig == 'unknown':
            trc = DEFAULT_TRC
        # FFmpeg also sometimes prefers the numerical '709' over 'bt709' for TRC
        elif trc_orig == 'bt709':
            trc = DEFAULT_TRC
        else:
            trc = trc_orig

        # --- Use these final 'space', 'primaries', and 'trc' variables in the FFmpeg command ---

        color_opts = [
            '-colorspace', space,
            '-color_primaries', primaries,
            '-color_trc', trc
        ]
        return color_opts

    def start_transcode_job(self, vid):
        """Start a transcoding job using FfmpegChooser."""
        os.chdir(os.path.dirname(vid.filepath))
        basename = os.path.basename(vid.filepath)
        probe = vid.probe0

        merged_external_subtitle = None
        if self.opts.merge_subtitles:  # Assuming you'll add this flag to opts
            subtitle_path = Path(vid.filepath).with_suffix('.en.srt')
            if subtitle_path.exists():
                merged_external_subtitle = str(subtitle_path)
                vid.standard_name = str(Path(vid.standard_name).with_suffix('.sb.mkv'))

        # Determine output file paths
        prefix = f'/heap/samples/SAMPLE.{self.opts.quality}' if self.opts.sample else 'TEMP'
        temp_file = f"{prefix}.{vid.standard_name}"
        orig_backup_file = f"ORIG.{basename}"

        if os.path.exists(temp_file):
            os.unlink(temp_file)

        # Calculate duration
        duration_secs = probe.duration
        if self.opts.sample:
            duration_secs = self.sample_seconds

        job = Job(vid, orig_backup_file, temp_file, duration_secs)
        job.input_file = basename

        # Create namespace with defaults
        params = self.chooser.make_namespace(
            input_file=job.input_file,
            output_file=job.temp_file
        )

        # Set quality
        params.crf = self.opts.quality

        # Set priority
        params.use_nice_ionice = not self.opts.full_speed

        # Set thread count
        params.thread_count = self.opts.thread_cnt

        # Sampling options
        if self.opts.sample:
            params.sample_mode = True
            start_secs = max(120, job.duration_secs) * 0.20
            params.pre_input_opts = ['-ss', job.duration_spec(start_secs)]
            params.post_input_opts = ['-t', str(self.sample_seconds)]

        # Scaling options
        MAX_HEIGHT = 1080
        if probe.height > MAX_HEIGHT:
            width = MAX_HEIGHT * probe.width // probe.height
            params.scale_opts = ['-vf', f'scale={width}:-2']

        # Color options
        params.color_opts = self.make_color_opts(vid.probe0.color_spt)

        # Stream mapping options
        map_copy = '-map 0:v:0 -map 0:a? -c:a copy -map'

        # Check for external subtitle file
        if merged_external_subtitle:
            # Don't copy internal subtitles, we're replacing with external
            map_copy += ' -0:s -map -0:t -map -0:d'
        else:
            # Copy internal subtitles, but drop unsafe ones (bitmap codecs like dvd_subtitle)
            # Check if probe has custom instructions to drop specific subtitle streams
            if probe.customs and 'drop_subs' in probe.customs:
                # Map all subtitles first, then explicitly exclude the unsafe ones
                map_copy += ' 0:s?'
                for sub_idx in probe.customs['drop_subs']:
                    map_copy += f' -map -0:s:{sub_idx}'
                map_copy += ' -map -0:t -map -0:d'
            else:
                # No custom subtitle filtering needed
                map_copy += ' 0:s? -map -0:t -map -0:d'

        params.map_opts = map_copy.split()
        params.external_subtitle = merged_external_subtitle

        # Set subtitle codec to srt for MKV compatibility (transcodes mov_text, ass, etc.)
        # When external subtitle is used, FfmpegChooser handles the codec internally
        params.subtitle_codec = 'srt' if not merged_external_subtitle else 'copy'

        # Generate the command
        ffmpeg_cmd = self.chooser.make_ffmpeg_cmd(params)

        # Store command for logging
        vid.command = self.bash_quote(ffmpeg_cmd)

        # Start the job
        if not self.opts.dry_run:
            job.ffsubproc.start(ffmpeg_cmd, temp_file=job.temp_file)
            self.progress_line_mono = time.monotonic()
        return job

    def monitor_transcode_progress(self, job):
        """
        Runs the FFmpeg transcode command and monitors its output for a non-scrolling display.
        """
        if not self.opts.dry_run:
            # --- Progress Monitoring Loop ---
            # Read stderr line-by-line until the process finishes
            while True:
                time.sleep(0.1)

                got = self.get_job_progress(job)

                    # 4. Print and reset timer
                if isinstance(got, str):
                    print('\r' + got, end='', flush=True)
                elif isinstance(got, int):
                    return_code = got
                    break

            # Clear the progress line and print final status
            print('\r' + ' ' * 120, end='', flush=True) # Overwrite last line with spaces

        if self.opts.dry_run or return_code == 0:
            print(f"\r{job.input_file}: Transcoding FINISHED"
                  f" (Elapsed: {timedelta(seconds=int(time.monotonic() - job.start_mono))})")
            return True # Success
        else:
            # Print a final error message
            print(f"\r{job.input_file}: Transcoding FAILED (Return Code: {job.return_code})")
            # In a real script, you'd save or display the full error output from stderr here.
            return False

    def get_job_progress(self, job):
        """ TBD """
        def rough_progress(frame_number):
            nonlocal job, vid
            total_frames = int(round(vid.probe0.fps * vid.probe0.duration))
            frame_number = int(frame_number)

            elapsed_time_sec = int(time.monotonic() - job.start_mono)

            if elapsed_time_sec < 5 or total_frames == 0:
                # Too early for a reliable estimate or total frames unknown
                return f"Frame {frame_number}: MAKING PROGRESS..."

            # 1. Calculate Estimated Total Time (based on elapsed time and frames done)
            # T_est = (Elapsed Time / Frames Done) * Total Frames
            # Avoid division by zero:
            if frame_number == 0:
                return f"Frame {frame_number}: MAKING PROGRESS..."
            estimated_total_time = (elapsed_time_sec / frame_number) * total_frames
            # 2. Calculate Remaining Time
            remaining_seconds = estimated_total_time - elapsed_time_sec
            # 3. Calculate Current FPS (Virtual Speed)
            current_fps, speed = frame_number / elapsed_time_sec, 'UNKx'
            if vid.probe0.fps > 0:
                speed = f'{round(current_fps / vid.probe0.fps, 1)}x'

            # --- Format the output line using the estimated values ---
            percent_complete = (frame_number / total_frames) * 100
            remaining_time_formatted = job.trim0(str(timedelta(seconds=int(remaining_seconds))))
            elapsed_time_formatted = job.trim0(str(timedelta(seconds=elapsed_time_sec)))
            if job.duration_secs > 0:
                at_seconds = (frame_number / total_frames) * job.duration_secs
                at_seconds_formatted = job.trim0( str(timedelta(seconds=int(at_seconds))))
                at_formatted = f'At ~{at_seconds_formatted}/{job.total_duration_formatted}'
            else:
                at_formatted = f"Frame {frame_number}/{total_frames}"

            # Use the estimated FPS for the speed report
            progress_line = (
                f"{percent_complete:.1f}% | "
                f"{elapsed_time_formatted} | "
                f"-{remaining_time_formatted} | "
                f"~{speed} | " # Report FPS clearly as Estimated
                + at_formatted
            )
            return progress_line


        vid = job.vid
        secs_max = self.opts.progress_secs_max
        while True:
            got = job.ffsubproc.poll()
            now_mono = time.monotonic()
            # print(f'\r{delta=} {got=}')
            if now_mono - self.progress_line_mono > secs_max:
                got = 254
                vid.texts.append('PROGRESS TIMEOUT')
                job.ffsubproc.stop(return_code=got)
                self.progress_line_mono = time.monotonic() + 1000000000
                continue

            if isinstance(got, str):
                line = got
                match = self.PROGRESS_RE.search(line)
                if not match:
                    vid.texts.append(line)
                    continue

                if now_mono - self.progress_line_mono < 3:
                    continue # don't update progress crazy often
                self.progress_line_mono = time.monotonic()

                # 1. Extract values from the regex match
                groups = match.groups()
                try:

                    # The first two parts (H and M) are integers. The third part (S.ms) is the float.
                    h = int(groups[1])
                    m = int(groups[2])
                    s = int(groups[3])
                    ms = int(groups[4])
                    time_encoded_seconds = h * 3600 + m * 60 + s + ms / 100
                    time_encoded_seconds = round(int(time_encoded_seconds))
                    speed = float(match.group(6))
                except Exception:
                    # return f"Frame {groups[0]}:  MAKING PROGRESS..."
                    return rough_progress(groups[0])

                elapsed_time_sec = int(time.monotonic() - job.start_mono)

                    # 2. Calculate remaining time
                if job.duration_secs > 0:
                    percent_complete = (time_encoded_seconds / job.duration_secs) * 100
                    if percent_complete > 0 and speed > 0:
                        # Time Remaining calculation (rough estimate)
                        # Remaining Time = (Total Time - Encoded Time) / Speed
                        remaining_seconds = (job.duration_secs - time_encoded_seconds) / speed
                        remaining_time_formatted = job.trim0(str(timedelta(seconds=int(remaining_seconds))))
                    else:
                        remaining_time_formatted = "N/A"
                else:
                    percent_complete = 0.0
                    remaining_time_formatted = "N/A"

                # 3. Format the output line
                # \r at the start makes the console cursor go back to the beginning of the line
                cur_time_formatted = job.trim0(str(timedelta(seconds=time_encoded_seconds)))
                progress_line = (
                    f"{percent_complete:.1f}% | "
                    f"{job.trim0(str(timedelta(seconds=elapsed_time_sec)))} | "
                    f"-{remaining_time_formatted} | "
                    f"{speed:.1f}x | "
                    f"At {cur_time_formatted}/{job.total_duration_formatted}"
                )
                return progress_line
            elif isinstance(got, int):
                vid.return_code = got
                return got
            else:
                return got

    human_readable_size = staticmethod(ConvertUtils.human_readable_size)

    is_valid_video_file = staticmethod(ConvertUtils.is_valid_video_file)

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

    def convert_one_file(self, vid):
        """ TBD """

        # 3. Transcode with monitored progress
        job = self.start_transcode_job(vid)
        success = self.monitor_transcode_progress(job)
        self.finish_transcode_job(success, job)

    def finish_transcode_job(self, success, job):
        """ TBD """
        # 4. Atomic Swap (Safe Replacement)
        dry_run = self.opts.dry_run
        vid = job.vid
        probe = None
        # space_saved_gb = 0.0
        if success:
            if not dry_run:
                probe = self.probe_cache.get(job.temp_file)
            if not probe:
                if dry_run:
                    success = True
                    vid.doit = 'ok'
                    net = -20
                    # space_saved_gb = vid.gb * 0.20  # Estimate for dry run
                else:
                    success = False
                    vid.doit = 'ERR'
                    net = 0
            else:
                net = (vid.gb - probe.gb) / vid.gb
                net = int(round(-net*100))
                # space_saved_gb = vid.gb - probe.gb
            if self.is_allowed_codec(probe) and net > -self.opts.min_shrink_pct:
                self.probe_cache.set_anomaly(vid.filepath, 'OPT')
                success = False
            vid.net = f'{net}%'

        # Track auto mode vitals
        if self.auto_mode_enabled:
            if success and not self.opts.sample:
                self.ok_count += 1
                self.consecutive_failures = 0
            elif not success:
                self.error_count += 1
                self.consecutive_failures += 1

        if success and not self.opts.sample:
            would = 'WOULD ' if dry_run else ''
            trashes = set()
            basename = os.path.basename(vid.filepath)

            # Preserve timestamps from original file
            timestamps = None
            if not dry_run:
                timestamps = FileOps.preserve_timestamps(basename)

            try:
                # Rename original to backup
                if not dry_run and self.opts.keep_backup:
                    os.rename(basename, job.orig_backup_file)
                if self.opts.keep_backup:
                    vid.ops.append(
                        f"{would} rename {basename!r} {job.orig_backup_file!r}")
                if not dry_run and not self.opts.keep_backup:
                    send2trash.send2trash(basename)
                if dry_run and not self.opts.keep_backup:
                    trashes.add(basename)
                if not self.opts.keep_backup:
                    vid.ops.append(f"{would}trash {basename!r}")

                # Rename temporary file to the original filename
                if not dry_run:
                    os.rename(job.temp_file, vid.standard_name)
                vid.ops.append(
                    f"{would}rename {job.temp_file!r} {vid.standard_name!r}")

                if vid.do_rename:
                    vid.ops += self.bulk_rename(basename, vid.standard_name, trashes)

                if not dry_run:
                    # Apply preserved timestamps to the new file
                    FileOps.apply_timestamps(vid.standard_name, timestamps)

                    # probe = self.get_video_metadata(vid.standard_name)
                    vid.basename1 = vid.standard_name
                    vid.probe1 = self.apply_probe(vid, probe)

            except OSError as e:
                print(f"ERROR during swap of {vid.filepath}: {e}")
                print(f"Original: {job.orig_backup_file}, New: {job.temp_file}. Manual cleanup required.")
        elif success and self.opts.sample:
            # probe = self.get_video_metadata(job.temp_file)
            vid.basename1 = job.temp_file
            vid.probe1 = self.apply_probe(vid, probe)
        elif not success:
            # Transcoding failed, delete the temporary file
            if os.path.exists(job.temp_file):
                os.remove(job.temp_file)
                print(f"FFmpeg failed. Deleted incomplete {job.temp_file}.")
            self.probe_cache.set_anomaly(vid.filepath, 'Err')

    get_candidate_video_files = staticmethod(ConvertUtils.get_candidate_video_files)

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
        """ Returns true if prohibited from re-encoding """
        base = os.path.basename(vid.filepath).lower()
        if (base.startswith('sample.')
                or base.startswith('test.')
                or base.endswith('.recode.mkv')
                or vid.doit in ('OK',)
                ):
            return True
        return False

    def print_auto_mode_vitals(self, stats):
        """Print vitals report and exit for auto mode."""
        runtime_hrs = (time.monotonic() - self.auto_mode_start_time) / 3600

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
        report += f"OK conversions:       {self.ok_count}\n"
        report += f"Error conversions:    {self.error_count}"

        if self.consecutive_failures >= 10:
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
        exit_code = 1 if self.consecutive_failures >= 10 else 0
        sys.exit(exit_code)

    def do_window_mode(self):
        """ TBD """
        def make_lines(doit_skips=None):
            nonlocal self
            lines, self.visible_vids = [], []
            stats = SimpleNamespace(total=0, picked=0, done=0, progress_idx=0,
                                    gb=0, delta_gb=0)
            jobcnt = 0

            for vid in self.vids:
                if self.state == 'convert' and vid.doit == '[ ]':
                    continue
                if doit_skips and vid.doit in doit_skips:
                    continue
                basename = vid.basename1 if vid.basename1 else os.path.basename(vid.filepath)
                dirname = os.path.dirname(vid.filepath)
                if self.spins.mangle:
                    basename = Mangler.mangle_title(basename)
                    dirname = Mangler.mangle(dirname)
                res = f'{vid.height}p'
                ht_over = ' ' if vid.res_ok else '^' # '■'
                br_over = ' ' if vid.bloat_ok else '^' # '■'
                co_over = ' ' if vid.codec_ok else '^'
                mins = int(round(vid.duration / 60))
                line = f'{vid.doit:>3} {vid.net} {vid.bloat:5}{br_over} {res:>5}{ht_over}'
                line += f' {vid.codec:>5}{co_over} {mins:>4} {vid.gb:>6.3f}   {basename} ---> {dirname}'
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
                # nses.append(vid)
                self.visible_vids.append(vid)
                if self.job and self.job.vid == vid:
                    jobcnt += 1
                    lines.append(f'-----> {self.job.progress}')
                    stats.progress_idx = len(self.visible_vids)
                    self.visible_vids.append(None)
                    if self.win.pick_mode:
                        stats.progress_idx -= 1
                        self.win.set_pick_mode(False)

                # print(line)
            stats.total = len(self.visible_vids) - jobcnt
            stats.gb = round(stats.gb, 1)
            stats.delta_gb = round(stats.delta_gb, 1)
            return lines, stats

        def render_screen():
            nonlocal self, spin, win
            if self.state == 'help':
                spin.show_help_nav_keys(win)
                spin.show_help_body(win)
            else:
                lines, stats = make_lines()
                if self.state == 'select':
                    # head = '[s]etAll [r]setAll [i]nit SP:toggle [g]o ?=help [q]uit'
                    head = '[r]setAll [i]nit SP:toggle [g]o ?=help [q]uit'
                    if self.search_re:
                        shown = Mangler.mangle(self.search_re) if spins.mangle else self.search_re
                        head += f' /{shown}'
                    win.add_header(head)
                    cpu_status = self.cpu.get_status_string()
                    win.add_header(f'     Picked={stats.picked}/{stats.total}'
                                   f'  GB={stats.gb}({stats.delta_gb})'
                                   f'  {cpu_status}'
                                   )
                    # lg.lg(f'{cpu_status=}')
                if self.state == 'convert':
                    head = ' ?=help q[uit]'
                    if self.search_re:
                        shown = Mangler.mangle(self.search_re) if spins.mangle else self.search_re
                        head += f' /{shown}'
                    cpu_status = self.cpu.get_status_string()
                    head += (f'     ToDo={stats.total-stats.done}/{stats.total}'
                                f'  GB={stats.gb}({stats.delta_gb})'
                                f'  {cpu_status}'
                                )
                    win.add_header(head)
                    # lg.lg(f'{cpu_status=}')

                win.add_header(f'CVT {"NET":>4} {"BLOAT":>5}  {"RES":>5}  {"CODEC":>5}  {"MINS":>4} {"GB":>6}   VIDEO{self.options_suffix}')
                if self.state == 'convert':
                    win.pick_pos = stats.progress_idx
                    win.scroll_pos = stats.progress_idx - win.scroll_view_size + 2
                for line in lines:
                    win.add_body(line)
            redraw = bool(time.monotonic() - self.redraw_mono >= 60)
            self.redraw_mono = time.monotonic() if redraw else self.redraw_mono
            win.render(redraw=redraw)

        def handle_keyboard():
            nonlocal self, spins, not_help_state

            if spins.help_mode:
                if self.state != 'help':
                    # enter help mode
                    not_help_state = self.state
                    self.state = 'help'
                    win.set_pick_mode(False, 1)
            else:
                if self.state == 'help':
                    # leave help mode
                    self.state = not_help_state
                    if self.state == 'select':
                        win.set_pick_mode(True, 1)
                    else:
                        win.set_pick_mode(False, 1)

            if spins.search != self.search_re:
                valid = True
                if self.state != 'select':
                    win.alert(message='Cannot change search unless in select screen')
                    valid = False
                try:
                    re.compile(spins.search)
                except Exception as exc:
                    win.alert(message=f'Ignoring invalid search: {exc}')
                    valid = False
                if valid:
                    self.search_re = spins.search
                else: # ignore pattern changes unless in select or if won't compile
                    spins.search = self.search_re

#           if spins.set_all:
#               spins.set_all = False
#               if self.state == 'select':
#                   for vid in self.visible_vids:
#                       if not self.dont_doit(vid) and vid.doit_auto.startswith('['):
#                           vid.doit = '[X]'

            if spins.reset_all:
                spins.reset_all = False
                if self.state == 'select':
                    for vid in self.visible_vids:
                        if vid.doit_auto.startswith('['):
                            vid.doit = '[ ]'

            if spins.init_all:
                spins.init_all = False
                if self.state == 'select':
                    for vid in self.visible_vids:
                        vid.doit = vid.doit_auto

            if spins.toggle:
                spins.toggle = False
                if self.state == 'select':
                    idx = win.pick_pos
                    if 0 <= idx < len(self.visible_vids):
                        toggle_doit(self.visible_vids[idx])
                        win.pick_pos += 1

            if spins.go:
                spins.go = False
                if self.state == 'select':
                    self.state = 'convert'
                        # self.win.set_pick_mode(False, 1)

            if spins.quit:
                spins.quit = False
                if self.state == 'select':
                    sys.exit(0)
                elif self.state == 'convert':
                    if self.job:
                        self.job.ffsubproc.stop()
                        self.job.vid.doit = '[X]'
                        self.job = None
                    # Disable auto mode when user interrupts
                    if self.auto_mode_enabled:
                        self.auto_mode_enabled = False
                        self.options_suffix = self.build_options_suffix()
                    self.state = 'select'
                    self.vids.sort(key=lambda vid: vid.bloat, reverse=True)
                    win.set_pick_mode(True, 1)

        def advance_jobs():
            nonlocal self

            if self.job and self.state in ('convert', 'help'):
                while True:
                    if self.opts.dry_run:
                        delta = time.monotonic() - self.job.start_mono
                        got = 0 if delta >= 3 else f'{delta=}'
                    else:
                        got = self.get_job_progress(self.job)
                    if isinstance(got, str):
                        self.job.progress = got
                    elif isinstance(got, int):
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
                        else:
                            title = 'RE-ENCODE-TO-H265'

                        lg.put('OK' if got == 0 else 'ERR',
                            title + ' ', json.dumps(dumped, indent=4))
                        self.job = None
                        break # finished job
                    else:
                        break # no progress on job
            if self.state == 'convert' and not self.job:
                gonners = []
                for vid in self.visible_vids:
                    if not vid:
                        continue
                    if vid.doit == '[X]':
                        if not os.path.isfile(vid.filepath):
                            gonners.append(vid)
                            continue
                        if not self.job: # start only one job
                            self.prev_time_encoded_secs = -1
                            self.job = self.start_transcode_job(vid)
                            vid.doit = 'IP '
                if gonners:  # any disappearing files?
                    vids = []
                    for vid in self.vids:
                        if vid not in gonners:
                            vids.append(vid)
                    self.vids = vids # pruned list
                    # Convert Vid dataclass objects to dicts for JSON serialization
                    # asdict() automatically handles nested Probe dataclasses
                    gonners_data = [asdict(v) for v in gonners]
                    lg.err('videos disappeared before conversion:\n'
                        + json.dumps(gonners_data, indent=4))

                if not self.job:
                    # Check auto mode exit conditions
                    if self.auto_mode_enabled:
                        # Calculate current stats for vitals report
                        _, stats = make_lines()

                        # Check exit conditions
                        time_exceeded = False
                        if self.auto_mode_start_time and self.auto_mode_hrs_limit:
                            runtime_hrs = (time.monotonic() - self.auto_mode_start_time) / 3600
                            time_exceeded = runtime_hrs >= self.auto_mode_hrs_limit

                        no_more_todo = (stats.total - stats.done) == 0
                        too_many_failures = self.consecutive_failures >= 10

                        if time_exceeded or no_more_todo or too_many_failures:
                            self.print_auto_mode_vitals(stats)
                            # print_auto_mode_vitals exits, so we never reach here

                    self.state = 'select'
                    self.vids.sort(key=lambda vid: (vid.all_ok, vid.bloat), reverse=True)
                    win.set_pick_mode(True, 1)

        def toggle_doit(vid):
            if vid.doit == '[X]':
                vid.doit = vid.doit_auto if vid.doit_auto != '[X]' else '[ ]'
            elif not vid.doit.startswith('?') and not self.dont_doit(vid):
                vid.doit = '[X]'

        spin = OptionSpinner()
        spin.add_key('help_mode', '? - help screen', vals=[False, True])
        # spin.add_key('set_all', 's - set all to "[X]"', category='action')
        spin.add_key('reset_all', 'r - reset all to "[ ]"', category='action')
        spin.add_key('init_all', 'i - set all automatic state', category='action')
        spin.add_key('toggle', 'SP - toggle current line state', category='action',
                     keys={ord(' '), })
        spin.add_key('go', 'g - begin conversions', category='action')
        spin.add_key('quit', 'q - quit converting OR exit app', category='action',
                     keys={ord('q'), 0x3})
        spin.add_key('freeze', 'p - pause/release screen', vals=[False, True])
        spin.add_key('search', '/ - search string',
                          prompt='Set search string, then Enter')
        spin.add_key('mangle', 'm - mangle titles', vals=[False, True])

        self.spins = spins = spin.default_obj

        self.win = win = ConsoleWindow(keys=spin.keys,
                        body_rows=10+len(self.vids), ctrl_c_terminates=False)
        curses.intrflush(False)
        self.state = 'select'

        win.set_pick_mode(True, 1)
        not_help_state = self.state

        while True:

            if not spins.freeze:
                render_screen()

            key = win.prompt(seconds=3.0) # Wait for half a second or a keypress

            if key in spin.keys:
                spin.do_key(key, win)

            handle_keyboard()

            # Auto-transition to convert state if auto mode enabled
            if self.auto_mode_enabled and self.state == 'select':
                self.state = 'convert'


            advance_jobs()

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
