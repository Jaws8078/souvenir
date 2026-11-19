# 숙소 검색 — Pilot MCP 우선

예약하지 않은 숙소 후보는 Pilot의 공개 MCP `search_hotels`로 먼저 조회한다. 호스트에 Pilot 도구가 연결돼 있으면 네이티브 도구를 사용하고, 로컬 실행기를 지원하면 `scripts/run_souvenir.py hotels --input <JSON>`을 사용한다. 이 연결은 읽기 전용이며 로그인·예약·결제·객실 확보를 수행하지 않는다. Pilot에 인증이나 호출 제한이 생기면 이를 우회하지 않고 호스트 웹 검색을 대체 경로로 사용한다.

## 실행 계약

사용자가 말한 조건을 AI가 아래 JSON으로 변환한다. 사용자는 JSON이나 Python 명령을 입력하지 않는다. 날짜는 `YYYY-MM-DD`, 금액은 KRW다. `budget_per_night_krw`는 사용자가 말한 1박 상한이며 실행기는 숙박 일수만큼 곱한 임시 총액 상한을 검색 결과에 로컬 적용한다.

```json
{
  "search": {
    "location": "Osaka",
    "check_in": "2026-12-19",
    "check_out": "2026-12-23",
    "guests": 2,
    "rooms": 1,
    "currency": "KRW",
    "guest_nationality": "KR",
    "limit": 50
  },
  "budget_per_night_krw": 100000,
  "room_filters": {
    "bed_type": "트윈",
    "refundable_only": true
  }
}
```

`search`에는 Pilot이 지원하는 지역·인원·객실·반경·성급·후기 평점·시설·숙소 유형·AI 검색 필터만 넣는다. 예산은 Pilot 검색 인자가 아니므로 공급자에 넘기지 않고 실행기에서 비교한다. `max_pages`는 로컬 테스트·진단용 선택 필드이며 일반 검색에서는 생략한다.

`room_filters`의 `bed_type`(트윈·더블·싱글·퀸·킹·벙커 또는 공급자가 반환하는 객실명)과 `refundable_only=true`(무료 취소 가능 객실만)는 `search_hotels`에 직접 전달되지 않는다. 예산 후보마다 `get_room_rates_for_hotel`을 읽기 전용으로 호출해 `roomName`·`refundable`을 확인한 뒤, 조건에 맞는 객실이 있는 숙소만 후보에 남긴다. 객실 필터를 쓰면 후보 수에 따라 추가 호출이 발생하며, 객실 요금 통화·세금·최종 취소 마감은 여전히 재확인한다.

실행기는 MCP 초기화 → `search_hotels` 규격 확인 → `next_offset` 페이지 순회 → 중복 숙소 제거 → KRW 예산 필터 순서로 실행한다. `has_more=false`여도 `next_offset`이 있으면 다음 페이지를 계속 읽는다. 빈 페이지·유효하지 않은 다음 offset·안전 상한에 도달하면 멈추고 `pages`와 `pagination_warnings`에 조사 범위를 남긴다. 공급자가 반환한 페이지를 모두 읽었다는 뜻이지 전 세계 숙소 재고를 전부 검증했다는 뜻은 아니다.

일시적인 Pilot 오류(`NETWORK`, `HTTP`, `RPC`, `RATE_LIMITED`, `SEARCH_ERROR`)는 최대 3회 지수 백오프로 자동 재시도한다. 후보 검토 보드의 같은 화면 검색도 공급자 오류 응답을 받으면 한 번 더 자동 재시도하고, 끝까지 실패하면 기존 후보를 유지한 채 `fallback=host_web_search` 상태를 표시한다. 로컬 실행기가 호스트 웹 검색 커넥터를 직접 호출하지는 않으므로, 실제 대체 후보를 만들려면 호스트의 웹 검색 결과를 다시 후보 패킷으로 전달해야 한다.

후보 검토 보드가 MCP Apps 호스트에 임베드된 경우에는 버튼이 호스트의 `souvenir_search_candidates` 도구를 호출하거나 현재 에이전트에 구조화된 follow-up을 보낸다. 호스트가 반환한 후보 패킷은 로컬 보드의 `/api/host-result`로 전달되어 같은 탭의 결과와 서버 상태를 함께 교체한다. 호스트 브리지가 없는 로컬 미리보기는 재검색하지 않고 연결 필요 상태를 표시한다.

## 가격과 검증 범위

Pilot `search_hotels`의 설명은 1박 가격을 언급하지만, 실조회에서는 같은 숙소의 날짜 길이에 따라 `price`가 달라지고 전체 숙박 요금처럼 보이는 응답이 확인됐다. 따라서 결과의 `price_scope`는 `provider_search_stay_rate_unverified`로 표시한다. 실행기의 `budget_total_krw = 1박 상한 × 숙박 일수` 비교는 **예비 필터**이며, 선택 전에 `get_room_rates_for_hotel`로 선택 객실·객실 수·인원·세금/수수료·조식·환불/취소 조건·실제 통화를 다시 확인한다. 검색 요금만으로 `예약 가능`, `최종 결제액`, `1박 확정가`라고 표현하지 않는다.

결과의 `candidates`에는 예산 필터를 통과한 숙소만 넣고, 초과·통화 불일치·가격 오류는 `rejected`에 이유와 함께 분리한다. 조건을 통과한 후보 수에 임의의 3개·5개·10개 상한을 두지 않으며, 많으면 여러 표나 연속 메시지로 끝까지 보여준다. `notion_written=false`인 읽기 전용 비교 결과를 먼저 보여주고, 사용자가 고른 후보만 숙소 DB에 저장한다. 선택은 예약 확정이나 결제가 아니다.

## 표시와 저장

후보 비교에는 숙소명·주소·평점/성급(반환된 경우)·검색 가격과 가격 범위·대략적인 1박 환산·사진/링크·조회 시각·확인 필요 항목을 넣는다. 원화가 아니거나 세금 포함 여부가 없으면 KRW로 추정 환산하지 않고 `확인 필요`로 남긴다. 저장할 때는 기존 `숙소 정보` DB의 숙소명·주소·링크·체크인/체크아웃·1박 비용·숙박 총액·사진 URL·출처를 실제로 확인한 값만 채우며, 기존 예약번호·메모·사용자 사진은 덮어쓰지 않는다. 예약 DB 연결과 `숙소 상태=확정`은 사용자가 후보를 선택하고 실제 예약 증빙을 준 뒤에만 반영한다.

## 로컬 호출 예

```bash
python3 scripts/run_souvenir.py hotels --input hotel-search.json --pretty
```

오류·인증·네트워크 제한이면 `status=error`와 `fallback=host_web_search`를 반환한다. 오류를 조건 완화나 예약 성공으로 바꾸지 않는다.
