#!/usr/bin/env python3
# pylint: disable=too-many-statements
"""
TBD
TODO:
- allow /search in select mode
- disallow /search in convert mode
- hide unselected in convert mode
x ensure the 10% better is enforced and the NET is computed
- expose/spin samples as option -- make samples-dir and option
- make cmf (or quality) a spinner / expose it
- expose bloat thresh (change by 100? or prompt for it)
"""
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
from textwrap import indent
from typing import Optional, Union
from copy import copy
from types import SimpleNamespace
from datetime import timedelta
import send2trash
from console_window import ConsoleWindow, OptionSpinner
from ProbeCache import ProbeCache
from VideoParser import VideoParser, Mangler
from IniManager import IniManager
from RotatingLogger import RotatingLogger

lg = RotatingLogger('rmbloat')

# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return,too-many-branches
# pylint: disable=too-many-return-statements,too-many-instance-attributes
# pylint: disable=consider-using-with

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
        # --- Stage 0: Process Queue First ---
        if self.output_queue:
            return self.output_queue.pop(0)

        if not self.process:
            return self.return_code

        # Check for termination status, but don't act on it yet.
        process_status = self.process.poll()

        # 1. Read available data non-blockingly
        try:
            chunk = self.process.stderr.read()
        except (IOError, OSError):
            chunk = b""

        # 2. Process NEW DATA
        if chunk:
            # Append the new chunk to the existing buffer
            data = self.partial_line + chunk

            # Split by the byte newline character
            lines = data.split(b'\n')

            # The last element is the new partial line; the rest are complete lines
            self.partial_line = lines[-1]

            # Put all complete lines onto the output queue
            for line_bytes in lines[:-1]:
                line_str = line_bytes.decode('utf-8', errors='ignore').lstrip('\r')
                self.output_queue.append(line_str)

            # --- PROGRESS LINE LOGIC (if no newline was found) ---
            # If we received new data but didn't find any newlines, it's likely a progress update.
            # We treat the accumulated partial_line as the progress line.
            if not self.output_queue and self.partial_line:
                # Return the progress line and clear the partial_line buffer.
                output_to_caller = self.partial_line.decode('utf-8', errors='ignore').lstrip('\r')
                self.partial_line = b"" # Assume the caller consumes this progress line
                return output_to_caller

        # --- Stage 3: Handle Termination (Last resort) ---
        if process_status is not None:
            # The process is done. Process any remaining data in partial_line.
            if self.partial_line:
                # The remaining partial line is the final output/error.
                final_output = self.partial_line.decode('utf-8', errors='ignore').lstrip('\r')
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
        # If running and the queue is still empty after the read attempt:
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
            self.process.wait(timeout=5) # Wait for it to die gracefully
        os.unlink(self.temp_file) if self.temp_file and os.path.exists(self.temp_file) else None
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

        # Regex to find FFmpeg progress lines (from stderr)
        # Looks for 'frame=  XXXXX' and 'time=00:00:00.00' and 'speed=XX.XXx'
    PROGRESS_RE = re.compile(
        r"frame=\s*(\d+)\s+.*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})\s+.*speed=\s*(\d+\.\d+)x"
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
        self.opts = opts
        self.spins = None # spinner values
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
        self.probe_cache.load()
        self.probe_cache.store()
        self.progress_line_mono = 0
        self.start_job_mono = 0
        atexit.register(store_cache_on_exit)

    def apply_probe(self, vid, probe):
        """ TBD """
        # shorthand
        vid.width = probe.width
        vid.height = probe.height
        vid.codec = probe.codec
        vid.bloat = probe.bloat
        vid.duration = probe.duration
        vid.gb = probe.gb

        vid.codec_ok = bool(self.opts.allowed_codecs == 'all')
        if self.opts.allowed_codecs == 'x265':
            vid.codec_ok = bool(vid.codec in ('hevc',))
        if self.opts.allowed_codecs == 'x26*':
            vid.codec_ok = bool(vid.codec in ('hevc','h264'))

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

    def start_transcode_job(self, vid):
        """ TBD """

        os.chdir(os.path.dirname(vid.filepath))
        basename = os.path.basename(vid.filepath)

        ## print(f'standard_name2: {do_rename=} {standard_name=})')
        prefix = f'/heap/samples/SAMPLE.{self.opts.quality}' if self.opts.sample else 'TEMP'
        temp_file = f"{prefix}.{vid.standard_name}"
        orig_backup_file = f"ORIG.{basename}"

        if os.path.exists(temp_file):
            os.unlink(temp_file)
        duration_secs = vid.probe0.duration
        if self.opts.sample:
            duration_secs = self.sample_seconds

        job = Job(vid, orig_backup_file, temp_file, duration_secs)
        pre_i_opts, post_i_opts = copy(self.ff_pre_i_opts), copy(self.ff_post_i_opts)
        job.input_file = basename

        nice_opts, thread_opts = [], []
        if not self.opts.full_speed:
            nice_opts = 'ionice -c3 nice -n19'.split()
            if self.opts.thread_cnt > 0:
                thread_opts = ['-threads', f'{self.opts.thread_cnt}']

        if self.opts.sample:
            start_secs = max(120, job.duration_secs)*.20
            pre_i_opts += [ '-ss', job.duration_spec(start_secs) ]
            post_i_opts =  ['-t', str(self.sample_seconds)]
            # post_i_opts += [ '-copyts', '-avoid_negative_ts', 'make_zero',
            #   '-t', str(self.sample_seconds)]
            # post_i_opts += [ '-ss', '1:15', '-t', str(self.sample_seconds)]

        # Define the FFmpeg command
        ffmpeg_cmd = [
            * nice_opts,
            'ffmpeg',
            '-y',                           # Overwrite temp file if exists
            # '-v', 'error',                  # suppress INFO/WARNINGS
            * pre_i_opts,
            '-i', job.input_file,
            * post_i_opts,
            '-c:v', 'libx265',
            * thread_opts,
            '-crf', str(self.opts.quality),
            '-preset', 'medium',
            # '-preset', 'fast',
            '-c:a', 'copy',
            '-c:s', 'copy',
            '-map', '0',
            job.temp_file
        ]
        vid.command = self.bash_quote(ffmpeg_cmd)
        if not self.opts.dry_run:
            job.ffsubproc.start(ffmpeg_cmd)
            self.progress_line_mono = time.monotonic() - 15.0
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
        vid = job.vid
        err = False
        while True:
            got = job.ffsubproc.poll()
            delta = time.monotonic() - self.progress_line_mono
            # print(f'\r{delta=} {got=}')
            if time.monotonic() - self.progress_line_mono > 30.0:
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

                self.progress_line_mono = time.monotonic()
                # print(f'\r{self.progress_line_mono=} {line}')

                # 1. Extract values from the regex match
                try:
                    groups = match.groups()

                    # The first two parts (H and M) are integers. The third part (S.ms) is the float.
                    h = int(groups[1])
                    m = int(groups[2])
                    s = int(groups[3])
                    ms = int(groups[4])
                    time_encoded_seconds = h * 3600 + m * 60 + s + ms / 100
                    time_encoded_seconds = round(int(time_encoded_seconds))
                    if self.prev_time_encoded_secs == time_encoded_seconds:
                        continue  # don't return too often
                    speed = float(match.group(6))
                except Exception:
                    print(f"\n{line=} {groups=}")
                    raise

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

    def is_valid_video_file(self, filename):
        """
        Checks if a file meets all the criteria:
        1. Does not start with 'TEMP.' or 'ORIG.'.
        2. Has a common video file extension (case-insensitive).
        """

        # 1. Check for prefixes to skip
        if filename.startswith(self.SKIP_PREFIXES):
            # print(f"Skipping '{filename}': Starts with a forbidden prefix.")
            return False

        # Get the file extension and convert to lowercase for case-insensitive check
        # os.path.splitext returns a tuple: (root, ext)
        _, ext = os.path.splitext(filename)

        # 2. Check if the extension is a recognized video format
        if ext.lower() not in self.VIDEO_EXTENSIONS:
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
                name = parsed.episode_key().replace('"', '')
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
            if net > -10:
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

    def create_video_file_list(self):
        """ TBD """
        video_files_out = []
        enqueued_paths = set()

        # 1. Gather all unique, absolute paths from arguments and stdin
        paths_from_args = []

        for file_arg in self.opts.files:
            if file_arg == "-":
                # Handle STDIN
                paths_from_args.extend(sys.stdin.read().splitlines())
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
                    if self.is_valid_video_file(full_path):
                        if full_path not in enqueued_paths:
                            group_files.append(full_path)
                            enqueued_paths.add(full_path)

            # Append all grouped and sorted file paths for the current directory
            paths_to_probe.extend(group_files)

        # 4. Process Individual Files: Append sorted immediate files
        paths_to_probe.extend(immediate_files)

        # 5. Final Probing and Progress Indicator 🎬
        total_files = len(paths_to_probe)
        probe_count = 0
        update_interval = 10  # Update the line every 10 probes

        if total_files > 0:
            # Print the initial line to start the progress bar
            sys.stderr.write(f"probing: 0% 0 of {total_files}\r")
            sys.stderr.flush()

        for video_file_path in paths_to_probe:
            probe_count += 1

            # probe = self.get_video_metadata(video_file_path)
            probe = self.probe_cache.get(video_file_path)

            if probe:
                ns = SimpleNamespace(video_file=video_file_path, probe=probe)
                video_files_out.append(ns)

            # Update the progress indicator every N probes or on the last file
            if probe_count % update_interval == 0 or probe_count == total_files:
                percent = int((probe_count / total_files) * 100)

                # \r (carriage return) moves the cursor to the start of the line for overwrite
                sys.stderr.write(f"probing: {percent}% {probe_count} of {total_files}\r")
                sys.stderr.flush()

        # Print a final newline character to clean the console after completion
        if total_files > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

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
            stats = SimpleNamespace(total=0, picked=0, done=0, progress_idx=0)

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

                lines.append(line)
                # nses.append(vid)
                self.visible_vids.append(vid)
                if self.job and self.job.vid == vid:
                    lines.append(f'-----> {self.job.progress}')
                    stats.progress_idx = len(self.visible_vids)
                    self.visible_vids.append(None)
                    if self.win.pick_mode:
                        stats.progress_idx -= 1
                        self.win.set_pick_mode(False)

                # print(line)
            stats.total = len(self.visible_vids)
            return lines, stats

        def toggle_doit(vid):
            if self.dont_doit(vid):
                vid.doit = '[ ]'
            else:
                vid.doit = '[X]' if vid.doit == '[ ]' else '[ ]'

        spin = OptionSpinner()
        spin.add_key('set_all', 's - set all to "[X]"', vals=[False, True])
        spin.add_key('reset_all', 'r - reset all to "[ ]"', vals=[False, True])
        spin.add_key('init_all', 'i,SP - set all initial state', vals=[False, True])
        spin.add_key('toggle', 't - toggle current line state', vals=[False, True])
        spin.add_key('quit', 'q - quit converting OR exit app', vals=[False, True])
        spin.add_key('search', '/ - search string',
                          prompt='Set search string, then Enter')
        spin.add_key('mangle', 'm - mangle titles', vals=[False, True])
        spin.add_key('help', '? - help screen', vals=[False, True])

        others={ord(' '), ord('g')}
        self.spins = spins = spin.default_obj

        self.win = win = ConsoleWindow(keys=spin.keys^others,
                        body_rows=10+len(self.vids), ctrl_c_terminates=False)
        self.state = 'select'

        win.set_pick_mode(True, 1)

        while True:
            lines, stats = make_lines()
            if self.state == 'select':
                head = '[s]etAll [r]setAll [i]nit SP:toggle [g]o [q]uit'
                if self.opts.dry_run:
                    head += ' DRY-RUN'
                if self.opts.sample:
                    head += ' SAMPLE'
                if spins.search:
                    head += f' /{spins.search}'
                win.add_header(head)
            win.add_header(f'     Picked={stats.picked}/{stats.total}')
            if self.state != 'select':
                win.add_header(f' Done={stats.done} q[uit]', resume=True)

            win.add_header(f'CVT {"NET":>4} {"BLOAT":>5}  {"RES":>5}  {"CODEC":>5}  {"MINS":>4} {"GB":>6}   VIDEO')
            if self.state == 'convert':
                win.pick_pos = stats.progress_idx
                win.scroll_pos = stats.progress_idx - win.scroll_view_size
            for line in lines:
                win.add_body(line)
            win.render()
            key = win.prompt(seconds=0.5) # Wait for half a second or a keypress
            if key in spin.keys:
                spin.do_key(key, win)

            if self.state == 'select':
                if spins.set_all:
                    for vid in self.visible_vids:
                        if not self.dont_doit(vid):
                            vid.doit = '[X]'
                    spins.set_all = False

                if spins.reset_all:
                    for vid in self.visible_vids:
                        if vid.doit in ('[ ]', '[X]'):
                            vid.doit = '[ ]'
                    spins.reset_all = False

                if spins.init_all:
                    for vid in self.visible_vids:
                        if vid.doit in ('[ ]', '[X]'):
                            if self.dont_doit(vid) or vid.all_ok:
                                vid.doit = '[ ]'
                            else:
                                vid.doit = '[X]'
                    spins.init_all = False

                if spins.toggle or key == ord(' '):
                    idx = win.pick_pos
                    if 0 <= idx < len(self.visible_vids):
                        toggle_doit(self.visible_vids[idx])
                        spins.toggle = False
                        win.pick_pos += 1

                if key == ord('g'):
                    self.state = 'convert'
                        # self.win.set_pick_mode(False, 1)

                if spins.quit:
                    sys.exit(0)
            if self.state == 'convert':
                if spins.quit:
                    spins.quit = False
                    if self.job:
                        self.job.ffsubproc.stop()
                        self.job.vid.doit = '[X]'
                        self.job = None
                    self.state = 'select'
                    self.vids.sort(key=lambda vid: vid.bloat, reverse=True)
                    win.set_pick_mode(True, 1)
                    continue
                if self.job:
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
                            dumped = vars(self.job.vid)
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
                            break
                        else:
                            break
                if not self.job:
                    for vid in self.visible_vids:
                        if not vid:
                            continue
                        if vid.doit == '[X]':
                            self.prev_time_encoded_secs = -1
                            self.job = self.start_transcode_job(vid)
                            vid.doit = 'IP '
                            break
                    if not self.job:
                        self.state = 'select'
                        self.vids.sort(key=lambda vid: vid.bloat, reverse=True)
                        win.set_pick_mode(True, 1)

            win.clear()

    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        video_files = self.create_video_file_list()
        video_files.sort(key=lambda vid: vid.probe.bloat, reverse=True)

        if not video_files:
            print("Usage: rmbloat {options} {video_file}...")
            sys.exit(1)

        # --- The main loop change is here ---
        for vid in video_files:
            input_file_path_str = vid.video_file
            file_dir, file_basename = os.path.split(input_file_path_str)
            if not file_dir:
                file_dir = os.path.abspath(os.path.dirname(input_file_path_str))

            # Use a try...finally block to ensure you always change back.
            try:
                os.chdir(file_dir)
                self.process_one_file(vid)

            except Exception as e:
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
                               keep_backup=False,
                               bloat_thresh=1600,
                               thread_cnt=3,
                               quality=28,
                               allowed_codecs='x265',
                               full_speed=False)
        vals = cfg.vals
        parser = argparse.ArgumentParser(
            description="A script that accepts dry-run, force, and debug flags.")
        # config options
        parser.add_argument('-B', '--keep-backup',
                    action='store_false' if vals.keep_backup else 'store_true',
                    help='if true, rename to ORIG.{videofile} rather than recycle'
                         + f' [dflt={vals.keep_backup}]')
        parser.add_argument('-b', '--bloat-thresh',
                    default=vals.bloat_thresh, type=int,
                    help='bloat threshold to convert'
                        + f' [dflt={vals.bloat_thresh},min=500]')
        parser.add_argument('-q', '--quality',
                    default=vals.quality, type=int,
                    help=f'output quality (CRF) [dflt={vals.quality}]')
        parser.add_argument('-a', '--allowed-codecs',
                    default=vals.allowed_codecs,
                    choices=('x26*', 'x265', 'all'),
                    help=f'allowed codecs [dflt={vals.allowed_codecs}]')
        parser.add_argument('-F', '--full-speed',
                    action='store_false' if vals.full_speed else 'store_true',
                    help=f'if true, do NOT set nice -n19, ionice -c3,'
                        + f' and thread_cnt [dflt={vals.full_speed}]')
        parser.add_argument('-t', '--thread-cnt',
                    default=vals.thread_cnt, type=int,
                    help='thread count for ffmpeg conversions'
                        + f' [dflt={vals.thread_cnt}]')

        # run-time options
        parser.add_argument('-S', '--save-defaults', action='store_true',
                    help='save the -B/-b/-q/-a/-F/-t options as defaults')
        parser.add_argument('-n', '--dry-run', action='store_true',
                    help='Perform a trial run with no changes made.')
        parser.add_argument('-s', '--sample', action='store_true',
                    help='produce 30s samples called SAMPLE.{input-file}')
        parser.add_argument('-L', '--logs', action='store_true',
                    help='view the logs')

        parser.add_argument('files', nargs='*',
            help='Non-option arguments (e.g., file paths or names).')
        opts = parser.parse_args(args)
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
