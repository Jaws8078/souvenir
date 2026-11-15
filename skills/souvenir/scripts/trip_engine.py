#!/usr/bin/env python3
"""Pure-Python travel checks, calculations, import helpers, and Notion refresh views.

The engine is deliberately separate from the template installer.  It can read a
JSON snapshot for offline use or query the databases created by
``notion_installer.py`` when a Notion token and installation mapping are supplied.
It never changes a Notion page during checks, calculations, imports, or refreshes.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


TIME_RANGE_RE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*[-~–]\s*(?P<end>\d{1,2}:\d{2})")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
URL_RE = re.compile(r"https?://[^\s|]+", re.I)
SHORT_MAP_HOSTS = {"maps.app.goo.gl", "goo.gl"}
SHOPPING_REQUIRED_FIELDS = ("구매 장소", "수량", "예상 금액")
INPUT_UNKNOWN = "미정"

# 대화에서 받은 값과 Notion 속성 사이의 단일 계약이다.  호스트는 이
# 목록을 먼저 사용자에게 보여주고, 답변은 아래 ``property`` 이름으로
# 분리해 저장해야 한다.  특히 사람별 정보는 ``의견·요청`` 같은 합친
# 열을 사용하지 않는다.
INPUT_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "trip": {
        "label": "기본 여행 정보",
        "databases": ["trip"],
        "fields": [
            {"name": "여행지", "property": "목적지", "required": True},
            {"name": "출발일", "property": "출발일", "required": False},
            {"name": "종료일", "property": "종료일", "required": False},
            {"name": "여행 기간(일)", "property": "기간(일)", "required": False},
            {"name": "인원 수", "property": "인원", "required": False},
            {"name": "기준 통화", "property": "기준 통화", "required": False},
            {"name": "여행 템포", "property": "여행 템포", "required": False},
            {"name": "여행 테마", "property": "여행 테마", "required": False},
            {"name": "가장 중요한 기준", "property": "가장 중요한 기준", "required": False},
            {"name": "메모", "property": "메모", "required": False},
        ],
    },
    "traveler": {
        "label": "여행 개요·취향 (사람별)",
        "databases": ["traveler"],
        "fields": [
            {"name": "이름", "property": "여행자", "required": True},
            {"name": "역할", "property": "역할", "required": True},
            {"name": "먹고 싶은 음식", "property": "먹고 싶은 음식", "required": True},
            {"name": "선호 이동", "property": "선호 이동", "required": True},
            {"name": "숙소 취향", "property": "숙소 취향", "required": True},
            {"name": "주의사항", "property": "주의사항", "required": True},
            {"name": "추가 요청", "property": "추가 요청", "required": True},
            {"name": "담당 영역", "property": "담당 영역", "required": False},
            {"name": "개인 예산", "property": "개인 예산", "required": False},
            {"name": "예산 통화", "property": "예산 통화", "required": False},
            {"name": "메모", "property": "메모", "required": False},
        ],
    },
    "itinerary": {
        "label": "날짜별 일정",
        "databases": ["itinerary"],
        "fields": [
            {"name": "일정명", "property": "일정", "required": True},
            {"name": "일차", "property": "일차", "required": True},
            {"name": "날짜", "property": "날짜", "required": False},
            {"name": "시작 시각", "property": "시작", "required": False},
            {"name": "종료 시각", "property": "종료", "required": False},
            {"name": "장소", "property": "장소", "required": False},
            {"name": "예약·메모", "property": "예약·메모", "required": False},
            {"name": "활동 상태", "property": "상태", "required": False},
            {"name": "플랜 B", "property": "플랜 B", "required": False},
            {"name": "플랜 C", "property": "플랜 C", "required": False},
        ],
    },
    "air": {
        "label": "항공",
        "databases": ["long_transport", "reservation"],
        "fields": [
            {"name": "구간", "property": "구간", "required": True},
            {"name": "출발 공항", "property": "출발 공항", "required": True},
            {"name": "도착 공항", "property": "도착 공항", "required": True},
            {"name": "운항사", "property": "운항사", "required": False},
            {"name": "편명", "property": "편명", "required": False},
            {"name": "출발 일시", "property": "출발", "required": False},
            {"name": "도착 일시", "property": "도착", "required": False},
            {"name": "선호 시간대", "property": "선호 시간대", "required": False},
            {"name": "항공권 예산(KRW)", "property": "항공권 예산", "required": False},
            {"name": "후보 상태", "property": "후보 상태", "required": False},
            {"name": "예약 상태", "property": "예약 상태", "required": False},
            {"name": "예약번호", "property": "예약번호", "required": False},
            {"name": "확인 링크", "property": "확인 링크", "required": False},
            {"name": "좌석·수하물", "property": "좌석·수하물", "required": False},
            {"name": "체크인·탑승 마감", "property": "체크인·탑승 마감", "required": False},
        ],
    },
    "stay": {
        "label": "숙소",
        "databases": ["stay", "reservation"],
        "fields": [
            {"name": "숙소명", "property": "숙소", "required": True},
            {"name": "주소", "property": "주소", "required": False},
            {"name": "숙소 링크", "property": "숙소 링크", "required": False},
            {"name": "체크인", "property": "체크인", "required": False},
            {"name": "체크아웃", "property": "체크아웃", "required": False},
            {"name": "장점", "property": "장점", "required": False},
            {"name": "단점", "property": "단점", "required": False},
            {"name": "1박 비용(KRW)", "property": "1박 비용", "required": False},
            {"name": "숙박 총액(KRW)", "property": "총액", "required": False},
            {"name": "예약번호", "property": "예약번호", "required": False},
            {"name": "지도 URL", "property": "지도 URL", "required": False},
        ],
    },
    "place": {
        "label": "장소 보관함",
        "databases": ["place"],
        "fields": [
            {"name": "장소명", "property": "장소", "required": True},
            {"name": "지역", "property": "지역", "required": False},
            {"name": "종류", "property": "종류", "required": False},
            {"name": "지도 URL", "property": "지도 URL", "required": False},
            {"name": "공식 링크", "property": "공식 링크", "required": False},
            {"name": "사진 URL", "property": "사진 URL", "required": False},
            {"name": "예산 메모", "property": "예산 메모", "required": False},
            {"name": "영업시간·휴무", "property": "영업시간·휴무", "required": False},
            {"name": "출처", "property": "출처", "required": False},
            {"name": "개인 평점", "property": "개인 평점", "required": False},
            {"name": "방문 완료", "property": "방문 완료", "required": False},
            {"name": "메모", "property": "메모", "required": False},
        ],
    },
    "shopping": {
        "label": "쇼핑",
        "databases": ["shopping", "expense"],
        "fields": [
            {"name": "품목명", "property": "품목", "required": True},
            {"name": "담당 여행자", "property": "담당 여행자", "required": False},
            {"name": "구매 장소", "property": "구매 장소", "required": True},
            {"name": "수량", "property": "수량", "required": True},
            {"name": "예상 금액", "property": "예상 금액", "required": True},
            {"name": "통화", "property": "통화", "required": False},
            {"name": "사진 URL", "property": "사진 URL", "required": False},
            {"name": "태그", "property": "태그", "required": False},
            {"name": "결제 수단", "property": "결제 수단", "required": False},
            {"name": "상태", "property": "상태", "required": False},
            {"name": "링크·메모", "property": "링크·메모", "required": False},
        ],
    },
    "budget": {
        "label": "예산·여행 지갑",
        "databases": ["wallet", "budget", "expense", "split", "settlement"],
        "fields": [
            {"name": "지갑 이름", "property": "지갑", "required": True},
            {"name": "통화", "property": "통화", "required": True},
            {"name": "준비 금액", "property": "준비 금액", "required": True},
            {"name": "사용처", "property": "사용처", "required": False},
            {"name": "거래 유형", "property": "거래 유형", "required": False},
            {"name": "결제 단계", "property": "결제 단계", "required": False},
            {"name": "결제 수단", "property": "결제 수단", "required": False},
            {"name": "실제 사용 금액", "property": "금액", "required": False},
            {"name": "예산 카테고리", "property": "카테고리", "required": False},
            {"name": "예산 범위", "property": "예산 범위", "required": False},
            {"name": "담당 여행자", "property": "결제자", "required": False},
        ],
    },
    "preparation": {
        "label": "준비물·준비할 일",
        "databases": ["task", "packing"],
        "fields": [
            {"name": "항목", "property": "할 일", "required": True},
            {"name": "시점", "property": "시점", "required": False},
            {"name": "상태", "property": "상태", "required": False},
            {"name": "중요도", "property": "중요도", "required": False},
            {"name": "담당자", "property": "담당자", "required": False},
            {"name": "기한", "property": "기한", "required": False},
            {"name": "공용·개인", "property": "공용·개인", "required": False},
            {"name": "분류", "property": "분류", "required": False},
            {"name": "필수", "property": "필수", "required": False},
            {"name": "수량", "property": "수량", "required": False},
            {"name": "구매 필요", "property": "구매 필요", "required": False},
        ],
    },
}

PART_ALIASES = {
    "기본 정보": "trip", "기본 여행 정보": "trip", "여행 정보": "trip", "여행": "trip", "여행 개요": "traveler",
    "여행 개요·취향": "traveler", "취향": "traveler", "동행자": "traveler",
    "일정": "itinerary", "날짜별 일정": "itinerary", "항공": "air",
    "항공편": "air", "숙소": "stay", "숙소 후보": "stay", "장소": "place",
    "장소 보관함": "place", "쇼핑": "shopping", "예산": "budget",
    "여행 지갑": "budget", "예산·여행 지갑": "budget", "준비물": "preparation",
    "준비할 일": "preparation", "준비물·준비할 일": "preparation",
}


def input_fields_for(part: str) -> Dict[str, Any]:
    """Return the explicit question/storage contract for one plan part."""
    key = PART_ALIASES.get(text(part), text(part).strip().lower())
    if key not in INPUT_CONTRACTS:
        raise ValueError(f"알 수 없는 입력 파트입니다: {part}")
    contract = INPUT_CONTRACTS[key]
    return json.loads(json.dumps({"part": key, **contract}, ensure_ascii=False))


def _input_value(row: Dict[str, Any], field: Dict[str, Any]) -> Any:
    property_name = field["property"]
    aliases = (field["name"], property_name)
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def _canonical_input_row(part: str, row: Dict[str, Any]) -> Dict[str, Any]:
    contract = input_fields_for(part)
    result: Dict[str, Any] = {}
    for field in contract["fields"]:
        raw = _input_value(row, field)
        if raw not in (None, ""):
            result[field["property"]] = raw
    # The title is a Notion title property, so preserve it under its exact
    # schema name instead of hiding it in a generic note.
    if part == "traveler" and "여행자" not in result and row.get("이름"):
        result["여행자"] = row["이름"]
    return result


def parse_traveler_profiles(content: Any, traveler_names: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Parse person-by-person answers into independent Notion properties.

    Accepted text format is one person per line:
    ``이름 | 역할 | 먹고 싶은 음식 | 선호 이동 | 숙소 취향 | 주의사항 | 추가 요청``.
    Missing answers are kept as ``미정``.  A legacy combined ``의견·요청`` key
    is rejected rather than silently mixing unrelated answers.
    """
    rows: List[Any]
    if isinstance(content, str):
        raw = content.strip()
        if not raw:
            return []
        if raw[:1] in "[{":
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("동행자 입력 JSON을 읽지 못했습니다.") from exc
            rows = loaded if isinstance(loaded, list) else [loaded]
        else:
            rows = [line.strip() for line in raw.splitlines() if line.strip()]
    elif isinstance(content, dict):
        rows = [content]
    elif isinstance(content, (list, tuple)):
        rows = list(content)
    else:
        raise ValueError("동행자 입력은 줄 목록, 객체, JSON 배열 중 하나여야 합니다.")

    fields = INPUT_CONTRACTS["traveler"]["fields"][:7]
    names = list(traveler_names)
    parsed: List[Dict[str, Any]] = []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            pieces = [part.strip() for part in item.split("|")]
            if index == 0 and pieces and pieces[0] in {field["name"] for field in fields}:
                # Allow the copyable header line from the conversation prompt.
                continue
            row = {field["property"]: pieces[position]
                   for position, field in enumerate(fields) if position < len(pieces) and pieces[position]}
            if not row.get("여행자") and index < len(names):
                row["여행자"] = names[index]
        elif isinstance(item, dict):
            if "의견·요청" in item:
                raise ValueError("의견·요청은 자동 분해하지 않습니다. 역할·음식·이동·숙소·주의사항·추가 요청을 각각 입력하세요.")
            row = _canonical_input_row("traveler", item)
        else:
            raise ValueError("동행자 각 입력은 문자열 또는 객체여야 합니다.")
        if not row.get("여행자"):
            row["여행자"] = names[index] if index < len(names) else f"동행자 {index + 1}"
        for field in fields[1:]:
            row.setdefault(field["property"], INPUT_UNKNOWN)
        parsed.append(row)
    return parsed


def normalize_part_input(part: str, payload: Any, *, start_date: Optional[str] = None,
                         duration_days: Optional[int] = None) -> Dict[str, Any]:
    """Normalize one conversation answer and report its exact DB destination.

    This function is intentionally write-free.  The host can show ``questions``
    before asking, pass the answer here, then route each returned row to the
    listed Notion database.  It prevents a combined chat answer from being
    stored in an unrelated column.
    """
    key = input_fields_for(part)["part"]
    contract = input_fields_for(key)
    if key == "traveler":
        rows = parse_traveler_profiles(payload)
    elif key == "itinerary":
        if isinstance(payload, str):
            rows = organize_schedule(payload, start_date, duration_days)
        elif isinstance(payload, dict):
            rows = [_canonical_input_row(key, payload)]
        elif isinstance(payload, (list, tuple)):
            rows = [_canonical_input_row(key, item) for item in payload if isinstance(item, dict)]
        else:
            raise ValueError("일정 입력은 텍스트 또는 행 객체 목록이어야 합니다.")
    elif key == "place":
        rows = parse_place_lines(payload) if isinstance(payload, str) else [
            _canonical_input_row(key, item) for item in payload if isinstance(item, dict)
        ]
    elif key == "shopping":
        rows = parse_shopping_lines(payload) if isinstance(payload, str) else [
            _canonical_input_row(key, item) for item in payload if isinstance(item, dict)
        ]
    elif isinstance(payload, dict):
        rows = [_canonical_input_row(key, payload)]
    elif isinstance(payload, (list, tuple)):
        rows = [_canonical_input_row(key, item) for item in payload if isinstance(item, dict)]
    else:
        raise ValueError(f"{contract['label']} 입력은 필드별 객체 또는 객체 목록이어야 합니다.")
    required_properties = [field["property"] for field in contract["fields"] if field.get("required")]
    missing = sorted({field for row in rows for field in required_properties if row.get(field) in (None, "")})
    return {
        "part": key,
        "label": contract["label"],
        "databases": contract["databases"],
        "questions": [field["name"] for field in contract["fields"]],
        "rows": rows,
        "missing": missing,
        "status": "확인 필요" if missing else "입력 완료",
    }

ALIASES = {
    "title": ("title", "이름", "일정", "예약", "숙소", "내역", "장소", "항공편", "자료", "지갑", "여행자", "분담", "송금", "할 일", "준비물", "예산 항목", "서비스", "이동 구간", "결정", "날씨 확인", "검사", "품목"),
    "travel_key": ("travel_key", "여행 키", "trip_id", "trip_key"),
    "date": ("date", "날짜", "이용 일시", "이용 날짜"),
    "start": ("start", "시작", "출발"),
    "end": ("end", "종료", "도착"),
    "day": ("day", "일차"),
    "place": ("place", "장소", "도착지"),
    "shopping_place": ("shopping_place", "구매 장소", "쇼핑 장소"),
    "status": ("status", "상태", "예약 상태", "거래 상태", "숙소 상태"),
    "amount": ("amount", "금액", "사용액", "준비 금액", "결제액", "부담액", "예산"),
    "currency": ("currency", "통화", "예산 통화"),
    "category": ("category", "카테고리", "예약 대상", "분류", "거래 유형"),
    "wallet": ("wallet", "지갑", "지갑 키"),
    "payer": ("payer", "결제자", "보낸 사람"),
    "person": ("person", "여행자", "부담자", "받은 사람"),
}


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def value(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    for candidate in ALIASES.get(key, (key,)):
        if candidate in row and row[candidate] not in (None, ""):
            return row[candidate]
    return default


def text(value_: Any) -> str:
    return "" if value_ is None else str(value_).strip()


def number(value_: Any, default: Optional[float] = None) -> Optional[float]:
    if value_ in (None, ""):
        return default
    if isinstance(value_, bool):
        return default
    try:
        return float(str(value_).replace(",", "").replace("₩", "").strip())
    except (TypeError, ValueError):
        return default


def parse_datetime(value_: Any) -> Optional[dt.datetime]:
    if value_ is None or value_ == "":
        return None
    if isinstance(value_, dt.datetime):
        return value_
    if isinstance(value_, dt.date):
        return dt.datetime.combine(value_, dt.time())
    raw = text(value_).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(raw), dt.time())
        except ValueError:
            return None


def parse_date(value_: Any) -> Optional[dt.date]:
    parsed = parse_datetime(value_)
    return parsed.date() if parsed else None


def parse_clock(value_: Any) -> Optional[dt.time]:
    raw = text(value_)
    if not TIME_RE.match(raw):
        return None
    try:
        parts = [int(part) for part in raw.split(":")]
        return dt.time(parts[0], parts[1], parts[2] if len(parts) == 3 else 0)
    except (ValueError, IndexError):
        return None


def event_datetime(row: Dict[str, Any], key: str) -> Optional[dt.datetime]:
    raw = value(row, key)
    parsed = parse_datetime(raw)
    if parsed:
        return parsed
    clock = parse_clock(raw)
    date_value = parse_date(value(row, "date"))
    return dt.datetime.combine(date_value, clock) if date_value and clock else None


def active(row: Dict[str, Any]) -> bool:
    return text(value(row, "status")).lower() not in {"취소", "건너뜀", "제외", "cancelled", "skipped"}


def row_id(row: Dict[str, Any]) -> str:
    return text(row.get("id") or value(row, "title"))


def parse_day_prefix(line: str) -> Tuple[Optional[int], str]:
    match = re.match(r"^\s*(\d+)\s*일차\s*(?:(?:[:.)-])\s*|\|\s*)?", line)
    if not match:
        return None, line.strip()
    day = int(match.group(1))
    cleaned = line[match.end():].strip(" :-.)|\t")
    return day, cleaned


def organize_schedule(content: str, start_date: Optional[str] = None,
                      duration_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Turn compact Korean schedule text into normalized itinerary records."""
    if not text(content):
        return []
    base = parse_date(start_date)
    current_day = 1
    records: List[Dict[str, Any]] = []
    lines = [line.strip(" -*\t") for line in re.split(r"[\r\n]+", content) if line.strip(" -*\t")]
    for line in lines:
        day, cleaned = parse_day_prefix(line)
        if day is not None:
            current_day = day
        if not cleaned:
            continue
        fields = [part.strip() for part in re.split(r"\s*\|\s*", cleaned)]
        title = fields[0]
        time_value = fields[1] if len(fields) > 1 else ""
        place = fields[2] if len(fields) > 2 else ""
        note = fields[3] if len(fields) > 3 else ""
        if parse_clock(title) or TIME_RANGE_RE.search(title):
            # Compact form: ``2일차 | 09:00-11:00 | 오호리 공원``.
            time_value = title
            compact_place = fields[1] if len(fields) > 1 else ""
            title = compact_place or "일정"
            place = compact_place
            note = fields[4] if len(fields) > 4 else ""
        range_match = TIME_RANGE_RE.search(time_value)
        start_clock = range_match.group("start") if range_match else (time_value if parse_clock(time_value) else "")
        end_clock = range_match.group("end") if range_match else ""
        record: Dict[str, Any] = {"일정": title, "일차": current_day}
        if base and (duration_days is None or current_day <= duration_days):
            record["날짜"] = (base + dt.timedelta(days=current_day - 1)).isoformat()
        if start_clock:
            record["시작"] = f"{record.get('날짜', '')}T{start_clock}:00" if record.get("날짜") else start_clock
        if end_clock:
            record["종료"] = f"{record.get('날짜', '')}T{end_clock}:00" if record.get("날짜") else end_clock
        if place:
            record["장소"] = place
        if note:
            record["예약·메모"] = note
        records.append(record)
    return records


def parse_place_lines(content: str, *, resolve_short_links: bool = True,
                      link_timeout: float = 8.0) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in re.split(r"[\r\n]+", content):
        cleaned = line.strip(" -*\t")
        if not cleaned:
            continue
        fields = [part.strip() for part in re.split(r"\s*\|\s*", cleaned)]
        urls = URL_RE.findall(cleaned)
        link_info = normalize_link(urls[0], resolve_short_links=resolve_short_links,
                                   timeout=link_timeout) if urls else None
        explicit_name = fields[0] if not (urls and URL_RE.fullmatch(fields[0])) else ""
        place_name = explicit_name or (link_info or {}).get("label", "")
        record: Dict[str, Any] = {"장소": place_name or "확인 필요"}
        if len(fields) > 1 and fields[1]:
            record["지역"] = fields[1]
        if len(fields) > 2 and fields[2]:
            record["종류"] = fields[2]
        if urls:
            record["지도 URL"] = link_info["url"]
            if link_info.get("resolution_status") == "needs_confirmation":
                record["정보 상태"] = "확인 필요"
                record["추가 확인"] = ["장소명" if not explicit_name else "지도 링크 확인"]
        if len(fields) > 3 and fields[3] and not urls:
            record["메모"] = fields[3]
        elif len(fields) > 4 and fields[4]:
            record["메모"] = fields[4]
        records.append(record)
    return records


def _location_token(value_: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text(value_).lower())


def _place_match_status(destination: str, region: str,
                        evidence: Optional[Dict[str, Any]] = None) -> str:
    """Return a conservative destination check for a place candidate."""
    evidence = evidence or {}
    explicit = evidence.get("destination_match", evidence.get("여행지 일치"))
    if isinstance(explicit, bool):
        return "확인 완료" if explicit else "불일치"
    if text(evidence.get("matched_destination")):
        matched = _location_token(evidence.get("matched_destination"))
        destination_token = _location_token(destination)
        if matched and destination_token and (destination_token in matched or matched in destination_token):
            return "확인 완료"
        if matched:
            return "불일치"
    destination_token = _location_token(destination)
    region_token = _location_token(region)
    if destination_token and region_token:
        if destination_token in region_token or region_token in destination_token:
            return "확인 완료"
        # A short district name such as ``하카타`` cannot be safely compared to
        # ``후쿠오카`` without geocoding evidence.  Keep it pending rather than
        # falsely rejecting a valid place.
    return "확인 필요"


def prepare_place_confirmation(content: str, destination: str = "",
                              evidence: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Prepare a place list for destination review before a Notion write.

    ``evidence`` is supplied by the host's web/Maps lookup and may contain a
    place name, ``destination_match`` boolean, ``matched_destination`` and a
    source URL.  Without evidence the result remains ``확인 필요``; this keeps
    an odd or unrelated link out of the saved place library.
    """
    rows = parse_place_lines(content)
    destination_text = text(destination)
    evidence_by_name: Dict[str, Dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        name = text(item.get("장소") or item.get("name") or item.get("place"))
        if name:
            evidence_by_name[_location_token(name)] = item

    confirmed_rows: List[Dict[str, Any]] = []
    needs_review: List[Dict[str, Any]] = []
    for row in rows:
        evidence_item = evidence_by_name.get(_location_token(row.get("장소")))
        status = _place_match_status(destination_text, text(row.get("지역")), evidence_item)
        if evidence_item:
            source = text(evidence_item.get("출처") or evidence_item.get("source") or evidence_item.get("url"))
            if source and not row.get("출처"):
                row["출처"] = source
            if evidence_item.get("지역") and not row.get("지역"):
                row["지역"] = text(evidence_item["지역"])
        row["여행지 확인"] = status
        row["저장 가능"] = status == "확인 완료"
        if status == "확인 완료":
            confirmed_rows.append(row)
        else:
            row["추가 확인"] = ["여행지와 장소의 일치 여부"]
            needs_review.append(row)
    return {
        "목적지": destination_text,
        "장소": rows,
        "확인 완료": confirmed_rows,
        "검토 필요": needs_review,
        "저장 가능": bool(rows) and not needs_review,
        "확인 질문": (
            "여행지와 맞지 않거나 확인되지 않은 장소가 있습니다. 확인 후 저장할까요?"
            if needs_review else "이 장소 목록을 저장할까요?"
        ),
    }


def confirm_place_rows(packet: Dict[str, Any], approved_places: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Apply an explicit user approval to pending place candidates."""
    if not isinstance(packet, dict):
        raise ValueError("장소 확인 패킷이 올바르지 않습니다.")
    approved = {_location_token(item) for item in approved_places if text(item)}
    result: List[Dict[str, Any]] = []
    for raw in packet.get("장소", []):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if _location_token(row.get("장소")) in approved:
            row["여행지 확인"] = "사용자 확인 완료"
            row["저장 가능"] = True
            row.pop("추가 확인", None)
        if row.get("저장 가능"):
            result.append(row)
    return result


def parse_shopping_lines(content: str) -> List[Dict[str, Any]]:
    """Parse compact shopping rows and flag missing confirmation fields.

    The original seven-column format remains supported::

        품목 | 담당자 | 구매 장소 | 태그 | 예상 금액 | 결제 수단 | 메모

    New input can put the two fields the wizard explicitly asks for first::

        품목 | 담당자 | 구매 장소 | 수량 | 예상 금액 | 태그 | 결제 수단 | 메모

    The short chat form also accepts either::

        품목 | 예상 금액 | 수량 | 구매 장소
        품목 | 수량 | 구매 장소 | 예상 금액

    A name-only row is never treated as complete.  ``정보 상태`` and
    ``추가 확인`` tell the caller which values must be confirmed before a
    Notion write.
    """
    records: List[Dict[str, Any]] = []

    def shopping_number(raw: str) -> Optional[float]:
        cleaned = text(raw).replace("개", "").replace("개입", "")
        parsed = number(cleaned)
        if parsed is not None:
            return parsed
        # Accept lightweight user formats such as ``JPY 1,200`` or
        # ``1,200엔`` without treating arbitrary text as a price.
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", cleaned)
        return number(match.group(0)) if match else None

    def compact_number(raw: str) -> Any:
        parsed = shopping_number(raw)
        if parsed is None:
            return raw
        return int(parsed) if parsed.is_integer() else parsed

    for line in re.split(r"[\r\n]+", content):
        cleaned = line.strip(" -*\t")
        if not cleaned:
            continue
        fields = [part.strip() for part in re.split(r"\s*\|\s*", cleaned)]
        record: Dict[str, Any] = {"품목": fields[0]}
        if len(fields) > 1 and fields[1]:
            record["담당 여행자"] = fields[1]
        if len(fields) > 2 and fields[2]:
            record["구매 장소"] = fields[2]

        # Compact wizard input without an explicit 담당 여행자 is also
        # accepted.  The two numeric fields are unambiguous enough for the
        # common ``품목 | 예상 금액 | 수량 | 구매 장소`` form, while the
        # alternate ``품목 | 수량 | 구매 장소 | 예상 금액`` form keeps the
        # quantity-first phrasing natural in chat.
        if len(fields) >= 4 and shopping_number(fields[1]) is not None:
            first = shopping_number(fields[1])
            second = shopping_number(fields[2]) if len(fields) > 2 else None
            third = shopping_number(fields[3]) if len(fields) > 3 else None
            if second is not None and third is None:
                record.pop("담당 여행자", None)
                record.pop("구매 장소", None)
                record["예상 금액"] = compact_number(fields[1])
                record["수량"] = compact_number(fields[2])
                record["구매 장소"] = fields[3]
                if len(fields) > 4 and fields[4]:
                    record["태그"] = fields[4]
                if len(fields) > 5 and fields[5]:
                    record["링크·메모"] = fields[5]
            elif second is None and third is not None:
                record.pop("담당 여행자", None)
                record.pop("구매 장소", None)
                record["수량"] = compact_number(fields[1])
                record["구매 장소"] = fields[2]
                record["예상 금액"] = compact_number(fields[3])
                if len(fields) > 4 and fields[4]:
                    record["태그"] = fields[4]
                if len(fields) > 5 and fields[5]:
                    record["링크·메모"] = fields[5]

        # If column four is numeric, use the explicit wizard format.  The
        # older format starts with a tag (e.g. ``디저트``), so it remains
        # backwards compatible without guessing from a free-text tag.
        elif len(fields) > 3 and shopping_number(fields[3]) is not None:
            record["수량"] = compact_number(fields[3])
            if len(fields) > 4 and fields[4]:
                record["예상 금액"] = compact_number(fields[4])
            if len(fields) > 5 and fields[5]:
                record["태그"] = fields[5]
            if len(fields) > 6 and fields[6]:
                record["결제 수단"] = fields[6]
            if len(fields) > 7 and fields[7]:
                record["링크·메모"] = fields[7]
        else:
            names = ("태그", "예상 금액", "결제 수단", "링크·메모")
            for index, name in enumerate(names, start=3):
                if len(fields) > index and fields[index]:
                    record[name] = fields[index]
            if "예상 금액" in record:
                record["예상 금액"] = compact_number(record["예상 금액"])

            # An optional quantity can be appended to the legacy format:
            # ``... | 결제 수단 | 메모 | 수량``.
            if len(fields) > 7 and shopping_number(fields[7]) is not None:
                record["수량"] = compact_number(fields[7])

        required = SHOPPING_REQUIRED_FIELDS
        missing = [name for name in required if name not in record or record[name] in (None, "")]
        record["정보 상태"] = "확인 필요" if missing else "입력 완료"
        if missing:
            record["추가 확인"] = missing
        records.append(record)
    return records


def _candidate_value(candidate: Dict[str, Any], names: Sequence[str]) -> Any:
    """Read the first non-empty value from a web/search candidate."""
    for name in names:
        value_ = candidate.get(name)
        if value_ not in (None, "", []):
            return value_
    return None


def normalize_shopping_candidate(candidate: Dict[str, Any], item_name: str = "",
                                 default_currency: str = "미정") -> Dict[str, Any]:
    """Normalize an externally researched shopping candidate.

    The search layer is intentionally supplied by the host (web search, an
    image reader, or a user-provided link).  This function only converts the
    untrusted result into a small, auditable row.  It never invents a missing
    price, quantity, or store and never treats a candidate as approved.
    """
    if not isinstance(candidate, dict):
        raise ValueError("쇼핑 후보는 객체여야 합니다.")
    item = text(_candidate_value(candidate, ("품목", "품목명", "name", "item", "title"))) or text(item_name)
    if not item:
        raise ValueError("쇼핑 후보에 품목명이 필요합니다.")

    result: Dict[str, Any] = {"품목": item}
    traveler = _candidate_value(candidate, ("담당 여행자", "담당자", "traveler", "person"))
    place = _candidate_value(candidate, ("구매 장소", "판매처", "매장", "shopping_place", "store", "place"))
    quantity = _candidate_value(candidate, ("수량", "quantity", "qty"))
    amount = _candidate_value(candidate, ("예상 금액", "가격", "price", "amount", "total_price"))
    currency = _candidate_value(candidate, ("통화", "currency")) or default_currency or "미정"
    tag = _candidate_value(candidate, ("태그", "tag", "category"))
    payment = _candidate_value(candidate, ("결제 수단", "payment_method", "payment"))
    status = _candidate_value(candidate, ("상태", "status")) or "구매 예정"
    source = _candidate_value(candidate, ("출처", "source", "source_url", "url", "link"))
    image_url = _candidate_value(candidate, ("사진 URL", "사진", "image_url", "image", "thumbnail"))
    note = _candidate_value(candidate, ("링크·메모", "메모", "note", "notes"))

    if traveler is not None:
        result["담당 여행자"] = text(traveler)
    if place is not None:
        result["구매 장소"] = text(place)
    def candidate_number(raw: Any) -> Optional[float]:
        parsed = number(raw)
        if parsed is not None:
            return parsed
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text(raw))
        return number(match.group(0)) if match else None

    if quantity is not None:
        parsed_quantity = candidate_number(quantity)
        result["수량"] = int(parsed_quantity) if parsed_quantity is not None and parsed_quantity.is_integer() else (parsed_quantity if parsed_quantity is not None else text(quantity))
    if amount is not None:
        parsed_amount = candidate_number(amount)
        result["예상 금액"] = int(parsed_amount) if parsed_amount is not None and parsed_amount.is_integer() else (parsed_amount if parsed_amount is not None else text(amount))
    result["통화"] = text(currency)
    if tag is not None:
        result["태그"] = tag if isinstance(tag, list) else [text(tag)]
    if payment is not None:
        result["결제 수단"] = text(payment)
    if image_url:
        result["사진 URL"] = text(image_url)
    if status in {"아이디어", "구매 예정", "구매 완료", "보류", "제외"}:
        result["상태"] = status
    else:
        result["상태"] = "구매 예정"

    notes: List[str] = []
    if note:
        notes.append(text(note))
    if source:
        source_text = text(source)
        if source_text not in notes:
            notes.append(f"출처: {source_text}")
    if notes:
        result["링크·메모"] = "\n".join(notes)
    return result


def prepare_shopping_research(item_name: str, destination: str = "",
                              candidates: Sequence[Dict[str, Any]] = (),
                              default_currency: str = "미정") -> Dict[str, Any]:
    """Build a confirmation packet for a name-only shopping request.

    A host can use ``검색어`` with its web/image tools, pass the results back
    as ``candidates``, and show this packet before any Notion write.  The
    packet deliberately has no write side effect and marks every candidate as
    awaiting user approval.
    """
    item = text(item_name)
    if not item:
        raise ValueError("쇼핑 품목명을 입력하세요.")
    query = " ".join(part for part in (text(destination), item) if part)
    normalized: List[Dict[str, Any]] = []
    for candidate in candidates:
        row = normalize_shopping_candidate(candidate, item, default_currency)
        missing = [field for field in SHOPPING_REQUIRED_FIELDS
                   if row.get(field) in (None, "")]
        row["정보 상태"] = "확인 필요" if missing else "확인 대기"
        row["추가 확인"] = missing
        row["저장 가능"] = not missing
        normalized.append(row)
    missing = list(SHOPPING_REQUIRED_FIELDS)
    if normalized:
        complete = [row for row in normalized if row.get("저장 가능")]
        if complete:
            missing = []
        else:
            missing = list(dict.fromkeys(field for row in normalized for field in row.get("추가 확인", [])))
    return {
        "품목": item,
        "검색어": query,
        "후보": normalized,
        "상태": "확인 대기" if normalized else "검색 필요",
        "추가 확인": missing,
        "확인 질문": "이 후보 정보로 저장할까요?" if normalized else "검색 결과를 확인한 뒤 후보를 보여드릴까요?",
        "저장 가능": bool(normalized) and any(row.get("저장 가능") for row in normalized),
    }


def confirm_shopping_candidate(item_name: str, candidate: Dict[str, Any],
                               traveler: str = "", default_currency: str = "미정",
                               confirmed: bool = False) -> Dict[str, Any]:
    """Turn one researched candidate into a save-ready, user-confirmed row."""
    row = normalize_shopping_candidate(candidate, item_name, default_currency)
    if traveler and not row.get("담당 여행자"):
        row["담당 여행자"] = text(traveler)
    missing = [field for field in SHOPPING_REQUIRED_FIELDS
               if row.get(field) in (None, "")]
    row["추가 확인"] = missing
    if missing:
        row["정보 상태"] = "확인 필요"
        row["저장 가능"] = False
        row["확인 상태"] = "보완 필요"
    elif not confirmed:
        row["정보 상태"] = "확인 대기"
        row["저장 가능"] = False
        row["확인 상태"] = "사용자 확인 대기"
    else:
        row["정보 상태"] = "사용자 확인 완료"
        row["저장 가능"] = True
        row["확인 상태"] = "승인됨"
    return row


def _clean_map_label(value_: Any) -> str:
    """Decode a Google Maps label without turning coordinates into a name."""
    label = html.unescape(urllib.parse.unquote_plus(text(value_))).strip()
    if not label:
        return ""
    label = re.split(r"(?=@-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)", label, maxsplit=1)[0]
    label = label.split("!", 1)[0]
    label = re.sub(r"\s+", " ", label).strip(" /,;|")
    if not label or label.startswith("@") or label.lower() in {"maps", "place", "search"}:
        return ""
    return label


def _map_label_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("query", "q", "destination", "place", "name"):
        values = query.get(key) or query.get(key.upper())
        if values:
            label = _clean_map_label(values[0])
            if label:
                return label
    segments = [urllib.parse.unquote_plus(segment) for segment in parsed.path.split("/") if segment]
    for marker in ("place", "search"):
        try:
            index = next(index for index, segment in enumerate(segments) if segment.lower() == marker)
        except StopIteration:
            continue
        if index + 1 < len(segments):
            label = _clean_map_label(segments[index + 1])
            if label:
                return label
    return ""


def _resolve_short_map_url(url: str, timeout: float = 8.0) -> Optional[str]:
    """Follow a Google Maps share link and return its final URL."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Souvenir travel planner)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = text(response.geturl())
            if final_url and final_url != url:
                return final_url
            body = response.read(256_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise ValueError(f"지도 링크를 열 수 없습니다 (HTTP {error.code}).") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValueError("지도 링크를 확인하지 못했습니다. 네트워크 연결 또는 링크 상태를 확인해주세요.") from error

    patterns = (
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            candidate = html.unescape(match.group(1)).strip()
            if candidate:
                return urllib.parse.urljoin(url, candidate)
    return None


def _normalize_link(url: str, *, resolve_short_links: bool = True,
                    timeout: float = 8.0) -> Dict[str, Any]:
    raw = text(url).strip("<>()[],.;")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("http 또는 https 링크만 가져올 수 있습니다.")
    host = parsed.netloc.lower().split(":", 1)[0]
    resolved_url = raw
    resolution_status = "not_needed"
    resolution_error = ""
    if resolve_short_links and host in SHORT_MAP_HOSTS:
        try:
            candidate = _resolve_short_map_url(raw, timeout=timeout)
            if candidate:
                resolved_url = candidate
                resolution_status = "resolved"
            else:
                resolution_status = "needs_confirmation"
                resolution_error = "최종 Google Maps 주소를 찾지 못했습니다."
        except ValueError as error:
            resolution_status = "needs_confirmation"
            resolution_error = str(error)
    label = _map_label_from_url(resolved_url)
    if host in SHORT_MAP_HOSTS and not label:
        resolution_status = "needs_confirmation"
        if not resolution_error:
            resolution_error = "장소명을 확인하지 못했습니다."
    result: Dict[str, Any] = {
        "url": raw,
        "host": host,
        "label": label,
        "resolution_status": resolution_status,
    }
    if resolved_url != raw:
        result["resolved_url"] = resolved_url
    if resolution_error:
        result["resolution_error"] = resolution_error
    return result


def normalize_link(url: str, *, resolve_short_links: bool = True,
                   timeout: float = 8.0) -> Dict[str, Any]:
    return _normalize_link(url, resolve_short_links=resolve_short_links, timeout=timeout)



def parse_ics(content: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current.get("일정"):
                records.append(current)
            current = {}
        elif ":" in line:
            key, item = line.split(":", 1)
            if key.startswith("DTSTART") or key.startswith("DTEND"):
                parsed = item.replace("Z", "")
                try:
                    stamp = dt.datetime.strptime(parsed, "%Y%m%dT%H%M%S")
                    current["시작" if key.startswith("DTSTART") else "종료"] = stamp.isoformat()
                except ValueError:
                    try:
                        current["날짜"] = dt.datetime.strptime(parsed, "%Y%m%d").date().isoformat()
                    except ValueError:
                        pass
            elif key == "SUMMARY":
                current["일정"] = item
            elif key == "LOCATION":
                current["장소"] = item
            elif key == "DESCRIPTION":
                current["예약·메모"] = item.replace("\\n", "\n")
    return records


def normalize_csv_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key).strip(): (str(val).strip() if val is not None else "") for key, val in row.items() if key}


def import_file(path: Path, kind: str = "auto") -> List[Dict[str, Any]]:
    """Import UTF-8 CSV/JSON/ICS/text without third-party dependencies."""
    suffix = path.suffix.lower()
    content = path.read_text(encoding="utf-8-sig")
    selected = kind if kind != "auto" else {".csv": "csv", ".json": "json", ".ics": "ics"}.get(suffix, "itinerary")
    if selected in {"csv", "expense"}:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [normalize_csv_row(row) for row in csv.DictReader(handle)]
    if selected == "json":
        loaded = json.loads(content)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        for key in ("records", "rows", "data", "itinerary", "expenses", "places", "shopping"):
            if isinstance(loaded, dict) and isinstance(loaded.get(key), list):
                return [item for item in loaded[key] if isinstance(item, dict)]
        return [loaded] if isinstance(loaded, dict) else []
    if selected == "ics":
        return parse_ics(content)
    if selected == "place":
        return parse_place_lines(content)
    if selected == "shopping":
        return parse_shopping_lines(content)
    if selected == "link":
        return [normalize_link(content.strip())]
    return organize_schedule(content)


def rate_for(currency: str, base_currency: str, rates: Optional[Dict[str, Any]]) -> Optional[float]:
    if currency == base_currency:
        return 1.0
    rates = rates or {}
    for key in (currency, f"{currency}/{base_currency}", f"{currency}_{base_currency}"):
        rate = number(rates.get(key))
        if rate is not None and rate >= 0:
            return rate
    return None


def calculate_budget(wallets: Sequence[Dict[str, Any]], expenses: Sequence[Dict[str, Any]],
                     travelers: Sequence[Dict[str, Any]] = (), splits: Sequence[Dict[str, Any]] = (),
                     base_currency: Optional[str] = None, rates: Optional[Dict[str, Any]] = None,
                     include_planned: bool = False) -> Dict[str, Any]:
    base = base_currency or text(value(wallets[0], "currency")) if wallets else (base_currency or "KRW")
    prepared = 0.0
    spent = 0.0
    unknown: List[Dict[str, Any]] = []
    wallet_report: Dict[str, Dict[str, Any]] = {}
    for wallet in wallets:
        currency = text(value(wallet, "currency")) or base
        amount = number(value(wallet, "amount"), 0.0) or 0.0
        converted = rate_for(currency, base, rates)
        if converted is None:
            unknown.append({"kind": "wallet", "name": row_id(wallet), "currency": currency})
            continue
        key = row_id(wallet)
        wallet_report[key] = {"wallet": key, "currency": currency, "prepared": amount,
                              "spent": 0.0, "remaining": amount, "usage_rate": 0.0}
        prepared += amount * converted
    category: Dict[str, float] = defaultdict(float)
    paid_by: Dict[str, float] = defaultdict(float)
    for expense in expenses:
        status = text(value(expense, "status"))
        if status in {"취소", "예정"} and not (include_planned and status == "예정"):
            continue
        currency = text(value(expense, "currency")) or base
        amount = number(value(expense, "amount"))
        if amount is None:
            continue
        converted = rate_for(currency, base, rates)
        if converted is None:
            unknown.append({"kind": "expense", "name": row_id(expense), "currency": currency})
            continue
        transaction = text(expense.get("거래 유형") or expense.get("transaction_type") or expense.get("type"))
        signed = -abs(amount) if transaction in {"환불", "refund"} else abs(amount)
        converted_amount = signed * converted
        spent += converted_amount
        category_name = text(expense.get("카테고리") or expense.get("예약 대상") or expense.get("분류") or transaction) or "기타"
        category[category_name] += converted_amount
        wallet_key = text(value(expense, "wallet"))
        if wallet_key in wallet_report:
            wallet_report[wallet_key]["spent"] += converted_amount
        payer = text(value(expense, "payer"))
        if payer:
            paid_by[payer] += converted_amount
    for item in wallet_report.values():
        item["remaining"] = item["prepared"] - item["spent"]
        item["usage_rate"] = (item["spent"] / item["prepared"] * 100) if item["prepared"] else 0.0
    for item in wallet_report.values():
        item["spent"] = round(item["spent"], 2)
        item["remaining"] = round(item["remaining"], 2)
        item["usage_rate"] = round(item["usage_rate"], 2)
    settlement = calculate_settlement(paid_by, splits, base, rates)
    return {"base_currency": base, "prepared": round(prepared, 2), "spent": round(spent, 2),
            "remaining": round(prepared - spent, 2),
            "usage_rate": round(spent / prepared * 100, 2) if prepared else 0.0,
            "by_category": {key: round(val, 2) for key, val in category.items()},
            "wallets": list(wallet_report.values()), "unknown_currency": unknown,
            "settlement": settlement}


def calculate_settlement(paid_by: Dict[str, float], splits: Sequence[Dict[str, Any]],
                         base_currency: str, rates: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    burden: Dict[str, float] = defaultdict(float)
    for split in splits:
        if text(value(split, "status")) in {"취소", "제외"}:
            continue
        person = text(value(split, "person"))
        amount = number(value(split, "amount"))
        currency = text(value(split, "currency")) or base_currency
        converted = rate_for(currency, base_currency, rates)
        if person and amount is not None and converted is not None:
            burden[person] += amount * converted
    net = {person: round(paid_by.get(person, 0.0) - burden.get(person, 0.0), 2)
           for person in set(paid_by) | set(burden)}
    creditors = sorted([[person, amount] for person, amount in net.items() if amount > 0.01])
    debtors = sorted([[person, -amount] for person, amount in net.items() if amount < -0.01])
    transfers: List[Dict[str, Any]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = round(min(debtors[i][1], creditors[j][1]), 2)
        transfers.append({"from": debtors[i][0], "to": creditors[j][0], "amount": amount,
                          "currency": base_currency})
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] <= 0.01:
            i += 1
        if creditors[j][1] <= 0.01:
            j += 1
    return {"balances": net, "transfers": transfers, "currency": base_currency}


def validate_trip(trip: Dict[str, Any], itinerary: Sequence[Dict[str, Any]] = (),
                  reservations: Sequence[Dict[str, Any]] = (), stays: Sequence[Dict[str, Any]] = (),
                  places: Sequence[Dict[str, Any]] = (), now: Optional[dt.datetime] = None,
                  travel_minutes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    current = now or dt.datetime.now()
    issues: List[Dict[str, Any]] = []

    def issue(kind: str, message: str, severity: str = "warning", record: Optional[Dict[str, Any]] = None) -> None:
        issues.append({"kind": kind, "severity": severity, "message": message,
                       "record": row_id(record) if record else None})

    destination = text(trip.get("목적지") or trip.get("destination"))
    if not destination:
        issue("required", "여행 목적지가 비어 있습니다.", "error")
    start = parse_date(trip.get("출발일") or trip.get("start_date"))
    end = parse_date(trip.get("종료일") or trip.get("end_date"))
    duration = number(trip.get("기간(일)") or trip.get("duration_days"))
    if not (start and end) and not duration:
        issue("required", "출발일·종료일 또는 여행 기간이 필요합니다.", "error")
    if start and end and end < start:
        issue("date", "종료일이 출발일보다 빠릅니다.", "error")
    if not text(trip.get("기준 통화") or trip.get("currency")):
        issue("required", "기준 통화가 비어 있습니다.")

    active_events = [row for row in itinerary if active(row)]
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in active_events:
        if not text(value(event, "title")):
            issue("required", "일정명이 비어 있습니다.", "error", event)
        event_date = parse_date(value(event, "date"))
        group_key = event_date.isoformat() if event_date else str(value(event, "day") or "미정")
        groups[group_key].append(event)
    for group in groups.values():
        ordered = sorted(group, key=lambda row: event_datetime(row, "start") or dt.datetime.max)
        for first, second in zip(ordered, ordered[1:]):
            first_end = event_datetime(first, "end")
            second_start = event_datetime(second, "start")
            if first_end and second_start and second_start < first_end:
                issue("overlap", f"일정 시간이 겹칩니다: {row_id(first)} ↔ {row_id(second)}", "error", second)
            if first_end and second_start and text(value(first, "place")) != text(value(second, "place")):
                gap = (second_start - first_end).total_seconds() / 60
                key = f"{text(value(first, 'place'))}->{text(value(second, 'place'))}"
                required = number(first.get("필요 이동(분)"))
                if required is None and travel_minutes:
                    required = number(travel_minutes.get(key))
                buffer = number(first.get("필요 여유(분)"), 0) or 0
                if required is None:
                    issue("movement_unknown", f"이동 시간을 확인해야 합니다: {key}", "warning", second)
                elif gap < required + buffer:
                    issue("movement", f"이동 여유가 부족합니다: {key} ({gap:.0f}분 / 필요 {required + buffer:.0f}분)", "error", second)

    for reservation in reservations:
        if not active(reservation) or text(value(reservation, "status")) in {"완료", "확정", "환불"}:
            continue
        for field in ("취소 마감", "결제 기한", "예약 오픈일"):
            deadline = parse_datetime(reservation.get(field))
            if deadline:
                days = (deadline - current).total_seconds() / 86400
                if days < 0:
                    issue("deadline", f"기한이 지났습니다: {row_id(reservation)} · {field}", "error", reservation)
                elif days <= 3:
                    issue("deadline", f"기한이 임박했습니다: {row_id(reservation)} · {field} ({days:.1f}일)", "warning", reservation)
    for stay in stays:
        checkin = parse_datetime(stay.get("체크인"))
        checkout = parse_datetime(stay.get("체크아웃"))
        if checkin and checkout and checkout <= checkin:
            issue("stay", f"체크아웃이 체크인보다 빠릅니다: {row_id(stay)}", "error", stay)
    for place in places:
        checked = parse_date(place.get("확인일"))
        cycle = number(place.get("재확인 주기"), 30) or 30
        if checked and (current.date() - checked).days > cycle:
            issue("stale", f"재확인이 필요합니다: {row_id(place)}", "warning", place)
    errors = sum(item["severity"] == "error" for item in issues)
    return {"ok": errors == 0, "issue_count": len(issues), "error_count": errors,
            "warning_count": len(issues) - errors, "issues": issues}


def refresh_view(trip: Dict[str, Any], itinerary: Sequence[Dict[str, Any]],
                 reservations: Sequence[Dict[str, Any]] = (), stays: Sequence[Dict[str, Any]] = (),
                 wallets: Sequence[Dict[str, Any]] = (), expenses: Sequence[Dict[str, Any]] = (),
                 now: Optional[dt.datetime] = None,
                 shopping: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    current = now or dt.datetime.now()
    events = [(event_datetime(row, "start") or parse_datetime(value(row, "date")), row)
              for row in itinerary if active(row)]
    today = [row for when, row in events if when and when.date() == current.date()]
    future = sorted([(when, row) for when, row in events if when and when > current], key=lambda pair: pair[0])
    next_event = future[0][1] if future else None
    deadlines = []
    for reservation in reservations:
        if active(reservation) and text(value(reservation, "status")) not in {"완료", "확정", "환불"}:
            for field in ("취소 마감", "결제 기한", "예약 오픈일"):
                deadline = parse_datetime(reservation.get(field))
                if deadline and deadline >= current:
                    deadlines.append({"예약": row_id(reservation), "항목": field,
                                      "기한": deadline.isoformat(),
                                      "남은 일수": round((deadline - current).total_seconds() / 86400, 1)})
    deadlines.sort(key=lambda item: item["기한"])
    budget = calculate_budget(wallets, expenses, base_currency=text(trip.get("기준 통화") or "KRW"))
    shopping_rows = [row for row in shopping if active(row)]
    shopping_summary = {"items": shopping_rows,
                        "planned": sum(text(value(row, "status")) in {"아이디어", "구매 예정", "보류", ""}
                                        for row in shopping_rows),
                        "purchased": sum(text(value(row, "status")) == "구매 완료" for row in shopping_rows)}
    return {"date": current.date().isoformat(), "today": today,
            "next": next_event, "confirmed_stays": [row for row in stays if text(value(row, "status")) == "확정"],
            "deadline_alerts": deadlines[:10], "budget": budget, "shopping": shopping_summary}


def notion_page_record(page: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"id": page.get("id")}
    for name, prop in (page.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        kind = prop.get("type")
        if kind == "title":
            result[name] = "".join(item.get("plain_text", "") for item in prop.get("title", []))
        elif kind in {"rich_text", "title"}:
            result[name] = "".join(item.get("plain_text", "") for item in prop.get(kind, []))
        elif kind == "number":
            result[name] = prop.get("number")
        elif kind == "select":
            result[name] = (prop.get("select") or {}).get("name")
        elif kind == "multi_select":
            result[name] = [item.get("name") for item in prop.get("multi_select", [])]
        elif kind == "checkbox":
            result[name] = prop.get("checkbox", False)
        elif kind == "url":
            result[name] = prop.get("url")
        elif kind == "date":
            result[name] = (prop.get("date") or {}).get("start")
        elif kind == "formula":
            formula = prop.get("formula") or {}
            result[name] = formula.get(formula.get("type"))
    return result


def _trip_scope_filter(candidates: Sequence[str]) -> Dict[str, Any]:
    """Build a server-side Notion filter for one trip's rows."""
    values = [text(item) for item in candidates if text(item)]
    clauses = [
        {"property": "여행 키", "rich_text": {"equals": candidate}}
        for candidate in dict.fromkeys(values)
    ]
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"or": clauses}


def load_notion_snapshot(client: Any, mapping: Dict[str, Any], trip_id: str,
                         command: Optional[str] = None) -> Dict[str, Any]:
    """Read only the current trip's DB rows needed by one command.

    Older installations may not have a ``여행 키`` column yet.  In that case
    we fall back to a full read for compatibility, but new installations use
    the server-side filter so companion/shopping rows from another trip never
    leak into the dashboard and refreshes do not scan every row.
    """
    trip_page = notion_page_record(client.get_page(trip_id))
    all_keys = {"itinerary": "itinerary", "reservation": "reservation", "stay": "stay",
                "wallet": "wallet", "expense": "expense", "place": "place", "shopping": "shopping",
                "traveler": "traveler", "split": "split", "settlement": "settlement",
                "budget": "budget", "task": "task", "packing": "packing", "material": "material"}
    needed_by_command = {
        "check": {"itinerary", "reservation", "stay", "place", "traveler", "task", "packing"},
        "budget": {"wallet", "expense", "traveler", "split", "settlement", "budget"},
        "refresh": {"itinerary", "reservation", "stay", "wallet", "expense", "shopping", "material"},
    }
    selected = needed_by_command.get(command, set(all_keys))
    candidates = [trip_id, text(trip_page.get("여행 키")), text(trip_page.get("여행")), text(trip_page.get("id"))]
    scope_filter = _trip_scope_filter(candidates)
    snapshot: Dict[str, Any] = {"trip": trip_page}
    for output_key, database_key in all_keys.items():
        database_id = (mapping.get("databases") or {}).get(database_key)
        rows = []
        if output_key in selected and database_id:
            try:
                payload = {"filter": scope_filter} if scope_filter else None
                rows = [notion_page_record(page) for page in client.query_database(database_id, payload)]
                # Notion's rich_text filter is exact.  Keep a final local
                # guard because legacy title-based rows may match the query.
                allowed = {text(item) for item in candidates if text(item)}
                rows = [row for row in rows if text(value(row, "travel_key")) in allowed]
            except Exception:
                # A pre-scope database can reject the filter.  Reading it once
                # is slower, but preserves compatibility while still applying
                # the same local scope guard.
                rows = [notion_page_record(page) for page in client.query_database(database_id)]
                allowed = {text(item) for item in candidates if text(item)}
                rows = [row for row in rows if text(value(row, "travel_key")) in allowed]
        snapshot[output_key] = rows
    return snapshot


def load_json(path: Path) -> Dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("JSON 스냅샷 최상위 값은 객체여야 합니다.")
    return loaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Souvenir Python travel engine")
    parser.add_argument("command", choices=["check", "budget", "organize", "import", "refresh", "shopping", "places", "fields"])
    parser.add_argument("--input", type=Path, help="JSON 스냅샷 또는 가져올 파일")
    parser.add_argument("--kind", choices=["auto", "itinerary", "place", "shopping", "link", "csv", "json", "ics", "expense"], default="auto")
    parser.add_argument("--text", help="organize/import에서 직접 받을 텍스트")
    parser.add_argument("--item-name", help="shopping 조사 대상 품목명")
    parser.add_argument("--candidate-input", type=Path, help="shopping 외부 검색 후보 JSON")
    parser.add_argument("--candidate-index", type=int, help="확인할 후보 번호(0부터 시작)")
    parser.add_argument("--evidence-input", type=Path, help="places 외부 지도/검색 확인 JSON")
    parser.add_argument("--approved-places", help="확인 후 저장을 허용할 장소명(쉼표 구분)")
    parser.add_argument("--traveler", help="확인된 쇼핑 행의 담당 여행자")
    parser.add_argument("--confirm", action="store_true", help="선택한 후보를 사용자 확인 완료로 표시")
    parser.add_argument("--destination", help="shopping 조사 검색어에 넣을 목적지")
    parser.add_argument("--start-date", help="일정 텍스트의 출발일 YYYY-MM-DD")
    parser.add_argument("--duration-days", type=int, help="일정 텍스트의 여행 일수")
    parser.add_argument("--mapping", type=Path, default=Path(".souvenir-install.json"))
    parser.add_argument("--trip-id", help="Notion 설치 기록의 여행 페이지 ID")
    parser.add_argument("--token", default=None, help="NOTION_TOKEN 대신 사용할 토큰")
    parser.add_argument("--now", help="검사 기준 시각 ISO 형식")
    parser.add_argument("--base-currency", help="예산 계산 기준 통화")
    parser.add_argument("--rate", action="append", default=[], help="환율 예: JPY=0.009")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--part", help="fields에서 표시할 입력 파트 이름")
    return parser


def parse_rates(items: Sequence[str]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("환율은 통화=숫자 형식이어야 합니다.")
        key, raw = item.split("=", 1)
        rate = number(raw)
        if not key.strip() or rate is None or rate < 0:
            raise ValueError("환율 값이 올바르지 않습니다.")
        result[key.strip()] = rate
    return result


def load_source(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input:
        return load_json(args.input)
    if not args.token:
        raise ValueError("Notion 읽기에는 --token 또는 NOTION_TOKEN이 필요합니다.")
    try:
        from notion_installer import NotionClient, read_json
    except ImportError as exc:
        raise ValueError("notion_installer.py를 같은 scripts 폴더에서 불러오지 못했습니다.") from exc
    mapping = read_json(args.mapping)
    if not args.trip_id:
        raise ValueError("Notion 읽기에는 --trip-id가 필요합니다.")
    client = NotionClient(args.token)
    return load_notion_snapshot(client, mapping, args.trip_id, args.command)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rates = parse_rates(args.rate)
        if args.command == "fields":
            if args.part:
                result = input_fields_for(args.part)
            else:
                result = {key: input_fields_for(key) for key in INPUT_CONTRACTS}
        elif args.command == "places":
            if args.text is not None:
                content = args.text
            elif args.input:
                content = args.input.read_text(encoding="utf-8-sig")
            else:
                raise ValueError("places에는 --text 또는 --input이 필요합니다.")
            evidence: List[Dict[str, Any]] = []
            if args.evidence_input:
                loaded = json.loads(args.evidence_input.read_text(encoding="utf-8-sig"))
                raw_evidence = loaded.get("evidence", loaded.get("근거", loaded)) if isinstance(loaded, dict) else loaded
                if isinstance(raw_evidence, dict):
                    raw_evidence = [raw_evidence]
                if isinstance(raw_evidence, list):
                    evidence = [item for item in raw_evidence if isinstance(item, dict)]
            packet = prepare_place_confirmation(content, args.destination or "", evidence)
            if args.approved_places:
                packet["승인된 장소"] = confirm_place_rows(
                    packet,
                    [item.strip() for item in args.approved_places.split(",") if item.strip()],
                )
            result = packet
        elif args.command == "shopping":
            item_name = args.item_name or args.text
            if not item_name:
                raise ValueError("shopping에는 --item-name 또는 --text가 필요합니다.")
            candidates: List[Dict[str, Any]] = []
            if args.candidate_input:
                raw_text = args.candidate_input.read_text(encoding="utf-8-sig")
                loaded = json.loads(raw_text)
                raw_candidates = loaded
                if isinstance(loaded, dict):
                    raw_candidates = loaded.get("candidates", loaded.get("후보", loaded))
                if isinstance(raw_candidates, list):
                    candidates = [item for item in raw_candidates if isinstance(item, dict)]
                elif isinstance(raw_candidates, dict):
                    candidates = [raw_candidates]
            if args.confirm:
                if args.candidate_index is None:
                    raise ValueError("확인 저장에는 --candidate-index가 필요합니다.")
                if args.candidate_index < 0 or args.candidate_index >= len(candidates):
                    raise ValueError("candidate-index가 후보 범위를 벗어났습니다.")
                result = confirm_shopping_candidate(
                    item_name,
                    candidates[args.candidate_index],
                    args.traveler or "",
                    args.base_currency or "미정",
                    True,
                )
                if not result.get("저장 가능"):
                    raise ValueError("후보에 구매 장소·수량·예상 금액이 모두 필요합니다.")
            else:
                result = prepare_shopping_research(
                    item_name,
                    args.destination or "",
                    candidates,
                    args.base_currency or "미정",
                )
        elif args.command in {"organize", "import"}:
            if args.text is not None:
                content = args.text
                if args.command == "organize" or args.kind in {"auto", "itinerary"}:
                    result = organize_schedule(content, args.start_date, args.duration_days)
                elif args.kind == "place":
                    result = parse_place_lines(content)
                elif args.kind == "shopping":
                    result = parse_shopping_lines(content)
                elif args.kind == "link":
                    result = normalize_link(content)
                elif args.kind == "ics":
                    result = parse_ics(content)
                else:
                    result = [json.loads(content)]
            elif not args.input:
                raise ValueError("organize/import에는 --input 또는 --text가 필요합니다.")
            else:
                if args.command == "organize":
                    result = organize_schedule(args.input.read_text(encoding="utf-8-sig"), args.start_date, args.duration_days)
                else:
                    result = import_file(args.input, args.kind)
        else:
            source = load_source(args)
            now = parse_datetime(args.now) if args.now else None
            if args.command == "check":
                result = validate_trip(source.get("trip", {}), source.get("itinerary", []),
                                       source.get("reservation", []), source.get("stay", []), source.get("place", []), now)
            elif args.command == "budget":
                result = calculate_budget(source.get("wallet", []), source.get("expense", []),
                                          source.get("traveler", []), source.get("split", []),
                                          args.base_currency, rates)
            else:
                result = refresh_view(source.get("trip", {}), source.get("itinerary", []),
                                      source.get("reservation", []), source.get("stay", []),
                                      source.get("wallet", []), source.get("expense", []), now,
                                      source.get("shopping", []))
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    configure_output()
    raise SystemExit(main())
