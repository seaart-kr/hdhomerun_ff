"""Scoped VAAPI driver selection and a real, cached encoder preflight."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEVICE = '/dev/dri/renderD128'
MANAGED_I965 = '/data/hdhomerun_ff/vaapi/i965'
DRIVERS = ('auto', 'i965', 'iHD')
_lock = threading.Lock()
_cache = {}


def validate_driver(driver):
    if driver not in DRIVERS:
        raise ValueError('VAAPI 드라이버를 확인하세요.')
    return driver


def environment(driver):
    """Apply driver overrides only to this plugin's FFmpeg subprocess."""
    validate_driver(driver)
    env = dict(os.environ)
    if driver != 'auto':
        env['LIBVA_DRIVER_NAME'] = driver
        if driver == 'i965' and Path(MANAGED_I965, 'i965_drv_video.so').is_file():
            env['LIBVA_DRIVERS_PATH'] = MANAGED_I965
    return env


def core_limited_command(command):
    # Avoid preexec_fn in the threaded FF server and prevent failed drivers
    # from producing large NAS core dumps. exec preserves process signalling.
    return [sys.executable, '-c',
            'import os,resource,sys; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.execv(sys.argv[1],sys.argv[1:])',
            *command]


def _key(driver):
    env = environment(driver)
    ffmpeg = shutil.which('ffmpeg') or ''
    paths = [DEVICE, ffmpeg, str(Path(MANAGED_I965, 'i965_drv_video.so'))]
    stats = []
    for path in paths:
        try:
            stat = os.stat(path)
            stats.append((path, stat.st_mtime_ns, stat.st_size, stat.st_rdev))
        except OSError:
            stats.append((path, None))
    return (driver, env.get('LIBVA_DRIVER_NAME'), env.get('LIBVA_DRIVERS_PATH'), tuple(stats))


def status(driver):
    key = _key(driver)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < (300 if cached[1]['available'] else 30):
            return dict(cached[1])
    return {'available': False, 'checked': False, 'driver': driver,
            'message': '미검사 · VAAPI 실제 변환 검사를 실행하세요.'}


def probe(driver='auto', *, force=False):
    key = _key(driver)
    with _lock:
        cached = _cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < (300 if cached[1]['available'] else 30):
            return dict(cached[1])
        result = {'available': False, 'checked': True, 'driver': driver,
                  'device': DEVICE, 'checked_at': datetime.now(timezone.utc).isoformat(),
                  'managed_driver': environment(driver).get('LIBVA_DRIVERS_PATH') == MANAGED_I965}
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            result['message'] = 'FFmpeg를 찾지 못했습니다.'
        elif not os.access(DEVICE, os.R_OK | os.W_OK):
            result['message'] = 'GPU 장치에 접근할 수 없습니다. /dev/dri 연결과 권한을 확인하세요.'
        else:
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                       '-init_hw_device', 'vaapi=va:' + DEVICE, '-filter_hw_device', 'va',
                       '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
                       '-frames:v', '30', '-vf', 'format=nv12,hwupload',
                       '-c:v', 'h264_vaapi', '-profile:v', 'main', '-b:v', '3M',
                       '-bf', '0', '-an', '-f', 'null', '-']
            try:
                started = time.monotonic()
                completed = subprocess.run(core_limited_command(command), env=environment(driver),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, timeout=20, check=False)
                result['elapsed_seconds'] = round(time.monotonic() - started, 2)
                result['available'] = completed.returncode == 0
                result['message'] = ('VAAPI H.264 실제 변환 검사 통과 · 720p 30프레임' if result['available'] else
                                     'VAAPI 변환 검사 실패 · 드라이버를 확인하거나 CPU 변환을 선택하세요.')
                if not result['available']:
                    result['detail'] = (completed.stderr or '')[-1800:]
            except (OSError, subprocess.SubprocessError):
                result['message'] = 'VAAPI 검사 실행 실패 또는 시간 초과 · CPU 변환을 사용할 수 있습니다.'
        _cache[key] = (time.monotonic(), dict(result))
        return result
