# -*- coding: utf-8 -*-
"""`share_chain_snapshot.py` 的窗口专用扩展夹具测试（不连生产）。

覆盖（执行轮 9 新增；round7 的 `test_um_share_tools.py` 继续独立存在）：

* 八类窗口信号（`window_signals`）逐项 `ok / absent / undecidable` 的分类，
  含**反例：非提升身份查不到 watchdog 任务 → `undecidable`，绝不记成「未注册」**；
* 第 ⑧ 类分享入口故障证据（`share_entry_faults`，broker `host-evidence{faults}`
  只读回包）的三态：缺文件（`verdict=null` → `undecidable`，**不填 0**）、
  空文件（可读、counts 全 0、`last_fault=null` → `absent`）与正常（末条故障 → `ok`）；
* 两个顶层证据节 `maintenance_state` / `supervision_state` 的三态：维护视图
  不可得 → `undecidable` 且不伪造记录状态；监督状态文件缺失 → `absent`、
  存在但读不出（空/畸形）→ `undecidable`，绝不折成空预算；
* `parse_watchdog_query` / `query_watchdog_task`（注入 runner，不跑真实 PowerShell）
  的解析与「查询没跑成」路径；
* `--compare` 新增 6 项判定的 PASS / FAIL / UNDECIDABLE 分支：
  读面键集迁移、8100 镜像/启动时间、8110 父 PID 关系、watchdog 任务、账本末行归因、
  分享入口故障证据；
* round7 兼容（只增不删）：两份 round7 schema 快照之间仍只出 10 项、语义不变，
  新增 6 项不纳入并在文本里打印说明；混用（一侧旧、一侧新）时纳入且缺失侧记
  `undecidable`。

隔离：全部夹具是内存字典；不连 127.0.0.1 的任何端口、不跑 netstat/CIM/PowerShell
（`query_watchdog_task` 用注入 runner）、不读账本文件、不写仓库 `data/`、
不重启任何服务、不生成密钥、不发模型请求。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_share_chain_window -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[1] / "tools" / "um-share"
             / "readonly")


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_" + module_name,
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("share_chain_snapshot")

FOUR_KEYS = ["key_chars", "key_file", "reason", "wired"]
SEVEN_KEYS = ["file_ready", "file_reason", "instance_loaded", "key_chars",
              "key_file", "request_attribution_detail",
              "request_attribution_observed"]

# round7 的 10 项判定 id（只增不删的基线；顺序也保留）。
ROUND7_ITEM_IDS = [
    "8096_health",
    "8100_listening",
    "8100_attribution",
    "8110_listening",
    "8110_auth",
    "funnel",
    "share_gateway",
    "share_ledger",
    "private_ledger",
    "read_surface_shape",
]

WINDOW_ITEM_IDS = [
    "key_set_transition",
    "gateway_image_start",
    "backend_parent_relation",
    "watchdog_task",
    "ledger_last_attribution",
    "share_entry_faults",
]


# --------------------------------------------------------------- 夹具构造
def _entry(status, code, error=None):
    return {"status": status, "code": code, "error": error}


def _listener(pids, reason=None):
    return {"pids": pids, "reason": reason}


def _signal(status, **extra):
    base = {"status": status, "reason": None, "detail": ""}
    base.update(extra)
    return base


def _watchdog_ok(state=4, state_text="Running"):
    return _signal("ok", task_name="umshare-watchdog", state=state,
                   state_text=state_text, elevated=True,
                   identity="DAICNT\\dacint",
                   detail="任务已注册：State=%s（%s）" % (state_text, state))


def _watchdog_undecidable(reason="not_visible_to_current_identity"):
    return _signal("undecidable", task_name="umshare-watchdog",
                   elevated=False, identity="DAICNT\\dacint", reason=reason,
                   detail="非提升身份查不到 SYSTEM 任务：按『读不到』记 undecidable，"
                          "绝不记成『未注册』")


def _fault_record(**overrides):
    record = {"at": 1790475900.5, "phase": "upstream", "status": 502,
              "code": "um_unavailable", "upstream_status": 503,
              "exception_type": None, "duration_ms": 120.5,
              "request_id": None, "stream_started": False}
    record.update(overrides)
    return record


def _faults_ok():
    """正常：故障文件可读且末条故障存在（`ok`）。"""
    counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
              "upstream": 3, "stream": 0}
    last = _fault_record()
    file_facts = {"present": True, "size": 4096, "mtime": 1790475900.0}
    return _signal("ok", last_fault=last, counts_by_phase=counts,
                   file=file_facts,
                   verdict={"last_fault": last, "counts_by_phase": counts,
                             "file": file_facts},
                   checked_at=1790475901, reason="ok",
                   detail="末条故障 phase='upstream' code='um_unavailable'")


def _faults_absent():
    """空文件：可读且确定无故障记录（`absent`，counts 全 0 来自读面）。"""
    counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
              "upstream": 0, "stream": 0}
    return _signal("absent", last_fault=None, counts_by_phase=counts,
                   file={"present": True, "size": 0, "mtime": 1790475900.0},
                   verdict={"last_fault": None, "counts_by_phase": counts,
                             "file": {"present": True, "size": 0,
                                      "mtime": 1790475900.0}},
                   checked_at=1790475901, reason="no_faults_recorded",
                   detail="故障文件可读且确定无故障记录（counts 全 0、"
                          "last_fault=null）")


def _maintenance_ok():
    """正常：维护视图可读且无租约在册（不伪造记录状态）。"""
    return _signal("ok", maintenance_state=None,
                   state_basis="lease_absent_state_not_exposed",
                   checked_at=1790475800, lease=None, lease_expired=False,
                   instance=None, last_outcome=None, reason="ok",
                   detail="维护视图可读但没有租约在册（maintenance-status "
                          "不回传记录状态）：maintenance_state 不猜")


def _supervision_ok():
    """正常：监督状态文件可读（运行意图 + 失败计数 + 最近决策）。"""
    return _signal("ok", desired_state="running",
                   desired_at_utc="2026-09-27T13:59:00+00:00",
                   desired_source="console", desired_boot_id="BOOT|2026-09-27",
                   watch_readable=True, watch_reason=None,
                   consecutive_failures=1, restart_attempts_utc=[],
                   idle_reason="", last_decision=None,
                   state_dir="C:\\watch", reason="ok",
                   detail="运行意图='running'；consecutive_failures=1")


def _v1_before():
    """round7 schema 的「重启前」快照（与 round7 夹具同形）。"""
    return {
        "tool": "share_chain_snapshot",
        "schema_version": 1,
        "captured_at": 1758950000.0,
        "captured_at_iso": "2026-09-27T14:00:00+08:00",
        "listeners": {
            "8000": _listener([35724]),
            "8096": _listener([51352]),
            "8100": _listener([37020]),
            "8110": _listener([54300]),
            "8320": _listener([8816]),
        },
        "health_8096": {"status": 200, "json": {"status": "ok",
                                                "service": "usagesrv",
                                                "port": 8096},
                        "error": None},
        "chain_parent": {"parent_port": 8096, "child_port": 8110,
                         "parent_pid": 51352, "child_pid": 54300,
                         "child_parent_pid": 51352,
                         "relation": "child_parent_is_parent_pid",
                         "reason": "ok"},
        "gateway_8100": {"port": 8100, "pid": 37020,
                         "image": "um-share-python.exe", "image_reason": "ok",
                         "started_at": 1790475720.8,
                         "started_at_reason": "ok", "reason": "ok"},
        "share_status": {"http_status": 200, "error": None,
                         "keys": list(FOUR_KEYS), "shape": "four_key",
                         "funnel_on": True, "file_ready": None,
                         "instance_loaded_value": None},
        "gateway_attribution": {
            "source": "in_process_share_attribution_status",
            "file_ready": False, "file_reason": "missing", "key_chars": None,
            "key_file": None, "instance_loaded_value": "unknown",
            "instance_loaded_basis": "inference",
            "instance_loaded_reason": "key_file_not_ready",
            "request_attribution_observed": False, "error": None},
        "backend_observe": {"running": True, "auth_state": "valid",
                            "auth_label": "认证有效",
                            "ledger_file": "usage-share-backend.jsonl",
                            "error": None},
        "share_entry_probe": {
            "root": _entry(404, "route_not_shared"),
            "health": _entry(404, "route_not_shared"),
            "whitelist_models": _entry(401, "invalid_api_key")},
        "operations": {"http_status": 200, "total": 50, "live": 0,
                       "error": None},
        "share_ledger": {"path": "usage-share-backend.jsonl", "rows": 1680,
                          "unparsed": 0,
                          "last": {"index": 1679,
                                   "time": "2026-09-26T23:11:25+08:00",
                                   "model": "GLM-5.3-Flash", "key_id": None,
                                   "attribution": "absent"},
                          "attributed_rows": [],
                          "attribution_counts": {"absent": 46, "legacy": 1634},
                          "error": None},
        "private_ledger": {"path": "usage-chat2api.jsonl", "rows": 93132,
                           "unparsed": 0, "key_id_nonempty": 0,
                           "attribution_verified": 0, "error": None},
        "not_covered": [],
    }


def _v1_after():
    """round7 schema 的「重启后、已接线」快照（与 round7 夹具同形）。"""
    snap = _v1_before()
    snap["listeners"]["8096"] = _listener([60000])
    snap["share_status"] = {"http_status": 200, "error": None,
                            "keys": list(SEVEN_KEYS), "shape": "seven_key",
                            "funnel_on": True, "file_ready": True,
                            "instance_loaded_value": "yes"}
    snap["gateway_attribution"] = {
        "source": "console_read_surface", "file_ready": True,
        "file_reason": "ok", "key_chars": 64,
        "key_file": r"C:\ProgramData\UMShareRun\run\share-attribution.key",
        "instance_loaded_value": "yes", "instance_loaded_basis": "inference",
        "instance_loaded_reason": "process_started_at_or_after_key_mtime",
        "request_attribution_observed": False, "error": None}
    snap["share_ledger"] = {"path": "usage-share-backend.jsonl", "rows": 1681,
                            "unparsed": 0,
                            "last": {"index": 1680,
                                     "time": "2026-09-27T14:10:00+08:00",
                                     "model": "GLM-5.3-Flash",
                                     "key_id": "key_test",
                                     "attribution": "verified"},
                            "attributed_rows": [{"index": 1680,
                                                 "time": "2026-09-27T14:10:00+08:00",
                                                 "model": "GLM-5.3-Flash",
                                                 "key_id": "key_test",
                                                 "attribution": "verified"}],
                            "attribution_counts": {"absent": 46, "legacy": 1634,
                                                    "verified": 1},
                            "error": None}
    return copy.deepcopy(snap)


def _v2_before():
    """窗口专用「重启前」快照：v1 之上只增 window_signals / 查询原文。"""
    snap = _v1_before()
    snap["schema_version"] = 2
    snap["window_signals"] = {
        "read_surface_keys": _signal("ok", keys=list(FOUR_KEYS),
                                    shape="four_key",
                                    detail="键集 %s（four_key）" % FOUR_KEYS),
        "gateway_8100_image_start": _signal(
            "ok", pid=37020, image="um-share-python.exe", image_expected=True,
            started_at=1790475720.8,
            detail="pid=37020 image=um-share-python.exe"),
        "backend_8110_pid_parent": _signal(
            "ok", backend_pid=54300, parent_pid=51352, console_pid=51352,
            parent_is_console_pid=True,
            detail="8110 PID=54300 父 PID=51352；父 PID 等于 8096 PID：True"),
        "watchdog_task": _watchdog_ok(),
        "share_entry_gates": _signal("ok", entries={
            "root": {"path": "/", "status": 404, "code": "route_not_shared",
                     "matches": True},
            "health": {"path": "/health", "status": 404,
                       "code": "route_not_shared", "matches": True},
            "whitelist_models": {"path": "/v1/models", "status": 401,
                                 "code": "invalid_api_key", "matches": True}},
            detail="root / → 404 route_not_shared"),
        "share_ledger_tail": _signal("ok", rows=1680, last_index=1679,
                                     last_attribution="absent",
                                     last_key_id=None,
                                     detail="rows=1680 末行 #1679"),
        "funnel": _signal("ok", funnel_on=True, detail="funnel_on=True"),
        "share_entry_faults": _faults_ok(),
    }
    snap["watchdog_task_query"] = {"query_ok": True, "elevated": True,
                                   "identity": "DAICNT\\dacint", "found": True,
                                   "state": 4, "state_text": "Running",
                                   "error": None, "error_kind": None}
    # 顶层证据节（契约 §8.3 的另外两个新信号；不进冻结的 WINDOW_SIGNAL_IDS）。
    snap["maintenance_state"] = _maintenance_ok()
    snap["supervision_state"] = _supervision_ok()
    return snap


def _v2_after(**overrides):
    """窗口后（健康窗口）：8096 PID 已变、读面 7 键、watchdog 仍 Running。"""
    snap = _v2_before()
    snap["listeners"]["8096"] = _listener([60000])
    snap["share_status"] = {"http_status": 200, "error": None,
                            "keys": list(SEVEN_KEYS), "shape": "seven_key",
                            "funnel_on": True, "file_ready": True,
                            "instance_loaded_value": "yes"}
    snap["gateway_attribution"] = {
        "source": "console_read_surface", "file_ready": True,
        "file_reason": "ok", "key_chars": 64,
        "key_file": r"C:\ProgramData\UMShareRun\run\share-attribution.key",
        "instance_loaded_value": "yes", "instance_loaded_basis": "inference",
        "instance_loaded_reason": "process_started_at_or_after_key_mtime",
        "request_attribution_observed": False, "error": None}
    snap["share_ledger"] = {"path": "usage-share-backend.jsonl", "rows": 1681,
                            "unparsed": 0,
                            "last": {"index": 1680,
                                     "time": "2026-09-27T14:10:00+08:00",
                                     "model": "GLM-5.3-Flash",
                                     "key_id": "key_test",
                                     "attribution": "verified"},
                            "attributed_rows": [{"index": 1680,
                                                 "time": "2026-09-27T14:10:00+08:00",
                                                 "model": "GLM-5.3-Flash",
                                                 "key_id": "key_test",
                                                 "attribution": "verified"}],
                            "attribution_counts": {"absent": 46, "legacy": 1634,
                                                    "verified": 1},
                            "error": None}
    signals = snap["window_signals"]
    signals["read_surface_keys"] = _signal(
        "ok", keys=list(SEVEN_KEYS), shape="seven_key",
        detail="键集 %s（seven_key）" % SEVEN_KEYS)
    signals["share_ledger_tail"] = _signal("ok", rows=1681, last_index=1680,
                                           last_attribution="verified",
                                           last_key_id="key_test",
                                           detail="rows=1681 末行 #1680")
    # 8096 已重启：8110 的父 PID 仍是旧 8096 PID（孤儿签名），8096 PID 换成 60000。
    signals["backend_8110_pid_parent"] = _signal(
        "ok", backend_pid=54300, parent_pid=51352, console_pid=60000,
        parent_is_console_pid=False,
        detail="8110 PID=54300 父 PID=51352；父 PID 等于 8096 PID：False")
    for key, value in overrides.items():
        snap[key] = value
    return copy.deepcopy(snap)


def _set_signal(snapshot, name, signal):
    snapshot["window_signals"][name] = signal
    return snapshot


def _verdicts(result):
    return {item["id"]: item["verdict"] for item in result["items"]}


def _item(result, item_id):
    for item in result["items"]:
        if item["id"] == item_id:
            return item
    raise AssertionError("no item %r in %r"
                         % (item_id, [i["id"] for i in result["items"]]))


# ----------------------------------------------------- watchdog 查询解析层
class WatchdogParseTests(unittest.TestCase):
    NON_ELEVATED_NOT_FOUND = json.dumps({
        "elevated": False, "name": "DAICNT\\dacint", "found": False,
        "state": None, "stateText": None,
        "error": "CimJobException|CmdletizationQuery_NotFound_TaskName,"
                 "Get-ScheduledTask"})
    ELEVATED_FOUND = json.dumps({
        "elevated": True, "name": "DAICNT\\dacint", "found": True,
        "state": 4, "stateText": "Running", "error": None})

    def test_elevated_found_reads_state(self):
        out = tool.parse_watchdog_query(self.ELEVATED_FOUND)
        self.assertTrue(out["query_ok"])
        self.assertTrue(out["elevated"])
        self.assertTrue(out["found"])
        self.assertEqual(4, out["state"])
        self.assertEqual("Running", out["state_text"])

    def test_non_elevated_not_found_keeps_the_error(self):
        out = tool.parse_watchdog_query(self.NON_ELEVATED_NOT_FOUND)
        self.assertTrue(out["query_ok"])
        self.assertFalse(out["elevated"])
        self.assertFalse(out["found"])
        self.assertEqual("not_found", out["error_kind"])

    def test_unreadable_output_is_not_a_negative(self):
        for text in (None, "", "not json", "[1,2]", json.dumps({"found": 1})):
            out = tool.parse_watchdog_query(text)
            self.assertFalse(out["query_ok"], repr(text))
            self.assertIsNone(out["found"], repr(text))

    def test_missing_elevation_field_is_not_query_ok(self):
        out = tool.parse_watchdog_query(
            json.dumps({"name": "x", "found": True, "state": 4}))
        self.assertFalse(out["query_ok"])
        self.assertIsNone(out["found"])

    def test_query_uses_the_injected_runner(self):
        calls = []

        def runner(args):
            calls.append(args)
            return self.ELEVATED_FOUND

        out = tool.query_watchdog_task(run=runner)
        self.assertTrue(out["query_ok"])
        self.assertEqual(1, len(calls))
        self.assertIn("umshare-watchdog", calls[0][-1])

    def test_query_runner_failure_is_not_a_negative(self):
        out = tool.query_watchdog_task(run=lambda _args: None)
        self.assertFalse(out["query_ok"])
        self.assertIsNone(out["found"])


# ------------------------------------------------------------ 八类窗口信号
class WindowSignalTests(unittest.TestCase):
    def test_read_surface_keys_ok_and_absent(self):
        ok = tool.read_surface_keys_signal(
            {"http_status": 200, "error": None, "keys": list(FOUR_KEYS),
             "shape": "four_key"})
        self.assertEqual("ok", ok["status"])
        self.assertEqual("four_key", ok["shape"])
        absent = tool.read_surface_keys_signal(
            {"http_status": 200, "error": None, "keys": None,
             "shape": "unknown"})
        self.assertEqual("absent", absent["status"])

    def test_read_surface_keys_unreadable_is_undecidable(self):
        out = tool.read_surface_keys_signal(
            {"http_status": None, "error": "URLError", "keys": None,
             "shape": "unknown"})
        self.assertEqual("undecidable", out["status"])

    def test_gateway_image_start_ok_absent_undecidable(self):
        ok = tool.gateway_image_start_signal(
            {"reason": "ok", "pid": 37020, "image": "um-share-python.exe",
             "started_at": 1.0})
        self.assertEqual("ok", ok["status"])
        self.assertTrue(ok["image_expected"])
        absent = tool.gateway_image_start_signal({"reason": "not_listening"})
        self.assertEqual("absent", absent["status"])
        undecidable = tool.gateway_image_start_signal(
            {"reason": "process_row_missing"})
        self.assertEqual("undecidable", undecidable["status"])

    def test_gateway_image_not_in_expected_set_is_flagged(self):
        out = tool.gateway_image_start_signal(
            {"reason": "ok", "pid": 1, "image": "python.exe",
             "started_at": 1.0})
        self.assertEqual("ok", out["status"])
        self.assertFalse(out["image_expected"])

    def test_backend_parent_relation_marks_the_console_pid(self):
        listeners = {"8096": _listener([51352]), "8110": _listener([54300])}
        rows = {54300: {"name": "python.exe", "parent_pid": 51352,
                         "started_at": 1.0, "started_at_reason": "ok"}}
        out = tool.backend_pid_parent_signal(listeners, rows)
        self.assertEqual("ok", out["status"])
        self.assertEqual(54300, out["backend_pid"])
        self.assertEqual(51352, out["parent_pid"])
        self.assertIs(True, out["parent_is_console_pid"])

    def test_backend_absent_and_unreadable_are_not_folded(self):
        absent = tool.backend_pid_parent_signal({"8110": _listener([])}, {})
        self.assertEqual("absent", absent["status"])
        undecidable = tool.backend_pid_parent_signal(
            {"8110": _listener([54300])}, None)
        self.assertEqual("undecidable", undecidable["status"])
        self.assertEqual("process_query_failed", undecidable["reason"])

    def test_watchdog_unreadable_is_undecidable_never_absent(self):
        """反例：非提升身份查不到 SYSTEM 任务——必须 undecidable，绝不是「未注册」。"""
        query = tool.parse_watchdog_query(
            WatchdogParseTests.NON_ELEVATED_NOT_FOUND)
        out = tool.watchdog_task_signal(query)
        self.assertEqual("undecidable", out["status"])
        self.assertNotEqual("absent", out["status"])
        self.assertEqual("not_visible_to_current_identity", out["reason"])
        self.assertIn("未注册", out["detail"])

    def test_watchdog_elevated_not_found_is_absent(self):
        query = tool.parse_watchdog_query(json.dumps({
            "elevated": True, "name": "DAICNT\\dacint", "found": False,
            "state": None, "stateText": None,
            "error": "CimJobException|CmdletizationQuery_NotFound_TaskName"}))
        out = tool.watchdog_task_signal(query)
        self.assertEqual("absent", out["status"])
        self.assertEqual("not_registered_elevated", out["reason"])

    def test_watchdog_query_failure_is_undecidable(self):
        out = tool.watchdog_task_signal(
            tool.parse_watchdog_query("not json"))
        self.assertEqual("undecidable", out["status"])
        self.assertEqual("query_failed", out["reason"])

    def test_watchdog_found_but_state_unreadable_is_undecidable(self):
        query = tool.parse_watchdog_query(json.dumps({
            "elevated": True, "name": "DAICNT\\dacint", "found": True,
            "state": None, "stateText": None, "error": None}))
        out = tool.watchdog_task_signal(query)
        self.assertEqual("undecidable", out["status"])
        self.assertEqual("state_unavailable", out["reason"])

    def test_share_entry_gates_ok_absent_undecidable(self):
        ok = tool.share_entry_gates_signal({
            "root": _entry(404, "route_not_shared"),
            "health": _entry(404, "route_not_shared"),
            "whitelist_models": _entry(401, "invalid_api_key")})
        self.assertEqual("ok", ok["status"])
        drift = tool.share_entry_gates_signal({
            "root": _entry(200, None),
            "health": _entry(404, "route_not_shared"),
            "whitelist_models": _entry(401, "invalid_api_key")})
        self.assertEqual("absent", drift["status"])
        self.assertEqual("gate_missing:root", drift["reason"])
        failed = tool.share_entry_gates_signal({
            "root": _entry(None, None, error="URLError"),
            "health": _entry(404, "route_not_shared"),
            "whitelist_models": _entry(401, "invalid_api_key")})
        self.assertEqual("undecidable", failed["status"])

    def test_share_ledger_tail_ok_absent_undecidable(self):
        ok = tool.share_ledger_tail_signal({
            "rows": 1680, "error": None,
            "last": {"index": 1679, "attribution": "absent", "key_id": None}})
        self.assertEqual("ok", ok["status"])
        self.assertEqual(1679, ok["last_index"])
        empty = tool.share_ledger_tail_signal({"rows": 0, "error": None,
                                               "last": None})
        self.assertEqual("absent", empty["status"])
        unreadable = tool.share_ledger_tail_signal(
            {"rows": 0, "error": "ledger_unreadable:FileNotFoundError"})
        self.assertEqual("undecidable", unreadable["status"])

    def test_funnel_ok_absent_undecidable(self):
        ok = tool.funnel_signal({"http_status": 200, "error": None,
                                 "funnel_on": False})
        self.assertEqual("ok", ok["status"])
        self.assertIs(False, ok["funnel_on"])
        absent = tool.funnel_signal({"http_status": 200, "error": None,
                                     "funnel_on": None})
        self.assertEqual("absent", absent["status"])
        unreadable = tool.funnel_signal({"http_status": None,
                                         "error": "URLError",
                                         "funnel_on": None})
        self.assertEqual("undecidable", unreadable["status"])

    def test_build_window_signals_covers_all_eight_ids(self):
        listeners = {"8096": _listener([51352]), "8110": _listener([54300])}
        rows = {54300: {"name": "python.exe", "parent_pid": 51352,
                         "started_at": 1.0, "started_at_reason": "ok"}}
        signals = tool.build_window_signals(
            listeners, rows,
            {"http_status": 200, "error": None, "keys": list(FOUR_KEYS),
             "shape": "four_key", "funnel_on": True},
            {"reason": "ok", "pid": 37020, "image": "um-share-python.exe",
             "started_at": 1.0},
            {"root": _entry(404, "route_not_shared"),
             "health": _entry(404, "route_not_shared"),
             "whitelist_models": _entry(401, "invalid_api_key")},
            {"rows": 1680, "error": None,
             "last": {"index": 1679, "attribution": "absent"}},
            tool.parse_watchdog_query(
                WatchdogParseTests.NON_ELEVATED_NOT_FOUND),
            {"reply": {"check": "faults", "verdict": {
                "last_fault": _fault_record(),
                "counts_by_phase": {"auth": 0, "config": 0, "body": 0,
                                    "limit": 0, "upstream": 3, "stream": 0},
                "file": {"present": True, "size": 4096,
                         "mtime": 1790475900.0}},
                "detail": "read tail ok", "checked_at": 1790475901},
             "error": None})
        self.assertEqual(sorted(tool.WINDOW_SIGNAL_IDS), sorted(signals))
        self.assertEqual("undecidable",
                          signals["watchdog_task"]["status"])
        for name in tool.WINDOW_SIGNAL_IDS:
            if name != "watchdog_task":
                self.assertEqual("ok", signals[name]["status"], name)


# ------------------------------------------- 第 ⑧ 类信号：分享入口故障证据
class ShareEntryFaultsSignalTests(unittest.TestCase):
    """broker `host-evidence{faults}` 只读回包的三态：缺文件 / 空文件 / 正常。"""

    def test_missing_file_is_undecidable_and_never_zero(self):
        """缺文件：broker 回 `verdict=null` + `detail=fault_log_absent`。"""
        out = tool.share_entry_faults_signal({
            "reply": {"check": "faults", "verdict": None,
                      "detail": "fault_log_absent", "checked_at": 1},
            "error": None})
        self.assertEqual("undecidable", out["status"])
        self.assertIsNone(out["last_fault"])
        self.assertIsNone(out["counts_by_phase"], "不可得时绝不填 0")
        self.assertEqual("fault_log_absent", out["reason"])
        self.assertEqual(1, out["checked_at"])

    def test_empty_file_is_absent_with_zero_counts_from_the_read_face(self):
        """空文件：可读且确定无故障记录（counts 全 0 来自 broker 读面）。"""
        counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
                  "upstream": 0, "stream": 0}
        out = tool.share_entry_faults_signal({
            "reply": {"check": "faults", "verdict": {
                "last_fault": None, "counts_by_phase": counts,
                "file": {"present": True, "size": 0, "mtime": 1.0}},
                "detail": "read tail ok", "checked_at": 2},
            "error": None})
        self.assertEqual("absent", out["status"])
        self.assertEqual("no_faults_recorded", out["reason"])
        self.assertEqual(counts, out["counts_by_phase"])
        self.assertIsNone(out["last_fault"])

    def test_normal_fault_record_is_ok(self):
        last = _fault_record()
        counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
                  "upstream": 3, "stream": 0}
        out = tool.share_entry_faults_signal({
            "reply": {"check": "faults", "verdict": {
                "last_fault": last, "counts_by_phase": counts,
                "file": {"present": True, "size": 4096, "mtime": 2.0}},
                "detail": "read tail ok", "checked_at": 3},
            "error": None})
        self.assertEqual("ok", out["status"])
        self.assertEqual(last, out["last_fault"])
        self.assertEqual(counts, out["counts_by_phase"])
        self.assertEqual(3, out["checked_at"])
        self.assertIn("um_unavailable", out["detail"])

    def test_broker_evidence_unavailable_is_undecidable_not_absent(self):
        """未接线 / 不可达 / 旧 broker：读不到 ≠ 无故障，绝不折成 absent。"""
        out = tool.share_entry_faults_signal(
            {"reply": None, "error": "broker_evidence_unavailable"})
        self.assertEqual("undecidable", out["status"])
        self.assertNotEqual("absent", out["status"])
        self.assertEqual("broker_evidence_unavailable", out["reason"])
        self.assertIsNone(out["counts_by_phase"])

    def test_verdict_null_never_folds_into_no_faults(self):
        """`verdict=null` 是 broker 的诚实不可裁定，不是「无故障」。"""
        out = tool.share_entry_faults_signal({
            "reply": {"check": "faults", "verdict": None,
                      "detail": "fault_log_absent", "checked_at": 1},
            "error": None})
        self.assertEqual("undecidable", out["status"])
        self.assertNotEqual("absent", out["status"])

    def test_shape_violations_are_rejected(self):
        for reply in ({"check": "limits", "verdict": {}, "detail": "x"},
                      {"check": "faults", "verdict": True, "detail": "x"},
                      {"check": "faults", "verdict": {"last_fault": {},
                                                     "counts_by_phase": None},
                       "detail": "x"},
                      {"check": "faults", "verdict": {"last_fault": 1,
                                                     "counts_by_phase": {}},
                       "detail": "x"}):
            out = tool.share_entry_faults_signal({"reply": reply, "error": None})
            self.assertEqual("undecidable", out["status"], repr(reply))
            self.assertIsNone(out["counts_by_phase"], repr(reply))


# ------------------------------------- 顶层证据节：维护状态 / 监督状态（§8.3）
class MaintenanceStateSignalTests(unittest.TestCase):
    """broker `maintenance-status` 只读视图的三态；不伪造记录状态。"""

    def test_unavailable_view_is_undecidable_and_never_fabricated(self):
        out = tool.maintenance_state_signal(
            {"view": None, "error": "maintenance_view_unavailable"})
        self.assertEqual("undecidable", out["status"])
        self.assertEqual("maintenance_view_unavailable", out["reason"])
        self.assertIsNone(out["maintenance_state"])
        self.assertIsNone(out["lease"])
        self.assertIsNone(out["last_outcome"])

    def test_no_lease_is_ok_without_guessing_the_record_state(self):
        """无租约：视图可读但记录状态不暴露——不猜成 idle（restarting 可无租约）。"""
        out = tool.maintenance_state_signal({
            "view": {"checked_at": 5, "lease": None, "lease_expired": False,
                     "instance": None, "last_outcome": None},
            "error": None})
        self.assertEqual("ok", out["status"])
        self.assertIsNone(out["maintenance_state"])
        self.assertEqual("lease_absent_state_not_exposed", out["state_basis"])

    def test_lease_in_force_is_leased(self):
        lease = {"lease_id": "a" * 32, "component": "share_gateway",
                 "instance_id": "i", "round": 7, "created_at": 1,
                 "expires_at": 9}
        out = tool.maintenance_state_signal({
            "view": {"checked_at": 6, "lease": lease, "lease_expired": False,
                     "instance": {"state": "draining", "active_count": 2},
                     "last_outcome": None},
            "error": None})
        self.assertEqual("ok", out["status"])
        self.assertEqual("leased", out["maintenance_state"])
        self.assertEqual(lease, out["lease"])

    def test_expired_lease_is_not_guessed_as_idle(self):
        lease = {"lease_id": "b" * 32, "component": "share_gateway",
                 "expires_at": 1}
        out = tool.maintenance_state_signal({
            "view": {"checked_at": 7, "lease": lease, "lease_expired": True,
                     "instance": None, "last_outcome": None},
            "error": None})
        self.assertEqual("ok", out["status"])
        self.assertEqual("lease_expired", out["maintenance_state"])
        self.assertEqual("lease_expired_flag", out["state_basis"])

    def test_failed_last_outcome_passes_through(self):
        """§8.5：最近一次维护结果含失败原样透出，成功不清空失败。"""
        outcome = {"at": 8, "action": "gateway-restart", "ok": False,
                   "code": "unconfirmed", "detail": "stop=False"}
        out = tool.maintenance_state_signal({
            "view": {"checked_at": 9, "lease": None, "lease_expired": False,
                     "instance": None, "last_outcome": outcome},
            "error": None})
        self.assertEqual("ok", out["status"])
        self.assertEqual(outcome, out["last_outcome"])


class SupervisionStateSignalTests(unittest.TestCase):
    """`share_backend_supervision` 两个状态文件的三态；不折成空预算。"""

    def test_missing_state_file_is_absent(self):
        out = tool.supervision_state_signal({
            "desired": None, "desired_error": None,
            "watch": {"unreadable": True, "reason": "missing"},
            "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual("absent", out["status"])
        self.assertEqual("watch_state_missing", out["reason"])
        self.assertIsNone(out["consecutive_failures"])

    def test_empty_or_malformed_state_is_undecidable(self):
        out = tool.supervision_state_signal({
            "desired": None, "desired_error": None,
            "watch": {"unreadable": True, "reason": "malformed"},
            "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual("undecidable", out["status"])
        self.assertEqual("watch_state_unreadable:malformed", out["reason"])
        self.assertIsNone(out["consecutive_failures"], "绝不折成空预算")

    def test_readable_state_is_ok(self):
        out = tool.supervision_state_signal({
            "desired": {"state": "running", "at_utc": "2026-09-27T13:59:00Z",
                        "source": "console", "boot_id": "BOOT|x"},
            "desired_error": None,
            "watch": {"unreadable": False, "consecutive_failures": 1,
                       "restart_attempts_utc": [], "idle_reason": "",
                       "last_decision": None, "boot_id": "BOOT|x"},
            "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual("ok", out["status"])
        self.assertEqual("running", out["desired_state"])
        self.assertEqual(1, out["consecutive_failures"])
        self.assertTrue(out["watch_readable"])

    def test_unreadable_desired_is_reported_not_fabricated(self):
        out = tool.supervision_state_signal({
            "desired": None, "desired_error": "read_failed:OSError",
            "watch": {"unreadable": False, "consecutive_failures": 0,
                       "restart_attempts_utc": [], "idle_reason": "",
                       "last_decision": None, "boot_id": "BOOT|x"},
            "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual("ok", out["status"])
        self.assertIsNone(out["desired_state"])


class SnapshotEvidenceCollectionTests(unittest.TestCase):
    """`collect_snapshot` 的三个新证据面：同一份快照里可读、可注入、离线。"""

    def _collect(self, faults, maintenance, supervision):
        saved = {}
        names = ("gateway_attribution_facts", "backend_observe_facts",
                 "private_ledger_path", "scan_share_ledger",
                 "scan_private_ledger")
        for name in names:
            saved[name] = getattr(tool, name)
        self.addCleanup(lambda: [setattr(tool, name, value)
                                for name, value in saved.items()])

        def fake_get(port, path, timeout=None):
            if port == tool.CONSOLE_PORT and path == "/health":
                return {"status": 200, "json": {"status": "ok",
                                               "service": "usagesrv",
                                               "port": tool.CONSOLE_PORT},
                        "error": None}
            if port == tool.CONSOLE_PORT \
                    and path == "/api/console/share-management/status":
                return {"status": 200, "json": {
                    "attribution": {key: None for key in FOUR_KEYS},
                    "funnel_on": True}, "error": None}
            if port == tool.CONSOLE_PORT and path.startswith("/api/console/"):
                return {"status": 200, "json": {"operations": []},
                        "error": None}
            if port == tool.GATEWAY_PORT:
                return {"status": 404,
                        "json": {"error": {"code": "route_not_shared"}},
                        "error": None}
            return {"status": None, "json": None, "error": "URLError"}

        tool.gateway_attribution_facts = lambda: {
            "source": "in_process_share_attribution_status", "file_ready": False,
            "file_reason": "missing", "key_chars": None, "key_file": None,
            "instance_loaded_value": "unknown",
            "instance_loaded_basis": "inference",
            "instance_loaded_reason": "key_file_not_ready",
            "request_attribution_observed": False, "error": None}
        tool.backend_observe_facts = lambda forget_stale=False: {
            "running": True, "auth_state": "valid", "auth_label": "x",
            "ledger_file": "usage-share-backend.jsonl", "error": None}
        tool.private_ledger_path = lambda: ("private.jsonl", None)
        tool.scan_share_ledger = lambda path, tail_fields=None: {
            "path": path, "rows": 0, "unparsed": 0, "last": None,
            "attributed_rows": [], "attribution_counts": {}, "error": None}
        tool.scan_private_ledger = lambda path: {
            "path": path, "rows": 0, "unparsed": 0, "key_id_nonempty": 0,
            "attribution_verified": 0, "error": None}
        snapshot = tool.collect_snapshot(
            get=fake_get, cim=lambda pids: {},
            run=lambda args, timeout=None: "",
            watchdog=lambda run=None: tool.parse_watchdog_query(
                WatchdogParseTests.NON_ELEVATED_NOT_FOUND),
            broker_faults=lambda: faults, maintenance=lambda: maintenance,
            supervision=lambda: supervision)
        return snapshot

    def test_the_three_new_signals_are_readable_in_one_snapshot(self):
        snapshot = self._collect(
            {"reply": {"check": "faults", "verdict": {
                "last_fault": _fault_record(),
                "counts_by_phase": {"auth": 0, "config": 0, "body": 0,
                                    "limit": 0, "upstream": 3, "stream": 0},
                "file": {"present": True, "size": 4096, "mtime": 2.0}},
                "detail": "read tail ok", "checked_at": 4},
             "error": None},
            {"view": None, "error": "maintenance_view_unavailable"},
            {"desired": None, "desired_error": None,
             "watch": {"unreadable": True, "reason": "missing"},
             "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual(sorted(tool.WINDOW_SIGNAL_IDS),
                          sorted(snapshot["window_signals"]))
        self.assertEqual("ok",
                          snapshot["window_signals"]["share_entry_faults"]["status"])
        self.assertEqual("undecidable", snapshot["maintenance_state"]["status"])
        self.assertEqual("absent", snapshot["supervision_state"]["status"])
        text = tool.format_snapshot_summary(snapshot)
        self.assertIn("share_entry_faults: [OK]", text)
        self.assertIn("maintenance_state: [UNDECIDABLE]", text)
        self.assertIn("supervision_state: [ABSENT]", text)

    def test_missing_evidence_is_undecidable_not_ok(self):
        snapshot = self._collect(
            {"reply": None, "error": "broker_evidence_unavailable"},
            {"view": None, "error": "maintenance_view_unavailable"},
            {"desired": None, "desired_error": None,
             "watch": {"unreadable": True, "reason": "malformed"},
             "watch_error": None, "state_dir": "C:\\watch"})
        self.assertEqual("undecidable",
                          snapshot["window_signals"]["share_entry_faults"]["status"])
        self.assertIsNone(
            snapshot["window_signals"]["share_entry_faults"]["counts_by_phase"])
        self.assertEqual("undecidable", snapshot["maintenance_state"]["status"])
        self.assertEqual("undecidable", snapshot["supervision_state"]["status"])


# ------------------------------------------------------- 新增判定项（比对）
class WindowCompareTests(unittest.TestCase):
    def test_healthy_window_passes_every_item(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])
        self.assertTrue(result["window_signals_compared"])
        self.assertEqual("pass", _item(result, "watchdog_task")["verdict"])
        self.assertEqual("pass", _item(result, "key_set_transition")["verdict"])

    def test_watchdog_running_to_ready_fails(self):
        after = _v2_after()
        _set_signal(after, "watchdog_task",
                    _watchdog_ok(state=3, state_text="Ready"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "watchdog_task")["verdict"])

    def test_watchdog_ok_to_undecidable_is_undecidable(self):
        after = _v2_after()
        _set_signal(after, "watchdog_task", _watchdog_undecidable())
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable",
                          _item(result, "watchdog_task")["verdict"])
        self.assertNotEqual("fail",
                             _item(result, "watchdog_task")["verdict"])

    def test_watchdog_ok_to_absent_fails(self):
        after = _v2_after()
        _set_signal(after, "watchdog_task",
                     _signal("absent", task_name="umshare-watchdog",
                             reason="not_registered_elevated"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "watchdog_task")["verdict"])

    def test_backend_parent_orphan_signature_passes(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        self.assertEqual("pass",
                          _item(result, "backend_parent_relation")["verdict"])

    def test_backend_parent_is_new_console_pid_fails(self):
        after = _v2_after()
        _set_signal(after, "backend_8110_pid_parent", _signal(
            "ok", backend_pid=54300, parent_pid=60000, console_pid=60000,
            parent_is_console_pid=True))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "backend_parent_relation")["verdict"])

    def test_backend_restart_fails(self):
        after = _v2_after()
        _set_signal(after, "backend_8110_pid_parent", _signal(
            "ok", backend_pid=99999, parent_pid=51352, console_pid=60000,
            parent_is_console_pid=False))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "backend_parent_relation")["verdict"])

    def test_backend_parent_unknown_pid_is_undecidable(self):
        after = _v2_after()
        _set_signal(after, "backend_8110_pid_parent", _signal(
            "ok", backend_pid=54300, parent_pid=4242, console_pid=60000,
            parent_is_console_pid=False))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable",
                          _item(result, "backend_parent_relation")["verdict"])

    def test_gateway_image_or_start_time_change_fails(self):
        after = _v2_after()
        _set_signal(after, "gateway_8100_image_start", _signal(
            "ok", pid=37020, image="python.exe", image_expected=False,
            started_at=1790475720.8))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "gateway_image_start")["verdict"])
        after = _v2_after()
        _set_signal(after, "gateway_8100_image_start", _signal(
            "ok", pid=37020, image="um-share-python.exe", image_expected=True,
            started_at=1790475999.0))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual("fail",
                          _item(result, "gateway_image_start")["verdict"])

    def test_key_set_still_four_fails(self):
        after = _v2_after()
        after["share_status"]["keys"] = list(FOUR_KEYS)
        after["share_status"]["shape"] = "four_key"
        _set_signal(after, "read_surface_keys", _signal(
            "ok", keys=list(FOUR_KEYS), shape="four_key"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "key_set_transition")["verdict"])
        self.assertEqual("fail", _item(result, "read_surface_shape")["verdict"])

    def test_ledger_last_row_rewrite_fails(self):
        before = _v2_before()
        before["share_ledger"]["last"] = dict(before["share_ledger"]["last"],
                                              attribution="verified",
                                              key_id="key_a")
        _set_signal(before, "share_ledger_tail", _signal(
            "ok", rows=1680, last_index=1679, last_attribution="verified",
            last_key_id="key_a"))
        after = _v2_after()
        after["share_ledger"]["last"] = dict(after["share_ledger"]["last"],
                                             attribution="absent", key_id=None)
        _set_signal(after, "share_ledger_tail", _signal(
            "ok", rows=1681, last_index=1679, last_attribution="absent",
            last_key_id=None))
        result = tool.compare_snapshots(before, after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "ledger_last_attribution")["verdict"])

    def test_ledger_new_bad_signature_row_fails(self):
        after = _v2_after()
        after["share_ledger"]["last"] = dict(after["share_ledger"]["last"],
                                            attribution="bad_signature")
        _set_signal(after, "share_ledger_tail", _signal(
            "ok", rows=1681, last_index=1680,
            last_attribution="bad_signature", last_key_id="key_test"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "ledger_last_attribution")["verdict"])

    def test_ledger_new_row_with_attribution_passes_with_detail(self):
        after = _v2_after()
        after["share_ledger"]["last"] = dict(after["share_ledger"]["last"],
                                            attribution="absent", key_id=None)
        _set_signal(after, "share_ledger_tail", _signal(
            "ok", rows=1681, last_index=1680, last_attribution="absent",
            last_key_id=None))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertTrue(result["ok"], _verdicts(result))
        detail = _item(result, "ledger_last_attribution")["detail"]
        self.assertIn("新增 1 行", detail)
        self.assertIn("absent", detail)

    def test_share_entry_faults_ok_to_ok_passes(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        self.assertEqual("pass",
                          _item(result, "share_entry_faults")["verdict"])

    def test_share_entry_faults_new_record_passes_with_counts(self):
        after = _v2_after()
        counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
                  "upstream": 4, "stream": 0}
        last = _fault_record(at=1790476200.0, code="um_connection_failed")
        _set_signal(after, "share_entry_faults", _signal(
            "ok", last_fault=last, counts_by_phase=counts,
            checked_at=1790476201, reason="ok", detail="末条故障"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertTrue(result["ok"], _verdicts(result))
        detail = _item(result, "share_entry_faults")["detail"]
        self.assertIn("um_connection_failed", detail)

    def test_share_entry_faults_record_rewrite_fails(self):
        """末条故障时间回退 = 故障文件被重写/替换（append-only 被破坏）。"""
        after = _v2_after()
        last = _fault_record(at=1790000000.0)
        _set_signal(after, "share_entry_faults", _signal(
            "ok", last_fault=last, counts_by_phase={
                "auth": 0, "config": 0, "body": 0, "limit": 0,
                "upstream": 1, "stream": 0},
            checked_at=1790476201, reason="ok", detail="末条故障"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "share_entry_faults")["verdict"])

    def test_share_entry_faults_undecidable_is_not_a_pass(self):
        after = _v2_after()
        _set_signal(after, "share_entry_faults", _signal(
            "undecidable", last_fault=None, counts_by_phase=None,
            checked_at=None, reason="fault_log_absent",
            detail="故障文件不存在 / 读不到"))
        result = tool.compare_snapshots(_v2_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable",
                          _item(result, "share_entry_faults")["verdict"])
        self.assertNotEqual("fail",
                            _item(result, "share_entry_faults")["verdict"])

    def test_share_entry_faults_missing_side_is_undecidable(self):
        result = tool.compare_snapshots(_v1_before(), _v2_after())
        self.assertEqual("undecidable",
                          _item(result, "share_entry_faults")["verdict"])

    def test_share_entry_faults_both_sides_missing_the_key_is_undecidable(self):
        """协调端裁定：两侧都缺该信号时记 undecidable、绝不记 ok。

        纳入规则按 ``window_signals`` 节的有无，**不得**改成「任一侧带该键才
        纳入」；两侧都有节、都缺该键时仍纳入并记 undecidable（读不到 ≠ 通过）。
        """
        before = _v2_before()
        after = _v2_after()
        del before["window_signals"]["share_entry_faults"]
        del after["window_signals"]["share_entry_faults"]
        result = tool.compare_snapshots(before, after)
        self.assertEqual("undecidable",
                          _item(result, "share_entry_faults")["verdict"])
        self.assertNotEqual("pass",
                            _item(result, "share_entry_faults")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_share_entry_faults_both_absent_passes(self):
        before = _v2_before()
        after = _v2_after()
        _set_signal(before, "share_entry_faults", _faults_absent())
        _set_signal(after, "share_entry_faults", _faults_absent())
        result = tool.compare_snapshots(before, after)
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual("pass",
                          _item(result, "share_entry_faults")["verdict"])


# ------------------------------------------------- round7 兼容（只增不删）
class Round7CompatibilityTests(unittest.TestCase):
    def test_round7_schema_pair_still_yields_exactly_the_ten_items(self):
        result = tool.compare_snapshots(_v1_before(), _v1_after())
        self.assertEqual(ROUND7_ITEM_IDS,
                          [item["id"] for item in result["items"]])
        self.assertFalse(result["window_signals_compared"])

    def test_round7_healthy_restart_still_passes(self):
        result = tool.compare_snapshots(_v1_before(), _v1_after())
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])

    def test_round7_missing_health_is_still_undecidable(self):
        after = _v1_after()
        del after["health_8096"]
        result = tool.compare_snapshots(_v1_before(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable", _item(result, "8096_health")["verdict"])

    def test_round7_rendered_text_names_the_skipped_window_items(self):
        result = tool.compare_snapshots(_v1_before(), _v1_after())
        text = tool.format_comparison(result)
        self.assertIn("window_signals", text)
        self.assertIn("未纳入", text)
        self.assertEqual(len(result["items"]),
                          sum(1 for line in text.splitlines()
                              if line.startswith("[")))

    def test_mixed_pair_includes_window_items_with_undecidable_side(self):
        """一侧旧、一侧新：新增 6 项纳入，旧侧缺失证据记 undecidable（读不到 ≠ 通过）。"""
        result = tool.compare_snapshots(_v1_before(), _v2_after())
        self.assertTrue(result["window_signals_compared"])
        verdicts = _verdicts(result)
        for item_id in WINDOW_ITEM_IDS:
            self.assertIn(item_id, verdicts)
        self.assertEqual("undecidable", verdicts["key_set_transition"])
        self.assertEqual("undecidable", verdicts["watchdog_task"])
        self.assertEqual(2, result["exit_code"])


class SnapshotSummaryTests(unittest.TestCase):
    def test_summary_prints_every_window_signal_status(self):
        snap = _v2_before()
        _set_signal(snap, "watchdog_task", _watchdog_undecidable())
        text = tool.format_snapshot_summary(snap)
        for name in tool.WINDOW_SIGNAL_IDS:
            self.assertIn(name, text)
        self.assertIn("watchdog_task: [UNDECIDABLE]", text)
        self.assertIn("read_surface_keys: [OK]", text)

    def test_summary_prints_the_watchdog_query_identity(self):
        text = tool.format_snapshot_summary(_v2_before())
        self.assertIn("elevated=True", text)
        self.assertIn("DAICNT\\dacint", text)


if __name__ == "__main__":
    unittest.main()
