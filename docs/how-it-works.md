# How It Works

## Bloat Metric

`rmbloat` ranks files with a resolution-normalized bitrate:

```
        bloat = 1000 * bitrate / sqrt(height*width)
```

| Bloat Range | Description | Typical Source | Potential Reduction |
|-------------|-------------|----------------|---------------------|
| 800-1200 | Efficient | Modern H.265/HEVC | Minimal |
| 1200-2000 | Moderate | Good H.264 or older H.265 | 20-40% |
| 2000-3000 | Poor | Older H.264, high bitrate | 40-60% |
| 3000-5000 | Very bloated | Blu-ray rips, older codecs | 60-75% |
| 5000+ | Extremely bloated | Old MPEG-4, uncompressed | 75-85% |

The default threshold is 1600; files above it are selected automatically. Adjust it with `-b/--bloat-thresh`.

## File Renaming Strategy

Parsed files are renamed to one of:

* `{tv-series}.sXXeXX.{encoding-info}.mkv`
* `{tv-series}.{year}.sXXeXX.{encoding-info}.mkv`
* `{movie-title}.{year}.{encoding-info}.mkv`

`{encoding-info}` is filled in at completion and reflects the encoder and quality, for example `Show.S01E01.1080p.x265.qp28.recode.mkv` for hardware or `...crf22...` for software. If the required parts cannot be parsed, only a wrong resolution or codec is corrected.

Companion files and matching folders (for example `Show.S01E01.cache/`) are renamed with the video. Unrelated directories are untouched, and existing files are never overwritten.

## Intelligent Quality Adjustment

The `-q` value is adjusted by resolution, then mapped to the encoder's scale:

| Resolution | Adjustment |
|------------|-----------|
| Below 480p | -4 |
| 480p-720p | -2 |
| 720p-1080p | none |
| 4K and above | +2 |

So with the default `-q 28`, a 1080p file uses 28, a 352p file uses 24, and a 4K file uses 30. The higher pixel count of 4K allows efficient compression without visible loss.

Software encoders use CRF and hardware encoders use QP; `rmbloat` maps between them so quality stays consistent across strategies. Mapping usually adds about 2 points, so `-q 28` becomes roughly QP 30 on hardware.

## Logging

Conversion events are written to `$XDG_STATE_HOME/rmbloat/events.jsonl` (`~/.local/state/rmbloat` by default). `rmbloat --logs` pages the file with `less -R +F`, so `less` must be installed. You can also read or page the JSON Lines file directly.

## Performance and Server Impact

FFmpeg runs with reduced `nice` and `ionice` priority, so the server stays responsive. Conversions run in a dedicated background thread, keeping the interface smooth and avoiding interference with the active transcode.

## Intelligent Resilience (The Retry Ladder)

If a hardware conversion fails, `rmbloat` logs it, adjusts the parameters (for example, falling back to CPU encoding), and retries automatically. Difficult files usually convert without manual intervention.

### Subtitle Handling

Text subtitle formats (subrip, ass, ssa, mov_text, webvtt, text) are kept and converted to SRT for MKV. Bitmap formats (dvd_subtitle, hdmv_pgs_subtitle, dvb_subtitle, xsub) are dropped during probing because they can crash FFmpeg and cannot be stored as MKV text tracks. External `.en.srt` files can be merged with `-M/--merge-subtitles`.

### What Gets Dropped or Changed

Dropped:

* Attachments -- Embedded fonts, cover art, and thumbnails are usually unused and can be tens of MB.
* Data streams -- Timecodes and miscellaneous metadata are rarely used by players.
* Bitmap subtitles -- Cannot be stored as MKV text and often cause FFmpeg failures.

Kept:
* Video -- Re-encoded to HEVC.
* Audio -- Copied unchanged, including all languages.
* Text subtitles -- Copied when the codec is safe.

Output is 10-bit when the hardware supports it (better gradients, slightly larger files), otherwise 8-bit. All output is MKV, which supports multiple audio/subtitle tracks and works well with Plex, Jellyfin, and Emby.

For example, a Blu-ray rip with a 40GB video track, five audio tracks, three PGS subtitles, two SRT subtitles, embedded fonts, and cover art becomes roughly 8GB: the video is re-encoded, audio and SRT tracks are kept, and the bitmap subtitles, fonts, and cover art are dropped.

### Videos Removed/Moved While Running

A file is checked again just before its conversion starts. If it is missing, it is removed from the queue and the event is logged; there is no other alert during an unattended run.
