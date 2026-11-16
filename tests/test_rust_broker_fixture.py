"""Freshness check for the generated broker wire fixture (#48)."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tests" / "generate_rust_broker_fixture.py"
FIXTURE = ROOT / "tests" / "fixtures" / "rust_broker_wire_rev1.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "rust_broker_fixture_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rust_broker_fixture_generator"] = module
    spec.loader.exec_module(module)
    return module


class BrokerFixtureFreshnessTests(unittest.TestCase):
    def test_committed_fixture_matches_the_generator(self):
        generator = _load_generator()
        expected = generator.generate()
        committed = __import__("json").loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(committed, expected)



if __name__ == "__main__":
    unittest.main()
