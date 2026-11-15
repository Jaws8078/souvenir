#!/usr/bin/env python3
"""Local-executor entry point for Souvenir's deterministic travel engine.

The Codex/Claude/Gemini local executor calls this file on behalf of the
conversation. Users should not need to type these commands themselves. The
wrapper keeps the host-facing entry point stable while the implementation can
continue to live in ``trip_engine.py``.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from trip_engine import configure_output, main as engine_main  # noqa: E402
from template_gate import detect_local_notion, main as template_gate_main  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Template duplication is a host-facing workflow.  The local process only
    # plans the next action and records host results; CUA/Notion tools perform
    # the actual browser or native duplication outside this Python process.
    if args and args[0] == "template-gate":
        return template_gate_main(args[1:])
    if args == ["--status"]:
        print(
            json.dumps(
                {
                    "status": "READY",
                    "engine": "souvenir-trip-engine",
                    "entrypoint": str(Path(__file__).resolve()),
                    "python": platform.python_version(),
                    "coordinator": {
                        "configured": bool(os.environ.get("SOUVENIR_COORDINATOR_URL")),
                        "endpoint": os.environ.get("SOUVENIR_COORDINATOR_URL") or None,
                        "mode": os.environ.get("SOUVENIR_COORDINATOR_MODE", "auto"),
                        "expected_concurrent": os.environ.get("SOUVENIR_EXPECT_CONCURRENT", "")
                        .strip().lower() in {"1", "true", "yes", "on"},
                    },
                    "http": {
                        "transport": os.environ.get("SOUVENIR_HTTP_TRANSPORT", "urllib"),
                        "max_retries": os.environ.get("SOUVENIR_HTTP_MAX_RETRIES", "3"),
                    },
                    "template_gate": {
                        "entrypoint": str(SCRIPT_DIR / "template_gate.py"),
                        "local_notion_preflight": detect_local_notion(),
                        "browser_actions": "host-provided",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    return engine_main(args)


if __name__ == "__main__":
    configure_output()
    raise SystemExit(main())
