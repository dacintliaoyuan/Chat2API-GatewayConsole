# -*- coding: utf-8 -*-
"""#71: a Python rollback release and a port-parameterized health gate.

The release that a Rust cutover rolls back to must be able to start the merged
Python gateway: its broker unit template has to allow writes to
``/var/lib/um-share-console``, and update/rollback must probe the target port
instead of hardcoding 8100.

The textual tests read the shipped units/scripts.  The shell tests run the real
``lib/common.sh`` helpers and the drill's ``drill_execute`` with doubles only at
external I/O (systemctl/curl/rollback.sh); no unit, service, release or production
path is touched and no drill is executed against a real host.

Run from the repository root:

    python -m unittest tests.test_cloud_rollback_release
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
DEPLOY = HERE / "um-cloud-deploy"
BASH = shutil.which("bash") or "bash"


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _posix(path):
    text = str(Path(path).resolve()).replace("\\", "/")
    if len(text) > 1 and text[1:2] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _read_write_paths(unit_text):
    paths = []
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ReadWritePaths="):
            paths.extend(stripped.split("=", 1)[1].split())
    return paths


class RollbackReleaseUnitTests(unittest.TestCase):
    def test_broker_unit_allows_the_merged_child_to_write_console_state(self):
        unit = _read("systemd/um-share-broker.service")
        paths = _read_write_paths(unit)
        self.assertIn("/var/lib/um-share-console", paths,
                      "the merged 8100 child cannot write token/profile/log")
        # The existing fences stay; this is an addition, not a replacement.
        for path in ("/var/lib/um-share", "/run/um-share",
                     "/var/log/um-share"):
            self.assertIn(path, paths)

    def test_install_refuses_a_release_without_the_console_write_path(self):
        install = _read("install.sh")
        self.assertIn(
            "require_share_backend_write_path \\\n"
            "  \"$TARGET/um-cloud-deploy/systemd/um-share-broker.service\"",
            install)
        self.assertLess(
            install.index("require_share_backend_write_path"),
            install.index("install_units \"$TARGET\""))
        self.assertLess(
            install.index("require_share_backend_write_path"),
            install.index('ln -sfn "$UM_RELEASES/$VERSION" "$UM_PREFIX/current.tmp.$$"'))

    def test_package_release_refuses_a_payload_without_the_write_path(self):
        package = _read("package-release.sh")
        self.assertIn(
            "require_share_backend_write_path \\\n"
            "  \"$PAYLOAD/um-cloud-deploy/systemd/um-share-broker.service\"",
            package)
        self.assertLess(
            package.index("require_share_backend_write_path"),
            package.index('"$PYTHON" "$PAYLOAD/um-cloud-deploy/make-manifest.py"'))

    def test_rollback_refuses_a_python_release_without_the_write_path(self):
        rollback = _read("rollback.sh")
        self.assertIn(
            "require_share_backend_write_path \\\n"
            "    \"$TARGET/um-cloud-deploy/systemd/um-share-broker.service\"",
            rollback)
        self.assertLess(
            rollback.index("require_share_backend_write_path"),
            rollback.index('atomic_symlink "$TARGET" "$CURRENT_LINK"'))

    def test_the_packaged_deployment_files_include_the_drill_and_unit(self):
        spec = importlib.util.spec_from_file_location(
            "um_cloud_make_manifest_for_rollback",
            DEPLOY / "make-manifest.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        paths = {entry["path"] for entry in
                 module._deployment_files(HERE, DEPLOY)}
        self.assertIn("um-cloud-deploy/rollback-drill.sh", paths)
        self.assertIn("um-cloud-deploy/systemd/um-share-broker.service", paths)

    def test_verify_release_requires_the_drill(self):
        verify = _read("verify-release.ps1")
        self.assertIn("'um-cloud-deploy/rollback-drill.sh'", verify)


class HealthGatePortTests(unittest.TestCase):
    def test_update_and_rollback_expose_the_target_and_current_ports(self):
        for name in ("update.sh", "rollback.sh"):
            text = _read(name)
            with self.subTest(script=name):
                self.assertIn("--health-port", text)
                self.assertIn("--current-health-port", text)
                self.assertIn(
                    'HEALTH_PORT="${TARGET_PORT:-$GATEWAY_PORT_DEFAULT}"', text)
                self.assertIn(
                    'CURRENT_HEALTH_PORT="${CURRENT_TARGET_PORT:-$GATEWAY_PORT_DEFAULT}"',
                    text)
                self.assertIn('health_gate "$HEALTH_PORT"', text)
                self.assertIn('health_gate "$CURRENT_HEALTH_PORT"', text)
                self.assertNotRegex(
                    text, r"wait_port_open 127\.0\.0\.1 8100\b",
                    "the gateway port is still hardcoded")
                self.assertNotRegex(
                    text, r"health_gate\s*(?:\|\||$)",
                    "the health gate still uses the hardcoded default")

    def test_health_gate_probes_the_requested_gateway_port(self):
        with tempfile.TemporaryDirectory(prefix="health-port-") as tmp:
            root = Path(tmp)
            etc = root / "etc"
            etc.mkdir()
            (etc / "console.env").write_text(
                "USAGESRV_PORT=8096\n", encoding="utf-8")
            trace = root / "trace"
            trace.write_text("", encoding="utf-8")
            script = root / "probe.sh"
            script.write_text(
                'set -euo pipefail\n'
                '. "$DEPLOY_LIB/common.sh"\n'
                'curl() {\n'
                '  local url="" arg\n'
                '  for arg in "$@"; do\n'
                '    case "$arg" in http*) url="$arg" ;; esac\n'
                '  done\n'
                '  printf "%s\\n" "$url" >> "$TRACE"\n'
                '  printf "200"\n'
                '}\n'
                'health_gate "$PORT"\n',
                encoding="utf-8", newline="\n")
            env = dict(os.environ,
                        DEPLOY_LIB=_posix(DEPLOY / "lib"),
                        UM_ETC=_posix(etc),
                        TRACE=_posix(trace))
            for port, expected in (("8003", ":8003/v1/models"),
                                   ("8100", ":8100/v1/models")):
                with self.subTest(port=port):
                    trace.write_text("", encoding="utf-8")
                    run_env = dict(env, PORT=port)
                    proc = subprocess.run([BASH, _posix(script)],
                                          capture_output=True, text=True,
                                          env=run_env, timeout=30)
                    self.assertEqual(0, proc.returncode, proc.stderr)
                    urls = trace.read_text(encoding="utf-8")
                    self.assertIn(expected, urls)
                    self.assertIn(":8096/", urls)

    def test_the_default_gateway_port_is_the_python_release(self):
        proc = subprocess.run(
            [BASH, "-c",
             '. "$1"; printf "%s\\n" "$GATEWAY_PORT_DEFAULT"',
             BASH, _posix(DEPLOY / "lib" / "common.sh")],
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("8100", proc.stdout.strip())

    def test_a_bad_health_port_is_refused(self):
        for value in ("", "abc", "0", "65536"):
            with self.subTest(value=value):
                proc = subprocess.run(
                    [BASH, "-c",
                     '. "$1"; validate_port --health-port "$2"',
                     BASH, _posix(DEPLOY / "lib" / "common.sh"), value],
                    capture_output=True, text=True)
                self.assertEqual(64, proc.returncode, proc.stderr)
                self.assertIn("--health-port must be 1..65535", proc.stderr)


def _python314_works() -> bool:
    try:
        return subprocess.run(
            ["python3.14", "--version"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class RollbackDrillTests(unittest.TestCase):
    def test_plan_stops_rust_starts_python_and_verifies_ready(self):
        proc = subprocess.run(
            [BASH, _posix(DEPLOY / "rollback-drill.sh"),
             "--to", "s2-python-test",
             "--health-port", "8100",
             "--current-health-port", "8003"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        plan = proc.stdout
        self.assertIn("--to s2-python-test --health-port 8100 "
                      "--current-health-port 8003 --no-edge", plan)
        self.assertIn("systemctl stop um-share-gateway.service", plan)
        self.assertIn("wait_port_open 127.0.0.1 8100", plan)
        self.assertIn("http://127.0.0.1:8100/ready (require 200)", plan)
        self.assertIn("http://127.0.0.1:8100/v1/models", plan)
        for path in ("/var/lib/um-share", "/var/lib/um-share-console",
                     "/etc/um-share"):
            self.assertIn(path, plan)
        self.assertNotIn("rm -rf", plan)
        self.assertIn("require broker unit ReadWritePaths "
                      "/var/lib/um-share-console", plan)

    @unittest.skipUnless(
        _python314_works(),
        "python3.14 is not installed; the rollback drill is not performed (2026-11-19 decision)",
    )
    def test_drill_stops_rust_starts_python_and_verifies_ready(self):
        with tempfile.TemporaryDirectory(prefix="rollback-drill-") as tmp:
            root = Path(tmp)
            releases = root / "releases"
            release = releases / "python"
            units = release / "um-cloud-deploy" / "systemd"
            units.mkdir(parents=True)
            (units / "um-share-broker.service").write_text(
                _read("systemd/um-share-broker.service"),
                encoding="utf-8", newline="\n")
            (release / "RELEASE-MANIFEST.json").write_text(
                "{}\n", encoding="utf-8")
            data_dirs = [root / "var", root / "console", root / "etc"]
            for path in data_dirs:
                path.mkdir()
            fake_rollback = root / "rollback.sh"
            fake_rollback.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'rollback %s\\n' \"$*\" >> \"$TRACE\"\n",
                encoding="utf-8", newline="\n")
            fake_rollback.chmod(0o755)
            trace = root / "trace"
            trace.write_text("", encoding="utf-8")
            driver = root / "driver.sh"
            driver.write_text(
                'set -uo pipefail\n'
                '. "$DRILL"\n'
                'set +e\n'
                'TARGET_VERSION="python"\n'
                'HEALTH_PORT="8100"\n'
                'CURRENT_HEALTH_PORT="8003"\n'
                'READY_PATH="/ready"\n'
                'READY_TIMEOUT="1"\n'
                'ACTIVE=1\n'
                'unit_active() { [ "$ACTIVE" = "1" ]; }\n'
                'systemctl() {\n'
                '  printf "systemctl %s\\n" "$*" >> "$TRACE"\n'
                '  if [ "$1" = "stop" ]; then ACTIVE=0; fi\n'
                '  return 0\n'
                '}\n'
                'curl() {\n'
                '  local url="" arg\n'
                '  for arg in "$@"; do\n'
                '    case "$arg" in http*) url="$arg" ;; esac\n'
                '  done\n'
                '  printf "curl %s\\n" "$url" >> "$TRACE"\n'
                '  printf "200"\n'
                '}\n'
                'wait_port_open() {\n'
                '  printf "wait-open %s %s\\n" "$1" "$2" >> "$TRACE"\n'
                '  return 0\n'
                '}\n'
                'wait_ready_http() {\n'
                '  printf "wait-ready %s %s %s\\n" "$1" "$2" "$3" >> "$TRACE"\n'
                '  return 0\n'
                '}\n'
                'RUST_UNITS=(um-share-gateway.service)\n'
                'DATA_DIRS=("$UM_VAR" "$UM_CONSOLE_VAR" "$UM_ETC")\n'
                'drill_execute\n',
                encoding="utf-8", newline="\n")
            env = dict(
                os.environ,
                DRILL=_posix(DEPLOY / "rollback-drill.sh"),
                TRACE=_posix(trace),
                UM_RELEASES=_posix(releases),
                UM_VAR=_posix(data_dirs[0]),
                UM_CONSOLE_VAR=_posix(data_dirs[1]),
                UM_ETC=_posix(data_dirs[2]),
                TARGET_VERSION="python",
                HEALTH_PORT="8100",
                CURRENT_HEALTH_PORT="8003",
                READY_PATH="/ready",
                READY_TIMEOUT="1",
                UM_HEALTH_TIMEOUT_S="1",
                ROLLBACK_DRILL_SCRIPT=_posix(fake_rollback),
                EX_OK="0",
                EX_HEALTH="68",
                EX_ROLLBACK="69",
                EX_OSERR="71")
            proc = subprocess.run([BASH, _posix(driver)],
                                  capture_output=True, text=True, env=env,
                                  timeout=60)
            self.assertEqual(0, proc.returncode, proc.stderr)
            lines = trace.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines)
            self.assertTrue(lines[0].startswith("rollback "), lines)
            self.assertIn("--to python --health-port 8100 "
                          "--current-health-port 8003 --no-edge", lines[0])
            stop = next(i for i, line in enumerate(lines)
                        if line == "systemctl stop um-share-gateway.service")
            wait = next(i for i, line in enumerate(lines)
                        if line.startswith("wait-open 127.0.0.1 8100"))
            ready = next(i for i, line in enumerate(lines)
                         if line.startswith("wait-ready 8100 /ready "))
            models = next(i for i, line in enumerate(lines)
                          if "http://127.0.0.1:8100/v1/models" in line)
            self.assertLess(0, stop)
            self.assertLess(stop, wait)
            self.assertLess(wait, ready)
            self.assertLess(ready, models)
            self.assertIn("drill ok: release=python gateway_port=8100",
                          proc.stdout)
            self.assertNotIn("rm -rf", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
