import importlib.util
import json
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "install_souvenir.py"
SPEC = importlib.util.spec_from_file_location("install_souvenir", SCRIPT)
assert SPEC and SPEC.loader
install_souvenir = importlib.util.module_from_spec(SPEC)
sys.modules["install_souvenir"] = install_souvenir
SPEC.loader.exec_module(install_souvenir)


class InstallSouvenirTests(unittest.TestCase):
    def test_detect_does_not_install_or_read_secrets(self):
        with patch(
            "install_souvenir.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ):
            state = install_souvenir.detect()
        self.assertTrue(state["python"])
        self.assertTrue(state["agents"]["codex"]["installed"])
        self.assertEqual(state["chatgpt"]["installation"], "manual")

    def test_check_is_dry_run(self):
        state = {
            "python": True,
            "agents": {"codex": {"installed": True}},
            "chatgpt": {"installation": "manual"},
        }
        with patch("install_souvenir.detect", return_value=state), patch(
            "install_souvenir.install"
        ) as installer, patch("builtins.print") as output:
            self.assertEqual(install_souvenir.main([]), 0)
        installer.assert_not_called()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "CHECKED")

    def test_apply_installs_and_verifies_codex(self):
        state = {
            "python": True,
            "agents": {"codex": {"installed": True}},
            "chatgpt": {"installation": "manual"},
        }
        with patch("install_souvenir.detect", return_value=state), patch(
            "install_souvenir._run", return_value=(True, "listed")
        ) as runner, patch("install_souvenir.shutil.which", return_value="/bin/codex"), patch("builtins.print") as output:
            self.assertEqual(install_souvenir.main(["--agent", "codex", "--apply"]), 0)
        self.assertEqual(runner.call_count, 3)
        self.assertIn("$souvenir", output.call_args_list[-1].args[0])

    def test_repeated_agent_is_installed_only_once(self):
        with patch("install_souvenir.install", return_value={"status": "READY"}) as installer, \
                patch("builtins.print"):
            self.assertEqual(install_souvenir.main(["--apply", "--agent", "codex", "--agent", "codex"]), 0)
        installer.assert_called_once_with("codex")

    def test_subprocess_failure_reports_stderr_not_progress(self):
        result = subprocess.CompletedProcess([], 1, "Installing...", "Permission denied")
        with patch("install_souvenir.subprocess.run", return_value=result):
            self.assertEqual(install_souvenir._run(["codex"]), (False, "Permission denied"))

    def test_timeout_returns_failure(self):
        with patch("install_souvenir.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 120)):
            self.assertFalse(install_souvenir._run(["codex"])[0])

    def test_failed_step_does_not_run_following_commands(self):
        with patch("install_souvenir.shutil.which", return_value="/bin/codex"), \
                patch("install_souvenir._run", return_value=(False, "failed")) as run:
            self.assertEqual(install_souvenir.install("codex")["status"], "FAILED")
        self.assertEqual(run.call_count, 1)

    def test_verification_failure_is_not_reported_as_ready(self):
        with patch("install_souvenir.shutil.which", return_value="/bin/codex"), \
                patch("install_souvenir._run", side_effect=[(True, ""), (True, ""), (False, "failed")]):
            self.assertEqual(install_souvenir.install("codex")["status"], "VERIFY_FAILED")

    def test_unavailable_agent_does_not_execute_commands(self):
        with patch("install_souvenir.shutil.which", return_value=None), patch("install_souvenir._run") as run:
            self.assertEqual(install_souvenir.install("codex")["status"], "UNAVAILABLE")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
