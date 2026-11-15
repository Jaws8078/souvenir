"""Tests for the host-facing template duplication gate."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "template_gate.py"
SPEC = importlib.util.spec_from_file_location("souvenir_template_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules["souvenir_template_gate"] = gate
SPEC.loader.exec_module(gate)


SOURCE = gate.load_source()


class TemplateGateTests(unittest.TestCase):
    def base(self):
        return {
            "workspace_id": "workspace-1",
            "workspace_name": "Yuseong's Workspace",
            "workspace_confirmed": True,
            "copy_checked": True,
            "valid_copy": False,
            "template_status": "needs_duplicate",
        }

    def test_workspace_confirmation_precedes_template_access(self):
        action = gate.next_action({"workspace_name": "Workspace"}, "manual", SOURCE)
        self.assertEqual(action["action"], "confirm_workspace")
        self.assertTrue(action["requires_user_confirmation"])

    def test_duplicate_requires_explicit_consent(self):
        action = gate.next_action(self.base(), "browser_automation", SOURCE)
        self.assertEqual(action["action"], "request_duplicate_consent")
        self.assertTrue(action["requires_user_confirmation"])
        self.assertEqual(action["template_url"], SOURCE["template_url"])

    def test_browser_action_is_host_owned(self):
        state = gate.apply_event(self.base(), "consent_granted")
        action = gate.next_action(state, "browser_automation", SOURCE)
        self.assertEqual(action["action"], "duplicate_browser")
        self.assertIn("Notion 로그인", action["sensitive_steps"])

    def test_manual_fallback_waits_without_creating_data(self):
        state = gate.apply_event(self.base(), "consent_granted")
        action = gate.next_action(state, "manual", SOURCE)
        self.assertEqual(action["action"], "wait_for_manual_duplicate")
        self.assertEqual(action["template_status"], "manual_duplicate_wait")

    def test_in_progress_states_are_polled_instead_of_duplicated_again(self):
        state = gate.apply_event(self.base(), "consent_granted")
        state = gate.apply_event(state, "browser_started")
        self.assertEqual(gate.next_action(state, "browser_automation", SOURCE)["action"],
                         "poll_browser_duplicate")
        state = gate.apply_event(state, "duplicate_detected", {
            "template_page_id": "page-1",
            "template_page_url": "https://notion.so/page-1",
            "template_workspace_id": "workspace-1",
        })
        self.assertEqual(gate.next_action(state, "browser_automation", SOURCE)["action"],
                         "validate_copy")

    def test_validated_copy_unlocks_planning(self):
        state = gate.apply_event(self.base(), "consent_granted")
        state = gate.apply_event(state, "duplicate_detected", {
            "template_page_id": "page-1",
            "template_page_url": "https://notion.so/page-1",
            "template_workspace_id": "workspace-1",
        })
        state = gate.apply_event(state, "copy_validated", {"valid": True})
        action = gate.next_action(state, "manual", SOURCE)
        self.assertEqual(action["action"], "reuse_existing_copy")
        self.assertEqual(action["template_status"], "confirmed")

    def test_failed_validation_blocks_writes(self):
        state = gate.apply_event(self.base(), "duplicate_failed", {"reason": "복제 허용 꺼짐"})
        action = gate.next_action(state, "manual", SOURCE)
        self.assertEqual(action["action"], "blocked")
        self.assertIn("복제 허용", action["reason"])

    def test_cached_copy_is_reused_within_ttl(self):
        state = self.base()
        state.update({
            "workspace_id": "workspace-1",
            "template_page_id": "page-1",
            "template_page_url": "https://notion.so/page-1",
            "template_workspace_id": "workspace-1",
            "valid_copy": True,
        })
        cache = {"version": gate.CACHE_VERSION, "entries": {
            f"workspace-1:{gate.source_fingerprint(SOURCE)}": {
                "valid_copy": True,
                "template_workspace_id": "workspace-1",
                "template_page_id": "page-1",
                "template_page_url": "https://notion.so/page-1",
                "checked_at": 100.0,
            }
        }}
        cached = gate.cached_copy(state, SOURCE, cache=cache, now=120.0, ttl_seconds=60)
        self.assertEqual(cached["template_page_id"], "page-1")

    def test_expired_cache_is_ignored(self):
        state = {"workspace_id": "workspace-1"}
        cache = {"version": gate.CACHE_VERSION, "entries": {
            f"workspace-1:{gate.source_fingerprint(SOURCE)}": {
                "valid_copy": True,
                "template_workspace_id": "workspace-1",
                "template_page_id": "page-1",
                "template_page_url": "https://notion.so/page-1",
                "checked_at": 100.0,
            }
        }}
        self.assertIsNone(gate.cached_copy(state, SOURCE, cache=cache, now=200.0, ttl_seconds=60))


if __name__ == "__main__":
    unittest.main()
