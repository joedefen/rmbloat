# rmbloat baseline audit (v0.8.5)

**Commit:** `04c21f8` · **Date:** 2026-11-10 · **Interpreter:** Python 3.13.5 (`.venv`)

## Scope and method

- Reviewed all 16 modules in [`rmbloat/`](rmbloat) (~7,500 lines), [`pyproject.toml`](pyproject.toml), [`README.md`](README.md), [`BLURB.md`](BLURB.md), [`linter`](linter), [`runner`](runner), and the test suite in [`tests/`](tests).
- Static review, the project's own verification gates (below), and targeted behavioural probes against the **real** classes rather than reimplementations: retry lifecycle, sample lifecycle, probe caching, cache pruning, thread error reporting, and the `linter` wrapper.
- FFmpeg was used only to test one option-compatibility hypothesis, which was **rejected** (see “Checked and rejected”). The container finding [M4](#m4---ipc-none-contradicts---shm-size-in-the-container-hardening) was filed as static analysis because the audit pass did not exercise a container runtime; it was subsequently **confirmed and fixed** against Docker 29.9.2 (evidence recorded under that finding).
- **The audit itself made no code changes.** It replaces the previous audit document. The remediation captured by that document is treated as the accepted baseline and was not re-litigated; see “Accepted baseline” for the decisions that are considered adequate. M1–M5 and L4–L6 were fixed after this baseline was taken and are marked resolved below.

## Baseline state (measured, not assumed)

| Gate | Command | Result |
| --- | --- | --- |
| Byte-compile | `.venv/bin/python -m py_compile rmbloat/*.py tests/*.py` | pass |
| Tests | `.venv/bin/python -m unittest discover -s tests -t .` | `OK` — 236 tests |
| Ruff | `.venv/bin/python -m ruff check --select F,E9 rmbloat tests` | `All checks passed!` |
| Pylint wrapper | `./linter` | exit 30 — see [L3](#l3-the-linter-wrapper-selects-an-interpreter-that-cannot-see-the-dependencies) |

Two environmental facts that matter for the upcoming CI work:

- The system `python3` cannot run the suite (`FAILED (errors=2)`): [`tests/test_video_parser_corpus.py`](tests/test_video_parser_corpus.py:1) and [`tests/test_video_parser_regressions.py`](tests/test_video_parser_regressions.py:1) import [`rmbloat.VideoParser`](rmbloat/VideoParser.py:32), which imports `ruamel.yaml`. This is **not** a defect: the suite is hermetic with respect to services and the filesystem, but it requires the package's declared dependencies to be installed. A CI job must install the project first.
- There are no workflow files in the repository and no dev-dependency group ([`pyproject.toml`](pyproject.toml:14) lists runtime dependencies only). CI configuration is the accepted next step, not an audit finding.

## Accepted baseline — not re-opened

The following areas were remediated previously and were **verified to still hold** where a cheap check existed. They are listed so future audits do not re-file them:

- **Transactional file swap.** Verify-temp, refuse-existing-destination, unique rollback name, no-clobber hard-link install with rename fallback, rollback on failure, and `doit='ERR'` propagation. No data-loss path was found in this pass. See [`_execute_file_swap()`](rmbloat/JobHandler.py:705).
- **Shrink/reject state machine.** `OPT` is distinct from `ERR`, the shrink guard applies to every transcode, and rejections do not trip auto-mode failure counters. See [`finish_transcode_job()`](rmbloat/JobHandler.py:545).
- **Auto-mode termination.** `--auto-hr` deadline, drained-queue exit, no-selection exit and the 10-consecutive-failure ladder all terminate the run. See [`_manage_queue_and_auto_mode()`](rmbloat/rmbloat.py:460).
- **Probe cache.** Path+size keying, `RLock` around every mutation, atomic store via `mkstemp` + `os.replace`, 4-week pruning, and — since [M5](#m5-files-that-are-not-videos-are-re-probed-on-every-single-run) — a negative cache for files that have no video stream. See [`ProbeCache`](rmbloat/ProbeCache.py:98).
- **Private data locations.** Logs in `$XDG_STATE_HOME/rmbloat`, cache and instance lock in `$XDG_CACHE_HOME/rmbloat`, directories 0700 and files 0600; atomic config/state writes. See [`default_log_dir()`](rmbloat/StructuredLogger.py:22), [`default_cache_dir()`](rmbloat/ProbeCache.py:51), [`IniManager.write()`](rmbloat/IniManager.py:151).
- **Sandbox design.** Container hardening, optional bwrap/systemd-run wrapping, environment allow-list, rlimits through [`exec_limits.py`](rmbloat/exec_limits.py:1), regular-file-only probes, and process-group termination. The design is accepted; [M4](#m4---ipc-none-contradicts---shm-size-in-the-container-hardening) was one internal flag inconsistency, now resolved without a redesign.
- **Docker identity parity and supply chain.** Probes and encodes share `--user`/`--group-add`; images are digest-pinned and never pulled implicitly.
- **Parser corpus gate.** [`VideoParser.run_regressions()`](rmbloat/VideoParser.py:433) returns a failure count, `python -m rmbloat.VideoParser` exits non-zero on regression, and the corpus is current. The `n/a` expectation for `Gone with the Wind.avi` is deliberate: with no year and no release token no movie pattern can anchor, and [`standard_name()`](rmbloat/ConvertUtils.py:243) falls back to codec-token purging.
- **CLI semantics and exit status.** Positive flags force `True`, `--no-*` force `False`, the last flag wins, `main()` returns a real status, and `--logs` requires `less` and says so.
- **Deliberate non-features.** Pretty-printing the log before paging was rejected (the pager is reached by `os.execvp`, so a rendered temp file could never be cleaned up, and `+F` follow mode would be lost). A single-instance policy is intended.
- **Job CWD isolation.** [`start_transcode_job()`](rmbloat/JobHandler.py:390) no longer calls `os.chdir()`; paths are absolute throughout. (`main_loop()`'s vestigial `chdir` has since been removed; see [L4](#l4-vestigial-process-state-and-dead-code).)
- **Status-line discipline** for scan and probe phases ([`StatusLine`](rmbloat/StatusLine.py:1)), and the shared hermetic stubs in [`tests/runtime_stubs.py`](tests/runtime_stubs.py:1).

---

# Medium-severity findings

### M1.✅ `--sample` cannot be run twice for the same video

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

**Where:** [`finish_transcode_job()`](rmbloat/JobHandler.py:613) builds the sample destination from the source directory plus the temp file's basename, then installs it with [`FileOps._rename_no_clobber()`](rmbloat/FileOps.py:158). The name is deterministic: `SAMPLE.<crf>.<standard_name>` ([`start_transcode_job()`](rmbloat/JobHandler.py:421)).

**Evidence (probe, real classes):** first attempt installs
`SAMPLE.28.movie.crf28.recode.mkv` and reports `doit=' OK'`; the second attempt on the same source produces:

```
ERROR: could not place sample '.../SAMPLE.28.movie.crf28.recode.mkv':
       destination already exists: .../SAMPLE.28.movie.crf28.recode.mkv
attempt 2: probe=False doit='ERR' work_dir_exists=True
```

**Impact:** sampling is a documented “test first” workflow ([`README.md:579`](README.md:579)), so re-sampling after changing a quality setting is the normal case, and it fails. Worse, the failure is counted as a conversion error: [`finish_transcode_job()`](rmbloat/JobHandler.py:642) increments `error_count`/`consecutive_failures` because `success` is `False` and `rejected` is `False`, so ten re-samples can end an `--auto-hr` run with exit status 1.

**Fix:** when the deterministic sample name already exists, either replace it only if it is a previous sample, or allocate a unique name with [`_unique_path()`](rmbloat/JobHandler.py:22). A rejection-shaped outcome (leave the existing sample, do not count an error) is also defensible.

**Test to add:** call `finish_transcode_job()` twice with a stubbed `TranscodeThread` and assert the second call does not set `doit='ERR'` and does not bump the failure counters.

---

### M2.✅ A retried job permanently leaks its previous private work directory and partial output

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

**Where:** each [`start_transcode_job()`](rmbloat/JobHandler.py:390) call creates a **new** `.rmbloat-tmp-*` directory ([`JobHandler.py:422`](rmbloat/JobHandler.py:422)) and a new `TEMP.*`/`SAMPLE.*` file, then overwrites `self.temp_dir`/`self.temp_file` ([`JobHandler.py:427`](rmbloat/JobHandler.py:427)). Retries re-enter this method from [`check_job_status()`](rmbloat/JobHandler.py:222). Cleanup only ever removed the *final* job's directory ([`JobHandler.py:659`](rmbloat/JobHandler.py:659), [`JobHandler.py:884`](rmbloat/JobHandler.py:884)).

**Evidence (probe, real classes):** one tolerant retry leaves two directories, both non-empty:

```
work dirs after one retry: ['.rmbloat-tmp-982290dg', '.rmbloat-tmp-ldz70za2']
non-empty leftovers: ['.rmbloat-tmp-982290dg/TEMP.movie.crf28.recode.mkv',
                      '.rmbloat-tmp-ldz70za2/TEMP.movie.crf28.recode.mkv']
```

**Impact:** the retry ladder exists precisely for corrupt media, which produce large partial encodes. Those files are never referenced again and, because the scanner excludes `.rmbloat-*` ([`ConvertUtils.py:23`](rmbloat/ConvertUtils.py:23)), neither the UI nor future runs reveal them. Disk usage grows one partial file per retry until an operator inspects the media directory by hand. The same fate applied to the private directory when the hard-link cleanup fails ([`JobHandler.py:822`](rmbloat/JobHandler.py:822)) or when a user skips a job from the Convert screen (the handler is torn down without `finish_transcode_job()`).

**Fix:** destroy the previous attempt's directory (and its temp file) before allocating the new one, and reuse one directory per `Vid` for the whole retry ladder.

**Test to add:** start a job, retry it, then assert exactly one `.rmbloat-tmp-*` directory exists; and assert a skipped job leaves none.

---

### M3.✅ Log trimming and history purging rewrite the log in place

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

**Where:** [`_trim_log_file()`](rmbloat/StructuredLogger.py:325) and [`purge_at_or_before()`](rmbloat/StructuredLogger.py:476) both rewrote the live file in place with `open(self.log_file, 'w')` after reading it. Every other persistent store in the project was made atomic (`config.ini`, `state.json`, the probe cache).

**Impact:** a crash or full disk inside that window truncates `events.jsonl` to a prefix or to zero, losing the entire history the History screen and `--logs` exist to show. The trim path runs automatically whenever the 50 MB cap is exceeded, and it rewrites the whole file, so the window is not small. Additionally, `purge_at_or_before()` silently drops malformed lines instead of preserving them, so one corrupt line deletes itself on the next purge.

**Fix:** write survivors to a `mkstemp` sibling and `os.replace()` it, exactly as [`ProbeCache.store()`](rmbloat/ProbeCache.py:502) does; keep unparseable lines rather than deleting them.

**Test to add:** simulate a failure of `os.replace()` during trim and assert the original log is intact.

---

### M4.✅ `--ipc none` contradicts `--shm-size` in the container hardening

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

**Where:** [`harden_container_args()`](rmbloat/Sandbox.py:113) emitted both `--ipc none` and `--shm-size 1g`. Its own docstring explained that the shm allowance exists because “VA-API historically uses shared memory for buffer hand-off; the default 64 MB is too small for 4K surfaces”.

**Evidence (measured, Docker 29.9.2, same runtime and image the tool uses):**

```
$ docker run --rm --ipc none    --shm-size 1g debian:13-slim df -h /dev/shm
df: /dev/shm: No such file or directory

$ docker run --rm --ipc private --shm-size 1g debian:13-slim df -h /dev/shm
Filesystem      Size  Used Avail Use% Mounted on
shm             1.0G     0  1.0G   0% /dev/shm
```

`--ipc none` gives the container a private IPC namespace **without** a shared-memory mount at all, so `--shm-size` is silently discarded. Only modes that keep a private shm segment (`private`, `shareable`) or share the host's (`host`) honour it.

**Impact:** the confirmed defect is that the allowance was ineffective, i.e. the flag pair did not mean what the docstring claimed. The originally filed wording (“hardware encoding will fail or silently fall back on 4K”) was **too strong**: a single-process 4K `h264_vaapi` encode of a 3840×2160 source completed under *both* IPC modes in this image, so no failure was reproducible. The risk is limited to pipelines that actually attach to `/dev/shm` — where a missing `/dev/shm` is a hard error — rather than to VA-API encoding in general. Tests only asserted that `--shm-size` was present and `--ipc=host` absent, so nothing could catch the discarded allowance.

**Fix (applied):** the mode is now the explicit constant [`DEFAULT_IPC_MODE = 'private'`](rmbloat/Sandbox.py:59), which [`harden_container_args()`](rmbloat/Sandbox.py:113) emits at [`Sandbox.py:136`](rmbloat/Sandbox.py:136) next to `--shm-size` at [`Sandbox.py:140`](rmbloat/Sandbox.py:140). `private` isolates the IPC namespace from the host exactly as `none` did, but keeps the sized shm segment. The mode is an overridable keyword argument, and the docstring now states why `none` must not be used.

**Verification:** the full hardened vector produced by `harden_container_args(memory_mb=2048)` was accepted by Docker 29.9.2 against the real `joedefen/ffmpeg-vaapi-docker:latest` image and reported `shm 1.0G`; a 4K `h264_vaapi` encode through `-vaapi_device /dev/dri/renderD128` succeeded (50 frames, exit 0); and a `run_tests()` pass over a generated 3840×2160 sample emitted `… --ipc private --cap-drop ALL … --shm-size 1g …` and reported `✓ docker_cpu 18.0s` / `✓ system_cpu 6.9s`, exit 0.

**Test to add (applied):** assert the IPC mode is compatible with `--shm-size`. [`assert_hardened()`](tests/test_sandbox_hardening.py:59) now rejects any IPC mode outside `private`/`shareable` and never accepts `host`, so every generated container command — probe and encode alike — is checked, and [`tests/test_sandbox_hardening.py:75`](tests/test_sandbox_hardening.py:75) pins the mode to `private` explicitly.

---

### M5.✅ Files that are not videos are re-probed on every single run

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

**Where:** [`_get_metadata_with_ffprobe()`](rmbloat/ProbeCache.py:208) returned `None` when ffprobe reported no video stream (the branch is at [`ProbeCache.py:295`](rmbloat/ProbeCache.py:295)), without writing a cache entry. [`get()`](rmbloat/ProbeCache.py:638) only stores a result when one was produced, and [`batch_get_or_probe()`](rmbloat/ProbeCache.py:742) omits those paths from its results.

**Evidence (probe, real classes, stubbed `run_capped` returning an audio-only stream list):**

```
probe results: None None
ffprobe invocations for two get() calls: 2
cache entries stored: {}
```

**Impact:** the skip is intentional (audio-only files must not become rows), but there was no negative cache, so every run re-launched ffprobe for every audio-only `.mp4`/`.mkv` it encountered — each with a 30-second timeout ([`ProbeCache.py:270`](rmbloat/ProbeCache.py:270)). A media share that mixes music and video paid that cost on every scan, and the per-file error text was printed each time. Transient probe failures *are* cached (`?P1`…`?P9`), so the behaviour was also inconsistent.

**Fix (applied):** record a durable “no video stream” anomaly — [`NOT_A_VIDEO_ANOMALY = '?NV'`](rmbloat/ProbeCache.py:35) — via [`_record_not_a_video()`](rmbloat/ProbeCache.py:407), which is terminal in [`_get_valid_entry()`](rmbloat/ProbeCache.py:579), so the file is skipped from the cache until its size changes. A distinct token is used rather than `?P9` so the retry ladder keeps its own meaning: `?P9` still means “the probe failed nine times” and still becomes a row, while `?NV` never does.

**Test to add (applied):** two `get()` calls on the same audio-only file must result in exactly one ffprobe invocation ([`tests/test_probe_negative_cache.py:79`](tests/test_probe_negative_cache.py:79)).

---

## Resolved after this baseline

A ✅ in a finding's title means it has been fixed since the baseline was taken; the text of each finding keeps the original diagnosis, and the subsection here records what changed and which tests now cover it.

### M1 — repeated `--sample` runs now replace the previous sample

[`_install_sample()`](rmbloat/JobHandler.py:371) moves the finished sample beside its source. When the deterministic destination already exists it is treated as a previous sample (possibly a truncated leftover from a crash) and replaced atomically with `os.replace()`; a directory at the destination is still refused, and the old sample is never left half-deleted. A replaced sample is recorded in `vid.ops` as `sample '…' (replaced previous sample)`, the job stays successful, and the auto-mode failure counters are untouched. Regression test: [`tests/test_file_swap.py:369`](tests/test_file_swap.py:369) finishes two sample jobs for the same source and asserts the second reports `doit=' OK'`, leaves only the newest sample beside the source, and keeps `error_count`/`consecutive_failures` at zero.

### M2 — a retried job no longer leaks its previous work directory

[`_discard_pending_temp()`](rmbloat/JobHandler.py:346) removes the previous attempt's temp output and its private directory at the top of every [`start_transcode_job()`](rmbloat/JobHandler.py:390), before a new `.rmbloat-tmp-*` directory is allocated. `Sandbox.remove_private_work_dir()` still removes only an empty directory, so a preserved output from a failed install is never deleted by this path. The exit path already removed the private directory ([`emergency_cleanup()`](rmbloat/JobHandler.py:112)), so a skipped job leaves nothing behind. Regression tests: [`tests/test_job_cwd_isolation.py:176`](tests/test_job_cwd_isolation.py:176) retries a job and asserts exactly one private directory survives, and [`tests/test_job_cwd_isolation.py:199`](tests/test_job_cwd_isolation.py:199) asserts that exit cleanup after a started job removes the partial file and the directory.

### M3 — log trim and history purge are now atomic

New [`_atomic_rewrite()`](rmbloat/StructuredLogger.py:299) writes the survivors to a 0600 `mkstemp` sibling inside the log directory, flushes and fsyncs it, then installs it with `os.replace()`. A failure unlinks the sibling and leaves `events.jsonl` byte-for-byte intact. Both the automatic trim at the 50 MB cap ([`_trim_log_file()`](rmbloat/StructuredLogger.py:325)) and the History screen's [`purge_at_or_before()`](rmbloat/StructuredLogger.py:476) use it. Unparseable lines are no longer deleted: trim carries them through verbatim ([`StructuredLogger.py:352`](rmbloat/StructuredLogger.py:352)) and purge keeps any line it cannot interpret instead of dropping it ([`StructuredLogger.py:499`](rmbloat/StructuredLogger.py:499)). Regression tests: [`tests/test_log_trim.py:99`](tests/test_log_trim.py:99) forces `os.replace()` to fail during a trim and asserts the original bytes are unchanged and no temp sibling remains; [`tests/test_log_trim.py:122`](tests/test_log_trim.py:122) and [`tests/test_log_trim.py:133`](tests/test_log_trim.py:133) assert malformed lines survive trim and purge.

### M4 — container IPC mode now keeps the shared-memory segment

The container IPC namespace is still isolated from the host, but it is now the explicit [`DEFAULT_IPC_MODE = 'private'`](rmbloat/Sandbox.py:59) rather than `none` ([`harden_container_args()`](rmbloat/Sandbox.py:113), flag at [`Sandbox.py:136`](rmbloat/Sandbox.py:136)). `private` is equally isolated, and it is one of the modes where `--shm-size` ([`Sandbox.py:140`](rmbloat/Sandbox.py:140)) actually applies — under `none` the container had no `/dev/shm` at all, so the 1 GB allowance was thrown away. The mode is a keyword argument (`ipc_mode=`), so the choice is explicit rather than incidental, and the docstring now records why `none` is wrong here. Regression tests: [`tests/test_sandbox_hardening.py:75`](tests/test_sandbox_hardening.py:75) asserts the emitted mode is `private` and not `none`; [`tests/test_sandbox_hardening.py:82`](tests/test_sandbox_hardening.py:82) asserts the mode stays `private` with and without acceleration and is never `host`; [`tests/test_sandbox_hardening.py:90`](tests/test_sandbox_hardening.py:90) asserts an override still carries `--shm-size`. Every container command generated by the chooser is checked by the shared [`assert_hardened()`](tests/test_sandbox_hardening.py:59) helper, which now rejects any IPC mode that cannot honour `--shm-size`.

### M5 — files with no video stream are probed once

ffprobe's “no video stream” verdict is now cached under a dedicated anomaly, [`NOT_A_VIDEO_ANOMALY = '?NV'`](rmbloat/ProbeCache.py:35), written by [`_record_not_a_video()`](rmbloat/ProbeCache.py:407) when the file exists and is readable but its stream list has no `video` entry ([`ProbeCache.py:295`](rmbloat/ProbeCache.py:295)). Like every other entry it is keyed by path *and* size, so a replaced file is probed again, and it ages out with the 4-week prune if the file stops being scanned. [`_get_valid_entry()`](rmbloat/ProbeCache.py:579) treats `?NV` as terminal instead of re-running the probe, and both [`get()`](rmbloat/ProbeCache.py:638) and [`batch_get_or_probe()`](rmbloat/ProbeCache.py:742) translate the verdict back into “absent”, so the file still never becomes a row. A missing `format` section alongside a real video stream is deliberately *not* cached: that shape usually means a truncated or still-copying file, which must stay undecided so a later run can succeed. The per-file error print is now one aggregate stderr line ([`ProbeCache.py:770`](rmbloat/ProbeCache.py:770)), because naming thousands of audio tracks on every scan would bury the files that need attention. Regression tests in [`tests/test_probe_negative_cache.py`](tests/test_probe_negative_cache.py:1): [`test_repeated_get_probes_an_audio_only_file_once()`](tests/test_probe_negative_cache.py:79) reproduces the audit's measurement and asserts one invocation for two `get()` calls, [`test_negative_verdict_is_terminal_in_the_validator()`](tests/test_probe_negative_cache.py:92) shows the lookup is a cache hit that is still not a probe, [`test_negative_verdict_is_persisted_for_the_next_run()`](tests/test_probe_negative_cache.py:103) opens a second `ProbeCache` over the same directory, [`test_changed_file_size_reprobes()`](tests/test_probe_negative_cache.py:116) invalidates by size, [`test_batch_omits_cached_non_videos_and_counts_them()`](tests/test_probe_negative_cache.py:127) covers the aggregate report, and [`test_video_without_format_data_is_not_cached_as_missing_a_stream()`](tests/test_probe_negative_cache.py:153) pins the boundary.

### L4 — vestigial process state and dead code are gone

`main_loop()` ([`rmbloat.py:984`](rmbloat/rmbloat.py:984)) is now just a pass over `ppps` followed by the window switch: no `os.chdir()`, no unreachable `if not file_dir:` fallback, and no `except Exception: raise`. `Converter.original_cwd` and `Converter.state` are gone, as is the uncalled `PersistentState.save_updated_opts()`. The no-op `if use_acceleration: pass` branch is gone from [`harden_container_args()`](rmbloat/Sandbox.py:113); the argument itself stays, because [`FfmpegChooser`](rmbloat/FfmpegChooser.py:182) passes it to state intent, and its docstring now says the render node is added by the caller via `--device`. Regression tests in [`tests/test_diagnostics.py`](tests/test_diagnostics.py:38): [`test_main_loop_never_enters_the_video_directory()`](tests/test_diagnostics.py:59) runs the loop from an unrelated directory and asserts the CWD never changes, and the four `VestigialCodeTests` ([`tests/test_diagnostics.py:137`](tests/test_diagnostics.py:137)) assert the `chdir`, the two attributes, the helper method, and the no-op branch have not returned.

### L5 — probe errors now share stderr with the probe status line

Every `print()` inside [`_get_metadata_with_ffprobe()`](rmbloat/ProbeCache.py:208) passes `file=sys.stderr` ([`ProbeCache.py:231`](rmbloat/ProbeCache.py:231)), covering the missing-file, stat, non-regular-file, timeout, truncation, missing-stream, JSON-decode, missing-ffprobe, size, and unusable-metadata paths. A probe error can no longer be painted over by the in-place status line, nor interleave with the detection summary when stdout is redirected. Regression test: [`tests/test_diagnostics.py:81`](tests/test_diagnostics.py:81) probes a missing file and asserts stdout stays empty while stderr carries the message.

### L6 — transcode diagnostics keep the real error text

`run()` interpolates the exception itself, so the job log now reads `JobERR: boom` ([`TranscodeThread.py:65`](rmbloat/TranscodeThread.py:65)) instead of `JobERR: str(boom)`. `run_ffmpeg()` flushes the unterminated final fragment through [`_flush_partial_line()`](rmbloat/TranscodeThread.py:167) before marking the job finished ([`TranscodeThread.py:164`](rmbloat/TranscodeThread.py:164)), so a one-line ffmpeg failure that never emits a newline still reaches the job log and the retry ladder's signal search. Leftover progress blocks are recognised by their leading key ([`PROGRESS_PREFIX_RE`](rmbloat/TranscodeThread.py:24)) and are not reported as messages. Regression tests: [`tests/test_diagnostics.py:108`](tests/test_diagnostics.py:108) asserts `JobERR: boom` and that no literal `str(` survives; [`tests/test_diagnostics.py:119`](tests/test_diagnostics.py:119) and [`tests/test_diagnostics.py:128`](tests/test_diagnostics.py:128) cover the flushed error line and the ignored progress fragment.

---

# Low-severity, docs, and packaging findings

### L1. `--save-defaults` cannot report a failed write

[`main()`](rmbloat/rmbloat.py:1809) prints the new defaults, calls `ini.write()` and then `sys.exit(0)`. [`IniManager.write()`](rmbloat/IniManager.py:195) catches every exception, prints an error and returns `None`, so a full disk or permission error still yields exit status 0. Scripts (and the forthcoming CI/cron usage) cannot tell whether defaults were persisted. Make `write()` return a boolean and propagate it into the exit status.

### L2. History clipboard exports are never pruned

[`write_history_export()`](rmbloat/FileOps.py:25) creates a new uniquely named `rmbloat-export-*.txt` in the cache directory on every copy and nothing ever removes them. Each copy contains at least one full log entry, including media paths and ffmpeg output. A session spent inspecting History accumulates files indefinitely. Prune old exports (age or count) when writing a new one.

### L3. The `linter` wrapper selects an interpreter that cannot see the dependencies

[`linter`](linter:24) prefers `python -m pylint` and falls back to a PATH `pylint` when the chosen interpreter cannot import pylint. In this checkout the virtualenv has no pylint while `/usr/bin/pylint` exists and runs under another interpreter, so `./linter` reports `E0401` for `console_window` and `send2trash` and exits 30. It also runs pylint's full default check set, which reports style messages the wrapper's own comment says are Ruff's job. Consequences: the wrapper cannot serve as a CI gate as written, and a clean checkout fails.

**Fix:** declare pylint as a dev dependency (or vendor a `[project.optional-dependencies]` group), treat “pylint not importable by `$PYTHON`” as an error instead of silently switching interpreters, and pass `--errors-only` to match the documented intent.

### L4.✅ Vestigial process state and dead code

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

- `main_loop()` performed a process-wide `os.chdir()` per file and restored it in `finally`. Every path downstream is absolute (`get_candidate_video_files()` resolves, `VideoParser` calls `abspath`, the swap and rename helpers use absolute paths), so the change was a no-op that could still fail if the directory vanished between the scan and the processing step. Its `if not file_dir:` branch was unreachable for absolute paths, and its `except Exception: raise` existed only to hold a commented-out handler.
- `Converter.state` was assigned once and never read, and `Converter.original_cwd` existed only so the loop could restore the directory it had just changed.
- `PersistentState.save_updated_opts()` had no callers.
- `harden_container_args()` contained an `if use_acceleration: pass` branch.

### L5.✅ Probe error messages are printed to stdout while the probe status line owns stderr

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

[`_get_metadata_with_ffprobe()`](rmbloat/ProbeCache.py:208) and its error paths used bare `print(...)`, i.e. stdout, while [`batch_get_or_probe()`](rmbloat/ProbeCache.py:742) keeps an in-place status line on stderr ([`StatusLine`](rmbloat/StatusLine.py:1)). Both share one terminal cursor, so a per-file probe error landed on the progress row and was immediately overwritten, and when stdout was redirected the messages interleaved with the detection summary. Every message from that function now goes to stderr, the stream the batch summary already uses ([`ProbeCache.py:865`](rmbloat/ProbeCache.py:865)).

### L6.✅ Transcode diagnostics lose information

**Status: resolved after this baseline** — see [Resolved after this baseline](#resolved-after-this-baseline).

- [`TranscodeThread.run()`](rmbloat/TranscodeThread.py:56) formatted its failure text as `f'JobERR: str({e})'`, producing the literal `JobERR: str(boom)`; the exception text was embedded but the `str(...)` wrapper was never evaluated. (The probe reproduced that exact text.)
- The final stderr fragment was discarded: `partial_line` was only consumed when a later `[\r\n]+` arrived ([`TranscodeThread.py:113`](rmbloat/TranscodeThread.py:113)), and the loop exited on `poll()` ([`TranscodeThread.py:153`](rmbloat/TranscodeThread.py:153)) without flushing it. ffmpeg errors are commonly the last line before exit, so the most useful error could be missing from the job log and from the retry ladder's signal search ([`JobHandler.py:296`](rmbloat/JobHandler.py:296)).

### L7. Numeric CLI options are not range-checked, and `-t` is ignored by the hardware path

[`--thread-cnt`](rmbloat/rmbloat.py:1755) has no `choices` or range check. A value above 256 raises `ValueError` inside [`make_ffmpeg_cmd()`](rmbloat/FfmpegChooser.py:979) after the job has started; the exception propagates out of `advance_jobs()` and terminates the curses session via [`main()`](rmbloat/rmbloat.py:1885) instead of reporting a bad option. The hardware branch never reads `params.thread_count`, so `-t` is silently ignored for VAAPI even though the help text says “thread count for ffmpeg conversions”. Validate the range at parse time (and document the hardware behaviour) so a typo cannot kill an unattended run.

### L8. Documentation drift

- [`IniManager.default_config_dir()`](rmbloat/IniManager.py:80) still describes itself as the single source of truth for “`config.ini`, `state.json`, and probe cache (and therefore the probe-cache lock file)”. Both the cache and its lock live in `$XDG_CACHE_HOME/rmbloat` ([`default_cache_dir()`](rmbloat/ProbeCache.py:51)); only config and state remain in `~/.config/rmbloat`.
- [`README.md:263`](README.md:263) documents `--sample` as producing `SAMPLE.{input-file}`, while the option's help text ([`rmbloat.py:1777`](rmbloat/rmbloat.py:1777)) says `SAMPLE.<crf>.<output>`; the code produces `SAMPLE.<crf>.<standard_name>` in a private work directory and then moves it beside the source.

### L9. The single-instance lock unlinks its own lockfile

[`_release_instance_lock()`](rmbloat/ProbeCache.py:157) unlocks and then `os.remove()`s the lock path. A process that opened the same inode microseconds earlier can acquire a lock on an already-unlinked file while a third process creates a new lockfile and acquires it, allowing two instances despite the documented policy ([`README.md:639`](README.md:639)). Leaving the lockfile in place (the `flock` itself is the lock) removes the race; the staleness test in [`rmbloatd`](rmbloat/rmbloatd.py:125) already treats an unlocked file as stale.

---

## Checked and rejected

- **VAAPI `-preset` incompatibility (suspected, disproved).** [`make_ffmpeg_cmd()`](rmbloat/FfmpegChooser.py:1001) appends `-preset` for hardware encoders too. `ffmpeg -c:v hevc_vaapi -preset medium` did not reject the option; it proceeded to device/filter initialisation and failed only because this host has no VAAPI surface path for `nullsrc`. Not a defect.
- **`config.ini` / `state.json` exposure (suspected, disproved).** Both are installed from a `mkstemp` file (0600) via `os.replace`, so they inherit private permissions even though the containing directory is created 0755. Media paths are not world-readable.
- **`--sample` producing a temp path in the UI (previously fixed)** remains fixed: only `basename1` is stored ([`JobHandler.py:630`](rmbloat/JobHandler.py:630)).
- **Audio-only files being skipped** is intended behaviour; only the missing negative cache was filed ([M5](#m5-files-that-are-not-videos-are-re-probed-on-every-single-run)), and that is now fixed without changing the skip.
- **`rmbloatd` argument quoting** is safe: [`shlex.quote`](rmbloat/rmbloatd.py:227) produces text that is re-parsed exactly once by the shell that tmux starts, so paths with spaces survive and no extra injection surface was found.
- **Parser corpus expectations** were re-verified: `0 failures of 20 tests`.

## Security posture (delta)

No new injection, traversal, or privilege surface was found. Argument vectors are still lists, `shlex.quote` is used for display/logging and for the tmux command string, container mounts keep `:z`/`:ro,z`, images stay digest-pinned, and local children get the allow-listed environment plus rlimits. [M4](#m4---ipc-none-contradicts---shm-size-in-the-container-hardening) did not weaken isolation: `--ipc private` is a host-private namespace, exactly as `none` was, and the fix restores the intended `/dev/shm` sizing rather than sharing the host's IPC. The remaining security-adjacent item this pass is [L2](#l2-history-clipboard-exports-are-never-pruned) — unbounded accumulation of exported log entries, which contain media paths.

## CI readiness and coverage gaps

The suite is hermetic apart from the installed dependencies and POSIX process groups ([`tests/test_sandbox_hardening.py:200`](tests/test_sandbox_hardening.py:200) skips on Windows). Recommended CI shape: install the project, then `py_compile`, `unittest discover`, and `ruff check --select F,E9`; leave `./linter` out until [L3](#l3-the-linter-wrapper-selects-an-interpreter-that-cannot-see-the-dependencies) is addressed.

Regression coverage was added for the now-resolved [M1](#m1---sample-cannot-be-run-twice-for-the-same-video), [M2](#m2-a-retried-job-permanently-leaks-its-previous-private-work-directory-and-partial-output), [M3](#m3-log-trimming-and-history-purging-rewrite-the-log-in-place), [M4](#m4---ipc-none-contradicts---shm-size-in-the-container-hardening), [M5](#m5-files-that-are-not-videos-are-re-probed-on-every-single-run), [L4](#l4-vestigial-process-state-and-dead-code), [L5](#l5-probe-error-messages-are-printed-to-stdout-while-the-probe-status-line-owns-stderr), and [L6](#l6-transcode-diagnostics-lose-information) (see [Resolved after this baseline](#resolved-after-this-baseline)). The M4 assertions are static — they check the argv the tool emits, because CI has no container runtime and no GPU; the runtime behaviour was verified once by hand and recorded under the finding. No test currently covers `--save-defaults` failure ([L1](#l1---save-defaults-cannot-report-a-failed-write)).

## Suggested order

1. **L1**, **L7**, **L9** — exit-status and input-validation polish that matters once CI and cron drive the tool.
2. **L3**, **L8**, **L2** — tooling and documentation hygiene.

## Verification performed for this audit

- `py_compile`, the 212-test suite, and `ruff --select F,E9` all passed under `.venv` at the time of the audit (table above).
- After the M1/M2/M3 fixes: `py_compile`, the 218-test suite (six new regressions), `ruff --select F,E9`, and `git diff --check` all pass.
- After the M4 fix: `py_compile`, the 221-test suite (nine new regressions in total), `ruff --select F,E9`, and `git diff --check` all pass.
- After the L4/L5/L6 fixes: `py_compile`, the 230-test suite (eighteen new regressions in total), `ruff --select F,E9`, and `git diff --check` all pass. The nine L4/L5/L6 regressions live in [`tests/test_diagnostics.py`](tests/test_diagnostics.py:1).
- After the M5 fix: `py_compile`, the 236-test suite (twenty-four new regressions in total), `ruff --select F,E9`, and `git diff --check` all pass. The six M5 regressions live in [`tests/test_probe_negative_cache.py`](tests/test_probe_negative_cache.py:1).
- M4 runtime verification (Docker 29.9.2, `joedefen/ffmpeg-vaapi-docker:latest`, `/dev/dri/renderD128`): `--ipc none --shm-size 1g` yields no `/dev/shm`; `--ipc private --shm-size 1g` yields `shm 1.0G`; the full hardened vector is accepted; a 4K `h264_vaapi` encode exits 0; and `run_tests()` on a generated 3840×2160 sample prints the `--ipc private … --shm-size 1g` command and reports both CPU strategies as successful.
- The system interpreter fails only on the two modules needing `ruamel.yaml`, confirming the dependency (not code) explanation.
- `./linter` exit 30 with four `E0401` errors, from `/usr/bin/pylint` (`#!/usr/bin/python3`) rather than the project interpreter.
- Behavioural probes (scratch harness outside the repository, stubs from [`tests/runtime_stubs.py`](tests/runtime_stubs.py:21)) reproduced: two leftover work directories after one retry; a second sample run setting `doit='ERR'`; two ffprobe invocations and zero cache entries for one audio-only file; an expired cache entry surviving `store()` with `dirty_count == 0`; and the literal `JobERR: str(boom)` text.
- `ffmpeg -f lavfi -i nullsrc=... -c:v hevc_vaapi -preset medium` was used only to disprove the `-preset` hypothesis.
