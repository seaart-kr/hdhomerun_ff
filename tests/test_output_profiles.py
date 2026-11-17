import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from model import ChannelStore
from playlist import build_shyni_m3u, resolve_output_profile


def channel(identity, group):
    return dict(channel_id=identity,device_id='box',name=identity,group_name=group,
                source_url='http://192.168.1.2/live',raw_data={'source':'native_lineup'})


class OutputProfilesTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ChannelStore(str(Path(self.temp.name,'channels.db')))
        self.channels=[channel('KBS1','지상파'),channel('SBS','지상파'),
                       channel('Sports','스포츠'),channel('Movie','영화')]
        self.store.replace_device_channels('box',self.channels)

    def tearDown(self):self.temp.cleanup()

    def effective(self, default='copy'):
        profiles=self.store.group_profiles()
        return {c['channel_id']:resolve_output_profile(c,profiles,default)
                for c in self.store.playback_channels()}

    def test_channel_then_group_then_default_and_clear_to_inherit(self):
        self.store.set_group_profile('지상파','mobile_vaapi')
        self.store.set_group_profile('스포츠','copy')
        self.store.set_channel_profile('SBS','mobile_cpu')
        self.assertEqual(self.effective(),{'KBS1':'mobile_vaapi','SBS':'mobile_cpu',
                                          'Sports':'copy','Movie':'copy'})
        self.store.set_channel_profile('SBS','')
        self.assertEqual(self.effective()['SBS'],'mobile_vaapi')
        self.store.set_group_profile('지상파','')
        self.assertEqual(self.effective()['SBS'],'copy')

    def test_settings_survive_rescan_and_database_reopen(self):
        self.store.set_group_profile('스포츠','copy')
        self.store.set_channel_profile('SBS','mobile_nvenc')
        self.store.replace_device_channels('box',self.channels)
        reopened=ChannelStore(self.store.path)
        self.assertEqual(reopened.group_profiles(),{'스포츠':'copy'})
        self.assertEqual(reopened.get_channel('SBS')['output_profile'],'mobile_nvenc')

    def test_uses_effective_manual_group_and_follows_future_group_changes(self):
        self.store.set_group_profile('내 채널','mobile_cpu')
        self.store.set_group('Movie','내 채널')
        self.assertEqual(self.effective()['Movie'],'mobile_cpu')
        self.store.set_group('Movie','')
        self.assertEqual(self.effective()['Movie'],'copy')

    def test_unconfigured_update_preserves_existing_global_profile(self):
        self.assertEqual(set(self.effective('mobile_vaapi').values()),{'mobile_vaapi'})
        self.assertTrue(all(c['output_profile']=='' for c in self.store.list_channels()))

    def test_invalid_profiles_and_groups_do_not_modify_existing_values(self):
        self.store.set_channel_profile('SBS','copy')
        self.store.set_group_profile('스포츠','copy')
        for value in ['auto','libx264','copy; echo bad','unknown']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.store.set_channel_profile('SBS',value)
                with self.assertRaises(ValueError):self.store.set_group_profile('스포츠',value)
        for name in ['', 'x'*101,'group\ninjection']:
            with self.assertRaises(ValueError):self.store.set_group_profile(name,'copy')
        self.assertEqual(self.store.group_profiles(),{'스포츠':'copy'})
        self.assertEqual(self.store.get_channel('SBS')['output_profile'],'copy')
        self.assertFalse(self.store.set_channel_profile('missing','copy'))

    def test_mixed_m3u_and_explicit_fixed_profile_playlists(self):
        self.store.set_group_profile('지상파','mobile_vaapi')
        self.store.set_channel_profile('SBS','copy_video_aac')
        rows=self.store.playback_channels()
        text=build_shyni_m3u(rows,'https://ff.example','hdhomerun_ff',output_profile='copy',
                            profile_overrides=self.effective(),api_key='test-key',consumer_id='viewer')
        observed={}
        for line in text.splitlines():
            if line.startswith('https://'):
                query=parse_qs(urlsplit(line).query)
                observed[query['i'][0]]=query['p'][0]
                self.assertEqual(query['apikey'],['test-key'])
                self.assertEqual(query['client'],['viewer'])
        self.assertEqual(observed,self.effective())
        fixed=build_shyni_m3u(rows,'https://ff.example','hdhomerun_ff',output_profile='mobile_cpu')
        self.assertEqual(fixed.count('p=mobile_cpu'),4)
        self.assertNotIn('192.168.1.2',text)

    def test_migration_adds_override_without_changing_existing_channel_settings(self):
        self.store.set_selected('SBS',False)
        self.store.set_group('Movie','즐겨찾기')
        self.store.set_logo('Movie','https://icons.example/movie.png')
        with sqlite3.connect(self.store.path) as c:
            c.execute('ALTER TABLE hdhomerun_ff_channel DROP COLUMN output_profile')
            c.execute('DROP TABLE hdhomerun_ff_group_profile')
        reopened=ChannelStore(self.store.path)
        rows={c['channel_id']:c for c in reopened.list_channels()}
        self.assertEqual(rows['Movie']['group_name'],'즐겨찾기')
        self.assertEqual(rows['Movie']['logo_url'],'https://icons.example/movie.png')
        self.assertFalse(rows['SBS']['selected'])
        self.assertTrue(all(c['output_profile']=='' for c in rows.values()))
        self.assertEqual(reopened.group_profiles(),{})

    def test_corrupt_or_unsupported_override_does_not_replace_valid_lower_priority(self):
        self.assertEqual(resolve_output_profile({'output_profile':'bad','group_name':'지상파'},
                                               {'지상파':'mobile_cpu'},'copy'),'mobile_cpu')


if __name__=='__main__':unittest.main()
