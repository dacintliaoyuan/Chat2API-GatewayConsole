"""S2 security fence: the management layer binds the runtime ACK watermark.

These tests drive the REAL ``ShareManager`` publication path (not
``store.commit_applied`` directly): a revoke/rotate intent recorded in the
security journal may be marked applied ONLY by an ACK whose
``security_generation`` covers it.  A missing, invalid or stale watermark is a
FAILED publication (fail-closed): the journal row stays pending and the runtime
is never credited with a revoke it did not apply.  A missing field is NEVER
read as zero.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share.broker_protocol import BrokerResult  # noqa: E402
from share.management import (BrokerClientAdapter, BrokerError,  # noqa: E402
                             ShareManager, _fingerprint)
from share.store import ShareStore, StoreError  # noqa: E402


class ScriptedBroker:
    """Broker double whose ACK shape each test controls explicitly."""

    def __init__(self):
        self.published = []
        self.security_generation = "auto"
        self.include_security = True
        self.instance_id = "runtime-instance-1"
        self.emergency_stops = 0

    def publish(self, projection, terminate_keys=None):
        self.published.append(projection)
        ack = {"applied_generation": projection["config_generation"],
               "instance_id": self.instance_id}
        if self.include_security:
            ack["security_generation"] = (
                projection["config_generation"]
                if self.security_generation == "auto"
                else self.security_generation)
        return ack

    def snapshot(self):
        return {"name": "scripted", "state": "ok"}

    def emergency_stop(self):
        self.emergency_stops += 1
        return {"state": "stopped", "stopped": True}


class _BrokerClient:
    """Minimal broker client returning one raw BrokerResult (adapter seam)."""

    def __init__(self, ack):
        self.ack = ack

    def publish(self, projection, terminate_keys=None):
        return BrokerResult.ok(dict(self.ack))

    def status(self):
        return BrokerResult.ok({})


class ShareSecurityGenerationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.store_path = Path(self.td.name) / "share.db"
        self.broker = ScriptedBroker()
        self.mgr = ShareManager(store_path=self.store_path, broker=self.broker)

    def _store(self):
        store = ShareStore(self.store_path)
        self.addCleanup(store.close)
        return store

    def _create(self, name="friendA"):
        created = self.mgr.create_key(name)
        self.assertTrue(created["published"], created)
        return created

    # ------------------------------------------------------------ success
    def test_ack_security_generation_binds_pending_revoke(self):
        created = self._create()
        target = created["desired_generation"] + 1
        revoked = self.mgr.revoke_and_terminate_key(created["key_id"])
        self.assertTrue(revoked["published"])
        store = self._store()
        record = store.security_records()[-1]
        self.assertEqual(record["action"], "revoke")
        self.assertEqual(record["generation"], target)
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["applied_generation"], target)
        self.assertEqual(record["instance_id"], "runtime-instance-1")
        generation = store.generation()
        self.assertEqual(generation["applied_generation"], target)
        self.assertTrue(generation["in_sync"])
        self.assertEqual(self.broker.emergency_stops, 0)
        # The applied publication op records the ACK's real watermark.
        applied_ops = [op for op in store.operations(kind="publish")
                       if op["status"] == "applied"]
        self.assertEqual(applied_ops[-1]["payload"]["security_generation"],
                         target)

    # ------------------------------------------------------- fail-closed
    def test_missing_security_generation_fails_closed(self):
        created = self._create()
        self.broker.include_security = False
        with self.assertRaises(BrokerError):
            self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        record = store.security_records()[-1]
        self.assertEqual(record["status"], "pending")
        generation = store.generation()
        self.assertEqual(generation["applied_generation"],
                         created["desired_generation"])
        self.assertFalse(generation["in_sync"])
        failure = self.mgr.status()["last_publication_failure"]
        self.assertEqual(failure["kind"], "publish")
        self.assertIn("no security_generation", failure["error"])
        failed = store.operations(kind="publish", status="failed")
        payload = failed[-1]["payload"]
        self.assertTrue(payload["security_unconfirmed"])
        self.assertIsNone(payload["ack_security_generation"])
        self.assertEqual(payload["pending_security_generations"],
                         [created["desired_generation"] + 1])
        self.assertEqual(self.broker.emergency_stops, 1)

    def test_stale_security_generation_fails_closed(self):
        created = self._create()
        target = created["desired_generation"] + 1
        self.broker.security_generation = target - 1
        with self.assertRaises(BrokerError):
            self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        record = store.security_records()[-1]
        self.assertEqual(record["generation"], target)
        self.assertEqual(record["status"], "pending")
        failed = store.operations(kind="publish", status="failed")
        payload = failed[-1]["payload"]
        self.assertEqual(payload["ack_security_generation"], target - 1)
        self.assertIn("does not cover", payload["error"])
        self.assertEqual(payload["pending_security_generations"], [target])
        self.assertEqual(self.broker.emergency_stops, 1)

    def test_runtime_entry_watermark_binds_like_an_int(self):
        created = self._create()
        target = created["desired_generation"] + 1
        # record_revoke_generation stores the per-Key entry dict; the
        # management side must read its ``generation`` as the watermark.
        self.broker.security_generation = {
            "generation": target, "applied_at": 0.0, "updated_at": 0.0}
        self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        record = store.security_records()[-1]
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["applied_generation"], target)

    def test_invalid_security_generation_fails_closed(self):
        for value in (-1, True, "2", 2.0, {}, {"generation": -1},
                      {"generation": True}):
            with self.subTest(value=value):
                td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                self.addCleanup(td.cleanup)
                broker = ScriptedBroker()
                broker.security_generation = value
                mgr = ShareManager(store_path=Path(td.name) / "share.db",
                                   broker=broker)
                # A malformed ACK watermark never counts as a confirmation.
                created = mgr.create_key("friendA")
                self.assertFalse(created["published"])
                self.assertFalse(created["in_sync"])
                failure = mgr.status()["last_publication_failure"]
                self.assertIn("not a non-negative integer", failure["error"])
                with self.assertRaises(BrokerError):
                    mgr.revoke_and_terminate_key(created["key_id"])
                store = ShareStore(Path(td.name) / "share.db")
                self.addCleanup(store.close)
                record = store.security_records()[-1]
                self.assertEqual(record["status"], "pending")
                self.assertEqual(store.generation()["applied_generation"], 0)
                self.assertEqual(broker.emergency_stops, 1)

    def test_zero_security_generation_is_not_missing(self):
        created = self._create()
        self.broker.security_generation = 0
        # No pending intent: zero is a real watermark (nothing to confirm).
        second = self.mgr.create_key("friendB")
        self.assertTrue(second["published"])
        # A pending revoke is NOT covered by zero.
        target = second["desired_generation"] + 1
        with self.assertRaises(BrokerError):
            self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        record = store.security_records()[-1]
        self.assertEqual(record["generation"], target)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(store.security_state()["generation_floor"], target)

    # --------------------------------------------------------- idempotency
    def test_repeat_publish_same_generation_is_idempotent(self):
        created = self._create()
        self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        records_before = store.security_records()
        applied_before = store.generation()["applied_generation"]
        # A second publication at the SAME generation confirms nothing new.
        current = store.get_share_max_concurrency()
        self.mgr.set_share_limits(current)
        store = self._store()
        records_after = store.security_records()
        self.assertEqual(len(records_after), len(records_before))
        self.assertEqual([(r["record_id"], r["status"]) for r in records_after],
                         [(r["record_id"], r["status"])
                          for r in records_before])
        self.assertEqual(store.generation()["applied_generation"],
                         applied_before)
        self.assertEqual(self.broker.emergency_stops, 0)

    # ------------------------------------------------------- journal floor
    def test_journal_generation_floor_tracks_newest_intent(self):
        created = self._create()
        first_target = created["desired_generation"] + 1
        self.mgr.revoke_and_terminate_key(created["key_id"])
        store = self._store()
        self.assertEqual(store.security_state()["generation_floor"],
                         first_target)
        # A newer revoke raises the floor and is NOT covered by the previous
        # watermark: the failed publication leaves it pending.
        second = self._create("friendB")
        second_target = second["desired_generation"] + 1
        self.broker.security_generation = first_target
        with self.assertRaises(BrokerError):
            self.mgr.delete_key(second["key_id"])
        store = self._store()
        state = store.security_state()
        self.assertEqual(state["generation_floor"], second_target)
        self.assertEqual(state["records"][-1]["generation"], second_target)
        self.assertEqual(state["records"][-1]["status"], "pending")
        # A later ACK at the journal floor applies the pending intent WITHOUT
        # lowering the floor.
        self.broker.security_generation = "auto"
        self.mgr.delete_key(second["key_id"])   # already deleted -> publish
        store = self._store()
        state = store.security_state()
        self.assertEqual(state["generation_floor"], second_target)
        self.assertEqual(state["records"][-1]["status"], "applied")
        self.assertEqual(state["records"][-1]["applied_generation"],
                         second_target)


class BrokerClientAdapterSecurityGenerationTests(unittest.TestCase):
    """The adapter must carry the ACK watermark verbatim (missing stays missing)."""

    def _projection(self):
        return {"config_generation": 5, "entries": []}

    def test_adapter_carries_runtime_security_generation(self):
        projection = self._projection()
        client = _BrokerClient({
            "generation": 5,
            "fingerprint": _fingerprint(projection),
            "instance_id": "instance-x",
            "security_generation": 4})
        out = BrokerClientAdapter(client).publish(projection)
        self.assertEqual(out["applied_generation"], 5)
        self.assertEqual(out["instance_id"], "instance-x")
        self.assertEqual(out["security_generation"], 4)

    def test_adapter_keeps_missing_security_generation_missing(self):
        projection = self._projection()
        client = _BrokerClient({
            "generation": 5,
            "fingerprint": _fingerprint(projection),
            "instance_id": "instance-x"})
        out = BrokerClientAdapter(client).publish(projection)
        self.assertNotIn("security_generation", out)

    def test_adapter_path_binds_journal_through_management(self):
        class _AdapterClient(_BrokerClient):
            def __init__(self):
                self.security_generation = None

            def publish(self, projection, terminate_keys=None):
                ack = {
                    "generation": projection["config_generation"],
                    "fingerprint": _fingerprint(projection),
                    "instance_id": "runtime-adapter",
                    "security_generation": (
                        projection["config_generation"]
                        if self.security_generation is None
                        else self.security_generation),
                }
                return BrokerResult.ok(ack)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            client = _AdapterClient()
            mgr = ShareManager(
                store_path=Path(td) / "share.db",
                broker=BrokerClientAdapter(client))
            created = mgr.create_key("friendA")
            self.assertTrue(created["published"])
            target = created["desired_generation"] + 1
            mgr.revoke_and_terminate_key(created["key_id"])
            store = ShareStore(Path(td) / "share.db")
            try:
                record = store.security_records()[-1]
                self.assertEqual(record["status"], "applied")
                self.assertEqual(record["instance_id"], "runtime-adapter")
                self.assertEqual(record["applied_generation"], target)
            finally:
                store.close()


class StoreCommitAppliedTests(unittest.TestCase):
    """The store's ACK watermark: five cases + journal floor consistency."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.store = ShareStore(Path(self.td.name) / "share.db")
        self.addCleanup(self.store.close)

    def _pending_revoke(self):
        created = self.store.create_key("friendA")
        current = self.store.generation()["config_generation"]
        self.store.commit_applied(current, security_generation=current)
        self.store.delete_key(created["key_id"])
        target = self.store.render_projection()["config_generation"]
        return created, target

    def test_missing_watermark_advances_config_only(self):
        created, target = self._pending_revoke()
        result = self.store.commit_applied(target)
        self.assertEqual(result["applied_generation"], target)
        self.assertEqual(result["security_applied"], [])
        self.assertEqual(result["security_pending"], 1)
        self.assertFalse(result["security_in_sync"])
        self.assertFalse(result["in_sync"])   # NOT fully applied
        generation = self.store.generation()
        self.assertEqual(generation["applied_generation"], target)
        self.assertFalse(generation["in_sync"])   # NOT fully applied
        self.assertEqual(generation["security_pending"], 1)
        self.assertFalse(generation["security_in_sync"])
        self.assertEqual(self.store.security_records()[-1]["status"],
                         "pending")

    def test_zero_watermark_advances_config_only(self):
        created, target = self._pending_revoke()
        result = self.store.commit_applied(target, security_generation=0)
        self.assertEqual(result["applied_generation"], target)
        self.assertEqual(result["security_applied"], [])
        self.assertEqual(result["security_pending"], 1)
        self.assertFalse(result["security_in_sync"])
        self.assertFalse(result["in_sync"])
        self.assertEqual(self.store.security_records()[-1]["status"],
                         "pending")

    def test_covering_watermark_marks_the_intent_applied(self):
        created, target = self._pending_revoke()
        record_id = self.store.security_records()[-1]["record_id"]
        result = self.store.commit_applied(
            target, security_generation=target)
        self.assertEqual(result["security_applied"], [record_id])
        self.assertEqual(result["security_pending"], 0)
        self.assertTrue(result["security_in_sync"])
        self.assertTrue(result["in_sync"])
        record = self.store.security_records()[-1]
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["applied_generation"], target)

    def test_watermark_below_pending_row_marks_nothing(self):
        created, target = self._pending_revoke()
        result = self.store.commit_applied(
            target, security_generation=target - 1)
        self.assertEqual(result["security_applied"], [])
        self.assertEqual(result["security_pending"], 1)
        self.assertEqual(self.store.security_records()[-1]["status"],
                         "pending")

    def test_watermark_above_generation_marks_only_up_to_generation(self):
        created, target = self._pending_revoke()
        result = self.store.commit_applied(
            target, security_generation=target + 5)
        self.assertEqual(result["applied_generation"], target)
        self.assertEqual(result["security_pending"], 0)
        self.assertEqual(self.store.security_records()[-1]["status"],
                         "applied")
        self.assertEqual(self.store.security_records()[-1]["applied_generation"],
                         target)

    def test_invalid_watermark_is_rejected(self):
        created, target = self._pending_revoke()
        for value in (-1, True, "2", 2.0):
            with self.subTest(value=value):
                with self.assertRaises(StoreError):
                    self.store.commit_applied(
                        target, security_generation=value)
                self.assertEqual(self.store.security_records()[-1]["status"],
                                 "pending")

    def test_journal_floor_is_never_lowered(self):
        created, target = self._pending_revoke()
        state = self.store.security_state()
        self.assertEqual(state["generation_floor"], target)
        self.store.commit_applied(target, security_generation=target - 1)
        self.assertEqual(self.store.security_state()["generation_floor"],
                         target)
        self.assertEqual(self.store.generation()["security_pending"], 1)
        self.store.commit_applied(target, security_generation=target)
        self.assertEqual(self.store.security_state()["generation_floor"],
                         target)
        self.assertEqual(self.store.generation()["security_pending"], 0)


if __name__ == "__main__":
    unittest.main()
