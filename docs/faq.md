# Frequently Asked Questions (FAQ)

## Can I run multiple instances of rmbloat?

**No.** `rmbloat` allows one instance at a time to avoid competing for hardware, duplicating work, and conflicting over the same files. Pass multiple directories to one run instead: `rmbloat /path1 /path2 /path3`.

## Will this hurt video quality?

At the default `-q 28`, most content should show minimal to no perceptible loss. The bloat metric targets files with far more bitrate than their resolution needs, and quality is adjusted by resolution automatically ([details](how-it-works.md#intelligent-quality-adjustment)).

- Archival or critical content: use `-q 22`.
- Tight storage: use `-q 32`.
- Compare first: use `--sample` for 30-second clips.

## How long will it take to convert my collection?

For a 10TB collection:

* Modern hardware acceleration: 30-60x realtime, 3-7 days
* Older hardware acceleration: 15-30x realtime, 1-2 weeks
* CPU-only encoding: 3-10x realtime, 1-2 months

Speed depends on the source codec, resolution, `-q` setting, acceleration, and system load. Test on 100-200GB first to estimate your own rate.

## Can I pause and resume conversions?

- `q` stops the current conversion, and that file restarts later.
- Completed files stay completed.
- Use `rmbloatd` with tmux to detach and reattach ([details](running-as-daemon.md)).
- Use `--auto-hr` for timed overnight runs.

## What happens to my original files?

By default the original is deleted after a successful conversion and replaced by the `.recode.mkv` file.

- `-B/--keep-backup` renames the original to `ORIG.{videofile}`.
- `-m/--min-shrink-pct` (default 10) keeps the original unless the result is at least that much smaller.
- `--sample` writes test clips without touching originals.

Replacement is transactional: a failed conversion leaves the original untouched, and companion files are renamed with the video.

## Does rmbloat work on Windows or macOS?

`rmbloat` is Linux-only: it relies on `ionice`, process management, tmux, and VA-API. Windows users can try WSL2; macOS is not supported.

## What about AV1 codec support?

`rmbloat` targets H.265/HEVC for broad playback support and mature hardware encoding. AV1 compresses better but encodes more slowly, has limited hardware support, and is not yet supported as an output codec.
