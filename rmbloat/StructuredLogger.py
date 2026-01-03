#!/usr/bin/env python3
"""
Structured Logger with JSON Lines format and fast indexing for TUI display.
"""
import os
import sys
import json
import gzip
import inspect
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Set, Tuple
import mmap
from collections import defaultdict

# ============================================================================
# Data Classes for Structured Logging
# ============================================================================

@dataclass
class LogEntry:
    """Structured log entry for JSON Lines format."""
    timestamp: str
    level: str  # 'ERR', 'OK', 'MSG', 'DBG', etc.
    file: str
    line: int
    function: str
    module: str = ""
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    _raw: str = ""  # Original raw message
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)
        # Remove private fields
        result.pop('_raw', None)
        return result
    
    @property
    def location(self) -> str:
        """Short location string for display."""
        return f"{self.file}:{self.line}"

@dataclass
class LogIndexEntry:
    """Lightweight index entry for fast TUI display."""
    position: int  # Byte position in file
    timestamp: str
    level: str
    file: str
    line: int
    function: str
    message_preview: str
    data_size: int = 0  # Size of data field in bytes
    
    def summary_line(self, show_time: bool = True) -> str:
        """Create a summary line for TUI display."""
        time_part = f"{self.timestamp[11:19]} " if show_time else ""
        preview = self.message_preview[:40] + "..." if len(self.message_preview) > 40 else self.message_preview
        return f"{time_part}[{self.level}] {self.file}:{self.line} {preview}"

# ============================================================================
# Main Logger Class
# ============================================================================

class StructuredLogger:
    """
    Structured logger using JSON Lines format with separate error/event files
    and fast indexing for TUI display.
    """
    
    # Size limits (adjust as needed)
    MAX_EVENTS_SIZE = 10 * 1024 * 1024  # 10 MB
    MAX_ERRORS_SIZE = 5 * 1024 * 1024   # 5 MB
    TRIM_RATIO = 0.33  # Cut by 1/3 when trimming
    
    # Compression for archived logs
    COMPRESS_ARCHIVES = True
    ARCHIVE_DAYS_TO_KEEP = 30
    
    def __init__(self, app_name: str = 'my_app', 
                 log_dir: Optional[Path] = None,
                 session_id: str = ""):
        """
        Initialize the structured logger.
        
        Args:
            app_name: Application name for log directory
            log_dir: Optional override for log directory
            session_id: Optional session identifier for log correlation
        """
        self.app_name = app_name
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._setup_paths(log_dir)
        
        # In-memory indices for fast TUI access
        self.events_index: List[LogIndexEntry] = []
        self.errors_index: List[LogIndexEntry] = []
        
        # Load existing indices
        self._build_indices()
        
        # Statistics
        self.stats = {
            'events_written': 0,
            'errors_written': 0,
            'last_trim': datetime.now()
        }
    
    def _setup_paths(self, log_dir: Optional[Path]) -> None:
        """Set up log directory and file paths."""
        try:
            if log_dir:
                base_dir = Path(log_dir)
            else:
                base_dir = Path.home() / '.config'
            
            # Create app-specific directory
            self.log_dir = base_dir / self.app_name
            self.log_dir.mkdir(parents=True, exist_ok=True)
            
            # Main log files (JSON Lines format)
            self.events_file = self.log_dir / "events.jsonl"
            self.errors_file = self.log_dir / "errors.jsonl"

            # Archive directory
            self.archive_dir = self.log_dir / "archive"
            self.archive_dir.mkdir(exist_ok=True)
            
        except Exception as e:
            print(f"FATAL: Cannot setup log directory: {e}", file=sys.stderr)
            # Fallback to current directory
            self.log_dir = Path.cwd()
            self.events_file = Path("events.jsonl")
            self.errors_file = Path("errors.jsonl")
            self.archive_dir = Path("archive")
    
    def _get_caller_info(self, depth: int = 3) -> tuple:
        """Get caller information from stack frame."""
        try:
            frame = inspect.currentframe()
            for _ in range(depth):
                if frame:
                    frame = frame.f_back
            
            if frame:
                return (
                    Path(frame.f_code.co_filename).name,
                    frame.f_lineno,
                    frame.f_code.co_name,
                    frame.f_code.co_filename.split('/')[-2] if '/' in frame.f_code.co_filename else ""
                )
        except Exception:
            pass
        return ("unknown", 0, "unknown", "")
    
    def _create_log_entry(self, level: str, *args, 
                         data: Optional[Dict] = None,
                         **kwargs) -> LogEntry:
        """Create a structured log entry."""
        file, line, function, module = self._get_caller_info()
        timestamp = datetime.now().isoformat()
        message = " ".join(str(arg) for arg in args)
        
        return LogEntry(
            timestamp=timestamp,
            level=level,
            file=file,
            line=line,
            function=function,
            module=module,
            message=message,
            data=data or {},
            session_id=self.session_id,
            _raw=message
        )
    
    def _create_index_entry(self, entry: LogEntry, position: int) -> LogIndexEntry:
        """Create an index entry from a log entry."""
        return LogIndexEntry(
            position=position,
            timestamp=entry.timestamp,
            level=entry.level,
            file=entry.file,
            line=entry.line,
            function=entry.function,
            message_preview=entry.message[:50],
            data_size=len(json.dumps(entry.data)) if entry.data else 0
        )
    
    def _append_jsonl(self, file_path: Path, entry: LogEntry, 
                     max_size: int, is_error: bool = False) -> None:
        """
        Append entry to JSONL file, trimming if necessary.
        
        Args:
            file_path: Path to JSONL file
            entry: Log entry to append
            max_size: Maximum file size before trimming
            is_error: Whether this is an error log (different trimming)
        """
        # Check if we need to trim
        if file_path.exists() and file_path.stat().st_size >= max_size:
            self._trim_jsonl_file(file_path, max_size, is_error)
        
        # Write the entry
        try:
            with open(file_path, 'a', encoding='utf-8') as f:
                position = f.tell()
                json_line = json.dumps(entry.to_dict())
                f.write(json_line + '\n')
            
            # Update in-memory index
            index_entry = self._create_index_entry(entry, position)
            if is_error:
                self.errors_index.append(index_entry)
                self.stats['errors_written'] += 1
            else:
                self.events_index.append(index_entry)
                self.stats['events_written'] += 1

        except Exception as e:
            print(f"LOG WRITE ERROR: {e}", file=sys.stderr)

    def _trim_jsonl_file(self, file_path: Path, max_size: int, 
                        is_error: bool = False) -> None:
        """
        Trim JSONL file by removing oldest complete entries.
        
        Args:
            file_path: Path to JSONL file
            max_size: Maximum allowed size
            is_error: Whether this is an error file (different behavior)
        """
        if not file_path.exists():
            return
        
        current_size = file_path.stat().st_size
        if current_size <= max_size:
            return
        
        try:
            # Read all lines
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Calculate how many lines to remove (keep newest)
            target_size = int(len(lines) * (1 - self.TRIM_RATIO))
            if target_size < 1:
                target_size = 1
            
            # Keep only the newest lines
            trimmed_lines = lines[-target_size:]
            
            # Write back
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(trimmed_lines)
            
            # Archive the removed lines if they contain errors
            if is_error and len(lines) > target_size:
                self._archive_entries(lines[:-target_size], is_error=True)
            
            # Rebuild index for this file
            self._rebuild_file_index(file_path, is_error)
            
            self.stats['last_trim'] = datetime.now()
            
        except Exception as e:
            print(f"TRIM ERROR for {file_path}: {e}", file=sys.stderr)
    
    def _archive_entries(self, lines: List[str], is_error: bool = False) -> None:
        """Archive old log entries."""
        if not lines:
            return
        
        # Create archive filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_type = "errors" if is_error else "events"
        archive_name = f"{archive_type}_{timestamp}.jsonl"
        
        if self.COMPRESS_ARCHIVES:
            archive_name += ".gz"
            archive_path = self.archive_dir / archive_name
            
            try:
                with gzip.open(archive_path, 'wt', encoding='utf-8') as f:
                    f.writelines(lines)
            except Exception:
                pass  # Non-critical
        else:
            archive_path = self.archive_dir / archive_name
            try:
                with open(archive_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
            except Exception:
                pass
        
        # Clean old archives
        self._clean_old_archives()
    
    def _clean_old_archives(self) -> None:
        """Remove archive files older than ARCHIVE_DAYS_TO_KEEP."""
        cutoff = datetime.now() - timedelta(days=self.ARCHIVE_DAYS_TO_KEEP)
        
        for archive_file in self.archive_dir.glob("*.jsonl*"):
            try:
                if archive_file.stat().st_mtime < cutoff.timestamp():
                    archive_file.unlink()
            except Exception:
                pass
    
    def _build_indices(self) -> None:
        """Build in-memory indices from log files."""
        self._build_file_index(self.events_file, is_error=False)
        self._build_file_index(self.errors_file, is_error=True)
    
    def _build_file_index(self, file_path: Path, is_error: bool) -> None:
        """Build index for a specific file."""
        if not file_path.exists():
            return
        
        index = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                position = 0
                for line in f:
                    if line.strip():
                        try:
                            entry_dict = json.loads(line)
                            # Convert back to LogEntry structure
                            entry = LogEntry(**entry_dict)
                            index_entry = self._create_index_entry(entry, position)
                            index.append(index_entry)
                        except json.JSONDecodeError:
                            pass
                    position = f.tell()
        except Exception as e:
            print(f"INDEX BUILD ERROR for {file_path}: {e}", file=sys.stderr)
        
        if is_error:
            self.errors_index = index
        else:
            self.events_index = index
    
    def _rebuild_file_index(self, file_path: Path, is_error: bool) -> None:
        """Rebuild index for a file after trimming."""
        self._build_file_index(file_path, is_error)
    
    # ========================================================================
    # Public API
    # ========================================================================
    
    def event(self, *args, data: Optional[Dict] = None, **kwargs) -> None:
        """Log an event (successful operation)."""
        entry = self._create_log_entry("OK", *args, data=data, **kwargs)
        self._append_jsonl(self.events_file, entry, self.MAX_EVENTS_SIZE, is_error=False)
    
    def error(self, *args, data: Optional[Dict] = None, **kwargs) -> None:
        """Log an error."""
        entry = self._create_log_entry("ERR", *args, data=data, **kwargs)
        self._append_jsonl(self.errors_file, entry, self.MAX_ERRORS_SIZE, is_error=True)
        
        # Also print to stderr for immediate visibility
        print(f"ERROR: {args[0] if args else ''}", file=sys.stderr)
        if data:
            print(f"  Data: {json.dumps(data, indent=2)[:200]}...", file=sys.stderr)
    
    def info(self, *args, data: Optional[Dict] = None, **kwargs) -> None:
        """Log informational message."""
        entry = self._create_log_entry("MSG", *args, data=data, **kwargs)
        self._append_jsonl(self.events_file, entry, self.MAX_EVENTS_SIZE, is_error=False)
    
    def debug(self, *args, data: Optional[Dict] = None, **kwargs) -> None:
        """Log debug message."""
        entry = self._create_log_entry("DBG", *args, data=data, **kwargs)
        self._append_jsonl(self.events_file, entry, self.MAX_EVENTS_SIZE, is_error=False)

    # ========================================================================
    # Backward Compatibility Aliases (for RotatingLogger API)
    # ========================================================================

    def lg(self, *args, **kwargs) -> None:
        """
        Alias for info() - backward compatibility with RotatingLogger.

        Logs an ordinary message with a 'MSG' tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        self.info(*args, **kwargs)

    def err(self, *args, **kwargs) -> None:
        """
        Alias for error() - backward compatibility with RotatingLogger.

        Logs an error message with an 'ERR' tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        self.error(*args, **kwargs)

    def put(self, message_type: str, *args, **kwargs) -> None:
        """
        Alias for custom level logging - backward compatibility with RotatingLogger.

        Logs a message with an arbitrary MESSAGE_TYPE tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        # Create entry with custom level
        entry = self._create_log_entry(str(message_type).upper(), *args,
                                      data=kwargs.get('data'), **kwargs)
        self._append_jsonl(self.events_file, entry, self.MAX_EVENTS_SIZE, is_error=False)

    # ========================================================================
    # Query Methods
    # ========================================================================

    def get_errors(self, limit: int = 100) -> List[LogEntry]:
        """Get recent error entries."""
        return self._get_entries(self.errors_file, self.errors_index, limit)
    
    def get_events(self, limit: int = 100) -> List[LogEntry]:
        """Get recent event entries."""
        return self._get_entries(self.events_file, self.events_index, limit)
    
    def _get_entries(self, file_path: Path, index: List[LogIndexEntry], 
                    limit: int) -> List[LogEntry]:
        """Get entries from file using index."""
        results = []
        if not file_path.exists():
            return results
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                # Get last 'limit' entries from index
                for idx_entry in index[-limit:]:
                    f.seek(idx_entry.position)
                    line = f.readline()
                    if line:
                        try:
                            entry_dict = json.loads(line)
                            results.append(LogEntry(**entry_dict))
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            print(f"READ ERROR for {file_path}: {e}", file=sys.stderr)
        
        return results
    
    def search(self, level: Optional[str] = None, 
               file: Optional[str] = None,
               function: Optional[str] = None,
               after: Optional[datetime] = None,
               before: Optional[datetime] = None,
               limit: int = 50) -> List[LogEntry]:
        """Search across both error and event logs."""
        all_entries = []
        
        # Search errors
        all_entries.extend(self._search_file(self.errors_file, self.errors_index, 
                                           level, file, function, after, before))
        # Search events
        all_entries.extend(self._search_file(self.events_file, self.events_index,
                                           level, file, function, after, before))
        
        # Sort by timestamp
        all_entries.sort(key=lambda x: x.timestamp, reverse=True)
        
        return all_entries[:limit]
    
    def _search_file(self, file_path: Path, index: List[LogIndexEntry],
                    level: Optional[str], file: Optional[str],
                    function: Optional[str], after: Optional[datetime],
                    before: Optional[datetime]) -> List[LogEntry]:
        """Search a specific file."""
        results = []
        if not file_path.exists():
            return results
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for idx_entry in index:
                    # Apply filters to index first (fast)
                    if level and idx_entry.level != level:
                        continue
                    if file and file not in idx_entry.file:
                        continue
                    if function and function != idx_entry.function:
                        continue
                    
                    # Read the actual entry
                    f.seek(idx_entry.position)
                    line = f.readline()
                    if line:
                        try:
                            entry_dict = json.loads(line)
                            entry = LogEntry(**entry_dict)
                            
                            # Apply timestamp filters
                            entry_time = datetime.fromisoformat(entry.timestamp)
                            if after and entry_time < after:
                                continue
                            if before and entry_time > before:
                                continue
                            
                            results.append(entry)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        
        return results

# ============================================================================
# TUI Viewer Class
# ============================================================================

class TUIStructuredLogViewer:
    """
    TUI viewer for structured logs with fast indexing and expandable entries.
    """
    
    def __init__(self, logger: StructuredLogger):
        self.logger = logger
        self.current_view = "errors"  # "errors", "events", or "all"
        self.selected_idx = 0
        self.scroll_offset = 0
        self.expanded_positions: Set[int] = set()
        self.filter_level: Optional[str] = None
        self.filter_file: Optional[str] = None
        self.search_term: Optional[str] = None
        
        # Cache for expanded entries
        self.entry_cache: Dict[int, LogEntry] = {}
    
    def get_current_index(self) -> List[LogIndexEntry]:
        """Get the appropriate index for current view."""
        if self.current_view == "errors":
            return self.logger.errors_index
        elif self.current_view == "events":
            return self.logger.events_index
        else:  # "all"
            # Combine and sort by timestamp (most recent first)
            combined = self.logger.errors_index + self.logger.events_index
            return sorted(combined, key=lambda x: x.timestamp, reverse=True)
    
    def get_filtered_index(self) -> List[LogIndexEntry]:
        """Get filtered index based on current filters."""
        index = self.get_current_index()
        
        filtered = []
        for entry in index:
            if self.filter_level and entry.level != self.filter_level:
                continue
            if self.filter_file and self.filter_file not in entry.file:
                continue
            filtered.append(entry)
        
        return filtered
    
    def render(self, height: int, width: int) -> List[str]:
        """Render log viewer interface."""
        lines = []
        
        # Header
        header = f"LOG VIEWER: {self.current_view.upper()}"
        if self.filter_level:
            header += f" [Level: {self.filter_level}]"
        if self.filter_file:
            header += f" [File: {self.filter_file}]"
        lines.append(header)
        lines.append("=" * min(width, 80))
        
        # Get filtered entries
        filtered_index = self.get_filtered_index()
        total_entries = len(filtered_index)
        
        if not filtered_index:
            lines.append("No log entries found.")
            return lines
        
        # Calculate viewport
        start_idx = max(0, self.selected_idx - height // 2)
        end_idx = min(total_entries, start_idx + height - 3)  # -3 for header/footer
        
        # Display entries
        for i in range(start_idx, end_idx):
            idx_entry = filtered_index[i]
            is_selected = (i == self.selected_idx)
            is_expanded = idx_entry.position in self.expanded_positions
            
            # Selection indicator
            prefix = "▶ " if is_selected else "  "
            
            # Summary line
            summary = idx_entry.summary_line(show_time=True)
            
            # Truncate to fit width
            if len(summary) > width - 3:
                summary = summary[:width - 6] + "..."
            
            lines.append(f"{prefix}{summary}")
            
            # Expanded view
            if is_expanded:
                entry = self._get_cached_entry(idx_entry.position)
                if entry:
                    # Show message
                    msg_line = f"    Message: {entry.message}"
                    if len(msg_line) > width:
                        msg_line = msg_line[:width-3] + "..."
                    lines.append(msg_line)
                    
                    # Show data if present
                    if entry.data:
                        data_str = json.dumps(entry.data, indent=2)
                        # Show first few lines of data
                        data_lines = data_str.split('\n')
                        for j, data_line in enumerate(data_lines[:5]):
                            lines.append(f"      {data_line}")
                        if len(data_lines) > 5:
                            lines.append(f"      ... ({len(data_lines)-5} more lines)")
        
        # Footer
        lines.append("-" * min(width, 80))
        footer = f"Entry {self.selected_idx + 1}/{total_entries}"
        if self.expanded_positions:
            footer += f" | {len(self.expanded_positions)} expanded"
        lines.append(footer)
        
        # Help hint
        lines.append("↑↓:Navigate  Space:Expand  E:Errors  V:Events  A:All  F:Filter  Q:Quit")
        
        return lines[:height]  # Ensure we don't exceed available height
    
    def _get_cached_entry(self, position: int) -> Optional[LogEntry]:
        """Get entry from cache or load from file."""
        if position in self.entry_cache:
            return self.entry_cache[position]
        
        # Determine which file contains this position
        file_path = None
        for idx_entry in self.logger.errors_index:
            if idx_entry.position == position:
                file_path = self.logger.errors_file
                break
        
        if not file_path:
            for idx_entry in self.logger.events_index:
                if idx_entry.position == position:
                    file_path = self.logger.events_file
                    break
        
        if file_path and file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    f.seek(position)
                    line = f.readline()
                    if line:
                        entry_dict = json.loads(line)
                        entry = LogEntry(**entry_dict)
                        self.entry_cache[position] = entry
                        return entry
            except Exception:
                pass
        
        return None
    
    def move_selection(self, delta: int) -> None:
        """Move selection up or down."""
        filtered_index = self.get_filtered_index()
        if not filtered_index:
            return
        
        new_idx = self.selected_idx + delta
        if 0 <= new_idx < len(filtered_index):
            self.selected_idx = new_idx
            
            # Auto-scroll
            if delta > 0 and self.selected_idx >= self.scroll_offset + 10:
                self.scroll_offset += 1
            elif delta < 0 and self.selected_idx < self.scroll_offset:
                self.scroll_offset = max(0, self.scroll_offset - 1)
    
    def toggle_expand(self) -> None:
        """Expand or collapse selected entry."""
        filtered_index = self.get_filtered_index()
        if not filtered_index or self.selected_idx >= len(filtered_index):
            return
        
        idx_entry = filtered_index[self.selected_idx]
        if idx_entry.position in self.expanded_positions:
            self.expanded_positions.remove(idx_entry.position)
            # Clear from cache if not used elsewhere
            if idx_entry.position in self.entry_cache:
                del self.entry_cache[idx_entry.position]
        else:
            self.expanded_positions.add(idx_entry.position)
    
    def set_view(self, view_type: str) -> None:
        """Switch between error, event, or all views."""
        if view_type in ["errors", "events", "all"]:
            self.current_view = view_type
            self.selected_idx = 0
            self.scroll_offset = 0
            self.expanded_positions.clear()
            self.entry_cache.clear()
    
    def set_filter(self, level: Optional[str] = None, 
                  file: Optional[str] = None) -> None:
        """Set filters for log display."""
        self.filter_level = level
        self.filter_file = file
        self.selected_idx = 0
        self.scroll_offset = 0
        self.expanded_positions.clear()
    
    def clear_filters(self) -> None:
        """Clear all filters."""
        self.filter_level = None
        self.filter_file = None
        self.search_term = None
        self.selected_idx = 0
        self.scroll_offset = 0

# ============================================================================
# Aliases for Backward Compatibility
# ============================================================================

# Alias for standard use (matches RotatingLogger pattern: Log = RotatingLogger)
Log = StructuredLogger

# ============================================================================
# Example Usage
# ============================================================================

def example_usage():
    """Example of how to use the structured logger."""
    
    # Create logger
    logger = StructuredLogger(
        app_name="VideoProcessor",
        session_id="session_12345"
    )
    
    print(f"Logs will be written to: {logger.log_dir}")
    print(f"Events file: {logger.events_file}")
    print(f"Errors file: {logger.errors_file}")
    
    # Log some events
    logger.info("Starting video processing batch")
    
    # Simulate processing
    for i in range(5):
        if i == 2:
            # Log an error with structured data
            logger.error(
                "Failed to encode video",
                data={
                    "filepath": f"/videos/video_{i}.mp4",
                    "error_code": 183,
                    "ffmpeg_output": ["Error opening input", "Invalid data"],
                    "attempts": 3
                }
            )
        else:
            # Log a successful event
            logger.event(
                f"Successfully encoded video_{i}",
                data={
                    "filepath": f"/videos/video_{i}.mp4",
                    "original_size": 1000000,
                    "encoded_size": 500000,
                    "reduction": "50%",
                    "duration_seconds": 120.5
                }
            )
    
    logger.info("Batch processing complete")
    
    # Demonstrate TUI viewer
    print("\n" + "="*60)
    print("TUI Viewer Simulation")
    print("="*60)
    
    viewer = TUIStructuredLogViewer(logger)
    
    # Show errors view
    viewer.set_view("errors")
    print("\nErrors View (simulated):")
    for line in viewer.render(height=20, width=80):
        print(line)
    
    # Show expanded view
    viewer.toggle_expand()
    print("\nExpanded Error View:")
    for line in viewer.render(height=20, width=80):
        print(line)
    
    # Get recent errors programmatically
    print("\n" + "="*60)
    print("Recent Errors (programmatic access):")
    recent_errors = logger.get_errors(limit=3)
    for error in recent_errors:
        print(f"{error.timestamp} [{error.level}] {error.location}: {error.message}")
        if error.data:
            print(f"  Data keys: {list(error.data.keys())}")

if __name__ == "__main__":
    example_usage()
