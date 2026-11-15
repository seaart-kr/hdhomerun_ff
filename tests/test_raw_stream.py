import unittest
from types import SimpleNamespace

from raw_stream import RawStreamSession, probe_http_format, is_mpeg_ts
from hdhr_control import HDHomeRunControlError
from hdhomerun import HDHomeRunError

CHANNEL = {'raw_data': {'frequency_hz':441000000, 'program_number':2073, 'lock':'8vsb'}}
TS = (b'\x47'+b'\x00'*187)*8


class FakeControl:
    def __init__(self):
        self.calls = []
        self.lost = False

    def acquire_idle_tuner(self, count):
        self.calls.append(('acquire', count))
        return SimpleNamespace(tuner_index=2, lockkey=123)

    def acquire_specific_idle_tuner(self, index):
        self.calls.append(('reacquire', index))
        self.lost = False
        return SimpleNamespace(tuner_index=index, lockkey=456)

    def get(self, name):return 'us-cable'

    def set(self, name, value, *, lockkey):
        self.calls.append(('set',name,value,lockkey))
        if self.lost:raise HDHomeRunControlError('lock no longer held')

    def release_tuner(self, lease):self.calls.append(('release',lease.lockkey))


class RawStreamTest(unittest.TestCase):
    def test_reserved_tuner_mode_and_restoration_after_http_releases_lock(self):
        control = FakeControl()
        session = RawStreamSession('192.168.2.17',4,CHANNEL,control_factory=lambda _:control)
        url = session.open()
        self.assertEqual(url,'http://192.168.2.17:5004/tuner2/ch441000000-2073?lockkey=123')
        self.assertIn(('set','/tuner2/channelmap','kr-bcast',123),control.calls)
        control.lost = True
        session.close()
        self.assertIn(('reacquire',2),control.calls)
        self.assertIn(('set','/tuner2/channelmap','us-cable',456),control.calls)
        self.assertEqual(control.calls[-1],('release',456))
        count = len(control.calls)
        session.close()
        self.assertEqual(len(control.calls),count)

    def test_http_200_html_is_not_a_working_stream_and_extension_falls_back(self):
        closed=[]
        class Session:
            def __init__(self,base,count,channel,suffix):self.suffix=suffix
            def open(self):return 'http://192.168.2.17/'+(self.suffix or 'none')
            def close(self):closed.append(self.suffix)
        class Response:
            status=200
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return self.data
        class Opener:
            def open(self,req,timeout):return Response(TS if req.full_url.endswith('/.mpeg') else b'<html>not video</html>')
        result=probe_http_format('192.168.2.17',4,CHANNEL,session_factory=Session,opener=Opener())
        self.assertEqual(result['suffix'],'.mpeg')
        self.assertEqual(closed,['','.mpeg'])
        self.assertFalse(is_mpeg_ts(b'G'+b'x'*1000))
        self.assertTrue(is_mpeg_ts(TS))

    def test_failed_formats_always_release_the_session(self):
        closed=[]
        class Session:
            def __init__(self,*args):pass
            def open(self):raise OSError('test failure')
            def close(self):closed.append(True)
        with self.assertRaises(HDHomeRunError):
            probe_http_format('192.168.2.17',4,CHANNEL,session_factory=Session)
        self.assertEqual(len(closed),2)
