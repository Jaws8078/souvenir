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
import os
import re
import socket
import time
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
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.75
RETRYABLE_ERROR_MARKERS = (
    "NETWORK_ERROR",
    "TIMEOUT",
    "HTTP_ERROR",
    "RPC_ERROR",
    "SEARCH_ERROR",
    "MISSING_RESPONSE",
    "INVALID_RESPONSE",
    "RATE_LIMITED",
)


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
        self.tools = None

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
        if self.tools is not None:
            return self.tools
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
        self.tools = tools if isinstance(tools, list) else []
        return self.tools

    @staticmethod
    def _find_tool(tools, name):
        tool = next((item for item in tools if isinstance(item, dict) and item.get("name") == name), None)
        if not tool:
            raise HotelError("PILOT_HOTEL_%s_TOOL_UNAVAILABLE" % name.upper())
        return tool

    def _call_tool(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return _tool_text(result)

    @staticmethod
    def _retry_settings():
        try:
            attempts = int(os.environ.get("SOUVENIR_PILOT_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS))
        except (TypeError, ValueError):
            attempts = DEFAULT_RETRY_ATTEMPTS
        try:
            backoff = float(os.environ.get("SOUVENIR_PILOT_RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS))
        except (TypeError, ValueError):
            backoff = DEFAULT_RETRY_BACKOFF_SECONDS
        return max(1, min(attempts, 5)), max(0.0, min(backoff, 10.0))

    @staticmethod
    def _retryable(error):
        code = str(error).upper()
        return any(marker in code for marker in RETRYABLE_ERROR_MARKERS)

    def _call_tool_with_retry(self, name, arguments):
        attempts, backoff = self._retry_settings()
        for attempt in range(attempts):
            try:
                return self._call_tool(name, arguments)
            except HotelError as exc:
                if attempt + 1 >= attempts or not self._retryable(exc):
                    raise
                time.sleep(backoff * (2 ** attempt))
        raise HotelError("PILOT_HOTEL_SEARCH_ERROR")

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
            data = self._call_tool_with_retry("search_hotels", page_args)
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
        return self._call_tool_with_retry("get_hotel_details", {"hotel_id": hotel_id})

    def get_room_rates_for_hotel(self, hotel_id, check_in=None, check_out=None, guests=None):
        """Read room offers for a hotel; never reserves or modifies anything."""
        tools = self._handshake()
        self._find_tool(tools, "get_room_rates_for_hotel")
        arguments = {"hotel_id": hotel_id}
        for key, value in (("check_in", check_in), ("check_out", check_out), ("guests", guests)):
            if value is not None:
                arguments[key] = value
        return self._call_tool_with_retry("get_room_rates_for_hotel", arguments)


def _date(value, code):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HotelError(code)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise HotelError(code) from exc


def _validate_room_filters(value):
    """Validate local filters that require room-rate offers rather than search_hotels."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"bed_type", "refundable_only"}:
        raise HotelError("INPUT_UNSUPPORTED_ROOM_FILTER")
    filters = {}
    if "bed_type" in value:
        bed_type = value["bed_type"]
        if not isinstance(bed_type, str) or not bed_type.strip() or len(bed_type.strip()) > 80:
            raise HotelError("INPUT_INVALID_BED_TYPE")
        filters["bed_type"] = bed_type.strip()
    if "refundable_only" in value:
        if type(value["refundable_only"]) is not bool:
            raise HotelError("INPUT_INVALID_REFUNDABLE_FILTER")
        filters["refundable_only"] = value["refundable_only"]
    return filters


def validate_input(payload, include_room_filters=False):
    """Validate the local search contract and return provider args + budget."""
    if not isinstance(payload, dict) or set(payload) - {"search", "budget_per_night_krw", "max_pages", "room_filters"}:
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
    room_filters = _validate_room_filters(payload.get("room_filters"))
    if include_room_filters:
        return args, budget, nights, max_pages, room_filters
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


_BED_TYPE_ALIASES = {
    "트윈": ("twin", "two single", "2 single", "2 twin"),
    "twin": ("twin", "two single", "2 single", "2 twin"),
    "더블": ("double", "full"),
    "double": ("double", "full"),
    "싱글": ("single",),
    "single": ("single",),
    "퀸": ("queen",),
    "queen": ("queen",),
    "킹": ("king",),
    "king": ("king",),
    "벙커": ("bunk",),
    "bunk": ("bunk",),
}


def _bed_matches(room_name, requested):
    """Match common Korean/English bed labels against Pilot's roomName."""
    room_text = _canonical_text(room_name)
    query = _canonical_text(requested)
    if not room_text:
        return False
    if query in {"침대", "bed", "any", "상관없음", "무관"}:
        return True
    tokens = _BED_TYPE_ALIASES.get(query, (query,))
    return any(token in room_text for token in tokens)


def _offer_summary(offer):
    return {
        key: offer[key]
        for key in ("roomName", "board", "refundable", "price", "currency")
        if key in offer
    }


def apply_room_filters(client, result, args, room_filters):
    """Filter candidates using read-only room offers from Pilot.

    Bed type and refundability are room-level properties, so they cannot be
    safely applied to the summary returned by ``search_hotels``.  Only active
    filters trigger one ``get_room_rates_for_hotel`` call per budget candidate.
    """
    room_filters = dict(room_filters or {})
    active = bool(room_filters.get("bed_type")) or room_filters.get("refundable_only") is True
    result["room_filters"] = room_filters
    result["room_filter_applied"] = active
    if not active:
        return result

    kept = []
    rejected = []
    lookup_errors = []
    checked_count = 0
    for candidate in result.get("candidates", []):
        hotel = candidate.get("hotel", {})
        hotel_id = hotel.get("id")
        checked_count += 1
        if not hotel_id:
            rejected.append({"id": None, "name": hotel.get("name"), "reasons": ["ROOM_ID_MISSING"]})
            continue
        try:
            rates = client.get_room_rates_for_hotel(
                hotel_id,
                check_in=args["check_in"],
                check_out=args["check_out"],
                guests=args["guests"],
            )
        except HotelError as exc:
            error = {"id": hotel_id, "name": hotel.get("name"), "error": str(exc)}
            lookup_errors.append(error)
            rejected.append({**error, "reasons": ["ROOM_RATE_LOOKUP_ERROR"]})
            continue
        offers = rates.get("offers") if isinstance(rates, dict) else None
        if not isinstance(offers, list):
            rejected.append({"id": hotel_id, "name": hotel.get("name"), "reasons": ["ROOM_RATE_INVALID_RESPONSE"]})
            continue
        matching = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            if room_filters.get("refundable_only") is True and offer.get("refundable") is not True:
                continue
            if room_filters.get("bed_type") and not _bed_matches(offer.get("roomName"), room_filters["bed_type"]):
                continue
            matching.append(_offer_summary(offer))
        if not matching:
            reasons = []
            if room_filters.get("bed_type"):
                reasons.append("BED_TYPE_NOT_FOUND")
            if room_filters.get("refundable_only") is True:
                reasons.append("REFUNDABLE_ROOM_NOT_FOUND")
            rejected.append({"id": hotel_id, "name": hotel.get("name"), "reasons": reasons or ["ROOM_FILTER_NO_MATCH"]})
            continue
        verified = dict(candidate)
        verified["room_rate_verification"] = {
            "status": "matched",
            "returned_offers": len(offers),
            "matching_offers": matching,
        }
        kept.append(verified)

    result["candidates"] = kept
    result["rejected"].extend(rejected)
    result["candidate_count"] = len(kept)
    result["rejected_count"] = len(result["rejected"])
    result["room_filter_checked_count"] = checked_count
    result["room_filter_rejected_count"] = len(rejected)
    result["room_filter_lookup_errors"] = lookup_errors
    result["room_filter_status"] = "matched" if kept else ("lookup_error" if lookup_errors else "no_matches")
    result["status"] = "candidates" if kept else "no_matches"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Pilot MCP hotel search")
    parser.add_argument("--input", type=Path, required=True, help="JSON containing hotel search arguments")
    parser.add_argument("--pretty", action="store_true")
    options = parser.parse_args(argv)
    try:
        payload = json.loads(options.input.read_text(encoding="utf-8"))
        args, budget, nights, max_pages, room_filters = validate_input(payload, include_room_filters=True)
        client = PilotHotelClient()
        raw = client.search(args, max_pages=max_pages)
        result = normalize(raw, args, budget, nights)
        result = apply_room_filters(client, result, args, room_filters)
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
