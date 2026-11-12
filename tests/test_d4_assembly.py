# -*- coding: utf-8 -*-
"""D4-P assembly fixtures (handoff §60.4): the experiment namespace
contract, the instance-SDDL wiring chain and the share-side import closure.
Pure/offline: no pipe, task, ACL, network or filesystem side effects beyond
temp dirs. cwd-sensitive: run from the ai-gateway directory.
"""
import importlib
import importlib.util
import contextlib
import io
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

_repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo / "um-share-deploy"))

from share_guard import (EXPERIMENT_PIPE_PREFIX, PRODUCTION_PIPE_PREFIX,
                         build_experiment_share_args,
                         validate_experiment_guard_invocation)
import share_guard as guard
import share_instance_pipe

_spec = importlib.util.spec_from_file_location(
    "share_gateway", _repo / "chat2api" / "share_gateway.py")
share = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(share)


class BuildExperimentShareArgsTests(unittest.TestCase):
    def test_happy_path_exact_string(self):
        args = build_experiment_share_args(
            r"C:\ProgramData\UMShareExp-broker\instance-channel-key",
            r"C:\ProgramData\UMShareExp-broker\run\live\instance-registration.json",
            "D:P(A;;FR;;;S-1-5-18-exp)")
        self.assertEqual(
            args,
            '--instance-key-file "C:\\ProgramData\\UMShareExp-broker'
            '\\instance-channel-key" --instance-pipe-prefix "'
            '\\\\.\\pipe\\um-share-instance-exp-" --registration-file "'
            'C:\\ProgramData\\UMShareExp-broker\\run\\live\\instance-registration'
            '.json" --instance-pipe-sddl "D:P(A;;FR;;;S-1-5-18-exp)"')

    def test_production_prefix_refuses(self):
        with self.assertRaises(ValueError):
            build_experiment_share_args("k", "r", "D:P", pipe_prefix=PRODUCTION_PIPE_PREFIX)

    def test_non_experiment_prefix_refuses(self):
        with self.assertRaises(ValueError):
            build_experiment_share_args("k", "r", "D:P",
                                        pipe_prefix=r"\\.\pipe\um-share-instance-other-")

    def test_empty_sddl_refuses(self):
        with self.assertRaises(ValueError):
            build_experiment_share_args("k", "r", "   ")

    def test_missing_paths_refuse(self):
        with self.assertRaises(ValueError):
            build_experiment_share_args("", "r", "D:P")
        with self.assertRaises(ValueError):
            build_experiment_share_args("k", None, "D:P")


class ValidateGuardInvocationTests(unittest.TestCase):
    def test_happy_path(self):
        self.assertEqual(
            validate_experiment_guard_invocation(
                r"C:\ProgramData\UMShareExp-broker\run\live", 9100, True,
                upstream_port=9100),
            (r"C:\ProgramData\UMShareExp-broker\run\live", 9100, True, "projection"))

    def test_port_must_be_9100(self):
        with self.assertRaises(ValueError):
            validate_experiment_guard_invocation("run", 8100, True,
                                                upstream_port=9100)

    def test_probe_only(self):
        with self.assertRaises(ValueError):
            validate_experiment_guard_invocation("run", 9100, False,
                                                upstream_port=9100)

    def test_projection_mode_only(self):
        with self.assertRaises(ValueError):
            validate_experiment_guard_invocation("run", 9100, True,
                                                 auth_mode="legacy",
                                                 upstream_port=9100)

    def test_run_dir_required(self):
        with self.assertRaises(ValueError):
            validate_experiment_guard_invocation("  ", 9100, True,
                                                upstream_port=9100)

    def test_upstream_port_is_pinned(self):
        # R13-3：实验分享没有上游后端，上游端口钉死在实验端口；缺失或
        # 指向生产分享后端（8110）或私人 8000 都拒绝。
        for bad in (None, 8000, 8110, 9101):
            with self.subTest(upstream_port=bad):
                with self.assertRaises(ValueError):
                    validate_experiment_guard_invocation(
                        "run", 9100, True, upstream_port=bad)


class GatewaySddlWiringTests(unittest.TestCase):
    """P2 wiring: share_gateway.main must forward --instance-pipe-sddl into
    start_instance_channel (the two-pipe-two-DACL contract)."""

    def _run_main(self, extra_argv, recorded):
        class StubServer:
            def __init__(self, address, config, **kwargs):
                self.instance_id = "stub-instance"
                self.kwargs = kwargs

            def serve_forever(self):
                raise KeyboardInterrupt()

            def server_close(self):
                pass

        def fake_start(server, secret, prefix, registration, sddl=None):
            recorded["sddl"] = sddl
            recorded["prefix"] = prefix
            recorded["registration"] = registration
            return {"ready": True, "pipe_name": prefix + "x", "server": None}

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "projection.json"
            cfg.write_text(json.dumps({"clients": {"friend": "a" * 64}}),
                           encoding="utf-8")
            key = Path(td) / "instance-channel-key"
            key.write_text("b" * 64, encoding="utf-8")
            reg = Path(td) / "instance-registration.json"
            argv = ["--config", str(cfg), "--port", "0", "--probe",
                    "--auth-mode", "legacy",
                    "--upstream-port", "8110",
                    "--instance-key-file", str(key),
                    "--instance-pipe-prefix", r"\\.\pipe\um-share-instance-exp-",
                    "--registration-file", str(reg)] + extra_argv
            with mock.patch.object(share, "ShareServer", StubServer), \
                 mock.patch.object(share_instance_pipe, "start_instance_channel",
                                   fake_start):
                share.main(argv)

    def test_sddl_forwarded(self):
        recorded = {}
        self._run_main(["--instance-pipe-sddl", "D:P(A;;FA;;;SY)"], recorded)
        self.assertEqual(recorded["sddl"], "D:P(A;;FA;;;SY)")

    def test_no_sddl_passes_none(self):
        recorded = {}
        self._run_main([], recorded)
        self.assertIsNone(recorded["sddl"])


class ImportClosureTests(unittest.TestCase):
    """The P2 copy manifest's share-side closure imports cleanly offline.

    share_gateway lives under chat2api/ and share_guard under
    um-share-deploy/ (both loaded by their real file paths); the remaining
    five are ai-gateway-root modules imported by name.
    """

    def test_share_closure_imports(self):
        self.assertIsNotNone(share.instance_pipe_name if hasattr(share, "instance_pipe_name") else share)
        for name in ("share_guard", "share_instance_pipe",
                     "share_broker_server", "share_broker_host",
                     "share_broker_protocol", "share_runtime"):
            self.assertIsNotNone(importlib.import_module(name))


_SDDL = "D:P(A;;FR;;;SY)"


def _guard_here():
    return Path(guard.__file__).resolve().parent


def _expected_um_share_args(sddl=_SDDL):
    here = _guard_here()
    return guard.build_experiment_share_args(
        str(here / "instance-channel-key"),
        str(here / "run" / "live" / "instance-registration.json"),
        sddl)


def _write_experiment_config(td, sddl=_SDDL, port=9100, probe=True):
    cfg = {
        "run_id": "20260909T120000-abcdef12",
        "identity_source": "collected",
        "operator_account": "dacint",
        "operator_sid": "S-1-5-21-3623811015-3361044348-30300820-1001",
        "share_account": "um_share",
        "share_sid": "S-1-5-21-3623811015-3361044348-30300820-1002",
        "logon_method": "unestablished",
        "runtime_source_dir": r"C:\rt",
        "runtime_files": [{"file": "python-core.bin", "sha256": "b" * 64}],
        "python_home": r"C:\PythonExp",
        "broker_pipe_sddl": "D:P(A;;GA;;;SY)",
        "instance_pipe_sddl": sddl,
        "instance_pipe_prefix": r"\\.\pipe\um-share-instance-exp-",
        "share_port": port,
        "probe": probe,
        "auth_mode": "projection",
        "limit_rules": [
            {"id": "9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55", "layer": "connect-v4",
             "action": "permit",
             "app_path": "C:/ProgramData/UMShareExp-broker/um-share-python.exe",
             "remote_addr": "127.0.0.1", "remote_port": 9100},
            {"id": "a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66", "layer": "connect-v4",
             "action": "block", "app_path": "C:/ProgramData/UMShareExp-broker/um-share-python.exe"},
            {"id": "b2d5f9a8-3c4a-4e67-9f81-2a0b3c4d5e77", "layer": "connect-v6",
             "action": "block", "app_path": "C:/ProgramData/UMShareExp-broker/um-share-python.exe"},
        ],
    }
    path = Path(td) / "experiment-config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class GuardMainExperimentWiringTests(unittest.TestCase):
    """R62-D guard wiring (handoff 64.3-3): main() consumes
    --experiment-config and REFUSES with zero side effects (no run dir, no
    status file, no Job, no child process) when the launch contract is
    missing/corrupt/placeholder, the UM_SHARE_ARGS env differs EXACTLY from
    the config-derived contract, or the invocation (port/probe/auth mode)
    violates the fixed experiment shape. The happy path proceeds past the
    validations (the boundary mock records the Job creation - the FIRST
    real domain side effect)."""

    def _argv(self, run_dir, cfg, port=9100, probe=True, extra=(),
              upstream=9100):
        return ["guard", "--run-dir", str(run_dir),
                "--experiment-config", str(cfg), "--port", str(port)] + \
               (["--probe"] if probe else []) + \
               (["--upstream-port", str(upstream)] if upstream is not None
                else []) + list(extra)

    def _invoke(self, run_dir, cfg, port=9100, probe=True, extra=(),
                upstream=9100):
        return mock.patch.object(
            sys, "argv", self._argv(run_dir, cfg, port, probe, extra, upstream))

    def _assert_zero_side_effects(self, run_dir):
        self.assertFalse(run_dir.exists())
        self.assertFalse((run_dir / "guard-status.json").exists())

    def test_missing_config_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            with self._invoke(run_dir, Path(td) / "missing.json"), \
                 self.assertRaises(SystemExit) as cm:
                guard.main()
            self.assertIn("config missing", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_corrupt_config_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "bad.json"
            cfg.write_text("{broken", encoding="utf-8")
            run_dir = Path(td) / "run"
            with self._invoke(run_dir, cfg), \
                 self.assertRaises(SystemExit) as cm:
                guard.main()
            self.assertIn("corrupt", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_placeholder_sddl_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td, sddl="D:P(A;;FA;;;S-1-5-18-exp)")
            run_dir = Path(td) / "run"
            with self._invoke(run_dir, cfg), \
                 self.assertRaises(SystemExit) as cm:
                guard.main()
            self.assertIn("calibrated", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_env_mismatch_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td)
            run_dir = Path(td) / "run"
            with mock.patch.dict(os.environ, {"UM_SHARE_ARGS": "tampered"}), \
                 self._invoke(run_dir, cfg), \
                 self.assertRaises(SystemExit) as cm:
                guard.main()
            self.assertIn("does not equal", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_non_9100_port_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td)
            run_dir = Path(td) / "run"
            stub = Path(td) / "sel.json"
            stub.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                              _expected_um_share_args()}), \
                 mock.patch.object(guard, "read_deploy_mode",
                                   return_value="projection"), \
                 mock.patch.object(guard, "select_config",
                                   return_value=stub), \
                 self._invoke(run_dir, cfg, port=8100), \
                 self.assertRaises(ValueError) as cm:
                guard.main()
            self.assertIn("9100", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_probe_refused_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td)
            run_dir = Path(td) / "run"
            stub = Path(td) / "sel.json"
            stub.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                              _expected_um_share_args()}), \
                 mock.patch.object(guard, "read_deploy_mode",
                                   return_value="projection"), \
                 mock.patch.object(guard, "select_config",
                                   return_value=stub), \
                 self._invoke(run_dir, cfg, probe=False), \
                 self.assertRaises(ValueError) as cm:
                guard.main()
            self.assertIn("probe", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_non_projection_mode_refuses_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td)
            run_dir = Path(td) / "run"
            stub = Path(td) / "sel.json"
            stub.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                              _expected_um_share_args()}), \
                 mock.patch.object(guard, "read_deploy_mode",
                                   return_value="legacy"), \
                 mock.patch.object(guard, "select_config",
                                   return_value=stub), \
                 self._invoke(run_dir, cfg), \
                 self.assertRaises(ValueError) as cm:
                guard.main()
            self.assertIn("projection", str(cm.exception))
            self._assert_zero_side_effects(run_dir)

    def test_happy_path_validates_then_proceeds(self):
        class FakeKernel:
            def __init__(self):
                self.calls = []

            def CreateJobObjectW(self, *a):
                self.calls.append("CreateJobObjectW")
                return 1

            def SetInformationJobObject(self, *a):
                self.calls.append("SetInformationJobObject")
                return True

            def CreateProcessW(self, *a):
                self.calls.append("CreateProcessW")
                # byref(pi) wraps the real PROCESS_INFORMATION; _obj unwraps
                # it so the child identity can be recorded like the OS does.
                pi = a[-1]._obj
                pi.hProcess = 100
                pi.hThread = 101
                pi.dwProcessId = 1234
                return True

            def AssignProcessToJobObject(self, *a):
                self.calls.append("AssignProcessToJobObject")
                return True

            def ResumeThread(self, *a):
                self.calls.append("ResumeThread")
                return True

            def WaitForSingleObject(self, *a):
                self.calls.append("WaitForSingleObject")
                return 0

            def GetExitCodeProcess(self, handle, code):
                code._obj.value = 0
                return True

            def CloseHandle(self, *a):
                self.calls.append("CloseHandle")
                return True

            def TerminateProcess(self, *a):
                return True

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            cfg = _write_experiment_config(td)
            stubs = Path(td) / "config-ok.json"
            stubs.write_text("{}", encoding="utf-8")
            fake_kernel = FakeKernel()
            with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                              _expected_um_share_args()}), \
                 mock.patch.object(guard, "kernel32", fake_kernel), \
                 mock.patch.object(guard, "read_deploy_mode",
                                   return_value="projection"), \
                 mock.patch.object(guard, "select_config",
                                   return_value=stubs), \
                 mock.patch("pathlib.Path.exists", return_value=True), \
                 self._invoke(run_dir, cfg):
                code = guard.main()
            self.assertEqual(code, 0)
            # The validations PASSED and the flow reached the FIRST domain
            # side effect (Job creation) - the wiring is live.
            self.assertIn("CreateJobObjectW", fake_kernel.calls)
            self.assertTrue(run_dir.exists())
            status = (run_dir / "guard-status.json").read_text(encoding="utf-8")
            self.assertIn('"guard": "ok"', status)
            self.assertIn("1234", status)
            # R13-3：生效的上游端口是运行证据的一部分（实验端口）。
            self.assertIn('"upstream_port": 9100', status)

    def test_missing_upstream_port_refuses_zero_side_effects(self):
        # R13-3：child 是 share_gateway.py 时没有显式端口即拒绝，且发生在
        # 任何目录写入/Job/进程创建之前（argparse 只给退出码，错误文本在
        # stderr）。
        with tempfile.TemporaryDirectory() as td:
            cfg = _write_experiment_config(td)
            run_dir = Path(td) / "run"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                 self._invoke(run_dir, cfg, upstream=None), \
                 self.assertRaises(SystemExit) as cm:
                guard.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("--upstream-port", stderr.getvalue())
            self._assert_zero_side_effects(run_dir)

    def test_upstream_port_not_9100_refuses_zero_side_effects(self):
        # R13-3：实验分享的上游端口钉死在 9100；指向生产分享后端
        # （8110）或私人 8000 都在任何副作用之前拒绝。
        for bad in (8000, 8110):
            with self.subTest(upstream_port=bad):
                with tempfile.TemporaryDirectory() as td:
                    cfg = _write_experiment_config(td)
                    run_dir = Path(td) / "run"
                    stub = Path(td) / "sel.json"
                    stub.write_text("{}", encoding="utf-8")
                    with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                                      _expected_um_share_args()}), \
                         mock.patch.object(guard, "read_deploy_mode",
                                           return_value="projection"), \
                         mock.patch.object(guard, "select_config",
                                           return_value=stub), \
                         self._invoke(run_dir, cfg, upstream=bad), \
                         self.assertRaises(ValueError) as cm:
                        guard.main()
                    self.assertIn("9100", str(cm.exception))
                    self._assert_zero_side_effects(run_dir)


class GuardChildPortCliTests(unittest.TestCase):
    """R13-3：guard 的 --upstream-port 只对 share_gateway.py 子进程必填。

    escape_probe.py（escape-job Canary）没有上游后端，必须继续在没有该参数
    的情况下工作；share_gateway.py 子进程则必须拿到显式端口，且 guard-status
    记录生效值。
    """

    class _FakeKernel:
        def __init__(self):
            self.calls = []
            self.cmdlines = []

        def CreateJobObjectW(self, *args):
            self.calls.append("CreateJobObjectW")
            return 1

        def SetInformationJobObject(self, *args):
            self.calls.append("SetInformationJobObject")
            return True

        def CreateProcessW(self, *args):
            self.calls.append("CreateProcessW")
            self.cmdlines.append(args[1])
            pi = args[-1]._obj
            pi.hProcess = 100
            pi.hThread = 101
            pi.dwProcessId = 1234
            return True

        def AssignProcessToJobObject(self, *args):
            self.calls.append("AssignProcessToJobObject")
            return True

        def ResumeThread(self, *args):
            self.calls.append("ResumeThread")
            return True

        def WaitForSingleObject(self, *args):
            self.calls.append("WaitForSingleObject")
            return 0

        def GetExitCodeProcess(self, handle, code):
            code._obj.value = 0
            return True

        def CloseHandle(self, *args):
            self.calls.append("CloseHandle")
            return True

        def TerminateProcess(self, *args):
            return True

    def _run(self, argv):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            fake_kernel = self._FakeKernel()
            stubs = Path(td) / "config-ok.json"
            stubs.write_text("{}", encoding="utf-8")
            with mock.patch.object(guard, "kernel32", fake_kernel), \
                 mock.patch.object(guard, "read_deploy_mode",
                                   return_value="projection"), \
                 mock.patch.object(guard, "select_config",
                                   return_value=stubs), \
                 mock.patch("pathlib.Path.exists", return_value=True), \
                 mock.patch.object(sys, "argv",
                                    ["guard", "--run-dir", str(run_dir)] + argv):
                code = guard.main()
            status_path = run_dir / "guard-status.json"
            status = status_path.read_text(encoding="utf-8") \
                if status_path.exists() else None
            return code, status, fake_kernel

    def test_escape_child_without_upstream_port_proceeds(self):
        # escape_probe.py 不拨上游：没有 --upstream-port 仍要能启动
        # （deploy-elevated.ps1:666 的 escape-job Canary 路径）。
        code, status, fake_kernel = self._run(
            ["--child", "escape_probe.py", "--child-args", "C:\\run\\escjob"])
        self.assertEqual(code, 0)
        self.assertIn("CreateJobObjectW", fake_kernel.calls)
        self.assertIn("escape_probe.py", fake_kernel.cmdlines[0])
        self.assertIn('"mode": "test:escape_probe.py"', status)
        self.assertNotIn("upstream_port", status)

    def test_ambient_upstream_port_does_not_satisfy_the_requirement(self):
        # R13-3：环境（UM_SHARE_ARGS）里的 --upstream-port 不算显式指定；
        # guard 只认自己的参数，缺失即拒绝且不创建运行目录。
        with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                          "--instance-key-file k.json "
                                          "--upstream-port 8110"}):
            with tempfile.TemporaryDirectory() as td:
                run_dir = Path(td) / "run"
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), \
                     mock.patch.object(sys, "argv",
                                        ["guard", "--run-dir", str(run_dir),
                                         "--port", "8100"]), \
                     self.assertRaises(SystemExit) as cm:
                    guard.main()
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("--upstream-port", stderr.getvalue())
                self.assertFalse(run_dir.exists())

    def test_share_child_status_records_effective_upstream_port(self):
        # share_gateway.py 子进程必须拿到显式端口，且 guard-status 记录
        # 生效值；环境里的 --upstream-port 被剥离。
        with mock.patch.dict(os.environ, {"UM_SHARE_ARGS":
                                          "--instance-key-file k.json "
                                          "--upstream-port 9999"}):
            code, status, fake_kernel = self._run(
                ["--port", "8100", "--upstream-port", "8110"])
        self.assertEqual(code, 0)
        cmdline = fake_kernel.cmdlines[0]
        self.assertIn("--upstream-port 8110", cmdline)
        self.assertNotIn("9999", cmdline)
        self.assertEqual(cmdline.count("--upstream-port"), 1)
        self.assertIn('"upstream_port": 8110', status)
        self.assertIn('"port": 8100', status)


class GuardFaultLogWiringTests(unittest.TestCase):
    """A9-6/§8.3：guard 必须把 run_dir 下的故障留证文件接线到 8100 子进程。

    读端（broker）固定在安装根 run\\live\\share-faults.jsonl
    （install-share-broker-lib.ps1:467 的 --share-faults-file），而 guard 的
    --run-dir 就是那个 run\\live 目录（deploy-elevated.ps1:820/858）——两端
    必须落在同一文件，否则读端永远读空。cmdline 由假 kernel 捕获，main() 离线
    执行（与 GuardChildPortCliTests 相同的替身，只换 run_dir 形状）。
    """

    def _run(self, root):
        run_dir = root / "run" / "live"
        fake_kernel = GuardChildPortCliTests._FakeKernel()
        stubs = root / "config-ok.json"
        stubs.write_text("{}", encoding="utf-8")
        with mock.patch.object(guard, "kernel32", fake_kernel), \
             mock.patch.object(guard, "read_deploy_mode",
                               return_value="projection"), \
             mock.patch.object(guard, "select_config",
                               return_value=stubs), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(sys, "argv",
                                ["guard", "--run-dir", str(run_dir),
                                 "--port", "8100",
                                 "--upstream-port", "8110"]):
            code = guard.main()
        return code, run_dir, fake_kernel

    def test_guard_passes_run_dir_fault_log(self):
        with tempfile.TemporaryDirectory() as td:
            code, run_dir, fake_kernel = self._run(Path(td))
        self.assertEqual(code, 0)
        cmdline = fake_kernel.cmdlines[0]
        self.assertIn(f'--fault-log "{run_dir / guard.FAULT_LOG_NAME}"',
                      cmdline)
        # 写端（gateway 的 FAULT_LOG_NAME）与 guard 的命名同源。
        self.assertEqual(guard.FAULT_LOG_NAME, share.FAULT_LOG_NAME)

    def test_cmdline_fault_log_is_optional(self):
        # fault_log=None 时旧 cmdline 不变：非 gateway 子进程与旧调用不受影响。
        plain = guard.share_child_cmdline("exe", "share_gateway.py",
                                         "cfg.json", 8100, "projection")
        self.assertNotIn("--fault-log", plain)
        with_log = guard.share_child_cmdline(
            "exe", "share_gateway.py", "cfg.json", 8100, "projection",
            fault_log=r"C:\run\live\share-faults.jsonl")
        self.assertIn('--fault-log "C:\\run\\live\\share-faults.jsonl"',
                      with_log)
        # 显式值排在环境值（UM_SHARE_ARGS）之后：argparse 取最后一个，
        # 环境里的旧 --fault-log 不能把留证改道。
        ordered = guard.share_child_cmdline(
            "exe", "share_gateway.py", "cfg.json", 8100, "projection",
            "--instance-key-file k.json --fault-log C:\\stale\\old.jsonl",
            fault_log=r"C:\run\live\share-faults.jsonl")
        self.assertLess(ordered.index("--fault-log C:\\stale"),
                        ordered.index('--fault-log "C:\\run\\live'))

    def test_fault_log_path_matches_broker_read_side(self):
        # 读端包装器模板恰好一条 --share-faults-file "%~dp0run\live\
        # share-faults.jsonl"（%~dp0 = 安装根）；guard 的 run_dir 必须与该
        # 相对位置解析出同一路径（同一目录 + 同一命名）。
        lib = _repo / "um-share-deploy" / "install-share-broker-lib.ps1"
        text = lib.read_text(encoding="utf-8")
        matches = re.findall(r'--share-faults-file "([^"]+)"', text)
        self.assertEqual(len(matches), 1,
                         "broker wrapper must carry exactly one "
                         "--share-faults-file")
        install_root = r"C:\ProgramData\UMShareRun"
        resolved = matches[0].replace("%~dp0", install_root + "\\")
        self.assertEqual(
            resolved,
            install_root + "\\run\\live\\" + guard.FAULT_LOG_NAME)
        with tempfile.TemporaryDirectory() as td:
            code, run_dir, fake_kernel = self._run(Path(td))
        self.assertEqual(code, 0)
        # 同一相对形状：run_dir=<root>\run\live，child 的 --fault-log 必须
        # 指向同一目录下的同一文件名（<root> 换成临时根）。
        expected = str(run_dir / guard.FAULT_LOG_NAME)
        self.assertTrue(expected.endswith("\\run\\live\\" +
                                        guard.FAULT_LOG_NAME))
        self.assertIn(f'--fault-log "{expected}"',
                      fake_kernel.cmdlines[0])


if __name__ == "__main__":
    unittest.main()
