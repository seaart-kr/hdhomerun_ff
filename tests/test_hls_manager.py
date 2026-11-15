# -*- coding: utf-8 -*-
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from hls_manager import HLSManager


class HLSManagerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        HLSManager.stop_all()
        HLSManager.configure(cache_root=self.tempdir.name, idle_timeout=60, start_timeout=1)

    def tearDown(self):
        HLSManager.stop_all()
        self.tempdir.cleanup()

    def test_copy_command_has_safe_hls_retention(self):
        command = HLSManager.build_command("http://192.168.1.10:5004/auto/v7", "device:7", "copy")
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertIn("-hls_time", command)
        self.assertEqual(command[command.index("-hls_time") + 1], "2")
        self.assertEqual(command[command.index("-hls_list_size") + 1], "30")
        self.assertEqual(command[command.index("-hls_delete_threshold") + 1], "30")
        self.assertIn("delete_segments+omit_endlist+independent_segments+temp_file", command)

    def test_shared_stream_reserves_once_and_idle_cleanup_releases_tuner(self):
        cleanup = Mock()
        factory = Mock(return_value=('http://192.168.1.10:5004/tuner0/ch441000000-3', cleanup))
        process = Mock()
        process.poll.return_value = None
        with patch('hls_manager.subprocess.Popen', return_value=process), patch.object(HLSManager, '_read_ready_playlist', return_value='#EXTM3U\n#EXTINF:2,\nsegment_000000001.ts\n'):
            self.assertEqual(HLSManager.ensure_stream('scan:3', 'http://192.168.1.10', source_factory=factory)['ret'],'success')
            self.assertEqual(HLSManager.ensure_stream('scan:3', 'http://192.168.1.10', source_factory=factory)['ret'],'success')
            factory.assert_called_once()
            cleanup.assert_not_called()
            HLSManager._streams[HLSManager.stream_key('scan:3')]['last_access'] = time.monotonic()-61
            HLSManager.cleanup()
        cleanup.assert_called_once()
        process.terminate.assert_called_once()

    def test_encoder_failure_releases_acquired_tuner(self):
        cleanup = Mock()
        factory = Mock(return_value=('http://192.168.1.10:5004/tuner0/ch441000000-3', cleanup))
        with patch('hls_manager.subprocess.Popen', side_effect=OSError('failed')):
            result=HLSManager.ensure_stream('scan:3', 'http://192.168.1.10', source_factory=factory)
        self.assertEqual(result['ret'],'warning')
        cleanup.assert_called_once()

    def test_mobile_cpu_command_transcodes_video_and_audio(self):
        command = HLSManager.build_command("http://192.168.1.10:5004/auto/v7", "device:7", "mobile_cpu")
        self.assertIn("libx264", command)
        self.assertEqual(command[command.index('-preset')+1], 'ultrafast')
        self.assertIn("aac", command)
        self.assertIn("yuv420p", command)
        vf = command[command.index('-vf')+1]
        self.assertIn("yadif=0:-1:1", vf)
        self.assertIn('min(1280,iw)', vf)
        self.assertIn('min(720,ih)', vf)
        self.assertEqual(command[command.index('-b:v')+1], '3000k')
        self.assertEqual(command[command.index('-maxrate')+1], '4000k')
        self.assertIn("expr:gte(t,n_forced*2)", command)
        self.assertNotIn("copy", command)
        self.assertNotIn("vaapi", " ".join(command).lower())

    def test_high_quality_profile_retains_original_dimensions(self):
        command=HLSManager.build_command('http://192.168.1.10/live','channel','mobile_1080')
        self.assertEqual(command[command.index('-vf')+1], 'yadif=0:-1:0')
        self.assertNotIn('-b:v',command)

    def test_stream_identity_is_isolated_by_profile(self):
        copy_dir = HLSManager.stream_directory("device:7", "copy")
        mobile_dir = HLSManager.stream_directory("device:7", "mobile_cpu")
        self.assertNotEqual(copy_dir, mobile_dir)
        self.assertTrue(copy_dir.startswith(os.path.realpath(self.tempdir.name) + os.path.sep))
        self.assertTrue(mobile_dir.startswith(os.path.realpath(self.tempdir.name) + os.path.sep))

    def test_invalid_profile_and_upstream_are_rejected(self):
        with self.assertRaises(ValueError):
            HLSManager.build_command("http://192.168.1.10/live", "channel", "vaapi")
        with self.assertRaises(ValueError):
            HLSManager.build_command("file:///etc/passwd", "channel", "copy")
        with self.assertRaises(ValueError):
            HLSManager.build_command("http://user:secret@192.168.1.10/live", "channel", "copy")

    def test_segment_reader_rejects_traversal_and_reads_only_valid_name(self):
        directory = HLSManager.stream_directory("device:7", "copy")
        os.makedirs(directory, mode=0o700)
        filename = "segment_000000001.ts"
        with open(os.path.join(directory, filename), "wb") as handle:
            handle.write(b"transport-stream")

        self.assertEqual(HLSManager.read_segment("device:7", filename, "copy"), b"transport-stream")
        self.assertIsNone(HLSManager.read_segment("device:7", "../segment_000000001.ts", "copy"))
        self.assertIsNone(HLSManager.read_segment("device:7", "index.m3u8", "copy"))
        self.assertIsNone(HLSManager.read_segment("device:7", "segment_1.ts", "copy"))

    def test_ready_playlist_requires_current_hls_manifest(self):
        directory = HLSManager.stream_directory("device:7", "copy")
        os.makedirs(directory, mode=0o700)
        playlist_path = os.path.join(directory, "index.m3u8")
        with open(playlist_path, "w", encoding="utf-8") as handle:
            handle.write("#EXTM3U\n"+''.join(f'#EXTINF:2.0,\nsegment_{i:09d}.ts\n' for i in range(3)))
        self.assertIn("#EXTINF", HLSManager.get_playlist("device:7", "copy"))
        old = time.time() - HLSManager.PLAYLIST_MAX_AGE - 1
        os.utime(playlist_path, (old, old))
        self.assertEqual(HLSManager.get_playlist("device:7", "copy"), "")

    def test_initial_playback_waits_for_three_segments(self):
        directory=HLSManager.stream_directory('device:7','mobile_cpu')
        os.makedirs(directory)
        for count in (1,2,3):
            with open(os.path.join(directory,'index.m3u8'),'w') as handle:
                handle.write('#EXTM3U\n'+''.join(f'#EXTINF:2.0,\nsegment_{i:09d}.ts\n' for i in range(count)))
            self.assertEqual(bool(HLSManager.get_playlist('device:7','mobile_cpu')), count==3)

    def test_clear_refuses_cache_root_or_outside_path(self):
        with self.assertRaises(ValueError):
            HLSManager._safe_clear_directory(self.tempdir.name)
        with self.assertRaises(ValueError):
            HLSManager._safe_clear_directory(os.path.dirname(self.tempdir.name))


if __name__ == "__main__":
    unittest.main()
