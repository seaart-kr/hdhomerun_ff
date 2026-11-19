"""Channel management rules shared by the admin preview and local store."""
try:
    from .playlist import resolve_output_profile
except ImportError:
    from playlist import resolve_output_profile


def is_encrypted(channel):
    flags = channel.get('raw_data', {}).get('flags', [])
    return isinstance(flags, (list, tuple, set)) and 'encrypted' in flags


def preview_channel(store, channel_id, device_id):
    channel = store.get_channel(str(channel_id or '').strip())
    if not channel or channel['device_id'] != device_id:
        raise ValueError('현재 장비의 채널을 찾지 못했습니다.')
    if is_encrypted(channel):
        raise ValueError('암호화 채널은 미리보기할 수 없습니다. 방송사 인증을 지원하는 공식 수신 장비가 필요합니다.')
    # Preview intentionally permits unnamed/unselected and control/no-data
    # rows. Those may still fail to produce media, but never enter the M3U
    # just because somebody opened a preview.
    return channel


def preview_profile(channel, group_profiles, default_profile):
    profile = resolve_output_profile(channel, group_profiles, default_profile)
    # Browser HLS cannot generally decode broadcast MPEG-2 video. Retain a
    # configured H.264 encoder or use the portable CPU compatibility profile.
    return profile if profile in {'mobile_cpu', 'mobile_1080', 'mobile_vaapi', 'mobile_nvenc'} else 'mobile_cpu'
