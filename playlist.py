# -*- coding: utf-8 -*-
"""M3U construction without leaking a user's HDHomeRun LAN URLs."""
from __future__ import annotations

from urllib.parse import urlencode

try:
    from .channel_groups import group_sort_key
except ImportError:
    from channel_groups import group_sort_key


SUPPORTED_OUTPUT_PROFILES = {'copy', 'copy_video_aac', 'mobile_cpu', 'mobile_1080', 'mobile_vaapi', 'mobile_nvenc'}
OUTPUT_PROFILE_LABELS = {
    'copy': '원본 유지 · 영상/음성 패스',
    'copy_video_aac': '영상 원본 · 음성 AAC',
    'mobile_cpu': 'H.264/AAC · CPU 변환',
    'mobile_vaapi': 'H.264/AAC · VAAPI 변환',
    'mobile_nvenc': 'H.264/AAC · NVIDIA 변환 (시험)',
    'mobile_1080': 'H.264/AAC · 원본 해상도 CPU 변환',
}


def validate_profile_override(value):
    value = str(value or '').strip()
    if value and value not in SUPPORTED_OUTPUT_PROFILES:
        raise ValueError('지원하지 않는 채널/그룹 출력 방식입니다.')
    return value


def resolve_output_profile(channel, group_profiles, default_profile):
    """Channel override, then the channel's effective group, then default."""
    for value in (channel.get('output_profile'), group_profiles.get(channel.get('group_name')),
                  default_profile):
        if value in SUPPORTED_OUTPUT_PROFILES:
            return value
    return 'copy'


def normalise_output_profile(value: str | None) -> str:
    value = str(value or '').strip().lower()
    return value if value in SUPPORTED_OUTPUT_PROFILES else 'copy'


def _m3u_text(value: object) -> str:
    """Return a one-line, quoted-attribute-safe M3U display value."""
    return str(value or '').replace('\r', ' ').replace('\n', ' ').replace('"', "'").strip()


def build_shyni_m3u(channels, base_url: str, package_name: str, api_key: str = '', output_profile: str = 'copy', consumer_id: str = '', profile_overrides: dict | None = None, epg_url: str = '') -> str:
    base_url = str(base_url or '').rstrip('/')
    package_name = str(package_name or '').strip().strip('/')
    profile = normalise_output_profile(output_profile)
    lines = ['#EXTM3U' + (f' x-tvg-url="{_m3u_text(epg_url)}"' if epg_url else '')]
    for channel in sorted(channels, key=group_sort_key):
        if not channel.get('enabled', True):
            continue
        channel_id = str(channel.get('channel_id') or '').strip()
        name = _m3u_text(channel.get('name'))
        if not channel_id or not name:
            continue
        epg_id = _m3u_text(channel.get('epg_channel_id') or channel_id)
        attrs = [f'tvg-id="{epg_id}"', f'tvg-name="{name}"']
        guide_number = _m3u_text(channel.get('guide_number'))
        if guide_number:
            attrs.append(f'tvg-chno="{guide_number}"')
        group_name = _m3u_text(channel.get('group_name'))
        if group_name:
            attrs.append(f'group-title="{group_name}"')
        logo_url = _m3u_text(channel.get('logo_url'))
        if logo_url:
            attrs.append(f'tvg-logo="{logo_url}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        channel_profile = normalise_output_profile((profile_overrides or {}).get(channel_id) or profile)
        query = [('i', channel_id), ('p', channel_profile)]
        if api_key:
            query.append(('apikey', str(api_key)))
        if consumer_id:
            query.append(('client', str(consumer_id)))
        lines.append(f'{base_url}/{package_name}/api/hls.m3u8?{urlencode(query)}')
    return '\n'.join(lines) + '\n'
