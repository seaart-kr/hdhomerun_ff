"""Bounded XMLTV cache and conservative name matching; no credentials in output."""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_XML = 128 * 1024 * 1024
REFRESH_SECONDS = 6 * 3600
_refresh_lock = threading.Lock()


def validate_url(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if len(text) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('EPG 주소 형식을 확인하세요.')
    try:
        parts = urllib.parse.urlsplit(text)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError()
        _ = parts.port
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address and (address.is_link_local or address.is_unspecified):
            raise ValueError()
    except ValueError:
        raise ValueError('EPG는 사용자명·비밀번호를 포함하지 않는 HTTP(S) XMLTV 주소를 입력하세요.') from None
    return text


def _fingerprint(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def _clean_name(value):
    return ' '.join(str(value or '').split())[:256]


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def parse_channels(path):
    channels, seen, depth, root, processed = [], set(), 0, None, 0
    for event, element in ET.iterparse(path, events=('start', 'end')):
        if event == 'start':
            depth += 1
            if root is None:
                root = element
                if _tag(root) != 'tv':
                    raise ValueError('XMLTV tv 문서가 아닙니다.')
            continue
        if depth == 2:
            if _tag(element) == 'channel':
                raw_identity = str(element.get('id') or '')
                identity = raw_identity.strip()
                if not identity or identity != raw_identity or len(identity) > 256 or '"' in identity or any(ord(c) < 32 or ord(c) == 127 for c in identity):
                    raise ValueError('EPG 채널 ID 형식을 확인하세요.')
                if identity in seen:
                    raise ValueError('EPG 채널 ID가 중복됩니다.')
                seen.add(identity)
                names, korean = [], []
                for child in element:
                    if _tag(child) != 'display-name':
                        continue
                    name = _clean_name(''.join(child.itertext()))
                    if name and name not in names:
                        names.append(name)
                    if name and str(child.get('lang') or '').lower() in {'ko', 'kor', 'kr'}:
                        korean.append(name)
                if names:
                    channels.append({'id': identity, 'name': (korean or names)[0], 'names': names})
                if len(seen) > 10000:
                    raise ValueError('EPG 채널 수가 너무 많습니다.')
            element.clear()
            root.clear()
            processed += 1
            if processed % 250 == 0:
                # FF may run cooperative HTTP workers: large XMLTV files
                # must give playlist/segment requests a chance to run.
                time.sleep(0)
        depth -= 1
    if not channels:
        raise ValueError('EPG에 이름이 있는 channel 항목이 없습니다.')
    return channels


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class EPGCache:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('HDHOMERUN_FF_EPG_DIR', '/data/hdhomerun_ff/epg'))

    def snapshot(self, url):
        if not url:
            return {}
        try:
            data = json.loads((self.root / 'index.json').read_text())
            if data.get('source_fingerprint') != _fingerprint(url):
                return {}
            filename = data.get('file_name', '')
            if not re.fullmatch(r'epg-[a-f0-9]{32}\.xml', filename) or not (self.root / filename).is_file():
                return {}
            return data
        except (OSError, ValueError, TypeError):
            return {}

    def refresh(self, url, opener=None):
        url = validate_url(url)
        if not url:
            raise ValueError('먼저 XMLTV EPG 주소를 입력하세요.')
        if not _refresh_lock.acquire(blocking=False):
            raise ValueError('EPG를 갱신 중입니다. 잠시 후 다시 확인하세요.')
        downloaded = xml_file = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            opener = opener or urllib.request.build_opener(_Redirects())
            request = urllib.request.Request(url, headers={'User-Agent': 'hdhomerun_ff/xmltv', 'Accept-Encoding': 'identity'})
            started, total = time.monotonic(), 0
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as output:
                downloaded = Path(output.name)
                with opener.open(request, timeout=15) as response:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD or time.monotonic() - started > 60:
                            raise ValueError('EPG 다운로드 크기 또는 대기 시간 한도를 초과했습니다.')
                        output.write(chunk)
            with downloaded.open('rb') as stream:
                compressed = stream.read(2) == b'\x1f\x8b'
            reader = gzip.open(downloaded, 'rb') if compressed else downloaded.open('rb')
            total, tail = 0, b''
            with reader, tempfile.NamedTemporaryFile(dir=self.root, delete=False) as output:
                xml_file = Path(output.name)
                while True:
                    chunk = reader.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_XML:
                        raise ValueError('압축 해제한 EPG 크기 한도를 초과했습니다.')
                    # XMLTV feeds are read as UTF-8/compatible text. Reject
                    # UTF-16/32 NUL bytes so declaration checks cannot be
                    # bypassed with an alternate byte representation.
                    if b'\x00' in chunk:
                        raise ValueError('EPG는 UTF-8 XMLTV 파일을 사용하세요.')
                    check = (tail + chunk).upper()
                    if b'<!DOCTYPE' in check or b'<!ENTITY' in check:
                        raise ValueError('DTD·엔티티 선언이 있는 EPG는 사용할 수 없습니다.')
                    tail = check[-16:]
                    output.write(chunk)
            channels = parse_channels(xml_file)
            filename = 'epg-' + uuid.uuid4().hex + '.xml'
            xml_file.replace(self.root / filename)
            xml_file = None
            data = {'source_fingerprint': _fingerprint(url), 'file_name': filename,
                    'updated_at': time.time(), 'updated_time': datetime.now(timezone.utc).isoformat(),
                    'channels': channels}
            with tempfile.NamedTemporaryFile(dir=self.root, mode='w', delete=False, encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=False)
                metadata = Path(output.name)
            metadata.replace(self.root / 'index.json')
            # Keep the previous revision for requests that already read its
            # metadata; never delete the active XMLTV file before a new one is valid.
            revisions = sorted(self.root.glob('epg-*.xml'), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in revisions[2:]:
                try:
                    path.unlink()
                except OSError:
                    pass
            return data
        except ValueError:
            raise
        except Exception:
            raise ValueError('EPG를 불러오지 못했습니다. 주소·인증·XMLTV 형식을 확인하세요. 기존 캐시는 유지됩니다.') from None
        finally:
            for path in (downloaded, xml_file):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            _refresh_lock.release()

    def for_playback(self, url):
        data = self.snapshot(url)
        if not data or time.time() - data['updated_at'] > REFRESH_SECONDS:
            try:
                return self.refresh(url)
            except ValueError:
                if not data:
                    raise
        return data

    def file_path(self, snapshot):
        filename = snapshot.get('file_name', '')
        if not re.fullmatch(r'epg-[a-f0-9]{32}\.xml', filename):
            raise ValueError('EPG 캐시를 먼저 불러오세요.')
        return self.root / filename


def exact_matches(channels, epg_channels):
    """Never infer an EPG identity from a frequency, service number or partial name."""
    def key(name):
        name = unicodedata.normalize('NFKC', str(name or '')).strip()
        if not name or name.lower() in {'unknown', '0'} or name.isdigit() or re.fullmatch(r'(?:Program\s*\d+|채널\s*[\d.]+)', name, re.I):
            return ''
        return re.sub(r'[\s\-_./()\[\]{}]+', '', name.upper())
    names, entries = {}, {}
    for entry in epg_channels:
        entries[entry['id']] = entry
        for name in entry.get('names') or [entry['name']]:
            normalized = key(name)
            if normalized:
                names.setdefault(normalized, set()).add(entry['id'])
    matches = {}
    for channel in channels:
        if channel.get('epg_channel_id'):
            continue
        ids = set()
        for name in [channel.get('name'), channel.get('broadcast_name')]:
            ids.update(names.get(key(name), ()))
        if len(ids) == 1:
            identity = next(iter(ids))
            matches[channel['channel_id']] = entries[identity]
    return matches
