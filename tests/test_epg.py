import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from epg import EPGCache, exact_matches, validate_url

XML = b'''<?xml version="1.0" encoding="utf-8"?><tv>
<channel id="mbc.example"><display-name lang="en">MBC</display-name><display-name lang="ko">MBC TV</display-name></channel>
<channel id="kbs.example"><display-name>KBS 1</display-name></channel>
<programme channel="mbc.example" start="20261119000000 +0900"><title>Test</title></programme></tv>'''


class Opener:
    def __init__(self, body):
        self.body = body

    def open(self, request, timeout):
        return io.BytesIO(self.body)


class EPGTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = EPGCache(self.temp.name)
        self.url = 'https://example.test/epg.xml?apikey=test-source-secret'

    def tearDown(self):
        self.temp.cleanup()

    def test_xml_and_gzip_cache_keep_programmes_and_do_not_store_source_credential(self):
        for body in [XML, gzip.compress(XML)]:
            data = self.cache.refresh(self.url, Opener(body))
            self.assertEqual(data['channels'][0]['name'], 'MBC TV')
            self.assertEqual(data['channels'][0]['names'], ['MBC', 'MBC TV'])
            self.assertEqual(self.cache.file_path(data).read_bytes(), XML)
            self.assertEqual(self.cache.snapshot(self.url), data)
            self.assertNotIn('test-source-secret', Path(self.temp.name, 'index.json').read_text())
            self.assertEqual(self.cache.snapshot('https://different.test/epg'), {})

    def test_bad_feed_preserves_previous_valid_revision(self):
        original = self.cache.refresh(self.url, Opener(XML))
        for bad in [b'<html>login</html>', b'<tv/>', b'<tv><channel',
                    b'<!DOCTYPE tv [<!ENTITY x "expanded">]><tv/>',
                    '<!DOCTYPE tv [<!ENTITY x "expanded">]><tv/>'.encode('utf-16'),
                    b'<tv><channel id="same"><display-name>A</display-name></channel><channel id="same"><display-name>B</display-name></channel></tv>',
                    b'<tv><channel id=" spaced "><display-name>A</display-name></channel></tv>']:
            with self.subTest(body=bad), self.assertRaises(ValueError):
                self.cache.refresh(self.url, Opener(bad))
            self.assertEqual(self.cache.snapshot(self.url), original)
            self.assertEqual(self.cache.file_path(original).read_bytes(), XML)

    def test_download_and_decompression_limits_leave_valid_cache_unchanged(self):
        original = self.cache.refresh(self.url, Opener(XML))
        with patch('epg.MAX_DOWNLOAD', 10), self.assertRaises(ValueError):
            self.cache.refresh(self.url, Opener(XML))
        with patch('epg.MAX_XML', 50), self.assertRaises(ValueError):
            self.cache.refresh(self.url, Opener(gzip.compress(XML)))
        self.assertEqual(self.cache.snapshot(self.url), original)

    def test_stale_refresh_failure_serves_cached_feed_but_no_cache_is_an_error(self):
        original = self.cache.refresh(self.url, Opener(XML))
        with patch('epg.REFRESH_SECONDS', -1), patch.object(self.cache, 'refresh', side_effect=ValueError('unavailable')):
            self.assertEqual(self.cache.for_playback(self.url), original)
            with self.assertRaises(ValueError):
                self.cache.for_playback('https://other.test/epg')

    def test_cache_metadata_cannot_escape_cache_directory(self):
        self.cache.refresh(self.url, Opener(XML))
        p = Path(self.temp.name, 'index.json')
        data = json.loads(p.read_text());data['file_name'] = '../outside.xml';p.write_text(json.dumps(data))
        self.assertEqual(self.cache.snapshot(self.url), {})
        with self.assertRaises(ValueError):
            self.cache.file_path(data)

    def test_only_unambiguous_full_names_match_and_program_numbers_are_not_guessed(self):
        epg = [dict(id='one',name='KBS 1',names=['KBS 1','KBS1TV']),
               dict(id='two',name='BBC News',names=['BBC News']),
               dict(id='amb1',name='Shared',names=['Shared']),
               dict(id='amb2',name='Shared',names=['Shared']),
               dict(id='numeric',name='12',names=['12'])]
        channels = [dict(channel_id='kbs',name='kbs 1'),dict(channel_id='short',name='BBC'),
                    dict(channel_id='program',name='Program 12'),dict(channel_id='native',name='채널 12'),
                    dict(channel_id='num',name='12'),dict(channel_id='ambiguous',name='Shared'),
                    dict(channel_id='already',name='KBS 1',epg_channel_id='existing')]
        self.assertEqual({k:v['id'] for k,v in exact_matches(channels,epg).items()}, {'kbs':'one'})

    def test_urls_allow_private_xmltv_and_reject_unsafe_schemes_and_credentials(self):
        self.assertEqual(validate_url(' http://192.168.2.10/epg.xml.gz '), 'http://192.168.2.10/epg.xml.gz')
        self.assertEqual(validate_url(''), '')
        for url in ['file:///etc/passwd','ftp://example.test/a','http://user:pw@example.test/epg',
                    'http://169.254.169.254/metadata','http://[fe80::1]/epg','https://example.test/a\r\nb']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_download_failure_message_does_not_include_upstream_credential(self):
        class Failed:
            def open(inner, request, timeout):
                raise OSError('failed ' + self.url)
        with self.assertRaises(ValueError) as error:
            self.cache.refresh(self.url, Failed())
        self.assertNotIn('test-source-secret', str(error.exception))
