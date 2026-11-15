#!/usr/bin/env python3
"""Host-facing state machine for the Souvenir template gate.

The local executor cannot click a browser or approve a Notion OAuth dialog by
itself.  This module therefore returns a deterministic action packet for the
host (Codex/ChatGPT/another computer-use host) and records the result of that
action.  A host may satisfy ``duplicate_browser`` with CUA, use
``duplicate_native`` when the source page is accessible in the connected
workspace, or fall back to ``wait_for_manual_duplicate``.

No credentials, cookies, or browser state are accepted or persisted here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
DEFAULT_SOURCE = PACKAGE_ROOT / "assets" / "template-source.json"

STATUSES = {
    "unknown",
    "found",
    "needs_duplicate",
    "duplicate_consent_pending",
    "native_in_progress",
    "browser_in_progress",
    "manual_duplicate_wait",
    "validating",
    "confirmed",
    "blocked",
}
CAPABILITIES = {"native_duplicate", "browser_automation", "manual"}


def load_source(path: Path = DEFAULT_SOURCE) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        source = json.load(handle)
    if not isinstance(source, dict):
        raise ValueError("template-source.json의 최상위 값은 객체여야 합니다.")
    required = ("template_url", "accepted_titles", "required_sections")
    missing = [key for key in required if not source.get(key)]
    if missing:
        raise ValueError(f"템플릿 배포 설정에 필수 값이 없습니다: {', '.join(missing)}")
    return source


def _copy_identity(state: Mapping[str, Any]) -> Dict[str, Any]:
    identity = {}
    for key in ("template_page_id", "template_page_url", "template_workspace_id"):
        if state.get(key):
            identity[key] = state[key]
    return identity


def next_action(state: Mapping[str, Any], capability: str = "manual",
                source: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return the next host action without performing external I/O."""
    if capability not in CAPABILITIES:
        raise ValueError(f"지원하지 않는 템플릿 복제 capability입니다: {capability}")
    source = source or load_source()
    status = str(state.get("template_status") or "unknown")
    if status not in STATUSES:
        raise ValueError(f"알 수 없는 template_status입니다: {status}")

    base = {
        "template_url": source["template_url"],
        "accepted_titles": list(source["accepted_titles"]),
        "required_sections": list(source["required_sections"]),
        "workspace_id": state.get("workspace_id"),
        "workspace_name": state.get("workspace_name"),
    }

    if not state.get("workspace_confirmed"):
        return {
            **base,
            "template_status": status,
            "action": "confirm_workspace",
            "requires_user_confirmation": True,
            "message": (
                f"연결된 워크스페이스가 {state.get('workspace_name') or '확인되지 않음'}입니다. "
                "이 워크스페이스로 Souvenir를 진행할까요?"
            ),
        }

    if state.get("valid_copy") and _copy_identity(state):
        return {
            **base,
            **_copy_identity(state),
            "template_status": "confirmed",
            "action": "reuse_existing_copy",
            "requires_user_confirmation": False,
            "message": "기존 Souvenir 복제본을 재사용합니다.",
        }

    if not state.get("copy_checked"):
        return {
            **base,
            "template_status": status,
            "action": "check_existing_copy",
            "requires_user_confirmation": False,
            "message": "설치 기록과 기준 제목·필수 섹션으로 기존 복제본을 확인합니다.",
        }

    if status == "blocked":
        return {
            **base,
            **_copy_identity(state),
            "template_status": "blocked",
            "action": "blocked",
            "requires_user_confirmation": False,
            "reason": state.get("blocked_reason") or "템플릿 복제 또는 검증에 실패했습니다.",
            "message": "복제본 확인 전에는 여행 데이터를 저장하지 않습니다.",
        }

    if status == "native_in_progress":
        return {
            **base,
            "template_status": status,
            "action": "poll_native_duplicate",
            "requires_user_confirmation": False,
            "message": "Notion 네이티브 복제가 끝났는지 확인합니다.",
        }
    if status == "browser_in_progress":
        return {
            **base,
            "template_status": status,
            "action": "poll_browser_duplicate",
            "requires_user_confirmation": False,
            "message": "브라우저 복제 결과와 새 페이지 URL을 확인합니다.",
        }
    if status == "manual_duplicate_wait":
        return {
            **base,
            "template_status": status,
            "action": "wait_for_manual_duplicate",
            "requires_user_confirmation": False,
            "message": "사용자가 Notion에서 Duplicate를 완료했다고 알릴 때까지 기다립니다.",
        }
    if status == "validating":
        return {
            **base,
            **_copy_identity(state),
            "template_status": status,
            "action": "validate_copy",
            "requires_user_confirmation": False,
            "message": "복제된 페이지의 필수 섹션과 데이터베이스를 검증합니다.",
        }

    if not state.get("consent_granted"):
        return {
            **base,
            "template_status": "duplicate_consent_pending",
            "action": "request_duplicate_consent",
            "requires_user_confirmation": True,
            "message": (
                "Souvenir 고정 템플릿 링크를 열고 현재 워크스페이스에 복제한 뒤 "
                "구조를 검증해도 될까요? 원본은 수정하지 않습니다."
            ),
        }

    if capability == "native_duplicate":
        return {
            **base,
            "template_status": "native_in_progress",
            "action": "duplicate_native",
            "requires_user_confirmation": False,
            "message": "현재 워크스페이스에서 Notion 네이티브 복제를 시작합니다.",
        }
    if capability == "browser_automation":
        return {
            **base,
            "template_status": "browser_in_progress",
            "action": "duplicate_browser",
            "requires_user_confirmation": False,
            "sensitive_steps": ["Notion 로그인", "MFA/CAPTCHA", "워크스페이스 선택"],
            "message": "브라우저에서 템플릿을 열고 Duplicate를 실행합니다.",
        }
    return {
        **base,
        "template_status": "manual_duplicate_wait",
        "action": "wait_for_manual_duplicate",
        "requires_user_confirmation": False,
        "message": "Notion에서 템플릿을 열고 Duplicate를 누른 뒤 복제 완료를 알려주세요.",
    }


def apply_event(state: Mapping[str, Any], event: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Apply one host result and return a new serializable gate state."""
    result = dict(state)
    payload = dict(payload or {})
    if event == "workspace_confirmed":
        result.update(payload)
        result["workspace_confirmed"] = True
        result["template_status"] = "unknown"
    elif event == "copy_checked":
        result["copy_checked"] = True
        result["valid_copy"] = bool(payload.get("valid_copy"))
        result.update({key: payload[key] for key in _copy_identity(payload)})
        result["template_status"] = "found" if result["valid_copy"] else "needs_duplicate"
    elif event == "consent_granted":
        result["consent_granted"] = True
        result["template_status"] = "needs_duplicate"
    elif event == "consent_denied":
        result["consent_granted"] = False
        result["template_status"] = "needs_duplicate"
    elif event == "native_started":
        result["template_status"] = "native_in_progress"
    elif event == "browser_started":
        result["template_status"] = "browser_in_progress"
    elif event == "manual_waiting":
        result["template_status"] = "manual_duplicate_wait"
    elif event == "duplicate_detected":
        result.update({key: payload[key] for key in _copy_identity(payload)})
        result["template_status"] = "validating"
    elif event == "copy_validated":
        valid = bool(payload.get("valid"))
        result["valid_copy"] = valid
        result["template_status"] = "confirmed" if valid else "blocked"
        result["template_confirmed"] = valid
        if not valid:
            result["blocked_reason"] = payload.get("reason") or "필수 섹션 검증 실패"
    elif event == "duplicate_failed":
        result["template_status"] = "blocked"
        result["blocked_reason"] = payload.get("reason") or "복제 실패"
    else:
        raise ValueError(f"지원하지 않는 템플릿 게이트 이벤트입니다: {event}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Souvenir template gate action planner")
    parser.add_argument("command", choices=["plan", "event"])
    parser.add_argument("--state", type=Path, help="현재 게이트 상태 JSON")
    parser.add_argument("--event", dest="event_name", help="적용할 host 이벤트")
    parser.add_argument("--payload", help="이벤트 payload JSON")
    parser.add_argument("--capability", choices=sorted(CAPABILITIES), default="manual")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    source = load_source(args.source)
    state: Dict[str, Any] = {}
    if args.state:
        with args.state.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("상태 JSON의 최상위 값은 객체여야 합니다.")
        state = loaded
    if args.command == "plan":
        result = next_action(state, args.capability, source)
    else:
        if not args.event_name:
            raise ValueError("event에는 --event가 필요합니다.")
        payload = json.loads(args.payload) if args.payload else {}
        result = apply_event(state, args.event_name, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
