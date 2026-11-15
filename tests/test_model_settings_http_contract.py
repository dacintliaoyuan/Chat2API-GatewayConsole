"""Retired settings HTTP contract (Ticket #32).

The model settings publication actions are retired with the client
synchronization. A submit follows the existing unknown-action contract (404
invalid_action) and creates no operation row.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # `python -m unittest tests.<module>` from the repo root
    from tests.test_console_api import (
        ConsoleApiTestBase as _ApiBase, _CONTROL_TOKEN)
except ImportError:  # `discover -s tests` (top-level module names)
    from test_console_api import ConsoleApiTestBase as _ApiBase, _CONTROL_TOKEN


class SettingsHttpContractTests(_ApiBase):
    RETIRED = (
        "model.settings_save", "model.settings_preview", "model.settings_apply",
    )

    def _submit(self, action, params=None):
        body = {"action": action, "target": "model"}
        if params is not None:
            body["params"] = params
        return self.req("POST", "/api/console/operations", body=body,
                        headers={"X-Admin-Control": _CONTROL_TOKEN})

    def _operation_count(self):
        status, raw = self.req("GET", "/api/console/operations?limit=200",
                              headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        return len(json.loads(raw)["operations"])

    def test_retired_settings_actions_are_unknown_and_create_no_row(self):
        for action in self.RETIRED:
            with self.subTest(action=action):
                before = self._operation_count()
                status, raw = self._submit(action, {"selection": {}})
                self.assertEqual(status, 404, action)
                self.assertEqual(json.loads(raw)["error"], "invalid_action",
                                 action)
                self.assertEqual(before, self._operation_count(),
                                 "拒绝不得入操作账本")


if __name__ == "__main__":
    unittest.main()
