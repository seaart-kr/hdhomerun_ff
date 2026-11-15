import unittest
from transcode_config import DEFAULTS, validate_options, video_filter, delayed_playlist
from hls_manager import HLSManager


class TranscodeOptionsTest(unittest.TestCase):
    def test_invalid_options_are_rejected_before_command_building(self):
        for key,value in [('preset','ultrafast;echo bad'),('height',721),('fps','300'),
                           ('max_kbps',1000),('delay_seconds',90),('audio_offset_ms',3000)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_options(dict(DEFAULTS,**{key:value}))

    def test_custom_options_reach_real_encoder_arguments(self):
        previous=dict(HLSManager.TRANSCODE)
        try:
            options=validate_options(dict(height=480,video_kbps=1500,max_kbps=2000,
                      preset='superfast',fps='25',audio_kbps=96,audio_offset_ms=-150))
            HLSManager.configure(transcode_options=options)
            command=HLSManager.build_command('http://192.168.1.2/live','test','mobile_cpu')
            self.assertEqual(command[command.index('-b:v')+1],'1500k')
            self.assertEqual(command[command.index('-preset')+1],'superfast')
            self.assertEqual(command[command.index('-b:a')+1],'96k')
            self.assertIn('min(480,ih)',command[command.index('-vf')+1])
            self.assertIn('(-0.150)/TB',command[command.index('-af')+1])
        finally:HLSManager.configure(transcode_options=previous)

    def test_original_video_can_convert_audio_without_h264_encoding(self):
        command=HLSManager.build_command('http://192.168.1.2/live','test','copy_video_aac')
        self.assertEqual(command[command.index('-c:v')+1],'copy')
        self.assertEqual(command[command.index('-c:a')+1],'aac')
        self.assertNotIn('-vf',command)

    def test_original_copy_can_adjust_audio_timestamps_without_reencoding(self):
        previous=dict(HLSManager.TRANSCODE)
        try:
            HLSManager.configure(transcode_options=dict(DEFAULTS,audio_offset_ms=-200))
            command=HLSManager.build_command('http://192.168.1.2/live','test','copy')
            self.assertEqual(command[command.index('-c')+1],'copy')
            self.assertIn('(-0.200)/TB',command[command.index('-bsf:a')+1])
        finally:HLSManager.configure(transcode_options=previous)

    def test_delay_holds_back_real_files_and_preserves_media_sequence(self):
        raw='#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:100\n'+''.join(
            f'#EXTINF:2.0,\nsegment_{i:09d}.ts\n' for i in range(100,112))
        result=delayed_playlist(raw,12)
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:100',result)
        self.assertIn('segment_000000105.ts',result)
        self.assertNotIn('segment_000000106.ts',result)
        self.assertEqual(result.count('#EXTINF:'),6)
        short='#EXTM3U\n'+''.join(f'#EXTINF:2.0,\nsegment_{i:09d}.ts\n' for i in range(8))
        self.assertEqual(delayed_playlist(short,12),'')
