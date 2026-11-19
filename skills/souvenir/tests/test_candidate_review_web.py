"""Tests for the local candidate-review board contract and HTTP boundary."""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "candidate_review_web.py"
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
