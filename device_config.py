"""Hardware tuner budgets and display metadata, independent of broadcast counts."""
from dataclasses import asdict


def tuner_budget(value, hardware_count):
    total=int(hardware_count)
    if not 1 <= total <= 32:raise ValueError('장비 튜너 수가 올바르지 않습니다.')
    if value is None or str(value).strip() in ('','all'):return total
    try:limit=int(value)
    except (ValueError,TypeError):raise ValueError('사용할 튜너 수를 확인하세요.')
    if not 1 <= limit <= 32:raise ValueError('사용 튜너 수는 1개 이상이어야 합니다.')
    return min(limit,total)


def discovery_summary(device):
    return asdict(device)


def usable_broadcast(channel):
    return (channel['raw_data'].get('source')=='preset_scan'
            and not set(channel['raw_data'].get('flags',[])) & {'encrypted','control','no data'}
            and not channel['name'].startswith('Program ')
            and channel['name'].lower() not in ('unknown','0',''))


def broadcast_counts(channels, device_id=None):
    scans=[c for c in channels if c['raw_data'].get('source')=='preset_scan'
           and (not device_id or c['device_id']==device_id)]
    usable=[c for c in scans if usable_broadcast(c)]
    return {'discovered':len(scans),'usable':len(usable),'selected':sum(c['selected'] for c in usable)}
