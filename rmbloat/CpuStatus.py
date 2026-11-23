#!/usr/bin/env python3
""" CPU Current Usage Support"""
import collections
import time


class CpuStatus:
    """Monitors CPU utilization on a Linux system using /proc/stat and /proc/cpuinfo.

    The utilization is calculated as a smoothed percentage over a defined time window.
    """

    def __init__(self, window_duration: float = 3.0):
        """Initializes the core count and the history tracking."""

        self.core_count = self._get_core_count()
        self.max_capacity = self.core_count * 100

        self._history: collections.deque[tuple[int, int, float]] = collections.deque()
        self.window_duration = window_duration

        self._add_current_reading()
        self._current_usage = 0

    # --- Private/Internal Methods ---

    def _get_core_count(self) -> int:
        """Reads /proc/cpuinfo to determine the number of logical cores."""
        try:
            with open("/proc/cpuinfo", encoding='utf-8') as f:
                return sum(1 for line in f if line.startswith("processor"))
        except FileNotFoundError:
            print("Warning: /proc/cpuinfo not found. Defaulting to 1 core.")
            return 1
        except Exception as e:
            print(f"Error reading /proc/cpuinfo: {e}. Defaulting to 1 core.")
            return 1

    def _get_current_jiffies(self) -> tuple[int, int]:
        """Reads the first line of /proc/stat and returns (work_jiffies, total_jiffies)."""
        try:
            with open("/proc/stat", encoding='utf-8') as f:
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
            raise RuntimeError(
                f"Failed to read CPU stats from /proc/stat: {e}") from None

    def _add_current_reading(self):
        """Helper to fetch and store the current data point in the history."""
        try:
            work_jiffies, total_jiffies = self._get_current_jiffies()
            timestamp = time.monotonic()
            self._history.append((total_jiffies, work_jiffies, timestamp))
        except RuntimeError as e:
            print(f"Warning: Could not add reading to history: {e}")

    def _update_stats(self) -> int:
        """Calculates the smoothed CPU utilization percentage based on the sliding window."""
        self._add_current_reading()
        now = time.monotonic()

        # Prune the history: Discard points if there is
        # a younger point as old as the window_duration
        while (len(self._history) > 2 and
              (now - self._history[1][2]) >= self.window_duration):
            self._history.popleft()

        if len(self._history) < 2:
            return self._current_usage

        total_jiffies_old, work_jiffies_old, _ = self._history[0]
        total_jiffies_new, work_jiffies_new, _ = self._history[-1]

        delta_total = total_jiffies_new - total_jiffies_old
        delta_work = work_jiffies_new - work_jiffies_old

        if delta_total == 0:
            return self._current_usage

        raw_usage = (delta_work / delta_total) * 100 * self.core_count

        self._current_usage = min(round(raw_usage), self.max_capacity)
        return self._current_usage

    # --- Public Interface Methods ---

    @property
    def usage_percent(self) -> int:
        """Returns the last calculated usage percentage (0-MaxCapacity)."""
        self._update_stats()
        return self._current_usage

    def get_status_string(self) -> str:
        """Returns the CPU status in the desired 'CPU=Used/Max%' format."""
        used = self.usage_percent
        return f"CPU={used}/{self.max_capacity}%"

    @property
    def capacity(self) -> int:
        """Returns the total capacity in percent (cores * 100)."""
        return self.max_capacity


if __name__ == "__main__": ## Example Usage
    WINDOW = 3.0
    REFRESH_RATE = 0.5

    cpu_monitor = CpuStatus(window_duration=WINDOW)
    print(f"Total logical cores: {cpu_monitor.core_count}")
    print(f"Max CPU Capacity: {cpu_monitor.capacity}%")
    print(f"Smoothing Window: {cpu_monitor.window_duration} seconds")
    print("-" * 35)

    print("Note: Now, accessing the properties triggers the calculation.")

    for i in range(1, 11):
        # The caller is responsible for the time interval between checks
        time.sleep(REFRESH_RATE)

        status = cpu_monitor.get_status_string()
        print(f"Sample {i}: {status}")
