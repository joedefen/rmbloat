#!/usr/bin/env python3
""" TBD """
import json
import os
import sys
import subprocess
import math
import re
import fcntl
import atexit
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, Union, List
from threading import Lock
from concurrent.futures import ThreadPoolExecutor # <-- NEW IMPORT
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=too-many-return-statements,too-many-statements

_dataclass_kwargs = {'slots': True} if sys.version_info >= (3, 10) else {}

@dataclass(**_dataclass_kwargs)
class Probe:
    """Video metadata probe results"""
    # Fields stored on disk
    anomaly: Optional[str] = None
    customs: Optional[Dict[str, Any]] = None
    width: int = 0
    height: int = 0
    codec: str = '---'
    color_spt: str = 'unknown,~,~'
    pix_fmt: str = 'unknown'
    bitrate: int = 0
    duration: float = 0.0
    fps: float = 0.0
    size_bytes: int = 0

    # Computed fields (not stored on disk)
    bloat: int = field(default=0, init=False)
    gb: float = field(default=0.0, init=False)

class ProbeCache:
    """ TBD """
    disk_fields = set('anomaly width height codec bitrate fps duration size_bytes color_spt customs pix_fmt'.split())

    """ TBD """
    def __init__(self, cache_file_name="video_probes.json", cache_dir_name="/tmp", chooser=None):
        self.cache_path = os.path.join(cache_dir_name, cache_file_name)
        self.lock_path = self.cache_path + ".lock"
        self.cache_data: Dict[str, Any] = {}
        self._dirty_count = 0
        self._cache_lock = Lock() # NEW: import Lock from threading
        self.chooser = chooser  # FfmpegChooser instance for building ffprobe commands
        self._lock_file = None

        # Acquire single-instance lock
        self._acquire_instance_lock()

        self.load()

    # --- Utility Methods ---

    def _acquire_instance_lock(self):
        """Acquire exclusive lock - only one rmbloat instance can run"""
        try:
            self._lock_file = open(self.lock_path, 'w')
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Register cleanup on exit
            atexit.register(self._release_instance_lock)
            # Ensure cache is saved on exit
            atexit.register(self.store)
        except IOError:
            print(f"ERROR: Another rmbloat instance is already running (lock: {self.lock_path})")
            sys.exit(1)

    def _release_instance_lock(self):
        """Release the instance lock"""
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
                if os.path.exists(self.lock_path):
                    os.remove(self.lock_path)
            except Exception:
                pass

    @staticmethod
    def _get_file_size_info(filepath: str) -> Optional[Dict[str, Union[int, float]]]:
        """Gets the size of a file in bytes storage."""
        try:
            size_bytes = os.path.getsize(filepath)
            return {
                'size_bytes': size_bytes,
            }
        except Exception:
            return None

    def _get_metadata_with_ffprobe(self, file_path: str) -> Optional[Probe]:
        """
        Extracts video metadata using ffprobe and creates a Probe object.
        """
        # --- START COMPACT COLOR PARAMETER EXTRACTION ---

        def get_color_spt(): # compact color spec
            nonlocal video_stream
                # 1. Load the three color fields, defaulting missing fields to 'unknown'
            colorspace = video_stream.get('color_space', 'unknown')
            color_primaries = video_stream.get('color_primaries', 'unknown')
            color_trc = video_stream.get('color_transfer', 'unknown')
                # 2. Build the compact list using the placeholder '~'
            parts = [colorspace] # Space is always the first part
            if color_primaries != colorspace:
                parts.append(color_primaries)
            else:
                parts.append("~") # Placeholder if Primaries == Space
            if color_trc != color_primaries:
                parts.append(color_trc)
            else:
                parts.append("~") # Placeholder if TRC == Primaries
            # 3. Store the compact, comma-separated string
            # Example: A:A:B becomes "A,~,B"
            return ",".join(parts)

        # --- END COMPACT COLOR PARAMETER EXTRACTION ---
        if not os.path.exists(file_path):
            print(f"Error: File not found at '{file_path}'")
            return None

        # Build ffprobe command using chooser if available, otherwise fall back to system ffprobe
        if self.chooser:
            command = self.chooser.make_ffprobe_cmd(
                file_path,
                '-v', 'error',
                '-print_format', 'json',
                '-show_format',
                '-show_streams'
            )
        else:
            command = [
                'ffprobe', '-v', 'error', '-print_format', 'json',
                '-show_format', '-show_streams', file_path
            ]

        try:
            # Added timeout and improved error handling for subprocess
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=30 # Add a timeout to prevent hanging
            )

            metadata = json.loads(result.stdout)

            video_stream = next((s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'), None)

            if not video_stream or not metadata.get("format"):
                print(f"Error: ffprobe output missing critical stream/format data for '{file_path}'.")
                return None

            meta = Probe(
                width=int(video_stream.get('width', 0)),
                height=int(video_stream.get('height', 0)),
                codec=video_stream.get('codec_name', '---'),
                color_spt=get_color_spt(),
                pix_fmt=video_stream.get('pix_fmt', 'unknown'),
                bitrate=int(int(metadata["format"].get('bit_rate', '0'))/1000),
                duration=float(metadata["format"].get('duration', 0.0)),
            )

            # Detect unsafe subtitle streams (bitmap codecs that cause conversion failures)
            # Safe text-based codecs: subrip, ass, ssa, mov_text, webvtt, text
            # Everything else (dvd_subtitle, hdmv_pgs_subtitle, etc.) should be dropped
            safe_subtitle_codecs = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
            subtitle_streams = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'subtitle']

            if subtitle_streams:
                drop_indices = []
                for idx, sub_stream in enumerate(subtitle_streams):
                    codec = sub_stream.get('codec_name', '')
                    if codec and codec not in safe_subtitle_codecs:
                        drop_indices.append(idx)

                if drop_indices:
                    meta.customs = {'drop_subs': drop_indices}

            # 1. Get the Raw Frame Rate String (r_frame_rate preferred)
            fps_str = video_stream.get('r_frame_rate') or video_stream.get('avg_frame_rate', '0/0')
            meta.fps = 0.0
            try:
                # The format is typically a fraction like "30000/1001"
                num, den = map(int, fps_str.split('/'))
                if den > 0:
                    # Calculate the float and immediately round to 3 decimal places
                    full_fps = float(num) / float(den)
                    meta.fps = round(full_fps, 3) # <-- ROUNDING HERE

            except Exception:
                # Handle cases where fps_str is non-standard
                pass

            size_info = self._get_file_size_info(file_path)
            if size_info is None:
                raise IOError("Failed to get file size after probe.")

            meta.size_bytes = size_info['size_bytes']
            return meta

        except subprocess.CalledProcessError as e:
            # print(f"Error executing ffprobe: {e.stderr}")
            # Increment probe failure counter and return placeholder
            return self._increment_probe_failure(file_path)
        except json.JSONDecodeError:
            print(f"Error: Failed to decode ffprobe JSON output for '{file_path}'.")
            return self._increment_probe_failure(file_path)
        except FileNotFoundError:
            print("Error: The 'ffprobe' command was not found. Is FFmpeg installed and in your PATH?")
            return self._increment_probe_failure(file_path)
        except IOError as e:
            print(f"File size error: {e}")
            return self._increment_probe_failure(file_path)


    # --- Cache Management Methods ---

    def _increment_probe_failure(self, filepath: str) -> Probe:
        """Increment the probe failure counter (?P1 -> ?P2 -> ... -> ?P9)

        Returns:
            Probe with placeholder values and incremented ?Pn anomaly
        """
        # Check if we have a cached entry with existing probe failure
        cached_data = self.cache_data.get(filepath, {})
        current_anomaly = cached_data.get('anomaly', None)

        # Determine the new probe failure number
        if current_anomaly and current_anomaly.startswith('?P'):
            # Extract current number and increment
            try:
                num = int(current_anomaly[2])  # Get digit after '?P'
                new_num = min(num + 1, 9)  # Cap at 9
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1

        new_anomaly = f'?P{new_num}'

        # Get actual file size for cache validation
        size_info = self._get_file_size_info(filepath)
        actual_size = size_info['size_bytes'] if size_info else 0

        # Create a minimal probe object with placeholders
        meta = Probe(
            anomaly=new_anomaly,
            size_bytes=actual_size  # Use actual file size for cache validation
        )

        # Store in cache (will be saved by caller in batch mode, or by store() call)
        self._set_cache(filepath, meta)

        return meta

    @staticmethod
    def _compute_fields(meta):
        # manufactured, but not stored fields (bloat and gigabytes)
        area = meta.width * meta.height
        if area > 0:
            meta.bloat = int(round((meta.bitrate / math.sqrt(area)) * 1000))
        else:
            meta.bloat = 0
        meta.gb = round(meta.size_bytes / (1024 * 1024 * 1024), 3)

    def _load_probe_data(self, filepath: str) -> Probe:
        """Helper to convert stored dictionary back into Probe."""
        meta = Probe(**self.cache_data[filepath])
        self._compute_fields(meta)

        return meta


    def load(self):
        """Loads cache data from the temporary JSON file."""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r', encoding='utf-8') as f:
                    self.cache_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                print(f"Warning: Could not read cache file at {self.cache_path}. Starting fresh.")
                self.cache_data = {}

            # IMPORTANT: We only call _get_valid_entry here to PURGE invalid entries,
            # NOT to convert the data. The data remains dicts in self.cache_data.
            for filepath in list(self.cache_data.keys()):
                self._get_valid_entry(filepath)


    def store(self):
        """Writes the current cache data atomically if dirty."""
        if self._dirty_count > 0:
            temp_path = self.cache_path + ".tmp"
            try:
                # 1. Write to a temporary file
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.cache_data, f, indent=4)

                # 2. Rename/Move the temp file to the final path (Atomic operation)
                os.replace(temp_path, self.cache_path)

                self._dirty_count = 0

            except IOError as e:
                print(f"Error writing cache file: {e}")
            finally:
                # Clean up temp file if it still exists (e.g., if os.replace failed)
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    def _set_cache(self, filepath: str, meta: Probe):
        """Stores the metadata in the cache dictionary and marks the cache as dirty."""
        # Convert the Probe dataclass to a dict for JSON storage
        probe_dict = asdict(meta)
        # Remove computed fields (not stored on disk)
        if 'gb' in probe_dict:
            del probe_dict['gb']
        if 'bloat' in probe_dict:
            del probe_dict['bloat']

        self.cache_data[filepath] = probe_dict
        self._dirty_count += 1

    def _get_valid_entry(self, filepath: str) -> Optional[Probe]:
        """ If the entry for the path is not valid, remove it.
            Return the cached entry (as Probe) if valid, else None
        """
        current_size_info = self._get_file_size_info(filepath)
        if current_size_info is None:
            if filepath in self.cache_data:
                # File deleted, invalidate cache entry (mark as dirty)
                del self.cache_data[filepath]
                self._dirty_count += 1
            return None

        if filepath in self.cache_data:
            fields = set(self.cache_data[filepath].keys())
            if fields != self.disk_fields:
                del self.cache_data[filepath]
                self._dirty_count += 1
                return None

            cached_bytes = self.cache_data[filepath]['size_bytes']

            if cached_bytes != current_size_info['size_bytes']:
                # File size changed, invalidate cache entry (mark as dirty)
                del self.cache_data[filepath]
                self._dirty_count += 1
                return None

            # Check if this is a retryable probe failure (?P1 through ?P8)
            cached_anomaly = self.cache_data[filepath].get('anomaly', None)
            if cached_anomaly and cached_anomaly.startswith('?P'):
                try:
                    num = int(cached_anomaly[2])
                    if num < 9:
                        # Retry the probe
                        return None
                except (ValueError, IndexError):
                    pass

            # Cache is VALID. Return the stored data converted to Probe.
            return self._load_probe_data(filepath)
        return None

    def get(self, filepath: str) -> Optional[Probe]:
        """
        Primary entry point. Tries cache first. If invalid, runs ffprobe,
        stores result, and returns it (Read-Through Cache).
        """

        # 1. Check for valid cache hit
        meta = self._get_valid_entry(filepath)
        if meta:
            return meta

        # 2. Cache miss/invalid: Run ffprobe
        meta = self._get_metadata_with_ffprobe(filepath)

        # 3. Store result in cache if successful
        if meta:
            self._set_cache(filepath, meta)

        self._compute_fields(meta)

        return meta

    def set_anomaly(self, filepath: str, anomaly: Optional[str]) -> Optional[Probe]:
        """
        Sets the anomaly field to the given value and, if updated,
        adds to the dirty count.  The entry MUST exist in the cache.
        """

        # 1. Check for valid cache hit
        meta = self._get_valid_entry(filepath)
        if meta:
            if anomaly and anomaly.startswith('Er'):
                if not meta.anomaly:
                    anomaly = 'Er1'
                else:
                    mat = re.match(r'^\bEr(\d)\b', meta.anomaly, re.IGNORECASE)
                    if mat:
                        num = int(mat.group(1))
                        if num <= 8:
                            anomaly = f'Er{num+1}'
                        else:
                            anomaly =  'Er9'

            if meta.anomaly != anomaly:
                meta.anomaly = anomaly
                self._set_cache(filepath, meta)
                # for cases it not happen often ... make sure it is saved NOW
                if anomaly != '---':
                    self.store()
        return meta
        
    def batch_get_or_probe(self, filepaths: List[str], max_workers: int = 8) -> Dict[str, Optional[Probe]]:
        """
        Batch process a list of file paths. Checks cache first, then runs ffprobe
        concurrently for all cache misses. Includes graceful handling for KeyboardInterrupt (Ctrl-C).
        """
        exit_please = False
        results: Dict[str, Optional[Probe]] = {}
        probe_needed_paths: List[str] = []

        # 1. First Pass: Check Cache for all files
        for filepath in filepaths:
            meta = self._get_valid_entry(filepath)
            if meta:
                results[filepath] = meta
            else:
                probe_needed_paths.append(filepath)

        # 2. Second Pass: Concurrent Probing for cache misses
        if not probe_needed_paths:
            return results
        total_files, probe_cnt = len(probe_needed_paths), 0

        print(f"Starting concurrent ffprobe for {len(probe_needed_paths)} files using {max_workers} threads...")

        def probe_wrapper(filepath: str) -> Optional[Probe]:
            return self._get_metadata_with_ffprobe(filepath)

        # Dictionary to hold all futures for easy cancellation later
        future_to_path: Dict[Future, str] = {}
        
        # Use ThreadPoolExecutor to run probes concurrently
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all probes to the executor
            future_to_path.update({executor.submit(probe_wrapper, path): path for path in probe_needed_paths})

            try:
                # Iterate over the completed futures (as they complete)
                for future in future_to_path:
                    filepath = future_to_path[future]
                    
                    try:
                        # Blocks until the result is ready or an exception occurs
                        meta = future.result() 
                        
                        # --- CRITICAL: Cache Update and Progress ---
                        with self._cache_lock:
                            probe_cnt += 1
                            self._set_cache(filepath, meta)
                            self._compute_fields(meta)
                            results[filepath] = meta
                            
                            # Store frequently to minimize lost work on crash/interrupt
                            if self._dirty_count >= 100:
                                self.store()
                                # Overwrite status line
                                percent = round(100 * probe_cnt / total_files, 1)
                                sys.stderr.write(f"probing: {percent}% {probe_cnt} of {total_files}\r")
                                sys.stderr.flush()

                    except KeyboardInterrupt:
                        # If an interrupt hits during a result fetch, stop all work.
                        print("\n🛑 Received interrupt. Shutting down worker threads...")
                        
                        # Cancel all futures that have not started or completed yet
                        for pending_future in future_to_path:
                            if not pending_future.done():
                                pending_future.cancel()
                        
                        # Re-raise the interrupt to jump to the 'finally' block for the final save
                        raise 

                    except Exception:
                        # Handle other exceptions from the probe (e.g., ffprobe timeout, corrupt file)
                        with self._cache_lock:
                            probe_cnt += 1
                        # results[filepath] is implicitly None/missing, or you can set it explicitly:
                        # results[filepath] = None 
                        
            except KeyboardInterrupt:
                # Catches the re-raised interrupt and passes control to 'finally'
                exit_please = True

            finally:
                # 3. Final Step: Graceful Shutdown and Final Cache Save
                
                # Ensure the executor is cleanly shut down and futures are cancelled
                executor.shutdown(wait=False, cancel_futures=True)

                for future in future_to_path: 
                    # Check if the Future object is done and not cancelled
                    if future.done() and not future.cancelled():
                        
                        filepath = future_to_path[future] # Get the filepath (string) from the Future (key)
                        
                        try:
                            meta = future.result()
                            with self._cache_lock:
                                # Only set if it wasn't already successfully processed
                                if filepath not in results:
                                    self._set_cache(filepath, meta)
                                    self._compute_fields(meta)
                                    results[filepath] = meta
                        except Exception:
                            pass # Ignore exceptions on shutdown

                # The final save is guaranteed to run here.
                with self._cache_lock:
                    self.store()
                if exit_please:
                    sys.exit(1)

        # Print a final newline character to clean the console after completion
        self.store()
        if total_files > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

        return results