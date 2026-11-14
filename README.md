# HDHomeRun FF

FF(FlaskFarm)에서 HDHomeRun 채널을 직접 읽고, Shyni 라이브 TV에 넣을 M3U/HLS 주소를 제공하는 플러그인입니다.

## 목표 흐름

```text
HDHomeRun → HDHomeRun FF → FFmpeg → Shyni용 M3U/HLS
```

TVHeadend는 이 플러그인의 재생 경로에 필요하지 않습니다. 기존 `tvh_m3u_plugin`과는 별도 DB·API·HLS 캐시를 사용하므로 함께 설치할 수 있습니다.

## 초기 사용 순서

1. 장비 자동 검색 또는 HDHomeRun LAN 주소를 입력합니다.
2. 장비 정보를 확인하고 내장 `한국 디지털 케이블 CMB` 프리셋을 선택합니다.
3. 장비의 안전한 native lineup을 가져와 채널 목록을 검토합니다.
4. Shyni용 `원본` 또는 `모바일 H.264/AAC` M3U 주소를 Shyni 라이브 TV에 등록합니다.

프리셋 스캔은 장비의 전역 channel map을 자동 변경하지 않습니다. 스캔 결과와 채널 목록은 각 FF 설치본의 로컬 DB에만 저장합니다.

## 재생 프로필

- `copy`: 원본 MPEG-TS 코덱을 보존합니다.
- `mobile_cpu`: FFmpeg `libx264` + `AAC`로 HLS를 생성합니다. NAS CPU를 사용하므로 채널당 부하가 생깁니다.

VAAPI는 단순히 FFmpeg 목록에 보인다고 활성화하지 않습니다. 실제 인코딩 진단이 통과한 환경에서만 별도 프로필로 추가해야 합니다.

## 보안 원칙

- Shyni M3U에는 HDHomeRun의 LAN 원본 주소가 노출되지 않습니다.
- 장비 검색/가져오기 API는 플러그인 관리자 메뉴에서만 사용합니다.
- 외부 재생은 FF의 API 키가 포함된 HLS 프록시 주소만 사용합니다.
