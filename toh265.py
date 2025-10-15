#!/usr/bin/env python3
""" TBD """
import sys
import os
import argparse
import subprocess
import json
import re
import time
from types import SimpleNamespace
from datetime import timedelta
import send2trash
# pylint: disable=too-many-locals,line-too-long,broad-exception-caught
# pylint: disable=no-else-return

class Converter:
    """ TBD """
    # --- Conversion Criteria Constants (Customize these) ---
    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    TARGET_CODECS = ['h265', 'hevc']
    MAX_BITRATE_KBPS = 2100 # about 15MB/min (or 600MB for 40m)

    # --- Configuration ---
    OUTPUT_CRF = 24          # Target CRF for new x265 encodes
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
            ns.duration = float(metadata["format"].get('duration', 0.0)) # in KBPS
            ns.size = self.get_file_size_readable(file_path)

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

    def already_converted(self):
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
        width = self.probe.width
        height = self.probe.height
        codec = self.probe.codec
        bitrate = self.probe.bitrate
        size = self.probe.size

        # 1. Check Resolution
        # Assuming resolution check is 'at least' the target
        res_ok = bool(height is not None and height <= self.TARGET_HEIGHT)

        # 2. Check Codec
        # codec_ok = (codec is not None and codec.lower() in self.TARGET_CODECS)

        # 3. Check Bitrate (with tolerance)
        bitrate_ok = bool(bitrate <= self.MAX_BITRATE_KBPS)

        summary = f'  {width}x{height} {codec} {bitrate:.0f} kbps {size}'

        if res_ok and bitrate_ok:
            print(f'     ok: {summary}')
            return True
        else:
            why = '' if res_ok else f'>{self.TARGET_HEIGHT}p '
            why += '' if bitrate_ok else f'>{self.MAX_BITRATE_KBPS} kbps'
            print(f'CONVERT: {summary}: {why}')
            return False

    def monitor_transcode_progress(self, input_file, temp_file, duration_seconds):
        """
        Runs the FFmpeg transcode command and monitors its output for a non-scrolling display.
        """
        def trim0(str):
            if str.startswith('0:'):
                return str[2:]
            return str

        # Define the FFmpeg command
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',                           # Overwrite temp file if exists
            '-i', input_file,
            '-c:v', 'libx265',
            '-crf', str(self.OUTPUT_CRF),
            '-preset', 'medium',
            '-c:a', 'copy',
            '-c:s', 'copy',
            '-map', '0',
            temp_file
        ]

        start_time = time.time()
        if self.opts.dry_run:
            print(f"SKIP RUNNING {ffmpeg_cmd}\n")
        else:
            # Start FFmpeg subprocess
            # We pipe stderr to capture progress updates
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,  # Discard normal stdout output
                stderr=subprocess.PIPE,     # Capture progress messages from stderr
                text=True,                  # Read output as text (string)
                bufsize=1
            )

            last_update_time = start_time
            total_duration_formatted = trim0(str(timedelta(seconds=int(duration_seconds))))

            # --- Progress Monitoring Loop ---
            # Read stderr line-by-line until the process finishes
            for line in process.stderr:
                match = self.PROGRESS_RE.search(line)

                # Check if the line contains progress data and if the update interval has passed
                if match and (time.time() - last_update_time) >= self.PROGRESS_UPDATE_INTERVAL:

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

                    elapsed_time_sec = int(time.time() - start_time)

                    # 2. Calculate remaining time
                    if duration_seconds > 0:
                        percent_complete = (time_encoded_seconds / duration_seconds) * 100

                        if percent_complete > 0 and speed > 0:
                            # Time Remaining calculation (rough estimate)
                            # Remaining Time = (Total Time - Encoded Time) / Speed
                            remaining_seconds = (duration_seconds - time_encoded_seconds) / speed
                            remaining_time_formatted = trim0(str(timedelta(seconds=int(remaining_seconds))))
                        else:
                            remaining_time_formatted = "N/A"
                    else:
                        percent_complete = 0.0
                        remaining_time_formatted = "N/A"

                    # 3. Format the output line
                    # \r at the start makes the console cursor go back to the beginning of the line
                    cur_time_formatted = trim0(str(timedelta(seconds=int(time_encoded_seconds))))
                    progress_line = (
                        f"\r{trim0(str(timedelta(seconds=elapsed_time_sec)))} | "
                        f"{percent_complete:.1f}% | "
                        f"ETA {remaining_time_formatted} | "
                        f"Speed {speed:.1f}x | "
                        f"Time {cur_time_formatted}/{total_duration_formatted}"
                    )

                    # 4. Print and reset timer
                    print(progress_line, end='', flush=True)
                    last_update_time = time.time()

            # Wait for the process to truly finish and get the return code
            process.wait()

            # Clear the progress line and print final status
            print('\r' + ' ' * 120, end='', flush=True) # Overwrite last line with spaces

        if self.opts.dry_run or process.returncode == 0:
            print(f"\r{input_file}: Transcoding FINISHED (Elapsed: {timedelta(seconds=int(time.time() - start_time))})")
            return True # Success
        else:
            # Print a final error message
            print(f"\r{input_file}: Transcoding FAILED (Return Code: {process.returncode})")
            # In a real script, you'd save or display the full error output from stderr here.
            return False

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
    def get_file_size_readable(filepath: str) -> str:
        """
        Gets the size of a given file path and returns it in a human-readable format.
        Returns:
            A string with the file size (e.g., "1.2 MB") or an error message.
        """
        try:
            # Get the size in bytes
            size_bytes = os.path.getsize(filepath)

            # Convert bytes to human-readable format
            return Converter.human_readable_size(size_bytes)

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
            print(f"Skipping '{filename}': Starts with a forbidden prefix.")
            return False

        # Get the file extension and convert to lowercase for case-insensitive check
        # os.path.splitext returns a tuple: (root, ext)
        _, ext = os.path.splitext(filename)

        # 2. Check if the extension is a recognized video format
        if ext.lower() not in self.VIDEO_EXTENSIONS:
            print(f"Skipping '{filename}': Not a recognized video file extension ('{ext.lower()}').")
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

    def process_one_file(self, input_file):
        """ Handle just one """
        dry_run = self.opts.dry_run
        if not self.is_valid_video_file(input_file):
            return  # Skip to the next file in the loop
        print("\n" + "=" * 80)
        print(f"{input_file}")

        self.probe = self.get_video_metadata(input_file)
        if not self.probe:
            print("SKIP: did not get probe metadata")
            return
        # --- File names for the safe replacement process ---
        do_rename, standard_name = self.standard_name(input_file, self.probe.height)
        
        if self.opts.rename_only:
            if do_rename:
                would = 'WOULD ' if dry_run else ''
                self.bulk_rename(input_file, standard_name)
            return

        # 1. Quality Check
        if self.already_converted():
            return
        if self.opts.info_only:
            return

        ## print(f'standard_name2: {do_rename=} {standard_name=})')
        temp_file = f"TEMP.{standard_name}"
        orig_backup_file = f"ORIG.{input_file}"

        # 2. Get total video duration for ETA calculation
        duration = self.probe.duration
        if duration == 0.0:
            print("WARNING: Cannot determine video duration. Progress monitor will only show elapsed time.")

        # 3. Transcode with monitored progress
        success = self.monitor_transcode_progress(input_file, temp_file, duration)

        # 4. Atomic Swap (Safe Replacement)
        if success:
            would = 'WOULD ' if dry_run else ''
            try:
                # Rename original to backup
                if not dry_run:
                    if self.opts.keep_backup:
                        os.rename(input_file, orig_backup_file)
                    else:
                        send2trash.send2trash(input_file)
                else:
                    if self.opts.keep_backup:
                        print(f"{would}Move Original to {orig_backup_file}")
                    else:
                        print(f"{would}Trash {input_file}")

                # Rename temporary file to the original filename
                if not dry_run:
                    os.rename(temp_file, standard_name)
                print(f"OK: {would}Replace {standard_name}")

                if do_rename:
                    self.bulk_rename(input_file, standard_name)

            except OSError as e:
                print(f"ERROR during swap of {input_file}: {e}")
                print(f"Original: {orig_backup_file}, New: {temp_file}. Manual cleanup required.")
        else:
            # Transcoding failed, delete the temporary file
            if os.path.exists(temp_file):
                os.remove(temp_file)
                print(f"FFmpeg failed. Deleted incomplete {temp_file}.")

    def main_loop(self):
        """ TBD """
        # sys.argv is the list of command-line arguments. sys.argv[0] is the script name.
        video_files = []

        for video_file in self.opts.files:
            if not self.is_valid_video_file(video_file):
                continue  # Skip to the next file in the loop
            video_files.append(video_file)

        if not video_files:
            print("Usage: toh265 {options} {video_file}...")
            sys.exit(1)

        # --- The main loop change is here ---
        original_cwd = os.getcwd()
        for input_file_path_str in video_files:
            file_dir, file_basename = os.path.split(input_file_path_str)
            if not file_dir:
                file_dir = os.path.abspath(os.path.dirname(input_file_path_str))

            # Use a try...finally block to ensure you always change back.
            try:
                os.chdir(file_dir)
                self.process_one_file(file_basename)

            except Exception as e:
                print(f"An error occurred while processing {file_basename}: {e}")
            finally:
                os.chdir(original_cwd)


    def __init__(self, opts):
        self.opts = opts
        self.probe = None

def main(args=None):
    """
    Convert video files to desired form
    """
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
    parser.add_argument('-D', '--debug', action='store_true',
                help='Enable debug output.')
    parser.add_argument('files', nargs='*',
        help='Non-option arguments (e.g., file paths or names).')
    opts = parser.parse_args(args)

    Converter(opts).main_loop()


if __name__ == '__main__':
    # When the script is run directly, call main
    # Pass sys.argv[1:] to main, but it's cleaner to let argparse
    # handle reading from sys.argv directly, as done above.
    main()
