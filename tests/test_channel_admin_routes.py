import importlib.util
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from flask import Flask, request
from jinja2 import ChoiceLoader, DictLoader
from model import ChannelStore


class Settings:
    data = {}

    @classmethod
    def get(cls, key):return cls.data.get(key, '')

    @classmethod
    def set(cls, key, value):cls.data[key] = value

    @classmethod
    def to_dict(cls):return dict(cls.data)


class AdminRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HDHOMERUN_FF_EPG_DIR': self.temp.name + '/epg'})
        self.env.start()
        root = Path(__file__).resolve().parents[1]
        name = '_hdhr_admin_test'
        for key in list(sys.modules):
            if key == name or key.startswith(name + '.'):
                del sys.modules[key]
        package = types.ModuleType(name);package.__path__ = [str(root)];sys.modules[name] = package
        framework = types.ModuleType('plugin')
        framework.PluginModuleBase = type('PluginModuleBase', (), {})
        framework.ToolUtil = types.SimpleNamespace(make_apikey_url=lambda path: path + '?apikey=test-only-key')
        setup = types.ModuleType(name + '.setup')
        setup.P = types.SimpleNamespace(package_name='hdhomerun_ff', ModelSetting=Settings)
        setup.logger = logging.getLogger('hdhr-test')
        sys.modules[name + '.setup'] = setup
        with patch.dict(sys.modules, {'plugin': framework}):
            spec = importlib.util.spec_from_file_location(name + '.mod_basic', root / 'mod_basic.py')
            self.module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = self.module
            spec.loader.exec_module(self.module)
        Settings.data = dict(self.module.ModuleBasic.db_default, basic_hdhomerun_device_id='box')
        self.store = ChannelStore(self.temp.name + '/channels.db')
        self.rows = [dict(channel_id=k,device_id='box',name='Program 3' if k=='free' else k,
                         source_url='',enabled=False,raw_data={'source':'preset_scan','flags':flags})
                     for k,flags in [('free',[]),('encrypted',['encrypted']),('control',['control']),('empty',['no data'])]]
        self.store.replace_preset_scan_channels('box',self.rows)
        self.module._store = lambda: self.store
        self.running = False
        self.module._PRESET_SCAN_RUNNER = types.SimpleNamespace(status=lambda:{'running':self.running})
        self.instance = object.__new__(self.module.ModuleBasic);self.instance.name='basic'
        self.app = Flask(__name__,template_folder=str(root/'templates'),static_folder=str(root/'static'),static_url_path='/hdhomerun_ff/static')
        self.app.jinja_loader = ChoiceLoader([self.app.jinja_loader,DictLoader({'base.html':'{% block content %}{% endblock %}'})])
        self.app.add_url_rule('/ajax/<sub>',endpoint='ajax',view_func=lambda sub:self.instance.process_ajax(sub,request),methods=['GET','POST'])
        self.app.add_url_rule('/menu/<sub>',endpoint='menu',view_func=lambda sub:self.instance.process_menu(sub,request))
        self.app.add_url_rule('/api/<sub>',endpoint='api',view_func=lambda sub:self.instance.process_api(sub,request))
        self.client=self.app.test_client()

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def seed_epg(self):
        url='https://epg.example.test/xml?apikey=test-source-secret'
        Settings.set('basic_epg_url',url)
        class Source:
            def open(self,req,timeout):return io.BytesIO(b'<tv><channel id="radio.example"><display-name>Radio Name</display-name></channel></tv>')
        return self.module.EPGCache().refresh(url,Source())

    def test_names_and_delete_require_post_confirmation_and_current_device(self):
        self.assertEqual(self.client.get('/ajax/set_channel_name').status_code,405)
        response=self.client.post('/ajax/set_channel_name',data={'channel_id':'free','name':'Radio Name'}).get_json()
        self.assertEqual(response['name'],'Radio Name')
        self.assertEqual(self.client.post('/ajax/delete_channel',data={'channel_id':'free'}).get_json()['ret'],'warning')
        self.running=True
        self.assertEqual(self.client.post('/ajax/delete_channel',data={'channel_id':'free','confirm':'yes'}).get_json()['ret'],'warning')
        self.assertIsNotNone(self.store.get_channel('free'))
        self.running=False
        self.store.replace_preset_scan_channels('other',[dict(self.rows[0],channel_id='other',device_id='other')])
        self.assertEqual(self.client.post('/ajax/set_channel_name',data={'channel_id':'other','name':'Wrong'}).get_json()['ret'],'warning')

    def test_bulk_endpoint_deletes_encryption_only_and_rescan_restores_it(self):
        ret=self.client.post('/ajax/delete_encrypted_channels',data={'confirm':'yes'}).get_json()
        self.assertEqual(ret['deleted'],1)
        self.assertEqual({c['channel_id'] for c in self.store.list_channels()},{'free','control','empty'})
        self.store.replace_preset_scan_channels('box',self.rows)
        self.assertIsNotNone(self.store.get_channel('encrypted'))

    def test_preview_and_device_templates_escape_names_and_bundle_player(self):
        self.store.set_name('free','<script>alert("name")</script>')
        self.seed_epg()
        response=self.client.get('/menu/preview?i=free')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>alert("name")</script>',html)
        self.assertIn('/hdhomerun_ff/static/vendor/hls.light.min.js',html)
        with patch.object(self.module.ToolUtil,'make_apikey_url',return_value='http://internal:9999/hdhomerun_ff/api/preview.m3u8?apikey=test-only-key'):
            proxied=self.client.get('/menu/preview?i=free').get_data(as_text=True)
        self.assertNotIn('http://internal:9999',proxied)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.client.get('/hdhomerun_ff/static/vendor/hls.light.min.js').status_code,200)
        device=self.client.get('/menu/device')
        self.assertEqual(device.status_code,200)
        self.assertIn('암호화 채널만 일괄 삭제',device.get_data(as_text=True))
        self.assertIn('XMLTV EPG 주소',device.get_data(as_text=True))

    def test_preview_blocks_encryption_and_active_scan(self):
        self.assertEqual(self.client.get('/menu/preview?i=encrypted').status_code,400)
        self.running=True
        self.assertEqual(self.client.get('/menu/preview?i=free').status_code,409)
        self.assertEqual(self.client.get('/api/preview.m3u8?i=free').status_code,409)

    def test_preview_api_can_serve_unselected_channel_without_enabling_it(self):
        playlist='#EXTM3U\n#EXTINF:2,\nsegment_000000001.ts\n'
        with patch.object(self.module.HLSManager,'get_playlist',return_value=playlist):
            response=self.client.get('/api/preview.m3u8?i=free&apikey=test-only-key')
        self.assertEqual(response.status_code,200)
        self.assertIn('hls_segment.ts',response.get_data(as_text=True))
        segment=next(line for line in response.get_data(as_text=True).splitlines() if line and not line.startswith('#'))
        self.assertTrue(segment.startswith('/hdhomerun_ff/api/hls_segment.ts'))
        self.assertFalse(self.store.get_channel('free')['selected'])
        self.assertEqual(self.store.playback_channels(),[])

    def test_epg_selection_name_and_id_reach_m3u_and_xmltv(self):
        self.seed_epg()
        result=self.client.post('/ajax/set_channel_epg',data={'channel_id':'free','epg_channel_id':'radio.example'}).get_json()
        self.assertEqual(result['name'],'Radio Name')
        result=self.client.post('/ajax/set_channel_epg',data={'channel_id':'free','epg_channel_id':'missing'}).get_json()
        self.assertEqual(result['ret'],'warning')
        response=self.client.get('/api/m3u_shyni?apikey=test-only-key')
        text=response.get_data(as_text=True)
        self.assertIn('tvg-id="radio.example"',text)
        self.assertIn('x-tvg-url=',text)
        self.assertNotIn('test-source-secret',text)
        epg=self.client.get('/api/epg.xml')
        self.assertEqual(epg.status_code,200)
        self.assertIn('attachment',epg.headers['Content-Disposition'])
        self.assertEqual(epg.headers['X-Content-Type-Options'],'nosniff')
        Settings.set('basic_epg_url','')
        cleared=self.client.post('/ajax/set_channel_epg',data={'channel_id':'free','epg_channel_id':''}).get_json()
        self.assertEqual(cleared['ret'],'success')
