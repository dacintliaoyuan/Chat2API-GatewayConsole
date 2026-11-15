"""Freshness check for the generated share-store fixture (#49)."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tests" / "generate_rust_store_fixture.py"
FIXTURE = ROOT / "tests" / "fixtures" / "rust_store_rev1.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "rust_store_fixture_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rust_store_fixture_generator"] = module
    spec.loader.exec_module(module)
    return module


class StoreFixtureFreshnessTests(unittest.TestCase):
    def test_committed_fixture_matches_the_generator(self):
        generator = _load_generator()
        expected = generator.generate()
        committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(committed, expected)


if __name__ == "__main__":
    unittest.main()
