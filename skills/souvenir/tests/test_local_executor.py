"""Tests for the local-executor entry point used by packaged hosts."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_souvenir.py"
SPEC = importlib.util.spec_from_file_location("run_souvenir", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_souvenir"] = runner
SPEC.loader.exec_module(runner)


class LocalExecutorTests(unittest.TestCase):
    def test_status_is_local_and_does_not_run_engine(self):
        with patch("run_souvenir.engine_main") as engine, patch("builtins.print") as output:
            self.assertEqual(runner.main(["--status"]), 0)
        engine.assert_not_called()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["engine"], "souvenir-trip-engine")

    def test_other_commands_delegate_to_trip_engine(self):
        with patch("run_souvenir.engine_main", return_value=7) as engine:
            self.assertEqual(runner.main(["refresh", "--pretty"]), 7)
        engine.assert_called_once_with(["refresh", "--pretty"])


if __name__ == "__main__":
    unittest.main()
