#!/usr/bin/env python3
"""Local-executor entry point for Souvenir's deterministic travel engine.

The Codex/Claude/Gemini local executor calls this file on behalf of the
conversation. Users should not need to type these commands themselves. The
wrapper keeps the host-facing entry point stable while the implementation can
continue to live in ``trip_engine.py``.
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from trip_engine import configure_output, main as engine_main  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--status"]:
        print(
            json.dumps(
                {
                    "status": "READY",
                    "engine": "souvenir-trip-engine",
                    "entrypoint": str(Path(__file__).resolve()),
                    "python": platform.python_version(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    return engine_main(args)


if __name__ == "__main__":
    configure_output()
    raise SystemExit(main())
