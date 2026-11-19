# -*- coding: utf-8 -*-
import logging

from plugin import create_plugin_instance


setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': 'basic',
    'menu': {
        'uri': __package__,
        'name': 'HDHomeRun Live for Shyni',
        'list': [
            {
                'uri': 'basic',
                'name': 'HDHomeRun',
                'list': [
                    {'uri': 'device', 'name': '장비 / 채널'},
                    {'uri': 'preset', 'name': '주파수 프리셋'},
                    {'uri': 'playback', 'name': '재생 / API'},
                    {'uri': 'diagnostics', 'name': '진단'},
                ],
            },
            {'uri': 'log', 'name': '로그'},
        ],
    },
    'setting_menu': None,
    'default_route': 'normal',
}

P = create_plugin_instance(setting)
P.recent_menu_plugin_except_list = list(getattr(P, 'recent_menu_plugin_except_list', []) or [])
if 'basic|preview' not in P.recent_menu_plugin_except_list:
    P.recent_menu_plugin_except_list.append('basic|preview')
logger = P.logger if getattr(P, 'logger', None) else logging.getLogger(__package__)
