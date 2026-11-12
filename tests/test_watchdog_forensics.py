# -*- coding: utf-8 -*-
"""``tools/um-share/readonly/watchdog_forensics.py`` 的夹具测试（不连生产）。

覆盖维护者点名的问题 4（重启 8096 会不会触发看门狗停分享链）与产出 2 要求的三类
夹具：

* 「重启 8096 后 8110 父 PID 变孤儿」——判定必须落在 **no-action**（看门狗不读
  ``ParentProcessId``；证据 ``watchdog-umshare.ps1:174-200`` 只看 8100 监听者的
  映像全路径 + owner）；
* 「8100 短暂不可达」——1 次缺席落在 **no-action**（宽限，:462-481），并附上该情形
  真正的边界：缺席达 3 次后任务可启动 → **restart**（:491-496）、任务被注销 →
  **stop**（:367-380）；
* **反例**：一份全信号健康的快照判定必须是 no-action（判定不是恒真的），并且
  「同一份快照改 parent_pid」不得改变判定（该信号不是看门狗输入）。

另有两条边界用例：LIMIT-LOSS（:444-460）为 **stop**；本身份读不到决定性信号
（任务状态／WFP 强校验）必须记 **undecidable**，绝不折成 no-action。

隔离：夹具全在内存字典里；不连 127.0.0.1 的任何端口、不跑 netstat/CIM、不读
RunRoot、不写仓库、不重启任何服务、不发模型请求。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_watchdog_forensics -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[1] / "tools" / "um-share"
             / "readonly")

EXPECTED_EXE = r"C:\ProgramData\UMShareRun\um-share-python.exe"


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_" + module_name,
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


forensics = _load("watchdog_forensics")

# 2026-09-27 现场只读读数（本线实测）：8096=51352、8100=37020、8110=54300，
# 8110 的父 PID = 51352（= 8096 的监听 PID）。
PID_8096 = 51352
PID_8100 = 37020
PID_8110 = 54300
STARTED_8100 = 1790475720.8


# --------------------------------------------------------------- 夹具构造
def _limits_ok():
    return {
        "backend_port": {"readable": True, "value": 8110, "error": None},
        "wfp_strong": {"query_ok": True, "rc": 0,
                        "text": "[WFP] permit port override: 8110 "
                                "[WFP-STRONG] filter0=[action:OK layer:OK "
                                "appid:MATCH];filter1=[action:OK layer:OK "
                                "appid:MATCH];filter2=[action:OK layer:OK "
                                "appid:MATCH]; STRONG=OK [WFP-STRONG] RESULT=OK"},
        "firewall": {"query_ok": True, "found": True, "enabled": True,
                      "action": "Block", "program": EXPECTED_EXE,
                      "direction": "Outbound", "scope_ok": True,
                      "scope_reason": "scope-ok", "error": None},
    }


def _share_verified(pid=PID_8100, creation=STARTED_8100):
    return {
        "listener": {"query_ok": True, "pids": [pid], "error": None},
        "identity": {"readable": True, "exe": EXPECTED_EXE,
                      "owner": "um_share", "creation_epoch": creation,
                      "error": None},
    }


def _process(pid, name, parent_pid, start_epoch):
    return {"name": name, "parent_pid": parent_pid, "start_epoch": start_epoch}


def _signals(**overrides):
    signals = {
        "read_by_caller": {"wfp_strong": True, "backend_port": True,
                            "firewall": True, "share_identity": True,
                            "umshare_live_task": True,
                            "umshare_watchdog_task": True,
                            "watchdog_decision_log": True,
                            "broker_status": True},
        "limits": _limits_ok(),
        "share": _share_verified(),
        "seen": {"state": "seen", "misses": 0},
        "recovery": {"boot_id": "1339-boot", "attempts": 0},
        "tasks": {
            "umshare-live": {"readable": True, "registered": True,
                              "state": "Running"},
            "umshare-watchdog": {"readable": True, "registered": True,
                                   "state": "Running"},
        },
        "processes": {
            PID_8096: _process(PID_8096, "python.exe", 9412, 1790387.0),
            PID_8100: _process(PID_8100, "um-share-python.exe", 20608,
                                STARTED_8100),
            PID_8110: _process(PID_8110, "python.exe", PID_8096,
                                STARTED_8110),
        },
        "ports": {
            "8096": {"pids": [PID_8096], "reason": None},
            "8100": {"pids": [PID_8100], "reason": None},
            "8110": {"pids": [PID_8110], "reason": None},
        },
        "health_8096": {"status": 200,
                          "json": {"status": "ok", "service": "usagesrv",
                                     "port": 8096},
                          "error": None},
        "log_tail": ["check-ok limits=ok share=%d (bounded poll)" % PID_8100],
    }
    signals.update(overrides)
    return signals


# 2026-09-27 现场读数：8110 启动于 10:22:09、8100 启动于 10:22:00。
STARTED_8100 = 1790475720.8
STARTED_8110 = 1790475729.9


def _fixture_restart_8096_orphan_8110():
    """情形 1：8096 重启（PID 51352 → 60001），8110 存活但其父 PID 已成死进程。"""
    before = _signals()
    after = _signals(
        # 8096 重启后的现场：新 PID，旧 PID 不在进程表里。
        processes={
            60001: _process(60001, "python.exe", 9412, 1790490000.0),
            PID_8100: _process(PID_8100, "um-share-python.exe", 20608,
                                STARTED_8100),
            # 8110 未随 8096 重启：PID 与启动时间不变，父 PID 仍写着旧 8096 PID。
            PID_8110: _process(PID_8110, "python.exe", PID_8096,
                                STARTED_8110),
        },
        ports={
            "8096": {"pids": [60001], "reason": None},
            "8100": {"pids": [PID_8100], "reason": None},
            "8110": {"pids": [PID_8110], "reason": None},
        },
    )
    return {"scenario": "restart-8096-orphan-8110",
            "snapshots": [{"label": "before", "signals": before},
                           {"label": "after", "signals": after}]}


def _fixture_8100_briefly_unreachable(misses=0, **after_overrides):
    """情形 2：8100 短暂不可达（查询成功、无人监听）。"""
    after_share = {"listener": {"query_ok": True, "pids": [], "error": None},
                    "identity": {"readable": True, "exe": EXPECTED_EXE,
                                   "owner": "um_share",
                                   "creation_epoch": STARTED_8100,
                                   "error": None}}
    after_seen = {"state": "seen", "misses": misses}
    after = _signals(share=after_share, seen=after_seen, **after_overrides)
    before = _signals()
    return {"scenario": "8100-briefly-unreachable",
            "snapshots": [{"label": "before", "signals": before},
                           {"label": "after", "signals": after}]}


# ------------------------------------------------------------------- 用例
class Orphan8110AfterRestart8096Test(unittest.TestCase):
    """情形 1：重启 8096 后 8110 的父 PID 变孤儿 → 判定必须落在 no-action。"""

    def test_verdict_is_no_action(self):
        report = forensics.judge_fixture(
            _fixture_restart_8096_orphan_8110())
        self.assertEqual(report["verdict"], "no")
        self.assertEqual(report["exit_code"], 0)
        for entry in report["snapshots"]:
            self.assertEqual(entry["result"]["verdict"], "no-action",
                              entry["label"])

    def test_parent_pid_rule_is_reported_as_non_input(self):
        report = forensics.judge_fixture(
            _fixture_restart_8096_orphan_8110())
        rules = report["window"]["rules"]
        parent_rule = [rule for rule in rules
                        if rule["id"] == "identity.parent-pid-not-read"]
        self.assertEqual(len(parent_rule), 1)
        detail = parent_rule[0]["detail"]
        # The orphan fact must be *named* in the report, not silently dropped.
        self.assertIn(str(PID_8110), detail)
        self.assertIn(str(PID_8096), detail)
        self.assertIn("不读", detail)
        self.assertEqual(parent_rule[0]["action"], "no-action")

    def test_8096_pid_change_is_reported_as_non_input(self):
        report = forensics.judge_fixture(
            _fixture_restart_8096_orphan_8110())
        ids = [rule["id"] for rule in report["window"]["rules"]]
        self.assertIn("window.8096-restart-not-an-input", ids)
        self.assertIn("identity.pid-change-not-a-trigger", ids)

    def test_parent_pid_is_not_a_judgement_input(self):
        """把 parent_pid 改掉（其它信号完全相同）判定不得改变。"""
        base = _signals()
        changed = _signals(processes={
            PID_8096: _process(PID_8096, "python.exe", 9412, 1790387.0),
            PID_8100: _process(PID_8100, "um-share-python.exe", 1,
                                STARTED_8100),
            PID_8110: _process(PID_8110, "python.exe", 999999,
                                STARTED_8110),
        })
        self.assertEqual(forensics.judge_snapshot(base)["verdict"],
                          forensics.judge_snapshot(changed)["verdict"])

    def test_8096_pid_change_alone_keeps_verified(self):
        base = _signals()
        changed = _signals(processes={
            PID_8096: _process(PID_8096, "python.exe", 9412, 1790387.0),
            PID_8100: _process(PID_8100, "um-share-python.exe", 20608,
                                STARTED_8100),
            PID_8110: _process(PID_8110, "python.exe", PID_8096,
                                STARTED_8110),
        }, ports={"8096": {"pids": [99999], "reason": None},
                   "8100": {"pids": [PID_8100], "reason": None},
                   "8110": {"pids": [PID_8110], "reason": None}})
        result = forensics.judge_snapshot(changed)
        self.assertEqual(result["verdict"], "no-action")
        self.assertEqual(result["share_state"], "verified")


class BriefUnreachable8100Test(unittest.TestCase):
    """情形 2：8100 短暂不可达 → 宽限内 no-action；边界另两态见下。"""

    def test_single_miss_stays_in_grace(self):
        report = forensics.judge_fixture(_fixture_8100_briefly_unreachable())
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "no-action")
        self.assertEqual(after["kind"], "grace")
        self.assertEqual(after["misses"], 1)
        self.assertEqual(report["verdict"], "no")
        ids = [rule["id"] for rule in after["rules"]]
        self.assertIn("share-absent.grace", ids)

    def test_two_misses_still_in_grace(self):
        report = forensics.judge_fixture(
            _fixture_8100_briefly_unreachable(misses=1))
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "no-action")
        self.assertEqual(after["misses"], 2)

    def test_third_miss_with_registered_task_restarts(self):
        report = forensics.judge_fixture(
            _fixture_8100_briefly_unreachable(
                misses=2, recovery_result={"listener_verified": True,
                                             "pid": 40001}))
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "restart")
        self.assertEqual(after["kind"], "recovery")
        self.assertEqual(report["verdict"], "yes")
        self.assertEqual(report["exit_code"], 4)

    def test_third_miss_with_unregistered_task_stops(self):
        report = forensics.judge_fixture(
            _fixture_8100_briefly_unreachable(
                misses=2, tasks={
                    "umshare-live": {"readable": True, "registered": False,
                                       "state": None}}))
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "stop")
        self.assertEqual(after["kind"], "recovery-failed")
        self.assertEqual(report["verdict"], "yes")
        self.assertEqual(report["exit_code"], 3)

    def test_third_miss_with_unreadable_task_is_undecidable(self):
        """恢复路径依赖 umshare-live 任务状态：读不到即不可判定，不折成 stop。"""
        report = forensics.judge_fixture(
            _fixture_8100_briefly_unreachable(
                misses=2, tasks={"umshare-live": {"readable": False,
                                                    "registered": None,
                                                    "state": None}}))
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "undecidable")
        self.assertEqual(report["verdict"], "depends")
        ids = [(rule["id"], rule["action"]) for rule in after["rules"]]
        self.assertIn(("recovery.task-unreadable", "undecidable"), ids)
        # 规则表与判定函数不得自相矛盾：不可判定结论下不得列出一条 stop 规则
        # （2026-09-28 复核 finding 5：原规则 recovery.task-not-startable 表里
        # action=stop、代码返回 undecidable）。
        self.assertNotIn("recovery.task-not-startable", forensics._RULES_BY_ID)
        self.assertEqual(
            forensics._RULES_BY_ID["recovery.task-unreadable"]["action"],
            "undecidable")
        self.assertEqual(
            forensics._RULES_BY_ID["recovery.task-not-registered"]["action"],
            "stop")

    def test_managed_stop_keeps_task_and_resets_misses(self):
        report = forensics.judge_fixture(
            _fixture_8100_briefly_unreachable(
                misses=2,
                broker_status={"readable": True, "lifecycle_state": "stopped",
                                "at": STARTED_8100 + 60,
                                "seen_creation_epoch": STARTED_8100}))
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "no-action")
        self.assertEqual(after["kind"], "expected-stop")


class LimitLossStopsTest(unittest.TestCase):
    """会：limits 丢失／检测失败 → stop（:444-460）。"""

    def test_firewall_query_error_stops(self):
        broken = _limits_ok()
        broken["firewall"] = {"query_ok": False, "found": None,
                               "enabled": None, "action": None,
                               "program": None, "direction": None,
                               "scope_ok": None, "scope_reason": None,
                               "error": "调用已取消"}
        report = forensics.judge_fixture(
            {"scenario": "firewall-transient-failure",
             "snapshots": [{"label": "before", "signals": _signals()},
                             {"label": "after",
                              "signals": _signals(limits=broken)}]})
        after = report["snapshots"][1]["result"]
        self.assertEqual(after["verdict"], "stop")
        self.assertEqual(after["kind"], "limit-loss")
        self.assertEqual(report["verdict"], "yes")
        ids = [rule["id"] for rule in after["rules"]]
        self.assertIn("limits.firewall-query-error", ids)

    def test_result_ok_without_strong_token_stops(self):
        """看门狗找的是 ``STRONG=OK`` 这个 token，不是 ``RESULT=OK``（:169）。"""
        broken = _limits_ok()
        broken["wfp_strong"] = {"query_ok": True, "rc": 0,
                                  "text": "[WFP-STRONG] RESULT=OK"}
        result = forensics.judge_snapshot(_signals(limits=broken))
        self.assertEqual(result["verdict"], "stop")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("limits.wfp-strong-not-ok", ids)

    def test_inbound_direction_stops(self):
        """规则被收窄到 Inbound 也要判停：源码 :125 检查 ``$rule.Direction``。"""
        broken = _limits_ok()
        broken["firewall"] = dict(broken["firewall"], direction="Inbound")
        result = forensics.judge_snapshot(_signals(limits=broken))
        self.assertEqual(result["verdict"], "stop")
        self.assertEqual(result["kind"], "limit-loss")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("limits.firewall-program-or-scope-narrowed", ids)

    def test_lowercase_outbound_direction_and_block_action_pass(self):
        """PowerShell ``-ne`` 大小写不敏感：``outbound``/``block`` 与 ``Outbound``/``Block`` 等价。

        复核者 2026-09-28 复现：原实现用区分大小写的 ``!=``，会把小写值误报成
        「会」（多报一次停链）。本用例钉住大小写不敏感语义。
        """
        limits = _limits_ok()
        limits["firewall"] = dict(limits["firewall"],
                                  direction="outbound", action="block")
        result = forensics.judge_snapshot(_signals(limits=limits))
        self.assertEqual(result["verdict"], "no-action")
        self.assertEqual(result["kind"], "ok")

    def test_strong_token_is_case_insensitive(self):
        """PowerShell ``-notlike`` 大小写不敏感：``strong=ok`` 必须判为通过。"""
        limits = _limits_ok()
        limits["wfp_strong"] = {"query_ok": True, "rc": 0, "text": "strong=ok"}
        result = forensics.judge_snapshot(_signals(limits=limits))
        self.assertEqual(result["verdict"], "no-action")
        self.assertEqual(result["kind"], "ok")

    def test_empty_strong_output_is_detection_failure(self):
        """``strong`` 输出为空先走 :167 的 ``$output.Count -eq 0``，不是 strong-not-ok。"""
        broken = _limits_ok()
        broken["wfp_strong"] = {"query_ok": True, "rc": 0, "text": "   "}
        result = forensics.judge_snapshot(_signals(limits=broken))
        self.assertEqual(result["verdict"], "stop")
        self.assertEqual(result["kind"], "limit-loss")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("limits.wfp-detection-failure", ids)
        self.assertNotIn("limits.wfp-strong-not-ok", ids)

    def test_missing_backend_port_file_stops(self):
        broken = _limits_ok()
        broken["backend_port"] = {"readable": False, "value": None,
                                   "error": "backend-port-unavailable(no backend-port.txt)"}
        result = forensics.judge_snapshot(_signals(limits=broken))
        self.assertEqual(result["verdict"], "stop")
        self.assertEqual(result["kind"], "limit-loss")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("limits.backend-port-unavailable", ids)

    def test_absent_share_still_cleans_up_task(self):
        """LIMIT-LOSS + 查询成功确认无人监听：无可杀对象也要注销任务（:446-447）。"""
        broken = _limits_ok()
        broken["wfp_strong"] = {"query_ok": False, "rc": 1, "text": ""}
        share = {"listener": {"query_ok": True, "pids": [], "error": None},
                  "identity": {"readable": True, "exe": None, "owner": None,
                                 "creation_epoch": None, "error": None}}
        result = forensics.judge_snapshot(_signals(limits=broken, share=share))
        self.assertEqual(result["verdict"], "stop")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("limit-loss.share-absent-nothing-to-kill", ids)


class AnomalyPrecedenceTest(unittest.TestCase):
    """ANOMALY（:417-424）先于 LIMIT-LOSS（:444）判定。"""

    def test_anomaly_wins_over_limit_loss(self):
        broken = _limits_ok()
        broken["wfp_strong"] = {"query_ok": False, "rc": 1, "text": ""}
        share = {"listener": {"query_ok": True, "pids": [PID_8100],
                               "error": None},
                  "identity": {"readable": True, "exe": "python.exe",
                                 "owner": "dacint",
                                 "creation_epoch": STARTED_8100,
                                 "error": None}}
        result = forensics.judge_snapshot(_signals(limits=broken, share=share))
        self.assertEqual(result["verdict"], "stop")
        self.assertEqual(result["kind"], "anomaly")

    def test_procquery_error_is_anomaly(self):
        share = {"listener": {"query_ok": True, "pids": [PID_8100],
                               "error": None},
                  "identity": {"readable": False, "exe": None, "owner": None,
                                 "creation_epoch": None,
                                 "error": "内存不足"}}
        result = forensics.judge_snapshot(_signals(share=share))
        self.assertEqual(result["verdict"], "stop")
        self.assertEqual(result["kind"], "anomaly")
        ids = [rule["id"] for rule in result["rules"]]
        self.assertIn("anomaly.procquery-error", ids)
        self.assertIn("anomaly.close-public-and-cleanup", ids)


class UndecidableDisciplineTest(unittest.TestCase):
    """读不到决定性信号 → undecidable（绝不是 no-action）。"""

    def test_unreadable_watchdog_task_makes_window_depends(self):
        before = _signals()
        after = _signals()
        after["read_by_caller"] = {"umshare_live_task": False}
        report = forensics.judge_fixture(
            {"scenario": "task-state-unreadable",
             "snapshots": [{"label": "before", "signals": before},
                             {"label": "after", "signals": after}]})
        self.assertEqual(report["verdict"], "depends")
        self.assertEqual(report["exit_code"], 2)
        ids = [rule["id"] for rule in report["window"]["rules"]]
        self.assertIn("read.decisive-signal-unreadable", ids)

    def test_unreadable_wfp_half_is_undecidable(self):
        signals = _signals(read_by_caller={"wfp_strong": False})
        signals["limits"].pop("wfp_strong")
        result = forensics.judge_snapshot(signals)
        self.assertEqual(result["verdict"], "undecidable")
        self.assertEqual(forensics.EXIT_BY_VERDICT["undecidable"], 2)

    def test_empty_snapshot_is_undecidable(self):
        result = forensics.judge_snapshot({})
        self.assertEqual(result["verdict"], "undecidable")

    def test_undecidable_verdict_never_carries_a_stop_rule(self):
        """不可判定结论下不得挂 stop 规则（规则表与判定函数不得自相矛盾）。"""
        fixtures = [
            {"scenario": "empty", "signals": {}},
            {"scenario": "no-task-signal", "signals": _signals(tasks={})},
            {"scenario": "unreadable-task",
             "signals": _signals(tasks={"umshare-live": {"readable": False,
                                                            "registered": None,
                                                            "state": None}})},
            {"scenario": "unreadable-limits",
             "signals": _signals(read_by_caller={"wfp_strong": False},
                                   limits={"backend_port": {"readable": False,
                                                             "value": None,
                                                             "error": "denied"}})},
        ]
        seen = set()
        for fixture in fixtures:
            report = forensics.judge_fixture(fixture)
            result = report["snapshots"][0]["result"]
            seen.add(result["verdict"])
            if result["verdict"] != "undecidable":
                continue
            stop_rules = [rule["id"] for rule in result["rules"]
                          if rule["action"] == "stop"]
            self.assertEqual(stop_rules, [],
                              "undecidable 结论下出现了 stop 规则：%s"
                              % (stop_rules,))
        self.assertIn("undecidable", seen)


class NotAlwaysStopTest(unittest.TestCase):
    """反例：判定不是恒真（健康现场不得被判成 stop／restart）。"""

    def test_healthy_snapshot_is_no_action(self):
        result = forensics.judge_snapshot(_signals())
        self.assertEqual(result["verdict"], "no-action")
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(result["share_pid"], PID_8100)

    def test_verdict_is_not_a_constant(self):
        """同一工具在健康／孤儿／丢失三份夹具上给出不同结论（不是恒真也不是恒假）。"""
        healthy = forensics.judge_snapshot(_signals())["verdict"]
        orphan = forensics.judge_fixture(
            _fixture_restart_8096_orphan_8110())["verdict"]
        broken = _limits_ok()
        broken["wfp_strong"] = {"query_ok": False, "rc": 1, "text": ""}
        limit_loss = forensics.judge_snapshot(_signals(limits=broken))["verdict"]
        self.assertEqual(healthy, "no-action")
        self.assertEqual(orphan, "no")
        self.assertEqual(limit_loss, "stop")
        self.assertGreater(len({healthy, orphan, limit_loss}), 1)

    def test_all_reported_rule_ids_exist_in_the_table(self):
        """任何被引用的规则 id 必须在规则表里（防止 _hit 抛 KeyError）。"""
        fixtures = [
            {"scenario": "healthy", "signals": _signals()},
            _fixture_restart_8096_orphan_8110(),
            _fixture_8100_briefly_unreachable(),
            _fixture_8100_briefly_unreachable(
                misses=2, recovery_result={"listener_verified": True,
                                             "pid": 40001}),
            _fixture_8100_briefly_unreachable(
                misses=2, tasks={"umshare-live": {"readable": True,
                                                   "registered": False,
                                                   "state": None}}),
        ]
        seen = set()
        for fixture in fixtures:
            report = forensics.judge_fixture(fixture)
            for entry in report["snapshots"]:
                for rule in entry["result"]["rules"]:
                    seen.add(rule["id"])
            window = report.get("window")
            for rule in (window or {}).get("rules", []):
                seen.add(rule["id"])
        self.assertTrue(seen)
        unknown = sorted(rule_id for rule_id in seen
                          if rule_id not in forensics._RULES_BY_ID)
        self.assertEqual(unknown, [])


class CliTest(unittest.TestCase):
    """CLI 只读边界：--print-rules 与 --from-fixture（夹具在临时目录）。"""

    def test_print_rules_exit_zero(self):
        self.assertEqual(forensics.main(["--print-rules"]), 0)

    def test_from_fixture_orphan_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orphan.json"
            path.write_text(json.dumps(_fixture_restart_8096_orphan_8110()),
                            encoding="utf-8")
            self.assertEqual(
                forensics.main(["--from-fixture", str(path)]), 0)

    def test_from_fixture_restart_exit_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recovery.json"
            path.write_text(json.dumps(_fixture_8100_briefly_unreachable(
                misses=2, recovery_result={"listener_verified": True,
                                             "pid": 40001})), encoding="utf-8")
            self.assertEqual(forensics.main(["--from-fixture", str(path)]), 4)

    def test_missing_fixture_is_exit_two(self):
        self.assertEqual(
            forensics.main(["--from-fixture", "no-such-fixture.json"]), 2)


if __name__ == "__main__":
    unittest.main()
