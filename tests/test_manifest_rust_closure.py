# -*- coding: utf-8 -*-
"""The Rust release manifest must keep the Python broker closure resolvable.

`um-cloud-deploy/make-manifest.py` computes the static AST closure from
`RUST_STATIC_ROOTS` and then requires every `RUST_CLOUD_RUNTIME` entry to be in
it. A standalone module (for example `share/instance_pipe.py`) is not reached by
any import, so it has to be an explicit static root. This test runs the real
generator and fails before a release package is built.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "um-cloud-deploy" / "make-manifest.py"
DEPENDENCIES = ROOT / "um-cloud-deploy" / "dependencies.lock.json"


class RustManifestClosureTests(unittest.TestCase):
    def test_rust_cloud_runtime_is_a_subset_of_the_static_closure(self):
        with tempfile.TemporaryDirectory(prefix="um-rust-manifest-") as tmp:
            output = Path(tmp) / "RELEASE-MANIFEST.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--root", str(ROOT),
                    "--deploy-root", str(ROOT / "um-cloud-deploy"),
                    "--version", "closure-test",
                    "--dependencies", str(DEPENDENCIES),
                    "--output", str(output),
                    "--runtime", "rust",
                    "--allow-missing-web",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            cloud = set(manifest["closures"]["cloud_runtime"]["files"])
            static = set(manifest["closures"]["static_superset"]["files"])
            self.assertTrue(cloud, "cloud runtime closure is empty")
            self.assertLessEqual(cloud, static, "cloud runtime is not in the static closure")
            self.assertIn("share/instance_pipe.py", cloud)
            self.assertIn("share/instance_pipe.py", static)


if __name__ == "__main__":
    unittest.main()
