"""
PersistentState class for saving user preferences
"""
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=consider-using-dict-items
import json
import os
import tempfile
from pathlib import Path

from .IniManager import IniManager


class PersistentState:
    """Manages persistent state for user preferences"""

    def __init__(self, config_path=None):
        """Initialize persistent state

        Args:
            config_path: Path to config file (default: ~/.config/rmbloat/state.json)
        """
        if config_path is None:
            config_dir = IniManager.default_config_dir('rmbloat')
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / 'state.json'

        self.config_path = Path(config_path)
        self.state = {
            'theme': 'default',
        }
        self.dirty = False
        self.load()

    def set(self, key, value):
        """Set one state value, marking the file dirty when it changed."""
        if self.state.get(key) != value:
            self.state[key] = value
            self.dirty = True

    def restore_updated_opts(self, opts):
        """Restore option variables from state to opts"""
        for key in self.state:
            if hasattr(opts, key):
                setattr(opts, key, self.state[key])

    def load(self):
        """Load state from disk"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    self.state.update(loaded)
            except (json.JSONDecodeError, IOError) as e:
                print(f'Warning: Could not load state from {self.config_path}: {e}')

    def save(self):
        """Save state to disk if dirty

        The new content is written to a temporary file in the same directory
        and then atomically moved into place with :func:`os.replace`, so a
        crash or power loss mid-write cannot truncate or corrupt ``state.json``.
        """
        if not self.dirty:
            return

        temp_path = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f'.{self.config_path.name}.',
                suffix='.tmp',
                dir=self.config_path.parent,
                text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.config_path)
            temp_path = None
            self.dirty = False
        except OSError as e:
            print(f'Warning: Could not save state to {self.config_path}: {e}')
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def sync(self):
        """Save state if dirty (called each loop)"""
        self.save()
