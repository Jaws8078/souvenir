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
```

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
3. 후보가 없거나 구조 검사가 실패하면 `needs_duplicate`로 멈춘다. 기준
   링크를 사용자에게 보여주고 Notion에서 **복제**를 누르도록 안내한다.
   복제는 사용자의 Notion 세션에서 수행하며 Souvenir가 쿠키·비밀번호·토큰을
   대신 사용하거나 권한 승인을 대신하지 않는다.
4. 사용자가 복제를 끝냈다고 답하면 같은 워크스페이스에서 1~2단계를 다시
   실행해 새 사본의 실제 URL·ID와 구조를 확인한다. 확인 전에는 여행 행,
   일정, 예산, 예약을 만들지 않는다.
5. 확인이 끝나면 `template_status=confirmed`로 기록하고 그 페이지의 블록,
   컬럼, 커버, 데이터베이스, 뷰, 수식을 그대로 재사용한다. 템플릿 구조를
   다시 만들거나 다른 페이지를 섞지 않는다.

## 중단 조건

- 링크가 비공개이거나 `복제 허용`이 꺼져 있음
- 후보가 두 개 이상이거나 제목·필수 섹션이 기준과 다름
- 복제본의 실제 페이지 ID·URL을 확인할 수 없음
- 연결된 워크스페이스가 설치 기록과 다름

위 경우에는 `template_status=blocked`로 남기고 문제와 필요한 사용자 조치만
알린다. **일반 `@souvenir` 실행에서는 청사진 재구축, 코드 설치, 임의의 빈
페이지 생성으로 우회하지 않는다.** `notion_installer.py`와 청사진 문서는
개발자 점검·명시적 별도 요청에만 사용하는 보조 도구다.
