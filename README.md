# `rmbloat` - the smart video converter for media server owners

`rmbloat` is an intelligent, interactive video converter designed specifically for media server owners to reclaim massive amounts of disk space effortlessly, while maintaining high visual quality. It identifies the most inefficient videos in your collection and lets you convert them in prioritized, low-impact background batches.

### The Compelling Problem (and the `rmbloat` Solution)

Your video library is likely filled with bloat: older H.264 (AVC), MPEG-4, or high-bitrate H.265 files that waste valuable storage and sometimes create playback challenges.

* **The Problem**: Manually finding and converting these files is tedious, requires dozens of FFmpeg commands, and can easily overwhelm your server.
* **The `rmbloat` Solution**: We use the unique BLOAT metric to prioritize files that will give you the largest size reduction per conversion. `rmbloat` then runs the conversions in a low-priority, controlled background process, creating space savings with minimal server disruption.

Since it is designed for mass conversions on a media server, it often makes sense to start `rmbloat` in a tmux or screen session that out-lives a log-in session (e.g., on a headless server).

### Easy Installation
To install `rmbloat`, use `pipx rmbloat`. If explanation is needed, see [Install and Execute Python Applications Using pipx](https://realpython.com/python-pipx/).

### Bloat Metric
`rmbloat` defines
```
        bloat = 1000 * bitrate / sqrt(height*width)
```
A bloat value of 1000 is roughly that of an aggressively compressed h265 file. It is common to see bloats of 4000 or more in typical collections; very bloated files can typically be reduced in size by a factor of 4 or more w/o too much loss of watchability.

## Using `rmbloat`
### Starting `rmbloat` from the CLI
`rmbloat` requires a list of files or directories to scan for conversion candidates.  The full list of options are:
```
usage: rmbloat.py [-h] [-B] [-b BLOAT_THRESH] [-q QUALITY] [-a {x26*,x265,all}] [-F] [-m MIN_SHRINK_PCT] [-S] [-n] [-s] [-L] [files ...]

CLI/curses bulk Video converter for media servers

positional arguments:
  files                 Video files and recursively scanned folders w Video files

options:
  -h, --help            show this help message and exit
  -B, --keep-backup     if true, rename to ORIG.{videofile} rather than recycle [dflt=False]
  -b BLOAT_THRESH, --bloat-thresh BLOAT_THRESH
                        bloat threshold to convert [dflt=1600,min=--save00]
  -q QUALITY, --quality QUALITY
                        output quality (CRF) [dflt=28]
  -a {x26*,x265,all}, --allowed-codecs {x26*,x265,all}
                        allowed codecs [dflt=x265]
  -F, --full-speed      if true, do NOT set nice -n19 and ionice -c3 dflt=False]
  -m MIN_SHRINK_PCT, --min-shrink-pct MIN_SHRINK_PCT
                        minimum conversion reduction percent for replacement [dflt=10]
  -S, --save-defaults   save the -B/-b/-q/-a/-F/-m/-M options and file paths as defaults
  -n, --dry-run         Perform a trial run with no changes made.
  -s, --sample          produce 30s samples called SAMPLE.{input-file}
  -L, --logs            view the logs
  ```
  You can customize the defaults by setting the desired options and adding the  `--save-defaults` option to write the current choices to its .ini file. This includes saving your video collection root paths, so you don't need to specify them every time you run `rmbloat`. File paths are automatically sanitized: converted to absolute paths, non-existing paths removed, and redundant paths (subdirectories of other saved paths) eliminated. Non-video files in the given files and directories are simply ignored.

  Candidate video files are probed (with `ffprobe`). If the probe fails, then the candidate is simply ignored. Probing many files can be time consuming, but `rmbloat` keeps a cache of probes so start-up can be fast if most of the candidates have been successfully probed.

## The Three Main Screens
The main screens are:
* **Selection Screen** - where you can customize the decisions and scope of the conversions. The Selecition screen is the first screen after start-up.
* **Conversion Screen** - where you can view the conversion progress. When conversions are completed (or manually aborted), it returns to the Selection screen.
* **Help Screen** - where you can see all available keys and meanings. Use the key, '?', to enter and exit the Help screen.

### Selection Screen
After scanning/probing the file and folder arguments, the selection screen will open.  In the example below, we have applied a filter pattern, `anqis.gsk`, to select only certain video files.

```
 [r]setAll [i]nit SP:toggle [g]o ?=help [q]uit /anqis.gsk
      Picked=3/10  GB=5.6(0)  CPU=736/800%
 CVT  NET BLOAT    RES  CODEC  MINS     GB   VIDEO
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
>[X]  ---  2831^  960p   hevc    50  1.342   Anqis.Gsk.Kbnvw.2020.S01E06.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
 [X]  ---  2796^  960p   hevc    50  1.321   Anqis.Gsk.Kbnvw.2020.S01E05.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
 [X]  ---  2769^  960p   hevc    42  1.116   Anqis.Gsk.Kbnvw.2020.S01E04.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
 [ ]  ---   801   960p   hevc    44  0.333   Anqis.Gsk.Kbnvw.2020.s01e02.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---   762   960p   hevc    48  0.350   Anqis.Gsk.Kbnvw.2020.s01e01.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---   633   960p   hevc    56  0.338   Anqis.Gsk.Kbnvw.2020.s01e09.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---   614   960p   hevc    50  0.289   Anqis.Gsk.Kbnvw.2020.s01e08.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---   608   960p   hevc    43  0.246   Anqis.Gsk.Kbnvw.2020.s01e07.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---   599   960p   hevc    41  0.234   Anqis.Gsk.Kbnvw.2020.s01e03.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsb
 [ ]  ---    86   960p   hevc    50  0.041   JSTY.Anqis.Gsk.Kbnvw.2020.s01e06.960p.x265.cmf28.recode.mkv ---> /dsqy/
```
**Notes.**
* `[ ]` denotes a video NOT selected for conversion.
* `[X]` denotes a video selected for conversion.
* other CVT values are:
  * `?Pn` - denotes probe failed `n` times (stops at 9)
    * A "hard" failure which cannot be overridden to start conversion
  * `ErN` - denotes conversion failed `N` times (stops at 9)
    * `Er1` is a "very soft" state (auto overriden); can manually select other values
  * `OPT` - denotes the prior conversion went OK except insuffient shrinkage
    * can manually select for conversion
* `^` denotes a value over the threshold for conversion. Besides an excessive bloat, the height could be too large, or the codec unacceptable; all depending on the program options.
* To change whether selected, you can use:
    * the s/r/i keys to affect potentially every select, and
    * SPACE to toggle just one; if one is toggled, the cursor moves to the next line so you can toggle sequences very quickly starting at the top.
* The videos are always sorted by their current bloat score, highest first.
* To start converting the selected videos, hit "go" (i.e., the `g` key), and the Conversion Screen replaces this Selection screen.
### Conversion Screen
The Conversion screen only shows the videos selected for conversion on the Selection screen. There is little that can be done other than monitor progress and abort the conversions (with 'q' key).
```
 ?=help q[uit] /anqis.gsk     ToDo=4/9  GB=11.5(-5.0)  CPU=711/800%
CVT  NET BLOAT    RES  CODEC  MINS     GB   VIDEO
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 OK -74%   762   960p   hevc    48  0.350   Anqis.Gsk.Kbnvw.2020.s01e01.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsbu
 OK -79%   599   960p   hevc    41  0.234   Anqis.Gsk.Kbnvw.2020.s01e03.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsbu
 OK -78%   633   960p   hevc    56  0.338   Anqis.Gsk.Kbnvw.2020.s01e09.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsbu
 OK -79%   614   960p   hevc    50  0.289   Anqis.Gsk.Kbnvw.2020.s01e08.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsbu
 OK -72%   801   960p   hevc    44  0.333   Anqis.Gsk.Kbnvw.2020.s01e02.960p.x265.cmf28.recode.mkv ---> /dsqy/Icwsbu
IP   ---  2870^  960p   hevc    43  1.158   Anqis.Gsk.Kbnvw.2020.S01E07.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
-----> 34.6% | 08:41 | -16:22 | 1.7x | At 14:43/42:32
[X]  ---  2831^  960p   hevc    50  1.342   Anqis.Gsk.Kbnvw.2020.S01E06.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
[X]  ---  2796^  960p   hevc    50  1.321   Anqis.Gsk.Kbnvw.2020.S01E05.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
[X]  ---  2769^  960p   hevc    42  1.116   Anqis.Gsk.Kbnvw.2020.S01E04.1080p.BluRay.10Bit,DDP5.1.H265-d3g.mkv --->
```
**Notes**: You can see:
* the net change in size, `(-5.0)` GB, and the current size, `11.5` GB.
* the CPU consumption which is often quite high as in this example.
* the progress of the singular In Progress conversion including percent complete, time elapsed, time remaining, conversion speed vs viewing speed (1.7x), and the position in the video file.
* for completed conversions, the reduction in size, the new size, and the new file name of the converted video.
### Help Screen
The Help screen is available from the other screens; enter the Help screen with `?` and exit it with another `?`
```
Navigation:      H/M/L:      top/middle/end-of-page
  k, UP:  up one row             0, HOME:  first row
j, DOWN:  down one row           $, END:  last row
  Ctrl-u:  half-page up     Ctrl-b, PPAGE:  page up
  Ctrl-d:  half-page down     Ctrl-f, NPAGE:  page down
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Type keys to alter choice:
                    ? - help screen:  off ON
             r - reset all to "[ ]"
     i - set all to automatic state
     SP - toggle current line state
              g - begin conversions
    q - quit converting OR exit app
           p - pause/release screen:  off ON
                  / - search string:  brave.new
                  m - mangle titles:  off ON
```
* Some keys are for navigation (they allow vi-like navigation).
* Some keys are set a state, and the current state is capitalized
* Some keys are to instigate some action (they have no value)
* Finally, `/` is to set the filter. The filter must be a valid python regular expression, and it is always case insensitive.

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

We attempted to further limit impact by using options to control the number of threads, but none were found that changed anything.  Similiarly, with process affinity, nothing really helped (`ffmpeg` seems to dodge any controls). `systemd` throttles worked, but they decimate the efficiency of `ffmpeg` so much that it seemed better to do without.

Furthermore, we attempted to use the Intel hardware accellerated h265 processing, but that never worked either.  This may be revisited some day.

### Videos Removed/Moved While Running
If videos are removed or moved while `rmbloat` is running, they will only be detected just before starting a conversion (if ever).
In that case, they are silently removed from the queue (in the Conversion screen), but there is a log of the event.
Since the conversions may be long-running and unattended, there is no alert other than the log.

### Upgrading to ffmpeg V8 on Ubuntu
```
  sudo apt remove ffmpeg
  sudo add-apt-repository ppa:ubuntuhandbook1/ffmpeg8
  sudo apt update
  sudo apt install ffmpeg
  ffmpeg -version
  sudo apt install libva-dev intel-media-va-driver-non-free
  sudo usermod -aG video $USER
  sudo usermod -aG render $USER
  sudo apt remove ffmpeg
  sudo add-apt-repository --remove ppa:ubuntuhandbook1/ffmpeg8
  sudo add-apt-repository ppa:ubuntuhandbook1/ffmpeg7
  sudo apt update
  sudo apt install ffmpeg
```

# TODO:
- controls over the status line timeouts should be considered (those are fixed)
- handling for failed and ineffective conversions

