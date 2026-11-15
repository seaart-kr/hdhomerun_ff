# -*- coding: utf-8 -*-
"""Local, device-scoped channel storage for HDHomeRun FF.

This deliberately does not reuse TVHeadend tables. A user's HDHomeRun endpoint
and channel choices remain local to that FF installation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def default_db_path() -> str:
    return os.environ.get('HDHOMERUN_FF_DB_PATH', '/data/db/hdhomerun_ff.db')


def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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
                    raw_data TEXT NOT NULL DEFAULT '{}',
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                )
                """
            )
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

    def list_channels(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.init_db()
        where = 'WHERE enabled = 1' if enabled_only else ''
        with self._connection() as connection:
            rows = connection.execute(
                f'''SELECT channel_id, device_id, guide_number, name, source_url, logo_url,
                           video_codec, audio_codec, group_name, enabled, raw_data,
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
            result.append(item)
        return result

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
