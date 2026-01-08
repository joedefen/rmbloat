#!/usr/bin/env python3
"""
Job handling for video conversion - manages transcoding jobs and progress monitoring
"""
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=broad-exception-caught,invalid-name
# pylint: disable=too-many-instance-attributes,no-else-return
# pylint: disable=line-too-long
import os
import re
import time
from datetime import timedelta
from pathlib import Path
import send2trash
from .Models import Job, Vid
from .ConvertUtils import bash_quote
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
        r"(?:.*?time[=\s]+(\d{2}):(\d{2}):(\d{2})\.(\d+))?"

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

    @staticmethod
    def should_use_10bit(pix_fmt, codec):
        """
        Determine whether to use 10-bit encoding based on input pixel format and codec.

        Args:
            pix_fmt: Input pixel format (e.g., 'yuv420p', 'yuv420p10le', 'p010le')
            codec: Input codec name

        Returns:
            bool: True if 10-bit encoding should be used

        Strategy:
            - Skip 10-bit for problematic codecs (mpeg4) - VAAPI compatibility issues
            - If source is already 10-bit → use 10-bit (preserve bit depth)
            - For standard 8-bit (yuv420p) → use 8-bit (VAAPI has issues with 8→10 bit conversion)
            - For exotic/problematic formats → use 8-bit for safety
        """
        # If source is already 10-bit, definitely use 10-bit output
        # (Even for problematic codecs - preserve the bit depth)
        if '10le' in pix_fmt or '10be' in pix_fmt or 'p010' in pix_fmt or 'p210' in pix_fmt:
            return True

        # Skip 10-bit for codecs known to have VAAPI compatibility issues with 8-bit sources
        # mpeg4 often has corrupt streams, dynamic resolution changes, and poor VAAPI support
        problematic_codecs = {'mpeg4'}
        if codec in problematic_codecs:
            return False

        # Standard 8-bit formats - keep as 8-bit to avoid VAAPI conversion issues
        safe_8bit_formats = {
            'yuv420p',    # Standard 8-bit 4:2:0
            'yuv422p',    # Standard 8-bit 4:2:2
            'yuv444p',    # Standard 8-bit 4:4:4
            'nv12',       # NVIDIA/Intel preferred format
            'nv21',       # Alternative NV format
        }

        if pix_fmt in safe_8bit_formats:
            return False  # Keep 8-bit sources as 8-bit (VAAPI has issues with 8->10 bit conversion)

        # For unknown or exotic formats, stay safe with 8-bit
        # This includes: yuvj420p (JPEG color range), bgr24, rgb24, etc.
        return False

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
        self.prev_ffmpeg_out_mono = 0

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

    @staticmethod
    def is_allowed_codec(opts, probe):
        """ Return whether the codec is 'allowed' """
        if not probe:
            return True
        if not re.match(r'^[a-z]\w*$', probe.codec, re.IGNORECASE):
            # if not a codec name (e.g., "---"), then it is OK
            # in the sense we will not choose it as an exception
            return True
        codec_ok = bool(opts.allowed_codecs == 'all')
        if opts.allowed_codecs == 'x265':
            codec_ok = bool(probe.codec in ('hevc',))
        if opts.allowed_codecs == 'x26*':
            codec_ok = bool(probe.codec in ('hevc','h264'))
        return codec_ok


    def check_job_status(self, job):
        """
        Monitors progress and handles the 'Retry Ladder' logic.
        Returns: (next_job, report_text, is_done)
        """
        got = self.get_job_progress(job)

        # 1. Job is still running (got is None or a progress string)
        if not isinstance(got, int):
            return job, got, False

        # 2. Job finished - check for retries
        return_code = got
        vid = job.vid
        vid.runs[-1].return_code = return_code

        # Tier 2: Retry with Error Tolerance
        if (return_code != 0 and
            not job.is_retry and not job.is_software_fallback and
            self._should_retry_with_error_tolerance(vid)):

            new_job = self.start_transcode_job(vid, retry_with_error_tolerance=True)
            return new_job, "RETRYING (Tolerant)...", False

        # Tier 3: Retry with Software
        if (return_code != 0 and
            job.is_retry and not job.is_software_fallback and
            self._should_retry_with_software(vid)):

            new_job = self.start_transcode_job(vid, retry_with_error_tolerance=True, force_software=True)
            return new_job, "RETRYING (Software)...", False

        # 3. Final Completion (No more retries)
        success = (return_code == 0)
        if success:
            status = 'OK3' if job.is_software_fallback else ('OK2' if job.is_retry else ' OK')
        else:
            status = 'ERR'

        return None, status, True

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

    def _should_retry_with_error_tolerance(self, vid):
        """
        Check if a failed job should be retried with error tolerance.
        Detects filter reinitialization errors and severe corruption.
        """
        if vid.runs[-1].return_code == 0:
            return False

        # Check for filter reinitialization error (the specific issue from the bug report)
        filter_error_signals = [
            "Error reinitializing filters",
            "Impossible to convert between the formats",
            "Reconfiguring filter graph because video parameters changed"
        ]

        for signal in filter_error_signals:
            for line in vid.runs[-1].texts:
                if signal in line:
                    return True

        # Also check for severe corruption (high severity score)
        corruption_signals = {
            "corrupt decoded frame": 10,
            "illegal mb_num": 9,
            "marker does not match f_code": 9,
        }

        total_severity = 0
        for line in vid.runs[-1].texts:
            for signal, score in corruption_signals.items():
                if signal in line:
                    total_severity += score
                    if total_severity >= 20:  # Quick exit if severe
                        return True
                    break

        return False

    def _should_retry_with_software(self, vid):
        """
        Check if a failed job should be retried with software encoding.
        Detects hardware-specific failures like filter reinitialization.
        """
        if vid.runs[-1].return_code == 0:
            return False

        # Check for filter reinitialization error (hardware can't handle dynamic changes)
        filter_reconfig_signals = [
            "Error reinitializing filters",
            "Impossible to convert between the formats",
        ]

        for signal in filter_reconfig_signals:
            for line in vid.runs[-1].texts:
                if signal in line:
                    return True

        return False

    def start_transcode_job(self, vid: Vid, retry_with_error_tolerance=False,
                            force_software=False):
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

        # Decide on 10-bit encoding based on input pixel format
        use_10bit = self.should_use_10bit(probe.pix_fmt, probe.codec)

        # Determine encoding strategy
        # If force_software is True, override chooser's acceleration setting
        original_use_acceleration = None
        if force_software:
            original_use_acceleration = self.chooser.use_acceleration
            self.chooser.use_acceleration = False


        # Store command for logging
        if not retry_with_error_tolerance and not force_software:
            vid.runs = []
        vid.start_new_run()
        if retry_with_error_tolerance and not force_software:
            vid.runs[-1].descr = 'redo w err tolerance'
        elif force_software:
            vid.runs[-1].descr = 'retry w S/W convert'
        else:
            vid.runs[-1].descr = 'initial run'


        job = Job(vid, orig_backup_file, temp_file, duration_secs)
        job.input_file = basename
        job.is_retry = retry_with_error_tolerance
        job.is_software_fallback = force_software

        # Create namespace with defaults
        params = self.chooser.make_namespace(
            input_file=job.input_file,
            output_file=job.temp_file,
            use_10bit=use_10bit,
            error_tolerant=retry_with_error_tolerance
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

#       # Check for external subtitle file
#       if merged_external_subtitle:
#           # Don't copy internal subtitles, we're replacing with external
#           map_copy += ' -0:s -map -0:t -map -0:d'
#       else:
#           # Copy internal subtitles, but drop unsafe ones (bitmap codecs like dvd_subtitle)
#           # Check if probe has custom instructions to drop specific subtitle streams
#           if probe.customs and 'drop_subs' in probe.customs:
#               # Map all subtitles first, then explicitly exclude the unsafe ones
#               map_copy += ' 0:s?'
#               for sub_idx in probe.customs['drop_subs']:
#                   map_copy += f' -map -0:s:{sub_idx}'
#               map_copy += ' -map -0:t -map -0:d'
#           else:
#               # No custom subtitle filtering needed
#               map_copy += ' 0:s? -map -0:t -map -0:d'
        # Expanded unsafe list for Blu-ray/DVD bitmap subs

        # Initialize map_opts as a list with video and audio defaults
        map_opts = ['-map', '0:v:0', '-map', '0:a?', '-c:a', 'copy']

        if merged_external_subtitle:
            # Drop all internal subs/tags to prepare for external merge
            map_opts.extend(['-map', '-0:s', '-map', '-0:t', '-map', '-0:d'])
        else:
            # 1. Start with the 'copy all subs' request (the ? makes it optional)
            map_opts.extend(['-map', '0:s?'])
            
            # 2. Iterate through known streams to surgically remove "unsafe" ones
            UNSAFE_SUBS = {'dvd_subtitle', 'pgs', 'hdmv_pgs_subtitle', 'dvbsub'}
            for idx, s in enumerate(probe.streams):
                if s.get('type') == 'subtitle' and s.get('codec') in UNSAFE_SUBS:
                    map_opts.extend(['-map', f'-0:s:{idx}'])
            
            # 3. Add safety nets and exclude attachments/data
            map_opts.extend(['-c:s', 'copy', '-map', '-0:t', '-map', '-0:d'])

        # No need to .split() since it is already a list
        params.map_opts = map_opts
        params.external_subtitle = merged_external_subtitle

        # Set subtitle codec to srt for MKV compatibility (transcodes mov_text, ass, etc.)
        # When external subtitle is used, FfmpegChooser handles the codec internally
        params.subtitle_codec = 'srt' if not merged_external_subtitle else 'copy'

        # For error-tolerant mode, add resolution to stabilize filter graph
        if retry_with_error_tolerance and not force_software:
            params.width = probe.width
            params.height = probe.height

        # Generate the command
        ffmpeg_cmd = self.chooser.make_ffmpeg_cmd(params)
        vid.runs[-1].command = bash_quote(ffmpeg_cmd)

        # Restore original acceleration setting if it was overridden
        if force_software and original_use_acceleration is not None:
            self.chooser.use_acceleration = original_use_acceleration

        # Start the job
        job.ffsubproc.start(ffmpeg_cmd, temp_file=job.temp_file)
        self.prev_ffmpeg_out_mono = self.progress_line_mono = time.monotonic()
        return job

    def get_job_progress(self, job):
        vid = job.vid
        secs_max = self.opts.progress_secs_max
        now_mono = time.monotonic()

        # 1. DRAIN THE BACKLOG (Prevent False Timeout)
        while len(job.ffsubproc.output_queue) > 1:
            discarded = job.ffsubproc.poll()
            if isinstance(discarded, str):
                self.prev_ffmpeg_out_mono = now_mono 
                if not discarded.startswith("PROGRESS:"):
                    vid.runs[-1].texts.append(discarded)
            elif isinstance(discarded, int):
                vid.runs[-1].return_code = discarded
                return discarded

        # 2. THE MONITORING LOOP
        while True:
            got = job.ffsubproc.poll()
            now_mono = time.monotonic()

            if isinstance(got, int):
                vid.runs[-1].return_code = got
                return got

            if isinstance(got, str):
                self.prev_ffmpeg_out_mono = now_mono 

                # Fast-forward: Skip old progress updates if we're backed up
                if len(job.ffsubproc.output_queue) > 0 and got.startswith("PROGRESS:"):
                    continue

                match = self.PROGRESS_RE.search(got)
                
                if not match:
                    # If it's a real log message, save it
                    if not got.startswith('PROGRESS:'):
                        vid.runs[-1].texts.append(got)
                    # If regex failed on a PROGRESS line, just wait for the next one
                    return None 

                # UI Throttle
                if now_mono - self.progress_line_mono < 1.8:
                    return None
                
                self.progress_line_mono = now_mono

                # Parsing
                groups = match.groups()
                try:
                    h, m, s = int(groups[1]), int(groups[2]), int(groups[3])
                    f_str = groups[4]
                    f_val = int(f_str) / (10 ** len(f_str))
                    time_encoded_seconds = h * 3600 + m * 60 + s + f_val
                except (ValueError, TypeError, IndexError):
                    # We dumped rough_progress, so we just return None if parsing fails
                    return None

                # 3. ACCURATE SPEED CALCULATION
                # We use the time encoded relative to the start
                elapsed_real = now_mono - job.start_mono
                if elapsed_real > 0.5 and time_encoded_seconds > 0:
                    avg_speed = time_encoded_seconds / elapsed_real
                else:
                    avg_speed = 0.0
                
                # 4. REMAINING TIME
                if job.duration_secs > 0 and avg_speed > 0:
                    percent_complete = (time_encoded_seconds / job.duration_secs) * 100
                    remaining_seconds = (job.duration_secs - time_encoded_seconds) / avg_speed
                    remaining_str = job.trim0(str(timedelta(seconds=int(remaining_seconds))))
                else:
                    percent_complete = 0.0
                    remaining_str = "N/A"

                # 5. FORMATTING
                cur_time_str = job.trim0(str(timedelta(seconds=int(round(time_encoded_seconds)))))
                elapsed_str = job.trim0(str(timedelta(seconds=int(elapsed_real))))
                
                return (
                    f"{percent_complete:.1f}% "
                    f"{elapsed_str} "
                    f"-{remaining_str} "
                    f"{avg_speed:.1f}x "
                    f"At {cur_time_str}/{job.total_duration_formatted}"
                )

            elif now_mono - self.prev_ffmpeg_out_mono > secs_max:
                vid.runs[-1].texts.append('PROGRESS TIMEOUT')
                job.ffsubproc.stop(return_code=254)
                self.prev_ffmpeg_out_mono = now_mono + 1_000_000
                return 254
            
            else:
                return None

    def finish_transcode_job(self, success, job):
        """
        Complete a transcoding job and handle file operations.

        Returns:
            probe: The probe of the transcoded file (or None if failed)
        """

        def elaborate_err(vid):
            """
            Analyzes FFmpeg output using a severity scoring system to detect
            severe stream corruption.
            """
            if vid.runs[-1].return_code != 0:
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
                last_score = 0

                for line in vid.runs[-1].texts:
                    this_line_signal_score = 0
                                    # 1. Check for signals
                    for signal, score in CORRUPTION_SEVERITY.items():
                        if signal in line:
                            total_severity += score
                            corruption_events += 1
                            this_line_signal_score = score
                            break 
                            
                    # 2. Check for repeats
                    repeat_match = re.search(r"Last message repeated (\d+) times", line)
                    if repeat_match:
                        multiplier = int(repeat_match.group(1))
                        # If last_score is 0, it means the repeated message 
                        # wasn't one of our corruption signals.
                        total_severity += (last_score * multiplier)
                        if last_score > 0:
                            corruption_events += multiplier
                        # A repeat line itself cannot be a signal for the NEXT line
                        this_line_signal_score = 0 

                    # 3. Update last_score for the NEXT iteration
                    # If the current line was neither a signal nor a repeat, 
                    # last_score becomes 0.
                    last_score = this_line_signal_score


                if total_severity >= SEVERITY_THRESHOLD:
                    vid.runs[-1].texts.append(f"CORRUPT VIDEO: Total Severity Score {total_severity} "
                        f"from {corruption_events} events. FFmpeg error_code={vid.return_code}")


        ##################################
        vid = job.vid
        probe = None
        # space_saved_gb = 0.0vid.net
        if success:
            probe = self.probe_cache.get(job.temp_file)
            if not probe:
                success = False
                vid.doit = 'ERR'
                elaborate_err(vid)
            else:
                # net is negative for shrink (e.g., -30)
                net = (probe.gb - vid.gb) / max(vid.gb,0.001)
                net = int(round(net * 100))
                vid.net = f'{net}%'

                # CHECK: Was the ORIGINAL file already a 'good' codec?
                original_was_allowed = self.is_allowed_codec(self.opts, vid.probe0)

                # If it was already a good codec, it MUST meet the shrink requirement
                if original_was_allowed and net > -self.opts.min_shrink_pct:
                    vid.ops.append(f"REJECTED: Already {vid.probe0.codec} and shrink ({net}%) not > -{self.opts.min_shrink_pct}%")
                    self.probe_cache.set_anomaly(vid.filepath, 'OPT')
                    success = False

        # Track auto mode vitals
        if self.auto_mode_enabled:
            if success and not self.opts.sample:
                self.ok_count += 1
                self.consecutive_failures = 0
            elif not success:
                self.error_count += 1
                self.consecutive_failures += 1

        if success and not self.opts.sample:
            trashes = set()
            basename = os.path.basename(vid.filepath)

            # Preserve timestamps from original file
            timestamps = None
            timestamps = FileOps.preserve_timestamps(basename)

            try:
                # Rename original to backup
                if self.opts.keep_backup:
                    os.rename(basename, job.orig_backup_file)
                    vid.ops.append(
                        f"rename {basename!r} {job.orig_backup_file!r}")
                else:
                    try:
                        send2trash.send2trash(basename)
                        trashes.add(basename)
                        vid.ops.append(f"trash {basename!r}")
                    except Exception as why:
                        vid.ops.append(f"ERROR during send2trash of {basename!r}: {why}")
                        vid.ops.append(f"ERROR: using os.unlink() instead")
                        os.unlink(basename)
                        trashes.add(basename)
                        vid.ops.append(f"unlink {basename!r}")

                # Rename temporary file to the original filename
                os.rename(job.temp_file, vid.standard_name)
                vid.ops.append(
                    f"rename {job.temp_file!r} {vid.standard_name!r}")

                if vid.do_rename:
                    # Call FileOps.bulk_rename directly
                    vid.ops += FileOps.bulk_rename(basename, vid.standard_name, trashes)

                # Apply preserved timestamps to the new file
                FileOps.apply_timestamps(vid.standard_name, timestamps)

                # Set basename1 for the successfully converted file
                vid.basename1 = vid.standard_name
                # probe will be returned to Converter for apply_probe

            except OSError as e:
                vid.ops.append(f"ERROR during swap of {vid.filepath}: {e}")
                vid.ops.append(f"Original: {job.orig_backup_file}, New: {job.temp_file}. Manual cleanup required.")
        elif success and self.opts.sample:
            # Set basename1 for the sample file
            vid.basename1 = job.temp_file
            # probe will be returned to Converter for apply_probe
        elif not success:
            # Transcoding failed or was rejected
            if os.path.exists(job.temp_file):
                os.remove(job.temp_file)
                # Ensure we log WHY it was deleted if it hasn't been logged yet
                if not vid.ops:
                    vid.ops.append(f"CLEANUP: Deleted {job.temp_file} - FFmpeg failed or quality check rejected file.")
            self.probe_cache.set_anomaly(vid.filepath, 'Err')

        # Return probe for Converter to apply
        return probe
