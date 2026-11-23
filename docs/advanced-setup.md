# Advanced Setup (Hardware Acceleration)

This guide covers the FFmpeg choices: local VA-API hardware acceleration, Docker/Podman, and CPU-only fallback, plus the sandbox used for untrusted media. For the basic install, see the [README](../README.md).

The instructions target Ubuntu/Debian; adjust them for other distributions. Software encoding works everywhere but is much slower, so hardware acceleration is recommended for large collections.

## System Requirements

- **CPU**: Intel 11th Gen (Tiger Lake) or newer, or AMD Ryzen 5000 (Zen 3) or newer, for modern 10-bit HEVC support.
- **RAM**: 8GB minimum; 16GB recommended for 4K collections.

## Option 1: Local FFmpeg with Hardware Acceleration (Recommended)

This has the lowest overhead and best performance.

```bash
sudo apt update
sudo apt install ffmpeg
ffmpeg -version
```

FFmpeg v7 is recommended; v6 works but has weaker HEVC performance. The `ppa:ubuntuhandbook1/ffmpeg7` PPA provides v7 on older Ubuntu releases.

Enable and verify VA-API:

```bash
sudo apt install libva-dev intel-media-va-driver-non-free
sudo usermod -aG video $USER
sudo usermod -aG render $USER
# Log out and back in, then:
vainfo
```

`vainfo` should list `VAEntrypointEncSlice` for HEVC. For AMD or NVIDIA GPUs, install the matching driver instead; Intel is the most common VA-API setup.

## Option 2: Docker/Podman with Hardware Acceleration

Use this if local acceleration is unavailable or you prefer containers. Install Docker or Podman, install the same VA-API host drivers and group memberships as Option 1, then pull the image once:

```bash
docker pull joedefen/ffmpeg-vaapi-docker:latest
# or: podman pull joedefen/ffmpeg-vaapi-docker:latest
```

`rmbloat` does not pull images during startup. It pins the local image for the run and uses it for every probe and encode. See https://github.com/joedefen/ffmpeg-vaapi-docker for image and host details.

## Option 3: CPU-Only Encoding (Fallback)

If acceleration is unavailable, `rmbloat` falls back to CPU encoding automatically. Install FFmpeg as above; expect conversions to be roughly 3-10x slower.

## Verify Your Setup

```bash
rmbloat --chooser-tests
rmbloat --chooser-tests /path/to/sample/video.mp4
```

The first command detects available strategies; adding a video runs 120-second encoding tests. `rmbloat` then selects the best working strategy at runtime.

## Hardware Acceleration

With the default `-p auto`, `rmbloat` prefers, in order:

1. System FFmpeg with hardware acceleration
2. System FFmpeg CPU-only
3. Docker/Podman with hardware acceleration
4. Docker/Podman CPU-only

System FFmpeg is always preferred when installed, even without working acceleration. Use `-p/--prefer-strategy` to override the choice.

## Security: Untrusted Media

Media decoders process attacker-controlled data, so `rmbloat` treats FFmpeg and ffprobe as untrusted:

- Container jobs run without network access, with dropped capabilities, a read-only root filesystem, and only a private per-job directory writable.
- Local jobs deny network protocols, close stdin, reject FIFOs and device nodes, scrub risky environment variables, and terminate the process group on abort. If `bwrap` is available, `--sandbox auto` also removes network and root write access.
- Each job writes to a private temporary directory on the media filesystem, keeping the final replace atomic.
- `--sandbox` accepts `auto`, `container`, `bwrap`, `systemd`, or `none`. If no isolation is available, `rmbloat` says so at startup.

A file that depends on remote resources, such as an HLS playlist pointing at `http://`, will fail to probe or convert. Download such media locally first. With `--sandbox none`, an FFmpeg exploit would gain your user's privileges; killing `rmbloat` with `SIGKILL` can also leave a local FFmpeg child running.
