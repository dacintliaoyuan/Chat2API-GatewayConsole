"""Private console ingress must authenticate Serve metadata before local routing."""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import console_tailnet


DOCUMENT = {
    "schema_version": 1, "enabled": True,
    "origin": "https://pc.example.ts.net:8001",
    "allowed_login": "owner@example.com",
    "allowed_peer_ips": ["100.119.187.73", "100.127.119.98",
                         "fd7a:115c:a1e0::1234"],
}


def headers(**replace):
    values = {
        "Host": "pc.example.ts.net:8001",
        "X-Forwarded-Host": "pc.example.ts.net:8001",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "100.127.119.98",
        "Tailscale-User-Login": "owner@example.com",
    }
    values.update(replace)
    result = Message()
    for name, value in values.items():
        if value is not None:
            result[name] = value
    return result


class TailnetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = console_tailnet.TailnetPolicy.from_document(DOCUMENT)

    def test_selected_phone_and_pc_are_allowed(self):
        for ip in DOCUMENT["allowed_peer_ips"]:
            with self.subTest(ip=ip):
                self.assertTrue(self.policy.allows(
                    headers(**{"X-Forwarded-For": ip}), "127.0.0.1"))

    def test_same_owner_cloud_node_is_not_authorized(self):
        self.assertFalse(self.policy.allows(
            headers(**{"X-Forwarded-For": "100.71.250.7"}), "127.0.0.1"))

    def test_peer_and_owner_are_both_required(self):
        for changes in (
            {"Tailscale-User-Login": "someone@example.com"},
            {"Tailscale-User-Login": None},
            {"X-Forwarded-For": None},
            {"X-Forwarded-For": "127.0.0.1"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.policy.allows(headers(**changes), "127.0.0.1"))
        self.assertFalse(self.policy.allows(headers(), "10.0.0.2"))

    def test_fake_localhost_and_foreign_origin_are_refused(self):
        for changes in (
            {"Host": "localhost:8001"},
            {"Host": "pc.example.ts.net.evil.test:8001"},
            {"X-Forwarded-Host": "localhost:8001"},
            {"X-Forwarded-Proto": "http"},
            {"Origin": "https://evil.test"},
            {"Origin": "http://127.0.0.1:8001"},
            {"Origin": "null"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.policy.allows(headers(**changes), "127.0.0.1"))
        self.assertTrue(self.policy.allows(
            headers(Origin=DOCUMENT["origin"]), "127.0.0.1"))

    def test_ambiguous_headers_and_funnel_are_refused(self):
        for name in ("Host", "Origin", "X-Forwarded-Host", "X-Forwarded-Proto",
                     "X-Forwarded-For", "Tailscale-User-Login"):
            h = headers(Origin=DOCUMENT["origin"])
            h[name] = h[name]
            with self.subTest(duplicate=name):
                self.assertFalse(self.policy.allows(h, "127.0.0.1"))
        for name in ("Host", "X-Forwarded-Host", "X-Forwarded-Proto",
                     "X-Forwarded-For", "Tailscale-User-Login"):
            h = headers()
            h.replace_header(name, h[name] + ", forged")
            with self.subTest(list_value=name):
                self.assertFalse(self.policy.allows(h, "127.0.0.1"))
        self.assertFalse(self.policy.allows(
            headers(**{"Tailscale-Funnel-Request": "?1"}), "127.0.0.1"))

    def test_proxy_metadata_never_falls_back_to_local(self):
        for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Proto",
                     "Tailscale-User-Login", "Tailscale-Funnel-Request"):
            h = Message(); h["Host"] = "localhost:8001"; h[name] = ""
            with self.subTest(name=name):
                self.assertTrue(console_tailnet.has_proxy_metadata(h))

    def test_bad_configuration_fails_closed(self):
        for change in (
            {"origin": "http://pc.example.ts.net:8001"},
            {"origin": "https://pc.example.ts.net:8001/path"},
            {"origin": "https://user@pc.example.ts.net:8001"},
            {"origin": "https://other.example:8001"},
            {"allowed_peer_ips": []}, {"allowed_peer_ips": ["0.0.0.0/0"]},
            {"allowed_login": ""}, {"schema_version": True},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    console_tailnet.TailnetPolicy.from_document(dict(DOCUMENT, **change))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.json"
            self.assertFalse(console_tailnet.load_policy(path).enabled)
            path.write_text("broken JSON", encoding="utf-8")
            self.assertFalse(console_tailnet.load_policy(path).enabled)
            path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
            self.assertTrue(console_tailnet.load_policy(path).enabled)


class TailnetHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("tailnet_test_usagesrv", ROOT / "usagesrv.py")
        cls.service = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.service
        spec.loader.exec_module(cls.service)
        cls.policy_patch = mock.patch.object(
            cls.service, "TAILNET_POLICY", console_tailnet.TailnetPolicy.from_document(DOCUMENT))
        cls.policy_patch.start()
        cls.server = cls.service.ThreadingHTTPServer(("127.0.0.1", 0), cls.service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3)
        cls.policy_patch.stop()

    def request(self, method="GET", h=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.putrequest(method, "/health" if method == "GET" else "/not-an-operation",
                            skip_host=h is not None)
            for name, value in h.items() if h is not None else ():
                conn.putheader(name, value)
            conn.putheader("Connection", "close")
            if method != "GET":
                conn.putheader("Content-Length", "0")
            conn.endheaders()
            response = conn.getresponse(); response.read()
            return response.status
        finally:
            conn.close()

    def test_trusted_serve_read_and_existing_local_read(self):
        self.assertEqual(self.request(h=headers()), 200)
        self.assertEqual(self.request(), 200)

    def test_localhost_spoof_and_cloud_peer_are_refused(self):
        self.assertEqual(self.request(h=headers(Host="localhost:8001")), 403)
        self.assertEqual(self.request(h=headers(**{"X-Forwarded-For": "100.71.250.7"})), 403)

    def test_remote_writes_still_need_csrf_marker(self):
        for method in ("POST", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, headers(Origin=DOCUMENT["origin"])), 403)
        self.assertEqual(self.request("POST", headers(
            Origin=DOCUMENT["origin"], **{"X-Requested-With": "XMLHttpRequest"})), 404)
        self.assertEqual(self.request("POST", headers(
            Origin="https://evil.test", **{"X-Requested-With": "XMLHttpRequest"})), 403)

    def test_duplicate_host_is_rejected_by_real_http_handler(self):
        h = headers(); h["Host"] = "localhost:8001"
        self.assertEqual(self.request(h=h), 403)


if __name__ == "__main__":
    unittest.main()
