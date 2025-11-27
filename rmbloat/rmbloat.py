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
    ffmpeg -i video.mkv -i video.en.srt \
    -map 0 -map 1:s:0 \
    -c copy \  # Copy all streams, no re-encoding
    -c:s srt \  # Only subtitle needs codec
    -metadata:s:s:0 language=eng \
    video.sb.mkv
"""

# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return,too-many-branches
# pylint: disable=too-many-return-statements,too-many-instance-attributes
# pylint: disable=consider-using-with,line-too-long,too-many-lines
# pylint: disable=too-many-nested-blocks,try-except-raise,line-too-long

import sys
import os
import math
import argparse
import subprocess
import traceback
import atexit
import shlex
import re
import time
import fcntl
import json
import random
import curses
from pathlib import Path
from typing import Optional, Union
# from copy import copy
from types import SimpleNamespace
from datetime import timedelta
import send2trash
from console_window import ConsoleWindow, OptionSpinner
from .ProbeCache import ProbeCache
from .VideoParser import VideoParser, Mangler
from .IniManager import IniManager
from .RotatingLogger import RotatingLogger
from .CpuStatus import CpuStatus
from .FfmpegChooser import FfmpegChooser
# from .SetupCgroup import set_cgroup_cpu_limit

lg = RotatingLogger('rmbloat')

def store_cache_on_exit():
    """ TBD """
    if Converter.singleton:
        if Converter.singleton.win:
            Converter.singleton.win.stop_curses()
        if Converter.singleton.probe_cache:
            Converter.singleton.probe_cache.store()



###
### import subprocess
### import re
###
### # --- Configuration for Aggressive Compression (Low Bitrate) ---
###
### # CRF 28 is the 'default' for libx265. We go higher for lower quality/smaller file.
### # Higher value = lower quality = smaller file.
### CRF_VALUE_AGGRESSIVE = 30
### # QSV's equivalent is -global_quality. 25 is a common 'good' starting point.
### # We go higher for lower quality/smaller file, aiming for ~1200kbps.
### QSV_QUALITY_AGGRESSIVE = 32
###
### # -----------------------------------------------------------
###
### def detect_qsv_support():
###     """Checks if the system's FFmpeg build supports hevc_qsv (Intel QSV HEVC encoding)."""
###     try:
###         # Command to list available encoders (We specifically look for 'hevc_qsv')
###         command = ['ffmpeg', '-hide_banner', '-encoders']
###
###         # Execute and check for success
###         result = subprocess.run(
###             command,
###             capture_output=True,
###             text=True,
###             check=True, # Raises CalledProcessError for non-zero exit status
###             timeout=5
###         )
###
###         # Search the output for the QSV HEVC encoder name
###         if re.search(r'\bhevc_qsv\b', result.stdout):
###             # This confirms the FFmpeg binary has QSV support compiled in.
###             return True
###         else:
###             return False
###
###     except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
###         # Handles errors like 'ffmpeg' not found, command failure, or timeout.
###         print(f"Warning: Could not execute FFmpeg to detect encoders. Assuming no QSV. Error: {e}")
###         return False
###
### # --- How to use it in your main application logic ---
###
### # Initialize all your conversion flags
### VIDEO_CODEC = ''
### HWACCEL_FLAG = []
### PIPLINE_ARG = []
### QUALITY_ARG = []
### PRESET_ARG = ['-preset', 'medium'] # Use a medium preset for a good balance
###
### if detect_qsv_support():
###     print(f"✅ Intel QSV (hevc_qsv) support detected. Using hardware acceleration.")
###
###     # 1. Hardware Encoder
###     VIDEO_CODEC = 'hevc_qsv'
###     VIDEO_CODEC = 'hevc_vaapi'
###
###     # 2. Hardware Acceleration Flag (may vary by system setup)
###     # HWACCEL_FLAG = '-hwaccel qsv'.split()
###     HWACCEL_FLAG = ['-init_hw_device', 'vaapi=va:/dev/dri/renderD128',
###                         '-filter_hw_device', 'va']
###     HWACCEL_FLAG = [ '-hwaccel', 'vaapi', # Enable VAAPI decoding
###                     '-hwaccel_device', 'va:/dev/dri/renderD128', ]
###     PIPELINE_ARG = [ '-vf', 'deinterlace_vaapi,hwmap=derive_device=va',]
###
###
###
###
###     # 1. Video Filter: Map and format the data for QSV, removing the problematic upload step
###     #    (The 'hwmap' step might be confusing the internal scaler, let's simplify to a pure format filter)
###     #    We are changing the filter entirely to focus on format conversion within the hardware context.
###
###     # 2. Output Pixel Format (CRITICAL)
###
###     # 3. Quality Control (ICQ is QSV's CRF equivalent)
###     # Target value {QSV_QUALITY_AGGRESSIVE} for aggressive (low bitrate) compression.
###     # QUALITY_ARG = ['-global_quality', str(QSV_QUALITY_AGGRESSI# 5. THE FIX: Use the native VAAPI HEVC encoder
###     QUALITY_ARG = ['-qp', '22',] # Quality parameter (Good balance for HEVC)VE)]
###
###     # Note: QSV presets are often simple numbers (1-7) for speed/quality trade-offs.
###     # The 'medium' preset may not be an exact QSV equivalent, but it's a good default.
###
### else:
###     print(f"❌ No Intel QSV support detected. Falling back to libx265 software encoding.")
###
###     # 1. Software Encoder
###     VIDEO_CODEC = 'libx265'
###
###     # 2. No HW acceleration needed here
###     HWACCEL_FLAG = []
###
###     # 3. Quality Control (CRF)
###     # Target value {CRF_VALUE_AGGRESSIVE} for aggressive (low bitrate) compression.
###     QUALITY_ARG = ['-crf', str(CRF_VALUE_AGGRESSIVE)]

class FfmpegMon:
    """
    Monitors an FFmpeg subprocess non-blockingly.

    Provides a clean .start() and .poll() interface for use in a
    single-threaded interactive loop (like a curses application).
    """

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.partial_line: bytes = b""
        self.output_queue: list[str] = []  # <--- NEW: Queue for complete lines
        self.return_code: Optional[int] = None
        self.temp_file = None

    def start(self, command_line: list[str]) -> None:
        """
        Starts the FFmpeg subprocess.

        Args:
            command_line: The full FFmpeg command as a list of strings.
        """
        if self.process:
            raise RuntimeError("FfmpegMon is already monitoring a process.")

        try:
            # Start the process, piping stderr for progress updates
            self.process = subprocess.Popen(
                command_line,
                stdout=subprocess.DEVNULL,  # Discard normal output
                stderr=subprocess.PIPE,     # Capture progress messages
                text=False,                  # Read output as text
                bufsize=0
            )

            # --- CRITICAL: Make stderr non-blocking ---
            # Get the file descriptor number for the stderr pipe
            fd = self.process.stderr.fileno()
            # Get the current flags
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            # Set the O_NONBLOCK flag
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        except Exception as e:
            # Handle common errors like 'ffmpeg' not found
            print(f"Error starting FFmpeg process: {e}")
            self.return_code = 127

    def poll(self) -> Union[Optional[int], str]:
        """
        Reads and processes data. Returns the next item from the internal queue
        (string output) or the final return code (integer).
        """
        # --- Stage 0: Process Queue First & Status Check (remains unchanged) ---
        if self.output_queue:
            return self.output_queue.pop(0)

        if not self.process:
            return self.return_code

        process_status = self.process.poll()

        # 1. Read available data non-blockingly
        try:
            chunk = self.process.stderr.read()
        except (IOError, OSError):
            chunk = b""

        # 2. Process NEW DATA
        if chunk:
            # Append the new chunk to the existing buffer
            # We must use bytes for the buffer: self.partial_line + chunk
            data = self.partial_line + chunk

            # --- CRITICAL FIX: Split by EITHER \n OR \r ---
            # We need to split by both, keeping the delimiters for context is not needed,
            # but handling trailing fragments is important.

            # 2a. Split data by \n or \r. Note: re.split discards delimiters.
            # Use 'b' prefix for regex pattern since data is bytes
            fragments = re.split(b'[\r\n]', data)

            # 2b. The last element is the new partial line (may be an empty fragment)
            self.partial_line = fragments[-1]

            # 2c. The rest are complete lines/progress updates
            # We ignore empty strings/fragments that occur from successive delimiters (like \r\n or \n\n).
            for line_bytes in fragments[:-1]:
                if line_bytes: # Ignore empty fragments
                    line_str = line_bytes.decode('utf-8', errors='ignore')
                    self.output_queue.append(line_str)

        # --- Stage 3 & 4: Handle Termination and Final Check (remains largely unchanged) ---
        if process_status is not None:
            # The process is done. Process any remaining data in partial_line.
            if self.partial_line:
                # Decode and add the final output/error line.
                final_output = self.partial_line.decode('utf-8', errors='ignore')
                self.partial_line = b"" # Buffer consumed
                self.output_queue.append(final_output) # Add the final line to the queue

            # If the queue now has items, return the first one.
            if self.output_queue:
                self.return_code = process_status # Store code for *after* the queue is empty
                return self.output_queue.pop(0)

            # If the queue is empty, we return the final code.
            self.return_code = process_status
            self.process = None
            return self.return_code

        # --- Stage 4: Final Check ---
        if self.output_queue:
            return self.output_queue.pop(0)

        return None

    def _read_remaining(self):
        """
        Helper to read any final buffered output after termination.
        """
        # Read all remaining output
        try:
            remaining_data = self.process.stderr.read()
            self.partial_line += remaining_data
        except (IOError, OSError):
            pass # Ignore if stream is already closed or empty

        # Check if the final output contains a full line we missed
        if self.partial_line:
            # This logic is a bit simple, but assumes the final chunk is mostly an error message.
            # You may want to store this in a separate error buffer for later dump.
            # For now, we'll just discard it if it's not a complete line.
            pass

    def stop(self, return_code=255):
        """
        Terminates the subprocess if it is still running.
        """
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15) # Wait for it to die gracefully
            except Exception:
                pass  # hope for the best
        if self.temp_file and os.path.exists(self.temp_file):
            os.unlink(self.temp_file)
        self.temp_file = None
        self.process = None
        self.partial_line = ""
        self.return_code = return_code

    def __del__(self):
        """Ensure the subprocess is terminated when the object is destroyed."""
        self.stop()

class Job: # class FfmpegJob:
    """ TBD """
    def __init__(self, vid, orig_backup_file, temp_file, duration_secs):
        converter = Converter.singleton
        self.vid = vid
        self.start_mono = time.monotonic()
        self.progress='DRY-RUN' if converter.opts.dry_run else 'Started'
        self.input_file = os.path.basename(vid.filepath)
        self.orig_backup_file=orig_backup_file
        self.temp_file=temp_file
        self.duration_secs=duration_secs
        self.total_duration_formatted=self.trim0(
                        str(timedelta(seconds=int(duration_secs))))
        self.ffsubproc=FfmpegMon()
        self.return_code = None

    @staticmethod
    def trim0(string):
        """ TBD """
        if string.startswith('0:'):
            return string[2:]
        return string

    @staticmethod
    def duration_spec(secs):
        """ TBD """
        secs = int(round(secs))
        hours = math.floor(secs / 3600)
        minutes = math.floor((secs % 3600) / 60)
        secs = secs % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

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

    # A common list of video extensions ffmpeg can typically handle.
    # NOTE: The check is *case-insensitive* by converting to lowercase.
    VIDEO_EXTENSIONS = {
        '.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv',
        '.wmv', '.mpg', '.mpeg', '.3gp', '.m4v', '.ts',
        '.ogg', '.ogv'
        # You may need to add or remove extensions based on your specific requirements
    }
    # Prefixes to skip (case-sensitive as requested)
    SKIP_PREFIXES = ('TEMP.', 'ORIG.')
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
        self.probe_cache = ProbeCache(cache_dir_name=cache_dir)
        self.chooser = FfmpegChooser(force_pull=False, prefer_strategy='auto', quiet=False)
        self.probe_cache.load()
        self.probe_cache.store()
        self.progress_line_mono = 0
        self.start_job_mono = 0
        self.cpu = CpuStatus()
        # self.cgroup_prefix = set_cgroup_cpu_limit(opts.thread_cnt*100)
        atexit.register(store_cache_on_exit)

    def is_allowed_codec(self, probe):
        """ Return whether the codec is 'allowed' """
        if not probe:
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

        vid.summary = (f'  {vid.width}x{vid.height}' +
                        f' {vid.codec} {vid.bloat}b {vid.gb}G')
        return probe

    def already_converted(self, basic_ns, video_file):
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

        vid = SimpleNamespace(
                    filepath=video_file,
                    filebase=os.path.basename(video_file),
                    doit='', net=' ---', width=None, height=None,
                    command=None, res_ok=None,
                    duration=None, codec=None, bitrate=None, bloat=None, bloat_ok=None,
                    codec_ok=None, gb=None, all_ok=None,
                    standard_name=basic_ns.standard_name,
                    do_rename=basic_ns.do_rename,
                    probe0=None, probe1=None, basename1=None,
                    return_code=None, texts=[], ops=[])
        vid.probe0 = self.apply_probe(vid, basic_ns.probe)
        self.vids.append(vid)

        vid.doit = '[ ]' if vid.all_ok or self.dont_doit(vid) else '[X]'

    @staticmethod
    def bash_quote(args):
        """
        Converts a Python list of arguments into a single, properly quoted
        Bash command string.
        """
        quoted_args = []
        for arg in args:
            # 1. Check if simple quoting is enough (no need to handle embedded quotes)
            # shlex.quote is the preferred, robust way in Python 3.3+
            quoted_arg = shlex.quote(arg)
            quoted_args.append(quoted_arg)

        return ' '.join(quoted_args)


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
            # Not merging subtitles, copy internal ones
            map_copy += ' 0:s? -c:s copy -map -0:t -map -0:d'

        params.map_opts = map_copy.split()
        params.external_subtitle = merged_external_subtitle

        # Generate the command
        ffmpeg_cmd = self.chooser.make_ffmpeg_cmd(params)

        # Store command for logging
        vid.command = self.bash_quote(ffmpeg_cmd)

        # Start the job
        if not self.opts.dry_run:
            job.ffsubproc.start(ffmpeg_cmd)
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

    @staticmethod
    def human_readable_size(size_bytes: int) -> str:
        """
        Converts a raw size in bytes to a human-readable string (e.g., 10 KB, 5.5 MB).
        Returns:
            A string representing the size in human-readable format.
        """
        if size_bytes is None:
            return "0 Bytes"

        if size_bytes == 0:
            return "0 Bytes"

        # Define the unit list (using 1024 for base-2, which is standard for file sizes)
        size_names = ("Bytes", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")

        # Use a loop to find the appropriate unit index (i)
        i = 0
        size = size_bytes
        while size >= 1024 and i < len(size_names) - 1:
            size /= 1024
            i += 1

        # Format the number, keeping two decimal places if it's not the 'Bytes' unit
        if i == 0:
            return f"{size_bytes} {size_names[i]}"
        else:
            return f"{size:.2f} {size_names[i]}"

    @staticmethod
    def is_valid_video_file(filename):
        """
        Checks if a file meets all the criteria:
        1. Does not start with 'TEMP.' or 'ORIG.'.
        2. Has a common video file extension (case-insensitive).
        """

        # 1. Check for prefixes to skip
        if filename.startswith(Converter.SKIP_PREFIXES):
            # print(f"Skipping '{filename}': Starts with a forbidden prefix.")
            return False

        # Get the file extension and convert to lowercase for case-insensitive check
        # os.path.splitext returns a tuple: (root, ext)
        _, ext = os.path.splitext(filename)

        # 2. Check if the extension is a recognized video format
        if ext.lower() not in Converter.VIDEO_EXTENSIONS:
            # print(f"Skipping '{filename}': Not a recognized video file extension ('{ext.lower()}').")
            return False

        # The file meets all criteria
        return True

    def standard_name(self, pathname: str, height: int) -> str:
        """
        If "parsed" create a simple standard name from the titile
        and episode number (if episode) OR title and year (if movie).
        Otherwise ...
        Replaces common H.264/AVC/Xvid/DivX codec strings in a filename
        with 'x265' or 'X265', preserving the original case where possible.
        Also change the height indicator if not in agreement with actual.
        Returns: Whether changed and filename string with x265 codec.
        """

        basename = os.path.basename(pathname)
        parsed = VideoParser(pathname)
        if parsed.is_movie_year() or parsed.is_tv_episode():
            if parsed.is_tv_episode():
                name = parsed.title
                if parsed.year:
                    name += f' {parsed.year}'
                name += f' s{parsed.season:02d}e{parsed.episode:02d}'
                name += f'-{parsed.episode_hi:02d}' if parsed.episode_hi else ''
            else:
                name = f'{parsed.title} {parsed.year}'

            name +=  f' {height}p x265-cmf{self.opts.quality} recode'
            name = re.sub(r'[\s\.\-]+', '.', name) + '.mkv'

            return bool(name != basename), name

        new_basename = basename
        # Regular expressions for the codecs to be replaced.
        # The groups will capture the exact string for case-checking later.
        pattern = r'\b([xh]\.?264|avc|xvid|divx)\b'
        regex = re.compile(pattern, re.IGNORECASE)
        end = 0
        while True:
            match = re.search(regex, new_basename[end:])
            if not match:
                break
            sub = 'X265' if match.group(1).isupper() else 'x265'
            start, end = match.span(1)
            new_basename = new_basename[:start] + sub + new_basename[end:]

        pattern = r'\b(\d+[pi]|UHD|4K|2160p|1440p|2K|8K)\b'
        regex = re.compile(pattern, re.IGNORECASE)
        height_str = f'{height}p' # e.g., '1080p'

        end = 0
        while True:
            match = re.search(regex, new_basename[end:])
            if not match:
                break
            matched_group = match.group(1) # The matched string (e.g., '4K' or '720i')
            start, end = match.span(1)

            if matched_group.lower().endswith(('k', 'hd')):
                # For '4K', 'UHD', etc. you can't rely on 'height_str' being correct,
                # so you must manually format the replacement based on the original's case.
                is_upper = matched_group.isupper() # Check if '4K' was '4K' or '4k'
                # The canonical replacement should be f'{height}p'
                sub = height_str.upper() if is_upper else height_str
            else:
                # Standard p/i resolution match (e.g., '720p', '1080i')
                sub = height_str.upper() if matched_group.isupper() else height_str

            new_basename = new_basename[:start] + sub + new_basename[end:]


        # nail down extension
        different = bool(new_basename != basename)
        base, _ = os.path.splitext(new_basename)
        new_basename = base + '.mkv'


        return different, new_basename

    def bulk_rename(self, old_file_name: str, new_file_name: str,
                    trashes: set):
        """
        Renames files and directories in the current working directory (CWD).

        It finds all items whose non-extension part matches the non-extension part
        of `old_file_name`, and renames them using the non-extension part of
        `new_file_name`, preserving the original file extensions.

        Args:
            old_file_name: A sample filename (e.g., 'oldie.mp4') used to define
                           the base name to look for ('oldie').
            new_file_name: A sample filename (e.g., 'newbie.mkv') used to define
                           the base name to rename to ('newbie').
        """
        ops = []
        dry_run = self.opts.dry_run
        would = 'WOULD ' if dry_run else ''

        old_base_name, _ = os.path.splitext(old_file_name)
        new_base_name, _ = os.path.splitext(new_file_name)

        # Define the special suffix to look for (case-insensitive search)
        special_ext = ".REFERENCE.srt"
        # 2. Use os.walk for recursive traversal starting from the current directory ('.')
        for root, dirs, files in os.walk('.', topdown=False):

            # Combine files and directories for unified processing.
            items_to_check = files + dirs

            for item_name in items_to_check:
                # Skip if the item is a special directory reference
                if item_name in ('.', '..'):
                    continue

                full_old_path = os.path.join(root, item_name)
                current_base, extension = os.path.splitext(item_name)
                current_base2, extension2 = os.path.splitext(current_base)
                extension2 = extension2 + extension

                new_item_name = None

                # --- Rule 1: Special Case - Full Name Match (item_name == old_base_name) ---
                if item_name == old_base_name:
                    new_item_name = new_base_name

                # --- Rule 2: Special Case - Reference SRT Suffix Match ---
                # Requires the item to end with ".reference.srt" AND the base part to match old_base_name
                elif (item_name.lower().endswith(special_ext.lower())
                      and item_name[:-len(special_ext)] == old_base_name):
                    new_item_name = new_base_name + special_ext

                elif current_base2 == old_base_name:
                    new_item_name = new_base_name + extension2

                # --- Rule 3: General Case - Base Name Match ---
                # Applies if the non-extension part matches the intended old base name,
                # and was not caught by the specific rules above.
                elif current_base == old_base_name:
                    # General Case: New name is new_base_name + original extension
                    new_item_name = new_base_name + extension

                # 4. If no matching rule was triggered, skip this one
                if not new_item_name:
                    continue

                # 5. Perform the rename operation
                full_new_path = os.path.join(root, new_item_name)
                try:
                    if os.path.basename(item_name) not in trashes:
                        if not dry_run:
                            os.rename(full_old_path, full_new_path)
                        ops.append(f"{would}rename {full_old_path!r} {full_new_path!r}")
                except Exception as e:
                    # Handle potential errors (e.g., permission errors, file in use)
                    ops.append(f"ERR: rename '{full_old_path}' '{full_new_path}': {e}")
        return ops

    def process_one_file(self, vid):
        """ Handle just one """
        input_file = vid.video_file
        if not self.is_valid_video_file(input_file):
            return  # Skip to the next file in the loop

        # --- File names for the safe replacement process ---
        do_rename, standard_name = self.standard_name(input_file, vid.probe.height)

        vid.do_rename = do_rename
        vid.standard_name = standard_name

        self.already_converted(vid, input_file)

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
        if success:
            if not dry_run:
                probe = self.probe_cache.get(job.temp_file)
            if not probe:
                if dry_run:
                    success = True
                    vid.doit = 'ok'
                    net = -20
                else:
                    success = False
                    vid.doit = 'ERR'
                    net = 0
            else:
                net = (vid.gb - probe.gb) / vid.gb
                net = int(round(-net*100))
            if self.is_allowed_codec(probe) and net > -self.opts.min_shrink_pct:
                self.probe_cache.set_anomaly(vid.filepath, 'OPT')
                success = False
            vid.net = f'{net}%'

        if success and not self.opts.sample:
            would = 'WOULD ' if dry_run else ''
            trashes = set()
            basename = os.path.basename(vid.filepath)
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

    @staticmethod
    def get_candidate_video_files(file_args):
        """
        Gather candidate video file paths from command-line arguments.

        Args:
            file_args: List of file/directory paths or "-" for stdin

        Returns:
            tuple: (paths_to_probe, read_pipe)
                - paths_to_probe: List of absolute file paths to probe
                - read_pipe: True if stdin was read (caller needs to restore TTY)
        """
        read_pipe = False
        enqueued_paths = set()
        paths_from_args = []

        # 1. Gather all unique, absolute paths from arguments and stdin
        for file_arg in file_args:
            if file_arg == "-":
                # Handle STDIN
                if not read_pipe:
                    paths_from_args.extend(sys.stdin.read().splitlines())
                    read_pipe = True
            else:
                # Convert to absolute path immediately
                abs_path = os.path.abspath(file_arg)
                if abs_path not in enqueued_paths:
                    paths_from_args.append(abs_path)
                    enqueued_paths.add(abs_path)

        # 2. Separate into directories and individual files, and sort for processing order
        directories = []
        immediate_files = []

        for path in paths_from_args:
            # Ignore empty lines from stdin
            if not path:
                continue

            if os.path.isdir(path):
                directories.append(path)
            else:
                immediate_files.append(path)

        # Sort the list of directories to be processed (case-insensitively)
        directories.sort(key=str.lower)

        # Sort the list of individual files (case-insensitively)
        immediate_files.sort(key=str.lower)

        # List to hold all file paths in the final desired, grouped, and sorted order
        paths_to_probe = []

        # 3. Process Directories: Find and group files recursively
        for dir_path in directories:
            # This list will hold all valid video files found in the current directory group
            group_files = []

            # Recursively walk the directory structure
            for root, dirs, files in os.walk(dir_path):

                # Sort the directory names before os.walk processes them (case-insensitive)
                # This ensures predictable traversal order of subdirectories
                dirs.sort(key=str.lower)

                # Sort the files within the current directory (case-insensitive)
                files.sort(key=str.lower)

                for file_name in files:
                    full_path = os.path.join(root, file_name)

                    # Check for validity and duplicates
                    if Converter.is_valid_video_file(full_path):
                        if full_path not in enqueued_paths:
                            group_files.append(full_path)
                            enqueued_paths.add(full_path)

            # Append all grouped and sorted file paths for the current directory
            paths_to_probe.extend(group_files)

        # 4. Process Individual Files: Append sorted immediate files
        paths_to_probe.extend(immediate_files)

        return paths_to_probe, read_pipe

    def create_video_file_list(self):
        """ TBD """
        video_files_out = []

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
            ns = SimpleNamespace(video_file=file_path, probe=probe)
            video_files_out.append(ns)

#       for video_file_path in paths_to_probe:
#           probe_count += 1

#           # probe = self.get_video_metadata(video_file_path)
#           probe = self.probe_cache.get(video_file_path)

#           if probe:
#               ns = SimpleNamespace(video_file=video_file_path, probe=probe)
#               video_files_out.append(ns)

#           # Update the progress indicator every N probes or on the last file
#           if probe_count % update_interval == 0 or probe_count == total_files:
#               percent = int((probe_count / total_files) * 100)

#               # \r (carriage return) moves the cursor to the start of the line for overwrite
#               sys.stderr.write(f"probing: {percent}% {probe_count} of {total_files}\r")
#               sys.stderr.flush()

        # Print a final newline character to clean the console after completion
#       if total_files > 0:
#           sys.stderr.write("\n")
#           sys.stderr.flush()

        return video_files_out

    def dont_doit(self, vid):
        """ Returns true if prohibited from re-encoding """
        base = os.path.basename(vid.filepath).lower()
        if (base.startswith('sample.')
                or base.startswith('test.')
                or base.endswith('.recode.mkv')
                or vid.doit not in ('[ ]', '[X]', '', None)
                ):
            return True
        return False

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
                    head = '[s]etAll [r]setAll [i]nit SP:toggle [g]o ?=help [q]uit'
                    if self.opts.dry_run:
                        head += ' DRY-RUN'
                    if self.opts.sample:
                        head += ' SAMPLE'
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

                win.add_header(f'CVT {"NET":>4} {"BLOAT":>5}  {"RES":>5}  {"CODEC":>5}  {"MINS":>4} {"GB":>6}   VIDEO')
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

            if spins.set_all:
                spins.set_all = False
                if self.state == 'select':
                    for vid in self.visible_vids:
                        if not self.dont_doit(vid):
                            vid.doit = '[X]'

            if spins.reset_all:
                spins.reset_all = False
                if self.state == 'select':
                    for vid in self.visible_vids:
                        if vid.doit in ('[ ]', '[X]'):
                            vid.doit = '[ ]'

            if spins.init_all:
                spins.init_all = False
                if self.state == 'select':
                    for vid in self.visible_vids:
                        if vid.doit in ('[ ]', '[X]'):
                            if self.dont_doit(vid) or vid.all_ok:
                                vid.doit = '[ ]'
                            else:
                                vid.doit = '[X]'

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
                        self.finish_transcode_job(
                            success=bool(got == 0), job=self.job)
                        dumped = dict(vars(self.job.vid))
                        if self.job.vid.probe0:
                            dumped['probe0'] = vars(dumped['probe0'])
                        if self.job.vid.probe1:
                            dumped['probe1'] = vars(dumped['probe1'])
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
                gonners = set()
                for vid in self.visible_vids:
                    if not vid:
                        continue
                    if vid.doit == '[X]':
                        if not os.path.isfile(vid.filepath):
                            gonners.add(vid)
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
                    lg.err('videos disappeared before conversion:\n'
                        + json.dumps(list(gonners), indent=4))

                if not self.job:
                    self.state = 'select'
                    self.vids.sort(key=lambda vid: (vid.all_ok, vid.bloat), reverse=True)
                    win.set_pick_mode(True, 1)

        def toggle_doit(vid):
            if self.dont_doit(vid):
                vid.doit = '[ ]'
            else:
                vid.doit = '[X]' if vid.doit == '[ ]' else '[ ]'

        spin = OptionSpinner()
        spin.add_key('help_mode', '? - help screen', vals=[False, True])
        spin.add_key('set_all', 's - set all to "[X]"', category='action')
        spin.add_key('reset_all', 'r - reset all to "[ ]"', category='action')
        spin.add_key('init_all', 'i,SP - set all initial state', category='action')
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

            advance_jobs()

            if not spins.freeze:
                win.clear()

    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        video_files = self.create_video_file_list()
        self.probe_cache.store()
        video_files.sort(key=lambda vid: vid.probe.bloat, reverse=True)

        if not video_files:
            print("Usage: rmbloat {options} {video_file}...")
            sys.exit(1)

        # --- The main loop change is here ---
        for vid in video_files:
            input_file_path_str = vid.video_file
            file_dir, _ = os.path.split(input_file_path_str)
            if not file_dir:
                file_dir = os.path.abspath(os.path.dirname(input_file_path_str))

            # Use a try...finally block to ensure you always change back.
            try:
                os.chdir(file_dir)
                self.process_one_file(vid)

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
                               full_speed=False,
                               keep_backup=False,
                               merge_subtitles=True,
                               min_shrink_pct=10,
                               thread_cnt=4,
                               quality=28,
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
                        + f' dflt={vals.full_speed}]')
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
        parser.add_argument('-q', '--quality',
                    default=vals.quality, type=int,
                    help=f'output quality (CRF) [dflt={vals.quality}]')
        parser.add_argument('-t', '--thread-cnt',
                    default=vals.thread_cnt, type=int,
                    help='thread count for ffmpeg conversions'
                        + f' [dflt={vals.thread_cnt}]')

        # run-time options
        parser.add_argument('-S', '--save-defaults', action='store_true',
                    help='save the -B/-b/-q/-a/-F/-m/-M options as defaults')
        parser.add_argument('-n', '--dry-run', action='store_true',
                    help='Perform a trial run with no changes made.')
        parser.add_argument('-s', '--sample', action='store_true',
                    help='produce 30s samples called SAMPLE.{input-file}')
        parser.add_argument('-L', '--logs', action='store_true',
                    help='view the logs')
        parser.add_argument('-T', '--chooser-tests', action='store_true',
                    help='run tests on ffmpeg choices w 30s cvt of 1st given video')

        parser.add_argument('files', nargs='*',
            help='Video files and recursively scanned folders w Video files')
        opts = parser.parse_args(args)
            # Fake as option ... if this needs tuning (which I doubt)
            # then make it an actual option.  It is the max time allowed
            # between progress updates when converting a video
        opts.progress_secs_max = 30

        if opts.save_defaults:
            print('Setting new defaults:')
            for key in vars(vals):
                new_value = getattr(opts, key)
                setattr(vals, key, new_value)
                print(f'- {key} {new_value}')
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
                show_test_encode=(video_file is None)  # Show example commands if no video
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
