"""D18-3 revoke cancellation at the 8100 upstream boundary (W1).

A revoke closes the live client socket AND the upstream connection this
request is blocked on, so a handler blocked in ``upstream.getresponse()`` (or a
streaming read) unwinds immediately and its ``finally`` releases the 8100
in-flight slot. A retryable dispatch request that has not started a response
keeps its upstream open: the backend's released count must still answer the
documented retryable 429 instead of a fabricated 502.
"""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))

import share_entry as share  # noqa: E402

KEY_ID = "ka"
SECRET = "test-secret"
INSTANCE_SECRET = "inst-secret"


def _entry(max_concurrency=4):
    return {"key_id": KEY_ID, "name": "friend",
            "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
            "secret_version": 1, "enabled": True,
            "max_concurrency": max_concurrency}


def _write_projection(path, generation=1):
    payload = {"schema_version": 2, "config_generation": generation,
               "share_max_concurrency": 4, "data": [_entry()]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _signed_load(server, payload, extra_fields=None, request_id=None):
    from share.broker_protocol import INSTANCE_SIGNED_KEYS, sign
    fields = {"generation": payload["config_generation"],
              "fingerprint": hashlib.sha256(json.dumps(
                  payload, sort_keys=True).encode()).hexdigest()}
    if extra_fields:
        fields.update(extra_fields)
    body = sign(INSTANCE_SECRET, {
        "v": 2, "action": "load",
        "request_id": (request_id
                       or "req-" + str(threading.get_ident())),
        "ts": int(__import__("time").time()) + 1,
        "instance_id": server.instance_id,
        "fields": fields}, INSTANCE_SIGNED_KEYS)
    return server.handle_instance_control(body, INSTANCE_SECRET)


class _DummyConn:
    def shutdown(self, _how):
        pass

    def close(self):
        pass


class _Upstream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class InflightContextTests(unittest.TestCase):
    def test_attach_after_revoke_closes_upstream_immediately(self):
        ctx = share._InflightCtx(_DummyConn(), retryable_refusal=False)
        self.assertTrue(ctx.revoke())
        self.assertTrue(ctx.terminated)
        upstream = _Upstream()
        self.assertFalse(ctx.attach_upstream(upstream))
        self.assertTrue(upstream.closed)
        self.assertTrue(ctx.upstream_closed)

    def test_close_upstream_is_idempotent(self):
        ctx = share._InflightCtx(_DummyConn(), retryable_refusal=False)
        upstream = _Upstream()
        self.assertTrue(ctx.attach_upstream(upstream))
        self.assertTrue(ctx.close_upstream())
        self.assertFalse(ctx.close_upstream())
        self.assertTrue(upstream.closed)

    def test_retryable_revoke_keeps_upstream_for_the_429(self):
        ctx = share._InflightCtx(_DummyConn(), retryable_refusal=True)
        upstream = _Upstream()
        self.assertTrue(ctx.attach_upstream(upstream))
        self.assertTrue(ctx.revoke())
        self.assertFalse(ctx.upstream_closed)
        self.assertFalse(upstream.closed)
        self.assertTrue(ctx.close_upstream())
        self.assertTrue(upstream.closed)


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "projection.json"
        self.addCleanup(self._close)

    def _close(self):
        upstream = getattr(self, "upstream", None)
        server = getattr(self, "server", None)
        release = getattr(self, "release", None)
        if release is not None:
            release.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if upstream is not None:
            upstream.shutdown()
            upstream.server_close()
        for thread in getattr(self, "threads", ()):
            thread.join(5)

    def _start(self, handler_cls):
        self.release = threading.Event()
        self.arrived = threading.Event()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.upstream.daemon_threads = True
        self.upstream.arrived = self.arrived
        self.upstream.release = self.release
        self.threads = []
        _write_projection(self.config)
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config, self.upstream.server_port)
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever,
                                       daemon=True)
            thread.start()
            self.threads.append(thread)
        self.payload = _write_projection(self.config)

    def _client(self, stream=False):
        import http.client
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=30)
        if stream:
            method, path = "POST", "/v1/chat/completions"
            body = json.dumps({"model": "m", "stream": True,
                                "messages": [{"role": "user",
                                              "content": "hi"}]})
        else:
            # GET /v1/models is a non-dispatch relay: a revoke closes its
            # upstream and client socket (the dispatch 429 rule does not apply).
            method, path, body = "GET", "/v1/models", None
        headers = {"Authorization": "Bearer " + SECRET}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _wait_active_empty(self, timeout=10.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.active == {}:
                return True
            time.sleep(0.02)
        return False


class _BlockingNonStream(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.arrived.set()
        self.server.release.wait(30)
        body = json.dumps({"object": "list", "data": []}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Chat2Api-Raw-Models", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class _BlockingStream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices": [{"delta": {"content": "a"}}]}\n\n')
            self.wfile.flush()
        except OSError:
            return
        self.server.arrived.set()
        self.server.release.wait(30)

    def log_message(self, *_args):
        pass


class _DelayedDispatchRefusal(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.server.arrived.set()
        self.server.release.wait(30)
        body = json.dumps({"error": {
            "message": "share_dispatch_revoked",
            "type": "share_dispatch_revoked",
            "code": "share_dispatch_revoked"}}).encode()
        try:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class RevokeUpstreamTests(_ServerCase):
    def test_revoke_closes_blocked_upstream_and_releases_slot(self):
        self._start(_BlockingNonStream)
        client = {}

        def run_client():
            response = None
            try:
                client["conn"], response = self._client()
            except Exception as exc:  # noqa: BLE001 - the client sees close
                client["error"] = exc
            finally:
                if response is not None:
                    response.close()

        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()
        self.assertTrue(self.arrived.wait(10))
        self.assertEqual(self.server.active, {KEY_ID: 1})
        response = _signed_load(
            self.server, self.payload, {"terminate_keys": [KEY_ID]})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["terminated"], 1)
        self.assertEqual(response["result"]["upstream_closed"], 1)
        # The revoke closed the upstream; the fake backend then finishes its
        # blocked handler (a real 8110 also notices the closed client), so the
        # 8100 handler unwinds and releases the slot in its own finally.
        self.release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self._wait_active_empty())
        self.assertNotIn(KEY_ID, self.server.inflight)
        conn = client.get("conn")
        if conn is not None:
            conn.close()

    def test_retryable_dispatch_429_is_still_relayed(self):
        self._start(_DelayedDispatchRefusal)
        client = {}

        def run_client():
            response = None
            try:
                client["conn"], response = self._client(stream=True)
                client["status"] = response.status
                client["body"] = json.loads(response.read().decode())
            except Exception as exc:  # noqa: BLE001 - failure evidence
                client["error"] = exc
            finally:
                if response is not None:
                    response.close()

        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()
        self.assertTrue(self.arrived.wait(10))
        response = _signed_load(
            self.server, self.payload, {"terminate_keys": [KEY_ID]})
        self.assertTrue(response["ok"], response)
        # C1: `terminated` counts the live client sockets 8100 actually
        # closed. A retryable dispatch request that has not started keeps
        # its client socket open so the documented 429 can still be relayed,
        # so it is NOT a terminated live connection.
        self.assertEqual(response["result"]["terminated"], 0)
        # A retryable dispatch request that has not started keeps its
        # upstream open, so the backend's released count can still answer
        # the documented 429 instead of a fabricated 502.
        self.assertEqual(response["result"]["upstream_closed"], 0)
        self.release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(client.get("status"), 429)
        error = client["body"]["error"]
        self.assertEqual(error["type"], "share_dispatch_revoked")
        self.assertTrue(self._wait_active_empty())
        conn = client.get("conn")
        if conn is not None:
            conn.close()

    def test_revoke_closes_streaming_upstream(self):
        self._start(_BlockingStream)
        client = {}
        first_chunk = threading.Event()

        def run_client():
            response = None
            try:
                client["conn"], response = self._client(stream=True)
                client["first"] = response.read1(4096)
                first_chunk.set()
            except Exception as exc:  # noqa: BLE001 - failure evidence
                client["error"] = exc
            finally:
                if response is not None:
                    response.close()

        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()
        self.assertTrue(first_chunk.wait(10))
        self.assertTrue(client.get("first"))
        response = _signed_load(
            self.server, self.payload, {"terminate_keys": [KEY_ID]})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["terminated"], 1)
        self.assertEqual(response["result"]["upstream_closed"], 1)
        self.release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self._wait_active_empty())
        conn = client.get("conn")
        if conn is not None:
            conn.close()


class AckCountTests(unittest.TestCase):
    def test_ack_counts_terminated_and_upstream_closed(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        config = Path(td.name) / "projection.json"
        _write_projection(config, generation=1)
        server = share.ShareServer(("127.0.0.1", 0), config)
        self.addCleanup(server.server_close)
        plain = share._InflightCtx(_DummyConn(), retryable_refusal=False)
        dispatch = share._InflightCtx(_DummyConn(), retryable_refusal=True)
        plain_upstream = _Upstream()
        dispatch_upstream = _Upstream()
        self.assertTrue(plain.attach_upstream(plain_upstream))
        self.assertTrue(dispatch.attach_upstream(dispatch_upstream))
        server.register_inflight(KEY_ID, plain)
        server.register_inflight(KEY_ID, dispatch)
        next_payload = _write_projection(config, generation=2)
        response = _signed_load(
            server, next_payload, {"terminate_keys": [KEY_ID]})
        self.assertTrue(response["ok"], response)
        # C1: only the non-retryable context closes its client socket; the
        # retryable dispatch context keeps it open for the documented 429.
        self.assertEqual(response["result"]["terminated"], 1)
        self.assertEqual(response["result"]["upstream_closed"], 1)
        self.assertTrue(plain_upstream.closed)
        self.assertFalse(dispatch_upstream.closed)

    def test_ack_carries_the_backend_released_and_aborted_counts(self):
        # D18-3: the load ACK must carry what 8110 released/aborted, not
        # only the 8100 terminated/upstream counts. The notice fixture
        # answers distinct values so a dropped field is visible.
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        config = Path(td.name) / "projection.json"
        _write_projection(config, generation=1)

        class _RevokeNotice(BaseHTTPRequestHandler):
            requests = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                type(self).requests.append((self.path, body))
                response = json.dumps(
                    {"ok": True, "released": 3, "aborted": 4}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RevokeNotice)
        self.addCleanup(upstream.server_close)
        thread = threading.Thread(target=upstream.serve_forever,
                                   daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        server = share.ShareServer(
            ("127.0.0.1", 0), config, upstream.server_port)
        self.addCleanup(server.server_close)
        server.attribution_secret = "s" * 48
        # Load the enabled generation first: the transition to a disabled Key
        # is what produces the revoke notice.
        first = _signed_load(
            server, _write_projection(config, generation=1),
            request_id="req-first")
        self.assertTrue(first["ok"], first)
        # The transition must DISABLE the key so the notice is sent.
        entry = _entry()
        entry["enabled"] = False
        next_payload = {
            "schema_version": 2, "config_generation": 2,
            "share_max_concurrency": 4, "data": [entry]}
        config.write_text(json.dumps(next_payload), encoding="utf-8")
        response = _signed_load(
            server, next_payload, {"terminate_keys": [KEY_ID]},
            request_id="req-revoke")
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["terminated"], 0)
        self.assertEqual(response["result"]["upstream_closed"], 0)
        self.assertEqual(response["result"]["released"], 3)
        self.assertEqual(response["result"]["aborted"], 4)
        self.assertTrue(_RevokeNotice.requests)
        self.assertEqual(_RevokeNotice.requests[0][0],
                         share.REVOKE_NOTICE_PATH)

    def test_ack_omits_unknown_backend_counts(self):
        # A failed notice has no released/aborted answer: the ACK omits both
        # fields instead of fabricating 0. The 8100 counts still travel.
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        config = Path(td.name) / "projection.json"
        _write_projection(config, generation=1)

        class _FailingNotice(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FailingNotice)
        self.addCleanup(upstream.server_close)
        thread = threading.Thread(target=upstream.serve_forever,
                                   daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        server = share.ShareServer(
            ("127.0.0.1", 0), config, upstream.server_port)
        self.addCleanup(server.server_close)
        server.attribution_secret = "s" * 48
        first = _signed_load(
            server, _write_projection(config, generation=1),
            request_id="req-first")
        self.assertTrue(first["ok"], first)
        entry = _entry()
        entry["enabled"] = False
        next_payload = {
            "schema_version": 2, "config_generation": 2,
            "share_max_concurrency": 4, "data": [entry]}
        config.write_text(json.dumps(next_payload), encoding="utf-8")
        response = _signed_load(
            server, next_payload, {"terminate_keys": [KEY_ID]},
            request_id="req-revoke")
        self.assertTrue(response["ok"], response)
        self.assertNotIn("released", response["result"])
        self.assertNotIn("aborted", response["result"])


if __name__ == "__main__":
    unittest.main()
