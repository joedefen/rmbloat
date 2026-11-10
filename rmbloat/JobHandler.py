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
import uuid
from pathlib import Path
import send2trash
from .Models import Job, Vid
from .ConvertUtils import bash_quote
from . import FileOps
from .TranscodeThread import TranscodeThread


def _unique_path(path):
    """Return *path*, or a numbered variant if that name is already taken."""
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path)
    for index in range(1, 10000):
        candidate = f"{root}.{index}{ext}"
        if not os.path.lexists(candidate):
            return candidate
    raise FileExistsError(f"could not find a free filename for {path!r}")


def emergency_cleanup(self):
    """Call this on crash or KeyboardInterrupt"""
    if self.temp_file:
        if os.path.exists(self.temp_file):
            print(f"Cleaning up orphaned temp file: {self.temp_file}")
            os.unlink(self.temp_file)

class JobHandler:
    """Handles video transcoding job execution and monitoring"""

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
        self.temp_file = None # her, for easy atexit cleanup

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
        success = bool(return_code == 0)
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
        DEFAULT_TRC = 'bt709'  # x265 requires 'bt709' format

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
        """Start a transcoding job using the Threaded TranscodeThread."""
        os.chdir(os.path.dirname(vid.filepath))
        basename = os.path.basename(vid.filepath)
        probe = vid.probe0

        # ... [Subtitle and Path Logic] ...
        merged_external_subtitle = None
        if self.opts.merge_subtitles:
            subtitle_path = Path(vid.filepath).with_suffix('.en.srt')
            if subtitle_path.exists() and self.validate_srt_file(subtitle_path):
                merged_external_subtitle = str(subtitle_path)
                vid.standard_name = str(Path(vid.standard_name).with_suffix('.sb.mkv'))

        # Samples are written next to the source file (we chdir'd there above)
        # so --sample works on any machine instead of relying on a developer's
        # hardcoded /heap/samples directory.
        prefix = f'SAMPLE.{self.opts.quality}' if self.opts.sample else 'TEMP'
        self.temp_file = temp_file = f"{prefix}.{vid.standard_name}"
        orig_backup_file = f"ORIG.{basename}"

        if os.path.exists(temp_file):
            os.unlink(temp_file)

        duration_secs = self.sample_seconds if self.opts.sample else probe.duration

        # 1. Initialize the Run and the Job
        if not retry_with_error_tolerance and not force_software:
            vid.runs = []

        vid.start_new_run()
        run = vid.runs[-1]

        if retry_with_error_tolerance and not force_software:
            run.descr = 'redo w err tolerance'
        elif force_software:
            run.descr = 'retry w S/W convert'
        else:
            run.descr = 'initial run'

        job = Job(vid, orig_backup_file, temp_file, duration_secs, self.opts)
        job.is_retry = retry_with_error_tolerance
        job.is_software_fallback = force_software

        # 2. Determine encoding strategy
        original_use_acceleration = None
        if force_software:
            # We temporarily force the chooser to software mode
            original_use_acceleration = self.chooser.use_acceleration
            self.chooser.use_acceleration = False

        # 3. Build FFmpeg Parameters
        params = self.chooser.make_namespace(
            input_file=basename,
            output_file=temp_file,
            use_10bit=self.should_use_10bit(probe.pix_fmt, probe.codec),
            error_tolerant=retry_with_error_tolerance
        )

        # REQUIRED: Pass the streams so the mapper can find unsafe subtitles
        params.streams = probe.streams
        # REQUIRED: Pass height so the quality calculation works
        params.height = probe.height

        params.crf = self.opts.quality
        params.use_nice_ionice = not self.opts.full_speed
        params.thread_count = self.opts.thread_cnt

        if self.opts.sample:
            params.sample_mode = True
            # Seek 20% into the movie (but at least two minutes in) so the
            # clip is representative. probe.duration is the *full* length;
            # job.duration_secs is only the sample length while sampling.
            start_secs = max(120, probe.duration) * 0.20
            params.pre_input_opts = ['-ss', job.duration_spec(start_secs)]
            params.post_input_opts = ['-t', str(self.sample_seconds)]

        # Scaling and Color
        if probe.height > self.opts.max_height:
            params.target_width = self.opts.max_height * probe.width // probe.height
            params.target_height = self.opts.max_height
        else:
            params.target_width = None
            params.target_height = None

        params.color_opts = self.make_color_opts(probe.color_spt)
        params.external_subtitle = merged_external_subtitle
        # Copy a merged external SRT as-is; otherwise re-encode the internal
        # text subtitles that survived the mapping to SRT.
        params.subtitle_codec = 'srt' if not merged_external_subtitle else 'copy'

        # 4. Finalize Command and Launch Thread
        # All the "Magic" (Scaling filters, Subtitle pruning, HW vs SW mapping)
        # now happens inside this one call.
        ffmpeg_cmd = self.chooser.make_ffmpeg_cmd(params)
        run.command = bash_quote(ffmpeg_cmd)

        # Restore acceleration state if we changed it for a retry
        if force_software and original_use_acceleration is not None:
            self.chooser.use_acceleration = original_use_acceleration

        job.thread = TranscodeThread(cmd=ffmpeg_cmd, run_info=run, job=job,
                    progress_secs_max=self.opts.progress_secs_max, temp_file=temp_file)
        job.thread.start()

        return job

    def get_job_progress(self, job):
        """ If the thread says it's done, return the return code (the 'int') """
        if job.thread.is_finished:
            return job.thread.info.return_code
        if not job.thread.is_alive():
            job.thread.info.return_code = 999
            return job.thread.info.return_code

        # Otherwise, just return the string the thread has prepared for us
        return job.thread.status_string

    def finish_transcode_job(self, job):
        """
        Finalizes the job after the TranscodeThread thread completes.
        Replaces the old 'success' argument by checking the thread's return_code.
        """
        # 1. Ensure the thread is dead before we reap it
        if job.thread.is_alive():
            job.thread.join(timeout=2)

        vid = job.vid
        # The thread has already populated vid.runs[-1] with the return_code and texts.
        run = vid.runs[-1]

        # Determine success based on FFmpeg return code
        success = bool(run.return_code == 0)

        # 2. Analyze errors if it failed (or even if it succeeded, to find corruption)
        self.elaborate_err(vid)

        probe = None
        rejected = False
        if success:
            # Check if the file actually exists and is valid
            probe = self.probe_cache.get(job.temp_file)
            if not probe:
                success = False
                vid.doit = 'ERR'
                run.texts.append("ERROR: FFmpeg returned 0 but output file is un-probable.")
            else:
                # Calculate shrink metrics
                net_ratio = (probe.gb - vid.gb) / max(vid.gb, 0.001)
                net_pct = int(round(net_ratio * 100))
                vid.net = f'{net_pct}%'

                # Minimum-shrink guard. This applies to *every* transcode, not
                # only ones whose original codec is already allowed: a
                # disallowed original codec is not a licence to replace a file
                # with a larger one. A rejection is NOT an error - it means the
                # source is already optimally sized - so keep the 'OPT' anomaly
                # (do not let the failure path overwrite it) and leave the
                # original file untouched.
                if net_pct > -self.opts.min_shrink_pct:
                    vid.ops.append(
                        f"REJECTED: shrink ({net_pct}%) not > "
                        f"-{self.opts.min_shrink_pct}% (original "
                        f"{vid.probe0.codec})")
                    self.probe_cache.set_anomaly(vid.filepath, 'OPT')
                    rejected = True
                    success = False
                    vid.doit = 'OPT'
                    probe = None  # the temp output will be deleted below

        # 3. File Swaps or Cleanup
        # A failed swap means the transcode itself was fine but we could not
        # safely install the result. Never report that as success, and never
        # delete the temp output in that case (it may be the only copy of the
        # newly encoded data).
        swap_ok = True
        if success and not self.opts.sample:
            swap_ok = self._execute_file_swap(vid, job)
            if not swap_ok:
                success = False
                vid.doit = 'ERR'
        elif success and self.opts.sample:
            # Keep only the filename: the UI shows basename1 in place of the
            # source name, and temp_file is relative to the video's directory.
            vid.basename1 = os.path.basename(job.temp_file)

        # 4. Handle Vitals for Auto Mode
        if self.auto_mode_enabled:
            if success and not self.opts.sample:
                self.ok_count += 1
                self.consecutive_failures = 0
            elif not success and not rejected:
                # A rejected job is a successful transcode whose output was
                # not worth keeping, so it must not trip the failure counters
                # (otherwise a run of already-optimal files would stop auto
                # mode via the "10 consecutive failures" guard).
                self.error_count += 1
                self.consecutive_failures += 1

        if not success and swap_ok:
            if os.path.exists(job.temp_file):
                os.remove(job.temp_file)
                if not vid.ops:
                    vid.ops.append(f"CLEANUP: Deleted {job.temp_file} - Job failed or rejected.")
            # Never overwrite the 'OPT' anomaly recorded for a rejection.
            if not rejected:
                self.probe_cache.set_anomaly(vid.filepath, 'Err')

        if not swap_ok:
            probe = None

        self.temp_file = None # so ataxit does not see it

        return probe

    def elaborate_err(self, vid):
        """ Analyzes vid.runs[-1].texts for corruption signals. """
        run = vid.runs[-1]
        CORRUPTION_SEVERITY = {
            "corrupt decoded frame": 10, "illegal mb_num": 9,
            "marker does not match f_code": 9, "damaged at": 8,
            "Error at MB:": 7, "time_increment_bits": 6,
            "slice end not reached": 5, "concealing": 2,
        }
        SEVERITY_THRESHOLD = 30
        total_severity = 0
        corruption_events = 0
        last_score = 0

        for line in run.texts:
            this_line_signal_score = 0
            for signal, score in CORRUPTION_SEVERITY.items():
                if signal in line:
                    total_severity += score
                    corruption_events += 1
                    this_line_signal_score = score
                    break

            repeat_match = re.search(r"Last message repeated (\d+) times", line)
            if repeat_match:
                multiplier = int(repeat_match.group(1))
                total_severity += (last_score * multiplier)
                if last_score > 0:
                    corruption_events += multiplier
                this_line_signal_score = 0

            last_score = this_line_signal_score

        if total_severity >= SEVERITY_THRESHOLD:
            run.texts.append(f"CORRUPT VIDEO: Severity {total_severity} ({corruption_events} events).")


    def _execute_file_swap(self, vid: Vid, job: Job):
        """
        Handles the actual file system dance:
        Original -> ORIG backup/Trash
        TEMP -> Original Filename

        The operation is transactional: the original is moved aside first and
        restored if the new file cannot be installed. Returns True only when
        the output is safely in place. A failed swap leaves the original (or a
        uniquely named rollback copy) and the transcoded temp file intact.
        """
        def finalize_standard_name(vid, job):
            """
            Surgically updates the filename based on the final command used.
            Replaces .cmfXX. with .qpYY. or .crfZZ.
            """
            run = vid.runs[-1]
            cmd = run.command or ""

            # 1. Identify what was actually used in the command
            # We look for the 'value' following the quality flag
            actual_val = ""
            new_tag = ""

            if "-qp " in cmd:
                match = re.search(r"-qp\s+(\d+)", cmd)
                if match:
                    actual_val = match.group(1)
                    new_tag = f".qp{actual_val}."
            elif "-crf " in cmd:
                match = re.search(r"-crf\s+(\d+)", cmd)
                if match:
                    actual_val = match.group(1)
                    new_tag = f".crf{actual_val}."

            if not new_tag: # If we couldn't find it in the command, use a safe fallback
                prefix = "ql" if job.is_software_fallback else "cmf"
                new_tag = f".{prefix}{self.opts.quality}."

            placeholder = f".cmf{self.opts.quality}." # surgical replacement
            if placeholder in vid.standard_name:
                vid.standard_name = vid.standard_name.replace(placeholder, new_tag)

        def restore_original(rollback_file, basename):
            """Put the original back in place after a failed installation."""
            try:
                os.rename(rollback_file, basename)
                vid.ops.append(f"ROLLBACK: restored {rollback_file!r} to {basename!r}")
            except OSError as why:
                vid.ops.append(
                    f"CRITICAL: could not restore original from {rollback_file!r}: {why}")

        #######################

        basename = os.path.basename(vid.filepath)

        # 1. Preserve timestamps from the original file
        timestamps = FileOps.preserve_timestamps(basename)

        # 2. Verify the output *before* touching the original. A zero-byte or
        #    missing temp file must never cost us the original.
        if not os.path.isfile(job.temp_file):
            vid.ops.append(f"ERROR: transcoded temp file missing: {job.temp_file!r}")
            return False
        try:
            if os.path.getsize(job.temp_file) <= 0:
                vid.ops.append(f"ERROR: transcoded temp file is empty: {job.temp_file!r}")
                return False
        except OSError as e:
            vid.ops.append(f"ERROR: cannot inspect temp file {job.temp_file!r}: {e}")
            return False

        # 3. Compute the final name and refuse to overwrite a pre-existing file.
        finalize_standard_name(vid, job)
        final_name = vid.standard_name
        same_path = os.path.abspath(final_name) == os.path.abspath(basename)

        if not same_path and os.path.lexists(final_name):
            vid.ops.append(
                f"ERROR: refusing to overwrite existing destination {final_name!r}")
            return False

        # 4. Move the original aside. This is our rollback record and happens
        #    before the new file is installed, so a later failure cannot lose it.
        try:
            if self.opts.keep_backup:
                rollback_file = _unique_path(job.orig_backup_file)
            else:
                rollback_file = _unique_path(
                    f".rmbloat-rollback-{uuid.uuid4().hex}.{basename}")
            os.rename(basename, rollback_file)
            vid.ops.append(f"rename {basename!r} {rollback_file!r}")
        except OSError as e:
            vid.ops.append(f"ERROR: could not move original {basename!r} aside: {e}")
            return False

        # 5. Install the transcoded file. Prefer a hard link so an existing
        #    destination can never be silently replaced (os.rename overwrites
        #    on POSIX). Fall back to rename on filesystems without hard links;
        #    the destination was already checked above.
        try:
            installed_via_link = True
            try:
                os.link(job.temp_file, final_name)
            except FileExistsError:
                raise
            except (OSError, NotImplementedError):
                installed_via_link = False

            if installed_via_link:
                vid.ops.append(f"link {job.temp_file!r} {final_name!r}")
                try:
                    os.unlink(job.temp_file)
                except OSError as why:
                    vid.ops.append(
                        f"WARNING: could not remove temp name {job.temp_file!r}: {why}")
            else:
                os.rename(job.temp_file, final_name)
                vid.ops.append(f"rename {job.temp_file!r} {final_name!r}")
        except FileExistsError:
            restore_original(rollback_file, basename)
            vid.ops.append(
                f"ERROR: destination {final_name!r} appeared during the swap; refusing to overwrite")
            vid.ops.append(f"Transcoded output kept at {job.temp_file!r}")
            return False
        except OSError as e:
            restore_original(rollback_file, basename)
            vid.ops.append(f"ERROR during swap of {vid.filepath}: {e}")
            vid.ops.append(
                f"Transcoded output kept at {job.temp_file!r}; original preserved")
            return False

        # 6. The destination is safely in place; now dispose of the original.
        if self.opts.keep_backup:
            if rollback_file != job.orig_backup_file:
                vid.ops.append(
                    f"NOTE: backup already existed; saved original as {rollback_file!r}")
        elif self.opts.use_trash:
            try:
                send2trash.send2trash(rollback_file)
                vid.ops.append(f"trash {rollback_file!r}")
            except Exception as why:
                # Never permanently delete a file after the user asked for the
                # trash. Keep the rollback copy and make the problem visible.
                vid.ops.append(f"ERROR during send2trash of {rollback_file!r}: {why}")
                vid.ops.append(
                    f"Original preserved at {rollback_file!r}; not permanently deleted")
        else:
            try:
                os.unlink(rollback_file)
                vid.ops.append(f"unlink {rollback_file!r}")
            except OSError as why:
                vid.ops.append(f"ERROR unlinking original {rollback_file!r}: {why}")
                vid.ops.append(f"Original preserved at {rollback_file!r}")

        # 7. Move probe cache entry to final path so it doesn't need re-probing
        try:
            final_path = os.path.abspath(final_name)
            self.probe_cache.move_entry(job.temp_file, final_path)
        except Exception as why:
            vid.ops.append(f"WARNING: could not move probe-cache entry: {why}")

        # 8. Handle bulk rename if needed (e.g., matching subtitle files)
        if vid.do_rename:
            trashes = {os.path.basename(rollback_file)}
            vid.ops += FileOps.bulk_rename(basename, final_name, trashes)

        # 9. Restore original timestamps to the new file
        if not FileOps.apply_timestamps(final_name, timestamps):
            vid.ops.append(f"WARNING: could not restore timestamps on {final_name!r}")

        # 10. Update Vid object for UI reference
        vid.basename1 = final_name
        return True
