#!/usr/bin/env python3
"""
Data models for rmbloat video conversion
"""
import os
import sys
import time
import math
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import timedelta
from .ProbeCache import Probe
from .FfmpegMon import FfmpegMon
# pylint: disable=import-outside-toplevel,too-many-instance-attributes

# Dataclass configuration for Python 3.10+ slots support
_dataclass_kwargs = {'slots': True} if sys.version_info >= (3, 10) else {}

@dataclass(**_dataclass_kwargs)
class PathProbePair:
    """ Pairs a video file path with its probe metadata """
    video_file: str
    probe: Probe
    do_rename: bool = field(default=False, init=False)
    standard_name: str = field(default='', init=False)

@dataclass
class FfmpegRun:
    """ per ffmpeg run info"""
    command: Optional[str] = None
    descr: Optional[str] = None # describe the run
    return_code: Optional[int] = None
    texts: List[str] = field(default_factory=list)

@dataclass(**_dataclass_kwargs)
class Vid:
    """ Our main object for the list of video entries """
    # Fields set from init parameters (via post_init)
    video_file: str = field(init=False)
    filepath: str = field(init=False)
    filebase: str = field(init=False)
    standard_name: str = field(init=False)
    do_rename: bool = field(init=False)

    # Fields with default values
    doit: str = field(default='', init=False)
    doit_auto: str = field(default='', init=False)
    net: str = field(default='  - ', init=False)
    width: Optional[int] = field(default=None, init=False)
    height: Optional[int] = field(default=None, init=False)
    res_ok: Optional[bool] = field(default=None, init=False)
    duration: Optional[float] = field(default=None, init=False)
    codec: Optional[str] = field(default=None, init=False)
    bitrate: Optional[float] = field(default=None, init=False)
    bloat: Optional[float] = field(default=None, init=False)
    bloat_ok: Optional[bool] = field(default=None, init=False)
    codec_ok: Optional[bool] = field(default=None, init=False)
    gb: Optional[float] = field(default=None, init=False)
    all_ok: Optional[bool] = field(default=None, init=False)
    probe0: Optional[Probe] = field(default=None, init=False)
    probe1: Optional[Probe] = field(default=None, init=False)
    basename1: Optional[str] = field(default=None, init=False)

    runs:  List[FfmpegRun] = field(default_factory=list, init=False)
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

class Job:
    """ Represents a video conversion job """
    def __init__(self, vid, orig_backup_file, temp_file, duration_secs):
        """
        Args:
            vid: Vid object
            orig_backup_file: Path to backup of original file
            temp_file: Path to temporary output file
            duration_secs: Video duration in seconds
        """
        self.vid = vid
        self.start_mono = time.monotonic()

        self.progress = f'Started  [{vid.runs[-1].descr}]'
        self.input_file = os.path.basename(vid.filepath)
        self.orig_backup_file = orig_backup_file
        self.temp_file = temp_file
        self.duration_secs = duration_secs
        self.total_duration_formatted = self.trim0(
                        str(timedelta(seconds=int(duration_secs))))

        self.ffsubproc = FfmpegMon()
        self.return_code = None

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
    
    def stop(self):
        """ stop the job """
        if self.ffsubproc:
            self.ffsubproc.stop()
