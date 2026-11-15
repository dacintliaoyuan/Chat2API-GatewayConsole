# -*- coding: utf-8 -*-
"""S8 boundaries: production tools stay Python and stateful actions are explicit.

The Rust crate is a read-only consumer of the frozen CLI/JSON contracts.  These
checks only read repository files; they never run a backup, restore, update,
rollback, Cloudflare reload or public probe.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "rust" / "crates" / "ops"
DEPLOY = ROOT / "um-cloud-deploy"


def read(path):
    return path.read_text(encoding="utf-8")


class RustS8BoundaryTests(unittest.TestCase):
    def test_production_units_still_run_the_python_tools(self):
        console = read(DEPLOY / "systemd" / "um-share-console.service")
        broker = read(DEPLOY / "systemd" / "um-share-broker.service")
        self.assertIn("usagesrv.py", console)
        self.assertIn("broker_ops.py", broker)
        for text in (console, broker):
            self.assertNotIn("um-ops", text)
            self.assertNotIn("um-gateway-candidate", text)

    def test_install_update_and_rollback_stay_python_bash(self):
        for name in ("install.sh", "update.sh", "rollback.sh"):
            text = read(DEPLOY / name)
            self.assertNotIn("um-ops", text, name)
        self.assertIn("backup_restore.py", read(DEPLOY / "update.sh"))
        self.assertIn("backup_restore.py", read(DEPLOY / "rollback.sh"))

    def test_no_automatic_backup_or_timer_was_added(self):
        names = {path.name for path in DEPLOY.rglob("*") if path.is_file()}
        for name in names:
            self.assertNotIn("backup.timer", name)
            self.assertNotIn("backup.service", name)
        for name in ("install.sh", "update.sh", "rollback.sh"):
            text = read(DEPLOY / name)
            self.assertNotIn("backup.timer", text)
            self.assertNotIn("backup.service", text)
        cli = read(RUST / "src" / "main.rs")
        self.assertNotIn("thread::spawn", cli)
        self.assertNotIn("sleep", cli)

    def test_read_only_modules_never_write_or_spawn(self):
        for name in ("ledger.rs", "ingress.rs", "public_probe.rs"):
            text = read(RUST / "src" / name)
            for forbidden in (
                "fs::write",
                "File::create",
                "OpenOptions",
                "remove_file",
                "remove_dir",
                "Command::new",
            ):
                self.assertNotIn(forbidden, text, f"{name}: {forbidden}")

    def test_the_ingress_checker_never_reloads_a_tunnel(self):
        text = read(RUST / "src" / "ingress.rs")
        code = chr(10).join(
            line for line in text.splitlines()
            if not line.strip().startswith("//")
        )
        for forbidden in ("reload", "restart", "systemctl", "Command"):
            self.assertNotIn(forbidden, code)
        self.assertIn("check_ingress", code)

    def test_stateful_tools_are_forwarded_to_the_existing_cli(self):
        cli = read(RUST / "src" / "main.rs")
        deploy = read(RUST / "src" / "deploy.rs")
        self.assertIn('"backup" | "restore"', cli)
        self.assertIn("run_backup_restore", cli)
        self.assertIn("run_deploy_script", cli)
        self.assertIn('"probe"', cli)
        self.assertIn("--check-connection", cli)
        self.assertIn("Command::new(python)", deploy)
        self.assertIn("bash_program()", deploy)
        self.assertNotIn("AESGCM", deploy)
        self.assertNotIn("UMCLDBK1", deploy)

    def test_rust_binary_is_not_in_the_release_manifest(self):
        manifest = read(ROOT / "um-cloud-deploy" / "RELEASE-MANIFEST.json")
        self.assertNotIn("um-ops", manifest)
        self.assertNotIn("rust/target", manifest)
        self.assertNotIn("rust/crates/ops", manifest)

    def test_the_public_probe_defaults_to_python(self):
        cli = read(RUST / "src" / "main.rs")
        self.assertIn('public_probe: PathBuf::from("console/public_probe.py")', cli)
        self.assertIn('backup_restore: PathBuf::from("um-cloud-deploy/backup_restore.py")', cli)
        self.assertIsNotNone(re.search(r'python: "python"', cli))


if __name__ == "__main__":
    unittest.main()
