# `rmbloat` - the smart video converter for media server owners

`rmbloat` is an intelligent, interactive video converter designed specifically for media server owners to reclaim massive amounts of disk space effortlessly, while maintaining high visual quality. It identifies the most inefficient videos in your collection and lets you convert them in prioritized, low-impact background batches.

### The Compelling Problem (and the `rmbloat` Solution)

Your video library is likely filled with bloat: older H.264 (AVC), MPEG-4, or high-bitrate H.265 files that waste valuable storage and sometimes create playback challenges.

* **The Problem**: Manually finding and converting these files is tedious, requires dozens of FFmpeg commands, and can easily overwhelm your server.
* **The `rmbloat` Solution**: We use the unique BLOAT metric to prioritize files that will give you the largest size reduction per conversion. `rmbloat` then runs the conversions in a low-priority, controlled background process, creating space savings with minimal server disruption.

Since it is designed for mass conversions on a media server, it often makes sense to start `rmbloat` in a tmux or screen session that out-lives a log-in session (e.g., on a headless server).

## Installation and System Preparation
To install `rmbloat`, use `pipx rmbloat`. If explanation is needed, see [Install and Execute Python Applications Using pipx](https://realpython.com/python-pipx/).

So installing `rmbloat` is simple. And if your system has `ffmpeg` installed with HEVC (H.265) encoding support, you are ready to go, however with agonizingly slow software encoding. The bigger your video collection, the less acceptable software encoding will be.

Anyhow, you have three options described below. These instructions are for Ubuntu and other Debian derived distros. For other distros, you must adjust the procedures accordingly.

### Option 1: Local FFmpeg with Hardware Acceleration (Recommended)

This provides the best performance with lowest overhead.

**Install FFmpeg on Ubuntu:**

`rmbloat` is tested with FFmpeg v7 (recommended at time of authoring). FFmpeg v6 should also work, but v7 has better HEVC encoding performance and dependability.

```bash
sudo apt update
sudo apt install ffmpeg

# Check your FFmpeg version
ffmpeg -version
```

**If you have FFmpeg v6 and want to upgrade to v7:**
```bash
# Remove existing FFmpeg
sudo apt remove ffmpeg

# Add FFmpeg v7 PPA and install
sudo add-apt-repository ppa:ubuntuhandbook1/ffmpeg7
sudo apt update
sudo apt install ffmpeg

# Verify version
ffmpeg -version
```

**Enable VA-API Hardware Acceleration:**
```bash
# Install VA-API drivers and Intel media driver
sudo apt install libva-dev intel-media-va-driver-non-free

# Add your user to video and render groups
sudo usermod -aG video $USER
sudo usermod -aG render $USER

# Log out and back in for group changes to take effect
# Or run: newgrp video && newgrp render

# Verify hardware acceleration is working
vainfo
# Should show: "vaQueryConfigEntrypoints: VAEntrypointEncSlice" for H265/HEVC

# Test with rmbloat
rmbloat --chooser-tests
```

**Note**: For non-Intel GPUs (AMD, NVIDIA), you'll need different drivers. Intel is most common for VA-API.

### Option 2: Docker/Podman with Hardware Acceleration

If you can't get local FFmpeg working with acceleration, or prefer containerization:

```bash
# Install Docker (Ubuntu)
sudo apt update
sudo apt install docker.io
sudo usermod -aG docker $USER
# Log out and back in

# OR install Podman (rootless alternative)
sudo apt install podman

# Install VA-API host requirements (same as Option 1)
sudo apt install libva-dev intel-media-va-driver-non-free
sudo usermod -aG video $USER
sudo usermod -aG render $USER
# Log out and back in
```

`rmbloat` will automatically pull and use the `joedefen/ffmpeg-vaapi-docker:latest` image. For details on this image and additional host setup requirements, see: https://github.com/joedefen/ffmpeg-vaapi-docker

### Option 3: CPU-Only Encoding (Fallback)

If hardware acceleration isn't available or working, `rmbloat` will automatically fall back to CPU encoding. This works but is significantly slower (3-10x depending on hardware).

```bash
# Just install FFmpeg. If v6, we suggest upgrading to v7
# as described above.
sudo apt update
sudo apt install ffmpeg
```

### Verify Your Setup

After setup, test what's working:

```bash
# Basic detection test (shows which options you have
# for conversion to hevc)
rmbloat --chooser-tests

# Full test with a video file (runs 30s encoding tests)
rmbloat --chooser-tests /path/to/sample/video.mp4
```

The output will show which strategies work and recommend the best one. `rmbloat` automatically selects the best available option at runtime.

### Bloat Metric
`rmbloat` defines
```
        bloat = 1000 * bitrate / sqrt(height*width)
```
A bloat value of 1000 is roughly that of an aggressively compressed h265 file. It is common to see bloats of 4000 or more in typical collections; very bloated files can typically be reduced in size by a factor of 4 or more w/o too much loss of watchability.

## Using `rmbloat`
### Starting `rmbloat` from the CLI
`rmbloat` requires a list of files or directories to scan for conversion candidates (or uses saved defaults if configured).  The full list of options are:
```
usage: rmbloat.py [-h] [-a {x26*,x265,all}] [-b BLOAT_THRESH] [-F] [-B]
                  [-M] [-m MIN_SHRINK_PCT]
                  [-p {auto,system_accel,docker_accel,system_cpu,docker_cpu}]
                  [-q QUALITY] [-t THREAD_CNT] [-S] [--auto-hr AUTO_HR]
                  [-n] [-s] [-L] [-T]
                  [files ...]

CLI/curses bulk Video converter for media servers

positional arguments:
  files                 Video files and recursively scanned folders w Video
                        files

options:
  -h, --help            show this help message and exit
  -a {x26*,x265,all}, --allowed-codecs {x26*,x265,all}
                        allowed codecs [dflt=x265]
  -b BLOAT_THRESH, --bloat-thresh BLOAT_THRESH
                        bloat threshold to convert [dflt=1600,min=500]
  -F, --full-speed      if true, do NOT set nice -n19 and ionice -c3
                        [dflt=False]
  -B, --keep-backup     if true, rename to ORIG.{videofile} rather than
                        recycle [dflt=False]
  -M, --merge-subtitles
                        Merge external .en.srt subtitle files into output
                        [dflt=False]
  -m MIN_SHRINK_PCT, --min-shrink-pct MIN_SHRINK_PCT
                        minimum conversion reduction percent for
                        replacement [dflt=10]
  -p {auto,system_accel,docker_accel,system_cpu,docker_cpu},
      --prefer-strategy {auto,system_accel,docker_accel,system_cpu,docker_cpu}
                        FFmpeg strategy preference [dflt=auto]
  -q QUALITY, --quality QUALITY
                        output quality (CRF) [dflt=28]
  -t THREAD_CNT, --thread-cnt THREAD_CNT
                        thread count for ffmpeg conversions [dflt=4]
  -S, --save-defaults   save the -B/-b/-p/-q/-a/-F/-m/-M options and file
                        paths as defaults
  --auto-hr AUTO_HR     Auto mode: run unattended for specified hours,
                        auto-select [X] files and auto-start conversions
  -n, --dry-run         Perform a trial run with no changes made.
  -s, --sample          produce 30s samples called SAMPLE.{input-file}
  -L, --logs            view the logs
  -T, --chooser-tests   run tests on ffmpeg choices w 30s cvt of 1st given
                        video
  ```
  You can (and should) customize the defaults by setting the desired options and adding the `--save-defaults` option to write the current choices to its .ini file.
  * Setting the default for `--prefer-strategy` will speed startup once you have tested and found the best strategy.
  * This includes saving your video collection root paths, so you don't need to specify them every time you run `rmbloat`. File paths are automatically sanitized: converted to absolute paths, non-existing paths removed, and redundant paths (subdirectories of other saved paths) eliminated. Non-video files in the given files and directories are simply ignored.

  Candidate video files are probed (with `ffprobe`). If the probe fails, then the candidate is simply ignored. Probing many files can be time consuming, but `rmbloat` keeps a cache of probes so start-up can be fast if most of the candidates have been successfully probed.

## The Three Main Screens
The main screens are:
* **Selection Screen** - where you can customize the decisions and scope of the conversions. The Selecition screen is the first screen after start-up.
* **Conversion Screen** - where you can view the conversion progress. When conversions are completed (or manually aborted), it returns to the Selection screen.
* **Help Screen** - where you can see all available keys and meanings. Use the key, '?', to enter and exit the Help screen.

### Selection Screen
After scanning/probing the file and folder arguments, the selection screen will open.  In the example below, we have applied a filter pattern, `anqis.gsk`, to select only certain video files.

```
 [r]setAll [i]nit SP:toggle [g]o ?=help [q]uit /kcjzd
      Picked=17/123  GB=83.5(0)  CPU=9/800%
 CVT  NET BLOAT    RES  CODEC  MINS     GB   VIDEO -- Q=28 Shr>=10 MrgSrt
─────────────────────────────────────────────────────────────────────────────────────
>[X]  ---  2725^  672p   h264^   89  1.567   Jds Kcjzd 2015 720p Readnfo HDRIP x264 A
 [X]  ---  2070^ 1080p   h264^   46  0.960   Q.Wcuzbisnl.bx.Kcjzdsu.S03E04.1080p.HBO.
 [X]  ---  2053^ 1080p   h264^   45  0.936   Q.Wcuzbisnl.bx.Kcjzdsu.S03E06.1080p.HBO.
 [X]  ---  2052^ 1080p   h264^   46  0.953   Q.Wcuzbisnl.bx.Kcjzdsu.S03E05.1080p.HBO.
 [X]  ---  2045^ 1080p   h264^   47  0.963   Q.Wcuzbisnl.bx.Kcjzdsu.S03E03.1080p.HBO.
 [X]  ---  2014^ 1080p   h264^   46  0.923   Q.Wcuzbisnl.bx.Kcjzdsu.S03E07.1080p.HBO.
 [X]  ---  1735^ 1080p   hevc    51  0.894   Jds.Kcjzdsn.S04E08.1080p.HEVC.x265-MeGus
   ...
```
**Notes.**
* `[ ]` denotes a video NOT selected for conversion.
* `[X]` denotes a video selected for conversion.
* other (often uncommon) CVT states are:
  * `---` - denotes a manually "skipped" (i.e., don't convert) video:
    * in select mode, `s` sets/clears skip mode
    * in convert mode, `s` skips stops the current video conversion and sets the state
  * `?Pn` - denotes probe failed `n` times (stops at 9)
    * A "hard" failure which cannot be overridden to start conversion
  * `ErN` - denotes conversion failed `N` times (stops at 9)
    * `Er1` is a "very soft" state (auto overriden); can manually select other values
  * `OPT` - denotes the prior conversion went OK except insuffient shrinkage
    * can manually select for conversion
  * `DUN` - denotes already re-encoded (filename ends with `.recode.mkv`)
    * "semi-soft" ban: first toggle attempt prompts for session-wide permission to re-encode
    * prevents accidental re-encoding of your own output files
* `^` denotes a value over the threshold for conversion. Besides an excessive bloat, the height could be too large, or the codec unacceptable; all depending on the program options.
* To change whether selected, you can use:
  * the s/r/i keys to affect potentially every select, and
  * SPACE to toggle just one; if one is toggled, the cursor moves to the next line so you can toggle sequences very quickly starting at the top.
* The videos are always sorted by their current bloat score, highest first.
* To start converting the selected videos, hit "go" (i.e., the `g` key), and the Conversion Screen replaces this Selection screen.
  * Some key option choices are summarized after "VIDEO --"; before hitting `z`, reviewing those options is suggested.
### Conversion Screen
The Conversion screen only shows the videos selected for conversion on the Selection screen. There is little that can be done other than monitor progress and abort the conversions (with 'q' key).
```
 ?=help q[uit] /kcjzd     ToDo=16/17  GB=15.9(-1.3)  CPU=630/800%
CVT  NET BLOAT    RES  CODEC  MINS     GB   VIDEO -- Q=28 Shr>=10 MrgSrt
─────────────────────────────────────────────────────────────────────────────────────
 OK -80%   541   672p   hevc    89  0.311   Jds.Kcjzd.2015.672p.x265.cmf28.recode.sb.
IP   ---  2070^ 1080p   h264^   46  0.960   Q.Wcuzbisnl.bx.Kcjzdsu.S03E04.1080p.HBO.W
-----> 7.1% | 01:26 | -18:18 | 2.3x | At 03:15/46:05
[X]  ---  2053^ 1080p   h264^   45  0.936   Q.Wcuzbisnl.bx.Kcjzdsu.S03E06.1080p.HBO.W
[X]  ---  2052^ 1080p   h264^   46  0.953   Q.Wcuzbisnl.bx.Kcjzdsu.S03E05.1080p.HBO.W
[X]  ---  2045^ 1080p   h264^   47  0.963   Q.Wcuzbisnl.bx.Kcjzdsu.S03E03.1080p.HBO.W
[X]  ---  2014^ 1080p   h264^   46  0.923   Q.Wcuzbisnl.bx.Kcjzdsu.S03E07.1080p.HBO.W
[X]  ---  1735^ 1080p   hevc    51  0.894   Jds.Kcjzdsn.S04E08.1080p.HEVC.x265-MeGust
 ...
```
**Notes**: You can see:
* the net change in size, `(-1.3)` GB, and the current size, `15.9` GB.
* the CPU consumption which is sometimes rathe. high as in this example depending on the effectiveness of hardware acceleration, etc.
* the progress of the In Progress (IP) conversion including percent complete, time elapsed, time remaining, conversion speed vs viewing speed (2.3x), and the position in the video file.
* for completed conversions, the reduction in size, the new size, and the new file name of the converted video.
* typing `q` will abandon any IP conversion and return to the selection screen.
### Help Screen
The Help screen is available from the other screens; enter the Help screen with `?` and exit it with another `?`
```
Navigation:      H/M/L:      top/middle/end-of-page
  k, UP:  up one row             0, HOME:  first row
j, DOWN:  down one row           $, END:  last row
  Ctrl-u:  half-page up     Ctrl-b, PPAGE:  page up
  Ctrl-d:  half-page down     Ctrl-f, NPAGE:  page down
──────────────────────────────────────────────────────────────────────
Type keys to alter choice:
                    ? - help screen:  off ON
             r - reset all to "[ ]"
        i - set all automatic state
     SP - toggle current line state
              g - begin conversions
    q - quit converting OR exit app
           p - pause/release screen:  off ON
                  / - search string:  witch
                  m - mangle titles:  off ON
```
* Some keys are for navigation (they allow vi-like navigation).
* Some keys are set a state, and the current state is capitalized
* Some keys are to instigate some action (they have no value)
* Finally, `/` is to set the filter. The filter must be a valid python regular expression, and it is always case insensitive.

## Running rmbloat Under tmux

### Why tmux?

`rmbloat` enforces a **single-instance policy** - only one instance can run at a time. This is by design for resource management: video conversion is CPU and I/O intensive, and running multiple instances would compete for system resources, slowing down all conversions.

Video conversion is often **very long-running** - processing a large video collection can take hours or even days. Using tmux keeps `rmbloat` running persistently without requiring you to stay connected. You can:
- Start a conversion session and detach
- Reconnect later to check progress
- Let conversions run overnight or over weekends
- Survive SSH disconnections without interrupting the work

### The rmbloatd Wrapper

`rmbloat` includes `rmbloatd`, a tmux wrapper that manages persistent sessions:

```bash
# Start rmbloat in tmux (fails if already running)
rmbloatd start

# Start with specific arguments
rmbloatd start -- --auto-hr 8 /path/to/videos

# Attach to running session
rmbloatd attach

# Check status
rmbloatd status

# Stop rmbloat
rmbloatd stop
```

**Important**: Only `start` accepts arguments. Use `attach` to connect to an existing session, and `stop` to terminate.

### Auto Mode for Maintenance Runs

Once your video collection is fully converted, you might want periodic "maintenance" runs to handle new arrivals. Auto mode (`--auto-hr`) is perfect for this - it runs unattended for a specified duration, automatically selecting and converting files.

**Example: Weekend maintenance run**
```bash
# Run for 48 hours, auto-selecting files needing conversion
rmbloatd start -- --auto-hr 48
```

The auto mode will:
- Automatically mark files for conversion based on your criteria
- Start conversions without manual intervention
- Stop cleanly after the specified time limit
- Use saved defaults for paths if configured with `--save-defaults`

### Scheduled Runs with Cron

For regular maintenance during off-hours, use cron. **Important**: Cron runs with a minimal PATH, so you need to either use absolute paths or set PATH in your crontab.

```bash
# Edit your crontab
crontab -e

# Set PATH to include where rmbloatd is installed
# (adjust path based on where pip installed it - use 'which rmbloatd' to find it)
PATH=/home/yourusername/.local/bin:/usr/local/bin:/usr/bin:/bin

# Example: Run Friday night at 11 PM for 48 hours (weekend maintenance)
# First, save your video paths as defaults:
#   rmbloat --save-defaults /path/to/videos
0 23 * * 5 rmbloatd start -- --auto-hr 48

# Example: Run every night at 2 AM for 6 hours
0 2 * * * rmbloatd start -- --auto-hr 6

# Example: Stop at 8 AM (before business hours)
0 8 * * * rmbloatd stop
```

**Alternative: Use absolute paths instead of setting PATH**
```bash
# Find where rmbloatd is installed
which rmbloatd
# Example output: /home/joe/.local/bin/rmbloatd

# Use that absolute path in cron
0 23 * * 5 /home/joe/.local/bin/rmbloatd start -- --auto-hr 48
```

**Note**: Cron jobs won't start if `rmbloat` is already running (the single-instance lock prevents this). You can safely have overlapping cron entries - if the previous run is still active, the new `start` will fail harmlessly.

### Using systemd (Alternative)

For systemd-based systems, you can create a timer unit for scheduled runs. This is more verbose than cron but integrates better with system logging:

```bash
# Create /etc/systemd/system/rmbloat-maintenance.service
[Unit]
Description=rmbloat video conversion maintenance
After=network.target

[Service]
Type=oneshot
User=your-username
ExecStart=/usr/local/bin/rmbloatd start -- --auto-hr 6

# Create /etc/systemd/system/rmbloat-maintenance.timer
[Unit]
Description=Run rmbloat maintenance nightly

[Timer]
OnCalendar=daily
OnCalendar=02:00
Persistent=true

[Install]
WantedBy=timers.target

# Enable the timer
sudo systemctl enable rmbloat-maintenance.timer
sudo systemctl start rmbloat-maintenance.timer
```

## Under the Covers
### File Renaming Strategy
Files are renamed in one of these forms if they are successfully "parsed":
* `{tv-series}.sXXeXX.{encoding-info}.mkv`
* `{tv-series}.{year}.sXXeXX.{encoding-info}.mkv`
* `{movie-title}.{year}.{encoding-info}.mkv`

For those video files for which the needed components cannot be determined, it changes resolution or codec if those parts are both found and are now wrong.

Companion files, like .srt files, and folders who share the same basename w/o the extension(s), will be renamed also if the video file was renamed.

### Logging (--logs)
When a conversion completes successfully or not, details are logged into files in your `~/.config/rmbloat` folder. You can view those files with `rmbloat --logs` using `less`; see the `less` man page if needed.

### Dry-Run (--dry-run)
If started with `--dry-run`, then conversions are not done, but the log is written with details like how file(s) will be renamed. This helps with testing screens and actions more quickly than waiting for actual conversions.

### Performance and Server Impact
By default, `ffmpeg` conversions are done with both `ionice` and `nice` lowering its priority. This will (in our experience) allow the server to run rather well.  But, your experience may vary.

### Hardware Acceleration
`rmbloat` automatically detects and uses hardware acceleration (VA-API) when available, providing significant performance improvements. The `FfmpegChooser` component:
- Detects system ffmpeg with VA-API support
- Falls back to Docker/Podman containers with hardware acceleration if needed
- Automatically selects the best strategy (system or container, with or without acceleration)
- Can be manually controlled with the `-p/--prefer-strategy` option

To test your hardware acceleration support:
```bash
rmbloat --chooser-tests /path/to/test/video.mp4
```

This will run 30-second encoding tests with different strategies and report which work best on your system.

### Subtitle Handling
`rmbloat` intelligently handles subtitle streams to prevent conversion failures:

**Safe Text-Based Subtitles** (kept and transcoded to SRT for MKV compatibility):
- subrip, ass, ssa, mov_text, webvtt, text

**Unsafe Bitmap Subtitles** (automatically dropped to prevent FFmpeg crashes):
- dvd_subtitle, hdmv_pgs_subtitle, dvb_subtitle, xsub, and other bitmap formats

During the probe phase, `rmbloat` detects problematic subtitle codecs and automatically excludes them from conversion. Text-based subtitles like `mov_text` (common in MP4 files) are transcoded to SRT format for universal MKV compatibility.

External `.en.srt` subtitle files can be merged into the output with the `-M/--merge-subtitles` option.

### Videos Removed/Moved While Running
If videos are removed or moved while `rmbloat` is running, they will only be detected just before starting a conversion (if ever).
In that case, they are silently removed from the queue (in the Conversion screen), but there is a log of the event.
Since the conversions may be long-running and unattended, there is no alert other than the log.