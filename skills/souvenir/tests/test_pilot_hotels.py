import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import pilot_hotels as pilot


def search_input():
    return {
        "search": {
            "location": "Osaka",
            "check_in": "2026-12-19",
            "check_out": "2026-12-23",
            "guests": 2,
            "rooms": 1,
            "currency": "KRW",
            "guest_nationality": "KR",
            "limit": 50,
        },
        "budget_per_night_krw": 100000,
    }


def hotel(name, price, hotel_id=None):
    return {
        "id": hotel_id or name.lower().replace(" ", "-"),
        "name": name,
        "price": price,
        "currency": "KRW",
        "address": "Osaka",
        "url": "https://pilotplans.com/hotels/" + (hotel_id or name.lower().replace(" ", "-")),
    }


class PilotHotelTests(unittest.TestCase):
    def test_budget_is_total_stay_precheck_and_candidates_are_not_capped(self):
        args, budget, nights, max_pages = pilot.validate_input(search_input())
        data = {"hotels": [hotel("Within", 400000), hotel("Over", 400001)]}
        result = pilot.normalize(data, args, budget, nights)
        self.assertEqual(nights, 4)
        self.assertEqual(result["budget_total_krw"], 400000)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["price_per_night_estimate"], 100000.0)
        self.assertEqual(result["rejected"][0]["reasons"], ["OVER_BUDGET"])
        self.assertEqual(result["budget_filter"], "local_krw_stay_total_precheck")
        self.assertEqual(result["price_semantics"], "provider_search_price_requires_room_rate_recheck")
        self.assertFalse(result["notion_written"])

    def test_search_walks_next_offset_even_when_provider_says_no_more_and_deduplicates(self):
        args, _, _, _ = pilot.validate_input(search_input())
        client = pilot.PilotHotelClient()
        properties = {key: {} for key in args}
        first = hotel("First", 300000, "same")
        second = hotel("Second", 250000, "second")
        with patch.object(
            client,
            "rpc",
            side_effect=[
                {"protocolVersion": "2025-03-26"},
                {},
                {"tools": [{"name": "search_hotels", "inputSchema": {"properties": properties}}]},
                {"structuredContent": {"hotels": [first], "returned": 1, "has_more": False, "next_offset": 50}},
                {"structuredContent": {"hotels": [copy.deepcopy(first), second], "returned": 2, "has_more": False, "next_offset": None}},
            ],
        ) as rpc:
            raw = client.search(args)
        self.assertEqual(len(raw["hotels"]), 3)
        self.assertEqual([page["offset"] for page in raw["pages"]], [0, 50])
        self.assertIn("PROVIDER_HAS_MORE_FALSE_WITH_NEXT_OFFSET", raw["pagination_warnings"])
        result = pilot.normalize(raw, args, 100000, 4, raw["pagination_warnings"], raw["pages"])
        self.assertEqual(result["returned_count"], 3)
        self.assertEqual(result["unique_count"], 2)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(rpc.call_args_list[-2].args[0], "tools/call")
        self.assertEqual(rpc.call_args_list[-2].args[1]["arguments"]["offset"], 0)
        self.assertEqual(rpc.call_args_list[-1].args[1]["arguments"]["offset"], 50)

    def test_remote_search_does_not_receive_local_budget(self):
        args, _, _, _ = pilot.validate_input(search_input())
        client = pilot.PilotHotelClient()
        properties = {key: {} for key in args}
        with patch.object(
            client,
            "rpc",
            side_effect=[
                {"protocolVersion": "2025-03-26"},
                {},
                {"tools": [{"name": "search_hotels", "inputSchema": {"properties": properties}}]},
                {"structuredContent": {"hotels": [], "returned": 0, "has_more": False, "next_offset": None}},
            ],
        ) as rpc:
            client.search(args)
        remote = rpc.call_args_list[-1].args[1]["arguments"]
        self.assertNotIn("budget_per_night_krw", remote)
        self.assertEqual(remote["currency"], "KRW")
        self.assertEqual(remote["offset"], 0)

    def test_validation_rejects_bad_dates_budget_filters_and_unknown_fields(self):
        for mutate in (
            lambda p: p["search"].update(check_out="2026-12-18"),
            lambda p: p.update(budget_per_night_krw=0),
            lambda p: p["search"].update(currency="JPY"),
            lambda p: p["search"].update(unknown=True),
            lambda p: p.update(max_pages=0),
        ):
            payload = search_input()
            mutate(payload)
            with self.assertRaises(pilot.HotelError):
                pilot.validate_input(payload)

    def test_invalid_currency_and_price_are_rejected(self):
        args, budget, nights, _ = pilot.validate_input(search_input())
        rows = [hotel("Bad currency", 100000, "bad-currency"), hotel("Bad price", "100000", "bad-price")]
        rows[0]["currency"] = "USD"
        result = pilot.normalize({"hotels": rows}, args, budget, nights)
        self.assertEqual(result["status"], "no_matches")
        self.assertEqual(result["rejected_count"], 2)
        self.assertIn("PRICE_CURRENCY_MISMATCH", result["rejected"][0]["reasons"])
        self.assertIn("INVALID_PRICE", result["rejected"][1]["reasons"])

    def test_json_sse_and_rpc_errors_are_sanitized(self):
        message = {"jsonrpc": "2.0", "id": 4, "result": {"tools": []}}
        for raw in (json.dumps(message), "event: message\r\ndata: " + json.dumps(message) + "\r\n\r\n"):
            self.assertEqual(pilot.decode_rpc(raw, 4), {"tools": []})
        with self.assertRaises(pilot.HotelError):
            pilot.decode_rpc("not-json", 4)
        with self.assertRaisesRegex(pilot.HotelError, "RPC_ERROR"):
            pilot.decode_rpc(json.dumps({"id": 4, "error": {"message": "private"}}), 4)

    def test_http_errors_are_sanitized(self):
        for status, code in ((401, "AUTH_REQUIRED"), (429, "RATE_LIMITED"), (500, "HTTP_ERROR")):
            def fail(*args, **kwargs):
                raise pilot.urllib.error.HTTPError(pilot.ENDPOINT, status, "private", {}, None)

            with self.subTest(status=status), self.assertRaisesRegex(pilot.HotelError, code):
                pilot.PilotHotelClient(opener=fail).rpc("initialize", {})


if __name__ == "__main__":
    unittest.main()
