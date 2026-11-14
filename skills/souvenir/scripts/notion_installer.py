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
import hashlib
import http.client
import json
import math
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "assets" / "template-manifest.json"
DEFAULT_MAPPING = Path(".souvenir-install.json")
UUID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)
BLOCK_BATCH_SIZE = 100
TEXT_LIMIT = 2000
IDEMPOTENCY_PROPERTY = "Souvenir 작업 키"
OPERATION_LOG_TITLE = "Souvenir · 작업 기록"
LEASE_ACTIVE = "반영 중"
LEASE_COMPLETED = "완료"
LEASE_CONFLICT = "충돌"
LEASE_REVIEW = "확인 필요"
LEASE_DEFAULT_MINUTES = 10


def configure_output() -> None:
    """Keep Korean CLI output usable on Windows' legacy console encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class InstallerError(RuntimeError):
    """A user-facing installation error."""


class UncertainWriteError(InstallerError):
    """The server may have saved a write; inspect it before replaying it."""


class ConflictError(InstallerError):
    """The target changed after it was read; do not apply a stale update."""


class LocalOperationLock:
    """Cross-platform, same-machine lock for one Souvenir write operation.

    ``O_EXCL`` makes lock acquisition atomic even when two Python processes start
    at the same time.  A stale lock is intentionally not removed automatically:
    deleting it could allow two writers after a slow process resumes.
    """

    def __init__(self, path: Path, operation: str) -> None:
        self.path = path
        self.operation = operation
        self.owner = uuid.uuid4().hex
        self._fd: Optional[int] = None

    def __enter__(self) -> "LocalOperationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": self.owner,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "operation": self.operation,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self._fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            os.fsync(self._fd)
        except FileExistsError as exc:
            details = ""
            try:
                details = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise InstallerError(
                f"다른 Souvenir 쓰기 작업이 진행 중입니다: {self.path}"
                + (f" ({details})" if details else "")
                + " 작업이 끝난 뒤 다시 시도하세요. 오래된 잠금은 실행 중인 프로세스가 없는지 확인한 뒤 직접 제거하세요."
            ) from exc
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise InstallerError(f"쓰기 잠금을 만들지 못했습니다: {self.path} ({exc})") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and current.get("owner") == self.owner:
                self.path.unlink()
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeError):
            # Never remove a lock whose ownership cannot be established.
            if exc_type is None:
                raise InstallerError(f"쓰기 잠금의 소유자를 확인하지 못했습니다: {self.path}")
        except OSError as exc:
            # The remote operation already completed; leave an explicit warning
            # rather than masking the original exception.
            if exc_type is None:
                raise InstallerError(f"쓰기 잠금을 해제하지 못했습니다: {self.path} ({exc})") from exc


def _lock_name(mapping_path: Path, scope: str) -> Path:
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return mapping_path.parent / f".{mapping_path.name}.{digest}.lock"


def _workspace_lock_name(mapping_path: Path, workspace_key: str) -> Path:
    digest = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()[:16]
    return mapping_path.parent / f".souvenir-workspace-{digest}.lock"


@contextmanager
def operation_locks(mapping_path: Path, workspace_key: str, operation: str) -> Any:
    """Serialize local writes for both one mapping and one Notion parent."""
    mapping_lock = LocalOperationLock(_lock_name(mapping_path, "mapping:" + str(mapping_path.resolve())), operation)
    workspace_lock = LocalOperationLock(_workspace_lock_name(mapping_path, workspace_key), operation)
    with mapping_lock:
        with workspace_lock:
            yield


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise InstallerError(f"파일을 찾을 수 없습니다: {path}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InstallerError(f"JSON 형식이 올바르지 않습니다: {path} ({exc})") from exc
    except OSError as exc:
        raise InstallerError(f"파일을 읽지 못했습니다: {path} ({exc})") from exc
    if not isinstance(value, dict):
        raise InstallerError(f"JSON 최상위 값은 객체여야 합니다: {path}")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    """Replace the checkpoint atomically; a failed write keeps the last good file."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise InstallerError(f"설치 기록을 저장하지 못했습니다: {path} ({exc})") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_pending(mapping: Dict[str, Any]) -> None:
    if mapping.get("pending_operation"):
        pending = mapping["pending_operation"]
        if isinstance(pending, dict):
            label = pending.get("key", "알 수 없는 작업")
        else:
            label = pending
        raise InstallerError(
            f"저장 결과 확인이 필요한 작업이 있습니다: {label}. "
            "대상 Notion에서 생성 여부를 확인하고 매핑을 복구한 뒤 재개하세요. "
            "확인 없이 설치 기록을 지우면 중복 생성될 수 있습니다."
        )


def check_manifest_version(mapping: Dict[str, Any], manifest: Dict[str, Any]) -> None:
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if mapping.get("manifest_digest") not in (None, digest):
        raise InstallerError("설치 당시와 매니페스트가 다릅니다. 기존 버전으로 재개하거나 새 --mapping을 지정하세요.")
    mapping["manifest_digest"] = digest


def recorded_write(mapping: Dict[str, Any], path: Path, key: str,
                   action: Callable[[], Any], record: Callable[[Any], None],
                   verify: Optional[Callable[[Any], None]] = None,
                   idempotency_key: Optional[str] = None) -> None:
    """Journal a remote mutation and checkpoint its result before proceeding."""
    check_pending(mapping)
    mapping["pending_operation"] = {
        "key": key,
        "operation_id": uuid.uuid4().hex,
        "idempotency_key": idempotency_key,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(path, mapping)
    try:
        result = action()
        if verify is not None:
            verify(result)
    except UncertainWriteError:
        raise
    except InstallerError:
        # A definite rejection (e.g. 400/401/429) is safe to resume later.
        mapping.pop("pending_operation")
        write_json(path, mapping)
        raise
    record(result)
    mapping.pop("pending_operation")
    write_json(path, mapping)


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
        pages = []
    if not isinstance(databases, list) or not databases:
        errors.append("databases가 비어 있습니다.")
        databases = []

    def valid_key(item: Any) -> bool:
        return isinstance(item, dict) and isinstance(item.get("key"), str) and bool(item["key"].strip())

    page_keys = {item["key"] for item in pages if valid_key(item)}
    db_keys = {item["key"] for item in databases if valid_key(item)}
    if len(page_keys) != len(pages or []):
        errors.append("페이지 key가 중복되었거나 빠졌습니다.")
    if len(db_keys) != len(databases or []):
        errors.append("데이터베이스 key가 중복되었거나 빠졌습니다.")
    if "dashboard" not in page_keys:
        errors.append("dashboard 페이지가 필요합니다.")
    if "target" in page_keys:
        errors.append("페이지 key에 예약어 target은 사용할 수 없습니다.")
    if not isinstance(manifest.get("template_key"), str) or not manifest["template_key"].strip():
        errors.append("template_key가 필요합니다.")
    for page in pages or []:
        if not isinstance(page, dict):
            errors.append("페이지 항목은 객체여야 합니다.")
            continue
        parent = page.get("parent")
        if not isinstance(parent, str) or parent not in {"target", *page_keys}:
            errors.append(f"페이지 {page.get('key')}의 parent를 찾을 수 없습니다: {parent}")
        if not isinstance(page.get("title"), str) or not page["title"].strip():
            errors.append(f"페이지 {page.get('key')}에 title이 필요합니다.")
        for field, targets in (("children", page_keys), ("database_keys", db_keys)):
            references = page.get(field, [])
            if not isinstance(references, list) or any(not isinstance(key, str) or key not in targets for key in references):
                errors.append(f"페이지 {page.get('key')}의 {field} 연결이 올바르지 않습니다.")
        for field in ("sections", "cards"):
            entries = page.get(field, [])
            if not isinstance(entries, list):
                errors.append(f"페이지 {page.get('key')}의 {field}는 배열이어야 합니다.")
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    errors.append(f"페이지 {page.get('key')}의 {field} 항목은 객체여야 합니다.")
                    continue
                kind = entry.get("kind") if field == "sections" else "card"
                required = {"callout": ("title", "body"), "heading": ("text",),
                            "links": ("title",), "card": ("title",)}
                if not isinstance(kind, str) or kind not in required:
                    errors.append(f"지원하지 않는 페이지 섹션: {kind}")
                    continue
                if any(not isinstance(entry.get(name), str) for name in required[kind]):
                    errors.append(f"페이지 {page.get('key')}의 {kind}에 필요한 텍스트가 없습니다.")
                if kind == "links":
                    targets = entry.get("targets", [])
                    if not isinstance(targets, list) or any(not isinstance(key, str) or key not in page_keys | db_keys for key in targets):
                        errors.append(f"페이지 {page.get('key')}의 links 대상이 올바르지 않습니다.")
                if kind == "card" and (not isinstance(entry.get("database_key"), str) or entry["database_key"] not in db_keys):
                    errors.append(f"페이지 {page.get('key')}의 카드 DB를 찾을 수 없습니다.")
    parents = {page["key"]: page.get("parent") for page in pages if valid_key(page)}
    checked = set()
    for key in parents:
        chain = set()
        while isinstance(key, str) and key in parents and key not in checked:
            if key in chain:
                errors.append(f"페이지 parent에 순환 연결이 있습니다: {key}")
                break
            chain.add(key)
            key = parents[key]
        checked.update(chain)
    for database in databases or []:
        if not isinstance(database, dict):
            errors.append("데이터베이스 항목은 객체여야 합니다.")
            continue
        if not isinstance(database.get("parent"), str) or database["parent"] not in page_keys:
            errors.append(f"데이터베이스 {database.get('key')}의 parent를 찾을 수 없습니다.")
        for field in ("title", "title_property"):
            if not isinstance(database.get(field), str) or not database[field].strip():
                errors.append(f"데이터베이스 {database.get('key')}에 {field}가 필요합니다.")
        properties = database.get("properties", [])
        if not isinstance(properties, list):
            errors.append(f"데이터베이스 {database.get('key')}의 properties는 배열이어야 합니다.")
            continue
        names = set()
        for prop in properties:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str) or not prop["name"].strip():
                errors.append(f"데이터베이스 {database.get('key')}의 속성 name이 올바르지 않습니다.")
                continue
            if prop["name"] in names:
                errors.append(f"중복 속성: {database.get('key')}.{prop['name']}")
            names.add(prop["name"])
            if not isinstance(prop.get("type"), str):
                errors.append(f"속성 type이 올바르지 않습니다: {prop['name']}")
                continue
            if prop.get("type") == "title" and prop["name"] != database.get("title_property"):
                errors.append(f"제목 속성은 하나만 지정하세요: {database.get('key')}")
            if prop.get("type") in {"select", "multi_select"}:
                options = prop.get("options", [])
                if not isinstance(options, list) or any(not isinstance(option, str) or not option for option in options):
                    errors.append(f"선택 속성 options가 올바르지 않습니다: {prop['name']}")
                    continue
            try:
                api_property(prop)
            except InstallerError as exc:
                errors.append(str(exc))
    seed_rows = manifest.get("seed_rows", {})
    if not isinstance(seed_rows, dict):
        errors.append("seed_rows는 데이터베이스 key를 가진 객체여야 합니다.")
    if isinstance(seed_rows, dict):
        for key, rows in seed_rows.items():
            if key not in db_keys:
                errors.append(f"seed_rows의 데이터베이스 key를 찾을 수 없습니다: {key}")
            if not isinstance(rows, list):
                errors.append(f"seed_rows.{key}는 배열이어야 합니다.")
            else:
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip():
                        errors.append(f"seed_rows.{key}의 각 항목에는 title이 필요합니다.")
    fixed_price_fields = {
        "long_transport": ["항공권 예산"],
        "stay": ["1박 비용", "총액"],
    }
    for database_key, fields in fixed_price_fields.items():
        database = next((item for item in databases or [] if isinstance(item, dict) and item.get("key") == database_key), None)
        if not database:
            continue
        properties = database.get("properties", [])
        if not isinstance(properties, list):
            continue
        specs = {item["name"]: item for item in properties if isinstance(item, dict) and isinstance(item.get("name"), str)}
        for field in fields:
            spec = specs.get(field)
            if not spec or spec.get("unit") != "KRW" or spec.get("format") != "won":
                errors.append(f"{database_key}.{field}는 unit=KRW·format=won이어야 합니다.")
        currency = specs.get("통화")
        if not currency or currency.get("options") != ["KRW"] or currency.get("fixed") != "KRW":
            errors.append(f"{database_key}.통화는 KRW 고정 선택지여야 합니다.")
    forbidden = ("app.notion.com", "notion.so", "collection://", "notion-file-block://")
    for text in iter_strings(manifest):
        if any(part in text for part in forbidden) or UUID_RE.search(text):
            errors.append("매니페스트에 Notion URL 또는 객체 ID가 포함되어 있습니다.")
            break
    if not errors:
        db_by_key = {database["key"]: database for database in databases}
        for key, rows in seed_rows.items():
            titles = set()
            for row in rows:
                if row["title"] in titles:
                    errors.append(f"중복 기본 행: {key}.{row['title']}")
                titles.add(row["title"])
                try:
                    seed_properties(db_by_key[key], row)
                except InstallerError as exc:
                    errors.append(str(exc))
    return errors


def normalize_id(value: str) -> str:
    match = UUID_RE.search(value.replace("-", ""))
    if not match:
        raise InstallerError("Notion 페이지 ID 또는 URL에서 UUID를 찾지 못했습니다.")
    raw = match.group(0).replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def rich_text(content: str) -> List[Dict[str, Any]]:
    if len(content) > TEXT_LIMIT * BLOCK_BATCH_SIZE:
        raise InstallerError("텍스트가 Notion 속성 한도를 초과합니다.")
    return [{"type": "text", "text": {"content": content[offset:offset + TEXT_LIMIT]}}
            for offset in range(0, len(content), TEXT_LIMIT)]


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
        return {"number": {"format": spec.get("format", "number")}}
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
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._request_spacing = 0.05

    def _wait_for_rate_slot(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self._request_spacing
        if delay:
            time.sleep(delay)

    def _defer_requests(self, delay: float) -> None:
        with self._rate_lock:
            self._next_request_at = max(self._next_request_at, time.monotonic() + max(0.0, delay))

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
        read_only = method.upper() == "GET"
        error_type = InstallerError if read_only else UncertainWriteError
        for attempt in range(4):
            self._wait_for_rate_slot()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                exc.close()
                try:
                    parsed = json.loads(detail)
                except json.JSONDecodeError:
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                extra = parsed.get("additional_data") or {}
                if not isinstance(extra, dict):
                    extra = {}
                blocked = extra.get("rate_limit_reason") == "public_api_request_blocked"
                retryable = (exc.code in {429, 529} and not blocked) or (
                    read_only and exc.code in {500, 502, 503, 504}
                )
                wait = (exc.headers.get("Retry-After") if exc.headers else None) or extra.get("retry_after")
                try:
                    delay = float(wait) if wait is not None else 2 ** attempt
                except (ValueError, TypeError):
                    delay = 2 ** attempt
                if not math.isfinite(delay) or delay < 0:
                    delay = 2 ** attempt
                # Do not retry early when the workspace asks for a longer pause.
                if retryable and attempt < 3 and delay <= 60:
                    self._defer_requests(delay + random.uniform(0, 0.25))
                    continue
                message = str(parsed.get("message", detail)).replace(self.token, "[redacted]")
                if not read_only and exc.code >= 500 and exc.code != 529:
                    raise UncertainWriteError(f"Notion API {exc.code}: {message} (저장 여부 확인 필요)") from exc
                raise InstallerError(f"Notion API {exc.code}: {message}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                raise error_type("Notion API 연결이 끊겼습니다. 쓰기 작업은 저장 여부를 먼저 확인하세요.") from exc
            except UnicodeError as exc:
                raise error_type("Notion API 응답을 UTF-8로 읽지 못했습니다.") from exc
            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise error_type("Notion API 응답이 JSON이 아닙니다.") from exc
            if not isinstance(result, dict):
                raise error_type("Notion API 응답은 객체여야 합니다.")
            return result
        raise InstallerError("Notion API 재시도 한도를 초과했습니다.")

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
            "title": title_payload(title),
            "properties": properties,
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        return self.request("POST", "/databases", payload)

    def get_database(self, database_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/databases/{database_id}")

    def add_database_property(self, database_id: str, name: str) -> Dict[str, Any]:
        """Add the implementation key to an older fallback database."""
        return self.request("PATCH", f"/databases/{database_id}", {
            "properties": {name: {"rich_text": {}}},
        })

    def create_database_page(self, database_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        """Create a starter row in a database without requiring a user token in chat."""
        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        return self.request("POST", "/pages", payload)

    def query_database(self, database_id: str, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Read every row from a database, following Notion's pagination cursor."""
        body = dict(payload or {})
        rows: List[Dict[str, Any]] = []
        while True:
            response = self.request("POST", f"/databases/{database_id}/query", body)
            results = response.get("results", [])
            if isinstance(results, list):
                rows.extend(item for item in results if isinstance(item, dict))
            if not response.get("has_more"):
                return rows
            cursor = response.get("next_cursor")
            if not cursor:
                return rows
            body["start_cursor"] = cursor

    def update_page_properties(self, page_id: str, properties: Dict[str, Any],
                               expected_last_edited_time: Optional[str] = None) -> Dict[str, Any]:
        """Patch selected properties with an optional optimistic version check.

        Notion does not expose a transaction in this client. When a caller has a
        snapshot timestamp, reread immediately before PATCH and abort if another
        writer changed the page. The response is reread after PATCH so callers do
        not treat an acknowledged request as verified data.
        """
        if expected_last_edited_time is not None:
            current = self.get_page(page_id)
            actual = current.get("last_edited_time")
            if actual != expected_last_edited_time:
                raise ConflictError(
                    f"Notion 페이지가 먼저 변경되었습니다: {page_id} "
                    f"(기대 {expected_last_edited_time}, 현재 {actual})"
                )
        result = self.request("PATCH", f"/pages/{page_id}", {"properties": properties})
        try:
            verified = self.get_page(page_id)
        except Exception as exc:
            raise UncertainWriteError("속성 저장 후 확인 읽기에 실패했습니다. 저장 여부를 확인하세요.") from exc
        if not isinstance(verified, dict) or verified.get("id") != page_id:
            raise UncertainWriteError("속성 저장 결과를 다시 확인하지 못했습니다. 저장 여부를 확인하세요.")
        return verified

    def append_blocks(self, block_id: str, blocks: List[Dict[str, Any]]) -> None:
        for offset in range(0, len(blocks), BLOCK_BATCH_SIZE):
            self.request("PATCH", f"/blocks/{block_id}/children",
                         {"children": blocks[offset:offset + BLOCK_BATCH_SIZE]})


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
    # Every fallback-created database gets an internal stable key field. The
    # Notion view configuration should hide this implementation field from the
    # user's primary views while it remains available for deduplication.
    properties.setdefault(IDEMPOTENCY_PROPERTY, {"rich_text": {}})
    return properties


def property_value(spec: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    """Convert a manifest seed value to a Notion page property payload."""
    if spec.get("computed") or spec.get("type") in {"formula", "rollup", "relation"}:
        return None
    kind = spec.get("type")
    if value is None:
        if kind in {"title", "rich_text", "multi_select"}:
            return {kind: []}
        if kind == "checkbox":
            return {kind: False}
        return {kind: None}
    if kind == "title":
        return {"title": title_payload(str(value))}
    if kind == "rich_text":
        return {"rich_text": rich_text(str(value))}
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InstallerError(f"{spec['name']}은 유한한 숫자여야 합니다.")
        return {"number": value}
    if kind == "checkbox":
        if not isinstance(value, bool):
            raise InstallerError(f"{spec['name']}은 true 또는 false여야 합니다.")
        return {"checkbox": value}
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
    values = row.get("values", {})
    if not isinstance(values, dict):
        raise InstallerError(f"{database['key']} seed 행의 values는 객체여야 합니다.")
    for name, value in values.items():
        spec = specs.get(name)
        if not spec:
            raise InstallerError(f"{database['key']} seed 행의 속성을 찾을 수 없습니다: {name}")
        encoded = property_value(spec, value)
        if encoded is not None:
            properties[name] = encoded
    if row.get("souvenir_key"):
        properties[IDEMPOTENCY_PROPERTY] = {"rich_text": rich_text(str(row["souvenir_key"]))}
    return properties


def stable_key(*parts: Any) -> str:
    """Return a deterministic, non-sensitive key suitable for a Notion row."""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "souvenir:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_created(client: Any, result: Any) -> None:
    """Confirm a newly created Notion page is readable before checkpointing it."""
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise UncertainWriteError("Notion 생성 응답에 페이지 ID가 없습니다. 저장 여부를 확인하세요.")
    try:
        verified = client.get_page(result["id"])
    except UncertainWriteError:
        raise
    except Exception as exc:  # A successful create followed by a failed read is uncertain.
        raise UncertainWriteError("생성 후 확인 읽기에 실패했습니다. 저장 여부를 확인하세요.") from exc
    if not isinstance(verified, dict) or verified.get("id") != result["id"]:
        raise UncertainWriteError("생성된 페이지를 다시 확인하지 못했습니다. 저장 여부를 확인하세요.")


def find_existing_row(client: Any, database_id: str, key: str) -> Optional[str]:
    """Find exactly one row with a stable key; never create blindly on ambiguity."""
    query = getattr(client, "query_database", None)
    if not callable(query):
        return None
    rows = query(database_id, {
        "filter": {
            "property": IDEMPOTENCY_PROPERTY,
            "rich_text": {"equals": key},
        },
        "page_size": 2,
    })
    if len(rows) > 1:
        raise ConflictError(f"동일한 작업 키를 가진 Notion 행이 여러 개입니다: {key}")
    if rows and isinstance(rows[0].get("id"), str):
        return rows[0]["id"]
    return None


def ensure_database_row(mapping: Dict[str, Any], path: Path, client: Any,
                        database_id: str, properties: Dict[str, Any],
                        operation: str, key: str,
                        record: Callable[[Dict[str, Any]], None]) -> None:
    """Reuse a row by key or create it once with journal + verification."""
    existing = find_existing_row(client, database_id, key)
    if existing:
        record({"id": existing})
        write_json(path, mapping)
        return
    recorded_write(
        mapping,
        path,
        operation,
        lambda: client.create_database_page(database_id, properties),
        record,
        verify=lambda created: verify_created(client, created),
        idempotency_key=key,
    )


def ensure_idempotency_schema(mapping: Dict[str, Any], path: Path, client: Any) -> None:
    """Migrate older fallback databases before any key-based create/query."""
    get_database = getattr(client, "get_database", None)
    add_property = getattr(client, "add_database_property", None)
    if not callable(get_database) or not callable(add_property):
        raise InstallerError("Souvenir 작업 키를 확인하려면 Notion 데이터베이스 읽기·수정 권한이 필요합니다.")
    if mapping.get("idempotency_schema") == 1:
        return
    for key, database_id in mapping.get("databases", {}).items():
        current = get_database(database_id)
        properties = current.get("properties", {}) if isinstance(current, dict) else {}
        if not isinstance(properties, dict):
            raise InstallerError(f"데이터베이스 속성을 읽지 못했습니다: {key}")
        if IDEMPOTENCY_PROPERTY in properties:
            continue
        recorded_write(
            mapping,
            path,
            f"schema:{key}:{IDEMPOTENCY_PROPERTY}",
            lambda database_id=database_id: add_property(database_id, IDEMPOTENCY_PROPERTY),
            lambda _: None,
            verify=lambda _: _verify_database_property(client, database_id),
            idempotency_key=stable_key("schema", mapping.get("template_key"), key, IDEMPOTENCY_PROPERTY),
        )
    mapping["idempotency_schema"] = 1
    write_json(path, mapping)


def _verify_database_property(client: Any, database_id: str) -> None:
    current = client.get_database(database_id)
    properties = current.get("properties", {}) if isinstance(current, dict) else {}
    if IDEMPOTENCY_PROPERTY not in properties:
        raise UncertainWriteError("Souvenir 작업 키 속성을 저장 후 확인하지 못했습니다.")


def operation_database_properties() -> Dict[str, Any]:
    return {
        "작업": {"title": {}},
        "작업 ID": {"rich_text": {}},
        "여행 키": {"rich_text": {}},
        "작업 종류": {"rich_text": {}},
        "작성자": {"rich_text": {}},
        "상태": {"select": {"options": [
            {"name": "대기"}, {"name": LEASE_ACTIVE}, {"name": LEASE_COMPLETED},
            {"name": LEASE_CONFLICT}, {"name": LEASE_REVIEW},
        ]}},
        "시작": {"date": {}},
        "만료": {"date": {}},
        "대상": {"rich_text": {}},
        "결과": {"rich_text": {}},
        IDEMPOTENCY_PROPERTY: {"rich_text": {}},
    }


def ensure_operation_log(mapping: Dict[str, Any], path: Path, client: Any) -> str:
    """Create or reuse the hidden shared lease database for this installation."""
    existing = mapping.get("operation_log_database_id")
    if existing:
        get_database = getattr(client, "get_database", None)
        add_property = getattr(client, "add_database_property", None)
        if callable(get_database) and callable(add_property):
            current = get_database(existing)
            properties = current.get("properties", {}) if isinstance(current, dict) else {}
            if IDEMPOTENCY_PROPERTY not in properties:
                recorded_write(
                    mapping,
                    path,
                    "schema:operation_log:Souvenir 작업 키",
                    lambda: add_property(existing, IDEMPOTENCY_PROPERTY),
                    lambda _: None,
                    verify=lambda _: _verify_database_property(client, existing),
                    idempotency_key=stable_key("schema", mapping.get("template_key"), "operation_log"),
                )
        return existing
    install_log = mapping.get("pages", {}).get("install_log")
    if not install_log:
        raise InstallerError("작업 기록 DB를 만들 설치 기록 페이지가 없습니다.")
    recorded_write(
        mapping,
        path,
        "database:operation_log",
        lambda: client.create_database(install_log, OPERATION_LOG_TITLE, operation_database_properties(), "🧭"),
        lambda created: mapping.update({"operation_log_database_id": created["id"]}),
        verify=lambda created: verify_created(client, created),
        idempotency_key=stable_key("operation-log", mapping.get("template_key"), install_log),
    )
    write_json(path, mapping)
    return mapping["operation_log_database_id"]


def _plain_property_text(properties: Dict[str, Any], name: str) -> str:
    prop = properties.get(name, {}) if isinstance(properties, dict) else {}
    values = prop.get("title") or prop.get("rich_text") or []
    if not isinstance(values, list):
        return ""
    return "".join(
        item.get("plain_text") or item.get("text", {}).get("content", "")
        for item in values if isinstance(item, dict)
    )


def _select_property_name(properties: Dict[str, Any], name: str) -> str:
    prop = properties.get(name, {}) if isinstance(properties, dict) else {}
    value = prop.get("select")
    return value.get("name", "") if isinstance(value, dict) else ""


def _date_property_start(properties: Dict[str, Any], name: str) -> Optional[str]:
    prop = properties.get(name, {}) if isinstance(properties, dict) else {}
    value = prop.get("date")
    return value.get("start") if isinstance(value, dict) else None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso_datetime(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _active_lease_rows(rows: List[Dict[str, Any]], now: dt.datetime) -> List[Dict[str, Any]]:
    active: List[Dict[str, Any]] = []
    for row in rows:
        properties = row.get("properties", {}) if isinstance(row, dict) else {}
        if _select_property_name(properties, "상태") != LEASE_ACTIVE:
            continue
        expiry = _date_property_start(properties, "만료")
        try:
            expires_at = dt.datetime.fromisoformat((expiry or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at > now:
            active.append(row)
    return active


def _expire_lease_rows(client: Any, rows: List[Dict[str, Any]], now: dt.datetime) -> None:
    """Mark expired claims for audit before a new claim is created."""
    for row in rows:
        properties = row.get("properties", {}) if isinstance(row, dict) else {}
        if _select_property_name(properties, "상태") != LEASE_ACTIVE:
            continue
        expiry = _date_property_start(properties, "만료")
        try:
            expires_at = dt.datetime.fromisoformat((expiry or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at > now:
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            continue
        client.update_page_properties(row_id, {
            "상태": {"select": {"name": LEASE_REVIEW}},
            "결과": {"rich_text": rich_text("lease 만료 후 재획득")},
        })


def claim_operation(mapping: Dict[str, Any], path: Path, client: Any, trip_key: str,
                    operation_type: str, owner: str, target: str = "",
                    lease_minutes: int = LEASE_DEFAULT_MINUTES,
                    now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Claim an advisory Notion lease for one trip write batch.

    Notion does not provide compare-and-swap for creating a lease row. We query
    active rows, create one, then query again. If the verification sees multiple
    live owners, this claim is marked as a conflict and no trip write proceeds.
    """
    if type(lease_minutes) is not int or lease_minutes < 1 or lease_minutes > 1440:
        raise InstallerError("작업 lease 시간은 1~1440분이어야 합니다.")
    database_id = mapping.get("operation_log_database_id") or ensure_operation_log(mapping, path, client)
    current = now or _utc_now()
    expires = current + dt.timedelta(minutes=lease_minutes)
    filters = {"and": [
        {"property": "여행 키", "rich_text": {"equals": trip_key}},
        {"property": "상태", "select": {"equals": LEASE_ACTIVE}},
    ]}
    candidates = client.query_database(database_id, {"filter": filters, "page_size": 20})
    _expire_lease_rows(client, candidates, current)
    existing = _active_lease_rows(candidates, current)
    if existing:
        other = _plain_property_text(existing[0].get("properties", {}), "작성자") or "다른 세션"
        raise ConflictError(f"이 여행에 이미 반영 중인 작업이 있습니다: {other}")

    operation_id = uuid.uuid4().hex
    key = stable_key("lease", trip_key, operation_id)
    properties = {
        "작업": {"title": title_payload(f"{operation_type} · {operation_id[:8]}")},
        "작업 ID": {"rich_text": rich_text(operation_id)},
        "여행 키": {"rich_text": rich_text(trip_key)},
        "작업 종류": {"rich_text": rich_text(operation_type)},
        "작성자": {"rich_text": rich_text(owner)},
        "상태": {"select": {"name": LEASE_ACTIVE}},
        "시작": {"date": {"start": _iso_datetime(current)}},
        "만료": {"date": {"start": _iso_datetime(expires)}},
        "대상": {"rich_text": rich_text(target)},
        "결과": {"rich_text": []},
        IDEMPOTENCY_PROPERTY: {"rich_text": rich_text(key)},
    }
    holder: Dict[str, Any] = {}
    recorded_write(
        mapping,
        path,
        f"lease:claim:{operation_id}",
        lambda: client.create_database_page(database_id, properties),
        lambda created: holder.update({"page_id": created["id"]}),
        verify=lambda created: verify_created(client, created),
        idempotency_key=key,
    )
    mapping.setdefault("active_leases", {})[operation_id] = {
        "page_id": holder["page_id"],
        "trip_key": trip_key,
        "expires_at": _iso_datetime(expires),
        "owner": owner,
    }
    write_json(path, mapping)

    confirmed = _active_lease_rows(client.query_database(database_id, {"filter": filters, "page_size": 20}), current)
    if len(confirmed) != 1 or confirmed[0].get("id") != holder["page_id"]:
        try:
            client.update_page_properties(holder["page_id"], {
                "상태": {"select": {"name": LEASE_CONFLICT}},
                "결과": {"rich_text": rich_text("동시 claim 충돌")},
            })
        finally:
            mapping.get("active_leases", {}).pop(operation_id, None)
            write_json(path, mapping)
        raise ConflictError("동시에 작업 lease를 획득한 세션이 있어 반영을 중단했습니다.")
    return {
        "operation_id": operation_id,
        "page_id": holder["page_id"],
        "trip_key": trip_key,
        "expires_at": _iso_datetime(expires),
        "owner": owner,
    }


def release_operation(mapping: Dict[str, Any], path: Path, client: Any, lease: Dict[str, Any],
                      status: str = LEASE_COMPLETED, result: str = "") -> None:
    if status not in {LEASE_COMPLETED, LEASE_CONFLICT, LEASE_REVIEW}:
        raise InstallerError(f"지원하지 않는 작업 lease 종료 상태입니다: {status}")
    # The protected operation may have reloaded and checkpointed a newer
    # mapping while the context manager still holds its original dictionary.
    # Always merge the release into the latest on-disk checkpoint so the lease
    # update cannot erase scaffold/install progress.
    latest = read_json(path)
    client.update_page_properties(lease["page_id"], {
        "상태": {"select": {"name": status}},
        "결과": {"rich_text": rich_text(result)},
    })
    latest.setdefault("active_leases", {}).pop(lease.get("operation_id"), None)
    write_json(path, latest)
    mapping.clear()
    mapping.update(latest)


def _best_effort_release(mapping: Dict[str, Any], path: Path, client: Any,
                         lease: Dict[str, Any], status: str, result: str,
                         original_error: Optional[BaseException] = None) -> None:
    """Record lease completion without masking the operation that failed."""
    try:
        release_operation(mapping, path, client, lease, status, result)
    except Exception as exc:
        try:
            latest = read_json(path)
        except Exception:
            latest = dict(mapping)
        latest.setdefault("lease_release_errors", []).append({
            "operation_id": lease.get("operation_id"),
            "status": status,
            "error": str(exc),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        latest.setdefault("active_leases", {}).pop(lease.get("operation_id"), None)
        try:
            write_json(path, latest)
            mapping.clear()
            mapping.update(latest)
        except Exception:
            pass
        if original_error is None:
            raise UncertainWriteError(
                "작업은 끝났지만 작업 기록의 종료 상태를 확인하지 못했습니다. Notion 작업 기록을 확인하세요."
            ) from exc


@contextmanager
def operation_lease(mapping: Dict[str, Any], path: Path, client: Any, trip_key: str,
                    operation_type: str, owner: str, target: str = "") -> Any:
    lease = claim_operation(mapping, path, client, trip_key, operation_type, owner, target)
    try:
        yield lease
    except ConflictError as exc:
        _best_effort_release(mapping, path, client, lease, LEASE_CONFLICT, str(exc), exc)
        raise
    except Exception as exc:
        _best_effort_release(mapping, path, client, lease, LEASE_REVIEW, str(exc), exc)
        raise
    else:
        _best_effort_release(mapping, path, client, lease, LEASE_COMPLETED, "작업 완료")


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
        existing_titles = {item["title"] for item in existing}
        for row in rows:
            if row["title"] in existing_titles:
                continue
            row = dict(row)
            row["souvenir_key"] = stable_key("seed", mapping.get("template_key"), key, row["title"])
            properties = seed_properties(db_by_key[key], row)
            ensure_database_row(
                mapping,
                mapping_path,
                client,
                mapping["databases"][key],
                properties,
                f"seed:{key}:{row['title']}",
                row["souvenir_key"],
                lambda created, title=row["title"]: existing.append({"title": title, "id": created["id"]}),
            )
            existing_titles.add(row["title"])


def database_by_key(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    try:
        return next(database for database in manifest["databases"] if database.get("key") == key)
    except StopIteration as exc:
        raise InstallerError(f"매니페스트에서 데이터베이스를 찾지 못했습니다: {key}") from exc


def parse_date(value: Optional[str], label: str) -> Optional[dt.date]:
    if not value:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InstallerError(f"{label}은 YYYY-MM-DD 형식이어야 합니다: {value}") from exc


def preview_trip_key(manifest: Dict[str, Any], destination: str,
                     start_date: Optional[str], end_date: Optional[str],
                     duration_days: Optional[int], traveler_count: int,
                     currency: str, trip_name: Optional[str] = None) -> str:
    """Validate scaffold inputs and derive the same identity used by scaffold."""
    destination = destination.strip()
    if not destination:
        raise InstallerError("여행 목적지를 입력하세요.")
    start = parse_date(start_date, "출발일")
    end = parse_date(end_date, "종료일")
    if start and end and end < start:
        raise InstallerError("종료일은 출발일보다 빠를 수 없습니다.")
    derived_days = (end - start).days + 1 if start and end else None
    if duration_days is not None and (type(duration_days) is not int or duration_days < 1):
        raise InstallerError("기간(일)은 1 이상의 정수여야 합니다.")
    if derived_days and duration_days and derived_days != duration_days:
        raise InstallerError("출발일·종료일과 기간(일)이 서로 다릅니다. 하나의 기준으로 다시 입력하세요.")
    days = derived_days or duration_days
    if not days:
        raise InstallerError("출발일·종료일 또는 기간(일) 중 하나가 필요합니다.")
    if type(traveler_count) is not int or traveler_count < 0:
        raise InstallerError("여행 인원은 0 이상이어야 합니다.")
    if currency not in {"KRW", "JPY", "USD", "EUR", "미정"}:
        raise InstallerError("기준 통화는 KRW·JPY·USD·EUR·미정 중 하나여야 합니다.")
    if start and not end:
        end = start + dt.timedelta(days=days - 1)
    elif end and not start:
        start = end - dt.timedelta(days=days - 1)
    trip_title = (trip_name or f"✈️ {destination} 여행").strip()
    fingerprint = json.dumps([
        destination,
        start.isoformat() if start else None,
        end.isoformat() if end else None,
        days,
        traveler_count,
        currency,
        trip_title,
    ], ensure_ascii=False)
    return stable_key("trip", manifest.get("template_key"), fingerprint)


def _scaffold_trip_unlocked(
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
    check_pending(mapping)
    check_manifest_version(mapping, manifest)
    if mapping.get("template_key") != manifest.get("template_key"):
        raise InstallerError("설치 기록과 매니페스트의 template_key가 다릅니다.")
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 install을 완료해 .souvenir-install.json에 대상 DB 매핑을 만들어야 합니다.")
    for key in ("trip", "itinerary", "traveler", "wallet", "task", "packing"):
        if key not in mapping.get("databases", {}):
            raise InstallerError(f"설치 기록에 {key} 데이터베이스 매핑이 없습니다.")

    destination = destination.strip()
    if not destination:
        raise InstallerError("여행 목적지를 입력하세요.")

    start = parse_date(start_date, "출발일")
    end = parse_date(end_date, "종료일")
    if start and end and end < start:
        raise InstallerError("종료일은 출발일보다 빠를 수 없습니다.")
    derived_days = (end - start).days + 1 if start and end else None
    if duration_days is not None and (type(duration_days) is not int or duration_days < 1):
        raise InstallerError("기간(일)은 1 이상의 정수여야 합니다.")
    if derived_days and duration_days and derived_days != duration_days:
        raise InstallerError("출발일·종료일과 기간(일)이 서로 다릅니다. 하나의 기준으로 다시 입력하세요.")
    days = derived_days or duration_days
    if not days:
        raise InstallerError("출발일·종료일 또는 기간(일) 중 하나가 필요합니다.")
    if type(traveler_count) is not int or traveler_count < 0:
        raise InstallerError("여행 인원은 0 이상이어야 합니다.")
    allowed_currency = {"KRW", "JPY", "USD", "EUR", "미정"}
    if currency not in allowed_currency:
        raise InstallerError("기준 통화는 KRW·JPY·USD·EUR·미정 중 하나여야 합니다.")

    try:
        if start and not end:
            end = start + dt.timedelta(days=days - 1)
        elif end and not start:
            start = end - dt.timedelta(days=days - 1)
    except OverflowError as exc:
        raise InstallerError("여행 기간이 지원하는 날짜 범위를 넘었습니다.") from exc

    trip_title = (trip_name or f"✈️ {destination} 여행").strip()
    fingerprint = json.dumps([destination, start.isoformat() if start else None,
                              end.isoformat() if end else None, days, traveler_count,
                              currency, trip_title], ensure_ascii=False)
    trip_key = stable_key("trip", manifest.get("template_key"), fingerprint)
    scaffolded = mapping.setdefault("scaffolded_trips", {})
    legacy_key = "|".join([destination, start_date or "", end_date or "",
                           str(days), str(traveler_count), currency])
    legacy = scaffolded.get(legacy_key)
    if legacy and legacy.get("trip_name") == trip_title and "status" not in legacy:
        return legacy
    if fingerprint in scaffolded and scaffolded[fingerprint].get("status") == "complete":
        return scaffolded[fingerprint]

    databases = {key: database_by_key(manifest, key)
                 for key in ("trip", "itinerary", "traveler", "wallet", "task", "packing")}
    trip_values: Dict[str, Any] = {
        "목적지": destination,
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
        "souvenir_key": trip_key,
        "values": {key: value for key, value in trip_values.items() if value is not None},
    }
    result: Dict[str, Any] = scaffolded.setdefault(fingerprint, {
        "status": "in_progress",
        "trip_id": None,
        "trip_name": trip_title,
        "destination": destination,
        "days": days,
        "traveler_count": traveler_count,
        "currency": currency,
        "itinerary": [],
        "travelers": [],
        "wallet": None,
        "tasks": [],
        "packing": [],
    })
    trip_properties = seed_properties(databases["trip"], trip_row)
    # Encode every row before creating anything, so a schema error cannot leave
    # a partially-created trip. The real trip ID is substituted after creation.
    trip_id = result["trip_id"] or "pending-trip-id"
    rows: List[tuple[str, str, Dict[str, Any]]] = []

    for day in range(1, days + 1):
        values: Dict[str, Any] = {"여행 키": trip_id, "일차": day, "상태": "계획"}
        if start:
            values["날짜"] = (start + dt.timedelta(days=day - 1)).isoformat()
        row = {"title": f"{day}일차 계획", "souvenir_key": stable_key("trip", trip_key, "itinerary", day), "values": values}
        rows.append(("itinerary", "itinerary", row))

    for index in range(1, traveler_count + 1):
        row = {
            "title": f"동행자 {index}",
            "souvenir_key": stable_key("trip", trip_key, "traveler", index),
            "values": {"여행 키": trip_id, "역할": "여행자", "상태": "참여"},
        }
        rows.append(("traveler", "travelers", row))

    wallet_row = {
        "title": f"{destination} 여행 지갑",
        "souvenir_key": stable_key("trip", trip_key, "wallet"),
        "values": {"여행 키": trip_id, "통화": currency, "금액 입력 완료": False},
    }
    rows.append(("wallet", "wallet", wallet_row))

    for key in ("task", "packing"):
        for starter in manifest.get("seed_rows", {}).get(key, []):
            values = dict(starter.get("values", {}))
            values["여행 키"] = trip_id
            values["원본 구분"] = "사용 중"
            row = {
                "title": starter["title"],
                "souvenir_key": stable_key("trip", trip_key, key, starter["title"]),
                "values": values,
            }
            rows.append((key, "tasks" if key == "task" else "packing", row))

    for key, _, row in rows:
        seed_properties(databases[key], row)
    if not result["trip_id"]:
        existing_trip = find_existing_row(client, mapping["databases"]["trip"], trip_key)
        if existing_trip:
            result["trip_id"] = existing_trip
            write_json(mapping_path, mapping)
        else:
            recorded_write(
                mapping,
                mapping_path,
                "scaffold:trip",
                lambda: client.create_database_page(mapping["databases"]["trip"], trip_properties),
                lambda created: result.update(trip_id=created["id"]),
                verify=lambda created: verify_created(client, created),
                idempotency_key=trip_key,
            )
    completed = {field: len(result[field]) for field in ("itinerary", "travelers", "tasks", "packing")}
    seen: Dict[str, int] = {}
    for key, field, row in rows:
        index = seen.get(field, 0)
        seen[field] = index + 1
        if (field == "wallet" and result[field]) or index < completed.get(field, 0):
            continue
        row["values"]["여행 키"] = result["trip_id"]
        properties = seed_properties(databases[key], row)

        def record(created: Dict[str, Any]) -> None:
            if field == "wallet":
                result[field] = created["id"]
            else:
                result[field].append(created["id"])

        ensure_database_row(
            mapping,
            mapping_path,
            client,
            mapping["databases"][key],
            properties,
            f"scaffold:{field}:{index}",
            row["souvenir_key"],
            record,
        )

    result["status"] = "complete"
    write_json(mapping_path, mapping)
    return result


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
    """Create/reuse a trip while holding both local write locks."""
    mapping = read_json(mapping_path)
    workspace_key = str(mapping.get("parent_page_id") or "unknown")
    with operation_locks(mapping_path, workspace_key, "scaffold"):
        mapping = read_json(mapping_path)
        ensure_operation_log(mapping, mapping_path, client)
        trip_key = preview_trip_key(
            manifest, destination, start_date, end_date, duration_days,
            traveler_count, currency, trip_name,
        )
        owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
        with operation_lease(mapping, mapping_path, client, trip_key, "scaffold", owner, destination):
            return _scaffold_trip_unlocked(
                manifest,
                client,
                mapping_path,
                destination,
                start_date,
                end_date,
                duration_days,
                traveler_count,
                currency,
                trip_name,
            )


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
                "properties": len(database_properties(database)),
                "deferred_properties": [prop["name"] for prop in database.get("properties", [])
                                        if api_property(prop) is None],
                "views": database.get("views", []),
            }
            for database in databases
        ],
        "notes": [
            "원본 Notion 페이지·ID·사용자 데이터는 읽지 않습니다.",
            "Notion Marketplace 템플릿이 주 원본이며, 공개·복제 가능한 링크가 있으면 반드시 그 경로를 먼저 사용하세요.",
            "코드 재구축에서는 데이터베이스 구조를 만들고 뷰 이름을 안내합니다. 뷰의 실제 배치·관계·수식은 Souvenir 스킬 또는 Notion에서 후속 구성합니다.",
            "기본 준비할 일·준비물 행은 개인 여행에 연결하지 않은 starter 행으로 한 번만 생성합니다.",
            "install·scaffold는 매핑·상위 페이지 잠금을 잡고, 생성 행에는 Souvenir 작업 키를 넣어 중복을 확인합니다.",
        ],
        "seed_rows": {key: len(rows) for key, rows in manifest.get("seed_rows", {}).items()},
    }


def _install_unlocked(manifest: Dict[str, Any], client: NotionClient, parent_id: str, mapping_path: Path) -> Dict[str, Any]:
    errors = validate_manifest(manifest)
    if errors:
        raise InstallerError("매니페스트 검증 실패:\n- " + "\n- ".join(errors))
    parent_id = normalize_id(parent_id)
    mapping: Dict[str, Any] = {}
    if mapping_path.exists():
        mapping = read_json(mapping_path)
        if mapping.get("template_key") != manifest.get("template_key"):
            raise InstallerError("기존 설치 기록은 다른 템플릿입니다. 새 --mapping 경로를 지정하세요.")
        if normalize_id(mapping.get("parent_page_id", "")) != parent_id:
            raise InstallerError("기존 설치 기록의 상위 페이지가 다릅니다. 대상별로 다른 --mapping 경로를 사용하세요.")
    check_pending(mapping)
    check_manifest_version(mapping, manifest)
    content_rendered = bool(mapping.get("content_rendered"))
    log_rendered = bool(mapping.get("log_rendered"))
    mapping.setdefault("template_key", manifest.get("template_key"))
    mapping.setdefault("template_source", "code_rebuild")
    mapping["status"] = "in_progress"
    mapping.setdefault("pages", {})
    mapping.setdefault("databases", {})
    mapping["parent_page_id"] = parent_id
    mapping["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    client.get_page(parent_id)
    write_json(mapping_path, mapping)

    page_by_key = {page["key"]: page for page in manifest["pages"]}

    def ensure_page(key: str) -> str:
        if key in mapping["pages"]:
            return mapping["pages"][key]
        page = page_by_key[key]
        parent = parent_id if page["parent"] == "target" else ensure_page(page["parent"])
        recorded_write(mapping, mapping_path, f"page:{key}",
                       lambda: client.create_page(parent, page["title"], page.get("icon")),
                       lambda created: mapping["pages"].update({key: created["id"]}),
                       verify=lambda created: verify_created(client, created),
                       idempotency_key=stable_key("page", mapping.get("template_key"), key, parent))
        return mapping["pages"][key]

    for page in manifest["pages"]:
        ensure_page(page["key"])

    for database in manifest["databases"]:
        key = database["key"]
        mapping.setdefault("computed_properties", {})[key] = [
            prop["name"] for prop in database.get("properties", []) if api_property(prop) is None
        ]
        mapping.setdefault("views", {})[key] = database.get("views", [])
        if key in mapping["databases"]:
            continue
        parent = ensure_page(database["parent"])
        properties = database_properties(database)
        recorded_write(mapping, mapping_path, f"database:{key}",
                       lambda: client.create_database(parent, database["title"], properties, database.get("icon")),
                       lambda created: mapping["databases"].update({key: created["id"]}),
                       verify=lambda created: verify_created(client, created),
                       idempotency_key=stable_key("database", mapping.get("template_key"), key, parent))

    ensure_idempotency_schema(mapping, mapping_path, client)
    ensure_operation_log(mapping, mapping_path, client)
    seed_default_rows(manifest, client, mapping, mapping_path)

    def render(key: str, blocks: List[Dict[str, Any]]) -> None:
        progress = mapping.setdefault("rendered_blocks", {})
        offset = progress.get(key, 0)
        while offset < len(blocks):
            batch = blocks[offset:offset + BLOCK_BATCH_SIZE]
            recorded_write(mapping, mapping_path, f"content:{key}:{offset}",
                           lambda: client.append_blocks(mapping["pages"][key], batch),
                           lambda _: progress.update({key: offset + len(batch)}))
            offset += len(batch)

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
        render("dashboard", dashboard_blocks)

        database_titles = {database["key"]: database["title"] for database in manifest["databases"]}
        for page in manifest["pages"]:
            role = page.get("role")
            if role == "group":
                blocks: List[Dict[str, Any]] = [callout_block(page["title"], "입력 항목은 먼저 작성하고, 자동 결과·보조 속성은 상세 화면에서 확인하세요.", page.get("icon", "🗂️"), page.get("color", "gray_background"))]
                for key in page.get("database_keys", []):
                    blocks.append(link_block(mapping["databases"][key], "database_id"))
                render(page["key"], blocks)
            elif role == "planning_zone":
                blocks = [callout_block(page["title"], "출발 전 작성·수정 공간입니다. 여행 중에는 확인 존을 먼저 보고, 계획을 바꿀 때만 이곳으로 들어오세요.", page.get("icon", "🛠️"), page.get("color", "blue_background"))]
                for child in page.get("children", []):
                    if child in mapping["pages"]:
                        blocks.append(link_block(mapping["pages"][child], "page_id"))
                render(page["key"], blocks)
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
                render(page["key"], blocks)
        mapping["content_rendered"] = True
        write_json(mapping_path, mapping)

    log_id = mapping["pages"].get("install_log")
    if log_id and not log_rendered:
        summary = json.dumps({"status": "완료", "template_source": "code_rebuild", "pages": mapping["pages"], "databases": mapping["databases"]}, ensure_ascii=False)
        render("install_log", [heading_block("설치 기록", 2), text_block("Souvenir 코드 설치가 완료되었습니다."), text_block(summary)])
        mapping["log_rendered"] = True

    mapping["status"] = "complete"
    mapping["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(mapping_path, mapping)
    return mapping


def install(manifest: Dict[str, Any], client: NotionClient, parent_id: str, mapping_path: Path) -> Dict[str, Any]:
    """Install/reuse a template while holding both local write locks."""
    normalized_parent = normalize_id(parent_id)
    with operation_locks(mapping_path, normalized_parent, "install"):
        return _install_unlocked(manifest, client, normalized_parent, mapping_path)


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
    configure_output()
    raise SystemExit(main())
