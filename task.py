# -*- coding: utf-8 -*-
"""Small orchestration helpers shared by FF menu and API routes."""
from __future__ import annotations

from hashlib import sha256
from typing import Any

try:
    from .hdhomerun import DeviceSnapshot
    from .hdhr_control import PresetMuxScan
except ImportError:  # Unit tests import modules directly from the plugin root.
    from hdhomerun import DeviceSnapshot  # type: ignore
    from hdhr_control import PresetMuxScan  # type: ignore


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


def preset_scan_channel_rows(device_id: str, scans: list[PresetMuxScan] | tuple[PresetMuxScan, ...]) -> list[dict[str, Any]]:
    """Convert preset-scan services to disabled discovery-only channel rows.

    A frequency scan releases its tuner before this function returns.  It
    therefore must never create an enabled M3U source URL: playback needs a
    separate, long-lived tuner-lease implementation.
    """

    device_id = str(device_id or '').strip().lower()
    if not device_id:
        raise ValueError('device_id is required')
    rows: list[dict[str, Any]] = []
    for scan in scans:
        frequency_hz = int(scan.target.frequency_hz)
        for service in scan.services:
            identity = f'{device_id}|{frequency_hz}|{scan.transport_stream_id}|{service.program_number}'
            digest = sha256(identity.encode('utf-8')).hexdigest()[:16]
            virtual_channel = str(service.virtual_channel or '').strip()
            guide_number = virtual_channel or f'{frequency_hz / 1_000_000:.3f}:{service.program_number}'
            rows.append({
                'channel_id': f'hdhrscan:{device_id}:{frequency_hz}:{service.program_number}:{digest}',
                'device_id': device_id,
                'guide_number': guide_number,
                'name': str(service.name or f'Program {service.program_number}').strip(),
                'source_url': '',
                'video_codec': '',
                'audio_codec': '',
                'group_name': 'Preset scan (discovery only)',
                'enabled': False,
                'raw_data': {
                    'source': 'preset_scan',
                    'frequency_hz': frequency_hz,
                    'modulation_expected': scan.target.modulation,
                    'lock': scan.status.lock,
                    'signal_strength': scan.status.signal_strength,
                    'symbol_quality': scan.status.symbol_quality,
                    'transport_stream_id': scan.transport_stream_id,
                    'original_network_id': scan.original_network_id,
                    'program_number': service.program_number,
                    'flags': list(service.flags),
                    'streaminfo_line': service.raw,
                },
            })
    return rows
