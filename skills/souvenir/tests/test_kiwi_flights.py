import copy
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import kiwi_flights as kiwi


def search_input():
    return {"search": {"flyFrom": "ICN", "flyTo": "Tokyo", "departureDate": "19/12/2026",
                       "returnDate": "23/12/2026", "adults": 2, "price_to": 740000,
                       "dtime_from": 0, "dtime_to": 11, "ret_dtime_from": 18,
                       "ret_dtime_to": 23, "adults_hold_bags": [1, 1]},
            "minimum_checked_bag_kg": 15}


def response():
    return {"currency": "KRW", "passengers": {"adults": 2, "children": 0, "infants": 0},
            "itineraries": [{"id": "example", "price": 700000,
                              "bookingUrl": "https://kiwi.com/u/example",
                              "outbound": {"from": "ICN", "to": "NRT", "departureTime": "2026-12-19T09:00:00"},
                              "inbound": {"from": "NRT", "to": "ICN", "departureTime": "2026-12-23T19:00:00"},
                              "baggage": {"checkedBag": 2}}]}


class FlightTests(unittest.TestCase):
    def test_budget_is_not_divided_or_duplicated_and_weight_remains_unknown(self):
        args, kg = kiwi.validate_input(search_input())
        result = kiwi.normalize(response(), args, kg)
        row = result["candidates"][0]
        self.assertEqual(row["itinerary"]["price"], 700000)
        self.assertEqual(row["price_scope"], "all_passengers_return")
        self.assertIn("checked_bag_weight_kg", row["checks_needed"])
        self.assertEqual(row["verification"], "needs_check")
        self.assertFalse(result["notion_written"])

    def test_provider_returning_wrong_conditions_is_rejected(self):
        args, kg = kiwi.validate_input(search_input())
        for field in ("price", "date", "time", "url"):
            data = response()
            row = data["itineraries"][0]
            if field == "price": row["price"] = 750000
            if field == "date": row["outbound"]["departureTime"] = "2026-12-20T09:00:00"
            if field == "time": row["inbound"]["departureTime"] = "2026-12-23T12:00:00"
            if field == "url": row["bookingUrl"] = "https://evil.example/"
            with self.subTest(field=field):
                self.assertEqual(kiwi.normalize(data, args, kg)["status"], "no_matches")

    def test_no_local_candidate_cap_and_exact_duplicates_removed(self):
        args, kg = kiwi.validate_input(search_input())
        data = response()
        data["itineraries"] = [copy.deepcopy(data["itineraries"][0]) for _ in range(30)]
        for i, row in enumerate(data["itineraries"]): row["price"] += i
        data["itineraries"].append(copy.deepcopy(data["itineraries"][0]))
        result = kiwi.normalize(data, args, kg)
        self.assertEqual(len(result["candidates"]), 30)
        self.assertFalse(result["all_market_flights_verified"])

    def test_query_currency_and_passengers_must_match(self):
        args, kg = kiwi.validate_input(search_input())
        for key, value in (("currency", "EUR"), ("passengers", {"adults": 1})):
            data = response()
            data[key] = value
            with self.assertRaisesRegex(kiwi.FlightError, "QUERY_MISMATCH"):
                kiwi.normalize(data, args, kg)

    def test_invalid_input_rejected_before_network(self):
        for key, value in (("returnDate", "18/12/2026"), ("adults", True),
                           ("adults_hold_bags", [1]), ("dtime_from", 24),
                           ("currency", "JPY"), ("price_to", -1), ("limit", 5)):
            payload = search_input()
            payload["search"][key] = value
            with self.subTest(key=key), self.assertRaises(kiwi.FlightError):
                kiwi.validate_input(payload)

    def test_empty_results_is_success_not_network_error(self):
        args, kg = kiwi.validate_input(search_input())
        data = response()
        data["itineraries"] = []
        self.assertEqual(kiwi.normalize(data, args, kg)["status"], "no_matches")
        data["error"] = "provider failure"
        with self.assertRaises(kiwi.FlightError): kiwi.normalize(data, args, kg)

    def test_wrong_airport_and_malformed_leg_are_not_candidates(self):
        args, kg = kiwi.validate_input(search_input())
        for value in ({"from": "GMP", "departureTime": "2026-12-19T09:00:00"}, ["bad"]):
            data = response()
            data["itineraries"][0]["outbound"] = value
            self.assertEqual(kiwi.normalize(data, args, kg)["status"], "no_matches")

    def test_http_errors_are_sanitized(self):
        for status, code in ((401, "AUTH_REQUIRED"), (429, "RATE_LIMITED"), (500, "HTTP_ERROR")):
            def fail(*args, **kwargs):
                raise kiwi.urllib.error.HTTPError(kiwi.ENDPOINT, status, "private", {}, None)
            with self.assertRaisesRegex(kiwi.FlightError, code):
                kiwi.KiwiClient(opener=fail).rpc("initialize", {})

    def test_json_sse_and_error_rpc(self):
        msg = {"jsonrpc": "2.0", "id": 4, "result": {"tools": []}}
        for raw in (json.dumps(msg), 'event: message\r\ndata: '+json.dumps(msg)+'\r\n\r\n'):
            self.assertEqual(kiwi.decode_rpc(raw, 4), {"tools": []})
        with self.assertRaises(kiwi.FlightError): kiwi.decode_rpc('not-json', 4)
        with self.assertRaises(kiwi.FlightError):
            kiwi.decode_rpc(json.dumps({"id": 4, "error": {"message": "private"}}), 4)

    def test_client_calls_only_search_after_handshake_and_schema_check(self):
        args, _ = kiwi.validate_input(search_input())
        client = kiwi.KiwiClient()
        with patch.object(client, "rpc", side_effect=[{"protocolVersion": "2025-03-26"}, {},
                {"tools": [{"name": "search-flight", "inputSchema": {"properties": dict.fromkeys(args)}}]},
                {"structuredContent": response()}]) as rpc:
            self.assertEqual(client.search(args), response())
        self.assertEqual([c.args[0] for c in rpc.call_args_list],
                         ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertEqual(rpc.call_args_list[-1].args[1]["name"], "search-flight")
        remote = rpc.call_args_list[-1].args[1]["arguments"]
        self.assertNotIn("price_to", remote)
        self.assertEqual(args["price_to"], 740000)
        self.assertEqual(remote["adults_hold_bags"], [1, 1])

    def test_unsupported_remote_filter_never_calls_search(self):
        args, _ = kiwi.validate_input(search_input())
        client = kiwi.KiwiClient()
        with patch.object(client, "rpc", side_effect=[{}, {},
                {"tools": [{"name": "search-flight", "inputSchema": {"properties": {}}}]}]) as rpc:
            with self.assertRaisesRegex(kiwi.FlightError, "UNSUPPORTED_FILTER"): client.search(args)
        self.assertEqual(rpc.call_count, 3)


if __name__ == "__main__":
    unittest.main()
