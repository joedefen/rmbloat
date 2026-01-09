#!/usr/bin/env python3
"""
SystemDiscovery - Detect system capabilities and available tools (Linux-only)

This module provides system capability detection including:
- Clipboard tools (wl-copy, xclip, xsel)
- Display server type (Wayland, X11, or headless)
- Available commands
"""

import os
import subprocess
import shutil
from typing import Optional, List


class SystemDiscovery:
    """Detects and caches Linux system capabilities"""

    def __init__(self):
        """Initialize and detect system capabilities"""
        self._clipboard_cmd = None
        self._display_server = None
        self._detect_display_server()
        self._detect_clipboard()

    def _detect_display_server(self) -> None:
        """Detect which display server is running (Wayland, X11, or headless)"""
        # Check for Wayland
        if os.environ.get('WAYLAND_DISPLAY'):
            self._display_server = 'wayland'
        # Check for X11
        elif os.environ.get('DISPLAY'):
            self._display_server = 'x11'
        else:
            self._display_server = 'headless'

    def _detect_clipboard(self) -> None:
        """Detect available clipboard tool and cache the command (Linux-only)"""
        # Linux clipboard tools in order of preference
        candidates: List[List[str]] = [
            ['wl-copy'],                                # Wayland
            ['xclip', '-selection', 'clipboard'],       # X11
            ['xsel', '--clipboard', '--input'],         # X11 alternative
        ]

        # Test each candidate to see if it's available AND actually works
        for cmd in candidates:
            if shutil.which(cmd[0]):
                # Tool exists, but can it actually connect?
                # Do a quick test with empty input
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    stdout, stderr = proc.communicate(input=b'', timeout=1)

                    # Check stderr for connection failures
                    stderr_text = stderr.decode('utf-8', errors='replace').lower()
                    if any(err in stderr_text for err in ['failed to connect', 'cannot open display', 'no such file or directory']):
                        # Connection failed, try next tool
                        continue

                    # If it returns 0, it definitely worked
                    # Return codes 1-2 might be ok (some tools return 1 for empty input)
                    if proc.returncode == 0 or (proc.returncode in (1, 2) and not stderr_text):
                        self._clipboard_cmd = cmd
                        return

                except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
                    # Tool exists but can't connect or timed out, try next
                    continue
                except Exception:
                    # Some other error, try next tool
                    continue

        # No clipboard tool found
        self._clipboard_cmd = None

    @property
    def clipboard_available(self) -> bool:
        """Check if clipboard functionality is available"""
        return self._clipboard_cmd is not None

    @property
    def clipboard_command(self) -> Optional[List[str]]:
        """Get the clipboard command, or None if not available"""
        return self._clipboard_cmd

    @property
    def display_server(self) -> str:
        """Get the detected display server type"""
        return self._display_server or 'unknown'

    def copy_to_clipboard(self, text: str) -> tuple[bool, Optional[str]]:
        """
        Copy text to clipboard.

        Args:
            text: The text to copy

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        if not self._clipboard_cmd:
            return False, "No clipboard tool found"

        try:
            proc = subprocess.Popen(
                self._clipboard_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = proc.communicate(input=text.encode('utf-8'), timeout=5)

            if proc.returncode == 0:
                return True, None
            else:
                error = stderr.decode('utf-8', errors='replace').strip()
                return False, f"Clipboard command failed: {error or 'unknown error'}"

        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "Clipboard command timed out"
        except Exception as exc:
            return False, f"Clipboard error: {exc}"

    def get_system_info(self) -> dict:
        """Get a summary of detected system capabilities"""
        return {
            'display_server': self._display_server or 'unknown',
            'clipboard_available': self.clipboard_available,
            'clipboard_command': ' '.join(self._clipboard_cmd) if self._clipboard_cmd else None,
        }


# Global singleton instance
_system_discovery = None


def get_system_discovery() -> SystemDiscovery:
    """Get the global SystemDiscovery singleton"""
    global _system_discovery
    if _system_discovery is None:
        _system_discovery = SystemDiscovery()
    return _system_discovery
