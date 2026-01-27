"""
PersistentState class for saving user preferences
"""
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=consider-using-dict-items
import json
from pathlib import Path


class PersistentState:
    """Manages persistent state for user preferences"""

    def __init__(self, config_path=None):
        """Initialize persistent state

        Args:
            config_path: Path to config file (default: ~/.config/rmbloat/state.json)
        """
        if config_path is None:
            config_dir = Path.home() / '.config' / 'rmbloat'
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / 'state.json'

        self.config_path = Path(config_path)
        self.state = {
            'theme': 'default',
        }
        self.dirty = False
        self.load()

    def save_updated_opts(self, opts):
        """Save updated option variables from opts to state"""
        for key in self.state:
            if hasattr(opts, key):
                value = getattr(opts, key)
                if self.state[key] != value:
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
        """Save state to disk if dirty"""
        if not self.dirty:
            return

        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
            self.dirty = False
        except IOError as e:
            print(f'Warning: Could not save state to {self.config_path}: {e}')

    def sync(self):
        """Save state if dirty (called each loop)"""
        self.save()
