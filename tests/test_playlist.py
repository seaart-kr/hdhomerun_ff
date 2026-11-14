from playlist import build_shyni_m3u, normalise_output_profile


def test_normalise_output_profile_uses_safe_copy_default():
    assert normalise_output_profile('mobile_cpu') == 'mobile_cpu'
    assert normalise_output_profile('unknown') == 'copy'


def test_m3u_uses_proxy_and_never_source_url():
    text = build_shyni_m3u(
        [{
            'channel_id': 'device:101',
            'name': 'KBS 1',
            'guide_number': '9.1',
            'group_name': '지상파',
            'source_url': 'http://192.168.1.10:5004/auto/v9',
            'enabled': True,
        }],
        'https://ff.example/',
        'hdhomerun_ff',
        'PUBLIC',
        'mobile_cpu',
    )
    assert '#EXTM3U' in text
    assert 'https://ff.example/hdhomerun_ff/api/hls.m3u8?' in text
    assert 'p=mobile_cpu' in text
    assert '192.168.1.10' not in text


def test_m3u_keeps_device_text_to_one_safe_line():
    text = build_shyni_m3u(
        [{
            'channel_id': 'device:101',
            'name': 'KBS "News"\nInjected',
            'group_name': 'Public\rTV',
            'enabled': True,
        }],
        'https://ff.example/',
        'hdhomerun_ff',
    )
    lines = text.splitlines()
    assert len(lines) == 3
    assert 'KBS \'News\' Injected' in lines[1]
    assert 'group-title="Public TV"' in lines[1]
