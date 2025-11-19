#!/usr/bin/env python3

import json
import os
import subprocess
import math
from types import SimpleNamespace
from typing import Optional, Dict, Any, Union

class ProbeCache:
    def __init__(self, cache_file_name="video_probes.json", cache_dir_name="/tmp"):
        self.cache_path = os.path.join(cache_dir_name, cache_file_name)
        self.cache_data: Dict[str, Dict[str, Any]] = {}
        # New: Tracks changes since the last store()
        self._dirty_count = 0 
        self.load()

    # --- Utility Methods ---

    @staticmethod
    def _get_file_size_info(filepath: str) -> Optional[Dict[str, Union[int, float]]]:
        """Gets the size of a file in bytes and GB for storage."""
        try:
            size_bytes = os.path.getsize(filepath)
            size_gb = round(size_bytes / (1024 * 1024 * 1024), 3)
            return {
                'size_bytes': size_bytes,
                'size_gb': size_gb
            }
        except Exception:
            return None

    def _get_metadata_with_ffprobe(self, file_path: str) -> Optional[SimpleNamespace]:
        """
        Extracts video metadata using ffprobe. (Your existing logic)
        """
        # ... (ffprobe command setup, error handling, and metadata parsing) ...
        # [Placeholder for the large ffprobe code block]
        # Assuming successful probe returns 'meta' SimpleNamespace

        if not os.path.exists(file_path):
            print(f"Error: File not found at '{file_path}'")
            return None

        command = [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', file_path
        ]

        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            
            metadata = json.loads(result.stdout)
            
            video_stream = next((s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'), None)
            
            if not video_stream or not metadata.get("format"):
                print(f"Error: ffprobe output missing critical stream/format data for '{file_path}'.")
                return None

            meta = SimpleNamespace()
            
            meta.width = int(video_stream.get('width', 0))
            meta.height = int(video_stream.get('height', 0))
            meta.codec = video_stream.get('codec_name', 'unk_codec')
            
            meta.bitrate = int(int(metadata["format"].get('bit_rate', 0))/1000) 
            meta.duration = float(metadata["format"].get('duration', 0.0))

            # meta.bloat = int(round((1000*1000*meta.bitrate)/(meta.height*meta.width)))
            area = meta.width * meta.height
            meta.bloat = int(round((meta.bitrate / math.sqrt(area)) * 1000))
            
            size_info = self._get_file_size_info(file_path)
            if size_info is None:
                raise IOError("Failed to get file size after probe.")
                
            meta.gb = size_info['size_gb']
            meta.size_bytes = size_info['size_bytes']

            if self._dirty_count >= 100:
                self.store()
            
            return meta

        except FileNotFoundError:
            print("Error: ffprobe command not found. Ensure FFmpeg/ffprobe is installed and in your system PATH.")
            return None
        except subprocess.CalledProcessError as e:
            print(f"Error running ffprobe for '{file_path}': [Code: {e.returncode}] {e.stderr.strip()}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred during probing or parsing for '{file_path}': {e}")
            return None

    # --- Cache Management Methods ---

    def load(self):
        """Loads cache data from the temporary JSON file."""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    self.cache_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                print(f"Warning: Could not read cache file at {self.cache_path}. Starting fresh.")
                self.cache_data = {}
            for filepath in list(self.cache_data.keys()):
                # in effect, purge invalid entries
                _ = self._get_valid_entry(filepath)


    def store(self):
        """Writes the current cache data to the temporary JSON file ONLY IF it is dirty."""
        if self._dirty_count > 0:
            try:
                with open(self.cache_path, 'w') as f:
                    json.dump(self.cache_data, f, indent=4)
                
                # Success! Clear the dirty count.
                self._dirty_count = 0
                # print(f"Cache stored successfully (dirty count cleared).")
                
            except IOError as e:
                pass
                # print(f"Error writing cache file: {e}")
        else:
            pass
            # print("Cache is clean. Disk write skipped.")


    def _set_cache(self, filepath: str, meta: SimpleNamespace):
        """Stores the metadata in the cache dictionary and marks the cache as dirty."""
        # Use meta.size_bytes for validation
        validation_keys = {'size_bytes': meta.size_bytes}
            
        # Convert the SimpleNamespace back to a dict for JSON storage
        probe_dict = vars(meta)
            
        self.cache_data[filepath] = {
            'validation': validation_keys,
            'probe_data': probe_dict
        }
        # New: Increment the dirty count
        self._dirty_count += 1 

    def _get_valid_entry(self, filepath: str):
        """ If the entry for the path is not valid, remove it.
            Return the cached entry if valid, else None
        """
        # 1. Check if file exists on disk
        current_size_info = self._get_file_size_info(filepath)
        if current_size_info is None:
            if filepath in self.cache_data:
                # File deleted, invalidate cache entry (mark as dirty)
                del self.cache_data[filepath]
                self._dirty_count += 1
            return None

        if filepath in self.cache_data:
            cached_bytes = self.cache_data[filepath]['validation']['size_bytes']
            
            if cached_bytes != current_size_info['size_bytes']:
                # File deleted, invalidate cache entry (mark as dirty)
                del self.cache_data[filepath]
                self._dirty_count += 1
                return None

            # Cache is VALID. Return the stored data.
            return SimpleNamespace(**self.cache_data[filepath]['probe_data'])
        return None

    def get(self, filepath: str) -> Optional[SimpleNamespace]:
        """
        Primary entry point. Tries cache first. If invalid, runs ffprobe, 
        stores result, and returns it (Read-Through Cache).
        """
        
        # 1. Check if file exists on disk
        # 2. Check for valid cache hit
        meta = self._get_valid_entry(filepath)
        if meta:
            return meta
        
        # 3. Cache miss/invalid: Run ffprobe
        # print(f"Cache miss/invalid for '{os.path.basename(filepath)}'. Running ffprobe...")
        meta = self._get_metadata_with_ffprobe(filepath)

        # 4. Store result in cache if successful
        if meta:
            self._set_cache(filepath, meta)
        
        return meta
