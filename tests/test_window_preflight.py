# -*- coding: utf-8 -*-
"""`ai-gateway/tools/um-share/window_preflight.py` 的夹具测试（不连生产）。

覆盖维护者点名的三组反向分支，以及其余判据的每一条：

* 第 1 项在途操作：``live=0`` → PASS；``live>0`` → FAIL；读不到 → UNDECIDABLE；
* 第 2 项回退副本：三份全符 → PASS；sha256 不符 → FAIL；字节数不符 → FAIL；
  文件缺失 → FAIL；读不到 → UNDECIDABLE；
* 第 5 项读面键集三态：7 键 → PASS；4 键 → PASS（旧形状，记录哪一种）；
  未知形状 → FAIL；读不到 → UNDECIDABLE；
* 第 3/4/6/7/8/9/10 项的每一条分支；
* 退出码契约：十项全 PASS → 0；任一 FAIL 或 UNDECIDABLE → 2；
* dry-run 不读任何现场（把采集入口打桩成「一调用就失败」后跑 ``main([])``）。
* ``--refresh-gate-table``（2026-09-28 执行轮 14 后加，显式开关；唯一写路径）：
  在**临时副本**上刷新（绝不写仓库真实文件）→ 两份文件前后 sha256 变化、
  写后读回一致、用刷新后的常量文本构造第 3 项事实得 ``table_match=true``；
  未给开关时 ``--check`` 只报 ``gate_table_stale`` 与可用提示，私有两份文件
  sha256 一字不变。

隔离：夹具全在内存字典与 ``tempfile`` 临时目录里；不连 127.0.0.1 的任何端口、
不跑 netstat/CIM/PowerShell、不写仓库任何路径（两份文件使用私有临时副本）、
不重启任何服务、不生成密钥、不发模型请求。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_window_preflight -q
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools" / "um-share"


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_" + module_name,
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load("window_preflight")

FOUR_KEYS = ["key_chars", "key_file", "reason", "wired"]
SEVEN_KEYS = ["file_ready", "file_reason", "instance_loaded", "key_chars",
              "key_file", "request_attribution_detail",
              "request_attribution_observed"]
_DEFAULT_ENVELOPE = object()


# --------------------------------------------------------------- 夹具构造
def _operations(live=0, total=50, error=None, http_status=200):
    return {"http_status": http_status, "total": total, "live": live,
            "error": error}


def _rollback_rows(**overrides):
    """三份回退副本的「全部相符」事实；``overrides`` 按 label 覆盖字段。"""
    rows = []
    for label, rel, want_bytes, want_sha in preflight.ROLLBACK_COPY_TABLE:
        row = {"label": label, "path": rel, "exists": True, "size": want_bytes,
               "sha256": want_sha, "error": None, "error_kind": None,
               "expected_size": want_bytes, "expected_sha256": want_sha}
        row.update(overrides.get(label, {}))
        rows.append(row)
    return rows


def _load_rows(**overrides):
    """三份「将载入版本」的「全部相符」事实；``overrides`` 按 label 覆盖字段。"""
    rows = []
    for label, rel, want_bytes, want_sha in preflight.LOAD_TARGET_TABLE:
        row = {"label": label, "path": rel,
               "table": {"bytes": want_bytes, "sha256": want_sha},
               "workspace": {"exists": True, "size": want_bytes,
                             "sha256": want_sha, "error": None,
                             "error_kind": None},
               "head": {"sha256": want_sha, "size": want_bytes,
                        "error": None},
               "diff": {"clean": True, "error": None, "output": ""},
               "repo_root": "R", "repo_error": None}
        row.update(overrides.get(label, {}))
        rows.append(row)
    return rows


def _listeners(**overrides):
    base = {"8000": {"pids": [35724], "reason": None},
            "8096": {"pids": [51352], "reason": None},
            "8100": {"pids": [37020], "reason": None},
            "8110": {"pids": [54300], "reason": None}}
    base.update(overrides)
    return base


def _surface(keys=None, error=None, funnel_on=True):
    surface = {"http_status": 200, "error": error, "keys": keys,
               "shape": "unknown", "funnel_on": funnel_on,
               "file_ready": None, "instance_loaded_value": None}
    if keys is not None:
        surface["keys"] = sorted(keys)
        surface["shape"] = preflight.snapshot_tool.attribution_shape(keys)
    return surface


def _gate_signal(status="ok", reason="ok", detail="ok"):
    return {"signal": "share_entry_gates", "status": status, "entries": {},
            "reason": reason, "detail": detail}


def _ledger(rows=1680, error=None, last=None, attributed=0):
    return {"path": "L", "rows": rows, "unparsed": 0,
            "last": last if last is not None else {
                "index": 1679, "time": "2026-09-26T23:11:25+08:00",
                "model": "GLM-5.3-Flash", "key_id": None,
                "attribution": "absent"},
            "attributed_rows": [{"index": 0}] * attributed,
            "attribution_counts": {"absent": rows}, "error": error}


def _authoritative(watchdog=None, wfp=None, available=True, reason=None):
    signals = {}
    if watchdog is not None:
        signals["watchdog_task"] = watchdog
    if wfp is not None:
        signals["wfp_limits"] = wfp
    return {"available": available, "reason": reason, "elevated": False,
            "identity": "DAICNT\\dacint", "captured_at": 1.0,
            "captured_at_iso": "2026-09-28T14:00:00+08:00",
            "encoding": "mbcs", "stdout_bytes": 1, "rc": 0,
            "signals": signals}


def _watchdog_signal(status="ok", state=4, state_text="Running",
                      query=_DEFAULT_ENVELOPE, reason="ok",
                      detail="任务已注册"):
    """``query`` 默认给一份与结论一致的信封（可重算）；显式 ``None`` = 无信封。"""
    signal = {"signal": "watchdog_task", "status": status, "state": state,
              "state_text": state_text, "reason": reason, "detail": detail}
    if query is _DEFAULT_ENVELOPE:
        if state is None:
            query = {"elevated": True, "name": "SYSTEM", "found": False,
                     "state": None, "stateText": None,
                     "error": "CimJobException|NotFound"}
        else:
            query = {"elevated": True, "name": "SYSTEM", "found": True,
                     "state": state, "stateText": state_text,
                     "error": None}
    if query is not None:
        signal["query"] = query
    return signal


def _wfp_signal(status="ok", reason="ok", detail="ok", wfp_ok=True,
                 firewall_ok=True):
    return {"signal": "wfp_limits", "status": status, "reason": reason,
            "detail": detail, "wfp_ok": wfp_ok, "firewall_ok": firewall_ok}


def _all_pass_facts():
    return {
        "operations": _operations(),
        "rollback": _rollback_rows(),
        "load_targets": _load_rows(),
        "listeners": _listeners(),
        "read_surface": _surface(keys=FOUR_KEYS),
        "share_entry_probes": _probes_all_ok(),
        "share_ledger": _ledger(),
        "authoritative": _authoritative(
            watchdog=_watchdog_signal(), wfp=_wfp_signal()),
    }


def _probes_all_ok():
    return {name: {"status": status, "code": code, "error": None}
            for name, _path, status, code
            in preflight.snapshot_tool.SHARE_ENTRY_EXPECTED}


# ------------------------------------------------------------------- 第 1 项
class OperationsTests(unittest.TestCase):
    def test_live_zero_passes(self):
        item = preflight.judge_operations(_operations(live=0))
        self.assertEqual(preflight.PASS, item["verdict"])

    def test_live_nonzero_fails(self):
        item = preflight.judge_operations(_operations(live=2))
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("in_flight:2", item["reason"])

    def test_probe_failure_is_undecidable(self):
        item = preflight.judge_operations(_operations(error="URLError"))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])

    def test_missing_live_is_undecidable(self):
        item = preflight.judge_operations({"http_status": 200, "total": 3})
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertEqual("operations_live_unavailable", item["reason"])


# ------------------------------------------------------------------- 第 2 项
class RollbackCopyTests(unittest.TestCase):
    def test_all_match_passes(self):
        item = preflight.judge_rollback_copies(_rollback_rows())
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual(3, len(item["readings"]["rows"]))

    def test_sha256_mismatch_fails(self):
        rows = _rollback_rows(**{"rollback-usagesrv.py": {"sha256": "0" * 64}})
        item = preflight.judge_rollback_copies(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("rollback_copy_sha256_mismatch:rollback-usagesrv.py",
                      item["reason"])

    def test_size_mismatch_fails(self):
        rows = _rollback_rows(**{"rollback-usagesrv.py": {"size": 161104}})
        item = preflight.judge_rollback_copies(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("rollback_copy_size_mismatch", item["reason"])

    def test_missing_copy_fails(self):
        rows = _rollback_rows(**{"rollback-usagesrv.py": {
            "exists": False, "size": None, "sha256": None,
            "error": "FileNotFoundError", "error_kind": "missing"}})
        item = preflight.judge_rollback_copies(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("rollback_copy_missing:rollback-usagesrv.py",
                      item["reason"])

    def test_unreadable_copy_is_undecidable(self):
        rows = _rollback_rows(**{"rollback-usagesrv.py": {
            "exists": False, "size": None, "sha256": None,
            "error": "PermissionError", "error_kind": "unreadable"}})
        item = preflight.judge_rollback_copies(rows)
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("rollback_copy_unreadable", item["reason"])


# ------------------------------------------------------------------- 第 3 项
class LoadTargetTests(unittest.TestCase):
    def test_workspace_head_and_table_match_passes(self):
        item = preflight.judge_load_targets_head(_load_rows())
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual([], item["readings"]["drift"])

    def test_workspace_differs_from_head_fails(self):
        rows = _load_rows(**{"usagesrv.py": {
            "workspace": {"exists": True, "size": 161584, "sha256": "f" * 64,
                          "error": None, "error_kind": None}}})
        item = preflight.judge_load_targets_head(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("workspace_differs_from_head:usagesrv.py", item["reason"])

    def test_stale_gate_table_fails(self):
        rows = _load_rows(**{"console/share_production.py": {
            "workspace": {"exists": True, "size": 99133, "sha256": "a" * 64,
                          "error": None, "error_kind": None},
            "head": {"sha256": "a" * 64, "size": 99133, "error": None}}})
        item = preflight.judge_load_targets_head(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("gate_table_stale:console/share_production.py",
                      item["reason"])
        drift = item["readings"]["drift"]
        self.assertEqual(1, len(drift))
        self.assertEqual("a" * 64, drift[0]["workspace_sha256"])

    def test_head_blob_unavailable_is_undecidable(self):
        rows = _load_rows(**{"usagesrv.py": {
            "head": {"sha256": None, "size": None,
                     "error": "head_blob_unavailable:rc=128"}}})
        item = preflight.judge_load_targets_head(rows)
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])

    def test_dirty_worktree_fails(self):
        rows = _load_rows(**{"usagesrv.py": {
            "diff": {"clean": False, "error": None, "output": "M\tx"}}})
        item = preflight.judge_load_targets_head(rows)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("git_diff_not_clean:usagesrv.py", item["reason"])


# ------------------------------------------------------------------- 第 4 项
class ListenerTests(unittest.TestCase):
    def test_all_listening_passes(self):
        item = preflight.judge_listeners(_listeners())
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual(35724, item["readings"]["8000"]["pids"][0])

    def test_port_without_listener_fails(self):
        item = preflight.judge_listeners(_listeners(**{"8100": {"pids": [],
                                                              "reason": None}}))
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("not_listening:8100", item["reason"])

    def test_query_failure_is_undecidable(self):
        item = preflight.judge_listeners(
            _listeners(**{"8096": {"pids": None, "reason": "query_failed"}}))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("listener_query_failed:8096", item["reason"])


# ------------------------------------------------------------------- 第 5 项
class ReadSurfaceTests(unittest.TestCase):
    def test_seven_keys_passes_and_records_new_shape(self):
        item = preflight.judge_read_surface(_surface(keys=SEVEN_KEYS))
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual("seven_key", item["reason"])

    def test_four_keys_passes_and_records_legacy_shape(self):
        item = preflight.judge_read_surface(_surface(keys=FOUR_KEYS))
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual("four_key", item["reason"])

    def test_unknown_shape_fails(self):
        item = preflight.judge_read_surface(_surface(keys=["a", "b", "c"]))
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertEqual("unknown_attribution_shape", item["reason"])

    def test_unreadable_is_undecidable(self):
        item = preflight.judge_read_surface(_surface(error="URLError"))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("read_surface_probe_failed", item["reason"])


# ------------------------------------------------------------------- 第 6 项
class FunnelTests(unittest.TestCase):
    def test_bool_passes(self):
        item = preflight.judge_funnel(_surface(keys=FOUR_KEYS, funnel_on=True))
        self.assertEqual(preflight.PASS, item["verdict"])

    def test_missing_field_is_undecidable(self):
        item = preflight.judge_funnel(
            _surface(keys=FOUR_KEYS, funnel_on=None))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertEqual("funnel_field_unavailable", item["reason"])

    def test_unreadable_is_undecidable(self):
        item = preflight.judge_funnel(_surface(error="URLError"))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])


# ------------------------------------------------------------------- 第 7 项
class EntryGateTests(unittest.TestCase):
    def test_ok_passes(self):
        item = preflight.judge_share_entry_gates(_probes_all_ok())
        self.assertEqual(preflight.PASS, item["verdict"])

    def test_gate_mismatch_fails(self):
        probes = _probes_all_ok()
        probes["whitelist_models"] = {"status": 404, "code": "route_not_shared",
                                     "error": None}
        item = preflight.judge_share_entry_gates(probes)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("whitelist_models", item["reason"])

    def test_probe_failure_is_undecidable(self):
        probes = _probes_all_ok()
        probes["root"] = {"status": None, "code": None, "error": "URLError"}
        item = preflight.judge_share_entry_gates(probes)
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])


# ------------------------------------------------------------------- 第 8 项
class ShareLedgerTests(unittest.TestCase):
    def test_rows_present_passes_and_records_tail(self):
        item = preflight.judge_share_ledger_tail(_ledger(rows=1680))
        self.assertEqual(preflight.PASS, item["verdict"])
        self.assertEqual("absent",
                         item["readings"]["last"]["attribution"])

    def test_empty_ledger_fails(self):
        item = preflight.judge_share_ledger_tail(_ledger(rows=0, last=None))
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertEqual("ledger_empty", item["reason"])

    def test_unreadable_ledger_is_undecidable(self):
        item = preflight.judge_share_ledger_tail(
            _ledger(rows=0, error="ledger_unreadable:FileNotFoundError"))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("ledger_unreadable", item["reason"])


# ------------------------------------------------------------------- 第 9 项
class WatchdogTests(unittest.TestCase):
    def test_running_passes(self):
        facts = _authoritative(watchdog=_watchdog_signal(state=4))
        item = preflight.judge_watchdog(facts)
        self.assertEqual(preflight.PASS, item["verdict"])

    def test_not_running_fails(self):
        facts = _authoritative(watchdog=_watchdog_signal(
            state=3, state_text="Ready"))
        item = preflight.judge_watchdog(facts)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertIn("watchdog_state:Ready", item["reason"])

    def test_absent_elevated_fails(self):
        facts = _authoritative(watchdog=_watchdog_signal(
            status="absent", state=None, state_text=None,
            reason="not_registered_elevated", detail="任务未注册"))
        item = preflight.judge_watchdog(facts)
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertEqual("watchdog_not_registered_elevated", item["reason"])

    def test_undecidable_stays_undecidable(self):
        facts = _authoritative(watchdog=_watchdog_signal(
            status="undecidable", state=None, state_text=None,
            reason="not_visible_to_current_identity",
            detail="非提升身份查不到"))
        item = preflight.judge_watchdog(facts)
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertNotEqual("watchdog_not_registered_elevated", item["reason"])

    def test_probe_unavailable_is_undecidable(self):
        facts = _authoritative(available=False, reason="probe_missing")
        item = preflight.judge_watchdog(facts)
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("probe_unavailable", item["reason"])

    def test_rederivation_mismatch_is_undecidable(self):
        query = {"elevated": True, "name": "SYSTEM",
                 "found": False, "state": None, "stateText": None,
                 "error": "CimJobException|NotFound"}
        signal = _watchdog_signal(state=4, query=query)
        item = preflight.judge_watchdog(_authoritative(watchdog=signal))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertEqual("probe_rederivation_mismatch", item["reason"])

    def test_missing_envelope_is_undecidable(self):
        # 探针自报 ok 但没有原始信封：重算输入读不到 → UNDECIDABLE，
        # 绝不静默跳过重算后拿探针的 ok 当 PASS。
        signal = _watchdog_signal(state=4, query=None)
        item = preflight.judge_watchdog(_authoritative(watchdog=signal))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertEqual("probe_rederivation_envelope_missing", item["reason"])
        self.assertFalse(item["readings"]["rederived"])

    def test_consistent_envelope_passes_and_records_rederived(self):
        # 反向对照：信封与结论一致时必须真的走完重算并 PASS（证明重算不是恒假）。
        signal = _watchdog_signal(state=4)
        self.assertIn("query", signal)
        item = preflight.judge_watchdog(_authoritative(watchdog=signal))
        self.assertEqual(preflight.PASS, item["verdict"])
        rederived = item["readings"]["rederived"]
        self.assertEqual("ok", rederived["status"])
        self.assertEqual(4, rederived["state"])


# ------------------------------------------------------------------ 第 10 项
class WfpTests(unittest.TestCase):
    def test_ok_passes(self):
        item = preflight.judge_wfp_limits(
            _authoritative(wfp=_wfp_signal(status="ok")))
        self.assertEqual(preflight.PASS, item["verdict"])

    def test_absent_fails(self):
        item = preflight.judge_wfp_limits(
            _authoritative(wfp=_wfp_signal(status="absent",
                                           reason="wfp_not_in_place",
                                           wfp_ok=False)))
        self.assertEqual(preflight.FAIL, item["verdict"])
        self.assertEqual("wfp_limits_not_in_place", item["reason"])

    def test_undecidable_stays_undecidable(self):
        item = preflight.judge_wfp_limits(
            _authoritative(wfp=_wfp_signal(
                status="undecidable", reason="detection_failure",
                detail="读不到")))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])

    def test_probe_unavailable_is_undecidable(self):
        item = preflight.judge_wfp_limits(
            _authoritative(available=False, reason="probe_run_failed"))
        self.assertEqual(preflight.UNDECIDABLE, item["verdict"])
        self.assertIn("probe_unavailable", item["reason"])


# --------------------------------------------------------------- 退出码契约
class ExitCodeTests(unittest.TestCase):
    def test_all_pass_is_zero(self):
        result = preflight.evaluate(_all_pass_facts())
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(10, len(result["items"]))
        self.assertEqual(10, result["counts"][preflight.PASS])

    def test_any_fail_is_two(self):
        facts = _all_pass_facts()
        facts["operations"] = _operations(live=1)
        result = preflight.evaluate(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual(1, result["counts"][preflight.FAIL])

    def test_any_undecidable_is_two(self):
        facts = _all_pass_facts()
        facts["authoritative"] = _authoritative(
            watchdog=_watchdog_signal(status="undecidable", state=None,
                                      state_text=None,
                                      reason="not_visible_to_current_identity"),
            wfp=_wfp_signal())
        result = preflight.evaluate(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual(1, result["counts"][preflight.UNDECIDABLE])

    def test_missing_facts_are_not_pass(self):
        result = preflight.evaluate({})
        self.assertEqual(2, result["exit_code"])
        self.assertEqual(0, result["counts"][preflight.PASS])

    def test_drift_becomes_warning_and_fails(self):
        facts = _all_pass_facts()
        facts["load_targets"] = _load_rows(**{"usagesrv.py": {
            "workspace": {"exists": True, "size": 1, "sha256": "b" * 64,
                          "error": None, "error_kind": None},
            "head": {"sha256": "b" * 64, "size": 1, "error": None}}})
        result = preflight.evaluate(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual(1, len(result["warnings"]))
        self.assertEqual("usagesrv.py", result["warnings"][0]["label"])


# ---------------------------------------------------------------- dry-run
class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_read_live_state(self):
        original = preflight.collect_facts

        def _boom(*args, **kwargs):
            raise AssertionError("dry-run 不得读现场")

        preflight.collect_facts = _boom
        try:
            rc = preflight.main([])
        finally:
            preflight.collect_facts = original
        self.assertEqual(0, rc)

    def test_plan_lists_gate_hashes(self):
        plan = preflight.format_plan()
        for _label, _rel, want_bytes, want_sha in \
                preflight.ROLLBACK_COPY_TABLE:
            self.assertIn(want_sha, plan)
            self.assertIn(str(want_bytes), plan)
        for _label, _rel, want_bytes, want_sha in preflight.LOAD_TARGET_TABLE:
            self.assertIn(want_sha, plan)

    def test_report_lists_undecidable_items(self):
        facts = _all_pass_facts()
        facts["authoritative"] = _authoritative(
            watchdog=_watchdog_signal(status="undecidable", state=None,
                                      state_text=None,
                                      reason="not_visible_to_current_identity"),
            wfp=_wfp_signal(status="undecidable", reason="detection_failure"))
        result = preflight.evaluate(facts)
        report = preflight.format_report(result, identity="DAICNT\\dacint",
                                        elevated=False,
                                        captured_at_iso="2026-09-28T14:00:00+0800")
        self.assertIn("watchdog_task, wfp_limits", report)
        self.assertIn("未完成核对", report)


# ------------------------------------------------- 门表刷新（显式开关；唯一写路径）
class RefreshGateTableTests(unittest.TestCase):
    """``--refresh-gate-table``：从工作区实测值重写 §1.4 表与 LOAD_TARGET_TABLE。

    夹具只在 ``tempfile`` 临时副本上刷新（工具与门表都传临时路径），
    并断言未给开关时私有两份文件的 sha256 一字不变；另用刷新后的常量文本
    构造第 3 项事实，钉住 ``table_match=true``。
    """

    MEASURED = {
        "share_production.py": (99134, "a" * 64),
        "usagesrv.py": (161585, "b" * 64),
        "share_actions.py": (58051, "c" * 64),
    }

    def _stale_gate_text(self):
        """§1.4 夹具：回退副本行 + 「将载入版本」行都用**当前（过期）常量**。"""
        lines = ["# 动作门（夹具；§1.4 表用当前常量，相对实测值已过期）", "",
                 "| 角色 | 文件（工作区相对路径） | 字节 | sha256 |",
                 "| --- | --- | --- | --- |"]
        for _label, rel, want_bytes, want_sha in preflight.ROLLBACK_COPY_TABLE:
            lines.append("| 回退副本 | `%s` | %s | `%s` |"
                         % (rel, want_bytes, want_sha))
        for _label, rel, want_bytes, want_sha in preflight.LOAD_TARGET_TABLE:
            lines.append("| 将载入版本（HEAD） | `%s` | %s | `%s` |"
                         % (rel, want_bytes, want_sha))
        return "\n".join(lines) + "\n"

    def _collect(self):
        def _facts(path):
            size, sha = self.MEASURED[os.path.basename(path)]
            return {"path": path, "exists": True, "size": size,
                    "sha256": sha, "error": None, "error_kind": None}
        return _facts

    def _parse_tool_table(self, text):
        rows = []
        for label, rel, _want_bytes, _want_sha in preflight.LOAD_TARGET_TABLE:
            pattern = re.compile(
                r'\("%s",\s*\n\s*"%s",\s*\n\s*(\d+),\s*\n\s*"([0-9a-f]{64})"\)'
                % (re.escape(label), re.escape(rel)))
            match = pattern.search(text)
            self.assertIsNotNone(match, "常量里找不到 %s 行" % label)
            rows.append((label, rel, int(match.group(1)), match.group(2)))
        return rows

    def _judge_rows(self, table_rows):
        rows = []
        for label, rel, size, sha in table_rows:
            rows.append({
                "label": label, "path": rel,
                "table": {"bytes": size, "sha256": sha},
                "workspace": {"exists": True, "size": size, "sha256": sha,
                              "error": None, "error_kind": None},
                "head": {"sha256": sha, "size": size, "error": None},
                "diff": {"clean": True, "error": None, "output": ""},
                "repo_root": "R", "repo_error": None})
        return rows

    def test_refresh_writes_both_files_and_table_match_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gate = tmp_path / "metering-action-gate.md"
            tool = tmp_path / "window_preflight.py"
            gate.write_text(self._stale_gate_text(), encoding="utf-8")
            tool.write_bytes((TOOLS_DIR / "window_preflight.py").read_bytes())
            gate_before = hashlib.sha256(gate.read_bytes()).hexdigest()
            tool_before = hashlib.sha256(tool.read_bytes()).hexdigest()
            report = preflight.refresh_gate_table(
                gate_path=str(gate), tool_path=str(tool),
                collect=self._collect())
            self.assertIsNone(report.get("error"), report)
            self.assertEqual(gate_before, report["gate"]["before_sha256"])
            self.assertEqual(tool_before, report["tool"]["before_sha256"])
            self.assertNotEqual(gate_before, report["gate"]["after_sha256"])
            self.assertNotEqual(tool_before, report["tool"]["after_sha256"])
            self.assertTrue(report["gate"]["readback_ok"])
            self.assertTrue(report["tool"]["readback_ok"])
            # 写后读回：常量文本逐项等于工作区实测值。
            refreshed = self._parse_tool_table(tool.read_text(encoding="utf-8"))
            for (label, rel, size, sha), row in zip(refreshed,
                                                    report["rows"]):
                self.assertEqual((label, rel, size, sha),
                                 (row["label"], row["path"], row["size"],
                                  row["sha256"]))
                self.assertEqual(self.MEASURED[os.path.basename(label)],
                                 (size, sha))
            gate_text = gate.read_text(encoding="utf-8")
            for label, _rel, size, sha in refreshed:
                self.assertIn("| 将载入版本（HEAD） | `%s` | %s | `%s` |"
                              % (label, size, sha), gate_text)
            # 刷新后 table_match=true：用刷新后的常量文本构造第 3 项事实。
            item = preflight.judge_load_targets_head(self._judge_rows(refreshed))
            self.assertEqual(preflight.PASS, item["verdict"])
            self.assertEqual([], item["readings"]["drift"])
            for entry in item["readings"]["rows"]:
                self.assertTrue(entry["table_match"])

    def test_check_without_switch_reports_and_never_writes(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        gate_path = Path(temp.name) / "metering-action-gate.md"
        tool_path = Path(temp.name) / "window_preflight.py"
        gate_path.write_text(self._stale_gate_text(), encoding="utf-8")
        tool_path.write_bytes((TOOLS_DIR / "window_preflight.py").read_bytes())
        gate_before = hashlib.sha256(gate_path.read_bytes()).hexdigest()
        tool_before = hashlib.sha256(tool_path.read_bytes()).hexdigest()
        original = preflight.collect_facts

        def _facts():
            facts = _all_pass_facts()
            facts["load_targets"] = _load_rows(**{
                "console/share_production.py": {
                    "workspace": {"exists": True, "size": 99134,
                                  "sha256": "a" * 64, "error": None,
                                  "error_kind": None},
                    "head": {"sha256": "a" * 64, "size": 99134,
                             "error": None}}})
            return facts

        preflight.collect_facts = _facts
        buffer = io.StringIO()
        try:
            with mock.patch.object(preflight, "GATE_TABLE_PATH", str(gate_path)), \
                    mock.patch.object(preflight, "TOOL_PATH", str(tool_path)), \
                    contextlib.redirect_stdout(buffer):
                rc = preflight.main(["--check"])
        finally:
            preflight.collect_facts = original
        self.assertEqual(2, rc)
        text = buffer.getvalue()
        self.assertIn("gate_table_stale", text)
        self.assertIn("--refresh-gate-table", text)
        # 未给开关：只报不写——私有两份文件 sha256 一字不变。
        self.assertEqual(gate_before,
                         hashlib.sha256(gate_path.read_bytes()).hexdigest())
        self.assertEqual(tool_before,
                         hashlib.sha256(tool_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
