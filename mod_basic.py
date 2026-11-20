# -*- coding: utf-8 -*-
"""FF routes for the HDHomeRun-direct live TV plugin."""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from flask import Response, jsonify, render_template, request, send_file, redirect
from plugin import PluginModuleBase, ToolUtil

from .diagnostics import ffmpeg_capabilities
from . import vaapi, nvenc
from .hdhr_control import HDHomeRunControlError, HDHomeRunControlClient
from .hdhomerun import HDHomeRunClient, HDHomeRunError
from .hls_manager import HLSManager
from .model import ChannelStore
from .playlist import (build_shyni_m3u, normalise_output_profile, resolve_output_profile,
                       validate_profile_override, OUTPUT_PROFILE_LABELS)
from .preset import PresetValidationError, list_bundled_presets, load_bundled_preset, korean_full_scan_preset, dump_preset
from .preset_scan_runner import PresetScanRunner
from .raw_stream import RawStreamSession, NativeStreamSession, probe_http_format
from .device_config import tuner_budget, discovery_summary, broadcast_counts, usable_broadcast
from .transcode_config import DEFAULTS as TRANSCODE_DEFAULTS, PRESETS, validate_options
from .setup import P, logger
from .task import snapshot_channel_rows
from .channel_groups import GROUP_ORDER, group_sort_key, fetch_rules as fetch_group_rules
from .channel_management import is_encrypted, preview_channel, preview_profile
from .epg import EPGCache, exact_matches as match_epg, filtered_xmltv, validate_url as validate_epg_url


_CONSUMER_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_PRESET_SCAN_RUNNER = PresetScanRunner()
_SHYNI_EPG_ENDPOINT = 'epg_shyni.xml'


def _as_int(value, fallback, minimum, maximum):
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return fallback


def _is_true(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'y'}


def _store():
    return ChannelStore()


def _scan_plan(req):
    mode = str(req.form.get('basic_hdhomerun_scan_mode') or P.ModelSetting.get('basic_hdhomerun_scan_mode') or 'auto')
    device_id = P.ModelSetting.get('basic_hdhomerun_device_id') or ''
    learned = _store().learned_preset(device_id)
    if mode == 'fast' or (mode == 'auto' and learned['muxes'] and _store().device_profile(device_id).get('full_scan_completed')):
        if not learned['muxes']:
            raise ValueError('저장된 수신 주파수가 없습니다. 한국 전체 탐색을 먼저 실행하세요.')
        preset = learned
    elif mode in {'auto', 'full_kr'}:
        preset = korean_full_scan_preset()
    elif mode == 'preset':
        preset = load_bundled_preset(str(req.form.get('basic_hdhomerun_preset_id') or P.ModelSetting.get('basic_hdhomerun_preset_id') or ''))
    else:
        raise ValueError('스캔 방식을 확인하세요.')
    return mode, preset, HDHomeRunClient.build_initial_scan_plan(preset)


def _playback_device():
    device = HDHomeRunClient(P.ModelSetting.get('basic_hdhomerun_base_url') or '').fetch_discover()
    if device.device_id != str(P.ModelSetting.get('basic_hdhomerun_device_id') or '').lower():
        raise ValueError('장비 확인을 다시 실행하세요.')
    return device


def _tuner_count(device):
    return tuner_budget(P.ModelSetting.get('basic_hdhomerun_tuner_limit') or 'all',device.tuner_count)


def _device_display(device):
    info=discovery_summary(device)
    info['tuner_limit']=_tuner_count(device)
    info['checked_at']=datetime.now().isoformat(timespec='seconds')
    statuses=[]
    control=HDHomeRunControlClient(device.base_url,timeout=1)
    for index in range(device.tuner_count):
        try:
            status=control.tuner_status(index)
            statuses.append({'index':index,'busy':status.fields.get('ch','none') not in ('none',''),
                             'lock':status.lock,'signal':status.signal_strength})
        except (HDHomeRunError,OSError):
            statuses.append({'index':index,'busy':None,'lock':'확인 불가','signal':0})
    info['tuners']=statuses
    _store().update_device_profile(device.device_id,discovery=info)
    return info


def _source_format(device, channel, force=False):
    mode = str(P.ModelSetting.get('basic_hdhomerun_stream_format') or 'auto')
    profile = _store().device_profile(device.device_id)
    cache = profile.get('http_stream', {})
    if not force and cache.get('firmware') == device.firmware_version and cache.get('base_url') == device.base_url and cache.get('mode') == mode and cache.get('suffix') in {'', '.mpeg'}:
        return cache
    result = probe_http_format(device.base_url, _tuner_count(device), channel, mode)
    result.update(firmware=device.firmware_version, base_url=device.base_url, mode=mode)
    _store().update_device_profile(device.device_id, http_stream=result)
    return result


def _output_profile():
    return normalise_output_profile(P.ModelSetting.get('basic_hls_output_profile') or 'mobile_cpu')


def _playlist_channels():
    source = P.ModelSetting.get('basic_hdhomerun_playlist_source') or 'scan'
    return [c for c in _store().playback_channels()
            if c['device_id']==P.ModelSetting.get('basic_hdhomerun_device_id')
            and (c['raw_data'].get('source') == 'preset_scan') == (source == 'scan')]


def _hls_options():
    options=validate_options({key:P.ModelSetting.get('basic_transcode_'+key) or default
                              for key,default in TRANSCODE_DEFAULTS.items()})
    return HLSManager.configure(
        idle_timeout=_as_int(P.ModelSetting.get('basic_hls_idle_timeout'), 60, 30, 600),
        transcode_options=options,
        vaapi_driver=P.ModelSetting.get('basic_vaapi_driver') or 'auto',
    )


def _transcode_form(req):
    return validate_options({key:req.form.get('basic_transcode_'+key,
                             P.ModelSetting.get('basic_transcode_'+key) or default)
                             for key,default in TRANSCODE_DEFAULTS.items()})


def _playback_form(req):
    profile = str(req.form.get('basic_hls_output_profile') or _output_profile())
    if profile not in HLSManager.PROFILES:
        raise ValueError('출력 방식을 확인하세요.')
    driver = vaapi.validate_driver(req.form.get('basic_vaapi_driver') or P.ModelSetting.get('basic_vaapi_driver') or 'auto')
    _check_profile_support(profile, driver)
    return profile, driver


def _check_profile_support(profile, driver):
    if profile == 'mobile_vaapi':
        result = vaapi.probe(driver)
        if not result['available']:
            raise ValueError(result['message'])
    elif profile == 'mobile_nvenc':
        result = nvenc.probe()
        if not result['available']:
            raise ValueError(result['message'])


def _profile_override(req):
    if 'output_profile' not in req.form:
        raise ValueError('출력 방식을 선택하세요.')
    profile = validate_profile_override(req.form['output_profile'])
    _check_profile_support(profile, P.ModelSetting.get('basic_vaapi_driver') or 'auto')
    return profile


def _consumer_id(req):
    value = str(req.args.get('client') or '').strip()
    return value if _CONSUMER_RE.fullmatch(value) else ''


def _api_url(path):
    try:
        return ToolUtil.make_apikey_url(path)
    except Exception:
        return path


def _playlist_with_local_segments(playlist, channel_id, output_profile, api_key, consumer_id, relative=False):
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
            origin = '' if relative else request.host_url.rstrip('/')
            line = f'{origin}/{P.package_name}/api/hls_segment.ts?{urlencode(query)}'
        lines.append(line)
    return '\n'.join(lines) + '\n'


class ModuleBasic(PluginModuleBase):
    db_default = {
        'basic_hdhomerun_base_url': '',
        'basic_hdhomerun_device_id': '',
        'basic_hdhomerun_device_name': '',
        'basic_hdhomerun_tuner_limit': 'all',
        'basic_hdhomerun_preset_id': 'kr-cable-cmb-v1',
        'basic_hdhomerun_scan_mode': 'auto',
        'basic_hdhomerun_stream_format': 'auto',
        'basic_hdhomerun_playlist_source': 'scan',
        'basic_epg_url': '',
        'basic_hls_output_profile': 'mobile_cpu',
        'basic_hls_idle_timeout': '60',
        'basic_vaapi_driver': 'auto',
        'basic_startup_buffer_version': '',
        **{'basic_transcode_'+key:str(value) for key,value in TRANSCODE_DEFAULTS.items()},
    }

    def __init__(self, P):
        super(ModuleBasic, self).__init__(P, name='basic', first_menu='device')

    def plugin_load(self):
        """Prepare local state after FF has loaded the plugin settings."""
        try:
            _store().init_db()
            if P.ModelSetting.get('recent_menu_plugin') == 'basic|preview':
                P.ModelSetting.set('recent_menu_plugin', 'basic|device')
            if P.ModelSetting.get('basic_startup_buffer_version') != '1':
                if P.ModelSetting.get('basic_transcode_delay_seconds') == '12':
                    P.ModelSetting.set('basic_transcode_delay_seconds', str(TRANSCODE_DEFAULTS['delay_seconds']))
                P.ModelSetting.set('basic_startup_buffer_version', '1')
            _hls_options()
            logger.info('[hdhomerun_ff] local channel store is ready')
        except Exception:
            # Do not make a failed optional initialization obscure FF's plugin
            # loader error; subsequent requests return the normal safe error.
            logger.exception('[hdhomerun_ff] plugin initialization failed')

    def plugin_unload(self):
        """Terminate local encoders when FF unloads or reloads this plugin."""
        _PRESET_SCAN_RUNNER.shutdown()
        HLSManager.stop_all()

    @staticmethod
    def _save_settings(req):
        _transcode_form(req)
        if 'basic_epg_url' in req.form:
            validate_epg_url(req.form['basic_epg_url'])
        if 'basic_hls_output_profile' in req.form:
            _playback_form(req)
        if 'basic_vaapi_driver' in req.form:
            vaapi.validate_driver(req.form['basic_vaapi_driver'])
        P.ModelSetting.setting_save(req)
        _hls_options()

    @staticmethod
    def _client_from_settings():
        return HDHomeRunClient(P.ModelSetting.get('basic_hdhomerun_base_url') or '')

    def _menu_arg(self, sub):
        settings = P.ModelSetting.to_dict()
        channels = [c for c in _store().list_channels() if c['device_id']==settings.get('basic_hdhomerun_device_id')]
        channels.sort(key=group_sort_key)
        group_profiles = _store().group_profiles()
        for channel in channels:
            channel['effective_output_profile'] = resolve_output_profile(channel, group_profiles, _output_profile())
            channel['encrypted'] = is_encrypted(channel)
        settings['basic_hls_output_profile'] = _output_profile()
        settings['basic_hls_idle_timeout'] = str(_as_int(settings.get('basic_hls_idle_timeout'), 60, 30, 600))
        options=_hls_options()['transcode']
        settings.update({'basic_transcode_'+key:str(value) for key,value in options.items()})
        device_info=_store().device_profile(settings.get('basic_hdhomerun_device_id') or '').get('discovery',{})
        epg = EPGCache().snapshot((settings.get('basic_epg_url') or '').strip())
        if device_info:
            device_info['tuner_limit']=tuner_budget(settings.get('basic_hdhomerun_tuner_limit') or 'all',device_info['tuner_count'])
        return {
            'arg': settings,
            'device_info':device_info,
            'broadcast_counts':broadcast_counts(channels,settings.get('basic_hdhomerun_device_id')),
            'usable_channel_ids':{c['channel_id'] for c in channels if usable_broadcast(c)},
            'playable_channel_count':len(_playlist_channels()),
            'transcode_presets': PRESETS,
            'transcode_options': options,
            'encoding_profiles': {profile: HLSManager.encoding_summary(profile) for profile in HLSManager.PROFILES},
            'vaapi_status': vaapi.status(settings.get('basic_vaapi_driver') or 'auto'),
            'nvenc_status': nvenc.status(),
            'page_sub': sub,
            'ajax_sub': self.name,
            'package_name': P.package_name,
            'presets': list_bundled_presets(),
            'channels': channels,
            'encrypted_channel_count': sum(c['encrypted'] for c in channels),
            'epg_channels': epg.get('channels', []),
            'epg_updated_time': epg.get('updated_time', ''),
            'epg_api_url': _api_url(f'/{P.package_name}/api/{_SHYNI_EPG_ENDPOINT}') if epg else '',
            'group_channel_counts': dict(Counter(c['group_name'] for c in channels)),
            'selected_channel_count': sum(channel['selected'] for channel in channels),
            'learned_mux_count': len(_store().learned_preset(settings.get('basic_hdhomerun_device_id') or '')['muxes']),
            'scan_export_url': _api_url(f'/{P.package_name}/api/scan_preset.json'),
            'channel_summary': _store().summary(),
            'group_rules_updated_time': _store().group_rules()['updated_time'],
            'group_names': list(dict.fromkeys([*GROUP_ORDER, *(c['group_name'] for c in channels)])),
            'output_profile_labels': OUTPUT_PROFILE_LABELS,
            'group_profiles': group_profiles,
            'profile_group_names': list(dict.fromkeys([*(c['group_name'] for c in channels), *group_profiles, *GROUP_ORDER])),
            'm3u_copy_url': _api_url(f'/{P.package_name}/api/m3u_shyni_copy'),
            'm3u_mobile_url': _api_url(f'/{P.package_name}/api/m3u_shyni_mobile'),
            'm3u_vaapi_url': _api_url(f'/{P.package_name}/api/m3u_shyni_vaapi'),
            'm3u_nvenc_url': _api_url(f'/{P.package_name}/api/m3u_shyni_nvenc'),
            'm3u_default_url': _api_url(f'/{P.package_name}/api/m3u_shyni'),
        }

    def process_menu(self, sub, req):
        try:
            if sub == 'preview':
                if not req.args.get('i'):
                    return redirect(f'/{P.package_name}/basic/device')
                if _PRESET_SCAN_RUNNER.status()['running']:
                    return Response('스캔이 끝난 뒤 미리보기를 실행하세요. 진행 중인 스캔은 중단하지 않았습니다.', status=409, mimetype='text/plain')
                channel = preview_channel(_store(), req.args.get('i'), P.ModelSetting.get('basic_hdhomerun_device_id'))
                path = f'/{P.package_name}/api/preview.m3u8'
                # Keep media on the page's origin even when FF is behind an
                # HTTPS reverse proxy and sees an internal HTTP request.
                query = urlsplit(_api_url(path)).query
                url = path + ('?' + query if query else '')
                url += ('&' if '?' in url else '?') + urlencode({'i': channel['channel_id']})
                return Response(render_template(f'{P.package_name}_preview.html', channel=channel,
                    package_name=P.package_name, preview_url=url,
                    epg_channels=EPGCache().snapshot((P.ModelSetting.get('basic_epg_url') or '').strip()).get('channels', [])), mimetype='text/html',
                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
            sub = sub or 'device'
            if sub not in {'device', 'preset', 'playback', 'diagnostics'}:
                sub = 'device'
            return render_template(
                f'{P.package_name}_{self.name}_setting.html',
                **self._menu_arg(sub),
            )
        except ValueError as exc:
            return Response(str(exc), status=400, mimetype='text/plain')
        except Exception as exc:
            logger.exception('[hdhomerun_ff] menu failed: %s', exc)
            return Response(f'HDHomeRun Live for Shyni menu error: {exc}', status=500, mimetype='text/plain')

    def process_ajax(self, sub, req):
        try:
            if sub in {'refresh_epg', 'auto_match_epg', 'set_channel_epg'}:
                if req.method != 'POST':
                    return jsonify({'ret': 'warning', 'msg': 'POST 요청이 필요합니다.'}), 405
                store, cache = _store(), EPGCache()
                saved_url = (P.ModelSetting.get('basic_epg_url') or '').strip()
                if sub == 'refresh_epg':
                    url = validate_epg_url(req.form.get('basic_epg_url', saved_url))
                    data = cache.refresh(url)
                    P.ModelSetting.set('basic_epg_url', url)
                    return jsonify({'ret': 'success', 'count': len(data['channels']),
                        'msg': f'EPG 채널 {len(data["channels"])}개를 불러왔습니다. 정확히 이름이 일치하는 채널은 자동 연결하고, 나머지는 목록에서 직접 선택하세요.'})
                data = cache.snapshot(saved_url)
                identity = str(req.form.get('epg_channel_id') or '').strip()
                if not data and not (sub == 'set_channel_epg' and not identity):
                    raise ValueError('먼저 EPG 주소를 저장하고 불러오세요.')
                device_id = P.ModelSetting.get('basic_hdhomerun_device_id')
                if sub == 'auto_match_epg':
                    channels = [c for c in store.list_channels() if c['device_id'] == device_id]
                    count = store.apply_epg_matches(match_epg(channels, data['channels']))
                    return jsonify({'ret': 'success', 'matched': count,
                        'msg': f'이름이 정확히 일치하는 {count}개를 EPG에 연결했습니다. 수동 이름은 유지했습니다. 이름 미수신·중복 후보는 직접 선택하세요.'})
                channel_id = str(req.form.get('channel_id') or '').strip()
                channel = store.get_channel(channel_id)
                if not channel or channel['device_id'] != device_id:
                    raise ValueError('현재 장비의 채널을 찾지 못했습니다.')
                entry = next((e for e in data.get('channels', []) if e['id'] == identity), None)
                if identity and not entry:
                    raise ValueError('불러온 EPG 목록에서 정확한 채널 ID를 선택하세요.')
                store.set_epg(channel_id, identity, entry['name'] if entry else '', apply_name=bool(identity))
                channel = store.get_channel(channel_id)
                return jsonify({'ret': 'success', 'name': channel['name'], 'name_override': channel['name_override'],
                    'msg': 'EPG 채널명과 ID를 적용했습니다. Shyni M3U·EPG 목록을 새로고침하세요.' if identity else 'EPG 연결을 해제했습니다.'})
            if sub in {'set_channel_name', 'delete_channel', 'delete_encrypted_channels'}:
                if req.method != 'POST':
                    return jsonify({'ret': 'warning', 'msg': 'POST 요청이 필요합니다.'}), 405
                store = _store()
                device_id = P.ModelSetting.get('basic_hdhomerun_device_id')
                if sub == 'delete_encrypted_channels':
                    if _PRESET_SCAN_RUNNER.status()['running']:
                        raise ValueError('스캔 완료 후 삭제하세요. 진행 중인 스캔은 중단하지 않았습니다.')
                    if not _is_true(req.form.get('confirm')):
                        raise ValueError('암호화 채널 일괄 삭제 확인이 필요합니다.')
                    count = store.delete_encrypted_channels(device_id)
                    return jsonify({'ret': 'success', 'deleted': count,
                        'msg': f'암호화 채널 {count}개를 삭제했습니다. 제어 서비스·데이터 없음은 유지하며, 재스캔에서 다시 발견되면 등록됩니다.'})
                channel_id = str(req.form.get('channel_id') or '').strip()
                channel = store.get_channel(channel_id)
                if not channel or channel['device_id'] != device_id:
                    raise ValueError('현재 장비의 채널을 찾지 못했습니다.')
                if sub == 'set_channel_name':
                    store.set_name(channel_id, req.form.get('name') or '')
                    channel = store.get_channel(channel_id)
                    return jsonify({'ret': 'success', 'name': channel['name'],
                        'name_override': channel['name_override'], 'broadcast_name': channel['broadcast_name'],
                        'msg': '채널명을 저장했습니다. Shyni의 M3U 채널 목록도 새로고침하세요.'})
                if _PRESET_SCAN_RUNNER.status()['running']:
                    raise ValueError('스캔 완료 후 삭제하세요. 진행 중인 스캔은 중단하지 않았습니다.')
                if not _is_true(req.form.get('confirm')):
                    raise ValueError('채널 삭제 확인이 필요합니다.')
                deleted = store.delete_channel(channel_id)
                return jsonify({'ret': 'success' if deleted else 'warning', 'deleted': int(deleted),
                    'msg': '현재 목록에서 삭제했습니다. 다음 스캔·라인업 갱신에서 다시 발견되면 등록됩니다.'})
            if sub == 'set_channel_profile':
                channel_id = str(req.form.get('channel_id') or '').strip()
                if not channel_id:
                    raise ValueError('채널 ID가 없습니다.')
                profile = _profile_override(req)
                store = _store()
                if not store.set_channel_profile(channel_id, profile):
                    return jsonify({'ret': 'warning', 'msg': '채널을 찾지 못했습니다.'})
                channel = next(c for c in store.list_channels() if c['channel_id'] == channel_id)
                effective = resolve_output_profile(channel, store.group_profiles(), _output_profile())
                return jsonify({'ret': 'success', 'effective_output_profile': effective,
                                'effective_output_label': OUTPUT_PROFILE_LABELS[effective],
                                'msg': '채널 출력을 저장했습니다. Shyni 기본 M3U 목록을 새로고침하고 다시 재생하세요.'})

            if sub == 'set_group_profile':
                profile = _profile_override(req)
                _store().set_group_profile(req.form.get('group_name'), profile)
                return jsonify({'ret': 'success', 'msg': '그룹 출력을 저장했습니다. Shyni 기본 M3U 목록을 새로고침하고 다시 재생하세요.'})

            if sub == 'refresh_channel_groups':
                rules = fetch_group_rules()
                store = _store()
                store.save_group_rules(rules)
                device_id = P.ModelSetting.get('basic_hdhomerun_device_id')
                channels = [c for c in store.list_channels() if c['device_id'] == device_id]
                grouped = sum(c['group_name'] != '그룹 없음' for c in channels)
                icons = sum(bool(c['logo_url']) for c in channels)
                return jsonify({'ret': 'success', 'msg': f'그룹·아이콘 기준표를 저장했습니다. 현재 장비 {len(channels)}개 중 그룹 {grouped}개, 아이콘 {icons}개를 적용했습니다.',
                                'grouped': grouped, 'icons': icons, 'total': len(channels)})

            if sub == 'set_channel_logo':
                channel_id = str(req.form.get('channel_id') or '').strip()
                if not channel_id:
                    raise ValueError('채널 ID가 없습니다.')
                store = _store()
                if not store.set_logo(channel_id, req.form.get('logo_url') or ''):
                    return jsonify({'ret': 'warning', 'msg': '채널을 찾지 못했습니다.'})
                channel = next(c for c in store.list_channels() if c['channel_id'] == channel_id)
                return jsonify({'ret': 'success', 'msg': '아이콘을 저장했습니다.' if channel['logo_override'] else '자동 아이콘으로 돌아갔습니다.',
                                'logo_url': channel['logo_url'], 'logo_override': channel['logo_override']})

            if sub == 'set_channel_group':
                channel_id = str(req.form.get('channel_id') or '').strip()
                if not channel_id:
                    raise ValueError('채널 ID가 없습니다.')
                store = _store()
                if not store.set_group(channel_id, req.form.get('group_name') or ''):
                    return jsonify({'ret': 'warning', 'msg': '채널을 찾지 못했습니다.'})
                channel = next(c for c in store.list_channels() if c['channel_id'] == channel_id)
                effective = resolve_output_profile(channel, store.group_profiles(), _output_profile())
                return jsonify({'ret': 'success', 'msg': '수동 그룹을 저장했습니다.' if channel['manual_group_name'] else '자동 그룹으로 돌아갔습니다.',
                                'group_name': channel['group_name'], 'manual_group_name': channel['manual_group_name'],
                                'effective_output_label': OUTPUT_PROFILE_LABELS[effective]})

            if sub == 'setting_save':
                self._save_settings(req)
                return jsonify({'ret': 'success', 'msg': '설정을 저장했습니다.'})

            if sub == 'transcode_setting_save':
                options=_transcode_form(req)
                profile, driver = _playback_form(req)
                for key,value in options.items():
                    P.ModelSetting.set('basic_transcode_'+key,str(value))
                P.ModelSetting.set('basic_hls_output_profile',profile)
                P.ModelSetting.set('basic_vaapi_driver',driver)
                _hls_options()
                stopped=0
                if _is_true(req.form.get('apply_now')):
                    HLSManager.stop_all()
                    stopped=1
                return jsonify({'ret':'success','options':options,'profile':profile,'vaapi':vaapi.status(driver),
                    'msg':('설정을 저장하고 현재 FF 재생 스트림을 종료했습니다. Shyni에서 재생을 다시 시작하세요.' if stopped else
                           '설정을 저장했습니다. 다음 새 재생부터 적용됩니다. 바로 반영하려면 저장·적용을 누르세요.')})

            if sub == 'probe_vaapi':
                driver = vaapi.validate_driver(req.form.get('basic_vaapi_driver') or P.ModelSetting.get('basic_vaapi_driver') or 'auto')
                result = vaapi.probe(driver, force=True)
                return jsonify({'ret': 'success' if result['available'] else 'warning',
                                'msg': result['message'], 'vaapi': result})

            if sub == 'probe_nvenc':
                result = nvenc.probe(force=True)
                return jsonify({'ret': 'success' if result['available'] else 'warning',
                                'msg': result['message'], 'nvenc': result})

            if sub == 'probe_device':
                base_url=req.form.get('basic_hdhomerun_base_url') or P.ModelSetting.get('basic_hdhomerun_base_url') or ''
                device=HDHomeRunClient(base_url).fetch_discover()
                changed=bool(P.ModelSetting.get('basic_hdhomerun_device_id') and P.ModelSetting.get('basic_hdhomerun_device_id')!=device.device_id)
                P.ModelSetting.set('basic_hdhomerun_base_url',device.base_url)
                P.ModelSetting.set('basic_hdhomerun_device_id',device.device_id)
                P.ModelSetting.set('basic_hdhomerun_device_name',device.friendly_name)
                info=_device_display(device)
                return jsonify({'ret': 'success', 'msg': 'HDHomeRun 장비를 확인했습니다.', 'device': info,
                                'device_changed':changed,
                                'broadcast_counts':broadcast_counts(_store().list_channels(),device.device_id)})

            if sub == 'save_tuner_limit':
                device=_playback_device()
                requested=str(req.form.get('basic_hdhomerun_tuner_limit') or 'all')
                limit=tuner_budget(requested,device.tuner_count)
                if requested!='all' and int(requested)>device.tuner_count:
                    raise ValueError(f'이 장비는 최대 {device.tuner_count}개 튜너를 사용할 수 있습니다.')
                old_limit=_tuner_count(device)
                P.ModelSetting.set('basic_hdhomerun_tuner_limit',requested)
                stopped=HLSManager.enforce_tuner_budget(device.base_url,limit)
                if limit<old_limit and _PRESET_SCAN_RUNNER.status().get('running'):
                    _PRESET_SCAN_RUNNER.cancel()
                return jsonify({'ret':'success','device':_device_display(device),
                    'msg':f'총 {device.tuner_count}개 중 FF 사용을 최대 {limit}개로 저장했습니다. 초과 FF 재생 {stopped}개를 정리했습니다.'})

            if sub == 'import_native_lineup':
                self._save_settings(req)
                snapshot = self._client_from_settings().fetch_snapshot()
                rows = snapshot_channel_rows(snapshot)
                imported = _store().replace_device_channels(
                    snapshot.device.device_id,
                    rows,
                    source_scope='native_lineup',
                )
                info = _device_display(snapshot.device)
                info.update(lineup_count=len(snapshot.lineup.channels),skipped_lineup_entries=snapshot.lineup.skipped_entries)
                P.ModelSetting.set('basic_hdhomerun_device_id', info['device_id'])
                P.ModelSetting.set('basic_hdhomerun_device_name', info['friendly_name'])
                return jsonify({
                    'ret': 'success',
                    'msg': (
                        f'HDHomeRun 장비 제공 native lineup 채널 {imported}개를 저장했습니다. '
                        '선택한 주파수 프리셋은 이 가져오기에 적용되지 않았습니다.'
                    ),
                    'device': info,
                    'summary': _store().summary(),
                    'source': 'native_lineup',
                    'preset_applied': False,
                    'notice': (
                        '이 채널은 HDHomeRun의 현재 /lineup.json 결과입니다. '
                        'CMB 프리셋으로 주파수를 스캔하거나 필터링한 결과가 아닙니다.'
                    ),
                })

            if sub == 'preview_scan_plan':
                mode, preset, plan = _scan_plan(req)
                return jsonify({
                    'ret': 'success',
                    'msg': (
                        f'{preset["name"]}: {len(plan.targets)}개 주파수/변조 조합입니다. '
                        '실제 스캔은 아래의 명시적 시작 버튼에서만 유휴 튜너를 확보해 실행됩니다.'
                    ),
                    'preset': {'id': plan.preset_id, 'delivery_system': plan.delivery_system},
                    'mux_count': len(plan.targets),
                    'changes_device_channelmap': plan.changes_device_channelmap,
                    'actual_scan_ran': False,
                    'native_lineup_used': False,
                })

            if sub == 'scan_preset':
                if not _is_true(req.form.get('confirm_preset_scan')):
                    raise HDHomeRunError('프리셋 실제 스캔 확인이 필요합니다.')
                base_url = str(P.ModelSetting.get('basic_hdhomerun_base_url') or '').strip()
                expected_device_id = str(P.ModelSetting.get('basic_hdhomerun_device_id') or '').strip().lower()
                if not base_url or not expected_device_id:
                    raise HDHomeRunError('장비 주소를 저장한 뒤 먼저 장비 확인을 실행하세요.')
                mode, preset, plan = _scan_plan(req)
                device = HDHomeRunClient(base_url).fetch_discover()
                if device.device_id != expected_device_id:
                    raise HDHomeRunError('저장된 장비와 현재 주소의 장비가 다릅니다. 장비 확인을 다시 실행하세요.')
                P.ModelSetting.set('basic_hdhomerun_scan_mode', mode)
                if mode == 'preset':
                    P.ModelSetting.set('basic_hdhomerun_preset_id', preset['id'])
                status = _PRESET_SCAN_RUNNER.start(
                    base_url=base_url,
                    device_id=device.device_id,
                    tuner_count=_tuner_count(device),
                    targets=plan.targets,
                    scan_kind='full_kr' if preset['id'] == 'kr-full-auto' else mode,
                )
                return jsonify({
                    'ret': 'success',
                    'msg': (
                        f'{preset["name"]} 프리셋 스캔을 시작했습니다. 유휴 튜너 1개만 사용하며, '
                        '발견한 주파수는 빠른 재탐색용으로 저장됩니다.'
                    ),
                    'scan': status,
                    'raw_playback_connected': True,
                })

            if sub == 'scan_preset_status':
                return jsonify({
                    'ret': 'success',
                    'scan': _PRESET_SCAN_RUNNER.status(),
                    'raw_playback_connected': True,
                })

            if sub == 'cancel_preset_scan':
                return jsonify({
                    'ret': 'success',
                    'msg': '프리셋 스캔 취소를 요청했습니다.',
                    'scan': _PRESET_SCAN_RUNNER.cancel(),
                })

            if sub == 'set_channel_enabled':
                channel_id = str(req.form.get('channel_id') or '').strip()
                if not channel_id:
                    return jsonify({'ret': 'warning', 'msg': '채널 ID가 없습니다.'})
                ok = _store().set_enabled(channel_id, _is_true(req.form.get('enabled')))
                return jsonify({'ret': 'success' if ok else 'warning', 'msg': '채널 상태를 저장했습니다.' if ok else '채널을 찾지 못했습니다.'})

            if sub == 'set_channel_selected':
                channel_id = str(req.form.get('channel_id') or '').strip()
                value = str(req.form.get('selected') or '').strip().lower()
                if not channel_id or value not in {'true', 'false'}:
                    raise ValueError('채널 ID와 사용 여부가 필요합니다.')
                selected = value == 'true'
                ok = _store().set_selected(channel_id, selected)
                return jsonify({
                    'ret': 'success' if ok else 'warning',
                    'msg': '채널 사용 여부를 저장했습니다.' if ok else '채널을 찾지 못했습니다.',
                    'selected': selected,
                })

            if sub == 'probe_source_format':
                self._save_settings(req)
                device = _playback_device()
                channels = [c for c in _store().playback_channels()
                            if c['device_id'] == device.device_id and c['raw_data'].get('source') == 'preset_scan']
                if not channels:
                    raise ValueError('먼저 이름이 있는 일반 스캔 채널을 사용 체크하세요.')
                result = _source_format(device, channels[0], force=True)
                return jsonify({'ret':'success', 'msg':'실제 MPEG-TS 영상 수신을 확인했습니다.',
                                'format':result, 'channel_name':channels[0]['name']})

            if sub == 'diagnostics':
                return jsonify({'ret': 'success', 'diagnostics': ffmpeg_capabilities()})

            return jsonify({'ret': 'warning', 'msg': f'알 수 없는 요청: {sub}'})
        except (HDHomeRunError, HDHomeRunControlError, PresetValidationError, ValueError) as exc:
            return jsonify({'ret': 'warning', 'msg': str(exc)})
        except Exception as exc:
            logger.exception('[hdhomerun_ff] ajax failed: %s', exc)
            return jsonify({'ret': 'danger', 'msg': '요청 처리 중 오류가 발생했습니다.'})

    def process_api(self, sub, req):
        try:
            if sub in {'epg.xml', _SHYNI_EPG_ENDPOINT}:
                url = (P.ModelSetting.get('basic_epg_url') or '').strip()
                if not url:
                    return Response('EPG 주소를 먼저 설정하세요.', status=404, mimetype='text/plain')
                cache = EPGCache()
                data = cache.for_playback(url)
                if sub == _SHYNI_EPG_ENDPOINT:
                    mapped_ids = {channel.get('epg_channel_id') for channel in _playlist_channels()}
                    channel_ids = {entry['id'] for entry in data['channels'] if entry['id'] in mapped_ids}
                    source = cache.file_path(data).open('rb') if channel_ids else None
                    response = Response(filtered_xmltv(source, channel_ids), mimetype='application/xml',
                                        headers={'Content-Disposition': 'attachment; filename=hdhomerun-shyni-epg.xml'})
                    if source is not None:
                        response.call_on_close(source.close)
                else:
                    response = send_file(cache.file_path(data), mimetype='application/xml', download_name='hdhomerun-epg.xml', as_attachment=True, conditional=False)
                response.headers['Cache-Control'] = 'no-store'
                response.headers['X-Content-Type-Options'] = 'nosniff'
                response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
                response.headers['Referrer-Policy'] = 'no-referrer'
                return response
            if sub == 'scan_preset.json':
                preset = _store().learned_preset(P.ModelSetting.get('basic_hdhomerun_device_id') or '')
                if not preset['muxes']:
                    return Response('아직 수신 주파수가 없습니다.', status=404)
                return Response(dump_preset(preset), mimetype='application/json', headers={
                    'Content-Disposition':'attachment; filename=hdhomerun-learned.json'})
            if sub in {'m3u_shyni', 'm3u_shyni_copy', 'm3u_shyni_mobile', 'm3u_shyni_vaapi', 'm3u_shyni_nvenc'}:
                profile = _output_profile()
                channels = _playlist_channels()
                overrides = None
                if sub == 'm3u_shyni':
                    group_profiles = _store().group_profiles()
                    overrides = {c['channel_id']: resolve_output_profile(c, group_profiles, profile) for c in channels}
                if sub == 'm3u_shyni_copy':
                    profile = 'copy'
                elif sub == 'm3u_shyni_mobile':
                    profile = 'mobile_cpu'
                elif sub == 'm3u_shyni_vaapi':
                    profile = 'mobile_vaapi'
                elif sub == 'm3u_shyni_nvenc':
                    profile = 'mobile_nvenc'
                if profile == 'mobile_nvenc' and overrides is None:
                    support = nvenc.probe()
                    if not support['available']:
                        return Response(support['message'], status=503, mimetype='text/plain')
                text = build_shyni_m3u(
                    channels,
                    request.host_url.rstrip('/'),
                    P.package_name,
                    api_key=str(req.args.get('apikey') or '').strip(),
                    output_profile=profile,
                    consumer_id=_consumer_id(req),
                    profile_overrides=overrides,
                    epg_url=(request.host_url.rstrip('/') + f'/{P.package_name}/api/{_SHYNI_EPG_ENDPOINT}?' + urlencode({'apikey': str(req.args.get('apikey') or '')}))
                        if EPGCache().snapshot((P.ModelSetting.get('basic_epg_url') or '').strip()) else '',
                )
                return Response(
                    text,
                    mimetype='audio/x-mpegurl',
                    headers={
                        'Content-Disposition': f'inline; filename=hdhomerun_shyni_{"default" if overrides is not None else profile}.m3u',
                        'Cache-Control': 'no-store',
                    },
                )

            if sub in {'hls.m3u8', 'preview.m3u8'}:
                channel_id = str(req.args.get('i') or '').strip()
                if sub == 'preview.m3u8':
                    if _PRESET_SCAN_RUNNER.status()['running']:
                        return Response('스캔 완료 후 미리보기하세요.', status=409, mimetype='text/plain')
                    channel = preview_channel(_store(), channel_id, P.ModelSetting.get('basic_hdhomerun_device_id'))
                    profile = preview_profile(channel, _store().group_profiles(), _output_profile())
                else:
                    channel = next((c for c in _playlist_channels() if c['channel_id'] == channel_id), None)
                    profile = normalise_output_profile(req.args.get('p') or _output_profile())
                consumer_id = _consumer_id(req)
                if not channel:
                    return Response('channel not found', status=404, mimetype='text/plain')
                _hls_options()
                playlist = HLSManager.get_playlist(channel_id, profile, consumer_id)
                if not playlist:
                    if channel['raw_data'].get('source') == 'preset_scan':
                        device = _playback_device()
                        if channel['device_id'] != device.device_id:
                            raise ValueError('현재 장비의 채널이 아닙니다.')
                        stream_format = _source_format(device, channel)
                        def source_factory():
                            session = RawStreamSession(device.base_url, _tuner_count(device), channel, stream_format['suffix'])
                            return session.open(), session.close
                        result = HLSManager.ensure_stream(channel_id, device.base_url, profile, consumer_id, source_factory=source_factory)
                    else:
                        device=_playback_device()
                        def native_source_factory():
                            session=NativeStreamSession(device.base_url,_tuner_count(device),channel)
                            return session.open(),session.close
                        result = HLSManager.ensure_stream(channel_id, channel['source_url'], profile, consumer_id,source_factory=native_source_factory)
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
                        relative=sub == 'preview.m3u8',
                    ),
                    mimetype='application/vnd.apple.mpegurl',
                    headers={'Cache-Control': 'no-store, no-cache, must-revalidate', 'X-Accel-Buffering': 'no',
                             'X-HDHR-Output-Profile':profile,
                             'X-HDHR-Buffer-Seconds':str(HLSManager.stream_options(channel_id,profile)['delay_seconds'])},
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
