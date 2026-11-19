# -*- coding: utf-8 -*-
"""Ticket #14：云端 display unit 安装但默认不 enable，人工接管按需启动。

部署 seam 是静态 shell/systemd 材料与脚本行为：默认安装只写 unit，不 enable；
按需路径由操作员显式执行 `manual-takeover.sh start`，脚本只 `systemctl start`，
从不 `enable`；默认 headless 登录不依赖 `DISPLAY`/`Xvfb`。真实 systemd 与真实
UM 登录仍留待目标机验证。
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "um-cloud-deploy"
DISPLAY_DIR = DEPLOY / "display"
INSTALL = DEPLOY / "install.sh"
CONSOLE_ENV = DEPLOY / "config" / "console.env.example"
CONSOLE_UNIT = DEPLOY / "systemd" / "um-share-console.service"
DISPLAY_UNIT = DEPLOY / "systemd" / "um-share-display.service"
BROKER_UNIT = DEPLOY / "systemd" / "um-share-broker.service"
START_DISPLAY = DISPLAY_DIR / "start-loopback-display.sh"
DISPLAY_README = DISPLAY_DIR / "README.md"
DEPLOY_README = DEPLOY / "README.md"
TAKEOVER = DISPLAY_DIR / "manual-takeover.sh"
BASH = shutil.which("bash") or "bash"


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _posix(path):
    """Return the path the Git Bash that runs this harness sees."""
    text = str(Path(path).resolve()).replace("\\", "/")
    if len(text) > 1 and text[1:2] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


class InstallDefaultTests(unittest.TestCase):
    def test_default_install_does_not_enable_display(self):
        install = _read(INSTALL).replace("\r\n", "\n")
        self.assertIn("WITH_DISPLAY=0", install)
        gate = install.index('if [ "$WITH_DISPLAY" -eq 1 ]; then')
        enable = install.index(
            'systemctl enable --now "$DISPLAY_UNIT"')
        self.assertGreater(enable, gate)
        self.assertEqual(
            1,
            install.count('systemctl enable --now "$DISPLAY_UNIT"'),
            "display must only be enabled by the explicit --with-display opt-in")
        self.assertLess(
            enable - gate,
            install[gate:].index("\n  fi\n"),
            "display enable is not inside the opt-in branch")

    def test_display_unit_is_installed_even_when_not_enabled(self):
        install = _read(INSTALL)
        self.assertIn("um-share-display.service", install)
        unit = _read(DISPLAY_UNIT)
        self.assertIn("[Install]", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("um-share-display.service", _read(CONSOLE_UNIT))

    def test_update_and_rollback_never_enable_display(self):
        for name in ("update.sh", "rollback.sh"):
            with self.subTest(script=name):
                text = _read(DEPLOY / name)
                self.assertNotIn(
                    "systemctl enable um-share-display.service", text)
                self.assertNotIn(
                    "systemctl enable --now um-share-display.service", text)
                self.assertIn("install_units", text)

    def test_default_console_env_does_not_depend_on_a_display(self):
        example = _read(CONSOLE_ENV)
        self.assertIn("CONSOLE_DISPLAY_ENABLED=0", example)
        self.assertNotIn("DISPLAY=:99", example)
        self.assertNotIn("XAUTHORITY=", example)

    def test_default_login_path_uses_headless_without_a_display(self):
        self.skipTest("Python closure retired (#90); the source/generator is frozen")
        chat2api = _read(ROOT / "chat2api" / "chat2api.py")
        start = chat2api.index("def browser_login(")
        end = chat2api.index("\ndef ", start + 1)
        body = chat2api[start:end]
        # 默认由运行时 display state 决定；没有人工 display 时 headless。
        self.assertIn("_login_display", body)
        self.assertIn('"headless": not headful', body)
        self.assertNotIn("display_missing", body)

    def test_broker_declares_the_optional_display_path(self):
        broker = _read(BROKER_UNIT)
        # 8100 子进程的浏览器启动需要 DISPLAY/XAUTHORITY；默认 headless
        # 不连接它们，只有 display 运行时状态完整时 browser_login 才用。
        self.assertIn("Environment=DISPLAY=:99", broker)
        self.assertIn(
            "Environment=XAUTHORITY=/run/um-share/display.auth", broker)

    def test_display_unit_shares_the_broker_private_tmp(self):
        unit = _read(DISPLAY_UNIT)
        broker = _read(BROKER_UNIT)
        # Xvfb 的 /tmp/.X11-unix socket 必须对 8100 子进程可见，否则
        # headful 浏览器无法连接人工 display。
        self.assertIn("JoinsNamespaceOf=um-share-broker.service", unit)
        self.assertIn("PrivateTmp=true", unit)
        self.assertIn("PrivateTmp=true", broker)

    def test_display_script_publishes_the_login_display_state(self):
        script = _read(START_DISPLAY)
        for line in ("UM_LOGIN_DISPLAY=", "UM_LOGIN_XAUTHORITY=",
                     "UM_LOGIN_DISPLAY_SOCKET=", "UM_LOGIN_DISPLAY_PID=",
                     "login-display.env"):
            self.assertIn(line, script)

    def test_manual_takeover_waits_for_and_clears_the_display_state(self):
        script = _read(TAKEOVER)
        self.assertIn("login-display.env", script)
        self.assertIn("UM_LOGIN_DISPLAY_FILE", script)
        self.assertIn("rm -f", script)

    def test_documented_install_does_not_enable_display(self):
        readme = _read(DEPLOY_README)
        section = readme[readme.index("## 安装、更新、回滚"):]
        block = section[section.index("```bash"):]
        block = block[:block.index("```", 3)]
        self.assertNotIn("--with-display", block)
        self.assertIn("manual-takeover.sh start", readme)
        self.assertIn("默认不 enable", readme)


class ManualTakeoverScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="manual-takeover-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.vnc = root / "vnc.passwd"
        self.vnc.write_text("synthetic-secret\n", encoding="utf-8")
        self.env_file = root / "console.env"
        self.env_file.write_text(
            "UM_SHARE_ROLE=cloud\nCONSOLE_DISPLAY_ENABLED=0\n",
            encoding="utf-8")
        self.trace = root / "trace"
        self.display_state = root / "login-display.env"
        self._stub(
            "id",
            'if [ "$1" = "-u" ]; then echo "${FAKE_UID:-0}"; exit 0; fi\n'
            'exec /usr/bin/id "$@"\n')
        self._stub(
            "stat",
            'if [ "$1" = "-c" ] && [ "$2" = "%u" ]; then\n'
            '  echo "${FAKE_OWNER:-0}"; exit 0\n'
            'fi\n'
            'if [ "$1" = "-c" ] && [ "$2" = "%a" ]; then\n'
            '  echo "${FAKE_MODE:-600}"; exit 0\n'
            'fi\n'
            'exec /usr/bin/stat "$@"\n')
        self._stub(
            "systemctl",
            'printf "systemctl %s\\n" "$*" >> "$TRACE"\n'
            'if [ "$1" = "start" ] && [ "$2" = "um-share-display.service" ]; then\n'
            '  printf "UM_LOGIN_DISPLAY=:99\\n'
            'UM_LOGIN_XAUTHORITY=/run/um-share/display.auth\\n'
            'UM_LOGIN_DISPLAY_SOCKET=/tmp/.X11-unix/X99\\n'
            'UM_LOGIN_DISPLAY_PID=%s\\n" "$$" > "$DISPLAY_STATE"\n'
            'fi\n'
            'if [ "$1" = "stop" ] && [ "$2" = "um-share-display.service" ]; then\n'
            '  rm -f "$DISPLAY_STATE"\n'
            'fi\n'
            'exit 0\n')

    def _stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _run(self, *args, uid="0", owner="0", mode="600"):
        env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            TRACE=_posix(self.trace),
            UM_VNC_PASSWD_FILE=_posix(self.vnc),
            UM_CONSOLE_ENV_FILE=_posix(self.env_file),
            UM_LOGIN_DISPLAY_FILE=_posix(self.display_state),
            DISPLAY_STATE=_posix(self.display_state),
            FAKE_UID=uid,
            FAKE_OWNER=owner,
            FAKE_MODE=mode,
        )
        return subprocess.run(
            [BASH, _posix(TAKEOVER), *args],
            capture_output=True, text=True, env=env)

    def _trace_text(self):
        return self.trace.read_text(encoding="utf-8") \
            if self.trace.exists() else ""

    def test_start_starts_display_without_enabling_it(self):
        proc = self._run("start")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        trace = self._trace_text()
        self.assertIn("systemctl start um-share-display.service", trace)
        self.assertIn("systemctl restart um-share-console-python.service", trace)
        self.assertNotIn("enable", trace)
        self.assertNotIn("disable", trace)
        env = _read(self.env_file)
        self.assertIn("CONSOLE_DISPLAY_ENABLED=1", env)
        self.assertEqual(1, env.count("CONSOLE_DISPLAY_ENABLED="))
        self.assertIn("UM_SHARE_ROLE=cloud", env)
        # 新登录轮次必须能从运行时状态看到可见 display。
        self.assertTrue(self.display_state.exists())
        self.assertIn("UM_LOGIN_DISPLAY=:99",
                      _read(self.display_state))

    def test_start_does_not_restart_console_when_already_enabled(self):
        self.env_file.write_text(
            "CONSOLE_DISPLAY_ENABLED=1\n", encoding="utf-8")
        proc = self._run("start")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        trace = self._trace_text()
        self.assertIn("systemctl start um-share-display.service", trace)
        self.assertNotIn("systemctl restart um-share-console-python.service", trace)

    def test_stop_stops_display_without_disabling_it(self):
        self.env_file.write_text(
            "CONSOLE_DISPLAY_ENABLED=1\n", encoding="utf-8")
        proc = self._run("stop")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        trace = self._trace_text()
        self.assertIn("systemctl stop um-share-display.service", trace)
        self.assertIn("systemctl restart um-share-console-python.service", trace)
        self.assertNotIn("enable", trace)
        self.assertNotIn("disable", trace)
        env = _read(self.env_file)
        self.assertIn("CONSOLE_DISPLAY_ENABLED=0", env)
        self.assertEqual(1, env.count("CONSOLE_DISPLAY_ENABLED="))
        # stop 后不能残留会让下一轮变成可见窗口的运行时状态。
        self.assertFalse(self.display_state.exists())

    def test_start_refuses_a_non_root_operator(self):
        proc = self._run("start", uid="1000")
        self.assertEqual(77, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("root", proc.stderr)
        self.assertFalse(self.trace.exists())
        self.assertIn("CONSOLE_DISPLAY_ENABLED=0", _read(self.env_file))

    def test_start_refuses_a_missing_vnc_password(self):
        self.vnc.unlink()
        proc = self._run("start")
        self.assertEqual(78, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(self.trace.exists())
        self.assertIn("vnc", proc.stderr.lower())

    def test_start_refuses_a_group_readable_vnc_password(self):
        proc = self._run("start", mode="644")
        self.assertEqual(77, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(self.trace.exists())

    def test_start_refuses_a_vnc_password_not_owned_by_root(self):
        proc = self._run("start", owner="1000")
        self.assertEqual(77, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(self.trace.exists())


class OnDemandDocumentationTests(unittest.TestCase):
    def test_display_readme_documents_the_on_demand_path(self):
        readme = _read(DISPLAY_README)
        self.assertIn("manual-takeover.sh start", readme)
        self.assertIn("manual-takeover.sh stop", readme)
        self.assertIn("systemctl start", readme)
        self.assertIn("systemctl stop", readme)
        self.assertIn("不 enable", readme)
        self.assertIn("CONSOLE_DISPLAY_ENABLED=1", readme)
        self.assertIn("目标机", readme)
        self.assertIn("可见浏览器", readme)
        self.assertIn("UM_LOGIN_DISPLAY", readme)

    def test_display_readme_states_the_default_path_needs_no_display(self):
        readme = _read(DISPLAY_README)
        self.assertIn("headless", readme)
        self.assertIn("默认路径", readme)
        self.assertIn("不依赖", readme)
        self.assertIn("新轮次", readme)


if __name__ == "__main__":
    unittest.main()
