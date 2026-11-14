# -*- coding: utf-8 -*-
"""Small orchestration helpers shared by FF menu and API routes."""
from __future__ import annotations

from typing import Any

from .hdhomerun import DeviceSnapshot


def snapshot_channel_rows(snapshot: DeviceSnapshot) -> list[dict[str, Any]]:
    """Convert validated device lineup data into local channel-store rows."""
    rows = []
    for channel in snapshot.lineup.channels:
        rows.append({
            'channel_id': channel.channel_id,
            'device_id': snapshot.device.device_id,
            'guide_number': channel.guide_number,
            'name': channel.guide_name,
            'source_url': channel.stream_url,
            'video_codec': channel.video_codec,
            'audio_codec': channel.audio_codec,
            'group_name': 'HDHomeRun',
            'enabled': True,
            'raw_data': {
                'source': 'native_lineup',
                'guide_number': channel.guide_number,
            },
        })
    return rows


def device_summary(snapshot: DeviceSnapshot) -> dict[str, Any]:
    """Return only non-secret device state suitable for the administrator UI."""
    device = snapshot.device
    return {
        'device_id': device.device_id,
        'friendly_name': device.friendly_name,
        'model_number': device.model_number,
        'firmware_version': device.firmware_version,
        'tuner_count': device.tuner_count,
        'lineup_count': len(snapshot.lineup.channels),
        'skipped_lineup_entries': snapshot.lineup.skipped_entries,
    }
