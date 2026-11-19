# 후보 검토 보드

`candidate_review_web.py`는 항공권·숙소·장소 검색 결과를 터틀넥의 단계형 문제 화면처럼 한 번에 하나의 검토 작업으로 보여주는 로컬 웹 보드다. 검색 결과는 공급자 페이지를 모두 읽고 만든 원본 패킷을 사용하며, 보드의 검색창·금액·정렬 필터는 이미 받은 후보를 표시하는 단계에서만 적용한다.

## 실행

로컬 실행기는 다음처럼 후보 JSON을 읽어 `127.0.0.1`에만 서버를 연다.

```bash
python3 skills/souvenir/scripts/run_souvenir.py candidate-review \
  --input /path/to/review-packet.json \
  --output /private/tmp/souvenir-selection.json
```

첫 줄의 `web_url`을 호스트의 브라우저 미리보기로 열면 된다. `--open`은 사용자가 명시적으로 로컬 브라우저를 열고 싶을 때만 사용한다. 기본 포트는 0이라 사용 가능한 포트를 고르고, 외부 주소에 바인딩하지 않는다.

## 입력 계약

한 파트의 기존 결과를 그대로 넘길 수 있다. `candidates`가 있는 Kiwi 결과는 `flights`, Pilot 결과는 `hotels`로 추론하며, 장소 결과는 `--kind places`를 붙인다.

세 파트를 한 화면에 묶으려면 다음 형태를 사용한다.

```json
{
  "trip": {
    "destination": "오사카",
    "start_date": "2026-12-19",
    "end_date": "2026-12-23",
    "travelers": "성인 2명"
  },
  "sections": {
    "flights": {"provider": "kiwi-mcp", "candidates": []},
    "hotels": {"provider": "pilot-mcp", "candidates": []},
    "places": {"provider": "Google Places API (New)", "candidates": []}
  }
}
```

각 후보는 `id`가 없어도 이름·호텔 ID·지도 URL·구간을 이용해 화면용 ID가 만들어진다. 가격과 객실 조건이 다시 확인되지 않은 값은 카드에 `확인 필요`를 붙인다. 사진·예약·지도 링크는 HTTPS일 때만 카드에서 연다.

## 같은 화면에서 다시 검색

보드의 `검색 조건 수정`을 열어 날짜·공항·지역·예산·침대·취소 조건을 바꾸고 `다시 검색`을 누르면 새 탭이나 새 로컬 사이트를 열지 않는다. 현재 탭이 유지된 채 로컬 서버의 `POST /api/search`가 해당 파트의 Kiwi 또는 Pilot 검색을 다시 호출하고, 예산·객실 필터를 재적용한 뒤 카드와 후보 수를 교체한다. 새 결과가 기존 선택을 대체할 수 있으므로 검색이 끝나면 선택 후보는 초기화된다.

장소 재검색은 `GOOGLE_PLACES_API_KEY`가 있는 로컬 환경에서 Google Places API (New)를 사용한다. 키가 없는 호스트 웹 검색 결과는 기존 후보를 검토할 수 있지만, 이 로컬 서버가 호스트의 웹 검색 커넥터를 대신 호출하지는 않는다. 재검색은 입력이 바뀔 때마다 자동 호출하지 않고 사용자가 `다시 검색`을 눌렀을 때만 실행한다.

보드가 MCP Apps 호스트 안에서 렌더링되면 `다시 검색`은 먼저 호스트 브리지를 확인한다. `window.openai.callTool`이 있으면 **데이터 전용** `souvenir_search_candidates` 도구에 `section`·`conditions`를 한 번에 전달하고, 도구가 반환한 source-shaped 후보 패킷을 로컬 서버의 `/api/host-result`에 반영한다. 이 도구는 호스트가 현재 Kiwi/Pilot MCP 또는 허용된 웹 검색을 조합해 제공해야 하며 로컬 실행기가 자동으로 등록하지는 않는다. 호출 도구가 없고 `sendFollowUpMessage`만 있으면 현재 에이전트에 같은 조건을 구조화된 재검색 요청으로 보내고, request ID가 포함된 tool output을 기다린다. 호스트 브리지가 없는 일반 로컬 미리보기에서는 기존 `/api/search` 직접 MCP 경로를 사용한다. 따라서 사용자는 같은 탭에서 버튼 한 번만 누르면 되지만, 호스트 검색 도구가 연결돼 있어야 GPT/Codex가 대신 검색할 수 있다.

검색 연결이 끊기거나 브라우저가 `Load failed`를 반환하면 원문 오류를 노출하지 않고 `검색 세션이 만료됐어요` 안내와 `세션 새로고침` 버튼을 보여준다. 버튼은 `/api/health`와 `/api/results`를 다시 확인해 같은 탭의 결과와 조건 편집 상태를 복구한다. 로컬 Python 서버가 종료된 경우에는 서버를 다시 켠 뒤 버튼을 누르면 된다.

Pilot 숙소 MCP가 일시적으로 실패하면 실행기가 최대 3회 지수 백오프로 재호출하고, 보드도 한 번 더 자동 재시도한다. 그래도 실패하면 내부 `PILOT_*` 코드는 숨기고 기존 후보를 유지하면서 공급자 지연 상태를 표시한다. 진짜 대체 후보가 필요하면 호스트 웹 검색 경로를 연결해야 하며, 로컬 실행기만으로는 호스트의 웹 검색 커넥터를 직접 호출하지 않는다.

## 선택 제출의 경계

`선택 제출`은 선택한 카드와 `선택·예약 전` 상태를 JSON으로 기록하는 일뿐이다. Notion 쓰기, 예약 확정, 결제, 좌석·객실 확보를 실행하지 않는다. 호스트는 이 파일을 읽은 뒤 기존 Souvenir 승인 질문을 보여주고, 사용자가 `반영`했을 때만 Notion 쓰기를 진행한다. 출력 파일의 기본 필드는 `notion_written=false`, `booking_completed=false`다.
