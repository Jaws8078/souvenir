#!/usr/bin/env python3
"""Install the portable Souvenir template with the official Notion API.

This is a dependency-free fallback for environments where the Notion Marketplace
template cannot be duplicated. The Marketplace/Notion template is the canonical
source of truth for design. This script never reads or writes the private
reference page. The checked-in manifest is only a sanitized fallback snapshot;
target page/database IDs are generated at install time and saved in a local
mapping file. After installation, the optional ``scaffold`` command creates a
new trip row, day rows, traveler rows, a wallet, and starter preparation rows.

The highest-fidelity route is still Notion's own Duplicate action on a public
template link. Use this installer when a link is unavailable or when a local,
auditable rebuild is preferred.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "assets" / "template-manifest.json"
DEFAULT_MAPPING = Path(".souvenir-install.json")
UUID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)


class InstallerError(RuntimeError):
    """A user-facing installation error."""


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise InstallerError(f"파일을 찾을 수 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InstallerError(f"JSON 형식이 올바르지 않습니다: {path} ({exc})") from exc
    if not isinstance(value, dict):
        raise InstallerError("매니페스트 최상위 값은 객체여야 합니다.")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_strings(child)


def validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version은 1이어야 합니다.")
    pages = manifest.get("pages")
    databases = manifest.get("databases")
    if not isinstance(pages, list) or not pages:
        errors.append("pages가 비어 있습니다.")
    if not isinstance(databases, list) or not databases:
        errors.append("databases가 비어 있습니다.")
    page_keys = {item.get("key") for item in pages or [] if isinstance(item, dict)}
    db_keys = {item.get("key") for item in databases or [] if isinstance(item, dict)}
    if len(page_keys) != len(pages or []):
        errors.append("페이지 key가 중복되었거나 빠졌습니다.")
    if len(db_keys) != len(databases or []):
        errors.append("데이터베이스 key가 중복되었거나 빠졌습니다.")
    for page in pages or []:
        if not isinstance(page, dict):
            errors.append("페이지 항목은 객체여야 합니다.")
            continue
        parent = page.get("parent")
        if parent not in {"target", *page_keys}:
            errors.append(f"페이지 {page.get('key')}의 parent를 찾을 수 없습니다: {parent}")
    for database in databases or []:
        if not isinstance(database, dict):
            errors.append("데이터베이스 항목은 객체여야 합니다.")
            continue
        if database.get("parent") not in page_keys:
            errors.append(f"데이터베이스 {database.get('key')}의 parent를 찾을 수 없습니다.")
        properties = database.get("properties")
        if not isinstance(properties, list) or not any(p.get("type") == "title" for p in properties if isinstance(p, dict)):
            # The installer inserts a title property from title_property, so this is informational.
            if not database.get("title_property"):
                errors.append(f"데이터베이스 {database.get('key')}에 title_property가 없습니다.")
    seed_rows = manifest.get("seed_rows", {})
    if seed_rows and not isinstance(seed_rows, dict):
        errors.append("seed_rows는 데이터베이스 key를 가진 객체여야 합니다.")
    if isinstance(seed_rows, dict):
        for key, rows in seed_rows.items():
            if key not in db_keys:
                errors.append(f"seed_rows의 데이터베이스 key를 찾을 수 없습니다: {key}")
            if not isinstance(rows, list):
                errors.append(f"seed_rows.{key}는 배열이어야 합니다.")
            else:
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                        errors.append(f"seed_rows.{key}의 각 항목에는 title이 필요합니다.")
    forbidden = ("app.notion.com", "notion.so", "collection://", "notion-file-block://")
    for text in iter_strings(manifest):
        if any(part in text for part in forbidden) or UUID_RE.search(text):
            errors.append("매니페스트에 Notion URL 또는 객체 ID가 포함되어 있습니다.")
            break
    return errors


def normalize_id(value: str) -> str:
    match = UUID_RE.search(value.replace("-", ""))
    if not match:
        raise InstallerError("Notion 페이지 ID 또는 URL에서 UUID를 찾지 못했습니다.")
    raw = match.group(0).replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def rich_text(content: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def title_payload(content: str) -> List[Dict[str, Any]]:
    return rich_text(content)


def api_property(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if spec.get("computed") or spec.get("type") in {"formula", "rollup", "relation"}:
        return None
    kind = spec.get("type")
    if kind == "title":
        return {"title": {}}
    if kind == "rich_text":
        return {"rich_text": {}}
    if kind == "number":
        return {"number": {"format": "number"}}
    if kind == "date":
        return {"date": {}}
    if kind == "checkbox":
        return {"checkbox": {}}
    if kind == "url":
        return {"url": {}}
    if kind == "select":
        return {"select": {"options": [{"name": option} for option in spec.get("options", [])]}}
    if kind == "multi_select":
        return {"multi_select": {"options": [{"name": option} for option in spec.get("options", [])]}}
    raise InstallerError(f"지원하지 않는 Notion 속성 유형입니다: {kind}")


class NotionClient:
    def __init__(self, token: str, api_version: str = "2022-06-28") -> None:
        self.token = token
        self.api_version = api_version

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.notion.com/v1" + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.api_version,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                message = parsed.get("message", detail)
            except json.JSONDecodeError:
                message = detail
            raise InstallerError(f"Notion API {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise InstallerError(f"Notion API에 연결하지 못했습니다: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InstallerError("Notion API 응답이 JSON이 아닙니다.") from exc

    def get_page(self, page_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/pages/{page_id}")

    def create_page(self, parent_id: str, title: str, icon: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": title_payload(title)}},
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        return self.request("POST", "/pages", payload)

    def create_database(self, parent_id: str, title: str, properties: Dict[str, Any], icon: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        return self.request("POST", "/databases", payload)

    def create_database_page(self, database_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        """Create a starter row in a database without requiring a user token in chat."""
        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        return self.request("POST", "/pages", payload)

    def append_blocks(self, block_id: str, blocks: List[Dict[str, Any]]) -> None:
        if blocks:
            self.request("PATCH", f"/blocks/{block_id}/children", {"children": blocks})


def text_block(text: str, block_type: str = "paragraph") -> Dict[str, Any]:
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text(text)}}


def heading_block(text: str, level: int = 2) -> Dict[str, Any]:
    block_type = f"heading_{max(1, min(level, 3))}"
    return text_block(text, block_type)


def callout_block(title: str, body: str, icon: str = "🧭", color: str = "gray_background") -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text(f"{title}\n{body}"),
            "icon": {"type": "emoji", "emoji": icon},
            "color": color,
        },
    }


def divider_block() -> Dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def link_block(object_id: str, object_type: str = "page_id") -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "link_to_page",
        "link_to_page": {"type": object_type, object_type: object_id},
    }


def database_properties(database: Dict[str, Any]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {database["title_property"]: {"title": {}}}
    for spec in database.get("properties", []):
        if spec.get("name") == database["title_property"]:
            continue
        value = api_property(spec)
        if value is not None:
            properties[spec["name"]] = value
    return properties


def property_value(spec: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    """Convert a manifest seed value to a Notion page property payload."""
    if spec.get("computed") or spec.get("type") in {"formula", "rollup", "relation"}:
        return None
    kind = spec.get("type")
    if kind == "title":
        return {"title": title_payload(str(value))}
    if kind == "rich_text":
        return {"rich_text": rich_text(str(value))}
    if kind == "number":
        return {"number": value}
    if kind == "checkbox":
        return {"checkbox": bool(value)}
    if kind == "url":
        return {"url": str(value)}
    if kind == "select":
        return {"select": {"name": str(value)}}
    if kind == "multi_select":
        values = value if isinstance(value, list) else [value]
        return {"multi_select": [{"name": str(item)} for item in values]}
    if kind == "date":
        if isinstance(value, dict):
            return {"date": value}
        return {"date": {"start": str(value)}}
    raise InstallerError(f"지원하지 않는 seed 속성 유형입니다: {kind}")


def seed_properties(database: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    specs = {spec["name"]: spec for spec in database.get("properties", []) if isinstance(spec, dict) and spec.get("name")}
    properties: Dict[str, Any] = {
        database["title_property"]: {"title": title_payload(row["title"])}
    }
    for name, value in row.get("values", {}).items():
        spec = specs.get(name)
        if not spec:
            raise InstallerError(f"{database['key']} seed 행의 속성을 찾을 수 없습니다: {name}")
        encoded = property_value(spec, value)
        if encoded is not None:
            properties[name] = encoded
    return properties


def seed_default_rows(manifest: Dict[str, Any], client: NotionClient, mapping: Dict[str, Any], mapping_path: Path) -> None:
    """Add generic, unassigned starter rows once; never copy personal trip data."""
    seed_rows = manifest.get("seed_rows", {})
    if not seed_rows:
        mapping.setdefault("seeded_rows", {})
        return
    db_by_key = {database["key"]: database for database in manifest["databases"]}
    seeded = mapping.setdefault("seeded_rows", {})
    for key, rows in seed_rows.items():
        if key not in mapping.get("databases", {}):
            continue
        existing = seeded.setdefault(key, [])
        for row in rows:
            if row.get("title") in {item.get("title") for item in existing}:
                continue
            created = client.create_database_page(mapping["databases"][key], seed_properties(db_by_key[key], row))
            existing.append({"title": row["title"], "id": created["id"]})
            write_json(mapping_path, mapping)


def database_by_key(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    try:
        return next(database for database in manifest["databases"] if database.get("key") == key)
    except StopIteration as exc:
        raise InstallerError(f"매니페스트에서 데이터베이스를 찾지 못했습니다: {key}") from exc


def parse_date(value: Optional[str], label: str) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InstallerError(f"{label}은 YYYY-MM-DD 형식이어야 합니다: {value}") from exc


def scaffold_trip(
    manifest: Dict[str, Any],
    client: NotionClient,
    mapping_path: Path,
    destination: str,
    start_date: Optional[str],
    end_date: Optional[str],
    duration_days: Optional[int],
    traveler_count: int,
    currency: str,
    trip_name: Optional[str] = None,
) -> Dict[str, Any]:
    mapping = read_json(mapping_path)
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 install을 완료해 .souvenir-install.json에 대상 DB 매핑을 만들어야 합니다.")
    for key in ("trip", "itinerary", "traveler", "wallet", "task", "packing"):
        if key not in mapping.get("databases", {}):
            raise InstallerError(f"설치 기록에 {key} 데이터베이스 매핑이 없습니다.")

    start = parse_date(start_date, "출발일")
    end = parse_date(end_date, "종료일")
    if start and end and end < start:
        raise InstallerError("종료일은 출발일보다 빠를 수 없습니다.")
    derived_days = (end - start).days + 1 if start and end else None
    if duration_days is not None and duration_days < 1:
        raise InstallerError("기간(일)은 1 이상의 정수여야 합니다.")
    if derived_days and duration_days and derived_days != duration_days:
        raise InstallerError("출발일·종료일과 기간(일)이 서로 다릅니다. 하나의 기준으로 다시 입력하세요.")
    days = derived_days or duration_days
    if not days:
        raise InstallerError("출발일·종료일 또는 기간(일) 중 하나가 필요합니다.")
    if traveler_count < 0:
        raise InstallerError("여행 인원은 0 이상이어야 합니다.")
    allowed_currency = {"KRW", "JPY", "USD", "EUR", "미정"}
    if currency not in allowed_currency:
        raise InstallerError("기준 통화는 KRW·JPY·USD·EUR·미정 중 하나여야 합니다.")

    fingerprint = "|".join([
        destination.strip(), start_date or "", end_date or "", str(days), str(traveler_count), currency,
    ])
    scaffolded = mapping.setdefault("scaffolded_trips", {})
    if fingerprint in scaffolded:
        return scaffolded[fingerprint]

    trip_db = database_by_key(manifest, "trip")
    trip_title = trip_name or f"✈️ {destination.strip()} 여행"
    trip_values: Dict[str, Any] = {
        "목적지": destination.strip(),
        "여행 상태": "계획 중",
        "인원": traveler_count if traveler_count else None,
        "인원 기준": "직접 입력" if traveler_count else None,
        "기준 통화": currency,
    }
    if start:
        trip_values["출발일"] = start.isoformat()
    if end:
        trip_values["종료일"] = end.isoformat()
    if start and end:
        trip_values["여행 기간"] = {"start": start.isoformat(), "end": end.isoformat()}
    trip_values["기간(일)"] = days
    trip_row = {
        "title": trip_title,
        "values": {key: value for key, value in trip_values.items() if value is not None},
    }
    trip_page = client.create_database_page(mapping["databases"]["trip"], seed_properties(trip_db, trip_row))
    trip_id = trip_page["id"]
    result: Dict[str, Any] = {
        "trip_id": trip_id,
        "trip_name": trip_title,
        "destination": destination.strip(),
        "days": days,
        "traveler_count": traveler_count,
        "currency": currency,
        "itinerary": [],
        "travelers": [],
        "wallet": None,
        "tasks": [],
        "packing": [],
    }

    itinerary_db = database_by_key(manifest, "itinerary")
    for day in range(1, days + 1):
        values: Dict[str, Any] = {"여행 키": trip_id, "일차": day, "상태": "계획"}
        if start:
            values["날짜"] = (start + dt.timedelta(days=day - 1)).isoformat()
        row = {"title": f"{day}일차 계획", "values": values}
        created = client.create_database_page(mapping["databases"]["itinerary"], seed_properties(itinerary_db, row))
        result["itinerary"].append(created["id"])

    traveler_db = database_by_key(manifest, "traveler")
    for index in range(1, traveler_count + 1):
        row = {
            "title": f"동행자 {index}",
            "values": {"여행 키": trip_id, "역할": "여행자", "상태": "참여"},
        }
        created = client.create_database_page(mapping["databases"]["traveler"], seed_properties(traveler_db, row))
        result["travelers"].append(created["id"])

    wallet_db = database_by_key(manifest, "wallet")
    wallet_row = {
        "title": f"{destination.strip()} 여행 지갑",
        "values": {"여행 키": trip_id, "통화": currency, "금액 입력 완료": False},
    }
    wallet_page = client.create_database_page(mapping["databases"]["wallet"], seed_properties(wallet_db, wallet_row))
    result["wallet"] = wallet_page["id"]

    for key in ("task", "packing"):
        database = database_by_key(manifest, key)
        for starter in manifest.get("seed_rows", {}).get(key, []):
            values = dict(starter.get("values", {}))
            values["여행 키"] = trip_id
            values["원본 구분"] = "사용 중"
            row = {"title": starter["title"], "values": values}
            created = client.create_database_page(mapping["databases"][key], seed_properties(database, row))
            result["tasks" if key == "task" else "packing"].append(created["id"])

    scaffolded[fingerprint] = result
    write_json(mapping_path, mapping)
    return result


def plan(manifest: Dict[str, Any]) -> Dict[str, Any]:
    pages = manifest["pages"]
    databases = manifest["databases"]
    return {
        "template": manifest.get("display_name"),
        "pages": [{"key": page["key"], "title": page["title"], "parent": page["parent"]} for page in pages],
        "databases": [
            {
                "key": database["key"],
                "title": database["title"],
                "parent": database["parent"],
                "properties": len(database.get("properties", [])) + 1,
                "views": database.get("views", []),
            }
            for database in databases
        ],
        "notes": [
            "원본 Notion 페이지·ID·사용자 데이터는 읽지 않습니다.",
            "Notion Marketplace 템플릿이 주 원본이며, 공개·복제 가능한 링크가 있으면 반드시 그 경로를 먼저 사용하세요.",
            "코드 재구축에서는 데이터베이스 구조를 만들고 뷰 이름을 안내합니다. 뷰의 실제 배치·관계·수식은 Souvenir 스킬 또는 Notion에서 후속 구성합니다.",
            "기본 준비할 일·준비물 행은 개인 여행에 연결하지 않은 starter 행으로 한 번만 생성합니다.",
        ],
        "seed_rows": {key: len(rows) for key, rows in manifest.get("seed_rows", {}).items()},
    }


def install(manifest: Dict[str, Any], client: NotionClient, parent_id: str, mapping_path: Path) -> Dict[str, Any]:
    mapping: Dict[str, Any] = {}
    if mapping_path.exists():
        previous = read_json(mapping_path)
        if previous.get("template_key") == manifest.get("template_key"):
            mapping = previous
    content_rendered = bool(mapping.get("content_rendered"))
    log_rendered = bool(mapping.get("log_rendered"))
    mapping.setdefault("template_key", manifest.get("template_key"))
    mapping.setdefault("template_source", "code_rebuild")
    mapping.setdefault("status", "in_progress")
    mapping.setdefault("pages", {})
    mapping.setdefault("databases", {})
    mapping["parent_page_id"] = parent_id
    mapping["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    client.get_page(parent_id)

    page_by_key = {page["key"]: page for page in manifest["pages"]}

    def ensure_page(key: str) -> str:
        if key in mapping["pages"]:
            return mapping["pages"][key]
        page = page_by_key[key]
        parent = parent_id if page["parent"] == "target" else ensure_page(page["parent"])
        created = client.create_page(parent, page["title"], page.get("icon"))
        object_id = created["id"]
        mapping["pages"][key] = object_id
        write_json(mapping_path, mapping)
        return object_id

    for page in manifest["pages"]:
        ensure_page(page["key"])

    db_by_key = {database["key"]: database for database in manifest["databases"]}
    for database in manifest["databases"]:
        key = database["key"]
        if key in mapping["databases"]:
            continue
        parent = ensure_page(database["parent"])
        created = client.create_database(parent, database["title"], database_properties(database), database.get("title", "")[0])
        mapping["databases"][key] = created["id"]
        mapping.setdefault("computed_properties", {})[key] = [
            prop["name"] for prop in database.get("properties", []) if prop.get("computed")
        ]
        mapping.setdefault("views", {})[key] = database.get("views", [])
        write_json(mapping_path, mapping)

    seed_default_rows(manifest, client, mapping, mapping_path)

    if not content_rendered:
        dashboard_blocks: List[Dict[str, Any]] = []
        for section in page_by_key["dashboard"].get("sections", []):
            kind = section.get("kind")
            if kind == "callout":
                dashboard_blocks.append(callout_block(section["title"], section["body"], section.get("icon", "🧭"), section.get("color", "gray_background")))
            elif kind == "heading":
                dashboard_blocks.append(heading_block(section["text"], 2))
            elif kind == "links":
                dashboard_blocks.append(text_block(section["title"], "paragraph"))
                for target in section.get("targets", []):
                    if target in mapping["pages"]:
                        dashboard_blocks.append(link_block(mapping["pages"][target], "page_id"))
                    elif target in mapping["databases"]:
                        dashboard_blocks.append(link_block(mapping["databases"][target], "database_id"))
        client.append_blocks(mapping["pages"]["dashboard"], dashboard_blocks)

        database_titles = {database["key"]: database["title"] for database in manifest["databases"]}
        for page in manifest["pages"]:
            role = page.get("role")
            if role == "group":
                blocks: List[Dict[str, Any]] = [callout_block(page["title"], "입력 항목은 먼저 작성하고, 자동 결과·보조 속성은 상세 화면에서 확인하세요.", page.get("icon", "🗂️"), page.get("color", "gray_background"))]
                for key in page.get("database_keys", []):
                    blocks.append(link_block(mapping["databases"][key], "database_id"))
                client.append_blocks(mapping["pages"][page["key"]], blocks)
            elif role == "planning_zone":
                blocks = [callout_block(page["title"], "출발 전 작성·수정 공간입니다. 여행 중에는 확인 존을 먼저 보고, 계획을 바꿀 때만 이곳으로 들어오세요.", page.get("icon", "🛠️"), page.get("color", "blue_background"))]
                for child in page.get("children", []):
                    if child in mapping["pages"]:
                        blocks.append(link_block(mapping["pages"][child], "page_id"))
                client.append_blocks(mapping["pages"][page["key"]], blocks)
            elif role == "travel_mode":
                blocks = [callout_block(page["title"], "여행 중 확인용 화면입니다. 계획을 수정하지 않을 때는 이 페이지를 잠가 두고 오늘 일정·예약·숙소·지갑만 확인하세요.", page.get("icon", "🧭"), page.get("color", "green_background"))]
                cards = page.get("cards", [])
                if cards:
                    for card in cards:
                        key = card["database_key"]
                        view_name = card.get("view", "확인 보기")
                        blocks.append(callout_block(card["title"], f"{view_name} · 입력 속성 숨김", card.get("icon", "🔎"), card.get("color", "gray_background")))
                        blocks.append(link_block(mapping["databases"][key], "database_id"))
                else:
                    for key in page.get("database_keys", []):
                        blocks.append(text_block(database_titles.get(key, key), "paragraph"))
                        blocks.append(link_block(mapping["databases"][key], "database_id"))
                        visible = page.get("visible_properties", {}).get(key, [])
                        if visible:
                            blocks.append(text_block("확인 표시: " + " · ".join(visible), "paragraph"))
                client.append_blocks(mapping["pages"][page["key"]], blocks)
        mapping["content_rendered"] = True

    log_id = mapping["pages"].get("install_log")
    if log_id and not log_rendered:
        summary = json.dumps({"status": "완료", "template_source": "code_rebuild", "pages": mapping["pages"], "databases": mapping["databases"]}, ensure_ascii=False)
        client.append_blocks(log_id, [heading_block("설치 기록", 2), text_block("Souvenir 코드 설치가 완료되었습니다."), text_block(summary)])
        mapping["log_rendered"] = True

    mapping["status"] = "complete"
    mapping["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(mapping_path, mapping)
    return mapping


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Souvenir portable Notion template installer")
    parser.add_argument("command", choices=["validate", "plan", "install", "scaffold"])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--parent-page", help="대상 워크스페이스에서 생성할 상위 Notion 페이지 ID 또는 URL")
    parser.add_argument("--token", default=None, help="Notion API 토큰. 생략하면 NOTION_TOKEN 환경 변수를 사용합니다.")
    parser.add_argument("--api-version", default=os.environ.get("NOTION_VERSION", "2022-06-28"))
    parser.add_argument("--pretty", action="store_true", help="계획/검증 결과를 보기 좋게 출력합니다.")
    parser.add_argument("--destination", help="scaffold 여행 목적지")
    parser.add_argument("--start-date", help="scaffold 출발일 YYYY-MM-DD")
    parser.add_argument("--end-date", help="scaffold 종료일 YYYY-MM-DD")
    parser.add_argument("--duration-days", type=int, help="scaffold 날짜가 없을 때 여행 기간(일)")
    parser.add_argument("--travelers", type=int, default=0, help="scaffold 여행 인원. 0이면 미정")
    parser.add_argument("--currency", default="미정", help="scaffold 기준 통화: KRW·JPY·USD·EUR·미정")
    parser.add_argument("--trip-name", help="scaffold 여행 페이지 제목")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = read_json(args.manifest)
        errors = validate_manifest(manifest)
        if errors:
            raise InstallerError("매니페스트 검증 실패:\n- " + "\n- ".join(errors))
        if args.command == "validate":
            print("Souvenir 매니페스트가 유효합니다.")
            print(f"페이지 {len(manifest['pages'])}개 · 데이터베이스 {len(manifest['databases'])}개")
            return 0
        if args.command == "plan":
            output = plan(manifest)
            print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))
            return 0
        token = args.token or os.environ.get("NOTION_TOKEN")
        if not token:
            raise InstallerError(f"{args.command}에는 NOTION_TOKEN 환경 변수 또는 --token이 필요합니다. 토큰을 채팅에 붙여넣지 마세요.")
        client = NotionClient(token, args.api_version)
        if args.command == "scaffold":
            if not args.destination:
                raise InstallerError("scaffold에는 --destination이 필요합니다.")
            result = scaffold_trip(
                manifest,
                client,
                args.mapping,
                args.destination,
                args.start_date,
                args.end_date,
                args.duration_days,
                args.travelers,
                args.currency,
                args.trip_name,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
            return 0
        if not args.parent_page:
            raise InstallerError("install에는 --parent-page가 필요합니다. 대상 워크스페이스의 상위 페이지를 지정하세요.")
        parent_id = normalize_id(args.parent_page)
        mapping = install(manifest, client, parent_id, args.mapping)
        print(f"Souvenir 설치 완료: 페이지 {len(mapping['pages'])}개 · 데이터베이스 {len(mapping['databases'])}개")
        print(f"설치 기록: {args.mapping}")
        return 0
    except InstallerError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
