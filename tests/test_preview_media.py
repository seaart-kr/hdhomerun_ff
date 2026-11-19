"""Exercise the browser compatibility path with real audio-only HLS media."""
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from hls_manager import HLSManager
from transcode_config import DEFAULTS


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is needed for the real preview media test')
class PreviewMediaTest(unittest.TestCase):
    def test_audio_only_broadcast_generates_aac_hls_without_video_or_gpu(self):
        sample=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
            'sine=frequency=440:duration=16','-c:a','mp2','-f','mpegts','pipe:1'],
            capture_output=True,timeout=20,check=True).stdout
        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length',str(len(sample)))
                self.end_headers();self.wfile.write(sample)
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Source)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        old=(HLSManager.CACHE_ROOT,dict(HLSManager.TRANSCODE))
        try:
            with tempfile.TemporaryDirectory() as directory:
                HLSManager.configure(cache_root=directory,transcode_options=dict(DEFAULTS,delay_seconds=0))
                target=Path(HLSManager.stream_directory('audio-only','mobile_cpu'));target.mkdir()
                command=HLSManager.build_command('http://127.0.0.1:'+str(server.server_port)+'/radio','audio-only','mobile_cpu')
                result=subprocess.run(command,capture_output=True,timeout=25)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace')[-1200:])
                self.assertGreaterEqual((target/'index.m3u8').read_text().count('#EXTINF:'),3)
                segment=next(target.glob('*.ts'))
                probe=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,codec_name',
                    '-of','json',str(segment)],capture_output=True,text=True,timeout=10,check=True)
                self.assertIn('"aac"',probe.stdout)
                self.assertNotIn('"video"',probe.stdout)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            HLSManager.configure(cache_root=old[0],transcode_options=old[1])
