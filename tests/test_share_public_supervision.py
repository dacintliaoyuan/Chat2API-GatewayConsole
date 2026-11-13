# -*- coding: utf-8 -*-
"""Retirement guard: the Windows Funnel public-supervision module is gone.

The old Windows local share chain was retired (#33): the Funnel supervision
round, its state/event/budget files and its cancel seam must not come back.
The Cloudflare/DoH/TLS public checks that remain live are the console's
``console/public_probe.py`` checks (covered by their own tests).

This file replaces the former offline supervision suite on purpose: it keeps a
runnable guard that the retired module and its console read face do not exist.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicSupervisionRetiredTests(unittest.TestCase):
    def test_module_file_is_removed(self):
        self.assertFalse(
            (ROOT / "share" / "public_supervision.py").exists(),
            "share/public_supervision.py must stay deleted with the old "
            "Windows share chain")

    def test_module_is_not_importable(self):
        sys.modules.pop("share.public_supervision", None)
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("share.public_supervision")

    def test_console_public_probe_keeps_the_cloudflare_checks(self):
        """The retained public checks are the Cloudflare/DoH/TLS probe."""
        import console.public_probe as probe

        self.assertTrue(callable(probe.check_public_connection))
        self.assertTrue(callable(probe.probe_public_model))
        self.assertTrue(callable(probe.origin_host))


if __name__ == "__main__":
    unittest.main()
