"""Read-only Kiwi MCP client. No credentials, booking actions or third-party SDK."""
from __future__ import annotations

import argparse
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT = "https://mcp.kiwi.com"
PROTOCOL = "2025-03-26"
MAX_RESPONSE = 5 * 1024 * 1024


class FlightError(ValueError):
    pass


def decode_rpc(raw, request_id):
    """Accept JSON or SSE, ignoring notifications and matching the RPC id."""
    try:
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            messages = [json.loads(raw)]
        else:
            messages = []
            for event in raw.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                                 if line.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
        for message in messages:
            if message.get("id") == request_id:
                if "error" in message:
                    raise FlightError("KIWI_RPC_ERROR")
                if not isinstance(message.get("result"), dict):
                    raise FlightError("KIWI_INVALID_RESPONSE")
                return message["result"]
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise FlightError("KIWI_INVALID_RESPONSE") from exc
    raise FlightError("KIWI_MISSING_RESPONSE")


class KiwiClient:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.session = None
        self.protocol = PROTOCOL
        self.sequence = 0

    def rpc(self, method, params, notify=False):
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = self.sequence
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers=headers)
        try:
            with self.opener(request, timeout=55) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise FlightError("KIWI_RESPONSE_TOO_LARGE")
                if notify:
                    return {}
                return decode_rpc(raw.decode("utf-8"), self.sequence)
        except urllib.error.HTTPError as exc:
            code = {401: "AUTH_REQUIRED", 403: "ACCESS_DENIED", 429: "RATE_LIMITED"}.get(exc.code, "HTTP_ERROR")
            raise FlightError("KIWI_" + code) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, UnicodeDecodeError) as exc:
            raise FlightError("KIWI_NETWORK_ERROR") from exc

    def search(self, arguments):
        # Live KRW searches returned zero with price_to even when the same
        # unfiltered search returned a cheaper itinerary. Apply the user's
        # KRW budget annotation locally in normalize(); retain over-budget rows.
        remote_arguments = {k: v for k, v in arguments.items() if k != "price_to"}
        init = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                      "clientInfo": {"name": "souvenir", "version": "1.0"}})
        self.protocol = init.get("protocolVersion", PROTOCOL)
        self.rpc("notifications/initialized", {}, notify=True)
        tools = self.rpc("tools/list", {}).get("tools", [])
        tool = next((t for t in tools if t.get("name") == "search-flight"), None)
        if not tool:
            raise FlightError("KIWI_SEARCH_TOOL_UNAVAILABLE")
        schema = tool.get("inputSchema", {})
        if not set(remote_arguments) <= set(schema.get("properties", {})):
            raise FlightError("KIWI_UNSUPPORTED_FILTER")
        result = self.rpc("tools/call", {"name": "search-flight", "arguments": remote_arguments})
        if result.get("isError"):
            raise FlightError("KIWI_SEARCH_ERROR")
        data = result.get("structuredContent")
        if data is None:
            try:
                data = json.loads(next(c["text"] for c in result.get("content", []) if c.get("type") == "text"))
            except (StopIteration, KeyError, json.JSONDecodeError) as exc:
                raise FlightError("KIWI_INVALID_RESPONSE") from exc
        return data


def validate_input(payload):
    if not isinstance(payload, dict) or set(payload) - {"search", "minimum_checked_bag_kg"}:
        raise FlightError("INPUT_EXPECTED_SEARCH_OBJECT")
    args = payload.get("search")
    if not isinstance(args, dict):
        raise FlightError("INPUT_EXPECTED_SEARCH_OBJECT")
    args = dict(args)
    for key in ("flyFrom", "flyTo"):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise FlightError("INPUT_MISSING_LOCATION")
    try:
        departure = datetime.strptime(args["departureDate"], "%d/%m/%Y").date()
        if args.get("returnDate") and datetime.strptime(args["returnDate"], "%d/%m/%Y").date() < departure:
            raise FlightError("INPUT_INVALID_DATES")
    except (KeyError, ValueError, TypeError) as exc:
        raise FlightError("INPUT_INVALID_DATES") from exc
    # This entry point deliberately supports exact dates and KRW only.
    allowed = {"flyFrom", "flyTo", "departureDate", "returnDate", "adults", "children", "infants",
               "cabinClass", "currency", "locale", "price_to", "dtime_from", "dtime_to",
               "ret_dtime_from", "ret_dtime_to", "adults_hold_bags", "children_hold_bags",
               "max_sector_stopovers", "sort", "allow_self_transfer",
               "allow_overnight_stopovers", "allow_diff_airport_connection"}
    if set(args) - allowed:
        raise FlightError("INPUT_UNSUPPORTED_FILTER")
    args.setdefault("currency", "KRW")
    args.setdefault("adults", 1)
    args.setdefault("children", 0)
    args.setdefault("infants", 0)
    args.setdefault("cabinClass", "M")
    args.setdefault("sort", "quality")
    if args["currency"] != "KRW" or args["cabinClass"] not in {"M", "W", "C", "F"}:
        raise FlightError("INPUT_INVALID_CURRENCY_OR_CABIN")
    if args["sort"] not in {"price", "duration", "quality", "date", "popularity"}:
        raise FlightError("INPUT_INVALID_SORT")
    for key, low, high in (("adults", 1, 9), ("children", 0, 8), ("infants", 0, 4)):
        if type(args[key]) is not int or not low <= args[key] <= high:
            raise FlightError("INPUT_INVALID_PASSENGERS")
    for prefix in ("dtime", "ret_dtime"):
        for suffix in ("from", "to"):
            v = args.get(prefix + "_" + suffix)
            if v is not None and (type(v) is not int or not 0 <= v <= 23):
                raise FlightError("INPUT_INVALID_HOURS")
        if args.get(prefix + "_from", 0) > args.get(prefix + "_to", 23):
            raise FlightError("INPUT_INVALID_HOURS")
    for key in ("price_to", "max_sector_stopovers"):
        if key in args and (type(args[key]) is not int or args[key] < 0):
            raise FlightError("INPUT_INVALID_LIMIT")
    for key in ("allow_self_transfer", "allow_overnight_stopovers", "allow_diff_airport_connection"):
        if key in args and type(args[key]) is not bool:
            raise FlightError("INPUT_INVALID_BOOLEAN")
    for group in ("adults", "children"):
        bags = args.get(group + "_hold_bags")
        if bags is not None and (not isinstance(bags, list) or len(bags) != args[group]
                                 or any(type(v) is not int or not 0 <= v <= 2 for v in bags)):
            raise FlightError("INPUT_INVALID_BAGS")
    kg = payload.get("minimum_checked_bag_kg")
    if kg is not None and (type(kg) not in (int, float) or not math.isfinite(kg) or kg <= 0):
        raise FlightError("INPUT_INVALID_BAG_WEIGHT")
    return args, kg


def normalize(data, args, kg):
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("itineraries"), list):
        raise FlightError("KIWI_SEARCH_ERROR")
    if data.get("currency") != "KRW" or data.get("passengers") != {
        k: args[k] for k in ("adults", "children", "infants")
    }:
        raise FlightError("KIWI_QUERY_MISMATCH")
    candidates, rejected, seen = [], [], set()
    for row in data["itineraries"]:
        if not isinstance(row, dict):
            raise FlightError("KIWI_INVALID_RESPONSE")
        reasons = []
        price = row.get("price")
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            reasons.append("INVALID_PRICE")
        for leg_name, date_key, prefix in (("outbound", "departureDate", "dtime"),
                                           ("inbound", "returnDate", "ret_dtime")):
            if not args.get(date_key):
                continue
            leg = row.get(leg_name) or {}
            if not isinstance(leg, dict):
                reasons.append("INVALID_LEG")
                continue
            try:
                departure = datetime.fromisoformat(leg["departureTime"])
                if departure.date() != datetime.strptime(args[date_key], "%d/%m/%Y").date():
                    reasons.append("WRONG_DATE")
                # MCP's hour filters are inclusive hours; don't silently accept afternoon/night flights.
                if not args.get(prefix + "_from", 0) <= departure.hour <= args.get(prefix + "_to", 23):
                    reasons.append("WRONG_TIME")
                if "max_sector_stopovers" in args and (type(leg.get("stops")) is not int
                        or leg["stops"] > args["max_sector_stopovers"]):
                    reasons.append("STOPOVERS_UNCONFIRMED")
                origin, destination = ((args["flyFrom"], args["flyTo"]) if leg_name == "outbound"
                                       else (args["flyTo"], args["flyFrom"]))
                for expected, field in ((origin, "from"), (destination, "to")):
                    if len(expected) == 3 and expected.isascii() and expected.isupper():
                        if leg.get(field) != expected:
                            reasons.append("WRONG_AIRPORT")
            except (KeyError, ValueError, TypeError):
                reasons.append("INVALID_DEPARTURE")
        url = row.get("bookingUrl", "")
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or parsed.hostname not in {"kiwi.com", "www.kiwi.com"}:
            reasons.append("INVALID_BOOKING_LINK")
        identity = json.dumps([row.get("outbound"), row.get("inbound"), price, row.get("baggage")], sort_keys=True)
        if reasons:
            rejected.append({"id": row.get("id"), "reasons": reasons})
            continue
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append({"itinerary": row, "price_scope": "all_passengers_return" if args.get("returnDate")
                           else "all_passengers_oneway", "verification": "needs_check",
                           "budget_status": ("not_set" if "price_to" not in args else
                                             "over_budget" if price > args["price_to"] else "within_budget"),
                           "budget_excess_total": max(0, price - args["price_to"]) if "price_to" in args else None,
                           "checks_needed": ["final_fare_conditions", "per_traveler_baggage_allowance",
                                             "city_airport_match_and_transfer_conditions"]
                           + (["checked_bag_weight_kg"] if kg is not None else [])})
    return {"provider": "kiwi-mcp", "status": "candidates" if candidates else "no_matches",
            "searched_at": datetime.now(timezone.utc).isoformat(), "search": args,
            "minimum_checked_bag_kg": kg, "returned_count": len(data["itineraries"]),
            "candidates": candidates, "rejected": rejected,
            "coverage": "provider_returned_results_only", "all_market_flights_verified": False,
            "budget_filter": "annotate_only",
            "notion_written": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Kiwi MCP flight search")
    parser.add_argument("--input", type=Path, required=True, help="JSON containing search arguments")
    parser.add_argument("--pretty", action="store_true")
    options = parser.parse_args(argv)
    try:
        payload = json.loads(options.input.read_text(encoding="utf-8"))
        args, kg = validate_input(payload)
        result = normalize(KiwiClient().search(args), args, kg)
        code = 0
    except (OSError, ValueError, TypeError) as exc:
        result = {"provider": "kiwi-mcp", "status": "error",
                  "error": str(exc) if isinstance(exc, FlightError) else "INPUT_OR_RESPONSE_ERROR",
                  "notion_written": False, "fallback": "host_web_search"}
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if options.pretty else None))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
