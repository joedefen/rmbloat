# rmbloat accepted baseline

**Last confirmed:** 2026-11-10 · Python 3.13.5 (`.venv`) · 272 tests

This file is the standing record of design decisions that a later review should treat as settled. It lists what is accepted, not what once went wrong, so it can be read in one sitting and extended one bullet at a time.

The 2026-11-10 baseline audit's findings, evidence and resolutions are preserved verbatim in commit `1614eb7` (`Preping to trim audit`), which moved the full record to [`tests/AUDIT.md`](tests/AUDIT.md):

```sh
git show 1614eb7:tests/AUDIT.md
```

Nothing from that record is open. Do not re-file anything below without a reproduction against the current tree.

## How to use this file

- Read the relevant section before writing anything up. If the behaviour is listed here, it is intended.
- Each bullet names the code that owns the behaviour, so a claim can be checked cheaply rather than argued.
- When a review settles a new area, add one bullet here — not a narrative section, and not a second audit.

## Accepted baseline — not re-opened

### Data safety

- **Transactional file swap.** Verify the temp file, refuse an existing destination, use a unique rollback name, install by no-clobber hard link with a rename fallback, roll back on failure, and propagate `doit='ERR'`. [`_execute_file_swap()`](rmbloat/JobHandler.py:706)
- **Sampling is repeatable.** A retried job discards its previous private work directory, and a sample install replaces only a previous sample at the deterministic name. [`_install_sample()`](rmbloat/JobHandler.py:372)
- **Shrink/reject state machine.** `OPT` is distinct from `ERR`, the shrink guard applies to every transcode, and rejections do not trip auto-mode failure counters. [`finish_transcode_job()`](rmbloat/JobHandler.py:546)
- **Auto-mode termination.** The `--auto-hr` deadline, a drained queue, no selection, and the ten-consecutive-failure ladder all end the run. [`_manage_queue_and_auto_mode()`](rmbloat/rmbloat.py:475)
- **Job working directory.** Every downstream path is absolute; no code path calls `os.chdir()`.

### Cache, state and logs

- **Probe cache.** Keyed by absolute path *and* size, an `RLock` around every mutation, atomic store via `mkstemp` + `os.replace`, and a four-week prune. [`ProbeCache`](rmbloat/ProbeCache.py:98)
- **Negative cache for non-videos.** A readable file with no video stream gets the terminal `?NV` anomaly: one probe per size, persisted across runs, and it still never becomes a row. A missing `format` section beside a real video stream is deliberately *not* cached, because that shape usually means a truncated or still-copying file. [`_record_not_a_video()`](rmbloat/ProbeCache.py:427)
- **Batch reporting.** Skipped non-videos are reported as one aggregate stderr line, not one line per file.
- **Private data locations.** Logs in `$XDG_STATE_HOME/rmbloat`; probe cache and instance lock in `$XDG_CACHE_HOME/rmbloat`; directories 0700 and files 0600. [`default_log_dir()`](rmbloat/StructuredLogger.py:22), [`default_cache_dir()`](rmbloat/ProbeCache.py:51)
- **Atomic config and state.** `config.ini` and `state.json` are written through `mkstemp` + `os.replace`, so they inherit 0600 even though the containing directory is 0755. [`IniManager.write()`](rmbloat/IniManager.py:157)
- **Bounded history exports.** At most twenty `rmbloat-export-*.txt` files, at most one week, and nothing else is eligible for deletion. [`_prune_history_exports()`](rmbloat/FileOps.py:38)
- **Atomic log rewriting.** Trimming keeps chronological order while favouring `ERR`, and a purge rewrite never truncates the file in place. [`_atomic_rewrite()`](rmbloat/StructuredLogger.py:299)
- **The lock file outlives its owner.** The `flock` is the lock and the file holds no data, so release only unlocks and closes; acquisition uses `O_CREAT` without truncating. An unlocked file is exactly what `rmbloatd`'s staleness check expects. [`_acquire_instance_lock()`](rmbloat/ProbeCache.py:138)
- **A single instance is intended.**

### Sandbox and process control

- **Container hardening.** No network, read-only media mounts (`:ro,z`), no-new-privileges, capability drops, a private namespace, and `--ipc private` paired with `--shm-size` so the segment is sized rather than shared with the host. [`harden_container_args()`](rmbloat/Sandbox.py:113)
- **Local children.** Optional `bwrap`/`systemd-run` wrapping, an explicit environment allow-list, rlimits through [`exec_limits.py`](rmbloat/exec_limits.py:1), regular-file-only probes, and process-group termination so no ffmpeg descendant outlives a job. [`run_capped()`](rmbloat/Sandbox.py:377)
- **Docker identity parity and supply chain.** Probes and encodes share `--user`/`--group-add`; images are digest-pinned and never pulled implicitly.
- **Quoting.** Argument vectors are lists throughout; `shlex.quote` is used for display and for the tmux command string, which the shell re-parses exactly once.

### Parsing and CLI

- **Parser corpus gate.** [`run_regressions()`](rmbloat/VideoParser.py:435) returns a failure count and `python -m rmbloat.VideoParser` exits non-zero on regression; the corpus is current. `Gone with the Wind.avi` is deliberately `n/a`: with no year and no release token no movie pattern can anchor, and `standard_name()` falls back to codec-token purging.
- **Boolean flags.** Positive flags force `True`, `--no-*` force `False`, and the last flag on the command line wins.
- **Exit status is real.** `main()` returns a status; `--logs` requires `less` and says so; `--save-defaults` reports a failed write in its exit status.
- **Numeric options are range-checked while the command line is parsed.** [`bounded_int()`](rmbloat/rmbloat.py:1671)
- **Input filtering.** Only files with a video extension are candidates; missing, unreadable and non-video arguments are skipped without a probe, and skip prefixes match by basename.
- **`--version` is side-effect free.** It prints `rmbloat <version>` and exits 0 before any configuration is read or written, so a first run in a throwaway environment creates no files. One `VERSION_BANNER` feeds both that early answer and argparse's action, so `--help` and the printed line cannot disagree. [`VERSION_BANNER`](rmbloat/rmbloat.py:55)
- **The `--help` block in [`README.md`](README.md) is test-maintained.** A test compares every declared option's help text with the parser, so the snapshot is regenerated rather than hand-edited.
- **Status-line discipline.** The scan/probe status line and per-event diagnostics share stderr, keeping redirected output readable. [`StatusLine`](rmbloat/StatusLine.py:1)

### Versioning, packaging and gates

- **One release number.** `__version__` lives in [`rmbloat/__init__.py`](rmbloat/__init__.py:9) and `pyproject.toml` declares `dynamic = ["version"]`; the build backend and the release gate read that line without importing the package.
- **PEP 639 metadata.** `license = "MIT"` plus `license-files`, built with `flit_core>=3.12,<4` so `License-Expression`/`License-File` survive into the wheel while the artifacts stay at Metadata 2.4, which PyPI's upload API still accepts (flit_core 4.x writes 2.5, which it rejects with HTTP 400).
- **The release gate is vendored, not written here.** [`bless`](bless) is emitted byte-for-byte by `~/Projects/blessd/build.py` and never edited in this tree; `cmp ../blessd/bless bless` is silent and `bt audit` reports `current`. Its stages are the CI shape: `python -m ruff check .`, `python -m compileall -q rmbloat tests`, `python -m pytest -q --cov=rmbloat --cov-report=term-missing`, then build, [`tests/inspect_dist.py`](tests/inspect_dist.py:1), `twine check --strict`, and a smoke run of the installed console script with `--version`.
- **The quality gates are frozen in [`pyproject.toml`](pyproject.toml).** Ruff runs the committed `E4,E7,E9,F,I,B` selection at `line-length = 120` and `target-version = "py311"`; coverage has a 60% floor. Formatting and annotation-modernisation rules are excluded on purpose, so enabling one is a deliberate policy change, not an oversight.
- **[`./linter`](linter) is a local extra, not a gate.** Errors-only pylint on the project interpreter, exit 78 with an install hint when pylint is absent.
- **Hermetic imports in tests.** [`tests/runtime_stubs.py`](tests/runtime_stubs.py:21) installs the third-party stubs that application modules need.

## Checked and rejected — do not re-file

- **`-preset` on hardware encoders.** It is passed to software and hardware encoders alike on purpose; ffmpeg accepts it, so it is not a compatibility defect.
- **`config.ini` / `state.json` permissions.** They are not world-readable; the atomic write installs them 0600 (above), regardless of the directory mode.
- **Audio-only files being skipped.** Intended. One probe per file size is the contract, and the `?NV` negative cache is how it is met.
- **Pretty-printing pager output before `less`.** Rejected: the pager is reached by `os.execvp`, so a rendered temp file could never be cleaned up, and `+F` follow mode would be lost.
- **`rmbloatd` argument quoting.** `shlex.quote` produces text the shell re-parses exactly once, so paths with spaces survive and there is no extra injection surface.

## Future considerations — unaddressed, not defects

- **No CI workflow file.** The gate is run locally (`./bless`, `bt test` / `bt prod`). If CI is ever added, its shape is already settled by the gate: the three gate commands on the `dev`-extra interpreter, plus the inspector over a built wheel and sdist. No further work is planned beyond that adoption.
- **The curses UI is driven by hand.** No automated test constructs the screens; the untested surface is the UI event loop, not the conversion logic behind it.
- **Hardware and container encode paths are asserted statically.** CI has no GPU and no container runtime, so the argv the tool would emit is checked, and the runtime behaviour was verified by hand against Docker once. Re-verify by hand after touching [`FfmpegChooser`](rmbloat/FfmpegChooser.py:23).
- **No Windows or macOS claim.** POSIX process groups are assumed; the related tests skip where they cannot run.
- **[`tests/AUDIT.md`](tests/AUDIT.md) is the archive.** It holds the full 2026-11-10 record behind these decisions. If history is ever rewritten or squashed so that `1614eb7` stops resolving, that evidence is gone.

## How this baseline was last confirmed

- `ruff check .` clean under the frozen selection; `compileall -q rmbloat tests` silent.
- `pytest -q --cov=rmbloat` → 272 tests passing at 60.54% against the 60% floor.
- `python -m build` → `rmbloat-1.0.0`; [`tests/inspect_dist.py`](tests/inspect_dist.py:1) accepts the wheel and sdist pair.
- `./bless` exits 0 through every stage and stops before tagging, uploading or pushing; `bt audit` reports `current`.
