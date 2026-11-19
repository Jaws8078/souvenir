#!/usr/bin/env python3
"""Serve Souvenir's local, read-only candidate review board.

The search clients return provider-shaped JSON because the provider details are
useful for later validation.  This module turns one result or a combined
``sections`` packet into a small public UI contract and serves it through the
Python standard library only.  Selecting a card is deliberately not a booking
or a Notion write; an optional output file records the review decision for the
host's existing approval flow.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR.parent / "web"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from kiwi_flights import KiwiClient, normalize as normalize_flights, validate_input as validate_flight_input  # noqa: E402
from pilot_hotels import (  # noqa: E402
    PilotHotelClient,
    apply_room_filters,
    normalize as normalize_hotels,
    validate_input as validate_hotel_input,
)
from trip_engine import prepare_google_place_candidate_page  # noqa: E402


MAX_BODY_BYTES = 1024 * 1024
PACKET_VERSION = 1

SECTION_DEFS: Dict[str, Dict[str, str]] = {
    "flights": {"label": "항공권", "icon": "✈️", "singular": "항공편"},
    "hotels": {"label": "숙소", "icon": "⌂", "singular": "숙소"},
    "places": {"label": "장소", "icon": "◉", "singular": "장소"},
}
SECTION_ALIASES = {
    "flights": ("flights", "flight", "air", "항공권", "항공", "airfare"),
    "hotels": ("hotels", "hotel", "숙소", "accommodation", "lodging"),
    "places": ("places", "place", "장소", "관광", "points_of_interest"),
}
SECRET_WORDS = re.compile(r"token|secret|password|cookie|authorization|api[_-]?key|credential", re.I)
NUMBER_RE = re.compile(r"[-+]?\d+(?:[,.]\d+)*(?:\.\d+)?")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def _first(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    rendered = _text(value).replace("₩", "").replace("원", "").replace("KRW", "")
    rendered = rendered.replace("\\u00a0", " ").replace(" ", "")
    match = NUMBER_RE.search(rendered)
    if not match:
        return None
    token = match.group(0)
    # Provider responses generally use commas as thousands separators.
    token = token.replace(",", "")
    try:
        parsed = float(token)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        values = [_display(item) for item in value]
        return ", ".join(item for item in values if item)
    if isinstance(value, dict):
        for key in ("name", "label", "code", "iata", "value"):
            if value.get(key) not in (None, ""):
                return _display(value[key])
        return ""
    return _text(value)


def _public_mapping(value: Any) -> Dict[str, Any]:
    """Keep simple search context while never exposing credential-like keys."""
    if not isinstance(value, Mapping):
        return {}
    public: Dict[str, Any] = {}
    for key, item in value.items():
        if SECRET_WORDS.search(str(key)):
            continue
        if isinstance(item, Mapping):
            nested = _public_mapping(item)
            if nested:
                public[str(key)] = nested
        elif isinstance(item, list):
            safe_items = [_display(part) for part in item if _display(part)]
            if safe_items:
                public[str(key)] = safe_items
        elif (
            isinstance(item, (str, int, bool))
            or (isinstance(item, float) and math.isfinite(item))
        ):
            public[str(key)] = item
    return public


def _slug_id(section: str, value: Any, index: int) -> str:
    raw = _display(value)
    raw = re.sub(r"[^\w가-힣.-]+", "-", raw, flags=re.UNICODE).strip("-")
    return f"{section}-{raw[:72] or 'candidate'}-{index + 1}"


def _format_price(value: Optional[float], currency: str = "") -> Optional[str]:
    if value is None:
        return None
    rendered = f"{value:,.0f}"
    unit = {"KRW": "원", "JPY": "엔", "USD": "$", "EUR": "€"}.get(currency.upper(), currency)
    return f"{rendered}{unit}" if unit else rendered


def _date_label(value: Any) -> str:
    rendered = _text(value)
    if not rendered:
        return ""
    try:
        parsed = dt.date.fromisoformat(rendered[:10])
    except ValueError:
        return rendered
    return f"{parsed.year}.{parsed.month}.{parsed.day}"


def _leg_time(leg: Any) -> Tuple[str, str]:
    if not isinstance(leg, Mapping):
        return "", ""
    origin = _display(_first(leg, "from", "origin", "departureAirport", "departure_airport"))
    destination = _display(_first(leg, "to", "destination", "arrivalAirport", "arrival_airport"))
    departure = _display(_first(leg, "departureTime", "departure_time", "dtime", "departure"))
    arrival = _display(_first(leg, "arrivalTime", "arrival_time", "atime", "arrival"))
    def short_time(value: str) -> str:
        match = re.search(r"(?:T|\s)(\d{2}:\d{2})", value)
        return match.group(1) if match else value
    left = f"{origin} {short_time(departure)}".strip()
    right = f"{destination} {short_time(arrival)}".strip()
    return left, right


def _flight_candidate(candidate: Mapping[str, Any], index: int) -> Dict[str, Any]:
    itinerary = candidate.get("itinerary") if isinstance(candidate.get("itinerary"), Mapping) else candidate
    outbound = itinerary.get("outbound") if isinstance(itinerary.get("outbound"), Mapping) else {}
    inbound = itinerary.get("inbound") if isinstance(itinerary.get("inbound"), Mapping) else {}
    out_left, out_right = _leg_time(outbound)
    in_left, in_right = _leg_time(inbound)
    route = " → ".join(part for part in (out_left.split(" ", 1)[0] if out_left else "", out_right.split(" ", 1)[0] if out_right else "") if part)
    airline = _display(_first(itinerary, "airline", "airlines", "carrier", "validatingCarrier"))
    if not airline:
        airline = _display(_first(outbound, "airline", "carrier", "airlineName"))
    title = airline or route or f"항공편 후보 {index + 1}"
    subtitle = route or "출발·도착 공항 확인 필요"
    price = _number(_first(itinerary, "price", "totalPrice", "amount"))
    if price is None:
        price = _number(_first(candidate, "price", "total_price"))
    currency = _display(_first(itinerary, "currency")) or "KRW"
    metadata: List[str] = []
    if out_left or out_right:
        metadata.append(f"가는 편  {out_left} → {out_right}".strip())
    if in_left or in_right:
        metadata.append(f"오는 편  {in_left} → {in_right}".strip())
    stops = _first(outbound, "stops", "stopovers")
    if stops not in (None, ""):
        metadata.append(f"경유 {stops}회" if _number(stops) is not None else _display(stops))
    baggage = _display(_first(itinerary, "baggage", "bags", "includedBaggage"))
    if baggage:
        metadata.append(f"수하물 {baggage}")
    tags = ["후보 검토"]
    if candidate.get("verification") == "needs_check":
        tags.append("최종 조건 확인 필요")
    booking_url = _display(_first(itinerary, "bookingUrl", "booking_url", "deepLink", "url"))
    identity = _first(candidate, "identity", "id") or _first(itinerary, "id", "bookingId") or route
    return {
        "id": _slug_id("flight", identity, index),
        "section": "flights",
        "title": title,
        "subtitle": subtitle,
        "location": route,
        "price": price,
        "currency": currency,
        "price_label": _format_price(price, currency),
        "price_note": "전체 여행자 기준 · 최종 결제액 아님" if price is not None else "가격 확인 필요",
        "metadata": metadata,
        "tags": tags,
        "url": booking_url if booking_url.startswith("https://") else "",
        "score": None,
        "verification": "needs_check" if candidate.get("verification") == "needs_check" else "review",
        "image_url": "",
        "details": {
            "가는 편": f"{out_left} → {out_right}".strip(" →") if out_left or out_right else "확인 필요",
            "오는 편": f"{in_left} → {in_right}".strip(" →") if in_left or in_right else "확인 필요",
        },
    }


def _hotel_candidate(candidate: Mapping[str, Any], index: int) -> Dict[str, Any]:
    hotel = candidate.get("hotel") if isinstance(candidate.get("hotel"), Mapping) else candidate
    title = _display(_first(hotel, "name", "hotelName", "title")) or f"숙소 후보 {index + 1}"
    address = _display(_first(hotel, "address", "location", "formattedAddress")) or "주소 확인 필요"
    price = _number(_first(candidate, "price_per_night_estimate", "price_per_night"))
    total_price = _number(_first(hotel, "price", "totalPrice", "total_price"))
    if price is None and total_price is not None:
        nights = _number(_first(candidate, "nights")) or 1
        price = total_price / nights
    currency = _display(_first(hotel, "currency")) or "KRW"
    metadata: List[str] = []
    rating = _number(_first(hotel, "rating", "guestRating", "reviewScore"))
    if rating is not None:
        metadata.append(f"평점 {rating:g}")
    rooms = _display(_first(hotel, "roomName", "room_type", "roomType"))
    if rooms:
        metadata.append(rooms)
    verification = candidate.get("room_rate_verification")
    offers = verification.get("matching_offers", []) if isinstance(verification, Mapping) else []
    if offers:
        offer_names = [_display(_first(offer, "roomName", "room_name")) for offer in offers[:2] if isinstance(offer, Mapping)]
        offer_names = [name for name in offer_names if name]
        if offer_names:
            metadata.append(" · ".join(offer_names))
    tags = ["후보 검토"]
    if offers:
        tags.append("객실 조건 일치")
        if any(isinstance(offer, Mapping) and offer.get("refundable") is True for offer in offers):
            tags.append("취소 가능")
    elif candidate.get("verification") == "needs_check":
        tags.append("객실·취소 조건 확인 필요")
    if candidate.get("price_scope"):
        tags.append("예산 필터 적용")
    identity = _first(candidate, "identity", "id") or _first(hotel, "id", "name") or title
    url = _display(_first(hotel, "bookingUrl", "booking_url", "url", "link"))
    return {
        "id": _slug_id("hotel", identity, index),
        "section": "hotels",
        "title": title,
        "subtitle": address,
        "location": address,
        "price": price,
        "currency": currency,
        "price_label": _format_price(price, currency),
        "price_note": "1박 추정 · 객실 요금 재확인 필요" if price is not None else "가격 확인 필요",
        "metadata": metadata,
        "tags": tags,
        "url": url if url.startswith("https://") else "",
        "score": rating,
        "verification": "matched" if offers else "needs_check",
        "image_url": _display(_first(hotel, "image", "image_url", "thumbnail", "photo")),
        "details": {
            "객실": " · ".join(_display(_first(offer, "roomName", "room_name")) for offer in offers[:2] if isinstance(offer, Mapping)) or "객실 조건 확인 필요",
            "총액": _format_price(total_price, currency) if total_price is not None else "확인 필요",
        },
    }


def _place_candidate(candidate: Mapping[str, Any], index: int) -> Dict[str, Any]:
    place = candidate.get("place") if isinstance(candidate.get("place"), Mapping) else candidate
    title = _display(_first(place, "장소", "name", "displayName", "title")) or f"장소 후보 {index + 1}"
    location = _display(_first(place, "지역", "region", "formattedAddress", "address")) or "지역 확인 필요"
    rating = _number(_first(place, "평점", "rating", "googleRating"))
    kind = _display(_first(place, "종류", "type", "category", "primaryTypeDisplayName"))
    status = _display(_first(place, "영업 상태", "businessStatus", "business_status"))
    metadata = [part for part in (kind, status, f"평점 {rating:g}" if rating is not None else "") if part]
    tags = ["후보 검토", "저장 전 확인"]
    if _display(_first(place, "여행지 확인", "destination_match")):
        tags.append(_display(_first(place, "여행지 확인", "destination_match")))
    identity = _first(place, "장소 ID", "place_id", "id") or _first(place, "지도 URL", "googleMapsUri", "maps_url") or title
    url = _display(_first(place, "지도 URL", "googleMapsUri", "maps_url", "map_url"))
    official = _display(_first(place, "공식 링크", "websiteUri", "website", "official_url"))
    return {
        "id": _slug_id("place", identity, index),
        "section": "places",
        "title": title,
        "subtitle": location,
        "location": location,
        "price": None,
        "currency": "",
        "price_label": "",
        "price_note": _display(_first(place, "예산 메모", "budget", "priceLevel", "price_level")) or "예산 정보 확인 필요",
        "metadata": metadata,
        "tags": tags,
        "url": url if url.startswith("https://") else official if official.startswith("https://") else "",
        "score": rating,
        "verification": "review",
        "image_url": _display(_first(place, "사진 URL", "photoUri", "photo_url", "image_url", "thumbnail")),
        "details": {
            "공식 링크": official or "확인 필요",
            "영업시간": _display(_first(place, "영업시간·휴무", "openingHours", "opening_hours", "hours")) or "확인 필요",
        },
    }


def _generic_candidate(section: str, candidate: Mapping[str, Any], index: int) -> Dict[str, Any]:
    title = _display(_first(candidate, "title", "name", "명칭")) or f"{SECTION_DEFS[section]['singular']} 후보 {index + 1}"
    price = _number(_first(candidate, "price", "amount", "total_price"))
    currency = _display(_first(candidate, "currency"))
    return {
        "id": _slug_id(section, _first(candidate, "id", "identity", "name") or title, index),
        "section": section,
        "title": title,
        "subtitle": _display(_first(candidate, "subtitle", "address", "location", "지역")) or "상세 정보 확인 필요",
        "location": _display(_first(candidate, "address", "location", "지역")),
        "price": price,
        "currency": currency,
        "price_label": _format_price(price, currency),
        "price_note": "후보 정보 확인 필요",
        "metadata": [],
        "tags": ["후보 검토"],
        "url": _display(_first(candidate, "url", "link", "source")),
        "score": _number(_first(candidate, "rating", "평점")),
        "verification": "review",
        "image_url": _display(_first(candidate, "image", "image_url", "thumbnail")),
        "details": {},
    }


def normalize_candidate(section: str, candidate: Any, index: int) -> Dict[str, Any]:
    if not isinstance(candidate, Mapping):
        candidate = {"title": _text(candidate)}
    if section == "flights":
        return _flight_candidate(candidate, index)
    if section == "hotels":
        return _hotel_candidate(candidate, index)
    if section == "places":
        return _place_candidate(candidate, index)
    return _generic_candidate(section, candidate, index)


def _canonical_section(key: Any) -> Optional[str]:
    rendered = _text(key).casefold()
    for canonical, aliases in SECTION_ALIASES.items():
        if rendered in {alias.casefold() for alias in aliases}:
            return canonical
    return None


def _section_payloads(raw: Mapping[str, Any], explicit_kind: Optional[str]) -> Dict[str, Any]:
    sections: Dict[str, Any] = {}
    container = _first(raw, "sections", "results", "결과", "파트")
    if isinstance(container, Mapping):
        for key, value in container.items():
            canonical = _canonical_section(key)
            if canonical:
                sections[canonical] = value
    if sections:
        return sections
    candidate_key = next((key for key in ("candidates", "후보", "items", "results") if key in raw), None)
    if candidate_key is not None:
        canonical = _canonical_section(explicit_kind) if explicit_kind else None
        provider = _text(raw.get("provider")).casefold()
        if not canonical:
            canonical = "flights" if "kiwi" in provider or "flight" in provider else "hotels" if "pilot" in provider or "hotel" in provider else "places"
        sections[canonical] = raw
    return sections


def _candidate_list(value: Any) -> List[Any]:
    if isinstance(value, Mapping):
        for key in ("candidates", "후보", "items", "results"):
            if key not in value:
                continue
            candidates = value[key]
            if isinstance(candidates, list):
                return candidates
            if isinstance(candidates, Mapping):
                return [candidates]
            return []
    if isinstance(value, list):
        return value
    return []


def _filter_labels(section: Mapping[str, Any], key: str) -> List[str]:
    labels: List[str] = []
    search = _first(section, "search", "검색", "filters", "필터")
    if isinstance(search, Mapping):
        pairs = [
            (("location", "목적지", "지역"), "지역"),
            (("check_in", "출발일", "가는 날짜"), "출발"),
            (("check_out", "도착일", "귀국일"), "귀국"),
            (("adults", "guests", "인원"), "인원"),
            (("flyFrom", "출발 공항"), "출발 공항"),
            (("flyTo", "도착 공항"), "도착 공항"),
        ]
        for keys, label in pairs:
            value = _first(search, *keys)
            if value not in (None, "", []):
                labels.append(f"{label} {_display(value)}")
    budget = _first(section, "budget_per_night_krw", "budget", "예산", "항공권 예산")
    if budget not in (None, "", []):
        labels.append(f"예산 {_format_price(float(budget), 'KRW')}" if isinstance(budget, (int, float)) and not isinstance(budget, bool) else f"예산 {_display(budget)}")
    room_filters = _first(section, "room_filters", "객실 필터")
    if isinstance(room_filters, Mapping):
        bed = _first(room_filters, "bed_type", "침대")
        if bed:
            labels.append(f"{_display(bed)} 침대")
        if room_filters.get("refundable_only") is True:
            labels.append("취소 가능")
    if not labels:
        labels.append("조건을 적용한 후보")
    return labels


def _trip_summary(raw: Mapping[str, Any], section_payloads: Mapping[str, Any]) -> Dict[str, str]:
    trip = _first(raw, "trip", "여행", "여행 정보", "summary")
    trip = trip if isinstance(trip, Mapping) else {}
    searches = [value for value in section_payloads.values() if isinstance(value, Mapping)]
    search = next((value.get("search") for value in searches if isinstance(value.get("search"), Mapping)), {})
    destination = _display(_first(trip, "destination", "목적지", "여행지", "location")) or _display(_first(search, "location", "destination", "flyTo", "도착 공항"))
    start = _first(trip, "start", "start_date", "출발일", "check_in") or _first(search, "departureDate", "start_date", "check_in")
    end = _first(trip, "end", "end_date", "귀국일", "check_out") or _first(search, "returnDate", "end_date", "check_out")
    travelers = _first(trip, "travelers", "travelers_label", "인원", "adults", "guests") or _first(search, "adults", "guests")
    if isinstance(travelers, (int, float)):
        travelers = f"성인 {int(travelers)}명"
    budget = _first(trip, "budget", "예산", "budget_label")
    if budget is None:
        budget = _first(next((value for value in searches if isinstance(value, Mapping)), {}), "budget_per_night_krw", "budget_total_krw")
    date_label = ""
    if start or end:
        date_label = " ~ ".join(part for part in (_date_label(start), _date_label(end)) if part)
    budget_label = _format_price(float(budget), "KRW") if isinstance(budget, (int, float)) and not isinstance(budget, bool) else _display(budget)
    return {
        "destination": destination or "여행지 미정",
        "start_date": _text(start),
        "end_date": _text(end),
        "date_label": date_label or "여행 날짜 확인 필요",
        "travelers_label": _display(travelers) or "인원 확인 필요",
        "budget_label": budget_label or "예산 미정",
        "title": f"{destination or '여행'} 후보 검토",
    }


def normalize_packet(raw: Any, explicit_kind: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("후보 패킷은 JSON 객체여야 합니다.")
    sections_raw = _section_payloads(raw, explicit_kind)
    if not sections_raw:
        raise ValueError("후보 패킷에서 candidates 또는 sections를 찾지 못했습니다.")
    sections: Dict[str, Any] = {}
    for key, payload in sections_raw.items():
        candidates = _candidate_list(payload)
        if not isinstance(payload, Mapping):
            payload = {}
        normalized = [normalize_candidate(key, item, index) for index, item in enumerate(candidates)]
        sections[key] = {
            "key": key,
            "label": SECTION_DEFS[key]["label"],
            "icon": SECTION_DEFS[key]["icon"],
            "provider": _display(_first(payload, "provider", "검색 제공자")) or "호스트 검색",
            "status": _display(_first(payload, "status", "상태")) or "candidates",
            "candidate_count": len(normalized),
            "rejected_count": _number(_first(payload, "rejected_count", "제외 수")) or 0,
            "filters": _filter_labels(payload, key),
            "search": _public_mapping(_first(payload, "search", "검색", "검색 설정")),
            "budget_per_night_krw": _first(payload, "budget_per_night_krw"),
            "room_filters": _public_mapping(_first(payload, "room_filters", "객실 필터")),
            "searchable": key in {"flights", "hotels", "places"},
            "coverage": _display(_first(payload, "coverage", "검색 범위")) or "provider_returned_results_only",
            "candidates": normalized,
        }
    if not sections:
        raise ValueError("표시할 후보가 없습니다.")
    generated = _display(_first(raw, "searched_at", "generated_at", "생성 시각")) or dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "version": PACKET_VERSION,
        "generated_at": generated,
        "trip": _trip_summary(raw, sections_raw),
        "sections": sections,
        "notion_written": False,
        "booking_completed": False,
        "review_only": True,
    }


class ReviewSearchError(ValueError):
    """A safe, user-facing error raised while refreshing one result tab."""

    def __init__(self, message: str, code: str = "SEARCH_FAILED", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def _section_from_source(source: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    """Find one provider-shaped section without exposing it to the browser."""
    container = _first(source, "sections", "results", "결과", "파트")
    if isinstance(container, Mapping):
        for key, value in container.items():
            if _canonical_section(key) == section and isinstance(value, Mapping):
                return value
    if any(key in source for key in ("candidates", "후보", "items", "results")):
        return source
    return {}


def _replace_source_section(source: Mapping[str, Any], section: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copied source packet with one section replaced."""
    updated = copy.deepcopy(dict(source))
    container_key = next((key for key in ("sections", "results", "결과", "파트") if isinstance(updated.get(key), Mapping)), None)
    if container_key:
        container = dict(updated[container_key])
        existing_key = next((key for key in container if _canonical_section(key) == section), section)
        container[existing_key] = copy.deepcopy(dict(payload))
        updated[container_key] = container
        return updated
    # A single-part input has no sections wrapper; preserve its trip metadata
    # while replacing the provider result at the top level.
    for key in ("candidates", "후보", "items", "results", "provider", "status", "search", "검색", "coverage", "검색 범위"):
        updated.pop(key, None)
    updated.update(copy.deepcopy(dict(payload)))
    return updated


def _condition_value(conditions: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = conditions.get(key, default)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return value


def _date_for_kiwi(value: Any, key: str, *, required: bool = False) -> str:
    rendered = _text(value)
    if not rendered:
        if required:
            raise ReviewSearchError(f"{key}를 입력해주세요.", "INPUT_INVALID_DATE")
        return ""
    for pattern, output in (("%Y-%m-%d", "%d/%m/%Y"), ("%d/%m/%Y", "%d/%m/%Y")):
        try:
            return dt.datetime.strptime(rendered, pattern).strftime(output)
        except ValueError:
            continue
    raise ReviewSearchError(f"{key} 형식이 올바르지 않습니다.", "INPUT_INVALID_DATE")


def _merge_allowed(source: Mapping[str, Any], conditions: Mapping[str, Any], allowed: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(conditions, Mapping):
        raise ReviewSearchError("검색 조건 형식이 올바르지 않습니다.", "INPUT_INVALID_CONDITIONS")
    unknown = set(conditions) - set(allowed)
    if unknown:
        raise ReviewSearchError("지원하지 않는 검색 조건이 포함되어 있습니다.", "INPUT_UNSUPPORTED_FILTER")
    merged = dict(source)
    for key in allowed:
        if key not in conditions:
            continue
        value = conditions[key]
        if value in (None, ""):
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _search_flights(source_section: Mapping[str, Any], conditions: Mapping[str, Any]) -> Dict[str, Any]:
    source_search = _first(source_section, "search", "검색")
    source_search = dict(source_search) if isinstance(source_search, Mapping) else {}
    allowed = (
        "flyFrom", "flyTo", "departureDate", "returnDate", "adults", "children", "infants",
        "cabinClass", "currency", "locale", "price_to", "dtime_from", "dtime_to",
        "ret_dtime_from", "ret_dtime_to", "max_sector_stopovers", "sort",
        "allow_self_transfer", "allow_overnight_stopovers", "allow_diff_airport_connection",
    )
    source_search = {key: value for key, value in source_search.items() if key in allowed}
    merged = _merge_allowed(source_search, conditions, allowed)
    if "flyFrom" in conditions:
        merged["flyFrom"] = _condition_value(conditions, "flyFrom")
    if "flyTo" in conditions:
        merged["flyTo"] = _condition_value(conditions, "flyTo")
    merged["departureDate"] = _date_for_kiwi(_condition_value(conditions, "departureDate", merged.get("departureDate")), "가는 날짜", required=True)
    if "returnDate" in conditions or merged.get("returnDate"):
        return_value = _condition_value(conditions, "returnDate", merged.get("returnDate"))
        if return_value:
            merged["returnDate"] = _date_for_kiwi(return_value, "오는 날짜")
        else:
            merged.pop("returnDate", None)
    merged["flyFrom"] = _text(merged.get("flyFrom"))
    merged["flyTo"] = _text(merged.get("flyTo"))
    if not merged["flyFrom"] or not merged["flyTo"]:
        raise ReviewSearchError("출발·도착 공항을 모두 입력해주세요.", "INPUT_MISSING_LOCATION")
    payload = {"search": merged}
    if source_section.get("minimum_checked_bag_kg") not in (None, ""):
        payload["minimum_checked_bag_kg"] = source_section["minimum_checked_bag_kg"]
    try:
        args, checked_bag_kg = validate_flight_input(payload)
        raw = KiwiClient().search(args)
        return normalize_flights(raw, args, checked_bag_kg)
    except ReviewSearchError:
        raise
    except (ValueError, TypeError) as exc:
        raise ReviewSearchError(str(exc), "FLIGHT_SEARCH_INVALID") from exc


def _search_hotels(source_section: Mapping[str, Any], conditions: Mapping[str, Any]) -> Dict[str, Any]:
    source_search = _first(source_section, "search", "검색")
    source_search = dict(source_search) if isinstance(source_search, Mapping) else {}
    allowed = (
        "location", "check_in", "check_out", "guests", "rooms", "children_ages", "currency",
        "guest_nationality", "limit", "offset", "radius_meters", "min_star_rating",
        "min_guest_rating", "amenity_ids", "property_type_ids", "ai_search",
    )
    source_search = {key: value for key, value in source_search.items() if key in allowed}
    provider_conditions = {key: value for key, value in conditions.items() if key not in {"budget_per_night_krw", "room_filters"}}
    merged = _merge_allowed(source_search, provider_conditions, allowed)
    for required_key, label in (("location", "숙소 지역"), ("check_in", "체크인"), ("check_out", "체크아웃")):
        value = _text(merged.get(required_key))
        if not value:
            raise ReviewSearchError(f"{label}을 입력해주세요.", "INPUT_MISSING_HOTEL_FIELD")
        merged[required_key] = value
    merged["offset"] = 0
    budget = conditions.get("budget_per_night_krw", source_section.get("budget_per_night_krw"))
    if budget in ("", None):
        budget = None
    room_filters = conditions.get("room_filters", source_section.get("room_filters", {}))
    if room_filters is None:
        room_filters = {}
    payload = {
        "search": merged,
        "budget_per_night_krw": budget,
        "max_pages": source_section.get("max_pages", 100),
        "room_filters": room_filters,
    }
    try:
        args, budget, nights, max_pages, validated_room_filters = validate_hotel_input(payload, include_room_filters=True)
        client = PilotHotelClient()
        raw = client.search(args, max_pages=max_pages)
        result = normalize_hotels(raw, args, budget, nights)
        return apply_room_filters(client, result, args, validated_room_filters)
    except ReviewSearchError:
        raise
    except (ValueError, TypeError) as exc:
        raise ReviewSearchError(str(exc), "HOTEL_SEARCH_INVALID") from exc


def _search_places(source_section: Mapping[str, Any], conditions: Mapping[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        raise ReviewSearchError(
            "장소 재검색은 GOOGLE_PLACES_API_KEY가 설정된 환경에서 사용할 수 있어요. 현재 후보는 호스트 웹 검색 결과입니다.",
            "PLACE_SEARCH_REQUIRES_HOST",
            422,
        )
    source_search = _first(source_section, "search", "검색", "검색 설정")
    source_search = dict(source_search) if isinstance(source_search, Mapping) else {}
    query = _text(_condition_value(conditions, "query", source_search.get("query") or source_section.get("검색어")))
    destination = _text(_condition_value(conditions, "destination", source_search.get("destination") or source_section.get("목적지")))
    if not query:
        raise ReviewSearchError("장소 검색어를 입력해주세요.", "INPUT_MISSING_PLACE_QUERY")
    rank = _text(_condition_value(conditions, "rank_preference", source_search.get("rank_preference") or "RELEVANCE")).upper()
    included_type = _text(_condition_value(conditions, "included_type", source_search.get("included_type") or ""))
    open_now = conditions.get("open_now", source_search.get("open_now"))
    if open_now in ("", None):
        open_now = None
    try:
        page = prepare_google_place_candidate_page(
            query,
            api_key,
            destination,
            page_size=10,
            language_code=_text(source_search.get("language_code") or "ko"),
            region_code=_text(source_search.get("region_code") or ""),
            open_now=open_now,
            rank_preference=rank or "RELEVANCE",
            included_type=included_type,
            location_bias=source_search.get("location_bias") or {},
            summary_only=bool(source_search.get("summary_only", False)),
        )
    except (ValueError, TypeError) as exc:
        raise ReviewSearchError(str(exc), "PLACE_SEARCH_INVALID") from exc
    return {
        "provider": page.get("검색 제공자") or "Google Places API (New)",
        "status": "candidates" if page.get("후보") else "no_matches",
        "searched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "search": {
            "query": query,
            "destination": destination,
            "rank_preference": rank or "RELEVANCE",
            "included_type": included_type,
            "open_now": open_now,
        },
        "candidates": page.get("후보", []),
        "rejected_count": 0,
        "coverage": "provider_returned_results_only",
        "notion_written": False,
    }


def requery_source_packet(source: Mapping[str, Any], section: str, conditions: Mapping[str, Any]) -> Dict[str, Any]:
    """Re-run one provider search and return a new source-shaped packet."""
    canonical = _canonical_section(section)
    if not canonical:
        raise ReviewSearchError("지원하지 않는 검색 파트입니다.", "INPUT_UNKNOWN_SECTION")
    source_section = _section_from_source(source, canonical)
    if canonical == "flights":
        result = _search_flights(source_section, conditions)
    elif canonical == "hotels":
        result = _search_hotels(source_section, conditions)
    else:
        result = _search_places(source_section, conditions)
    return _replace_source_section(source, canonical, result)


def _selection_records(packet: Mapping[str, Any], selected: Sequence[Any]) -> List[Dict[str, str]]:
    index: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for section, payload in (packet.get("sections") or {}).items():
        if not isinstance(payload, Mapping):
            continue
        for candidate in payload.get("candidates", []):
            if isinstance(candidate, Mapping):
                index[(str(section), _text(candidate.get("id")))] = candidate
    records: List[Dict[str, str]] = []
    seen = set()
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        section = _canonical_section(item.get("section")) or _text(item.get("section"))
        identifier = _text(item.get("id"))
        candidate = index.get((section, identifier))
        if candidate is None or (section, identifier) in seen:
            continue
        seen.add((section, identifier))
        records.append({
            "section": section,
            "id": identifier,
            "title": _text(candidate.get("title")),
            "status": "선택·예약 전",
        })
    return records


def save_selection(path: Path, packet: Mapping[str, Any], selected: Sequence[Any]) -> Dict[str, Any]:
    records = _selection_records(packet, selected)
    payload = {
        "version": PACKET_VERSION,
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "selection_status": "선택·예약 전",
        "notion_written": False,
        "booking_completed": False,
        "selections": records,
    }
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)
    return payload


class CandidateReviewHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying only one in-memory, read-only review packet."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], packet: Dict[str, Any], output_path: Optional[Path] = None,
                 source_packet: Optional[Mapping[str, Any]] = None):
        super().__init__(address, CandidateReviewHandler)
        self.packet = packet
        self.source_packet = copy.deepcopy(dict(source_packet or packet))
        self.output_path = output_path
        self.selection: List[Dict[str, str]] = []
        self.selection_payload: Optional[Dict[str, Any]] = None
        self.state_lock = threading.Lock()
        self.search_lock = threading.Lock()

    def requery(self, section: str, conditions: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.search_lock.acquire(blocking=False):
            raise ReviewSearchError("이미 다른 검색을 진행하고 있어요. 잠시 후 다시 시도해주세요.", "SEARCH_IN_PROGRESS", 409)
        try:
            updated_source = requery_source_packet(self.source_packet, section, conditions)
            updated_packet = normalize_packet(updated_source)
            with self.state_lock:
                self.source_packet = updated_source
                self.packet = updated_packet
                # A new search can invalidate every old candidate ID.  Keep
                # the same page open but require a fresh explicit selection.
                self.selection = []
                self.selection_payload = None
            return updated_packet
        finally:
            self.search_lock.release()

    def submit_selection(self, selected: Sequence[Any]) -> Dict[str, Any]:
        with self.state_lock:
            records = _selection_records(self.packet, selected)
            self.selection = records
            if self.output_path:
                payload = save_selection(self.output_path, self.packet, selected)
            else:
                payload = {
                    "version": PACKET_VERSION,
                    "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "selection_status": "선택·예약 전",
                    "notion_written": False,
                    "booking_completed": False,
                    "selections": records,
                }
            self.selection_payload = payload
            return payload


class CandidateReviewHandler(BaseHTTPRequestHandler):
    server: CandidateReviewHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # The CLI prints one machine-readable READY line; request logs would
        # make it difficult for a host to parse, so keep the local server quiet.
        return

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self._headers(content_type, len(data))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self._serve_asset("candidate-review.html", "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json({"status": "READY", "review_only": True, "search_enabled": True})
            return
        if path == "/api/results":
            packet = dict(self.server.packet)
            with self.server.state_lock:
                packet["selected"] = list(self.server.selection)
            self._send_json(packet)
            return
        if path in {"/candidate-review.css", "/candidate-review.js"}:
            filename = path.lstrip("/")
            content_type = "text/css; charset=utf-8" if filename.endswith(".css") else "text/javascript; charset=utf-8"
            self._serve_asset(filename, content_type)
            return
        self._send_json({"error": "NOT_FOUND"}, 404)

    def _serve_asset(self, filename: str, content_type: str) -> None:
        asset = (WEB_DIR / filename).resolve()
        if WEB_DIR.resolve() not in asset.parents or not asset.is_file():
            self._send_json({"error": "ASSET_NOT_FOUND"}, 404)
            return
        try:
            self._send_bytes(asset.read_bytes(), content_type)
        except OSError:
            self._send_json({"error": "ASSET_READ_ERROR"}, 500)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/api/selection", "/api/search"}:
            self._send_json({"error": "NOT_FOUND"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json({"error": "PAYLOAD_TOO_LARGE"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "INVALID_JSON"}, 400)
            return
        if not isinstance(body, Mapping):
            self._send_json({"error": "INVALID_JSON_OBJECT"}, 400)
            return
        if path == "/api/search":
            section = _canonical_section(body.get("section"))
            conditions = body.get("conditions")
            if not section or not isinstance(conditions, Mapping):
                self._send_json({"error": "SEARCH_REQUEST_INVALID", "message": "검색 파트와 조건을 확인해주세요."}, 400)
                return
            try:
                packet = self.server.requery(section, conditions)
            except ReviewSearchError as exc:
                self._send_json({"error": exc.code, "message": str(exc)}, exc.status)
                return
            except Exception:
                # Provider adapters already sanitize expected errors.  Never
                # expose a traceback or request payload through this endpoint.
                self._send_json({"error": "SEARCH_FAILED", "message": "검색을 완료하지 못했어요. 조건을 확인하고 다시 시도해주세요."}, 502)
                return
            self._send_json({
                "status": "results_updated",
                "section": section,
                "results": packet,
                "selection_reset": True,
                "same_site": True,
            })
            return
        selected = body.get("selected")
        if not isinstance(selected, list):
            self._send_json({"error": "SELECTED_LIST_REQUIRED"}, 400)
            return
        payload = self.server.submit_selection(selected)
        self._send_json({
            "status": "selection_recorded",
            "selection_count": len(payload["selections"]),
            "selection_status": payload["selection_status"],
            "notion_written": False,
            "booking_completed": False,
            "output_path": str(self.server.output_path) if self.server.output_path else None,
        })


def build_server(packet: Dict[str, Any], host: str = "127.0.0.1", port: int = 0,
                 output_path: Optional[Path] = None, source_packet: Optional[Mapping[str, Any]] = None) -> CandidateReviewHTTPServer:
    return CandidateReviewHTTPServer((host, port), packet, output_path=output_path, source_packet=source_packet)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Serve Souvenir's local candidate review board")
    parser.add_argument("--input", type=Path, required=True, help="후보 JSON 또는 sections 패킷")
    parser.add_argument("--kind", choices=("flights", "hotels", "places"), help="candidates만 있을 때의 파트")
    parser.add_argument("--output", type=Path, help="선택 제출 JSON을 기록할 경로")
    parser.add_argument("--host", default="127.0.0.1", help="기본값: 로컬 컴퓨터에서만 접근")
    parser.add_argument("--port", type=int, default=0, help="포트, 0이면 사용 가능한 포트 자동 선택")
    parser.add_argument("--open", action="store_true", help="준비된 URL을 기본 브라우저에서 열기")
    parser.add_argument("--pretty", action="store_true", help="READY 출력을 보기 좋게 표시")
    options = parser.parse_args(argv)
    try:
        raw = json.loads(options.input.read_text(encoding="utf-8-sig"))
        packet = normalize_packet(raw, explicit_kind=options.kind)
        if not (0 <= options.port <= 65535):
            raise ValueError("포트는 0~65535 범위여야 합니다.")
        server = build_server(packet, host=options.host, port=options.port, output_path=options.output, source_packet=raw)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc), "review_only": True}, ensure_ascii=False), file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    ready = {"status": "READY", "web_url": url, "review_only": True, "sections": list(packet["sections"])}
    print(json.dumps(ready, ensure_ascii=False, indent=2 if options.pretty else None), flush=True)
    if options.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
