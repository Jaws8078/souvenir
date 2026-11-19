#!/usr/bin/env python3
"""Souvenir MCP Apps server.

This is a small dependency-free stdio MCP server for the candidate review
board.  Codex/ChatGPT starts it from ``mcp.json``; users do not run the local
candidate-review HTTP preview server for the MCP Apps flow.

The server exposes two tools:

* ``souvenir_render_candidate_review`` returns the UI resource and a public
  candidate packet for the initial widget render.
* ``souvenir_search_candidates`` runs the existing read-only search executors
  and returns a public packet directly to the widget.

The UI resource is inlined so the MCP Apps host does not need to resolve local
CSS/JavaScript URLs or call a result relay endpoint.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
WEB_DIR = SKILL_DIR / "web"
SAMPLE_PACKET = SKILL_DIR / "assets" / "candidate-review.sample.json"
RESOURCE_URI = "ui://souvenir/candidate-review-v1.html"
MCP_PROTOCOL = "2025-03-26"
SEARCH_TIMEOUT_SECONDS = 120

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from candidate_review_web import normalize_packet  # noqa: E402


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    rendered = str(value).strip()
    return rendered or fallback


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": code, "message": message}


def _public_packet(raw: Any, explicit_kind: Optional[str] = None) -> Dict[str, Any]:
    """Normalize raw provider data while accepting an already-public packet."""
    if not isinstance(raw, Mapping):
        raise ValueError("후보 패킷은 JSON 객체여야 합니다.")
    sections = raw.get("sections")
    if isinstance(sections, Mapping) and raw.get("review_only") is True:
        # A widget may call the render tool again with a packet it already
        # received.  Keep its public IDs and presentation fields stable.
        return copy.deepcopy(dict(raw))
    return normalize_packet(raw, explicit_kind=explicit_kind)


def _widget_html() -> str:
    html = (WEB_DIR / "candidate-review.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "candidate-review.css").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "candidate-review.js").read_text(encoding="utf-8")
    stylesheet = f"<style>\n{css}\n</style>"
    # The HTML is embedded inside a resource string.  Escaping a closing
    # script token prevents a candidate value from terminating the wrapper.
    safe_javascript = javascript.replace("</script", "<\\/script")
    script = f"<script>\n{safe_javascript}\n</script>"
    html = html.replace('<link rel="stylesheet" href="candidate-review.css">', stylesheet)
    html = html.replace('<script src="candidate-review.js" defer></script>', script)
    return html


def _tool_text(summary: str) -> list[Dict[str, str]]:
    return [{"type": "text", "text": summary}]


def _tool_result(packet: Mapping[str, Any], summary: str) -> Dict[str, Any]:
    return {
        "content": _tool_text(summary),
        "structuredContent": dict(packet),
    }


def _render(args: Mapping[str, Any]) -> Dict[str, Any]:
    raw = args.get("packet")
    if raw is None:
        raw = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    packet = _public_packet(raw, _text(args.get("kind")) or None)
    destination = packet.get("trip", {}).get("destination", "여행")
    count = sum(
        len(section.get("candidates", []))
        for section in (packet.get("sections") or {}).values()
        if isinstance(section, Mapping)
    )
    return _tool_result(packet, f"{destination} 후보 검토 화면을 열었어요. 후보 {count}개를 확인할 수 있어요.")


def _flight_payload(conditions: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {
        "flyFrom",
        "flyTo",
        "departureDate",
        "returnDate",
        "adults",
        "price_to",
        "bags",
        "cabinClass",
        "maxStops",
        "departAfter",
        "departBefore",
        "returnAfter",
        "returnBefore",
    }
    return {key: value for key, value in conditions.items() if key in allowed and value not in (None, "")}


def _hotel_payload(conditions: Mapping[str, Any]) -> Dict[str, Any]:
    search_keys = {
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
    search = {key: value for key, value in conditions.items() if key in search_keys and value not in (None, "")}
    if "guests" not in search:
        search["guests"] = 2
    room_filters = conditions.get("room_filters")
    if not isinstance(room_filters, Mapping):
        room_filters = {
            key: conditions[key]
            for key in ("bed_type", "refundable_only")
            if key in conditions and conditions[key] not in (None, "", False)
        }
    payload: Dict[str, Any] = {"search": search}
    if conditions.get("budget_per_night_krw") not in (None, ""):
        payload["budget_per_night_krw"] = conditions["budget_per_night_krw"]
    if room_filters:
        payload["room_filters"] = dict(room_filters)
    return payload


def _places_args(conditions: Mapping[str, Any]) -> list[str]:
    query = _text(conditions.get("query"), _text(conditions.get("destination"), "여행지"))
    args = ["places", "--google-query", query]
    destination = _text(conditions.get("destination"))
    if destination:
        args.extend(["--destination", destination])
    rank = _text(conditions.get("rank_preference"))
    if rank in {"RELEVANCE", "DISTANCE"}:
        args.extend(["--google-rank", rank])
    included_type = _text(conditions.get("included_type"))
    if included_type:
        args.extend(["--google-included-type", included_type])
    if conditions.get("open_now") is True:
        args.append("--google-open-now")
    return args


def _run_search(section: str, conditions: Mapping[str, Any]) -> Dict[str, Any]:
    if section == "flights":
        payload = _flight_payload(conditions)
        command = ["flights"]
    elif section == "hotels":
        payload = _hotel_payload(conditions)
        command = ["hotels"]
    elif section == "places":
        payload = None
        command = _places_args(conditions)
    else:
        raise ValueError("지원하지 않는 검색 파트입니다.")

    input_path: Optional[Path] = None
    try:
        if payload is not None:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                input_path = Path(handle.name)
            command.extend(["--input", str(input_path)])
        command.extend(["--pretty"])
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "run_souvenir.py"), *command],
            cwd=str(SCRIPT_DIR),
            text=True,
            capture_output=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
            check=False,
        )
        stdout = completed.stdout.strip()
        if stdout:
            try:
                raw = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("검색 실행기가 JSON 결과를 반환하지 않았어요.") from exc
        else:
            raw = {}
        if completed.returncode != 0 and not raw.get("candidates"):
            error_text = _text(raw.get("error"), "SEARCH_EXECUTOR_ERROR") if isinstance(raw, Mapping) else "SEARCH_EXECUTOR_ERROR"
            raise RuntimeError(error_text)
        return _public_packet(raw, section)
    finally:
        if input_path:
            try:
                input_path.unlink()
            except OSError:
                pass


def _search(args: Mapping[str, Any]) -> Dict[str, Any]:
    section = _text(args.get("section"))
    conditions = args.get("conditions")
    if not isinstance(conditions, Mapping):
        raise ValueError("검색 조건은 객체여야 합니다.")
    packet = _run_search(section, conditions)
    count = len((packet.get("sections", {}).get(section, {}) or {}).get("candidates", []))
    return _tool_result(packet, f"{section} 조건으로 후보 {count}개를 검색했어요.")


TOOLS = [
    {
        "name": "souvenir_render_candidate_review",
        "title": "Souvenir 후보 검토 화면 열기",
        "description": "공개 후보 패킷을 같은 화면의 Souvenir 후보 검토 UI로 표시합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packet": {"type": "object", "description": "항공권·숙소·장소 후보 원본 또는 공개 패킷"},
                "kind": {"type": "string", "enum": ["flights", "hotels", "places"]},
            },
            "required": ["packet"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"version": {"type": "integer"}, "sections": {"type": "object"}}},
        "_meta": {
            "ui": {"resourceUri": RESOURCE_URI, "visibility": "model"},
            "openai/outputTemplate": RESOURCE_URI,
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "후보 검토 화면을 여는 중…",
            "openai/toolInvocation/invoked": "후보 검토 화면을 열었어요.",
        },
    },
    {
        "name": "souvenir_search_candidates",
        "title": "Souvenir 후보 다시 검색",
        "description": "현재 후보 보드의 한 파트 조건을 읽기 전용으로 다시 검색하고 공개 UI 패킷을 반환합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "section": {"type": "string", "enum": ["flights", "hotels", "places"]},
                "conditions": {"type": "object"},
                "return_contract": {"type": "string", "enum": ["public_packet"]},
            },
            "required": ["section", "conditions"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"version": {"type": "integer"}, "sections": {"type": "object"}}},
        "_meta": {
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "조건으로 다시 검색하는 중…",
            "openai/toolInvocation/invoked": "검색 결과를 갱신했어요.",
        },
    },
]


def _resource() -> Dict[str, Any]:
    return {
        "uri": RESOURCE_URI,
        "mimeType": "text/html;profile=mcp-app",
        "text": _widget_html(),
        "_meta": {"ui": {"prefersBorder": True, "widgetDescription": "Souvenir 여행 후보를 비교하고 조건을 다시 검색하는 화면입니다."}},
    }


def _send(message: Mapping[str, Any]) -> None:
    sys.stdout.write(_json(message) + "\n")
    sys.stdout.flush()


def _respond(request_id: Any, result: Mapping[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _fail(request_id: Any, code: int, message: str, data: Any = None) -> None:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _send({"jsonrpc": "2.0", "id": request_id, "error": error})


def _handle(message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
    if method == "initialize":
        requested = _text(params.get("protocolVersion"), MCP_PROTOCOL)
        _respond(request_id, {
            "protocolVersion": requested if requested in {MCP_PROTOCOL, "2025-06-18"} else MCP_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": "souvenir-mcp-app", "version": "0.1.0"},
            "instructions": "Souvenir 후보 검색 결과가 준비되면 souvenir_render_candidate_review 도구로 후보 검토 UI를 여세요.",
        })
        return None
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        _respond(request_id, {})
        return None
    if method == "tools/list":
        _respond(request_id, {"tools": TOOLS})
        return None
    if method == "resources/list":
        resource = _resource()
        _respond(request_id, {"resources": [{key: resource[key] for key in ("uri", "mimeType")}]})
        return None
    if method == "resources/read":
        if params.get("uri") != RESOURCE_URI:
            _fail(request_id, -32002, "리소스를 찾을 수 없어요.")
        else:
            resource = _resource()
            _respond(request_id, {"contents": [{key: resource[key] for key in ("uri", "mimeType", "text")} | {"_meta": resource["_meta"]}]})
        return None
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
        try:
            if name == "souvenir_render_candidate_review":
                result = _render(arguments)
            elif name == "souvenir_search_candidates":
                result = _search(arguments)
            else:
                _fail(request_id, -32601, f"도구를 찾을 수 없어요: {name}")
                return None
            _respond(request_id, result)
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            _fail(request_id, -32000, str(exc), {"error": "SOUVENIR_SEARCH_ERROR"})
        return None
    if request_id is not None:
        _fail(request_id, -32601, f"지원하지 않는 MCP 메서드입니다: {method}")
    return None


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _fail(None, -32700, "JSON을 읽을 수 없어요.")
            continue
        if isinstance(message, Mapping):
            _handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
