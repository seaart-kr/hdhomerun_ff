import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vaapi
from hls_manager import HLSManager
from playlist import build_shyni_m3u


class VaapiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.device = Path(self.directory.name, 'renderD128')
        self.device.touch()
        self.driver_dir = Path(self.directory.name, 'driver')
        self.driver_dir.mkdir()
        self.library = self.driver_dir / 'i965_drv_video.so'
        self.library.write_bytes(b'test driver')
        self.device_patch = patch.object(vaapi, 'DEVICE', str(self.device))
        self.driver_patch = patch.object(vaapi, 'MANAGED_I965', str(self.driver_dir))
        self.device_patch.start()
        self.driver_patch.start()
        vaapi._cache.clear()
        self.previous_driver = HLSManager.VAAPI_DRIVER
        self.previous_cache = HLSManager.CACHE_ROOT
        HLSManager.configure(cache_root=self.directory.name, vaapi_driver='i965')

    def tearDown(self):
        HLSManager.stop_all()
        HLSManager.configure(cache_root=self.previous_cache, vaapi_driver=self.previous_driver)
        vaapi._cache.clear()
        self.driver_patch.stop()
        self.device_patch.stop()
        self.directory.cleanup()

    def test_managed_driver_environment_is_scoped_to_child(self):
        before = dict(os.environ)
        env = vaapi.environment('i965')
        self.assertEqual(env['LIBVA_DRIVER_NAME'], 'i965')
        self.assertEqual(env['LIBVA_DRIVERS_PATH'], str(self.driver_dir))
        self.assertEqual(dict(os.environ), before)
        self.assertNotEqual(vaapi.environment('iHD').get('LIBVA_DRIVERS_PATH'), str(self.driver_dir))
        with self.assertRaises(ValueError):
            vaapi.environment('i965; command')

    def test_successful_real_probe_is_cached_and_driver_changes_invalidate_it(self):
        completed = subprocess.CompletedProcess([], 0, '', '')
        with patch('vaapi.shutil.which', return_value='/usr/bin/ffmpeg'), patch('vaapi.subprocess.run', return_value=completed) as run:
            self.assertTrue(vaapi.probe('i965')['available'])
            self.assertTrue(vaapi.status('i965')['available'])
            self.assertTrue(vaapi.probe('i965')['available'])
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertIn('h264_vaapi', command)
            self.assertIn('format=nv12,hwupload', command)
            self.assertIn('resource.setrlimit', command[2])
            self.library.write_bytes(b'changed driver contents')
            self.assertFalse(vaapi.status('i965')['checked'])
            vaapi.probe('i965')
            self.assertEqual(run.call_count, 2)

    def test_probe_failure_and_timeout_do_not_report_support(self):
        with patch('vaapi.shutil.which', return_value='/usr/bin/ffmpeg'), patch('vaapi.subprocess.run', return_value=subprocess.CompletedProcess([], -6, '', 'driver failed')):
            result = vaapi.probe('i965')
            self.assertFalse(result['available'])
            self.assertIn('driver failed', result['detail'])
        with patch('vaapi.shutil.which', return_value='/usr/bin/ffmpeg'), patch('vaapi.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 20)):
            self.assertFalse(vaapi.probe('i965', force=True)['available'])

    def test_missing_gpu_does_not_launch_a_probe(self):
        self.device.unlink()
        with patch('vaapi.subprocess.run') as run:
            self.assertFalse(vaapi.probe('i965')['available'])
            run.assert_not_called()

    def test_failed_probe_does_not_acquire_a_tuner_or_start_a_stream(self):
        source = Mock()
        with patch('vaapi.probe', return_value={'available':False, 'message':'검사 실패'}), patch('hls_manager.subprocess.Popen') as start:
            result = HLSManager.ensure_stream('test', 'http://192.168.1.2/live', 'mobile_vaapi', source_factory=source)
        self.assertEqual(result['status'], 503)
        source.assert_not_called()
        start.assert_not_called()

    def test_vaapi_stream_uses_scoped_driver_and_m3u_keeps_its_profile(self):
        process = Mock()
        process.poll.return_value = None
        with patch('vaapi.probe', return_value={'available':True}), patch('hls_manager.subprocess.Popen', return_value=process) as start, patch.object(HLSManager, '_read_ready_playlist', return_value='#EXTM3U\n'):
            result = HLSManager.ensure_stream('test', 'http://192.168.1.2/live', 'mobile_vaapi')
            self.assertEqual(result['ret'], 'success')
            self.assertEqual(start.call_args.kwargs['env']['LIBVA_DRIVERS_PATH'], str(self.driver_dir))
            command = start.call_args.args[0]
            self.assertIn('h264_vaapi', command)
            self.assertNotIn('libx264', command)
            self.assertNotIn('-preset', command)
            self.assertIn('format=nv12,hwupload', command[command.index('-vf')+1])
            self.assertEqual(command[command.index('-c:a')+1], 'aac')
            self.assertTrue(HLSManager.encoding_summary('mobile_vaapi')['vaapi'])
        text = build_shyni_m3u([{'channel_id':'test','name':'Test'}], 'http://ff.example', 'hdhomerun_ff', output_profile='mobile_vaapi')
        self.assertIn('p=mobile_vaapi', text)
