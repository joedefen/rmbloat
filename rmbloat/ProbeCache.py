#!/usr/bin/env python3
""" ProbeCache - Video metadata cache keyed by basename + size """
import json
import os
import sys
import subprocess
import math
import re
import fcntl
import atexit
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=too-many-return-statements,too-many-statements

_dataclass_kwargs = {'slots': True} if sys.version_info >= (3, 10) else {}

# 4 weeks in seconds
EXPIRY_SECONDS = 4 * 7 * 24 * 60 * 60


@dataclass(**_dataclass_kwargs)
class Probe:
    """Video metadata probe results"""
    # Fields stored on disk
    folder: str = ''  # directory path ending in /
    last_seen: int = 0  # Unix timestamp
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
    streams: List[str] = field(default_factory=list)  # compact "type:codec" strings

    # Computed fields (not stored on disk)
    bloat: int = field(default=0, init=False)
    gb: float = field(default=0.0, init=False)

class ProbeCache:
    """Video probe cache keyed by basename + size_bytes"""
    disk_fields = set(('folder last_seen anomaly width height codec bitrate fps'
                      ' duration size_bytes color_spt customs pix_fmt streams').split())

    def __init__(self, cache_file_name="video_probes.json", cache_dir_name="/tmp", chooser=None):
        self.cache_path = os.path.join(cache_dir_name, cache_file_name)
        self.lock_path = self.cache_path + ".lock"
        self.cache_data: Dict[str, Dict[str, Any]] = {}  # basename -> {size_str -> probe_dict}
        self._dirty_count = 0
        self._cache_lock = Lock()
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
    def _get_file_size(filepath: str) -> Optional[int]:
        """Gets the size of a file in bytes."""
        try:
            return os.path.getsize(filepath)
        except Exception:
            return None

    def _get_metadata_with_ffprobe(self, file_path: str) -> Optional[Probe]:
        """
        Extracts video metadata using ffprobe and creates a Probe object.
        """
        def get_color_spt():
            nonlocal video_stream
            colorspace = video_stream.get('color_space', 'unknown')
            color_primaries = video_stream.get('color_primaries', 'unknown')
            color_trc = video_stream.get('color_transfer', 'unknown')
            parts = [colorspace]
            if color_primaries != colorspace:
                parts.append(color_primaries)
            else:
                parts.append("~")
            if color_trc != color_primaries:
                parts.append(color_trc)
            else:
                parts.append("~")
            return ",".join(parts)

        if not os.path.exists(file_path):
            print(f"Error: File not found at '{file_path}'")
            return None

        # Build ffprobe command using chooser if available
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
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=30
            )

            metadata = json.loads(result.stdout)
            video_stream = next((s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'), None)

            if not video_stream or not metadata.get("format"):
                print(f"Error: ffprobe output missing critical stream/format data for '{file_path}'.")
                return None

            # Capture all streams as compact "type:codec" strings
            all_streams = []
            for s in metadata.get('streams', []):
                stream_type = s.get('codec_type', 'unknown')
                stream_codec = s.get('codec_name', 'unknown')
                all_streams.append(f"{stream_type}:{stream_codec}")

            size_bytes = self._get_file_size(file_path)
            if size_bytes is None:
                raise IOError("Failed to get file size after probe.")

            meta = Probe(
                folder=os.path.dirname(file_path) + '/',
                last_seen=int(time.time()),
                width=int(video_stream.get('width', 0)),
                height=int(video_stream.get('height', 0)),
                codec=video_stream.get('codec_name', '---'),
                color_spt=get_color_spt(),
                pix_fmt=video_stream.get('pix_fmt', 'unknown'),
                bitrate=int(int(metadata["format"].get('bit_rate', '0'))/1000),
                duration=float(metadata["format"].get('duration', 0.0)),
                streams=all_streams,
                size_bytes=size_bytes,
            )

            # Detect unsafe subtitle streams
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

            # Parse frame rate
            fps_str = video_stream.get('r_frame_rate') or video_stream.get('avg_frame_rate', '0/0')
            meta.fps = 0.0
            try:
                num, den = map(int, fps_str.split('/'))
                if den > 0:
                    meta.fps = round(float(num) / float(den), 3)
            except Exception:
                pass

            return meta

        except subprocess.CalledProcessError:
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
        """Increment the probe failure counter (?P1 -> ?P2 -> ... -> ?P9)"""
        basename = os.path.basename(filepath)
        size_bytes = self._get_file_size(filepath)
        if size_bytes is None:
            size_bytes = 0
        size_str = str(size_bytes)

        # Check for existing probe failure
        current_anomaly = None
        if basename in self.cache_data and size_str in self.cache_data[basename]:
            current_anomaly = self.cache_data[basename][size_str].get('anomaly')

        # Determine new probe failure number
        if current_anomaly and current_anomaly.startswith('?P'):
            try:
                num = int(current_anomaly[2])
                new_num = min(num + 1, 9)
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1

        meta = Probe(
            folder=os.path.dirname(filepath) + '/',
            last_seen=int(time.time()),
            anomaly=f'?P{new_num}',
            size_bytes=size_bytes
        )

        self._set_cache(filepath, meta)
        return meta

    @staticmethod
    def _compute_fields(meta):
        """Compute derived fields (bloat and gigabytes)"""
        area = meta.width * meta.height
        if area > 0:
            meta.bloat = int(round((meta.bitrate / math.sqrt(area)) * 1000))
        else:
            meta.bloat = 0
        meta.gb = round(meta.size_bytes / (1024 * 1024 * 1024), 3)

    def _is_valid_entry(self, entry: dict) -> bool:
        """Check if an entry dict has the expected fields"""
        if not isinstance(entry, dict):
            return False
        fields = set(entry.keys())
        return fields == self.disk_fields

    def _load_probe_data(self, basename: str, size_str: str) -> Probe:
        """Helper to convert stored dictionary back into Probe."""
        meta = Probe(**self.cache_data[basename][size_str])
        self._compute_fields(meta)
        return meta

    def load(self):
        """Loads cache data from the JSON file, invalidating old format entries."""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                print(f"Warning: Could not read cache file at {self.cache_path}. Starting fresh.")
                raw_data = {}

            # Validate and migrate: new format has basename -> {size_str -> probe_dict}
            self.cache_data = {}
            for key, value in raw_data.items():
                # Check if this looks like new format (basename key with dict of size keys)
                if isinstance(value, dict):
                    # Check if value contains size sub-keys (digits) with probe dicts
                    valid_sizes = {}
                    for size_key, probe_dict in value.items():
                        if size_key.isdigit() and self._is_valid_entry(probe_dict):
                            valid_sizes[size_key] = probe_dict
                    if valid_sizes:
                        self.cache_data[key] = valid_sizes
                # Old format entries (full path -> probe_dict) are simply discarded

    def store(self):
        """Writes the current cache data atomically if dirty, pruning old entries."""
        if self._dirty_count > 0:
            # Prune entries older than 4 weeks
            now = int(time.time())
            cutoff = now - EXPIRY_SECONDS
            basenames_to_delete = []

            for basename, sizes in self.cache_data.items():
                sizes_to_delete = []
                for size_str, probe_dict in sizes.items():
                    last_seen = probe_dict.get('last_seen', 0)
                    if last_seen < cutoff:
                        sizes_to_delete.append(size_str)

                for size_str in sizes_to_delete:
                    del sizes[size_str]

                if not sizes:
                    basenames_to_delete.append(basename)

            for basename in basenames_to_delete:
                del self.cache_data[basename]

            # Write atomically
            temp_path = self.cache_path + ".tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.cache_data, f, indent=4)
                os.replace(temp_path, self.cache_path)
                self._dirty_count = 0
            except IOError as e:
                print(f"Error writing cache file: {e}")
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    def _set_cache(self, filepath: str, meta: Probe):
        """Stores the metadata in the cache dictionary."""
        basename = os.path.basename(filepath)
        size_str = str(meta.size_bytes)

        # Ensure folder and last_seen are set
        meta.folder = os.path.dirname(filepath) + '/'
        if meta.last_seen == 0:
            meta.last_seen = int(time.time())

        # Convert to dict, removing computed fields
        probe_dict = asdict(meta)
        probe_dict.pop('gb', None)
        probe_dict.pop('bloat', None)

        if basename not in self.cache_data:
            self.cache_data[basename] = {}
        self.cache_data[basename][size_str] = probe_dict
        self._dirty_count += 1

    def _get_valid_entry(self, filepath: str) -> Optional[Probe]:
        """
        Look up cache entry by basename + size.
        Updates path if file moved, updates last_seen.
        Returns Probe if valid, None otherwise.
        """
        basename = os.path.basename(filepath)
        current_size = self._get_file_size(filepath)

        if current_size is None:
            # File doesn't exist
            return None

        size_str = str(current_size)

        if basename not in self.cache_data:
            return None

        if size_str not in self.cache_data[basename]:
            return None

        entry = self.cache_data[basename][size_str]

        # Validate entry has correct fields
        if not self._is_valid_entry(entry):
            del self.cache_data[basename][size_str]
            if not self.cache_data[basename]:
                del self.cache_data[basename]
            self._dirty_count += 1
            return None

        # Check for retryable probe failure (?P1 through ?P8)
        cached_anomaly = entry.get('anomaly')
        if cached_anomaly and cached_anomaly.startswith('?P'):
            try:
                num = int(cached_anomaly[2])
                if num < 9:
                    return None  # Retry the probe
            except (ValueError, IndexError):
                pass

        # Update folder if file moved
        folder = os.path.dirname(filepath) + '/'
        if entry.get('folder') != folder:
            entry['folder'] = folder
            self._dirty_count += 1

        # Update last_seen
        entry['last_seen'] = int(time.time())
        self._dirty_count += 1

        return self._load_probe_data(basename, size_str)

    def get(self, filepath: str) -> Optional[Probe]:
        """
        Primary entry point. Tries cache first (by basename+size).
        If invalid, runs ffprobe, stores result, and returns it.
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
        Sets the anomaly field to the given value.
        The entry MUST exist in the cache.
        """
        basename = os.path.basename(filepath)
        current_size = self._get_file_size(filepath)

        if current_size is None:
            print(f"Warning: set_anomaly({anomaly}) - file not found: {filepath}")
            return None

        size_str = str(current_size)

        if basename not in self.cache_data or size_str not in self.cache_data[basename]:
            print(f"Warning: set_anomaly({anomaly}) - no cache entry for {filepath}")
            return None

        entry = self.cache_data[basename][size_str]

        if anomaly and anomaly.startswith('Er'):
            current_anomaly = entry.get('anomaly')
            if not current_anomaly:
                anomaly = 'Er1'
            else:
                mat = re.match(r'^\bEr(\d)\b', current_anomaly, re.IGNORECASE)
                if mat:
                    num = int(mat.group(1))
                    anomaly = f'Er{min(num + 1, 9)}'

        if entry.get('anomaly') != anomaly:
            entry['anomaly'] = anomaly
            self._dirty_count += 1
            if anomaly != '---':
                self.store()

        return self._load_probe_data(basename, size_str)

    def move_entry(self, old_path: str, new_path: str) -> bool:
        """
        Update the stored folder for a cache entry after file rename.
        Looks up by basename+size of old_path, updates folder to new location.
        """
        old_basename = os.path.basename(old_path)
        new_basename = os.path.basename(new_path)
        old_folder = os.path.dirname(old_path) + '/'
        new_folder = os.path.dirname(new_path) + '/'

        if old_basename not in self.cache_data:
            return False

        # Find entry matching old folder
        for size_str, entry in self.cache_data[old_basename].items():
            if entry.get('folder') == old_folder:
                # Found it - move to new basename if different
                if old_basename != new_basename:
                    # Remove from old basename
                    probe_dict = self.cache_data[old_basename].pop(size_str)
                    if not self.cache_data[old_basename]:
                        del self.cache_data[old_basename]
                    # Add to new basename
                    if new_basename not in self.cache_data:
                        self.cache_data[new_basename] = {}
                    probe_dict['folder'] = new_folder
                    probe_dict['last_seen'] = int(time.time())
                    self.cache_data[new_basename][size_str] = probe_dict
                else:
                    entry['folder'] = new_folder
                    entry['last_seen'] = int(time.time())

                self._dirty_count += 1
                self.store()
                return True

        return False

    def batch_get_or_probe(self, filepaths: List[str], max_workers: int = 8) -> Dict[str, Optional[Probe]]:
        """
        Batch process a list of file paths. Checks cache first, then runs ffprobe
        concurrently for all cache misses.
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

        future_to_path: Dict = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path.update({executor.submit(probe_wrapper, path): path for path in probe_needed_paths})

            try:
                for future in future_to_path:
                    filepath = future_to_path[future]

                    try:
                        meta = future.result()

                        with self._cache_lock:
                            probe_cnt += 1
                            if meta:
                                self._set_cache(filepath, meta)
                                self._compute_fields(meta)
                            results[filepath] = meta

                            if self._dirty_count >= 100:
                                self.store()
                                percent = round(100 * probe_cnt / total_files, 1)
                                sys.stderr.write(f"probing: {percent}% {probe_cnt} of {total_files}\r")
                                sys.stderr.flush()

                    except KeyboardInterrupt:
                        print("\n🛑 Received interrupt. Shutting down worker threads...")
                        for pending_future in future_to_path:
                            if not pending_future.done():
                                pending_future.cancel()
                        raise

                    except Exception:
                        with self._cache_lock:
                            probe_cnt += 1

            except KeyboardInterrupt:
                exit_please = True

            finally:
                executor.shutdown(wait=False, cancel_futures=True)

                for future in future_to_path:
                    if future.done() and not future.cancelled():
                        filepath = future_to_path[future]
                        try:
                            meta = future.result()
                            with self._cache_lock:
                                if filepath not in results:
                                    if meta:
                                        self._set_cache(filepath, meta)
                                        self._compute_fields(meta)
                                    results[filepath] = meta
                        except Exception:
                            pass

                with self._cache_lock:
                    self.store()
                if exit_please:
                    sys.exit(1)

        self.store()
        if total_files > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

        return results
