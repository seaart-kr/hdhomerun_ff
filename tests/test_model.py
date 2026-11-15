import tempfile
import unittest

from model import ChannelStore


def channel(channel_id='hdhr:box:7:abc', enabled=True, name='MBC', device_id='box'):
    return {
        'channel_id': channel_id,
        'device_id': device_id,
        'guide_number': '7',
        'name': name,
        'source_url': 'http://192.168.1.10:5004/auto/v7',
        'video_codec': 'mpeg2',
        'audio_codec': 'ac3',
        'enabled': enabled,
        'raw_data': {'source': 'test'},
    }


class ChannelStoreTest(unittest.TestCase):
    def scan_channel(self, name='KBS 1', suffix='3'):
        return dict(channel_id='hdhrscan:box:'+suffix, device_id='box', name=name,
                    source_url='', enabled=False, raw_data={'source':'preset_scan'})

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ChannelStore(f'{self.tempdir.name}/channels.db')

    def tearDown(self):
        self.tempdir.cleanup()

    def test_lineup_refresh_preserves_user_enabled_choice(self):
        self.assertEqual(self.store.replace_device_channels('box', [channel()]), 1)
        self.assertTrue(self.store.set_enabled('hdhr:box:7:abc', False))
        refreshed = channel(name='MBC HD')
        self.assertEqual(self.store.replace_device_channels('box', [refreshed]), 1)
        rows = self.store.list_channels()
        self.assertEqual(rows[0]['name'], 'MBC HD')
        self.assertFalse(rows[0]['enabled'])

    def test_empty_refresh_cannot_delete_existing_channels(self):
        self.store.replace_device_channels('box', [channel()])
        with self.assertRaisesRegex(ValueError, 'zero channels'):
            self.store.replace_device_channels('box', [])
        self.assertEqual(self.store.summary(), {'total': 1, 'enabled': 1})

    def test_stale_channels_are_removed_only_for_refreshed_device(self):
        self.store.replace_device_channels('box', [channel(), channel('hdhr:box:9:def', name='KBS')])
        self.store.replace_device_channels('other', [channel('hdhr:other:1:ghi', name='Other', device_id='other')])
        self.store.replace_device_channels('box', [channel()])
        self.assertEqual([row['channel_id'] for row in self.store.list_channels()], ['hdhr:box:7:abc', 'hdhr:other:1:ghi'])

    def test_preset_scan_rows_are_disabled_and_do_not_replace_native_lineup(self):
        native = channel()
        native['raw_data'] = {'source': 'native_lineup'}
        self.store.replace_device_channels('box', [native], source_scope='native_lineup')
        scan = {
            'channel_id': 'hdhrscan:box:267000000:3:abc',
            'device_id': 'box',
            'guide_number': '7.1',
            'name': 'KBS 1',
            'source_url': '',
            'enabled': False,
            'raw_data': {'source': 'preset_scan'},
        }
        self.assertEqual(self.store.replace_preset_scan_channels('box', [scan]), 1)
        self.assertEqual(self.store.summary(), {'total': 2, 'enabled': 1})
        self.assertEqual(len(self.store.list_channels(enabled_only=True)), 1)
        with self.assertRaisesRegex(ValueError, '재생 주소가 없는'):
            self.store.set_enabled(scan['channel_id'], True)
        self.assertEqual(self.store.summary(), {'total': 2, 'enabled': 1})

        refreshed_native = dict(native)
        refreshed_native['name'] = 'MBC HD'
        self.store.replace_device_channels('box', [refreshed_native], source_scope='native_lineup')
        self.assertEqual(self.store.summary(), {'total': 2, 'enabled': 1})

    def test_scan_selection_defaults_to_real_name_without_enabling_playback(self):
        rows = [self.scan_channel(name, str(i)) for i, name in enumerate(
            ['KBS 1', 'Program 12', 'Unknown', '0', '(encrypted)', '시네마천국'])]
        self.store.replace_preset_scan_channels('box', rows)
        saved = {row['name']:row for row in self.store.list_channels()}
        self.assertTrue(saved['KBS 1']['selected'])
        self.assertTrue(saved['시네마천국']['selected'])
        for name in ['Program 12', 'Unknown', '0', '(encrypted)']:
            self.assertFalse(saved[name]['selected'])
        self.assertEqual(self.store.list_channels(enabled_only=True), [])

    def test_scan_selection_survives_refresh_and_database_reopen(self):
        rows = [self.scan_channel(), self.scan_channel('Program 4', '4')]
        self.store.replace_preset_scan_channels('box', rows)
        self.assertTrue(self.store.set_selected(rows[0]['channel_id'], False))
        self.assertTrue(self.store.set_selected(rows[1]['channel_id'], True))
        self.store.replace_preset_scan_channels('box', rows)
        reopened = ChannelStore(self.store.path)
        saved = {row['channel_id']:row for row in reopened.list_channels()}
        self.assertFalse(saved[rows[0]['channel_id']]['selected'])
        self.assertTrue(saved[rows[1]['channel_id']]['selected'])
        self.assertEqual(reopened.list_channels(enabled_only=True), [])

    def test_selection_controls_native_playlist_inclusion(self):
        self.store.replace_device_channels('box', [channel()])
        self.assertTrue(self.store.set_selected(channel()['channel_id'], False))
        self.assertEqual(self.store.list_channels(enabled_only=True), [])
        self.assertTrue(self.store.set_selected(channel()['channel_id'], True))
        self.assertEqual(len(self.store.list_channels(enabled_only=True)), 1)
        self.assertFalse(self.store.set_selected('missing', True))

    def test_legacy_database_adds_selection_without_losing_channels(self):
        import sqlite3
        self.store.replace_preset_scan_channels('box', [self.scan_channel()])
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('ALTER TABLE hdhomerun_ff_channel DROP COLUMN selected')
        rows = self.store.list_channels()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['selected'])
        self.assertFalse(rows[0]['enabled'])

    def test_device_profile_and_learned_muxes_survive_restart(self):
        self.store.update_device_profile('box',learned_muxes=[{'frequency_hz':441000000,'modulation':'VSB/8'}])
        self.store.update_device_profile('box',http_stream={'suffix':'','firmware':'20260326'})
        reopened=ChannelStore(self.store.path)
        self.assertEqual(reopened.device_profile('box')['http_stream']['suffix'],'')
        self.assertEqual(reopened.learned_preset('box')['muxes'],[{'frequency_hz':441000000,'modulation':'VSB/8'}])
        self.assertEqual(reopened.learned_preset('other')['muxes'],[])

    def test_selected_raw_services_enter_playlist_except_control_or_encrypted(self):
        a=self.scan_channel('KBS 1','1');a['raw_data']['lock']='8vsb';a['raw_data']['frequency_hz']=441000000
        b=self.scan_channel('Program 2','2');b['raw_data']['flags']=['control']
        c=self.scan_channel('Encrypted TV','3');c['raw_data']['flags']=['encrypted']
        self.store.replace_preset_scan_channels('box',[a,b,c])
        self.assertEqual([c['name'] for c in self.store.playback_channels()],['KBS 1'])
        self.store.set_selected(a['channel_id'],False)
        self.assertEqual(self.store.playback_channels(),[])


if __name__ == '__main__':
    unittest.main()
