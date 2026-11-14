# -*- coding: utf-8 -*-
"""FF routes for the HDHomeRun-direct live TV plugin."""
from __future__ import annotations

import os
import re
from urllib.parse import urlencode

from flask import Response, jsonify, render_template, request
from plugin import PluginModuleBase, ToolUtil

from .diagnostics import ffmpeg_capabilities
from .hdhomerun import HDHomeRunClient, HDHomeRunError
from .hls_manager import HLSManager
from .model import ChannelStore
from .playlist import build_shyni_m3u, normalise_output_profile
from .preset import PresetValidationError, list_bundled_presets, load_bundled_preset
from .setup import P, logger
from .task import device_summary, snapshot_channel_rows


_CONSUMER_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def _as_int(value, fallback, minimum, maximum):
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return fallback


def _is_true(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'y'}


def _store():
    return ChannelStore()


def _output_profile():
    return normalise_output_profile(P.ModelSetting.get('basic_hls_output_profile') or 'copy')


def _hls_options():
    return HLSManager.configure(
        idle_timeout=_as_int(P.ModelSetting.get('basic_hls_idle_timeout'), 60, 30, 600),
    )


def _consumer_id(req):
    value = str(req.args.get('client') or '').strip()
    return value if _CONSUMER_RE.fullmatch(value) else ''


def _api_url(path):
    try:
        return ToolUtil.make_apikey_url(path)
    except Exception:
        return path


def _playlist_with_local_segments(playlist, channel_id, output_profile, api_key, consumer_id):
    lines = []
    for raw_line in str(playlist or '').splitlines():
        line = raw_line.strip()
        if line and not line.startswith('#'):
            filename = os.path.basename(line)
            query = [('i', channel_id), ('p', output_profile), ('f', filename)]
            if api_key:
                query.append(('apikey', api_key))
            if consumer_id:
                query.append(('client', consumer_id))
            line = f'{request.host_url.rstrip("/")}/{P.package_name}/api/hls_segment.ts?{urlencode(query)}'
        lines.append(line)
    return '\n'.join(lines) + '\n'


class ModuleBasic(PluginModuleBase):
    db_default = {
        'basic_hdhomerun_base_url': '',
        'basic_hdhomerun_device_id': '',
        'basic_hdhomerun_device_name': '',
        'basic_hdhomerun_preset_id': 'kr-cable-cmb-v1',
        'basic_hls_output_profile': 'copy',
        'basic_hls_idle_timeout': '60',
    }

    def __init__(self, P):
        super(ModuleBasic, self).__init__(P, name='basic', first_menu='device')
        _hls_options()

    @staticmethod
    def _save_settings(req):
        P.ModelSetting.setting_save(req)
        _hls_options()

    @staticmethod
    def _client_from_settings():
        return HDHomeRunClient(P.ModelSetting.get('basic_hdhomerun_base_url') or '')

    def _menu_arg(self, sub):
        settings = P.ModelSetting.to_dict()
        settings['basic_hls_output_profile'] = _output_profile()
        settings['basic_hls_idle_timeout'] = str(_as_int(settings.get('basic_hls_idle_timeout'), 60, 30, 600))
        return {
            'arg': settings,
            'page_sub': sub,
            'ajax_sub': self.name,
            'package_name': P.package_name,
            'presets': list_bundled_presets(),
            'channels': _store().list_channels(),
            'channel_summary': _store().summary(),
            'm3u_copy_url': _api_url(f'/{P.package_name}/api/m3u_shyni_copy'),
            'm3u_mobile_url': _api_url(f'/{P.package_name}/api/m3u_shyni_mobile'),
            'm3u_default_url': _api_url(f'/{P.package_name}/api/m3u_shyni'),
        }

    def process_menu(self, sub, req):
        try:
            sub = sub or 'device'
            if sub not in {'device', 'preset', 'playback', 'diagnostics'}:
                sub = 'device'
            return render_template(
                f'{P.package_name}_{self.name}_setting.html',
                **self._menu_arg(sub),
            )
        except Exception as exc:
            logger.exception('[hdhomerun_ff] menu failed: %s', exc)
            return Response(f'HDHomeRun FF menu error: {exc}', status=500, mimetype='text/plain')

    def process_ajax(self, sub, req):
        try:
            if sub == 'setting_save':
                self._save_settings(req)
                return jsonify({'ret': 'success', 'msg': '설정을 저장했습니다.'})

            if sub == 'probe_device':
                self._save_settings(req)
                snapshot = self._client_from_settings().fetch_snapshot()
                info = device_summary(snapshot)
                P.ModelSetting.set('basic_hdhomerun_device_id', info['device_id'])
                P.ModelSetting.set('basic_hdhomerun_device_name', info['friendly_name'])
                return jsonify({'ret': 'success', 'msg': 'HDHomeRun 장비를 확인했습니다.', 'device': info})

            if sub == 'import_native_lineup':
                self._save_settings(req)
                snapshot = self._client_from_settings().fetch_snapshot()
                rows = snapshot_channel_rows(snapshot)
                imported = _store().replace_device_channels(snapshot.device.device_id, rows)
                info = device_summary(snapshot)
                P.ModelSetting.set('basic_hdhomerun_device_id', info['device_id'])
                P.ModelSetting.set('basic_hdhomerun_device_name', info['friendly_name'])
                return jsonify({
                    'ret': 'success',
                    'msg': f'HDHomeRun native lineup 채널 {imported}개를 저장했습니다.',
                    'device': info,
                    'summary': _store().summary(),
                })

            if sub == 'preview_scan_plan':
                self._save_settings(req)
                preset = load_bundled_preset(P.ModelSetting.get('basic_hdhomerun_preset_id') or '')
                plan = HDHomeRunClient.build_initial_scan_plan(preset)
                return jsonify({
                    'ret': 'success',
                    'msg': f'{preset["name"]} 프리셋 {len(plan.targets)}개 mux를 준비했습니다. 실제 장비 scan은 유휴 튜너를 예약하는 다음 단계에서 실행됩니다.',
                    'preset': {'id': plan.preset_id, 'delivery_system': plan.delivery_system},
                    'mux_count': len(plan.targets),
                    'changes_device_channelmap': plan.changes_device_channelmap,
                })

            if sub == 'set_channel_enabled':
                channel_id = str(req.form.get('channel_id') or '').strip()
                if not channel_id:
                    return jsonify({'ret': 'warning', 'msg': '채널 ID가 없습니다.'})
                ok = _store().set_enabled(channel_id, _is_true(req.form.get('enabled')))
                return jsonify({'ret': 'success' if ok else 'warning', 'msg': '채널 상태를 저장했습니다.' if ok else '채널을 찾지 못했습니다.'})

            if sub == 'diagnostics':
                return jsonify({'ret': 'success', 'diagnostics': ffmpeg_capabilities()})

            return jsonify({'ret': 'warning', 'msg': f'알 수 없는 요청: {sub}'})
        except (HDHomeRunError, PresetValidationError, ValueError) as exc:
            return jsonify({'ret': 'warning', 'msg': str(exc)})
        except Exception as exc:
            logger.exception('[hdhomerun_ff] ajax failed: %s', exc)
            return jsonify({'ret': 'danger', 'msg': '요청 처리 중 오류가 발생했습니다.'})

    def process_api(self, sub, req):
        try:
            if sub in {'m3u_shyni', 'm3u_shyni_copy', 'm3u_shyni_mobile'}:
                profile = _output_profile()
                if sub == 'm3u_shyni_copy':
                    profile = 'copy'
                elif sub == 'm3u_shyni_mobile':
                    profile = 'mobile_cpu'
                text = build_shyni_m3u(
                    _store().list_channels(enabled_only=True),
                    request.host_url.rstrip('/'),
                    P.package_name,
                    api_key=str(req.args.get('apikey') or '').strip(),
                    output_profile=profile,
                    consumer_id=_consumer_id(req),
                )
                return Response(
                    text,
                    mimetype='audio/x-mpegurl',
                    headers={
                        'Content-Disposition': f'inline; filename=hdhomerun_shyni_{profile}.m3u',
                        'Cache-Control': 'no-store',
                    },
                )

            if sub == 'hls.m3u8':
                channel_id = str(req.args.get('i') or '').strip()
                profile = normalise_output_profile(req.args.get('p') or _output_profile())
                consumer_id = _consumer_id(req)
                channel = _store().get_channel(channel_id)
                if not channel:
                    return Response('channel not found', status=404, mimetype='text/plain')
                _hls_options()
                playlist = HLSManager.get_playlist(channel_id, profile, consumer_id)
                if not playlist:
                    result = HLSManager.ensure_stream(channel_id, channel['source_url'], profile, consumer_id)
                    if result.get('ret') != 'success':
                        return Response(result.get('msg') or 'HLS unavailable', status=int(result.get('status') or 502), mimetype='text/plain')
                    playlist = str(result.get('playlist') or '')
                return Response(
                    _playlist_with_local_segments(
                        playlist,
                        channel_id,
                        profile,
                        str(req.args.get('apikey') or '').strip(),
                        consumer_id,
                    ),
                    mimetype='application/vnd.apple.mpegurl',
                    headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
                )

            if sub == 'hls_segment.ts':
                channel_id = str(req.args.get('i') or '').strip()
                profile = normalise_output_profile(req.args.get('p') or _output_profile())
                segment = HLSManager.read_segment(channel_id, req.args.get('f'), profile, _consumer_id(req))
                if segment is None:
                    return Response('segment not found', status=404, mimetype='text/plain')
                return Response(segment, content_type='video/mp2t', headers={'Cache-Control': 'no-store'})

            return Response('unknown API request', status=404, mimetype='text/plain')
        except ValueError:
            return Response('invalid request', status=400, mimetype='text/plain')
        except Exception as exc:
            logger.exception('[hdhomerun_ff] api failed: %s', exc)
            return Response('service unavailable', status=503, mimetype='text/plain')
