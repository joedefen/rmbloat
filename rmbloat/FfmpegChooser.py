#!/usr/bin/env python3
"""
FfmpegChooser - Intelligent FFmpeg runtime selection with hardware acceleration support
"""
# pylint: disable=too-many-branches,too-many-statements,too-many-locals,invalid-name
# pylint: disable=too-many-instance-attributes,broad-exception-caught,line-too-long

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from . import Sandbox


class FfmpegChooser:
    """
    Detects and configures the best available FFmpeg runtime.

    Priority order:
    1. System ffmpeg with working hardware acceleration
    2. System ffmpeg without hardware acceleration (if system ffmpeg exists)
    3. Docker/Podman with hardware acceleration (only if system ffmpeg doesn't exist)
    4. Docker/Podman without hardware acceleration (last resort)
    """

    # Strategy options in order of preference (after 'auto')
    STRATEGIES = ['auto', 'system_accel', 'system_cpu', 'docker_accel', 'docker_cpu']

    # Timeouts (seconds)
    DOCKER_INFO_TIMEOUT = 5
    ACCEL_TEST_TIMEOUT = 30
    VAAPI_TEST_TIMEOUT = 10
    IMAGE_PULL_TIMEOUT = 300
    IMAGE_INSPECT_TIMEOUT = 5

    # Safe subtitle codecs that can be stored in MKV containers
    SAFE_SUBTITLE_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}

    def __init__(self, force_pull: bool = False, image: str = "joedefen/ffmpeg-vaapi-docker:latest",
                 prefer_strategy: str = 'auto', quiet: bool = False,
                 sandbox: str = 'auto') -> None:
        """
        Initialize and detect the best FFmpeg configuration.

        Args:
            force_pull: Pull the Docker image even if it already exists.  This
                is the only path that performs a network pull; normal startup
                uses a locally available image and never pulls implicitly.
            image: Docker image to use (default: joedefen/ffmpeg-vaapi-docker:latest).
                Tags are resolved to an immutable content digest before use.
            prefer_strategy: Strategy preference - 'auto', 'docker_accel', 'docker_cpu',
                           'system_cpu', or 'system_accel' (default: 'auto')
            quiet: Suppress detection output (default: False)
        """
        self.image = image
        self.image_ref = None  # Resolved immutable digest/ID for container runs
        self.runtime = None  # 'docker', 'podman', or None
        self.has_docker_acceleration = False
        self.has_system_acceleration = False
        self.use_docker = False
        self.use_acceleration = False
        self.system_ffmpeg_path = None
        self.render_device = None
        self.vaapi_supports_10bit = False  # Will be detected during init
        self.prefer_strategy = prefer_strategy
        self.quiet = quiet
        self.sandbox_preference = (sandbox if sandbox in Sandbox.SANDBOX_CHOICES
                                   else 'auto')
        self.sandbox_mode = 'none'
        self.strategy = None  # Will be set to the chosen strategy

        if not quiet:
            print("Detecting FFmpeg configuration...")

        # Check system ffmpeg first
        self._detect_system_ffmpeg()

        # Detect container runtime
        self._detect_runtime()

        # If we have a runtime, ensure image and test acceleration
        if self.runtime:
            self._ensure_image(force_pull)
            self._test_docker_acceleration()

        # Decide final strategy
        self._decide_strategy()
        self.sandbox_mode = self._resolve_sandbox_mode()

        # Print summary (handles quiet mode internally)
        self._print_summary()

    def _run_command(self, cmd: List[str], timeout: int = 5,
                     text_output: bool = False) -> Optional[subprocess.CompletedProcess]:
        """
        Run a command with consistent error handling.

        Args:
            cmd: Command and arguments as a list
            timeout: Timeout in seconds
            text_output: If True, decode stdout/stderr as text

        Returns:
            CompletedProcess object or None if timeout/error occurred
        """
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=text_output,
                check=False
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            # A missing executable, a permission error (EACCES), or any other
            # OS-level failure (ENOMEM, EMFILE, ...) must be treated like an
            # unusable command, not allowed to abort startup detection.
            if not self.quiet:
                command = cmd[0] if cmd else 'command'
                print(f"  ✗ Could not run {command}: {exc}")
            return None

    def _resolve_sandbox_mode(self) -> str:
        """Return the isolation actually available for the chosen strategy.

        Container runs are isolated by the runtime flags.  Local runs use
        bubblewrap when it is present and functional; an explicit request for
        an unavailable strategy degrades to ``none`` with a visible warning.
        """
        if self.use_docker:
            if self.sandbox_preference in ('bwrap', 'systemd', 'none'):
                print(f"WARNING: --sandbox {self.sandbox_preference} does not apply "
                      f"to the container runtime; using container isolation")
            return 'container'

        preference = self.sandbox_preference
        if preference == 'container':
            print("WARNING: no container runtime available; "
                  "falling back to local sandbox detection")
            preference = 'auto'

        mode = Sandbox.detect_local_sandbox(preference)
        if preference in ('bwrap', 'systemd') and mode == 'none':
            print(f"WARNING: requested sandbox '{preference}' is not available; "
                  "media decoders will run with full user privileges")
        return mode

    def describe_sandbox(self) -> str:
        """Return a one-line description for the startup summary."""
        if self.sandbox_mode == 'container':
            runtime = self.runtime or 'container'
            return f"{runtime} (network/caps/PIDs/read-only hardening)"
        if self.sandbox_mode == 'bwrap':
            return "bubblewrap (no network, read-only root, private /tmp)"
        if self.sandbox_mode == 'systemd':
            return "systemd-run (private network, strict filesystem)"
        return "none (decoders run with full user privileges)"

    def child_env(self, tmpdir: Optional[str] = None) -> Optional[dict]:
        """Return the environment for a child process.

        The docker/podman CLI needs the caller's environment (registry auth,
        DOCKER_HOST, runtime dir), while a local ffmpeg must not inherit
        ``FFREPORT`` or ``LD_PRELOAD``.  Return ``None`` for the container path
        so ``subprocess`` inherits normally.
        """
        if self.use_docker:
            return None
        return Sandbox.local_env(tmpdir=tmpdir)

    def _container_hardening(self, use_acceleration: bool = False) -> List[str]:
        """Return the shared isolation flags placed after ``run --rm``."""
        return Sandbox.harden_container_args(use_acceleration=use_acceleration)

    def _detect_system_ffmpeg(self) -> None:
        """Detect if system ffmpeg exists and test hardware acceleration."""
        self.system_ffmpeg_path = shutil.which('ffmpeg')

        if not self.system_ffmpeg_path:
            if not self.quiet:
                print("  ✗ System ffmpeg not found")
            return

        if not self.quiet:
            print(f"  ✓ System ffmpeg found: {self.system_ffmpeg_path}")

        # Test hardware acceleration
        self.has_system_acceleration = self._test_system_acceleration()
        if not self.quiet:
            if self.has_system_acceleration:
                print("  ✓ System ffmpeg has working hardware acceleration")
            else:
                print("  ✗ System ffmpeg hardware acceleration not available")

        # Detect 10-bit VAAPI support (only if acceleration is available)
        if self.has_system_acceleration:
            self.vaapi_supports_10bit = self._detect_vaapi_10bit_support()

    def _test_system_acceleration(self) -> bool:
        """Test if system ffmpeg can use hardware acceleration."""
        # First check if /dev/dri exists
        if not Path("/dev/dri").exists():
            return False

        # Find render device
        render_device = self._find_render_device()
        if not render_device:
            return False

        # Test HEVC encoding with hardware acceleration
        test_cmd = [
            'ffmpeg',
            '-y',
            '-init_hw_device', f'vaapi=va:{render_device}',
            '-filter_hw_device', 'va',
            '-f', 'lavfi', '-i', 'nullsrc=s=128x128:d=1',
            '-vf', 'format=nv12,hwupload',
            '-c:v', 'hevc_vaapi',
            '-frames:v', '1',
            '-f', 'null', '-'
        ]

        result = self._run_command(test_cmd, timeout=self.VAAPI_TEST_TIMEOUT)
        if result and result.returncode == 0:
            self.render_device = render_device
            return True

        return False

    def _vaapi_10bit_test_args(self, render_device: str) -> List[str]:
        """Return the ffmpeg arguments for a short 10-bit VAAPI test encode."""
        return [
            '-y',
            '-init_hw_device', f'vaapi=va:{render_device}',
            '-filter_hw_device', 'va',
            '-f', 'lavfi', '-i', 'nullsrc=s=128x128:d=1',
            '-vf', 'format=p010le,hwupload',
            '-c:v', 'hevc_vaapi',
            '-profile:v', 'main10',
            '-frames:v', '1',
            '-f', 'null', '-'
        ]

    def _detect_vaapi_10bit_support(self, container: bool = False) -> bool:
        """
        Detect if VAAPI supports 10-bit HEVC encoding.

        Args:
            container: When True, run the fallback encode through the
                configured container runtime using the same user/group/device
                arguments as a real encode. When False, test the host ffmpeg.

        Returns:
            bool: True if VAAPI supports main10 profile
        """
        if not self.render_device:
            return False

        # vainfo is only used on the host. The container image may not ship
        # the tool, and its view of the device is exactly what the fallback
        # encode below already verifies.
        if not container and shutil.which('vainfo'):
            result = self._run_command(
                ['vainfo', '--display', 'drm', '--device', self.render_device],
                timeout=self.DOCKER_INFO_TIMEOUT,
                text_output=True
            )
            if result and result.returncode == 0:
                output = result.stdout
                # Look for HEVC Main10 profile
                # Example lines:
                #   VAProfileHEVCMain10             : VAEntrypointVLD
                #   VAProfileHEVCMain10             : VAEntrypointEncSlice
                if 'VAProfileHEVCMain10' in output and 'VAEntrypointEncSlice' in output:
                    if not self.quiet:
                        print("  ✓ VAAPI supports 10-bit HEVC encoding")
                    return True

        # Fallback: test encode with 10-bit
        # This is slower but works if vainfo isn't available
        args = self._vaapi_10bit_test_args(self.render_device)

        if container:
            if not self.runtime:
                return False
            test_cmd = [
                self.runtime, 'run', '--rm',
                *self._container_hardening(use_acceleration=True),
                '--device=/dev/dri:/dev/dri',
                *self._docker_user_and_group_args(self.render_device),
                self._image_for_run(),
                *args,
            ]
        else:
            test_cmd = ['ffmpeg', *args]

        result = self._run_command(test_cmd, timeout=self.VAAPI_TEST_TIMEOUT, text_output=True)
        if result:
            if result.returncode == 0:
                if not self.quiet:
                    print("  ✓ VAAPI supports 10-bit HEVC encoding (verified by test)")
                return True
            # Check if error is specifically about profile
            if 'No usable encoding profile found' in result.stderr or 'profile' in result.stderr.lower():
                if not self.quiet:
                    print("  ⚠ VAAPI does not support 10-bit HEVC encoding (will use 8-bit)")

        return False

    def _detect_runtime(self) -> None:
        """Detect if docker or podman is available."""
        # Try docker first
        if shutil.which('docker'):
            result = self._run_command(['docker', 'info'], timeout=self.DOCKER_INFO_TIMEOUT)
            if result and result.returncode == 0:
                self.runtime = 'docker'
                if not self.quiet:
                    print("  ✓ Docker detected and running")
                return

        # Try podman
        if shutil.which('podman'):
            result = self._run_command(['podman', 'info'], timeout=self.DOCKER_INFO_TIMEOUT)
            if result and result.returncode == 0:
                self.runtime = 'podman'
                if not self.quiet:
                    print("  ✓ Podman detected and running")
                return

        if not self.quiet:
            print("  ✗ Neither Docker nor Podman found")
        self.runtime = None

    def _ensure_image(self, force_pull: bool) -> None:
        """Ensure the image exists locally and resolve an immutable reference.

        Normal startup never pulls: pulling is a network/supply-chain side
        effect, so it only happens when the caller explicitly requests it
        (``force_pull``, e.g. ``--chooser-tests``).  Once the image is present,
        its content digest (or image ID as a fallback) is recorded in
        ``self.image_ref`` and used for every probe/encode, so a mutable tag
        cannot change underneath a run.
        """
        if not self.runtime:
            return

        # Check if image exists locally
        check_cmd = [self.runtime, 'image', 'inspect', self.image]
        result = self._run_command(check_cmd, timeout=self.IMAGE_INSPECT_TIMEOUT)
        image_exists = result is not None and result.returncode == 0

        if not image_exists and not force_pull:
            if not self.quiet:
                print(f"  ✗ Image {self.image} is not available locally.")
                print(f"    Pull it explicitly with: {self.runtime} pull {self.image}")
            self.runtime = None
            return

        # Pull only on an explicit request (or when the caller forced a pull)
        if force_pull or not image_exists:
            if not self.quiet:
                if force_pull:
                    print(f"  → Force pulling {self.image}...")
                else:
                    print(f"  → Pulling {self.image}...")

            pull_cmd = [self.runtime, 'pull', self.image]
            result = self._run_command(pull_cmd, timeout=self.IMAGE_PULL_TIMEOUT)
            if result and result.returncode == 0:
                if not self.quiet:
                    print(f"  ✓ Image {self.image} ready")
            else:
                if not self.quiet:
                    print(f"  ✗ Failed to pull image {self.image}")
                self.runtime = None
                return
        elif not self.quiet:
            print(f"  ✓ Image {self.image} already available locally")

        self.image_ref = self._resolve_pinned_image()

    def _resolve_pinned_image(self) -> str:
        """Return an immutable image reference for container runs.

        Prefers the registry content digest (``repo@sha256:...``) and falls back
        to the local image ID, which is also content-addressed.  If neither can
        be read, the original tag is returned with a warning rather than
        silently pretending it is pinned.
        """
        if not self.runtime:
            return self.image

        digest_cmd = [
            self.runtime, 'image', 'inspect', '--format',
            '{{index .RepoDigests 0}}', self.image,
        ]
        result = self._run_command(
            digest_cmd, timeout=self.IMAGE_INSPECT_TIMEOUT, text_output=True)
        if result and result.returncode == 0 and result.stdout:
            digest_ref = result.stdout.strip()
            if '@sha256:' in digest_ref:
                return digest_ref

        id_cmd = [
            self.runtime, 'image', 'inspect', '--format',
            '{{.Id}}', self.image,
        ]
        result = self._run_command(
            id_cmd, timeout=self.IMAGE_INSPECT_TIMEOUT, text_output=True)
        if result and result.returncode == 0 and result.stdout:
            image_id = result.stdout.strip()
            if image_id.startswith('sha256:'):
                return image_id

        if not self.quiet:
            print(f"  ⚠ Could not resolve an immutable reference for {self.image}")
        return self.image

    def _image_for_run(self) -> str:
        """Return the pinned container image reference for a run/probe."""
        return getattr(self, 'image_ref', None) or self.image

    def _find_render_device(self) -> Optional[str]:
        """Find the first available render device."""
        dri_path = Path("/dev/dri")
        if not dri_path.exists():
            return None

        # Look for renderD128, renderD129, etc.
        for device in sorted(dri_path.glob("renderD*")):
            return str(device)

        return None

    def _docker_gpu_groups(self, render_device: Optional[str] = None) -> List[str]:
        """
        Return numeric group ids that grant access to /dev/dri.

        ``docker run`` executes the container process as ``uid:gid`` and does
        not inherit the caller's supplementary groups. The render (and often
        video) group must therefore be re-added explicitly with
        ``--group-add``; otherwise a probe that ran as root can pass while a
        real encode running as the invoking user fails with permission errors.
        """
        gids = set()

        try:
            import grp  # pylint: disable=import-outside-toplevel
        except ImportError:  # pragma: no cover - non-Unix platforms
            grp = None

        if grp is not None:
            for name in ('render', 'video'):
                try:
                    gids.add(grp.getgrnam(name).gr_gid)
                except KeyError:
                    pass

        # The group that owns the actual render node is authoritative even if
        # the conventional names above are missing or remapped.
        if render_device:
            try:
                gids.add(os.stat(render_device).st_gid)
            except OSError:
                pass

        return [str(gid) for gid in sorted(gids)]

    def _docker_user_and_group_args(self, render_device: Optional[str] = None) -> List[str]:
        """
        Build the container identity arguments shared by probes and encodes.

        Keeping a single helper ensures the acceleration/10-bit probes run
        with exactly the same user and supplementary GPU groups as real
        encodes, so a successful probe implies a working encode.
        """
        args = ['--user', f'{os.getuid()}:{os.getgid()}']
        for gid in self._docker_gpu_groups(render_device):
            args.extend(['--group-add', gid])
        return args

    def _docker_accel_will_be_used(self) -> bool:
        """
        Predict whether strategy selection will end on ``docker_accel``.

        Used to avoid paying for the extra container 10-bit probe when a
        system strategy (or CPU-only Docker) will win instead.
        """
        if not self.has_docker_acceleration:
            return False

        if self.prefer_strategy == 'docker_accel':
            return True
        if self.prefer_strategy == 'system_accel' and self.has_system_acceleration:
            return False
        if self.prefer_strategy == 'system_cpu' and self.system_ffmpeg_path:
            return False
        if self.prefer_strategy == 'docker_cpu':
            return False

        # 'auto', or an unavailable preference that falls back to auto: the
        # system ffmpeg is preferred whenever it exists (with or without
        # acceleration), so docker_accel only wins when there is no system
        # ffmpeg at all.
        return not self.system_ffmpeg_path

    def _test_docker_acceleration(self) -> None:
        """Test if Docker/Podman can use hardware acceleration."""
        if not self.runtime:
            return

        # Check if /dev/dri exists
        if not Path("/dev/dri").exists():
            if not self.quiet:
                print("  ✗ /dev/dri not found - hardware acceleration unavailable")
            return

        # Find render device
        render_device = self._find_render_device()
        if not render_device:
            if not self.quiet:
                print("  ✗ No render device found in /dev/dri")
            return

        if not self.quiet:
            print(f"  → Testing hardware acceleration with {render_device}...")

        # Test command. It runs with the same user and supplementary GPU
        # groups as real encodes; running it as root without --user previously
        # let the probe pass while real encodes failed on device permissions.
        test_cmd = [
            self.runtime, 'run', '--rm',
            *self._container_hardening(use_acceleration=True),
            '--device=/dev/dri:/dev/dri',
            *self._docker_user_and_group_args(render_device),
            self._image_for_run(),
            '-y',
            '-init_hw_device', f'vaapi=va:{render_device}',
            '-filter_hw_device', 'va',
            '-f', 'lavfi', '-i', 'nullsrc=s=128x128:d=1',
            '-vf', 'format=nv12,hwupload',
            '-c:v', 'hevc_vaapi',
            '-frames:v', '1',
            '-f', 'null', '-'
        ]

        result = self._run_command(test_cmd, timeout=self.ACCEL_TEST_TIMEOUT)
        if result:
            if result.returncode == 0:
                self.has_docker_acceleration = True
                self.render_device = render_device
                if not self.quiet:
                    print(f"  ✓ Hardware acceleration working in {self.runtime}")

                # Detect 10-bit support with the same container identity a
                # real encode will use. Skip the extra probe when the system
                # path already established it and docker will not be chosen.
                if self._docker_accel_will_be_used():
                    self.vaapi_supports_10bit = self._detect_vaapi_10bit_support(
                        container=True)
            else:
                if not self.quiet:
                    print(f"  ✗ Hardware acceleration test failed in {self.runtime}")
        else:
            if not self.quiet:
                print("  ✗ Hardware acceleration test timed out")

    def _decide_strategy(self) -> None:
        """Decide which FFmpeg to use based on available options and preference."""

        # If user specified a preference, try to honor it
        if self.prefer_strategy != 'auto':
            if self._try_strategy(self.prefer_strategy):
                return

            # If preferred strategy not available, fall through to auto
            # Always warn about this, even in quiet mode
            print(f"WARNING: Preferred strategy '{self.prefer_strategy}' not available, falling back to auto selection")

        # Auto priority (based on real-world performance and availability)
        # If system ffmpeg exists, prefer it (with or without acceleration)
        if self.system_ffmpeg_path:
            # Priority 1: System with acceleration (fastest - no container overhead)
            if self._try_strategy('system_accel'):
                return

            # Priority 2: System without acceleration (system ffmpeg exists, use it)
            if self._try_strategy('system_cpu'):
                return

        # Only fall back to Docker if system ffmpeg doesn't exist
        # Priority 3: Docker with acceleration (system ffmpeg not available)
        if self._try_strategy('docker_accel'):
            return

        # Priority 4: Docker without acceleration (last resort)
        if self._try_strategy('docker_cpu'):
            return

        # No viable option
        raise RuntimeError(
            "No usable FFmpeg found. Please install either:\n"
            "  - Docker/Podman with ffmpeg-vaapi-docker image, or\n"
            "  - System ffmpeg package"
        )

    def _try_strategy(self, strategy: str) -> bool:
        """
        Try to enable a specific strategy.

        Returns True if successful, False if not available.
        """
        if strategy == 'docker_accel':
            if self.runtime and self.has_docker_acceleration:
                self.use_docker = True
                self.use_acceleration = True
                self.strategy = 'docker_accel'
                return True

        elif strategy == 'docker_cpu':
            if self.runtime:
                self.use_docker = True
                self.use_acceleration = False
                self.strategy = 'docker_cpu'
                return True

        elif strategy == 'system_cpu':
            if self.system_ffmpeg_path:
                self.use_docker = False
                self.use_acceleration = False
                self.strategy = 'system_cpu'
                return True

        elif strategy == 'system_accel':
            if self.has_system_acceleration:
                self.use_docker = False
                self.use_acceleration = True
                self.strategy = 'system_accel'
                return True

        return False

    def _print_summary(self) -> None:
        """Print a summary of the chosen configuration."""
        # Quiet mode: show minimal or no output, but never hide the fact that
        # decoders are running without any isolation.
        if self.quiet:
            if self.prefer_strategy != 'auto':
                # User selected a specific strategy - show validation
                print(f"Strategy: {self.strategy} (selected and validated)")
            if self.sandbox_mode == 'none':
                print("WARNING: sandbox: none - media decoders run with full "
                      "user privileges", file=sys.stderr)
            # else: completely silent for auto detection in quiet mode
            return

        # Full summary for verbose mode
        print("\n" + "="*60)
        print("FFmpeg Configuration Summary")
        print("="*60)

        if self.use_docker:
            print(f"Runtime:      {self.runtime.capitalize()} ({self.image})")
        else:
            print(f"Runtime:      System FFmpeg ({self.system_ffmpeg_path})")

        if self.use_acceleration:
            print(f"Acceleration: Enabled (VA-API via {self.render_device})")
        else:
            print("Acceleration: Disabled (CPU encoding)")

        print(f"Strategy:     {self.strategy}")
        print(f"Sandbox:      {self.describe_sandbox()}")
        print("="*60 + "\n")


    def make_namespace(self, validate_input_exists: bool = True,
                       **overrides) -> SimpleNamespace:
        """
        Create a namespace with sensible defaults for FFmpeg encoding.

        Required arguments (must be provided):
            input_file: Input video file path
            output_file: Output video file path

        Args:
            validate_input_exists: When True (the default), raise
                FileNotFoundError if input_file does not exist. Set to False
                when building an illustrative namespace (e.g. to print an
                example command) where the path is a placeholder.

        Optional arguments with defaults:
            crf: Quality (default: 28, lower=better quality)
            preset: Encoding preset (default: 'medium')
            codec: Target codec (default: 'hevc')
            use_10bit: Use 10-bit encoding (default: True)
            color_opts: Color space options (default: [])
            subtitle_codec: Subtitle codec (default: 'copy')
            thread_count: Thread limit, 0=auto (default: 0)
            use_nice_ionice: Use nice/ionice for low priority (default: True)
            pre_input_opts: Options before -i (default: [])
            post_input_opts: Options after -i (default: [])
            error_tolerant: Add flags for corrupt/problematic videos (default: False)
            streams: List of stream info dicts for subtitle filtering (default: [])

        Returns:
            SimpleNamespace with all parameters

        Example:
            params = chooser.make_namespace(
                input_file='input.mp4',
                output_file='output.mkv',
                crf=28,
                color_opts=['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709']
            )
        """
        defaults = SimpleNamespace(
            # Required - must provide
            input_file=None,
            output_file=None,

            # Quality/encoding
            crf=28,
            preset='medium',
            codec='hevc',
            use_10bit=True,

            # Filtering/processing
            color_opts=[],
            external_subtitle=None,  # Path to external .srt file to merge
            subtitle_codec='copy',  # Subtitle codec: 'copy', 'srt', 'ass', etc.

            # Threading
            thread_count=0,  # 0 = auto

            # Priority
            use_nice_ionice=True,

            # Pre/post input opts
            pre_input_opts=[],
            post_input_opts=[],

            # Error handling
            error_tolerant=False,

            # Stream information (for subtitle filtering)
            streams=[],

            # Private per-job output directory (created by JobHandler).  The
            # container mounts only this directory read-write.
            temp_dir=None,
        )

        # Apply overrides
        for key, value in overrides.items():
            if not hasattr(defaults, key):
                raise ValueError(f"Unknown parameter: {key}")
            setattr(defaults, key, value)

        # Validate required fields with better error messages
        if defaults.input_file is None:
            raise ValueError("input_file is required but was not provided")
        if validate_input_exists and not Path(defaults.input_file).exists():
            raise FileNotFoundError(f"input_file does not exist: {defaults.input_file}")

        if defaults.output_file is None:
            raise ValueError("output_file is required but was not provided")

        return defaults

    def _get_target_quality(self, base_crf: int, acceleration: bool = True,
                            height: Optional[int] = None) -> int:
        """
        Transforms a base CRF value into a target Quality (QP or CRF)
        based on resolution and encoding method.
        """
        target = base_crf

        # 1. Resolution Adjustment
        # Balance quality across resolutions: boost SD, allow savings for 4K+
        if height:
            if height < 480:     # 352p etc - significant boost
                target -= 4
            elif height < 720:   # 480p/576p - moderate boost
                target -= 2
            elif height >= 2160: # 4K (2160p) and above - allow higher compression
                target += 2
            # 720p-1080p: no adjustment (baseline)

        # 2. Hardware Mapping (The "VA-API Tax")
        # QP 30 != CRF 28. Hardware needs a lower QP to match software quality.
        if acceleration:
            # Using your existing logic but shifting based on our new target
            mapping = {
                14: 16, 15: 17, 16: 18, 17: 19, 18: 20,
                19: 21, 20: 22, 21: 23, 22: 24, 23: 25,
                24: 26, 25: 27, 26: 28, 27: 29, 28: 30,
                29: 31, 30: 32, 31: 33, 32: 34
            }
            # Get mapped value or fallback to +2 offset
            target = mapping.get(target, target + 2)

        return min(51, max(0, target))

    def _map_preset(self, preset: str, for_hardware: bool) -> str:
        """
        Map preset names between software and hardware encoders.

        Hardware encoders don't support ultrafast, superfast, or placebo.
        """
        if not for_hardware:
            return preset

        # Map unsupported presets to nearest supported one
        preset_map = {
            'ultrafast': 'veryfast',
            'superfast': 'veryfast',
            'placebo': 'veryslow',
        }

        return preset_map.get(preset, preset)


    def make_ffmpeg_cmd(self, params: SimpleNamespace) -> List[str]:
        """
        Build an ffmpeg command from the provided parameters.
        Consolidates mapping, scaling, and subtitle logic into a single source of truth.
        """
        cmd = []

        # 1. Global / Process Options
        if params.use_nice_ionice and not self.use_docker:
            cmd.extend(['ionice', '-c3', 'nice', '-n20'])

        input_path = Path(params.input_file).resolve()
        workdir = str(input_path.parent)
        input_basename = input_path.name

        # 2. Docker Wrapper logic
        temp_dir = getattr(params, 'temp_dir', None)
        if self.use_docker:
            # Mount the media read-only and make only the private per-job
            # output directory writable.  ``:z`` relabels the bind mount for
            # SELinux hosts; it is a no-op elsewhere.
            if temp_dir and os.path.abspath(temp_dir) != os.path.abspath(workdir):
                mounts = ['-v', f'{workdir}:{workdir}:ro,z',
                          '-v', f'{temp_dir}:{temp_dir}:z']
            else:
                # Fallback when a private work directory could not be made.
                mounts = ['-v', f'{workdir}:{workdir}:z']

            cmd.extend([
                self.runtime, 'run', '--rm',
                *self._container_hardening(
                    use_acceleration=self.use_acceleration),
                # Use the same identity/group arguments as the acceleration
                # probe so a passing probe predicts a working real encode.
                *self._docker_user_and_group_args(
                    self.render_device if self.use_acceleration else None),
                *mounts,
                '-w', workdir,
            ])
            if self.use_acceleration:
                cmd.extend(['--device=/dev/dri:/dev/dri'])

            cmd.append(self._image_for_run())

            if params.use_nice_ionice:
                cmd.extend(['ionice', '-c3', 'nice', '-n20'])

        # 3. FFmpeg Base
        if not self.use_docker:
            cmd.append('ffmpeg')

        cmd.extend('-y -progress pipe:2'.split())
        # Do not read stdin and do not allow network protocols.  The whitelist
        # is repeated before the subtitle input because format options apply
        # to the next input only.
        cmd.extend(['-nostdin', '-protocol_whitelist', Sandbox.PROTOCOL_WHITELIST])

        # 4. Hardware Initialization (Must be before -i)
        if self.use_acceleration:
            cmd.extend([
                '-init_hw_device', f'vaapi=va:{self.render_device}',
                '-filter_hw_device', 'va'
            ])

        # 5. Input Files
        # Pre-input options (e.g., -ss for seeking before decoding)
        if params.pre_input_opts:
            cmd.extend(params.pre_input_opts)

        if params.error_tolerant:
            cmd.extend(['-err_detect', 'ignore_err', '-fflags', '+genpts'])

        cmd.extend(['-i', input_basename if self.use_docker else params.input_file])

        # Post-input options (e.g., -t for duration limiting)
        if params.post_input_opts:
            cmd.extend(params.post_input_opts)

        if params.external_subtitle:
            sub_path = Path(params.external_subtitle)
            cmd.extend(['-protocol_whitelist', Sandbox.PROTOCOL_WHITELIST,
                        '-i', sub_path.name if self.use_docker else str(sub_path)])

        # 6. Stream Mapping (The Core Logic)
        # Map Video (v:0) and Audio (a?), copy audio
        cmd.extend(['-map', '0:v:0', '-map', '0:a?', '-c:a', 'copy'])

        # Subtitle Logic per README: Keep Safe Text, Drop Unsafe Bitmap
        if params.external_subtitle:
            # Merge external: drop all internal subs, map the external file (input 1)
            cmd.extend(['-map', '-0:s', '-map', '1:s:0'])
            cmd.extend(['-c:s', params.subtitle_codec, '-metadata:s:s:0', 'language=eng'])
        else:
            # Internal cleanup: map all, then negative-map the unsafe ones
            cmd.extend(['-map', '0:s?'])
            subtitle_idx = 0
            for s in params.streams:
                # streams are stored as "type:codec" strings
                stream_type, stream_codec = s.split(':', 1) if ':' in s else (s, '')
                if stream_type == 'subtitle':
                    if stream_codec.lower() not in self.SAFE_SUBTITLE_CODECS:
                        cmd.extend(['-map', f'-0:s:{subtitle_idx}'])
                    subtitle_idx += 1

            # Copy remaining safe subs (or use specified codec)
            cmd.extend(['-c:s', params.subtitle_codec])

        # Drop "bloat" (attachments/data)
        cmd.extend(['-map', '-0:t', '-map', '-0:d'])

        # 7. Encoder, Quality & Scaling Logic
        source_height = getattr(params, 'height', None)
        quality_val = self._get_target_quality(
            base_crf=params.crf,
            acceleration=self.use_acceleration,
            height=source_height
        )

        filters = []
        # Check for target scaling (calculated in caller)
        if getattr(params, 'target_width', None):
            filters.append(f'scale={params.target_width}:-2')

        if self.use_acceleration:
            # Hardware Path (VAAPI)
            # Validate codec support for hardware encoding
            supported_hw_codecs = {'hevc': 'hevc_vaapi', 'h264': 'h264_vaapi', 'vp9': 'vp9_vaapi'}
            if params.codec not in supported_hw_codecs:
                raise ValueError(f"Unsupported hardware codec: {params.codec}. Supported: {list(supported_hw_codecs.keys())}")
            video_codec = supported_hw_codecs[params.codec]

            cmd.extend(['-c:v', video_codec, '-qp', str(quality_val)])

            # Handle 10-bit hardware vs 8-bit
            if params.use_10bit and self.vaapi_supports_10bit:
                filter_pix_fmt = 'p010le'
                cmd.extend(['-profile:v', 'main10'])
            else:
                filter_pix_fmt = 'nv12'

            # Build VAAPI filter chain
            filters.extend([f'format={filter_pix_fmt}', 'hwupload'])
            cmd.extend(['-vf', ','.join(filters)])
        else:
            # Software Path (CPU)
            # Validate codec and thread count
            if params.thread_count < 0 or params.thread_count > 256:
                raise ValueError(f"Invalid thread_count: {params.thread_count}. Must be 0-256.")

            supported_sw_codecs = {'hevc': 'libx265', 'h264': 'libx264'}
            if params.codec not in supported_sw_codecs:
                raise ValueError(f"Unsupported software codec: {params.codec}. Supported: {list(supported_sw_codecs.keys())}")

            video_codec = supported_sw_codecs[params.codec]
            if params.codec == 'hevc' and params.thread_count > 0:
                cmd.extend(['-x265-params', f'pools={params.thread_count}'])
            elif params.codec == 'h264' and params.thread_count > 0:
                cmd.extend(['-threads', str(params.thread_count)])

            cmd.extend(['-c:v', video_codec, '-crf', str(quality_val)])

            pix_fmt = 'yuv420p10le' if params.use_10bit else 'yuv420p'
            cmd.extend(['-pix_fmt', pix_fmt])

            if filters:
                cmd.extend(['-vf', ','.join(filters)])

        # 8. Final Touches
        preset = self._map_preset(params.preset, self.use_acceleration)
        cmd.extend(['-preset', preset])

        if params.color_opts:
            cmd.extend(params.color_opts)

        # 9. Output
        if self.use_docker:
            if temp_dir:
                # The private directory is a subdirectory of the mounted media
                # directory, so a relative path is stable inside the container.
                output_arg = os.path.relpath(params.output_file, workdir)
            else:
                output_arg = Path(params.output_file).name
        else:
            output_arg = params.output_file
        cmd.append(output_arg)

        # 10. Local sandbox and resource limits.  The output file is
        # pre-created by JobHandler, so ffmpeg truncates an existing inode
        # instead of following a link planted in the media directory.
        if not self.use_docker:
            try:
                input_size = os.path.getsize(params.input_file)
            except OSError:
                input_size = 0
            fsize = (max(Sandbox.MIN_OUTPUT_LIMIT, input_size * 4)
                     if input_size else None)
            cmd = Sandbox.apply_limits(cmd, fsize=fsize)
            cmd = Sandbox.wrap_local(
                cmd,
                input_dirs=[workdir],
                outdir=temp_dir or workdir,
                workdir=workdir,
                allow_gpu=self.use_acceleration,
                render_device=self.render_device,
                strategy=getattr(self, 'sandbox_mode', 'none'),
            )

        return cmd



    def real_world_tests(self, video_file: str, duration: int = 30,
                         output_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Test all viable encoding strategies with a real video file.

        Args:
            video_file: Path to test video file
            duration: Seconds to encode (default: 30)
            output_dir: Directory for test outputs (default: temp directory)

        Returns:
            dict: Results for each strategy tested
                {
                    'docker_accel': {'success': bool, 'time': float, 'size': int, 'error': str},
                    'docker_cpu': {...},
                    'system_cpu': {...},
                    'system_accel': {...}
                }
        """

        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix='ffmpeg_test_')
        else:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        video_path = Path(video_file).resolve()
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_file}")

        results = {}
        strategies_to_test = []

        # Determine which strategies are viable
        if self.runtime and self.has_docker_acceleration:
            strategies_to_test.append('docker_accel')
        if self.runtime:
            strategies_to_test.append('docker_cpu')
        if self.system_ffmpeg_path:
            strategies_to_test.append('system_cpu')
        if self.has_system_acceleration:
            strategies_to_test.append('system_accel')

        print(f"\nTesting {len(strategies_to_test)} strategies with {video_file} ({duration}s)...")
        print("="*60)

        for strategy in strategies_to_test:
            print(f"\nTesting {strategy}...")

            # Create temporary chooser with this strategy
            temp_chooser = FfmpegChooser(
                image=self._image_for_run(),
                prefer_strategy=strategy,
                quiet=True,
                sandbox=getattr(self, 'sandbox_preference', 'auto'),
            )

            # Build a unique output filename.  Never reuse (or unlink) a
            # ``test_<strategy>.mkv`` in the source directory: that name can
            # belong to a user file or a previous run, so deleting it would
            # destroy data.  Docker encodes must stay in the input directory
            # because only that directory is mounted into the container;
            # system encodes can use ``output_dir``.
            if strategy.startswith('docker'):
                test_dir = video_path.parent
            else:
                test_dir = Path(output_dir)
            output_file = test_dir / (
                f".rmbloat-test-{strategy}-{os.getpid()}-{uuid.uuid4().hex[:8]}.mkv")

            # Create encoding parameters
            try:
                params = temp_chooser.make_namespace(
                    input_file=str(video_path),
                    output_file=str(output_file),
                    post_input_opts=['-t', str(duration)],
                    use_nice_ionice=False,  # Don't use nice for tests
                )
                cmd = temp_chooser.make_ffmpeg_cmd(params)

                cmd_str = ' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in cmd)
                print(f"  → {cmd_str}")

                # Run the encoding
                start_time = time.time()
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=duration * 5,  # Generous timeout
                    check=False
                )
                elapsed = time.time() - start_time

                if result.returncode == 0 and output_file.exists():
                    file_size = output_file.stat().st_size
                    results[strategy] = {
                        'success': True,
                        'time': elapsed,
                        'size': file_size,
                        'error': None
                    }
                    print(f"  ✓ Success: {elapsed:.1f}s, {file_size / 1024 / 1024:.1f}MB")
                else:
                    error_msg = result.stderr.decode('utf-8', errors='ignore')[-500:]
                    results[strategy] = {
                        'success': False,
                        'time': elapsed,
                        'size': 0,
                        'error': error_msg
                    }
                    print(f"  ✗ Failed: {error_msg[:100]}...")

            except subprocess.TimeoutExpired:
                results[strategy] = {
                    'success': False,
                    'time': duration * 5,
                    'size': 0,
                    'error': 'Timeout'
                }
                print(f"  ✗ Timeout after {duration * 5}s")
            except Exception as e:
                results[strategy] = {
                    'success': False,
                    'time': 0,
                    'size': 0,
                    'error': str(e)
                }
                print(f"  ✗ Error: {e}")
            finally:
                # Remove only the file this test created (including a partial
                # file left behind by a timeout).  Pre-existing files, such as
                # a user's own ``test_<strategy>.mkv``, are never touched.
                try:
                    output_file.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"  ✗ Could not remove test output {output_file}: {exc}")

        # Print summary
        print("\n" + "="*60)
        print("Test Results Summary")
        print("="*60)

        for strategy in strategies_to_test:
            result = results[strategy]
            if result['success']:
                status = "✓"
                time_str = f"{result['time']:.1f}s"
                size_str = f"{result['size'] / 1024 / 1024:.1f}MB"
                print(f"{status} {strategy:15s} {time_str:>8s}  {size_str:>8s}")
            else:
                print(f"✗ {strategy:15s} FAILED")

        # Show recommended strategy
        print("\n" + "="*60)
        print(f"Current auto strategy: {self.strategy}")

        # Recommend based on what succeeded
        if 'system_accel' in results and results['system_accel']['success']:
            print("Recommended: system_accel (best performance)")
        elif 'docker_accel' in results and results['docker_accel']['success']:
            print("Recommended: docker_accel (great performance + reliability)")
        elif 'system_cpu' in results and results['system_cpu']['success']:
            print("Recommended: system_cpu (CPU fallback)")

        print("="*60 + "\n")

        return results

    def make_ffprobe_cmd(self, input_file: str, *extra_args: str) -> List[str]:
        """
        Build an ffprobe command.

        Args:
            input_file: Path to input file
            *extra_args: Additional ffprobe arguments

        Returns:
            List of command arguments ready for subprocess

        Example:
            cmd = chooser.make_ffprobe_cmd('input.mp4', '-show_format', '-show_streams')
            result = subprocess.run(cmd, capture_output=True, text=True)
        """
        cmd = []

        # Determine working directory
        input_path = Path(input_file).resolve()
        workdir = str(input_path.parent)
        input_basename = input_path.name

        # Media files are local.  Denying the network protocols blocks a
        # crafted playlist/manifest from making ffprobe fetch remote data.
        hardened = ['-protocol_whitelist', Sandbox.PROTOCOL_WHITELIST]

        # Build base command (docker/podman vs system)
        if self.use_docker:
            cmd.extend([
                self.runtime, 'run', '--rm',
                *self._container_hardening(use_acceleration=False),
                *self._docker_user_and_group_args(None),
                '-v', f'{workdir}:{workdir}:ro,z',
                '-w', workdir,
                '--entrypoint', 'ffprobe',
                self._image_for_run(),
                *hardened,
            ])
            cmd.append(input_basename)
        else:
            cmd.extend(['ffprobe', *hardened, input_file])

        # Add extra arguments
        if extra_args:
            cmd.extend(extra_args)

        if not self.use_docker:
            cmd = Sandbox.wrap_local(
                cmd, input_dirs=[workdir], workdir=workdir,
                strategy=getattr(self, 'sandbox_mode', 'none'))

        return cmd

    def run_tests(self, video_file: Optional[str] = None, duration: int = 30,
                  output_dir: Optional[str] = None, show_test_encode: bool = False) -> int:
        """
        Run various tests on the FfmpegChooser.

        Args:
            video_file: Optional video file for real-world tests
            duration: Duration in seconds for real-world test (default: 30)
            output_dir: Output directory for test files (default: temp directory)
            show_test_encode: Show example encode commands (default: False)

        Returns:
            int: 0 for success, 1 for failure
        """
        try:
            # Real-world tests with video file
            if video_file:
                results = self.real_world_tests(
                    video_file,
                    duration=duration,
                    output_dir=output_dir
                )

                # Return 0 if any test succeeded, 1 if all failed
                return 0 if any(r['success'] for r in results.values()) else 1

            # Basic detection info
            print(f"\nStatus: Using {self.runtime} container" if self.use_docker
                  else "\nStatus: Using system ffmpeg")
            print("Status: Hardware acceleration enabled" if self.use_acceleration
                  else "Status: CPU encoding only")

            # Test command generation if requested
            if show_test_encode:
                print("\n" + "="*60)
                print("Example FFmpeg Command")
                print("="*60)

                # These paths are illustrative only, so do not require the
                # input file to actually exist.
                params = self.make_namespace(
                    input_file='input.mp4',
                    output_file='output.mkv',
                    crf=28,
                    preset='medium',
                    color_opts=['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709'],
                    validate_input_exists=False,
                )

                cmd = self.make_ffmpeg_cmd(params)
                print(' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in cmd))

                print("\n" + "="*60)
                print("Example FFprobe Command")
                print("="*60)

                probe_cmd = self.make_ffprobe_cmd('input.mp4', '-show_format', '-show_streams')
                print(' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in probe_cmd))

            return 0

        except (RuntimeError, FileNotFoundError) as e:
            print(f"\nError: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            return 130

def main() -> int:
    """Test the FfmpegChooser."""

    parser = argparse.ArgumentParser(
        description="Detect and configure FFmpeg runtime with hardware acceleration"
    )
    parser.add_argument(
        '--force-pull',
        action='store_true',
        help='Force pull the Docker image even if it exists'
    )
    parser.add_argument(
        '--image',
        default='joedefen/ffmpeg-vaapi-docker:latest',
        help='Docker image to use (default: joedefen/ffmpeg-vaapi-docker:latest)'
    )
    parser.add_argument(
        '--prefer-strategy',
        choices=['auto', 'docker_accel', 'docker_cpu', 'system_cpu', 'system_accel'],
        default='auto',
        help='Preferred encoding strategy (default: auto)'
    )
    parser.add_argument(
        '--sandbox',
        choices=Sandbox.SANDBOX_CHOICES,
        default='auto',
        help='Isolation for local ffmpeg/ffprobe (default: auto)'
    )
    parser.add_argument(
        '--test-encode',
        action='store_true',
        help='Show example encode command'
    )
    parser.add_argument(
        '--real-test',
        metavar='VIDEO_FILE',
        help='Run real-world encoding tests on the specified video file'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=30,
        help='Duration in seconds for real-world test (default: 30)'
    )
    parser.add_argument(
        '--output-dir',
        help='Output directory for test files (default: temp directory)'
    )

    args = parser.parse_args()

    # Create chooser with appropriate strategy
    prefer_strategy = 'auto' if args.real_test else args.prefer_strategy
    chooser = FfmpegChooser(
        force_pull=args.force_pull,
        image=args.image,
        prefer_strategy=prefer_strategy,
        sandbox=args.sandbox,
    )

    # Run tests
    return chooser.run_tests(
        video_file=args.real_test,
        duration=args.duration,
        output_dir=args.output_dir,
        show_test_encode=args.test_encode
    )


if __name__ == '__main__':
    sys.exit(main())
