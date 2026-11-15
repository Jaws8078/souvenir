# Souvenir 고정 템플릿 게이트

`@souvenir` 또는 `$souvenir`로 여행 계획을 시작할 때는 사용자가 제공한
Notion 링크 배포본만 디자인 원본으로 사용한다. 저장소의 매니페스트,
다른 템플릿, 임의의 빈 페이지는 일반 실행의 대체 경로가 아니다.

## 기준 원본

- 기준 설정: [`assets/template-source.json`](../assets/template-source.json)
- 현재 링크: `template_url` 값
- 복제 허용이 켜진 공개 링크만 유효하다.
- 페이지 제목이 바뀌었거나 구조를 확인할 수 없으면 같은 템플릿으로 추정하지
  않는다.

## 실행 상태

```text
template_status:
  unknown          # 아직 검사하지 않음
  found            # 현재 워크스페이스에서 기준 템플릿 후보를 찾음
  needs_duplicate  # 링크를 열어 사용자가 복제해야 함
  duplicate_consent_pending # 복제를 시작하기 전 사용자 허락 대기
  native_in_progress # 연결된 Notion 도구의 네이티브 복제 진행 중
  browser_in_progress # CUA 브라우저의 링크·Duplicate 진행 중
  manual_duplicate_wait # 브라우저 자동화가 없어 사용자 클릭 대기
  validating        # 복제된 페이지의 실제 구조 검증 중
  confirmed        # 사본의 페이지 ID·URL·설치를 확인함
  blocked          # 링크/권한/복제 문제가 해결되지 않음
```

상태와 함께 아래 값을 현재 대화와 `Souvenir · 설치 기록`에 기록한다.

```text
template_source = public_notion_link
template_source_url = <assets/template-source.json의 template_url>
template_workspace_id = <복제본이 있는 워크스페이스 ID>
template_page_id = <복제본 페이지 ID>
template_page_url = <복제본 페이지 URL>
template_confirmed = true|false
duplicate_mode = native_duplicate|browser_automation|manual
consent_granted = true|false
```

## 복제 capability와 자동화 우선순위

복제는 Python 로컬 실행기가 임의로 브라우저를 조작하는 기능이 아니다. 호스트가
제공하는 capability를 먼저 확인하고, 실제 브라우저·Notion 도구를 호스트에
위임한다.

1. `native_duplicate` — 기준 페이지가 현재 연결된 워크스페이스 안에 있고
   Notion 도구에 복제 권한이 있을 때만 `duplicate_page`를 사용한다. 복제는
   비동기로 끝날 수 있으므로 반환 URL을 즉시 완료로 표시하지 말고 다시 읽어
   확인한다.
2. `browser_automation` — CUA가 있으면 고정 링크를 열고, 사용자가 허락한
   뒤 `Duplicate`와 대상 워크스페이스 선택을 수행한다. 로그인·OAuth·MFA·
   CAPTCHA는 사용자가 직접 처리한다. CUA가 없거나 브라우저 세션이 끊기면
   `manual_duplicate_wait`로 전환한다.
3. `manual` — 링크를 보여주고 사용자가 Notion에서 직접 `Duplicate`를 누른
   뒤 완료했다고 답할 때까지 기다린다.

어떤 capability든 먼저 아래 한 번의 명시적 허락을 받는다.

```text
연결된 워크스페이스: <이름>
Souvenir 고정 템플릿을 열어 이 워크스페이스에 복제하고,
복제본의 구조를 검증한 뒤 여행 계획을 시작할까요?
원본은 수정하지 않습니다. [진행] [취소]
```

호스트가 CUA 자동화를 시작하면 다음 작업을 수행한다.

```text
고정 링크 열기 → 로그인 상태 확인 → Duplicate 클릭
→ 대상 워크스페이스 선택 → 복제 완료 URL 확인
→ Notion 재조회로 필수 섹션·DB·뷰 검증
```

CUA나 네이티브 복제가 실패하면 코드 청사진으로 몰래 전환하지 않는다. 사용자
클릭 fallback으로 전환하고, 복제 확인 전에는 여행 데이터를 쓰지 않는다.

## 사용자에게 보이는 흐름

### 새 워크스페이스 + CUA 가능

```text
사용자: @souvenir
Souvenir: 연결된 계정과 워크스페이스를 확인했습니다.
          Yuseong's Workspace에 고정 템플릿을 복제하고 검증할까요? [진행] [취소]
사용자: 진행
Souvenir: 템플릿 링크를 여는 중… Duplicate를 실행하는 중…
          로그인·MFA가 필요하면 화면에서 직접 처리해주세요.
Souvenir: 복제본 URL을 확인했습니다. 필수 섹션과 데이터베이스를 검증하는 중…
Souvenir: 검증 완료. 이제 여행 정보를 입력할 수 있습니다.
```

### 브라우저 자동화가 없는 경우

```text
Souvenir: 현재 환경에서는 브라우저 자동화를 사용할 수 없습니다.
          아래 링크에서 Duplicate를 누르고 대상 워크스페이스를 선택해주세요.
사용자: 복제 완료
Souvenir: 새 페이지를 다시 읽어 구조를 확인하는 중…
Souvenir: 검증 완료. 이제 여행 정보를 입력할 수 있습니다.
```

복제 실패·취소·검증 실패 시에는 `blocked` 또는 `needs_duplicate`로 남기고,
여행 데이터는 생성하지 않는다.

## 검사 순서

연결된 계정과 워크스페이스를 사용자에게 보여주고 진행 확인을 받은 뒤에만
아래 검사를 한다.

1. 정확한 제목의 `Souvenir · 설치 기록`만 읽는다. 기록의
   `template_source`가 `public_notion_link`이고 `template_source_url`이
   기준 링크와 같으며 `template_workspace_id`가 현재 워크스페이스와 같고,
   실제 `template_page_id`가 존재할 때만 재사용한다.
2. 유효한 설치 기록이 없으면 기준 설정의 `accepted_titles`에 있는 제목만
   검색한다. 후보 페이지를 열어 `required_sections`를 확인하고, 기준 링크의
   복제본으로 식별되는 페이지가 정확히 하나일 때만 `found`로 둔다.
3. 후보가 없거나 구조 검사가 실패하면 `duplicate_consent_pending`으로 바꾸고
   복제 허락을 한 번 받는다. 허락 전에는 링크를 열거나 복제를 호출하지 않는다.
4. 허락 후 capability에 따라 `native_in_progress`, `browser_in_progress`,
   `manual_duplicate_wait` 중 하나로 바꾼다. 브라우저 복제는 사용자의 Notion
   세션에서 수행하며 Souvenir가 쿠키·비밀번호·토큰을 대신 사용하거나 권한
   승인을 대신하지 않는다.
5. 네이티브·브라우저 복제 결과 또는 사용자가 제공한 완료 신호가 있으면
   `validating`으로 바꾸고 같은 워크스페이스에서 설치 기록·기준 제목·필수
   섹션·실제 URL·ID를 다시 읽는다. 확인 전에는 여행 행, 일정, 예산, 예약을
   만들지 않는다.
6. 확인이 끝나면 `template_status=confirmed`로 기록하고 그 페이지의 블록,
   컬럼, 커버, 데이터베이스, 뷰, 수식을 그대로 재사용한다. 템플릿 구조를
   다시 만들거나 다른 페이지를 섞지 않는다.

## 중단 조건

- 링크가 비공개이거나 `복제 허용`이 꺼져 있음
- 후보가 두 개 이상이거나 제목·필수 섹션이 기준과 다름
- 복제본의 실제 페이지 ID·URL을 확인할 수 없음
- 연결된 워크스페이스가 설치 기록과 다름
- CUA가 로그인·MFA·CAPTCHA 단계에서 사용자 입력을 기다리는 중

위 경우에는 `template_status=blocked`로 남기고 문제와 필요한 사용자 조치만
알린다. **일반 `@souvenir` 실행에서는 청사진 재구축, 코드 설치, 임의의 빈
페이지 생성으로 우회하지 않는다.** `notion_installer.py`와 청사진 문서는
개발자 점검·명시적 별도 요청에만 사용하는 보조 도구다.
