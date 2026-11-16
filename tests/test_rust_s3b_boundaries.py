# -*- coding: utf-8 -*-
"""S3b boundaries: one SQLite, one broker, one 8003 starter, old rollback.

The Rust slice must not add a second SQLite file, a second broker process or a
second 8003 launcher, and the rollback path stays the old broker unit with the
old Python entry. These are structural assertions over the product files, not a
second implementation.
"""
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import share.broker_ops as share_broker_ops  # noqa: E402

STORE_SRC = ROOT / "rust" / "crates" / "store" / "src"
BROKER_SRC = ROOT / "rust" / "crates" / "broker" / "src"


class S3bBoundaryTests(unittest.TestCase):
    def test_one_sqlite_owner_in_the_rust_store(self):
        opens = []
        for path in sorted(STORE_SRC.glob("*.rs")):
            text = path.read_text(encoding="utf-8")
            if "Connection::open(" in text:
                opens.append(path.name)
        self.assertEqual(opens, ["schema.rs"])
        self.assertEqual(
            (STORE_SRC / "schema.rs").read_text(encoding="utf-8").count(
                "Connection::open("),
            1,
        )

    def test_the_rust_store_never_spawns_a_process(self):
        for path in sorted(STORE_SRC.glob("*.rs")):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("Popen", "Command::new", "std::process"):
                self.assertNotIn(
                    forbidden, text,
                    f"{path.name} must not spawn a process")

    def test_one_broker_and_one_8003_starter(self):
        text = (ROOT / "share" / "broker_ops.py").read_text(
            encoding="utf-8")
        self.assertEqual(text.count("subprocess.Popen("), 1)
        self.assertEqual(text.count("self._spawner("), 1)
        tree = ast.parse(text)
        host_ops = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "LinuxHostOps")
        methods = {node.name: node for node in host_ops.body
                   if isinstance(node, ast.FunctionDef)}
        self.assertIn("start_host", methods)
        source = ast.get_source_segment(text, methods["start_host"])
        self.assertIn("subprocess.Popen(", source)

    def test_the_rollback_path_is_the_old_python_entry(self):
        unit = (ROOT / "um-cloud-deploy" / "systemd"
                / "um-share-broker.service").read_text(encoding="utf-8")
        self.assertIn("--host-command /opt/um-share/venv/bin/python", unit)
        self.assertIn(
            "--expect-instance-cmdline-marker chat2api.py", unit)
        self.assertIn(
            "--expect-instance-image /opt/um-share/venv/bin/python", unit)
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        values = dict(
            socket_path=str(directory / "broker.sock"),
            key_file=str(directory / "broker-control.key"),
            instance_key_file=str(directory / "instance.key"),
            registration_file=str(directory / "instance-registration.json"),
            instance_socket_prefix=str(directory / "instance-"),
            projection_path=str(directory / "share-projection.json"),
            broker_dir=str(directory / "broker"),
            host_pid_file=str(directory / "share-gateway.pid"),
            host_command=["/opt/um-share/venv/bin/python",
                          "/opt/um-share/current/chat2api/chat2api.py",
                          "--port", "8003"])
        plan = share_broker_ops.linux_server_plan(SimpleNamespace(**values))
        self.assertEqual(plan["host_entry_kind"], "python")
        self.assertEqual(plan["host_command"][0], values["host_command"][0])


if __name__ == "__main__":
    unittest.main()
