#!/usr/bin/env python3
"""
FfmpegChooser - Intelligent FFmpeg runtime selection with hardware acceleration support
"""

import subprocess
import sys
import shutil
from pathlib import Path


class FfmpegChooser:
    """
    Detects and configures the best available FFmpeg runtime.
    
    Priority order:
    1. System ffmpeg with working hardware acceleration
    2. Docker/Podman with hardware acceleration
    3. Docker/Podman without hardware acceleration (CPU fallback)
    4. System ffmpeg without hardware acceleration
    """
    
    def __init__(self, force_pull=False, image="joedefen/ffmpeg-vaapi-docker:latest", 
                 prefer_strategy='auto', quiet=False):
        """
        Initialize and detect the best FFmpeg configuration.
        
        Args:
            force_pull: Force pull the Docker image even if it exists locally
            image: Docker image to use (default: joedefen/ffmpeg-vaapi-docker:latest)
            prefer_strategy: Strategy preference - 'auto', 'docker_accel', 'docker_cpu', 
                           'system_cpu', or 'system_accel' (default: 'auto')
            quiet: Suppress detection output (default: False)
        """
        self.image = image
        self.runtime = None  # 'docker', 'podman', or None
        self.has_docker_acceleration = False
        self.has_system_acceleration = False
        self.use_docker = False
        self.use_acceleration = False
        self.system_ffmpeg_path = None
        self.render_device = None
        self.prefer_strategy = prefer_strategy
        self.quiet = quiet
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
        
        # Print summary
        if not quiet:
            self._print_summary()
    
    def _detect_system_ffmpeg(self):
        """Detect if system ffmpeg exists and test hardware acceleration."""
        self.system_ffmpeg_path = shutil.which('ffmpeg')
        
        if not self.system_ffmpeg_path:
            print("  ✗ System ffmpeg not found")
            return
        
        print(f"  ✓ System ffmpeg found: {self.system_ffmpeg_path}")
        
        # Test hardware acceleration
        self.has_system_acceleration = self._test_system_acceleration()
        if self.has_system_acceleration:
            print(f"  ✓ System ffmpeg has working hardware acceleration")
        else:
            print(f"  ✗ System ffmpeg hardware acceleration not available")
    
    def _test_system_acceleration(self):
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
        
        try:
            result = subprocess.run(
                test_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10
            )
            if result.returncode == 0:
                self.render_device = render_device
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        return False
    
    def _detect_runtime(self):
        """Detect if docker or podman is available."""
        # Try docker first
        if shutil.which('docker'):
            try:
                result = subprocess.run(
                    ['docker', 'info'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5
                )
                if result.returncode == 0:
                    self.runtime = 'docker'
                    print("  ✓ Docker detected and running")
                    return
            except subprocess.TimeoutExpired:
                pass
        
        # Try podman
        if shutil.which('podman'):
            try:
                result = subprocess.run(
                    ['podman', 'info'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5
                )
                if result.returncode == 0:
                    self.runtime = 'podman'
                    print("  ✓ Podman detected and running")
                    return
            except subprocess.TimeoutExpired:
                pass
        
        print("  ✗ Neither Docker nor Podman found")
        self.runtime = None
    
    def _ensure_image(self, force_pull):
        """Ensure the Docker/Podman image is available locally."""
        if not self.runtime:
            return
        
        # Check if image exists locally
        check_cmd = [self.runtime, 'image', 'inspect', self.image]
        
        try:
            result = subprocess.run(
                check_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5
            )
            image_exists = result.returncode == 0
        except subprocess.TimeoutExpired:
            image_exists = False
        
        # Pull if needed or forced
        if force_pull or not image_exists:
            if force_pull:
                print(f"  → Force pulling {self.image}...")
            else:
                print(f"  → Pulling {self.image}...")
            
            pull_cmd = [self.runtime, 'pull', self.image]
            try:
                result = subprocess.run(pull_cmd, timeout=300)
                if result.returncode == 0:
                    print(f"  ✓ Image {self.image} ready")
                else:
                    print(f"  ✗ Failed to pull image {self.image}")
                    self.runtime = None
            except subprocess.TimeoutExpired:
                print(f"  ✗ Timeout pulling image {self.image}")
                self.runtime = None
        else:
            print(f"  ✓ Image {self.image} already available locally")
    
    def _find_render_device(self):
        """Find the first available render device."""
        dri_path = Path("/dev/dri")
        if not dri_path.exists():
            return None
        
        # Look for renderD128, renderD129, etc.
        for device in sorted(dri_path.glob("renderD*")):
            return str(device)
        
        return None
    
    def _test_docker_acceleration(self):
        """Test if Docker/Podman can use hardware acceleration."""
        if not self.runtime:
            return
        
        # Check if /dev/dri exists
        if not Path("/dev/dri").exists():
            print("  ✗ /dev/dri not found - hardware acceleration unavailable")
            return
        
        # Find render device
        render_device = self._find_render_device()
        if not render_device:
            print("  ✗ No render device found in /dev/dri")
            return
        
        print(f"  → Testing hardware acceleration with {render_device}...")
        
        # Test command
        test_cmd = [
            self.runtime, 'run', '--rm',
            f'--device=/dev/dri:/dev/dri',
            self.image,
            '-y',
            '-init_hw_device', f'vaapi=va:{render_device}',
            '-filter_hw_device', 'va',
            '-f', 'lavfi', '-i', 'nullsrc=s=128x128:d=1',
            '-vf', 'format=nv12,hwupload',
            '-c:v', 'hevc_vaapi',
            '-frames:v', '1',
            '-f', 'null', '-'
        ]
        
        try:
            result = subprocess.run(
                test_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30
            )
            
            if result.returncode == 0:
                self.has_docker_acceleration = True
                self.render_device = render_device
                print(f"  ✓ Hardware acceleration working in {self.runtime}")
            else:
                print(f"  ✗ Hardware acceleration test failed in {self.runtime}")
        except subprocess.TimeoutExpired:
            print(f"  ✗ Hardware acceleration test timed out")
    
    def _decide_strategy(self):
        """Decide which FFmpeg to use based on available options and preference."""
        
        # If user specified a preference, try to honor it
        if self.prefer_strategy != 'auto':
            if self._try_strategy(self.prefer_strategy):
                return
            
            # If preferred strategy not available, fall through to auto
            if not self.quiet:
                print(f"  ⚠ Preferred strategy '{self.prefer_strategy}' not available, using auto")
        
        # Auto priority (based on real-world performance)
        # Priority 1: System with acceleration (fastest - no container overhead)
        if self._try_strategy('system_accel'):
            return
        
        # Priority 2: Docker with acceleration (very fast, most reliable)
        if self._try_strategy('docker_accel'):
            return
        
        # Priority 3: System without acceleration (slower but works)
        if self._try_strategy('system_cpu'):
            return
        
        # Priority 4: Docker without acceleration (slowest - container + CPU)
        if self._try_strategy('docker_cpu'):
            return
        
        # No viable option
        raise RuntimeError(
            "No usable FFmpeg found. Please install either:\n"
            "  - Docker/Podman with ffmpeg-vaapi-docker image, or\n"
            "  - System ffmpeg package"
        )
    
    def _try_strategy(self, strategy):
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
    
    def _print_summary(self):
        """Print a summary of the chosen configuration."""
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
            print(f"Acceleration: Disabled (CPU encoding)")
        
        print(f"Strategy:     {self.strategy}")
        print("="*60 + "\n")


    def make_namespace(self, **overrides):
        """
        Create a namespace with sensible defaults for FFmpeg encoding.
        
        Required arguments (must be provided):
            input_file: Input video file path
            output_file: Output video file path
        
        Optional arguments with defaults:
            crf: Quality (default: 28, lower=better quality)
            preset: Encoding preset (default: 'medium')
            codec: Target codec (default: 'hevc')
            use_10bit: Use 10-bit encoding (default: True)
            scale_opts: Scaling filter options (default: [])
            color_opts: Color space options (default: [])
            map_opts: Stream mapping options (default: [])
            thread_count: Thread limit, 0=auto (default: 0)
            sample_mode: Extract sample clip (default: False)
            sample_start_secs: Sample start time in seconds (default: None)
            sample_duration_secs: Sample duration in seconds (default: None)
            use_nice_ionice: Use nice/ionice for low priority (default: True)
            pre_input_opts: Options before -i (default: [])
            post_input_opts: Options after -i (default: [])
        
        Returns:
            SimpleNamespace with all parameters
        
        Example:
            params = chooser.make_namespace(
                input_file='input.mp4',
                output_file='output.mkv',
                crf=28,
                color_opts=['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', '709']
            )
        """
        from types import SimpleNamespace
        
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
            scale_opts=[],
            color_opts=[],
            map_opts=[],
            external_subtitle=None,  # Path to external .srt file to merge
            
            # Threading
            thread_count=0,  # 0 = auto
            
            # Sampling
            sample_mode=False,
            sample_start_secs=None,
            sample_duration_secs=None,
            
            # Priority
            use_nice_ionice=True,
            
            # Pre/post input opts
            pre_input_opts=[],
            post_input_opts=[],
        )
        
        # Apply overrides
        for key, value in overrides.items():
            if not hasattr(defaults, key):
                raise ValueError(f"Unknown parameter: {key}")
            setattr(defaults, key, value)
        
        # Validate required fields
        if defaults.input_file is None:
            raise ValueError("input_file is required")
        if defaults.output_file is None:
            raise ValueError("output_file is required")
        
        return defaults
    
    def _crf_to_qp(self, crf):
        """
        Map CRF (software encoding) to QP (hardware encoding).
        
        CRF is used by x264/x265, QP is used by hardware encoders.
        This provides a reasonable quality mapping between the two.
        """
        CRF_TO_QP = {
            18: 20, 19: 21, 20: 22, 21: 23, 22: 24,
            23: 25, 24: 26, 25: 27, 26: 28, 27: 29,
            28: 30, 29: 31, 30: 32, 31: 33, 32: 34,
            33: 35, 34: 36, 35: 37, 36: 38, 37: 39,
            38: 40, 39: 41, 40: 42,
        }
        
        if crf in CRF_TO_QP:
            return CRF_TO_QP[crf]
        # Fallback for values outside table
        return min(51, max(0, crf + 2))
    
    def _map_preset(self, preset, for_hardware):
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
    
    def make_ffmpeg_cmd(self, params):
        """
        Build an ffmpeg command from the provided parameters.
        
        Args:
            params: SimpleNamespace from make_namespace()
        
        Returns:
            List of command arguments ready for subprocess
        
        Example:
            params = chooser.make_namespace(
                input_file='input.mp4',
                output_file='output.mkv'
            )
            cmd = chooser.make_ffmpeg_cmd(params)
            subprocess.run(cmd)
        """
        cmd = []
        
        # Add nice/ionice at the very beginning (affects entire process)
        if params.use_nice_ionice:
            cmd.extend(['ionice', '-c3', 'nice', '-n20'])
        
        # Determine working directory (absolute path of input file's directory)
        input_path = Path(params.input_file).resolve()
        workdir = str(input_path.parent)
        input_basename = input_path.name
        
        # Handle external subtitle if provided
        subtitle_basename = None
        if params.external_subtitle:
            subtitle_path = Path(params.external_subtitle).resolve()
            if subtitle_path.exists():
                subtitle_basename = subtitle_path.name
                # Ensure subtitle is in same directory as input for Docker mounting
                if subtitle_path.parent != input_path.parent:
                    print(f"Warning: Subtitle file must be in same directory as input for Docker. Ignoring subtitle.")
                    subtitle_basename = None
        
        # Build base command (docker/podman vs system)
        if self.use_docker:
            cmd.extend([
                self.runtime, 'run', '--rm',
                '-v', f'{workdir}:{workdir}',
                '-w', workdir,
            ])
            
            # Add device passthrough if using acceleration
            if self.use_acceleration:
                cmd.extend(['--device=/dev/dri:/dev/dri'])
            
            cmd.append(self.image)
        
        # FFmpeg arguments start here
        # (Docker image already has ffmpeg as entrypoint, don't add it again)
        if not self.use_docker:
            cmd.append('ffmpeg')
        
        cmd.append('-y')
        
        # Pre-input options (e.g., -ss for seeking)
        if params.pre_input_opts:
            cmd.extend(params.pre_input_opts)
        
        # Input file
        cmd.extend(['-i', input_basename if self.use_docker else params.input_file])
        
        # Add external subtitle as additional input if provided
        subtitle_input_index = None
        if subtitle_basename:
            cmd.extend(['-i', subtitle_basename if self.use_docker else params.external_subtitle])
            subtitle_input_index = 1  # Subtitle is the second input (index 1)
        
        # Post-input options (e.g., -t for duration)
        if params.post_input_opts:
            cmd.extend(params.post_input_opts)
        
        # Scaling options
        if params.scale_opts:
            cmd.extend(params.scale_opts)
        
        # Determine encoder and quality settings
        if self.use_acceleration:
            # Hardware encoding
            if params.codec == 'hevc':
                codec = 'hevc_vaapi'
            elif params.codec == 'h264':
                codec = 'h264_vaapi'
            else:
                codec = f'{params.codec}_vaapi'
            
            # Quality: use QP for hardware
            qp = self._crf_to_qp(params.crf)
            cmd.extend(['-qp', str(qp)])
            
            # Pixel format and profile
            if params.use_10bit:
                pix_fmt = 'p010le'
                cmd.extend(['-profile:v', 'main10'])
            else:
                pix_fmt = 'nv12'
            
            # Hardware upload filter
            cmd.extend([
                '-vf', f'format={pix_fmt},hwupload',
                '-vaapi_device', self.render_device,
            ])
            
        else:
            # Software encoding
            if params.codec == 'hevc':
                codec = 'libx265'
            elif params.codec == 'h264':
                codec = 'libx264'
            else:
                codec = f'lib{params.codec}'
            
            # Quality: use CRF for software
            cmd.extend(['-crf', str(params.crf)])
            
            # Pixel format
            if params.use_10bit:
                pix_fmt = 'yuv420p10le'
            else:
                pix_fmt = 'yuv420p'
            
            # Thread control for software encoding
            if params.thread_count > 0:
                if params.codec == 'hevc':
                    cmd.extend(['-x265-params', f'pools={params.thread_count}'])
                elif params.codec == 'h264':
                    cmd.extend(['-threads', str(params.thread_count)])
        
        # Preset (mapped if needed)
        preset = self._map_preset(params.preset, self.use_acceleration)
        cmd.extend(['-preset', preset])
        
        # Pixel format
        cmd.extend(['-pix_fmt', pix_fmt])
        
        # Color options
        if params.color_opts:
            cmd.extend(params.color_opts)
        
        # Stream mapping
        if params.map_opts:
            cmd.extend(params.map_opts)
        
        # If we have an external subtitle, map it and set metadata
        if subtitle_input_index is not None:
            cmd.extend(['-map', f'{subtitle_input_index}:s:0'])  # Map first subtitle stream from subtitle input
            cmd.extend(['-c:s', 'srt'])  # Keep as SRT format
            cmd.extend(['-metadata:s:s:0', 'language=eng'])  # Set language to English
            cmd.extend(['-metadata:s:s:0', 'title=English'])  # Set title to English
        
        # Codec
        cmd.extend(['-c:v', codec, '-c:a', 'copy', '-c:s', 'copy'])
        
        # Output file
        output_basename = Path(params.output_file).name if self.use_docker else params.output_file
        cmd.append(output_basename)
        
        return cmd
    
    def real_world_tests(self, video_file, duration=30, output_dir=None):
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
        import tempfile
        import time
        
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
                image=self.image,
                prefer_strategy=strategy,
                quiet=True
            )
            
            # Build output filename
            # For Docker, output must be in the same dir as input (mounted workdir)
            # For system, can use output_dir
            if strategy.startswith('docker'):
                output_file = video_path.parent / f"test_{strategy}.mkv"
            else:
                output_file = Path(output_dir) / f"test_{strategy}.mkv"
            
            if output_file.exists():
                output_file.unlink()
            
            # Create encoding parameters
            try:
                params = temp_chooser.make_namespace(
                    input_file=str(video_path),
                    output_file=str(output_file),
                    post_input_opts=['-t', str(duration)],
                    use_nice_ionice=False,  # Don't use nice for tests
                )
                
                cmd = temp_chooser.make_ffmpeg_cmd(params)
                
                # Print the command being run (helpful for debugging)
                cmd_str = ' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in cmd)
                print(f"  → {cmd_str}")
                
                # Run the encoding
                start_time = time.time()
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=duration * 5  # Generous timeout
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
                
                # Clean up output file
                if output_file.exists():
                    output_file.unlink()
                    
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
        
        # Build base command (docker/podman vs system)
        if self.use_docker:
            cmd.extend([
                self.runtime, 'run', '--rm',
                '-v', f'{workdir}:{workdir}',
                '-w', workdir,
                '--entrypoint', 'ffprobe',
                self.image,
            ])
            cmd.append(input_basename)
        else:
            cmd.append('ffprobe')
            cmd.append(input_file)
        
        # Add extra arguments
        if extra_args:
            cmd.extend(extra_args)
        
        return cmd


def main():
    """Test the FfmpegChooser."""
    import argparse
    
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
    
    try:
        # Real-world tests require detection first but don't need a specific strategy
        if args.real_test:
            # Detect with auto to see all capabilities
            chooser = FfmpegChooser(
                force_pull=args.force_pull, 
                image=args.image,
                prefer_strategy='auto'
            )
            
            results = chooser.real_world_tests(
                args.real_test,
                duration=args.duration,
                output_dir=args.output_dir
            )
            
            # Return 0 if any test succeeded, 1 if all failed
            return 0 if any(r['success'] for r in results.values()) else 1
        
        # Normal detection and testing
        chooser = FfmpegChooser(
            force_pull=args.force_pull, 
            image=args.image,
            prefer_strategy=args.prefer_strategy
        )
        
        # Print status for scripting
        if chooser.use_docker:
            print(f"Status: Using {chooser.runtime} container")
        else:
            print(f"Status: Using system ffmpeg")
        
        if chooser.use_acceleration:
            print("Status: Hardware acceleration enabled")
        else:
            print("Status: CPU encoding only")
        
        # Test command generation if requested
        if args.test_encode:
            print("\n" + "="*60)
            print("Example FFmpeg Command")
            print("="*60)
            
            params = chooser.make_namespace(
                input_file='input.mp4',
                output_file='output.mkv',
                crf=28,
                preset='medium',
                color_opts=['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', '709'],
            )
            
            cmd = chooser.make_ffmpeg_cmd(params)
            print(' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in cmd))
            
            print("\n" + "="*60)
            print("Example FFprobe Command")
            print("="*60)
            
            probe_cmd = chooser.make_ffprobe_cmd('input.mp4', '-show_format', '-show_streams')
            print(' '.join(f"'{arg}'" if ' ' in str(arg) else str(arg) for arg in probe_cmd))
        
        return 0
    
    except RuntimeError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 130


if __name__ == '__main__':
    sys.exit(main())