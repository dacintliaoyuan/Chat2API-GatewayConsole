# -*- coding: utf-8 -*-
"""Linux process identity: the ENTRY SCRIPT, never the last argument.

The production cloud command is
``[python, chat2api.py, --port, 28100, --upstream-port, 28110]``. The old
``LinuxHostOps._identity_ok`` took the LAST element's basename as the entry
marker, so it looked for a process running ``28110`` and never recognized the
live share host (the management read judged a running service as "not it").

These tests drive the real ``LinuxHostOps`` over an injected ``/proc`` root
populated from a REAL child process: on Linux the child's own
``/proc/<pid>/cmdline`` is read; off Linux the exact argv list the child was
started with is used (Popen hands that same list to CreateProcess, so it IS the
process's command line). They pin the entry-script rule:

* the production cloud shape matches the real command line;
* another entry script is rejected;
* the script name used as an argument to ``python -c`` is rejected;
* a wrong ``--port`` and a wrong uid are rejected;
* a missing/empty cmdline is rejected and the pid is never signalled.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from console import share_production as console_share_production  # noqa: E402
import share.broker_ops as share_broker_ops  # noqa: E402

GATEWAY_PORT = 28100
BACKEND_PORT = 28110


def _sleep_script(directory, name):
    path = Path(directory) / name
    path.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    return path


def _child_cmdline(pid, argv):
    """The REAL command line of the live child.

    Linux: the kernel's ``/proc/<pid>/cmdline`` (the bytes the kernel itself
    reports). Off Linux there is no ``/proc``, so the exact argv list the child
    was started with is used - Popen passes that same list to CreateProcess, so it
    IS the process's command line.

    WSL2 may expose an empty ``cmdline`` for a moment right after the child is
    spawned even though it is already running, so wait (bounded) for the kernel to
    publish the real bytes. A child that never publishes a cmdline still yields an
    empty list and fails the caller's assertions.
    """
    path = Path("/proc", str(pid), "cmdline")
    try:
        raw = path.read_bytes()
    except OSError:
        return [str(part) for part in argv]
    deadline = time.monotonic() + 5.0
    while not raw and time.monotonic() < deadline:
        time.sleep(0.01)
        try:
            raw = path.read_bytes()
        except OSError:
            return [str(part) for part in argv]
    return [part.decode("utf-8", errors="replace")
            for part in raw.split(b"\x00") if part]


class _ProcRoot:
    """Temp ``/proc`` root: one pid directory with a cmdline file."""

    def __init__(self, root):
        self.root = Path(root)

    def install(self, pid, cmdline):
        directory = self.root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmdline").write_bytes(
            b"\x00".join(part.encode("utf-8") for part in cmdline)
            + b"\x00")
        return directory


class _KillSpy:
    """Records every signal; never touches the real process.

    ``LinuxHostOps`` is POSIX-only and is exercised here through this injected
    boundary, so the real child stays alive for the whole test.
    """

    def __init__(self, alive=True):
        self.alive = alive
        self.signals = []

    def __call__(self, pid, sig):
        self.signals.append((int(pid), int(sig)))
        if not self.alive:
            raise ProcessLookupError


class _RealChildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.proc_root = _ProcRoot(self.proc)
        self.state = self.root / "state"
        self.pid_file = self.state / "share-host.pid"
        self.children = []

    def tearDown(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def _spawn(self, argv):
        proc = subprocess.Popen(
            [str(part) for part in argv],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(proc)
        return proc

    def _production_command(self, script):
        return [sys.executable, str(script), "--port", str(GATEWAY_PORT),
                "--upstream-port", str(BACKEND_PORT)]

    def _ops(self, command, *, expected_port=GATEWAY_PORT, expected_uid=None,
             kill_fn=None):
        return share_broker_ops.LinuxHostOps(
            str(self.pid_file), command,
            state_dir=str(self.state), expected_port=expected_port,
            expected_uid=expected_uid, proc_root=str(self.proc),
            kill_fn=kill_fn, clock=lambda: 0.0,
            sleeper=lambda _seconds: None)

    def _write_pid(self, pid):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(pid), encoding="ascii")

    def _install_real(self, proc, argv):
        cmdline = _child_cmdline(proc.pid, argv)
        self.proc_root.install(proc.pid, cmdline)
        self._write_pid(proc.pid)
        return cmdline


class LinuxEntryScriptTests(unittest.TestCase):
    def test_production_shape_uses_the_script_not_the_trailing_port(self):
        entry = share_broker_ops.linux_entry_script(
            [sys.executable, "chat2api.py", "--port", "28100",
             "--upstream-port", "28110"])
        self.assertEqual(os.path.basename(entry), "chat2api.py")
        self.assertNotEqual(os.path.basename(entry), "28110")

    def test_c_argument_is_not_an_entry(self):
        self.assertIsNone(share_broker_ops.linux_entry_script(
            [sys.executable, "-c", "print(1)", "chat2api.py",
             "--port", "28100"]))

    def test_option_values_are_skipped(self):
        entry = share_broker_ops.linux_entry_script(
            [sys.executable, "-u", "-W", "ignore", "chat2api.py"])
        self.assertEqual(os.path.basename(entry), "chat2api.py")

    def test_script_without_py_suffix_falls_back_to_first_positional(self):
        entry = share_broker_ops.linux_entry_script(
            [sys.executable, "share_entry", "--port", "28100"])
        self.assertEqual(os.path.basename(entry), "share_entry")


class LinuxHostOpsIdentityTests(_RealChildCase):
    def test_production_cloud_command_matches_the_real_cmdline(self):
        script = _sleep_script(self.root, "chat2api.py")
        argv = self._production_command(script)
        proc = self._spawn(argv)
        cmdline = self._install_real(proc, argv)
        # The real command line ends with the BACKEND port: the old
        # ``command[-1]`` marker could never be found in it.
        self.assertEqual(cmdline[-1], str(BACKEND_PORT))
        self.assertNotIn("chat2api.py", [cmdline[-1]])
        spy = _KillSpy()
        ops = self._ops(argv, kill_fn=spy)
        self.assertTrue(ops.verify_running(), ops.last_error)
        self.assertEqual(ops.status()["state"], "running")

    def test_other_entry_script_is_rejected(self):
        expected = _sleep_script(self.root, "chat2api.py")
        actual = _sleep_script(self.root, "other_entry.py")
        argv = [sys.executable, str(actual), "--port", str(GATEWAY_PORT),
                "--upstream-port", str(BACKEND_PORT)]
        proc = self._spawn(argv)
        self._install_real(proc, argv)
        ops = self._ops(self._production_command(expected),
                        kill_fn=_KillSpy())
        self.assertFalse(ops.verify_running())
        self.assertEqual(ops.status()["state"], "conflict")

    def test_script_name_as_a_c_argument_is_rejected(self):
        script = _sleep_script(self.root, "chat2api.py")
        code = "import time; time.sleep(60)"
        argv = [sys.executable, "-c", code, str(script), "--port",
                str(GATEWAY_PORT)]
        proc = self._spawn(argv)
        cmdline = self._install_real(proc, argv)
        # The script name IS on the line (as the -c code's argument) and the
        # --port pair matches: only the entry-script rule can reject this.
        self.assertIn("chat2api.py", " ".join(cmdline))
        self.assertEqual(cmdline[-2:], ["--port", str(GATEWAY_PORT)])
        ops = self._ops(self._production_command(script),
                        kill_fn=_KillSpy())
        self.assertFalse(ops.verify_running())
        self.assertEqual(ops.status()["state"], "conflict")

    def test_wrong_port_is_rejected(self):
        script = _sleep_script(self.root, "chat2api.py")
        argv = self._production_command(script)
        proc = self._spawn(argv)
        self._install_real(proc, argv)
        ops = self._ops(argv, expected_port=GATEWAY_PORT + 1,
                        kill_fn=_KillSpy())
        self.assertFalse(ops.verify_running())
        self.assertEqual(ops.status()["state"], "conflict")

    def test_wrong_uid_is_rejected(self):
        script = _sleep_script(self.root, "chat2api.py")
        argv = self._production_command(script)
        proc = self._spawn(argv)
        self._install_real(proc, argv)
        actual_uid = os.stat(self.proc / str(proc.pid)).st_uid
        ops = self._ops(argv, expected_uid=actual_uid + 1,
                        kill_fn=_KillSpy())
        self.assertFalse(ops.verify_running())
        self.assertEqual(ops.status()["state"], "conflict")

    def test_missing_cmdline_is_rejected_and_never_signalled(self):
        script = _sleep_script(self.root, "chat2api.py")
        argv = self._production_command(script)
        proc = self._spawn(argv)
        (self.proc / str(proc.pid)).mkdir(parents=True, exist_ok=True)
        self._write_pid(proc.pid)
        spy = _KillSpy()
        ops = self._ops(argv, kill_fn=spy)
        self.assertFalse(ops.verify_running())
        self.assertFalse(ops.stop_host())
        self.assertEqual([sig for _pid, sig in spy.signals if sig in (15, 9)],
                         [])

    def test_empty_cmdline_is_rejected_and_never_signalled(self):
        script = _sleep_script(self.root, "chat2api.py")
        argv = self._production_command(script)
        proc = self._spawn(argv)
        directory = self.proc / str(proc.pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmdline").write_bytes(b"")
        self._write_pid(proc.pid)
        spy = _KillSpy()
        ops = self._ops(argv, kill_fn=spy)
        self.assertFalse(ops.verify_running())
        self.assertFalse(ops.stop_host())
        self.assertEqual([sig for _pid, sig in spy.signals if sig in (15, 9)],
                         [])


class LinuxBackendIdentityTests(unittest.TestCase):
    """``_LinuxBackendOps`` has the same entry rule (never an argument)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _ops(self):
        return console_share_production._LinuxBackendOps(
            gateway_dir=str(self.root), port=8110)

    def test_entry_script_is_accepted(self):
        self.assertTrue(self._ops()._identity_ok(
            {"port": 8110}, sys.executable,
            "python chat2api.py --port 8110 --profile profile"))

    def test_script_name_as_an_argument_is_rejected(self):
        self.assertFalse(self._ops()._identity_ok(
            {"port": 8110}, sys.executable,
            "python -c print(1) chat2api.py --port 8110"))


if __name__ == "__main__":
    unittest.main()
