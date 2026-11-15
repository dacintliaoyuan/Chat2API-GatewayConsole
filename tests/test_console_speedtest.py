# -*- coding: utf-8 -*-
"""Retired model speedtest: assert it is gone, not exercised.

The UM model speedtest (``model.speedtest`` and ``console/speedtest.py``) was
retired with the local feature set. This suite keeps the regression runnable
while pinning the retirement:

* the ``console.speedtest`` module does not import;
* neither the local nor the cloud registry registers a speedtest action;
* submitting the retired action is refused before an operation row exists;
* the context probe (the kept sibling capability) is still registered.

No network, no stub server and no real credential is touched here.
"""

import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import adapters as console_adapters  # noqa: E402
from console.operations import (  # noqa: E402
    OperationError,
    OperationManager,
)

RETIRED_ACTION = "model.speedtest"
KEPT_ACTIONS = (
    "model.catalog",
    "model.context_probe_preview",
    "model.context_probe_run",
    "model.context_overrides",
    "model.context_override_set",
    "model.context_override_clear",
)


class RetiredModuleTests(unittest.TestCase):
    def test_speedtest_module_does_not_import(self):
        self.assertIsNone(importlib.util.find_spec("console.speedtest"))
        with self.assertRaises(ModuleNotFoundError):
            __import__("console.speedtest")


class RegistryAbsenceTests(unittest.TestCase):
    def _names(self, cloud):
        return {spec.name for spec in console_adapters.build_registry(
            cloud=cloud)}

    def test_local_registry_has_no_speedtest_action(self):
        names = self._names(cloud=False)
        self.assertNotIn(RETIRED_ACTION, names)
        self.assertEqual([name for name in names if "speedtest" in name], [])

    def test_cloud_registry_has_no_speedtest_action(self):
        names = self._names(cloud=True)
        self.assertNotIn(RETIRED_ACTION, names)
        self.assertEqual([name for name in names if "speedtest" in name], [])

    def test_context_probe_remains_registered(self):
        specs = {spec.name: spec for spec in console_adapters.build_registry()}
        for name in KEPT_ACTIONS:
            with self.subTest(action=name):
                self.assertIn(name, specs)
                self.assertIsNotNone(specs[name].handler)
        self.assertTrue(specs["model.context_probe_run"].enabled)
        self.assertIsNone(
            specs["model.context_probe_run"].disabled_reason)


class SubmitRefusalTests(unittest.TestCase):
    def test_retired_action_is_refused_before_an_operation_row(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OperationManager(
                os.path.join(directory, "state.sqlite3"),
                console_adapters.build_registry(),
                os.path.join(directory, "locks"))
            try:
                with self.assertRaises(OperationError) as caught:
                    manager.submit(RETIRED_ACTION, target="model",
                                   params={"source": "um",
                                            "model": "gpt-test"})
                self.assertEqual(caught.exception.code, "invalid_action")
                self.assertEqual(manager.list_operations(), [])
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
