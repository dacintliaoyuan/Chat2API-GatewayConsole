# -*- coding: utf-8 -*-
"""Rust 契约夹具的新鲜度与覆盖检查（Ticket #43 S0）。

`tests/fixtures/rust_contract_rev1.json` 必须由
`tests/generate_rust_contract_fixture.py` 从当前 `docs/rust-contract.md` 与当前
Python 常量生成。本模块只读文件与生成器，不启动进程、不发请求、不读写运行数据。
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_contract_rev1.json"
GENERATOR = ROOT / "tests" / "generate_rust_contract_fixture.py"

sys.path.insert(0, str(ROOT / "tests"))


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class RustContractFixtureTests(unittest.TestCase):
    def test_fixture_is_fresh_from_the_current_generator(self):
        self.skipTest("Python closure retired (#90); the source/generator is frozen")
        import generate_rust_contract_fixture as generator

        self.assertEqual(generator.generate(), _fixture(),
                         "Rust contract fixture is stale; run "
                         "python tests/generate_rust_contract_fixture.py")

    def test_source_hashes_match_the_current_files(self):
        self.skipTest("Python closure retired (#90); the source/generator is frozen")
        fixture = _fixture()
        self.assertEqual("utf8-lf", fixture["metadata"]["sourceHashEncoding"])
        for name, expected in fixture["metadata"]["sourceHashes"].items():
            text = (ROOT / name).read_text(encoding="utf-8")
            actual = hashlib.sha256(
                text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
            self.assertEqual(expected, actual, "%s: source hash is stale" % name)

    def test_docs_hash_matches_the_current_contract(self):
        import generate_rust_contract_fixture as generator

        fixture = _fixture()
        self.assertEqual(
            fixture["metadata"]["sourceHashes"]["docs/rust-contract.md"],
            generator.contract_doc_hash(),
            "contract document hash is stale; run "
            "python tests/generate_rust_contract_fixture.py --docs-only",
        )

    def test_fixture_marks_every_required_coverage_surface(self):
        fixture = _fixture()
        self.assertTrue(fixture["metadata"]["fixture"])
        self.assertEqual("docs/rust-contract.md", fixture["metadata"]["contract"])
        for category in ("endpoints", "errors", "sseEvents", "timeouts",
                         "cancellation", "idempotency", "sideEffects"):
            self.assertTrue(fixture["coverage"][category], category)
        self.assertTrue(fixture["endpoints"])
        self.assertTrue(fixture["errors"])
        self.assertTrue(fixture["sse"]["gateway"]["responses"]["events"])
        self.assertTrue(fixture["boundaries"]["timeouts"])
        self.assertTrue(fixture["boundaries"]["cancellation"])
        self.assertTrue(fixture["boundaries"]["idempotency"])
        self.assertTrue(fixture["boundaries"]["sideEffects"])
        self.assertTrue(fixture["boundaries"]["forbidden"])

    def test_harness_metadata_has_no_production_targets(self):
        fixture = _fixture()
        harness = fixture["harness"]
        self.assertEqual("127.0.0.1", harness["bindHost"])
        self.assertEqual(0, harness["bindPort"])
        self.assertTrue(harness["tempDirPrefix"])
        self.assertEqual(fixture["ports"]["production"],
                         harness["productionPorts"])
        self.assertTrue(harness["productionDataRoots"])


if __name__ == "__main__":
    unittest.main()
