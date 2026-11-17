"""Tuner-free NVIDIA decode/CUDA/encode preflight for optional playback."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

try:
    from .vaapi import core_limited_command
except ImportError:
    from vaapi import core_limited_command

_lock = threading.Lock()
_cache = {}


def input_arguments():
    return ['-init_hw_device', 'cuda=cu:0', '-filter_hw_device', 'cu',
            '-hwaccel', 'cuda', '-hwaccel_device', 'cu',
            '-hwaccel_output_format', 'cuda', '-extra_hw_frames', '16']


def video_filter(options):
    filters = ['yadif_cuda=mode=send_frame:parity=auto:deint=interlaced']
    if options['height']:
        height = options['height']
        width = {480:854, 720:1280, 1080:1920}[height]
        # FFmpeg 4.4 scale_cuda lacks a format option. CUDA MPEG-2 decoding
        # already provides NV12; keep frames on the GPU through the encoder.
        filters.append(f"scale_cuda=w='min({width},iw)':h='min({height},ih)'"
                       ':force_original_aspect_ratio=decrease:force_divisible_by=2')
    if options['fps'] != 'source':
        filters.append('fps=' + {'30':'30000/1001', '60':'60000/1001'}.get(options['fps'], options['fps']))
    return ','.join(filters)


def _key():
    paths = [shutil.which('ffmpeg') or '', '/dev/nvidia0', '/dev/nvidiactl',
             '/proc/driver/nvidia/version']
    stats = []
    for path in paths:
        try:
            stat = os.stat(path)
            stats.append((path, stat.st_mtime_ns, stat.st_size, stat.st_rdev))
        except OSError:
            stats.append((path, None))
    return (tuple(stats), os.environ.get('NVIDIA_VISIBLE_DEVICES'),
            os.environ.get('NVIDIA_DRIVER_CAPABILITIES'), os.environ.get('CUDA_VISIBLE_DEVICES'))


def _cached(key):
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < (300 if cached[1]['available'] else 30):
        return dict(cached[1])


def status():
    with _lock:
        return _cached(_key()) or {
            'available': False, 'checked': False,
            'message': '미검사 · NVIDIA 실제 변환 검사를 통과하면 출력 목록에 표시됩니다.'}


def probe(*, force=False):
    key = _key()
    with _lock:
        cached = _cached(key)
        if cached and not force:
            return cached
        result = {'available': False, 'checked': True,
                  'checked_at': datetime.now(timezone.utc).isoformat()}
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            result['message'] = 'FFmpeg를 찾지 못했습니다.'
        else:
            started = time.monotonic()
            try:
                sample = subprocess.run(core_limited_command([
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=30',
                    '-frames:v', '30', '-threads', '2', '-flags', '+ilme+ildct', '-top', '1',
                    '-c:v', 'mpeg2video', '-b:v', '8M', '-f', 'mpegts', 'pipe:1']),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=10, check=True)
                completed = subprocess.run(core_limited_command([
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    *input_arguments(), '-threads', '2', '-filter_threads', '2',
                    '-f', 'mpegts', '-i', 'pipe:0', '-frames:v', '30',
                    '-vf', video_filter({'height':720, 'fps':'30'}),
                    '-c:v', 'h264_nvenc', '-preset', 'fast', '-profile:v', 'main',
                    '-b:v', '3M', '-bf', '0', '-an', '-f', 'h264', 'pipe:1']),
                    input=sample.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=20, check=False)
                result['available'] = completed.returncode == 0 and bool(completed.stdout)
                result['message'] = ('NVIDIA 실제 변환 검사 통과 · NVDEC·CUDA 전처리·NVENC H.264' if result['available'] else
                                     'NVIDIA 변환 검사 실패 · GPU 연결·드라이버·FFmpeg 지원을 확인하세요.')
                if not result['available']:
                    result['detail'] = (completed.stderr or b'').decode('utf-8', 'replace')[-1800:]
            except subprocess.CalledProcessError as exc:
                result['message'] = '검사용 MPEG-2 영상 생성에 실패했습니다.'
                result['detail'] = (exc.stderr or b'').decode('utf-8', 'replace')[-1800:]
            except (OSError, subprocess.SubprocessError):
                result['message'] = 'NVIDIA 검사 실행 실패 또는 시간 초과 · CPU 변환을 사용할 수 있습니다.'
            result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        _cache[key] = (time.monotonic(), dict(result))
        return result
