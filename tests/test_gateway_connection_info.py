# -*- coding: utf-8 -*-
"""Rev15 连接信息窄只读读面的隔离测试（合成值，不发任何模型请求）。

范围：``console_gateway_connection``（模块级读面）与 ``usagesrv`` 的
``GET /api/console/gateway-connection`` 路由。所有 Key 值都是合成哨兵；
文件来源指向临时目录；runtime 快照与命令行都是注入替身——不触碰真实
data/ 凭据文件、不 spawn 真实 PowerShell、不连任何服务、不发模型请求。

断言重点：
* 读面形状与状态闭集（present / absent / needs_key_unconfigured /
  unreadable / unknown）；
* 端口只从快照取（夹具用与描述符不同的端口，证明不是猜的）；
* 未知绝不降级成「没有」或「0」；
* chat2api 的鉴权判定只由运行实例自己的启动记录（服务日志）驱动，
  控制台环境与启动路径扫描只是 evidence，绝不驱动状态；
* 每源 ``key_in_use`` 闭集（per_request / startup_env / config_consumed /
  unknown）如实区分「配置文件值」与「实例已确认使用值」；
* 合成 Key 只出现在返回载荷的 ``key_plaintext``，不进 stdout/stderr、
  错误文本或其它来源的载荷；
* 路由排在 console-manager 装配之前，不写操作账本。
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import gateway_connection as conn  # noqa: E402


def _restore_process_env(saved):
    """Put the named process variables back exactly as they were saved."""
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# 测试专用合成值：绝不来自真实文件，也绝不写进任何真实文件。
SENTINEL_CN = "fixture-not-a-credential-cn-7f3a91"
ENV_NAME_CHAT2API = "CHAT2API_LOCAL_API_KEY"

# 实例启动记录（合成日志行）：时间戳镜像真实读数
# chat2api.log.1 的 ``2026-10-05 08:11:36,945 INFO listening ...``，
# 但端口/pid/键要求都是合成值。行时间戳是 logging 的本地墙钟。
LOG_STAMP = "2026-10-05 08:11:36,945"
LOG_PREFIX = LOG_STAMP + " INFO "
LOG_PORT = 8000
LOG_PID = 30580

REQUIRED_KEYS = (
    "ok", "service", "display_name", "port", "health_path",
    "health_path_source", "base_url", "protocol", "protocol_path",
    "runtime_state", "runtime_healthy", "auth", "key_state",
    "key_plaintext", "key_source", "key_source_state", "key_in_use",
    "reason", "evidence", "checked_epoch",
)

KEY_IN_USE_VALUES = ("per_request", "startup_env", "config_consumed",
                     "unknown")


def _record_line(port=LOG_PORT, pid=LOG_PID, downstream_key=None,
                  host="127.0.0.1", stamp=LOG_STAMP):
    line = "%s INFO listening host=%s port=%d pid=%d" % (stamp, host, port, pid)
    if downstream_key is not None:
        line += " downstream_key=%s" % downstream_key
    return line


def _line_epoch(stamp=LOG_STAMP):
    """与实现同口径解析行时间戳（本地墙钟）；只用于构造守卫反例。"""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def _entry(key, port, display_name="X", state="Running", healthy=True,
            pids=(4242,)):
    return {"key": key, "display_name": display_name, "port": port,
            "state": state, "healthy": healthy, "detail": "",
            "process_ids": list(pids)}


def _snapshot(*entries):
    return {"ok": True, "services": list(entries),
            "meta": {"source": "gateway-runtime", "generated_epoch": 1}}


def _assert_shape(case, payload):
    case.assertEqual(payload.get("ok"), True)
    for key in REQUIRED_KEYS:
        case.assertIn(key, payload, key)
    case.assertIsInstance(payload["evidence"], list)
    case.assertIsInstance(payload["checked_epoch"], int)


class RetiredServiceRemovalTests(unittest.TestCase):
    """B0（2026-11-01）退役来源的专用拒绝契约已删除。

    Qoder 国内 / 国际 / CLIProxy 与已归档的 M365 都不在服务表内：读面与
    未知 service 同码拒绝（ValueError → HTTP 400 invalid_service），绝不
    读取旧 Key 文件、旧 config.yaml，也不探测旧端口。
    """

    def test_retired_services_are_unknown_and_refused(self):
        for service in ("Qoder", "QoderGlobal", "CLIProxy", "M365"):
            with self.subTest(service=service):
                with self.assertRaises(ValueError) as caught:
                    conn.connection_info(service, snapshot=_snapshot())
                self.assertNotIsInstance(caught.exception, AttributeError)
                self.assertNotIn("source_retired",
                                 getattr(caught.exception, "code", ""))

    def test_retired_service_contract_is_gone(self):
        self.assertFalse(hasattr(conn, "RETIRED_SERVICES"))
        self.assertFalse(hasattr(conn, "RetiredServiceError"))
        for service in ("Qoder", "QoderGlobal", "CLIProxy", "M365"):
            with self.subTest(service=service):
                self.assertNotIn(service, conn.SERVICES)
        self.assertEqual(set(conn.SERVICES), {"chat2api"})

    def test_no_cliproxy_config_is_read(self):
        # 退役来源不再读取 cliproxy/config.yaml：模块里也不再有该路径常量。
        self.assertFalse(hasattr(conn, "CLIPROXY_CONFIG_FILE"))
        self.assertFalse(hasattr(conn, "CLIPROXY_ALT_PROTOCOL"))
        self.assertFalse(hasattr(conn, "_cliproxy_key_in_config"))


class ServiceLogPathTests(unittest.TestCase):
    """服务日志路径解析：CHAT2API_SERVICE_LOG 覆盖或默认 chat2api/chat2api.log。"""

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ,
                            {"CHAT2API_SERVICE_LOG": "X:/synthetic/service.log"}):
            self.assertEqual(conn._service_log_path(), "X:/synthetic/service.log")

    def test_default_matches_chat2api_service_log(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHAT2API_SERVICE_LOG", None)
            parts = Path(conn._service_log_path()).parts[-2:]
            self.assertEqual(parts, ("chat2api", "chat2api.log"))


class SnapshotHonestyTests(unittest.TestCase):
    """快照不可用/条目缺失/端口异常 → unknown，绝不降级成「没有」。"""

    def test_missing_entry_is_unknown(self):
        payload = conn.connection_info(
            "chat2api", snapshot=_snapshot(_entry("share", 8100)))
        self.assertEqual(payload["key_state"], "unknown")
        self.assertIsNone(payload["base_url"])
        self.assertIsNone(payload["display_name"])

    def test_bad_port_is_unknown_and_never_guessed(self):
        payload = conn.connection_info(
            "chat2api", snapshot=_snapshot(_entry("chat2api", None)))
        self.assertEqual(payload["key_state"], "unknown")
        self.assertIsNone(payload["base_url"])
        self.assertIsNone(payload["port"])
        self.assertTrue(payload["reason"])


class Chat2ApiSourceTests(unittest.TestCase):
    """chat2api：实例自己的启动记录 + 运行实例身份；控制台环境只作 evidence。

    R32-KEY-AUTH：运行实例在进程导入时读自己的环境（chat2api.py:121/:6407），
    回环绑定仍可要求 Key；控制台进程环境、启动脚本扫描与回环绑定都不能证明
    「入站鉴权关闭」。只有该实例自己写在服务日志里的 ``listening`` 记录
    （含 ``downstream_key=``）且行时间戳不早于进程创建时间，才允许给出
    ``absent``（或 ``needs_key_unconfigured``）；否则一律 ``unknown``。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="gwconn-chat2api-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bat = self.tmp / "start_chat2api.bat"
        self.vbs = self.tmp / "start_chat2api_hidden.vbs"
        self.ps1 = self.tmp / "gateway-runtime.ps1"
        for path in (self.bat, self.vbs, self.ps1):
            path.write_text("rem synthetic launcher: no key variable here\n",
                            encoding="utf-8")
        patcher = mock.patch.object(
            conn, "CHAT2API_LAUNCH_FILES",
            (str(self.bat), str(self.vbs), str(self.ps1)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log_path = self.tmp / "chat2api.log"
        log_patcher = mock.patch.object(
            conn, "_service_log_path", lambda: str(self.log_path))
        log_patcher.start()
        self.addCleanup(log_patcher.stop)
        self.line_epoch = _line_epoch()

    def write_log(self, *lines, name="chat2api.log"):
        path = self.tmp / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _info(self, cmdline="python chat2api.py --base-url https://example.invalid",
              env=None, records=None, reader=None, state="Running",
              pids=(LOG_PID,), created=None):
        if reader is None:
            if records is None:
                if created is None:
                    created = self.line_epoch - 0.322  # 记录晚于进程创建
                records = {pid: {"command_line": cmdline,
                                 "created_epoch": created}
                           for pid in pids}
            reader = lambda ids: {pid: records[pid]
                                 for pid in ids if pid in records}
        snapshot = _snapshot(_entry("chat2api", LOG_PORT,
                                   display_name="chat2api",
                                   state=state, pids=pids))
        return conn.connection_info(
            "chat2api", snapshot=snapshot, cmdline_reader=reader,
            env=env if env is not None else {})

    def _joined(self, payload):
        return " ".join(payload["evidence"]) + " " + payload["reason"]

    # --- 实例记录驱动的状态 ---------------------------------------------

    def test_record_not_required_is_absent_with_record_evidence(self):
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info()
        _assert_shape(self, payload)
        self.assertEqual(payload["key_state"], "absent")
        self.assertIsNone(payload["key_plaintext"])
        self.assertEqual(payload["auth"], "loopback_no_key")
        self.assertEqual(payload["key_source_state"], "absent")
        self.assertEqual(payload["key_source"], "env:" + ENV_NAME_CHAT2API)
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8000/v1")
        self.assertEqual(payload["health_path"], "/health")
        self.assertIn(payload["key_in_use"], KEY_IN_USE_VALUES)
        joined = self._joined(payload)
        self.assertIn("chat2api.log", joined)      # 记录证据：文件
        self.assertIn("pid=%d" % LOG_PID, joined)  # pid
        self.assertIn(LOG_STAMP, joined)           # 时间
        self.assertIn("--host", joined)            # 回环绑定证据
        self.assertIn(ENV_NAME_CHAT2API, joined)   # 控制台环境证据
        self.assertNotIn("fixture-not-a-credential",
                         json.dumps(payload, ensure_ascii=False))

    def test_record_required_is_needs_key_unconfigured(self):
        self.write_log(_record_line(downstream_key="required"))
        payload = self._info()
        _assert_shape(self, payload)
        self.assertEqual(payload["key_state"], "needs_key_unconfigured")
        self.assertIsNone(payload["key_plaintext"])
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertNotEqual(payload["key_state"], "present")
        self.assertEqual(payload["auth"], "bearer")
        self.assertEqual(payload["key_source_state"], "unknown")
        joined = self._joined(payload)
        self.assertIn("进程级环境不可读", joined)
        self.assertIn("配置缺失", joined)
        self.assertIn("不得表示可免鉴权", joined)

    def test_record_missing_is_unknown(self):
        payload = self._info()
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIsNone(payload["key_plaintext"])
        self.assertEqual(payload["auth"], "unknown")
        self.assertIn("记录", payload["reason"])

    def test_log_unreadable_is_unknown(self):
        self.log_path.mkdir()  # 存在但读取失败（OSError）
        payload = self._info()
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_oversized_log_is_unknown(self):
        # 读取上限守卫：异常巨大的文件拒绝整体读入，按缺口上报
        self.write_log(_record_line(downstream_key="not_required"))
        with mock.patch.object(conn.os.path, "getsize",
                               return_value=10 ** 12):
            payload = self._info()
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_record_before_process_creation_is_unknown(self):
        # PID 复用守卫：行时间戳早于该进程的创建时间
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(created=self.line_epoch + 1.0)
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("时间戳", payload["reason"])

    def test_creation_time_unreadable_is_unknown(self):
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(created=None, records={LOG_PID: {
            "command_line": "python chat2api.py", "created_epoch": None}})
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_old_record_without_field_is_unknown(self):
        self.write_log(_record_line())  # 旧版记录：缺 downstream_key=
        payload = self._info()
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("downstream_key", payload["reason"])

    def test_latest_record_wins(self):
        self.write_log(_record_line(downstream_key="not_required"),
                       _record_line(downstream_key="required"))
        payload = self._info()
        self.assertEqual(payload["key_state"], "needs_key_unconfigured")
        self.write_log(_record_line(downstream_key="required"),
                       _record_line(downstream_key="not_required"))
        payload = self._info()
        self.assertEqual(payload["key_state"], "absent")

    def test_rotated_log_copy_is_found(self):
        self.write_log(_record_line(downstream_key="not_required"),
                        name="chat2api.log.1")
        self.write_log(LOG_STAMP + " INFO request summary status=ok")
        payload = self._info()
        self.assertEqual(payload["key_state"], "absent")
        self.assertIn("chat2api.log.1", self._joined(payload))

    # --- 控制台环境 / 启动路径 / 回环绑定只作 evidence -------------------

    def test_console_env_never_drives_state(self):
        self.write_log(_record_line(downstream_key="not_required"))
        with_env = self._info(env={ENV_NAME_CHAT2API: "synthetic-env-9a4f"})
        self.assertEqual(with_env["key_state"], "absent")
        self.assertIn(ENV_NAME_CHAT2API, self._joined(with_env))
        self.assertNotIn("synthetic-env-9a4f",
                         json.dumps(with_env, ensure_ascii=False))
        self.write_log(_record_line(downstream_key="required"))
        without_env = self._info(env={})
        self.assertEqual(without_env["key_state"], "needs_key_unconfigured")
        self.assertNotEqual(without_env["key_state"], "absent")

    def test_console_env_visible_without_record_is_unknown(self):
        payload = self._info(env={ENV_NAME_CHAT2API: "synthetic-env-9a4f"})
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "present")
        self.assertIn("进程级环境不可读", payload["reason"])

    def test_launch_path_mention_is_evidence_not_state(self):
        # 夹具只让启动脚本出现变量名（合成注释，无任何凭据赋值）
        self.bat.write_text("rem synthetic launcher mentions "
                            + ENV_NAME_CHAT2API + "\n", encoding="utf-8")
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info()
        self.assertEqual(payload["key_state"], "absent")
        self.assertIn("start_chat2api.bat", self._joined(payload))

    def test_launch_path_unreadable_is_evidence_not_state(self):
        (self.tmp / "gateway-runtime.ps1").unlink()
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info()
        self.assertEqual(payload["key_state"], "absent")
        self.assertIn("gateway-runtime.ps1", self._joined(payload))

    def test_legacy_string_reader_without_creation_time_is_unknown(self):
        # 边界容错：注入的读取器只给命令行（无创建时间）时不能跳过守卫
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(
            reader=lambda ids: {pid: "python chat2api.py" for pid in ids})
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    # --- 运行实例身份守卫（沿用 R32-KEY-AUTH 的既有反例） -----------------

    def test_non_loopback_host_is_unknown_never_auth_free(self):
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(cmdline="python chat2api.py --host 0.0.0.0")
        self.assertEqual(payload["key_state"], "unknown")
        self.assertIsNone(payload["key_plaintext"])
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("非回环", self._joined(payload))

    def test_non_loopback_with_not_required_record_is_unknown(self):
        # 记录说「不需要」而命令行是非回环：两者矛盾，不得给 absent
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(cmdline="python chat2api.py --host 0.0.0.0")
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_equals_form_non_loopback_is_unknown(self):
        payload = self._info(cmdline="python chat2api.py --host=0.0.0.0")
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_bare_host_flag_is_malformed_unknown(self):
        payload = self._info(cmdline="python chat2api.py --host")
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("形状异常", payload["reason"])

    def test_share_instance_port_mismatch_is_unknown(self):
        # 反例（调查 §3.1）：8110 share 实例与私有实例共用同一脚本，按 --port 区分
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info(
            cmdline="python chat2api.py --port 8110 --profile x")
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("8110", payload["reason"])

    def test_stopped_instance_is_unknown(self):
        payload = self._info(state="Stopped", pids=())
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_cmdline_unavailable_is_unknown(self):
        def _boom(ids):
            raise OSError("synthetic CIM failure")
        payload = self._info(reader=_boom)
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")
        self.assertIn("命令行读取失败", payload["reason"])

    def test_empty_cmdline_is_unknown(self):
        payload = self._info(reader=lambda ids: {pid: "" for pid in ids})
        self.assertEqual(payload["key_state"], "unknown")
        self.assertNotEqual(payload["key_state"], "absent")

    def test_key_in_use_is_startup_env(self):
        self.write_log(_record_line(downstream_key="not_required"))
        payload = self._info()
        self.assertEqual(payload["key_in_use"], "startup_env")


class ScopeAndHygieneTests(unittest.TestCase):
    """退役来源拒绝、未知 service 拒绝、旧 Key 不外泄。"""

    def test_m365_is_rejected(self):
        with self.assertRaises(ValueError):
            conn.connection_info("M365", snapshot=_snapshot())

    def test_unknown_service_is_rejected(self):
        with self.assertRaises(ValueError):
            conn.connection_info("nope", snapshot=_snapshot())

    def test_retired_services_never_read_old_key_files(self):
        with tempfile.TemporaryDirectory(prefix="gwconn-hygiene-") as tmp:
            path = Path(tmp) / "qoder-cn-api-token"
            path.write_text(SENTINEL_CN + "\n", encoding="utf-8")
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), \
                    contextlib.redirect_stderr(captured):
                with self.assertRaises(ValueError):
                    conn.connection_info(
                        "Qoder",
                        snapshot=_snapshot(_entry("Qoder", 8095,
                                                 display_name="Qoder（国内）")))
        self.assertNotIn(SENTINEL_CN, captured.getvalue())


# ------------------------------------------------------------------ HTTP 路由

_FAKE_SNAPSHOT = _snapshot(
    _entry("chat2api", 8000, display_name="chat2api", pids=(4242,)),
)


class GatewayConnectionRouteTests(unittest.TestCase):
    """HTTP 层：直读 + _send_json，排在 console-manager 装配之前。"""

    @classmethod
    def setUpClass(cls):
        import os
        cls._tmp = tempfile.TemporaryDirectory(prefix="gwconn-route-")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = Path(cls._tmp.name)
        # ``usagesrv`` binds CONSOLE_STATE_FILE/CONSOLE_LOCK_DIR at import time,
        # so the redirect has to be in the environment while ``exec_module``
        # runs. It must NOT stay there afterwards: the module constants keep the
        # fixture paths, and a process-wide value pointing into an already
        # deleted temp directory leaks into every later test module of the same
        # ``discover`` run (test_r19_cloud_root.KeyApiTests failed on exactly
        # that leftover). The saved values are restored immediately after the
        # import, and again as a class cleanup.
        cls._saved_env = {name: os.environ.get(name) for name in
                          ("CONSOLE_STATE_FILE", "CONSOLE_LOCK_DIR")}
        os.environ["CONSOLE_STATE_FILE"] = str(root / "console-state.sqlite3")
        os.environ["CONSOLE_LOCK_DIR"] = str(root / "console-locks")
        spec = importlib.util.spec_from_file_location(
            "usage_service_gateway_connection",
            Path(__file__).resolve().parents[1] / "usagesrv.py")
        cls.usagesrv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.usagesrv
        spec.loader.exec_module(cls.usagesrv)
        # The import consumed the redirect; drop it before any test runs so the
        # process environment never keeps a fixture path.
        _restore_process_env(cls._saved_env)
        cls.addClassCleanup(_restore_process_env, cls._saved_env)
        cls.status_patcher = mock.patch(
            "console.adapters.service_status", return_value=_FAKE_SNAPSHOT)
        cls.status_patcher.start()
        cls.addClassCleanup(cls.status_patcher.stop)
        cls.server = cls.usagesrv.ThreadingHTTPServer(
            ("127.0.0.1", 0), cls.usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def setUp(self):
        # TTL 缓存逐用例清空（快照替身可能在用例间变化）
        self.usagesrv._console_status_cache["value"] = None
        self.usagesrv._console_status_cache["at"] = 0.0

    def _get(self, path):
        client = HTTPConnection("127.0.0.1", self.port, timeout=10)
        client.request("GET", path)
        resp = client.getresponse()
        raw = resp.read()
        client.close()
        return resp.status, resp.getheader("Cache-Control"), raw

    def test_class_leaves_no_fixture_path_in_the_process_environment(self):
        """The import redirect must not outlive the import.

        Regression: the class used to leave ``CONSOLE_STATE_FILE`` pointing at
        its (later deleted) temp root for the rest of the process, which made
        ``test_r19_cloud_root.KeyApiTests`` fail in a full ``discover`` run while
        both modules passed in isolation.
        """
        self.assertIsNone(os.environ.get("CONSOLE_STATE_FILE"))
        self.assertIsNone(os.environ.get("CONSOLE_LOCK_DIR"))
        # The module constants still carry the fixture paths: the redirect was
        # consumed by the import, not silently dropped.
        self.assertEqual(
            self.usagesrv.CONSOLE_STATE_FILE,
            os.path.join(self._tmp.name, "console-state.sqlite3"))

    def test_route_serves_payload_before_manager_assembly(self):
        import os
        with mock.patch.object(
                self.usagesrv, "get_console_manager",
                side_effect=AssertionError("manager must not assemble")):
            status, cache_control, raw = self._get(
                "/api/console/gateway-connection?service=chat2api")
        self.assertEqual(status, 200)
        self.assertEqual(cache_control, "no-store")
        payload = json.loads(raw.decode("utf-8"))
        # chat2api 的调用 Key 只在运行实例自己的启动记录里判定：没有
        # listening 记录时如实 unknown，绝不伪造 present / 明文。
        self.assertEqual(payload["key_state"], "unknown")
        self.assertIsNone(payload["key_plaintext"])
        self.assertEqual(payload["port"], 8000)
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8000/v1")
        self.assertFalse(os.path.exists(self.usagesrv.CONSOLE_STATE_FILE))

    def test_route_rejects_unknown_service(self):
        status, _, raw = self._get(
            "/api/console/gateway-connection?service=nope")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"],
                         "invalid_service")
        status, _, raw = self._get("/api/console/gateway-connection")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"],
                         "invalid_service")

    def test_route_rejects_retired_sources_as_unknown(self):
        # B0: retired sources are not in the service table, so the route
        # answers the SAME 400 invalid_service as any unknown service and
        # never reads the runtime snapshot.
        for service in ("Qoder", "QoderGlobal", "CLIProxy", "M365"):
            with self.subTest(service=service):
                with mock.patch.object(
                        self.usagesrv, "console_services_snapshot",
                        side_effect=AssertionError(
                            "retired source must not read the snapshot")), \
                        mock.patch.object(
                            self.usagesrv, "get_console_manager",
                            side_effect=AssertionError(
                                "manager must not assemble")):
                    status, _, raw = self._get(
                        "/api/console/gateway-connection?service=" + service)
                self.assertEqual(status, 400)
                payload = json.loads(raw.decode("utf-8"))
                self.assertEqual(payload["error"], "invalid_service")

    def test_route_write_methods_and_subpaths_are_unknown(self):
        client = HTTPConnection("127.0.0.1", self.port, timeout=10)
        client.request("POST", "/api/console/gateway-connection?service=chat2api",
                       body=json.dumps({"x": 1}),
                       headers={"Content-Type": "application/json"})
        resp = client.getresponse()
        resp.read()
        client.close()
        self.assertEqual(resp.status, 404)
        status, _, raw = self._get(
            "/api/console/gateway-connection/extra?service=chat2api")
        self.assertEqual(status, 404)

    def test_route_snapshot_failure_is_unknown_not_absent(self):
        with mock.patch("console.adapters.service_status",
                        side_effect=RuntimeError("synthetic snapshot failure")):
            status, _, raw = self._get(
                "/api/console/gateway-connection?service=chat2api")
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["key_state"], "unknown")
        self.assertIsNone(payload["base_url"])
        self.assertNotEqual(payload["key_state"], "absent")


if __name__ == "__main__":
    unittest.main()
