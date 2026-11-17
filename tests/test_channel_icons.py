import tempfile
import unittest
from pathlib import Path

from channel_groups import effective_logo, logo_url, parse_rules, GITHUB_ASSETS
from model import ChannelStore
from playlist import build_shyni_m3u


class ChannelIconsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ChannelStore(str(Path(self.temp.name, 'channels.db')))
        self.row = dict(channel_id='test',device_id='box',name='KBS1 HD',
                        source_url='http://192.168.1.2/live',logo_url='https://native.example/logo.png',
                        raw_data={'source':'native_lineup'})
        self.store.replace_device_channels('box',[self.row])

    def tearDown(self):
        self.temp.cleanup()

    def rules(self, **fields):
        return parse_rules({'ret':'success','rules':{'alias_exact':{'KBS1 HD':dict(group_name='지상파',**fields)}}})

    def test_shared_custom_and_provider_paths_use_standalone_github(self):
        for path,relative in [
            ('__FF_BASE_URL__/customlogo/custom logo.png','custom%20logo.png'),
            ('https://ff.example/tvh_m3u_plugin/docs/assets/kt/channel.png','kt/channel.png'),
            ('https://github.com/seaart-kr/tvh_m3u_plugin/blob/main/docs/assets/custom.png','custom.png'),
        ]:
            with self.subTest(path=path):self.assertEqual(logo_url(path),GITHUB_ASSETS+relative)
        self.assertEqual(logo_url('https://my.example/customlogo/custom.png',shared=False),
                         'https://my.example/customlogo/custom.png')

    def test_unsafe_or_invalid_urls_are_not_used(self):
        for value in ['javascript:alert(1)','file:///etc/passwd','https://user:password@host/icon.png',
                      '__FF_BASE_URL__/customlogo/../private.png','/customlogo/%2e%2e/private.png',
                      'https://host/icon.png\nmalicious','https://[invalid/icon.png']:
            with self.subTest(value=value):self.assertEqual(logo_url(value),'')
        with self.assertRaises(ValueError):self.store.set_logo('test','javascript:alert(1)')

    def test_manual_then_custom_then_provider_then_original_priority(self):
        rules=self.rules(custom_logo_url='__FF_BASE_URL__/customlogo/custom.png',
                         provider_logo_url='https://provider.example/default.png')
        self.assertEqual(effective_logo('KBS1 HD','','https://native.example/logo.png',rules),GITHUB_ASSETS+'custom.png')
        self.assertEqual(effective_logo('KBS1 HD','https://my.example/icon.png','',rules),'https://my.example/icon.png')
        provider=self.rules(custom_logo_url='javascript:bad',provider_logo_url='https://provider.example/default.png')
        self.assertEqual(effective_logo('KBS1 HD','','',provider),'https://provider.example/default.png')
        self.assertEqual(effective_logo('unknown','','https://native.example/logo.png',rules),'https://native.example/logo.png')

    def test_icon_only_catalog_and_old_group_only_cache_are_supported(self):
        rules=parse_rules({'ret':'success','rules':{'master_exact':{'icon':{'custom_logo_url':'/customlogo/icon.png'}}}})
        self.store.save_group_rules(rules)
        self.assertEqual(effective_logo('icon','','',rules),GITHUB_ASSETS+'icon.png')
        self.assertEqual(effective_logo('KBS1 HD','','https://native.example/logo.png',{'alias_exact':{'kbs1 hd':'지상파'}}),
                         'https://native.example/logo.png')

    def test_manual_icon_survives_refresh_rescan_and_reopen_then_can_reset(self):
        rules=self.rules(custom_logo_url='/customlogo/custom.png')
        self.store.save_group_rules(rules)
        self.store.set_logo('test','https://my.example/icon.png')
        self.store.replace_device_channels('box',[dict(self.row,logo_url='https://new.example/icon.png')])
        self.store.save_group_rules(self.rules(custom_logo_url='/customlogo/new-custom.png'))
        reopened=ChannelStore(self.store.path)
        self.assertEqual(reopened.list_channels()[0]['logo_url'],'https://my.example/icon.png')
        reopened.set_logo('test','')
        self.assertEqual(reopened.get_channel('test')['logo_url'],GITHUB_ASSETS+'new-custom.png')

    def test_m3u_contains_effective_tvg_logo(self):
        self.store.save_group_rules(self.rules(custom_logo_url='/customlogo/custom.png'))
        text=build_shyni_m3u(self.store.playback_channels(),'http://ff.example','hdhomerun_ff')
        self.assertIn('tvg-logo="'+GITHUB_ASSETS+'custom.png"',text)


if __name__=='__main__':unittest.main()
