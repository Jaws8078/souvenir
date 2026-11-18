"""Offline tests for Souvenir's Python travel engine."""
import csv
import contextlib
import importlib.util
import io
import json
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "trip_engine.py"
SPEC = importlib.util.spec_from_file_location("trip_engine", SCRIPT)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class TripEngineTests(unittest.TestCase):
    def test_google_mixed_language_addresses_can_be_approved(self):
        for address in ("일본 〒812-0038 Fukuoka, Hakata Ward", "福岡県福岡市博多区", "후쿠오카시 하카타구"):
            with self.subTest(address=address):
                with patch.object(engine, "google_places_text_search", return_value={
                    "query": "후쿠오카 카페", "source": "Google Places API (New)",
                    "next_page_token": "", "candidates": [{
                        "name": "카페", "region": address, "place_id": "cafe-id",
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                    }],
                }):
                    packet = engine.prepare_google_place_candidate_page("후쿠오카 카페", "secret", "후쿠오카")
                self.assertEqual(packet["후보"][0]["여행지 확인"], "확인 완료")
                self.assertEqual(len(engine.confirm_place_rows(packet, ["1"], {"1": "내 카페"})), 1)
        self.assertEqual(engine._place_match_status("후쿠오카", "도쿄도 Tokyo"), "불일치")
        self.assertEqual(engine._place_match_status("알 수 없는 도시", "unknown locality"), "확인 필요")
        self.assertNotEqual(engine._location_token("東京"), engine._location_token("京都"))

    def test_google_final_page_counts_only_new_candidates_across_pages(self):
        answers = [
            {"query": "카페", "source": "Google Places API (New)", "next_page_token": "next",
             "candidates": [{"name": "A", "place_id": "a"}, {"name": "B", "place_id": "b"}]},
            {"query": "카페", "source": "Google Places API (New)", "next_page_token": "",
             "candidates": [{"name": "B", "place_id": "b"}, {"name": "C", "place_id": "c"}]},
        ]
        with patch.object(engine, "google_places_text_search", side_effect=answers):
            first = engine.prepare_google_place_candidate_page("카페", "secret", "후쿠오카", page_size=2)
            state = first["후속 검색 상태"]
            last = engine.prepare_google_place_candidate_page(
                "카페", "secret", "후쿠오카", page_size=2,
                page_token="next", page_index=1, session_state=state,
            )
        self.assertEqual(last["후보 수"]["전체"], 3)
        self.assertEqual(last["전체 후보 수"], 3)
        self.assertEqual(last["현재 페이지"], 2)
        self.assertEqual(last["중복 제거 수"], 1)
        self.assertEqual([r["추천 순위"] for r in last["후보"]], [3])
        self.assertFalse(last["다음 후보 있음"])
        for destination, index in (("도쿄", 1), ("후쿠오카", 8)):
            with patch.object(engine, "google_places_text_search") as search:
                with self.assertRaises(ValueError):
                    engine.prepare_google_place_candidate_page(
                        "카페", "secret", destination, page_size=2,
                        page_token="next", page_index=index, session_state=state,
                    )
                search.assert_not_called()

    def test_google_http_400_invalid_key_is_auth_not_bad_query(self):
        error = urllib.error.HTTPError(engine.GOOGLE_PLACES_ENDPOINT, 400, "bad request", {}, None)
        error.read = lambda _size=-1: json.dumps({"error": {
            "message": "API key not valid: secret", "details": [{"reason": "API_KEY_INVALID"}],
        }}).encode()
        with patch.object(engine.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(engine.GooglePlacesError) as raised:
                engine.google_places_text_search("카페", "secret")
        self.assertEqual(raised.exception.error_code, "GOOGLE_AUTH")
        self.assertNotIn("secret", str(raised.exception))

    def test_google_transport_failures_and_invalid_json_are_classified(self):
        for cause in (TimeoutError("secret"), urllib.error.URLError("secret")):
            with self.subTest(cause=type(cause).__name__):
                with patch.object(engine.urllib.request, "urlopen", side_effect=cause):
                    with self.assertRaises(engine.GooglePlacesError) as raised:
                        engine.google_places_text_search("카페", "secret")
                self.assertEqual(raised.exception.error_code, "GOOGLE_UNAVAILABLE")
                self.assertTrue(raised.exception.retryable)
                self.assertNotIn("secret", str(raised.exception))
        class BadResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return b"not-json secret"
        with patch.object(engine.urllib.request, "urlopen", return_value=BadResponse()):
            with self.assertRaises(engine.GooglePlacesError) as raised:
                engine.google_places_text_search("카페", "secret")
        self.assertEqual(raised.exception.error_code, "GOOGLE_RESPONSE_INVALID")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))

    def test_error_packet_classifies_input_and_external_failures(self):
        invalid = engine.error_packet(ValueError("필수 값 누락"), "places")
        self.assertEqual(invalid["error_code"], "INPUT_INVALID")
        self.assertFalse(invalid["retryable"])
        self.assertEqual(invalid["stage"], "places")

        unavailable = engine.error_packet(urllib.error.URLError("offline"), "places")
        self.assertEqual(unavailable["error_code"], "EXTERNAL_UNAVAILABLE")
        self.assertTrue(unavailable["retryable"])
        self.assertIn("다시", unavailable["next_action"])

        google_unavailable = engine.error_packet(
            engine.GooglePlacesError("Google API timeout", "GOOGLE_UNAVAILABLE", True),
            "places",
        )
        self.assertEqual(google_unavailable["error_code"], "GOOGLE_UNAVAILABLE")
        self.assertTrue(google_unavailable["retryable"])

    def test_input_contract_lists_every_person_field_separately(self):
        contract = engine.input_fields_for("여행 개요·취향")
        self.assertEqual(contract["databases"], ["traveler"])
        self.assertEqual(
            [field["property"] for field in contract["fields"][:7]],
            ["여행자", "역할", "먹고 싶은 음식", "선호 이동", "숙소 취향", "주의사항", "추가 요청"],
        )
        self.assertNotIn("의견·요청", [field["property"] for field in contract["fields"]])

    def test_input_contract_separates_minimal_and_optional_questions(self):
        contract = engine.input_fields_for("여행 개요·취향")
        self.assertEqual(
            [field["property"] for field in contract["question_fields"]],
            ["여행자", "역할"],
        )
        optional_food = next(field for field in contract["optional_fields"] if field["property"] == "먹고 싶은 음식")
        self.assertFalse(optional_food["required"])
        self.assertFalse(optional_food["input_required"])
        place = engine.input_fields_for("장소 보관함")
        self.assertEqual(
            [field["property"] for field in place["question_fields"]],
            ["장소", "지역", "종류", "지도 URL"],
        )

    def test_traveler_profiles_are_split_and_missing_values_are_explicit(self):
        rows = engine.parse_traveler_profiles(
            "유성 | 총괄 | 라멘 | 지하철 | 역 근처 | 견과류 알레르기 | 늦은 밤 일정 제외\n"
            "민수 | | 스시 | | | |"
        )
        self.assertEqual(rows[0]["역할"], "총괄")
        self.assertEqual(rows[0]["추가 요청"], "늦은 밤 일정 제외")
        self.assertEqual(rows[1]["역할"], "미정")
        self.assertEqual(rows[1]["선호 이동"], "미정")
        self.assertNotIn("의견·요청", rows[0])

    def test_combined_legacy_person_field_is_not_silently_saved(self):
        with self.assertRaisesRegex(ValueError, "의견·요청"):
            engine.parse_traveler_profiles([{"이름": "유성", "의견·요청": "총괄, 라멘"}])

    def test_normalize_part_input_reports_target_databases_and_missing_fields(self):
        packet = engine.normalize_part_input(
            "항공",
            {"구간": "가는 편", "출발 공항": "ICN", "도착 공항": "FUK", "항공권 예산": 300000},
        )
        self.assertEqual(packet["databases"], ["long_transport", "reservation"])
        self.assertEqual(packet["rows"][0]["항공권 예산"], 300000)
        self.assertEqual(packet["status"], "입력 완료")
        self.assertEqual(packet["question_fields"][:3], ["구간", "출발 공항", "도착 공항"])

    def test_minimal_traveler_input_does_not_require_optional_preferences(self):
        packet = engine.normalize_part_input(
            "여행 개요·취향",
            {"여행자": "유성", "역할": "총괄"},
        )
        self.assertEqual(packet["status"], "입력 완료")
        self.assertEqual(packet["missing"], [])
        self.assertIn("먹고 싶은 음식", packet["optional_fields"])

    def test_photo_input_is_shared_by_air_stay_place_and_shopping(self):
        photo = "https://example.com/travel.jpg"
        for part, payload in (
            ("항공", {"구간": "가는 편", "출발 공항": "ICN", "도착 공항": "FUK", "사진": photo}),
            ("숙소", {"숙소명": "하카타 호텔", "사진 URL": photo}),
            ("장소", {"장소명": "오호리 공원", "사진": photo}),
            ("쇼핑", {"품목명": "유자 킷캣", "구매 장소": "하카타역", "수량": 1, "예상 금액": 1200, "사진": photo}),
        ):
            packet = engine.normalize_part_input(part, payload)
            self.assertEqual(packet["rows"][0]["사진"], photo)
            self.assertEqual(packet["rows"][0]["사진 URL"], photo)

    def test_organize_schedule_assigns_days_dates_and_times(self):
        rows = engine.organize_schedule(
            "1일차: 공항 도착 | 14:00-16:00 | 공항 | 체크인\n"
            "2일차 | 09:00-11:00 | 오호리 공원",
            "2026-12-12",
        )
        self.assertEqual([row["일차"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["날짜"], "2026-12-13")
        self.assertEqual(rows[0]["시작"], "2026-12-12T14:00:00")
        self.assertEqual(rows[1]["일정"], "오호리 공원")

    def test_organize_without_dates_preserves_day_only(self):
        rows = engine.organize_schedule("3일차 | 카페 투어", duration_days=4)
        self.assertEqual(rows, [{"일정": "카페 투어", "일차": 3}])

    def test_validate_detects_overlap_and_missing_movement_basis(self):
        report = engine.validate_trip(
            {"목적지": "후쿠오카", "출발일": "2026-12-12", "종료일": "2026-12-15", "기준 통화": "JPY"},
            [
                {"일정": "A", "날짜": "2026-12-12", "시작": "2026-12-12T10:00:00", "종료": "2026-12-12T12:00:00", "장소": "A"},
                {"일정": "B", "날짜": "2026-12-12", "시작": "2026-12-12T11:00:00", "종료": "2026-12-12T13:00:00", "장소": "B"},
            ],
        )
        kinds = {item["kind"] for item in report["issues"]}
        self.assertIn("overlap", kinds)
        self.assertIn("movement_unknown", kinds)
        self.assertFalse(report["ok"])

    def test_validate_detects_insufficient_travel_buffer(self):
        report = engine.validate_trip(
            {"목적지": "도쿄", "기간(일)": 2, "기준 통화": "JPY"},
            [
                {"일정": "A", "일차": 1, "시작": "2026-12-12T10:00:00", "종료": "2026-12-12T11:00:00", "장소": "A", "필요 이동(분)": 45},
                {"일정": "B", "일차": 1, "시작": "2026-12-12T11:30:00", "종료": "2026-12-12T12:00:00", "장소": "B"},
            ],
        )
        self.assertTrue(any(item["kind"] == "movement" for item in report["issues"]))

    def test_validate_deadline_and_stale_place(self):
        report = engine.validate_trip(
            {"목적지": "오사카", "기간(일)": 3, "기준 통화": "JPY"},
            reservations=[{"예약": "식당", "예약 상태": "대기", "취소 마감": "2026-12-12T12:00:00"}],
            places=[{"장소": "공원", "확인일": "2026-10-01", "재확인 주기": 30}],
            now=datetime.fromisoformat("2026-12-10T12:00:00"),
        )
        self.assertTrue(any(item["kind"] == "deadline" for item in report["issues"]))
        self.assertTrue(any(item["kind"] == "stale" for item in report["issues"]))

    def test_budget_calculates_wallet_balance_and_categories(self):
        report = engine.calculate_budget(
            [{"지갑": "공용", "통화": "JPY", "준비 금액": 100000}],
            [
                {"내역": "식사", "사용처": "식당", "거래 유형": "지출", "금액": 12000, "지갑 키": "공용", "상태": "완료", "통화": "JPY", "예약 대상": "식비"},
                {"내역": "환불", "거래 유형": "환불", "금액": 2000, "지갑 키": "공용", "상태": "완료", "통화": "JPY", "예약 대상": "식비"},
                {"내역": "예정", "금액": 5000, "지갑 키": "공용", "상태": "예정", "통화": "JPY"},
            ],
            base_currency="JPY",
        )
        self.assertEqual(report["spent"], 10000)
        self.assertEqual(report["remaining"], 90000)
        self.assertEqual(report["wallets"][0]["remaining"], 90000)
        self.assertEqual(report["by_category"]["식비"], 10000)

    def test_budget_reports_missing_exchange_rate(self):
        report = engine.calculate_budget(
            [{"지갑": "공용", "통화": "JPY", "준비 금액": 10000}],
            [{"내역": "항공", "금액": 100, "통화": "USD", "상태": "완료"}],
            base_currency="KRW",
        )
        self.assertEqual(report["spent"], 0)
        self.assertEqual(report["unknown_currency"][0]["currency"], "JPY")
        self.assertEqual(len(report["unknown_currency"]), 2)

    def test_budget_attaches_compact_expense_to_the_only_wallet(self):
        report = engine.calculate_budget(
            [{"지갑": "공용", "통화": "JPY", "준비 금액": 1000}],
            [{"내역": "커피", "금액": 100, "통화": "JPY", "상태": "완료"}],
            base_currency="JPY",
        )
        self.assertEqual(report["spent"], 100)
        self.assertEqual(report["wallets"][0]["spent"], 100)
        self.assertEqual(report["wallets"][0]["remaining"], 900)

    def test_budget_does_not_count_an_unlinked_expense_with_multiple_wallets(self):
        report = engine.calculate_budget(
            [{"지갑": "현금", "통화": "JPY", "준비 금액": 1000},
             {"지갑": "카드", "통화": "JPY", "준비 금액": 1000}],
            [{"내역": "커피", "금액": 100, "통화": "JPY", "상태": "완료"}],
            base_currency="JPY",
        )
        self.assertEqual(report["spent"], 0)
        self.assertEqual(report["remaining"], 2000)
        self.assertEqual(report["unknown_currency"][0]["reason"], "wallet_required")

    def test_budget_resolves_notion_relation_wallet_page_id(self):
        wallet_page = {
            "id": "wallet-page-id",
            "properties": {
                "지갑": {"type": "title", "title": [{"plain_text": "공용"}]},
                "통화": {"type": "select", "select": {"name": "JPY"}},
                "준비 금액": {"type": "number", "number": 1000},
            },
        }
        expense_page = {
            "id": "expense-page-id",
            "properties": {
                "내역": {"type": "title", "title": [{"plain_text": "커피"}]},
                "금액": {"type": "number", "number": 100},
                "거래 상태": {"type": "select", "select": {"name": "완료"}},
                "지갑": {"type": "relation", "relation": [{"id": "wallet-page-id"}]},
            },
        }
        report = engine.calculate_budget(
            [engine.notion_page_record(wallet_page)],
            [engine.notion_page_record(expense_page)],
            base_currency="JPY",
        )
        self.assertEqual(report["spent"], 100)
        self.assertEqual(report["wallets"][0]["spent"], 100)
        self.assertEqual(report["wallets"][0]["remaining"], 900)
        self.assertEqual(report["unknown_currency"], [])

    def test_progress_combines_automatic_statuses_and_exclusions(self):
        report = engine.calculate_progress(
            itinerary=[
                {"일정": "계획", "상태": "계획"},
                {"일정": "완료", "상태": "완료"},
                {"일정": "취소", "상태": "취소"},
            ],
            reservations=[{"예약": "항공", "예약 상태": "확정"}, {"예약": "환불", "예약 상태": "환불"}],
            tasks=[{"할 일": "여권", "상태": "완료"}],
            packing=[{"준비물": "충전기", "상태": "준비 완료"}],
        )
        self.assertEqual(report["completed"], 4)
        self.assertEqual(report["target"], 5)
        self.assertEqual(report["percent"], 80.0)
        self.assertEqual(report["categories"]["reservation"]["target"], 1)

    def test_progress_manual_complete_and_auto_exclude_are_reflected(self):
        report = engine.calculate_progress(
            itinerary=[
                {"일정": "자동 완료지만 검토", "상태": "완료", "자동 제외": True},
                {"일정": "직접 완료", "상태": "계획", "수동 완료": True},
            ],
            tasks=[{"할 일": "자동 대기", "상태": "시작 전"}],
        )
        self.assertEqual(report["completed"], 1)
        self.assertEqual(report["target"], 3)
        self.assertEqual(report["categories"]["itinerary"]["mode"], "수동")

    def test_progress_global_manual_controls_override_automatic_status(self):
        report = engine.calculate_progress(
            itinerary=[{"일정": "공항", "상태": "계획"}],
            reservations=[{"예약": "숙소", "예약 상태": "대기"}],
            trip={"진행도 수동 완료": True},
        )
        self.assertEqual(report["completed"], 2)
        self.assertEqual(report["percent"], 100.0)

    def test_progress_empty_and_disabled_categories_are_not_100_percent(self):
        report = engine.calculate_progress(
            itinerary=[],
            tasks=[{"할 일": "완료", "상태": "완료"}],
            overrides={"task": {"사용 여부": False}},
        )
        self.assertEqual(report["target"], 0)
        self.assertEqual(report["percent"], 0.0)
        self.assertEqual(report["status"], "등록 전")

    def test_settlement_creates_transfers(self):
        report = engine.calculate_budget(
            [], [],
            splits=[{"부담자": "민수", "부담액": 50000, "통화": "KRW"}, {"부담자": "지수", "부담액": 50000, "통화": "KRW"}],
            base_currency="KRW",
        )
        report["settlement"] = engine.calculate_settlement({"유성": 100000}, [
            {"부담자": "민수", "부담액": 50000, "통화": "KRW"},
            {"부담자": "지수", "부담액": 50000, "통화": "KRW"},
        ], "KRW", {})
        self.assertEqual(report["settlement"]["transfers"], [
            {"from": "민수", "to": "유성", "amount": 50000.0, "currency": "KRW"},
            {"from": "지수", "to": "유성", "amount": 50000.0, "currency": "KRW"},
        ])

    def test_import_csv_json_and_ics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "expenses.csv"
            csv_path.write_text("내역,금액,통화\n커피,500,JPY\n", encoding="utf-8")
            self.assertEqual(engine.import_file(csv_path, "auto")[0]["내역"], "커피")
            json_path = root / "rows.json"
            json_path.write_text(json.dumps({"records": [{"장소": "공원"}]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(engine.import_file(json_path, "auto"), [{"장소": "공원"}])
            ics_path = root / "trip.ics"
            ics_path.write_text("BEGIN:VEVENT\nSUMMARY:공항\nDTSTART:20261212T140000\nLOCATION:FUK\nEND:VEVENT\n", encoding="utf-8")
            self.assertEqual(engine.import_file(ics_path, "auto")[0]["장소"], "FUK")

    def test_place_import_and_link_validation(self):
        rows = engine.parse_place_lines("오호리 공원 | 후쿠오카 | 관광 | https://maps.google.com/?query=Ohori")
        self.assertEqual(rows[0]["장소"], "오호리 공원")
        self.assertEqual(engine.normalize_link(rows[0]["지도 URL"])["label"], "Ohori")
        self.assertEqual(engine.parse_place_lines("https://maps.google.com/?query=Ohori")[0]["장소"], "Ohori")

    def test_google_maps_link_search_packet_builds_a_no_key_search_flow(self):
        packet = engine.prepare_google_maps_link_search("카페", "후쿠오카")
        self.assertEqual(packet["검색 제공자"], "Google Maps URL")
        self.assertEqual(packet["상태"], "검색 링크 확인 대기")
        self.assertFalse(packet["후보 자동 수집"])
        self.assertFalse(packet["저장 가능"])
        self.assertIn("api=1", packet["검색 URL"])
        self.assertIn("query=%ED%9B%84%EC%BF%A0%EC%98%A4%EC%B9%B4+%EC%B9%B4%ED%8E%98", packet["검색 URL"])
        self.assertIn("링크를 보내주세요", packet["확인 질문"])

    def test_google_maps_link_search_requires_a_query_or_destination(self):
        with self.assertRaisesRegex(ValueError, "검색어 또는 여행지"):
            engine.prepare_google_maps_link_search("")

    def test_host_place_search_packet_is_the_default_without_api_key(self):
        packet = engine.prepare_host_place_search("카페", "후쿠오카")
        self.assertEqual(packet["검색 제공자"], "호스트 웹 검색")
        self.assertEqual(packet["검색 방식"], "AI 웹 검색")
        self.assertEqual(packet["상태"], "외부 검색 대기")
        self.assertEqual(packet["외부 조회"][0]["provider"], "host_web_search")
        self.assertEqual(packet["외부 조회"][0]["max_results"], 10)
        self.assertIn("검색 URL", packet["검색 링크 대체"])
        self.assertEqual(packet["검색 계약"]["버전"], 1)
        self.assertEqual(packet["검색 계약"]["최대 후보 수"], 10)
        self.assertEqual(packet["검색 설정"]["sort"], "추천순")
        self.assertEqual(packet["후보 수"]["현재 페이지"], 0)
        self.assertEqual(packet["후보 수"]["상태"], "검색 전")
        self.assertEqual(packet["외부 조회"][0]["result_contract"]["sort"], "추천순")

    def test_host_place_search_normalizes_sort_and_filters_without_api(self):
        packet = engine.prepare_host_place_search(
            "카페", "후쿠오카", sort="DISTANCE",
            search_filters={"includedType": "cafe", "openNow": True, "거리": "2km"},
        )
        self.assertEqual(packet["정렬"], "거리순")
        self.assertEqual(packet["검색 설정"]["sort"], "거리순")
        self.assertEqual(packet["검색 설정"]["filters"], {
            "included_type": "cafe", "open_now": True, "distance": "2km",
        })
        self.assertEqual(packet["외부 조회"][0]["sort"], "거리순")
        self.assertEqual(packet["외부 조회"][0]["filters"]["included_type"], "cafe")

    def test_host_candidates_use_the_same_contract_and_dedupe_as_api(self):
        packet = engine.prepare_place_candidate_page(
            [
                {"name": "모모치", "formattedAddress": "후쿠오카", "primaryType": "공원",
                 "googleMapsUri": "https://maps.google.com/?cid=momochi", "rank": 2},
                {"name": "오호리 공원", "formattedAddress": "후쿠오카", "primaryType": "공원",
                 "googleMapsUri": "https://maps.google.com/?cid=ohori", "rank": 1},
                # Same map URL: the lower-ranked duplicate must not consume a slot.
                {"name": "오호리 공원", "formattedAddress": "후쿠오카", "primaryType": "공원",
                 "googleMapsUri": "https://maps.google.com/?cid=ohori", "rank": 3},
            ],
            "후쿠오카",
            query="공원",
            search_filters={"kind": "공원", "openNow": True, "budget": "중간"},
        )
        self.assertEqual([row["추천 순위"] for row in packet["후보"]], [1, 2])
        self.assertEqual(packet["전체 후보 수"], 2)
        self.assertEqual(packet["후보 수"]["전체"], 2)
        self.assertEqual(packet["중복 제거 수"], 1)
        self.assertEqual(packet["검색 설정"]["filters"], {
            "kind": "공원", "open_now": True, "budget": "중간",
        })
        self.assertEqual(packet["정렬"], "추천순")
        self.assertEqual(packet["후보"][0]["검색 제공자"], "호스트 웹 검색")
        self.assertEqual(packet["후보"][0]["여행지 확인"], "확인 완료")

    def test_host_evidence_wrapper_preserves_query_and_filter_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "host-results.json"
            evidence_path.write_text(json.dumps({
                "query": "후쿠오카 카페",
                "destination": "후쿠오카",
                "provider": "호스트 웹 검색",
                "sort": "추천순",
                "filters": {"kind": "카페", "open_now": False},
                "evidence": [{"name": "카페 A", "region": "후쿠오카"}],
            }, ensure_ascii=False), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = engine.main([
                    "places", "--destination", "후쿠오카",
                    "--evidence-input", str(evidence_path),
                ])
        self.assertEqual(result, 0)
        packet = json.loads(output.getvalue())
        self.assertEqual(packet["검색 설정"]["query"], "후쿠오카 카페")
        self.assertEqual(packet["검색 설정"]["filters"], {"kind": "카페", "open_now": False})
        self.assertEqual(packet["후보"][0]["검색 제공자"], "호스트 웹 검색")

    def test_google_maps_search_url_is_not_treated_as_a_place(self):
        search_url = engine.build_google_maps_search_url("카페", "후쿠오카")
        row = engine.parse_place_lines(search_url)[0]
        self.assertEqual(row["정보 상태"], "검색 링크")
        self.assertEqual(row["장소"], "확인 필요")
        self.assertFalse(row["저장 가능"])
        packet = engine.prepare_place_confirmation(search_url, "후쿠오카")
        self.assertFalse(packet["저장 가능"])
        self.assertEqual(engine.confirm_place_rows(packet, ["1"]), [])

    def test_google_query_requests_host_search_when_api_key_is_missing(self):
        output = io.StringIO()
        with patch.dict(engine.os.environ, {}, clear=True):
            with contextlib.redirect_stdout(output):
                result = engine.main([
                    "places",
                    "--destination", "후쿠오카",
                    "--google-query", "카페",
                ])
        self.assertEqual(result, 0)
        packet = json.loads(output.getvalue())
        self.assertEqual(packet["검색 제공자"], "호스트 웹 검색")
        self.assertEqual(packet["상태"], "외부 검색 대기")
        self.assertEqual(packet["외부 조회"][0]["provider"], "host_web_search")

    def test_place_input_contract_exposes_two_stage_notion_structure(self):
        contract = engine.input_fields_for("장소 보관함")
        self.assertEqual(contract["notion_structure"]["canonical"]["database_key"], "place")
        self.assertFalse(contract["notion_structure"]["candidate_review"]["persist_before_approval"])
        with self.assertRaises(ValueError):
            engine.normalize_link("javascript:alert(1)")

    def test_google_short_link_resolves_place_name(self):
        short_url = "https://maps.app.goo.gl/example"
        resolved_url = "https://www.google.com/maps/place/Ohori+Park/@33.589,130.376"
        with patch.object(engine, "_resolve_short_map_url", return_value=resolved_url):
            info = engine.normalize_link(short_url)
            self.assertEqual(info["label"], "Ohori Park")
            self.assertEqual(info["resolution_status"], "resolved")
            self.assertEqual(info["resolved_url"], resolved_url)
            row = engine.parse_place_lines(short_url)[0]
            self.assertEqual(row["장소"], "Ohori Park")
            self.assertNotIn("정보 상태", row)

    def test_google_short_link_failure_needs_confirmation(self):
        short_url = "https://maps.app.goo.gl/expired"
        with patch.object(engine, "_resolve_short_map_url", return_value=None):
            info = engine.normalize_link(short_url)
            self.assertEqual(info["label"], "")
            self.assertEqual(info["resolution_status"], "needs_confirmation")
            row = engine.parse_place_lines(short_url)[0]
            self.assertEqual(row["장소"], "확인 필요")
            self.assertEqual(row["정보 상태"], "확인 필요")
            self.assertEqual(row["추가 확인"], ["장소명"])
            self.assertNotEqual(row["장소"], "maps.app.goo.gl")

    def test_external_place_lookup_can_be_deferred_from_core_parse(self):
        short_url = "https://maps.app.goo.gl/slow"
        with patch.object(engine, "_resolve_short_map_url", side_effect=AssertionError("must be deferred")):
            packet = engine.prepare_place_confirmation(
                short_url, "후쿠오카", defer_external=True
            )
        self.assertTrue(packet["외부 조회 분리"])
        self.assertEqual(packet["외부 조회"][0]["kind"], "place_lookup")
        self.assertEqual(packet["상태"] if "상태" in packet else packet["검토 필요"][0]["정보 상태"], "외부 확인 대기")

    def test_external_shopping_lookup_returns_job_without_search(self):
        packet = engine.prepare_shopping_research("유자 킷캣", "후쿠오카", defer_external=True)
        self.assertEqual(packet["상태"], "외부 확인 대기")
        self.assertEqual(packet["외부 조회"][0]["kind"], "shopping_lookup")

    def test_place_confirmation_holds_unknown_or_mismatched_destinations(self):
        packet = engine.prepare_place_confirmation(
            "오호리 공원 | 후쿠오카 | 관광 | https://maps.google.com/?query=Ohori\n"
            "도쿄 타워 | 도쿄 | 관광",
            "후쿠오카",
            [{"장소": "오호리 공원", "destination_match": True,
              "source": "https://example.com/ohori"},
             {"장소": "도쿄 타워", "destination_match": False,
              "source": "https://example.com/tokyo"}],
        )
        self.assertEqual(packet["장소"][0]["여행지 확인"], "확인 완료")
        self.assertEqual(packet["장소"][1]["여행지 확인"], "불일치")
        self.assertFalse(packet["저장 가능"])
        self.assertEqual([row["장소"] for row in packet["확인 완료"]], ["오호리 공원"])

    def test_place_confirmation_requires_evidence_when_region_is_ambiguous(self):
        packet = engine.prepare_place_confirmation("오호리 공원 | 하카타 | 관광", "후쿠오카")
        self.assertEqual(packet["장소"][0]["여행지 확인"], "확인 필요")
        self.assertEqual(engine.confirm_place_rows(packet, ["오호리 공원"])[0]["여행지 확인"], "사용자 확인 완료")

    def test_place_candidates_are_paginated_in_ranked_batches_of_ten(self):
        candidates = [
            {"name": f"장소 {index}", "region": "후쿠오카", "추천 순위": index}
            for index in range(1, 13)
        ]
        first = engine.prepare_place_candidate_page(candidates, "후쿠오카")
        self.assertEqual(len(first["후보"]), 10)
        self.assertEqual([row["추천 순위"] for row in first["후보"]], list(range(1, 11)))
        self.assertTrue(first["다음 후보 있음"])
        self.assertEqual(first["다음 시작"], 10)
        self.assertIn("10곳 더", first["더 필요 질문"])
        self.assertFalse(first["저장 가능"])
        self.assertFalse(first["Notion 구조"]["candidate_review"]["persist_before_approval"])
        self.assertEqual(first["Notion 구조"]["canonical"]["database_key"], "place")

        reordered = list(reversed(candidates))
        ranked = engine.prepare_place_candidate_page(reordered, "후쿠오카")
        self.assertEqual([row["장소"] for row in ranked["후보"][:2]], ["장소 1", "장소 2"])

        second = engine.prepare_place_candidate_page(candidates, "후쿠오카", offset=10)
        self.assertEqual(len(second["후보"]), 2)
        self.assertEqual([row["추천 순위"] for row in second["후보"]], [11, 12])
        self.assertFalse(second["다음 후보 있음"])
        self.assertIsNone(second["다음 시작"])
        self.assertIn("모두", second["더 필요 질문"])

    def test_place_candidates_show_fewer_than_ten_without_fake_rows(self):
        candidates = [{"name": "오호리 공원", "region": "후쿠오카"}, {"name": "모모치", "region": "후쿠오카"}]
        packet = engine.prepare_place_candidate_page(candidates, "후쿠오카")
        self.assertEqual(packet["전체 후보 수"], 2)
        self.assertEqual(len(packet["장소"]), 2)
        self.assertFalse(packet["다음 후보 있음"])
        self.assertEqual(packet["더 필요 질문"], "조건에 맞는 후보를 모두 보여드렸어요.")

    def test_explicit_place_text_is_not_truncated_by_candidate_paging(self):
        packet = engine.prepare_place_confirmation(
            "A | 후쿠오카\nB | 후쿠오카\nC | 후쿠오카", "후쿠오카",
            candidate_offset=10,
        )
        self.assertEqual([row["장소"] for row in packet["장소"]], ["A", "B", "C"])

    def test_google_places_search_normalizes_results_without_exposing_key(self):
        payload = {
            "places": [{
                "id": "places/abc",
                "displayName": {"text": "오호리 공원"},
                "formattedAddress": "후쿠오카시 주오구",
                "googleMapsUri": "https://maps.google.com/?cid=abc",
                "rating": 4.7,
                "userRatingCount": 1234,
                "primaryTypeDisplayName": {"text": "공원"},
                "currentOpeningHours": {"openNow": True, "weekdayDescriptions": ["월요일: 24시간"]},
                "photos": [{"name": "places/abc/photos/1"}],
            }],
            "nextPageToken": "opaque-next-token",
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch.object(engine.urllib.request, "urlopen", return_value=FakeResponse()) as opener:
            result = engine.google_places_text_search("후쿠오카 공원", "secret-key")
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("X-goog-api-key"), "secret-key")
        self.assertNotIn("places.photos", request.get_header("X-goog-fieldmask"))
        self.assertEqual(result["candidates"][0]["name"], "오호리 공원")
        self.assertEqual(result["candidates"][0]["photo_resource"], "places/abc/photos/1")
        self.assertEqual(result["next_page_token"], "opaque-next-token")
        self.assertNotIn("secret-key", json.dumps(result, ensure_ascii=False))

    def test_google_place_page_uses_provider_token_for_next_ten(self):
        with patch.object(
            engine, "google_places_text_search",
            return_value={
                "query": "후쿠오카 관광지",
                "source": "Google Places API (New)",
                "next_page_token": "next-token",
                "candidates": [{"name": f"장소 {index}"} for index in range(1, 11)],
            },
        ) as search:
            packet = engine.prepare_google_place_candidate_page(
                "후쿠오카 관광지", "secret-key", "후쿠오카", page_index=1,
                page_token="previous-token",
                session_state={
                    "destination": "후쿠오카",
                    "page_token": "previous-token",
                    "page_index": 1,
                    "query": "후쿠오카 관광지",
                    "language_code": "ko",
                    "region_code": "",
                    "open_now": None,
                    "rank_preference": "RELEVANCE",
                    "included_type": "",
                    "location_bias": {},
                    "summary_only": False,
                    "page_size": 10,
                    "issued_at": time.time(),
                },
            )
        search.assert_called_once()
        self.assertEqual([row["추천 순위"] for row in packet["후보"]], list(range(11, 21)))
        self.assertTrue(packet["다음 후보 있음"])
        self.assertEqual(packet["후속 검색 상태"]["page_token"], "next-token")
        self.assertEqual(packet["검색 계약"]["버전"], 1)
        self.assertEqual(packet["검색 설정"]["offset"], 10)
        self.assertEqual(packet["후보 수"]["시작 순위"], 11)
        self.assertTrue(packet["후보 수"]["다음 묶음 있음"])

    def test_google_place_page_marks_destination_mismatch_and_requires_session_match(self):
        with patch.object(
            engine, "google_places_text_search",
            return_value={
                "query": "후쿠오카 관광지",
                "source": "Google Places API (New)",
                "next_page_token": "",
                "candidates": [{"name": "도쿄 타워", "region": "도쿄도"}],
            },
        ):
            packet = engine.prepare_google_place_candidate_page(
                "후쿠오카 관광지", "secret-key", "후쿠오카"
            )
        self.assertEqual(packet["후보"][0]["여행지 확인"], "불일치")
        self.assertEqual(engine.confirm_place_rows(
            {"장소": [{
                "장소": "도쿄 타워", "장소 ID": "places/tokyo", "추천 순위": 1,
                "지역": "도쿄도", "Google Maps 제공": True,
                "여행지 확인": "불일치",
            }]},
            ["1"], {"1": "도쿄 타워"},
        ), [])
        with self.assertRaisesRegex(ValueError, "세션 상태"):
            engine.prepare_google_place_candidate_page(
                "후쿠오카 관광지", "secret-key", "후쿠오카", page_token="stale-token"
            )

    def test_google_duplicate_names_require_rank_or_place_id(self):
        packet = {"장소": [
            {"장소": "스타벅스", "장소 ID": "places/a", "추천 순위": 1,
             "Google Maps 제공": True, "여행지 확인": "확인 완료"},
            {"장소": "스타벅스", "장소 ID": "places/b", "추천 순위": 2,
             "Google Maps 제공": True, "여행지 확인": "확인 완료"},
        ]}
        self.assertEqual(engine.confirm_place_rows(packet, ["스타벅스"], {
            "스타벅스": "내 카페"
        }), [])
        saved = engine.confirm_place_rows(packet, ["2"], {"2": "두 번째 카페"})
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["장소"], "두 번째 카페")

    def test_google_summary_mode_uses_minimal_field_mask(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"places": []}).encode("utf-8")

        with patch.object(engine.urllib.request, "urlopen", return_value=FakeResponse()) as opener:
            engine.google_places_text_search("후쿠오카 공원", "secret-key", summary_only=True)
        self.assertEqual(
            opener.call_args.args[0].get_header("X-goog-fieldmask"),
            engine.GOOGLE_PLACES_SUMMARY_FIELD_MASK,
        )

    def test_google_search_options_are_sent_without_exposing_key(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"places": []}).encode("utf-8")

        with patch.object(engine.urllib.request, "urlopen", return_value=FakeResponse()) as opener:
            engine.google_places_text_search(
                "후쿠오카 카페",
                "secret-key",
                rank_preference="DISTANCE",
                included_type="cafe",
                location_bias={"circle": {"center": {"latitude": 33.59, "longitude": 130.40}, "radius": 1200}},
                open_now=True,
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["rankPreference"], "DISTANCE")
        self.assertEqual(body["includedType"], "cafe")
        self.assertTrue(body["openNow"])
        self.assertIn("locationBias", body)
        self.assertNotIn("secret-key", request.data.decode("utf-8"))

    def test_google_http_errors_are_classified_and_secret_is_redacted(self):
        cases = ((401, "GOOGLE_AUTH", False), (403, "GOOGLE_AUTH", False), (429, "GOOGLE_RATE_LIMIT", True), (503, "GOOGLE_UNAVAILABLE", True))
        for status, expected_code, retryable in cases:
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    engine.GOOGLE_PLACES_ENDPOINT,
                    status,
                    "provider error",
                    {"Content-Type": "application/json"},
                    None,
                )
                error.read = lambda _size=-1: json.dumps({
                    "error": {"message": "bad secret-key response"}
                }).encode("utf-8")
                with patch.object(engine.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaises(engine.GooglePlacesError) as raised:
                        engine.google_places_text_search("후쿠오카", "secret-key")
                self.assertEqual(raised.exception.error_code, expected_code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("secret-key", str(raised.exception))

    def test_google_page_token_rejects_expired_search_session(self):
        with self.assertRaisesRegex(ValueError, "만료"):
            engine.prepare_google_place_candidate_page(
                "후쿠오카 관광지", "secret-key", "후쿠오카", page_index=1,
                page_token="expired-token",
                session_state={
                    "destination": "후쿠오카",
                    "page_token": "expired-token",
                    "page_index": 1,
                    "query": "후쿠오카 관광지",
                    "language_code": "ko",
                    "region_code": "",
                    "open_now": None,
                    "rank_preference": "RELEVANCE",
                    "included_type": "",
                    "location_bias": {},
                    "summary_only": False,
                    "page_size": 10,
                    "issued_at": time.time() - engine.GOOGLE_PAGE_TOKEN_TTL_SECONDS - 1,
                },
            )

    def test_google_candidates_need_user_label_before_notion_save(self):
        packet = {
            "장소": [{
                "장소": "Google 장소명",
                "장소 ID": "places/abc",
                "지도 URL": "https://maps.google.com/?cid=abc",
                "Google Maps 제공": True,
                "저장 가능": False,
            }]
        }
        self.assertEqual(engine.confirm_place_rows(packet, ["Google 장소명"]), [])
        saved = engine.confirm_place_rows(
            packet,
            ["Google 장소명"],
            {"Google 장소명": "내 여행 장소"},
        )
        self.assertEqual(saved[0]["장소"], "내 여행 장소")
        self.assertEqual(saved[0]["장소 ID"], "places/abc")
        self.assertNotIn("Google Maps 제공", saved[0])
        self.assertNotIn("평점", saved[0])
        self.assertEqual(saved[0]["검색 제공자"], "Google Maps")
        self.assertEqual(saved[0]["정보 상태"], "사용자 확인 완료")

    def test_host_search_fallback_works_without_google_api_key(self):
        packet = engine.prepare_place_confirmation(
            "", "후쿠오카", [
                {"name": "오호리 공원", "region": "후쿠오카"},
                {"name": "모모치 해변", "region": "후쿠오카"},
            ],
        )
        self.assertEqual([row["장소"] for row in packet["후보"]], ["오호리 공원", "모모치 해변"])
        self.assertFalse(packet["저장 가능"])
        with self.assertRaisesRegex(ValueError, "GOOGLE_PLACES_API_KEY"):
            engine.google_places_text_search("후쿠오카 관광지", "")

    def test_shopping_import_preserves_optional_fields(self):
        rows = engine.parse_shopping_lines("유자 킷캣 | 유성 | 하카타역 | 디저트 | 1200 | 카드 | 선물용")
        self.assertEqual(rows[0]["품목"], "유자 킷캣")
        self.assertEqual(rows[0]["담당 여행자"], "유성")
        self.assertEqual(rows[0]["예상 금액"], 1200.0)
        self.assertEqual(rows[0]["태그"], "디저트")

    def test_shopping_import_requires_price_quantity_and_place(self):
        rows = engine.parse_shopping_lines("유자 킷캣 | 유성 | 하카타역 | 3개 | JPY 1,200 | 디저트 | 카드 | 선물용")
        self.assertEqual(rows[0]["수량"], 3)
        self.assertEqual(rows[0]["예상 금액"], 1200)
        self.assertEqual(rows[0]["정보 상태"], "입력 완료")
        self.assertNotIn("추가 확인", rows[0])

        incomplete = engine.parse_shopping_lines("유자 킷캣 | 유성")
        self.assertEqual(incomplete[0]["정보 상태"], "확인 필요")
        self.assertEqual(set(incomplete[0]["추가 확인"]), {"구매 장소", "수량", "예상 금액"})

        short = engine.parse_shopping_lines("유자 킷캣 | JPY 1,200 | 3개 | 하카타역 돈키호테")
        self.assertEqual(short[0]["예상 금액"], 1200)
        self.assertEqual(short[0]["수량"], 3)
        self.assertEqual(short[0]["구매 장소"], "하카타역 돈키호테")
        self.assertEqual(short[0]["정보 상태"], "입력 완료")

        quantity_first = engine.parse_shopping_lines("유자 킷캣 | 3개 | 하카타역 돈키호테 | JPY 1,200")
        self.assertEqual(quantity_first[0]["예상 금액"], 1200)
        self.assertEqual(quantity_first[0]["수량"], 3)
        self.assertEqual(quantity_first[0]["구매 장소"], "하카타역 돈키호테")

    def test_shopping_research_never_guesses_name_only_values(self):
        packet = engine.prepare_shopping_research("유자 킷캣", "후쿠오카")
        self.assertEqual(packet["검색어"], "후쿠오카 유자 킷캣")
        self.assertEqual(packet["상태"], "검색 필요")
        self.assertFalse(packet["저장 가능"])
        self.assertEqual(set(packet["추가 확인"]), {"구매 장소", "수량", "예상 금액"})

    def test_shopping_research_candidates_need_confirmation(self):
        packet = engine.prepare_shopping_research(
            "유자 킷캣",
            "후쿠오카",
            [{"price": "JPY 1,200", "quantity": "3개", "store": "하카타역 돈키호테",
              "source_url": "https://example.com/item"}],
            "JPY",
        )
        self.assertEqual(packet["상태"], "확인 대기")
        self.assertTrue(packet["저장 가능"])
        self.assertEqual(packet["후보"][0]["예상 금액"], 1200)
        self.assertIn("출처: https://example.com/item", packet["후보"][0]["링크·메모"])

    def test_shopping_confirmation_requires_explicit_approval(self):
        candidate = {"price": 1200, "quantity": 3, "store": "하카타역 돈키호테", "currency": "JPY"}
        pending = engine.confirm_shopping_candidate("유자 킷캣", candidate, "유성", "JPY")
        self.assertFalse(pending["저장 가능"])
        self.assertEqual(pending["확인 상태"], "사용자 확인 대기")
        approved = engine.confirm_shopping_candidate("유자 킷캣", candidate, "유성", "JPY", True)
        self.assertTrue(approved["저장 가능"])
        self.assertEqual(approved["정보 상태"], "사용자 확인 완료")

    def test_refresh_view_returns_today_next_deadlines_and_budget(self):
        view = engine.refresh_view(
            {"기준 통화": "JPY"},
            [
                {"일정": "오늘", "날짜": "2026-12-12", "시작": "2026-12-12T10:00:00", "상태": "계획"},
                {"일정": "내일", "날짜": "2026-12-13", "시작": "2026-12-13T10:00:00", "상태": "계획"},
            ],
            reservations=[{"예약": "티켓", "예약 상태": "대기", "취소 마감": "2026-12-12T18:00:00"}],
            wallets=[{"지갑": "공용", "통화": "JPY", "준비 금액": 1000}],
            expenses=[{"내역": "커피", "금액": 100, "통화": "JPY", "상태": "완료"}],
            now=datetime.fromisoformat("2026-12-12T09:00:00"),
            shopping=[{"품목": "과자", "상태": "구매 예정"}, {"품목": "엽서", "상태": "구매 완료"}],
        )
        self.assertEqual([row["일정"] for row in view["today"]], ["오늘"])
        self.assertEqual(view["next"]["일정"], "오늘")
        self.assertEqual(view["budget"]["remaining"], 900)
        self.assertEqual(view["deadline_alerts"][0]["예약"], "티켓")
        self.assertEqual(view["shopping"]["planned"], 1)
        self.assertEqual(view["shopping"]["purchased"], 1)
        self.assertEqual(view["progress"]["target"], 3)
        self.assertEqual(view["progress"]["percent"], 0.0)

    def test_notion_page_record_flattens_properties(self):
        page = {"id": "page-1", "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "여행"}]},
            "인원": {"type": "number", "number": 3},
            "상태": {"type": "select", "select": {"name": "계획 중"}},
            "완료": {"type": "checkbox", "checkbox": True},
            "날짜": {"type": "date", "date": {"start": "2026-12-12"}},
        }}
        result = engine.notion_page_record(page)
        self.assertEqual(result["이름"], "여행")
        self.assertEqual(result["인원"], 3)
        self.assertTrue(result["완료"])
        self.assertEqual(result["날짜"], "2026-12-12")

    def test_notion_snapshot_reads_only_current_trip_and_needed_databases(self):
        class SnapshotClient:
            def __init__(self):
                self.calls = []

            def get_page(self, page_id):
                return {"id": page_id, "properties": {
                    "여행": {"type": "title", "title": [{"plain_text": "오사카 여행"}]},
                    "여행 키": {"type": "rich_text", "rich_text": [{"plain_text": "trip-key"}]},
                }}

            def query_database(self, database_id, payload=None):
                self.calls.append((database_id, payload))
                return [
                    {"id": "current", "properties": {"여행 키": {"type": "rich_text", "rich_text": [{"plain_text": "page-1"}]}}},
                    {"id": "stale", "properties": {"여행 키": {"type": "rich_text", "rich_text": [{"plain_text": "other-trip"}]}}},
                ]

        client = SnapshotClient()
        snapshot = engine.load_notion_snapshot(
            client,
            {"databases": {"itinerary": "itinerary-db", "reservation": "reservation-db", "stay": "stay-db",
                            "wallet": "wallet-db", "expense": "expense-db", "shopping": "shopping-db",
                            "material": "material-db"}},
            "page-1",
            "refresh",
        )
        self.assertEqual({call[0] for call in client.calls}, {"itinerary-db", "reservation-db", "stay-db", "wallet-db", "expense-db", "shopping-db", "material-db"})
        self.assertNotIn("stale", {row["id"] for row in snapshot["itinerary"]})
        self.assertTrue(client.calls[0][1]["filter"])

    def test_notion_snapshot_uses_live_trip_relation_when_key_is_missing(self):
        class RelationSnapshotClient:
            def __init__(self):
                self.calls = []

            def get_page(self, page_id):
                return {"id": page_id, "properties": {
                    "여행": {"type": "title", "title": [{"plain_text": "후쿠오카 여행"}]},
                }}

            def get_database(self, database_id):
                return {"id": database_id, "properties": {"여행": {"type": "relation"}}}

            def query_database(self, database_id, payload=None):
                self.calls.append((database_id, payload))
                return [{"id": "current", "properties": {
                    "여행": {"type": "relation", "relation": [{"id": "11111111-1111-1111-1111-111111111111"}]},
                }}]

        client = RelationSnapshotClient()
        snapshot = engine.load_notion_snapshot(
            client, {"databases": {"itinerary": "itinerary-db"}}, "11111111-1111-1111-1111-111111111111", "check"
        )
        self.assertEqual(snapshot["itinerary"][0]["id"], "current")
        self.assertEqual(client.calls[0][1]["filter"], {
            "property": "여행", "relation": {"contains": "11111111-1111-1111-1111-111111111111"},
        })


class NotionClientExtensionTests(unittest.TestCase):
    def test_query_database_follows_cursor(self):
        from importlib.util import spec_from_file_location, module_from_spec
        script = Path(__file__).parents[1] / "scripts" / "notion_installer.py"
        spec = spec_from_file_location("notion_installer_for_engine_test", script)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.NotionClient("token")
        with patch.object(client, "request", side_effect=[
            {"results": [{"id": "1"}], "has_more": True, "next_cursor": "next"},
            {"results": [{"id": "2"}], "has_more": False},
        ]) as request:
            self.assertEqual([row["id"] for row in client.query_database("db")], ["1", "2"])
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
