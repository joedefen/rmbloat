#!/usr/bin/env python3
""" TBD """
# pylint: disable=invalid-name,broad-exception-caught,multiple-statements
# pylint: disable=too-many-nested-blocks,too-many-instance-attributes
# pylint: disable=too-many-locals,consider-using-with
import os
import fcntl
import subprocess
import threading
import time
from types import SimpleNamespace
from datetime import timedelta
from typing import Optional


class FfmpegMon(threading.Thread):
    """ TBD """
    def __init__(self, cmd, run_info, job, progress_secs_max, temp_file=None):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.info = run_info
        self.job = job
        self.progress_secs_max = progress_secs_max
        self.temp_file = temp_file

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
            self.status_string = f'JobERR: str(e)'
        finally:
            self.is_finished = True

    def run_ffmpeg(self):
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0
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

        PROGRESS_KEYS = {
            b"frame", b"fps", b"bitrate", b"out_time", b"speed", b"progress"
        }

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
                fragments = data.split(b'\n')
                partial_line = fragments[-1]

                for line_bytes in fragments[:-1]:
                    if not line_bytes: continue

                    if b'=' in line_bytes:
                        parts = line_bytes.split(b'=', 1)
                        key_b = parts[0].strip()
                        if key_b in PROGRESS_KEYS:
                            key_str = key_b.decode('utf-8')
                            val_str = parts[1].strip().decode('utf-8', errors='ignore')
                            self.progress_buffer[key_str] = val_str

                            if key_str == "progress":
                                # Pass the dict as a Namespace for clean attribute access
                                ns = SimpleNamespace(**self.progress_buffer)
                                self.status_string = self._generate_display_string(ns)
                                self.progress_buffer = {}
                            continue

                    self.info.texts.append(line_bytes.decode('utf-8', errors='ignore').strip())

            # Timeout Watchdog
            if now - self.last_activity_mono > self.progress_secs_max:
                self.info.texts.append('PROGRESS TIMEOUT')
                self.stop(return_code=254)
                break

            if self.process.poll() is not None:
                self.info.return_code = self.process.returncode
                break

            if not chunk: time.sleep(0.10)

        self.is_finished = True

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

    def stop(self, return_code=255):  # TODO: rename abort
        """ Forcefully stop FFmpeg and delete the partial temp file """
        self._stop_event.set()
        
        if self.process and self.process.poll() is None:
            try:
                # Send SIGTERM
                self.process.terminate()
                # Give it a moment to release the file handle
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # If it's being stubborn, SIGKILL
                self.process.kill()
            except Exception:
                pass

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