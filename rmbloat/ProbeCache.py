#!/usr/bin/env python3
""" TBD """
import json
import os
import subprocess
import math
import re
from types import SimpleNamespace
from typing import Optional, Dict, Any, Union, List
from threading import Lock
from concurrent.futures import ThreadPoolExecutor # <-- NEW IMPORT
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=too-many-return-statements

class ProbeCache:
    """ TBD """
    disk_fields = set('anomaly width height codec bitrate duration size_bytes color_spt'.split())

    """ TBD """
    def __init__(self, cache_file_name="video_probes.json", cache_dir_name="/tmp"):
        self.cache_path = os.path.join(cache_dir_name, cache_file_name)
        self.cache_data: Dict[str, Any] = {}
        self._dirty_count = 0
        self._cache_lock = Lock() # NEW: import Lock from threading
        self.load()

    # --- Utility Methods ---

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

    def _get_metadata_with_ffprobe(self, file_path: str) -> Optional[SimpleNamespace]:
        """
        Extracts video metadata using ffprobe and creates a SimpleNamespace object.
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

            meta = SimpleNamespace()
            meta.anomaly = None # use for errors and ineffective conversions

            meta.width = int(video_stream.get('width', 0))
            meta.height = int(video_stream.get('height', 0))
            meta.codec = video_stream.get('codec_name', 'unk_codec')

            meta.color_spt = get_color_spt()

            # Ensure safe integer conversion
            bitrate_str = metadata["format"].get('bit_rate', '0')
            meta.bitrate = int(int(bitrate_str)/1000)

            meta.duration = float(metadata["format"].get('duration', 0.0))

            size_info = self._get_file_size_info(file_path)
            if size_info is None:
                raise IOError("Failed to get file size after probe.")

            meta.size_bytes = size_info['size_bytes']

            # Check for dirty count to store cache
            if self._dirty_count >= 100:
                self.store()

            return meta

        except subprocess.CalledProcessError as e:
            print(f"Error executing ffprobe: {e.stderr}")
            return None
        except json.JSONDecodeError:
            print(f"Error: Failed to decode ffprobe JSON output for '{file_path}'.")
            return None
        except FileNotFoundError:
            print("Error: The 'ffprobe' command was not found. Is FFmpeg installed and in your PATH?")
            return None
        except IOError as e:
            print(f"File size error: {e}")
            return None


    # --- Cache Management Methods ---

    @staticmethod
    def _compute_fields(meta):
        # manufactured, but not stored fields (bloat and gigabytes)
        area = meta.width * meta.height
        if area > 0:
            meta.bloat = int(round((meta.bitrate / math.sqrt(area)) * 1000))
        else:
            meta.bloat = 0
        meta.gb = round(meta.size_bytes / (1024 * 1024 * 1024), 3)

    def _load_probe_data(self, filepath: str) -> SimpleNamespace:
        """Helper to convert stored dictionary back into SimpleNamespace."""
        meta = SimpleNamespace(**self.cache_data[filepath])
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

    def _set_cache(self, filepath: str, meta: SimpleNamespace):
        """Stores the metadata in the cache dictionary and marks the cache as dirty."""
        # Convert the SimpleNamespace back to a dict for JSON storage
        probe_dict = dict(vars(meta))
        if 'gb' in probe_dict:
            del probe_dict['gb']
        if 'bloat' in probe_dict:
            del probe_dict['bloat']

        self.cache_data[filepath] = probe_dict
        self._dirty_count += 1

    def _get_valid_entry(self, filepath: str):
        """ If the entry for the path is not valid, remove it.
            Return the cached entry (as SimpleNamespace) if valid, else None
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

            # Cache is VALID. Return the stored data converted to SimpleNamespace.
            return self._load_probe_data(filepath)
        return None

    def get(self, filepath: str) -> Optional[SimpleNamespace]:
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

    def set_anomaly(self, filepath: str, anomaly: str) -> Optional[SimpleNamespace]:
        """
        Sets the anomaly field to the given value and, if updated,
        adds to the dirty count.  The entry MUST exist in the cache.
        """

        # 1. Check for valid cache hit
        meta = self._get_valid_entry(filepath)
        if meta:
            if anomaly.startswith('Er'):
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
                # this does not happen often ... make sure it is saved NOW
                self.store()
        return meta

    def batch_get_or_probe(self, filepaths: List[str], max_workers: int = 8) -> Dict[str, Optional[SimpleNamespace]]:
        """
        Batch process a list of file paths. Checks cache first, then runs ffprobe
        concurrently for all cache misses.
        """
        results: Dict[str, Optional[SimpleNamespace]] = {}
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

        print(f"Starting concurrent ffprobe for {len(probe_needed_paths)} files using {max_workers} threads...")

        def probe_wrapper(filepath: str) -> Optional[SimpleNamespace]:
            return self._get_metadata_with_ffprobe(filepath)

        # Use ThreadPoolExecutor to run probes concurrently
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Map the probe_needed_paths to the probe_and_store function
            future_to_path = {executor.submit(probe_wrapper, path): path for path in probe_needed_paths}

            for future in future_to_path:
                filepath = future_to_path[future]
                try:
                    meta = future.result()
                    with self._cache_lock:
                        self._set_cache(filepath, meta)
                        self._compute_fields(meta)
                        results[filepath] = meta
                        if self._dirty_count >= 100:
                            self.store()
                except Exception as exc:
                    print(f"Error probing {filepath}: {exc}")
                    # results[filepath] = None

        # 3. Final Step: Save all new probes to disk once (thread-safe store)
        with self._cache_lock:
            self.store()

        return results
