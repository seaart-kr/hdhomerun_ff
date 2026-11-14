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
        'encoders': {'libx264': False, 'h264_vaapi': False, 'aac': False},
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
        result['note'] = '실제 VAAPI 인코딩은 별도 진단을 통과한 경우에만 활성화하세요.'
    except (OSError, subprocess.SubprocessError) as exc:
        result['note'] = f'FFmpeg 진단 실패: {exc}'
    return result
