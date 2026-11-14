<p align="center">
  <img src="assets/souvenir-icon.png" alt="Souvenir icon" width="176" />
</p>

<h1 align="center">Souvenir · 여행 도우미</h1>

<p align="center"><strong>여행은 한눈에, 작성은 간단하게.</strong></p>

<p align="center">Notion과 연결해 여행 계획부터 여행 중 확인까지 이어가는 Codex · ChatGPT 플러그인</p>

<p align="center">
  <img src="https://img.shields.io/badge/Notion-connected-2F5D8A?style=flat-square" alt="Notion connected" />
  <img src="https://img.shields.io/badge/local--first-2D6A4F?style=flat-square" alt="Local first" />
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9 or newer" />
</p>

<p align="center"><em>목적지와 날짜 또는 기간만 입력하면, 나머지는 대화로 함께 완성합니다.</em></p>

<p align="center">
  <a href="#빠른-시작">빠른 시작</a> ·
  <a href="#핵심-기능">핵심 기능</a> ·
  <a href="#템플릿-사용">템플릿 사용</a> ·
  <a href="#개발자-검증">개발자 검증</a>
</p>

## Souvenir가 하는 일

- 목적지와 날짜·기간만으로 여행 페이지를 시작합니다.
- 여행 개요, 일정, 항공, 숙소, 장소, 쇼핑, 예산, 준비물을 대화로 채웁니다.
- 여행 중에는 오늘·다음 일정, 예약, 숙소, 지갑 잔액을 확인 화면에서 빠르게 봅니다.
- 기존 메모·예약·다른 날짜는 보존하고, 요청한 항목만 변경합니다.
- 자동 계산 결과와 직접 입력 항목을 분리해 Notion 화면을 단순하게 유지합니다.

## 빠른 시작

> **현재 배포 상태**
> 최신 Notion 디자인은 공개·복제 가능한 링크 배포본에 반영되어 있습니다. Souvenir 플러그인은 대화·검증을 담당하고, 실제 여행 페이지는 사용자가 복제한 자기 워크스페이스의 사본을 사용합니다.

### Codex CLI

```bash
codex plugin marketplace add Jaws8078/souvenir --ref main
codex plugin add souvenir@souvenir-marketplace
```

새 채팅에서 다음 중 하나를 입력합니다.

```text
$souvenir
Souvenir로 후쿠오카 3박 4일 여행 계획을 시작해줘.
```

### ChatGPT 데스크톱

1. Plugins Directory를 엽니다.
2. `Add Marketplace` 또는 `Add from repository`를 선택합니다.
3. `https://github.com/Jaws8078/souvenir`를 입력합니다.
4. Souvenir를 설치하고 새 Work 채팅에서 `@Souvenir`를 선택합니다.

### 첫 실행 흐름

Souvenir는 Notion 연결을 먼저 확인합니다. 연결이 없으면 공식 연결 UI에서 승인한 뒤 같은 대화에서 재개합니다.

1. Notion 연결·로그인 확인
2. 템플릿 설치 또는 복제 승인
3. 여행지·출발일·종료일(또는 기간)·인원·기준 통화 입력
4. 필요한 계획 파트 선택
5. 변경안을 확인한 뒤 Notion에 반영

처음에는 `$souvenir`만 입력해도 되고, 메뉴에서 `1. 여행 플래너 만들기`, `2. 새 여행 계획`, `3. 기존 일정 수정`을 선택할 수 있습니다.

## 핵심 기능

| 영역 | 제공 기능 |
| --- | --- |
| 🗺️ 계획·장소 | 날짜별 일정, 장소 보관함, 날씨·플랜 B/C, 선택형 쇼핑 목록 |
| 🎟️ 이동·예약 | 항공 후보, 현지 이동, 숙소 후보·확정 숙소, 예약 상태 |
| 💴 예산·동행 | 여행 지갑, 실제 지출, 잔액·사용률, 인원별 분담·정산 |
| 🎒 준비·자료 | 준비할 일, 준비물, 담당자·기한, 예약 확인서·중요 자료 |

입력 속성과 자동 결과를 분리합니다. 사용자는 필요한 기본 항목만 작성하고, 잔액·기간·진행률·확인 결과는 수식과 계산으로 표시됩니다. 항공권과 숙소 가격은 원화(KRW)로 관리하며, 확인 근거 없는 환율·날씨·운영 정보는 `확인 필요`로 남깁니다.

### 지갑은 다섯 칸만 입력

사용 내역의 기본 입력은 `사용처 · 거래 유형 · 결제 단계 · 결제 수단 · 금액`입니다. 사용 금액은 연결된 지갑에서 자동 차감되고, `사용액 · 남은 금액 · 사용률`은 읽기 전용 결과로 표시됩니다. 예정 비용은 실제 지출과 분리합니다.

## 여러 사람이 동시에 사용할 때

같은 여행을 두 세션이 동시에 수정하면 중복 저장을 막기 위해 먼저 시작한 작업만 반영합니다.

```text
현재 이 여행에 다른 반영 작업이 진행 중입니다.
기존 작업이 끝난 뒤 다시 시도해주세요.
```

충돌이 나도 사용자 메모·예약·다른 날짜는 보존하고, 변경된 항목만 다시 확인합니다. 혼자 사용하는 경우에는 이 보호 기능이 거의 보이지 않습니다. 동시성 설계의 전체 범위는 [동시성 대응 메모](docs/concurrency-roadmap.md)에서 확인할 수 있습니다.

## 템플릿 사용

### 권장: Notion 링크 배포본 복제

Souvenir의 디자인 원본은 Notion 템플릿입니다. [Souvenir 링크 배포본](https://rift-volcano-c77.notion.site/Souvenir-4171036c4e4280928564d129600f6472?source=copy_link)을 열고 Notion에서 **복제**하면 원본의 커버·레이아웃·DB 뷰를 가장 충실하게 사용할 수 있습니다. 현재 배포본은 개인 여행·예약·지출 데이터와 이전 `새 여행` 연결을 제거한 빈 시작 상태입니다.

사용자는 링크를 연 뒤 **복제**만 하면 됩니다. 별도 마켓 등록은 필요하지 않습니다. 링크 소유자는 `공유 → 웹에 게시 → 복제 허용`을 켜고, 비공개 작업 원본·예약·지갑·개인 메모는 공개하지 않습니다. 배포 링크는 [`template-source.json`](skills/souvenir/assets/template-source.json)에 관리합니다.

### 템플릿 업데이트 순서

템플릿을 바꿀 때는 다음 순서를 지킵니다.

1. 비공개 Notion 원본을 수정합니다.
2. 개인 데이터와 테스트 행을 제거한 링크 배포본에 같은 변경을 반영합니다.
3. 같은 링크를 유지한 채 README와 스킬 규칙이 새 입력·뷰를 설명하는지 확인합니다.

이미 복제한 사용자의 사본은 원본 링크가 자동으로 갱신되지 않습니다. 새 사용자는 같은 링크에서 최신본을 복제하고, 기존 사본은 필요한 변경만 직접 반영합니다.

### 보조: 링크 없이 코드로 재구축

공개 링크를 사용할 수 없을 때만 fallback 설치기를 사용합니다.

```bash
python3 skills/souvenir/scripts/notion_installer.py validate
python3 skills/souvenir/scripts/notion_installer.py plan --pretty
export NOTION_TOKEN="secret_..."
python3 skills/souvenir/scripts/notion_installer.py install \
  --parent-page "<대상 상위 페이지 URL 또는 ID>"
```

이 경로는 비식별 매니페스트로 구조를 새로 만들며, Notion 네이티브 복제처럼 원본의 모든 폭·뷰·관계·수식을 픽셀 단위로 보존한다고 약속하지 않습니다. 토큰은 채팅·GitHub·매핑 파일에 넣지 않습니다.

## 여러 컴퓨터에서 강한 잠금이 필요할 때

선택형 P2 coordinator를 한 대의 서버에서 실행하면 여행별 lease·heartbeat·작업 큐·멱등 키를 중앙에서 관리할 수 있습니다.

```bash
export SOUVENIR_COORDINATOR_TOKEN='긴 무작위 토큰'
python3 skills/souvenir/scripts/coordinator.py serve \
  --db /var/lib/souvenir/coordinator.sqlite3 \
  --host 0.0.0.0 \
  --port 8765
```

각 컴퓨터에는 다음을 설정합니다.

```bash
export SOUVENIR_COORDINATOR_URL='https://coordinator.example.com'
export SOUVENIR_COORDINATOR_TOKEN='긴 무작위 토큰'
```

URL을 설정했는데 coordinator가 꺼져 있으면 안전을 위해 Notion 쓰기를 시작하지 않습니다. HTTPS reverse proxy·인증·백업은 [P2 coordinator 가이드](docs/coordinator.md)를 참고합니다.

## 업데이트와 문제 해결

Git으로 설치했다면 저장소에서 최신 버전을 받습니다.

```bash
cd souvenir
git pull
```

플러그인 코드와 Notion 템플릿은 별도로 업데이트됩니다. GitHub/마켓플레이스 업데이트는 대화 규칙과 Python 엔진을 갱신하고, 링크 배포본 업데이트는 새로 복제하는 사본에 반영됩니다. 기존 Notion 사본의 내용은 자동으로 덮어쓰지 않습니다.

- `$souvenir`가 인식되지 않으면 Codex에서 저장소 루트 폴더를 엽니다.
- Notion 페이지가 만들어지지 않으면 Notion 연결과 대상 워크스페이스 쓰기 권한을 확인합니다.
- 설치가 중단되면 매핑 파일을 지우지 말고 `$souvenir`를 다시 실행합니다. 중단 지점부터 재개합니다.
- 저장 여부가 불명확하다는 안내가 나오면 Notion에서 실제 생성 여부를 먼저 확인합니다.

## 선택 기능: Python 여행 엔진

Codex·Claude Code·Gemini처럼 로컬 실행기를 지원하는 환경에서는 사용자가 Python 명령을 입력하지 않아도 Souvenir가 필요할 때 엔진을 호출합니다. 일정 충돌·이동 여유·예산·파일 입력·오늘/다음 일정 확인을 계산하며, 기본적으로 먼저 결과를 보여주고 승인 후 Notion에 반영합니다.

로컬 실행기 상태는 다음 명령으로 확인할 수 있습니다.

```bash
python3 skills/souvenir/scripts/run_souvenir.py --status
```

ChatGPT 웹·모바일처럼 로컬 실행기가 없는 환경에서도 Notion 연결과 대화형 계획 기능은 사용할 수 있습니다.

## 개발자 검증

실제 Notion 데이터를 만들지 않는 오프라인 테스트입니다.

```bash
python3 -m unittest discover -s skills/souvenir/tests -p 'test_*.py'
python3 skills/souvenir/scripts/notion_installer.py validate
python3 skills/souvenir/scripts/notion_installer.py plan --pretty
```

세부 규칙은 [Souvenir 스킬](skills/souvenir/SKILL.md), [대화형 계획 마법사](skills/souvenir/references/conversation-flow.md), [데이터 운영 규칙](skills/souvenir/references/data-operations.md), [구현 기록](docs/implementation-notes.md)에서 관리합니다. 변경 우선순위와 보호 범위는 [동시성 대응 메모](docs/concurrency-roadmap.md)에서 확인할 수 있습니다.

## 라이선스

MIT
