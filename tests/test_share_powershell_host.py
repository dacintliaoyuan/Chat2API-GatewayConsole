"""Offline coverage for a broker inheriting Task Scheduler's stale PATH."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from share.broker_ops import WindowsHostOps


class SharePowerShellHostTests(unittest.TestCase):
    def test_installed_powershell_runs_with_no_path_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "PowerShell" / "7" / "pwsh.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            args = ["pwsh", "-NoProfile", "-Command", "Write-Output '状态'"]
            reply = subprocess.CompletedProcess(args, 0, "状态\n", "")
            with patch.dict(os.environ, {"ProgramFiles": directory, "PATH": ""}), \
                    patch("share.broker_ops.subprocess.run", return_value=reply) as run:
                actual = WindowsHostOps._default_runner(args, 3)
            self.assertIs(actual, reply)
            self.assertEqual(run.call_args.args[0], [str(executable), *args[1:]])
            self.assertEqual(args[0], "pwsh", "The caller's argument list is immutable")
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_other_commands_keep_their_arguments(self):
        args = ["schtasks", "/Query", "/TN", "share fixture"]
        with patch("share.broker_ops.subprocess.run") as run:
            WindowsHostOps._default_runner(args, 4)
        self.assertEqual(run.call_args.args[0], args)
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

    @unittest.skipUnless(os.name == "nt", "Native Windows output uses the OEM code page")
    def test_localized_task_output_is_decoded_before_state_normalization(self):
        try:
            payload = '"share fixture","N/A","正在运行"\r\n'.encode("oem")
        except UnicodeEncodeError:
            self.skipTest("The current OEM code page does not represent Chinese task states")
        command = "import sys; sys.stdout.buffer.write(bytes.fromhex(%r))" % payload.hex()
        reply = WindowsHostOps._default_runner([sys.executable, "-c", command], 5)
        self.assertEqual(reply.returncode, 0)
        self.assertEqual(WindowsHostOps._parse_state(reply.stdout), "Running")

    def test_path_install_remains_supported_without_standard_install(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"ProgramFiles": directory}), \
                patch("share.broker_ops.subprocess.run") as run:
            WindowsHostOps._default_runner(["pwsh", "-NoProfile"], 3)
        self.assertEqual(run.call_args.args[0], ["pwsh", "-NoProfile"])


if __name__ == "__main__":
    unittest.main()
