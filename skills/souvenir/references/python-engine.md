# Souvenir Python 엔진

`skills/souvenir/scripts/trip_engine.py`는 템플릿 디자인을 바꾸지 않고 여행 데이터를 검사·계산·정리하는 선택형 엔진이다. 플러그인은 `requires_local_executor`를 선언하고 `scripts/run_souvenir.py`를 로컬 실행기 진입점으로 제공하므로, 지원되는 Codex·Claude Code·Gemini 환경에서는 사용자가 Python 명령을 직접 입력하지 않아도 스킬이 필요한 순간 엔진을 호출한다. 연결된 Notion을 수정하기 전에 결과를 먼저 JSON으로 만들고, 스킬이 변경 요약과 사용자 반영 요청을 보여주는 구조를 사용한다.

## 제공 기능

- `check`: 일정 겹침, 이동 여유 부족, 예약 기한, 체크인·체크아웃 오류, 장소 정보 오래됨, 필수 정보 누락을 검사한다.
- `budget`: 지갑·지출·분담 데이터를 통화별로 계산하고 잔액·사용률·카테고리 합계·송금 방향을 만든다. 사용 내역 화면에서 지갑 키를 숨겨도 지갑이 하나면 자동 연결하고, Notion 관계형 속성이 반환하는 지갑 페이지 ID도 같은 지갑으로 해석한다. 지갑이 여러 개인데 연결이 없으면 어느 카드에도 임의 반영하지 않고 확인 필요로 남긴다. 확인되지 않은 환율은 자동으로 적용하지 않는다.
- `organize`: `일정명 | 시작-종료 | 장소 | 메모` 형식의 짧은 입력을 일차·날짜·시간 필드로 분리한다. `3박 4일`처럼 실제 날짜가 없으면 일차만 만든다.
- `import`: 표준 라이브러리만 사용해 UTF-8 CSV, JSON, iCalendar(`.ics`), 텍스트 장소 목록과 지도 링크를 읽는다. Google Maps 공유 단축 링크(`maps.app.goo.gl`, `goo.gl/maps`)는 리디렉션을 따라 최종 주소에서 장소명을 추출한다. 링크가 만료·차단돼 해석되지 않으면 호스트명을 장소명으로 저장하지 않고 `확인 필요` 상태를 반환한다. PDF·이미지 OCR은 별도 승인과 도구가 필요한 후속 범위다.
- `refresh`: 현재 날짜 기준 오늘 일정·다음 일정·예약 기한·확정 숙소·지갑 요약과 `progress` 집계를 읽기 전용 화면용 JSON으로 만든다. 진행도는 일정·예약·할 일·준비물(선택한 경우 쇼핑)의 **완료 건수 ÷ 유효 대상 건수**로 다시 계산하며 취소·환불·제외는 분모에서도 뺀다. `진행도 수동 완료`/`직접 완료`는 자동 상태보다 우선하고 `자동 제외`는 자동 완료를 보류 상태로 되돌린다. 기본 계산은 Notion 페이지를 수정하지 않는다.
- `shopping`: 품목명만 받은 요청을 조사 후보 확인 패킷으로 만들거나, 외부 검색 결과 JSON에서 선택한 후보를 `사용자 확인 완료` 상태의 저장 준비 행으로 정규화한다. 검색 자체는 호스트의 웹·이미지 도구가 수행하고, 엔진은 값을 추정하지 않는다.
- `places`: 여러 장소 입력을 목적지 적합성 확인 패킷으로 만든다. 지도·검색 근거가 없거나 목적지와 불일치하면 저장 가능 상태를 끄고, 사용자 승인 이후에만 저장할 수 있는 행으로 분리한다. 검색 결과만 `--evidence-input`으로 넣으면 추천순 최대 10개씩 후보를 보여주고 `--candidate-offset 10`으로 다음 10개를 요청할 수 있다. 결과가 10개보다 적으면 있는 만큼만 반환한다. `GOOGLE_PLACES_API_KEY`가 있는 로컬 실행기에서는 Places API (New) Text Search를 명시적으로 호출해 같은 후보 패킷을 만들 수 있고, 다음 묶음은 응답의 페이지 토큰으로 이어간다. 키는 결과·Notion에 저장하지 않는다. `--defer-external`을 사용하면 단축 지도 링크 해석을 기다리지 않고 `외부 조회` 작업만 반환한다.
- `fields`: 각 파트가 사용자에게 먼저 제시해야 하는 전체 입력 필드와 대상 DB를 반환한다. `normalize_part_input`은 답변을 해당 DB의 속성명으로 분리하며, 사람별 `의견·요청` 합치기 입력은 거부한다.
- 외부 검색은 엔진이 임의로 실행하지 않는다. `shopping --defer-external`은 품목·목적지 검색 작업을 반환하고, 호스트가 조사한 후보를 다시 넣은 뒤 승인 패킷을 만든다.
- fallback 설치기는 승인된 정규화 행을 `part-save --part <파트> --confirm`으로 해당 DB에 저장한다. 일정은 기존 `일차` 행을 재사용하고, 항공·숙소·장소·예산·준비물도 각 DB의 제목·속성에 직접 매핑한다.

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

# 품목명만 받은 경우: 검색 후보를 저장하지 않고 확인용으로만 만든다.
python3 skills/souvenir/scripts/run_souvenir.py shopping \
  --item-name '유자 킷캣' --destination 후쿠오카 --pretty

# 호스트가 조사한 후보 JSON을 보여준 뒤, 사용자가 고른 후보만 승인 상태로 만든다.
python3 skills/souvenir/scripts/run_souvenir.py shopping \
  --item-name '유자 킷캣' --candidate-input shopping-candidates.json \
  --candidate-index 0 --traveler '동행자 1' --base-currency JPY --confirm --pretty

# 여러 장소를 목적지와 대조한다. evidence가 없으면 확인 필요로 남긴다.
python3 skills/souvenir/scripts/run_souvenir.py places \
  --destination 후쿠오카 \
  --text $'오호리 공원 | 후쿠오카 | 관광\n도쿄 타워 | 도쿄 | 관광' \
  --evidence-input place-evidence.json --pretty

# 호스트가 조사한 추천순 후보를 최대 10개씩 보여준다. 다음 묶음은
# 사용자 요청 뒤에만 offset을 10 올려 다시 호출한다.
python3 skills/souvenir/scripts/run_souvenir.py places \
  --destination 후쿠오카 --evidence-input place-search-results.json \
  --candidate-offset 0 --candidate-page-size 10 --pretty
python3 skills/souvenir/scripts/run_souvenir.py places \
  --destination 후쿠오카 --evidence-input place-search-results.json \
  --candidate-offset 10 --candidate-page-size 10 --pretty

# Google Places API (New)를 직접 쓰는 선택 경로. 키는 환경변수로만 전달하고
# 응답의 `후속 검색 상태.page_token`은 호스트가 보관해 다음 10개 요청에 사용한다.
GOOGLE_PLACES_API_KEY="secret_..." \
python3 skills/souvenir/scripts/run_souvenir.py places \
  --destination 후쿠오카 --google-query '후쿠오카 관광지' --pretty
```

단축 링크도 같은 방식으로 가져올 수 있다. 네트워크에서 최종 주소를 확인할 수 없을 때는 저장하지 않고 확인 요청용 결과를 만든다.

```bash
python3 skills/souvenir/scripts/trip_engine.py import \
  --text 'https://maps.app.goo.gl/공유토큰' --kind link --pretty

# 파트별 질문 필드와 저장 대상 확인
python3 skills/souvenir/scripts/run_souvenir.py fields --part '여행 개요·취향' --pretty
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

fallback 설치기를 쓰는 환경에서는 승인된 행 JSON만 Notion에 저장할 수 있다. 후보를 먼저 보여주고 사용자 승인을 받은 뒤 실행한다.

```bash
NOTION_TOKEN="secret_..." python3 skills/souvenir/scripts/notion_installer.py shopping-save \
  --mapping .souvenir-install.json --trip-id "<여행 페이지 ID>" \
  --shopping-input shopping-confirmed.json --confirm --pretty

# 사람별 입력을 동행자 표의 독립 열에 저장
NOTION_TOKEN="secret_..." python3 skills/souvenir/scripts/notion_installer.py traveler-save \
  --mapping .souvenir-install.json --trip-id "<여행 페이지 ID>" \
  --traveler-input traveler-profiles.json --confirm --pretty

# 다른 파트도 같은 정규화 행 계약으로 저장
NOTION_TOKEN="secret_..." python3 skills/souvenir/scripts/notion_installer.py part-save \
  --mapping .souvenir-install.json --trip-id "<여행 페이지 ID>" \
  --part "날짜별 일정" --part-input itinerary.json --confirm --pretty
```

로컬 실행기는 사용자의 요청이 있을 때만 실행되며 백그라운드에서 계속 돌지 않는다. 로컬 실행기가 없는 ChatGPT 웹·모바일 환경에서는 Python 엔진을 자동 호출할 수 없고, Notion 플러그인과 채팅 안내만 사용할 수 있다. 별도 MCP 서버를 설치해야 하는 구조는 아니다.
