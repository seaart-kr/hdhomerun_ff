"""Validated administrator options; never accept arbitrary FFmpeg arguments."""
from __future__ import annotations

DEFAULTS = dict(height=720, video_kbps=3000, max_kbps=4000, audio_kbps=128,
                fps='30', preset='ultrafast', threads=2, delay_seconds=4, window_seconds=180,
                audio_offset_ms=0)
PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium')


def validate_options(values):
    result = dict(DEFAULTS)
    result.update(values)
    for key, low, high in [('video_kbps',500,20000),('max_kbps',500,30000),
                           ('audio_kbps',64,320),('threads',0,8),
                           ('delay_seconds',0,30),('window_seconds',120,600),('audio_offset_ms',-2000,2000)]:
        try: result[key] = int(result[key])
        except (ValueError,TypeError): raise ValueError(f'{key}: 정수를 입력하세요.')
        if not low <= result[key] <= high: raise ValueError(f'{key}: {low}~{high} 범위로 입력하세요.')
    try: result['height'] = int(result['height'])
    except (ValueError,TypeError): raise ValueError('해상도를 확인하세요.')
    if result['height'] not in (0,480,720,1080): raise ValueError('지원하지 않는 해상도입니다.')
    if result['max_kbps'] < result['video_kbps']: raise ValueError('최대 비트레이트는 목표 비트레이트 이상이어야 합니다.')
    result['fps'] = str(result['fps'])
    if result['fps'] not in ('source','25','30','50','60'): raise ValueError('지원하지 않는 프레임률입니다.')
    if result['preset'] not in PRESETS: raise ValueError('지원하지 않는 변환 속도입니다.')
    return {key:result[key] for key in DEFAULTS}


def video_filter(options):
    filters = ['yadif=0:-1:1']
    if options['fps'] != 'source':
        fps = {'30':'30000/1001','60':'60000/1001'}.get(options['fps'],options['fps'])
        filters.append('fps='+fps)
    if options['height']:
        height = options['height']; width = {480:854,720:1280,1080:1920}[height]
        filters.append(f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear")
    return ','.join(filters)


def delayed_playlist(playlist, delay_seconds, minimum_segments=3):
    """Publish completed files behind the live edge; preserve media sequence."""
    headers=[];records=[];pending=[];started=False
    for line in playlist.splitlines():
        if line.startswith(('#EXTINF:', '#EXT-X-DISCONTINUITY', '#EXT-X-PROGRAM-DATE-TIME')):
            started=True;pending.append(line)
        elif line and not line.startswith('#'):
            pending.append(line);records.append(pending);pending=[]
        elif started:
            if line != '#EXT-X-ENDLIST': pending.append(line)
        else: headers.append(line)
    hidden=0;duration=0.0
    for record in reversed(records):
        if duration >= delay_seconds:break
        duration += next((float(l.partition(':')[2].rstrip(',')) for l in record if l.startswith('#EXTINF:')),0)
        hidden+=1
    visible = records[:len(records)-hidden] if hidden else records
    if len(visible) < minimum_segments:return ''
    return '\n'.join(headers+[l for record in visible for l in record])+'\n'
