# Google Places API 배포 체크리스트

Souvenir의 Google Places 직접 검색은 선택 기능이다. 키가 없으면 호스트 검색·지도 링크 확인 경로를 사용하며, 공개 플러그인에 Google 키를 포함하지 않는다.

## 배포 전

- Google Cloud 프로젝트에서 **Places API (New)**를 활성화한다.
- 결제 계정과 사용량 알림·쿼터를 설정한다.
- API 키를 환경변수 `GOOGLE_PLACES_API_KEY`로만 제공한다.
- 키를 GitHub, `plugin.json`, Notion, 채팅, 명령줄 인자에 넣지 않는다.
- 키에 Places API만 허용하고 배포 환경에 맞는 애플리케이션·IP 제한을 적용한다.
- 실제 서비스라면 Google 요구사항을 반영한 이용약관·개인정보처리방침 URL을 공개한다.

## 화면·저장 규칙

- 후보 화면에 Google 장소 정보를 표시할 때 `Google Maps` attribution과 공식 로고·필요한 제공자 고지를 함께 표시한다.
- 지도·검색 후보는 사용자 검토용 임시 결과로 유지한다.
- 사용자가 직접 정한 표시명, `place_id`, Google Maps 링크만 정식 장소 행에 저장한다.
- 주소·평점·영업시간·가격·사진·리뷰를 자체 목록처럼 복사하거나 재배포하지 않는다.
- 후보 승인에는 장소명보다 후보 번호 또는 `place_id`를 우선 사용한다.
- 목적지 불일치 후보는 승인 목록에 있어도 저장하지 않는다.

## 검색 세션·비용

- 다음 페이지를 요청할 때 query·언어·지역·정렬·유형·영업 중·위치 반경·필드 모드를 동일하게 유지한다.
- 검색 세션의 `page_token`은 임시 상태로만 보관하고 Notion에 저장하지 않는다.
- 후보 카드에 꼭 필요하지 않은 평점·영업시간·가격 필드는 `--google-summary-only`로 제외할 수 있다.
- 동일 장소의 최신 정보가 필요할 때만 새 조회를 수행하고, 검색 결과를 장기 캐시하지 않는다.
- 실 API 테스트는 `SOUVENIR_LIVE_GOOGLE_TEST=1`과 로컬 키가 모두 있을 때만 실행한다.

이 문서는 구현 체크리스트이며 Google 약관에 대한 법률 자문이 아니다. 최신 요구사항은 [Places API 정책](https://developers.google.com/maps/documentation/places/web-service/policies)과 [Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search)를 배포 시 다시 확인한다.
