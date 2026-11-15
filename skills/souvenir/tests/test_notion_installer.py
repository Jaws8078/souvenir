"""Offline regression tests: never contact Notion or create real travel data."""
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "notion_installer.py"
SPEC = importlib.util.spec_from_file_location("notion_installer", SCRIPT)
ni = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ni
SPEC.loader.exec_module(ni)
PARENT = "11111111-1111-1111-1111-111111111111"


class FakeNotion:
    def __init__(self):
        self.created = []
        self.blocks = []
        self.fail_at = None
        self.fail_on_append_after = None
        self.append_calls = 0
        self.fail_on_database_id = None
        self.fail_on_database_page_after = None
        self.database_page_calls = {}
        self.error = ni.InstallerError("definite rejection")
        self.calls = 0

    def get_page(self, page_id):
        for _kind, _parent, properties, row_id in self.created:
            if row_id == page_id:
                return {"id": row_id, "properties": copy.deepcopy(properties), "last_edited_time": "v1"}
        return {"id": page_id, "last_edited_time": "v1"}

    def get_database(self, database_id):
        for kind, _parent, properties, row_id in self.created:
            if kind == "database" and row_id == database_id:
                return {"id": row_id, "properties": copy.deepcopy(properties)}
        return {"id": database_id, "properties": {ni.IDEMPOTENCY_PROPERTY: {"rich_text": {}}}}

    def add_database_property(self, database_id, name):
        for kind, _parent, properties, row_id in self.created:
            if kind == "database" and row_id == database_id:
                properties[name] = {"rich_text": {}}
                return {"id": row_id, "properties": copy.deepcopy(properties)}
        return {"id": database_id, "properties": {name: {"rich_text": {}}}}

    def create(self, kind, parent, payload):
        self.calls += 1
        if self.calls == self.fail_at:
            raise self.error
        result = {"id": f"object-{len(self.created) + 1}"}
        self.created.append((kind, parent, copy.deepcopy(payload), result["id"]))
        return result

    def create_page(self, parent, title, icon=None):
        return self.create("page", parent, title)

    def create_database(self, parent, title, properties, icon=None):
        return self.create("database", parent, properties)

    def create_database_page(self, parent, properties):
        count = self.database_page_calls.get(parent, 0) + 1
        self.database_page_calls[parent] = count
        if (parent == self.fail_on_database_id and
                self.fail_on_database_page_after is not None and
                count == self.fail_on_database_page_after):
            raise self.error
        return self.create("row", parent, properties)

    def query_database(self, database_id, payload=None):
        payload = payload or {}
        filter_value = payload.get("filter", {})

        def read_text(properties, name):
            values = properties.get(name, {}).get("title") or properties.get(name, {}).get("rich_text") or []
            return "".join(item.get("text", {}).get("content", "") for item in values)

        def matches_filter(properties, current):
            if "and" in current:
                return all(matches_filter(properties, child) for child in current["and"])
            name = current.get("property")
            if "rich_text" in current:
                return read_text(properties, name) == current["rich_text"].get("equals")
            if "select" in current:
                return properties.get(name, {}).get("select", {}).get("name") == current["select"].get("equals")
            return False

        results = []
        for kind, parent, properties, row_id in self.created:
            if kind != "row" or parent != database_id:
                continue
            if matches_filter(properties, filter_value):
                results.append({"id": row_id, "properties": copy.deepcopy(properties)})
        return results

    def update_page_properties(self, page_id, properties, expected_last_edited_time=None):
        for index, (kind, parent, current, row_id) in enumerate(self.created):
            if row_id != page_id:
                continue
            if expected_last_edited_time is not None and expected_last_edited_time != "v1":
                raise ni.ConflictError("stale")
            current.update(copy.deepcopy(properties))
            self.created[index] = (kind, parent, current, row_id)
            return {"id": row_id, "properties": copy.deepcopy(current), "last_edited_time": "v1"}
        return {"id": page_id, "properties": copy.deepcopy(properties), "last_edited_time": "v1"}

    def append_blocks(self, page_id, blocks):
        self.calls += 1
        self.append_calls += 1
        if self.append_calls == self.fail_on_append_after:
            raise self.error
        if self.calls == self.fail_at:
            raise self.error
        self.blocks.append((page_id, copy.deepcopy(blocks)))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mapping.json"
        self.manifest = ni.read_json(ni.DEFAULT_MANIFEST)
        self.client = FakeNotion()

    def install(self):
        return ni.install(self.manifest, self.client, PARENT, self.path)

    def scaffold(self, **kwargs):
        options = dict(destination="후쿠오카", start_date="2026-12-12", end_date="2026-12-15",
                       duration_days=None, traveler_count=4, currency="JPY")
        options.update(kwargs)
        return ni.scaffold_trip(self.manifest, self.client, self.path, **options)

    def core_row_ids(self):
        mapping = ni.read_json(self.path)
        database_ids = {mapping["databases"][key] for key in
                        ("trip", "itinerary", "traveler", "wallet", "task", "packing")}
        return {row_id for kind, parent, _payload, row_id in self.client.created
                if kind == "row" and parent in database_ids}

    def test_manifest_and_repeat_install(self):
        self.assertEqual(ni.validate_manifest(self.manifest), [])
        first = self.install()
        calls = self.client.calls
        again = self.install()
        self.assertEqual(first["pages"], again["pages"])
        self.assertEqual(again["status"], "complete")
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(len(first["seeded_rows"]["task"]), 15)
        self.assertEqual(len(first["seeded_rows"]["packing"]), 8)

    def test_manifest_uses_free_text_for_personal_preferences(self):
        traveler = ni.database_by_key(self.manifest, "traveler")
        props = {item["name"]: item for item in traveler["properties"]}
        for name in ("역할", "먹고 싶은 음식", "선호 이동", "숙소 취향", "주의사항", "추가 요청"):
            self.assertEqual(props[name]["type"], "rich_text")
        shopping = ni.database_by_key(self.manifest, "shopping")
        shopping_props = {item["name"]: item for item in shopping["properties"]}
        self.assertEqual(shopping_props["수량"]["type"], "number")
        self.assertIn("정보 상태", shopping_props)

    def test_confirmed_shopping_rows_are_created_and_re_read(self):
        self.install()
        result = self.scaffold(traveler_count=2)
        mapping = ni.read_json(self.path)
        rows = [{
            "품목": "유자 킷캣",
            "담당 여행자": "동행자 1",
            "구매 장소": "하카타역 돈키호테",
            "수량": 3,
            "예상 금액": 1200,
            "통화": "JPY",
            "상태": "구매 예정",
            "정보 상태": "사용자 확인 완료",
            "링크·메모": "출처: https://example.com/item",
        }]
        saved = ni.save_shopping_rows(self.manifest, self.client, self.path,
                                      result["trip_id"], rows, confirmed=True)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(len(saved["created"]), 1)
        self.assertEqual(len(saved["rows"]), 1)
        shopping_db = mapping["databases"]["shopping"]
        payload = next(payload for kind, parent, payload, row_id in self.client.created
                       if kind == "row" and parent == shopping_db and row_id == saved["created"][0])
        self.assertEqual(payload["수량"]["number"], 3)
        self.assertEqual(payload["정보 상태"]["select"]["name"], "사용자 확인 완료")
        self.assertEqual(payload["여행 키"]["rich_text"][0]["text"]["content"], result["trip_id"])

    def test_traveler_profiles_write_independent_columns_and_are_idempotent(self):
        self.install()
        result = self.scaffold(traveler_count=2)
        profiles = [
            {
                "여행자": "유성",
                "역할": "총괄",
                "먹고 싶은 음식": "라멘",
                "선호 이동": "지하철",
                "숙소 취향": "역 근처",
                "주의사항": "견과류 없음",
                "추가 요청": "늦은 밤 일정 제외",
            },
            {
                "여행자": "민수",
                "역할": "예약",
                "먹고 싶은 음식": "스시",
                "선호 이동": "도보",
                "숙소 취향": "조용한 방",
                "주의사항": "미정",
                "추가 요청": "미정",
            },
        ]
        saved = ni.save_traveler_profiles(self.manifest, self.client, self.path,
                                          result["trip_id"], profiles, confirmed=True)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(len(saved["updated"]), 2)
        self.assertEqual(saved["created"], [])
        traveler_db = ni.read_json(self.path)["databases"]["traveler"]
        row_id = saved["updated"][0]
        payload = next(payload for kind, parent, payload, current_id in self.client.created
                       if kind == "row" and parent == traveler_db and current_id == row_id)
        self.assertEqual(payload["역할"]["rich_text"][0]["text"]["content"], "총괄")
        self.assertEqual(payload["먹고 싶은 음식"]["rich_text"][0]["text"]["content"], "라멘")
        self.assertEqual(payload["주의사항"]["rich_text"][0]["text"]["content"], "견과류 없음")
        self.assertNotIn("의견·요청", payload)
        second = ni.save_traveler_profiles(self.manifest, self.client, self.path,
                                           result["trip_id"], profiles, confirmed=True)
        self.assertEqual(second["created"], [])
        self.assertEqual(second["updated"], saved["updated"])

    def test_traveler_profiles_reject_combined_legacy_column(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        with self.assertRaisesRegex(ni.InstallerError, "의견·요청"):
            ni.save_traveler_profiles(
                self.manifest, self.client, self.path, result["trip_id"],
                [{"여행자": "유성", "의견·요청": "총괄, 라멘"}], confirmed=True,
            )

    def test_part_save_routes_fields_and_reuses_scaffold_rows(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        rows = [{
            "일정": "공항 도착·체크인",
            "일차": 1,
            "날짜": "2026-12-12",
            "시작": "2026-12-12T14:00:00",
            "종료": "2026-12-12T16:00:00",
            "장소": "후쿠오카 공항",
            "플랜 B": "공항 라운지",
        }]
        saved = ni.save_part_rows(self.manifest, self.client, self.path,
                                  result["trip_id"], "날짜별 일정", rows, confirmed=True)
        self.assertEqual(saved["created"], [])
        self.assertEqual(len(saved["updated"]), 1)
        itinerary_db = ni.read_json(self.path)["databases"]["itinerary"]
        row_id = saved["updated"][0]
        payload = next(payload for kind, parent, payload, current_id in self.client.created
                       if kind == "row" and parent == itinerary_db and current_id == row_id)
        self.assertEqual(payload["일차"]["number"], 1)
        self.assertEqual(payload["장소"]["rich_text"][0]["text"]["content"], "후쿠오카 공항")
        self.assertEqual(payload["플랜 B"]["rich_text"][0]["text"]["content"], "공항 라운지")

    def test_part_save_updates_trip_summary_in_place(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        saved = ni.save_part_rows(
            self.manifest, self.client, self.path, result["trip_id"], "기본 여행 정보",
            [{"여행지": "후쿠오카", "여행 템포": "느긋하게", "가장 중요한 기준": "이동 편의"}],
            confirmed=True,
        )
        self.assertEqual(saved["created"], [])
        self.assertEqual(saved["updated"], [result["trip_id"]])
        trip_db = ni.read_json(self.path)["databases"]["trip"]
        payload = next(payload for kind, parent, payload, row_id in self.client.created
                       if kind == "row" and parent == trip_db and row_id == result["trip_id"])
        self.assertEqual(payload["목적지"]["rich_text"][0]["text"]["content"], "후쿠오카")
        self.assertEqual(payload["여행 템포"]["select"]["name"], "느긋하게")

    def test_part_save_rejects_legacy_combined_field(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        with self.assertRaisesRegex(ni.InstallerError, "의견·요청"):
            ni.save_part_rows(self.manifest, self.client, self.path,
                              result["trip_id"], "숙소",
                              [{"숙소명": "하카타 호텔", "의견·요청": "역 근처"}],
                              confirmed=True)

    def test_budget_part_splits_wallet_and_expense_rows(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        saved = ni.save_part_rows(
            self.manifest, self.client, self.path, result["trip_id"], "예산·여행 지갑",
            [{"지갑 이름": "유성 개인 지갑", "통화": "JPY", "준비 금액": 50000,
              "사용처": "라멘", "거래 유형": "지출", "결제 단계": "현장 결제",
              "결제 수단": "카드", "실제 사용 금액": 1200, "예산 카테고리": "식비"}],
            confirmed=True,
        )
        self.assertEqual(saved["status"], "complete")
        mapping = ni.read_json(self.path)["databases"]
        wallet_payload = next(payload for kind, parent, payload, _row_id in self.client.created
                              if kind == "row" and parent == mapping["wallet"]
                              and payload.get("통화", {}).get("select", {}).get("name") == "JPY"
                              and payload.get("준비 금액", {}).get("number") == 50000)
        expense_payload = next(payload for kind, parent, payload, _row_id in self.client.created
                               if kind == "row" and parent == mapping["expense"]
                               and payload.get("금액", {}).get("number") == 1200)
        self.assertEqual(wallet_payload["준비 금액"]["number"], 50000)
        self.assertEqual(expense_payload["사용처"]["rich_text"][0]["text"]["content"], "라멘")
        budget_payload = next(payload for kind, parent, payload, _row_id in self.client.created
                              if kind == "row" and parent == mapping["budget"]
                              and payload.get("카테고리", {}).get("select", {}).get("name") == "식비")
        self.assertIn("카테고리", budget_payload)

    def test_unconfirmed_or_incomplete_shopping_rows_never_write(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        before = len(self.client.created)
        with self.assertRaisesRegex(ni.InstallerError, "사용자 확인"):
            ni.save_shopping_rows(self.manifest, self.client, self.path,
                                  result["trip_id"], [{"품목": "유자 킷캣"}], confirmed=False)
        self.assertEqual(len(self.client.created), before)
        with self.assertRaisesRegex(ni.InstallerError, "확인이 필요한"):
            ni.save_shopping_rows(self.manifest, self.client, self.path,
                                  result["trip_id"], [{"품목": "유자 킷캣", "수량": 1}], confirmed=True)
        self.assertEqual(len(self.client.created), before)

    def test_repeating_confirmed_shopping_save_updates_same_row(self):
        self.install()
        result = self.scaffold(traveler_count=1)
        row = {"품목": "유자 킷캣", "담당 여행자": "동행자 1", "구매 장소": "돈키호테",
               "수량": 1, "예상 금액": 500, "통화": "JPY"}
        first = ni.save_shopping_rows(self.manifest, self.client, self.path,
                                      result["trip_id"], [row], confirmed=True)
        row["수량"] = 2
        second = ni.save_shopping_rows(self.manifest, self.client, self.path,
                                       result["trip_id"], [row], confirmed=True)
        self.assertEqual(first["created"], second["updated"])
        self.assertEqual(second["created"], [])
        self.assertEqual(len(second["rows"]), 1)

    def test_old_install_is_migrated_with_idempotency_property(self):
        self.install()
        mapping = ni.read_json(self.path)
        mapping.pop("idempotency_schema", None)
        for kind, _parent, properties, _row_id in self.client.created:
            if kind == "database":
                properties.pop(ni.IDEMPOTENCY_PROPERTY, None)
        ni.write_json(self.path, mapping)
        self.install()
        self.assertEqual(ni.read_json(self.path)["idempotency_schema"], 1)
        for kind, _parent, properties, _row_id in self.client.created:
            if kind == "database":
                self.assertIn(ni.IDEMPOTENCY_PROPERTY, properties)

    def test_wrong_parent_or_template_never_writes(self):
        self.install()
        before = self.path.read_bytes()
        calls = self.client.calls
        with self.assertRaisesRegex(ni.InstallerError, "상위 페이지"):
            ni.install(self.manifest, self.client, "22222222222222222222222222222222", self.path)
        self.manifest["template_key"] = "another-template"
        with self.assertRaisesRegex(ni.InstallerError, "다른 템플릿"):
            self.install()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, calls)

    def test_changed_manifest_requires_explicit_migration(self):
        self.install()
        self.manifest["display_name"] = "new version"
        with self.assertRaisesRegex(ni.InstallerError, "설치 당시"):
            self.install()

    def test_install_resumes_each_content_page_without_duplicates(self):
        # Let multiple page bodies persist, then fail on a later content batch.
        self.client.fail_on_append_after = 3
        with self.assertRaises(ni.InstallerError):
            self.install()
        created_count = len(self.client.created)
        first_blocks = copy.deepcopy(self.client.blocks)
        self.assertGreater(len(first_blocks), 0)
        self.assertNotIn("pending_operation", ni.read_json(self.path))
        self.client.fail_on_append_after = None
        self.install()
        self.assertEqual(len(self.client.created), created_count)
        self.assertEqual(self.client.blocks[:len(first_blocks)], first_blocks)
        page_ids = [item[0] for item in self.client.blocks]
        self.assertEqual(len(page_ids), len(set(page_ids)))

    def test_scaffold_dates_currency_and_reuse(self):
        self.install()
        result = self.scaffold()
        self.assertEqual((len(result["itinerary"]), len(result["travelers"])), (4, 4))
        self.assertEqual((len(result["tasks"]), len(result["packing"])), (15, 8))
        dates = [payload["날짜"]["date"]["start"] for kind, _, payload, row_id in self.client.created
                 if row_id in result["itinerary"]]
        self.assertEqual(dates, ["2026-12-12", "2026-12-13", "2026-12-14", "2026-12-15"])
        core_rows = self.core_row_ids()
        self.assertEqual(self.scaffold(), result)
        self.assertEqual(self.core_row_ids(), core_rows)

    def test_scaffold_scopes_summary_and_child_rows_to_trip_key(self):
        self.install()
        result = self.scaffold(traveler_count=2)
        trip_rows = [payload for kind, parent, payload, row_id in self.client.created
                     if kind == "row" and parent == ni.read_json(self.path)["databases"]["trip"]
                     and row_id == result["trip_id"]]
        self.assertEqual(len(trip_rows), 1)
        trip_key = next(
            "".join(item.get("text", {}).get("content", "") for item in trip_rows[0]["여행 키"]["rich_text"])
            for _ in [0]
        )
        self.assertTrue(trip_key.startswith("souvenir:"))
        child_databases = {
            ni.read_json(self.path)["databases"][key]
            for key in ("itinerary", "traveler", "wallet", "task", "packing")
        }
        for kind, parent, payload, row_id in self.client.created:
            if kind != "row" or parent not in child_databases:
                continue
            if "여행 키" in payload:
                child_key = "".join(item.get("text", {}).get("content", "") for item in payload["여행 키"]["rich_text"])
                self.assertEqual(child_key, result["trip_id"])

    def test_scaffold_links_child_rows_when_template_has_trip_relation(self):
        self.install()
        mapping = ni.read_json(self.path)
        traveler_db = mapping["databases"]["traveler"]
        original_get_database = self.client.get_database

        def get_database(database_id):
            payload = original_get_database(database_id)
            if database_id == traveler_db:
                payload.setdefault("properties", {})["여행"] = {"type": "relation"}
            return payload

        self.client.get_database = get_database
        result = self.scaffold(traveler_count=2)
        traveler_rows = [payload for kind, parent, payload, row_id in self.client.created
                         if kind == "row" and parent == traveler_db and row_id in result["travelers"]]
        self.assertEqual(len(traveler_rows), 2)
        for payload in traveler_rows:
            self.assertEqual(payload["여행"]["relation"], [{"id": result["trip_id"]}])

    def test_scaffold_resumes_partial_trip(self):
        self.install()
        itinerary_db = ni.read_json(self.path)["databases"]["itinerary"]
        self.client.fail_on_database_id = itinerary_db
        self.client.fail_on_database_page_after = 3
        with self.assertRaises(ni.InstallerError):
            self.scaffold()
        partial = next(iter(ni.read_json(self.path)["scaffolded_trips"].values()))
        self.assertEqual(partial["status"], "in_progress")
        self.assertEqual(len(partial["itinerary"]), 2)
        self.client.fail_on_database_id = None
        self.client.fail_on_database_page_after = None
        result = self.scaffold()
        self.assertEqual(result["trip_id"], partial["trip_id"])
        self.assertEqual(result["itinerary"][:2], partial["itinerary"])
        self.assertEqual(len(result["itinerary"]), 4)
        self.assertEqual(result["status"], "complete")
        trip_db = ni.read_json(self.path)["databases"]["trip"]
        self.assertEqual(sum(parent == trip_db for _, parent, _, _ in self.client.created), 1)

    def test_uncertain_write_stops_automatic_replay(self):
        self.install()
        itinerary_db = ni.read_json(self.path)["databases"]["itinerary"]
        self.client.fail_on_database_id = itinerary_db
        self.client.fail_on_database_page_after = 1
        self.client.error = ni.UncertainWriteError("timeout after saving")
        with self.assertRaises(ni.UncertainWriteError):
            self.scaffold()
        self.assertIn("pending_operation", ni.read_json(self.path))
        calls = self.client.calls
        with self.assertRaisesRegex(ni.InstallerError, "저장 결과 확인"):
            self.scaffold()
        self.assertEqual(self.client.calls, calls)

    def test_pending_operation_contains_owner_and_idempotency_key(self):
        self.install()
        itinerary_db = ni.read_json(self.path)["databases"]["itinerary"]
        self.client.fail_on_database_id = itinerary_db
        self.client.fail_on_database_page_after = 1
        self.client.error = ni.UncertainWriteError("timeout after saving")
        with self.assertRaises(ni.UncertainWriteError):
            self.scaffold(traveler_count=0)
        pending = ni.read_json(self.path)["pending_operation"]
        self.assertIsInstance(pending, dict)
        self.assertTrue(pending["operation_id"])
        self.assertTrue(pending["idempotency_key"])

    def test_active_trip_lease_blocks_second_writer(self):
        self.install()
        mapping = ni.read_json(self.path)
        trip_key = ni.preview_trip_key(
            self.manifest, "후쿠오카", "2026-12-12", "2026-12-15", None, 4, "JPY"
        )
        now = ni.dt.datetime(2026, 12, 1, tzinfo=ni.dt.timezone.utc)
        lease = ni.claim_operation(mapping, self.path, self.client, trip_key, "수정", "세션 A", "첫날 일정", now=now)
        with self.assertRaisesRegex(ni.ConflictError, "이미 반영 중"):
            ni.claim_operation(ni.read_json(self.path), self.path, self.client,
                               trip_key, "수정", "세션 B", "예산", now=now)
        ni.release_operation(mapping, self.path, self.client, lease)
        self.assertFalse(ni._active_lease_rows(
            self.client.query_database(mapping["operation_log_database_id"]), now
        ))

    def test_expired_trip_lease_is_audited_and_reacquired(self):
        self.install()
        mapping = ni.read_json(self.path)
        trip_key = ni.preview_trip_key(
            self.manifest, "후쿠오카", "2026-12-12", "2026-12-15", None, 4, "JPY"
        )
        first_now = ni.dt.datetime(2026, 12, 1, tzinfo=ni.dt.timezone.utc)
        first = ni.claim_operation(mapping, self.path, self.client, trip_key, "수정", "세션 A", now=first_now)
        later = first_now + ni.dt.timedelta(minutes=11)
        second = ni.claim_operation(ni.read_json(self.path), self.path, self.client,
                                    trip_key, "수정", "세션 B", now=later)
        old = next(properties for kind, _parent, properties, row_id in self.client.created
                   if row_id == first["page_id"])
        self.assertEqual(old["상태"]["select"]["name"], ni.LEASE_REVIEW)
        ni.release_operation(ni.read_json(self.path), self.path, self.client, second)

    def test_same_mapping_is_locked_during_write(self):
        with ni.operation_locks(self.path, PARENT, "test"):
            with self.assertRaisesRegex(ni.InstallerError, "쓰기 작업이 진행 중"):
                ni.install(self.manifest, self.client, PARENT, self.path)

    def test_same_workspace_is_locked_across_mappings(self):
        other = Path(self.tmp.name) / "other.json"
        with ni.operation_locks(self.path, PARENT, "test"):
            with self.assertRaisesRegex(ni.InstallerError, "쓰기 작업이 진행 중"):
                ni.install(self.manifest, self.client, PARENT, other)

    def test_duplicate_row_key_is_a_conflict(self):
        self.install()
        key = ni.stable_key("duplicate")
        database_id = ni.read_json(self.path)["databases"]["trip"]
        self.client.created.extend([
            ("row", database_id, {ni.IDEMPOTENCY_PROPERTY: {"rich_text": [{"text": {"content": key}}]}}, "duplicate-1"),
            ("row", database_id, {ni.IDEMPOTENCY_PROPERTY: {"rich_text": [{"text": {"content": key}}]}}, "duplicate-2"),
        ])
        with self.assertRaises(ni.ConflictError):
            ni.find_existing_row(self.client, database_id, key)

    def test_scaffold_reuses_rows_by_key_from_another_mapping(self):
        self.install()
        first = self.scaffold()
        original_core_rows = self.core_row_ids()
        second_path = Path(self.tmp.name) / "second.json"
        mapping = ni.read_json(self.path)
        mapping["scaffolded_trips"] = {}
        ni.write_json(second_path, mapping)
        second = ni.scaffold_trip(
            self.manifest, self.client, second_path, "후쿠오카", "2026-12-12", "2026-12-15", None, 4, "JPY"
        )
        self.assertEqual(second["trip_id"], first["trip_id"])
        self.assertEqual(second["itinerary"], first["itinerary"])
        self.assertEqual(self.core_row_ids(), original_core_rows)

    def test_duration_only_has_no_invented_dates(self):
        self.install()
        result = self.scaffold(start_date=None, end_date=None, duration_days=4, traveler_count=0)
        for _, _, payload, row_id in self.client.created:
            if row_id in result["itinerary"]:
                self.assertNotIn("날짜", payload)
        self.assertEqual(result["travelers"], [])

    def test_start_plus_duration_derives_end_and_same_identity(self):
        self.install()
        result = self.scaffold(end_date=None, duration_days=4)
        trip = next(payload for _, _, payload, row_id in self.client.created if row_id == result["trip_id"])
        self.assertEqual(trip["종료일"]["date"]["start"], "2026-12-15")
        core_rows = self.core_row_ids()
        self.assertEqual(self.scaffold(), result)
        self.assertEqual(self.core_row_ids(), core_rows)

    def test_different_trip_names_do_not_collide(self):
        self.install()
        self.assertNotEqual(self.scaffold(trip_name="봄 여행")["trip_id"],
                            self.scaffold(trip_name="가을 여행")["trip_id"])

    def test_invalid_trip_input_never_writes(self):
        self.install()
        calls = self.client.calls
        for options in ({"destination": "   "}, {"duration_days": 5}, {"traveler_count": -1},
                        {"start_date": "20261212"}, {"currency": "XXX"},
                        {"end_date": "2026-12-10"}, {"duration_days": 1.5}):
            with self.subTest(options=options), self.assertRaises(ni.InstallerError):
                self.scaffold(**options)
        self.assertEqual(self.client.calls, calls)

    def test_invalid_seed_schema_is_detected_before_any_write(self):
        self.manifest["seed_rows"]["task"][0]["values"]["missing property"] = "bad"
        with self.assertRaises(ni.InstallerError):
            self.install()
        self.assertEqual(self.client.calls, 0)

    def test_atomic_save_preserves_previous_checkpoint_on_failure(self):
        ni.write_json(self.path, {"saved": 1})
        with patch.object(ni.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(ni.InstallerError):
                ni.write_json(self.path, {"saved": 2})
        self.assertEqual(ni.read_json(self.path), {"saved": 1})
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_malformed_manifest_reports_errors_not_tracebacks(self):
        for field, value in (("pages", "bad"), ("databases", None), ("seed_rows", [])):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            with self.subTest(field=field):
                self.assertTrue(ni.validate_manifest(manifest))
        for bad in ({"key": []}, {"key": None}, {"key": "dashboard", "parent": []}):
            manifest = copy.deepcopy(self.manifest)
            manifest["pages"][0] = bad
            self.assertTrue(ni.validate_manifest(manifest))

    def test_parent_cycle_is_rejected(self):
        self.manifest["pages"][0]["parent"] = "dashboard"
        self.assertTrue(any("순환" in error for error in ni.validate_manifest(self.manifest)))

    def test_broken_page_reference_is_rejected_before_install(self):
        self.manifest["pages"][0]["sections"].append({"kind": "links", "title": "broken", "targets": ["missing"]})
        with self.assertRaises(ni.InstallerError):
            self.install()
        self.assertEqual(self.client.calls, 0)

    def test_plan_reports_only_properties_actually_created(self):
        report = ni.plan(self.manifest)
        for database, planned in zip(self.manifest["databases"], report["databases"]):
            self.assertEqual(planned["properties"], len(ni.database_properties(database)))

    def test_old_completed_mapping_is_reused_without_new_trip(self):
        self.install()
        result = self.scaffold()
        mapping = ni.read_json(self.path)
        legacy = dict(result)
        legacy.pop("status")
        mapping["scaffolded_trips"] = {"후쿠오카|2026-12-12|2026-12-15|4|4|JPY": legacy}
        mapping.pop("manifest_digest")
        ni.write_json(self.path, mapping)
        core_rows = self.core_row_ids()
        self.assertEqual(self.scaffold(), legacy)
        self.assertEqual(self.core_row_ids(), core_rows)

    def test_missing_scaffold_property_is_detected_before_trip_created(self):
        self.install()
        itinerary = ni.database_by_key(self.manifest, "itinerary")
        itinerary["properties"] = [p for p in itinerary["properties"] if p["name"] != "일차"]
        mapping = ni.read_json(self.path)
        mapping.pop("manifest_digest")  # Simulate a legacy installation without a digest.
        ni.write_json(self.path, mapping)
        core_rows = self.core_row_ids()
        with self.assertRaisesRegex(ni.InstallerError, "일차"):
            self.scaffold()
        self.assertEqual(self.core_row_ids(), core_rows)

    def test_text_chunks_preserve_long_korean_text(self):
        value = "여행 정보🧳" * 500
        chunks = ni.rich_text(value)
        self.assertTrue(all(len(chunk["text"]["content"]) <= 2000 for chunk in chunks))
        self.assertEqual("".join(chunk["text"]["content"] for chunk in chunks), value)
        self.assertEqual(ni.rich_text(""), [])

    def test_null_and_checkbox_values_are_not_coerced(self):
        self.assertEqual(ni.property_value({"type": "rich_text"}, None), {"rich_text": []})
        self.assertEqual(ni.property_value({"type": "select"}, None), {"select": None})
        with self.assertRaises(ni.InstallerError):
            ni.property_value({"type": "checkbox", "name": "완료"}, "false")
        with self.assertRaises(ni.InstallerError):
            ni.property_value({"type": "number", "name": "금액"}, float("nan"))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ni.NotionClient("test-token")

    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return response

    def http_error(self, code, headers=None, extra=None):
        body = json.dumps({"message": "test failure", "additional_data": extra or {}}).encode()
        return urllib.error.HTTPError("https://api.notion.com", code, "failure", headers or {}, io.BytesIO(body))

    def test_rate_limits_honor_retry_after_for_writes(self):
        for status in (429, 529):
            with self.subTest(status=status), patch.object(ni.urllib.request, "urlopen", side_effect=[
                self.http_error(status, {"Retry-After": "2"}), self.response({"id": "saved"})
            ]) as request, patch.object(ni.time, "sleep") as sleep:
                self.assertEqual(self.client.request("POST", "/pages", {}), {"id": "saved"})
                self.assertEqual(request.call_count, 2)
                self.assertGreaterEqual(sleep.call_args.args[0], 2)

    def test_permanent_errors_and_long_wait_are_not_retried(self):
        cases = [(400, {}, {}), (401, {}, {}), (429, {"Retry-After": "120"}, {}),
                 (429, {}, {"rate_limit_reason": "public_api_request_blocked"})]
        for code, headers, extra in cases:
            self.client._next_request_at = 0.0
            with self.subTest(code=code, extra=extra), patch.object(ni.urllib.request, "urlopen",
                side_effect=self.http_error(code, headers, extra)) as request, patch.object(ni.time, "sleep") as sleep:
                with self.assertRaises(ni.InstallerError):
                    self.client.request("POST", "/pages", {})
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_server_error_write_is_uncertain_and_not_retried(self):
        with patch.object(ni.urllib.request, "urlopen", side_effect=self.http_error(503)) as request:
            with self.assertRaises(ni.UncertainWriteError):
                self.client.request("POST", "/pages", {})
        self.assertEqual(request.call_count, 1)

    def test_read_server_error_can_retry(self):
        with patch.object(ni.urllib.request, "urlopen", side_effect=[self.http_error(503), self.response({"id": "ok"})]), \
                patch.object(ni.time, "sleep"):
            self.assertEqual(self.client.get_page("some-id"), {"id": "ok"})

    def test_retry_count_is_bounded(self):
        with patch.object(ni.urllib.request, "urlopen", side_effect=[self.http_error(429) for _ in range(4)]) as request, \
                patch.object(ni.time, "sleep"):
            with self.assertRaises(ni.InstallerError):
                self.client.request("POST", "/pages", {})
        self.assertEqual(request.call_count, 4)

    def test_timeout_or_bad_response_is_uncertain(self):
        for result in (TimeoutError(), self.response(["invalid"]), self.response(None)):
            with self.subTest(result=result), patch.object(ni.urllib.request, "urlopen", side_effect=[result]):
                with self.assertRaises(ni.UncertainWriteError):
                    self.client.request("POST", "/pages", {})

    def test_update_aborts_when_snapshot_is_stale(self):
        current = self.response({"id": "page", "last_edited_time": "v2"})
        with patch.object(ni.urllib.request, "urlopen", return_value=current) as request:
            with self.assertRaises(ni.ConflictError):
                self.client.update_page_properties("page", {"properties": {}}, expected_last_edited_time="v1")
        self.assertEqual(request.call_count, 1)

    def test_update_rereads_after_patch(self):
        before = self.response({"id": "page", "last_edited_time": "v1"})
        patched = self.response({"id": "page"})
        after = self.response({"id": "page", "last_edited_time": "v2"})
        with patch.object(ni.urllib.request, "urlopen", side_effect=[before, patched, after]) as request:
            result = self.client.update_page_properties("page", {"Name": {"title": []}}, expected_last_edited_time="v1")
        self.assertEqual(result["last_edited_time"], "v2")
        self.assertEqual(request.call_count, 3)

    def test_append_blocks_chunks_at_100(self):
        blocks = [ni.text_block(str(index)) for index in range(205)]
        with patch.object(self.client, "request", return_value={}) as request:
            self.client.append_blocks("page", blocks)
        batches = [call.args[2]["children"] for call in request.call_args_list]
        self.assertEqual([len(batch) for batch in batches], [100, 100, 5])
        self.assertEqual([block for batch in batches for block in batch], blocks)


if __name__ == "__main__":
    unittest.main()
