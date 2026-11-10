#!/usr/bin/env python3
"""
File operation utilities for rmbloat
"""
# pylint: disable=broad-exception-caught,too-many-locals
# pylint: disable=line-too-long,invalid-name
import os
import tempfile
import time
from pathlib import Path


HISTORY_EXPORT_PREFIX = 'rmbloat-export-'
HISTORY_EXPORT_SUFFIX = '.txt'
# A copy used to leave one file in the cache directory forever, so a session
# spent reading History accumulated media paths and ffmpeg output without
# bound.  Only the newest few, and nothing older than a week, are kept.
HISTORY_EXPORT_MAX_FILES = 20
HISTORY_EXPORT_MAX_AGE_SECS = 7 * 24 * 60 * 60


def history_export_dir():
    """Return the per-user cache directory for clipboard fallback exports.

    Uses ``$XDG_CACHE_HOME`` when set (falling back to ``~/.cache``) instead
    of a world-writable ``/tmp`` path, so one user cannot read another user's
    copied log entries or pre-create the destination as a symlink.
    """
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
        os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'rmbloat')


def _is_history_export(name):
    """Return True for a name this module owns (never for a planted symlink)."""
    return name.startswith(HISTORY_EXPORT_PREFIX) and name.endswith(HISTORY_EXPORT_SUFFIX)


def _prune_history_exports(export_dir, keep=HISTORY_EXPORT_MAX_FILES,
                           max_age=HISTORY_EXPORT_MAX_AGE_SECS, now=None,
                           protect=None):
    """Delete old ``rmbloat-export-*.txt`` files; return how many were removed.

    The exports are throwaway copies of log entries that contain media paths and
    ffmpeg output, so a bounded store is both a privacy and a disk-usage fix.
    The newest ``keep`` files survive, as does anything younger than ``max_age``;
    everything else is unlinked.  ``protect`` names the file that must never be
    removed (the export just written), which also disambiguates equal mtimes.

    Only regular files created by :func:`write_history_export` are considered,
    and every failure is ignored: housekeeping must never fail a copy.
    """
    now = time.time() if now is None else now
    candidates = []
    try:
        with os.scandir(export_dir) as entries:
            for entry in entries:
                if not _is_history_export(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    candidates.append(
                        (entry.stat(follow_symlinks=False).st_mtime, entry.path))
                except OSError:
                    continue
    except OSError:
        return 0

    # Newest first, with the protected file pinned to the front.  mtimes can
    # collide within one second, so ``protect`` cannot rely on sorting alone.
    candidates.sort(key=lambda item: (item[1] != protect, -item[0]))
    removed = 0
    for index, (mtime, path) in enumerate(candidates):
        if index < keep and now - mtime <= max_age:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            continue
    return removed


def write_history_export(text):
    """Write ``text`` to a new private export file and return its path.

    ``tempfile.mkstemp`` creates the file with mode 0600 and a unique name, so
    the export can never clobber or follow a path planted by another user.
    Previous exports are pruned after the new one is durable, so the directory
    keeps a bounded number of recent copies instead of growing forever.
    """
    export_dir = history_export_dir()
    os.makedirs(export_dir, mode=0o700, exist_ok=True)
    fd, export_path = tempfile.mkstemp(
        prefix=HISTORY_EXPORT_PREFIX, suffix=HISTORY_EXPORT_SUFFIX,
        dir=export_dir, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(export_path)
        except OSError:
            pass
        raise
    _prune_history_exports(export_dir, protect=export_path)
    return export_path


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


def _rename_no_clobber(source_path: str, target_path: str, is_dir: bool) -> None:
    """Rename ``source_path`` without replacing an existing destination.

    ``os.rename()`` overwrites existing files on POSIX.  Companion renaming
    must never destroy an unrelated file, so regular files are installed with
    a hard link first and directories are renamed only after an existence
    check.  Filesystems without hard-link support fall back to a checked
    ``os.rename()``.
    """
    if os.path.lexists(target_path):
        raise FileExistsError(f'destination already exists: {target_path}')

    if is_dir:
        os.rename(source_path, target_path)
        return

    try:
        os.link(source_path, target_path, follow_symlinks=False)
    except FileExistsError:
        raise
    except (NotImplementedError, OSError):
        # Cross-device or no-hard-link filesystems: re-check and rename.
        if os.path.lexists(target_path):
            raise FileExistsError(
                f'destination already exists: {target_path}') from None
        os.rename(source_path, target_path)
    else:
        os.unlink(source_path)


def bulk_rename(old_file_name: str, new_file_name: str, trashes: set):
    """
    Renames companion files and directories beside ``old_file_name``.

    It finds items whose non-extension part matches the non-extension part of
    `old_file_name` and renames them using the non-extension part of
    `new_file_name`, preserving the original extensions.  The search starts in
    the old file's directory, recurses only into directories whose own name
    matches the old basename (for example ``movie.cache/``), and never renames
    items in unrelated subdirectories.  Destinations are never overwritten.

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
    old_dir = os.path.dirname(os.path.abspath(old_file_name))
    old_base_name, _ = os.path.splitext(os.path.basename(old_file_name))
    new_base_name, _ = os.path.splitext(os.path.basename(new_file_name))

    # Define the special suffix to look for (case-insensitive search)
    special_ext = ".REFERENCE.srt"
    special_ext_lower = special_ext.lower()
    skip_names = set(trashes or ())

    def renamed_name(item_name):
        """Return the target name for a companion item, or ``None``."""
        current_base, extension = os.path.splitext(item_name)
        current_base2, extension2 = os.path.splitext(current_base)
        extension2 = extension2 + extension

        # Full name match (often a directory such as ``movie.cache``).
        if item_name == old_base_name:
            return new_base_name

        # Reference-subtitle suffix match.
        if (item_name.lower().endswith(special_ext_lower)
                and item_name[:-len(special_ext)] == old_base_name):
            return new_base_name + special_ext

        # Multi-part suffix such as ``movie.EMBEDDED.srt``.
        if current_base2 == old_base_name:
            return new_base_name + extension2

        # General case: ``movie.en.srt``, ``movie.mkv``, ``movie.cache``.
        if current_base == old_base_name:
            return new_base_name + extension
        return None

    def walk(directory):
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            ops.append(f"ERROR: cannot scan {directory!r}: {exc}")
            return

        for entry in entries:
            item_name = entry.name
            if item_name in skip_names:
                continue
            new_item_name = renamed_name(item_name)
            if not new_item_name:
                continue

            full_old_path = entry.path
            full_new_path = os.path.join(directory, new_item_name)
            is_dir = entry.is_dir(follow_symlinks=False)
            try:
                _rename_no_clobber(full_old_path, full_new_path, is_dir)
            except FileExistsError:
                ops.append(
                    f"ERROR: refusing to overwrite existing {full_new_path!r}")
                continue
            except OSError as exc:
                ops.append(
                    f"ERROR: rename {full_old_path!r} {full_new_path!r}: {exc}")
                continue

            ops.append(f"rename {full_old_path!r} {full_new_path!r}")
            if is_dir:
                walk(full_new_path)

    walk(old_dir)
    return ops
