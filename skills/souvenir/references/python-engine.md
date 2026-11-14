# Souvenir Python 엔진

`skills/souvenir/scripts/trip_engine.py`는 템플릿 디자인을 바꾸지 않고 여행 데이터를 검사·계산·정리하는 선택형 엔진이다. 플러그인은 `requires_local_executor`를 선언하고 `scripts/run_souvenir.py`를 로컬 실행기 진입점으로 제공하므로, 지원되는 Codex·Claude Code·Gemini 환경에서는 사용자가 Python 명령을 직접 입력하지 않아도 스킬이 필요한 순간 엔진을 호출한다. 연결된 Notion을 수정하기 전에 결과를 먼저 JSON으로 만들고, 스킬이 변경 요약과 사용자 반영 요청을 보여주는 구조를 사용한다.

## 제공 기능

- `check`: 일정 겹침, 이동 여유 부족, 예약 기한, 체크인·체크아웃 오류, 장소 정보 오래됨, 필수 정보 누락을 검사한다.
- `budget`: 지갑·지출·분담 데이터를 통화별로 계산하고 잔액·사용률·카테고리 합계·송금 방향을 만든다. 확인되지 않은 환율은 자동으로 적용하지 않는다.
- `organize`: `일정명 | 시작-종료 | 장소 | 메모` 형식의 짧은 입력을 일차·날짜·시간 필드로 분리한다. `3박 4일`처럼 실제 날짜가 없으면 일차만 만든다.
- `import`: 표준 라이브러리만 사용해 UTF-8 CSV, JSON, iCalendar(`.ics`), 텍스트 장소 목록과 지도 링크를 읽는다. PDF·이미지 OCR은 별도 승인과 도구가 필요한 후속 범위다.
- `refresh`: 현재 날짜 기준 오늘 일정·다음 일정·예약 기한·확정 숙소·지갑 요약을 읽기 전용 화면용 JSON으로 만든다. 기본 계산은 Notion 페이지를 수정하지 않는다.

## 오프라인 사용

### 로컬 실행기 자동 호출

호스트가 로컬 실행기를 지원하면 스킬은 먼저 아래 상태 확인을 수행한 뒤 필요한 하위 명령을 자동으로 실행한다.

```bash
python3 scripts/run_souvenir.py --status
```

사용자가 직접 명령을 입력할 필요는 없다. 직접 실행해야 하는 개발자·fallback 환경에서는 기존처럼 `trip_engine.py`를 호출해도 된다.

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

로컬 실행기는 사용자의 요청이 있을 때만 실행되며 백그라운드에서 계속 돌지 않는다. 로컬 실행기가 없는 ChatGPT 웹·모바일 환경에서는 Python 엔진을 자동 호출할 수 없고, Notion 플러그인과 채팅 안내만 사용할 수 있다. 별도 MCP 서버를 설치해야 하는 구조는 아니다.
