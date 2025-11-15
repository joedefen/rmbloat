import configparser
import os
import types
from pathlib import Path

class ConfigManager:
    """
    A class to manage application configuration stored in a config.ini file
    within the user's home directory.

    All configuration fields are stored within a SimpleNamespace object accessible
    via the 'vals' attribute (e.g., cfg.vals.field1). This avoids Pylint/static
    analysis errors related to dynamic attribute creation.

    The constructor handles initial setup:
    1. Determines the file path (~/app_name/config.ini).
    2. Creates the directory if it doesn't exist.
    3. Populates object attributes (self.vals) using either existing INI values 
       (if file exists and fields are present) or the provided constructor defaults.
    4. Ensures the config.ini file is written/updated to reflect the final state.
    """

    SECTION = 'options'

    def __init__(self, app_name, **kwargs):
        """
        Initializes the ConfigManager and performs initial read/write operations.

        :param app_name: The name of the application, used for the directory (~/app_name).
        :param kwargs: Keyword arguments representing field names and their default values.
                       e.g., field1='val1', field2=10
        """
        if not app_name:
            raise ValueError("app_name must be provided.")

        self.app_name = app_name
        self._default_values = kwargs
        self._config_file_path = self._determine_config_path()
        self._ensure_directory_exists()
        
        # New: Initialize SimpleNamespace to hold all configuration values
        self.vals = types.SimpleNamespace()

        # 1. Initialize self.vals attributes with constructor defaults
        for key, value in kwargs.items():
            setattr(self.vals, key, value)

        # 2. Attempt to read from file, overriding defaults if successful
        config = self._read_config_file()
        is_file_missing_or_mismatch = not config.sections() or self.SECTION not in config

        if not is_file_missing_or_mismatch:
            # File exists and has the section. Read existing values.
            self._update_vals_from_config(config)

            # Check if existing fields match expectations (the 'if needed' logic)
            file_keys = set(config.options(self.SECTION))
            default_keys = set(self._default_values.keys())

            # If the file is missing keys or has extra keys, a write is needed.
            if file_keys != default_keys:
                print(f"INFO: Config mismatch detected in '{self._config_file_path}'. Updating INI file.")
                is_file_missing_or_mismatch = True
            else:
                print(f"INFO: Config file read successfully from '{self._config_file_path}'.")

        # 3. If file was missing or structure didn't match, write the current state
        if is_file_missing_or_mismatch:
            self.write()


    def _determine_config_path(self):
        """Calculates the full path to the config.ini file."""
        home_dir = Path.home()
        app_dir = home_dir / self.app_name
        return app_dir / 'config.ini'

    def _ensure_directory_exists(self):
        """Creates the application configuration directory if it doesn't exist."""
        try:
            os.makedirs(self._config_file_path.parent, exist_ok=True)
        except OSError as e:
            print(f"ERROR: Could not create directory {self._config_file_path.parent}. Error: {e}")
            raise

    def _read_config_file(self):
        """Reads the INI file into a ConfigParser object."""
        config = configparser.ConfigParser()
        if os.path.exists(self._config_file_path):
            try:
                config.read(self._config_file_path)
            except Exception as e:
                print(f"WARNING: Failed to read config file: {e}. Using defaults.")
        return config

    def _update_vals_from_config(self, config):
        """Sets object attributes within self.vals based on values read from the ConfigParser."""
        if self.SECTION in config:
            for key in self._default_values.keys():
                if key in config[self.SECTION]:
                    try:
                        # Attempt to cast back based on the type of the default value.
                        default_type = type(self._default_values[key])
                        
                        value_to_set = config[self.SECTION][key]

                        if default_type is int:
                            value_to_set = config.getint(self.SECTION, key)
                        elif default_type is bool:
                            value_to_set = config.getboolean(self.SECTION, key)
                        elif default_type is float:
                            value_to_set = config.getfloat(self.SECTION, key)
                        
                        setattr(self.vals, key, value_to_set)

                    except ValueError:
                        current_val = getattr(self.vals, key)
                        print(f"WARNING: Value for '{key}' in file is invalid. Using default value: {current_val}")


    def read(self):
        """
        Reads configuration values FROM the INI file TO the object attributes (self.vals).
        This updates the object's state to match the file's current state.
        """
        config = self._read_config_file()
        if self.SECTION in config:
            self._update_vals_from_config(config)
            print(f"Read operation complete. Object attributes updated from {self._config_file_path}")
        else:
            print(f"Read failed: Section '{self.SECTION}' not found in {self._config_file_path}")

    def write(self):
        """
        Writes configuration values FROM the object attributes (self.vals) TO the INI file.
        This saves the object's current state to disk.
        """
        config = configparser.ConfigParser()
        config[self.SECTION] = {}

        # Transfer all current attribute values from self.vals
        for key in self._default_values.keys():
            value = getattr(self.vals, key)
            config[self.SECTION][key] = str(value)

        try:
            with open(self._config_file_path, 'w') as configfile:
                config.write(configfile)
            print(f"Write operation complete. Configuration saved to {self._config_file_path}")
        except Exception as e:
            print(f"ERROR: Failed to write config file to {self._config_file_path}. Error: {e}")


# --- Example Usage ---
if __name__ == '__main__':
    APP_NAME = "my_python_app_config_test"

    print("--- 1. First run (config file does not exist) ---")
    print(f"Expected path: {Path.home() / APP_NAME / 'config.ini'}")

    # Create the manager, it should create the file with these defaults
    manager1 = ConfigManager(
        app_name=APP_NAME,
        log_level="DEBUG",
        max_threads=5,
        is_enabled=True
    )

    # Note the access change: manager1.vals.log_level
    print(f"Initial attributes: LogLevel={manager1.vals.log_level}, Threads={manager1.vals.max_threads}, Enabled={manager1.vals.is_enabled}\n")

    # Change attributes in memory
    manager1.vals.log_level = "INFO"
    manager1.vals.max_threads = 10
    print(f"Updated attributes in memory: LogLevel={manager1.vals.log_level}, Threads={manager1.vals.max_threads}\n")

    # Save changes to the file
    manager1.write()
    print("-" * 40 + "\n")


    print("--- 2. Second run (config file exists and fields match) ---")
    # This run should read the updated values (INFO, 10) from the file
    manager2 = ConfigManager(
        app_name=APP_NAME,
        log_level="TRACE", # Default will be ignored
        max_threads=1,     # Default will be ignored
        is_enabled=True    # Default will be read
    )

    print(f"Loaded attributes: LogLevel={manager2.vals.log_level}, Threads={manager2.vals.max_threads}, Enabled={manager2.vals.is_enabled}\n")
    print("-" * 40 + "\n")


    print("--- 3. Third run (adding a new field - requires a write) ---")
    # Adding a new field 'timeout_seconds' with a default of 30
    manager3 = ConfigManager(
        app_name=APP_NAME,
        log_level="INFO",
        max_threads=10,
        is_enabled=True,
        timeout_seconds=30
    )
    # The constructor should have printed an INFO message about updating the INI file
    print(f"Loaded attributes: Timeout={manager3.vals.timeout_seconds}")
    print("The config.ini file should now include 'timeout_seconds=30'")

    # Clean up the test files/folder (optional)
    # import shutil
    # shutil.rmtree(manager3._config_file_path.parent)