"""Firmware-compatible HTTP transport using a reserved tuner per HLS stream."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

try:
    from .hdhomerun import HDHomeRunError, _NoRedirect, normalize_base_url
    from .hdhr_control import HDHomeRunControlClient, HDHomeRunControlError
except ImportError:
    from hdhomerun import HDHomeRunError, _NoRedirect, normalize_base_url
    from hdhr_control import HDHomeRunControlClient, HDHomeRunControlError

logger = logging.getLogger(__name__)


class RawStreamSession:
    """Do not force locks; restore only a tuner we can still reserve safely."""

    def __init__(self, base_url, tuner_count, channel, suffix='', control_factory=HDHomeRunControlClient):
        self.base_url = normalize_base_url(base_url)
        self.control = control_factory(self.base_url)
        self.tuner_count = tuner_count
        raw = channel['raw_data']
        self.frequency = int(raw['frequency_hz'])
        self.program = int(raw['program_number'])
        self.mode = {'8vsb':'kr-bcast', 'qam256':'kr-cable', 'VSB/8':'kr-bcast',
                     'QAM/256':'kr-cable'}.get(raw.get('lock') or raw.get('modulation_expected'))
        if not self.mode or not 40_000_000 <= self.frequency <= 1_000_000_000 or not 0 < self.program <= 65535:
            raise ValueError('원본 채널의 주파수·서비스 정보가 올바르지 않습니다.')
        if suffix not in ('', '.mpeg'):
            raise ValueError('지원하지 않는 원본 주소 확장자입니다.')
        self.suffix = suffix
        self.lease = None
        self.original_map = None

    def open(self):
        self.lease = self.control.acquire_idle_tuner(self.tuner_count)
        path = f'/tuner{self.lease.tuner_index}'
        try:
            self.original_map = self.control.get(path+'/channelmap')
            self.control.set(path+'/channelmap', self.mode, lockkey=self.lease.lockkey)
            return self.url()
        except Exception:
            self.close()
            raise

    def url(self, suffix=None):
        host = urlsplit(self.base_url).hostname
        host = f'[{host}]' if ':' in host else host
        extension = self.suffix if suffix is None else suffix
        return (f'http://{host}:5004/tuner{self.lease.tuner_index}/ch'
                f'{self.frequency}-{self.program}{extension}?lockkey={self.lease.lockkey}')

    def close(self):
        lease, self.lease = self.lease, None
        if lease is None:
            return
        path = f'/tuner{lease.tuner_index}'
        try:
            try:
                # HTTP closes can release the control lock themselves.
                if self.original_map is not None:
                    self.control.set(path+'/channelmap', self.original_map, lockkey=lease.lockkey)
            except HDHomeRunControlError:
                # Reacquisition is atomic and never steals a new viewer's lock.
                lease = self.control.acquire_specific_idle_tuner(lease.tuner_index)
                if self.original_map is not None:
                    self.control.set(path+'/channelmap', self.original_map, lockkey=lease.lockkey)
            self.control.release_tuner(lease)
        except Exception:
            logger.warning('HDHomeRun tuner cleanup deferred: tuner is busy or unreachable')


def is_mpeg_ts(data: bytes) -> bool:
    """Require multiple 188-byte packet syncs, not merely HTTP 200."""
    return any(all(data[offset+n*188] == 0x47 for n in range(4))
               for offset in range(min(188, max(0, len(data)-188*3))))


def probe_http_format(base_url, tuner_count, channel, mode='auto', session_factory=RawStreamSession, opener=None):
    if mode not in {'auto', 'none', 'mpeg'}:
        raise ValueError('원본 주소 형식은 자동/확장자 없음/.mpeg 중 선택하세요.')
    extensions = ['', '.mpeg'] if mode == 'auto' else ['.mpeg' if mode == 'mpeg' else '']
    attempts = []
    opener = opener or build_opener(_NoRedirect())
    for extension in extensions:
        session = session_factory(base_url, tuner_count, channel, extension)
        try:
            url = session.open()
            with opener.open(Request(url, headers={'User-Agent':'hdhomerun_ff/compatibility'}), timeout=8) as response:
                if response.status == 200 and is_mpeg_ts(response.read(188*8)):
                    return {'suffix':extension, 'attempts':attempts, 'verified':True}
                attempts.append({'suffix':extension, 'error':'영상 TS 데이터가 아닙니다.'})
        except Exception as exc:
            # Do not include URLs with transient lockkeys in user-visible errors.
            attempts.append({'suffix':extension, 'error':type(exc).__name__})
        finally:
            session.close()
    raise HDHomeRunError('호환 원본 주소에서 영상 데이터를 받지 못했습니다: '+str(attempts))
