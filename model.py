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

try:
    from .channel_groups import effective_group, effective_logo, logo_url
    from .playlist import validate_profile_override
    from .channel_management import is_encrypted
except ImportError:
    from channel_groups import effective_group, effective_logo, logo_url
    from playlist import validate_profile_override
    from channel_management import is_encrypted


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
            if 'logo_override' not in columns:
                connection.execute("ALTER TABLE hdhomerun_ff_channel ADD COLUMN logo_override TEXT NOT NULL DEFAULT ''")
            if 'manual_group_name' not in columns:
                connection.execute("ALTER TABLE hdhomerun_ff_channel ADD COLUMN manual_group_name TEXT NOT NULL DEFAULT ''")
            if 'output_profile' not in columns:
                connection.execute("ALTER TABLE hdhomerun_ff_channel ADD COLUMN output_profile TEXT NOT NULL DEFAULT ''")
            if 'name_override' not in columns:
                connection.execute("ALTER TABLE hdhomerun_ff_channel ADD COLUMN name_override TEXT NOT NULL DEFAULT ''")
            for column in ('epg_channel_id', 'epg_channel_name'):
                if column not in columns:
                    connection.execute(f"ALTER TABLE hdhomerun_ff_channel ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            connection.execute('''CREATE TABLE IF NOT EXISTS hdhomerun_ff_device (
                device_id TEXT PRIMARY KEY, profile TEXT NOT NULL DEFAULT '{}'
            )''')
            connection.execute('''CREATE TABLE IF NOT EXISTS hdhomerun_ff_group_rules (
                id INTEGER PRIMARY KEY CHECK (id = 1), rules TEXT NOT NULL,
                updated_time TEXT NOT NULL
            )''')
            connection.execute('''CREATE TABLE IF NOT EXISTS hdhomerun_ff_group_profile (
                group_name TEXT PRIMARY KEY, output_profile TEXT NOT NULL
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
                        group_name=CASE WHEN TRIM(hdhomerun_ff_channel.group_name) NOT IN
                            ('', 'HDHomeRun', 'Preset scan (discovery only)', '그룹 없음')
                            THEN hdhomerun_ff_channel.group_name ELSE excluded.group_name END,
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
            if is_encrypted(channel):
                continue
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
                           video_codec, audio_codec, group_name, manual_group_name, output_profile, enabled, selected, logo_override, name_override, epg_channel_id, epg_channel_name, raw_data,
                           created_time, updated_time
                    FROM hdhomerun_ff_channel {where}
                    ORDER BY CAST(guide_number AS REAL), guide_number, name, channel_id'''
            ).fetchall()
        rules = self.group_rules()['rules']
        return [self._present_row(row, rules) for row in rows]

    def _present_row(self, row, rules):
        item = dict(row)
        try:
            item['raw_data'] = json.loads(item['raw_data'] or '{}')
            if not isinstance(item['raw_data'], dict):
                item['raw_data'] = {}
        except (TypeError, ValueError):
            item['raw_data'] = {}
        item['enabled'] = bool(item['enabled'])
        item['broadcast_name'] = item['name']
        item['name'] = item['name_override'] or item['epg_channel_name'] or item['broadcast_name']
        item['selected'] = self._selected(item)
        # Keep a known broadcaster's icon/group after a display-name change;
        # newly identified services can also use their manual/EPG name.
        group = effective_group(item['broadcast_name'], item['group_name'], rules)
        if group in {'', 'HDHomeRun', 'Preset scan (discovery only)', '그룹 없음'}:
            group = effective_group(item['name'], item['group_name'], rules)
        item['group_name'] = item['manual_group_name'] or group
        item['logo_url'] = (effective_logo(item['broadcast_name'], item['logo_override'], item['logo_url'], rules)
                            or effective_logo(item['name'], item['logo_override'], item['logo_url'], rules))
        return item

    def group_rules(self) -> dict[str, Any]:
        """Read the last successful snapshot; menu and playback never use HTTP."""
        self.init_db()
        with self._connection() as connection:
            row = connection.execute('SELECT rules, updated_time FROM hdhomerun_ff_group_rules WHERE id = 1').fetchone()
        if row:
            return {'rules': json.loads(row['rules']), 'updated_time': row['updated_time']}
        return {'rules': {}, 'updated_time': ''}

    def save_group_rules(self, rules: dict[str, Any]) -> None:
        if not isinstance(rules, dict) or not (any(rules.get(key) for key in ('master_exact', 'alias_exact', 'master_norm', 'alias_norm')) or any(rules.get('_logos', {}).values())):
            raise ValueError('빈 그룹 기준표로 기존 분류를 교체할 수 없습니다.')
        self.init_db()
        with self._connection() as connection:
            connection.execute('''INSERT INTO hdhomerun_ff_group_rules (id, rules, updated_time)
                VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET rules=excluded.rules, updated_time=excluded.updated_time''',
                (json.dumps(rules, ensure_ascii=False), now_str()))

    def set_logo(self, channel_id: str, value: str) -> bool:
        value = str(value or '').strip()
        url = logo_url(value, shared=False)
        if value and not url:
            raise ValueError('아이콘은 http 또는 https 주소를 입력하세요. 비워서 저장하면 자동 아이콘으로 돌아갑니다.')
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('UPDATE hdhomerun_ff_channel SET logo_override = ?, updated_time = ? WHERE channel_id = ?',
                                         (url, now_str(), str(channel_id or '').strip())).rowcount
        return changed == 1

    def set_name(self, channel_id: str, value: str) -> bool:
        """Keep a display override separate from the next scan's broadcast name."""
        value = str(value or '').strip()
        if len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('채널명은 줄바꿈 없이 256자 이내로 입력하세요. 비우면 연결된 EPG명 또는 방송명을 사용합니다.')
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('UPDATE hdhomerun_ff_channel SET name_override=?,updated_time=? WHERE channel_id=?',
                                         (value, now_str(), str(channel_id or '').strip())).rowcount
        return changed == 1

    def set_epg(self, channel_id: str, epg_id: str, epg_name: str, *, apply_name: bool = False) -> bool:
        epg_id, epg_name = str(epg_id or '').strip(), str(epg_name or '').strip()
        if any(len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value) for value in (epg_id, epg_name)):
            raise ValueError('EPG 채널 ID·이름 형식을 확인하세요.')
        if bool(epg_id) != bool(epg_name):
            raise ValueError('EPG 채널 ID와 이름이 함께 필요합니다.')
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('''UPDATE hdhomerun_ff_channel SET epg_channel_id=?,epg_channel_name=?,
                name_override=CASE WHEN ? THEN '' ELSE name_override END,updated_time=? WHERE channel_id=?''',
                (epg_id, epg_name, int(apply_name), now_str(), str(channel_id or '').strip())).rowcount
        return changed == 1

    def apply_epg_matches(self, matches: dict[str, dict[str, Any]]) -> int:
        """Apply exact matches atomically without replacing manual display names."""
        self.init_db()
        count = 0
        with self._connection() as connection:
            for channel_id, entry in matches.items():
                count += connection.execute('''UPDATE hdhomerun_ff_channel SET epg_channel_id=?,epg_channel_name=?,updated_time=?
                    WHERE channel_id=? AND epg_channel_id='' ''',
                    (entry['id'], entry['name'], now_str(), channel_id)).rowcount
        return count

    def delete_channel(self, channel_id: str) -> bool:
        """Remove the saved row only; the next scan may discover it again."""
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('DELETE FROM hdhomerun_ff_channel WHERE channel_id=?',
                                         (str(channel_id or '').strip(),)).rowcount
        return changed == 1

    def delete_encrypted_channels(self, device_id: str) -> int:
        """Delete only explicitly encrypted rows of the current device."""
        device_id = str(device_id or '').strip()
        if not device_id:
            raise ValueError('먼저 사용할 장비를 확인하세요.')
        self.init_db()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            ids = []
            for row in connection.execute('SELECT channel_id,raw_data FROM hdhomerun_ff_channel WHERE device_id=?', (device_id,)):
                try:
                    raw = json.loads(row['raw_data'] or '{}')
                except (TypeError, ValueError):
                    continue
                if isinstance(raw, dict) and is_encrypted({'raw_data': raw}):
                    ids.append((row['channel_id'],))
            connection.executemany('DELETE FROM hdhomerun_ff_channel WHERE channel_id=?', ids)
        return len(ids)

    def set_group(self, channel_id: str, value: str) -> bool:
        value = str(value or '').strip()
        if len(value) > 100 or any(c in value for c in '\r\n\x00'):
            raise ValueError('그룹명은 줄바꿈 없이 100자 이내로 입력하세요.')
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('UPDATE hdhomerun_ff_channel SET manual_group_name = ?, updated_time = ? WHERE channel_id = ?',
                                         (value, now_str(), str(channel_id or '').strip())).rowcount
        return changed == 1

    def group_profiles(self) -> dict[str, str]:
        self.init_db()
        with self._connection() as connection:
            return dict(connection.execute('SELECT group_name,output_profile FROM hdhomerun_ff_group_profile'))

    def set_group_profile(self, group_name: str, profile: str) -> None:
        group_name = str(group_name or '').strip()
        if not group_name or len(group_name) > 100 or any(c in group_name for c in '\r\n\x00'):
            raise ValueError('올바른 그룹명을 입력하세요.')
        profile = validate_profile_override(profile)
        self.init_db()
        with self._connection() as connection:
            if profile:
                connection.execute('''INSERT INTO hdhomerun_ff_group_profile (group_name,output_profile)
                    VALUES (?,?) ON CONFLICT(group_name) DO UPDATE SET output_profile=excluded.output_profile''',
                    (group_name, profile))
            else:
                connection.execute('DELETE FROM hdhomerun_ff_group_profile WHERE group_name=?', (group_name,))

    def set_channel_profile(self, channel_id: str, profile: str) -> bool:
        profile = validate_profile_override(profile)
        self.init_db()
        with self._connection() as connection:
            changed = connection.execute('UPDATE hdhomerun_ff_channel SET output_profile=?,updated_time=? WHERE channel_id=?',
                                         (profile, now_str(), str(channel_id or '').strip())).rowcount
        return changed == 1

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
        """Get a saved row for management; playback uses playback_channel()."""
        channel_id = str(channel_id or '').strip()
        if not channel_id:
            return None
        self.init_db()
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM hdhomerun_ff_channel WHERE channel_id = ?',
                (channel_id,),
            ).fetchone()
        if row is None:
            return None
        return self._present_row(row, self.group_rules()['rules'])

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
