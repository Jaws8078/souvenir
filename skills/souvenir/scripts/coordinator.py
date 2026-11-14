#!/usr/bin/env python3
"""Small, dependency-free Souvenir coordinator.

The Notion API is the source of travel data, but it cannot provide a strong
compare-and-swap lease across independent clients.  This module supplies an
optional central coordinator backed by SQLite.  It owns only coordination
metadata (leases, queue jobs, idempotency records, and compensation events);
it never stores Notion page contents or credentials.

Run a coordinator on a trusted host and point local executors at it with
``SOUVENIR_COORDINATOR_URL`` and (recommended) ``SOUVENIR_COORDINATOR_TOKEN``.
The HTTP surface is intentionally small so it can sit behind a normal TLS
reverse proxy without external Python dependencies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union


LEASE_ACTIVE = "active"
LEASE_COMPLETED = "completed"
LEASE_FAILED = "failed"
LEASE_EXPIRED = "expired"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_CANCELLED = "cancelled"
JOB_FAILED = "failed"

EVENT_PENDING = "pending"
EVENT_APPLYING = "applying"
EVENT_COMPLETED = "completed"
EVENT_SKIPPED = "skipped"


class CoordinatorError(RuntimeError):
    """Base error returned by the coordinator."""


class CoordinatorConflict(CoordinatorError):
    """Another writer owns the requested scope or idempotency key."""


class CoordinatorNotFound(CoordinatorError):
    """The requested lease, job, or event is not known."""


class CoordinatorUnavailable(CoordinatorError):
    """The configured coordinator could not be reached."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


class CoordinatorStore:
    """Transactional SQLite state for one coordinator instance."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Union[Path, str], clock: Any = _now) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _migrate(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    workspace_key TEXT NOT NULL,
                    trip_key TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    target TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS leases_scope_status
                    ON leases(workspace_key, trip_key, status, expires_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    owner TEXT,
                    token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_key, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_order
                    ON jobs(scope_key, status, available_at, created_at);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    scope_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS compensation_events (
                    event_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    compensation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claimed_by TEXT,
                    claim_token TEXT,
                    claim_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS compensation_pending
                    ON compensation_events(scope_key, status, created_at);
                """
            )
            existing = self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            columns = {
                row["name"] for row in self.connection.execute(
                    "PRAGMA table_info(compensation_events)"
                ).fetchall()
            }
            for name, definition in (
                ("claimed_by", "TEXT"),
                ("claim_token", "TEXT"),
                ("claim_expires_at", "TEXT"),
            ):
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE compensation_events ADD COLUMN {name} {definition}"
                    )
            if existing is None:
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, _iso(self.clock())),
                )
            elif existing["version"] > self.SCHEMA_VERSION:
                raise CoordinatorError("coordinator 데이터베이스 버전이 더 새롭습니다.")
            elif existing["version"] < self.SCHEMA_VERSION:
                self.connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, _iso(self.clock())),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _expire_leases(self, connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            """UPDATE leases SET status=?, finished_at=?, result_json=?
               WHERE status=? AND expires_at <= ?""",
            (LEASE_EXPIRED, now, _json({"reason": "heartbeat_timeout"}), LEASE_ACTIVE, now),
        )

    def acquire_lease(
        self,
        workspace_key: str,
        trip_key: str,
        operation_type: str,
        owner: str,
        target: str = "",
        lease_seconds: int = 600,
        request_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not workspace_key or not trip_key or not operation_type or not owner:
            raise CoordinatorError("workspace_key·trip_key·operation_type·owner가 필요합니다.")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise CoordinatorError("lease_seconds는 1~86400초여야 합니다.")
        current = self.clock()
        now = _iso(current)
        expires = _iso(current + dt.timedelta(seconds=lease_seconds))
        request_key = request_key or uuid.uuid4().hex
        with self._transaction() as connection:
            self._expire_leases(connection, now)
            previous = connection.execute(
                "SELECT * FROM leases WHERE request_key=?", (request_key,)
            ).fetchone()
            if previous is not None:
                if previous["owner"] == owner and previous["status"] == LEASE_ACTIVE:
                    return _row(previous) or {}
                raise CoordinatorConflict("같은 coordinator 작업 키가 이미 사용되었습니다.")
            active = connection.execute(
                """SELECT * FROM leases
                   WHERE workspace_key=? AND trip_key=? AND status=? AND expires_at > ?
                   LIMIT 1""",
                (workspace_key, trip_key, LEASE_ACTIVE, now),
            ).fetchone()
            if active is not None:
                raise CoordinatorConflict(
                    f"여행에 이미 coordinator 작업이 있습니다: {active['owner']}"
                )
            lease_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            connection.execute(
                """INSERT INTO leases(
                   lease_id, request_key, workspace_key, trip_key, operation_type,
                   owner, token, target, status, started_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lease_id, request_key, workspace_key, trip_key, operation_type,
                 owner, token, target, LEASE_ACTIVE, now, now, expires),
            )
            return _row(connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
            ).fetchone()) or {}

    def heartbeat_lease(self, lease_id: str, token: str, lease_seconds: int = 600) -> Dict[str, Any]:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise CoordinatorError("lease_seconds는 1~86400초여야 합니다.")
        current = self.clock()
        now = _iso(current)
        expires = _iso(current + dt.timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row is None:
                raise CoordinatorNotFound("coordinator lease를 찾지 못했습니다.")
            if row["token"] != token or row["status"] != LEASE_ACTIVE or row["expires_at"] <= now:
                raise CoordinatorConflict("coordinator lease가 만료되었거나 소유자가 다릅니다.")
            connection.execute(
                """UPDATE leases SET heartbeat_at=?, expires_at=?
                   WHERE lease_id=? AND token=? AND status=?""",
                (now, expires, lease_id, token, LEASE_ACTIVE),
            )
            return _row(connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
            ).fetchone()) or {}

    def release_lease(
        self, lease_id: str, token: str, status: str = LEASE_COMPLETED,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if status not in {LEASE_COMPLETED, LEASE_FAILED, LEASE_EXPIRED}:
            raise CoordinatorError("지원하지 않는 coordinator lease 상태입니다.")
        now = _iso(self.clock())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row is None:
                raise CoordinatorNotFound("coordinator lease를 찾지 못했습니다.")
            if row["token"] != token:
                raise CoordinatorConflict("coordinator lease 소유자가 다릅니다.")
            if row["status"] == status:
                return _row(row) or {}
            if row["status"] != LEASE_ACTIVE:
                raise CoordinatorConflict("이미 종료된 coordinator lease입니다.")
            connection.execute(
                """UPDATE leases SET status=?, finished_at=?, result_json=?
                   WHERE lease_id=? AND token=? AND status=?""",
                (status, now, _json(result or {}), lease_id, token, LEASE_ACTIVE),
            )
            return _row(connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
            ).fetchone()) or {}

    def enqueue_job(
        self, scope_key: str, operation_type: str, payload: Dict[str, Any],
        idempotency_key: str, available_at: Optional[dt.datetime] = None,
    ) -> Dict[str, Any]:
        if not scope_key or not operation_type or not idempotency_key:
            raise CoordinatorError("queue scope·operation_type·idempotency_key가 필요합니다.")
        now = _iso(self.clock())
        available = _iso(available_at or self.clock())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()
            if existing is not None:
                return _row(existing) or {}
            job_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO jobs(
                   job_id, scope_key, idempotency_key, operation_type, payload_json,
                   status, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, scope_key, idempotency_key, operation_type, _json(payload),
                 JOB_QUEUED, available, now, now),
            )
            return _row(connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()) or {}

    def claim_next_job(self, scope_key: str, owner: str, lease_seconds: int = 600) -> Optional[Dict[str, Any]]:
        if not scope_key or not owner:
            raise CoordinatorError("queue scope와 owner가 필요합니다.")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise CoordinatorError("lease_seconds는 1~86400초여야 합니다.")
        current = self.clock()
        now = _iso(current)
        expires = _iso(current + dt.timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            connection.execute(
                """UPDATE jobs SET status=?, owner=NULL, token=NULL, lease_expires_at=NULL,
                   updated_at=? WHERE scope_key=? AND status=? AND lease_expires_at <= ?""",
                (JOB_QUEUED, now, scope_key, JOB_RUNNING, now),
            )
            running = connection.execute(
                """SELECT 1 FROM jobs
                   WHERE scope_key=? AND status=? AND lease_expires_at > ? LIMIT 1""",
                (scope_key, JOB_RUNNING, now),
            ).fetchone()
            if running is not None:
                return None
            row = connection.execute(
                """SELECT * FROM jobs
                   WHERE scope_key=? AND status=? AND available_at <= ?
                   ORDER BY created_at, rowid LIMIT 1""",
                (scope_key, JOB_QUEUED, now),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            connection.execute(
                """UPDATE jobs SET status=?, owner=?, token=?, attempts=attempts+1,
                   lease_expires_at=?, updated_at=? WHERE job_id=? AND status=?""",
                (JOB_RUNNING, owner, token, expires, now, row["job_id"], JOB_QUEUED),
            )
            return _row(connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()) or {}

    def finish_job(self, job_id: str, token: str, status: str,
                   result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if status not in {JOB_COMPLETED, JOB_CANCELLED, JOB_FAILED}:
            raise CoordinatorError("지원하지 않는 queue 작업 종료 상태입니다.")
        now = _iso(self.clock())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise CoordinatorNotFound("queue 작업을 찾지 못했습니다.")
            if row["token"] != token or row["status"] != JOB_RUNNING:
                raise CoordinatorConflict("queue 작업 lease가 만료되었거나 소유자가 다릅니다.")
            connection.execute(
                """UPDATE jobs SET status=?, result_json=?, token=NULL,
                   lease_expires_at=NULL, updated_at=? WHERE job_id=? AND token=?""",
                (status, _json(result or {}), now, job_id, token),
            )
            return _row(connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()) or {}

    def claim_idempotency(self, scope_key: str, idempotency_key: str,
                          operation_type: str, request: Dict[str, Any]) -> Dict[str, Any]:
        now = _iso(self.clock())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()
            if row is not None:
                return {"claimed": False, **(_row(row) or {})}
            connection.execute(
                """INSERT INTO idempotency_keys(
                   scope_key, idempotency_key, operation_type, status,
                   request_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)""",
                (scope_key, idempotency_key, operation_type, _json(request), now, now),
            )
            return {"claimed": True, **(_row(connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()) or {})}

    def finish_idempotency(self, scope_key: str, idempotency_key: str,
                           status: str, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise CoordinatorError("지원하지 않는 idempotency 종료 상태입니다.")
        now = _iso(self.clock())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()
            if row is None:
                raise CoordinatorNotFound("idempotency 키를 찾지 못했습니다.")
            connection.execute(
                """UPDATE idempotency_keys SET status=?, result_json=?, updated_at=?
                   WHERE scope_key=? AND idempotency_key=?""",
                (status, _json(result or {}), now, scope_key, idempotency_key),
            )
            return _row(connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()) or {}

    def record_compensation(self, scope_key: str, operation_id: str, event_type: str,
                            payload: Dict[str, Any], compensation: Dict[str, Any]) -> Dict[str, Any]:
        now = _iso(self.clock())
        event_id = uuid.uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO compensation_events(
                   event_id, scope_key, operation_id, event_type, payload_json,
                   compensation_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, scope_key, operation_id, event_type, _json(payload),
                 _json(compensation), EVENT_PENDING, now, now),
            )
            return _row(connection.execute(
                "SELECT * FROM compensation_events WHERE event_id=?", (event_id,)
            ).fetchone()) or {}

    def claim_compensation(self, scope_key: str, owner: str, lease_seconds: int = 600) -> Optional[Dict[str, Any]]:
        if not scope_key or not owner:
            raise CoordinatorError("보상 scope와 owner가 필요합니다.")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise CoordinatorError("lease_seconds는 1~86400초여야 합니다.")
        current = self.clock()
        now = _iso(current)
        expires = _iso(current + dt.timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            connection.execute(
                """UPDATE compensation_events SET status=?, claimed_by=NULL,
                   claim_token=NULL, claim_expires_at=NULL, updated_at=?
                   WHERE scope_key=? AND status=? AND claim_expires_at <= ?""",
                (EVENT_PENDING, now, scope_key, EVENT_APPLYING, now),
            )
            row = connection.execute(
                """SELECT * FROM compensation_events
                   WHERE scope_key=? AND status=? ORDER BY created_at, event_id LIMIT 1""",
                (scope_key, EVENT_PENDING),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            connection.execute(
                """UPDATE compensation_events SET status=?, claimed_by=?, claim_token=?,
                   claim_expires_at=?, updated_at=? WHERE event_id=? AND status=?""",
                (EVENT_APPLYING, owner, token, expires, now, row["event_id"], EVENT_PENDING),
            )
            result = _row(connection.execute(
                "SELECT * FROM compensation_events WHERE event_id=?", (row["event_id"],)
            ).fetchone()) or {}
            return result

    def finish_compensation(self, event_id: str, status: str,
                            result: Optional[Dict[str, Any]] = None,
                            token: Optional[str] = None) -> Dict[str, Any]:
        if status not in {EVENT_COMPLETED, EVENT_SKIPPED, EVENT_PENDING}:
            raise CoordinatorError("지원하지 않는 보상 이벤트 상태입니다.")
        now = _iso(self.clock())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM compensation_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise CoordinatorNotFound("보상 이벤트를 찾지 못했습니다.")
            if status in {EVENT_COMPLETED, EVENT_SKIPPED} and (
                    row["status"] != EVENT_APPLYING or not token or row["claim_token"] != token):
                raise CoordinatorConflict("보상 이벤트의 claim 소유자가 다르거나 만료되었습니다.")
            connection.execute(
                """UPDATE compensation_events SET status=?, result_json=?, claimed_by=NULL,
                   claim_token=NULL, claim_expires_at=NULL, updated_at=? WHERE event_id=?""",
                (status, _json(result or {}), now, event_id),
            )
            return _row(connection.execute(
                "SELECT * FROM compensation_events WHERE event_id=?", (event_id,)
            ).fetchone()) or {}


def _public_lease(row: Dict[str, Any]) -> Dict[str, Any]:
    # The per-lease token is required for heartbeat/release.  It is returned
    # only over the authenticated coordinator connection; never persist it in
    # the Notion mapping or expose it in user-facing logs.
    return dict(row)


def _public_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    for key in ("payload_json", "result_json", "request_json", "compensation_json"):
        if key in result:
            result[key[:-5] if key.endswith("_json") else key] = _decode(result.pop(key), {})
    return result


class CoordinatorClient:
    """HTTP client used by local executors when a P2 coordinator is configured."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = None if payload is None else _json(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                payload_value = json.loads(detail)
            except json.JSONDecodeError:
                payload_value = {}
            message = payload_value.get("error", f"coordinator HTTP {exc.code}") if isinstance(payload_value, dict) else str(exc)
            if exc.code == HTTPStatus.CONFLICT:
                raise CoordinatorConflict(message) from exc
            if exc.code == HTTPStatus.NOT_FOUND:
                raise CoordinatorNotFound(message) from exc
            raise CoordinatorError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise CoordinatorUnavailable("Souvenir coordinator에 연결하지 못했습니다.") from exc
        if not isinstance(data, dict):
            raise CoordinatorError("coordinator 응답 형식이 올바르지 않습니다.")
        return data

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz")

    def acquire_lease(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/leases/acquire", kwargs)["lease"]

    def heartbeat_lease(self, lease_id: str, token: str, lease_seconds: int = 600) -> Dict[str, Any]:
        return self._request("POST", "/v1/leases/heartbeat", {
            "lease_id": lease_id, "token": token, "lease_seconds": lease_seconds,
        })["lease"]

    def release_lease(self, lease_id: str, token: str, status: str = LEASE_COMPLETED,
                      result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", "/v1/leases/release", {
            "lease_id": lease_id, "token": token, "status": status, "result": result or {},
        })["lease"]

    @contextmanager
    def lease(self, workspace_key: str, trip_key: str, operation_type: str,
              owner: str, target: str = "", lease_seconds: int = 600,
              request_key: Optional[str] = None, heartbeat: bool = True) -> Iterator[Dict[str, Any]]:
        lease = self.acquire_lease(workspace_key=workspace_key, trip_key=trip_key,
                                   operation_type=operation_type, owner=owner, target=target,
                                   lease_seconds=lease_seconds, request_key=request_key)
        stop = threading.Event()
        heartbeat_error: List[BaseException] = []
        thread: Optional[threading.Thread] = None
        if heartbeat:
            interval = max(1, lease_seconds // 3)

            def beat() -> None:
                while not stop.wait(interval):
                    try:
                        refreshed = self.heartbeat_lease(lease["lease_id"], lease["token"], lease_seconds)
                        lease.update(refreshed)
                    except BaseException as exc:  # keep the original operation in control
                        heartbeat_error.append(exc)
                        return

            thread = threading.Thread(target=beat, name="souvenir-coordinator-heartbeat", daemon=True)
            thread.start()
        original: Optional[BaseException] = None
        try:
            yield lease
        except BaseException as exc:
            original = exc
            raise
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1)
            try:
                self.release_lease(
                    lease["lease_id"], lease["token"],
                    LEASE_FAILED if original is not None else LEASE_COMPLETED,
                    {"error": str(original)} if original is not None else {"status": "ok"},
                )
            except Exception as exc:
                if original is None:
                    raise CoordinatorError("coordinator lease 종료를 확인하지 못했습니다.") from exc
            if heartbeat_error and original is None:
                raise CoordinatorError("coordinator lease heartbeat가 끊겨 작업을 안전하게 종료했습니다.") from heartbeat_error[0]

    def enqueue_job(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/jobs/enqueue", kwargs)["job"]

    def claim_next_job(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self._request("POST", "/v1/jobs/claim", kwargs).get("job")

    def finish_job(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/jobs/finish", kwargs)["job"]

    def claim_idempotency(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/idempotency/claim", kwargs)["record"]

    def finish_idempotency(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/idempotency/finish", kwargs)["record"]

    def record_compensation(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/compensations", kwargs)["event"]

    def claim_compensation(self, scope_key: str, owner: str) -> Optional[Dict[str, Any]]:
        return self._request("POST", "/v1/compensations/claim", {
            "scope_key": scope_key, "owner": owner,
        }).get("event")

    def finish_compensation(self, **kwargs: Any) -> Dict[str, Any]:
        return self._request("POST", "/v1/compensations/finish", kwargs)["event"]


class _CoordinatorHandler(BaseHTTPRequestHandler):
    server: "CoordinatorHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise CoordinatorError("요청 본문이 너무 큽니다.")
        body = self.rfile.read(length) if length else b"{}"
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise CoordinatorError("요청 본문은 JSON 객체여야 합니다.")
        return value

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        data = _json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = self.server.token
        if not expected:
            return True
        actual = self.headers.get("Authorization", "")
        return secrets.compare_digest(actual, f"Bearer {expected}")

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send(HTTPStatus.OK, {"status": "READY", "schema_version": self.server.store.SCHEMA_VERSION})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "coordinator 인증이 필요합니다."})
            return
        try:
            body = self._json_body()
            result = self._dispatch(self.path, body)
            self._send(HTTPStatus.OK, result)
        except CoordinatorConflict as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except CoordinatorNotFound as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (CoordinatorError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "coordinator 내부 오류"})

    def _dispatch(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        store = self.server.store
        if path == "/v1/leases/acquire":
            return {"lease": _public_lease(store.acquire_lease(**body))}
        if path == "/v1/leases/heartbeat":
            return {"lease": _public_lease(store.heartbeat_lease(**body))}
        if path == "/v1/leases/release":
            return {"lease": _public_lease(store.release_lease(**body))}
        if path == "/v1/jobs/enqueue":
            return {"job": _public_row(store.enqueue_job(**body))}
        if path == "/v1/jobs/claim":
            return {"job": _public_row(store.claim_next_job(**body))}
        if path == "/v1/jobs/finish":
            return {"job": _public_row(store.finish_job(**body))}
        if path == "/v1/idempotency/claim":
            return {"record": _public_row(store.claim_idempotency(**body))}
        if path == "/v1/idempotency/finish":
            return {"record": _public_row(store.finish_idempotency(**body))}
        if path == "/v1/compensations":
            return {"event": _public_row(store.record_compensation(**body))}
        if path == "/v1/compensations/claim":
            return {"event": _public_row(store.claim_compensation(**body))}
        if path == "/v1/compensations/finish":
            return {"event": _public_row(store.finish_compensation(**body))}
        raise CoordinatorNotFound("coordinator 경로를 찾지 못했습니다.")


class CoordinatorHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: CoordinatorStore, token: Optional[str]) -> None:
        self.store = store
        self.token = token
        super().__init__(address, _CoordinatorHandler)


def serve(path: Path, host: str = "127.0.0.1", port: int = 8765,
          token: Optional[str] = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise CoordinatorError("원격 coordinator는 --token 또는 SOUVENIR_COORDINATOR_TOKEN이 필요합니다.")
    store = CoordinatorStore(path)
    server = CoordinatorHTTPServer((host, port), store, token)
    try:
        print(f"Souvenir coordinator listening on {host}:{port}")
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Souvenir P2 coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    server_parser = subparsers.add_parser("serve", help="SQLite coordinator HTTP server")
    server_parser.add_argument("--db", type=Path, required=True)
    server_parser.add_argument("--host", default="127.0.0.1")
    server_parser.add_argument("--port", type=int, default=8765)
    server_parser.add_argument("--token", default=os.environ.get("SOUVENIR_COORDINATOR_TOKEN"))
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args.db, args.host, args.port, args.token)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
