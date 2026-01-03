#!/usr/bin/env python3
"""
Job handling for video conversion - manages transcoding jobs and progress monitoring
"""
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=broad-exception-caught,invalid-name
# pylint: disable=too-many-instance-attributes,no-else-return
import os
import re
import time
from datetime import timedelta
from pathlib import Path
import send2trash
from .Models import Job
from . import FileOps


class JobHandler:
    """Handles video transcoding job execution and monitoring"""

    # Regex for parsing FFmpeg progress output
    PROGRESS_RE = re.compile(
        # 1. Frame Section (Required, Strict Numerical Capture)
        # Looks for 'frame=', then captures the integer (G1).
        r"\s*frame[=\s]+(\d+)\s+"

        # 2. Time Section (Optional, Strict Numerical Capture)
        # Looks for 'time=', then attempts to capture the precise HH:MM:SS.cs format (G2-G5).
        r"(?:.*?time[=\s]+(\d{2}):(\d{2}):(\d{2})\.(\d{2}))?"

        # 3. Speed Section (Optional, Strict Numerical Capture)
        # Looks for 'speed=', then captures the float (G6).
        r"(?:.*?speed[=\s]+(\d+\.\d+)x)?",

        re.IGNORECASE
    )

    # Regex for validating SRT timestamp lines (HH:MM:SS,mmm --> HH:MM:SS,mmm)
    SRT_TIMESTAMP_RE = re.compile(
        r'^\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}',
        re.IGNORECASE
    )

    sample_seconds = 30

    def __init__(self, opts, chooser, probe_cache, auto_mode_enabled=False):
        """
        Initialize job handler.

        Args:
            opts: Command-line options
            chooser: FfmpegChooser instance
            probe_cache: ProbeCache instance
            auto_mode_enabled: Whether auto mode is enabled
        """
        self.opts = opts
        self.chooser = chooser
        self.probe_cache = probe_cache

        # Progress tracking
        self.progress_line_mono = 0

        # Auto mode tracking
        self.auto_mode_enabled = auto_mode_enabled
        self.auto_mode_start_time = time.monotonic() if auto_mode_enabled else None
        self.consecutive_failures = 0
        self.ok_count = 0
        self.error_count = 0

    def validate_srt_file(self, filepath, min_captions=12):
        """
        Validate an SRT subtitle file.

        Checks that the file:
        - Is not empty
        - Has valid SRT format (sequence numbers, timestamps, text)
        - Contains at least min_captions caption entries

        Args:
            filepath: Path to the SRT file
            min_captions: Minimum number of captions required (default: 12)

        Returns:
            True if valid, False otherwise
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if not lines:
                return False  # Empty file

            caption_count = 0
            i = 0

            while i < len(lines):
                line = lines[i].strip()

                # Skip blank lines
                if not line:
                    i += 1
                    continue

                # Expect sequence number
                if not line.isdigit():
                    # Could be malformed, but allow some flexibility
                    i += 1
                    continue

                i += 1
                if i >= len(lines):
                    break

                # Expect timestamp line
                timestamp_line = lines[i].strip()
                if not self.SRT_TIMESTAMP_RE.match(timestamp_line):
                    # Not a valid timestamp, skip
                    i += 1
                    continue

                i += 1

                # Expect at least one line of caption text
                has_text = False
                while i < len(lines) and lines[i].strip():
                    has_text = True
                    i += 1

                if has_text:
                    caption_count += 1

                # i is now at a blank line or EOF

            return caption_count >= min_captions

        except Exception:
            return False

    def make_color_opts(self, color_spt):
        """ Generate FFmpeg color space options from color_spt string """
        spt_parts = color_spt.split(',')

        # 1. Reconstruct the three full, original values (can contain 'unknown')
        space_orig = spt_parts[0]
        primaries_orig = spt_parts[1] if spt_parts[1] != "~" else space_orig
        trc_orig = spt_parts[2] if spt_parts[2] != "~" else primaries_orig

        # 2. Define the final, valid FFmpeg values using fallback logic

        # Use BT.709 as the default standard for all three components
        DEFAULT_SPACE = 'bt709'
        DEFAULT_PRIMARIES = 'bt709'
        DEFAULT_TRC = '709'  # Note: TRC often uses '709' instead of 'bt709' string

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

    def start_transcode_job(self, vid, bash_quote_func):
        """Start a transcoding job using FfmpegChooser."""
        os.chdir(os.path.dirname(vid.filepath))
        basename = os.path.basename(vid.filepath)
        probe = vid.probe0

        merged_external_subtitle = None
        if self.opts.merge_subtitles:
            subtitle_path = Path(vid.filepath).with_suffix('.en.srt')
            if subtitle_path.exists() and self.validate_srt_file(subtitle_path):
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

        job = Job(vid, orig_backup_file, temp_file, duration_secs, dry_run=self.opts.dry_run)
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
        vid.command = bash_quote_func(ffmpeg_cmd)

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
            print('\r' + ' ' * 120, end='', flush=True)  # Overwrite last line with spaces

        if self.opts.dry_run or return_code == 0:
            print(f"\r{job.input_file}: Transcoding FINISHED"
                  f" (Elapsed: {timedelta(seconds=int(time.monotonic() - job.start_mono))})")
            return True  # Success
        else:
            # Print a final error message
            print(f"\r{job.input_file}: Transcoding FAILED (Return Code: {job.return_code})")
            # In a real script, you'd save or display the full error output from stderr here.
            return False

    def get_job_progress(self, job):
        """ Get current progress of a job """
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
                at_seconds_formatted = job.trim0(str(timedelta(seconds=int(at_seconds))))
                at_formatted = f'At ~{at_seconds_formatted}/{job.total_duration_formatted}'
            else:
                at_formatted = f"Frame {frame_number}/{total_frames}"

            # Use the estimated FPS for the speed report
            progress_line = (
                f"{percent_complete:.1f}% | "
                f"{elapsed_time_formatted} | "
                f"-{remaining_time_formatted} | "
                f"~{speed} | "  # Report FPS clearly as Estimated
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
                    continue  # don't update progress crazy often
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

    def finish_transcode_job(self, success, job, is_allowed_codec_func):
        """
        Complete a transcoding job and handle file operations.

        Returns:
            probe: The probe of the transcoded file (or None if failed/dry_run)
        """

        def elaborate_err(vid):
            """
            Analyzes FFmpeg output using a severity scoring system to detect 
            severe stream corruption.
            """
            if vid.return_code != 0:
                CORRUPTION_SEVERITY = {
                    "corrupt decoded frame": 10,
                    "illegal mb_num": 9,
                    "marker does not match f_code": 9,
                    "damaged at": 8,
                    "Error at MB:": 7,
                    "time_increment_bits": 6,
                    "slice end not reached": 5,
                    "concealing": 2,  # Low weight to filter out minor issues
                }

                # Define the threshold for flagging the file as "CORRUPT"
                # 30-50 is a good starting point to confirm systemic failure.
                SEVERITY_THRESHOLD = 30
                total_severity = 0
                corruption_events = 0
                
                for line in vid.texts:
                    for signal, score in CORRUPTION_SEVERITY.items():
                        if signal in line:
                            total_severity += score
                            corruption_events += 1
                            # Stop checking other signals for this line once one is found (prevents double-counting)
                            break 
                
                if total_severity >= SEVERITY_THRESHOLD:
                    vid.texts.append(f"CORRUPT VIDEO: Total Severity Score {total_severity} "
                        f"from {corruption_events} events. FFmpeg error_code={vid.return_code}")


        def old_elaborate_err(vid):
            """ Analyzes FFmpeg output for signs of stream corruption and appends 
            a single, descriptive error line to vid.texts if detected.  """
            # 1. Define the specific error string to look for
            CORRUPT_FRAME_SIGNAL = "corrupt decoded frame"
            if vid.return_code != 0:
                corruption_count = 0
                for line in vid.texts:
                    if CORRUPT_FRAME_SIGNAL in line:
                        corruption_count += 1
                if corruption_count > 0:
                    vid.texts.append(f"CORRUPT VIDEO: {corruption_count} corrupt frames, "
                        f"FFmpeg error_code={vid.return_code}")

        ##################################
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
                    elaborate_err(vid)
            else:
                net = (vid.gb - probe.gb) / vid.gb
                net = int(round(-net*100))
                # space_saved_gb = vid.gb - probe.gb
            if is_allowed_codec_func(probe) and net > -self.opts.min_shrink_pct:
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
                    # Call FileOps.bulk_rename directly
                    vid.ops += FileOps.bulk_rename(basename, vid.standard_name, trashes, dry_run)

                if not dry_run:
                    # Apply preserved timestamps to the new file
                    FileOps.apply_timestamps(vid.standard_name, timestamps)

                    # Set basename1 for the successfully converted file
                    vid.basename1 = vid.standard_name
                    # probe will be returned to Converter for apply_probe

            except OSError as e:
                print(f"ERROR during swap of {vid.filepath}: {e}")
                print(f"Original: {job.orig_backup_file}, New: {job.temp_file}. Manual cleanup required.")
        elif success and self.opts.sample:
            # Set basename1 for the sample file
            vid.basename1 = job.temp_file
            # probe will be returned to Converter for apply_probe
        elif not success:
            # Transcoding failed, delete the temporary file
            if os.path.exists(job.temp_file):
                os.remove(job.temp_file)
                print(f"FFmpeg failed. Deleted incomplete {job.temp_file}.")
            self.probe_cache.set_anomaly(vid.filepath, 'Err')

        # Return probe for Converter to apply
        return probe
