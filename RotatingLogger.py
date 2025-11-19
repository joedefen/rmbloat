#!/usr/bin/env python3
# pylint: disable=too-many-statements
import os
import sys
import inspect
import time
from pathlib import Path
from datetime import datetime
import json # Used for structured logging demo

class RotatingLogger:
    """
    A two-file rotating logger designed for application history logging.

    The active log is always 'log_0.txt'. When it reaches MAX_BYTES, it is
    renamed to 'log_1.txt' (the archive), and a new 'log_0.txt' is created.
    """
    MAX_BYTES = 4 * 1024 * 1024  # 4 MB
    # Log files are now ordered: [Active, Backup]
    LOG_FILES = ['log_0.txt', 'log_1.txt']
    # Indentation for subsequent lines of a multi-line message
    INDENT = "    " 

    def __init__(self, app_name='my_app', log_dir: str | Path | None = None):
        """
        Initializes the logger and sets up the configuration directory.

        :param app_name: The name of the application, used as a subdirectory name.
        :param log_dir: Optional base directory for logs. If None, uses ~/.config/.
                        If provided, logs go to {log_dir}/{app_name}/.
        """
        self.app_name = app_name
        self.log_dir_override = log_dir
        self._setup_paths()
        # Active file is always at index 0 (log_0.txt)

    def _setup_paths(self):
        """
        Calculates the appropriate configuration directory and defines the log file paths.
        Uses log_dir_override if provided, otherwise defaults to ~/.config/.
        """
        try:
            if self.log_dir_override:
                # Use the provided log_dir: {log_dir}/{app_name}
                base_dir = Path(self.log_dir_override)
            else:
                # Default behavior: ~/.config/{app_name}
                base_dir = Path.home() / '.config'
            
            # The final configuration directory path
            config_dir = base_dir / self.app_name
            config_dir.mkdir(parents=True, exist_ok=True)
            
            # 2. Define the absolute paths for the two log files
            self.log_paths = [config_dir / name for name in self.LOG_FILES]
        except Exception as e:
            # Fallback in case of permission or system issues
            print(f"Error setting up log directory: {e}. Falling back to current directory.", file=sys.stderr)
            self.log_paths = [Path(name) for name in self.LOG_FILES]

    @property
    def _active_path(self):
        """Returns the pathlib.Path object for the currently active log file (always log_0.txt)."""
        # The active log is always the first path in the list
        return self.log_paths[0]

    def _rotate_log(self):
        """
        Implements the move-and-recreate rotation scheme:
        1. Deletes the old backup (log_1.txt).
        2. Renames the active log (log_0.txt) to the backup (log_1.txt).
        3. Writes a rotation notification to the newly created log_0.txt.
        """
        active_path = self.log_paths[0] # log_0.txt
        backup_path = self.log_paths[1] # log_1.txt
        
        try:
            # 1. Delete the old backup (log_1.txt) to make way for the new backup
            if backup_path.exists():
                backup_path.unlink()
            
            # 2. Rename the active file (log_0.txt) to the backup file (log_1.txt)
            if active_path.exists():
                active_path.rename(backup_path)
            
            # The next log operation will implicitly create the new, empty log_0.txt.
            
            # 3. Write a rotation notification to the new file (using the standard logging method)
            # caller_depth=1 points to the _rotate_log method call
            self.lg(f"--- LOG ROTATION: {active_path.name} moved to {backup_path.name}. New {active_path.name} is active. ---",
                    caller_depth=1)
            
        except OSError as e:
            # Handle potential file system errors
            print(f"Error during log rotation/renaming: {e}", file=sys.stderr)

    def _write_log(self, message_type: str, log_messages: list, caller_depth: int = 2):
        """
        Handles the core logic: checks for rotation, formats the message, and writes to the file.
        Now includes support for multi-line messages with indentation.

        :param message_type: Tag for the log entry (e.g., 'MSG', 'ERR', 'DB').
        :param log_messages: A list of strings/objects to be logged.
        :param caller_depth: How many stack frames to go back to find the calling code.
        """
        
        # 1. Check for rotation before writing
        try:
            # Check the size of the active file (log_0.txt)
            if self._active_path.exists() and os.path.getsize(self._active_path) >= self.MAX_BYTES:
                print(f"Log file {self._active_path.name} reached {self.MAX_BYTES / (1024 * 1024):.1f}MB. Rotating...")
                self._rotate_log()
        except OSError:
            # File system error, proceed with logging attempt but rotation skipped
            pass 

        # 2. Get caller information (filename and line number)
        caller_info = ""
        try:
            # We go back 'caller_depth' frames to find the user's call to lg/err/put
            frame = inspect.currentframe()
            for _ in range(caller_depth):
                if frame:
                    frame = frame.f_back
                else:
                    break
            
            if frame:
                line_number = frame.f_lineno
                file_name = Path(frame.f_code.co_filename).name
                caller_info = f"({file_name}:{line_number})"
            else:
                caller_info = "(unknown location)"
        except Exception:
            caller_info = "(unknown location)"

        # 3. Format the log entry with multi-line support
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # CRITICAL: Join all arguments into a single raw string
        raw_message = "".join(str(m) for m in log_messages)
        
        # Split the raw message into lines
        all_lines = raw_message.split('\n')
        
        formatted_lines = []
        
        # The main log line format: [TIMESTAMP] [TYPE] (FILE:LINE) CONTENT
        # The first line is prefixed with the header
        header = f"[{timestamp}] [{message_type}] {caller_info} "
        formatted_lines.append(header + all_lines[0])
        
        # Subsequent lines are prefixed with 4 spaces
        for subsequent_line in all_lines[1:]:
            formatted_lines.append(self.INDENT + subsequent_line)
        
        # Join lines and add final newline
        log_entry = '\n'.join(formatted_lines) + '\n'

        # 4. Write to the active file (which may be newly created after rotation)
        try:
            # Use 'a' mode for append. If the file doesn't exist (after rotation/rename), it creates it.
            with open(self._active_path, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except OSError as e:
            print(f"FATAL LOGGING ERROR writing to {self._active_path.name}: {e}", file=sys.stderr)
            
    def _prepare_messages(self, args):
        """
        Checks if the first argument is a list of strings and formats it by joining with '\n'.
        Otherwise, returns the arguments as-is.
        """
        if args and isinstance(args[0], list):
            # If the first argument is a list, join its elements with '\n'
            # and prepend it to the rest of the arguments
            list_message = '\n'.join(str(item) for item in args[0])
            return [list_message] + list(args[1:])
        return list(args)

    def put(self, message_type: str, *args, **kwargs):
        """
        Logs a message with an arbitrary MESSAGE_TYPE tag.
        
        If the first argument is a list of strings, they are joined by newline characters.
        Supports print-style arguments and multi-line messages with indentation.
        """
        prepared_args = self._prepare_messages(args)
        # caller_depth=2 points to the user's call to put()
        self._write_log(str(message_type).upper(), prepared_args, caller_depth=2)

    def lg(self, *args, **kwargs):
        """
        Logs an ordinary message with a 'MSG' tag.
        
        If the first argument is a list of strings, they are joined by newline characters.
        Supports print-style arguments and multi-line messages with indentation.
        """
        prepared_args = self._prepare_messages(args)
        # caller_depth=2 points to the user's call to lg()
        self._write_log("MSG", prepared_args, caller_depth=2)

    def err(self, *args, **kwargs):
        """
        Logs an error message with an 'ERR' tag.
        
        If the first argument is a list of strings, they are joined by newline characters.
        Supports print-style arguments and multi-line messages with indentation.
        """
        prepared_args = self._prepare_messages(args)
        # Print to stderr for immediate visibility of errors, then log formally
        print(f"!!! ERROR: {''.join(str(m) for m in prepared_args)}", file=sys.stderr) 
        # caller_depth=2 points to the user's call to err()
        self._write_log("ERR", prepared_args, caller_depth=2)

# Alias for standard use
Log = RotatingLogger


# --- Self-testing Main Block (formerly demo_app.py content) ---
if __name__ == "__main__":
    
    # Configuration for demo run
    APP_NAME = "MySuperApp"
    # Overriding the log directory to /tmp for easy access and cleanup during testing
    LOG_DIR_OVERRIDE = "/tmp" 
    LOG_MSG_COUNT = 3
    
    # Instantiate the logger, using the /tmp override for testing
    logger = RotatingLogger(app_name=APP_NAME, log_dir=LOG_DIR_OVERRIDE)

    print(f"Logger initialized for application: {APP_NAME}")
    print(f"Logs are being written to: {logger._active_path}")
    print("-" * 50)
    
    # 1. Log an ordinary message
    logger.lg("Application started successfully in demonstration mode.")
    
    # --- DEMO 1: Standard lg() with manual multi-line message ---
    logger.lg("This is a manual multiple\nline log message demonstrating the 4-space indent.")
    
    # --- DEMO 2: NEW FEATURE: lg() with list of strings ---
    multi_step_log = [
        "Starting file integrity check:",
        "  - Comparing checksums with master manifest.",
        "  - Found 3 corrupted files.",
        "Check FAILED."
    ]
    logger.lg(multi_step_log, "Process finished.")
    
    # --- DEMO 3: Custom put() with structured, multi-line data (using JSON) ---
    data_dict = {
        "status": "success",
        "records_processed": 1000,
        "duration_ms": 45.2,
        "source": "api_endpoint_v2"
    }
    logger.put("API", "Database transaction committed successfully.\n" + 
                     json.dumps(data_dict, indent=4))
    
    # --- DEMO 4: err() with multi-line stack trace ---
    try:
        raise ValueError("Configuration file missing.")
    except ValueError as e:
        # Constructing a multi-line error message explicitly using f-string newlines
        error_details = (f"A critical setup error occurred: {str(e)}\n"
                         f"Check the configuration environment variable.\n"
                         f"--- Simplified Traceback ---\n"
                         f"Line: {sys._getframe().f_lineno} in {Path(sys._getframe().f_code.co_filename).name}")
        logger.err(error_details)
        
    # 5. Log a loop of messages
    print(f"Logging {LOG_MSG_COUNT} routine messages...")
    for i in range(1, LOG_MSG_COUNT + 1):
        if i == 2:
             logger.lg("Mid-run check-in. Everything looks okay.")
        else:
            logger.lg(f"Loop iteration {i} of {LOG_MSG_COUNT} completed.")
        time.sleep(0.001)
        
    # 6. Final message
    logger.lg("Application shutdown initiated.")
    
    print("-" * 50)
    print(f"Demonstration complete.")
    print(f"Check the log files in the directory: {logger._active_path.parent}")