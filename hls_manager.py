# -*- coding: utf-8 -*-
"""Safe, shared FFmpeg HLS streams for the HDHomeRun FF plugin.

The manager deliberately has no Flask/FF-plugin dependency.  The caller owns
authentication and turns the returned local playlist/segment names into its
own protected HTTP endpoints.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit
try:
    from .transcode_config import DEFAULTS, validate_options, video_filter, delayed_playlist
except ImportError:
    from transcode_config import DEFAULTS, validate_options, video_filter, delayed_playlist


class HLSManager:
    """Run one FFmpeg process per ``(channel_id, output_profile)`` pair.

    A stream is intentionally shared by all viewers of the same channel and
    profile.  This keeps a mobile conversion from being duplicated for every
    Shyni client.  Only opaque SHA-256 based directory names are used on disk;
    caller supplied channel IDs never become filesystem paths.
    """

    DEFAULT_CACHE_ROOT = "/data/tmp/hdhomerun_ff_hls"
    CACHE_ROOT = DEFAULT_CACHE_ROOT

    PROFILES = ("copy", "copy_video_aac", "mobile_cpu", "mobile_1080")
    SEGMENT_RE = re.compile(r"^segment_[0-9]{9}\.ts$")
    IDLE_TIMEOUT = 60
    START_TIMEOUT = 25
    MIN_READY_SEGMENTS = 3
    PLAYLIST_MAX_AGE = 8
    HLS_TIME = 2
    HLS_LIST_SIZE = 90
    HLS_DELETE_THRESHOLD = 30

    _lock = threading.RLock()
    _streams: Dict[str, Dict[str, object]] = {}
    _janitor_stop = None
    TRANSCODE = dict(DEFAULTS)

    @classmethod
    def configure(
        cls,
        *,
        cache_root: Optional[str] = None,
        idle_timeout: Optional[int] = None,
        start_timeout: Optional[int] = None,
        transcode_options: Optional[dict] = None,
    ) -> Dict[str, object]:
        """Configure runtime paths and bounded lifecycle timeouts.

        ``cache_root`` is resolved before being saved, which makes all later
        path containment checks independent of symlinks in a parent directory.
        """
        with cls._lock:
            if cache_root is not None:
                value = str(cache_root).strip()
                if not value:
                    raise ValueError("cache_root is required")
                cls.CACHE_ROOT = os.path.realpath(value)
            if idle_timeout is not None:
                cls.IDLE_TIMEOUT = cls._bounded_int(idle_timeout, 30, 600, 60)
            if start_timeout is not None:
                cls.START_TIMEOUT = cls._bounded_int(start_timeout, 1, 60, 25)
            if transcode_options is not None:
                cls.TRANSCODE = validate_options(transcode_options)
                cls.HLS_LIST_SIZE = max(60, cls.TRANSCODE['window_seconds']//cls.HLS_TIME)
                if start_timeout is None:
                    cls.START_TIMEOUT = min(60,max(25,cls.TRANSCODE['delay_seconds']+cls.MIN_READY_SEGMENTS*cls.HLS_TIME+10))
            return {
                "cache_root": cls.CACHE_ROOT,
                "idle_timeout": cls.IDLE_TIMEOUT,
                "start_timeout": cls.START_TIMEOUT,
                "profiles": list(cls.PROFILES),
                "transcode": dict(cls.TRANSCODE),
            }

    @staticmethod
    def _bounded_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return fallback

    @classmethod
    def normalize_profile(cls, output_profile: object) -> str:
        profile = str(output_profile or "copy").strip().lower()
        if profile not in cls.PROFILES:
            raise ValueError("unsupported HLS output profile")
        return profile

    @classmethod
    def normalise_output_profile(cls, output_profile: object) -> str:
        """British-spelling public alias used by the plugin route layer."""
        return cls.normalize_profile(output_profile)

    @staticmethod
    def _normalise_consumer_id(consumer_id: object) -> str:
        """Validate an optional viewer marker without making streams per-user."""
        value = str(consumer_id or "").strip()
        if not value:
            return ""
        if len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            return ""
        return value

    @staticmethod
    def _validate_channel_id(channel_id: object) -> str:
        value = str(channel_id or "").strip()
        if not value or len(value) > 256:
            raise ValueError("invalid channel id")
        return value

    @staticmethod
    def _validate_upstream_url(upstream_url: object) -> str:
        value = str(upstream_url or "").strip()
        if not value or len(value) > 4096:
            raise ValueError("invalid upstream URL")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("upstream URL must be HTTP(S)")
        if parts.username or parts.password:
            raise ValueError("upstream URL must not embed credentials")
        return value

    @classmethod
    def stream_key(cls, channel_id: object, output_profile: object = "copy") -> str:
        channel = cls._validate_channel_id(channel_id)
        profile = cls.normalize_profile(output_profile)
        material = f"{channel}\0{profile}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    @classmethod
    def stream_directory(cls, channel_id: object, output_profile: object = "copy") -> str:
        return os.path.join(cls._cache_root(), cls.stream_key(channel_id, output_profile))

    @classmethod
    def _cache_root(cls) -> str:
        root = os.path.realpath(cls.CACHE_ROOT)
        if not root or root == os.path.sep:
            raise ValueError("unsafe HLS cache root")
        return root

    @classmethod
    def _assert_stream_directory(cls, directory: str) -> str:
        root = cls._cache_root()
        target = os.path.realpath(directory)
        if target == root or not target.startswith(root + os.path.sep):
            raise ValueError("unsafe HLS cache path")
        return target

    @classmethod
    def _safe_clear_directory(cls, directory: str) -> None:
        target = cls._assert_stream_directory(directory)
        if os.path.isdir(target):
            shutil.rmtree(target)

    @classmethod
    def _build_command(cls, upstream_url: object, directory: str, output_profile: object = "copy") -> list[str]:
        """Build the exact FFmpeg command without executing it."""
        source = cls._validate_upstream_url(upstream_url)
        profile = cls.normalize_profile(output_profile)
        target = cls._assert_stream_directory(directory)
        ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"

        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-user_agent",
            "hdhomerun_ff/hls-proxy",
            "-i",
            source,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
        ]

        if profile == 'mobile_cpu':
            # Bound decoder/filter thread overhead on small NAS CPUs.
            at = command.index('-i')
            command[at:at] = ['-threads', str(cls.TRANSCODE['threads']), '-filter_threads', str(cls.TRANSCODE['threads'])]

        if profile == "copy":
            command.extend(["-c", "copy"])
            if cls.TRANSCODE['audio_offset_ms']:
                offset = cls.TRANSCODE['audio_offset_ms']/1000
                command.extend(['-bsf:a', f'setts=pts=PTS+({offset:.3f})/TB:dts=DTS+({offset:.3f})/TB'])
        elif profile == 'copy_video_aac':
            command.extend(['-c:v', 'copy', '-c:a', 'aac', '-b:a', f"{cls.TRANSCODE['audio_kbps']}k", '-ac','2','-ar','48000'])
        else:
            # Mobile output is deliberately software-only for now.  Hardware
            # profiles must only be added after a real encoder probe succeeds.
            command.extend([
                "-c:v",
                "libx264",
                "-preset",
                cls.TRANSCODE['preset'] if profile == 'mobile_cpu' else 'ultrafast',
                "-tune",
                "zerolatency",
                "-vf",
                (video_filter(cls.TRANSCODE)
                 if profile == 'mobile_cpu' else "yadif=0:-1:0"),
                "-profile:v",
                "main",
                "-pix_fmt",
                "yuv420p",
                "-force_key_frames",
                "expr:gte(t,n_forced*2)",
                "-sc_threshold",
                "0",
                "-c:a",
                "aac",
                "-b:a",
                f"{cls.TRANSCODE['audio_kbps']}k" if profile == 'mobile_cpu' else '128k',
                "-ac",
                "2",
                "-ar",
                "48000",
            ])
            if profile == 'mobile_cpu':
                command.extend(['-threads:v', str(cls.TRANSCODE['threads']), '-b:v', f"{cls.TRANSCODE['video_kbps']}k",
                                '-maxrate', f"{cls.TRANSCODE['max_kbps']}k", '-bufsize', f"{cls.TRANSCODE['max_kbps']*2}k"])

        if profile != 'copy':
            offset = cls.TRANSCODE['audio_offset_ms']/1000
            command.extend(['-af', f'aresample=async=1,asetpts=PTS+({offset:.3f})/TB'])

        command.extend([
            "-f",
            "hls",
            "-hls_time",
            str(cls.HLS_TIME),
            "-hls_list_size",
            str(cls.HLS_LIST_SIZE),
            "-hls_delete_threshold",
            str(cls.HLS_DELETE_THRESHOLD),
            "-hls_flags",
            "delete_segments+omit_endlist+independent_segments+temp_file",
            "-hls_segment_filename",
            os.path.join(target, "segment_%09d.ts"),
            os.path.join(target, "index.m3u8"),
        ])
        return command

    @classmethod
    def build_command(cls, upstream_url: object, channel_id: object, output_profile: object = "copy") -> list[str]:
        """Public convenience wrapper used by diagnostics and unit tests."""
        directory = cls.stream_directory(channel_id, output_profile)
        return cls._build_command(upstream_url, directory, output_profile)

    @classmethod
    def _stream_identity(cls, channel_id: object, output_profile: object) -> Tuple[str, str, str]:
        channel = cls._validate_channel_id(channel_id)
        profile = cls.normalize_profile(output_profile)
        return cls.stream_key(channel, profile), channel, profile

    @classmethod
    def _read_ready_playlist(cls, directory: str, options=None) -> str:
        playlist_path = os.path.join(cls._assert_stream_directory(directory), "index.m3u8")
        try:
            if time.time() - os.path.getmtime(playlist_path) > cls.PLAYLIST_MAX_AGE:
                return ""
            with open(playlist_path, "r", encoding="utf-8") as handle:
                playlist = handle.read()
            if "#EXTM3U" not in playlist or playlist.count('#EXTINF:') < cls.MIN_READY_SEGMENTS:
                return ""
            return delayed_playlist(playlist, (options or cls.TRANSCODE)['delay_seconds'], cls.MIN_READY_SEGMENTS)
        except (OSError,ValueError):
            return ""

    @classmethod
    def get_playlist(
        cls,
        channel_id: object,
        output_profile: object = "copy",
        consumer_id: object = "",
    ) -> str:
        """Return a fresh local playlist and mark its shared stream active."""
        # A consumer identifier may be used by callers for analytics or channel
        # switching policy, but does not enter the cache key: one channel/profile
        # process is deliberately shared among all viewers.
        cls._normalise_consumer_id(consumer_id)
        key, _channel, _profile = cls._stream_identity(channel_id, output_profile)
        with cls._lock:
            state = cls._streams.get(key)
            if state:
                state["last_access"] = time.monotonic()
            directory = str(state["directory"]) if state else cls.stream_directory(channel_id, output_profile)
            options=state.get('options') if state else None
        return cls._read_ready_playlist(directory,options)

    @classmethod
    def ensure_stream(
        cls,
        channel_id: object,
        upstream_url: object,
        output_profile: object = "copy",
        consumer_id: object = "",
        source_factory=None,
    ) -> Dict[str, object]:
        """Start or reuse a shared FFmpeg process and return its HLS playlist.

        FFmpeg output is intentionally discarded here; the host application's
        own logging covers process lifecycle.  Failures return a safe message
        instead of source URLs or command lines that may reveal LAN topology.
        """
        cls._normalise_consumer_id(consumer_id)
        key, channel, profile = cls._stream_identity(channel_id, output_profile)
        source = cls._validate_upstream_url(upstream_url)
        directory = cls.stream_directory(channel, profile)
        now = time.monotonic()

        with cls._lock:
            cls._cleanup_locked(now)
            state = cls._streams.get(key)
            if state and cls._is_alive(state):
                state["last_access"] = now
            else:
                if state:
                    cls._stop_state_locked(key, clear=True)
                try:
                    cleanup = None
                    cls._safe_clear_directory(directory)
                    os.makedirs(directory, mode=0o700, exist_ok=True)
                    if source_factory is not None:
                        source, cleanup = source_factory()
                        source = cls._validate_upstream_url(source)
                    process = subprocess.Popen(
                        cls._build_command(source, directory, profile),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                    )
                except Exception:
                    if cleanup is not None:
                        cleanup()
                    return {
                        "ret": "warning",
                        "status": 502,
                        "msg": "HLS 원본 연결 또는 인코더 시작에 실패했습니다. 유휴 튜너와 원본 주소를 확인하세요.",
                    }
                state = {
                    "process": process,
                    "directory": directory,
                    "channel_id": channel,
                    "output_profile": profile,
                    "last_access": now,
                    "cleanup": cleanup,
                    "options": dict(cls.TRANSCODE),
                }
                cls._streams[key] = state
                cls._start_janitor_locked()

        deadline = time.monotonic() + cls.START_TIMEOUT
        while time.monotonic() < deadline:
            playlist = cls._read_ready_playlist(directory,state.get('options'))
            if playlist:
                return {
                    "ret": "success",
                    "status": 200,
                    "playlist": playlist,
                    "channel_id": channel,
                    "output_profile": profile,
                }
            with cls._lock:
                current = cls._streams.get(key)
                if current is not state:
                    return {'ret':'warning','status':409,'msg':'재생 설정이 변경되었습니다. 다시 재생하세요.'}
                if current is None or not cls._is_alive(current):
                    cls._stop_state_locked(key, clear=True)
                    return {"ret": "warning", "status": 502, "msg": "HLS encoder stopped"}
            time.sleep(0.1)

        with cls._lock:
            if cls._streams.get(key) is state:
                cls._stop_state_locked(key, clear=True)
        return {"ret": "warning", "status": 504, "msg": "HLS encoder start timed out"}

    @classmethod
    def _start_janitor_locked(cls):
        if cls._janitor_stop is not None and not cls._janitor_stop.is_set():
            return
        stop = cls._janitor_stop = threading.Event()
        def reap():
            while not stop.wait(5):
                cls.cleanup()
        threading.Thread(target=reap, name='hdhomerun-hls-cleanup', daemon=True).start()

    @classmethod
    def _is_alive(cls, state: Dict[str, object]) -> bool:
        process = state.get("process")
        return process is not None and getattr(process, "poll")() is None

    @classmethod
    def _stop_state_locked(cls, key: str, clear: bool) -> None:
        state = cls._streams.pop(key, None)
        if not state:
            return
        process = state.get("process")
        if process is not None and getattr(process, "poll")() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
        cleanup = state.get('cleanup')
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass
        if clear:
            try:
                cls._safe_clear_directory(str(state.get("directory") or ""))
            except (OSError, ValueError):
                pass

    @classmethod
    def _cleanup_locked(cls, now: Optional[float] = None) -> None:
        timestamp = time.monotonic() if now is None else now
        stale = []
        for key, state in list(cls._streams.items()):
            last_access = float(state.get("last_access") or 0)
            if not cls._is_alive(state) or timestamp - last_access > cls.IDLE_TIMEOUT:
                stale.append(key)
        for key in stale:
            cls._stop_state_locked(key, clear=True)

    @classmethod
    def cleanup(cls) -> None:
        with cls._lock:
            cls._cleanup_locked()

    @classmethod
    def stop_stream(cls, channel_id: object, output_profile: object = "copy") -> None:
        key, _channel, _profile = cls._stream_identity(channel_id, output_profile)
        with cls._lock:
            cls._stop_state_locked(key, clear=True)

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            if cls._janitor_stop is not None:
                cls._janitor_stop.set()
            for key in list(cls._streams):
                cls._stop_state_locked(key, clear=True)

    @classmethod
    def stop_transcoded_streams(cls) -> int:
        with cls._lock:
            keys=[key for key,state in cls._streams.items() if state.get('output_profile') != 'copy']
            for key in keys:cls._stop_state_locked(key,clear=True)
            return len(keys)

    @classmethod
    def stream_options(cls, channel_id, output_profile):
        with cls._lock:
            state=cls._streams.get(cls.stream_key(channel_id,output_profile))
            return dict(state.get('options',cls.TRANSCODE) if state else cls.TRANSCODE)

    @classmethod
    def read_segment(
        cls,
        channel_id: object,
        filename: object,
        output_profile: object = "copy",
        consumer_id: object = "",
    ) -> Optional[bytes]:
        """Return one safe HLS segment, or ``None`` for absent/invalid names."""
        cls._normalise_consumer_id(consumer_id)
        cls._stream_identity(channel_id, output_profile)
        name = str(filename or "")
        if not cls.SEGMENT_RE.fullmatch(name):
            return None
        directory = cls.stream_directory(channel_id, output_profile)
        try:
            target = os.path.realpath(os.path.join(cls._assert_stream_directory(directory), name))
            root = cls._assert_stream_directory(directory)
            if not target.startswith(root + os.path.sep):
                return None
            with open(target, "rb") as handle:
                return handle.read()
        except (OSError, ValueError):
            return None


atexit.register(HLSManager.stop_all)
