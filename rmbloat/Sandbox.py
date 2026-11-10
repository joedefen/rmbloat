#!/usr/bin/env python3
"""Sandbox helpers for untrusted media processing.

Every ffmpeg/ffprobe invocation goes through this module so the hardening is
defined once instead of drifting between the probe and encode paths.

Two layers are provided:

* **Container hardening** (:func:`harden_container_args`) - flags added to the
  existing docker/podman wrapper.  The container root filesystem is read-only,
  the network is disabled, capabilities are dropped, and the PID count is
  bounded.  The caller still supplies the image, user, device, and bind mounts.
* **Local sandboxing** (:func:`wrap_local`) - an optional bubblewrap or
  systemd-run wrapper for the default host-ffmpeg path, plus environment
  scrubbing and (via ``exec_limits.py``) resource limits.

The limits are deliberately conservative in what they *forbid* and generous in
what they *allow*: VA-API drivers reserve large address space and x265 spawns
many threads, so an aggressive memory/PID cap would break legitimate encodes.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Sequence

# Strategies accepted on the command line.  ``auto`` picks the strongest
# available local strategy (currently bubblewrap) and never silently pretends
# that an unsandboxed run is sandboxed.
SANDBOX_CHOICES = ('auto', 'container', 'bwrap', 'systemd', 'none')

# Input protocols ffmpeg may use.  Media files are local, so the network
# protocols (http, tcp, udp, rtmp, data, crypto, ...) are not needed and are
# the vector by which a "local" file can make ffmpeg fetch remote data.
PROTOCOL_WHITELIST = 'file,pipe'

# Probe output is bounded independently of the decoder's willingness to emit
# data.  Real ffprobe JSON for a feature film is a few kilobytes.
PROBE_MAX_STDOUT = 8 * 1024 * 1024
PROBE_MAX_STDERR = 64 * 1024

# As explained in the module docstring, these are upper bounds, not tuning
# knobs for memory: they stop runaway growth without rejecting real encodes.
DEFAULT_PIDS_LIMIT = 1024
DEFAULT_SHM_SIZE = '1g'

# IPC isolation mode for container runs.  ``private`` gives the container its
# own IPC namespace (so it cannot reach the host's), *and* it is the only mode
# other than ``host`` that honours ``--shm-size``.  ``none``, by contrast,
# mounts no shared memory at all, which would silently discard the enlargement
# that VA-API surface hand-off depends on.
DEFAULT_IPC_MODE = 'private'

# A short encode is still allowed to be larger than this; the value is a floor
# for RLIMIT_FSIZE, not a cap on typical output.  It is scaled by the input
# size by the caller.
MIN_OUTPUT_LIMIT = 8 * 1024 * 1024 * 1024

# Variables that let a child process write reports or load attacker-influenced
# code.  They are removed even though the local environment is otherwise an
# allow-list, so a future allow-list edit cannot reintroduce them.
DANGEROUS_ENV = frozenset({
    'FFREPORT',
    'LD_PRELOAD',
    'LD_LIBRARY_PATH',
    'LD_AUDIT',
    'DYLD_INSERT_LIBRARIES',
})

# Environment needed by ffmpeg, its VA-API loader, and the shell utilities
# (nice/ionice) that may prefix the command.  HOME is intentionally retained:
# fontconfig uses it for user fonts, and changing it would alter subtitle
# rendering.  Docker CLI credentials also live under HOME.
SAFE_ENV_KEYS = (
    'PATH',
    'HOME',
    'LANG',
    'LANGUAGE',
    'LC_ALL',
    'LC_CTYPE',
    'TZ',
    'TERM',
    'XDG_RUNTIME_DIR',
    'XDG_DATA_HOME',
    'XDG_DATA_DIRS',
    'LIBVA_DRIVER_NAME',
    'LIBVA_DRIVERS_PATH',
    'DRI_PRIME',
    'MESA_LOADER_DRIVER_OVERRIDE',
    'VK_ICD_FILENAMES',
    'GALLIUM_DRIVER',
)


@dataclass
class CappedResult:
    """Result of :func:`run_capped`."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    truncated: bool = False


def harden_container_args(use_acceleration: bool = False,
                          pids_limit: int = DEFAULT_PIDS_LIMIT,
                          shm_size: str = DEFAULT_SHM_SIZE,
                          memory_mb: Optional[int] = None,
                          ipc_mode: str = DEFAULT_IPC_MODE) -> list:
    """Return the isolation flags shared by every container invocation.

    ``--read-only`` needs writable scratch space, so ``/tmp`` and ``/run`` are
    overlaid with noexec tmpfs mounts and ``/dev/shm`` is enlarged with
    ``--shm-size``.  VA-API historically uses shared memory for buffer
    hand-off; the default 64 MB is too small for 4K surfaces.

    ``use_acceleration`` records the caller's intent but adds nothing here: the
    render node is exposed with ``--device`` by the caller, which is the only
    place that knows the node path.

    The IPC namespace must therefore stay ``private`` rather than ``none``:
    ``none`` removes the shared-memory segment outright, so the enlarged
    ``--shm-size`` would be ignored and hardware encoding of large frames
    could fail.  ``private`` is just as isolated from the host as ``none``.
    """
    args = [
        '--network', 'none',
        '--ipc', ipc_mode,
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '--pids-limit', str(max(64, int(pids_limit))),
        '--shm-size', str(shm_size),
        '--read-only',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev',
        '--tmpfs', '/run:rw,noexec,nosuid,nodev',
    ]
    if memory_mb:
        args.extend(['--memory', f'{int(memory_mb)}m'])
    return args


def local_env(tmpdir: Optional[str] = None,
              overrides: Optional[dict] = None) -> dict:
    """Return a scrubbed environment for a locally executed ffmpeg/ffprobe."""
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    env.setdefault('PATH', os.defpath)
    if tmpdir:
        env['TMPDIR'] = tmpdir
    for key in DANGEROUS_ENV:
        env.pop(key, None)
    if overrides:
        env.update(overrides)
    return env


def _limits_helper_path() -> Optional[str]:
    """Return the path to the standalone rlimit helper, if available."""
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'exec_limits.py')
    return candidate if os.path.isfile(candidate) else None


def apply_limits(argv: Sequence[str], fsize: Optional[int] = None,
                 address_space: Optional[int] = None) -> list:
    """Prefix ``argv`` with the rlimit helper.

    The helper is a separate executable because ``preexec_fn`` is documented as
    unsafe in a multithreaded process, which rmbloat always is.  The helper
    ``exec``s the real command, so it does not add a persistent process.
    """
    helper = _limits_helper_path()
    executable = sys.executable
    if not helper or not executable:
        return list(argv)

    wrapper = [executable, helper, '--core', '0']
    if fsize:
        wrapper.extend(['--fsize', str(int(fsize))])
    if address_space:
        wrapper.extend(['--as', str(int(address_space))])
    wrapper.append('--')
    return wrapper + list(argv)


@lru_cache(maxsize=1)
def _bwrap_works() -> bool:
    """Return True only when bubblewrap can actually create a namespace."""
    if not shutil.which('bwrap'):
        return False
    try:
        result = subprocess.run(
            ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
             '--', '/bin/true'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def detect_local_sandbox(preference: str = 'auto') -> str:
    """Resolve a local sandbox strategy to ``bwrap``, ``systemd``, or ``none``.

    ``auto`` deliberately does not select systemd-run: its stdio and scope
    semantics vary between releases, and a failed wrapper would break every
    encode.  Users who want it can request it explicitly.
    """
    if preference not in SANDBOX_CHOICES:
        preference = 'auto'
    if preference in ('none', 'container'):
        return 'none'
    if preference == 'bwrap':
        return 'bwrap' if _bwrap_works() else 'none'
    if preference == 'systemd':
        has_user_systemd = bool(os.environ.get('XDG_RUNTIME_DIR'))
        return 'systemd' if shutil.which('systemd-run') and has_user_systemd else 'none'
    return 'bwrap' if _bwrap_works() else 'none'


def wrap_local(argv: Sequence[str], input_dirs: Sequence[str] = (),
               outdir: Optional[str] = None, workdir: Optional[str] = None,
               allow_gpu: bool = False, render_device: Optional[str] = None,
               strategy: str = 'none') -> list:
    """Wrap a local ffmpeg/ffprobe argv in an optional sandbox.

    bubblewrap gets a read-only view of the whole filesystem (so shared
    libraries and fonts resolve) with only ``outdir`` writable.  The media
    directory is therefore readable but not writable, and the network is
    unshared.  systemd-run gets the equivalent properties.
    """
    argv = list(argv)
    if strategy == 'bwrap' and _bwrap_works():
        wrapped = [
            'bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/', '/',
            '--dev', '/dev',
            '--proc', '/proc',
            '--tmpfs', '/tmp',
        ]
        if outdir:
            wrapped.extend(['--bind', outdir, outdir])
        if allow_gpu and render_device:
            dri_dir = os.path.dirname(render_device)
            if dri_dir:
                wrapped.extend(['--dev-bind', dri_dir, dri_dir])
        if workdir:
            wrapped.extend(['--chdir', workdir])
        wrapped.append('--')
        return wrapped + argv

    if strategy == 'systemd':
        wrapped = [
            'systemd-run', '--user', '--scope', '--quiet', '--collect', '--pipe',
            '-p', 'PrivateNetwork=yes',
            '-p', 'ProtectSystem=strict',
            '-p', 'ProtectHome=read-only',
            '-p', 'NoNewPrivileges=yes',
        ]
        for path in input_dirs:
            wrapped.extend(['-p', f'ReadOnlyPaths={path}'])
        if outdir:
            wrapped.extend(['-p', f'BindPaths={outdir}'])
        if allow_gpu and render_device:
            # DeviceAllow takes a space-separated "<path> <access>" value.
            wrapped.extend(['-p', f'DeviceAllow={render_device} rw'])
        if workdir:
            wrapped.extend(['-p', f'WorkingDirectory={workdir}'])
        wrapped.append('--')
        return wrapped + argv

    return argv


def terminate_process_group(process, grace: float = 2.0) -> None:
    """Terminate ``process`` and every descendant, then reap it.

    A plain ``terminate()``/``kill()`` only signals the direct child, which
    leaves ffmpeg alive when the child is a bwrap/systemd/docker wrapper.
    """
    if process is None or process.poll() is not None:
        return

    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _signal(sig):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            process.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    _signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal(signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def create_private_work_dir(parent: str) -> Optional[str]:
    """Create a 0700 work directory next to the media, or return ``None``.

    Keeping it on the media's filesystem preserves the atomic same-filesystem
    install done by ``_execute_file_swap``; a cache-directory temp would turn
    the final install into a copy.
    """
    try:
        path = tempfile.mkdtemp(prefix='.rmbloat-tmp-', dir=parent)
        os.chmod(path, 0o700)
        return path
    except OSError:
        return None


def remove_private_work_dir(path: Optional[str]) -> None:
    """Remove ``path`` only when it is empty (never delete an output)."""
    if not path:
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


def _drain(stream, chunks: list, limit: int,
           overflow: Optional[threading.Event] = None) -> None:
    """Read ``stream`` to EOF, retaining at most ``limit`` bytes.

    The stream is still drained after the limit so the child cannot block on a
    full pipe; the overflow event lets the caller kill it promptly.
    """
    total = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if total < limit:
                chunks.append(chunk[:limit - total])
            elif overflow is not None:
                overflow.set()
            total += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_capped(argv: Sequence[str], env: Optional[dict] = None,
               cwd: Optional[str] = None, timeout: float = 30.0,
               stdout_limit: int = PROBE_MAX_STDOUT,
               stderr_limit: int = PROBE_MAX_STDERR) -> CappedResult:
    """Run a short-lived command with bounded output and group termination.

    Used for ffprobe.  A crafted file can emit unbounded JSON or stderr, so
    stdout is read only up to ``stdout_limit``; exceeding it kills the whole
    process group instead of waiting for the timeout.  stderr is drained in a
    background thread so the child cannot block on a full stderr pipe.
    """
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )

    stdout_chunks = []
    stderr_chunks = []
    overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_drain,
        args=(process.stdout, stdout_chunks, stdout_limit, overflow),
        daemon=True)
    stderr_thread = threading.Thread(
        target=_drain, args=(process.stderr, stderr_chunks, stderr_limit),
        daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    truncated = False
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                truncated = True
                terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_process_group(process)
                break
            time.sleep(0.05)
        if overflow.is_set():
            truncated = True
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
    finally:
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    return CappedResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=b''.join(stdout_chunks),
        stderr=b''.join(stderr_chunks),
        timed_out=timed_out,
        truncated=truncated,
    )
