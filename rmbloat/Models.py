#!/usr/bin/env python3
"""Data models for rmbloat video conversion
"""
import math
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta

from .ProbeCache import Probe
from .TranscodeThread import TranscodeThread


@dataclass(slots=True)
class PathProbePair:
    """ Pairs a video file path with its probe metadata """
    video_file: str
    probe: Probe
    do_rename: bool = field(default=False, init=False)
    standard_name: str = field(default='', init=False)

@dataclass
class FfmpegRun:
    """ per ffmpeg run info"""
    command: str | None = None
    descr: str | None = None # describe the run
    return_code: int | None = None
    texts: list[str] = field(default_factory=list)

@dataclass(slots=True)
class Vid:
    """ Our main object for the list of video entries """
    video_file: str = field(init=False)
    filepath: str = field(init=False)
    filebase: str = field(init=False)
    standard_name: str = field(init=False)
    do_rename: bool = field(init=False)

    doit: str = field(default='', init=False)
    doit_auto: str = field(default='', init=False)
    net: str = field(default='  - ', init=False)
    width: int | None = field(default=None, init=False)
    height: int | None = field(default=None, init=False)
    res_ok: bool | None = field(default=None, init=False)
    duration: float | None = field(default=None, init=False)
    codec: str | None = field(default=None, init=False)
    bitrate: float | None = field(default=None, init=False)
    bloat: float | None = field(default=None, init=False)
    bloat_ok: bool | None = field(default=None, init=False)
    codec_ok: bool | None = field(default=None, init=False)
    gb: float | None = field(default=None, init=False)
    all_ok: bool | None = field(default=None, init=False)
    probe0: Probe | None = field(default=None, init=False)
    probe1: Probe | None = field(default=None, init=False)
    basename1: str | None = field(default=None, init=False)

    runs:  list[FfmpegRun] = field(default_factory=list, init=False)
    texts: list = field(default_factory=list, init=False)

    ops: list = field(default_factory=list, init=False)

    def post_init(self, ppp):
        """ Custom initialization logic after dataclass __init__ """
        self.video_file = ppp.video_file
        self.filepath = ppp.video_file
        self.filebase = os.path.basename(ppp.video_file)
        self.standard_name = ppp.standard_name
        self.do_rename = ppp.do_rename

    def start_new_run(self, command=None):
        """ Creates place for new run to be stored """
        self.runs.append(FfmpegRun(command=command))

    def descr_str(self):
        """ return the descr string for adorning progress """
        # A run only gets its ``descr`` later (JobHandler sets 'initial run'),
        # and `start_new_run()` leaves it None, so tolerate both that and an empty runs list
        # instead of raising TypeError/IndexError.
        if not self.runs:
            return ''
        descr = self.runs[-1].descr
        if not descr or 'initial' in str(descr):
            return ''
        return f'  [{descr}]'

class Job:
    """ Represents a video conversion job """
    def __init__(self, vid, orig_backup_file, temp_file, duration_secs, opts,
                 is_retry=False, is_software_fallback=False, temp_dir=None):
        self.vid = vid
        self.opts = opts # Needed for TranscodeThread timeout logic
        self.start_mono = time.monotonic()

        self.input_file = os.path.basename(vid.filepath)
        self.orig_backup_file = orig_backup_file
        self.temp_file = temp_file
        self.duration_secs = duration_secs

        self.is_retry = bool(is_retry)
        self.is_software_fallback = bool(is_software_fallback)
        self.temp_dir = temp_dir
        self.total_duration_formatted = self.trim0(
                        str(timedelta(seconds=int(duration_secs))))

        self.thread: TranscodeThread | None = None

    @property
    def progress(self):
        """The UI calls this.  It simply grabs the pre-baked string from the background thread.
        """
        if self.thread:
            # If the thread is finished, it might return the int return_code
            if self.thread.is_finished:
                return self.thread.info.return_code
            return self.thread.status_string + self.vid.descr_str()
        return "Pending..."

    @staticmethod
    def trim0(string):
        """ Remove leading '0:' from time string """
        if string.startswith('0:'):
            return string[2:]
        return string

    @staticmethod
    def duration_spec(secs):
        """ Convert seconds to HH:MM:SS format """
        secs = int(round(secs))
        hours = math.floor(secs / 3600)
        minutes = math.floor((secs % 3600) / 60)
        secs = secs % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def abort(self):
        """ Stop the job and kill the thread/process """
        if self.thread:
            self.thread.abort()
