# -*- coding: utf-8 -*-
"""C3: the fee HTTP entry (``GET /api/console/share-usage``) is read-only.

Real regression through the release's OWN default assembly: the real
``usagesrv.Handler`` behind a real loopback ``TrackedHTTPServer``, with only the
end data relocated into a temporary directory (price fixture, share ledger, share
store, deployment config). The fixture store carries an ACTIVE key and a DELETED key
(a REVOKING intent): the fee read must return fees and key names without ever
assembling ShareManager, the deployment loader, the broker/kernel boundary or a real
task/control action, and it must neither restore nor stop sharing.

Every refused seam appends to ``MANAGEMENT_ATTEMPTS`` BEFORE raising, so an
exception the code under test swallows still fails the test on the cumulative
record -- the guard cannot be hidden by a broad ``except`` clause.
"""

import contextlib
import http.client as http_client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _entry in (str(ROOT), str(TESTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import _chat2api_http_harness as harness  # noqa: E402
import share_store  # noqa: E402
import usagesrv  # noqa: E402

# Cumulative record of every management/broker/control attempt. A guard appends
# here BEFORE raising, so a swallowed exception is still visible to the test.
MANAGEMENT_ATTEMPTS = []


class FeeReadSafetyTests(unittest.TestCase):
    PRICED_MODEL = "zfpricedfixture"
    LEDGER_NAME = "usage-share-backend.jsonl"
    ROUTE = "/api/console/share-usage"
    ALIAS_ROUTE = "/gw/usagesrv/api/console/share-usage"

    def setUp(self):
        MANAGEMENT_ATTEMPTS.clear()
        self.tmp = tempfile.TemporaryDirectory(prefix="fee-read-safety-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        # 1. Price fixture: the ONLY price source the fee GET may read.
        prices = {
            "_说明": "public share fee-read fixture",
            "checkedAt": "2026-10-07",
            "exchangeRate": 7.2,
            "models": {},
            "providers": {"fixture": {"models": {
                self.PRICED_MODEL: {"input": 1.0, "output": 2.0}}}},
        }
        self.prices_path = os.path.join(self.tmp.name, "prices.json")
        with open(self.prices_path, "w", encoding="utf-8") as handle:
            json.dump(prices, handle, ensure_ascii=False)
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self._stack.enter_context(
            mock.patch.object(usagesrv, "PRICE_FILE", self.prices_path))
        self._stack.enter_context(
            mock.patch.object(usagesrv, "OVERRIDE_FILE",
                              os.path.join(self.tmp.name, "no-override.json")))
        self._stack.enter_context(
            mock.patch.dict(usagesrv._price_cache,
                            {"at": 0.0, "data": None}, clear=True))

        # 2. Share store fixture: ACTIVE key + DELETED key (revoking intent).
        self.share_db = os.path.join(self.tmp.name, "share-store.db")
        store = share_store.ShareStore(self.share_db)
        try:
            self.key_a = store.create_key("甲钥匙")["key_id"]
            self.key_b = store.create_key("乙钥匙")["key_id"]
            store.delete_key(self.key_b)
        finally:
            store.close()
        self._stack.enter_context(
            mock.patch.dict(os.environ, {"SHARE_STORE_FILE": self.share_db}))

        # 3. Share ledger fixture behind the release's own env override.
        ledger_dir = os.path.join(self.tmp.name, "share-backend")
        os.makedirs(ledger_dir)
        self.ledger = os.path.join(ledger_dir, self.LEDGER_NAME)
        base = {
            "service": "chat2api", "stream": False, "status": "ok",
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "duration_ms": 1000, "protocol": "chat", "attribution": "verified",
        }
        with open(self.ledger, "w", encoding="utf-8") as handle:
            for rec in (
                {**base, "time": "2026-10-07T10:00:00+08:00",
                 "model": self.PRICED_MODEL, "input_tokens": 1000000,
                 "output_tokens": 0, "total_tokens": 1000000,
                 "estimated": False, "usage_source": "upstream",
                 "key_id": self.key_a, "request_id": "r1"},
                {**base, "time": "2026-10-07T11:00:00+08:00",
                 "model": self.PRICED_MODEL, "input_tokens": 1000000,
                 "output_tokens": 0, "total_tokens": 1000000,
                 "estimated": True, "usage_source": "estimated",
                 "key_id": self.key_a, "request_id": "r2"},
                {**base, "time": "2026-10-07T12:00:00+08:00",
                 "model": self.PRICED_MODEL, "input_tokens": 500000,
                 "output_tokens": 0, "total_tokens": 500000,
                 "estimated": False, "usage_source": "upstream",
                 "key_id": self.key_b, "request_id": "r3"},
            ):
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._stack.enter_context(
            mock.patch.dict(os.environ,
                            {"CONSOLE_SHARE_BACKEND_DIR": ledger_dir}))

        # 4. Deployment config: the route must never reach the loader; if it did
        #    the temp absent config would keep the assembly offline anyway.
        self._stack.enter_context(mock.patch.object(
            usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE",
            os.path.join(self.tmp.name, "no-share-deployment.json")))
        self._stack.enter_context(
            mock.patch.dict(os.environ,
                            {"SHARE_MANAGEMENT_CONFIG": os.path.join(
                                self.tmp.name, "no-share-deployment.json")}))
        # A call to get_share_manager() would have to ASSEMBLE, never reuse a
        # pre-built manager, so the guard is the only thing that can answer.
        self._stack.enter_context(
            mock.patch.object(usagesrv, "_share_manager", None))

        # 5. Refuse (and cumulatively record) every management seam.
        self._stack.enter_context(self._management_guard())

        self.before = Path(self.share_db).read_bytes()
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), usagesrv.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._join_server_thread, server, thread)
        harness.install_tracked_server(self, server, name="usagesrv-fee-read")
        self.port = server.server_port

    @staticmethod
    def _join_server_thread(server, thread):
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("usagesrv test server thread did not exit")

    def _management_guard(self):
        """Refuse + record ShareManager, loader, broker/kernel, task/control."""
        import console_share_actions
        import share_broker_client
        import share_broker_host
        import share_broker_ops
        import share_broker_server

        def refuse(kind):
            def refuse_impl(*args, **kwargs):
                MANAGEMENT_ATTEMPTS.append(kind)
                raise AssertionError("fee read attempted " + kind)
            return refuse_impl

        stack = contextlib.ExitStack()
        for owner, name, kind in (
                (usagesrv, "get_share_manager", "get_share_manager"),
                (usagesrv, "_load_share_management_deployment",
                 "deployment loader"),
                (share_broker_client.NamedPipeBrokerTransport, "open",
                 "broker/kernel pipe"),
                (share_broker_server, "_declared_kernel",
                 "broker kernel declaration"),
                (console_share_actions, "share_open", "share_open"),
                (console_share_actions, "share_close", "share_close"),
                (console_share_actions, "share_estop", "share_estop"),
                (console_share_actions, "share_retry_stop", "share_retry_stop"),
                (share_broker_ops.WindowsHostOps, "start_host",
                 "task start_host"),
                (share_broker_ops.WindowsHostOps, "stop_host",
                 "task stop_host"),
                (share_broker_host.BrokerHostCoordinator, "_start",
                 "broker host _start")):
            stack.enter_context(
                mock.patch.object(owner, name, side_effect=refuse(kind)))
        return stack

    def _request(self, path=ROUTE):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw
        except (OSError, http_client.HTTPException) as exc:
            # A refused seam can kill the handler thread before a response goes
            # out; report the cumulative record with the transport failure.
            raise AssertionError(
                "fee GET %s failed: %r; management attempts=%r"
                % (path, exc, MANAGEMENT_ATTEMPTS))
        finally:
            conn.close()

    def _payload(self, path=ROUTE):
        status, raw = self._request(path)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    @staticmethod
    def _key(payload, key_id):
        for row in payload["keys"]:
            if row["key_id"] == key_id:
                return row
        raise AssertionError("key %r missing from payload" % (key_id,))

    def _assert_fees_and_names(self, payload):
        keys = {row["key_id"]: row for row in payload["keys"]}
        self.assertEqual(set(keys), {self.key_a, self.key_b})
        self.assertEqual(payload["requests"], 3)
        self.assertEqual(payload["coverage"]["keyNames"], 2)
        # ACTIVE key: name readable, deleted=False, real fee.
        self.assertEqual(keys[self.key_a]["keyName"], "甲钥匙")
        self.assertIs(keys[self.key_a]["deleted"], False)
        self.assertEqual(keys[self.key_a]["requests"], 2)
        self.assertEqual(keys[self.key_a]["knownCostUsd"], 1.0)
        self.assertEqual(keys[self.key_a]["estimatedCostUsd"], 1.0)
        # DELETED key (revoking intent): name still readable, deleted=True,
        # fee retained -- the read neither folds it away nor acts on the intent.
        self.assertEqual(keys[self.key_b]["keyName"], "乙钥匙")
        self.assertIs(keys[self.key_b]["deleted"], True)
        self.assertEqual(keys[self.key_b]["requests"], 1)
        self.assertEqual(keys[self.key_b]["knownCostUsd"], 0.5)
        raw_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret", raw_text)
        self.assertNotIn("um-share-", raw_text)

    def _assert_no_management_attempt(self):
        self.assertEqual(MANAGEMENT_ATTEMPTS, [],
                         "fee read attempted management seams: %r"
                         % (MANAGEMENT_ATTEMPTS,))

    def _assert_store_bytes_unchanged(self):
        self.assertEqual(Path(self.share_db).read_bytes(), self.before)

    def _assert_revoking_intent_still_pending(self):
        """The deleted key's REVOKING intent survived the read untouched."""
        store = share_store.ShareStore(self.share_db)
        try:
            self.assertIsNotNone(store.get_key(self.key_b)["deleted_at"])
            generation = store.generation()["config_generation"]
            intent = store.find_pending_publish_intent(generation)
            self.assertIsNotNone(intent, "the delete publish intent vanished")
            self.assertIs(intent["payload"]["revoking"], True)
            self.assertEqual(intent["payload"]["key_id"], self.key_b)
            self.assertEqual(intent["payload"]["action_kind"], "delete")
        finally:
            store.close()

    def test_fee_get_returns_fees_and_names_without_management(self):
        payload = self._payload()
        self._assert_fees_and_names(payload)
        # The alias path (the frontend's real route) is the same read-only
        # projection: identical payload apart from the read timestamp.
        alias = self._payload(self.ALIAS_ROUTE)
        self.assertEqual({k: v for k, v in alias.items() if k != "checkedAt"},
                         {k: v for k, v in payload.items() if k != "checkedAt"})
        self._assert_no_management_attempt()
        self._assert_store_bytes_unchanged()
        self._assert_revoking_intent_still_pending()

    def test_name_resolution_failure_keeps_fees_unknown_not_zero(self):
        # Expected fees are independent literals, so this test cannot pass by
        # recomputing whatever the read happens to return.
        missing = os.path.join(self.tmp.name, "missing-store.db")
        with mock.patch.dict(os.environ, {"SHARE_STORE_FILE": missing}):
            payload = self._payload()
        # Fees survive the name-resolution failure: same counts and amounts,
        # never folded to zero and never dropped from the payload.
        self.assertEqual(payload["requests"], 3)
        key_a = self._key(payload, self.key_a)
        key_b = self._key(payload, self.key_b)
        self.assertEqual(key_a["requests"], 2)
        self.assertEqual(key_a["knownCostUsd"], 1.0)
        self.assertEqual(key_a["estimatedCostUsd"], 1.0)
        self.assertEqual(key_b["requests"], 1)
        self.assertEqual(key_b["knownCostUsd"], 0.5)
        for row in (key_a, key_b):
            # Unknown stays unknown (None), never an empty name or False.
            self.assertIsNone(row["keyName"])
            self.assertIsNone(row["deleted"])
        self.assertIsNone(payload["coverage"]["keyNames"])
        self._assert_no_management_attempt()
        self._assert_store_bytes_unchanged()

    def test_management_guards_are_live_not_vacuous(self):
        # A guard that never fires would make the assertions above vacuous.
        with self.assertRaises(AssertionError):
            usagesrv.get_share_manager()
        self.assertEqual(MANAGEMENT_ATTEMPTS, ["get_share_manager"])
        MANAGEMENT_ATTEMPTS.clear()


if __name__ == "__main__":
    unittest.main()
