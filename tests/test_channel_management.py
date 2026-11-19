import sqlite3
import tempfile
import unittest

from channel_management import is_encrypted, preview_channel, preview_profile
from model import ChannelStore
from playlist import build_shyni_m3u


def scan(identity, name='Program 12', flags=(), device='box'):
    return dict(channel_id=identity, device_id=device, name=name, source_url='', enabled=False,
                raw_data={'source': 'preset_scan', 'flags': list(flags)})


class ChannelManagementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ChannelStore(self.temp.name + '/channels.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_unnamed_and_unselected_services_can_be_previewed_without_selecting(self):
        rows = [scan('free'), scan('control', flags=['control']), scan('empty', flags=['no data'])]
        self.store.replace_preset_scan_channels('box', rows)
        for row in rows:
            found = preview_channel(self.store, row['channel_id'], 'box')
            self.assertEqual(found['channel_id'], row['channel_id'])
            self.assertFalse(found['selected'])
        self.assertEqual(self.store.playback_channels(), [])

    def test_encrypted_cross_device_and_missing_previews_are_rejected(self):
        self.store.replace_preset_scan_channels('box', [scan('encrypted', flags=['encrypted'])])
        self.store.replace_preset_scan_channels('other', [scan('other', device='other')])
        for identity in ['encrypted', 'other', 'missing']:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                preview_channel(self.store, identity, 'box')

    def test_preview_uses_browser_h264_and_retains_supported_gpu_profile(self):
        for profile in ['copy', 'copy_video_aac']:
            self.assertEqual(preview_profile({}, {}, profile), 'mobile_cpu')
        self.assertEqual(preview_profile({'group_name': 'TV'}, {'TV': 'mobile_vaapi'}, 'copy'), 'mobile_vaapi')
        self.assertEqual(preview_profile({'output_profile': 'mobile_nvenc'}, {}, 'copy'), 'mobile_nvenc')

    def test_name_override_survives_rescan_without_changing_source_or_id(self):
        row = scan('free')
        self.store.replace_preset_scan_channels('box', [row])
        self.store.set_selected('free', False)
        self.assertTrue(self.store.set_name('free', '확인한 라디오'))
        self.store.replace_preset_scan_channels('box', [dict(row, name='Updated broadcast')])
        saved = ChannelStore(self.store.path).get_channel('free')
        self.assertEqual(saved['name'], '확인한 라디오')
        self.assertEqual(saved['broadcast_name'], 'Updated broadcast')
        self.assertEqual(saved['source_url'], '')
        self.assertFalse(saved['selected'])
        self.store.set_name('free', '')
        self.assertEqual(self.store.get_channel('free')['name'], 'Updated broadcast')

    def test_native_name_and_source_survive_lineup_refresh(self):
        row = dict(channel_id='native',device_id='box',name='Unknown',source_url='http://192.168.1.2/auto/v3',
                   raw_data={'source':'native_lineup'})
        self.store.replace_device_channels('box', [row])
        self.store.set_name('native', '내 방송')
        self.store.replace_device_channels('box', [dict(row, name='New name')])
        channel = preview_channel(self.store, 'native', 'box')
        self.assertEqual(channel['name'], '내 방송')
        self.assertEqual(channel['source_url'], row['source_url'])

    def test_invalid_names_cannot_inject_m3u_lines(self):
        self.store.replace_preset_scan_channels('box', [scan('free')])
        for name in ['x\ny', 'x\ry', 'x\0y', 'x\ty', 'x' * 257]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.set_name('free', name)
        self.assertFalse(self.store.set_name('missing', 'New name'))

    def test_bulk_delete_removes_only_explicit_encryption_in_current_device(self):
        rows = [scan('encrypted', flags=['encrypted']), scan('mixed', flags=['control','encrypted']),
                scan('control', flags=['control']), scan('empty', flags=['no data']),
                scan('unnamed'), scan('misleading_name', name='Encrypted TV')]
        self.store.replace_preset_scan_channels('box', rows)
        self.store.replace_preset_scan_channels('other', [scan('other', flags=['encrypted'], device='other')])
        self.assertEqual(self.store.delete_encrypted_channels('box'), 2)
        self.assertEqual({row['channel_id'] for row in self.store.list_channels()},
                         {'control','empty','unnamed','misleading_name','other'})
        self.store.replace_preset_scan_channels('box', rows)
        self.assertIsNotNone(self.store.get_channel('encrypted'))
        self.assertEqual(self.store.delete_encrypted_channels('box'), 2)
        with self.assertRaises(ValueError):
            self.store.delete_encrypted_channels('')
        self.assertFalse(is_encrypted({'raw_data':{'flags':'not encrypted'}}))

    def test_single_delete_is_not_a_permanent_rescan_exclusion(self):
        row = scan('free', name='MBC')
        self.store.replace_preset_scan_channels('box', [row])
        self.assertTrue(self.store.delete_channel('free'))
        self.assertFalse(self.store.delete_channel('free'))
        self.assertEqual(self.store.playback_channels(), [])
        self.store.replace_preset_scan_channels('box', [row])
        self.assertEqual(self.store.get_channel('free')['name'], 'MBC')

    def test_epg_mapping_survives_refresh_and_uses_id_without_altering_play_url(self):
        row = scan('free')
        self.store.replace_preset_scan_channels('box', [row])
        self.store.set_name('free', '수동 이름')
        self.store.set_epg('free', 'radio.example', 'EPG 라디오')
        self.assertEqual(self.store.get_channel('free')['name'], '수동 이름')
        self.store.set_epg('free', 'radio.example', 'EPG 라디오', apply_name=True)
        self.assertEqual(self.store.get_channel('free')['name'], 'EPG 라디오')
        self.store.replace_preset_scan_channels('box', [row])
        saved = self.store.get_channel('free')
        self.assertEqual(saved['epg_channel_id'], 'radio.example')
        text = build_shyni_m3u(self.store.playback_channels(), 'https://ff.example','hdhomerun_ff',epg_url='https://ff.example/epg.xml')
        self.assertIn('tvg-id="radio.example"', text)
        self.assertIn('i=free', text)
        self.assertIn('x-tvg-url="https://ff.example/epg.xml"', text)
        self.store.set_epg('free', '', '')
        self.assertEqual(self.store.get_channel('free')['name'], 'Program 12')

    def test_automatic_epg_link_preserves_manual_name_and_existing_mapping(self):
        self.store.replace_preset_scan_channels('box', [scan('free', name='MBC')])
        self.store.set_name('free', '우리 집 MBC')
        matches={'free':{'id':'mbc.example','name':'MBC'}}
        self.assertEqual(self.store.apply_epg_matches(matches), 1)
        self.assertEqual(self.store.get_channel('free')['name'], '우리 집 MBC')
        self.assertEqual(self.store.apply_epg_matches({'free':{'id':'wrong','name':'Wrong'}}), 0)
        self.assertEqual(self.store.get_channel('free')['epg_channel_id'], 'mbc.example')

    def test_migration_adds_management_fields_without_losing_selection(self):
        self.store.replace_preset_scan_channels('box', [scan('free', name='MBC')])
        self.store.set_selected('free', False)
        with sqlite3.connect(self.store.path) as db:
            for column in ['name_override','epg_channel_id','epg_channel_name']:
                db.execute('ALTER TABLE hdhomerun_ff_channel DROP COLUMN ' + column)
        saved = ChannelStore(self.store.path).get_channel('free')
        self.assertFalse(saved['selected'])
        self.assertEqual(saved['name'], 'MBC')
        self.assertEqual(saved['epg_channel_id'], '')
