# -*- coding: utf-8 -*-
"""Local, device-scoped channel storage for HDHomeRun FF.

This deliberately does not reuse TVHeadend tables. A user's HDHomeRun endpoint
and channel choices remain local to that FF installation.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def default_db_path() -> str:
    return os.environ.get('HDHOMERUN_FF_DB_PATH', '/data/db/hdhomerun_ff.db')


def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def has_channel_name(name: str) -> bool:
    """Distinguish broadcast names from scanner/device placeholders."""
    name = str(name or '').strip()
    return bool(name and name.lower() not in {'0', 'unknown', '(encrypted)', '(control)', '(no data)'}
                and not re.fullmatch(r'Program\s+\d+', name, re.IGNORECASE))


class ChannelStore:
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()

    @contextmanager
    def _connection(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_db(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hdhomerun_ff_channel (
                    channel_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    guide_number TEXT DEFAULT '',
                    name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '',
                    video_codec TEXT DEFAULT '',
                    audio_codec TEXT DEFAULT '',
                    group_name TEXT DEFAULT 'HDHomeRun',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selected INTEGER DEFAULT NULL,
                    raw_data TEXT NOT NULL DEFAULT '{}',
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                )
                """
            )
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(hdhomerun_ff_channel)')}
            if 'selected' not in columns:
                connection.execute('ALTER TABLE hdhomerun_ff_channel ADD COLUMN selected INTEGER DEFAULT NULL')
            connection.execute('''CREATE TABLE IF NOT EXISTS hdhomerun_ff_device (
                device_id TEXT PRIMARY KEY, profile TEXT NOT NULL DEFAULT '{}'
            )''')
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hdhomerun_ff_channel_device
                ON hdhomerun_ff_channel(device_id, enabled, guide_number)
                """
            )

    @staticmethod
    def _normalise_channel(row: dict[str, Any]) -> dict[str, Any]:
        source_url = str(row.get('source_url') or '').strip()
        channel_id = str(row.get('channel_id') or '').strip()
        name = str(row.get('name') or '').strip()
        device_id = str(row.get('device_id') or '').strip()
        enabled = 1 if bool(row.get('enabled', True)) else 0
        raw_data = row.get('raw_data') or {}
        if not all([channel_id, name, device_id]):
            raise ValueError('channel_id, device_id and name are required')
        # A preset scan discovers services but does not hold a tuner lease for
        # playback.  Such rows intentionally have no stream URL and must stay
        # disabled until a dedicated playback layer is implemented.
        if enabled and not source_url:
            raise ValueError('enabled channels require source_url')
        return {
            'channel_id': channel_id,
            'device_id': device_id,
            'guide_number': str(row.get('guide_number') or '').strip(),
            'name': name,
            'source_url': source_url,
            'logo_url': str(row.get('logo_url') or '').strip(),
            'video_codec': str(row.get('video_codec') or '').strip().lower(),
            'audio_codec': str(row.get('audio_codec') or '').strip().lower(),
            'group_name': str(row.get('group_name') or 'HDHomeRun').strip() or 'HDHomeRun',
            'enabled': enabled,
            'raw_data': json.dumps(raw_data, ensure_ascii=False, sort_keys=True),
        }

    @staticmethod
    def _row_source(raw_data: str) -> str:
        try:
            value = json.loads(raw_data or '{}')
        except (TypeError, ValueError):
            return ''
        return str(value.get('source') or '').strip()

    def replace_device_channels(
        self,
        device_id: str,
        channels: Iterable[dict[str, Any]],
        *,
        source_scope: str | None = None,
        require_disabled: bool = False,
    ) -> int:
        """Replace one device's channel rows, optionally scoped by source.

        ``source_scope`` prevents a native HDHomeRun lineup refresh from
        deleting discovery-only preset-scan rows (and vice versa).  The
        default retains the original full-device refresh behavior.
        """
        self.init_db()
        device_id = str(device_id or '').strip()
        if not device_id:
            raise ValueError('device_id is required')
        rows = [self._normalise_channel(row) for row in channels]
        if not rows:
            raise ValueError('refusing to replace a device lineup with zero channels')
        if any(row['device_id'] != device_id for row in rows):
            raise ValueError('all channels must belong to the selected device')
        if source_scope and any(self._row_source(row['raw_data']) != source_scope for row in rows):
            raise ValueError('all channels must use the requested source scope')
        if require_disabled and any(row['enabled'] for row in rows):
            raise ValueError('discovery-only channels must stay disabled')

        timestamp = now_str()
        incoming_ids = {row['channel_id'] for row in rows}
        with self._connection() as connection:
            existing = {}
            for current in connection.execute(
                'SELECT channel_id, enabled, raw_data FROM hdhomerun_ff_channel WHERE device_id = ?',
                (device_id,),
            ):
                if source_scope is None or self._row_source(current['raw_data']) == source_scope:
                    existing[current['channel_id']] = current['enabled']
            for row in rows:
                if require_disabled:
                    row['enabled'] = 0
                else:
                    row['enabled'] = existing.get(row['channel_id'], row['enabled'])
                connection.execute(
                    """
                    INSERT INTO hdhomerun_ff_channel (
                        channel_id, device_id, guide_number, name, source_url,
                        logo_url, video_codec, audio_codec, group_name, enabled,
                        raw_data, created_time, updated_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        device_id=excluded.device_id,
                        guide_number=excluded.guide_number,
                        name=excluded.name,
                        source_url=excluded.source_url,
                        logo_url=excluded.logo_url,
                        video_codec=excluded.video_codec,
                        audio_codec=excluded.audio_codec,
                        group_name=excluded.group_name,
                        enabled=excluded.enabled,
                        raw_data=excluded.raw_data,
                        updated_time=excluded.updated_time
                    """,
                    (
                        row['channel_id'], row['device_id'], row['guide_number'], row['name'],
                        row['source_url'], row['logo_url'], row['video_codec'], row['audio_codec'],
                        row['group_name'], row['enabled'], row['raw_data'], timestamp, timestamp,
                    ),
                )
            stale_ids = set(existing) - incoming_ids
            if stale_ids:
                placeholders = ','.join('?' for _ in stale_ids)
                connection.execute(
                    f'DELETE FROM hdhomerun_ff_channel WHERE device_id = ? AND channel_id IN ({placeholders})',
                    (device_id, *sorted(stale_ids)),
                )
        return len(rows)

    def replace_preset_scan_channels(self, device_id: str, channels: Iterable[dict[str, Any]]) -> int:
        """Store non-playable services discovered by an explicit preset scan."""

        return self.replace_device_channels(
            device_id,
            channels,
            source_scope='preset_scan',
            require_disabled=True,
        )

    def device_profile(self, device_id: str) -> dict[str, Any]:
        self.init_db()
        with self._connection() as connection:
            row = connection.execute('SELECT profile FROM hdhomerun_ff_device WHERE device_id = ?',
                                     (device_id,)).fetchone()
        return json.loads(row['profile']) if row else {}

    def update_device_profile(self, device_id: str, **updates: Any) -> None:
        self.init_db()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT profile FROM hdhomerun_ff_device WHERE device_id = ?',
                                     (device_id,)).fetchone()
            profile = json.loads(row['profile']) if row else {}
            profile.update(updates)
            connection.execute('''INSERT INTO hdhomerun_ff_device(device_id, profile) VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET profile=excluded.profile''',
                (device_id, json.dumps(profile, ensure_ascii=False)))

    def learned_preset(self, device_id: str) -> dict[str, Any]:
        """Use completed-scan RF pairs, including installations upgraded from 0.2."""
        pairs = set()
        profile = self.device_profile(device_id)
        for m in profile.get('learned_muxes', []):
            pairs.add((int(m['frequency_hz']), m['modulation']))
        if not pairs:
            for row in self.list_channels():
                raw = row['raw_data']
                if row['device_id'] == device_id and raw.get('source') == 'preset_scan':
                    modulation = {'8vsb':'VSB/8', 'qam256':'QAM/256', 'qam64':'QAM/64'}.get(raw.get('lock'))
                    if modulation:
                        pairs.add((int(raw['frequency_hz']), modulation))
        return {'version': 1, 'id': 'learned-device', 'name': '발견 주파수 빠른 재탐색',
                'country': 'KR', 'delivery_system': 'ATSC-T', 'source': 'cable',
                'muxes': [{'frequency_hz': f, 'modulation': m} for f,m in sorted(pairs)]}

    def playback_channels(self) -> list[dict[str, Any]]:
        """Selected raw services are tuned on demand, not assigned a stale URL."""
        result = []
        for channel in self.list_channels():
            raw = channel['raw_data']
            if raw.get('source') == 'preset_scan':
                if not channel['selected'] or set(raw.get('flags', [])) & {'control','no data','encrypted'}:
                    continue
                channel['enabled'] = True
            elif not channel['enabled']:
                continue
            result.append(channel)
        return result

    def playback_channel(self, channel_id: str) -> dict[str, Any] | None:
        return next((c for c in self.playback_channels() if c['channel_id'] == channel_id), None)

    def list_channels(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.init_db()
        where = 'WHERE enabled = 1' if enabled_only else ''
        with self._connection() as connection:
            rows = connection.execute(
                f'''SELECT channel_id, device_id, guide_number, name, source_url, logo_url,
                           video_codec, audio_codec, group_name, enabled, selected, raw_data,
                           created_time, updated_time
                    FROM hdhomerun_ff_channel {where}
                    ORDER BY CAST(guide_number AS REAL), guide_number, name, channel_id'''
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item['raw_data'] = json.loads(item['raw_data'] or '{}')
            except (TypeError, ValueError):
                item['raw_data'] = {}
            item['enabled'] = bool(item['enabled'])
            item['selected'] = self._selected(item)
            result.append(item)
        return result

    @staticmethod
    def _selected(item: dict[str, Any]) -> bool:
        if item.get('selected') is not None:
            return bool(item['selected'])
        if item['raw_data'].get('source') == 'preset_scan':
            return has_channel_name(item['name'])
        return bool(item['enabled'])

    def set_selected(self, channel_id: str, selected: bool) -> bool:
        """Save a user's choice without enabling a source that cannot play."""
        self.init_db()
        with self._connection() as connection:
            updated = connection.execute(
                '''UPDATE hdhomerun_ff_channel
                   SET selected = ?, enabled = CASE WHEN TRIM(source_url) != '' THEN ? ELSE 0 END,
                       updated_time = ? WHERE channel_id = ?''',
                (int(bool(selected)), int(bool(selected)), now_str(), str(channel_id or '').strip()),
            ).rowcount
        return updated == 1

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        channel_id = str(channel_id or '').strip()
        if not channel_id:
            return None
        self.init_db()
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM hdhomerun_ff_channel WHERE channel_id = ? AND enabled = 1',
                (channel_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item['raw_data'] = json.loads(item.get('raw_data') or '{}')
        except (TypeError, ValueError):
            item['raw_data'] = {}
        item['enabled'] = bool(item['enabled'])
        return item

    def set_enabled(self, channel_id: str, enabled: bool) -> bool:
        self.init_db()
        channel_id = str(channel_id or '').strip()
        if not channel_id:
            return False
        with self._connection() as connection:
            if enabled:
                channel = connection.execute(
                    'SELECT source_url FROM hdhomerun_ff_channel WHERE channel_id = ?',
                    (channel_id,),
                ).fetchone()
                if channel is not None and not str(channel['source_url'] or '').strip():
                    raise ValueError(
                        '재생 주소가 없는 프리셋 발견 결과는 활성화할 수 없습니다.'
                    )
            updated = connection.execute(
                'UPDATE hdhomerun_ff_channel SET enabled = ?, updated_time = ? WHERE channel_id = ?',
                (1 if enabled else 0, now_str(), channel_id),
            ).rowcount
        return updated == 1

    def summary(self) -> dict[str, int]:
        self.init_db()
        with self._connection() as connection:
            total = connection.execute('SELECT COUNT(*) FROM hdhomerun_ff_channel').fetchone()[0]
            enabled = connection.execute('SELECT COUNT(*) FROM hdhomerun_ff_channel WHERE enabled = 1').fetchone()[0]
        return {'total': int(total), 'enabled': int(enabled)}
