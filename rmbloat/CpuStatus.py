#!/usr/bin/env python3
import time
import collections
from typing import Tuple, Deque, List

class CpuStatus:
    """
    Monitors CPU utilization on a Linux system using /proc/stat and /proc/cpuinfo.

    The utilization is calculated as a smoothed percentage over a defined time window.
    """

    def __init__(self, window_duration: float = 3.0):
        """Initializes the core count and the history tracking."""

        # Core & Capacity setup
        self.core_count = self._get_core_count()
        self.max_capacity = self.core_count * 100

        # History setup
        self._history: Deque[Tuple[int, int, float]] = collections.deque()
        self.window_duration = window_duration

        # Store the initial reading to populate the history
        self._add_current_reading()
        # Initial usage is 0 until the first calculation
        self._current_usage = 0

    # --- Private/Internal Methods ---

    def _get_core_count(self) -> int:
        """Reads /proc/cpuinfo to determine the number of logical cores."""
        try:
            with open("/proc/cpuinfo", "r") as f:
                return sum(1 for line in f if line.startswith("processor"))
        except FileNotFoundError:
            print("Warning: /proc/cpuinfo not found. Defaulting to 1 core.")
            return 1
        except Exception as e:
            print(f"Error reading /proc/cpuinfo: {e}. Defaulting to 1 core.")
            return 1

    def _get_current_jiffies(self) -> Tuple[int, int]:
        """Reads the first line of /proc/stat and returns (work_jiffies, total_jiffies)."""
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline().split()
                if not line or line[0] != 'cpu':
                    raise ValueError("Invalid format in /proc/stat")

                # Work Jiffies = user + nice + system + irq + softirq + steal
                work_jiffies_fields = [1, 2, 3, 6, 7, 8]
                work_jiffies = sum(int(line[i]) for i in work_jiffies_fields if i < len(line))

                # Total Jiffies = sum of all fields (from index 1 onwards)
                total_jiffies = sum(int(line[i]) for i in range(1, len(line)))

                return work_jiffies, total_jiffies
        except Exception as e:
            raise RuntimeError(f"Failed to read CPU stats from /proc/stat: {e}")

    def _add_current_reading(self):
        """Helper to fetch and store the current data point in the history."""
        try:
            work_jiffies, total_jiffies = self._get_current_jiffies()
            timestamp = time.monotonic()
            self._history.append((total_jiffies, work_jiffies, timestamp))
        except RuntimeError as e:
            print(f"Warning: Could not add reading to history: {e}")

    def _update_stats(self) -> int:
        """
        Calculates the smoothed CPU utilization percentage based on the sliding window.
        This method is now **internal**.
        """
        # 1. Take a new reading and add it to the history
        self._add_current_reading()
        now = time.monotonic()

        # 2. Prune the history: Discard points if there is a younger
        #  point as old as the window_duration
        while (len(self._history) > 2 and
              (now - self._history[1][2]) >= self.window_duration):
            self._history.popleft()

        # 3. Check if we have enough data for a meaningful delta
        if len(self._history) < 2:
            return self._current_usage

        # 4. Calculate the delta using the oldest (index 0) and newest (index -1) points
        total_jiffies_old, work_jiffies_old, _ = self._history[0]
        total_jiffies_new, work_jiffies_new, _ = self._history[-1]

        delta_total = total_jiffies_new - total_jiffies_old
        delta_work = work_jiffies_new - work_jiffies_old

        if delta_total == 0:
            return self._current_usage

        # Formula: (change_in_work / change_in_total) * 100 * core_count
        raw_usage = (delta_work / delta_total) * 100 * self.core_count

        self._current_usage = min(round(raw_usage), self.max_capacity)
        return self._current_usage

    # --- Public Interface Methods ---

    @property
    def usage_percent(self) -> int:
        """
        Returns the last calculated usage percentage (0-MaxCapacity).

        **Calls the internal update method to ensure the data is fresh.**
        """
        # Trigger the calculation every time this property is accessed
        self._update_stats()
        return self._current_usage

    def get_status_string(self) -> str:
        """
        Returns the CPU status in the desired 'CPU=Used/Max%' format.

        **Calls usage_percent to ensure a fresh calculation.**
        """
        # Accessing the property calls _update_stats internally
        used = self.usage_percent
        return f"CPU={used}/{self.max_capacity}%"

    @property
    def capacity(self) -> int:
        """Returns the total capacity in percent (cores * 100)."""
        # This is a static value, no update needed
        return self.max_capacity

# -------------------------
## 💡 Example Usage

if __name__ == "__main__":
    # Define the averaging window externally
    WINDOW = 3.0
    # Define the refresh rate for the caller
    REFRESH_RATE = 0.5

    # Initialize with the desired window duration
    cpu_monitor = CpuStatus(window_duration=WINDOW)
    print(f"Total logical cores: {cpu_monitor.core_count}")
    print(f"Max CPU Capacity: {cpu_monitor.capacity}%")
    print(f"Smoothing Window: {cpu_monitor.window_duration} seconds")
    print("-" * 35)

    print("Note: Now, accessing the properties triggers the calculation.")

    for i in range(1, 11):
        # The caller is responsible for the time interval between checks
        time.sleep(REFRESH_RATE)

        # Accessing get_status_string() automatically triggers the update
        status = cpu_monitor.get_status_string()
        print(f"Sample {i}: {status}")