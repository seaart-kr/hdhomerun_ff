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


if __name__ == '__main__':
    unittest.main()
