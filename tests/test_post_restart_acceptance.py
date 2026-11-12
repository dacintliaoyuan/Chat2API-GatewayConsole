# -*- coding: utf-8 -*-
"""`post_restart_acceptance` 的夹具测试（不连生产、不读任何现场）。

覆盖：

* 八层各自的 PASS / FAIL / UNDECIDABLE 三态；
* 「任一层 UNDECIDABLE → 退出码 2 并打印未完成核对」这条反例；
* 缺重启前快照时 ①④⑥⑦ 记 UNDECIDABLE、②③⑤⑧ 照常判定；
* dry-run 不采集任何事实（采集函数被替换成必炸的桩仍然退出码 0）；
* `--check` 读 `--before` 快照并把八层判定串起来；
* 纯解析：启动行两种合法实际值、marker 找不到、raw.txt 的 UTF-8 与 GBK
  两种编码、watchdog 的 JSON 与 schtasks 中英状态标签；
* `collect_facts`（2026-09-28 复审执行轮 9 后加）：在临时目录的**陈旧**
  `backend.pid` 上跑真实采集路径，断言文件仍在、size / mtime_ns / 内容逐字
  不变、`backend_pid` 事实照实报「陈旧」，且运行时写哨兵一次都没响；
  反向对照证明默认档仍会删（开关不是恒真）。
* 窗口后权威读数接缝（2026-09-28 复审执行轮 11 后加，`--authoritative`）：
  四道校验（结构 / 新鲜度 / 身份 / 时间顺序）**按 ①②③④ 的顺序求值**、
  各自的拒绝路径与原因码（顺序夹具钉住 ② 在 ③ 之前）、
  ⑧ 由权威读数判定（本地读数读不到也能 PASS）、③ 的 raw.txt mtime 兜底、
  ④ 的 backend.pid 旁证；⑧ 需要原始信封重算——删掉 `query` → undecidable
  （`authoritative_envelope_missing`）、信封在但重算不一致 → undecidable、
  信封在且一致才可能 PASS；以及「不提供该开关时接缝不参与判定」的
  加性回归（缺 `--authoritative` → ⑧ 仍 undecidable 且退出码 2）。
* ③ 启动行读不到时的等价权威证据（2026-09-28 执行轮 14 后加，
  `share_attribution_status` 的 `file_ready` / `instance_loaded` 事实）：
  密钥就绪 + `instance_loaded.value='yes'` → PASS；密钥未就绪
  （`file_ready=False`）+ `reason='key_file_not_ready'` → PASS（密钥未生成
  时的预期状态）；密钥就绪但实例载入 `no`/`unknown` 或事实读不到 →
  UNDECIDABLE；启动行可读时仍优先用它（反向对照夹具钉住）；每种情形的
  detail 都带「raw.txt 在本拓扑下结构性为空」与代码行依据，绝不记成 FAIL。

隔离：夹具全在内存字典与 `tempfile` 临时目录里；不连 127.0.0.1 的任何端口、
不跑 netstat/CIM/Get-ScheduledTask/schtasks、不读 raw.txt、不重启任何服务、
不生成密钥、不发模型请求、不写仓库任何路径。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_post_restart_acceptance -q
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AI_GATEWAY_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = AI_GATEWAY_DIR / "tools" / "um-share"
if str(AI_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_DIR))


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location(
        "um_share_" + module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("post_restart_acceptance")

from console import share_production as prod  # noqa: E402  (products' read-only ops)

FOUR_KEYS = ["key_chars", "key_file", "reason", "wired"]
SEVEN_KEYS = ["file_ready", "file_reason", "instance_loaded", "key_chars",
              "key_file", "request_attribution_detail",
              "request_attribution_observed"]

STARTUP_ENABLED = ("UM 分享入口 http://127.0.0.1:8100；上游后端 "
                   "http://127.0.0.1:8110；模式：UM 转发；请求归因：已启用；"
                   "Ctrl+C 关闭。")
STARTUP_DISABLED = ("UM 分享入口 http://127.0.0.1:8100；上游后端 "
                    "http://127.0.0.1:8110；模式：UM 转发；请求归因："
                    "未启用（共享密钥不可读，后端记为未归属）；Ctrl+C 关闭。")


# --------------------------------------------------------------- 夹具构造
def _entry(status, code, error=None):
    return {"status": status, "code": code, "error": error}


def _listener(pids, reason=None):
    return {"pids": pids, "reason": reason}


def _before(**overrides):
    """一份「重启前」快照夹具（形状 = share_chain_snapshot.py --out 的 JSON）。"""
    base = {
        "tool": "share_chain_snapshot",
        "schema_version": 1,
        "captured_at": 100.0,
        "captured_at_iso": "2026-09-28T09:00:00+08:00",
        "listeners": {
            "8000": _listener([35724]),
            "8096": _listener([51352]),
            "8100": _listener([37020]),
            "8110": _listener([54300]),
            "8320": _listener([8816]),
        },
        "share_status": {"http_status": 200, "error": None,
                         "keys": list(FOUR_KEYS), "shape": "four_key",
                         "funnel_on": True},
        "backend_observe": {"running": True, "auth_state": "valid",
                            "auth_label": "认证有效",
                            "ledger_file": "usage-share-backend.jsonl",
                            "error": None},
        "share_ledger": {"path": "usage-share-backend.jsonl", "rows": 1680,
                         "attributed_rows": [], "error": None},
    }
    for key, value in overrides.items():
        base[key] = value
    return copy.deepcopy(base)


def _after(**overrides):
    """一份「重启后、八层全 PASS」的事实夹具。"""
    base = {
        "listeners": {
            "8096": _listener([60000]),
            "8100": _listener([37020]),
            "8110": _listener([54300]),
        },
        "health_8096": {"status": 200,
                        "json": {"status": "ok", "service": "usagesrv",
                                 "port": 8096},
                        "error": None},
        "share_status": {"http_status": 200, "error": None,
                         "keys": list(SEVEN_KEYS), "shape": "seven_key",
                         "funnel_on": True},
        "share_entry_probe": {
            "root": _entry(404, "route_not_shared"),
            "health": _entry(404, "route_not_shared"),
            "whitelist_models": _entry(401, "invalid_api_key")},
        "backend_observe": {"running": True, "auth_state": "valid",
                            "auth_label": "认证有效",
                            "ledger_file": "usage-share-backend.jsonl",
                            "error": None},
        "share_ledger": {"path": "usage-share-backend.jsonl", "rows": 1680,
                         "attributed_rows": [], "error": None},
        "gateway_startup": {
            "pid": 37020, "started_at": 1.0,
            "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
            "raw_mtime": 2.0, "line": STARTUP_ENABLED,
            "attribution": "enabled", "reason": "ok", "error": None},
        "watchdog": {"state": "Running", "query": "Get-ScheduledTask",
                     "error": None},
    }
    for key, value in overrides.items():
        base[key] = value
    return copy.deepcopy(base)


def _verdicts(result):
    return {item["index"]: item["verdict"] for item in result["items"]}


def _item(result, index):
    for item in result["items"]:
        if item["index"] == index:
            return item
    raise AssertionError("no layer %r in %r"
                         % (index, [i["index"] for i in result["items"]]))


# --------------------------------------------- 窗口后权威读数夹具（探针 JSON）
# 形状逐字对照 tools/um-share/authoritative_readings.ps1 的输出与
# tests/test_authoritative_readings.py 的夹具：顶层 tool / schema_version /
# read_only / elevated / identity / captured_at / window_signals。
AUTH_CAPTURED_AT = 1790562898.0351868
AUTH_CAPTURED_AT_ISO = "2026-09-28T10:34:58+08:00"
AUTH_IDENTITY = "DAICNT\\dacint"
AUTH_PATH = "authoritative-after.json"
BEFORE_READING_AT = 150.0


def _signal(name, status, **extra):
    base = {"signal": name, "status": status, "reason": None, "detail": ""}
    base.update(extra)
    return base


def _query_envelope(elevated=True, found=True, state=4,
                     state_text="Running", error=None):
    return {"elevated": elevated, "name": AUTH_IDENTITY, "found": found,
            "state": state, "stateText": state_text, "error": error}


def _watchdog_signal(state=4, state_text="Running"):
    return _signal("watchdog_task", "ok", task_name="umshare-watchdog",
                   state=state, state_text=state_text, elevated=True,
                   identity=AUTH_IDENTITY, reason="ok",
                   detail="任务已注册：State=%s（%s）" % (state_text, state),
                   query=_query_envelope(state=state, state_text=state_text))


def _raw_txt_signal(mtime=1790562898.0, path=None):
    return _signal("raw_txt", "ok",
                   path=path if path is not None
                   else _after()["gateway_startup"]["raw_path"],
                   exists=True, size=4096, mtime=mtime,
                   mtime_utc_iso="2026-09-28T02:34:58.0351868Z",
                   mtime_reason="ok", reason="ok",
                   detail="raw.txt 存在：size=4096（内容不读）")


def _backend_pid_signal(content="54300", pid=54300):
    return _signal("backend_pid_file", "ok",
                   path=(r"C:\Users\dacint\AppData\Local\SelfUseAI\ai-gateway"
                          r"\share-backend\backend.pid"),
                   path_source="LOCALAPPDATA_default",
                   content=content, bytes_hex=content.encode("ascii").hex(),
                   size=len(content), pid=pid,
                   mtime=1790562898.0, reason="ok",
                   detail="backend.pid 字节=%d 内容=%s" % (len(content),
                                                            content))


def _authoritative_payload(**overrides):
    """探针 JSON 夹具（提升身份采的「窗口后」读数）。"""
    payload = {
        "tool": "authoritative_readings",
        "schema_version": 1,
        "read_only": True,
        "write_actions": [],
        "elevated": True,
        "identity": AUTH_IDENTITY,
        "probe_pid": 4242,
        "captured_at": AUTH_CAPTURED_AT,
        "captured_at_iso": AUTH_CAPTURED_AT_ISO,
        "run_root": r"C:\ProgramData\UMShareRun",
        "window_signals": {
            "watchdog_task": _watchdog_signal(),
            "raw_txt": _raw_txt_signal(),
            "backend_pid_file": _backend_pid_signal(),
        },
    }
    for key, value in overrides.items():
        payload[key] = value
    return copy.deepcopy(payload)


def _loaded(payload=None, error=None):
    """``load_authoritative_readings`` 的产物夹具（纯字典，不读盘）。"""
    return {"path": AUTH_PATH, "sha256": "a" * 64, "encoding": "utf-8",
            "readings": (payload if payload is not None
                          else _authoritative_payload()),
            "error": error}


def _before_with_reading(reading_at=BEFORE_READING_AT, **overrides):
    """窗口前快照 + 已合并的窗口前权威读数（`--authoritative` 那份）。

    ``overrides`` 里的 ``captured_at`` 改的是**快照自身**的采集时刻；
    ``reading_at`` 改的是那份**权威读数**的采集时刻（用于时间顺序回归）。
    """
    base = _before(**overrides)
    base["authoritative_readings"] = {
        "source": {"tool": "authoritative_readings", "schema_version": 1,
                    "path": "authoritative-before.json",
                    "sha256": "b" * 64, "encoding": "utf-8",
                    "read_only": True, "elevated": True,
                    "identity": AUTH_IDENTITY, "captured_at": reading_at,
                    "captured_at_iso": "2026-09-28T09:05:00+08:00"}}
    return base


def _seam(payload=None, after=None, before=None, error=None):
    """纯函数层：夹具 → ``authoritative_evidence``（不读盘、不连端口）。"""
    return tool.authoritative_evidence(
        _loaded(payload, error), after if after is not None else _after(),
        before if before is not None else _before_with_reading())


def _denied_reason(evidence):
    """被拒绝的读数必须给出原因码且不得可用。"""
    assert evidence["usable"] is False, evidence
    assert isinstance(evidence["reason"], str) and evidence["reason"], evidence
    return evidence["reason"]


def _live_watchdog_denied():
    """本工具自身身份下的 watchdog 读数：拒绝访问（第 ⑧ 层原来只能记 undecidable）。"""
    return {"state": None, "query": "Get-ScheduledTask+schtasks",
            "error": "get_scheduled_task:query_failed:CimJobException | "
                     "schtasks:错误: 拒绝访问。"}


class LayerOneTests(unittest.TestCase):
    """① 8096 健康 200 且 PID 已变。"""

    def test_restarted_8096_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "①")["verdict"])

    def test_unchanged_pid_fails(self):
        result = tool.evaluate(_before(), _after(
            listeners={"8096": _listener([51352]),
                        "8100": _listener([37020]),
                        "8110": _listener([54300])}))
        self.assertEqual("fail", _item(result, "①")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_health_is_undecidable(self):
        after = _after()
        del after["health_8096"]
        result = tool.evaluate(_before(), after)
        self.assertEqual("undecidable", _item(result, "①")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_bad_health_body_fails(self):
        result = tool.evaluate(_before(), _after(
            health_8096={"status": 200,
                         "json": {"status": "ok", "service": "other",
                                  "port": 8096},
                         "error": None}))
        self.assertEqual("fail", _item(result, "①")["verdict"])

    def test_non_200_status_is_fail_even_without_json_body(self):
        """状态读到了但非 200 = FAIL（不是「不可判定」）。"""
        result = tool.evaluate(_before(), _after(
            health_8096={"status": 503, "json": None, "error": None}))
        self.assertEqual("fail", _item(result, "①")["verdict"])
        result = tool.evaluate(_before(), _after(
            health_8096={"status": 503, "json": {"error": "boom"},
                         "error": None}))
        self.assertEqual("fail", _item(result, "①")["verdict"])

    def test_200_with_non_json_body_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            health_8096={"status": 200, "json": None, "error": None}))
        self.assertEqual("undecidable", _item(result, "①")["verdict"])

    def test_offline_before_snapshot_is_undecidable_not_pass(self):
        """反例：窗口前 8096 已掉线（before pids 为空）时，没有旧 PID 可证
        「已变」——必须记 UNDECIDABLE，不得报 PASS。"""
        result = tool.evaluate(_before(
            listeners={"8000": _listener([35724]), "8096": _listener([]),
                        "8100": _listener([37020]),
                        "8110": _listener([54300])}), _after())
        self.assertEqual("undecidable", _item(result, "①")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_before_listener_is_undecidable(self):
        result = tool.evaluate(_before(
            listeners={"8096": _listener(None, "query_failed"),
                        "8100": _listener([37020]),
                        "8110": _listener([54300])}), _after())
        self.assertEqual("undecidable", _item(result, "①")["verdict"])


class LayerTwoTests(unittest.TestCase):
    """② 8096 读面 7 键新形状。"""

    def test_seven_key_shape_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "②")["verdict"])

    def test_four_key_shape_fails(self):
        result = tool.evaluate(_before(), _after(
            share_status={"http_status": 200, "error": None,
                          "keys": list(FOUR_KEYS), "shape": "four_key",
                          "funnel_on": True}))
        self.assertEqual("fail", _item(result, "②")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_key_set_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            share_status={"http_status": 503, "error": None, "keys": None,
                          "shape": "unknown", "funnel_on": None}))
        self.assertEqual("undecidable", _item(result, "②")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unknown_key_set_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            share_status={"http_status": 200, "error": None,
                          "keys": ["a", "b"], "shape": "unknown",
                          "funnel_on": True}))
        self.assertEqual("undecidable", _item(result, "②")["verdict"])


class LayerGatewayTests(unittest.TestCase):
    """③ 8100 仍在监听且启动行如实报归因状态。"""

    def test_listening_with_enabled_startup_line_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "③")["verdict"])
        self.assertIn("已启用", _item(result, "③")["detail"])

    def test_listening_with_disabled_startup_line_is_also_legal(self):
        """未启用也是如实状态（合法），但必须打印实际值。"""
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": 1.0,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": 2.0, "line": STARTUP_DISABLED,
                             "attribution": "disabled", "reason": "ok",
                             "error": None}))
        self.assertEqual("pass", _item(result, "③")["verdict"])
        self.assertIn("未启用", _item(result, "③")["detail"])

    def test_offline_8100_fails(self):
        result = tool.evaluate(_before(), _after(
            listeners={"8096": _listener([60000]),
                        "8100": _listener([]),
                        "8110": _listener([54300])}))
        self.assertEqual("fail", _item(result, "③")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_startup_line_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": None,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": None, "line": None,
                             "attribution": None, "reason": "raw_unreadable",
                             "error": "PermissionError"}))
        self.assertEqual("undecidable", _item(result, "③")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_stale_startup_line_is_undecidable(self):
        """raw.txt 早于当前 8100 进程启动时间：不可能是当前实例的启动行。"""
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": 2000.0,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": 1000.0, "line": STARTUP_ENABLED,
                             "attribution": "enabled", "reason": "ok",
                             "error": None}))
        self.assertEqual("undecidable", _item(result, "③")["verdict"])
        self.assertEqual(2, result["exit_code"])
        self.assertIn("陈旧行", _item(result, "③")["detail"])

    def test_startup_line_without_process_start_time_is_undecidable(self):
        """8100 进程启动时间读不到时不得 PASS（复核者 2026-09-28 发现的窄类假 PASS）。"""
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": None,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": 1000.0, "line": STARTUP_ENABLED,
                             "attribution": "enabled", "reason": "ok",
                             "error": None}))
        self.assertEqual("undecidable", _item(result, "③")["verdict"])
        self.assertEqual(2, result["exit_code"])
        self.assertIn("启动时间读不到", _item(result, "③")["detail"])

    def test_startup_line_not_older_than_process_passes(self):
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": 1000.0,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": 1000.0, "line": STARTUP_ENABLED,
                             "attribution": "enabled", "reason": "ok",
                             "error": None}))
        self.assertEqual("pass", _item(result, "③")["verdict"])


class LayerGatewayAttributionFallbackTests(unittest.TestCase):
    """③ 启动行读不到时的等价且权威证据（`share_attribution_status` 事实）。

    维护者 2026-09-28 执行轮 14 口径：本拓扑下 raw.txt 结构性为空
    （share_guard.py:488 的 CreateProcessW 第 5 参 bInheritHandles=False
    且该文件 print( 计数为 0；deploy-elevated.ps1:536 把 guard 的 stdout
    重定向到 run\\live\\raw.txt），启动行永远采不到——**这是拓扑事实，不是
    窗口失败**。四种情形：

    * 密钥就绪 + instance_loaded.value='yes' → PASS；
    * 密钥未就绪（file_ready=False）+ reason='key_file_not_ready' → PASS
      （密钥未生成时的预期状态，不是故障）；
    * 密钥未就绪但 instance_loaded.value 带非空值（yes/no 等矛盾事实；
      该组合生产不可达）→ UNDECIDABLE（防御加固，执行轮 14）；
    * 密钥就绪但实例载入 no/unknown → UNDECIDABLE（先补证据）；
    * 读不到任何一项 → UNDECIDABLE。

    每种情形的 detail 都必须带上「结构性为空」与代码行依据，绝不记成 FAIL。
    """

    # 本拓扑下启动行的真实读法：文件可读但结构性为空（marker_not_found）。
    STRUCTURAL_STARTUP = {
        "pid": 37020, "started_at": 1790601547.15,
        "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
        "raw_mtime": 1790601547.0, "line": None,
        "attribution": None, "reason": "marker_not_found",
        "error": None}

    def _facts(self, **overrides):
        base = {"source": "in_process_share_attribution_status",
                "file_ready": None, "file_reason": None, "key_chars": None,
                "key_file": None, "instance_loaded_value": None,
                "instance_loaded_basis": None,
                "instance_loaded_reason": None,
                "request_attribution_observed": None, "error": None}
        base.update(overrides)
        return base

    def _evaluate(self, facts):
        return tool.evaluate(_before(), _after(
            gateway_startup=dict(self.STRUCTURAL_STARTUP),
            gateway_attribution=facts))

    def test_key_ready_and_instance_loaded_passes(self):
        result = self._evaluate(self._facts(
            file_ready=True, file_reason="ok", key_chars=64,
            key_file=r"C:\ProgramData\UMShareRun\run\share-attribution.key",
            instance_loaded_value="yes", instance_loaded_basis="inference",
            instance_loaded_reason="process_started_at_or_after_key_mtime"))
        item = _item(result, "③")
        self.assertEqual("pass", item["verdict"])
        self.assertIn("已启用（以密钥就绪 + 实例载入推断）", item["detail"])
        self.assertIn("结构性为空", item["detail"])
        self.assertIn("share_guard.py:488", item["detail"])
        self.assertIn("deploy-elevated.ps1:536", item["detail"])

    def test_key_not_ready_is_expected_state_and_passes(self):
        """密钥未生成：file_ready=False + reason=key_file_not_ready → PASS（预期状态）。"""
        result = self._evaluate(self._facts(
            file_ready=False, file_reason="missing",
            instance_loaded_value="unknown", instance_loaded_basis="inference",
            instance_loaded_reason="key_file_not_ready"))
        item = _item(result, "③")
        self.assertEqual("pass", item["verdict"])
        self.assertIn("未启用（密钥未就绪）", item["detail"])
        self.assertIn("结构性为空", item["detail"])
        self.assertIn("share_guard.py:488", item["detail"])

    def test_file_not_ready_with_a_loaded_value_is_undecidable(self):
        """防御加固（执行轮 14）：file_ready=False 时 instance_loaded.value 只可能
        是空/unknown（没有 mtime 可比 → 探测不会被调用）；事实若带着 yes/no 这类
        非空值，说明事实自相矛盾（该组合生产不可达），不得按「未启用」放行。"""
        for value in ("yes", "no"):
            with self.subTest(value=value):
                result = self._evaluate(self._facts(
                    file_ready=False, file_reason="missing",
                    instance_loaded_value=value,
                    instance_loaded_basis="inference",
                    instance_loaded_reason="key_file_not_ready"))
                item = _item(result, "③")
                self.assertEqual("undecidable", item["verdict"])
                self.assertEqual(2, result["exit_code"])
                self.assertIn("facts_inconsistent", item["detail"])
                self.assertIn("结构性为空", item["detail"])
                # 矛盾事实不得被记成「未启用（密钥未就绪）」放行。
                self.assertNotIn(
                    tool.ATTRIBUTION_DISABLED_KEY_NOT_READY, item["detail"])

    def test_key_ready_but_instance_not_loaded_is_undecidable(self):
        for value in ("no", "unknown"):
            with self.subTest(value=value):
                result = self._evaluate(self._facts(
                    file_ready=True, file_reason="ok", key_chars=64,
                    instance_loaded_value=value,
                    instance_loaded_basis="inference",
                    instance_loaded_reason="process_started_before_key_mtime"))
                item = _item(result, "③")
                self.assertEqual("undecidable", item["verdict"])
                self.assertEqual(2, result["exit_code"])
                self.assertIn("reason=instance_loaded_%s" % value,
                              item["detail"])
                self.assertIn("结构性为空", item["detail"])

    def test_unreadable_facts_are_undecidable(self):
        result = self._evaluate(None)
        item = _item(result, "③")
        self.assertEqual("undecidable", item["verdict"])
        self.assertEqual(2, result["exit_code"])
        self.assertIn("attribution_facts_missing", item["detail"])
        self.assertIn("结构性为空", item["detail"])

    def test_facts_error_is_undecidable(self):
        result = self._evaluate(self._facts(
            error="share_attribution_status_failed:OSError"))
        item = _item(result, "③")
        self.assertEqual("undecidable", item["verdict"])
        self.assertIn("attribution_facts_unreadable", item["detail"])
        self.assertIn("结构性为空", item["detail"])

    def test_startup_facts_missing_also_uses_facts(self):
        """启动行事实整块缺失也走等价证据（读不到 ≠ 失败）。"""
        after = _after(gateway_attribution=self._facts(
            file_ready=False, file_reason="missing",
            instance_loaded_value="unknown",
            instance_loaded_basis="inference",
            instance_loaded_reason="key_file_not_ready"))
        del after["gateway_startup"]
        result = tool.evaluate(_before(), after)
        self.assertEqual("pass", _item(result, "③")["verdict"])
        self.assertIn("未启用（密钥未就绪）", _item(result, "③")["detail"])

    def test_readable_line_still_wins_over_facts(self):
        """反向对照：启动行可读时优先用它，不被等价事实覆盖。"""
        result = tool.evaluate(_before(), _after(
            gateway_startup={"pid": 37020, "started_at": 1.0,
                             "raw_path": r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                             "raw_mtime": 2.0, "line": STARTUP_DISABLED,
                             "attribution": "disabled", "reason": "ok",
                             "error": None},
            gateway_attribution=self._facts(
                file_ready=True, file_reason="ok", key_chars=64,
                instance_loaded_value="yes", instance_loaded_basis="inference",
                instance_loaded_reason="process_started_at_or_after_key_mtime")))
        item = _item(result, "③")
        self.assertEqual("pass", item["verdict"])
        self.assertIn("未启用（共享密钥不可读，后端记为未归属）",
                      item["detail"])


class LayerBackendTests(unittest.TestCase):
    """④ 8110 仍在监听、auth_state 与重启前一致。"""

    def test_same_auth_state_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "④")["verdict"])

    def test_changed_auth_state_fails(self):
        result = tool.evaluate(_before(), _after(
            backend_observe={"running": True, "auth_state": "expired",
                            "auth_label": "认证过期",
                            "ledger_file": "usage-share-backend.jsonl",
                            "error": None}))
        self.assertEqual("fail", _item(result, "④")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_offline_8110_fails(self):
        result = tool.evaluate(_before(), _after(
            listeners={"8096": _listener([60000]),
                        "8100": _listener([37020]),
                        "8110": _listener([])}))
        self.assertEqual("fail", _item(result, "④")["verdict"])

    def test_missing_backend_observe_is_undecidable(self):
        after = _after()
        del after["backend_observe"]
        result = tool.evaluate(_before(), after)
        self.assertEqual("undecidable", _item(result, "④")["verdict"])
        self.assertEqual(2, result["exit_code"])


class LayerShareEntryTests(unittest.TestCase):
    """⑤ 分享入口 404 route_not_shared / 401 invalid_api_key。"""

    def test_expected_404_and_401_pass(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "⑤")["verdict"])

    def test_route_drift_fails(self):
        result = tool.evaluate(_before(), _after(
            share_entry_probe={"root": _entry(200, None),
                              "health": _entry(404, "route_not_shared"),
                              "whitelist_models":
                                  _entry(401, "invalid_api_key")}))
        self.assertEqual("fail", _item(result, "⑤")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreachable_probe_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            share_entry_probe={
                "root": _entry(None, None, "URLError"),
                "health": _entry(404, "route_not_shared"),
                "whitelist_models": _entry(401, "invalid_api_key")}))
        self.assertEqual("undecidable", _item(result, "⑤")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_missing_probe_entry_is_undecidable(self):
        result = tool.evaluate(_before(), _after(share_entry_probe={}))
        self.assertEqual("undecidable", _item(result, "⑤")["verdict"])


class LayerFunnelTests(unittest.TestCase):
    """⑥ Funnel 状态与重启前一致。"""

    def test_unchanged_funnel_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "⑥")["verdict"])

    def test_funnel_on_to_off_fails(self):
        result = tool.evaluate(_before(), _after(
            share_status={"http_status": 200, "error": None,
                           "keys": list(SEVEN_KEYS), "shape": "seven_key",
                           "funnel_on": False}))
        self.assertEqual("fail", _item(result, "⑥")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_funnel_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            share_status={"http_status": 200, "error": None,
                           "keys": list(SEVEN_KEYS), "shape": "seven_key",
                           "funnel_on": None}))
        self.assertEqual("undecidable", _item(result, "⑥")["verdict"])
        self.assertEqual(2, result["exit_code"])


class LayerShareLedgerTests(unittest.TestCase):
    """⑦ 8110 账本行数只增不减。"""

    def test_equal_row_count_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "⑦")["verdict"])

    def test_growing_row_count_passes(self):
        result = tool.evaluate(_before(), _after(
            share_ledger={"path": "usage-share-backend.jsonl", "rows": 1681,
                          "attributed_rows": [], "error": None}))
        self.assertEqual("pass", _item(result, "⑦")["verdict"])

    def test_shrinking_row_count_fails(self):
        result = tool.evaluate(_before(), _after(
            share_ledger={"path": "usage-share-backend.jsonl", "rows": 1679,
                          "attributed_rows": [], "error": None}))
        self.assertEqual("fail", _item(result, "⑦")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_row_count_is_undecidable(self):
        result = tool.evaluate(_before(), _after(
            share_ledger={"path": None, "rows": None,
                          "attributed_rows": None, "error": "ledger_unreadable"}))
        self.assertEqual("undecidable", _item(result, "⑦")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_ledger_path_change_fails(self):
        """行数不减但换了新文件（路径变了）也必须 FAIL。"""
        result = tool.evaluate(_before(), _after(
            share_ledger={"path": "usage-share-backend-new.jsonl",
                          "rows": 1680, "attributed_rows": [],
                          "error": None}))
        self.assertEqual("fail", _item(result, "⑦")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_attributed_row_loss_fails(self):
        before_row = {"index": 7, "time": "t", "model": "m", "key_id": "k1",
                      "attribution": "verified"}
        result = tool.evaluate(
            _before(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1681,
                                  "attributed_rows": [before_row],
                                  "error": None}),
            _after(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1681, "attributed_rows": [],
                                  "error": None}))
        self.assertEqual("fail", _item(result, "⑦")["verdict"])

    def test_attributed_row_rewrite_fails(self):
        before_row = {"index": 7, "time": "t", "model": "m", "key_id": "k1",
                      "attribution": "verified"}
        after_row = dict(before_row, key_id="k2")
        result = tool.evaluate(
            _before(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1681,
                                  "attributed_rows": [before_row],
                                  "error": None}),
            _after(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1681,
                                  "attributed_rows": [after_row],
                                  "error": None}))
        self.assertEqual("fail", _item(result, "⑦")["verdict"])

    def test_attributed_row_preserved_passes(self):
        before_row = {"index": 7, "time": "t", "model": "m", "key_id": "k1",
                      "attribution": "verified"}
        result = tool.evaluate(
            _before(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1681,
                                  "attributed_rows": [before_row],
                                  "error": None}),
            _after(share_ledger={"path": "usage-share-backend.jsonl",
                                  "rows": 1682,
                                  "attributed_rows": [before_row],
                                  "error": None}))
        self.assertEqual("pass", _item(result, "⑦")["verdict"])


class LayerWatchdogTests(unittest.TestCase):
    """⑧ watchdog 任务状态；读不到即 UNDECIDABLE。"""

    def test_running_watchdog_passes(self):
        result = tool.evaluate(_before(), _after())
        self.assertEqual("pass", _item(result, "⑧")["verdict"])

    def test_registered_but_not_running_fails(self):
        result = tool.evaluate(_before(), _after(
            watchdog={"state": "Ready", "query": "Get-ScheduledTask",
                       "error": None}))
        self.assertEqual("fail", _item(result, "⑧")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_watchdog_is_undecidable_not_unregistered(self):
        result = tool.evaluate(_before(), _after(
            watchdog={"state": None, "query": "Get-ScheduledTask+schtasks",
                      "error": "access_denied"}))
        self.assertEqual("undecidable", _item(result, "⑧")["verdict"])
        self.assertEqual(2, result["exit_code"])
        self.assertIn("access_denied", _item(result, "⑧")["detail"])


class OverallVerdictTests(unittest.TestCase):
    def test_all_eight_layers_pass_is_zero(self):
        result = tool.evaluate(_before(), _after())
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])
        text = tool.format_report(_after(), result, "before.json")
        self.assertIn("八层全部 PASS", text)
        self.assertNotIn("未完成核对", text)

    def test_one_undecidable_layer_is_exit_two_and_not_complete(self):
        """反例：其余七层全 PASS，只有 ⑧ 读不到 → 退出码 2 且打印未完成核对。"""
        result = tool.evaluate(_before(), _after(
            watchdog={"state": None, "query": "Get-ScheduledTask+schtasks",
                      "error": "access_denied"}))
        self.assertEqual(2, result["exit_code"])
        self.assertFalse(result["ok"])
        text = tool.format_report(_after(), result, "before.json")
        self.assertIn("未完成核对", text)
        self.assertIn("退出码 2", text)

    def test_missing_before_snapshot_makes_four_layers_undecidable(self):
        """缺 --before：①④⑥⑦ UNDECIDABLE，②③⑤⑧ 照常 PASS，总判定仍是 2。"""
        result = tool.evaluate(None, _after(), None)
        verdicts = _verdicts(result)
        self.assertEqual({"①": "undecidable", "④": "undecidable",
                          "⑥": "undecidable", "⑦": "undecidable",
                          "②": "pass", "③": "pass", "⑤": "pass",
                          "⑧": "pass"}, verdicts)
        self.assertEqual(2, result["exit_code"])
        text = tool.format_report(_after(), result, None)
        self.assertIn("未完成核对", text)

    def test_unreadable_before_snapshot_names_the_error(self):
        result = tool.evaluate(None, _after(), "FileNotFoundError:gone.json")
        self.assertEqual(2, result["exit_code"])
        self.assertIn("FileNotFoundError", _item(result, "①")["detail"])

    def test_non_dict_facts_make_every_layer_undecidable(self):
        """采集形状不对：八层全部 UNDECIDABLE，绝不当成通过。"""
        result = tool.evaluate(_before(), None)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual({"①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"},
                          {index for index, verdict in _verdicts(result).items()
                           if verdict == "undecidable"})

    def test_every_layer_is_independent(self):
        """把 8100 掉线后，其余层不受影响——判定不是从一层复制到另一层。"""
        result = tool.evaluate(_before(), _after(
            listeners={"8096": _listener([60000]),
                        "8100": _listener([]),
                        "8110": _listener([54300])}))
        verdicts = _verdicts(result)
        self.assertEqual("fail", verdicts["③"])
        self.assertEqual("pass", verdicts["①"])
        self.assertEqual("pass", verdicts["④"])
        self.assertEqual("pass", verdicts["②"])


class StartupLineParseTests(unittest.TestCase):
    def test_enabled_and_disabled_are_both_recognized(self):
        enabled = tool.parse_startup_attribution(STARTUP_ENABLED)
        self.assertEqual("enabled", enabled["attribution"])
        disabled = tool.parse_startup_attribution(STARTUP_DISABLED)
        self.assertEqual("disabled", disabled["attribution"])
        self.assertIn("未启用", disabled["line"])

    def test_last_marker_line_wins(self):
        text = "旧行 请求归因：未启用（共享密钥不可读，后端记为未归属）；\n" \
               + STARTUP_ENABLED
        parsed = tool.parse_startup_attribution(text)
        self.assertEqual("enabled", parsed["attribution"])

    def test_missing_marker_is_not_guessed(self):
        parsed = tool.parse_startup_attribution("UM 分享入口 端口 8100\n")
        self.assertIsNone(parsed["attribution"])
        self.assertEqual("marker_not_found", parsed["reason"])

    def test_non_text_is_unavailable(self):
        parsed = tool.parse_startup_attribution(None)
        self.assertIsNone(parsed["attribution"])
        self.assertEqual("text_unavailable", parsed["reason"])

    def test_utf8_and_local_codepage_bytes_both_find_the_marker(self):
        text, encoding = tool.decode_startup_text(
            STARTUP_ENABLED.encode("utf-8"))
        self.assertIn(tool.STARTUP_MARKER, text)
        if os.name == "nt":
            text, encoding = tool.decode_startup_text(
                STARTUP_DISABLED.encode("mbcs"))
            self.assertIn(tool.STARTUP_MARKER, text)
            self.assertIn("未启用", text)


class WatchdogParseTests(unittest.TestCase):
    def test_json_state_is_read_back(self):
        text = json.dumps({"name": "umshare-watchdog", "state": "Running"})
        self.assertEqual("Running", tool.parse_watchdog_state(text, 0))

    def test_nonzero_returncode_and_bad_json_are_none(self):
        self.assertIsNone(tool.parse_watchdog_state(
            json.dumps({"state": "Running"}), 3))
        self.assertIsNone(tool.parse_watchdog_state("not json", 0))
        self.assertIsNone(tool.parse_watchdog_state(None, 0))

    def test_schtasks_state_labels_both_languages(self):
        self.assertEqual("Running", tool.parse_schtasks_state("状态: 正在运行"))
        self.assertEqual("Ready", tool.parse_schtasks_state("Status: Ready"))
        self.assertIsNone(tool.parse_schtasks_state("没有状态行"))


class MainModeTests(unittest.TestCase):
    """dry-run 不采集；--check 读 --before 并把八层串起来。"""

    def test_dry_run_does_not_collect_anything(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("dry-run 不得采集任何事实")

        with mock.patch.object(tool, "collect_facts", boom), \
                mock.patch.object(tool, "read_before_snapshot", boom):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = tool.main([])
        self.assertEqual(0, code)
        self.assertIn("dry-run", output.getvalue())
        self.assertIn("未完成核对", output.getvalue())

    def test_check_without_before_reports_incomplete(self):
        with mock.patch.object(tool, "collect_facts", lambda: _after()):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = tool.main(["--check"])
        self.assertEqual(2, code)
        self.assertIn("未完成核对", output.getvalue())

    def test_check_reads_before_file_and_passes(self):
        with tempfile.TemporaryDirectory(prefix="post-restart-before-") as tmp:
            path = os.path.join(tmp, "snapshot-before.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(_before(), handle, ensure_ascii=False)
            with mock.patch.object(tool, "collect_facts", lambda: _after()):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = tool.main(["--check", "--before", path])
        self.assertEqual(0, code)
        self.assertIn("八层全部 PASS", output.getvalue())

    def test_check_with_unreadable_before_reports_incomplete(self):
        with mock.patch.object(tool, "collect_facts", lambda: _after()):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = tool.main(["--check", "--before", "no-such-snapshot.json"])
        self.assertEqual(2, code)
        self.assertIn("未完成核对", output.getvalue())

    def test_collect_failure_also_prints_incomplete(self):
        """采集层抛异常：退出码 2，且 stdout 同样打印未完成核对。"""

        def boom():
            raise RuntimeError("模拟采集失败")

        with mock.patch.object(tool, "collect_facts", boom):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = tool.main(["--check"])
        self.assertEqual(2, code)
        self.assertIn("未完成核对", output.getvalue())


class _NoWriteGuard:
    """运行时写哨兵：只读采集路径上出现任何写盘动作就立刻失败。

    ``io.open`` 与 ``builtins.open`` 都换（``Path.open``/``Path.write_text``
    走 ``io.open``，普通 ``open()`` 走 ``builtins.open``），删除／重命名／
    替换／建目录各边界同样拦住。``writes`` 空表才是「零写副作用」的**运行时
    证据**，不是注释里的声明。
    """

    _WRITE_FLAGS = ("w", "a", "x", "+")

    def __init__(self):
        self.writes = []
        self._patchers = []

    def _fail(self, what):
        self.writes.append(what)
        raise AssertionError("只读采集路径出现写盘动作：%s" % what)

    def __enter__(self):
        real_open = io.open

        def guard_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in self._WRITE_FLAGS):
                self._fail("open(%r, mode=%r)" % (file, mode))
            return real_open(file, mode, *args, **kwargs)

        def guard_remove(path, *args, **kwargs):
            self._fail("remove(%r)" % (path,))

        def guard_rename(*args, **kwargs):
            self._fail("rename/replace%r" % (args,))

        def guard_mkdir(*args, **kwargs):
            self._fail("mkdir/makedirs%r" % (args,))

        for owner, name, value in (
                (io, "open", guard_open),
                (builtins, "open", guard_open),
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


class CollectFactsZeroWriteTests(unittest.TestCase):
    """``post_restart_acceptance.collect_facts`` 在**陈旧** backend.pid 上零写。

    夹具：tempfile 临时目录 + ``CONSOLE_SHARE_BACKEND_DIR`` 指向它，里面放一份
    陈旧 pid 文件（PID 不匹配或进程已不存在）。采集层的**全部** OS/网络边界
    替身化（8096 只读 GET 注入、netstat / CIM / Get-ScheduledTask / 8100 探测
    替身），只留 8110 的 pid 文件与归因读面走真实代码路径——被收紧的正是
    那条。运行时写哨兵拦住任何写盘动作。反向对照证明开关不是恒真：同一份夹具
    下默认档仍会删。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            "CONSOLE_SHARE_RUN_ROOT": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.pid_path = os.path.join(self.backend_dir, "backend.pid")
        # 进程 / watchdog / 8100 探测边界全部替身化：本套件不跑真实
        # netstat / Get-CimInstance / Get-ScheduledTask。
        for owner, name, value in (
                # 8110 有监听者（第 ④ 层因此走到 running 检查）、其余端口无人
                # 监听：两者都是「查询成功」的读数，不是「读不到」。
                (tool.snapshot_tool, "listening_pids",
                 lambda port, run=None: {"pids": [54300] if port == 8110
                                        else [], "reason": "ok"}),
                (tool.snapshot_tool, "query_processes", lambda pids: {}),
                (tool, "read_watchdog_state",
                 lambda run=None: {"state": None, "query": "stub",
                                  "error": "stub"}),
                (prod, "share_entry_instance_probe",
                 lambda *a, **k: {"pid": None, "started_at": None,
                                 "reason": "not_listening"})):
            patcher = mock.patch.object(owner, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
        """8096 / 8100 读面替身：本套件不连任何端口。"""
        if path == "/health" and port == 8096:
            return {"status": 200,
                    "json": {"status": "ok", "service": "usagesrv",
                             "port": 8096},
                    "error": None}
        if path == "/api/console/share-management/status":
            return {"status": 200,
                    "json": {"funnel_on": True,
                             "attribution": {key: None for key in SEVEN_KEYS}},
                    "error": None}
        if path == "/v1/models":
            return {"status": 401,
                    "json": {"error": {"code": "invalid_api_key"}},
                    "error": None}
        return {"status": 404, "json": {"error": {"code": "route_not_shared"}},
                "error": None}

    def _collect(self, ops):
        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops):
            return tool.collect_facts(get=self._get)

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
        self.assertIs(facts["backend_observe"]["running"], False)
        self.assertEqual(
            facts["backend_observe"]["ledger_file"],
            os.path.join(self.backend_dir, "usage-share-backend.jsonl"))

    def test_collection_reports_absent_pid_file_as_absent(self):
        ops = self._ops()
        guard = _NoWriteGuard()
        with guard:
            facts = self._collect(ops)
        self.assertEqual(guard.writes, [])
        self.assertFalse(os.path.exists(self.pid_path))
        self.assertEqual(facts["backend_pid"]["state"], "pid_file_absent")

    def test_backend_layer_names_the_stale_state(self):
        """第 ④ 层把陈旧 pid 文件照实打印出来（running 不是 True 时）。"""
        ops = self._ops(matches=False)
        self._write_stale_pid()
        after = self._collect(ops)
        result = tool.evaluate(None, after)
        layer = _item(result, "④")
        self.assertEqual("fail", layer["verdict"])
        self.assertIn("pid_stale", layer["detail"])
        self.assertEqual(2, result["exit_code"])

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

    def test_collection_reads_the_8100_pid_and_start_time(self):
        """真机缺陷回归（执行轮 11）：采集层必须读出 8100 的 PID 与进程启动时间。

        修前：``collect_facts`` 把采集层的**内层表**（``{port: {pids}}``）交给
        ``_listener_state``，后者按快照形状取 ``container["listeners"]`` → 找不到
        键 → 8100 的 PID 与启动时间**永远是 None**，第 ③ 层的「陈旧行」核对
        与 PID 记录成了死代码（真机实测：第 ③ 层打印 ``监听 pid=[37020]`` 而
        ``8100_started_at=None``）。
        """
        with mock.patch.object(
                tool.snapshot_tool, "listening_pids",
                new=lambda port, run=None: {
                    "pids": [37020] if port == 8100 else [], "reason": "ok"}), \
                mock.patch.object(
                    tool.snapshot_tool, "query_processes",
                    new=lambda pids: {37020: {"name": "um-share-python.exe",
                                              "parent_pid": 20608,
                                              "started_at": 1000.0,
                                              "started_at_reason": "ok"}}):
            ops = self._ops(matches=False)
            facts = self._collect(ops)
        self.assertEqual(37020, facts["gateway_startup"]["pid"])
        self.assertEqual(1000.0, facts["gateway_startup"]["started_at"])
        self.assertEqual("ok", facts["gateway_startup"]["started_at_reason"])
        # 第 ③ 层因此真的能核对启动行是否属于当前实例（不再一律读不到启动时间）。
        layer = _item(tool.evaluate(None, facts), "③")
        self.assertIn("8100_started_at=1000.0", layer["detail"])


# ------------------------------------- 窗口后权威读数接缝（执行轮 11；加性）
class AuthoritativeSeamStructureTests(unittest.TestCase):
    """① 结构可用：tool / schema_version / window_signals 与快照同 schema。"""

    def test_good_payload_passes_the_four_checks(self):
        evidence = _seam()
        self.assertTrue(evidence["usable"], evidence)
        self.assertIsNone(evidence["reason"])
        self.assertEqual(4, evidence["watchdog"]["state"])
        self.assertEqual("Running", evidence["watchdog"]["state_text"])
        self.assertTrue(evidence["elevated"])
        self.assertEqual(AUTH_IDENTITY, evidence["identity"])
        self.assertEqual(AUTH_CAPTURED_AT, evidence["captured_at"])

    def test_unreadable_file_is_rejected_with_the_load_error(self):
        evidence = _seam(error="authoritative_unreadable:FileNotFoundError")
        self.assertEqual("authoritative_unreadable:FileNotFoundError",
                          _denied_reason(evidence))

    def test_unparsable_file_is_rejected(self):
        evidence = _seam(error="authoritative_unparsable:not_json")
        self.assertEqual("authoritative_unparsable:not_json",
                          _denied_reason(evidence))

    def test_wrong_tool_is_rejected(self):
        """合并过的快照（tool=share_chain_snapshot）不得当权威读数用。"""
        evidence = _seam(_before())
        self.assertEqual("authoritative_tool_mismatch:'share_chain_snapshot'",
                          _denied_reason(evidence))

    def test_other_schema_version_is_rejected(self):
        evidence = _seam(_authoritative_payload(schema_version=2))
        self.assertEqual("authoritative_schema_version_mismatch:2",
                          _denied_reason(evidence))

    def test_missing_window_signals_is_rejected(self):
        payload = _authoritative_payload()
        del payload["window_signals"]
        self.assertEqual("authoritative_shape:authoritative_readings_invalid:"
                          "window_signals 缺失",
                          _denied_reason(_seam(payload)))

    def test_bad_status_is_rejected_not_accepted_halfway(self):
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"] = _signal(
            "watchdog_task", "maybe")
        evidence = _seam(payload)
        self.assertEqual("authoritative_signal_rejected:watchdog_task:"
                          "bad_status:'maybe'",
                          _denied_reason(evidence))

    def test_watchdog_missing_from_the_readings_is_rejected(self):
        payload = _authoritative_payload()
        del payload["window_signals"]["watchdog_task"]
        self.assertEqual("authoritative_signal_missing:watchdog_task",
                          _denied_reason(_seam(payload)))

    def test_watchdog_with_missing_keys_is_rejected(self):
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"] = _signal(
            "watchdog_task", "ok")
        self.assertEqual(
            "authoritative_signal_rejected:watchdog_task:missing_keys:"
            "task_name,state,state_text,elevated,identity",
            _denied_reason(_seam(payload)))

    def test_rederivation_mismatch_is_undecidable_not_pass(self):
        """探针结论与快照工具重算不一致 → ⑧ 记 undecidable（绝不折成通过）。"""
        payload = _authoritative_payload()
        # 探针自称 ok（State=Running），原始信封却是提升身份下的「未注册」：
        # 快照工具按自己的规则重算得到 absent，两者不一致 → 不折成通过。
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            found=False, state=None, state_text=None,
            error="CimJobException|CmdletizationQuery_NotFound_TaskName")
        evidence = _seam(payload)
        self.assertTrue(evidence["usable"], evidence["reason"])
        self.assertIn("probe_rederivation_mismatch", evidence["warnings"])
        self.assertEqual("undecidable", evidence["watchdog"]["status"])
        layer = _item(tool.evaluate(_before_with_reading(), _after(), None,
                                     evidence), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("probe_rederivation_mismatch", layer["detail"])
        self.assertIn("先补取证据", layer["detail"])

    def test_probe_not_marked_read_only_is_a_recorded_warning(self):
        """read_only 不是 True：记告警（读数仍然可用），绝不静默。"""
        evidence = _seam(_authoritative_payload(read_only=False))
        self.assertTrue(evidence["usable"])
        self.assertIn("probe_not_marked_read_only:False", evidence["warnings"])

    def test_rejected_optional_signals_are_recorded_not_fatal(self):
        """raw_txt / backend_pid_file 形状被拒：只记告警，⑧ 仍可用。"""
        payload = _authoritative_payload()
        payload["window_signals"]["raw_txt"] = _signal("raw_txt", "ok",
                                                     path="p")
        evidence = _seam(payload)
        self.assertTrue(evidence["usable"], evidence["reason"])
        self.assertIsNone(evidence["raw_mtime"])
        self.assertIn("raw_txt_rejected:missing_keys:exists,size,mtime,"
                      "mtime_utc_iso", evidence["warnings"])

    def test_the_loader_is_the_snapshot_tools_own_loader(self):
        """同一口径：加载走 share_chain_snapshot.load_authoritative_file。"""
        with tempfile.TemporaryDirectory(prefix="post-restart-auth-") as tmp:
            path = os.path.join(tmp, "authoritative-after.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(_authoritative_payload(), handle, ensure_ascii=False)
            loaded = tool.load_authoritative_readings(path)
            self.assertIsNone(loaded["error"])
            self.assertEqual(AUTH_IDENTITY,
                             loaded["readings"]["identity"])
            evidence = tool.authoritative_evidence(loaded, _after(),
                                                   _before_with_reading())
            self.assertTrue(evidence["usable"], evidence["reason"])


class AuthoritativeSeamEnvelopeTests(unittest.TestCase):
    """① 的补强：需要原始信封重算的 watchdog_task 缺可解析信封 → ⑧ undecidable。

    复核者（执行轮 11）现场：合并只在 ``query`` 是 dict 时才重算，手改 JSON
    删掉 ``query`` 后只凭自报字段仍给 ``usable=True`` → ⑧ 曾可能报 PASS。
    现在由 ``share_chain_snapshot.merge_authoritative_readings`` 这个**唯一判定点**
    判 ``undecidable``（``authoritative_envelope_missing`` /
    ``authoritative_envelope_unparsable``）——绝不只凭自报字段给 PASS。
    """

    def test_watchdog_without_the_envelope_is_undecidable_not_pass(self):
        """反例（复核者）：删掉 query（自报 ok/State=Running）→ ⑧ 不得报 PASS。"""
        payload = _authoritative_payload()
        del payload["window_signals"]["watchdog_task"]["query"]
        evidence = _seam(payload)
        # 四道校验（结构 / 新鲜度 / 身份 / 时间顺序）仍通过；但 watchdog_task
        # 这一条信号没有可核对的信封 → ⑧ 按信号状态记 undecidable。
        self.assertTrue(evidence["usable"], evidence)
        self.assertEqual("undecidable", evidence["watchdog"]["status"])
        self.assertEqual("authoritative_envelope_missing",
                          evidence["watchdog"]["reason"])
        self.assertIn("probe_envelope_missing", evidence["warnings"])
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     evidence), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_envelope_missing", layer["detail"])
        self.assertIn("先补取证据", layer["detail"])

    def test_envelope_present_but_recompute_mismatched_is_undecidable(self):
        """信封在但重算与自报不一致 → ⑧ undecidable（绝不折成通过）。"""
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            found=False, state=None, state_text=None,
            error="CimJobException|CmdletizationQuery_NotFound_TaskName")
        evidence = _seam(payload)
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     evidence), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("probe_rederivation_mismatch", layer["detail"])
        self.assertIn("先补取证据", layer["detail"])

    def test_envelope_present_and_consistent_may_pass(self):
        """信封在且与自报一致（State=Running）→ ⑧ 才可能 PASS。"""
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam()), "⑧")
        self.assertEqual("pass", layer["verdict"])
        self.assertIn("status='ok'", layer["detail"])
        self.assertIn("state=4", layer["detail"])


class AuthoritativeSeamFreshnessTests(unittest.TestCase):
    """② 新鲜度：不早于 8100 进程启动时间、不早于窗口前快照。"""

    def test_capture_before_the_8100_start_is_undecidable(self):
        after = _after(gateway_startup=dict(
            _after()["gateway_startup"], started_at=AUTH_CAPTURED_AT + 60.0))
        evidence = _seam(after=after)
        self.assertEqual("authoritative_stale:before_8100_start",
                          _denied_reason(evidence))
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     evidence), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("先补取证据", layer["detail"])

    def test_capture_before_the_before_snapshot_is_undecidable(self):
        evidence = _seam(before=_before_with_reading(
            captured_at=AUTH_CAPTURED_AT + 60.0))
        self.assertEqual("authoritative_stale:before_snapshot",
                          _denied_reason(evidence))

    def test_reading_from_before_the_window_is_undecidable(self):
        """反例：把窗口前那份旧读数当窗口后读数交进来 → 拒绝（不得报 PASS）。"""
        stale = _authoritative_payload(captured_at=50.0)
        after = _after(gateway_startup=dict(
            _after()["gateway_startup"], started_at=200.0))
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(stale, after=after)), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_stale:before_8100_start",
                      layer["detail"])
        self.assertIn("先补取证据", layer["detail"])

    def test_unreadable_8100_start_time_is_unverifiable(self):
        after = _after(gateway_startup=dict(
            _after()["gateway_startup"], started_at=None))
        evidence = _seam(after=after)
        self.assertEqual("authoritative_freshness_unverifiable:"
                          "gateway_started_at_unavailable",
                          _denied_reason(evidence))

    def test_missing_before_snapshot_is_unverifiable(self):
        evidence = tool.authoritative_evidence(_loaded(), _after(), None)
        self.assertEqual("authoritative_freshness_unverifiable:"
                          "before_snapshot_captured_at_unavailable",
                          _denied_reason(evidence))

    def test_before_snapshot_without_capture_time_is_unverifiable(self):
        before = _before_with_reading()
        del before["captured_at"]
        evidence = tool.authoritative_evidence(_loaded(), _after(), before)
        self.assertEqual("authoritative_freshness_unverifiable:"
                          "before_snapshot_captured_at_unavailable",
                          _denied_reason(evidence))

    def test_unreadable_capture_time_is_rejected(self):
        evidence = _seam(_authoritative_payload(captured_at=None))
        self.assertEqual("authoritative_captured_at_unreadable:None",
                          _denied_reason(evidence))

    def test_capture_time_equal_to_the_8100_start_is_fresh(self):
        """边界：采集时刻等于 8100 启动时间（不早于）→ 仍然可用。"""
        after = _after(gateway_startup=dict(
            _after()["gateway_startup"], started_at=AUTH_CAPTURED_AT))
        evidence = _seam(after=after)
        self.assertTrue(evidence["usable"], evidence["reason"])

    def test_freshness_is_evaluated_before_identity(self):
        """顺序夹具：既过期又非提升身份的读数先得「过期」原因码。

        求值顺序 = 判据编号 ① 结构 → ② 新鲜度 → ③ 身份 → ④ 时间顺序；
        若实现改回 ①→③→②→④，这条会得 ``authoritative_not_elevated``。
        """
        payload = _authoritative_payload(elevated=False)
        after = _after(gateway_startup=dict(
            _after()["gateway_startup"], started_at=AUTH_CAPTURED_AT + 60.0))
        evidence = _seam(payload, after=after)
        self.assertFalse(evidence["usable"], evidence)
        self.assertEqual("authoritative_stale:before_8100_start",
                          evidence["reason"])
        self.assertNotIn("authoritative_not_elevated", evidence["reason"])
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     evidence), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_stale:before_8100_start", layer["detail"])


class AuthoritativeSeamIdentityTests(unittest.TestCase):
    """③ 身份：读数须声明采集身份且为提升身份。"""

    def test_non_elevated_reading_is_rejected(self):
        evidence = _seam(_authoritative_payload(elevated=False))
        self.assertEqual("authoritative_not_elevated", _denied_reason(evidence))
        self.assertIs(False, evidence["elevated"])

    def test_undeclared_elevated_is_rejected(self):
        payload = _authoritative_payload()
        del payload["elevated"]
        evidence = _seam(payload)
        self.assertIn("authoritative_identity_undeclared",
                      _denied_reason(evidence))

    def test_undeclared_identity_name_is_rejected(self):
        payload = _authoritative_payload()
        del payload["identity"]
        evidence = _seam(payload)
        self.assertIn("authoritative_identity_undeclared",
                      _denied_reason(evidence))

    def test_empty_identity_name_is_rejected(self):
        evidence = _seam(_authoritative_payload(identity=""))
        self.assertIn("authoritative_identity_undeclared",
                      _denied_reason(evidence))

    def test_non_elevated_envelope_conflict_is_rejected(self):
        """顶层自称提升、原始信封 elevated=False：两份读数矛盾，不得当权威。"""
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            elevated=False, found=True, state=4, state_text="Running")
        evidence = _seam(payload)
        self.assertEqual(
            "authoritative_identity_conflict:watchdog_envelope",
            _denied_reason(evidence))

    def test_identity_conflict_between_top_level_and_envelope_is_rejected(self):
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"]["identity"] = "OTHER\\user"
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            elevated=True, found=True, state=4, state_text="Running")
        payload["window_signals"]["watchdog_task"]["query"]["name"] = \
            "OTHER\\user"
        evidence = _seam(payload)
        self.assertIn("authoritative_identity_conflict",
                      _denied_reason(evidence))

    def test_non_elevated_reading_never_decides_layer_eight(self):
        """反例：非提升身份采的读数即使说 Running，也不得让 ⑧ 报 PASS。"""
        payload = _authoritative_payload(elevated=False)
        payload["window_signals"]["watchdog_task"]["elevated"] = False
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            elevated=False, found=True, state=4, state_text="Running")
        layer = _item(tool.evaluate(_before_with_reading(), _after(), None,
                                     _seam(payload)), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_not_elevated", layer["detail"])
        self.assertIn("先补取证据", layer["detail"])


class AuthoritativeSeamOrderTests(unittest.TestCase):
    """④ 时间顺序：窗口后读数不得早于窗口前读数，颠倒即拒绝。"""

    def test_reading_earlier_than_the_before_reading_is_rejected(self):
        evidence = _seam(before=_before_with_reading(
            reading_at=AUTH_CAPTURED_AT + 60.0))
        self.assertEqual("authoritative_time_reversed:before_reading",
                          _denied_reason(evidence))
        self.assertEqual(AUTH_CAPTURED_AT + 60.0,
                          evidence["references"]["before_reading_captured_at"])

    def test_before_reading_without_capture_time_is_unverifiable(self):
        evidence = _seam(before=_before())
        self.assertEqual("authoritative_freshness_unverifiable:"
                          "before_reading_captured_at_unavailable",
                          _denied_reason(evidence))

    def test_equal_times_are_not_reversed(self):
        evidence = _seam(before=_before_with_reading(
            reading_at=AUTH_CAPTURED_AT))
        self.assertTrue(evidence["usable"], evidence["reason"])

    def test_reversed_reading_never_decides_layer_eight(self):
        """反例：前后时间颠倒 → ⑧ 记 undecidable，绝不报 PASS。"""
        before = _before_with_reading(reading_at=AUTH_CAPTURED_AT + 60.0)
        layer = _item(tool.evaluate(before, _after(), None, _seam(
            before=before)), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_time_reversed:before_reading",
                      layer["detail"])
        self.assertIn("先补取证据", layer["detail"])


class AuthoritativeSeamVerdictTests(unittest.TestCase):
    """⑧（及 ③/④）用权威读数判定；给定时以它为准，绝不退回自身身份读数。"""

    def test_authoritative_running_decides_layer_eight_when_the_local_read_is_denied(self):
        """维护者点名的场景：普通权限读不到 watchdog，权威读数说 Running → ⑧ PASS。"""
        after = _after(watchdog=_live_watchdog_denied())
        result = tool.evaluate(_before_with_reading(), after, None, _seam())
        layer = _item(result, "⑧")
        self.assertEqual("pass", layer["verdict"])
        self.assertIn("status='ok'", layer["detail"])
        self.assertIn("identity='DAICNT\\\\dacint'", layer["detail"])

    def test_authoritative_ready_decides_layer_eight_as_fail(self):
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"] = _watchdog_signal(
            state=3, state_text="Ready")
        payload["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            state=3, state_text="Ready")
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(payload)), "⑧")
        self.assertEqual("fail", layer["verdict"])
        self.assertIn("Ready", layer["detail"])

    def test_authoritative_absent_is_a_fail_not_undecidable(self):
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"] = _signal(
            "watchdog_task", "absent", task_name="umshare-watchdog",
            state=None, state_text=None, elevated=True,
            identity=AUTH_IDENTITY, reason="not_registered_elevated",
            detail="提升身份下的权威读数：任务未注册",
            query=_query_envelope(found=False, state=None, state_text=None,
                                   error="CimJobException|"
                                         "CmdletizationQuery_NotFound_TaskName"))
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(payload)), "⑧")
        self.assertEqual("fail", layer["verdict"])
        self.assertIn("未注册", layer["detail"])

    def test_authoritative_undecidable_signal_is_undecidable(self):
        """自报 undecidable + 一致信封（重算同样 undecidable）→ ⑧ undecidable。

        本工具只采信信封重算的结果（reason 来自重算，不是自报字段）；
        层 ⑧ 绝不因此退回自身身份那次读数（本地读数是拒绝访问）。
        """
        payload = _authoritative_payload()
        payload["window_signals"]["watchdog_task"] = _signal(
            "watchdog_task", "undecidable", task_name="umshare-watchdog",
            state=None, state_text=None, elevated=True,
            identity=AUTH_IDENTITY, reason="not_visible_to_current_identity",
            detail="探针自己也没读到",
            query=_query_envelope(found=True, state=None, state_text=None))
        after = _after(watchdog=_live_watchdog_denied())
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(payload)), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("state_unavailable", layer["detail"])
        self.assertIn("先补取证据", layer["detail"])
        self.assertNotIn("拒绝访问", layer["detail"])

    def test_rejected_reading_never_falls_back_to_the_local_read(self):
        """反例：给了 --authoritative 但读数被拒 → ⑧ 绝不退回自身身份那次读数。"""
        after = _after()  # 自身身份这次读到 Running
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(error="authoritative_unreadable:"
                                                  "FileNotFoundError")), "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("authoritative_unreadable:FileNotFoundError",
                      layer["detail"])

    def test_layer_three_uses_the_authoritative_raw_mtime(self):
        """③ 的 raw.txt mtime 用权威读数补（路径一致时），并记录来源。"""
        startup = dict(_after()["gateway_startup"], raw_mtime=None,
                       reason="raw_unreadable", error="PermissionError")
        after = _after(gateway_startup=startup)
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam()), "③")
        self.assertIn("raw_mtime=1790562898.0", layer["detail"])
        self.assertIn("raw_mtime_source='窗口后权威读数'", layer["detail"])
        # 夹具里启动行本身读得到（真机上普通权限读不到 raw.txt），所以这里 PASS；
        # 真机上启动行读不到时该层仍是 undecidable——权威读数只补 mtime 这条核对。
        self.assertEqual("pass", layer["verdict"])

    def test_layer_three_ignores_a_mismatched_raw_path(self):
        """权威读数的 raw.txt 路径与本次采集的 raw_path 不一致 → 不拿它当 mtime。"""
        payload = _authoritative_payload()
        payload["window_signals"]["raw_txt"] = _raw_txt_signal(
            path=r"D:\other\raw.txt")
        startup = dict(_after()["gateway_startup"], raw_mtime=None,
                       reason="raw_unreadable", error="PermissionError")
        after = _after(gateway_startup=startup)
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam(payload)), "③")
        self.assertNotIn("raw_mtime=1790562898.0", layer["detail"])
        self.assertIn("raw_mtime_source='本工具采集'", layer["detail"])

    def test_layer_three_keeps_the_local_mtime_when_it_is_readable(self):
        """本地能读 raw.txt mtime 时仍用本地的（权威读数只是兜底）。"""
        layer = _item(tool.evaluate(_before_with_reading(), _after(), None,
                                     _seam()), "③")
        self.assertIn("raw_mtime=2.0", layer["detail"])
        self.assertIn("raw_mtime_source='本工具采集'", layer["detail"])
        self.assertEqual("pass", layer["verdict"])

    def test_layer_four_prints_the_authoritative_pid_corroboration(self):
        """④ 的 backend.pid 身份旁证来自权威读数，但判定仍由自身观察决定。"""
        after = _after(backend_pid={"state": "pid_stale", "pid_file_present":
                                    True, "pid": 4242, "pid_matches": False,
                                    "port_listening": True, "error": None})
        layer = _item(tool.evaluate(_before_with_reading(), after, None,
                                     _seam()), "④")
        self.assertEqual("pass", layer["verdict"])
        self.assertIn("窗口后权威读数 backend.pid 身份旁证", layer["detail"])
        self.assertIn("content='54300'", layer["detail"])
        self.assertIn("pid=54300", layer["detail"])

    def test_layer_four_verdict_is_unchanged_by_a_rejected_reading(self):
        layer = _item(tool.evaluate(_before_with_reading(), _after(), None,
                                     _seam(error="authoritative_unreadable:"
                                                  "FileNotFoundError")), "④")
        self.assertEqual("pass", layer["verdict"])
        self.assertIn("authoritative_unreadable:FileNotFoundError",
                      layer["detail"])

    def test_the_seam_reuses_the_snapshot_tools_watchdog_signal_shape(self):
        """同一口径：⑧ 用的是快照工具重算出来的规范信号（含 state/state_text）。"""
        evidence = _seam()
        signal = evidence["watchdog"]
        self.assertEqual("watchdog_task", signal["signal"])
        self.assertEqual("ok", signal["status"])
        self.assertEqual(4, signal["state"])
        self.assertEqual("Running", signal["state_text"])
        self.assertIs(True, signal["elevated"])
        self.assertEqual(AUTH_IDENTITY, signal["identity"])


class AuthoritativeSeamAdditivityTests(unittest.TestCase):
    """加性：不提供 --authoritative 时，八层判定与报告逐字不变。"""

    def test_without_the_switch_the_verdicts_are_byte_identical(self):
        after = _after(watchdog=_live_watchdog_denied())
        before = _before_with_reading()
        plain = tool.evaluate(before, after, None)
        explicit = tool.evaluate(before, after, None, None)
        self.assertEqual(plain, explicit)
        self.assertEqual(2, plain["exit_code"])
        layer = _item(plain, "⑧")
        self.assertEqual("undecidable", layer["verdict"])
        self.assertIn("拒绝访问", layer["detail"])
        # 不提供开关时绝不出现接缝的文案 / 原因码。
        self.assertNotIn("权威读数", layer["detail"])
        self.assertNotIn("先补取证据", layer["detail"])

    def test_without_the_switch_the_report_is_byte_identical(self):
        after = _after(watchdog=_live_watchdog_denied())
        before = _before_with_reading()
        result = tool.evaluate(before, after, None)
        self.assertEqual(tool.format_report(after, result, "before.json"),
                          tool.format_report(after, result, "before.json",
                                               None, None))
        text = tool.format_report(after, result, "before.json")
        self.assertNotIn("窗口后权威读数", text)

    def test_without_the_switch_the_plan_is_byte_identical(self):
        self.assertEqual(tool.format_plan("before.json"),
                          tool.format_plan("before.json", None))
        self.assertNotIn("--authoritative", tool.format_plan("before.json"))

    def test_with_the_switch_the_report_names_the_reading(self):
        after = _after(watchdog=_live_watchdog_denied())
        evidence = _seam()
        result = tool.evaluate(_before_with_reading(), after, None, evidence)
        text = tool.format_report(after, result, "before.json",
                                   AUTH_PATH, evidence)
        self.assertIn("窗口后权威读数（--authoritative）：path=%s" % AUTH_PATH,
                      text)
        self.assertIn("usable=True", text)
        self.assertIn("窗口后权威读数 watchdog：state=4", text)

    def test_with_the_switch_the_plan_names_the_switch(self):
        text = tool.format_plan("before.json", AUTH_PATH)
        self.assertIn("--authoritative", text)
        self.assertIn("时间顺序", text)
        self.assertIn("先补取证据", text)


class AuthoritativeSeamCliTests(unittest.TestCase):
    """CLI：--check + --authoritative 端到端（采集替身，不连任何端口）。"""

    def _paths(self, tmp, payload=None, before=None):
        auth = os.path.join(tmp, "authoritative-after.json")
        with open(auth, "w", encoding="utf-8") as handle:
            json.dump(payload if payload is not None
                      else _authoritative_payload(), handle,
                      ensure_ascii=False)
        prior = os.path.join(tmp, "snapshot-before.json")
        with open(prior, "w", encoding="utf-8") as handle:
            json.dump(before if before is not None
                      else _before_with_reading(), handle,
                      ensure_ascii=False)
        return prior, auth

    def _run(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = tool.main(argv)
        return code, output.getvalue()

    def test_check_with_a_usable_reading_passes_every_layer(self):
        with tempfile.TemporaryDirectory(prefix="post-restart-seam-") as tmp:
            before_path, auth_path = self._paths(tmp)
            with mock.patch.object(
                    tool, "collect_facts",
                    lambda: _after(watchdog=_live_watchdog_denied())):
                code, text = self._run(["--check", "--before", before_path,
                                       "--authoritative", auth_path])
        self.assertEqual(0, code, text)
        self.assertIn("八层全部 PASS", text)

    def test_check_with_a_rejected_reading_is_exit_two_with_guidance(self):
        """四类拒绝之一：读数被拒 → 退出码 2，⑧ undecidable，文案仍是先补取证据。"""
        with tempfile.TemporaryDirectory(prefix="post-restart-seam-") as tmp:
            before_path, auth_path = self._paths(
                tmp, payload=_authoritative_payload(elevated=False))
            with mock.patch.object(tool, "collect_facts",
                                   lambda: _after()):
                code, text = self._run(["--check", "--before", before_path,
                                       "--authoritative", auth_path])
        self.assertEqual(2, code)
        self.assertIn("未完成核对", text)
        self.assertIn("authoritative_not_elevated", text)
        self.assertIn("先补取证据", text)

    def test_check_with_an_unreadable_reading_names_the_error(self):
        with tempfile.TemporaryDirectory(prefix="post-restart-seam-") as tmp:
            before_path, _auth = self._paths(tmp)
            missing = os.path.join(tmp, "no-such-authoritative.json")
            with mock.patch.object(tool, "collect_facts",
                                   lambda: _after()):
                code, text = self._run(["--check", "--before", before_path,
                                       "--authoritative", missing])
        self.assertEqual(2, code)
        self.assertIn("authoritative_unreadable:FileNotFoundError", text)
        self.assertIn("先补取证据", text)

    def test_dry_run_with_the_switch_collects_nothing(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("dry-run 不得采集任何事实")

        with mock.patch.object(tool, "collect_facts", boom), \
                mock.patch.object(tool, "read_before_snapshot", boom), \
                mock.patch.object(tool, "load_authoritative_readings", boom):
            code, text = self._run(["--authoritative", "auth.json"])
        self.assertEqual(0, code)
        self.assertIn("--authoritative", text)
        self.assertIn("先补取证据", text)

    def test_the_seam_only_reads_the_reading_file(self):
        """接缝只读：加载 + 四道校验 + 判定全程零写动作。"""
        with tempfile.TemporaryDirectory(prefix="post-restart-seam-") as tmp:
            _before_path, auth_path = self._paths(tmp)
            guard = _NoWriteGuard()
            with guard:
                loaded = tool.load_authoritative_readings(auth_path)
                evidence = tool.authoritative_evidence(
                    loaded, _after(), _before_with_reading())
                tool.evaluate(_before_with_reading(), _after(), None, evidence)
        self.assertEqual(guard.writes, [])
        self.assertTrue(evidence["usable"], evidence["reason"])


if __name__ == "__main__":
    unittest.main()
