# Souvenir P2 coordinator

P2는 여러 컴퓨터의 Souvenir 실행기가 같은 Notion 여행을 동시에 쓰는 상황을 조정하기 위한 선택형 중앙 서비스다. coordinator는 Notion 본문이나 토큰을 저장하지 않고, 다음 메타데이터만 SQLite에 보관한다.

- workspace·여행별 lease와 heartbeat 만료 시각
- 순차 실행할 queue 작업과 재시도 횟수
- 요청 멱등 키와 완료 결과
- 실패 뒤 실행할 보상(compensation) 이벤트와 감사 상태
- SQLite schema migration 버전

## 실행

신뢰할 수 있는 한 대의 서버에서 실행한다. SQLite 파일을 여러 컴퓨터가 공유 폴더로 직접 열면 안 된다.

```bash
export SOUVENIR_COORDINATOR_TOKEN='긴 무작위 토큰'
python3 skills/souvenir/scripts/coordinator.py serve \
  --db /var/lib/souvenir/coordinator.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

실제 외부 접속은 HTTPS reverse proxy와 방화벽 뒤에 두고, 각 실행 환경에는 다음만 설정한다.

```bash
export SOUVENIR_COORDINATOR_URL='https://coordinator.example.com'
export SOUVENIR_COORDINATOR_TOKEN='같은 토큰'
```

이 환경 변수가 없으면 Souvenir는 기존 P1 경로(로컬 잠금 + Notion 작업 기록)를 사용한다. URL이 설정됐는데 coordinator에 연결할 수 없으면 안전을 위해 Notion 쓰기를 시작하지 않고 중단한다.

## 동작 보장

`workspace_key + trip_key` 범위에는 SQLite `BEGIN IMMEDIATE` 트랜잭션으로 활성 lease 하나만 생성한다. lease 소유자는 일정 주기로 heartbeat를 보내고, heartbeat가 끊기면 만료된 lease가 다음 claim 때 `expired`로 정리된다. 같은 요청 키를 다시 보내면 기존 lease를 임의로 복제하지 않는다.

queue는 같은 scope에서 실행 중인 작업이 끝나기 전 다음 작업을 claim하지 않는다. 같은 `scope_key + idempotency_key`를 다시 enqueue하면 기존 작업을 돌려준다. Notion API 호출 자체는 coordinator가 대신하지 않으며, coordinator lease를 얻은 실행기만 Notion 쓰기를 수행한다.

보상 이벤트는 “어떤 외부 변경을 되돌려야 하는지”를 기록하고 worker가 claim·완료 처리한다. Notion 페이지를 자동 삭제하거나 되돌리는 권한을 coordinator가 추측하지 않으므로, 실제 보상 API 호출은 실행기가 명시적으로 수행해야 한다.

## 운영 주의

- 토큰과 SQLite 파일을 Git에 올리지 않는다.
- coordinator의 HTTPS·인증·백업은 배포 환경에서 별도로 운영한다.
- coordinator가 영구적으로 꺼진 동안에는 자동으로 P1 fallback으로 내려가지 않는다. 두 writer가 동시에 Notion을 쓰는 것을 막기 위해 먼저 coordinator를 복구한다.
- coordinator는 여행 데이터 원본이 아니다. 최종 값·예약·메모는 항상 Notion에서 다시 읽고 확인한다.
