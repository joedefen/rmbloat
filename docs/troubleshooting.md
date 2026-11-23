# Troubleshooting

## Hardware Acceleration Problems

**Symptoms**: `--chooser-tests` reports no hardware acceleration, or conversion fails with "Permission denied" on a render device.

```bash
# Render device should exist
ls -l /dev/dri/

# video and render should be listed
groups

# Add missing groups, then log out and back in
sudo usermod -aG video $USER
sudo usermod -aG render $USER

# Should list VAEntrypointEncSlice for HEVC
vainfo

# Force the hardware strategy for a test
rmbloat --chooser-tests --prefer-strategy system_accel
```

Relaxing permissions on the render device (`sudo chmod 666 /dev/dri/renderD128`) is only a temporary test. See [Advanced Setup](advanced-setup.md) for driver installation.

## Docker Image Not Available

**Symptom**: `Image joedefen/ffmpeg-vaapi-docker:latest is not available locally`

`rmbloat` does not pull images automatically. Check that Docker works and pull the image once:

```bash
docker run hello-world
docker pull joedefen/ffmpeg-vaapi-docker:latest
# Podman: podman pull joedefen/ffmpeg-vaapi-docker:latest
```

## Conversions Repeatedly Failing

**Symptom**: repeated `Er1`, `Er2`, etc. in the CVT column.

- **Corrupt source**: play the file, try `--sample`, and check `rmbloat --logs`.
- **Low disk space**: keep at least twice the source size free.
- **Hardware driver bugs**: `rmbloat` retries with CPU encoding; check the logs for the strategy that succeeded.
- **Bitmap subtitles**: these are dropped automatically; check the logs for related warnings.

The fallback behavior is described in [How It Works](how-it-works.md#intelligent-resilience-the-retry-ladder).

## rmbloat Won't Start - "Already Running"

```bash
ps aux | grep rmbloat
rm "${XDG_CACHE_HOME:-$HOME/.cache}"/rmbloat/*.lock
rmbloatd status
rmbloatd stop
```

## Slow Conversion Speed

Typical hardware-accelerated throughput is 30-60x realtime; CPU-only is 3-10x. If it is slower:

```bash
rmbloat --chooser-tests
rmbloat --prefer-strategy system_accel /path/to/videos
rmbloat --prefer-strategy docker_accel /path/to/videos
```

Check `htop` for FFmpeg CPU use and `intel_gpu_top` or `radeontop` for GPU use.
