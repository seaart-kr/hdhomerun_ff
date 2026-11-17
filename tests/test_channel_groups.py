import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from channel_groups import effective_group, fetch_rules, parse_rules
from model import ChannelStore
from playlist import build_shyni_m3u


def payload():
    return {'ret':'success','rules':{
        'master_exact':{'KBS 1':{'group_name':'지상파'},'JTBC':{'group_name':'종편'},
                        'YTN':{'group_name':'뉴스'},'투니버스':{'group_name':'어린이/애니'}},
        'alias_exact':{'KBS1 HD':{'group_name':'지상파'}},
        'master_norm':{'KBS1':{'group_name':'지상파'}},
        'alias_norm':{'KBS1HD':{'group_name':'지상파'}},
    }}


def channel(name, identity='test', group='HDHomeRun', device='box'):
    return dict(channel_id=identity,device_id=device,name=name,group_name=group,
                guide_number='7',source_url='http://192.168.1.2/live',raw_data={'source':'native_lineup'})


class ChannelGroupsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ChannelStore(str(Path(self.temp.name,'channels.db')))
        self.rules = parse_rules(payload())

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_matching_and_group_aliases_without_fuzzy_guesses(self):
        for name,expected in [('KBS 1','지상파'),('kbs1 hd','지상파'),('KBS-1(HD)','지상파'),
                              ('jtbc','종합편성'),('YTN','뉴스/경제'),('투니버스','어린이/만화'),
                              ('KBS1 unrelated','그룹 없음'),('Program 12','그룹 없음')]:
            with self.subTest(name=name):
                self.assertEqual(effective_group(name,'HDHomeRun',self.rules),expected)

    def test_master_exact_precedes_conflicting_alias_and_custom_groups_win(self):
        self.rules['alias_exact']['jtbc']='스포츠'
        self.assertEqual(effective_group('JTBC','HDHomeRun',self.rules),'종합편성')
        self.assertEqual(effective_group('JTBC','내 채널',self.rules),'내 채널')

    def test_unused_empty_and_malformed_remote_rules_are_not_saved(self):
        data=payload()
        data['rules']['master_exact']['unused']={'group_name':'미사용'}
        self.assertNotIn('unused',parse_rules(data)['master_exact'])
        self.store.save_group_rules(self.rules)
        for data in [{}, {'ret':'success','rules':{}}, {'ret':'success','rules':{'master_exact':[]}}]:
            with self.subTest(data=data),self.assertRaises(ValueError):
                self.store.save_group_rules(parse_rules(data))
        self.assertEqual(self.store.group_rules()['rules'],self.rules)

    def test_existing_and_future_channels_use_persistent_rules_without_network(self):
        self.store.replace_device_channels('box',[channel('KBS1 HD'),channel('Unknown','unknown')])
        self.store.set_selected('test',False)
        with patch('channel_groups.urllib.request.urlopen') as network:
            self.assertEqual(self.store.list_channels()[0]['group_name'],'그룹 없음')
            self.store.save_group_rules(self.rules)
            reopened=ChannelStore(self.store.path)
            rows={r['channel_id']:r for r in reopened.list_channels()}
            self.assertEqual(rows['test']['group_name'],'지상파')
            self.assertFalse(rows['test']['selected'])
            self.assertEqual(rows['unknown']['group_name'],'그룹 없음')
            reopened.replace_device_channels('box',[channel('KBS1 HD'),channel('JTBC','new')])
            self.assertEqual(reopened.get_channel('new')['group_name'],'종합편성')
            self.assertFalse(next(r for r in reopened.list_channels() if r['channel_id']=='test')['selected'])
            network.assert_not_called()

    def test_custom_groups_survive_rules_refresh_and_channel_rescan(self):
        self.store.replace_device_channels('box',[channel('KBS 1',group='즐겨찾기')])
        self.store.save_group_rules(self.rules)
        self.store.replace_device_channels('box',[channel('KBS 1')])
        self.assertEqual(self.store.list_channels()[0]['group_name'],'즐겨찾기')

    def test_existing_database_gets_rules_table_without_changing_channel_rows(self):
        self.store.replace_device_channels('box',[channel('KBS 1')])
        with sqlite3.connect(self.store.path) as conn:
            before=conn.execute('SELECT * FROM hdhomerun_ff_channel').fetchall()
            conn.execute('DROP TABLE hdhomerun_ff_group_rules')
        self.store.save_group_rules(self.rules)
        self.assertEqual(self.store.list_channels()[0]['group_name'],'지상파')
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(conn.execute('SELECT * FROM hdhomerun_ff_channel').fetchall(),before)

    def test_shared_endpoint_is_read_only_and_connection_failure_keeps_cache(self):
        response=MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps(payload()).encode()
        with patch('channel_groups.urllib.request.urlopen',return_value=response) as request:
            self.assertEqual(fetch_rules(),self.rules)
            self.assertIn('/api/basic/match_rules?',request.call_args.args[0])
            self.assertEqual(request.call_args.kwargs['timeout'],20)
        self.store.save_group_rules(self.rules)
        with patch('channel_groups.urllib.request.urlopen',side_effect=OSError('offline')):
            with self.assertRaises(ValueError):fetch_rules()
        self.assertEqual(self.store.group_rules()['rules'],self.rules)

    def test_m3u_uses_shared_group_order_and_group_title(self):
        self.store.save_group_rules(self.rules)
        rows=[channel('YTN','news'),channel('JTBC','jtbc'),channel('KBS 1','kbs'),channel('Unknown','unknown')]
        self.store.replace_device_channels('box',rows)
        text=build_shyni_m3u(self.store.playback_channels(),'http://ff.example','hdhomerun_ff')
        self.assertLess(text.index('group-title="지상파"'),text.index('group-title="종합편성"'))
        self.assertLess(text.index('group-title="종합편성"'),text.index('group-title="뉴스/경제"'))
        self.assertLess(text.index('group-title="뉴스/경제"'),text.index('group-title="그룹 없음"'))


if __name__=='__main__':unittest.main()
