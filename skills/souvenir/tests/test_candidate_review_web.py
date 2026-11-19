"""Tests for the local candidate-review board contract and HTTP boundary."""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "candidate_review_web.py"
WEB_DIR = Path(__file__).parents[1] / "web"
SPEC = importlib.util.spec_from_file_location("candidate_review_web", SCRIPT)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
sys.modules["candidate_review_web"] = review
SPEC.loader.exec_module(review)


def packet():
    return {
        "trip": {
            "destination": "오사카",
            "start_date": "2026-12-19",
            "end_date": "2026-12-23",
            "travelers": "성인 2명",
        },
        "sections": {
            "flights": {
                "provider": "kiwi-mcp",
                "search": {"flyFrom": "ICN", "flyTo": "KIX", "api_key": "must-not-leak"},
                "candidates": [{
                    "itinerary": {
                        "id": "flight-1",
                        "price": 320000,
                        "currency": "KRW",
                        "outbound": {"from": "ICN", "to": "KIX", "departureTime": "2026-12-19T18:00:00", "arrivalTime": "2026-12-19T20:00:00"},
                        "inbound": {"from": "KIX", "to": "ICN", "departureTime": "2026-12-23T12:00:00", "arrivalTime": "2026-12-23T14:00:00"},
                        "bookingUrl": "https://www.kiwi.com/flight-1",
                    },
                }],
            },
            "hotels": {
                "provider": "pilot-mcp",
                "budget_per_night_krw": 100000,
                "room_filters": {"bed_type": "트윈", "refundable_only": True},
                "candidates": [{
                    "identity": "hotel-1",
                    "hotel": {"id": "hotel-1", "name": "난바 호텔", "address": "오사카 난바", "price": 350000, "currency": "KRW"},
                    "price_per_night_estimate": 87500,
                    "room_rate_verification": {"matching_offers": [{"roomName": "Twin Room", "refundable": True}]},
                }],
            },
            "places": {
                "provider": "Google Places API (New)",
                "candidates": [{"place_id": "place-1", "name": "도톤보리", "formattedAddress": "오사카", "rating": 4.4, "googleMapsUri": "https://maps.google.com/?q=dotonbori"}],
            },
        },
    }


class CandidateReviewTests(unittest.TestCase):
    def test_ui_replaces_raw_load_error_with_session_refresh_action(self):
        html = (WEB_DIR / "candidate-review.html").read_text(encoding="utf-8")
        javascript = (WEB_DIR / "candidate-review.js").read_text(encoding="utf-8")
        self.assertIn('id="session-refresh"', html)
        self.assertIn('id="empty-title"', html)
        self.assertIn("검색 세션이 만료됐어요.", javascript)
        self.assertIn('fetch("/api/health"', javascript)
        self.assertIn("검색 공급자가 응답하지 않아 자동으로 다시 시도하고 있어요", javascript)
        self.assertIn("기존 결과를 유지했어요", javascript)
        self.assertNotIn('error.message || "결과를 불러오지 못했어요."', javascript)

    def test_normalize_combined_packet_has_three_sections_and_public_context(self):
        result = review.normalize_packet(packet())
        self.assertEqual(set(result["sections"]), {"flights", "hotels", "places"})
        self.assertEqual(result["trip"]["destination"], "오사카")
        self.assertEqual(result["sections"]["hotels"]["candidates"][0]["price_label"], "87,500원")
        self.assertNotIn("api_key", json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["notion_written"])
        self.assertFalse(result["booking_completed"])

    def test_single_result_infers_provider_kind(self):
        raw = {"provider": "pilot-mcp", "candidates": [{"hotel": {"id": "h1", "name": "A", "price": 100000, "currency": "KRW"}, "price_per_night_estimate": 100000}]}
        result = review.normalize_packet(raw)
        self.assertEqual(list(result["sections"]), ["hotels"])

    def test_no_matches_still_opens_an_empty_review_section(self):
        result = review.normalize_packet({"provider": "kiwi-mcp", "status": "no_matches", "candidates": []})
        self.assertEqual(result["sections"]["flights"]["candidate_count"], 0)
        self.assertEqual(result["sections"]["flights"]["status"], "no_matches")

    def test_selection_rejects_unknown_ids_and_writes_safe_review_state(self):
        result = review.normalize_packet(packet())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "selection.json"
            saved = review.save_selection(output, result, [
                {"section": "hotels", "id": "hotel-hotel-1-1"},
                {"section": "hotels", "id": "missing"},
            ])
            self.assertEqual(len(saved["selections"]), 1)
            self.assertEqual(saved["selection_status"], "선택·예약 전")
            self.assertFalse(saved["notion_written"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["selections"][0]["title"], "난바 호텔")

    def test_flight_requery_replaces_one_section_without_rebuilding_packet(self):
        provider_result = {
            "currency": "KRW",
            "passengers": {"adults": 2, "children": 0, "infants": 0},
            "itineraries": [{
                "id": "new-flight",
                "price": 280000,
                "outbound": {"from": "GMP", "to": "KIX", "departureTime": "2026-12-20T18:00:00", "arrivalTime": "2026-12-20T20:00:00", "stops": 0},
                "inbound": {"from": "KIX", "to": "GMP", "departureTime": "2026-12-23T12:00:00", "arrivalTime": "2026-12-23T14:00:00", "stops": 0},
                "bookingUrl": "https://www.kiwi.com/new-flight",
            }],
        }
        fake_client = Mock()
        fake_client.search.return_value = provider_result
        with patch.object(review, "KiwiClient", return_value=fake_client):
            updated_source = review.requery_source_packet(packet(), "flights", {
                "flyFrom": "GMP",
                "flyTo": "KIX",
                "departureDate": "2026-12-20",
                "returnDate": "2026-12-23",
                "adults": 2,
                "price_to": 500000,
            })
        self.assertEqual(updated_source["sections"]["flights"]["search"]["flyFrom"], "GMP")
        self.assertEqual(updated_source["sections"]["hotels"]["candidates"][0]["hotel"]["name"], "난바 호텔")
        refreshed = review.normalize_packet(updated_source)
        self.assertEqual(refreshed["sections"]["flights"]["candidates"][0]["title"], "GMP → KIX")
        self.assertEqual(refreshed["sections"]["places"]["candidate_count"], 1)

    def test_hotel_requery_reapplies_budget_and_room_filters(self):
        source = {
            "trip": {"destination": "오사카"},
            "sections": {"hotels": {"provider": "pilot-mcp", "candidates": []}},
        }
        provider_result = {"hotels": [{"id": "hotel-new", "name": "새 난바 호텔", "address": "난바", "price": 360000, "currency": "KRW"}], "pages": []}
        fake_client = Mock()
        fake_client.search.return_value = provider_result
        fake_client.get_room_rates_for_hotel.return_value = {
            "offers": [{"roomName": "Twin Room", "refundable": True, "price": 360000, "currency": "KRW"}],
        }
        with patch.object(review, "PilotHotelClient", return_value=fake_client):
            updated_source = review.requery_source_packet(source, "hotels", {
                "location": "난바",
                "check_in": "2026-12-19",
                "check_out": "2026-12-23",
                "guests": 2,
                "rooms": 1,
                "budget_per_night_krw": 100000,
                "room_filters": {"bed_type": "트윈", "refundable_only": True},
            })
        hotel = updated_source["sections"]["hotels"]["candidates"][0]
        self.assertEqual(hotel["hotel"]["name"], "새 난바 호텔")
        self.assertEqual(hotel["room_rate_verification"]["matching_offers"][0]["roomName"], "Twin Room")

    def test_hotel_provider_error_is_translated_before_http_response(self):
        source = {
            "trip": {"destination": "오사카"},
            "sections": {"hotels": {"provider": "pilot-mcp", "candidates": []}},
        }
        fake_client = Mock()
        fake_client.search.side_effect = review.HotelError("PILOT_HOTEL_SEARCH_ERROR")
        with patch.object(review, "PilotHotelClient", return_value=fake_client):
            with self.assertRaises(review.ReviewSearchError) as raised:
                review.requery_source_packet(source, "hotels", {
                    "location": "난바",
                    "check_in": "2026-12-19",
                    "check_out": "2026-12-23",
                    "guests": 2,
                    "rooms": 1,
                    "budget_per_night_krw": 100000,
                    "room_filters": {"bed_type": "트윈", "refundable_only": True},
                })
        self.assertEqual(raised.exception.code, "HOTEL_SEARCH_PROVIDER")
        self.assertIn("숙소 검색 공급자 응답", str(raised.exception))
        self.assertNotIn("PILOT_HOTEL_SEARCH_ERROR", str(raised.exception))
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.fallback, "host_web_search")

    def test_http_search_updates_results_on_same_server(self):
        provider_result = {
            "currency": "KRW",
            "passengers": {"adults": 2, "children": 0, "infants": 0},
            "itineraries": [{
                "id": "same-tab-flight",
                "price": 290000,
                "outbound": {"from": "GMP", "to": "KIX", "departureTime": "2026-12-20T18:00:00", "arrivalTime": "2026-12-20T20:00:00", "stops": 0},
                "inbound": {"from": "KIX", "to": "GMP", "departureTime": "2026-12-23T12:00:00", "arrivalTime": "2026-12-23T14:00:00", "stops": 0},
                "bookingUrl": "https://www.kiwi.com/same-tab-flight",
            }],
        }
        fake_client = Mock()
        fake_client.search.return_value = provider_result
        result = review.normalize_packet(packet())
        server = review.build_server(result, port=0, source_packet=packet())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            body = json.dumps({"section": "flights", "conditions": {
                "flyFrom": "GMP", "flyTo": "KIX", "departureDate": "2026-12-20", "returnDate": "2026-12-23", "adults": 2,
            }}).encode("utf-8")
            request = urllib.request.Request(base + "/api/search", data=body, headers={"Content-Type": "application/json"}, method="POST")
            with patch.object(review, "KiwiClient", return_value=fake_client):
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["status"], "results_updated")
            self.assertTrue(payload["same_site"])
            self.assertTrue(payload["selection_reset"])
            self.assertEqual(payload["results"]["sections"]["flights"]["candidates"][0]["location"], "GMP → KIX")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_hotel_error_returns_safe_retry_and_fallback_metadata(self):
        result = review.normalize_packet(packet())
        server = review.build_server(result, port=0, source_packet=packet())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fake_client = Mock()
        fake_client.search.side_effect = review.HotelError("PILOT_HOTEL_SEARCH_ERROR")
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            body = json.dumps({"section": "hotels", "conditions": {
                "location": "난바",
                "check_in": "2026-12-19",
                "check_out": "2026-12-23",
                "guests": 2,
                "rooms": 1,
                "budget_per_night_krw": 100000,
                "room_filters": {"bed_type": "트윈", "refundable_only": True},
            }}).encode("utf-8")
            request = urllib.request.Request(base + "/api/search", data=body, headers={"Content-Type": "application/json"}, method="POST")
            with patch.object(review, "PilotHotelClient", return_value=fake_client):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=3)
            payload = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(raised.exception.code, 502)
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["fallback"], "host_web_search")
        self.assertNotIn("PILOT_HOTEL_SEARCH_ERROR", payload["message"])

    def test_http_server_serves_ui_and_records_selection(self):
        result = review.normalize_packet(packet())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "selection.json"
            server = review.build_server(result, port=0, output_path=output)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base + "/", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("후보를 고르고", response.read().decode("utf-8"))
                with urllib.request.urlopen(base + "/api/results", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["sections"]["places"]["candidate_count"], 1)
                body = json.dumps({"selected": [{"section": "places", "id": "place-place-1-1"}]}).encode("utf-8")
                request = urllib.request.Request(base + "/api/selection", data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=2) as response:
                    submitted = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(submitted["status"], "selection_recorded")
                    self.assertEqual(submitted["selection_count"], 1)
                self.assertTrue(output.is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
