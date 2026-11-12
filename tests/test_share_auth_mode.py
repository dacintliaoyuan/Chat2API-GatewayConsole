"""§35 R35-2 acceptance: the deployment mode is carried through the whole
share auth boundary.

Offline only: temp-dir fixtures exercise load_config/ShareServer auth_mode
and the guard's deployment-mode gate (read_deploy_mode, select_config,
share_child_cmdline). The guard's CreateProcessW main loop is NOT executed
offline; its cmdline contract is covered by the pure helper. Nothing here
touches the run copy, live gateways or OS permissions.
"""
import hashlib
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_broker_protocol as bp  # noqa: E402

_SHARE_GW = importlib.util.spec_from_file_location(
    "share_entry_authmode_test",
    Path(__file__).resolve().parents[1] / "chat2api" / "share_entry.py")
share_gw = importlib.util.module_from_spec(_SHARE_GW)
_SHARE_GW.loader.exec_module(share_gw)

_GUARD = importlib.util.spec_from_file_location(
    "share_guard_authmode_test",
    Path(__file__).resolve().parents[1] / "um-share-deploy" / "share_guard.py")
guard = importlib.util.module_from_spec(_GUARD)
_GUARD.loader.exec_module(guard)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _projection(generation=3, entries=None):
    return {"schema_version": 1, "config_generation": generation,
            "data": [{"key_id": "uuid-1", "name": "friend1",
                      "secret_hash": _digest("test-secret"),
                      "secret_version": 1, "enabled": True}]}


class ShareAuthModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.mode_file = Path(self.temp.name) / "deploy-mode.json"
        self.mode_file.write_text(json.dumps({"mode": "projection"}),
                                  encoding="utf-8")
        self._write_projection()
        self.server = share_gw.ShareServer(
            ("127.0.0.1", 0), self.config, auth_mode="projection")
        self._threads = []

    def tearDown(self):
        if self._threads:
            # serve_forever must stop BEFORE the socket closes, otherwise the
            # selector reads a closed socket (WinError 10038).
            self.server.shutdown()
        self.server.server_close()
        for t in self._threads:
            t.join()
        self.temp.cleanup()

    def _write_projection(self, payload=None, raw=None):
        if raw is None:
            raw = json.dumps(payload if payload is not None
                             else _projection())
        self.config.write_text(raw, encoding="utf-8")

    def _write_legacy(self, name="friend1", secret="test-secret"):
        self.config.write_text(
            json.dumps({"clients": {name: _digest(secret)}}),
            encoding="utf-8")

    def start_serving(self):
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self._threads.append(thread)

    def http_get(self, key):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_port, timeout=3)
        try:
            conn.request("GET", "/v1/models",
                         headers={"Authorization": "Bearer " + key})
            return conn.getresponse().status
        finally:
            conn.close()

    # ------------------------------------------------ §35.3 (1) HTTP auth/acquire

    def test_projection_mode_rejects_legacy_swap_at_auth_read(self):
        # 有效 projection 启动 → 运行期把派生文件换成合法 legacy clients →
        # HTTP 初始 auth 读与 acquire 的锁内重读全部拒绝；模式固定，公开
        # API 无途径改变。
        self.start_serving()
        self.assertTrue(self.server.acquire("uuid-1", generation=3,
                                            secret_version=1))
        self.server.active.clear()
        on_disk = self.config.read_text(encoding="utf-8")
        self._write_legacy()
        # HTTP auth：连 legacy 文件自己的密钥也必须被拒（projection 模式
        # 在每次 config 读取处拒绝 legacy → fail-closed 503）。
        self.assertEqual(self.http_get("test-secret"), 503)
        # acquire 的锁内重读同样拒绝。
        self.assertFalse(self.server.acquire("friend1", generation=0,
                                             secret_version=1))
        self.assertFalse(self.server.acquire("uuid-1", generation=3,
                                             secret_version=1))
        # 实例通道同样拒绝（load 走共享 parser → 文件已不是投影）。
        load = bp.build_request("inst-k", "load",
                                {"generation": 4, "fingerprint": "0" * 64},
                                instance_id=self.server.instance_id)
        resp = self.server.handle_instance_control(load, "inst-k")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "fingerprint_mismatch")
        # 换回有效 projection：模式没有变，授权恢复（无 API 可改变模式）。
        self.config.write_text(on_disk, encoding="utf-8")
        self.assertTrue(self.server.acquire("uuid-1", generation=3,
                                            secret_version=1))
        self.assertEqual(self.server.auth_mode, "projection")

    # ------------------------------------------------ §35.3 (2) start 前

    def test_projection_mode_rejects_bad_content_before_start(self):
        cases = {
            "bool schema": json.dumps({"schema_version": True,
                                       "config_generation": 3, "data": []}),
            "invalid entry": json.dumps({
                "schema_version": 1, "config_generation": 3,
                "data": [{"key_id": "k", "name": "n",
                          "secret_hash": "short",
                          "secret_version": 1, "enabled": True}]}),
            "missing generation": json.dumps(
                {"schema_version": 1, "data": []}),
            "mixed formats": json.dumps(
                {"clients": {"friend1": _digest("test-secret")},
                 "schema_version": 1, "config_generation": 3, "data": []}),
            "corrupt": "not-json",
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.config.write_text(payload, encoding="utf-8")
                with self.assertRaises(ValueError):
                    share_gw.ShareServer(("127.0.0.1", 0), self.config,
                                         auth_mode="projection")
                # guard 门禁同样在启动前拒绝（select_config 是 Prep 之外的
                # 第二道完整校验）。
                with self.assertRaises(SystemExit):
                    guard.select_config(self.temp.name)

    # ------------------------------------------------ guard 纯函数矩阵

    def test_guard_select_config_full_validates_derived_file(self):
        # 完整校验复用共享 parser：合法 projection 通过；legacy clients、
        # bool schema、坏条目、缺 generation、mixed、损坏 JSON 全部拒绝。
        self._write_projection()
        self.assertEqual(
            guard.select_config(self.temp.name, self.mode_file).name,
            "um-share-projection.json")

        self._write_legacy()
        with self.assertRaises(SystemExit):
            guard.select_config(self.temp.name, self.mode_file)

        bad_payloads = {
            "bool schema": json.dumps({"schema_version": True,
                                       "config_generation": 3, "data": []}),
            "missing generation": json.dumps({"schema_version": 1,
                                              "data": []}),
            "mixed": json.dumps({"clients": {"friend1": _digest("s")},
                                 "schema_version": 1,
                                 "config_generation": 3, "data": []}),
            "corrupt": "not-json",
            "unknown entry field": json.dumps(
                {"schema_version": 1, "config_generation": 3,
                 "data": [{"key_id": "k", "name": "n",
                           "secret_hash": _digest("test-secret"),
                           "secret_version": 1, "enabled": True,
                           "extra": 1}]}),
        }
        for label, payload in bad_payloads.items():
            with self.subTest(case=label):
                self.config.write_text(payload, encoding="utf-8")
                with self.assertRaises(SystemExit):
                    guard.select_config(self.temp.name, self.mode_file)

        # 缺失/损坏/非法的 mode 文件同样拒绝启动（模式从不被推断）。
        self.mode_file.unlink()
        with self.assertRaises(SystemExit):
            guard.select_config(self.temp.name, self.mode_file)
        self.mode_file.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            guard.select_config(self.temp.name, self.mode_file)
        self.mode_file.write_text(json.dumps({"mode": "auto"}),
                                  encoding="utf-8")
        with self.assertRaises(SystemExit):
            guard.select_config(self.temp.name, self.mode_file)

    def test_guard_child_cmdline_carries_auth_mode(self):
        # guard 的 CreateProcessW 主循环无法离线执行；cmdline 契约由纯函数
        # 覆盖：--auth-mode 显式转发且在 --config 之后、任意子参数之前。
        # R13-3：上游端口同样必须显式出现在 cmdline 里（无默认值）。
        cmdline = guard.share_child_cmdline(
            "exe", "share_entry.py", "cfg.json", 8100, "projection",
            "--instance-key-file key.json --upstream-port 8110", probe=True)
        self.assertIn('--config "cfg.json"', cmdline)
        self.assertIn("--auth-mode projection", cmdline)
        self.assertIn("--upstream-port 8110", cmdline)
        self.assertLess(cmdline.index("--auth-mode"),
                        cmdline.index("--upstream-port 8110"))
        plain = guard.share_child_cmdline("exe", "share_entry.py",
                                          "cfg.json", 8100, "legacy")
        self.assertIn("--auth-mode legacy", plain)
        self.assertNotIn("--probe", plain)

    def test_guard_share_child_argv_requires_explicit_port(self):
        # R13-3：没有显式端口就拒绝，绝不追加一个默认的 8000。
        for missing in (None,):
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    guard.share_child_argv("", missing)
        with self.assertRaises(ValueError):
            guard.share_child_argv("--instance-key-file key.json", 0)
        with self.assertRaises(ValueError):
            guard.share_child_argv("", 65536)

    def test_guard_share_child_argv_strips_ambient_upstream_port(self):
        # R6-A1/R13-3：环境（UM_SHARE_ARGS）里的 --upstream-port 先被剥离，
        # 再无条件追加显式值；两种写法（空格/等号）都要剥干净，连值一起
        # 剥掉，不给子进程留孤儿位置参数。
        self.assertEqual(
            guard.share_child_argv(
                "--instance-key-file key.json --upstream-port 9999", 8110),
            "--instance-key-file key.json --upstream-port 8110")
        self.assertEqual(
            guard.share_child_argv(
                "--instance-key-file key.json --upstream-port=9999", 8110),
            "--instance-key-file key.json --upstream-port 8110")
        self.assertEqual(
            guard.share_child_argv("--instance-key-file key.json", 8110),
            "--instance-key-file key.json --upstream-port 8110")
        # 被剥掉的端口绝不出现在结果里（最后一个才是生效值）。
        argv = guard.share_child_argv(
            "--upstream-port 9999 --instance-key-file key.json", 8110)
        self.assertNotIn("9999", argv)
        self.assertEqual(argv.count("--upstream-port"), 1)

    def test_read_deploy_mode_refuses_ambiguity(self):
        # 缺失/损坏/非法 mode 都拒绝；合法值返回。
        with tempfile.TemporaryDirectory() as td:
            mode_file = Path(td) / "deploy-mode.json"
            with self.assertRaises(SystemExit):
                guard.read_deploy_mode(td, mode_file)
            mode_file.write_text("{broken", encoding="utf-8")
            with self.assertRaises(SystemExit):
                guard.read_deploy_mode(td, mode_file)
            mode_file.write_text(json.dumps({"mode": "auto"}),
                                 encoding="utf-8")
            with self.assertRaises(SystemExit):
                guard.read_deploy_mode(td, mode_file)
            mode_file.write_text(json.dumps({"mode": "projection"}),
                                 encoding="utf-8")
            self.assertEqual(guard.read_deploy_mode(td, mode_file),
                             "projection")

    # ------------------------------------------------ §35.3 (3) 双合法路径

    def test_valid_projection_and_explicit_legacy_both_pass(self):
        # projection 模式 + 有效 projection：auth 通过。
        self.assertTrue(self.server.acquire("uuid-1", generation=3,
                                            secret_version=1))
        self.server.active.clear()
        # 显式 legacy 模式 + legacy 客户端文件：实例可启动并接受该密钥。
        legacy_cfg = Path(self.temp.name) / "um-share.json"
        legacy_cfg.write_text(
            json.dumps({"clients": {"friend1": _digest("test-secret")}}),
            encoding="utf-8")
        legacy_server = share_gw.ShareServer(("127.0.0.1", 0), legacy_cfg,
                                             auth_mode="legacy")
        try:
            self.assertTrue(legacy_server.acquire("friend1"))
            self.assertEqual(legacy_server.auth_mode, "legacy")
            # legacy 模式下投影文件被拒（模式而非文件决定格式）。
            with self.assertRaises(ValueError):
                share_gw.ShareServer(("127.0.0.1", 0), self.config,
                                     auth_mode="legacy")
        finally:
            legacy_server.server_close()

    # ------------------------------------------------ load_config 模式矩阵

    def test_load_config_mode_matrix(self):
        self._write_projection()
        cfg = share_gw.load_config(self.config, auth_mode="projection")
        self.assertEqual(cfg["config_generation"], 3)
        self.assertFalse(cfg["legacy"])
        # 非法 auth_mode 一律拒绝（不存在“自动猜测模式”的旁路）。
        for mode in ("auto", "PROJECTION", 1, True):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    share_gw.load_config(self.config, auth_mode=mode)
        self._write_legacy()
        with self.assertRaises(ValueError):
            share_gw.load_config(self.config, auth_mode="projection")
        cfg = share_gw.load_config(self.config, auth_mode="legacy")
        self.assertTrue(cfg["legacy"])
        self.assertEqual(cfg["entries"][0]["key_id"], "friend1")
        self._write_projection()
        with self.assertRaises(ValueError):
            share_gw.load_config(self.config, auth_mode="legacy")

    def test_projection_mode_start_refuses_legacy_entry_fields(self):
        # R31-6 口径贯通：条目未知字段在 auth loader 同样拒绝（共享 parser
        # 的 whitelist），不再比 writer/ACK 更宽松。
        payload = _projection()
        payload["data"][0]["extra"] = "x"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            share_gw.load_config(self.config, auth_mode="projection")

    def test_instance_channel_bad_signature_is_structured_refusal(self):
        # 连带修复（R35-2）：handle_instance_control 的 except ProtocolError
        # 此前名字未导入，坏签名实例请求以 NameError 崩溃；现在返回结构化
        # 拒绝而非异常逃逸。
        bad = bp.build_request("WRONG-SECRET", "instance-status",
                               instance_id=self.server.instance_id)
        resp = self.server.handle_instance_control(bad, "inst-k")
        self.assertFalse(resp["ok"])
        self.assertNotEqual(resp["error_code"], "ok")
        self.assertIn("detail", resp["result"])


if __name__ == "__main__":
    unittest.main()
