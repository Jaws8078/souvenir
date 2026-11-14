"""Offline tests for Souvenir's Python travel engine."""
import csv
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "trip_engine.py"
SPEC = importlib.util.spec_from_file_location("trip_engine", SCRIPT)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class TripEngineTests(unittest.TestCase):
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
        with self.assertRaises(ValueError):
            engine.normalize_link("javascript:alert(1)")

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
        )
        self.assertEqual([row["일정"] for row in view["today"]], ["오늘"])
        self.assertEqual(view["next"]["일정"], "오늘")
        self.assertEqual(view["budget"]["remaining"], 900)
        self.assertEqual(view["deadline_alerts"][0]["예약"], "티켓")

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
