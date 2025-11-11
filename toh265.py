#!/usr/bin/env python3
# pylint: disable=too-many-statements
""" TBD """
import sys
import os
import math
import argparse
import subprocess
import traceback
import json
import re
import time
import fcntl
from textwrap import indent
from typing import Optional, Union
from copy import copy
from types import SimpleNamespace
from datetime import timedelta
import send2trash
from console_window import ConsoleWindow, OptionSpinner
# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return,too-many-branches
# pylint: disable=too-many-return-statements,too-many-instance-attributes
# pylint: disable=consider-using-with

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

    def stop(self):
        """
        Terminates the subprocess if it is still running.
        """
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5) # Wait for it to die gracefully
        self.process = None
        self.partial_line = ""
        self.return_code = None

    def __del__(self):
        """Ensure the subprocess is terminated when the object is destroyed."""
        self.stop()

class Job: # class FfmpegJob:
    """ TBD """
    def __init__(self, ns, orig_backup_file, temp_file, duration_secs):
        self.ns = ns
        self.start_time=time.time()
        self.progress='Started'
        self.input_file = ns.filebase
        self.orig_backup_file=orig_backup_file
        self.temp_file=temp_file
        self.duration_secs=duration_secs
        self.total_duration_formatted=str(timedelta(seconds=int(duration_secs)))
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


# --- Example Usage in Your Curses Loop ---

# # ... (Setup your FFmpeg command line) ...
# ffmpeg_cmd = [
#     'ffmpeg', '-i', 'input.mp4', '-c:v', 'libx265', '-crf', '30', 'output.mp4'
# ]
#
# monitor = FfmpegMon()
# monitor.start(ffmpeg_cmd)
#
# while True:
#     # 1. Poll the process
#     status = monitor.poll()
#
#     # 2. Handle the status
#     if isinstance(status, int):
#         # Process is done. Handle success/failure based on the return code.
#         # print(f"FFmpeg finished with code: {status}")
#         break
#     elif isinstance(status, str):
#         # New progress line received!
#         # progress_line = status
#         # Match your PROGRESS_RE and update your curses window here
#         pass
#     elif status is None:
#         # No new line. Use this time to handle user input (getch()),
#         # update your UI's clock, or refresh the screen to prevent flickering.
#         # time.sleep(0.01)
#         pass
#
#     # CRITICAL: Sleep briefly to prevent a busy-loop from consuming CPU
#     time.sleep(0.01)

class Converter:
    """ TBD """
    # --- Conversion Criteria Constants (Customize these) ---
    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    TARGET_CODECS = ['h265', 'hevc']
    MAX_BITRATE_KBPS = 2100 # about 15MB/min (or 600MB for 40m)

    # --- Configuration ---
    OUTPUT_CRF = 22          # Target CRF for new x265 encodes
    PROGRESS_UPDATE_INTERVAL = 3  # Seconds between print updates

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

    def __init__(self, opts):
        assert Converter.singleton is None
        Converter.singleton = self
        self.opts = opts
        self.videos = []
        self.original_cwd = os.getcwd()
        self.ff_pre_i_opts = []
        self.ff_post_i_opts = []
        self.state = 'probe' # 'select', 'convert'
        self.job = None
        self.win = None

    def get_video_metadata(self, file_path):
        """
        Extracts video metadata using ffprobe and returns it as a Python dictionary.
        Returns: dict or None: A dictionary containing the ffprobe output, or None if an
                          error occurs (e.g., file not found, ffprobe fails).
        """
        # Check if the file exists
        if not os.path.exists(file_path):
            print(f"Error: File not found at '{file_path}'")
            return None

        # ffprobe command to output format and stream information in JSON format
        # -v error: Suppress all non-error messages (like the banner)
        # -print_format json: Output in JSON format
        # -show_format: Include container format information
        # -show_streams: Include stream (video, audio, etc.) information
        command = [
            'ffprobe',
            '-v', 'error',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            file_path
        ]

        try:
            # Execute the command
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True  # Raise a CalledProcessError for non-zero exit codes
            )

            # Parse the JSON output
            if self.opts.debug:
                print(result.stdout)
            metadata = json.loads(result.stdout)
            # Extract values for comparison
            video_stream = next((s for s in metadata.get('streams', [])
                                 if s.get('codec_type') == 'video'), None)
            ns = SimpleNamespace()
            ns.width = int(video_stream.get('width', 0))
            ns.height = int(video_stream.get('height', 0))
            ns.codec = video_stream.get('codec_name', 'unk_codec')
            ns.bitrate = int(int(metadata["format"].get('bit_rate', 0))/1000) # in KBPS
            ns.duration = float(metadata["format"].get('duration', 0.0)) # in secs
            ns.gb = self.get_file_size_gb(file_path)

            return ns

        except FileNotFoundError:
            # This occurs if 'ffprobe' is not found in the system's PATH
            print("Error: ffprobe command not found. Ensure FFmpeg/ffprobe is installed and in your system PATH.")
            return None
        except subprocess.CalledProcessError as e:
            # This occurs if ffprobe runs but returns an error code (e.g., file is corrupt)
            print(f"Error running ffprobe for '{file_path}':")
            print(f"  Command: {' '.join(e.cmd)}")
            print(f"  Return Code: {e.returncode}")
            print(f"  Stderr: {e.stderr.strip()}")
            return None
        except json.JSONDecodeError:
            # This occurs if the output is not valid JSON
            print(f"Error: Failed to decode JSON from ffprobe output for '{file_path}'.")
            # print(f"Raw output: {result.stdout.strip()}") # Uncomment for debugging
            return None
        except Exception as e:
            # Catch any other unexpected errors
            print(f"An unexpected error occurred: {e}")
            return None

    def already_converted(self, basic_ns, video_file):
        """
        Checks if a video file already meets the updated conversion criteria:
        1. Resolution is at least TARGET_WIDTH x TARGET_HEIGHT.
        2. Video codec is TARGET_CODECS (e.g., 'h264').
        3. Video bitrate is below MAX_BITRATE_KBPS.

        Args:
            filepath (str): The path to the video file.

        Returns:
            bool: True if the file meets all criteria, False otherwise.
        """
        # shorthand
        probe = basic_ns.probe
        width = probe.width
        height = probe.height
        codec = probe.codec
        bitrate = probe.bitrate
        gb = probe.gb

        # 1. Check Resolution
        # Assuming resolution check is 'at least' the target
        res_ok = bool(height is not None and height <= self.TARGET_HEIGHT)

        # 2. Check Codec
        # codec_ok = (codec is not None and codec.lower() in self.TARGET_CODECS)

        # 3. Check Bitrate (with tolerance)
        bitrate_ok = bool(bitrate <= self.MAX_BITRATE_KBPS)
        all_ok = bool(res_ok and bitrate_ok)

        summary = f'  {width}x{height} {codec} {bitrate:.0f} kbps {gb}G'
        ns = SimpleNamespace(doit='', width=width, height=height, res_ok=res_ok,
                             codec=codec, bitrate=bitrate, bitrate_ok=bitrate_ok,
                             gb=gb, all_ok=all_ok, filepath=video_file,
                             filedir=os.path.dirname(video_file),
                             filebase=os.path.basename(video_file),
                             standard_name=basic_ns.standard_name,
                             do_rename=basic_ns.do_rename, probe=basic_ns.probe,
                             return_code=None, texts=[])
        self.videos.append(ns)

        if (res_ok and bitrate_ok) or ns.filebase.startswith('SAMPLE.'):
            if self.opts.window_mode:
                ns.doit = '[ ]'
            else:
                print(f'      -: {summary}')
            return True
        else:
            why = '' if res_ok else f'>{self.TARGET_HEIGHT}p '
            why += '' if bitrate_ok else f'>{self.MAX_BITRATE_KBPS} kbps'
            if why:
                why = f' [{why}]'
            if self.opts.window_mode:
                ns.doit = '[X]'
            else:
                print(f'CONVERT: {summary}{why}')
            return False

    def start_transcode_job(self, ns):
        """ TBD """

        os.chdir(ns.filedir)

        ## print(f'standard_name2: {do_rename=} {standard_name=})')
        prefix = '/tmp/SAMPLE' if self.opts.sample else 'TEST'
        temp_file = f"{prefix}.{ns.standard_name}"
        orig_backup_file = f"ORIG.{ns.filebase}"

        if os.path.exists(temp_file):
            os.unlink(temp_file)
        duration_secs = ns.probe.duration
        if self.opts.sample:
            duration_secs = self.sample_seconds

        job = Job(ns, orig_backup_file, temp_file, duration_secs)
        pre_i_opts, post_i_opts = copy(self.ff_pre_i_opts), copy(self.ff_post_i_opts)
        job.input_file = ns.filebase

        if self.opts.sample:
            start_secs = max(120, job.duration_secs)*.20
            pre_i_opts += [ '-ss', job.duration_spec(start_secs) ]
            post_i_opts += [ '-t', str(self.sample_seconds)]

        # Define the FFmpeg command
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',                           # Overwrite temp file if exists
            # '-v', 'error',                  # suppress INFO/WARNINGS
            * pre_i_opts,
            '-i', job.input_file,
            * post_i_opts,
            '-c:v', 'libx265',
            '-crf', str(self.opts.quality),
            '-preset', 'medium',
            # '-preset', 'fast',
            '-c:a', 'copy',
            '-c:s', 'copy',
            '-map', '0',
            job.temp_file
        ]
        if self.opts.dry_run:
            print(f"SKIP RUNNING {ffmpeg_cmd}\n")
        else:
            job.ffsubproc.start(ffmpeg_cmd)
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
                    print(got, end='', flush=True)
                elif isinstance(got, int):
                    return_code = got
                    break

            # Clear the progress line and print final status
            print('\r' + ' ' * 120, end='', flush=True) # Overwrite last line with spaces

        if self.opts.dry_run or return_code == 0:
            print(f"\r{job.input_file}: Transcoding FINISHED"
                  f" (Elapsed: {timedelta(seconds=int(time.time() - job.start_time))})")
            return True # Success
        else:
            # Print a final error message
            print(f"\r{job.input_file}: Transcoding FAILED (Return Code: {job.returncode})")
            # In a real script, you'd save or display the full error output from stderr here.
            return False

    def get_job_progress(self, job):
        """ TBD """
        ns = job.ns
        while True:
            got = job.ffsubproc.poll()
            if isinstance(got, str):
                line = got
                match = self.PROGRESS_RE.search(line)
                if not match:
                    ns.texts.append(line)
                    continue

                # 1. Extract values from the regex match
                try:
                    groups = match.groups()

                    # The first two parts (H and M) are integers. The third part (S.ms) is the float.
                    h = int(groups[1])
                    m = int(groups[2])
                    s = int(groups[3])
                    ms = int(groups[4])
                    time_encoded_seconds = h * 3600 + m * 60 + s + ms / 100
                    speed = float(match.group(6))
                except Exception:
                    print(f"\n{line=} {groups=}")
                    raise

                elapsed_time_sec = int(time.time() - job.start_time)

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
                cur_time_formatted = job.trim0(str(timedelta(seconds=int(time_encoded_seconds))))
                progress_line = (
                    f"{job.trim0(str(timedelta(seconds=elapsed_time_sec)))} | "
                    f"{percent_complete:.1f}% | "
                    f"ETA {remaining_time_formatted} | "
                    f"Speed {speed:.1f}x | "
                    f"Time {cur_time_formatted}/{job.total_duration_formatted}"
                )
                return progress_line
            elif isinstance(got, int):
                ns.return_code = got 
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
    def get_file_size_gb(filepath: str) -> str:
        """
        Gets the size of a given file path and returns it in a human-readable format.
        Returns:
            A string with the file size (e.g., "1.2 MB") or an error message.
        """
        try:
            # Get the size in bytes
            size_bytes = os.path.getsize(filepath)

            # Convert bytes to human-readable format
            return round(size_bytes / (1024*1024*1024), 3)
            # return Converter.human_readable_size(size_bytes)

        except FileNotFoundError:
            return f"Error: File not found at '{filepath}'"
        except Exception as e:
            return f"Error getting file size: {e}"


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

    def standard_name(self, filename: str, height: int) -> str:
        """
        Replaces common H.264/AVC/Xvid/DivX codec strings in a filename
        with 'x265' or 'X265', preserving the original case where possible.
        Also change the height indicator if not in agreement with actual.
        Returns: Whether changed and filename string with x265 codec.
        """

        new_filename = filename
        # Regular expressions for the codecs to be replaced.
        # The groups will capture the exact string for case-checking later.
        pattern = r'\b([xh]\.?264|avc|xvid|divx)\b'
        regex = re.compile(pattern, re.IGNORECASE)
        end = 0
        while True:
            match = re.search(regex, new_filename[end:])
            if not match:
                break
            sub = 'X265' if match.group(1).isupper() else 'x265'
            start, end = match.span(1)
            new_filename = new_filename[:start] + sub + new_filename[end:]

        pattern = r'\b(\d+[pi]|UHD|4K|2160p|1440p|2K|8K)\b'
        regex = re.compile(pattern, re.IGNORECASE)
        height_str = f'{height}p' # e.g., '1080p'

        end = 0
        while True:
            match = re.search(regex, new_filename[end:])
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

            new_filename = new_filename[:start] + sub + new_filename[end:]


        # nail down extension
        different = bool(new_filename != filename)
        base, _ = os.path.splitext(new_filename)
        new_filename = base + '.mkv'

        if self.opts.debug:
            print(f'standard_name: {different=} {new_filename=})')

        return different, new_filename

    def bulk_rename(self, old_file_name: str, new_file_name: str):
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
        dry_run = self.opts.dry_run

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

                new_item_name = None

                # --- Rule 1: Special Case - Full Name Match (item_name == old_base_name) ---
                if item_name == old_base_name:
                    new_item_name = new_base_name

                # --- Rule 2: Special Case - Reference SRT Suffix Match ---
                # Requires the item to end with ".reference.srt" AND the base part to match old_base_name
                elif (item_name.lower().endswith(special_ext.lower())
                      and item_name[:-len(special_ext)] == old_base_name):
                    new_item_name = new_base_name + special_ext

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
                    if dry_run:
                        print(f"  WOULD rename as: '{full_new_path}'")
                    else:
                        os.rename(full_old_path, full_new_path)
                except OSError as e:
                    # Handle potential errors (e.g., permission errors, file in use)
                    print(f"  ERROR renaming '{full_old_path}' to '{full_new_path}': {e}")
                except Exception as e:
                    # Catch other unexpected errors
                    print(f"  An unexpected error occurred with '{full_old_path}': {e}")

    def process_one_file(self, ns):
        """ Handle just one """
        input_file = ns.video_file
        if not self.is_valid_video_file(input_file):
            return  # Skip to the next file in the loop
        if not self.opts.window_mode:
            print("\n" + "=" * 80)
            print(f"{input_file}")

        # --- File names for the safe replacement process ---
        do_rename, standard_name = self.standard_name(os.path.basename(input_file), ns.probe.height)

        ns.do_rename = do_rename
        ns.standard_name = standard_name

        if self.opts.rename_only:
            if do_rename:
                self.bulk_rename(input_file, standard_name)
            return

        # 1. Quality Checkns
        if self.already_converted(ns, input_file):
            return
        if self.opts.info_only:
            return
        if self.opts.window_mode:
            return
        self.convert_one_file(ns)

    def convert_one_file(self, ns):
        """ TBD """

        # 3. Transcode with monitored progress
        job = self.start_transcode_job(ns)
        success = self.monitor_transcode_progress(job)
        self.finish_transcode_job(success, job)

    def finish_transcode_job(self, success, job):
        """ TBD """
        # 4. Atomic Swap (Safe Replacement)
        dry_run = self.opts.dry_run
        ns = job.ns
        if success and not self.opts.sample:
            would = 'WOULD ' if dry_run else ''
            try:
                # Rename original to backup
                if not dry_run:
                    if self.opts.keep_backup:
                        os.rename(ns.filebase, job.orig_backup_file)
                    else:
                        send2trash.send2trash(ns.filebase)
                else:
                    if self.opts.keep_backup:
                        print(f"{would}Move Original to {job.orig_backup_file}")
                    else:
                        print(f"{would}Trash {ns.filebase}")

                # Rename temporary file to the original filename
                if not dry_run:
                    os.rename(job.temp_file, ns.standard_name)
                print(f"OK: {would}Replace {ns.standard_name}")

                if ns.do_rename:
                    self.bulk_rename(ns.filebase, ns.standard_name)
                
                if not dry_run:
                    ns.probe = self.get_video_metadata(ns.standard_name)

            except OSError as e:
                print(f"ERROR during swap of {ns.filepath}: {e}")
                print(f"Original: {job.orig_backup_file}, New: {job.temp_file}. Manual cleanup required.")
        elif success and self.opts.sample:
                ns.probe = self.get_video_metadata(job.temp_file)
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

            probe = self.get_video_metadata(video_file_path)

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

    def do_window_mode(self):
        """ TBD """
        def make_lines(doit_skips=None):
            lines, nses, progress_idx = [], [], 0

            for idx, ns in enumerate(self.videos):
                if doit_skips and ns.doit in doit_skips:
                    continue
                basename = os.path.basename(ns.filepath)
                dirname = os.path.dirname(ns.filepath)
                res = f'{ns.width}x{ns.height}'
                ht_over = ' ' if ns.res_ok else '^' # '■'
                br_over = ' ' if ns.bitrate_ok else '^' # '■'
                line = f'{ns.doit:>3} {res:>9}{ht_over} {ns.bitrate:5}{br_over} {ns.gb:>6}   {basename} ON {dirname}'
                lines.append(line)
                nses.append(ns)
                if self.job and self.job.ns == ns:
                    lines.append(f'-----> {self.job.progress}')
                    progress_idx = 1+idx

                # print(line)
            return lines, nses, progress_idx

        spin = OptionSpinner()
        spin.add_key('set_all', 's - set all to "[X]"', vals=[False, True])
        spin.add_key('reset_all', 'r - reset all to "[ ]"', vals=[False, True])
        spin.add_key('init_all', 'i,SP - set all initial state', vals=[False, True])
        spin.add_key('toggle', 't - toggle current line state', vals=[False, True])
        spin.add_key('quit', 'q - exit the program', vals=[False, True])
        others={ord(' '), ord('g')}
        vals = spin.default_obj

        self.win = win = ConsoleWindow(keys=spin.keys^others)
        self.state = 'select'

        win.set_pick_mode(True, 1)

        while True:
            win.add_header('[s]etAll [r]setAll [i]nit SP:toggle [G]o [q]uit')
            win.add_header(f'CVT {"RES":>9}  {"KPBS":>5}  {"GB":>6}   VIDEO')
            lines, _, progress_idx = make_lines()
            if self.state == 'convert':
                win.pick_pos = progress_idx
            for line in lines:
                win.add_body(line)
            win.render()
            key = win.prompt(seconds=0.5) # Wait for half a second or a keypress
            if key in spin.keys:
                spin.do_key(key, win)

            if self.state == 'select':
                if vals.set_all:
                    for ns in self.videos:
                        if not ns.filebase.startswith('SAMPLE.'):
                            ns.doit = '[X]'
                    vals.set_all = False

                if vals.reset_all:
                    for ns in self.videos:
                        ns.doit = '[ ]'
                    vals.reset_all = False

                if vals.init_all:
                    for ns in self.videos:
                        ns.doit = '[X]' if '[' in ns.over else '[ ]'
                    vals.init_all = False

                if vals.toggle or key == ord(' '):
                    idx = win.pick_pos
                    if 0 <= idx < len(self.videos):
                        ns = self.videos[idx]
                        if not ns.filebase.startswith('SAMPLE.'):
                            ns.doit = '[X]' if ns.doit == '[ ]' else '[ ]'
                        vals.toggle = False
                        win.pick_pos += 1

                if key == ord('g'):
                    if self.opts.keep_window:
                        self.state = 'convert'
                        # self.win.set_pick_mode(False, 1)
                    else:
                        win.stop_curses()
                        break

                if vals.quit:
                    sys.exit(0)
            elif self.state == 'convert':
                if vals.quit:
                    if self.job:
                        self.job.ffsubproc.stop()
                        self.job.ns.doit = 'ABT'
                        self.job = None
                        break
                if self.job:
                    while True:
                        got = self.get_job_progress(self.job)
                        if isinstance(got, str):
                            self.job.progress = got
                        elif isinstance(got, int):
                            self.job.ns.doit = ' OK' if got == 0 else 'ERR'
                            self.finish_transcode_job(
                                success=bool(got == 0), job=self.job)
                            self.job = None
                            break
                        else:
                            break
                if not self.job:
                    for ns in self.videos:
                        if ns.doit == '[X]':
                            self.job = self.start_transcode_job(ns)
                            ns.doit = 'IP '
                            break
                    if not self.job:
                        win.stop_curses()
                        break

            win.clear()

        if not self.opts.keep_window:
            for ns in self.videos:
                if 'X' in ns.doit:
                    print(f'>>> {ns.filebase}')
                    self.convert_one_file(ns)
        else:
            lines, nses, _ = make_lines(doit_skips={'[ ]', '[X]'})
            for idx, line in enumerate(lines):
                ns = nses[idx]
                print(line)
                if ns.return_code:
                    indent('\n'.join(ns.texts), '  ')


    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        video_files = self.create_video_file_list()

        if not video_files:
            print("Usage: toh265 {options} {video_file}...")
            sys.exit(1)

        # --- The main loop change is here ---
        for ns in video_files:
            input_file_path_str = ns.video_file
            file_dir, file_basename = os.path.split(input_file_path_str)
            if not file_dir:
                file_dir = os.path.abspath(os.path.dirname(input_file_path_str))

            # Use a try...finally block to ensure you always change back.
            try:
                os.chdir(file_dir)
                self.process_one_file(ns)

            except Exception as e:
                print(f"An error occurred while processing {file_basename}: {e}")
            finally:
                os.chdir(self.original_cwd)
        if self.opts.window_mode:
            self.do_window_mode()



def main(args=None):
    """
    Convert video files to desired form
    """
    try:
        parser = argparse.ArgumentParser(
            description="A script that accepts dry-run, force, and debug flags.")
        parser.add_argument('-b', '--keep-backup', action='store_true',
                    help='rather than recycle, rename to ORIG.{videofile}')
        parser.add_argument('-i', '--info-only', action='store_true',
                    help='print just basic info')
        parser.add_argument('-n', '--dry-run', action='store_true',
                    help='Perform a trial run with no changes made.')
        parser.add_argument('-f', '--force', action='store_true',
                    help='Force the operation to proceed.')
        parser.add_argument('-r', '--rename-only', action='store_true',
                    help='just look for re-names')
        parser.add_argument('-s', '--sample', action='store_false',
                    help='produce 30s samples called SAMPLE.{input-file}')
        parser.add_argument('-w', '--window-mode', action='store_false',
                    help='just look for re-names')
        parser.add_argument('-q', '--quality', default=23,
                    help='output quality (CRF) [dflt=23]')
        parser.add_argument('-W', '--keep-window', action='store_false',
                    help='run conversions in window mode')
        parser.add_argument('-D', '--debug', action='store_true',
                    help='Enable debug output.')
        parser.add_argument('files', nargs='*',
            help='Non-option arguments (e.g., file paths or names).')
        opts = parser.parse_args(args)
        if opts.sample:
            opts.dry_run = False
        if opts.dry_run or opts.rename_only:
            opts.keep_window = False

        Converter(opts).main_loop()
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
