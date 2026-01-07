#!/usr/bin/env python3
"""
File operation utilities for rmbloat
"""
# pylint: disable=broad-exception-caught,too-many-locals
# pylint: disable=line-too-long,invalid-name
import os
import time
from pathlib import Path

def sanitize_file_paths(paths):
    """
    Sanitize a list of file paths by:
    1. Converting all to absolute paths
    2. Removing non-existing paths
    3. Removing redundant paths (paths contained within other paths)

    Returns a sorted list of unique, clean paths.
    """
    if not paths:
        return []

    # Convert all to absolute paths and resolve symlinks
    abs_paths = []
    for path_str in paths:
        if not path_str or not path_str.strip():
            continue
        try:
            path = Path(path_str).resolve()
            if path.exists():
                abs_paths.append(path)
        except (OSError, RuntimeError):
            # Skip invalid paths
            continue

    if not abs_paths:
        return []

    # Remove duplicates and sort
    abs_paths = sorted(set(abs_paths))

    # Remove redundant paths (paths that are subdirectories of other paths)
    filtered_paths = []
    for path in abs_paths:
        # Check if this path is a subdirectory of any already-added path
        is_redundant = False
        for existing_path in list(filtered_paths):
            try:
                # Check if path is relative to existing_path (i.e., is a subdirectory)
                path.relative_to(existing_path)
                is_redundant = True
                break
            except ValueError:
                # Not a subdirectory, check reverse (is existing_path a subdirectory of path?)
                try:
                    existing_path.relative_to(path)
                    # existing_path is redundant, remove it and add path instead
                    filtered_paths.remove(existing_path)
                    is_redundant = False
                    break
                except ValueError:
                    # Neither is a subdirectory of the other
                    continue

        if not is_redundant:
            filtered_paths.append(path)

    # Convert back to strings
    return [str(p) for p in sorted(filtered_paths)]


def preserve_timestamps(filepath):
    """
    Get timestamps from a file, validating they're not in the future.

    Args:
        filepath: Path to file to read timestamps from

    Returns:
        Tuple of (atime, mtime) or None if failed
    """
    try:
        stat_info = os.stat(filepath)
        atime = stat_info.st_atime
        mtime = stat_info.st_mtime

        # If timestamps are in the future, set them to 1 year ago
        now = time.time()
        one_year_ago = now - (365 * 24 * 60 * 60)
        if atime > now or mtime > now:
            atime = one_year_ago
            mtime = one_year_ago

        return (atime, mtime)
    except OSError:
        return None


def apply_timestamps(filepath, timestamps):
    """
    Apply timestamps to a file.

    Args:
        filepath: Path to file to update
        timestamps: Tuple of (atime, mtime) from preserve_timestamps()

    Returns:
        True if successful, False otherwise
    """
    if timestamps is None:
        return False

    try:
        os.utime(filepath, timestamps)
        return True
    except OSError:
        return False


def bulk_rename(old_file_name: str, new_file_name: str, trashes: set):
    """
    Renames files and directories in the current working directory (CWD).

    It finds all items whose non-extension part matches the non-extension part
    of `old_file_name`, and renames them using the non-extension part of
    `new_file_name`, preserving the original file extensions.

    Args:
        old_file_name: A sample filename (e.g., 'oldie.mp4') used to define
                       the base name to look for ('oldie').
        new_file_name: A sample filename (e.g., 'newbie.mkv') used to define
                       the base name to rename to ('newbie').
        trashes: Set of filenames that are being trashed (skip these)

    Returns:
        List of operation strings describing what was done
    """
    ops = []
    old_base_name, _ = os.path.splitext(old_file_name)
    new_base_name, _ = os.path.splitext(new_file_name)

    # Define the special suffix to look for (case-insensitive search)
    special_ext = ".REFERENCE.srt"
    # 2. Use os.walk for recursive traversal starting from the current directory ('.')
    for root, dirs, files in os.walk('.', topdown=False):

        # Combine files and directories for unified processing.
        items_to_check = files + dirs

        for item_name in items_to_check:
            # Skip if the item is a special directory reference
            if item_name in ('.', '..'):
                continue

            full_old_path = os.path.join(root, item_name)
            current_base, extension = os.path.splitext(item_name)
            current_base2, extension2 = os.path.splitext(current_base)
            extension2 = extension2 + extension

            new_item_name = None

            # --- Rule 1: Special Case - Full Name Match (item_name == old_base_name) ---
            if item_name == old_base_name:
                new_item_name = new_base_name

            # --- Rule 2: Special Case - Reference SRT Suffix Match ---
            # Requires the item to end with ".reference.srt" AND the base part to match old_base_name
            elif (item_name.lower().endswith(special_ext.lower())
                  and item_name[:-len(special_ext)] == old_base_name):
                new_item_name = new_base_name + special_ext

            elif current_base2 == old_base_name:
                new_item_name = new_base_name + extension2

            # --- Rule 3: General Case - Base Name Match ---
            # Applies if the non-extension part matches the intended old base name,
            # and was not caught by the specific rules above.
            elif current_base == old_base_name:
                # General Case: New name is new_base_name + original extension
                new_item_name = new_base_name + extension

            # 4. If no matching rule was triggered, skip this one
            if not new_item_name:
                continue

            # 5. Perform the rename operation
            full_new_path = os.path.join(root, new_item_name)
            try:
                if os.path.basename(item_name) not in trashes:
                    os.rename(full_old_path, full_new_path)
                    ops.append(f"rename {full_old_path!r} {full_new_path!r}")
            except Exception as e:
                # Handle potential errors (e.g., permission errors, file in use)
                ops.append(f"ERR: rename '{full_old_path}' '{full_new_path}': {e}")
    return ops
