# -*- coding: utf-8 -*-
"""Rust S1a 行为夹具的新鲜度检查（Ticket #44）。

`tests/fixtures/rust_gateway_rev1.json` 必须由
`tests/generate_rust_gateway_fixture.py` 从真实 Python `chat2api.Handler` 与
`ShareGate` 生成。本模块只启动 loopback stub 与临时目录，不绑定生产端口、
不读写生产数据。
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_gateway_rev1.json"

sys.path.insert(0, str(ROOT / "tests"))


class RustGatewayFixtureTests(unittest.TestCase):
    def test_fixture_is_fresh_from_the_real_python_handler(self):
        self.skipTest("Python closure retired (#90); the source/generator is frozen")
        import generate_rust_gateway_fixture as generator

        self.assertEqual(
            generator.generate(),
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            "Rust gateway fixture is stale; run "
            "python tests/generate_rust_gateway_fixture.py",
        )

    def test_fixture_records_every_acceptance_surface(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertTrue(fixture["metadata"]["fixture"])
        for name in ("health", "readyValid", "version", "versionShare",
                     "models", "modelsRawFresh"):
            self.assertIn(name, fixture["readOnly"])
        self.assertTrue(fixture["shareGate"]["credentialCases"])
        self.assertTrue(fixture["shareGate"]["routeCases"])
        self.assertTrue(fixture["shareGate"]["gateReasons"])
        self.assertTrue(fixture["bodyRejection"]["share"])
        self.assertTrue(fixture["bodyRejection"]["console"])
        self.assertTrue(fixture["cors"])
        self.assertTrue(fixture["control"])
        self.assertIn("willClose", fixture["closeOnError"])


if __name__ == "__main__":
    unittest.main()
