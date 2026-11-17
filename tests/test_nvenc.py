import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import nvenc
from hls_manager import HLSManager
from playlist import build_shyni_m3u, normalise_output_profile
from transcode_config import DEFAULTS


class NvencTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_cache = HLSManager.CACHE_ROOT
        self.previous_options = dict(HLSManager.TRANSCODE)
        HLSManager.configure(cache_root=self.directory.name, transcode_options=DEFAULTS)
        nvenc._cache.clear()

    def tearDown(self):
        HLSManager.stop_all()
        HLSManager.configure(cache_root=self.previous_cache, transcode_options=self.previous_options)
        nvenc._cache.clear()
        self.directory.cleanup()

    def test_probe_requires_real_output_and_caches_success(self):
        sample = subprocess.CompletedProcess([], 0, b'mpeg2', b'')
        encoded = subprocess.CompletedProcess([], 0, b'h264', b'')
        with patch('nvenc.shutil.which', return_value='/usr/bin/ffmpeg'), patch('nvenc.subprocess.run', side_effect=[sample, encoded]) as run:
            self.assertFalse(nvenc.status()['available'])
            self.assertTrue(nvenc.probe()['available'])
            self.assertTrue(nvenc.status()['available'])
            self.assertTrue(nvenc.probe()['available'])
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertIn('h264_nvenc', command)
            self.assertIn('cuda', command)
            self.assertIn('yadif_cuda', command[command.index('-vf')+1])
            self.assertEqual(run.call_args.kwargs['input'], b'mpeg2')

    def test_probe_failure_empty_output_and_timeout_hide_support(self):
        sample = subprocess.CompletedProcess([], 0, b'mpeg2', b'')
        for result in [subprocess.CompletedProcess([], 1, b'', b'no GPU'),
                       subprocess.CompletedProcess([], 0, b'', b''),
                       subprocess.TimeoutExpired('ffmpeg', 20)]:
            with self.subTest(result=result), patch('nvenc.shutil.which', return_value='/usr/bin/ffmpeg'), patch('nvenc.subprocess.run', side_effect=[sample,result]):
                self.assertFalse(nvenc.probe(force=True)['available'])
                self.assertFalse(nvenc.status()['available'])

    def test_missing_ffmpeg_does_not_launch_probe(self):
        with patch('nvenc.shutil.which', return_value=None), patch('nvenc.subprocess.run') as run:
            self.assertFalse(nvenc.probe()['available'])
            run.assert_not_called()

    def test_cached_results_expire_and_environment_changes_invalidate_them(self):
        with patch('nvenc._key', return_value=('gpu1',)), patch('nvenc.time.monotonic', return_value=100):
            nvenc._cache[('gpu1',)] = (0, {'available': True, 'checked': True})
            self.assertTrue(nvenc.status()['available'])
        with patch('nvenc._key', return_value=('gpu2',)):
            self.assertFalse(nvenc.status()['checked'])
        with patch('nvenc._key', return_value=('gpu1',)), patch('nvenc.time.monotonic', return_value=301):
            self.assertFalse(nvenc.status()['checked'])

    def test_failed_preflight_does_not_acquire_tuner_or_launch_encoder(self):
        source = Mock()
        with patch('nvenc.probe', return_value={'available':False,'message':'GPU unavailable'}), patch('hls_manager.subprocess.Popen') as launch:
            result = HLSManager.ensure_stream('test', 'http://192.168.1.2/live', 'mobile_nvenc', source_factory=source)
        self.assertEqual(result['status'], 503)
        source.assert_not_called()
        launch.assert_not_called()

    def test_nvidia_stream_keeps_cuda_frames_and_preserves_custom_options(self):
        options = dict(DEFAULTS, height=480, fps='60', video_kbps=1500, max_kbps=2000,
                       audio_kbps=96, audio_offset_ms=-150, preset='superfast')
        HLSManager.configure(transcode_options=options)
        process = Mock()
        process.poll.return_value = None
        cleanup = Mock()
        source = Mock(return_value=('http://192.168.1.2/live', cleanup))
        with patch('nvenc.probe', return_value={'available':True}), patch('hls_manager.subprocess.Popen', return_value=process) as launch, patch.object(HLSManager, '_read_ready_playlist', return_value='#EXTM3U\n'):
            for _ in range(2):
                result = HLSManager.ensure_stream('test', 'http://192.168.1.2/live', 'mobile_nvenc', source_factory=source)
                self.assertEqual(result['ret'], 'success')
            source.assert_called_once()
            launch.assert_called_once()
            command = launch.call_args.args[0]
            self.assertIn('h264_nvenc', command)
            self.assertNotIn('h264_vaapi', command)
            self.assertNotIn('libx264', command)
            self.assertNotIn('hwdownload', ' '.join(command))
            self.assertIn('min(480,ih)', command[command.index('-vf')+1])
            self.assertIn('fps=60000/1001', command[command.index('-vf')+1])
            self.assertNotIn('scale_cuda=format', command[command.index('-vf')+1])
            self.assertEqual(command[command.index('-b:v')+1], '1500k')
            self.assertEqual(command[command.index('-b:a')+1], '96k')
            self.assertEqual(command[command.index('-preset')+1], 'fast')
            self.assertIn('(-0.150)/TB', command[command.index('-af')+1])
        HLSManager.stop_all()
        cleanup.assert_called_once()
        self.assertTrue(HLSManager.encoding_summary('mobile_nvenc')['nvenc'])
        self.assertFalse(HLSManager.encoding_summary('mobile_cpu')['nvenc'])

    def test_playlist_keeps_nvenc_profile_and_separate_cache_directory(self):
        self.assertEqual(normalise_output_profile('mobile_nvenc'), 'mobile_nvenc')
        text = build_shyni_m3u([{'channel_id':'test','name':'Test'}], 'http://ff.example', 'hdhomerun_ff', output_profile='mobile_nvenc')
        self.assertIn('p=mobile_nvenc', text)
        self.assertNotEqual(HLSManager.stream_directory('test','mobile_cpu'),
                            HLSManager.stream_directory('test','mobile_nvenc'))


if __name__ == '__main__':
    unittest.main()
