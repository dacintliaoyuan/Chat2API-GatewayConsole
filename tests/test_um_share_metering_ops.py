# -*- coding: utf-8 -*-
"""`ai-gateway/tools/um-share/` 两个**动作工具**的夹具测试（不连生产）。

覆盖（动作门 §④ 的工具化）：

* ``test_key_ops``：dry-run / apply 判定；**未 ``--apply`` 绝不写盘**；
  测试前缀（``zz-test-``）与生产 Key 的区分；``--apply --create`` 缺
  ``--secret-out``、存储不存在未给 ``--allow-new-store``、目标已删除、目标非
  测试 Key 等拒绝分支；revoke 在无 broker 下的 fail-closed 部分状态。
* ``metering_request_probe``：``--run`` 的上限拒绝四态（缺任一参数即退出码 2）；
  **计划打印先于任何请求**；逐条记录与停止规则（429/非 2xx）；费用无价或
  超上限即拒发；结束后入口状态核对与三事实验收的调用。
* ``metering_acceptance.collect_facts``（2026-09-28 复审执行轮 9 后加）：在
  临时目录的**陈旧** ``backend.pid`` 上跑真实采集路径，断言文件仍在、size /
  mtime_ns / 内容逐字不变、事实照实报「陈旧」，且运行时写哨兵一次都没响；
  反向对照证明默认档仍会删（开关不是恒真）。

隔离：夹具全在 ``tempfile`` 临时目录里；不连 127.0.0.1 的任何端口、不跑
netstat/CIM、**不碰** ``ai-gateway/data/share-store.db``（默认存储的两处解析点
都改指临时目录里的哨兵路径，操作若回退默认就会落到哨兵上；生产库绝不打开）、
不重启任何服务、不发模型请求。``--apply`` 只在临时库里跑，
``--broker none``（NoopBroker）→ 不会在线发布任何东西。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_um_share_metering_ops -q
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

AI_GATEWAY_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = AI_GATEWAY_DIR / "tools" / "um-share"
if str(AI_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_DIR))

import console_share_production as prod  # noqa: E402
import share_management  # noqa: E402


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_ops_" + module_name,
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


key_ops = _load("test_key_ops")
probe = _load("metering_request_probe")
acceptance = _load("metering_acceptance")

TEST_LABEL = "zz-test-alpha"


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_main(module, argv, deps=None):
    """跑 main 并同时抓 stdout/stderr；返回 ``(code, out, err)``。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv, deps) if deps is not None \
            else module.main(argv)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------- test_key_ops
class KeyOpsDecideTests(unittest.TestCase):
    """``decide`` 的拒绝/放行判定（纯函数，不碰存储）。"""

    def _intent(self, **overrides):
        base = {"action": "create", "apply": False, "label": TEST_LABEL,
                "key_id": None, "secret_out": None,
                "secret_out_exists": False, "store": "s.db",
                "store_exists": True, "allow_new_store": False,
                "max_concurrency": 1, "key_row": None,
                "active_names": set(), "broker": "none"}
        base.update(overrides)
        return base

    def test_dry_run_create_is_allowed_without_secret_out(self):
        decision = key_ops.decide(self._intent())
        self.assertEqual(0, decision["exit_code"], decision["refusals"])
        self.assertEqual("dry_run", decision["mode"])

    def test_create_label_without_test_prefix_is_refused(self):
        decision = key_ops.decide(self._intent(label="小墨"))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("zz-test-" in reason
                            for reason in decision["refusals"]))

    def test_apply_create_requires_secret_out(self):
        decision = key_ops.decide(self._intent(apply=True))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("--secret-out" in reason
                            for reason in decision["refusals"]))

    def test_apply_create_refuses_existing_secret_out(self):
        decision = key_ops.decide(self._intent(
            apply=True, secret_out="s.txt", secret_out_exists=True))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("拒绝覆盖" in reason
                            for reason in decision["refusals"]))

    def test_apply_create_refuses_missing_store_without_flag(self):
        decision = key_ops.decide(self._intent(
            apply=True, secret_out="s.txt", store_exists=False))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("--allow-new-store" in reason
                            for reason in decision["refusals"]))

    def test_apply_create_allows_new_store_with_flag(self):
        decision = key_ops.decide(self._intent(
            apply=True, secret_out="s.txt", store_exists=False,
            allow_new_store=True))
        self.assertEqual(0, decision["exit_code"], decision["refusals"])
        self.assertEqual("apply", decision["mode"])

    def test_list_refuses_apply(self):
        decision = key_ops.decide(self._intent(action="list", apply=True))
        self.assertEqual(2, decision["exit_code"])

    def test_revoke_requires_key_id(self):
        decision = key_ops.decide(self._intent(action="revoke"))
        self.assertEqual(2, decision["exit_code"])

    def test_revoke_unknown_key_id_is_refused(self):
        decision = key_ops.decide(self._intent(action="revoke",
                                              key_id="nope", key_row=None))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("查不到" in reason
                            for reason in decision["refusals"]))

    def test_revoke_of_a_production_key_is_refused(self):
        decision = key_ops.decide(self._intent(
            action="revoke", key_id="k1",
            key_row={"key_id": "k1", "name": "小墨", "deleted_at": None}))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("不是测试 Key" in reason
                            for reason in decision["refusals"]))

    def test_revoke_of_an_already_deleted_key_is_refused(self):
        decision = key_ops.decide(self._intent(
            action="revoke", key_id="k1",
            key_row={"key_id": "k1", "name": TEST_LABEL,
                      "deleted_at": "2026-09-27T00:00:00.000000Z"}))
        self.assertEqual(2, decision["exit_code"])
        self.assertTrue(any("已经删除过" in reason
                            for reason in decision["refusals"]))

    def test_revoke_of_a_test_key_is_allowed(self):
        decision = key_ops.decide(self._intent(
            action="revoke", key_id="k1",
            key_row={"key_id": "k1", "name": TEST_LABEL, "deleted_at": None}))
        self.assertEqual(0, decision["exit_code"], decision["refusals"])


class KeyOpsStoreTests(unittest.TestCase):
    """临时库上的 dry-run / apply 全流程（绝不碰生产库）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="um-share-ops-")
        self.dir = Path(self._tmp.name)
        self.store = str(self.dir / "share-store.db")
        self.secret = str(self.dir / "test-key.txt")
        # 隔离哨兵：把「默认存储」的两处解析点（``share_management`` 常量与
        # ``SHARE_STORE_FILE`` 环境变量）都改指到临时目录里的哨兵路径。显式
        # ``--store`` 的操作若漏用参数、回退默认，就会落到哨兵上（而不是
        # 生产库）；生产库因此不必被打开，读哈希见证也一并取消。
        self.default_sentinel = self.dir / "default-store-sentinel.db"
        constant = mock.patch.object(share_management, "DEFAULT_STORE_FILE",
                                     self.default_sentinel)
        constant.start()
        self.addCleanup(constant.stop)
        env = mock.patch.dict(os.environ, {
            "SHARE_STORE_FILE": str(self.default_sentinel)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, *extra):
        return _run_main(key_ops, ["--create", "--label", TEST_LABEL,
                                   "--store", self.store,
                                   "--secret-out", self.secret, *extra])

    def _apply_create(self, label=TEST_LABEL):
        """临时库不存在 → 必须显式 --allow-new-store（新库不是生产库）。"""
        return _run_main(key_ops, ["--create", "--label", label, "--apply",
                                   "--allow-new-store", "--store", self.store,
                                   "--secret-out", self.secret])

    def _rows(self):
        store = key_ops.read_store(self.store)
        self.assertIsNone(store["error"], store["error"])
        return store

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        code, out, _err = self._create("--dry-run")
        self.assertEqual(0, code)
        self.assertFalse(os.path.exists(self.store),
                          "dry-run 绝不能创建存储")
        self.assertFalse(os.path.exists(self.secret))
        self.assertIn("DRY-RUN", out)
        self.assertIn("将写入字段", out)
        self.assertIn("key_id 形态", out)
        self.assertIn("config_generation +1", out)
        self.assertIn("必须立刻撤销", out)
        self.assertIn(TEST_LABEL, out)

    def test_unreadable_store_is_not_folded_into_missing(self):
        """读不到 ≠ 不存在（动作门 §1.5）：非 SQLite 文件 → store_unreadable。"""
        broken = self.dir / "broken.db"
        broken.write_text("这不是一个 SQLite 库", encoding="utf-8")
        store = key_ops.read_store(str(broken))
        self.assertTrue(store["exists"])
        self.assertEqual("store_unreadable", store["reason"])
        self.assertTrue(store["error"].startswith("store_"), store["error"])
        self.assertIn("failed:", store["error"])
        self.assertEqual([], store["keys"])
        code, out, err = _run_main(key_ops, ["--list", "--store", str(broken)])
        self.assertEqual(2, code)
        self.assertIn("store_unreadable", out)
        self.assertIn("store_unreadable", err)

    def test_apply_create_on_an_unreadable_store_is_refused_not_a_traceback(self):
        """读不到 ≠ 新库：写面不得静默初始化 0 字节/损坏文件（B1 复核发现）。"""
        broken = self.dir / "broken.db"
        broken.write_text("这不是一个 SQLite 库", encoding="utf-8")
        before = broken.read_bytes()
        code, _out, err = _run_main(key_ops, [
            "--create", "--label", TEST_LABEL, "--apply", "--store",
            str(broken), "--secret-out", self.secret])
        self.assertEqual(2, code)
        self.assertIn("store_unreadable", err)
        self.assertIn("--allow-new-store", err)
        self.assertEqual(before, broken.read_bytes(), "被拒绝的 create 绝不能写盘")
        self.assertFalse(os.path.exists(self.secret))
        # 给了 --allow-new-store 也不崩：StoreError 按「未写盘」exit 2 报，
        # 不冒泡 traceback（非 SQLite 文件 sqlite 初始化也会失败）。
        code, _out, err = _run_main(key_ops, [
            "--create", "--label", TEST_LABEL, "--apply", "--allow-new-store",
            "--store", str(broken), "--secret-out", self.secret])
        self.assertEqual(2, code)
        self.assertIn("未写盘", err)
        self.assertIn("StoreError", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(before, broken.read_bytes())
        self.assertFalse(os.path.exists(self.secret))

    def test_zero_byte_store_reads_unreadable_and_apply_needs_the_flag(self):
        """0 字节文件：读面「读不到」、写面默认拒绝（不静默初始化），夹具固定两面对齐。"""
        empty = self.dir / "empty.db"
        empty.write_bytes(b"")
        code, out, err = _run_main(key_ops, ["--list", "--store", str(empty)])
        self.assertEqual(2, code)
        self.assertIn("store_unreadable", out)
        self.assertIn("store_unreadable", err)
        code, _out, err = _run_main(key_ops, [
            "--create", "--label", TEST_LABEL, "--apply", "--store",
            str(empty), "--secret-out", self.secret])
        self.assertEqual(2, code)
        self.assertIn("store_unreadable", err)
        self.assertEqual(b"", empty.read_bytes(), "默认不得静默初始化 0 字节文件")
        self.assertFalse(os.path.exists(self.secret))
        # 显式 --allow-new-store 才允许把它初始化成分享库（并在计划里写明）。
        code, out, err = _run_main(key_ops, [
            "--create", "--label", TEST_LABEL, "--apply", "--allow-new-store",
            "--store", str(empty), "--secret-out", self.secret])
        self.assertEqual(0, code, err)
        self.assertIn("已给 --allow-new-store → --apply 会新建/初始化它", out)
        self.assertGreater(empty.stat().st_size, 0)
        self.assertTrue(os.path.exists(self.secret))

    def test_dry_run_of_a_revoke_does_not_touch_the_store(self):
        code, _out, _err = self._apply_create()
        self.assertEqual(0, code)
        before = _file_sha256(self.store)
        store = self._rows()
        key_id = store["keys"][0]["key_id"]
        generation = store["generation"]["config_generation"]
        code, out, _err = _run_main(key_ops, ["--revoke", "--key-id", key_id,
                                               "--dry-run",
                                               "--store", self.store])
        self.assertEqual(0, code)
        self.assertEqual(before, _file_sha256(self.store))
        after = self._rows()
        self.assertEqual(generation, after["generation"]["config_generation"])
        self.assertIsNone(after["keys"][0]["deleted_at"])
        self.assertEqual(1, after["keys"][0]["enabled"])
        self.assertIn("fail-closed", out)

    def test_apply_create_writes_the_row_and_never_prints_the_plaintext(self):
        code, out, err = self._apply_create()
        self.assertEqual(0, code, err)
        self.assertTrue(os.path.exists(self.store))
        self.assertTrue(os.path.exists(self.secret))
        store = self._rows()
        self.assertEqual(1, len(store["keys"]))
        row = store["keys"][0]
        self.assertEqual(TEST_LABEL, row["name"])
        self.assertEqual(1, row["enabled"])
        self.assertEqual(1, row["max_concurrency"])
        self.assertIsNone(row["deleted_at"])
        self.assertEqual(1, row["row_version"])
        # 新库从 config_generation=1 起，create 把它推到 2（desired），
        # 而 NoopBroker 无法确认 applied → in_sync=False（如实打印）。
        self.assertEqual(2, store["generation"]["config_generation"])
        self.assertEqual(0, store["generation"]["applied_generation"])
        secret = Path(self.secret).read_text(encoding="utf-8")
        self.assertGreaterEqual(len(secret), 32)
        # 掩码只给 scheme + 末 4 字符（share_store.py:85-87），不是明文。
        self.assertEqual("um-share-****" + secret[-4:], row["masked_key"])
        # 明文绝不进 stdout/stderr（只进 --secret-out 文件）。
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)
        self.assertNotIn(secret, json.dumps(row))
        # 未在线发布：NoopBroker 不确认，如实打印。
        self.assertIn("published = False", out)
        self.assertIn("必须立刻撤销", out)

    def test_apply_create_refuses_to_overwrite_the_secret_file(self):
        self._apply_create()
        before = _file_sha256(self.store)
        code, _out, err = _run_main(key_ops, [
            "--create", "--label", TEST_LABEL + "-2", "--apply",
            "--allow-new-store", "--store", self.store,
            "--secret-out", self.secret])
        self.assertEqual(2, code)
        self.assertIn("拒绝覆盖", err)
        self.assertEqual(before, _file_sha256(self.store),
                          "被拒绝的 create 绝不能写盘")

    def test_apply_create_refuses_a_missing_store_without_the_flag(self):
        code, _out, err = self._create("--apply")
        self.assertEqual(2, code)
        self.assertIn("--allow-new-store", err)
        self.assertIn("store_missing", err)
        self.assertFalse(os.path.exists(self.store), "被拒绝的 create 绝不能建库")
        code, _out, err = self._apply_create()
        self.assertEqual(0, code, err)
        self.assertTrue(os.path.exists(self.store))

    def test_apply_revoke_writes_a_tombstone_and_reports_the_partial_state(self):
        self._apply_create()
        row = self._rows()["keys"][0]
        code, out, _err = _run_main(key_ops, [
            "--revoke", "--key-id", row["key_id"], "--apply",
            "--store", self.store, "--broker", "none"])
        # NoopBroker 不能确认在线发布 → 存储已变更、发布未确认（fail-closed）。
        self.assertEqual(3, code)
        after = self._rows()
        self.assertEqual(0, after["keys"][0]["enabled"])
        self.assertIsNotNone(after["keys"][0]["deleted_at"])
        self.assertIn("未确认", out)
        self.assertIn("blocked", out)
        self.assertIn("必须立刻撤销", out)

    def test_list_on_a_missing_store_is_not_an_empty_list(self):
        missing = str(self.dir / "absent.db")
        code, out, err = _run_main(key_ops, ["--list", "--store", missing])
        self.assertEqual(2, code)
        self.assertIn("存储不存在", out)
        self.assertIn("store_missing", out)
        self.assertIn("store_missing", err)
        self.assertFalse(os.path.exists(missing))

    def test_list_shows_the_created_key_and_marks_it_as_test(self):
        self._apply_create()
        code, out, _err = _run_main(key_ops, ["--list", "--store", self.store])
        self.assertEqual(0, code)
        row = self._rows()["keys"][0]
        self.assertIn(row["key_id"], out)
        self.assertIn("[测试]", out)
        self.assertIn(TEST_LABEL, out)
        secret = Path(self.secret).read_text(encoding="utf-8")
        self.assertNotIn(secret, out)

    def test_broker_console_refuses_a_mismatched_store(self):
        code, _out, err = _run_main(key_ops, [
            "--list", "--broker", "console", "--store",
            str(self.dir / "other.db")])
        self.assertEqual(2, code)
        self.assertIn("--store", err)
        self.assertFalse(os.path.exists(str(self.dir / "other.db")))

    def test_the_default_store_is_never_touched(self):
        """显式 ``--store`` 的操作绝不回退到默认存储（哨兵从未被创建）。"""
        self._apply_create()
        self.assertFalse(self.default_sentinel.exists(),
                         "操作回退到了默认存储（哨兵被创建）")

    def test_default_resolution_lands_on_the_sentinel(self):
        """入口负控：不给 ``--store`` 时默认档确实落在哨兵上（上面的断言不恒真）。"""
        self.assertEqual(str(self.default_sentinel),
                         key_ops.default_store_path())
        code, out, _err = _run_main(key_ops, ["--list"])
        self.assertEqual(2, code)
        self.assertIn(str(self.default_sentinel), out)
        self.assertIn("store_missing", out)
        self.assertFalse(self.default_sentinel.exists(),
                         "只读的 --list 绝不能创建哨兵")


# ------------------------------------------------- metering_request_probe 夹具
ENTRY_GATES_OK = {
    "root": {"status": 404, "code": "route_not_shared", "error": None},
    "whitelist": {"status": 401, "code": "invalid_api_key", "error": None},
}
NEW_ROW = {"time": "2026-09-27T14:10:00+08:00", "model": "glm-5.3-flash",
           "key_id": "key_test", "attribution": "verified"}


class FakeDeps:
    """探针夹具：记录调用顺序；send 会先断言「计划已打印」。"""

    def __init__(self, *, gates_before=None, gates_after=None,
                 funnel_before=True, funnel_after=True, sends=None,
                 snapshots=None, ledger_path="share.jsonl",
                 ledger_error=None, credential="um-share-" + "x" * 43,
                 credential_error=None, priced=True, cost=0.000038,
                 acceptance_ok=True):
        self.calls = []
        self.sent = []
        self.buffer = None
        self.gates_before = gates_before or ENTRY_GATES_OK
        self.gates_after = gates_after or ENTRY_GATES_OK
        self.funnel_before = funnel_before
        self.funnel_after = funnel_after
        self.sends = list(sends or [{"status": 200, "duration_s": 1.0,
                                     "error": None,
                                     "summary": {"model": "glm-5.3-flash",
                                                  "usage": {"prompt_tokens": 5,
                                                            "completion_tokens": 3},
                                                  "choices": 1,
                                                  "error_code": None}}])
        self.snapshots = list(snapshots or [
            {"rows": 10, "verified": 0, "tail": [], "error": None},
            {"rows": 11, "verified": 1, "tail": [NEW_ROW], "error": None}])
        self.ledger_path = ledger_path
        self.ledger_error = ledger_error
        self.credential = credential
        self.credential_error = credential_error
        self.cost = {"priced": priced, "cost_usd": cost, "matched": "glm",
                      "peak": False, "version_id": None,
                      "prompt_tokens": 4, "error": None} \
            if priced else {"priced": False, "cost_usd": 0.0, "matched": None,
                             "peak": False, "version_id": None,
                             "prompt_tokens": None, "error": "价格未知"}
        self.acceptance_ok = acceptance_ok

    # ------------------------------------------------------------ 注入面
    def get(self, port, path):
        self.calls.append(("get", port, path))
        if path == "/":
            return _gate_response(self.gates_before if not self.sent
                                  else self.gates_after, "root", 404,
                                  "route_not_shared")
        if path == "/v1/models":
            return _gate_response(self.gates_before if not self.sent
                                  else self.gates_after, "whitelist", 401,
                                  "invalid_api_key")
        funnel = self.funnel_before if not self.sent else self.funnel_after
        return {"status": 200, "json": {"funnel_on": funnel}, "error": None}

    def send(self, plan, credential):
        if self.buffer is not None:
            text = self.buffer.getvalue()
            for marker in ("[计划] 请求数", "[计划] 模型", "[计划] 费用上限",
                            "[计划] 完成后入口状态"):
                assert marker in text, "计划未先打印：%s" % marker
        self.calls.append(("send", plan["path"], plan["method"]))
        self.sent.append({"plan": plan, "credential": credential})
        index = min(len(self.sent) - 1, len(self.sends) - 1)
        return dict(self.sends[index])

    def ledger_snapshot(self, path):
        self.calls.append(("ledger", path))
        if self.snapshots:
            return dict(self.snapshots.pop(0))
        return {"rows": 11, "verified": 1, "tail": [NEW_ROW], "error": None}

    def resolve_ledger_path(self):
        return self.ledger_path, self.ledger_error

    def read_credential(self, path):
        return self.credential, self.credential_error

    def estimate_cost(self, model, prompt_chars, max_tokens):
        return dict(self.cost)

    def acceptance(self):
        return ({"facts": True},
                {"ok": self.acceptance_ok,
                 "items": [{"id": "fact1_file_ready", "verdict": "pass"}]},
                "== 三事实验收（动作门 §④，只读） ==\n结论：%s"
                % ("三件事实都成立" if self.acceptance_ok else "存在未成立项"))

    def deps(self):
        return {"get": self.get, "send": self.send,
                "ledger_snapshot": self.ledger_snapshot,
                "resolve_ledger_path": self.resolve_ledger_path,
                "read_credential": self.read_credential,
                "estimate_cost": self.estimate_cost,
                "acceptance": self.acceptance}


def _gate_response(gates, name, status, code):
    gate = (gates or {}).get(name)
    if gate is None:
        return {"status": None, "json": None, "error": "URLError"}
    return {"status": gate["status"],
            "json": {"error": {"code": gate["code"]}}, "error": gate["error"]}


def _run_probe(argv, deps):
    return _run_main(probe, argv, deps.deps())


class ProbeStartGateTests(unittest.TestCase):
    """``--run`` 的上限拒绝四态：缺任一参数即退出码 2，且不发任何请求。"""

    def _refuse(self, argv, expected_missing):
        deps = FakeDeps()
        code, _out, err = _run_probe(argv, deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent, "被拒绝的启动绝不能发请求")
        for name in expected_missing:
            self.assertIn(name, err)
        return err

    def test_run_alone_is_refused(self):
        err = self._refuse(["--run"],
                            ["--authorized-requests", "--max-cost-usd",
                             "--model", "--credential-file"])
        self.assertIn("不发任何请求", err)

    def test_run_with_count_only_is_refused(self):
        self._refuse(["--run", "--authorized-requests", "1"],
                      ["--max-cost-usd", "--model", "--credential-file"])

    def test_run_without_model_is_refused(self):
        self._refuse(["--run", "--authorized-requests", "1",
                       "--max-cost-usd", "0.05", "--credential-file", "c.txt"],
                      ["--model"])

    def test_run_without_credential_file_is_refused(self):
        self._refuse(["--run", "--authorized-requests", "1",
                       "--max-cost-usd", "0.05", "--model", "glm-5.3-flash"],
                      ["--credential-file"])

    def test_invalid_values_are_refused_even_without_run(self):
        deps = FakeDeps()
        code, _out, err = _run_probe(["--authorized-requests", "0"], deps)
        self.assertEqual(2, code)
        self.assertIn("≥1", err)
        code, _out, err = _run_probe(["--max-cost-usd", "0"], deps)
        self.assertEqual(2, code)
        self.assertIn(">0", err)

    def test_dry_run_prints_the_four_things_and_touches_nothing(self):
        # 默认依赖全是真网络/账本/验收；dry-run 一个都不该被调到。
        code, out, _err = _run_main(probe, [])
        self.assertEqual(0, code)
        self.assertIn("[计划] 请求数", out)
        self.assertIn("[计划] 模型", out)
        self.assertIn("[计划] 费用上限", out)
        self.assertIn("[计划] 完成后入口状态", out)
        self.assertIn("dry-run：本工具没有发任何请求", out)

    def test_dry_run_with_values_prints_them_without_any_request(self):
        code, out, _err = _run_main(probe, [
            "--authorized-requests", "1", "--max-cost-usd", "0.05",
            "--model", "glm-5.3-flash"])
        self.assertEqual(0, code)
        self.assertIn("请求数（--authorized-requests）：1", out)
        self.assertIn("模型（--model）：glm-5.3-flash", out)
        self.assertIn("费用上限（--max-cost-usd）：0.05", out)
        self.assertIn("完成后入口状态（--post-entry-state）：unchanged", out)
        self.assertIn("POST http://127.0.0.1:8100/v1/chat/completions", out)
        self.assertIn("GET / → 404 route_not_shared", out)


class ProbeRunTests(unittest.TestCase):
    """真实运行的顺序、上限与停止规则（全部夹具，不连生产）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="um-share-probe-")
        self.dir = Path(self._tmp.name)
        self.credential_file = self.dir / "test-key.txt"
        self.credential_file.write_text("um-share-" + "x" * 43,
                                        encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _argv(self, count="1", cap="0.05", model="glm-5.3-flash"):
        return ["--run", "--authorized-requests", count, "--max-cost-usd",
                cap, "--model", model, "--credential-file",
                str(self.credential_file)]

    def test_plan_is_printed_before_any_request(self):
        deps = FakeDeps()
        code, out, _err = _run_probe(self._argv("2"), deps)
        self.assertEqual(0, code, out)
        self.assertEqual(2, len(deps.sent), "请求数不得超过 --authorized-requests")
        self.assertIn("[计划] 请求数（--authorized-requests）：2", out)
        self.assertIn("出现（新增 1 行", out)
        self.assertIn("key_test", out)
        self.assertIn("三事实验收", out)
        self.assertIn("通过（退出码 0）", out)
        # 计划必须在任何请求之前打印：send 里的断言已核过，这里再核顺序。
        self.assertLess(out.index("[计划] 请求数"), out.index("逐条记录"))

    def test_stop_on_429_reports_the_code_and_sends_no_more(self):
        deps = FakeDeps(sends=[
            {"status": 429, "duration_s": 0.1, "error": None,
             "summary": {"error_code": "sharing_limit_reached",
                          "error_type": "share_gateway_error"}}])
        code, out, _err = _run_probe(self._argv("3"), deps)
        self.assertEqual(2, code)
        self.assertEqual(1, len(deps.sent), "429 后必须立即停止")
        self.assertIn("状态码 429 是停止条件", out)
        self.assertIn("sharing_limit_reached", out)

    def test_stop_on_401_and_403_reports_honestly(self):
        for status, code_name in ((401, "invalid_api_key"),
                                  (403, "forbidden")):
            deps = FakeDeps(sends=[
                {"status": status, "duration_s": 0.1, "error": None,
                 "summary": {"error_code": code_name}}])
            code, out, _err = _run_probe(self._argv("2"), deps)
            self.assertEqual(2, code)
            self.assertEqual(1, len(deps.sent))
            self.assertIn("状态码 %d 是停止条件" % status, out)

    def test_transport_error_stops_without_retry(self):
        deps = FakeDeps(sends=[{"status": None, "duration_s": 0.1,
                                 "error": "URLError", "summary": None}])
        code, out, _err = _run_probe(self._argv("2"), deps)
        self.assertEqual(2, code)
        self.assertEqual(1, len(deps.sent))
        self.assertIn("非 2xx", out)
        self.assertIn("URLError", out)

    def test_cost_above_the_cap_is_refused_before_sending(self):
        deps = FakeDeps(cost=0.5)
        code, _out, err = _run_probe(self._argv("1", cap="0.05"), deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent)
        self.assertIn("超过上限", err)

    def test_unpriced_model_is_refused_before_sending(self):
        deps = FakeDeps(priced=False)
        code, _out, err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent)
        self.assertIn("费用无法证明", err)

    def test_unreadable_credential_is_refused_before_sending(self):
        deps = FakeDeps(credential_error="credential_unreadable:OSError")
        code, _out, err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent)
        self.assertIn("凭据不可用", err)

    def test_unresolved_ledger_path_is_refused_before_sending(self):
        deps = FakeDeps(ledger_path=None, ledger_error="backend_observe_failed")
        code, _out, err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent)
        self.assertIn("账本路径不可解析", err)

    def test_entry_gate_mismatch_is_refused_before_sending(self):
        deps = FakeDeps(gates_before={"root": {"status": 404,
                                                "code": "route_not_shared",
                                                "error": None},
                                      "whitelist": {"status": 200,
                                                     "code": None,
                                                     "error": None}})
        code, _out, err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertEqual([], deps.sent)
        self.assertIn("入口门不符", err)

    def test_entry_state_flip_fails_the_final_check(self):
        deps = FakeDeps(funnel_after=False)
        code, out, _err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertIn("[FAIL] funnel_after", out)
        self.assertIn("未通过（退出码 2）", out)

    def test_entry_state_open_declaration_requires_the_funnel_on(self):
        deps = FakeDeps(funnel_before=True, funnel_after=True)
        argv = self._argv("1") + ["--post-entry-state", "open"]
        code, out, _err = _run_probe(argv, deps)
        self.assertEqual(0, code, out)
        self.assertIn("[PASS] funnel_after", out)

    def test_failed_acceptance_fails_the_run(self):
        deps = FakeDeps(acceptance_ok=False)
        code, out, _err = _run_probe(self._argv("1"), deps)
        self.assertEqual(2, code)
        self.assertIn("存在未成立项", out)

    def test_credential_is_read_from_the_file_and_never_printed(self):
        deps = FakeDeps()
        code, out, _err = _run_probe(self._argv("1"), deps)
        self.assertEqual(0, code, out)
        secret = self.credential_file.read_text(encoding="utf-8")
        self.assertNotIn(secret, out)
        self.assertEqual([secret], [item["credential"] for item in deps.sent])


class EntryGatePureTests(unittest.TestCase):
    """``gate_check`` / ``new_rows_between`` 的纯判定分支（字典进、字典出）。"""

    def _facts(self, status=404, code="route_not_shared", funnel=True):
        return {"gates": {"root": {"status": status, "code": code,
                                    "error": None},
                           "whitelist": {"status": 401,
                                          "code": "invalid_api_key",
                                          "error": None}},
                "funnel_on": funnel, "error": None}

    def test_unchanged_requires_both_gates_and_the_same_funnel(self):
        check = probe.gate_check(self._facts(), self._facts(), "unchanged")
        self.assertTrue(check["ok"], check["items"])

    def test_unchanged_fails_when_the_funnel_flips(self):
        check = probe.gate_check(self._facts(), self._facts(funnel=False),
                                  "unchanged")
        self.assertFalse(check["ok"])
        self.assertFalse([item for item in check["items"]
                           if item["id"] == "funnel_after"][0]["ok"])

    def test_unreadable_gate_is_not_a_pass(self):
        after = self._facts(status=None, code=None)
        check = probe.gate_check(self._facts(), after, "unchanged")
        self.assertFalse(check["ok"])

    def test_closed_declaration_requires_the_funnel_off(self):
        check = probe.gate_check(self._facts(), self._facts(funnel=False),
                                  "closed")
        self.assertTrue(check["ok"], check["items"])
        check = probe.gate_check(self._facts(), self._facts(funnel=True),
                                  "closed")
        self.assertFalse(check["ok"])

    def test_new_rows_are_counted_and_bounded(self):
        before = {"rows": 10, "tail": [], "error": None}
        after = {"rows": 11, "tail": [NEW_ROW], "error": None}
        out = probe.new_rows_between(before, after)
        self.assertEqual(1, out["rows"])
        self.assertEqual([NEW_ROW], out["new"])
        self.assertIsNone(out["error"])

    def test_unreadable_ledger_is_not_zero_new_rows(self):
        before = {"rows": 10, "tail": [], "error": None}
        after = {"rows": None, "tail": None,
                  "error": "ledger_unreadable:OSError"}
        out = probe.new_rows_between(before, after)
        self.assertIsNone(out["rows"])
        self.assertEqual("ledger_unreadable:OSError", out["error"])


# ------------------------------------- metering_acceptance 采集：零写副作用
class _NoWriteGuard:
    """运行时写哨兵：只读采集路径上出现任何写盘动作就立刻失败。

    ``io.open`` 与 ``builtins.open`` 都换（``Path.open``/``Path.write_text``
    走 ``io.open``，普通 ``open()`` 走 ``builtins.open``），删除／重命名／
    替换／建目录各边界同样拦住，``os.open`` 只在带写标志时拦、``os.utime``
    一律拦（它们绕不过哨兵）。``writes`` 空表才是「零写副作用」的**运行时
    证据**，不是注释里的声明。
    """

    _WRITE_FLAGS = ("w", "a", "x", "+")
    _OS_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
                       | os.O_TRUNC)

    def __init__(self):
        self.writes = []
        self._patchers = []

    def _fail(self, what):
        self.writes.append(what)
        raise AssertionError("只读采集路径出现写盘动作：%s" % what)

    def __enter__(self):
        real_open = io.open
        real_os_open = os.open

        def guard_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in self._WRITE_FLAGS):
                self._fail("open(%r, mode=%r)" % (file, mode))
            return real_open(file, mode, *args, **kwargs)

        def guard_os_open(path, flags, *args, **kwargs):
            if flags & self._OS_WRITE_FLAGS:
                self._fail("os.open(%r, flags=%r)" % (path, flags))
            return real_os_open(path, flags, *args, **kwargs)

        def guard_utime(*args, **kwargs):
            self._fail("utime%r" % (args,))

        def guard_remove(path, *args, **kwargs):
            self._fail("remove(%r)" % (path,))

        def guard_rename(*args, **kwargs):
            self._fail("rename/replace%r" % (args,))

        def guard_mkdir(*args, **kwargs):
            self._fail("mkdir/makedirs%r" % (args,))

        for owner, name, value in (
                (io, "open", guard_open),
                (builtins, "open", guard_open),
                (os, "open", guard_os_open),
                (os, "utime", guard_utime),
                (os, "remove", guard_remove),
                (os, "unlink", guard_remove),
                (os, "rename", guard_rename),
                (os, "replace", guard_rename),
                (os, "mkdir", guard_mkdir),
                (os, "makedirs", guard_mkdir)):
            patcher = mock.patch.object(owner, name, new=value)
            patcher.start()
            self._patchers.append(patcher)
        return self

    def __exit__(self, exc_type, exc, tb):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []
        return False


class MeteringAcceptanceCollectionTests(unittest.TestCase):
    """``metering_acceptance.collect_facts`` 在**陈旧** backend.pid 上零写。

    夹具：tempfile 临时目录 + ``CONSOLE_SHARE_BACKEND_DIR`` 指向它，里面放一份
    陈旧 pid 文件（PID 不匹配或进程已不存在）。采集只走**只读**那一档
    （``forget_stale=False`` + ``production_backend_pid_state``），断言文件仍在、
    size / mtime_ns / 内容逐字不变、事实照实报「陈旧」，且写哨兵一次都没响。
    反向对照证明开关不是恒真：同一份夹具下默认档仍会删。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.pid_path = os.path.join(self.backend_dir, "backend.pid")
        self.private_ledger = os.path.join(self._tmp.name, "private.jsonl")
        # 8100 运行实例探测替身：本套件不跑真实 netstat/CIM。
        probe = mock.patch.object(
            prod, "share_entry_instance_probe",
            lambda *a, **k: {"pid": None, "started_at": None,
                            "reason": "not_listening"})
        probe.start()
        self.addCleanup(probe.stop)

    def _write_stale_pid(self, pid=4242, text=None):
        with open(self.pid_path, "w", encoding="ascii") as handle:
            handle.write(str(pid) if text is None else text)
        os.utime(self.pid_path, (1000000000.0, 1000000000.0))
        stat = os.stat(self.pid_path)
        with open(self.pid_path, encoding="ascii") as handle:
            content = handle.read()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "content": content}

    def _assert_untouched(self, before):
        self.assertTrue(os.path.exists(self.pid_path),
                        "陈旧 backend.pid 被删掉了")
        stat = os.stat(self.pid_path)
        self.assertEqual(stat.st_size, before["size"], "size 变了")
        self.assertEqual(stat.st_mtime_ns, before["mtime_ns"], "mtime 变了")
        with open(self.pid_path, encoding="ascii") as handle:
            self.assertEqual(handle.read(), before["content"], "内容变了")

    def _ops(self, matches=False, listening=False):
        """ops 的三个 OS 边界全部替身化（零真实 tasklist/netstat/powershell）。"""
        ops = prod._ProductionBackendOps(
            gateway_dir=os.path.join(self.backend_dir, "chat2api"),
            port=8110, base_url="https://backend.example")
        for name, value in (("_pid_matches", lambda p, pid: matches),
                            ("_pid_alive", lambda pid: False),
                            ("_port_listening", lambda p: listening)):
            patcher = mock.patch.object(ops, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return ops

    def _get(self, port, path, timeout=None):
        """8096 读面替身：本套件不连任何端口。"""
        return {"status": None, "json": None, "error": "ConnectionRefusedError"}

    def _collect(self, ops):
        # 8110 健康探测也在它自己的窄边界上替身化（返回不可达）：本套件
        # 真的不连 127.0.0.1 的任何端口，模块 docstring 的隔离声明成立。
        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops), \
                mock.patch.object(prod, "_backend_health_snapshot",
                                  return_value=(False, None)), \
                mock.patch.object(acceptance, "private_ledger_path",
                                  return_value=(self.private_ledger, None)):
            return acceptance.collect_facts(get=self._get)

    def test_collection_never_removes_a_stale_pid_file(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        guard = _NoWriteGuard()
        with guard:
            facts = self._collect(ops)
        self.assertEqual(guard.writes, [])
        self._assert_untouched(before)
        self.assertEqual(facts["backend_pid"]["state"], "pid_stale")
        self.assertIs(facts["backend_pid"]["pid_file_present"], True)
        self.assertIs(facts["backend_pid"]["pid_matches"], False)
        self.assertEqual(facts["backend_pid"]["pid"], 4242)

    def test_collection_reports_running_backend_as_running(self):
        """对照：同一份只读报告在 PID 匹配 + 端口在听时报 running。"""
        ops = self._ops(matches=True, listening=True)
        self._write_stale_pid()
        guard = _NoWriteGuard()
        with guard:
            facts = self._collect(ops)
        self.assertEqual(guard.writes, [])
        self.assertEqual(facts["backend_pid"]["state"], "running")
        self.assertIs(facts["backend_pid"]["port_listening"], True)

    def test_default_observe_still_removes_it_the_tool_path_does_not(self):
        """反向对照：同一份夹具，工具采集档不删、默认档仍删。"""
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        facts = self._collect(ops)
        self._assert_untouched(before)
        self.assertEqual(facts["backend_pid"]["state"], "pid_stale")
        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops):
            observed = prod.production_backend_observe()
        self.assertIs(observed["running"], False)
        self.assertFalse(os.path.exists(self.pid_path),
                         "默认档必须仍然清理陈旧 pid 文件")

    def test_printed_acceptance_names_the_stale_pid_file(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        guard = _NoWriteGuard()
        output = io.StringIO()
        # The CLI must use the same private OS/file/network boundaries as
        # collect_facts; preparing a fixture beforehand does not bind main().
        with guard, \
                mock.patch.object(prod, "production_backend_ops", return_value=ops), \
                mock.patch.object(acceptance, "private_ledger_path",
                                  return_value=(self.private_ledger, None)), \
                mock.patch.object(acceptance, "http_get_json", side_effect=self._get) as get, \
                redirect_stdout(output):
            code = acceptance.main([])
        self.assertEqual(guard.writes, [])
        self._assert_untouched(before)
        self.assertEqual(get.call_args_list, [
            mock.call(acceptance.CONSOLE_PORT, "/api/console/share-management/status"),
            mock.call(acceptance.CONSOLE_PORT, "/api/console/share-usage"),
        ])
        text = output.getvalue()
        self.assertEqual(2, code)   # 三件事实不成立：门必须报非零
        self.assertIn("state=pid_stale", text)
        self.assertIn("未被删除", text)


if __name__ == "__main__":
    unittest.main()
