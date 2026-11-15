# -*- coding: utf-8 -*-
"""Broker 8003 start identity: Rust entry accepted, Python entry kept.

The broker used to derive the share host entry marker from the interpreter's
SCRIPT operand only. The Rust gateway is a native binary, so its argv[0] IS the
entry; ``--host-entry-kind native`` selects that rule while the historical
``python`` kind (the default) keeps the old interpreter+script identity and stays
the rollback path. Both kinds go through the SAME ``LinuxHostOps`` identity check
and the SAME broker/unit - no second broker and no second 8003 starter.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import share.broker_ops as share_broker_ops  # noqa: E402


class _ProcRoot:
    """A temp ``/proc`` root with one pid whose cmdline is the given argv."""

    def __init__(self, root):
        self.root = Path(root)

    def install(self, pid, argv):
        directory = self.root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmdline").write_bytes(
            b"\x00".join(str(part).encode("utf-8") for part in argv)
            + b"\x00")
        return pid


class BrokerStartIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.proc = _ProcRoot(Path(self.directory.name) / "proc")
        self.root = Path(self.directory.name)

    def _host_ops(self, command, entry_kind):
        return share_broker_ops.LinuxHostOps(
            str(self.root / "share-host.pid"), command,
            state_dir=str(self.root / "state"),
            proc_root=str(self.proc.root), entry_kind=entry_kind)

    def test_python_entry_keeps_the_interpreter_script_identity(self):
        command = ["/opt/um-share/venv/bin/python",
                   "/opt/um-share/current/chat2api/chat2api.py",
                   "--port", "8003"]
        self.assertEqual(
            share_broker_ops.linux_entry_identity(command, "python"),
            "/opt/um-share/current/chat2api/chat2api.py")
        pid = self.proc.install(4321, command)
        self.assertTrue(
            self._host_ops(command, "python")._identity_ok(pid))

    def test_native_entry_uses_the_binary_as_its_own_identity(self):
        command = ["/opt/um-share/current/rust/um-gateway",
                   "--port", "8003"]
        self.assertEqual(
            share_broker_ops.linux_entry_identity(command, "native"),
            "/opt/um-share/current/rust/um-gateway")
        pid = self.proc.install(4322, command)
        self.assertTrue(self._host_ops(command, "native")._identity_ok(pid))

    def test_the_wrong_entry_kind_is_refused(self):
        python_command = ["/opt/um-share/venv/bin/python",
                          "/opt/um-share/current/chat2api/chat2api.py",
                          "--port", "8003"]
        pid = self.proc.install(4323, python_command)
        self.assertFalse(self._host_ops(python_command, "native")._identity_ok(pid))
        native_command = ["/opt/um-share/current/rust/um-gateway",
                          "--port", "8003"]
        pid = self.proc.install(4324, native_command)
        self.assertFalse(self._host_ops(native_command, "python")._identity_ok(pid))

    def test_an_unknown_entry_kind_is_refused(self):
        with self.assertRaises(ValueError):
            share_broker_ops.linux_entry_identity(
                ["/opt/um-share/current/rust/um-gateway"], "guessed")
        with self.assertRaises(ValueError):
            share_broker_ops.LinuxHostOps(
                str(self.root / "share-host.pid"),
                ["/opt/um-share/current/rust/um-gateway"],
                state_dir=str(self.root / "state"),
                proc_root=str(self.proc.root), entry_kind="guessed")

    def test_the_plan_carries_both_kinds_and_defaults_to_python(self):
        values = dict(
            socket_path=str(self.root / "run" / "broker.sock"),
            key_file=str(self.root / "control-key"),
            instance_key_file=str(self.root / "instance-key"),
            registration_file=str(self.root / "registration.json"),
            instance_socket_prefix=str(self.root / "run" / "instance-"),
            projection_path=str(self.root / "projection.json"),
            broker_dir=str(self.root / "broker"),
            host_pid_file=str(self.root / "share-host.pid"),
            host_command=["/opt/um-share/venv/bin/python",
                          "/opt/um-share/current/chat2api/chat2api.py",
                          "--port", "8003"])
        plan = share_broker_ops.linux_server_plan(SimpleNamespace(**values))
        self.assertEqual(plan["host_entry_kind"], "python")
        values["host_entry_kind"] = "native"
        values["host_command"] = ["/opt/um-share/current/rust/um-gateway",
                                 "--port", "8003"]
        plan = share_broker_ops.linux_server_plan(SimpleNamespace(**values))
        self.assertEqual(plan["host_entry_kind"], "native")
        values["host_entry_kind"] = "guessed"
        with self.assertRaises(ValueError):
            share_broker_ops.linux_server_plan(SimpleNamespace(**values))

    def test_the_broker_cli_accepts_the_native_entry_kind(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--host-entry-kind",
                            choices=share_broker_ops.LINUX_HOST_ENTRY_KINDS,
                            default="python")
        self.assertEqual(
            parser.parse_args([]).host_entry_kind, "python")
        self.assertEqual(
            parser.parse_args(["--host-entry-kind", "native"]).host_entry_kind,
            "native")


if __name__ == "__main__":
    unittest.main()
