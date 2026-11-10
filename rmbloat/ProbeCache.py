#!/usr/bin/env python3
""" ProbeCache - Video metadata cache keyed by absolute path + size """
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=too-many-return-statements,too-many-statements
# pylint: disable=too-many-locals,too-many-branches,consider-using-with
# pylint: disable=too-many-instance-attributes,consider-using-dict-items

import json
import os
import stat
import sys
import math
import re
import fcntl
import atexit
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from . import Sandbox

# 4 weeks in seconds
EXPIRY_SECONDS = 4 * 7 * 24 * 60 * 60

# Only ask ffprobe for the fields the cache actually consumes.  Besides being
# smaller, this keeps a crafted file's tag/chapter metadata out of the JSON
# parser and the memory footprint.
PROBE_SHOW_ENTRIES = (
    'stream=codec_type,codec_name,width,height,pix_fmt,color_space,'
    'color_primaries,color_transfer,r_frame_rate,avg_frame_rate:'
    'format=duration,bit_rate'
)

# Default probe-cache filename.  Exposed so CLI wrappers (rmbloatd) can derive
# the instance-lock path without duplicating the string.
DEFAULT_CACHE_FILE_NAME = "video_probes.json"


def default_cache_dir():
    """
    Returns the per-user XDG cache directory for the probe cache.

    The cache contains absolute media paths and ffmpeg-derived metadata, so it
    belongs in ``$XDG_CACHE_HOME/rmbloat`` (``~/.cache/rmbloat`` by default)
    rather than the configuration directory or a shared ``/tmp``.  The
    ``.lock`` file lives here too, and :func:`rmbloatd` derives its path from
    this function.
    """
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
        os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'rmbloat')


def default_lock_path(cache_file_name=DEFAULT_CACHE_FILE_NAME):
    """
    Returns the path of the single-instance lock used by :class:`ProbeCache`.

    This is the exact path the application acquires, so external tools such as
    ``rmbloatd`` can detect a running instance without hardcoding it.
    """
    return os.path.join(default_cache_dir(), cache_file_name) + ".lock"


@dataclass(slots=True)
class Probe:
    """Video metadata probe results"""
    # Fields stored on disk
    folder: str = ''  # directory path ending in /
    last_seen: int = 0  # Unix timestamp
    anomaly: Optional[str] = None
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
    """Video probe cache keyed by absolute file path + size_bytes"""
    disk_fields = set(('folder last_seen anomaly width height codec bitrate fps'
                      ' duration size_bytes color_spt pix_fmt streams').split())

    def __init__(self, cache_file_name=DEFAULT_CACHE_FILE_NAME, cache_dir_name=None,
                 chooser=None):
        if cache_dir_name is None:
            cache_dir_name = default_cache_dir()
        # Keep the cache directory private: entries expose the user's media
        # paths.  ``mode`` and the explicit chmod cover an older directory
        # created under a permissive umask.
        os.makedirs(cache_dir_name, mode=0o700, exist_ok=True)
        try:
            os.chmod(cache_dir_name, 0o700)
        except OSError:
            pass
        self.cache_path = os.path.join(cache_dir_name, cache_file_name)
        self.lock_path = self.cache_path + ".lock"
        if os.path.exists(self.cache_path):
            try:
                os.chmod(self.cache_path, 0o600)
            except OSError:
                pass
        self.cache_data: Dict[str, Dict[str, Any]] = {}  # abs_path -> {size_str -> probe_dict}
        self._dirty_count = 0
        # Reentrant: callers such as batch_get_or_probe() already hold this lock
        # when they invoke store()/_set_cache(), while worker threads take it
        # themselves via _increment_probe_failure().  RLock keeps both safe.
        self._cache_lock = RLock()
        self.chooser = chooser  # FfmpegChooser instance for building ffprobe commands
        self._lock_file = None

        # Acquire single-instance lock
        self._acquire_instance_lock()

        self.load()

    # --- Utility Methods ---

    def _acquire_instance_lock(self):
        """Acquire exclusive lock - only one rmbloat instance can run"""
        try:
            fd = os.open(self.lock_path,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            self._lock_file = os.fdopen(fd, 'w', encoding='utf-8')
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
    def _cache_key(filepath: str) -> str:
        """Canonical cache key for a file: its absolute, normalized path."""
        return os.path.abspath(filepath)

    @staticmethod
    def _get_file_size(filepath: str) -> Optional[int]:
        """Gets the size of a file in bytes."""
        try:
            return os.path.getsize(filepath)
        except Exception:
            return None

    @staticmethod
    def _as_int(value, default=0):
        """
        Best-effort int conversion for ffprobe fields.

        ffprobe reports some numbers as strings, may omit them entirely, and
        emits placeholders such as ``'N/A'`` for unusual or truncated files.
        Returning ``default`` instead of raising keeps such values from
        bypassing the probe error path with ``TypeError``/``ValueError``.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default=0.0):
        """Best-effort float conversion for ffprobe fields (see _as_int)."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_metadata_with_ffprobe(self, file_path: str) -> Optional[Probe]:
        """
        Extracts video metadata using ffprobe and creates a Probe object.
        """
        def get_color_spt():
            nonlocal video_stream
            # Coerce to text: unusual streams may report JSON nulls or other
            # non-string types, which would break the join below.
            colorspace = str(video_stream.get('color_space') or 'unknown')
            color_primaries = str(video_stream.get('color_primaries') or 'unknown')
            color_trc = str(video_stream.get('color_transfer') or 'unknown')
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

        # Feed ffmpeg only regular files.  A FIFO, device node, or symlink to
        # one can hang the probe or expose unrelated data; symlinks to real
        # media are resolved and accepted.
        try:
            real_path = os.path.realpath(file_path)
            file_stat = os.stat(real_path)
        except OSError as exc:
            print(f"Error: cannot stat '{file_path}': {exc}")
            return self._increment_probe_failure(file_path)

        if not stat.S_ISREG(file_stat.st_mode):
            print(f"Error: refusing to probe non-regular file '{file_path}'")
            return self._increment_probe_failure(file_path)

        # Build ffprobe command using chooser if available
        if self.chooser:
            command = self.chooser.make_ffprobe_cmd(
                real_path,
                '-v', 'error',
                '-print_format', 'json',
                '-show_entries', PROBE_SHOW_ENTRIES,
            )
        else:
            command = [
                'ffprobe',
                '-protocol_whitelist', Sandbox.PROTOCOL_WHITELIST,
                '-v', 'error', '-print_format', 'json',
                '-show_entries', PROBE_SHOW_ENTRIES, real_path
            ]

        try:
            env = self.chooser.child_env() if self.chooser else Sandbox.local_env()
            result = Sandbox.run_capped(
                command,
                env=env,
                timeout=30,
                stdout_limit=Sandbox.PROBE_MAX_STDOUT,
                stderr_limit=Sandbox.PROBE_MAX_STDERR,
            )
            if result.timed_out:
                print(f"Error: ffprobe timed out for '{file_path}'.")
                return self._increment_probe_failure(file_path)
            if result.truncated:
                print(f"Error: ffprobe output exceeded the safety limit "
                      f"for '{file_path}'.")
                return self._increment_probe_failure(file_path)
            if result.returncode != 0:
                return self._increment_probe_failure(file_path)

            metadata = json.loads(result.stdout.decode('utf-8'))
            if not isinstance(metadata, dict):
                raise ValueError("ffprobe output is not a JSON object.")
            raw_streams = metadata.get('streams') or []
            if not isinstance(raw_streams, list):
                raise ValueError("ffprobe 'streams' is not a list.")
            # Drop malformed stream entries up front so later field access is safe.
            streams = [s for s in raw_streams if isinstance(s, dict)]
            video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)

            if not video_stream or not metadata.get("format"):
                print(f"Error: ffprobe output missing critical stream/format data for '{file_path}'.")
                return None

            # Capture all streams as compact "type:codec" strings
            all_streams = []
            for s in streams:
                stream_type = s.get('codec_type') or 'unknown'
                stream_codec = s.get('codec_name') or 'unknown'
                all_streams.append(f"{stream_type}:{stream_codec}")

            size_bytes = self._get_file_size(real_path)
            if size_bytes is None:
                raise IOError("Failed to get file size after probe.")

            meta = Probe(
                folder=os.path.dirname(file_path) + '/',
                last_seen=int(time.time()),
                width=self._as_int(video_stream.get('width')),
                height=self._as_int(video_stream.get('height')),
                codec=video_stream.get('codec_name') or '---',
                color_spt=get_color_spt(),
                pix_fmt=video_stream.get('pix_fmt') or 'unknown',
                bitrate=self._as_int(metadata["format"].get('bit_rate')) // 1000,
                duration=self._as_float(metadata["format"].get('duration')),
                streams=all_streams,
                size_bytes=size_bytes,
            )

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

        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"Error: Failed to decode ffprobe JSON output for '{file_path}'.")
            return self._increment_probe_failure(file_path)
        except FileNotFoundError:
            print("Error: The 'ffprobe' command was not found. Is FFmpeg installed and in your PATH?")
            return self._increment_probe_failure(file_path)
        except IOError as e:
            print(f"File size error: {e}")
            return self._increment_probe_failure(file_path)
        except (TypeError, ValueError, AttributeError) as e:
            # Last-resort guard for unexpected metadata shapes (e.g. a stream
            # field that is neither numeric nor textual).
            print(f"Error: Unusable ffprobe metadata for '{file_path}': {e}")
            return self._increment_probe_failure(file_path)

    # --- Cache Management Methods ---

    def _increment_probe_failure(self, filepath: str) -> Probe:
        """Increment the probe failure counter (?P1 -> ?P2 -> ... -> ?P9)"""
        key = self._cache_key(filepath)
        size_bytes = self._get_file_size(filepath)
        if size_bytes is None:
            size_bytes = 0
        size_str = str(size_bytes)

        # Worker threads may call this concurrently; the read-check-write of the
        # anomaly counter must be atomic (RLock is reentrant for _set_cache).
        with self._cache_lock:
            # Check for existing probe failure
            current_anomaly = None
            if key in self.cache_data and size_str in self.cache_data[key]:
                current_anomaly = self.cache_data[key][size_str].get('anomaly')

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
                folder=os.path.dirname(key) + '/',
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

    def _load_probe_data(self, key: str, size_str: str) -> Probe:
        """Helper to convert stored dictionary back into Probe."""
        meta = Probe(**self.cache_data[key][size_str])
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

            # Validate and migrate to the current format:
            #   absolute file path -> {size_str -> probe_dict}
            # Older releases keyed the outer mapping by basename, which made
            # same-named, same-sized files in different folders collide. Those
            # entries are re-keyed here using their stored ``folder`` field.
            with self._cache_lock:
                self.cache_data = {}
                for key, value in raw_data.items():
                    if not isinstance(value, dict):
                        # Legacy flat ``full path -> probe_dict`` entries are discarded.
                        continue

                    # Value must be a mapping of size keys to probe dicts.
                    # Drop keys from older schemas (such as the removed
                    # write-only ``customs`` field) so upgrading does not throw
                    # away an otherwise valid probe cache.
                    valid_sizes = {}
                    for size_key, probe_dict in value.items():
                        if isinstance(probe_dict, dict):
                            probe_dict = {k: v for k, v in probe_dict.items()
                                          if k in self.disk_fields}
                        if size_key.isdigit() and self._is_valid_entry(probe_dict):
                            valid_sizes[size_key] = probe_dict
                    if not valid_sizes:
                        continue

                    if os.path.isabs(key):
                        # Already path-keyed; normalize so lookups match exactly.
                        bucket = self.cache_data.setdefault(os.path.normpath(key), {})
                        bucket.update(valid_sizes)
                        continue

                    # Legacy basename-keyed format: rebuild the full path from the
                    # stored folder so distinct files no longer share an entry.
                    for size_key, probe_dict in valid_sizes.items():
                        folder = probe_dict.get('folder') or ''
                        if not folder:
                            continue
                        path_key = self._cache_key(os.path.join(folder, key))
                        self.cache_data.setdefault(path_key, {})[size_key] = probe_dict

    def store(self):
        """Writes the current cache data atomically if dirty, pruning old entries."""
        # Hold the lock for the whole prune+serialize pass so worker threads
        # cannot mutate self.cache_data mid-iteration ("dictionary changed size
        # during iteration") and cannot have an in-flight _set_cache() dropped.
        with self._cache_lock:
            if self._dirty_count <= 0:
                return

            # Prune entries older than 4 weeks
            now = int(time.time())
            cutoff = now - EXPIRY_SECONDS
            basenames_to_delete = []

            for cache_key, sizes in self.cache_data.items():
                sizes_to_delete = []
                for size_str, probe_dict in sizes.items():
                    last_seen = probe_dict.get('last_seen', 0)
                    if last_seen < cutoff:
                        sizes_to_delete.append(size_str)

                for size_str in sizes_to_delete:
                    del sizes[size_str]

                if not sizes:
                    basenames_to_delete.append(cache_key)

            for cache_key in basenames_to_delete:
                del self.cache_data[cache_key]

            # Write atomically through a unique, private (0600) temp file in
            # the same directory.  The previous ``cache_path + ".tmp"`` name
            # was predictable, so another user could pre-create or symlink it.
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=f'.{os.path.basename(self.cache_path)}.',
                    suffix='.tmp',
                    dir=os.path.dirname(self.cache_path) or '.',
                    text=True)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self.cache_data, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.cache_path)
                temp_path = None
                self._dirty_count = 0
            except IOError as e:
                print(f"Error writing cache file: {e}")
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def _set_cache(self, filepath: str, meta: Probe):
        """Stores the metadata in the cache dictionary."""
        key = self._cache_key(filepath)
        size_str = str(meta.size_bytes)

        # Ensure folder and last_seen are set
        meta.folder = os.path.dirname(key) + '/'
        if meta.last_seen == 0:
            meta.last_seen = int(time.time())

        # Convert to dict, removing computed fields
        probe_dict = asdict(meta)
        probe_dict.pop('gb', None)
        probe_dict.pop('bloat', None)

        with self._cache_lock:
            if key not in self.cache_data:
                self.cache_data[key] = {}
            self.cache_data[key][size_str] = probe_dict
            self._dirty_count += 1

    def _get_valid_entry(self, filepath: str) -> Optional[Probe]:
        """
        Look up cache entry by absolute path + size.
        Updates the stored folder and last_seen.
        Returns Probe if valid, None otherwise.
        """
        key = self._cache_key(filepath)
        current_size = self._get_file_size(filepath)

        if current_size is None:
            # File doesn't exist
            return None

        size_str = str(current_size)

        with self._cache_lock:
            if key not in self.cache_data:
                return None

            if size_str not in self.cache_data[key]:
                return None

            entry = self.cache_data[key][size_str]

            # Validate entry has correct fields
            if not self._is_valid_entry(entry):
                del self.cache_data[key][size_str]
                if not self.cache_data[key]:
                    del self.cache_data[key]
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

            # Keep the stored folder in sync with the key's directory.
            folder = os.path.dirname(key) + '/'
            if entry.get('folder') != folder:
                entry['folder'] = folder
                self._dirty_count += 1

            # Update last_seen
            entry['last_seen'] = int(time.time())
            self._dirty_count += 1

            return self._load_probe_data(key, size_str)

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
        key = self._cache_key(filepath)
        current_size = self._get_file_size(filepath)

        if current_size is None:
            print(f"Warning: set_anomaly({anomaly}) - file not found: {filepath}")
            return None

        size_str = str(current_size)

        with self._cache_lock:
            if key not in self.cache_data or size_str not in self.cache_data[key]:
                print(f"Warning: set_anomaly({anomaly}) - no cache entry for {filepath}")
                return None

            entry = self.cache_data[key][size_str]

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

            return self._load_probe_data(key, size_str)

    def move_entry(self, old_path: str, new_path: str) -> bool:
        """
        Re-key a cache entry after a file rename/move.
        Looks up the exact old path and moves all its size entries to new_path.
        """
        old_key = self._cache_key(old_path)
        new_key = self._cache_key(new_path)

        with self._cache_lock:
            if old_key not in self.cache_data:
                return False

            entries = self.cache_data[old_key]
            if not entries:
                del self.cache_data[old_key]
                return False

            now = int(time.time())

            if old_key == new_key:
                for entry in entries.values():
                    entry['last_seen'] = now
                self._dirty_count += 1
                self.store()
                return True

            new_folder = os.path.dirname(new_key) + '/'
            target = self.cache_data.setdefault(new_key, {})
            for size_str, entry in entries.items():
                entry['folder'] = new_folder
                entry['last_seen'] = now
                target[size_str] = entry
            del self.cache_data[old_key]

            self._dirty_count += 1
            self.store()
            return True

    def batch_get_or_probe(self, filepaths: List[str], max_workers: int = 8) -> Dict[str, Probe]:
        """
        Batch process a list of file paths. Checks cache first, then runs ffprobe
        concurrently for all cache misses.

        Files that produce no metadata (missing, audio-only, or otherwise not a
        probeable video) are omitted from the returned mapping and reported on
        stderr, so callers never have to handle a ``None`` probe.
        """
        exit_please = False
        results: Dict[str, Probe] = {}
        failed_paths: List[str] = []
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
                            else:
                                failed_paths.append(filepath)

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
                                if filepath not in results and filepath not in failed_paths:
                                    if meta:
                                        self._set_cache(filepath, meta)
                                        self._compute_fields(meta)
                                        results[filepath] = meta
                                    else:
                                        failed_paths.append(filepath)
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

        if failed_paths:
            sys.stderr.write(
                f"Skipped {len(failed_paths)} file(s) with no video stream"
                " or that could not be probed:\n")
            for filepath in failed_paths:
                sys.stderr.write(f"  {filepath}\n")
            sys.stderr.flush()

        return results
