# Souvenir Python 엔진

`skills/souvenir/scripts/trip_engine.py`는 템플릿 디자인을 바꾸지 않고 여행 데이터를 검사·계산·정리하는 선택형 엔진이다. 연결된 Notion을 수정하기 전에 결과를 먼저 JSON으로 만들고, 스킬이 변경 요약과 사용자 반영 요청을 보여주는 구조를 사용한다.

## 제공 기능

- `check`: 일정 겹침, 이동 여유 부족, 예약 기한, 체크인·체크아웃 오류, 장소 정보 오래됨, 필수 정보 누락을 검사한다.
- `budget`: 지갑·지출·분담 데이터를 통화별로 계산하고 잔액·사용률·카테고리 합계·송금 방향을 만든다. 확인되지 않은 환율은 자동으로 적용하지 않는다.
- `organize`: `일정명 | 시작-종료 | 장소 | 메모` 형식의 짧은 입력을 일차·날짜·시간 필드로 분리한다. `3박 4일`처럼 실제 날짜가 없으면 일차만 만든다.
- `import`: 표준 라이브러리만 사용해 UTF-8 CSV, JSON, iCalendar(`.ics`), 텍스트 장소 목록과 지도 링크를 읽는다. PDF·이미지 OCR은 별도 승인과 도구가 필요한 후속 범위다.
- `refresh`: 현재 날짜 기준 오늘 일정·다음 일정·예약 기한·확정 숙소·지갑 요약을 읽기 전용 화면용 JSON으로 만든다. 기본 계산은 Notion 페이지를 수정하지 않는다.

## 오프라인 사용

여행 데이터를 JSON 스냅샷으로 준비하면 토큰 없이 검사할 수 있다.

```bash
python3 skills/souvenir/scripts/trip_engine.py check --input trip.json --pretty
python3 skills/souvenir/scripts/trip_engine.py budget --input trip.json --base-currency JPY --rate JPY=0.009 --pretty
python3 skills/souvenir/scripts/trip_engine.py refresh --input trip.json --now 2026-12-12T09:00:00 --pretty
```

일정 입력과 파일 가져오기는 다음처럼 사용한다.

```bash
python3 skills/souvenir/scripts/trip_engine.py organize \
  --text $'1일차 | 14:00-16:00 | 공항 | 체크인\n2일차 | 오호리 공원' \
  --start-date 2026-12-12 --pretty
python3 skills/souvenir/scripts/trip_engine.py import --input expenses.csv --kind auto --pretty
python3 skills/souvenir/scripts/trip_engine.py import --input trip.ics --kind auto --pretty
python3 skills/souvenir/scripts/trip_engine.py import --text 'https://maps.google.com/?query=Ohori' --kind link --pretty
```

## Notion 연결

코드 fallback 설치가 끝나고 `.souvenir-install.json`에 생성된 DB 매핑이 있으면, 여행 페이지 ID를 지정해 읽을 수 있다.

```bash
NOTION_TOKEN="secret_..." python3 skills/souvenir/scripts/trip_engine.py check \
  --mapping .souvenir-install.json --trip-id "<여행 페이지 ID>" --pretty
NOTION_TOKEN="secret_..." python3 skills/souvenir/scripts/trip_engine.py refresh \
  --mapping .souvenir-install.json --trip-id "<여행 페이지 ID>" --pretty
```

이 경로는 현재 일정·예약·숙소·장소·지갑·지출 DB를 읽어 결과를 만드는 단계다. 자동 수정은 하지 않으며, 스킬이 결과를 사용자에게 보여주고 `반영`을 받은 뒤 필요한 행만 Notion 도구로 수정한다. 토큰·쿠키·비밀번호는 JSON 스냅샷이나 매핑 파일에 저장하지 않는다.

Python은 Notion 플러그인 안에서 임의로 백그라운드 실행되지 않는다. 대화에서 자동 호출하려면 이후에 이 엔진을 로컬 MCP 도구로 노출해야 하며, 그때 `check_trip`, `calculate_budget`, `organize_schedule`, `refresh_trip` 같은 도구 이름으로 연결한다.
