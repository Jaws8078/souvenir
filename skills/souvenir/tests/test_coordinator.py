"""Offline tests for the optional P2 coordinator."""
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "coordinator.py"
SPEC = importlib.util.spec_from_file_location("souvenir_coordinator", SCRIPT)
assert SPEC and SPEC.loader
coordinator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coordinator)


class Clock:
    def __init__(self):
        self.value = coordinator.dt.datetime(2026, 11, 14, tzinfo=coordinator.dt.timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += coordinator.dt.timedelta(seconds=seconds)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = coordinator.CoordinatorStore(Path(self.tmp.name) / "coordinator.sqlite3", self.clock)
        self.addCleanup(self.store.close)

    def test_lease_is_serialized_heartbeated_and_reclaimed_after_expiry(self):
        first = self.store.acquire_lease("workspace", "trip", "edit", "A", lease_seconds=10, request_key="a")
        with self.assertRaises(coordinator.CoordinatorConflict):
            self.store.acquire_lease("workspace", "trip", "edit", "B", lease_seconds=10, request_key="b")
        self.clock.advance(5)
        refreshed = self.store.heartbeat_lease(first["lease_id"], first["token"], lease_seconds=10)
        self.assertGreater(refreshed["expires_at"], first["expires_at"])
        self.clock.advance(11)
        second = self.store.acquire_lease("workspace", "trip", "edit", "B", lease_seconds=10, request_key="b")
        self.assertEqual(second["owner"], "B")
        old = self.store.connection.execute(
            "SELECT status FROM leases WHERE lease_id=?", (first["lease_id"],)
        ).fetchone()
        self.assertEqual(old["status"], coordinator.LEASE_EXPIRED)
        self.store.release_lease(second["lease_id"], second["token"])

    def test_queue_is_idempotent_and_one_writer_at_a_time(self):
        first = self.store.enqueue_job("workspace/trip", "edit", {"field": "a"}, "key-a")
        duplicate = self.store.enqueue_job("workspace/trip", "edit", {"field": "changed"}, "key-a")
        self.assertEqual(first["job_id"], duplicate["job_id"])
        second = self.store.enqueue_job("workspace/trip", "edit", {"field": "b"}, "key-b")
        running = self.store.claim_next_job("workspace/trip", "worker-a", lease_seconds=30)
        self.assertEqual(running["job_id"], first["job_id"])
        self.assertIsNone(self.store.claim_next_job("workspace/trip", "worker-b", lease_seconds=30))
        self.store.finish_job(running["job_id"], running["token"], coordinator.JOB_COMPLETED, {"ok": True})
        next_job = self.store.claim_next_job("workspace/trip", "worker-b", lease_seconds=30)
        self.assertEqual(next_job["job_id"], second["job_id"])

    def test_idempotency_and_compensation_log_are_durable(self):
        first = self.store.claim_idempotency("workspace/trip", "request-1", "edit", {"x": 1})
        self.assertTrue(first["claimed"])
        replay = self.store.claim_idempotency("workspace/trip", "request-1", "edit", {"x": 2})
        self.assertFalse(replay["claimed"])
        self.store.finish_idempotency("workspace/trip", "request-1", "completed", {"page": "p1"})
        done = self.store.claim_idempotency("workspace/trip", "request-1", "edit", {"x": 3})
        self.assertEqual(done["status"], "completed")
        event = self.store.record_compensation(
            "workspace/trip", "op-1", "created_row", {"page_id": "p1"}, {"action": "archive", "page_id": "p1"}
        )
        claimed = self.store.claim_compensation("workspace/trip", "worker")
        self.assertEqual(claimed["event_id"], event["event_id"])
        self.store.finish_compensation(
            event["event_id"], coordinator.EVENT_COMPLETED, {"archived": True}, claimed["claim_token"]
        )
        final = self.store.connection.execute(
            "SELECT status FROM compensation_events WHERE event_id=?", (event["event_id"],)
        ).fetchone()
        self.assertEqual(final["status"], coordinator.EVENT_COMPLETED)

    def test_http_client_uses_authentication_and_lease_context(self):
        try:
            server = coordinator.CoordinatorHTTPServer(
                ("127.0.0.1", 0), self.store, "secret-token"
            )
        except PermissionError:
            self.skipTest("이 실행 환경에서는 localhost socket bind가 허용되지 않습니다.")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        client = coordinator.CoordinatorClient(
            f"http://127.0.0.1:{server.server_port}", "secret-token"
        )
        self.assertEqual(client.health()["status"], "READY")
        with self.assertRaises(coordinator.CoordinatorError):
            coordinator.CoordinatorClient(
                f"http://127.0.0.1:{server.server_port}", "wrong-token"
            )._request("POST", "/v1/jobs/claim", {"scope_key": "workspace/trip", "owner": "x"})
        with client.lease("workspace", "trip", "edit", "http-worker", lease_seconds=10) as lease:
            self.assertTrue(lease["lease_id"])
            self.assertTrue(lease["token"])
        released = self.store.acquire_lease(
            "workspace", "trip", "edit", "other", lease_seconds=10, request_key="other"
        )
        self.assertEqual(released["owner"], "other")


if __name__ == "__main__":
    unittest.main()
