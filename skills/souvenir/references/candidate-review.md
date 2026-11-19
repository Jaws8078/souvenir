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

## 선택 제출의 경계

`선택 제출`은 선택한 카드와 `선택·예약 전` 상태를 JSON으로 기록하는 일뿐이다. Notion 쓰기, 예약 확정, 결제, 좌석·객실 확보를 실행하지 않는다. 호스트는 이 파일을 읽은 뒤 기존 Souvenir 승인 질문을 보여주고, 사용자가 `반영`했을 때만 Notion 쓰기를 진행한다. 출력 파일의 기본 필드는 `notion_written=false`, `booking_completed=false`다.
