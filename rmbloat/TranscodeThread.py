#!/usr/bin/env python3
""" TBD """
# pylint: disable=invalid-name,broad-exception-caught,multiple-statements
# pylint: disable=too-many-nested-blocks,too-many-instance-attributes
# pylint: disable=too-many-locals,consider-using-with,too-many-branches
# pylint: disable=too-many-arguments,too-many-positional-arguments
import fcntl
import os
import re
import subprocess
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

from . import Sandbox


class TranscodeThread(threading.Thread):
    """ TBD """
    # ffmpeg's machine-readable progress blocks always begin with one of these
    # keys, so a leftover fragment that starts this way carries no diagnostic
    # value and must not be reported as a log line.
    PROGRESS_PREFIX_RE = re.compile(
        r'^\s*(?:frame|fps|bitrate|total_size|out_time|out_time_ms|'
        r'out_time_us|dup_frames|drop_frames|speed|progress|size|time)=')

    # Bound retained ffmpeg stderr so corrupt media cannot exhaust memory.
    # When a limit is crossed the oldest ~25% is discarded (amortized O(1)).
    MAX_LOG_LINES = 2000
    MAX_LOG_CHARS = 512 * 1024
    LOG_TRIM_TARGET = 0.75
    # A single stderr "line" without a newline can also grow arbitrarily.
    MAX_MESSAGE_CHARS = 4096

    def __init__(self, cmd, run_info, job, progress_secs_max, temp_file=None,
                 env=None):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.info = run_info
        self.job = job
        self.progress_secs_max = progress_secs_max
        self.temp_file = temp_file
        self.env = env

        self.process: Optional[subprocess.Popen] = None
        self.progress_buffer = {}
        self._stop_event = threading.Event()

        # The TUI "Billboard"
        self.status_string = "Initializing..."
        self.is_finished = False
        self.last_activity_mono = time.monotonic()
        self.err_msg = None

    def run(self):
        """ Wrap the whole job for max safety """
        try:
            self.run_ffmpeg()
        except Exception as e:
            self.info.return_code = 127
            # Interpolate the exception itself.  The previous text embedded a
            # literal ``str(...)`` wrapper instead of calling it, so the log
            # read ``JobERR: str(boom)`` and the actual message was buried.
            self.status_string = f'JobERR: {e}'
        finally:
            self.is_finished = True

    def run_ffmpeg(self):
        """ thread main loop """
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=self.env,
                start_new_session=True,
            )
            # Set non-blocking
            fd = self.process.stderr.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        except Exception as e:
            self.info.return_code = 127
            self.status_string = f"Error: {e}"
            self.is_finished = True
            return

        # Added 'total_size' and 'out_time_ms' to filter out more bloat
#       PROGRESS_KEYS = {
#           b"frame", b"fps", b"bitrate", b"out_time", b"progress",
#           b"speed", b"total_size", b"out_time_ms", b"out_time_us"
#       }

        partial_line = b""
        while not self._stop_event.is_set():
            now = time.monotonic()
            chunk = None
            try:
                chunk = self.process.stderr.read()
            except (IOError, OSError):
                pass

            if chunk:
                self.last_activity_mono = now
                data = partial_line + chunk

                # Robust split: FFmpeg uses \r for progress and \n for logs.
                # Regex [ \r\n]+ handles any combination or repetition.
                fragments = re.split(b'[\r\n]+', data)
                partial_line = fragments[-1]

                for line_bytes in fragments[:-1]:
                    if not line_bytes:
                        continue

                    if b'=' in line_bytes:
                        parts = line_bytes.split(b'=', 1)
                        key_b = parts[0].strip()

                        # Process progress keys
                        if len(parts) == 2 and b' ' not in key_b:
                            key_str = key_b.decode('utf-8', errors='ignore')
                            val_str = parts[1].strip().decode('utf-8', errors='ignore')
                            self.progress_buffer[key_str] = val_str

                            if key_str == "progress":
                                ns = SimpleNamespace(**self.progress_buffer)
                                self.status_string = self._generate_display_string(ns)
                                self.progress_buffer = {}

                            continue # swallow anything key=value

                    # Real messages (headers, mappings, errors)
                    msg = line_bytes.decode('utf-8', errors='ignore').strip()
                    if msg:
                        self._record_text(msg)

                # Do not let a newline-less stream grow the partial buffer
                # without bound either.
                if len(partial_line) > self.MAX_MESSAGE_CHARS:
                    partial_line = partial_line[-self.MAX_MESSAGE_CHARS:]

            # Timeout Watchdog
            if now - self.last_activity_mono > self.progress_secs_max:
                self._record_text('PROGRESS TIMEOUT')
                self.abort(return_code=254)
                break

            if self.process.poll() is not None:
                self.info.return_code = self.process.returncode
                break

            if not chunk:
                time.sleep(0.10)

        # ffmpeg's failure reason is usually the last thing it writes, and the
        # read loop can reach EOF with that line still buffered because no
        # newline ever arrived.  Dropping it hides the most useful diagnostic
        # from the job log and from the retry ladder's error-signal search.
        self._flush_partial_line(partial_line)
        self.is_finished = True

    def _flush_partial_line(self, partial_line):
        """Record a final stderr fragment that arrived without a newline."""
        msg = partial_line.decode('utf-8', errors='ignore').strip()
        if not msg:
            return
        # A leftover progress fragment ("frame=  12 fps=0.0") is not a message.
        if self.PROGRESS_PREFIX_RE.match(msg):
            return
        self._record_text(msg)

    def _record_text(self, msg):
        """Append a stderr line, keeping ``info.texts`` within bounded size."""
        if len(msg) > self.MAX_MESSAGE_CHARS:
            msg = msg[:self.MAX_MESSAGE_CHARS] + '... [truncated]'

        texts = self.info.texts
        texts.append(msg)

        if (len(texts) <= self.MAX_LOG_LINES
                and sum(len(text) for text in texts) <= self.MAX_LOG_CHARS):
            return

        # Discard the oldest entries down to the trim targets in one slice so
        # steady-state appends stay cheap.
        target_lines = max(1, int(self.MAX_LOG_LINES * self.LOG_TRIM_TARGET))
        target_chars = max(1, int(self.MAX_LOG_CHARS * self.LOG_TRIM_TARGET))
        drop = max(0, len(texts) - target_lines)
        kept_chars = sum(len(text) for text in texts[drop:])
        while kept_chars > target_chars and drop < len(texts) - 1:
            kept_chars -= len(texts[drop])
            drop += 1
        if drop:
            del texts[:drop]

    def _generate_display_string(self, ns):
        """ Processes SimpleNamespace(frame, fps, bitrate, out_time, speed) """
        try:
            now_mono = time.monotonic()

            # Parse 'out_time' (00:00:00.000000)
            t_parts = ns.out_time.split(':')
            h = int(t_parts[0])
            m = int(t_parts[1])
            s_float = float(t_parts[2])
            time_encoded_seconds = h * 3600 + m * 60 + s_float

            elapsed_real = now_mono - self.job.start_mono
            avg_speed = time_encoded_seconds / elapsed_real if elapsed_real > 0.5 else 0.0

            if self.job.duration_secs > 0 and avg_speed > 0:
                percent = (time_encoded_seconds / self.job.duration_secs) * 100
                rem_secs = (self.job.duration_secs - time_encoded_seconds) / avg_speed
                remaining_str = self.job.trim0(str(timedelta(seconds=int(rem_secs))))
            else:
                percent, remaining_str = 0.0, "N/A"

            cur_time_str = self.job.trim0(str(timedelta(seconds=int(round(time_encoded_seconds)))))
            elapsed_str = self.job.trim0(str(timedelta(seconds=int(elapsed_real))))

            return (f"{percent:.1f}% {elapsed_str} -{remaining_str} "
                    f"{avg_speed:.1f}x At {cur_time_str}/{self.job.total_duration_formatted}")
        except Exception:
            # If parsing fails (e.g. out_time is malformed), keep last status
            return self.status_string

    def abort(self, return_code=255):
        """ Forcefully stop FFmpeg and delete the partial temp file """
        self._stop_event.set()

        if self.process and self.process.poll() is None:
            # Signal the whole process group: the direct child may be a
            # bwrap/systemd/docker wrapper with ffmpeg underneath it.
            Sandbox.terminate_process_group(self.process, grace=2.0)

        # Now that the process is dead and the handle is released:
        self.cleanup()
        self.info.return_code = return_code

    def cleanup(self):
        """ TBD """
        if self.temp_file and os.path.exists(self.temp_file):
            try:
                os.unlink(self.temp_file)
            except OSError:
                pass
        self.temp_file = None
