# -*- coding: utf-8 -*-
"""M3U construction without leaking a user's HDHomeRun LAN URLs."""
from __future__ import annotations

from urllib.parse import urlencode


SUPPORTED_OUTPUT_PROFILES = {'copy', 'mobile_cpu'}


def normalise_output_profile(value: str | None) -> str:
    value = str(value or '').strip().lower()
    return value if value in SUPPORTED_OUTPUT_PROFILES else 'copy'


def build_shyni_m3u(channels, base_url: str, package_name: str, api_key: str = '', output_profile: str = 'copy', consumer_id: str = '') -> str:
    base_url = str(base_url or '').rstrip('/')
    package_name = str(package_name or '').strip().strip('/')
    profile = normalise_output_profile(output_profile)
    lines = ['#EXTM3U']
    for channel in channels:
        if not channel.get('enabled', True):
            continue
        channel_id = str(channel.get('channel_id') or '').strip()
        name = str(channel.get('name') or '').strip()
        if not channel_id or not name:
            continue
        attrs = [f'tvg-id="{channel_id}"', f'tvg-name="{name}"']
        guide_number = str(channel.get('guide_number') or '').strip()
        if guide_number:
            attrs.append(f'tvg-chno="{guide_number}"')
        group_name = str(channel.get('group_name') or '').strip()
        if group_name:
            attrs.append(f'group-title="{group_name}"')
        logo_url = str(channel.get('logo_url') or '').strip()
        if logo_url:
            attrs.append(f'tvg-logo="{logo_url}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        query = [('i', channel_id), ('p', profile)]
        if api_key:
            query.append(('apikey', str(api_key)))
        if consumer_id:
            query.append(('client', str(consumer_id)))
        lines.append(f'{base_url}/{package_name}/api/hls.m3u8?{urlencode(query)}')
    return '\n'.join(lines) + '\n'
