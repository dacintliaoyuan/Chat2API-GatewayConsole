# -*- coding: utf-8 -*-
"""Retired price-version ledger: assert it is gone, not exercised.

The price-version stamp on each chat2api ledger row and the
``CHAT2API_PRICE_TABLE_FILE`` environment variable were retired with the
cost/price feature set (#30). This suite keeps the regression runnable while
pinning the retirement:

* a written ledger row no longer carries ``price_version`` or
  ``request_started_at`` (the price-eligibility start);
* the raw usage fields (time/model/status/tokens/usage_source) are still
  written and still aggregate;
* ``chat2api`` reads no price-table environment variable.

All file inputs are redirected to temp dirs; the production ledger is never
written.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(AI_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_ROOT))

CHAT2API_PATH = AI_GATEWAY_ROOT / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location("chat2api_price_ledger",
                                             CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)


class RetiredPriceVersionLedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.usage_log = self.base / "usage-chat2api.jsonl"
        for attr, value in (
                ("USAGE_LOG", str(self.usage_log)),
                ("_usage_disabled", False),
        ):
            patcher = mock.patch.object(chat2api, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, chat2api, "_usage_disabled", False)

    def _write(self, **facts):
        for name, value in (
                ("protocol", "chat"),
                ("key_id", None),
                ("request_id", "req-1"),
                ("attribution", "absent"),
                ("attribution_ts", None),
        ):
            setattr(chat2api._request_facts, name, value)
        chat2api.record_usage(
            "glm-5.3", False, time.time(), {
                "prompt_tokens": 100,
                "completion_tokens": 10,
            }, 100, 10, status="ok", usage_source="upstream")
        lines = self.usage_log.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])

    def test_written_row_has_no_price_version_or_start(self):
        row = self._write()
        self.assertNotIn("price_version", row)
        self.assertNotIn("request_started_at", row)

    def test_raw_usage_fields_are_still_written(self):
        row = self._write()
        self.assertEqual(row["model"], "glm-5.3")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 10)
        self.assertEqual(row["usage_source"], "upstream")
        self.assertEqual(row["service"], "chat2api")

    def test_chat2api_reads_no_price_environment(self):
        source = CHAT2API_PATH.read_text(encoding="utf-8")
        for token in (
            "CHAT2API_PRICE_TABLE_FILE",
            "PRICE_TABLE_FILE",
            "prices.json",
            "_price_table_version",
            "_price_version_cache",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_price_table_file_constant_is_gone(self):
        self.assertFalse(hasattr(chat2api, "PRICE_TABLE_FILE"))
        self.assertFalse(hasattr(chat2api, "_price_table_version"))
        self.assertFalse(hasattr(chat2api, "_price_version_cache"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
