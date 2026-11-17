"""Reuse TVH M3U's public, read-only channel-name grouping rules."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

RULES_URL = 'https://ff.aha3011.mywire.org/ff_tvh_sheet_write/api/basic/match_rules'
# Distributed with tvh_m3u_plugin for read-only access to the shared catalog.
PUBLIC_READ_APIKEY = 'TLP1TOGA4P'
BUCKETS = ('master_exact', 'alias_exact', 'master_norm', 'alias_norm')
DEFAULT_GROUPS = ('', 'HDHomeRun', 'Preset scan (discovery only)', '그룹 없음')
GROUP_ORDER = (
    '지상파', '종합편성', '연예/오락', '드라마', '영화', '스포츠',
    '뉴스/경제', '음악', '어린이/만화', '교양/다큐', '교육', '여성/패션',
    '레저', '공공', '종교', '홈쇼핑', '해외위성', '라디오', '기타', '그룹 없음',
)
GITHUB_ASSETS = 'https://raw.githubusercontent.com/seaart-kr/tvh_m3u_plugin/main/docs/assets/'


def logo_url(value, *, shared=True):
    """Turn shared FF asset paths into standalone GitHub URLs."""
    text = str(value or '').strip()
    if not text or len(text) > 4096 or any(c in text for c in '\r\n\x00'):
        return ''
    try:
        parts = urllib.parse.urlsplit(text)
        for prefix in ('/customlogo/', '/tvh_m3u_plugin/docs/assets/', '/blob/main/docs/assets/'):
            if prefix in parts.path:
                if not shared and not prefix.startswith('/blob/'):
                    continue
                # A GitHub blob path is only meaningful for the shared repo.
                if prefix.startswith('/blob/') and not (parts.hostname == 'github.com' and parts.path.startswith('/seaart-kr/tvh_m3u_plugin/')):
                    continue
                relative = urllib.parse.unquote(parts.path.split(prefix, 1)[1])
                if not relative or any(p in ('', '.', '..') for p in relative.split('/')) or '\\' in relative or any(c in relative for c in '\r\n\x00'):
                    return ''
                return GITHUB_ASSETS + urllib.parse.quote(relative, safe='/')
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return ''
        return text
    except ValueError:
        return ''


def normalize_name(value):
    return re.sub(r'[\s\-_./()\[\]{}]+', '', str(value or '').strip().upper())


def normalize_group(value):
    name = str(value or '').strip()
    compact = name.replace(' ', '')
    for group, aliases in {
        '종합편성': ('종편', '종합편성', '종합편성/종편'),
        '뉴스/경제': ('뉴스', '경제', '뉴스/경제'),
        '어린이/만화': ('어린이', '어린이/애니', '애니', '만화', '어린이/만화'),
        '교양/다큐': ('교양', '다큐', '교양/다큐'),
    }.items():
        if compact in aliases:
            return group
    return name


def parse_rules(payload):
    if not isinstance(payload, dict) or payload.get('ret') != 'success':
        raise ValueError('공용 채널 기준표를 불러오지 못했습니다. 기존 그룹을 유지합니다.')
    source = payload.get('rules')
    if not isinstance(source, dict):
        raise ValueError('공용 채널 기준표 형식을 확인하세요. 기존 그룹을 유지합니다.')
    rules = {bucket: {} for bucket in BUCKETS}
    logos = {bucket: {} for bucket in BUCKETS}
    for bucket in BUCKETS:
        entries = source.get(bucket) or {}
        if not isinstance(entries, dict):
            raise ValueError('공용 채널 기준표 형식을 확인하세요. 기존 그룹을 유지합니다.')
        for name, item in entries.items():
            if not isinstance(item, dict):
                continue
            group = normalize_group(item.get('group_name') or item.get('group_category'))
            if '미사용' in group or '미사용' in str(item.get('receive_category') or ''):
                continue
            key = normalize_name(name) if bucket.endswith('_norm') else str(name).strip().lower()
            if not key or len(key) > 256:
                continue
            if group and len(group) <= 100 and not any(c in group for c in '\r\n\x00'):
                rules[bucket][key] = group
            for field in ('custom_logo_url', 'provider_logo_url', 'final_logo_url'):
                url = logo_url(item.get(field))
                if url:
                    logos[bucket][key] = url
                    break
    if any(logos.values()):
        rules['_logos'] = logos
    if not any(rules[bucket] for bucket in BUCKETS) and not any(logos.values()):
        raise ValueError('분류 가능한 공용 채널 기준표가 없습니다. 기존 그룹을 유지합니다.')
    return rules


def _download_catalog(endpoint, api_key):
    url = RULES_URL.rsplit('/', 1)[0] + '/' + endpoint + '?' + urllib.parse.urlencode({'apikey': api_key})
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('공용 채널 기준표가 너무 큽니다. 기존 그룹을 유지합니다.')
        return json.loads(data)
    except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('공용 채널 기준표 연결에 실패했습니다. 기존 그룹을 유지하며 잠시 후 다시 시도할 수 있습니다.') from exc


def parse_icon_catalog(payload):
    """The shared match endpoint has no icons; reuse its separate logo tables."""
    if not isinstance(payload, dict) or payload.get('ret', 'success') != 'success':
        raise ValueError('공용 아이콘 목록을 불러오지 못했습니다. 기존 기준표를 유지합니다.')
    tables = payload.get('tables', payload)
    if not isinstance(tables, dict) or not any(key in tables for key in ('custom_logo', 'provider_logo_local')):
        raise ValueError('공용 아이콘 목록 형식을 확인하세요. 기존 기준표를 유지합니다.')

    def rows(table):
        value = tables.get(table, [])
        if not isinstance(value, list):
            raise ValueError('공용 아이콘 목록 형식을 확인하세요. 기존 기준표를 유지합니다.')
        return [row for row in value if isinstance(row, dict)]

    def first(row, *keys):
        return next((str(row[key]).strip() for key in keys if row.get(key) is not None and str(row[key]).strip()), '')

    def aliases(value):
        return [name.strip() for name in re.split(r'[|\r\n]+', value) if name.strip()]

    names_by_id = {}
    ids_by_name = {}
    for row in rows('channel_master'):
        identity = first(row, 'id', 'channel_master_id', 'channel_id', 'master_id')
        name = first(row, 'standard_name', 'channel_name', 'name', 'title')
        if identity and name:
            names_by_id.setdefault(identity, []).append((name, False))
            ids_by_name[name.lower()] = identity
            for alias in aliases(first(row, 'e_aka', 'aka_names', 'aka_name', 'aliases', 'aka')):
                names_by_id[identity].append((alias, True))
    for row in rows('channel_alias'):
        identity = first(row, 'channel_master_id', 'channel_id', 'matched_channel_id', 'master_id')
        identity = identity or ids_by_name.get(first(row, 'standard_name', 'channel_name', 'master_name').lower(), '')
        if identity:
            for alias in aliases(first(row, 'alias_name', 'aka_name', 'aka_names', 'aka', 'alias', 'name', 'e_aka')):
                names_by_id.setdefault(identity, []).append((alias, True))

    logos = {bucket: {} for bucket in BUCKETS}
    scores = {bucket: {} for bucket in BUCKETS}
    for table in ('custom_logo', 'provider_logo_local'):
        for row in rows(table):
            provider = first(row, 'provider', 'provider_name', 'provider_type', 'logo_type', 'source_type').lower()
            priority = 0 if table == 'custom_logo' or any(k in provider for k in ('custom', 'manual', 'user')) else {'kt':1,'wavve':2,'tving':3,'sk':4}.get(provider, 5)
            raw_url = first(row, 'logo_url_template', 'custom_logo_url', 'logo_url', 'preview_url', 'local_url', 'stored_path', 'path', 'url')
            filename = first(row, 'stored_filename', 'filename', 'logo_file', 'file_name')
            url = logo_url(raw_url) or logo_url('/customlogo/' + filename if filename else '') or logo_url(first(row, 'original_logo_url', 'origin_logo_url'))
            if not url:
                continue
            identity = first(row, 'matched_channel_id', 'channel_master_id', 'channel_id', 'master_id', 'cm_id')
            names = list(names_by_id.get(identity, []))
            names.append((first(row, 'standard_name', 'channel_name', 'name'), False))
            names.append((first(row, 'source_channel_name', 'provider_channel_name', 'original_name', 'source_name'), False))
            for name, alias in names:
                if not name or len(name) > 256:
                    continue
                prefix = 'alias' if alias else 'master'
                for bucket, key in ((prefix+'_exact', name.lower()), (prefix+'_norm', normalize_name(name))):
                    if key and priority <= scores[bucket].get(key, 99):
                        scores[bucket][key] = priority
                        logos[bucket][key] = url
    if not any(logos.values()):
        raise ValueError('공용 아이콘 목록에 사용할 이미지가 없습니다. 기존 기준표를 유지합니다.')
    return logos


def fetch_rules():
    api_key = os.environ.get('HDHOMERUN_FF_GROUP_APIKEY', PUBLIC_READ_APIKEY).strip()
    rules = parse_rules(_download_catalog('match_rules', api_key))
    logos = parse_icon_catalog(_download_catalog('db_dump', api_key))
    for bucket in BUCKETS:
        logos[bucket] = {**rules.get('_logos', {}).get(bucket, {}), **logos[bucket]}
    rules['_logos'] = logos
    return rules


def effective_group(name, stored_group, rules):
    stored_group = str(stored_group or '').strip()
    if stored_group not in DEFAULT_GROUPS:
        return stored_group
    exact, normalized = str(name or '').strip().lower(), normalize_name(name)
    for bucket in BUCKETS:
        group = rules.get(bucket, {}).get(normalized if bucket.endswith('_norm') else exact)
        if group:
            return group
    return '그룹 없음'


def effective_logo(name, override, original, rules):
    manual = logo_url(override, shared=False)
    if manual:
        return manual
    exact, normalized = str(name or '').strip().lower(), normalize_name(name)
    for bucket in BUCKETS:
        url = logo_url(rules.get('_logos', {}).get(bucket, {}).get(normalized if bucket.endswith('_norm') else exact))
        if url:
            return url
    return logo_url(original)


def group_sort_key(channel):
    group = channel.get('group_name') or '그룹 없음'
    if group == '그룹 없음':
        return (len(GROUP_ORDER), '')
    return (GROUP_ORDER.index(group), '') if group in GROUP_ORDER else (len(GROUP_ORDER) - 1, group)
