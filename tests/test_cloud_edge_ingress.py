# -*- coding: utf-8 -*-
"""D18-1: the Cloudflare Tunnel ingress is read-only and API-only.

The public edge must route exactly the four shared API paths to the loopback
8003 Rust gateway and answer every other path with 404.  The private console
(8002), the display (8097) and the retired Python ports
(8100/8096/18096/5901) must never appear as an ingress service.
``check-ingress.py`` only reads the file and reports
``{ok, problems, rules}``; it never writes or reloads a tunnel.

Run from ``ai-gateway``:

    python -m unittest tests.test_cloud_edge_ingress
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]      # ai-gateway/
DEPLOY = HERE / "um-cloud-deploy"
CHECKER = DEPLOY / "cloudflared" / "check-ingress.py"
CONFIG = DEPLOY / "config" / "cloudflared.yml.example"
HOST = "dacintapi.de5.net"
API_PATHS = ("/v1/models", "/v1/chat/completions", "/v1/messages",
             "/v1/responses")
FORBIDDEN_PORTS = ("8002", "8097", "8100", "8096", "18096", "5901")


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "um_cloudflared_check_ingress", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root, name, text):
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def _config(rules):
    return "ingress:\n" + "\n".join(rules) + "\n"


def _api_rule(path, host=HOST, service="http://127.0.0.1:8003"):
    return (f"  - hostname: {host}\n"
            f"    path: ^{path}$\n"
            f"    service: {service}\n")


class CloudflaredIngressExampleTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        checker = _load_checker()
        result = checker.check_ingress(CONFIG, expect_host=HOST)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual([], result["problems"])
        self.assertEqual(5, len(result["rules"]))
        services = [rule["service"] for rule in result["rules"]]
        self.assertEqual(
            ["http://127.0.0.1:8003"] * 4 + ["http_status:404"], services)

    def test_example_routes_each_api_path_once(self):
        checker = _load_checker()
        result = checker.check_ingress(CONFIG, expect_host=HOST)
        paths = [rule["path"] for rule in result["rules"][:-1]]
        self.assertEqual(sorted(API_PATHS), sorted(paths))
        self.assertIsNone(result["rules"][-1]["path"])
        self.assertIsNone(result["rules"][-1]["hostname"])

    def test_example_never_names_a_private_port(self):
        text = CONFIG.read_text(encoding="utf-8")
        for port in FORBIDDEN_PORTS:
            self.assertNotIn(port, text,
                             f"the public ingress names private port {port}")

    def test_example_host_matches_the_deployment_domain(self):
        checker = _load_checker()
        result = checker.check_ingress(CONFIG, expect_host=HOST)
        self.assertTrue(result["ok"], result["problems"])
        for rule in result["rules"][:-1]:
            self.assertEqual(HOST, rule["hostname"])

    def test_a_private_console_service_is_refused(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-console-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                _api_rule("/v1/models"),
                "  - hostname: %s\n    path: ^/console$\n"
                "    service: http://127.0.0.1:8096\n" % HOST,
                "  - service: http_status:404\n",
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(any("8096" in item for item in result["problems"]),
                        result["problems"])

    def test_a_display_service_is_refused(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-display-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                "  - hostname: %s\n    path: ^/display/$\n"
                "    service: http://127.0.0.1:8097\n" % HOST,
                "  - service: http_status:404\n",
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(any("8097" in item for item in result["problems"]),
                        result["problems"])

    def test_legacy_private_services_are_still_refused(self):
        # Expand-contract (2026-11-14): the old ports are private listeners
        # too and must never be routed by the public edge.
        checker = _load_checker()
        for port in ("18096", "5901"):
            with self.subTest(port=port):
                with tempfile.TemporaryDirectory(prefix="ingress-legacy-") as tmp:
                    root = Path(tmp)
                    path = _write(root, "cloudflared.yml", _config([
                        "  - hostname: %s\n    path: ^/console$\n"
                        "    service: http://127.0.0.1:%s\n" % (HOST, port),
                        "  - service: http_status:404\n",
                    ]))
                    result = checker.check_ingress(path, expect_host=HOST)
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any(port in item for item in result["problems"]),
                    result["problems"])

    def test_the_catch_all_must_be_the_last_rule(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-order-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                "  - service: http_status:404\n",
                _api_rule("/v1/models"),
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("catch-all" in item or "last" in item
                for item in result["problems"]),
            result["problems"])

    def test_every_api_path_is_required(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-missing-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                _api_rule("/v1/models"),
                "  - service: http_status:404\n",
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        missing = [item for item in result["problems"]
                   if "missing" in item]
        self.assertEqual(3, len(missing), result["problems"])

    def test_a_rule_without_a_hostname_is_refused(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-host-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                "  - path: ^/v1/models$\n"
                "    service: http://127.0.0.1:8100\n",
                "  - service: http_status:404\n",
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(any("hostname" in item for item in result["problems"]),
                        result["problems"])

    def test_an_unexpected_hostname_is_refused(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-wrong-host-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", _config([
                _api_rule("/v1/models", host="example.invalid"),
                "  - service: http_status:404\n",
            ]))
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(any("example.invalid" in item
                            for item in result["problems"]),
                        result["problems"])

    def test_invalid_yaml_is_reported(self):
        checker = _load_checker()
        with tempfile.TemporaryDirectory(prefix="ingress-yaml-") as tmp:
            root = Path(tmp)
            path = _write(root, "cloudflared.yml", "ingress: [\n")
            result = checker.check_ingress(path, expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])

    def test_missing_config_is_reported(self):
        checker = _load_checker()
        result = checker.check_ingress(
            DEPLOY / "config" / "missing-cloudflared.yml", expect_host=HOST)
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])

    def test_checker_is_read_only(self):
        before = CONFIG.read_bytes()
        before_mtime = CONFIG.stat().st_mtime_ns
        checker = _load_checker()
        checker.check_ingress(CONFIG, expect_host=HOST)
        self.assertEqual(before, CONFIG.read_bytes())
        self.assertEqual(before_mtime, CONFIG.stat().st_mtime_ns)


class CheckIngressCliTests(unittest.TestCase):
    def test_cli_prints_ok_problems_and_rules(self):
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--config", str(CONFIG),
             "--expect-host", HOST],
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual({"ok", "problems", "rules"}, set(payload))
        self.assertTrue(payload["ok"], payload["problems"])
        self.assertEqual(5, len(payload["rules"]))

    def test_cli_refuses_an_unexpected_host(self):
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--config", str(CONFIG),
             "--expect-host", "example.invalid"],
            capture_output=True, text=True)
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])


if __name__ == "__main__":
    unittest.main()
