"""One explicit, cancellable HDHomeRun preset-scan job per FF process."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Sequence

try:
    from .hdhr_control import HDHomeRunControlClient, HDHomeRunControlError
    from .model import ChannelStore
    from .task import preset_scan_channel_rows
except ImportError:  # Unit tests import modules directly from the plugin root.
    from hdhr_control import HDHomeRunControlClient, HDHomeRunControlError  # type: ignore
    from model import ChannelStore  # type: ignore
    from task import preset_scan_channel_rows  # type: ignore


logger = logging.getLogger(__name__)


class PresetScanRunner:
    """Run a scan in the background without ever enabling raw playback rows."""

    def __init__(
        self,
        *,
        control_factory: Callable[[str], HDHomeRunControlClient] = HDHomeRunControlClient,
        store_factory: Callable[[], ChannelStore] = ChannelStore,
        rows_factory: Callable[..., list[dict[str, Any]]] = preset_scan_channel_rows,
    ) -> None:
        self._control_factory = control_factory
        self._store_factory = store_factory
        self._rows_factory = rows_factory
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._new_state()

    @staticmethod
    def _new_state() -> dict[str, Any]:
        return {
            'state': 'idle',
            'running': False,
            'completed_muxes': 0,
            'total_muxes': 0,
            'found_services': 0,
            'stored_channels': 0,
            'message': '프리셋 스캔을 시작하지 않았습니다.',
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(
        self,
        *,
        base_url: str,
        device_id: str,
        tuner_count: int,
        targets: Sequence[Any],
        scan_kind: str = '',
    ) -> dict[str, Any]:
        if not str(device_id or '').strip():
            raise ValueError('HDHomeRun 장비 확인을 먼저 실행하세요.')
        if not targets:
            raise ValueError('스캔할 프리셋 주파수가 없습니다.')
        with self._lock:
            if self._state.get('running'):
                raise ValueError('이미 프리셋 스캔이 실행 중입니다.')
            self._cancel = threading.Event()
            self._state = {
                'state': 'running',
                'running': True,
                'completed_muxes': 0,
                'total_muxes': len(targets),
                'found_services': 0,
                'stored_channels': 0,
                'message': '유휴 튜너를 확보한 뒤 선택한 프리셋 주파수를 스캔합니다.',
            }
            self._thread = threading.Thread(
                target=self._run,
                kwargs={
                    'base_url': base_url,
                    'device_id': str(device_id),
                    'tuner_count': int(tuner_count),
                    'targets': tuple(targets),
                    'cancel': self._cancel,
                    'scan_kind': scan_kind,
                },
                name='hdhomerun-ff-preset-scan',
                daemon=True,
            )
            self._thread.start()
            return dict(self._state)

    def _set_progress(self, completed: int, total: int, scan: Any) -> None:
        with self._lock:
            self._state['completed_muxes'] = int(completed)
            self._state['total_muxes'] = int(total)
            self._state['found_services'] = int(self._state.get('found_services') or 0) + len(scan.services)
            self._state['message'] = f'{completed}/{total} mux 스캔 완료'

    def _finish(self, **updates: Any) -> None:
        with self._lock:
            self._state.update(updates)
            self._state['running'] = False

    def cancel(self) -> dict[str, Any]:
        """Request cancellation; the control client releases its tuner lease."""

        with self._lock:
            if not self._state.get('running'):
                return dict(self._state)
            self._cancel.set()
            self._state['message'] = '프리셋 스캔 취소를 요청했습니다. 현재 mux를 정리한 뒤 유휴 튜너를 해제합니다.'
            return dict(self._state)

    def _run(self, *, base_url: str, device_id: str, tuner_count: int, targets: Sequence[Any], cancel: threading.Event, scan_kind: str = '') -> None:
        try:
            control = self._control_factory(base_url)
            scans = control.scan_targets(
                targets,
                tuner_count=tuner_count,
                progress_callback=self._set_progress,
                cancelled=cancel.is_set,
            )
            if cancel.is_set():
                raise HDHomeRunControlError('프리셋 스캔이 취소되었습니다.')
            rows = self._rows_factory(device_id, scans)
            if not rows:
                self._finish(
                    state='complete_no_services',
                    message='프리셋 주파수에서 저장할 서비스가 발견되지 않았습니다. 기존 채널 목록은 변경하지 않았습니다.',
                )
                return
            stored = self._store_factory().replace_preset_scan_channels(device_id, rows)
            learned = [{'frequency_hz': s.target.frequency_hz, 'modulation':
                        {'8vsb':'VSB/8','qam256':'QAM/256','qam64':'QAM/64'}.get(s.status.lock, s.target.modulation)}
                       for s in scans if s.status.is_locked and s.services]
            updates = {'learned_muxes': learned}
            if scan_kind == 'full_kr':
                updates['full_scan_completed'] = True
            self._store_factory().update_device_profile(device_id, **updates)
            self._finish(
                state='complete',
                stored_channels=int(stored),
                message=(
                    f'서비스 {len(rows)}개와 수신 주파수/변조 {len(learned)}개를 저장했습니다. '
                    '사용 체크된 일반 채널은 Shyni M3U에서 재생할 수 있습니다.'
                ),
            )
        except HDHomeRunControlError as exc:
            self._finish(
                state='cancelled' if cancel.is_set() else 'failed',
                message='프리셋 스캔이 취소되었습니다.' if cancel.is_set() else str(exc),
            )
        except Exception:
            logger.exception('[hdhomerun_ff] preset scan failed')
            self._finish(state='failed', message='프리셋 스캔 중 안전하게 완료하지 못했습니다. 로그를 확인하세요.')

    def shutdown(self, timeout: float = 6.0) -> None:
        """Ask a normal FF unload to release its tuner lock before exit."""

        with self._lock:
            thread = self._thread
            if self._state.get('running'):
                self._cancel.set()
                self._state['message'] = 'FF 재시작으로 프리셋 스캔 취소를 요청했습니다.'
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, float(timeout)))
