"""Read-only Pilot MCP hotel search client.

The client deliberately does not expose booking or account operations.  It
searches the public Pilot MCP endpoint, walks every page advertised by the
response, removes repeated properties, and applies Souvenir's local KRW
budget filter before returning candidates to the host.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ENDPOINT = "https://mcp.pilotplans.com/mcp"
PROTOCOL = "2025-03-26"
MAX_RESPONSE = 5 * 1024 * 1024
DEFAULT_LIMIT = 50
DEFAULT_MAX_PAGES = 100


class HotelError(ValueError):
    """A sanitized, host-facing error from validation or the Pilot MCP."""


def decode_rpc(raw, request_id):
    """Accept JSON or SSE, ignoring notifications and matching the RPC id."""
    try:
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            messages = [json.loads(raw)]
        else:
            messages = []
            for event in raw.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                )
                if data:
                    messages.append(json.loads(data))
        for message in messages:
            if message.get("id") == request_id:
                if "error" in message:
                    raise HotelError("PILOT_HOTEL_RPC_ERROR")
                if not isinstance(message.get("result"), dict):
                    raise HotelError("PILOT_HOTEL_INVALID_RESPONSE")
                return message["result"]
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise HotelError("PILOT_HOTEL_INVALID_RESPONSE") from exc
    raise HotelError("PILOT_HOTEL_MISSING_RESPONSE")


def _tool_text(result):
    """Extract a structured tool result, including text-only MCP responses."""
    if not isinstance(result, dict):
        raise HotelError("PILOT_HOTEL_INVALID_RESPONSE")
    if result.get("isError"):
        raise HotelError("PILOT_HOTEL_SEARCH_ERROR")
    data = result.get("structuredContent")
    if isinstance(data, dict):
        return data
    for content in result.get("content", []):
        if isinstance(content, dict) and content.get("type") == "text":
            try:
                data = json.loads(content.get("text", ""))
            except (TypeError, json.JSONDecodeError) as exc:
                raise HotelError("PILOT_HOTEL_INVALID_RESPONSE") from exc
            if isinstance(data, dict):
                return data
    raise HotelError("PILOT_HOTEL_INVALID_RESPONSE")


class PilotHotelClient:
    """Small Streamable-HTTP MCP client for Pilot's public hotel tools."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.session = None
        self.protocol = PROTOCOL
        self.sequence = 0

    def rpc(self, method, params, notify=False):
        self.sequence += 1
        request_id = self.sequence
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with self.opener(request, timeout=55) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise HotelError("PILOT_HOTEL_RESPONSE_TOO_LARGE")
                if notify:
                    return {}
                return decode_rpc(raw.decode("utf-8"), request_id)
        except urllib.error.HTTPError as exc:
            code = {401: "AUTH_REQUIRED", 403: "ACCESS_DENIED", 429: "RATE_LIMITED"}.get(
                exc.code, "HTTP_ERROR"
            )
            raise HotelError("PILOT_HOTEL_" + code) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, UnicodeDecodeError) as exc:
            raise HotelError("PILOT_HOTEL_NETWORK_ERROR") from exc

    def _handshake(self):
        init = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "souvenir", "version": "1.0"},
            },
        )
        self.protocol = init.get("protocolVersion", PROTOCOL)
        self.rpc("notifications/initialized", {}, notify=True)
        tools = self.rpc("tools/list", {}).get("tools", [])
        return tools if isinstance(tools, list) else []

    @staticmethod
    def _find_tool(tools, name):
        tool = next((item for item in tools if isinstance(item, dict) and item.get("name") == name), None)
        if not tool:
            raise HotelError("PILOT_HOTEL_%s_TOOL_UNAVAILABLE" % name.upper())
        return tool

    def _call_tool(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return _tool_text(result)

    def search(self, arguments, max_pages=DEFAULT_MAX_PAGES):
        """Search all reachable pages, even when ``has_more`` is false.

        Pilot has returned a usable ``next_offset`` alongside ``has_more=false``
        in live responses.  The offset is therefore the termination signal;
        ``has_more`` is retained as diagnostic metadata only.
        """
        tools = self._handshake()
        tool = self._find_tool(tools, "search_hotels")
        properties = (tool.get("inputSchema") or {}).get("properties", {})
        if not isinstance(properties, dict) or not set(arguments) <= set(properties):
            raise HotelError("PILOT_HOTEL_UNSUPPORTED_FILTER")

        try:
            page_limit = int(max_pages)
        except (TypeError, ValueError) as exc:
            raise HotelError("INPUT_INVALID_MAX_PAGES") from exc
        if page_limit < 1 or page_limit > 1000:
            raise HotelError("INPUT_INVALID_MAX_PAGES")

        offset = arguments.get("offset", 0)
        rows = []
        pages = []
        seen_offsets = set()
        warnings = []
        for page_number in range(page_limit):
            if type(offset) is not int or offset < 0 or offset in seen_offsets:
                warnings.append("PAGINATION_OFFSET_LOOP")
                break
            seen_offsets.add(offset)
            page_args = dict(arguments)
            page_args["offset"] = offset
            data = self._call_tool("search_hotels", page_args)
            page_rows = data.get("hotels")
            if not isinstance(page_rows, list):
                raise HotelError("PILOT_HOTEL_INVALID_RESPONSE")
            rows.extend(page_rows)
            next_offset = data.get("next_offset")
            has_more = data.get("has_more")
            pages.append(
                {
                    "page": page_number + 1,
                    "offset": offset,
                    "returned": len(page_rows),
                    "provider_returned": data.get("returned"),
                    "provider_has_more": has_more if isinstance(has_more, bool) else None,
                    "next_offset": next_offset,
                }
            )
            if isinstance(has_more, bool) and not has_more and next_offset is not None:
                warnings.append("PROVIDER_HAS_MORE_FALSE_WITH_NEXT_OFFSET")
            if not page_rows or next_offset is None:
                break
            if type(next_offset) is not int or next_offset <= offset or next_offset in seen_offsets:
                warnings.append("PAGINATION_OFFSET_LOOP")
                break
            offset = next_offset
        else:
            warnings.append("MAX_PAGES_REACHED")
        if pages and pages[-1]["next_offset"] is not None and len(pages) >= page_limit:
            warnings.append("MAX_PAGES_REACHED")
        return {"hotels": rows, "pages": pages, "pagination_warnings": sorted(set(warnings))}

    def get_hotel_details(self, hotel_id):
        """Read details for one opaque Pilot hotel id."""
        tools = self._handshake()
        self._find_tool(tools, "get_hotel_details")
        return self._call_tool("get_hotel_details", {"hotel_id": hotel_id})

    def get_room_rates_for_hotel(self, hotel_id, check_in=None, check_out=None, guests=None):
        """Read room offers for a hotel; never reserves or modifies anything."""
        tools = self._handshake()
        self._find_tool(tools, "get_room_rates_for_hotel")
        arguments = {"hotel_id": hotel_id}
        for key, value in (("check_in", check_in), ("check_out", check_out), ("guests", guests)):
            if value is not None:
                arguments[key] = value
        return self._call_tool("get_room_rates_for_hotel", arguments)


def _date(value, code):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HotelError(code)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise HotelError(code) from exc


def validate_input(payload):
    """Validate the local search contract and return provider args + budget."""
    if not isinstance(payload, dict) or set(payload) - {"search", "budget_per_night_krw", "max_pages"}:
        raise HotelError("INPUT_EXPECTED_SEARCH_OBJECT")
    args = payload.get("search")
    if not isinstance(args, dict):
        raise HotelError("INPUT_EXPECTED_SEARCH_OBJECT")
    args = dict(args)
    for key in ("location", "check_in", "check_out"):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise HotelError("INPUT_MISSING_LOCATION" if key == "location" else "INPUT_INVALID_DATES")
    check_in = _date(args["check_in"], "INPUT_INVALID_DATES")
    check_out = _date(args["check_out"], "INPUT_INVALID_DATES")
    nights = (check_out - check_in).days
    if nights < 1:
        raise HotelError("INPUT_INVALID_DATES")

    allowed = {
        "location",
        "check_in",
        "check_out",
        "guests",
        "rooms",
        "children_ages",
        "currency",
        "guest_nationality",
        "limit",
        "offset",
        "radius_meters",
        "min_star_rating",
        "min_guest_rating",
        "amenity_ids",
        "property_type_ids",
        "ai_search",
    }
    if set(args) - allowed:
        raise HotelError("INPUT_UNSUPPORTED_FILTER")
    args.setdefault("guests", 2)
    args.setdefault("rooms", 1)
    args.setdefault("currency", "KRW")
    args.setdefault("guest_nationality", "KR")
    args.setdefault("limit", DEFAULT_LIMIT)
    args.setdefault("offset", 0)
    if args["currency"] != "KRW":
        raise HotelError("INPUT_INVALID_CURRENCY")
    for key, low, high in (("guests", 1, 20), ("rooms", 1, 10), ("limit", 1, 50), ("offset", 0, None)):
        value = args[key]
        if type(value) is not int or value < low or (high is not None and value > high):
            raise HotelError("INPUT_INVALID_%s" % key.upper())
    if type(args.get("children_ages")) not in (list, type(None)):
        raise HotelError("INPUT_INVALID_CHILDREN")
    if args.get("children_ages") is not None and any(type(age) is not int or age < 0 or age > 17 for age in args["children_ages"]):
        raise HotelError("INPUT_INVALID_CHILDREN")
    for key in ("radius_meters", "min_star_rating", "min_guest_rating"):
        if key in args and (type(args[key]) not in (int, float) or not math.isfinite(args[key]) or args[key] < 0):
            raise HotelError("INPUT_INVALID_FILTER")
    for key in ("amenity_ids", "property_type_ids"):
        if key in args and (not isinstance(args[key], list) or any(not isinstance(item, (str, int)) for item in args[key])):
            raise HotelError("INPUT_INVALID_FILTER")
    if "ai_search" in args and type(args["ai_search"]) is not bool:
        raise HotelError("INPUT_INVALID_FILTER")

    budget = payload.get("budget_per_night_krw")
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0):
        raise HotelError("INPUT_INVALID_BUDGET")
    max_pages = payload.get("max_pages", DEFAULT_MAX_PAGES)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise HotelError("INPUT_INVALID_MAX_PAGES")
    return args, budget, nights, max_pages


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _canonical_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _identity(row):
    if row.get("id") not in (None, ""):
        return "id:" + str(row["id"])
    for key in ("bookingUrl", "url", "link"):
        value = row.get(key)
        if isinstance(value, str) and value:
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme and parsed.netloc:
                return "url:" + urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", ""))
    return "name:" + _canonical_text(row.get("name")) + "|address:" + _canonical_text(row.get("address"))


def normalize(data, args, budget, nights, pagination_warnings=None, pages=None):
    """Filter raw Pilot rows and return the stable host-facing result shape."""
    if not isinstance(data, dict) or not isinstance(data.get("hotels"), list):
        raise HotelError("PILOT_HOTEL_SEARCH_ERROR")
    candidates, rejected, seen = [], [], set()
    budget_total = budget * nights if budget is not None else None
    for row in data["hotels"]:
        if not isinstance(row, dict):
            raise HotelError("PILOT_HOTEL_INVALID_RESPONSE")
        identity = _identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        reasons = []
        price = row.get("price")
        currency = row.get("currency", args.get("currency"))
        if not _number(price):
            reasons.append("INVALID_PRICE")
        if currency != "KRW":
            reasons.append("PRICE_CURRENCY_MISMATCH")
        if budget_total is not None and _number(price) and currency == "KRW" and price > budget_total:
            reasons.append("OVER_BUDGET")
        if reasons:
            rejected.append({"id": row.get("id"), "name": row.get("name"), "reasons": reasons})
            continue
        candidate = {
            "hotel": row,
            "identity": identity,
            "price_scope": "provider_search_stay_rate_unverified",
            "price_per_night_estimate": round(price / nights, 2) if _number(price) else None,
            "verification": "needs_check",
            "checks_needed": [
                "room_rate_recheck_for_selected_room",
                "taxes_fees_and_currency",
                "room_count_and_occupancy",
                "board_refundability_and_cancellation",
                "booking_url_and_availability",
            ],
        }
        candidates.append(candidate)
    warnings = list(pagination_warnings or data.get("pagination_warnings", []))
    return {
        "provider": "pilot-mcp",
        "status": "candidates" if candidates else "no_matches",
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "search": args,
        "nights": nights,
        "budget_per_night_krw": budget,
        "budget_total_krw": budget_total,
        "budget_filter": "local_krw_stay_total_precheck" if budget is not None else "not_requested",
        "price_semantics": "provider_search_price_requires_room_rate_recheck",
        "returned_count": len(data["hotels"]),
        "unique_count": len(seen),
        "candidate_count": len(candidates),
        "rejected_count": len(rejected),
        "pages": pages or data.get("pages", []),
        "pagination_warnings": sorted(set(warnings)),
        "candidates": candidates,
        "rejected": rejected,
        "coverage": "provider_returned_pages_only",
        "all_market_hotels_verified": False,
        "notion_written": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Pilot MCP hotel search")
    parser.add_argument("--input", type=Path, required=True, help="JSON containing hotel search arguments")
    parser.add_argument("--pretty", action="store_true")
    options = parser.parse_args(argv)
    try:
        payload = json.loads(options.input.read_text(encoding="utf-8"))
        args, budget, nights, max_pages = validate_input(payload)
        raw = PilotHotelClient().search(args, max_pages=max_pages)
        result = normalize(raw, args, budget, nights)
        code = 0
    except (OSError, ValueError, TypeError) as exc:
        result = {
            "provider": "pilot-mcp",
            "status": "error",
            "error": str(exc) if isinstance(exc, HotelError) else "INPUT_OR_RESPONSE_ERROR",
            "notion_written": False,
            "fallback": "host_web_search",
        }
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if options.pretty else None))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
