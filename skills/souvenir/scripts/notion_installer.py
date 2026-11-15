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
import concurrent.futures
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

try:
    from coordinator import CoordinatorClient, CoordinatorError
except ModuleNotFoundError:  # The installer is also loaded directly by offline tests.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from coordinator import CoordinatorClient, CoordinatorError


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


class PerformanceMetrics:
    """Small, secret-free timing recorder for one local operation.

    The installer deliberately keeps this dependency-free.  Metrics are kept
    in memory and returned with a save result so the host can explain where a
    delay came from without logging tokens, page contents, or URLs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.started_at = time.perf_counter()
            self.requests = 0
            self.retries = 0
            self.request_elapsed_ms = 0.0
            self.phases: Dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Any:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                self.phases[name] = self.phases.get(name, 0.0) + elapsed

    def record_request(self, attempts: int, elapsed_ms: float) -> None:
        with self._lock:
            self.requests += 1
            self.retries += max(0, attempts - 1)
            self.request_elapsed_ms += elapsed_ms

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "request_ms": round(self.request_elapsed_ms, 1),
                "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 1),
                "phases_ms": {key: round(value, 1) for key, value in self.phases.items()},
            }


def _operation_metrics(client: Any) -> Optional[PerformanceMetrics]:
    metrics = getattr(client, "metrics", None)
    return metrics if isinstance(metrics, PerformanceMetrics) else None


def _performance_result(client: Any) -> Dict[str, Any]:
    metrics = _operation_metrics(client)
    return {"performance": metrics.snapshot()} if metrics is not None else {}


def _coordinator_client() -> Optional[CoordinatorClient]:
    endpoint = os.environ.get("SOUVENIR_COORDINATOR_URL", "").strip()
    if not endpoint:
        return None
    return CoordinatorClient(endpoint, os.environ.get("SOUVENIR_COORDINATOR_TOKEN"))


@contextmanager
def distributed_lease(workspace_key: str, trip_key: str, operation_type: str,
                      owner: str, target: str = "", request_key: Optional[str] = None) -> Any:
    """Use P2 coordination when configured; fail closed if it is unavailable."""
    client = _coordinator_client()
    if client is None:
        yield None
        return
    try:
        with client.lease(
            workspace_key=workspace_key,
            trip_key=trip_key,
            operation_type=operation_type,
            owner=owner,
            target=target,
            lease_seconds=int(os.environ.get("SOUVENIR_COORDINATOR_LEASE_SECONDS", "600")),
            request_key=request_key or os.environ.get("SOUVENIR_OPERATION_ID") or uuid.uuid4().hex,
        ) as lease:
            yield lease
    except CoordinatorError as exc:
        raise InstallerError(f"중앙 Souvenir coordinator 작업을 시작하지 못했습니다: {exc}") from exc


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
    pending_value = mapping.get("pending_operation") or mapping.get("pending_operations")
    if pending_value:
        pending = pending_value
        if isinstance(pending, dict) and "key" in pending:
            label = pending.get("key", "알 수 없는 작업")
        elif isinstance(pending, dict):
            label = f"{len(pending)}개 일괄 작업"
        elif isinstance(pending, list):
            label = f"{len(pending)}개 일괄 작업"
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


class BatchWriteJournal:
    """Thread-safe checkpoint journal for bounded batch writes.

    A single legacy ``pending_operation`` field is intentionally kept for
    sequential operations.  Parallel rows use ``pending_operations`` so a
    crash or uncertain response cannot be mistaken for a clean commit.
    """

    def __init__(self, mapping: Dict[str, Any], path: Path,
                 entries: Sequence[tuple[str, Optional[str]]]) -> None:
        self.mapping = mapping
        self.path = path
        self._lock = threading.RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        for key, idempotency_key in entries:
            self._pending[key] = {
                "key": key,
                "operation_id": uuid.uuid4().hex,
                "idempotency_key": idempotency_key,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "pending",
            }
        self._flush()

    def _flush(self) -> None:
        with self._lock:
            if self._pending:
                self.mapping["pending_operations"] = copy_json(self._pending)
            else:
                self.mapping.pop("pending_operations", None)
            write_json(self.path, self.mapping)

    def _mark(self, key: str, status: str) -> None:
        with self._lock:
            if key in self._pending:
                if status == "completed":
                    self._pending.pop(key, None)
                else:
                    self._pending[key]["status"] = status
                self._flush()

    def run(self, key: str, action: Callable[[], Any],
            record: Callable[[Any], None],
            verify: Optional[Callable[[Any], None]] = None) -> Any:
        try:
            result = action()
            if verify is not None:
                verify(result)
        except UncertainWriteError:
            self._mark(key, "uncertain")
            raise
        except InstallerError:
            self._mark(key, "rejected")
            raise
        record(result)
        self._mark(key, "completed")
        return result


def copy_json(value: Any) -> Any:
    """Copy JSON-compatible checkpoint data without importing copy globally."""
    return json.loads(json.dumps(value, ensure_ascii=False))


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
    # A fresh install must never surface a generic sample trip.  The first
    # real trip is created only after the user supplies its destination and
    # dates; placeholder rows make dashboard filters point at stale data.
    placeholder_titles = {"새 여행", "나의 다음 여행", "✈️ 새 여행"}
    for key, rows in (manifest.get("seed_rows") or {}).items():
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("title") in placeholder_titles:
                errors.append(f"seed_rows.{key}에 일반 여행 placeholder를 넣을 수 없습니다: {row['title']}")
    # Every row-level database that can appear in a trip view needs the same
    # scope key.  This is what prevents a new workspace from showing the
    # previous trip's companion, shopping, or dashboard rows.
    scoped_databases = {
        "itinerary", "place", "shopping", "weather", "schedule_check",
        "long_transport", "local_transport", "transport_service", "stay",
        "reservation", "wallet", "budget", "expense", "traveler", "split",
        "settlement", "decision", "task", "packing", "material",
    }
    for database in databases or []:
        if not isinstance(database, dict) or database.get("key") not in scoped_databases:
            continue
        names = {prop.get("name") for prop in database.get("properties", []) if isinstance(prop, dict)}
        if "여행 키" not in names:
            errors.append(f"데이터베이스 {database.get('key')}에는 여행 키 속성이 필요합니다.")
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
        self.metrics = PerformanceMetrics()
        self._souvenir_schema_cache: Dict[str, Dict[str, Any]] = {}
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
        started = time.perf_counter()
        attempts = 0
        try:
            for attempt in range(4):
                attempts += 1
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
        finally:
            self.metrics.record_request(attempts, (time.perf_counter() - started) * 1000)

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
        result = self.request("PATCH", f"/databases/{database_id}", {
            "properties": {name: {"rich_text": {}}},
        })
        self._souvenir_schema_cache.pop(f"__schema__:{database_id}", None)
        return result

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
                               expected_last_edited_time: Optional[str] = None,
                               verify_response: bool = True) -> Dict[str, Any]:
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
        if not verify_response:
            return result
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


def verify_updated(result: Any, page_id: str) -> None:
    """Validate the already-reread response returned by an update call."""
    if not isinstance(result, dict) or result.get("id") != page_id:
        raise UncertainWriteError("수정된 쇼핑 행을 다시 확인하지 못했습니다. 저장 여부를 확인하세요.")


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


def _shopping_row_properties(manifest: Dict[str, Any], row: Dict[str, Any],
                             trip_id: str, operation_key: str,
                             include_empty: bool = False) -> Dict[str, Any]:
    """Encode one confirmed shopping row using the installed DB schema.

    Only fields present in the manifest are written.  This keeps the writer
    compatible with older copied templates while still preserving the source
    URL in the existing ``링크·메모`` field.
    """
    database = database_by_key(manifest, "shopping")
    specs = {item["name"]: item for item in database.get("properties", [])
             if isinstance(item, dict) and isinstance(item.get("name"), str)}
    item = str(row.get("품목", "")).strip()
    if not item:
        raise InstallerError("쇼핑 행에 품목명이 필요합니다.")
    properties: Dict[str, Any] = {database["title_property"]: {"title": title_payload(item)}}
    values = dict(row)
    values["여행 키"] = trip_id
    # The write endpoint is only called after the user has approved the
    # candidate, so make that state explicit in Notion.
    values["정보 상태"] = "사용자 확인 완료"
    values.pop("품목", None)
    for name, value in values.items():
        if name not in specs or name in {database["title_property"], "추가 확인", "저장 가능", "확인 상태"}:
            continue
        if value in (None, "") and not include_empty:
            continue
        encoded = property_value(specs[name], value)
        if encoded is not None:
            properties[name] = encoded
    properties[IDEMPOTENCY_PROPERTY] = {"rich_text": rich_text(operation_key)}
    return properties


def _shopping_row_key(trip_id: str, row: Dict[str, Any]) -> str:
    """Build a stable key that is safe to retry and scoped to one trip."""
    def value_text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    return stable_key(
        "shopping",
        trip_id,
        value_text(row.get("담당 여행자")) or "공용",
        value_text(row.get("품목")),
        value_text(row.get("구매 장소")),
    )


def _live_database_schema(client: Any, database_id: str,
                          cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read the live copied-template schema, not only the fallback manifest.

    The Marketplace template has relation/formula/select properties that are
    intentionally omitted from the portable manifest.  Using the live schema
    here lets writes use those properties so Notion's saved views can display
    the rows immediately.
    """
    cache = cache if cache is not None else {}
    key = f"__schema__:{database_id}"
    if key in cache:
        return cache[key]
    shared_cache = getattr(client, "_souvenir_schema_cache", None)
    if isinstance(shared_cache, dict) and key in shared_cache:
        cache[key] = shared_cache[key]
        return cache[key]
    getter = getattr(client, "get_database", None)
    if not callable(getter):
        cache[key] = {}
        return cache[key]
    try:
        payload = getter(database_id)
    except Exception:
        payload = {}
    properties = payload.get("properties", {}) if isinstance(payload, dict) else {}
    cache[key] = properties if isinstance(properties, dict) else {}
    if isinstance(shared_cache, dict):
        shared_cache[key] = cache[key]
    return cache[key]


def _trip_scope_filter(client: Any, database_id: str, trip_id: str,
                       cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a scope filter that matches either live relations or legacy keys."""
    schema = _live_database_schema(client, database_id, cache)
    relation = schema.get("여행") if isinstance(schema, dict) else None
    if isinstance(relation, dict) and relation.get("type") == "relation":
        return {"property": "여행", "relation": {"contains": trip_id}}
    if "여행 키" in schema:
        return {"property": "여행 키", "rich_text": {"equals": trip_id}}
    return {}


def _normalized_page_id(value: Any) -> str:
    match = UUID_RE.search(str(value or ""))
    return match.group(0).replace("-", "").lower() if match else ""


def _relation_ids(properties: Dict[str, Any], name: str = "여행") -> set[str]:
    prop = properties.get(name, {}) if isinstance(properties, dict) else {}
    values = prop.get("relation", []) if isinstance(prop, dict) else []
    if not isinstance(values, list):
        return set()
    return {
        normalized
        for item in values
        if isinstance(item, dict)
        for normalized in [_normalized_page_id(item.get("id") or item.get("url"))]
        if normalized
    }


def _row_belongs_to_trip(row: Dict[str, Any], trip_id: str) -> bool:
    properties = row.get("properties", {}) if isinstance(row, dict) else {}
    wanted = _normalized_page_id(trip_id)
    if wanted and wanted in _relation_ids(properties):
        return True
    return _plain_property_text(properties, "여행 키").strip() == str(trip_id).strip()


def _rows_for_trip(client: Any, database_id: str, trip_id: str,
                   cache: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    scope = _trip_scope_filter(client, database_id, trip_id, cache)
    try:
        rows = client.query_database(database_id, {"filter": scope} if scope else None)
    except Exception:
        # Some older copies reject relation filters even though the property is
        # present.  A full read is safe because the final local guard is still
        # applied before anything is returned to the caller.
        rows = client.query_database(database_id)
    return [row for row in rows if _row_belongs_to_trip(row, trip_id)]


def _shopping_scope_filter(trip_id: str) -> Dict[str, Any]:
    """Legacy helper kept for callers/tests that only have a trip key."""
    return {"property": "여행 키", "rich_text": {"equals": trip_id}}


def _shopping_rows_for_trip(client: Any, database_id: str, trip_id: str) -> List[Dict[str, Any]]:
    return _rows_for_trip(client, database_id, trip_id)


def save_shopping_rows(manifest: Dict[str, Any], client: NotionClient,
                       mapping_path: Path, trip_id: str,
                       rows: Sequence[Dict[str, Any]],
                       confirmed: bool = False) -> Dict[str, Any]:
    """Create or update approved, trip-scoped shopping rows in Notion.

    This is the write half of the shopping wizard.  The host may research an
    item with web/image tools, show the candidate and source to the user, then
    call this function with ``confirmed=True``.  It refuses name-only or
    unconfirmed rows and rereads the filtered database after every batch.
    """
    if not trip_id:
        raise InstallerError("쇼핑을 저장하려면 여행 페이지 ID가 필요합니다.")
    if not rows:
        return {"status": "skipped", "created": [], "updated": [], "rows": []}
    if not confirmed:
        raise InstallerError("쇼핑 후보를 저장하기 전에 사용자 확인이 필요합니다.")
    mapping = read_json(mapping_path)
    check_pending(mapping)
    check_manifest_version(mapping, manifest)
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 Souvenir 설치를 완료해야 쇼핑 목록을 저장할 수 있습니다.")
    database_id = (mapping.get("databases") or {}).get("shopping")
    if not database_id:
        raise InstallerError("설치 기록에 쇼핑 데이터베이스 매핑이 없습니다.")

    normalized: List[Dict[str, Any]] = []
    def value_text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    for raw in rows:
        if not isinstance(raw, dict):
            raise InstallerError("쇼핑 행은 객체여야 합니다.")
        row = dict(raw)
        missing = [field for field in ("구매 장소", "수량", "예상 금액")
                   if row.get(field) in (None, "")]
        if missing:
            raise InstallerError(f"{row.get('품목') or '쇼핑 품목'}에 확인이 필요한 값이 있습니다: {', '.join(missing)}")
        if not value_text(row.get("품목")):
            raise InstallerError("쇼핑 행에 품목명이 필요합니다.")
        normalized.append(row)

    workspace_key = str(mapping.get("parent_page_id") or "unknown")
    owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
    # Ensure the shared operation log exists before taking the advisory lease.
    with operation_locks(mapping_path, workspace_key, "shopping-save"):
        mapping = read_json(mapping_path)
        ensure_operation_log(mapping, mapping_path, client)
        trip_key = value_text(trip_id)
        with distributed_lease(workspace_key, trip_key, "shopping-save", owner, database_id):
            with operation_lease(mapping, mapping_path, client, trip_key, "shopping-save", owner, database_id):
                batch_entries: List[Dict[str, Any]] = []
                for row in normalized:
                    key = _shopping_row_key(trip_id, row)
                    properties = _shopping_row_properties(manifest, row, trip_id, key)
                    batch_entries.append({
                        "database_key": "shopping",
                        "row": row,
                        "stable": key,
                        "properties": properties,
                    })
                return _persist_row_batch(
                    manifest, client, mapping_path, mapping, trip_id, "shopping", batch_entries,
                )


def _traveler_row_properties(manifest: Dict[str, Any], row: Dict[str, Any],
                             trip_id: str, operation_key: str,
                             include_empty: bool = False) -> Dict[str, Any]:
    """Encode one person's answers without collapsing fields into a memo."""
    database = database_by_key(manifest, "traveler")
    specs = {item["name"]: item for item in database.get("properties", [])
             if isinstance(item, dict) and isinstance(item.get("name"), str)}
    name = str(row.get("여행자") or row.get("이름") or "").strip()
    if not name:
        raise InstallerError("동행자 입력에 이름이 필요합니다.")
    if "의견·요청" in row:
        raise InstallerError("의견·요청은 저장하지 않습니다. 역할·음식·이동·숙소·주의사항·추가 요청을 각각 입력하세요.")
    properties: Dict[str, Any] = {
        database["title_property"]: {"title": title_payload(name)},
        IDEMPOTENCY_PROPERTY: {"rich_text": rich_text(operation_key)},
    }
    values = dict(row)
    values.pop("여행자", None)
    values.pop("이름", None)
    values["여행 키"] = trip_id
    for property_name, value in values.items():
        if property_name not in specs or property_name in {database["title_property"], "의견·요청"}:
            continue
        if value in (None, "") and not include_empty:
            continue
        encoded = property_value(specs[property_name], value)
        if encoded is not None:
            properties[property_name] = encoded
    return properties


def _traveler_rows_for_trip(client: Any, database_id: str, trip_id: str) -> List[Dict[str, Any]]:
    return _rows_for_trip(client, database_id, trip_id)


def save_traveler_profiles(manifest: Dict[str, Any], client: NotionClient,
                           mapping_path: Path, trip_id: str,
                           profiles: Sequence[Dict[str, Any]],
                           confirmed: bool = False) -> Dict[str, Any]:
    """Save person-by-person answers into their separate traveler columns.

    The caller should display every field and obtain one batch confirmation
    before calling this function.  Existing rows are updated in place with an
    optimistic version check; unrelated properties and rows are preserved.
    """
    if not trip_id:
        raise InstallerError("동행자 정보를 저장하려면 여행 페이지 ID가 필요합니다.")
    if not profiles:
        return {"status": "skipped", "created": [], "updated": [], "rows": []}
    if not confirmed:
        raise InstallerError("동행자 입력을 저장하기 전에 사용자 확인이 필요합니다.")
    mapping = read_json(mapping_path)
    check_pending(mapping)
    # This guard is intentionally local to the writer so a malformed host
    # payload cannot bypass the input contract and write a combined field.
    for profile in profiles:
        if not isinstance(profile, dict):
            raise InstallerError("동행자 입력은 행 객체 배열이어야 합니다.")
        if "의견·요청" in profile:
            raise InstallerError("의견·요청은 저장하지 않습니다. 각 입력 항목을 분리해 주세요.")
    database_id = (mapping.get("databases") or {}).get("traveler")
    if not database_id:
        raise InstallerError("설치 기록에 동행자 데이터베이스 매핑이 없습니다.")
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 Souvenir 설치를 완료해야 동행자 정보를 저장할 수 있습니다.")

    trip_page = client.get_page(trip_id)
    trip_properties = trip_page.get("properties", {}) if isinstance(trip_page, dict) else {}
    trip_key = _plain_property_text(trip_properties, "여행 키") or trip_id

    normalized = [dict(profile) for profile in profiles]
    workspace_key = str(mapping.get("parent_page_id") or "unknown")
    owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
    with operation_locks(mapping_path, workspace_key, "traveler-save"):
        mapping = read_json(mapping_path)
        ensure_operation_log(mapping, mapping_path, client)
        with distributed_lease(workspace_key, trip_id, "traveler-save", owner, database_id):
            with operation_lease(mapping, mapping_path, client, trip_id, "traveler-save", owner, database_id):
                batch_entries: List[Dict[str, Any]] = []
                for index, profile in enumerate(normalized, start=1):
                    name = str(profile.get("여행자") or profile.get("이름") or f"동행자 {index}").strip()
                    profile["여행자"] = name
                    profile.setdefault("상태", "참여")
                    # scaffold_trip keys traveler rows with the summary row's
                    # stable 여행 키, not its opaque Notion page ID.
                    key = stable_key("trip", trip_key, "traveler", index)
                    properties = _traveler_row_properties(manifest, profile, trip_id, key)
                    batch_entries.append({
                        "database_key": "traveler",
                        "row": profile,
                        "stable": key,
                        "properties": properties,
                    })
                return _persist_row_batch(
                    manifest, client, mapping_path, mapping, trip_id, "traveler", batch_entries,
                )


PART_SAVE_DATABASES = {
    "trip": "trip",
    "traveler": "traveler",
    "itinerary": "itinerary",
    "air": "long_transport",
    "stay": "stay",
    "place": "place",
    "shopping": "shopping",
    "budget": "budget",
    "preparation": "task",
}
PART_SAVE_ALIASES = {
    "여행": "trip", "기본 정보": "trip", "기본 여행 정보": "trip", "여행 정보": "trip",
    "여행 개요·취향": "traveler", "동행자": "traveler", "여행자": "traveler",
    "날짜별 일정": "itinerary", "일정": "itinerary",
    "항공": "air", "항공편": "air", "숙소": "stay", "숙소 후보": "stay",
    "장소": "place", "장소 보관함": "place", "쇼핑": "shopping",
    "예산": "budget", "여행 지갑": "budget", "예산·여행 지갑": "budget",
    "준비물": "preparation", "준비할 일": "preparation", "준비물·준비할 일": "preparation",
}
PART_TITLE_FIELDS = {
    "trip": ("여행", "여행지", "목적지"),
    "traveler": ("여행자", "이름"),
    "itinerary": ("일정", "일정명"),
    "air": ("항공편", "구간"),
    "stay": ("숙소", "숙소명"),
    "place": ("장소", "장소명"),
    "shopping": ("품목", "품목명"),
    "budget": ("예산 항목", "지갑 이름", "지갑", "카테고리"),
    "preparation": ("할 일", "준비물", "항목"),
}
DATABASE_PART_KEYS = {
    "trip": "trip",
    "traveler": "traveler", "itinerary": "itinerary", "long_transport": "air",
    "stay": "stay", "place": "place", "shopping": "shopping", "budget": "budget",
    "task": "preparation", "packing": "preparation", "wallet": "budget",
    "expense": "budget", "budget": "budget", "split": "budget", "settlement": "budget",
}


def _part_key(value: str) -> str:
    key = str(value or "").strip()
    return PART_SAVE_ALIASES.get(key, key.lower())


def _part_title(part: str, row: Dict[str, Any]) -> str:
    for field in PART_TITLE_FIELDS[part]:
        value = row.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _part_database_for_row(manifest: Dict[str, Any], part: str, row: Dict[str, Any]) -> str:
    """Choose the concrete DB for a preparation row without asking twice."""
    if part != "preparation":
        return PART_SAVE_DATABASES[part]
    packing_fields = {"준비물", "공용·개인", "분류", "필수", "수량", "구매 필요"}
    return "packing" if packing_fields.intersection(row) else "task"


def _expand_part_rows(part: str, rows: Sequence[Dict[str, Any]]) -> List[tuple[str, Dict[str, Any], int]]:
    """Expand mixed budget answers into the concrete tables they belong to."""
    expanded: List[tuple[str, Dict[str, Any], int]] = []
    for source_index, raw in enumerate(rows, start=1):
        row = dict(raw)
        if part != "budget":
            expanded.append((_part_database_for_row({}, part, row), row, source_index))
            continue
        wallet_fields = {"지갑 이름", "지갑", "통화", "준비 금액"}
        expense_fields = {"사용처", "거래 유형", "결제 단계", "결제 수단", "실제 사용 금액", "금액", "지갑 키"}
        budget_fields = {"예산 카테고리", "카테고리", "예산 범위", "예산"}
        split_fields = {"사용 내역 키", "부담자", "분담 방식", "부담액", "정산 포함"}
        settlement_fields = {"보낸 사람", "받은 사람", "상태", "날짜"}
        emitted = False
        if wallet_fields.intersection(row):
            expanded.append(("wallet", row, source_index))
            emitted = True
        if expense_fields.intersection(row):
            expense = dict(row)
            if "실제 사용 금액" in expense and "금액" not in expense:
                expense["금액"] = expense["실제 사용 금액"]
            expanded.append(("expense", expense, source_index))
            emitted = True
        if budget_fields.intersection(row):
            budget = dict(row)
            if "예산 카테고리" in budget and "카테고리" not in budget:
                budget["카테고리"] = budget["예산 카테고리"]
            expanded.append(("budget", budget, source_index))
            emitted = True
        if split_fields.intersection(row):
            expanded.append(("split", row, source_index))
            emitted = True
        if settlement_fields.intersection(row) and (row.get("보낸 사람") or row.get("받은 사람")):
            expanded.append(("settlement", row, source_index))
            emitted = True
        if not emitted:
            expanded.append(("budget", row, source_index))
    return expanded


def _part_row_properties(manifest: Dict[str, Any], database_key: str,
                         row: Dict[str, Any], trip_id: str,
                         operation_key: str) -> Dict[str, Any]:
    database = database_by_key(manifest, database_key)
    specs = {item["name"]: item for item in database.get("properties", [])
             if isinstance(item, dict) and isinstance(item.get("name"), str)}
    part_key = DATABASE_PART_KEYS.get(database_key, database_key)
    title = _part_title(part_key, row)
    # preparation rows are selected by their concrete DB, not by the alias
    # name, so resolve their title directly from the schema.
    if database_key == "task":
        title = str(row.get("할 일") or row.get("항목") or "").strip()
    elif database_key == "packing":
        title = str(row.get("준비물") or row.get("항목") or "").strip()
    elif database_key == "wallet":
        title = str(row.get("지갑 이름") or row.get("지갑") or "").strip()
    elif database_key == "expense":
        title = str(row.get("내역") or row.get("사용처") or "").strip()
    elif database_key == "budget":
        title = str(row.get("예산 항목") or row.get("예산 카테고리") or row.get("카테고리") or "").strip()
    elif database_key == "split":
        title = str(row.get("분담") or row.get("사용 내역 키") or row.get("부담자") or "").strip()
    elif database_key == "settlement":
        title = f"{row.get('보낸 사람', '미정')} → {row.get('받은 사람', '미정')}"
    if not title:
        raise InstallerError(f"{database.get('title', database_key)} 입력에 제목 필드가 필요합니다.")
    properties: Dict[str, Any] = {
        database["title_property"]: {"title": title_payload(title)},
        IDEMPOTENCY_PROPERTY: {"rich_text": rich_text(operation_key)},
    }
    values = dict(row)
    title_aliases = set(PART_TITLE_FIELDS.get(part_key, ()))
    title_aliases.update({"지갑 이름", "내역", "예산 항목", "분담"})
    for alias in title_aliases:
        # Keep an alias when it is a real non-title schema property (for
        # example budget's ``카테고리``).  Drop only presentation aliases that
        # have no matching Notion column.
        if alias != database["title_property"] and alias not in specs:
            values.pop(alias, None)
    values["여행 키"] = trip_id
    # Confirmation metadata is useful to the host but is not a Notion schema
    # field unless the copied template explicitly has it.
    for name, value in values.items():
        if name not in specs or name in {"의견·요청", "추가 확인", "저장 가능", "여행지 확인"}:
            continue
        if value in (None, ""):
            continue
        encoded = property_value(specs[name], value)
        if encoded is not None:
            properties[name] = encoded
    return properties


def save_trip_fields(manifest: Dict[str, Any], client: NotionClient,
                     mapping_path: Path, trip_id: str,
                     row: Dict[str, Any], confirmed: bool = False) -> Dict[str, Any]:
    """Update the existing trip summary without creating a second trip row."""
    if not trip_id:
        raise InstallerError("기본 여행 정보 저장에는 여행 페이지 ID가 필요합니다.")
    if not confirmed:
        raise InstallerError("기본 여행 정보 저장 전에 사용자 확인이 필요합니다.")
    if not isinstance(row, dict):
        raise InstallerError("기본 여행 정보는 하나의 행 객체여야 합니다.")
    if "의견·요청" in row:
        raise InstallerError("의견·요청은 저장하지 않습니다. 독립 입력 필드를 사용하세요.")
    mapping = read_json(mapping_path)
    check_pending(mapping)
    check_manifest_version(mapping, manifest)
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 Souvenir 설치를 완료해야 기본 여행 정보를 저장할 수 있습니다.")
    database_id = (mapping.get("databases") or {}).get("trip")
    if not database_id:
        raise InstallerError("설치 기록에 여행 데이터베이스 매핑이 없습니다.")
    database = database_by_key(manifest, "trip")
    specs = {item["name"]: item for item in database.get("properties", [])
             if isinstance(item, dict) and isinstance(item.get("name"), str)}
    aliases = {"여행지": "목적지", "인원 수": "인원", "여행 기간(일)": "기간(일)"}
    properties: Dict[str, Any] = {}
    for name, value in row.items():
        canonical = aliases.get(name, name)
        if canonical not in specs or canonical in {"여행 키", "자동 제외"}:
            continue
        if value in (None, ""):
            continue
        encoded = property_value(specs[canonical], value)
        if encoded is not None:
            properties[canonical] = encoded
    if not properties:
        raise InstallerError("저장할 기본 여행 정보가 없습니다.")
    workspace_key = str(mapping.get("parent_page_id") or "unknown")
    owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
    with operation_locks(mapping_path, workspace_key, "trip-save"):
        mapping = read_json(mapping_path)
        ensure_operation_log(mapping, mapping_path, client)
        with distributed_lease(workspace_key, trip_id, "trip-save", owner, database_id):
            with operation_lease(mapping, mapping_path, client, trip_id, "trip-save", owner, database_id):
                metrics = _operation_metrics(client)
                if metrics is not None:
                    metrics.reset()
                current = client.get_page(trip_id)
                expected = current.get("last_edited_time") if isinstance(current, dict) else None
                recorded_write(
                    mapping, mapping_path, f"trip:update:{trip_id}",
                    lambda: client.update_page_properties(trip_id, properties, expected),
                    lambda _result: None,
                    verify=lambda result: verify_updated(result, trip_id),
                    idempotency_key=stable_key("trip-update", trip_id, sorted(properties)),
                )
                _link_row_to_trip(client, trip_id, database_id, trip_id, {})
                result = {"status": "complete", "part": "trip", "trip_id": trip_id,
                          "updated": [trip_id], "created": [], "properties": sorted(properties)}
                result.update(_performance_result(client))
                return result


def save_part_rows(manifest: Dict[str, Any], client: NotionClient,
                   mapping_path: Path, trip_id: str, part: str,
                   rows: Sequence[Dict[str, Any]], confirmed: bool = False) -> Dict[str, Any]:
    """Persist any normalized plan part using its explicit field contract.

    This is the common fallback writer for itinerary, air, stay, place,
    shopping, budget and preparation.  It accepts only normalized row objects,
    writes each value to the matching schema property, reuses the same row on
    retries, and rereads the trip-scoped rows before returning.  Specialized
    writers (traveler-save and shopping-save) keep their stricter validation.
    """
    key = _part_key(part)
    if key not in PART_SAVE_DATABASES:
        raise InstallerError(f"저장할 수 없는 입력 파트입니다: {part}")
    if not trip_id:
        raise InstallerError("파트 저장에는 여행 페이지 ID가 필요합니다.")
    if not rows:
        return {"status": "skipped", "part": key, "created": [], "updated": [], "rows": []}
    if not confirmed:
        raise InstallerError("파트 입력을 저장하기 전에 사용자 확인이 필요합니다.")
    if any(not isinstance(row, dict) for row in rows):
        raise InstallerError("파트 입력은 행 객체 배열이어야 합니다.")
    if key == "trip":
        if len(rows) != 1:
            raise InstallerError("기본 여행 정보는 한 번에 한 요약 행만 저장합니다.")
        return save_trip_fields(manifest, client, mapping_path, trip_id, rows[0], confirmed=True)
    if key == "traveler":
        return save_traveler_profiles(manifest, client, mapping_path, trip_id, rows, confirmed=True)
    if key == "shopping":
        return save_shopping_rows(manifest, client, mapping_path, trip_id, rows, confirmed=True)
    for row in rows:
        if "의견·요청" in row:
            raise InstallerError("의견·요청은 저장하지 않습니다. 독립 입력 필드를 사용하세요.")

    mapping = read_json(mapping_path)
    check_pending(mapping)
    check_manifest_version(mapping, manifest)
    if mapping.get("status") != "complete":
        raise InstallerError("먼저 Souvenir 설치를 완료해야 파트 입력을 저장할 수 있습니다.")
    entries = _expand_part_rows(key, rows)
    database_keys = [database_key for database_key, _row, _source_index in entries]
    for database_key in database_keys:
        if database_key not in mapping.get("databases", {}):
            raise InstallerError(f"설치 기록에 {database_key} 데이터베이스 매핑이 없습니다.")

    trip_page = client.get_page(trip_id)
    trip_properties = trip_page.get("properties", {}) if isinstance(trip_page, dict) else {}
    trip_key = _plain_property_text(trip_properties, "여행 키") or trip_id
    workspace_key = str(mapping.get("parent_page_id") or "unknown")
    owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
    with operation_locks(mapping_path, workspace_key, f"{key}-save"):
        mapping = read_json(mapping_path)
        ensure_operation_log(mapping, mapping_path, client)
        target = ",".join(sorted(set(database_keys)))
        with distributed_lease(workspace_key, trip_id, f"{key}-save", owner, target):
            with operation_lease(mapping, mapping_path, client, trip_id, f"{key}-save", owner, target):
                batch_entries: List[Dict[str, Any]] = []
                for index, (database_key, row, source_index) in enumerate(entries, start=1):
                    title = _part_title(key, row)
                    if key == "itinerary" and row.get("일차") not in (None, ""):
                        # scaffold_trip already created one row per day with
                        # this exact key; update it instead of appending a
                        # second copy when the user fills that day in.
                        stable = stable_key("trip", trip_key, "itinerary", int(row["일차"]))
                    elif key == "preparation" and title:
                        stable = stable_key("trip", trip_key, database_key, title)
                    else:
                        stable = stable_key("trip", trip_key, key, database_key, source_index)
                    properties = _part_row_properties(manifest, database_key, row, trip_id, stable)
                    batch_entries.append({
                        "database_key": database_key,
                        "row": row,
                        "stable": stable,
                        "properties": properties,
                    })
                result = _persist_row_batch(
                    manifest, client, mapping_path, mapping, trip_id, key, batch_entries,
                )
                result["part"] = key
                return result


def _has_trip_relation(client: Any, database_id: str) -> bool:
    """Return whether a target database exposes the live template's 여행 relation.

    The embedded fallback manifest intentionally creates portable databases
    without cross-database relations.  A copied Notion template, however,
    already has a ``여행`` relation.  Detect it at runtime so scaffolded rows
    can be linked when available and remain portable when it is not.
    """
    getter = getattr(client, "get_database", None)
    if not callable(getter):
        return False
    try:
        payload = getter(database_id)
    except Exception:
        return False
    properties = payload.get("properties", {}) if isinstance(payload, dict) else {}
    relation = properties.get("여행") if isinstance(properties, dict) else None
    return isinstance(relation, dict) and relation.get("type") == "relation"


def _link_row_to_trip(client: Any, page_id: str, database_id: str,
                      trip_id: str, relation_cache: Dict[str, Any]) -> None:
    """Attach and activate a row so the copied template's views show it.

    Marketplace copies use both a ``여행`` relation and a ``구분`` select.  A
    row can be successfully created yet remain invisible when either relation
    or the ``사용 중`` marker is missing, so both are applied in one update.
    Portable fallback databases simply skip the properties they do not have.
    """
    if not page_id or not trip_id:
        return
    schema = _live_database_schema(client, database_id, relation_cache)
    relation = schema.get("여행") if isinstance(schema, dict) else None
    has_relation = isinstance(relation, dict) and relation.get("type") == "relation"
    relation_cache[database_id] = has_relation
    updates: Dict[str, Any] = {}
    if has_relation:
        updates["여행"] = {"relation": [{"id": trip_id}]}
    marker = schema.get("구분") if isinstance(schema, dict) else None
    if isinstance(marker, dict) and marker.get("type") in {"select", "status"}:
        current_marker = ""
        getter = getattr(client, "get_page", None)
        if callable(getter):
            try:
                current = getter(page_id)
                current_marker = _select_property_name(
                    current.get("properties", {}) if isinstance(current, dict) else {}, "구분"
                )
            except Exception:
                current_marker = ""
        if current_marker != "사용 중":
            updates["구분"] = ({"status": {"name": "사용 중"}}
                               if marker.get("type") == "status"
                               else {"select": {"name": "사용 중"}})
    updater = getattr(client, "update_page_properties", None)
    if not updates or not callable(updater):
        return
    updater(page_id, updates)


def _batch_worker_limit(size: int) -> int:
    """Return a conservative, user-tunable write concurrency limit."""
    try:
        configured = int(os.environ.get("SOUVENIR_WRITE_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        configured = 4
    return max(1, min(5, configured, max(1, size)))


def _page_stable_key(row: Dict[str, Any]) -> str:
    properties = row.get("properties", {}) if isinstance(row, dict) else {}
    return _plain_property_text(properties, IDEMPOTENCY_PROPERTY).strip()


def _build_row_index(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index one prefetched trip scope and reject ambiguous duplicate keys."""
    indexed: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _page_stable_key(row)
        if not key:
            continue
        if key in indexed:
            raise ConflictError(f"같은 Souvenir 작업 키가 여러 행에 있습니다: {key}")
        indexed[key] = row
    return indexed


def _visibility_properties(schema: Dict[str, Any], trip_id: str) -> Dict[str, Any]:
    """Encode copied-template relation/marker fields into the main write."""
    properties: Dict[str, Any] = {}
    relation = schema.get("여행") if isinstance(schema, dict) else None
    if isinstance(relation, dict) and relation.get("type") == "relation":
        properties["여행"] = {"relation": [{"id": trip_id}]}
    marker = schema.get("구분") if isinstance(schema, dict) else None
    if isinstance(marker, dict) and marker.get("type") in {"select", "status"}:
        properties["구분"] = (
            {"status": {"name": "사용 중"}}
            if marker.get("type") == "status"
            else {"select": {"name": "사용 중"}}
        )
    return properties


def _update_without_post_read(client: Any, page_id: str,
                              properties: Dict[str, Any],
                              expected: Optional[str]) -> Dict[str, Any]:
    """Use the PATCH response in a batch; final DB reread verifies visibility."""
    updater = getattr(client, "update_page_properties", None)
    if not callable(updater):
        raise InstallerError("Notion 클라이언트가 페이지 수정을 지원하지 않습니다.")
    try:
        return updater(page_id, properties, expected, verify_response=False)
    except TypeError as exc:
        # Offline adapters and older host clients may not know the optional
        # flag.  They retain the safe, slower per-row verification path.
        if "verify_response" not in str(exc):
            raise
        return updater(page_id, properties, expected)


def _verify_page_reference(result: Any, message: str) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise UncertainWriteError(message)


def _persist_row_batch(manifest: Dict[str, Any], client: Any,
                       mapping_path: Path, mapping: Dict[str, Any],
                       trip_id: str, operation_name: str,
                       entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Persist normalized rows with one prefetch, bounded fan-out, and one verification per DB."""
    if not entries:
        return {"status": "skipped", "created": [], "updated": [], "rows": [], **_performance_result(client)}

    metrics = _operation_metrics(client)
    if metrics is not None:
        metrics.reset()

    database_ids = {
        entry["database_key"]: mapping["databases"][entry["database_key"]]
        for entry in entries
    }
    schema_cache: Dict[str, Any] = {}
    existing_by_database: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with metrics.phase("prefetch") if metrics else _null_context():
        for database_key, database_id in database_ids.items():
            schema = _live_database_schema(client, database_id, schema_cache)
            rows = _rows_for_trip(client, database_id, trip_id, schema_cache)
            existing_by_database[database_key] = _build_row_index(rows)
            for entry in entries:
                if entry["database_key"] == database_key:
                    entry["schema"] = schema

    journal_entries: List[tuple[str, Optional[str]]] = []
    seen_keys: set[str] = set()
    for entry in entries:
        journal_key = f"{operation_name}:{entry['database_key']}:{entry['stable']}"
        if journal_key in seen_keys:
            raise ConflictError(f"한 번의 반영에 같은 작업 키가 중복되었습니다: {journal_key}")
        seen_keys.add(journal_key)
        entry["journal_key"] = journal_key
        journal_entries.append((journal_key, entry["stable"]))

    journal = BatchWriteJournal(mapping, mapping_path, journal_entries)
    created: List[str] = []
    updated: List[str] = []
    results_lock = threading.Lock()

    def write_one(entry: Dict[str, Any]) -> tuple[str, str]:
        database_key = entry["database_key"]
        database_id = database_ids[database_key]
        existing = existing_by_database[database_key].get(entry["stable"])
        properties = dict(entry["properties"])
        properties.update(_visibility_properties(entry.get("schema", {}), trip_id))
        if existing:
            page_id = str(existing.get("id") or "")
            if not page_id:
                raise UncertainWriteError("기존 Notion 행의 페이지 ID를 확인하지 못했습니다.")
            expected = existing.get("last_edited_time")
            if not expected:
                current = client.get_page(page_id)
                expected = current.get("last_edited_time") if isinstance(current, dict) else None
            result = journal.run(
                entry["journal_key"],
                lambda: _update_without_post_read(client, page_id, properties, expected),
                lambda _result: None,
                verify=lambda value: verify_updated(value, page_id),
            )
            _verify_page_reference(result, "수정된 Notion 행의 페이지 ID를 확인하지 못했습니다.")
            with results_lock:
                updated.append(page_id)
            return "updated", page_id

        result = journal.run(
            entry["journal_key"],
            lambda: client.create_database_page(database_id, properties),
            lambda _result: None,
            verify=lambda value: _verify_page_reference(value, "생성된 Notion 행의 페이지 ID를 확인하지 못했습니다."),
        )
        page_id = str(result.get("id"))
        with results_lock:
            created.append(page_id)
        return "created", page_id

    errors: List[BaseException] = []
    with metrics.phase("write") if metrics else _null_context():
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_batch_worker_limit(len(entries)),
            thread_name_prefix="souvenir-write",
        ) as executor:
            futures = [executor.submit(write_one, entry) for entry in entries]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:  # Preserve all journal entries before surfacing the first error.
                    errors.append(exc)
    if errors:
        raise errors[0]

    visible_rows: List[Dict[str, Any]] = []
    with metrics.phase("verify") if metrics else _null_context():
        for database_key, database_id in database_ids.items():
            scoped = _rows_for_trip(client, database_id, trip_id, schema_cache)
            visible_rows.extend(scoped)
            visible_keys = {_page_stable_key(row) for row in scoped}
            expected_keys = {
                entry["stable"] for entry in entries if entry["database_key"] == database_key
            }
            if not expected_keys.issubset(visible_keys):
                raise UncertainWriteError(
                    f"{database_key} 저장 후 현재 여행에 표시되는 행을 확인하지 못했습니다."
                )
    result = {
        "status": "complete",
        "trip_id": trip_id,
        "created": created,
        "updated": updated,
        "rows": visible_rows,
    }
    result.update(_performance_result(client))
    return result


@contextmanager
def _null_context() -> Any:
    yield


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
    relation_cache: Dict[str, bool] = {}
    trip_values: Dict[str, Any] = {
        # Keep the stable trip key on the summary row as well as on every
        # child row.  Dashboard views can then filter by this value instead
        # of inheriting a title or a relation from the template sample trip.
        "여행 키": trip_key,
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
    # The live template filters active trip cards by ``구분 = 사용 중``.
    # Apply that marker even when the portable manifest does not declare it.
    _link_row_to_trip(client, result["trip_id"], mapping["databases"]["trip"],
                      result["trip_id"], relation_cache)
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
            created_id = created.get("id") if isinstance(created, dict) else None
            if isinstance(created_id, str):
                _link_row_to_trip(
                    client,
                    created_id,
                    mapping["databases"][key],
                    result["trip_id"],
                    relation_cache,
                )
            if field == "wallet":
                result[field] = created_id
            else:
                result[field].append(created_id)

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
        with distributed_lease(workspace_key, trip_key, "scaffold", owner, destination):
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
        owner = os.environ.get("SOUVENIR_OPERATOR") or f"{socket.gethostname()}:{os.getpid()}"
        with distributed_lease(
            normalized_parent,
            "install:" + normalized_parent,
            "install",
            owner,
            normalized_parent,
        ):
            return _install_unlocked(manifest, client, normalized_parent, mapping_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Souvenir portable Notion template installer")
    parser.add_argument("command", choices=["validate", "plan", "install", "scaffold", "part-save", "shopping-save", "traveler-save"])
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
    parser.add_argument("--trip-id", help="저장 대상 여행 페이지 ID")
    parser.add_argument("--part", help="part-save 입력 파트 이름")
    parser.add_argument("--part-input", type=Path, help="part-save 승인된 정규화 행 JSON")
    parser.add_argument("--shopping-input", type=Path, help="shopping-save 승인된 쇼핑 행 JSON")
    parser.add_argument("--traveler-input", type=Path, help="traveler-save에서 사용할 사람별 입력 JSON")
    parser.add_argument("--confirm", action="store_true", help="shopping-save 사용자 확인 완료")
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
        if args.command == "shopping-save":
            if not args.trip_id:
                raise InstallerError("shopping-save에는 --trip-id가 필요합니다.")
            if not args.shopping_input:
                raise InstallerError("shopping-save에는 --shopping-input JSON 파일이 필요합니다.")
            if not args.confirm:
                raise InstallerError("쇼핑 후보를 저장하기 전에 --confirm 승인이 필요합니다.")
            try:
                loaded = json.loads(args.shopping_input.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                raise InstallerError(f"쇼핑 입력 JSON을 읽지 못했습니다: {args.shopping_input}") from exc
            rows = loaded.get("rows", loaded.get("쇼핑", loaded)) if isinstance(loaded, dict) else loaded
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise InstallerError("쇼핑 입력 JSON은 행 객체 배열이어야 합니다.")
            result = save_shopping_rows(manifest, client, args.mapping, args.trip_id, rows, confirmed=True)
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
            return 0
        if args.command == "part-save":
            if not args.trip_id:
                raise InstallerError("part-save에는 --trip-id가 필요합니다.")
            if not args.part:
                raise InstallerError("part-save에는 --part가 필요합니다.")
            if not args.part_input:
                raise InstallerError("part-save에는 --part-input JSON 파일이 필요합니다.")
            if not args.confirm:
                raise InstallerError("파트 입력을 저장하기 전에 --confirm 승인이 필요합니다.")
            try:
                loaded = json.loads(args.part_input.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                raise InstallerError(f"파트 입력 JSON을 읽지 못했습니다: {args.part_input}") from exc
            rows = loaded.get("rows", loaded.get("입력", loaded)) if isinstance(loaded, dict) else loaded
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise InstallerError("파트 입력 JSON은 행 객체 배열이어야 합니다.")
            result = save_part_rows(manifest, client, args.mapping, args.trip_id,
                                    args.part, rows, confirmed=True)
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
            return 0
        if args.command == "traveler-save":
            if not args.trip_id:
                raise InstallerError("traveler-save에는 --trip-id가 필요합니다.")
            if not args.traveler_input:
                raise InstallerError("traveler-save에는 --traveler-input JSON 파일이 필요합니다.")
            if not args.confirm:
                raise InstallerError("동행자 입력을 저장하기 전에 --confirm 승인이 필요합니다.")
            try:
                loaded = json.loads(args.traveler_input.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                raise InstallerError(f"동행자 입력 JSON을 읽지 못했습니다: {args.traveler_input}") from exc
            profiles = loaded.get("profiles", loaded.get("동행자", loaded)) if isinstance(loaded, dict) else loaded
            if isinstance(profiles, dict):
                profiles = [profiles]
            if not isinstance(profiles, list) or any(not isinstance(row, dict) for row in profiles):
                raise InstallerError("동행자 입력 JSON은 행 객체 배열이어야 합니다.")
            result = save_traveler_profiles(manifest, client, args.mapping, args.trip_id,
                                            profiles, confirmed=True)
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
            return 0
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
