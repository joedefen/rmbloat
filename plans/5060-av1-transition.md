# RTX 5060 + AV1 Transition Plan

Goal: run Whisper transcription on the RTX 5060 with minimal downtime, keep rmbloat converting throughout, and add AV1 output last.

## Verified current state

- GPU: RTX 3060 Ti (Ampere, compute 8.6), driver 550.163.01.
- CPU/iGPU: Intel i3-13100, UHD 730 (Gen12). `vainfo` shows HEVC 8/10-bit encode (`hevc_vaapi`); no AV1 encode.
- Host ffmpeg includes `libsvtav1`, `libaom-av1`, `librav1e`, `h264_nvenc`, `hevc_nvenc`, `av1_nvenc` (compiled).
- `av1_nvenc` fails today with `No capable devices found` (Ampere has no AV1 encoder).
- HEVC throughput (synthetic 1080p): UHD 730 ~4.49x, 3060 Ti ~7.42x, libx265 medium ~0.92x.

## Blackwell blockers

| Area | Current | 5060 needs |
| --- | --- | --- |
| NVIDIA driver | 550.163.01 | >= 570 |
| Whisper CUDA base | CUDA 12.4.1 | CUDA >= 12.8 |
| Whisper CUDA arch | 86;89 | add 120 (sm_120) |
| rmbloat encoding | VA-API only | NVENC backend (new code) |

## Phase 0 - Prep now (no downtime)

Whisper ([my-whisper-cpp-12](../../my-whisper-cpp-12/Dockerfile)):
- Dockerfile: base `nvidia/cuda:12.8.x-devel-ubuntu22.04` and `12.8.x-runtime-ubuntu22.04`; set `-DCMAKE_CUDA_ARCHITECTURES="86;89;120"`.
- Confirm whisper.cpp v1.9.5 / ggml v0.26.0 builds for sm_120; bump the pin if not.
- Build candidate: `PUSH=0 GIT_TAG=0 IMAGE=joedefen/whisper-cuda-12:candidate ./build.sh`.
- Run `./test.sh` on the 3060 Ti; both CPU and CUDA0 paths must pass.

rmbloat:
- Run a baseline HEVC batch on the iGPU; `rmbloat --chooser-tests` should confirm `system_accel`.
- Branch-only prep (kept off by default): NVENC backend in [FfmpegChooser.py](../rmbloat/FfmpegChooser.py) (detect `/dev/nvidia*` and `nvidia-smi`, probe `hevc_nvenc`, emit `-rc vbr -cq -preset pN -tune hq`); add `--codec` plumbing that still defaults to `hevc`.
- Confirm `libsvtav1` produces an AV1 sample (software fallback).

## Phase 1 - Swap + driver (the only downtime)

- Finish or cleanly abort the in-flight rmbloat job; temp/private work dirs are cleaned on the next run.
- Power down; swap 3060 Ti to 5060; install driver >= 570; reboot.
- Verify `nvidia-smi` reports `compute_cap 12.0` and `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi` works.
- Resume rmbloat on the iGPU immediately.
- Rollback: keep the 3060 Ti and the last-good driver package; the Whisper CPU backend still runs.

## Phase 2 - Whisper on 5060; optional NVENC-HEVC

- Run `IMAGE=joedefen/whisper-cuda-12:candidate ./test.sh`; expect `using CUDA0 backend`; compare to the ~90 s baseline.
- Publish with `./build.sh` once green.
- rmbloat: driver >= 570 enables `hevc_nvenc`; enable the NVENC backend as opt-in, sample with `rmbloat -s`, then choose the default (iGPU efficiency vs NVENC speed).

## Phase 3 - AV1 in rmbloat (last, no downtime)

- Add `--codec av1`: software `libsvtav1`, hardware `av1_nvenc`.
- AV1 scales differ: CRF 0-63 (not 0-51), SVT numeric preset, NVENC `-cq`/`-preset pN`; add a `-cq` case to `finalize_standard_name`.
- Default to 10-bit for AV1; change the `x265` filename tag to `av1`; extend `is_allowed_codec` so AV1 sources are not AV1-to-AV1.
- Sample, then batch: `rmbloat -s --codec av1 <dir>`, then a full run.
- Keep HEVC as the default for older-client compatibility.

## Conversion driver per phase

| Phase | Whisper | rmbloat | Downtime |
| --- | --- | --- | --- |
| 0 Prep | build candidate, test on 3060 Ti | HEVC on iGPU; NVENC/`--codec` on a branch | none |
| 1 Swap+driver | - | pause, reboot, resume HEVC on iGPU | reboot window |
| 2 Validate | 5060 CUDA0 green, publish | HEVC continues; optional NVENC-HEVC opt-in | none |
| 3 AV1 | stable | add `--codec av1`; drive AV1 batches | none |

## Notes

- The system ffmpeg path covers NVENC and AV1; no change to `ffmpeg-vaapi-docker` is required for this plan.
- AV1 direct-play needs modern clients; older TVs/clients may force server transcodes, so HEVC stays the safe default.
