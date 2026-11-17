# -*- coding: utf-8 -*-
"""#70: um-cloud-deploy packages and switches the Rust 8003/8002 release.

The Python release stays the rollback target (8100/8096).  The Rust release
ships ``bin/um-gateway`` plus ``um-share-gateway.service``; its console unit
listens on 8002 and its display on 8097.  ``install.sh``/``update.sh``/
``rollback.sh`` read the runtime frozen in ``RELEASE-MANIFEST.json`` instead of
hardcoding 8100.

The textual tests read the shipped units/scripts.  The shell tests run the real
``lib/common.sh`` helpers against temporary release trees and env files; no unit,
service, release or production path is touched.
"""
import json
import os
import shutil
import subprocess
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


class RustReleaseMaterialTests(unittest.TestCase):
    def test_gateway_unit_runs_the_rust_production_entry(self):
        unit = _read("systemd/um-share-gateway.service")
        for value in ("--mode production", "--port 8003",
                      "--host-entry-kind native",
                      "--expect-instance-port 8003",
                      "/opt/um-share/bin/um-gateway",
                      "--expect-instance-cmdline-marker um-gateway"):
            self.assertIn(value, unit)
        for old in ("8100", "8096", "18096"):
            self.assertNotIn(old, unit, f"the Rust unit names {old}")
        for path in ("/var/lib/um-share", "/var/lib/um-share-console",
                     "/run/um-share", "/var/log/um-share"):
            self.assertIn(path, unit)
        self.assertIn(
            "ReadWritePaths=/var/lib/um-share /run/um-share "
            "/var/log/um-share /var/lib/um-share-console", unit)
        self.assertIn("--check-deployment-assembly broker", unit)
        self.assertIn("um-share-gatewayctl.py", unit)

    def test_console_unit_runs_the_rust_production_entry(self):
        unit = _read("systemd/um-share-console.service")
        self.assertIn(
            "ExecStart=/opt/um-share/bin/um-console "
            "--mode production --bind-port 8002", unit)
        self.assertIn("ConditionPathExists=/opt/um-share/bin/um-console", unit)
        self.assertNotIn(
            "ConditionPathExists=/opt/um-share/current/usagesrv.py", unit)
        exec_starts = [line for line in unit.splitlines()
                       if line.startswith("ExecStart=")]
        self.assertEqual(
            ["ExecStart=/opt/um-share/bin/um-console "
             "--mode production --bind-port 8002"], exec_starts)

    def test_console_unit_targets_8002_and_8003(self):
        unit = _read("systemd/um-share-console.service")
        self.assertIn("Environment=USAGESRV_PORT=8002", unit)
        self.assertIn("Environment=UM_SHARE_GATEWAY_PORT=8003", unit)
        self.assertIn("NoExecPaths=-/opt/um-share/bin/um-gateway", unit)
        for old in ("8096", "18096"):
            self.assertNotIn(old, unit, f"the console unit names {old}")

    def test_python_console_unit_keeps_8096_and_the_rust_unit(self):
        unit = _read("systemd/um-share-console-python.service")
        self.assertIn("Environment=USAGESRV_PORT=8096", unit)
        self.assertIn("Environment=UM_SHARE_GATEWAY_PORT=8100", unit)
        self.assertIn(
            "ExecStart=/opt/um-share/venv/bin/python "
            "/opt/um-share/current/usagesrv.py", unit)
        self.assertIn(
            "--check-deployment-assembly console "
            "/etc/systemd/system/um-share-console-python.service", unit)
        self.assertIn("NoExecPaths=-/opt/um-share/bin/um-gateway", unit)
        self.assertIn(
            "ReadWritePaths=/var/lib/um-share-console "
            "/var/log/um-share-console", unit)
        self.assertIn("After=network-online.target "
                      "um-share-broker.service", unit)

    def test_python_release_ships_both_console_units(self):
        package = _read("package-release.sh")
        python_branch = package[package.index(
            "# The Python rollback release ships"):]
        self.assertNotIn(
            'install -m 0644 "$PAYLOAD/um-cloud-deploy/systemd/'
            'um-share-console-python.service" \\\n'
            '    "$PAYLOAD/um-cloud-deploy/systemd/'
            'um-share-console.service"', python_branch)
        self.assertNotIn(
            'rm -f -- "$PAYLOAD/um-cloud-deploy/systemd/'
            'um-share-console-python.service"', python_branch)
        self.assertNotIn(
            'rm -f -- "$PAYLOAD/um-cloud-deploy/systemd/'
            'um-share-console.service"', python_branch)
        self.assertIn(
            'rm -f -- "$PAYLOAD/um-cloud-deploy/bin/um-console"',
            python_branch)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "um_cloud_make_manifest_for_console",
            DEPLOY / "make-manifest.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        import sys
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        paths = {entry["path"] for entry in
                 module._deployment_files(HERE, DEPLOY)}
        for unit in ("um-share-console.service",
                     "um-share-console-python.service"):
            self.assertIn("um-cloud-deploy/systemd/" + unit, paths)

    def test_install_selects_the_runtime_console_unit_and_health_port(self):
        install = _read("install.sh")
        self.assertIn(
            'TARGET_CONSOLE_UNIT="$(release_console_unit "$TARGET_RUNTIME")"',
            install)
        self.assertIn(
            'TARGET_CONSOLE_PORT="$(release_console_port "$TARGET_RUNTIME")"',
            install)
        self.assertIn(
            'systemctl enable "$TARGET_GATEWAY_UNIT" "$TARGET_CONSOLE_UNIT"',
            install)
        self.assertIn('systemctl restart "$TARGET_CONSOLE_UNIT"', install)
        self.assertIn(
            'wait_port_open 127.0.0.1 "$TARGET_CONSOLE_PORT"', install)
        self.assertIn(
            'health_gate "$HEALTH_PORT" "$TARGET_CONSOLE_PORT"', install)
        self.assertIn('"$UM_SYSTEMD_DIR/$TARGET_CONSOLE_UNIT"', install)
        self.assertNotIn("systemctl restart um-share-console.service", install)
        self.assertNotIn("wait_port_open 127.0.0.1 8100", install)

    def test_update_and_rollback_select_current_and_target_console_units(self):
        for name in ("update.sh", "rollback.sh"):
            text = _read(name)
            with self.subTest(script=name):
                self.assertIn(
                    'CURRENT_CONSOLE_UNIT="$(release_console_unit "$CURRENT_RUNTIME")"',
                    text)
                self.assertIn(
                    'TARGET_CONSOLE_UNIT="$(release_console_unit "$TARGET_RUNTIME")"',
                    text)
                self.assertIn(
                    'CURRENT_CONSOLE_PORT="$(release_console_port "$CURRENT_RUNTIME")"',
                    text)
                self.assertIn(
                    'TARGET_CONSOLE_PORT="$(release_console_port "$TARGET_RUNTIME")"',
                    text)
                self.assertIn('systemctl start "$TARGET_CONSOLE_UNIT"', text)
                self.assertIn('systemctl start "$CURRENT_CONSOLE_UNIT"', text)
                self.assertIn('systemctl stop "$CURRENT_CONSOLE_UNIT"', text)
                self.assertIn('systemctl stop "$TARGET_CONSOLE_UNIT"', text)
                self.assertIn(
                    'wait_port_open 127.0.0.1 "$TARGET_CONSOLE_PORT"', text)
                self.assertIn(
                    'wait_port_open 127.0.0.1 "$CURRENT_CONSOLE_PORT"', text)
                self.assertIn(
                    'health_gate "$HEALTH_PORT" "$TARGET_CONSOLE_PORT"', text)
                self.assertIn(
                    'health_gate "$CURRENT_HEALTH_PORT" '
                    '"$CURRENT_CONSOLE_PORT"', text)
                self.assertNotIn('systemctl start "$CONSOLE_UNIT"', text)
                self.assertNotIn('systemctl stop "$CONSOLE_UNIT"', text)

    def test_health_gate_uses_the_explicit_console_port(self):
        with tempfile.TemporaryDirectory(prefix="health-console-") as tmp:
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
                'health_gate 8003 8002\n',
                encoding="utf-8", newline="\n")
            env = dict(os.environ,
                        DEPLOY_LIB=_posix(DEPLOY / "lib"),
                        UM_ETC=_posix(etc),
                        TRACE=_posix(trace))
            proc = subprocess.run([BASH, _posix(script)],
                                  capture_output=True, text=True,
                                  env=env, timeout=30)
            self.assertEqual(0, proc.returncode, proc.stderr)
            urls = trace.read_text(encoding="utf-8")
            self.assertIn(":8002/", urls)
            self.assertIn(":8003/v1/models", urls)
            self.assertNotIn(":8096/", urls)

    def test_console_unit_keeps_the_8002_fences(self):
        unit = _read("systemd/um-share-console.service")
        self.assertIn("EnvironmentFile=/etc/um-share/console.env", unit)
        self.assertIn(
            "ExecStartPre=/opt/um-share/venv/bin/python "
            "/opt/um-share/current/um-cloud-deploy/backup_restore.py "
            "--check-deployment-assembly console "
            "/etc/systemd/system/um-share-console.service "
            "/etc/um-share/console.env", unit)
        self.assertIn("ReadOnlyPaths=-/run/um-share", unit)
        self.assertIn(
            "ReadWritePaths=/var/lib/um-share-console "
            "/var/log/um-share-console", unit)
        self.assertIn(
            "NoExecPaths=-/opt/um-share/current/chat2api/chat2api.py", unit)
        self.assertIn("NoExecPaths=-/opt/um-share/bin/um-gateway", unit)
        self.assertNotIn("NoExecPaths=-/opt/um-share/bin/um-console", unit)

    def test_display_unit_targets_8097_and_the_gateway_namespace(self):
        unit = _read("systemd/um-share-display-rust.service")
        self.assertIn("UM_VNC_PORT=8097", unit)
        self.assertIn("JoinsNamespaceOf=um-share-gateway.service", unit)
        self.assertIn("PartOf=um-share-gateway.service", unit)
        for old in ("8100", "8096", "18096", "5901"):
            self.assertNotIn(old, unit, f"the Rust display unit names {old}")

    def test_gateway_env_is_the_rust_target(self):
        env = _read("config/gateway.env.example")
        self.assertIn("UM_SHARE_GATEWAY_PORT=8003", env)
        self.assertIn("UM_SHARE_GATEWAY_ENTRY_KIND=native", env)
        self.assertIn("UM_SHARE_GATEWAY_MODE=production", env)
        for old in ("8100", "8096", "18096"):
            self.assertNotIn(old, env)

    def test_console_env_is_the_rust_target(self):
        env = _read("config/console.env.example")
        self.assertIn("USAGESRV_PORT=8002", env)
        self.assertIn("UM_SHARE_GATEWAY_PORT=8003", env)
        self.assertIn("CONSOLE_DISPLAY_VNC_PORT=8097", env)
        for old in ("8100", "8096", "18096"):
            self.assertNotIn(old, env, f"the Rust console env names {old}")

    def test_python_rollback_env_keeps_the_old_ports(self):
        broker = _read("config/broker-python.env.example")
        console = _read("config/console-python.env.example")
        self.assertIn("UM_SHARE_GATEWAY_PORT=8100", broker)
        self.assertIn("USAGESRV_PORT=8096", console)
        self.assertIn("UM_SHARE_GATEWAY_PORT=8100", console)

    def test_ingress_and_port_audit_target_8003(self):
        ingress = _read("cloudflared/check-ingress.py")
        self.assertIn('API_SERVICE = "http://127.0.0.1:8003"', ingress)
        for port in ("8002", "8100", "8096", "18096", "5901"):
            self.assertIn(f'"{port}"', ingress)
        config = _read("config/cloudflared.yml.example")
        self.assertIn("service: http://127.0.0.1:8003", config)
        self.assertNotIn("service: http://127.0.0.1:8100", config)
        import check_port_bindings as audit
        self.assertEqual(("um-share gateway", "loopback"),
                         audit.PORT_ALLOCATIONS[8003])
        self.assertEqual(("usagesrv cloud console backend", "loopback"),
                         audit.PORT_ALLOCATIONS[8002])
        self.assertTrue(audit.is_governed(8002))
        self.assertTrue(audit.is_governed(8003))


class RustReleaseScriptTests(unittest.TestCase):
    def test_package_release_carries_the_rust_binary_and_runtime(self):
        package = _read("package-release.sh")
        self.assertIn("--runtime RUNTIME", package)
        self.assertIn("--rust-bin PATH", package)
        self.assertIn('install -m 0755 "$RUST_BIN"', package)
        self.assertIn('"$PAYLOAD/um-cloud-deploy/bin/um-gateway"', package)
        self.assertIn("--runtime \"$RUNTIME\"", package)
        self.assertIn(
            'require_share_backend_write_path \\\n'
            '  "$PAYLOAD/um-cloud-deploy/systemd/um-share-gateway.service"',
            package)
        self.assertLess(
            package.index('"$PAYLOAD/um-cloud-deploy/systemd/um-share-gateway.service"'),
            package.index('"$PYTHON" "$PAYLOAD/um-cloud-deploy/make-manifest.py"'))

    def test_make_manifest_records_the_runtime(self):
        manifest = _read("make-manifest.py")
        self.assertIn('RUNTIMES = ("python", "rust")', manifest)
        self.assertIn('parser.add_argument("--runtime"', manifest)
        self.assertIn('"runtime": getattr(args, "runtime", "python")', manifest)

    def test_install_selects_the_runtime_unit_set_and_port(self):
        install = _read("install.sh")
        self.assertIn('TARGET_RUNTIME="$(release_runtime "$TARGET")"', install)
        self.assertIn('mapfile -t TARGET_UNITS < <(release_units "$TARGET_RUNTIME")', install)
        self.assertIn('HEALTH_PORT="$(release_gateway_port "$TARGET_RUNTIME")"', install)
        self.assertIn('install_units "$TARGET" "${TARGET_UNITS[@]}"', install)
        self.assertIn('"$TARGET/um-cloud-deploy/systemd/um-share-gateway.service"',
                      install)
        self.assertIn('systemctl restart "$TARGET_GATEWAY_UNIT"', install)
        self.assertIn('wait_port_open 127.0.0.1 "$HEALTH_PORT"', install)
        self.assertIn('health_gate "$HEALTH_PORT"', install)

    def test_update_and_rollback_follow_the_release_runtimes(self):
        for name in ("update.sh", "rollback.sh"):
            text = _read(name)
            with self.subTest(script=name):
                self.assertIn('release_runtime "$CURRENT_TARGET"', text)
                self.assertIn('release_runtime "$TARGET"', text)
                self.assertIn("CURRENT_GATEWAY_UNIT", text)
                self.assertIn("TARGET_GATEWAY_UNIT", text)
                self.assertIn("sync_runtime_env", text)
                self.assertIn("release_gateway_port", text)
                self.assertNotRegex(
                    text, r"systemctl (?:start|stop) um-share-broker\.service",
                    "a runtime switch still hardcodes the Python broker unit")
                self.assertNotRegex(
                    text, r"wait_port_open 127\.0\.0\.1 8100\b",
                    "the health gate still hardcodes 8100")

    def test_verify_release_is_runtime_aware(self):
        verify = _read("verify-release.ps1")
        self.assertIn("$manifest.runtime", verify)
        self.assertIn("'um-cloud-deploy/bin/um-gateway'", verify)
        self.assertIn("'um-cloud-deploy/bin/um-console'", verify)
        self.assertIn("'um-cloud-deploy/systemd/um-share-gateway.service'", verify)
        self.assertIn("'um-cloud-deploy/systemd/um-share-broker.service'", verify)
        self.assertIn(
            "'um-cloud-deploy/systemd/um-share-console-python.service'", verify)


class RuntimeHelperTests(unittest.TestCase):
    def _run_helper(self, body, releases, etc=None):
        with tempfile.TemporaryDirectory(prefix="rust-release-helper-") as tmp:
            root = Path(tmp)
            etc_path = etc or (root / "etc")
            script = root / "helper.sh"
            script.write_text(
                'set -euo pipefail\n'
                '. "$DEPLOY_LIB/common.sh"\n'
                'RELEASE="$RELEASES/release"\n' + body + "\n",
                encoding="utf-8", newline="\n")
            env = dict(os.environ,
                        DEPLOY_LIB=_posix(DEPLOY / "lib"),
                        RELEASES=_posix(releases),
                        UM_ETC=_posix(etc_path))
            proc = subprocess.run([BASH, _posix(script)], capture_output=True,
                                  text=True, env=env, timeout=30)
            return proc

    def _release(self, root, runtime=None, gateway=False, broker=False,
                 console=False, console_python=False):
        release = root / "release"
        units = release / "um-cloud-deploy" / "systemd"
        units.mkdir(parents=True)
        if gateway:
            (units / "um-share-gateway.service").write_text(
                _read("systemd/um-share-gateway.service"),
                encoding="utf-8", newline="\n")
        if broker:
            (units / "um-share-broker.service").write_text(
                _read("systemd/um-share-broker.service"),
                encoding="utf-8", newline="\n")
        if console:
            (units / "um-share-console.service").write_text(
                _read("systemd/um-share-console.service"),
                encoding="utf-8", newline="\n")
        if console_python:
            (units / "um-share-console-python.service").write_text(
                _read("systemd/um-share-console-python.service"),
                encoding="utf-8", newline="\n")
        if runtime is not None:
            (release / "RELEASE-MANIFEST.json").write_text(
                json.dumps({"runtime": runtime}), encoding="utf-8")
        return release

    def test_manifest_runtime_selects_unit_and_port(self):
        with tempfile.TemporaryDirectory(prefix="rust-release-helper-") as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            self._release(releases, runtime="rust", gateway=True)
            proc = self._run_helper(
                'printf "%s %s %s\\n" "$(release_runtime "$RELEASE")" '
                '"$(release_gateway_unit "$(release_runtime "$RELEASE")")" '
                '"$(release_gateway_port "$(release_runtime "$RELEASE")")"',
                releases)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("rust um-share-gateway.service 8003",
                             proc.stdout.strip())

    def test_unit_evidence_is_the_legacy_fallback(self):
        with tempfile.TemporaryDirectory(prefix="rust-release-helper-") as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            self._release(releases, gateway=True)
            proc = self._run_helper(
                'printf "%s\\n" "$(release_runtime "$RELEASE")"', releases)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("rust", proc.stdout.strip())

    def test_python_release_keeps_8100_and_8096(self):
        with tempfile.TemporaryDirectory(prefix="rust-release-helper-") as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            self._release(releases, runtime="python", broker=True)
            proc = self._run_helper(
                'printf "%s %s\\n" "$(release_gateway_port python)" '
                '"$(release_console_port python)"', releases)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("8100 8096", proc.stdout.strip())

    def test_runtime_selects_the_console_unit_and_unit_set(self):
        with tempfile.TemporaryDirectory(prefix="rust-release-helper-") as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            self._release(releases, runtime="rust", gateway=True,
                           console=True, console_python=True)
            proc = self._run_helper(
                'printf "%s\\n" "$(release_console_unit rust)" '
                '"$(release_console_unit python)" '
                '"$(release_console_port rust)" '
                '"$(release_console_port python)"\n'
                'release_units rust\n'
                'printf "%s\\n" ---\n'
                'release_units python', releases)
            self.assertEqual(0, proc.returncode, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(
                ["um-share-console.service",
                 "um-share-console-python.service",
                 "8002", "8096"], lines[:4])
            self.assertEqual(
                ["um-share-gateway.service",
                 "um-share-console.service",
                 "um-share-display.service"], lines[4:7])
            self.assertEqual(
                ["um-share-broker.service",
                 "um-share-console-python.service",
                 "um-share-display.service"], lines[8:11])

    def test_sync_runtime_env_rewrites_only_the_port_keys(self):
        with tempfile.TemporaryDirectory(prefix="rust-release-env-") as tmp:
            root = Path(tmp)
            etc = root / "etc"
            etc.mkdir()
            (etc / "console.env").write_text(
                "USAGESRV_PORT=8096\nUM_SHARE_GATEWAY_PORT=8100\n"
                "CONSOLE_DISPLAY_VNC_PORT=8097\nSECRET=keep\n",
                encoding="utf-8")
            (etc / "gateway.env").write_text(
                "UM_SHARE_GATEWAY_PORT=8100\nUM_SHARE_GATEWAY_ENTRY_KIND=python\n",
                encoding="utf-8")
            releases = root / "releases"
            releases.mkdir()
            self._release(releases, runtime="rust", gateway=True)
            proc = self._run_helper(
                'sync_runtime_env rust\n'
                'printf "%s\\n" "$(cat "$UM_ETC/console.env")"\n'
                'printf "%s\\n" "$(cat "$UM_ETC/gateway.env")"',
                releases, etc=etc)
            self.assertEqual(0, proc.returncode, proc.stderr)
            output = proc.stdout
            self.assertIn("USAGESRV_PORT=8002", output)
            self.assertIn("UM_SHARE_GATEWAY_PORT=8003", output)
            self.assertIn("UM_SHARE_GATEWAY_ENTRY_KIND=native", output)
            self.assertIn("UM_SHARE_GATEWAY_MODE=production", output)
            self.assertIn("SECRET=keep", output)
            self.assertNotIn("USAGESRV_PORT=8096", output)
            self.assertNotIn("UM_SHARE_GATEWAY_PORT=8100", output)


if __name__ == "__main__":
    unittest.main()
