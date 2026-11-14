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
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


TIME_RANGE_RE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*[-~–]\s*(?P<end>\d{1,2}:\d{2})")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
URL_RE = re.compile(r"https?://[^\s|]+", re.I)

ALIASES = {
    "title": ("title", "이름", "일정", "예약", "숙소", "내역", "장소", "항공편", "자료", "지갑", "여행자", "분담", "송금", "할 일", "준비물", "예산 항목", "서비스", "이동 구간", "결정", "날씨 확인", "검사"),
    "travel_key": ("travel_key", "여행 키", "trip_id", "trip_key"),
    "date": ("date", "날짜", "이용 일시", "이용 날짜"),
    "start": ("start", "시작", "출발"),
    "end": ("end", "종료", "도착"),
    "day": ("day", "일차"),
    "place": ("place", "장소", "도착지"),
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


def parse_place_lines(content: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in re.split(r"[\r\n]+", content):
        cleaned = line.strip(" -*\t")
        if not cleaned:
            continue
        fields = [part.strip() for part in re.split(r"\s*\|\s*", cleaned)]
        urls = URL_RE.findall(cleaned)
        link_info = normalize_link(urls[0]) if len(fields) == 1 and urls else None
        record: Dict[str, Any] = {"장소": (link_info or {}).get("label") or (link_info or {}).get("host") or fields[0]}
        if len(fields) > 1 and fields[1]:
            record["지역"] = fields[1]
        if len(fields) > 2 and fields[2]:
            record["종류"] = fields[2]
        if urls:
            record["지도 URL"] = (link_info or normalize_link(urls[0]))["url"]
        if len(fields) > 3 and fields[3] and not urls:
            record["메모"] = fields[3]
        elif len(fields) > 4 and fields[4]:
            record["메모"] = fields[4]
        records.append(record)
    return records


def normalize_link(url: str) -> Dict[str, str]:
    raw = text(url).strip("<>(),")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("http 또는 https 링크만 가져올 수 있습니다.")
    query = urllib.parse.parse_qs(parsed.query)
    label = ""
    for key in ("query", "q", "destination", "place"):
        if query.get(key):
            label = query[key][0]
            break
    return {"url": raw, "host": parsed.netloc.lower(), "label": urllib.parse.unquote(label)}


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
        for key in ("records", "rows", "data", "itinerary", "expenses", "places"):
            if isinstance(loaded, dict) and isinstance(loaded.get(key), list):
                return [item for item in loaded[key] if isinstance(item, dict)]
        return [loaded] if isinstance(loaded, dict) else []
    if selected == "ics":
        return parse_ics(content)
    if selected == "place":
        return parse_place_lines(content)
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
                 now: Optional[dt.datetime] = None) -> Dict[str, Any]:
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
    return {"date": current.date().isoformat(), "today": today,
            "next": next_event, "confirmed_stays": [row for row in stays if text(value(row, "status")) == "확정"],
            "deadline_alerts": deadlines[:10], "budget": budget}


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


def load_notion_snapshot(client: Any, mapping: Dict[str, Any], trip_id: str) -> Dict[str, Any]:
    """Read only the DBs needed by checks, budget, and travel-mode refresh."""
    trip_page = notion_page_record(client.get_page(trip_id))
    keys = {"itinerary": "itinerary", "reservation": "reservation", "stay": "stay",
            "wallet": "wallet", "expense": "expense", "place": "place"}
    snapshot: Dict[str, Any] = {"trip": trip_page}
    for output_key, database_key in keys.items():
        database_id = (mapping.get("databases") or {}).get(database_key)
        rows = []
        if database_id:
            rows = [notion_page_record(page) for page in client.query_database(database_id)]
            rows = [row for row in rows if text(value(row, "travel_key")) in {trip_id, text(trip_page.get("여행"))}]
        snapshot[output_key] = rows
    return snapshot


def load_json(path: Path) -> Dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("JSON 스냅샷 최상위 값은 객체여야 합니다.")
    return loaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Souvenir Python travel engine")
    parser.add_argument("command", choices=["check", "budget", "organize", "import", "refresh"])
    parser.add_argument("--input", type=Path, help="JSON 스냅샷 또는 가져올 파일")
    parser.add_argument("--kind", choices=["auto", "itinerary", "place", "link", "csv", "json", "ics", "expense"], default="auto")
    parser.add_argument("--text", help="organize/import에서 직접 받을 텍스트")
    parser.add_argument("--start-date", help="일정 텍스트의 출발일 YYYY-MM-DD")
    parser.add_argument("--duration-days", type=int, help="일정 텍스트의 여행 일수")
    parser.add_argument("--mapping", type=Path, default=Path(".souvenir-install.json"))
    parser.add_argument("--trip-id", help="Notion 설치 기록의 여행 페이지 ID")
    parser.add_argument("--token", default=None, help="NOTION_TOKEN 대신 사용할 토큰")
    parser.add_argument("--now", help="검사 기준 시각 ISO 형식")
    parser.add_argument("--base-currency", help="예산 계산 기준 통화")
    parser.add_argument("--rate", action="append", default=[], help="환율 예: JPY=0.009")
    parser.add_argument("--pretty", action="store_true")
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
    return load_notion_snapshot(client, mapping, args.trip_id)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rates = parse_rates(args.rate)
        if args.command in {"organize", "import"}:
            if args.text is not None:
                content = args.text
                if args.command == "organize" or args.kind in {"auto", "itinerary"}:
                    result = organize_schedule(content, args.start_date, args.duration_days)
                elif args.kind == "place":
                    result = parse_place_lines(content)
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
                                      source.get("wallet", []), source.get("expense", []), now)
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    configure_output()
    raise SystemExit(main())
