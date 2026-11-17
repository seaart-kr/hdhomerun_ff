# -*- coding: utf-8 -*-
"""FFmpeg capability inspection used by the administrator-only diagnostics page."""
from __future__ import annotations

import shutil
import subprocess


def ffmpeg_capabilities(timeout: int = 10) -> dict:
    ffmpeg = shutil.which('ffmpeg')
    result = {
        'ffmpeg_path': ffmpeg or '',
        'available': bool(ffmpeg),
        'encoders': {'libx264': False, 'h264_vaapi': False, 'h264_nvenc': False, 'aac': False},
        'note': '',
    }
    if not ffmpeg:
        result['note'] = 'FFmpeg를 찾지 못했습니다.'
        return result
    try:
        completed = subprocess.run(
            [ffmpeg, '-hide_banner', '-encoders'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        text = completed.stdout or ''
        for name in result['encoders']:
            result['encoders'][name] = name in text
        result['note'] = 'VAAPI·NVIDIA 하드웨어 변환은 각각 실제 변환 검사를 통과해야 사용할 수 있습니다.'
    except (OSError, subprocess.SubprocessError) as exc:
        result['note'] = f'FFmpeg 진단 실패: {exc}'
    return result
