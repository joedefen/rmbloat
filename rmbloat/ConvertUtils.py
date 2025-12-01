#!/usr/bin/env python3
"""
Utility functions for video file conversion
"""
# pylint: disable=too-many-locals,too-many-branches
import os
import sys
import shlex
import re
from .VideoParser import VideoParser

# Video file extensions recognized by ffmpeg
VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv',
    '.wmv', '.mpg', '.mpeg', '.3gp', '.m4v', '.ts',
    '.vob', '.ogv', '.m2ts', '.mts'
}

# Filename prefixes to skip during processing
SKIP_PREFIXES = ('TEMP.', 'ORIG.')


def bash_quote(args):
    """
    Converts a Python list of arguments into a single, properly quoted
    Bash command string.
    """
    quoted_args = []
    for arg in args:
        # shlex.quote is the preferred, robust way in Python 3.3+
        quoted_arg = shlex.quote(arg)
        quoted_args.append(quoted_arg)

    return ' '.join(quoted_args)


def human_readable_size(size_bytes: int) -> str:
    """
    Converts a raw size in bytes to a human-readable string (e.g., 10 KB, 5.5 MB).

    Returns:
        A string representing the size in human-readable format.
    """
    if size_bytes is None:
        return "0 Bytes"

    if size_bytes == 0:
        return "0 Bytes"

    # Define the unit list (using 1024 for base-2, which is standard for file sizes)
    size_names = ("Bytes", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")

    # Use a loop to find the appropriate unit index (i)
    i = 0
    size = size_bytes
    while size >= 1024 and i < len(size_names) - 1:
        size /= 1024
        i += 1

    # Format the number, keeping two decimal places if it's not the 'Bytes' unit
    if i == 0:
        return f"{size_bytes} {size_names[i]}"
    return f"{size:.2f} {size_names[i]}"


def is_valid_video_file(filename):
    """
    Checks if a file meets all the criteria:
    1. Does not start with 'TEMP.' or 'ORIG.'.
    2. Has a common video file extension (case-insensitive).
    """

    # 1. Check for prefixes to skip
    if filename.startswith(SKIP_PREFIXES):
        return False

    # Get the file extension and convert to lowercase for case-insensitive check
    _, ext = os.path.splitext(filename)

    # 2. Check if the extension is a recognized video format
    if ext.lower() not in VIDEO_EXTENSIONS:
        return False

    # The file meets all criteria
    return True


def get_candidate_video_files(file_args):
    """
    Gather candidate video file paths from command-line arguments.

    Args:
        file_args: List of file/directory paths or "-" for stdin

    Returns:
        tuple: (paths_to_probe, read_pipe)
            - paths_to_probe: List of absolute file paths to probe
            - read_pipe: True if stdin was read (caller needs to restore TTY)
    """
    read_pipe = False
    enqueued_paths = set()
    paths_from_args = []

    # 1. Gather all unique, absolute paths from arguments and stdin
    for file_arg in file_args:
        if file_arg == "-":
            # Handle STDIN
            if not read_pipe:
                paths_from_args.extend(sys.stdin.read().splitlines())
                read_pipe = True
        else:
            # Convert to absolute path immediately
            abs_path = os.path.abspath(file_arg)
            if abs_path not in enqueued_paths:
                paths_from_args.append(abs_path)
                enqueued_paths.add(abs_path)

    # 2. Separate into directories and individual files, and sort for processing order
    directories = []
    immediate_files = []

    for path in paths_from_args:
        # Ignore empty lines from stdin
        if not path:
            continue

        if os.path.isdir(path):
            directories.append(path)
        else:
            immediate_files.append(path)

    # Sort the list of directories to be processed (case-insensitively)
    directories.sort(key=str.lower)

    # Sort the list of individual files (case-insensitively)
    immediate_files.sort(key=str.lower)

    # List to hold all file paths in the final desired, grouped, and sorted order
    paths_to_probe = []

    # 3. Process Directories: Find and group files recursively
    for dir_path in directories:
        # This list will hold all valid video files found in the current directory group
        group_files = []

        # Recursively walk the directory structure
        for root, dirs, files in os.walk(dir_path):

            # Sort the directory names before os.walk processes them (case-insensitive)
            # This ensures predictable traversal order of subdirectories
            dirs.sort(key=str.lower)

            # Sort the files within the current directory (case-insensitive)
            files.sort(key=str.lower)

            for file_name in files:
                full_path = os.path.join(root, file_name)

                # Check for validity and duplicates
                if is_valid_video_file(full_path):
                    if full_path not in enqueued_paths:
                        group_files.append(full_path)
                        enqueued_paths.add(full_path)

        # Append all grouped and sorted file paths for the current directory
        paths_to_probe.extend(group_files)

    # 4. Process Individual Files: Append sorted immediate files
    paths_to_probe.extend(immediate_files)

    return paths_to_probe, read_pipe


def standard_name(pathname: str, height: int, quality: int) -> tuple[bool, str]:
    """
    Create a standardized filename for a video file.

    If "parsed" creates a simple standard name from the title
    and episode number (if episode) OR title and year (if movie).
    Otherwise replaces common H.264/AVC/Xvid/DivX codec strings in a filename
    with 'x265' or 'X265', preserving the original case where possible.
    Also changes the height indicator if not in agreement with actual.

    Args:
        pathname: Path to video file
        height: Actual video height in pixels
        quality: Quality parameter for encoding (CRF value)

    Returns:
        tuple: (changed, new_name)
            - changed: Whether the name was modified
            - new_name: Standardized filename
    """

    basename = os.path.basename(pathname)
    parsed = VideoParser(pathname)
    if parsed.is_movie_year() or parsed.is_tv_episode():
        if parsed.is_tv_episode():
            name = parsed.title
            if parsed.year:
                name += f' {parsed.year}'
            name += f' s{parsed.season:02d}e{parsed.episode:02d}'
            name += f'-{parsed.episode_hi:02d}' if parsed.episode_hi else ''
        else:
            name = f'{parsed.title} {parsed.year}'

        name += f' {height}p x265-cmf{quality} recode'
        name = re.sub(r'[\s\.\-]+', '.', name) + '.mkv'

        return bool(name != basename), name

    new_basename = basename
    # Regular expressions for the codecs to be replaced.
    # The groups will capture the exact string for case-checking later.
    pattern = r'\b([xh]\.?264|avc|xvid|divx)\b'
    regex = re.compile(pattern, re.IGNORECASE)
    end = 0
    while True:
        match = re.search(regex, new_basename[end:])
        if not match:
            break
        sub = 'X265' if match.group(1).isupper() else 'x265'
        start, end = match.span(1)
        new_basename = new_basename[:start] + sub + new_basename[end:]

    pattern = r'\b(\d+[pi]|UHD|4K|2160p|1440p|2K|8K)\b'
    regex = re.compile(pattern, re.IGNORECASE)
    height_str = f'{height}p'  # e.g., '1080p'

    end = 0
    while True:
        match = re.search(regex, new_basename[end:])
        if not match:
            break
        matched_group = match.group(1)  # The matched string (e.g., '4K' or '720i')
        start, end = match.span(1)

        if matched_group.lower().endswith(('k', 'hd')):
            # For '4K', 'UHD', etc. you can't rely on 'height_str' being correct,
            # so you must manually format the replacement based on the original's case.
            is_upper = matched_group.isupper()  # Check if '4K' was '4K' or '4k'
            # The canonical replacement should be f'{height}p'
            sub = height_str.upper() if is_upper else height_str
        else:
            # Standard p/i resolution match (e.g., '720p', '1080i')
            sub = height_str.upper() if matched_group.isupper() else height_str

        new_basename = new_basename[:start] + sub + new_basename[end:]

    # nail down extension
    different = bool(new_basename != basename)
    base, _ = os.path.splitext(new_basename)
    new_basename = base + '.mkv'

    return different, new_basename
