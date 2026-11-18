"""Opt-in live smoke test for Google Places (never runs in normal CI)."""
import os
import unittest
import json
import subprocess
import sys
import time
from unittest.mock import patch

from pathlib import Path
import importlib.util


SCRIPT = Path(__file__).parents[1] / "scripts" / "trip_engine.py"
SPEC = importlib.util.spec_from_file_location("souvenir_google_live_engine", SCRIPT)
engine = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(engine)


LIVE = os.environ.get("SOUVENIR_LIVE_GOOGLE_TEST", "").strip().lower() in {"1", "true", "yes", "on"}
API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()


@unittest.skipUnless(LIVE and API_KEY, "opt-in: set SOUVENIR_LIVE_GOOGLE_TEST=1 and GOOGLE_PLACES_API_KEY")
class GooglePlacesLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = []
        packet = engine.prepare_google_place_candidate_page(
            "후쿠오카 카페", API_KEY, "후쿠오카", summary_only=True
        )
        cls.pages.append(packet)
        while packet["다음 후보 있음"] and len(cls.pages) < 6:
            state = packet["후속 검색 상태"]
            packet = engine.prepare_google_place_candidate_page(
                "후쿠오카 카페", API_KEY, "후쿠오카", summary_only=True,
                page_token=state["page_token"], page_index=state["page_index"],
                session_state=state,
            )
            cls.pages.append(packet)

    def test_text_search_returns_safe_candidate_packet(self):
        packet = self.pages[0]
        self.assertEqual(packet["검색 제공자"], "Google Places API (New)")
        self.assertGreater(len(packet["후보"]), 0)
        self.assertLessEqual(len(packet["후보"]), 10)
        self.assertNotIn(API_KEY, str(packet))
        for row in packet["후보"]:
            self.assertTrue(row.get("장소 ID"))
            self.assertTrue(row.get("지도 URL", "").startswith("https://"))
            self.assertFalse(row["저장 가능"])
        self.assertEqual(packet["Google Maps 표시 고지"], "Google Maps")

    def test_next_pages_have_continuous_ranks_and_correct_total(self):
        ranks = [row["추천 순위"] for page in self.pages for row in page["후보"]]
        self.assertEqual(ranks, list(range(1, len(ranks) + 1)))
        ids = [row["장소 ID"] for page in self.pages for row in page["후보"]]
        self.assertEqual(len(ids), len(set(ids)))
        if not self.pages[-1]["다음 후보 있음"]:
            self.assertEqual(self.pages[-1]["후보 수"]["전체"], len(ranks))

    def test_live_candidate_approval_only_retains_safe_fields(self):
        packet = self.pages[0]
        self.assertEqual(engine.confirm_place_rows(packet), [])
        self.assertEqual(engine.confirm_place_rows(packet, ["1"]), [])
        rows = engine.confirm_place_rows(packet, ["1"], {"1": "API 연결 검증용 카페"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["장소"], "API 연결 검증용 카페")
        self.assertTrue(rows[0]["저장 가능"])
        for field in ("지역", "평점", "리뷰 수", "영업시간·휴무", "사진 URL"):
            self.assertNotIn(field, rows[0])
        self.assertNotIn(API_KEY, str(rows))

    def test_open_now_filter_returns_open_places(self):
        packet = engine.prepare_google_place_candidate_page(
            "후쿠오카 카페", API_KEY, "후쿠오카", included_type="cafe", open_now=True
        )
        self.assertGreater(len(packet["후보"]), 0)
        for row in packet["후보"]:
            self.assertTrue(str(row.get("영업시간·휴무", "")).startswith("영업 중"))
        self.assertTrue(packet["필터"]["open_now"])
        self.assertNotIn(API_KEY, str(packet))

    def test_distance_language_region_and_small_page(self):
        packet = engine.prepare_google_place_candidate_page(
            "cafes in Fukuoka", API_KEY, "Fukuoka", page_size=3,
            rank_preference="DISTANCE", included_type="cafe",
            language_code="en", region_code="JP", summary_only=True,
            location_bias={"circle": {"center": {"latitude": 33.5902, "longitude": 130.4017}, "radius": 1500}},
        )
        self.assertGreater(len(packet["후보"]), 0)
        self.assertLessEqual(len(packet["후보"]), 3)
        self.assertEqual(packet["정렬"], "거리순")

    def test_wrong_destination_is_not_approved(self):
        packet = engine.prepare_google_place_candidate_page(
            "Tokyo Tower Tokyo Japan", API_KEY, "후쿠오카", summary_only=True, page_size=1
        )
        self.assertGreater(len(packet["후보"]), 0)
        self.assertEqual(packet["후보"][0]["여행지 확인"], "불일치")
        self.assertEqual(engine.confirm_place_rows(packet, ["1"], {"1": "잘못된 목적지"}), [])

    def test_changed_session_and_expired_token_fail_before_network(self):
        state = dict(self.pages[0]["후속 검색 상태"])
        if not state["page_token"]:
            self.skipTest("provider did not return a next page")
        for changes in ({"query": "다른 검색"}, {"issued_at": time.time() - 601}):
            with self.subTest(changes=list(changes)):
                altered = dict(state, **changes)
                with patch.object(engine, "google_places_text_search") as search:
                    with self.assertRaises(ValueError):
                        engine.prepare_google_place_candidate_page(
                            "후쿠오카 카페", API_KEY, "후쿠오카", summary_only=True,
                            page_token=state["page_token"], page_index=state["page_index"],
                            session_state=altered,
                        )
                    search.assert_not_called()

    def test_destination_and_page_index_are_bound_to_session(self):
        state = self.pages[0]["후속 검색 상태"]
        if not state["page_token"]:
            self.skipTest("provider did not return a next page")
        for destination, index in (("도쿄", state["page_index"]), ("후쿠오카", 7)):
            with self.subTest(destination=destination, index=index):
                with patch.object(engine, "google_places_text_search") as search:
                    with self.assertRaises(ValueError):
                        engine.prepare_google_place_candidate_page(
                            "후쿠오카 카페", API_KEY, destination, summary_only=True,
                            page_token=state["page_token"], page_index=index, session_state=state,
                        )
                    search.assert_not_called()

    def test_invalid_key_returns_classified_error_without_secret(self):
        invalid_key = "souvenir-intentionally-invalid-test-key"
        with self.assertRaises(engine.GooglePlacesError) as raised:
            engine.google_places_text_search("후쿠오카 카페", invalid_key, summary_only=True)
        self.assertEqual(raised.exception.error_code, "GOOGLE_AUTH")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn(invalid_key, str(raised.exception))

    def test_invalid_type_is_request_error(self):
        with self.assertRaises(engine.GooglePlacesError) as raised:
            engine.google_places_text_search(
                "후쿠오카 카페", API_KEY, included_type="souvenir_nonexistent_type", summary_only=True
            )
        self.assertEqual(raised.exception.error_code, "GOOGLE_REQUEST_FAILED")
        self.assertNotIn(API_KEY, str(raised.exception))

    def test_packaged_cli_and_keyless_fallback(self):
        runner = SCRIPT.with_name("run_souvenir.py")
        for use_key in (True, False):
            env = dict(os.environ)
            if not use_key:
                env.pop("GOOGLE_PLACES_API_KEY", None)
            proc = subprocess.run(
                [sys.executable, str(runner), "places", "--destination", "후쿠오카",
                 "--google-query", "후쿠오카 카페", "--google-summary-only"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, "CLI failed; provider details withheld")
            self.assertNotIn(API_KEY, proc.stdout + proc.stderr)
            packet = json.loads(proc.stdout)
            self.assertEqual(packet["검색 제공자"], "Google Places API (New)" if use_key else "호스트 웹 검색")


if __name__ == "__main__":
    unittest.main()
