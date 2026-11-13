#!/usr/bin/env python3
"""Check the local environment and optionally install Souvenir through Codex."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from typing import Any


REPOSITORY = "Jaws8078/souvenir"
MARKETPLACE = "souvenir-marketplace"
PLUGIN = "souvenir"

INSTALLERS: dict[str, dict[str, Any]] = {
    "codex": {
        "command": "codex",
        "install": [
            ["codex", "plugin", "marketplace", "add", REPOSITORY, "--ref", "main"],
            ["codex", "plugin", "add", f"{PLUGIN}@{MARKETPLACE}"],
        ],
        "verify": ["codex", "plugin", "list"],
    }
}


def detect() -> dict[str, Any]:
    """Return environment facts without installing anything or reading secrets."""
    return {
        "python": bool(shutil.which("python3") or shutil.which("python") or shutil.which("py")),
        "agents": {
            name: {"installed": bool(shutil.which(item["command"]))}
            for name, item in INSTALLERS.items()
        },
        "chatgpt": {
            "installation": "manual",
            "message": "ChatGPT 데스크톱은 Plugins Directory에서 Souvenir를 설치하세요.",
        },
    }


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)

    output = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode == 0, output[-1] if output else ""


def install(agent: str) -> dict[str, str]:
    info = INSTALLERS[agent]
    command_name = info["command"]
    if not shutil.which(command_name):
        return {
            "agent": agent,
            "status": "UNAVAILABLE",
            "message": f"{command_name}를 찾지 못했어요.",
        }

    for command in info["install"]:
        ok, message = _run(command)
        if not ok:
            return {
                "agent": agent,
                "status": "FAILED",
                "message": message or "공식 설치 명령이 실패했어요.",
            }

    ok, message = _run(info["verify"])
    return {
        "agent": agent,
        "status": "READY" if ok else "VERIFY_FAILED",
        "message": message
        or ("설치와 플러그인 목록 확인을 마쳤어요." if ok else "설치 후 목록 확인에 실패했어요."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Souvenir 설치 환경 확인 및 명시적 Codex 플러그인 등록"
    )
    parser.add_argument(
        "--agent",
        choices=tuple(INSTALLERS),
        action="append",
        help="등록할 에이전트; 생략하면 설치된 지원 에이전트를 사용합니다.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="공식 설치 명령을 실제로 실행합니다. 기본값은 점검만 수행합니다.",
    )
    args = parser.parse_args(argv)

    state = detect()
    selected = args.agent or [
        name for name, item in state["agents"].items() if item["installed"]
    ]

    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "CHECKED",
                    "environment": state,
                    "selected": selected,
                    "message": "설치를 진행하려면 사용자가 --apply를 선택해야 해요.",
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not selected:
        print(
            json.dumps(
                {
                    "status": "NO_SUPPORTED_AGENT",
                    "environment": state,
                    "message": "Codex CLI를 설치한 뒤 다시 실행하거나 ChatGPT Plugins Directory를 사용하세요.",
                },
                ensure_ascii=False,
            )
        )
        return 2

    results = [install(agent) for agent in selected]
    failed = any(item["status"] != "READY" for item in results)
    print(
        json.dumps(
            {
                "status": "FAILED" if failed else "READY",
                "environment": state,
                "results": results,
            },
            ensure_ascii=False,
        )
    )
    if not failed:
        print("설치가 끝났어요. 새 Codex 채팅에서 $souvenir를 입력하세요.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
