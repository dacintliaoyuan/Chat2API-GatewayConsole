"""Catalog contracts that must not require billed model requests."""
import contextlib
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import model_actions as console_model_actions  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402
from console.operations import OperationError  # noqa: E402


class _Ctx:
    def raise_if_cancelled(self):
        pass


class CatalogCapabilityTests(unittest.TestCase):
    def test_retired_catalog_helpers_are_gone(self):
        # B0: the Qoder CN / Qoder Global catalog shaping helpers are no
        # longer part of the module; the retired sources never sync.
        for name in ("qoder_cn_catalog_entries",
                     "qoder_global_catalog_entries"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(model_sync, name))

    def test_retired_sources_cannot_be_selected(self):
        with mock.patch.object(model_sync, "_get_json") as get:
            for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
                with self.subTest(source=source):
                    with self.assertRaisesRegex(ValueError, "退役来源"):
                        model_sync.collect_catalogs([source])
        get.assert_not_called()

    def test_retired_bindings_are_not_assembled(self):
        self.assertFalse(hasattr(model_sync, "ACTIVE_BINDINGS"))
        self.assertFalse(hasattr(model_sync, "DEFAULT_BINDINGS"))


class _CatalogHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({
            "path": self.path, "headers": dict(self.headers)})
        self.send_response(self.server.status)
        for key, value in self.server.response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):
        pass


class _FakeCatalogServer:
    """Terminal fake UM: real HTTP over loopback, no real endpoint."""

    def __init__(self, status=200, body=b'{"data": []}', headers=None):
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _CatalogHandler)
        self.server.status = status
        self.server.body = body
        self.server.response_headers = dict(headers or {})
        self.server.requests = []
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def requests(self):
        return self.server.requests


@contextlib.contextmanager
def _um_endpoint(server):
    """Point the fixed UM adapter at the terminal fake server.

    The production boundary stays fixed; only the module constants (and the
    allowlist entry for the loopback test URL) are redirected in the test.
    """
    url = "http://127.0.0.1:%d/v1/models" % server.port
    with mock.patch.object(model_sync, "UM_MODELS_URL", url), \
         mock.patch.object(model_sync, "UM_MODELS_HOST", "127.0.0.1"), \
         mock.patch.object(model_sync, "UM_MODELS_PORT", server.port), \
         mock.patch.object(model_sync, "UM_MODELS_PATH", "/v1/models"), \
         mock.patch.object(
             model_sync, "_CATALOG_ENDPOINTS",
             frozenset(set(model_sync._CATALOG_ENDPOINTS) | {url})):
        yield


def _json_body(payload):
    return json.dumps(payload).encode("utf-8")


FRESH_MARKER = {"X-Chat2Api-Raw-Models": "1"}


class FreshUmCatalogTests(unittest.TestCase):
    def setUp(self):
        model_sync.reset_fresh_um_catalog_cache()

    def test_strict_fresh_sends_raw_fresh_header_and_reports_fresh(self):
        body = _json_body({"data": [{"id": "m1", "max_model_len": 128000}]})
        with _FakeCatalogServer(body=body, headers=FRESH_MARKER) as server, \
             _um_endpoint(server):
            report = model_sync.collect_fresh_um_catalog()
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/models")
            self.assertEqual(
                request["headers"].get("X-Chat2Api-Models"), "raw-fresh")
            self.assertEqual(report["source"], "um")
            self.assertTrue(report["requested"])
            self.assertTrue(report["success"])
            self.assertTrue(report["fresh"])
            self.assertEqual(report["status"], "fresh")
            self.assertIsNotNone(report["collected_at"])
            self.assertEqual(report["model_count"], 1)
            self.assertEqual(report["models"][0].model_id, "m1")
            self.assertEqual(report["models"][0].context, 128000)

    def test_console_refresh_returns_explicit_refresh_metadata(self):
        body = _json_body({"data": [{
            "id": "DeepSeek-V4-Flash-0731", "max_model_len": 128000}]})
        with _FakeCatalogServer(body=body, headers=FRESH_MARKER) as server, \
             _um_endpoint(server):
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
        self.assertEqual(len(server.requests), 1)
        self.assertTrue(result["success"])
        self.assertTrue(result["fresh"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["source"], "um")
        self.assertEqual(result["sources"], ["um"])
        self.assertIsNotNone(result["collected_at"])
        refresh = result["refresh"]
        self.assertTrue(refresh["success"])
        self.assertTrue(refresh["fresh"])
        self.assertEqual(refresh["status"], "fresh")
        self.assertEqual(refresh["source"], "um")
        self.assertEqual(refresh["model_count"], 1)
        self.assertIsNotNone(refresh["collected_at"])
        row = result["catalogs"]["um"][0]
        self.assertEqual(row["id"], "DeepSeek-V4-Flash-0731")
        # The compatibility output cap is not the UM declaration; the catalog
        # declaration and local known effort tiers keep their source labels.
        self.assertEqual(row["context_state"], "declared")
        self.assertEqual(row["efforts_source"], "known")
        self.assertEqual(row["output_token_cap"],
                         {"value": 16384, "source": "compat_default"})

    def test_default_call_keeps_old_behavior_without_raw_fresh_header(self):
        body = _json_body({"data": [{"id": "m1"}]})
        with _FakeCatalogServer(body=body) as server, _um_endpoint(server):
            catalogs, warnings = model_sync.collect_catalogs(sources=["um"])
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn(
                "X-Chat2Api-Models", server.requests[0]["headers"])
        self.assertEqual(warnings, [])
        self.assertEqual([m.model_id for m in catalogs["um"]], ["m1"])

    def test_200_without_raw_fresh_marker_is_not_a_refresh_success(self):
        # The gateway only adds the marker on a fresh upstream answer. A 200
        # without it is a stale-cache answer: success must stay False and the
        # data must never be presented as a successful refresh.
        body = _json_body({"data": [{"id": "cached"}]})
        with _FakeCatalogServer(body=body) as server, _um_endpoint(server):
            report = model_sync.collect_fresh_um_catalog()
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
        self.assertFalse(report["success"])
        self.assertFalse(report["fresh"])
        self.assertFalse(report["marker"])
        self.assertEqual(report["status"], "not_fresh")
        self.assertIn("raw-fresh", report["error"])
        self.assertFalse(result["success"])
        self.assertFalse(result["fresh"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["refresh"]["status"], "not_fresh")
        self.assertEqual(result["catalogs"], {})

    def test_legal_empty_catalog_is_a_fresh_success(self):
        with _FakeCatalogServer(
                body=_json_body({"data": []}), headers=FRESH_MARKER) as server, \
             _um_endpoint(server):
            report = model_sync.collect_fresh_um_catalog()
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
        self.assertTrue(report["success"])
        self.assertTrue(report["fresh"])
        self.assertEqual(report["status"], "empty")
        self.assertEqual(report["model_count"], 0)
        self.assertIsNotNone(report["collected_at"])
        self.assertTrue(result["success"])
        self.assertTrue(result["fresh"])
        self.assertEqual(result["catalogs"], {"um": []})
        self.assertEqual(result["refresh"]["status"], "empty")

    def test_auth_page_error_page_and_unavailable_are_distinct(self):
        cases = [
            (200, b"<html><body>Please sign in</body></html>", FRESH_MARKER,
             "auth_required"),
            (200, b"<html><body>upstream error</body></html>", FRESH_MARKER,
             "error_page"),
            (200, _json_body({"error": "upstream down"}), FRESH_MARKER,
             "error_page"),
            (401, _json_body({"error": "unauthorized"}), {},
             "auth_required"),
            (502, _json_body({"error": "bad gateway"}), {},
             "unavailable"),
        ]
        for status, body, headers, expected in cases:
            with self.subTest(status=status, expected=expected):
                model_sync.reset_fresh_um_catalog_cache()
                with _FakeCatalogServer(
                        status=status, body=body, headers=headers) as server, \
                     _um_endpoint(server):
                    report = model_sync.collect_fresh_um_catalog()
                self.assertFalse(report["success"])
                self.assertFalse(report["fresh"])
                self.assertEqual(report["status"], expected)
                self.assertIsNone(report["collected_at"])

    def test_failure_keeps_last_success_but_marks_it_stale(self):
        first = _json_body({"data": [{"id": "m1", "max_model_len": 128000}]})
        with _FakeCatalogServer(body=first, headers=FRESH_MARKER) as server, \
             _um_endpoint(server):
            self.assertTrue(model_sync.collect_fresh_um_catalog()["success"])
        with _FakeCatalogServer(
                status=502, body=_json_body({"error": "down"})) as server, \
             _um_endpoint(server):
            report = model_sync.collect_fresh_um_catalog()
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
        self.assertFalse(report["success"])
        self.assertFalse(report["fresh"])
        self.assertEqual(report["status"], "unavailable")
        self.assertTrue(report["stale_fallback"])
        self.assertIsNone(report["collected_at"])
        self.assertEqual(report["model_count"], 1)
        self.assertEqual(report["models"][0].model_id, "m1")
        self.assertEqual(report["last_success"]["model_count"], 1)
        self.assertIsNotNone(report["last_success"]["collected_at"])
        # The console may show the last successful data, but it is explicitly
        # stale and the refresh is explicitly failed — never a fresh success.
        self.assertFalse(result["success"])
        self.assertFalse(result["fresh"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["catalogs"]["um"][0]["id"], "m1")
        self.assertEqual(result["refresh"]["status"], "unavailable")
        self.assertTrue(result["refresh"]["stale_fallback"])

    def test_console_refresh_only_requests_um(self):
        body = _json_body({"data": [{"id": "m1"}]})
        with _FakeCatalogServer(body=body, headers=FRESH_MARKER) as server, \
             _um_endpoint(server):
            with mock.patch.object(
                    model_sync, "_get_json",
                    side_effect=AssertionError("other source requested")):
                result = console_model_actions.model_catalog(
                    "", {"sources": ["um"], "fresh": True}, _Ctx())
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(result["success"])
            for params in ({"fresh": True},
                           {"sources": ["qoder_cn"], "fresh": True},
                           {"sources": ["um", "qoder_cn"], "fresh": True},
                           {"sources": ["um"], "fresh": "yes"}):
                with self.subTest(params=params):
                    with self.assertRaises(OperationError) as caught:
                        console_model_actions.model_catalog(
                            "", params, _Ctx())
                    self.assertEqual("invalid_params", caught.exception.code)
            self.assertEqual(len(server.requests), 1)

    def test_strict_refresh_has_no_inference_probe_or_sync_side_effects(self):
        body = _json_body({"data": [{"id": "m1"}]})
        with _FakeCatalogServer(body=body, headers=FRESH_MARKER) as server, \
             _um_endpoint(server), \
             mock.patch.object(
                 model_sync, "_post_json",
                 side_effect=AssertionError("inference sent")), \
             mock.patch.object(
                 model_sync, "apply_config_transaction",
                 side_effect=AssertionError("config transaction written")):
            self.assertFalse(hasattr(model_sync, "run_sync"))
            self.assertFalse(hasattr(model_sync, "collect_consumers"))
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
        self.assertTrue(result["success"])
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
