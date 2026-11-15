# HDHomeRun FF

FF(FlaskFarm)에서 HDHomeRun 채널을 직접 읽고, Shyni 라이브 TV에 넣을 M3U/HLS 주소를 제공하는 플러그인입니다.

## 목표 흐름

```text
HDHomeRun → HDHomeRun FF → FFmpeg → Shyni용 M3U/HLS
```

TVHeadend는 이 플러그인의 재생 경로에 필요하지 않습니다. 기존 `tvh_m3u_plugin`과는 별도 DB·API·HLS 캐시를 사용하므로 함께 설치할 수 있습니다.

## 초기 사용 순서

1. 장비 자동 검색 또는 HDHomeRun LAN 주소를 입력합니다.
2. 장비 정보를 확인합니다.
3. 필요하면 장비의 native lineup을 가져와 채널 목록을 검토합니다. 이 목록은 HDHomeRun의 현재 channel map 결과이며, CMB 프리셋과 무관합니다.
4. 내장 `한국 디지털 케이블 CMB` 프리셋을 선택하고 명시적으로 실제 스캔을 시작합니다. 스캔은 유휴 튜너 하나만 잠시 잠그며 HDHomeRun의 전역 channel map은 바꾸지 않습니다.
5. 발견 서비스는 별도 발견 목록에 비활성으로 저장됩니다. 주파수 기반 재생은 장기 튜너 임대가 필요하므로 아직 Shyni M3U에 추가되지 않습니다.
6. 현재 재생 가능한 native lineup이 필요하면 Shyni용 `원본` 또는 `모바일 H.264/AAC` M3U 주소를 사용합니다.

프리셋 스캔은 유휴 튜너 예약·주파수 tune·서비스 판독을 수행하며, 결과와 채널 목록은 각 FF 설치본의 로컬 DB에만 저장합니다. 스캔 결과를 재생하려면 서비스별 튜너 임대를 유지하는 별도 재생 계층이 필요합니다.

## 재생 프로필

- `copy`: 원본 MPEG-TS 코덱을 보존합니다.
- `mobile_cpu`: FFmpeg `libx264` + `AAC`로 HLS를 생성합니다. NAS CPU를 사용하므로 채널당 부하가 생깁니다.

VAAPI는 단순히 FFmpeg 목록에 보인다고 활성화하지 않습니다. 실제 인코딩 진단이 통과한 환경에서만 별도 프로필로 추가해야 합니다.

## 보안 원칙

- Shyni M3U에는 HDHomeRun의 LAN 원본 주소가 노출되지 않습니다.
- 장비 검색/가져오기 API는 플러그인 관리자 메뉴에서만 사용합니다.
- 외부 재생은 FF의 API 키가 포함된 HLS 프록시 주소만 사용합니다.
