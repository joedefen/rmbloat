#!/usr/bin/env python3
import os
import fcntl
import subprocess
from typing import Optional, Union

class FfmpegMon:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.partial_line: bytes = b""
        self.output_queue: list[str] = []
        self.return_code: Optional[int] = None
        self.temp_file = None
        # The Accumulator: stores keys until 'progress=' arrives
        self.progress_buffer: dict[str, str] = {}

    def start(self, command_line: list[str], temp_file: Optional[str] = None) -> None:
        self.temp_file = temp_file
        if self.process:
            raise RuntimeError("FfmpegMon is already monitoring a process.")
        try:
            self.process = subprocess.Popen(
                command_line,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0
            )
            fd = self.process.stderr.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        except Exception as e:
            self.return_code = 127

    def poll(self) -> Union[Optional[int], str]:
        if self.output_queue:
            return self.output_queue.pop(0)

        if not self.process:
            return self.return_code

        try:
            chunk = self.process.stderr.read()
        except (IOError, OSError):
            chunk = None

        if chunk:
            data = self.partial_line + chunk
            fragments = data.split(b'\n')
            self.partial_line = fragments[-1]

            PROGRESS_KEYS = {
                b"frame", b"fps", b"stream_0_0_q", b"bitrate", b"total_size", 
                b"out_time_us", b"out_time", b"dup_frames", b"drop_frames", 
                b"speed", b"progress", b"out_time_ms"
            }

            for line_bytes in fragments[:-1]:
                if not line_bytes:
                    continue
                
                # Check for progress key=value
                if b'=' in line_bytes:
                    parts = line_bytes.split(b'=', 1)
                    key_b = parts[0].strip()
                    if key_b in PROGRESS_KEYS:
                        key_str = key_b.decode('utf-8')
                        val_str = parts[1].strip().decode('utf-8', errors='ignore')
                        
                        self.progress_buffer[key_str] = val_str
                        
                        # Once we see the 'progress' key, the block is finished
                        if key_str == "progress":
                            # Construct a string compatible with your PROGRESS_RE
                            # Note: out_time=00:00:00.000000
                            b = self.progress_buffer
                            composite = (
                                f"frame={b.get('frame', '0')} "
                                f"fps={b.get('fps', '0')} "
                                f"bitrate={b.get('bitrate', '0')} "
                                f"time={b.get('out_time', '00:00:00.00')} "
                                f"speed={b.get('speed', '0')}x"
                            )
                            self.output_queue.append(f"PROGRESS:{composite}")
                            self.progress_buffer = {} # Reset accumulator
                        continue
                
                # If it's not a progress key, it's a real log message (errors, etc.)
                self.output_queue.append(line_bytes.decode('utf-8', errors='ignore').strip())

        # Handle Termination
        process_status = self.process.poll()
        if process_status is not None:
            if self.partial_line:
                l_str = self.partial_line.decode('utf-8', errors='ignore').strip()
                if l_str: self.output_queue.append(l_str)
                self.partial_line = b""

            if self.output_queue:
                self.return_code = process_status
                return self.output_queue.pop(0)

            self.process = None
            self.return_code = process_status
            return self.return_code

        return None

    def stop(self, return_code=255):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=15) 
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait()
                except:
                    pass 

        if self.temp_file and os.path.exists(self.temp_file):
            try:
                os.unlink(self.temp_file)
            except OSError:
                pass

        self.temp_file = None
        self.process = None
        self.partial_line = b""
        self.progress_buffer = {}
        self.return_code = return_code

    def __del__(self):
        self.stop()
