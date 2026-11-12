"""Exercise the desktop BAT boundary with an isolated PowerShell entry."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"),
                     "requires Windows and PowerShell 7")
class ConsoleLauncherExitTests(unittest.TestCase):
    def _run_entry(self, source):
        launcher = pathlib.Path(__file__).resolve().parents[1] / "Open-GatewayConsole.bat"
        with tempfile.TemporaryDirectory(prefix="console launcher ") as directory:
            root = pathlib.Path(directory)
            bat = root / launcher.name
            bat.write_bytes(launcher.read_bytes())
            # Replacing only the final entry prevents any service or browser I/O.
            (root / "Open-GatewayConsole.ps1").write_text(source, encoding="utf-8")
            command = '"{}" /d /s /c ""{}""'.format(
                os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), bat)
            return subprocess.run(command, capture_output=True, timeout=15).returncode

    def test_failure_exit_is_preserved(self):
        self.assertEqual(self._run_entry("exit 7\n"), 7)

    def test_success_exit_is_preserved(self):
        self.assertEqual(self._run_entry("exit 0\n"), 0)

    def test_terminating_error_is_not_reported_as_success(self):
        self.assertEqual(self._run_entry("throw 'isolated launch failure'\n"), 1)


if __name__ == "__main__":
    unittest.main()
