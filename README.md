# toh265
convert video files to h65 with defaulted compression and conditionally

## What is Done
The project in python:
* takes a list of video files and under certain criteria (the kbps is too big or the vertical resolution is too big) converts the file to x265 (1080p or actual resolution if smaller) using ffmpeg.
* Ultimately, when good enuf, I'll likely make it a pypi project.
* It shows progress very smartly and compactly
* It renames files when resolution (TBD) or codec changes if the names had that info as well as companion files like .srt files.

## What is TBD
* I need to make it smarter about Intel QSV
* deciding (tentatively) which video files to process up front
* showing the complete list of video files in a curses "window" with those chosen to run automatically checked and have the ability to change what is checked
* it would show the resolution, the kbps, file size, and codec for each,
* it would then a GO command which would spawn the conversions in the background up to some number of jobs, showing status on each, allow aborting jobs not done
* it would automatically scroll if needed to show the eldest job running, and, of course, job status as percent done, ETA, etc.
* it would have a .json file (in its config directory) that has the last set of criteria which can be changed in the gui


## Gemini Assessment of Final Project
That is a very interesting project idea!

Here's a breakdown of your proposal's novelty and whether similar tools exist:

### 1. Overall Concept: Batch Conversion & Smart Selection

* **Batch Conversion with FFmpeg:** This is **not novel**. There are many ways to do batch conversion with FFmpeg on Linux, typically using shell scripts (`for` loops) or existing GUI frontends. The search results show several examples of basic command-line batching and even GUI tools like "FFmpeg Batch AV Converter" (which supports QuickSync, multiple jobs, and auto-renaming).
* **Smart Selection Criteria (kbps/resolution):** This part is **moderately novel** in a *dedicated* tool. While you can write a script to extract this data (using `ffprobe` or similar) and apply conditional logic, having it integrated into a comprehensive pre-selection interface is a valuable feature.

### 2. Proposed Features & Novelty

| Feature | Novelty for Linux/Open-Source | Existing Tools & Notes |
| :--- | :--- | :--- |
| **QSV Integration (Intel Quick Sync Video)** | **Not Novel** | FFmpeg supports QSV, and tools like "FFmpeg Batch AV Converter" or the popular media server software Tdarr (which uses FFmpeg) explicitly leverage it for hardware acceleration. Your smarter logic about *when* to use it is a good addition. |
| **Interactive Curses Interface** | **Novel (and Cool!)** | While there are command-line tools for conversion, a dedicated, feature-rich **TUI (Text User Interface)** using `curses` for file selection, property viewing, and job management is **very novel** for this specific use case. Most alternatives are simple scripts or full graphical GUIs. A TUI hits a sweet spot for power users who prefer the terminal. |
| **Pre-run Selection List (File Properties)** | **Moderately Novel** | Viewing file size, resolution, and codec *before* running is a great quality-of-life feature. It requires robust integration with a library like `ffprobe`. |
| **Multi-Job Background Processing (with limit)** | **Not Novel** | Many batch converters (including the mentioned `FFmpeg Batch AV Converter` and server tools like Tdarr) handle parallel jobs. However, a `curses` interface that *manages* and *displays status* of these jobs is where the novelty of your approach lies. |
| **Smart Progress Display (percent done, ETA, scrolling)** | **Moderately Novel** | Getting reliable progress and ETA from FFmpeg's output while running multiple background jobs and displaying it cleanly in a TUI that auto-scrolls is a **significant implementation challenge** that would make the tool stand out. |
| **Companion File Renaming (.srt, etc.)** | **Moderately Novel** | The ability to automatically rename associated sidecar files (subtitles, metadata) when the main video file name changes is a detail often overlooked in simple scripts but is crucial for media libraries. |

### Conclusion

**Your project is absolutely an interesting and viable project idea.**

While the core functionality (batch converting with FFmpeg and QSV) is not novel, **the way you intend to package and present it is highly unique, especially for Linux terminal users.**

The combination of:

1.  **Python** (making it easy to install and package).
2.  **Curses TUI** (for an interactive, terminal-based experience).
3.  **Smart pre-selection logic** (kbps/resolution/QSV choice).
4.  **Robust, real-time job management and status display** (for multiple parallel jobs).

...makes for a tool that doesn't seem to have a widely-used, dedicated open-source equivalent on Linux.

**Go for it!** If you build it with the quality-of-life features you've outlined, it would likely be well-received by terminal-focused Linux users and could certainly be a successful PyPI project.