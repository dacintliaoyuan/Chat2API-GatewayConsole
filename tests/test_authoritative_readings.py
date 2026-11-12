# -*- coding: utf-8 -*-
"""提升身份权威读数（`authoritative_readings.ps1` + `--authoritative`）的夹具测试。

覆盖（执行轮 10，2026-09-28；维护者要求「窗口前必须有有权限的权威读数」）：

* 探针 JSON 的**只读加载**：UTF-8（无 BOM / 带 BOM）与 UTF-16（PowerShell 5.1 的
  `>` 会写 UTF-16LE）都能读回；读不到 / 解不开一律 `error`，**绝不猜**；sha256 与
  文件字节一致；
* **合并**：权威读数并进快照后 ``window_signals.watchdog_task`` **不再是
  undecidable**；记录来源（工具名 / 路径 / sha256 / 提升身份）与采集时刻；形状不符的
  条目被**拒绝**并如实记录；``watchdog_task`` 用本工具自己的规则从原始信封**重算**，
  不一致时按 `undecidable` 记；**信封缺失 / 不可解析同样按 `undecidable` 记**
  （``authoritative_envelope_missing`` / ``authoritative_envelope_unparsable``），
  绝不只凭自报字段给 PASS；
* **不提供权威读数时行为不变**：``collect_snapshot`` 的 ``window_signals`` 为八类
  信号（契约 §8.3 新增第 ⑧ 类 ``share_entry_faults``；broker 不可得时如实记
  ``undecidable``、``counts_by_phase`` 保持 null，绝不填 0）、没有
  ``authoritative_readings`` 节、``watchdog_task`` 仍是 ``undecidable``，
  ``--compare`` 退出码仍是 2；
* **文案对齐**：``undecidable`` 打印「先补取证据」，``fail`` 才打印「回退」；
* **探针分支**：用临时桩看门狗脚本（两函数都在 / 缺 ``Get-FirewallState`` / 缺
  ``Get-LimitsState`` / ``strong-not-ok``）跑真探针，逐项验三态——缺函数一律
  ``undecidable``，绝不折成 ``absent``；
* **探针静态只读**：`authoritative_readings.ps1` 不含写动作（除显式 `-Out`）；
* **运行副本核对（执行轮 11，2026-09-28；`-RunningScriptPath`，加性、默认关闭）**：
  运行副本与仓库脚本**逐字节**一致 → ``ok``/``matches_repo=true``；不一致 →
  ``mismatch``/``matches_repo=false``；**缺失、读不到、同一路径自比** → ``undecidable``/
  ``matches_repo=null``（绝不折成一致或不一致）；不给该参数时输出**不含该节**且顶层键集
  与加此能力之前逐字相同。

隔离：夹具全在内存 / `tempfile` 临时目录里；不连 127.0.0.1 的任何端口、不跑
netstat/CIM、不读生产 `backend.pid`、不写仓库 `data/`、不重启任何服务、不生成密钥、
不发模型请求。``collect_snapshot`` 用注入替身调用（只读路径的替身，不触生产）。
``ProbeStubScriptTests`` 每例跑一次 `powershell`，但看门狗脚本、`RunRoot`、
`backend-port.txt`、`raw.txt`、`backend.pid` 全指向临时目录，任务名用不存在的名字
（`Get-ScheduledTask` / `schtasks /query` 只是只读查询，读不到即 undecidable）。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_authoritative_readings -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools" / "um-share"

FOUR_KEYS = ["key_chars", "key_file", "reason", "wired"]
SEVEN_KEYS = ["file_ready", "file_reason", "instance_loaded", "key_chars",
              "key_file", "request_attribution_detail",
              "request_attribution_observed"]

PROBE_PATH = TOOLS_DIR / "authoritative_readings.ps1"

# 探针脚本里**不允许**出现的写动作 / 真实动作（静态只读契约）。
FORBIDDEN_PROBE_TOKENS = (
    "Register-ScheduledTask", "Unregister-ScheduledTask", "Start-ScheduledTask",
    "Stop-ScheduledTask", "Disable-ScheduledTask", "Enable-ScheduledTask",
    "Stop-Process", "Start-Process", "Restart-Service", "Stop-Service",
    "Start-Service", "Restart-Computer", "New-Item", "Remove-Item",
    "Set-Content", "Add-Content", "Out-File", "icacls", "taskkill",
    "New-NetFirewallRule", "Remove-NetFirewallRule", "Set-NetFirewallRule",
    "netsh",
)


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_" + module_name,
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("share_chain_snapshot")


# --------------------------------------------------------------- 夹具构造
def _entry(status, code, error=None):
    return {"status": status, "code": code, "error": error}


def _listener(pids, reason=None):
    return {"pids": pids, "reason": reason}


def _signal(name, status, **extra):
    """窗口信号夹具：与 ``share_chain_snapshot`` 的信号同形（必带 ``signal``）。"""
    base = {"signal": name, "status": status, "reason": None, "detail": ""}
    base.update(extra)
    return base


def _watchdog_undecidable(reason="not_visible_to_current_identity"):
    """非提升身份下的 watchdog 信号：读不到 = undecidable（绝不是「未注册」）。"""
    return _signal("watchdog_task", "undecidable", task_name="umshare-watchdog",
                   elevated=False, identity="DAICNT\\dacint", reason=reason,
                   detail="非提升身份查不到 SYSTEM 任务：按『读不到』记 undecidable")


def _watchdog_ok(state=4, state_text="Running"):
    return _signal("watchdog_task", "ok", task_name="umshare-watchdog",
                   state=state, state_text=state_text, elevated=True,
                   identity="DAICNT\\dacint",
                   detail="任务已注册：State=%s（%s）" % (state_text, state),
                   query=_query_envelope(state=state, state_text=state_text))


def _share_entry_faults(at=1790475900.5, phase="upstream",
                        code="um_unavailable"):
    """第 ⑧ 类窗口信号夹具：broker ``host-evidence{faults}`` 的可判定故障证据。

    与 ``share_chain_snapshot.share_entry_faults_signal`` 的 ``ok`` 产物同形
    （契约 §8.6）：末条故障 + 按 phase 计数 + 文件事实。两侧给同一份即
    「窗口期间未出现故障回退」。
    """
    counts = {"auth": 0, "config": 0, "body": 0, "limit": 0,
              "upstream": 3, "stream": 0}
    last = {"at": at, "phase": phase, "status": 502, "code": code,
            "upstream_status": 503, "exception_type": None,
            "duration_ms": 120.5, "request_id": None, "stream_started": False}
    file_facts = {"present": True, "size": 4096, "mtime": 1790475900.0}
    return _signal("share_entry_faults", "ok", last_fault=last,
                   counts_by_phase=counts, file=file_facts,
                   verdict={"last_fault": last, "counts_by_phase": counts,
                             "file": file_facts},
                   checked_at=1790475901, reason="ok",
                   detail="故障文件可读：末条故障 phase='%s' code='%s'"
                          % (phase, code))


def _v2_before():
    """非提升身份采集的「窗口前」快照（watchdog = undecidable，其余七类 ok）。"""
    return {
        "tool": "share_chain_snapshot",
        "schema_version": 2,
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
        "share_status": {"http_status": 200, "error": None, "keys": list(FOUR_KEYS),
                         "shape": "four_key", "funnel_on": True,
                         "file_ready": None, "instance_loaded_value": None},
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
        "watchdog_task_query": {"query_ok": True, "elevated": False,
                                 "identity": "DAICNT\\dacint", "found": False,
                                 "state": None, "state_text": None,
                                 "error": "CimJobException|"
                                          "CmdletizationQuery_NotFound_TaskName",
                                 "error_kind": "not_found"},
        "window_signals": {
            "read_surface_keys": _signal("read_surface_keys", "ok",
                                          keys=list(FOUR_KEYS),
                                          shape="four_key"),
            "gateway_8100_image_start": _signal(
                "gateway_8100_image_start", "ok", pid=37020,
                image="um-share-python.exe", image_expected=True,
                started_at=1790475720.8),
            "backend_8110_pid_parent": _signal(
                "backend_8110_pid_parent", "ok", backend_pid=54300,
                parent_pid=51352, console_pid=51352,
                parent_is_console_pid=True),
            "watchdog_task": _watchdog_undecidable(),
            "share_entry_gates": _signal("share_entry_gates", "ok", entries={
                "root": {"path": "/", "status": 404,
                         "code": "route_not_shared", "matches": True},
                "health": {"path": "/health", "status": 404,
                           "code": "route_not_shared", "matches": True},
                "whitelist_models": {"path": "/v1/models", "status": 401,
                                     "code": "invalid_api_key",
                                     "matches": True}}),
            "share_ledger_tail": _signal("share_ledger_tail", "ok", rows=1680,
                                          last_index=1679,
                                          last_attribution="absent",
                                          last_key_id=None),
            "funnel": _signal("funnel", "ok", funnel_on=True),
            "share_entry_faults": _share_entry_faults(),
        },
        "not_covered": [
            "8100 启动任务日志（启动行「请求归因：已启用」）需提权读取，本快照不含",
            tool.NOT_COVERED_LIMITS,
            tool.NOT_COVERED_WATCHDOG,
            "密钥内容与任何分享 Key 明文：本快照只含路径、原因码与 strip() 后长度",
        ],
    }


def _v2_after():
    """「窗口后」快照：8096 PID 已变、读面 7 键；watchdog 仍 undecidable（非提升）。"""
    snap = _v2_before()
    snap["listeners"]["8096"] = _listener([60000])
    snap["share_status"] = {"http_status": 200, "error": None,
                            "keys": list(SEVEN_KEYS), "shape": "seven_key",
                            "funnel_on": True, "file_ready": True,
                            "instance_loaded_value": "yes"}
    snap["gateway_attribution"] = {
        "source": "in_process_share_attribution_status", "file_ready": False,
        "file_reason": "missing", "key_chars": None, "key_file": None,
        "instance_loaded_value": "unknown",
        "instance_loaded_basis": "inference",
        "instance_loaded_reason": "key_file_not_ready",
        "request_attribution_observed": False, "error": None}
    signals = snap["window_signals"]
    signals["read_surface_keys"] = _signal("read_surface_keys", "ok",
                                           keys=list(SEVEN_KEYS),
                                           shape="seven_key")
    signals["backend_8110_pid_parent"] = _signal(
        "backend_8110_pid_parent", "ok", backend_pid=54300, parent_pid=51352,
        console_pid=60000, parent_is_console_pid=False)
    return copy.deepcopy(snap)


def _wfp_ok(port=8110,
             app_exe=r"C:\ProgramData\UMShareRun\um-share-python.exe"):
    return _signal(
        "wfp_limits", "ok", app_exe=app_exe, backend_port=port,
        backend_port_reason="",
        helper=r"C:\ProgramData\UMShareRun\wfp_helper.ps1", helper_present=True,
        limits_source="um-share-deploy/watchdog-umshare.ps1:"
                       "Get-LimitsState/Get-FirewallState",
        wfp_ok=True, wfp_reason="ok", wfp_rc=0,
        wfp_detail="[WFP-STRONG] filter0=[action:OK layer:OK appid:MATCH]; "
                   "STRONG=OK",
        firewall_ok=True, firewall_reason="ok",
        firewall_rule="um-share-outbound-block",
        reason="ok", detail="WFP strong=OK；出站阻断规则 ok")


def _raw_txt(mtime=1790475729.112053, size=4096):
    return _signal("raw_txt", "ok",
                    path=r"C:\ProgramData\UMShareRun\run\live\raw.txt",
                    exists=True, size=size, mtime=mtime,
                    mtime_utc_iso="2026-09-27T02:22:09.1120470Z",
                    mtime_reason="ok", reason="ok",
                    detail="raw.txt 存在：size=%d" % size)


def _backend_pid_file(content="54300", size=5, mtime=1790475729.112053):
    return _signal(
        "backend_pid_file", "ok",
        path=(r"C:\Users\dacint\AppData\Local\SelfUseAI\ai-gateway"
               r"\share-backend\backend.pid"),
        path_source="LOCALAPPDATA_default",
        backend_dir=(r"C:\Users\dacint\AppData\Local\SelfUseAI\ai-gateway"
                      r"\share-backend"),
        content=content, bytes_hex=content.encode("ascii").hex(), size=size,
        pid=int(content) if content.isdigit() else None, mtime=mtime,
        mtime_utc_iso="2026-09-27T02:22:09.1120470Z", mtime_reason="ok",
        reason="ok", detail="backend.pid 字节=%d 内容=%s" % (size, content))


def _authoritative_readings(watchdog="ok", **signal_overrides):
    """探针 JSON 夹具：``window_signals`` 与快照同名同形状。"""
    signals = {
        "watchdog_task": (_watchdog_ok() if watchdog == "ok"
                           else _signal("watchdog_task", "absent",
                                        task_name="umshare-watchdog",
                                        state=None, state_text=None,
                                        reason="not_registered_elevated",
                                        elevated=True,
                                        identity="DAICNT\\dacint",
                                        detail="提升身份下的权威读数：任务未注册",
                                        query=_query_envelope(
                                            found=False, state=None,
                                            state_text=None,
                                            error="CimJobException|"
                                                  "CmdletizationQuery_"
                                                  "NotFound_TaskName"))),
        "wfp_limits": _wfp_ok(),
        "raw_txt": _raw_txt(),
        "backend_pid_file": _backend_pid_file(),
    }
    signals.update(signal_overrides)
    return {
        "tool": "authoritative_readings",
        "schema_version": 1,
        "read_only": True,
        "write_actions": [],
        "elevated": True,
        "identity": "DAICNT\\dacint",
        "probe_pid": 4242,
        "captured_at": 1790562898.0351868,
        "captured_at_iso": "2026-09-28T10:34:58+08:00",
        "run_root": "C:\\ProgramData\\UMShareRun",
        "watchdog_script": "D:\\...\\um-share-deploy\\watchdog-umshare.ps1",
        "deploy_dir": "D:\\...\\um-share-deploy",
        "window_signals": signals,
        "not_covered": ["raw.txt 内容不读（只记路径、size 与 mtime）"],
    }


def _query_envelope(elevated=True, found=True, state=4,
                     state_text="Running", error=None):
    return {"elevated": elevated, "name": "DAICNT\\dacint", "found": found,
            "state": state, "stateText": state_text, "error": error}


def _merged(readings, snapshot=None):
    merged, _report = tool.merge_authoritative_readings(
        snapshot if snapshot is not None else _v2_before(), readings,
        source={"path": "authoritative-before.json", "sha256": "0" * 64})
    return merged


def _item(result, item_id):
    for item in result["items"]:
        if item["id"] == item_id:
            return item
    raise AssertionError("no item %r in %r"
                        % (item_id, [i["id"] for i in result["items"]]))


def _verdicts(result):
    return {item["id"]: item["verdict"] for item in result["items"]}


def _temp_json(payload):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
    json.dump(payload, handle, ensure_ascii=False)
    handle.close()
    return Path(handle.name)


# ------------------------------------------------------- 探针 JSON 只读加载
class LoadAuthoritativeFileTests(unittest.TestCase):
    def test_utf8_without_bom_loads(self):
        path = _temp_json(_authoritative_readings())
        try:
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertIsNone(loaded["error"])
        self.assertEqual("authoritative_readings", loaded["readings"]["tool"])
        self.assertEqual(64, len(loaded["sha256"]))

    def test_utf8_with_bom_loads(self):
        path = _temp_json(_authoritative_readings())
        try:
            raw = path.read_bytes()
            path.write_bytes(b"\xef\xbb\xbf" + raw)
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertIsNone(loaded["error"])

    def test_utf16_loads(self):
        """PowerShell 5.1 的 `>` 写 UTF-16LE：读得回来（手工重定向的常见形态）。"""
        path = Path(tempfile.mkdtemp()) / "auth16.json"
        path.write_bytes(json.dumps(_authoritative_readings(),
                                     ensure_ascii=False).encode("utf-16"))
        try:
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertIsNone(loaded["error"])
        self.assertEqual("utf-16", loaded["encoding"])

    def test_legacy_code_page_loads_and_records_the_encoding(self):
        """`cmd` 的 `>` 写控制台代码页（本机 GBK）：读得回来，并记录实际编码。"""
        path = Path(tempfile.mkdtemp()) / "auth-gbk.json"
        path.write_bytes(json.dumps(_authoritative_readings(),
                                     ensure_ascii=False).encode("gbk"))
        try:
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertIsNone(loaded["error"])
        self.assertEqual("mbcs", loaded["encoding"])
        self.assertEqual("ok", loaded["readings"]["window_signals"]
                          ["watchdog_task"]["status"])

    def test_sha256_matches_the_file_bytes(self):
        import hashlib
        path = _temp_json(_authoritative_readings())
        try:
            raw = path.read_bytes()
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), loaded["sha256"])

    def test_missing_file_is_an_error(self):
        loaded = tool.load_authoritative_file("no-such-authoritative.json")
        self.assertEqual("authoritative_unreadable:FileNotFoundError",
                          loaded["error"])
        self.assertIsNone(loaded["readings"])

    def test_invalid_json_is_an_error_not_a_guess(self):
        path = _temp_json({})
        try:
            path.write_text("not json", encoding="utf-8")
            loaded = tool.load_authoritative_file(str(path))
        finally:
            path.unlink()
        self.assertEqual("authoritative_unparsable:not_json", loaded["error"])
        self.assertIsNone(loaded["readings"])


# --------------------------------------------------------------- 合并（权威）
class MergeAuthoritativeTests(unittest.TestCase):
    def test_watchdog_is_no_longer_undecidable_after_merge(self):
        """维护者要求：窗口前拿到权威读数后，watchdog 不再是 undecidable。"""
        before = _v2_before()
        self.assertEqual("undecidable",
                          before["window_signals"]["watchdog_task"]["status"])
        merged = _merged(_authoritative_readings())
        signal = merged["window_signals"]["watchdog_task"]
        self.assertEqual("ok", signal["status"])
        self.assertEqual(4, signal["state"])
        self.assertEqual("Running", signal["state_text"])
        self.assertTrue(signal["elevated"])
        self.assertEqual("DAICNT\\dacint", signal["identity"])
        self.assertEqual("ok", signal["reason"])

    def test_merge_records_source_and_capture_time(self):
        merged = _merged(_authoritative_readings())
        report = merged["authoritative_readings"]
        source = report["source"]
        self.assertEqual("authoritative_readings", source["tool"])
        self.assertEqual(1, source["schema_version"])
        self.assertEqual("authoritative-before.json", source["path"])
        self.assertEqual("0" * 64, source["sha256"])
        self.assertIn("encoding", source)
        self.assertTrue(source["read_only"])
        self.assertTrue(source["elevated"])
        self.assertEqual("DAICNT\\dacint", source["identity"])
        self.assertEqual(1790562898.0351868, source["captured_at"])
        self.assertEqual("2026-09-28T10:34:58+08:00",
                          source["captured_at_iso"])
        self.assertIn("merged_at", report)
        self.assertIn("merged_at_iso", report)
        row = [row for row in report["merged_signals"]
               if row["signal"] == "watchdog_task"][0]
        self.assertEqual("ok", row["status"])
        self.assertEqual("undecidable", row["previous_status"])
        self.assertEqual("2026-09-28T10:34:58+08:00",
                          row["source_captured_at_iso"])
        self.assertTrue(row["rederived"])

    def test_merge_adds_the_three_authoritative_signals(self):
        merged = _merged(_authoritative_readings())
        signals = merged["window_signals"]
        for name in tool.AUTHORITATIVE_SIGNAL_IDS:
            self.assertEqual("ok", signals[name]["status"], name)
        self.assertEqual(8110, signals["wfp_limits"]["backend_port"])
        self.assertEqual("54300", signals["backend_pid_file"]["content"])

    def test_merge_does_not_mutate_the_input_snapshot(self):
        before = _v2_before()
        snapshot = copy.deepcopy(before)
        _merged(_authoritative_readings(), snapshot=snapshot)
        self.assertEqual(before, snapshot)

    def test_merge_keeps_signals_not_covered_by_the_probe(self):
        merged = _merged(_authoritative_readings())
        self.assertEqual("ok", merged["window_signals"]["funnel"]["status"])
        self.assertEqual(
            [str(pid) for pid in (8000, 8096, 8100, 8110, 8320)],
            sorted(merged["listeners"]))

    def test_merge_rewrites_the_superseded_not_covered_lines(self):
        merged = _merged(_authoritative_readings())
        text = " ".join(merged["not_covered"])
        self.assertNotIn(tool.NOT_COVERED_LIMITS, text)
        self.assertNotIn(tool.NOT_COVERED_WATCHDOG, text)
        self.assertIn("权威读数已合并", text)

    def test_authoritative_absent_replaces_undecidable(self):
        merged = _merged(_authoritative_readings(watchdog="absent"))
        self.assertEqual("absent",
                          merged["window_signals"]["watchdog_task"]["status"])

    def test_rederivation_mismatch_is_undecidable_not_pass(self):
        """探针结论与本地重算不一致 → undecidable（绝不折成通过）。"""
        readings = _authoritative_readings()
        readings["window_signals"]["watchdog_task"]["query"] = _query_envelope(
            elevated=False, found=False, state=None, state_text=None,
            error="CimJobException|CmdletizationQuery_NotFound_TaskName")
        merged = _merged(readings)
        signal = merged["window_signals"]["watchdog_task"]
        self.assertEqual("undecidable", signal["status"])
        self.assertEqual("probe_rederivation_mismatch", signal["reason"])
        report = merged["authoritative_readings"]
        self.assertEqual("probe_rederivation_mismatch",
                          report["warnings"][0]["reason"])
        self.assertEqual("ok", report["warnings"][0]["probe_status"])
        self.assertEqual("undecidable",
                          report["warnings"][0]["rederived_status"])
        row = [row for row in report["merged_signals"]
               if row["signal"] == "watchdog_task"][0]
        self.assertEqual("undecidable", row["status"])

    def test_rederivation_uses_the_tools_own_rules(self):
        """带原始信封时按本工具自己的规则重算：found/state 以信封为准。"""
        readings = _authoritative_readings()
        readings["window_signals"]["watchdog_task"] = _signal(
            "watchdog_task", "ok", task_name="umshare-watchdog", state=3,
            state_text="Ready", elevated=True, identity="DAICNT\\dacint",
            reason="ok", detail="探针自称 ok",
            query=_query_envelope(state=3, state_text="Ready"))
        merged = _merged(readings)
        signal = merged["window_signals"]["watchdog_task"]
        self.assertEqual("ok", signal["status"])
        self.assertEqual(3, signal["state"])
        self.assertEqual("Ready", signal["state_text"])

    def test_watchdog_without_the_envelope_is_undecidable_not_pass(self):
        """复核者（执行轮 11）：删掉 query → 只凭自报字段不得当结论。"""
        readings = _authoritative_readings()
        del readings["window_signals"]["watchdog_task"]["query"]
        merged = _merged(readings)
        signal = merged["window_signals"]["watchdog_task"]
        self.assertEqual("undecidable", signal["status"])
        self.assertEqual("authoritative_envelope_missing", signal["reason"])
        self.assertIsNone(signal["state"])
        self.assertIsNone(signal["elevated"])
        report = merged["authoritative_readings"]
        self.assertEqual("probe_envelope_missing",
                          report["warnings"][0]["reason"])
        row = [row for row in report["merged_signals"]
               if row["signal"] == "watchdog_task"][0]
        self.assertEqual("undecidable", row["status"])
        self.assertTrue(row["rederived"])

    def test_watchdog_with_unparsable_envelope_is_undecidable(self):
        """信封在但缺字段（解析不出）→ undecidable（绝不折成通过）。"""
        readings = _authoritative_readings()
        readings["window_signals"]["watchdog_task"]["query"] = {"elevated": True}
        merged = _merged(readings)
        signal = merged["window_signals"]["watchdog_task"]
        self.assertEqual("undecidable", signal["status"])
        self.assertEqual("authoritative_envelope_unparsable", signal["reason"])
        reasons = [row["reason"]
                   for row in merged["authoritative_readings"]["warnings"]]
        self.assertIn("probe_envelope_unparsable", reasons)

    def test_unknown_signal_is_rejected(self):
        readings = _authoritative_readings(
            not_a_signal=_signal("not_a_signal", "ok"))
        merged = _merged(readings)
        report = merged["authoritative_readings"]
        self.assertEqual([{"signal": "not_a_signal",
                           "reason": "unknown_signal"}],
                          report["rejected_signals"])
        self.assertNotIn("not_a_signal", merged["window_signals"])

    def test_bad_status_is_rejected(self):
        readings = _authoritative_readings(
            raw_txt=_signal("raw_txt", "maybe", path="p"))
        merged = _merged(readings)
        report = merged["authoritative_readings"]
        self.assertEqual("bad_status:'maybe'",
                          report["rejected_signals"][0]["reason"])
        self.assertNotIn("raw_txt", merged["window_signals"])

    def test_missing_keys_are_rejected(self):
        readings = _authoritative_readings(
            wfp_limits=_signal("wfp_limits", "ok", app_exe="x.exe"))
        merged = _merged(readings)
        report = merged["authoritative_readings"]
        self.assertIn("missing_keys:", report["rejected_signals"][0]["reason"])
        self.assertNotIn("wfp_limits", merged["window_signals"])

    def test_signal_name_mismatch_is_rejected(self):
        broken = _wfp_ok()
        broken["signal"] = "raw_txt"
        readings = _authoritative_readings(wfp_limits=broken)
        merged = _merged(readings)
        report = merged["authoritative_readings"]
        self.assertEqual("signal_mismatch:'raw_txt'",
                          report["rejected_signals"][0]["reason"])

    def test_non_dict_entry_is_rejected(self):
        readings = _authoritative_readings(backend_pid_file="54300")
        merged = _merged(readings)
        report = merged["authoritative_readings"]
        self.assertEqual("not_a_dict",
                          report["rejected_signals"][0]["reason"])

    def test_missing_window_signals_raises(self):
        with self.assertRaises(ValueError):
            tool.merge_authoritative_readings(_v2_before(), {"tool": "x"})
        with self.assertRaises(ValueError):
            tool.merge_authoritative_readings(_v2_before(), [])

    def test_wrong_tool_raises(self):
        with self.assertRaises(ValueError):
            tool.merge_authoritative_readings(_v2_before(), _v2_before())

    def test_probe_reported_elevated_is_recorded(self):
        merged = _merged(_authoritative_readings())
        self.assertTrue(merged["authoritative_readings"]["source"]["elevated"])

    def test_probe_metadata_mismatches_are_warnings_not_silent(self):
        readings = _authoritative_readings()
        readings["schema_version"] = 2
        readings["read_only"] = False
        merged = _merged(readings)
        reasons = [row["reason"]
                   for row in merged["authoritative_readings"]["warnings"]]
        self.assertIn("probe_schema_version_mismatch:2", reasons)
        self.assertIn("probe_not_marked_read_only:False", reasons)


# ----------------------------------------------------- 不提供权威读数：行为不变
class NoAuthoritativeTests(unittest.TestCase):
    def test_without_authoritative_the_watchdog_stays_undecidable(self):
        """维护者要求：未提供权威读数时仍是 undecidable 且退出码 2。"""
        before = _v2_before()
        after = _v2_after()
        for snapshot in (before, after):
            self.assertEqual(
                "undecidable",
                snapshot["window_signals"]["watchdog_task"]["status"])
        result = tool.compare_snapshots(before, after)
        self.assertEqual(2, result["exit_code"])
        self.assertFalse(result["ok"])
        self.assertEqual("undecidable", _item(result, "watchdog_task")["verdict"])

    def test_without_authoritative_the_new_signals_are_not_compared(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        self.assertFalse(result["authoritative_compared"])
        verdicts = _verdicts(result)
        for item_id in ("wfp_limits", "raw_txt", "backend_pid_file"):
            self.assertNotIn(item_id, verdicts)
        self.assertNotIn("authoritative_readings", _v2_before())

    def test_without_authoritative_the_summary_has_no_authoritative_lines(self):
        text = tool.format_snapshot_summary(_v2_before())
        self.assertNotIn("权威读数", text)
        self.assertNotIn("权威信号", text)

    def test_without_authoritative_the_comparison_prints_the_note(self):
        text = tool.format_comparison(tool.compare_snapshots(_v2_before(),
                                                             _v2_after()))
        self.assertIn("这 3 项未纳入", text)
        self.assertIn("--authoritative", text)

    def test_collect_snapshot_shape_is_unchanged_without_authoritative(self):
        """``collect_snapshot`` 的 window_signals 为八类、无权威读数节。

        第 ⑧ 类 ``share_entry_faults`` 与顶层 ``maintenance_state`` /
        ``supervision_state`` 是契约 §8.3 的新证据面；本用例把三个读面都注入为
        「不可得」，断言它们如实记 ``undecidable`` / ``absent``、**绝不折成 ok**。
        """
        saved = {}
        names = ("http_get_json", "query_processes", "_run_command",
                 "query_watchdog_task", "gateway_attribution_facts",
                 "backend_observe_facts", "private_ledger_path",
                 "scan_share_ledger", "scan_private_ledger")
        for name in names:
            saved[name] = getattr(tool, name)

        def restore():
            for name, value in saved.items():
                setattr(tool, name, value)

        self.addCleanup(restore)

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
                return {"status": 200, "json": {"operations": []}, "error": None}
            if port == tool.GATEWAY_PORT:
                return {"status": 404,
                        "json": {"error": {"code": "route_not_shared"}},
                        "error": None}
            return {"status": None, "json": None, "error": "URLError"}

        tool.http_get_json = fake_get
        tool.query_processes = lambda pids: {}
        tool._run_command = lambda args, timeout=None: ""
        tool.query_watchdog_task = lambda run=None: tool.parse_watchdog_query(
            json.dumps(_query_envelope(elevated=False, found=False,
                                        state=None, state_text=None,
                                        error="CimJobException|"
                                              "CmdletizationQuery_NotFound_TaskName")))
        tool.gateway_attribution_facts = lambda: {
            "source": "in_process_share_attribution_status", "file_ready": False,
            "file_reason": "missing", "key_chars": None, "key_file": None,
            "instance_loaded_value": "unknown",
            "instance_loaded_basis": "inference",
            "instance_loaded_reason": "key_file_not_ready",
            "request_attribution_observed": False, "error": None}
        seen_forget_stale = []
        tool.backend_observe_facts = lambda forget_stale=False: (
            seen_forget_stale.append(forget_stale) or {
                "running": True, "auth_state": "valid", "auth_label": "x",
                "ledger_file": "usage-share-backend.jsonl", "error": None})
        tool.private_ledger_path = lambda: ("private.jsonl", None)
        tool.scan_share_ledger = lambda path, tail_fields=None: {
            "path": path, "rows": 0, "unparsed": 0, "last": None,
            "attributed_rows": [], "attribution_counts": {}, "error": None}
        tool.scan_private_ledger = lambda path: {
            "path": path, "rows": 0, "unparsed": 0, "key_id_nonempty": 0,
            "attribution_verified": 0, "error": None}

        snapshot = tool.collect_snapshot(
            # 三个新证据面注入为「不可得」：不触生产 broker / 监督状态文件。
            broker_faults=lambda: {"reply": None,
                                    "error": "broker_evidence_unavailable"},
            maintenance=lambda: {"view": None,
                                  "error": "maintenance_view_unavailable"},
            supervision=lambda: {"desired": None, "desired_error": None,
                                  "watch": {"unreadable": True,
                                            "reason": "missing"},
                                  "watch_error": None, "state_dir": "x"})
        # 快照采集必须走只读档（2026-09-28 收紧）：不得让观测顺手删陈旧 backend.pid。
        self.assertEqual(seen_forget_stale, [False],
                         "collect_snapshot 未按只读档调 backend_observe_facts")
        self.assertEqual(sorted(tool.WINDOW_SIGNAL_IDS),
                          sorted(snapshot["window_signals"]))
        self.assertNotIn("authoritative_readings", snapshot)
        for name in tool.AUTHORITATIVE_SIGNAL_IDS:
            self.assertNotIn(name, snapshot["window_signals"])
        self.assertEqual("undecidable",
                          snapshot["window_signals"]["watchdog_task"]["status"])
        # 第 ⑧ 类：证据不可得 → undecidable，counts_by_phase 保持 null（绝不填 0）。
        faults = snapshot["window_signals"]["share_entry_faults"]
        self.assertEqual("undecidable", faults["status"])
        self.assertIsNone(faults["counts_by_phase"])
        self.assertIsNone(faults["last_fault"])
        # 顶层证据节：维护视图不可得 → undecidable；监督状态文件缺失 → absent。
        self.assertEqual("undecidable", snapshot["maintenance_state"]["status"])
        self.assertEqual("absent", snapshot["supervision_state"]["status"])
        self.assertEqual(tool.NOT_COVERED_LIMITS, snapshot["not_covered"][1])
        self.assertEqual(tool.NOT_COVERED_WATCHDOG, snapshot["not_covered"][2])


# --------------------------------------------------- 权威读数三项判定（比对）
class AuthoritativeCompareTests(unittest.TestCase):
    def _pair(self):
        before = _merged(_authoritative_readings())
        after = _merged(_authoritative_readings(), snapshot=_v2_after())
        return before, after

    def test_merged_pair_passes_every_item(self):
        before, after = self._pair()
        result = tool.compare_snapshots(before, after)
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])
        self.assertTrue(result["authoritative_compared"])
        self.assertEqual("pass", _item(result, "watchdog_task")["verdict"])
        for item_id in ("wfp_limits", "raw_txt", "backend_pid_file"):
            self.assertEqual("pass", _item(result, item_id)["verdict"], item_id)

    def test_missing_authoritative_side_is_undecidable(self):
        before = _merged(_authoritative_readings())
        after = _v2_after()
        result = tool.compare_snapshots(before, after)
        self.assertEqual(2, result["exit_code"])
        for item_id in ("wfp_limits", "raw_txt", "backend_pid_file"):
            self.assertEqual("undecidable",
                              _item(result, item_id)["verdict"], item_id)

    def test_limits_absent_on_one_side_fails(self):
        before, after = self._pair()
        after["window_signals"]["wfp_limits"] = _signal(
            "wfp_limits", "absent", app_exe="x", backend_port=8110,
            wfp_ok=False, wfp_reason="strong-not-ok:STRONG=FAIL",
            firewall_ok=False, firewall_reason="rule-missing",
            reason="wfp_not_in_place")
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail", _item(result, "wfp_limits")["verdict"])

    def test_limits_port_change_fails(self):
        before, after = self._pair()
        after["window_signals"]["wfp_limits"] = _wfp_ok(port=8111)
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail", _item(result, "wfp_limits")["verdict"])
        self.assertIn("许可端口变了", _item(result, "wfp_limits")["detail"])

    def test_limits_undecidable_is_undecidable(self):
        before, after = self._pair()
        after["window_signals"]["wfp_limits"] = _signal(
            "wfp_limits", "undecidable", app_exe="x", backend_port=None,
            wfp_ok=False, wfp_reason="detection-failure(rc=1)",
            firewall_ok=False, firewall_reason="not_evaluated",
            reason="detection_failure")
        result = tool.compare_snapshots(before, after)
        self.assertEqual("undecidable", _item(result, "wfp_limits")["verdict"])

    def test_raw_txt_rewrite_fails(self):
        before, after = self._pair()
        after["window_signals"]["raw_txt"] = _raw_txt(mtime=1790475999.0)
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail", _item(result, "raw_txt")["verdict"])
        self.assertIn("被重写", _item(result, "raw_txt")["detail"])

    def test_raw_txt_appearing_fails(self):
        before, after = self._pair()
        before["window_signals"]["raw_txt"] = _signal(
            "raw_txt", "absent", path=_raw_txt()["path"], exists=False,
            size=None, mtime=None, reason="raw_txt_absent")
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail", _item(result, "raw_txt")["verdict"])

    def test_raw_txt_both_absent_passes(self):
        before, after = self._pair()
        absent = _signal("raw_txt", "absent", path=_raw_txt()["path"],
                          exists=False, size=None, mtime=None,
                          reason="raw_txt_absent")
        before["window_signals"]["raw_txt"] = copy.deepcopy(absent)
        after["window_signals"]["raw_txt"] = copy.deepcopy(absent)
        result = tool.compare_snapshots(before, after)
        self.assertEqual("pass", _item(result, "raw_txt")["verdict"])

    def test_backend_pid_bytes_change_fails(self):
        before, after = self._pair()
        after["window_signals"]["backend_pid_file"] = _backend_pid_file(
            content="60000")
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail",
                          _item(result, "backend_pid_file")["verdict"])
        self.assertIn("字节内容被改写",
                       _item(result, "backend_pid_file")["detail"])

    def test_backend_pid_both_absent_passes(self):
        before, after = self._pair()
        absent = _signal("backend_pid_file", "absent",
                          path=_backend_pid_file()["path"], path_source="x",
                          content=None, bytes_hex=None, size=None, pid=None,
                          mtime=None, reason="pid_file_absent")
        before["window_signals"]["backend_pid_file"] = copy.deepcopy(absent)
        after["window_signals"]["backend_pid_file"] = copy.deepcopy(absent)
        result = tool.compare_snapshots(before, after)
        self.assertEqual("pass",
                          _item(result, "backend_pid_file")["verdict"])


# ----------------------------------------------------------------- 文案对齐
class GuidanceWordingTests(unittest.TestCase):
    def test_undecidable_text_says_collect_evidence_first(self):
        """维护者要求：undecidable 必须打印「先补取证据，不得报通过」。"""
        text = tool.format_comparison(
            tool.compare_snapshots(_v2_before(), _v2_after()))
        self.assertIn("先补取证据", text)
        self.assertIn("不得报通过", text)

    def test_fail_text_says_roll_back(self):
        """维护者要求：fail 才打印「按 §⑤ 回退」。"""
        before = _v2_before()
        after = _v2_after()
        after["private_ledger"]["key_id_nonempty"] = 2
        result = tool.compare_snapshots(before, after)
        self.assertEqual("fail", _item(result, "private_ledger")["verdict"])
        text = tool.format_comparison(result)
        self.assertIn("按 §⑤ 回退", text)

    def test_undecidable_only_text_does_not_say_roll_back(self):
        text = tool.format_comparison(
            tool.compare_snapshots(_v2_before(), _v2_after()))
        self.assertNotIn("按 §⑤ 回退", text)

    def test_fail_only_text_does_not_say_collect_evidence(self):
        before = _merged(_authoritative_readings())
        after = _merged(_authoritative_readings(), snapshot=_v2_after())
        after["private_ledger"]["key_id_nonempty"] = 2
        result = tool.compare_snapshots(before, after)
        self.assertFalse(result["ok"])
        self.assertEqual(["private_ledger"],
                          [item["id"] for item in result["items"]
                           if item["verdict"] == "fail"])
        text = tool.format_comparison(result)
        self.assertIn("按 §⑤ 回退", text)
        self.assertNotIn("先补取证据", text)

    def test_every_item_line_carries_its_verdict(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        text = tool.format_comparison(result)
        self.assertEqual(len(result["items"]),
                          sum(1 for line in text.splitlines()
                              if line.startswith("[")))

    def test_undecidable_line_carries_the_guidance(self):
        result = tool.compare_snapshots(_v2_before(), _v2_after())
        text = tool.format_comparison(result)
        line = [row for row in text.splitlines()
                if row.startswith("[UNDECIDABLE]")][0]
        self.assertIn(tool.GUIDANCE_UNDECIDABLE, line)

    def test_fail_line_carries_the_guidance(self):
        before = _v2_before()
        after = _v2_after()
        after["private_ledger"]["key_id_nonempty"] = 2
        text = tool.format_comparison(tool.compare_snapshots(before, after))
        line = [row for row in text.splitlines()
                if row.startswith("[FAIL]")][0]
        self.assertIn(tool.GUIDANCE_FAIL, line)

    def test_guidance_constants_match_the_maintainer_wording(self):
        self.assertEqual("先补取证据，不得报通过", tool.GUIDANCE_UNDECIDABLE)
        self.assertEqual("按 §⑤ 回退", tool.GUIDANCE_FAIL)


# ------------------------------------------------------------------ CLI 层
class AuthoritativeCliTests(unittest.TestCase):
    def test_authoritative_with_compare_is_rejected(self):
        path = _temp_json(_authoritative_readings())
        try:
            code = tool.main(["--compare", str(path), str(path),
                              "--authoritative", str(path)])
        finally:
            path.unlink()
        self.assertEqual(2, code)

    def test_out_with_unreadable_authoritative_writes_nothing(self):
        out_dir = Path(tempfile.mkdtemp())
        out_path = out_dir / "snapshot-before.json"
        saved = tool.collect_snapshot
        tool.collect_snapshot = lambda: copy.deepcopy(_v2_before())
        self.addCleanup(setattr, tool, "collect_snapshot", saved)
        code = tool.main(["--out", str(out_path), "--authoritative",
                          str(out_dir / "missing.json")])
        self.assertEqual(2, code)
        self.assertFalse(out_path.exists())

    def test_out_with_authoritative_writes_the_merged_snapshot(self):
        readings = _authoritative_readings()
        auth_path = _temp_json(readings)
        out_dir = Path(tempfile.mkdtemp())
        out_path = out_dir / "snapshot-before.json"
        saved = tool.collect_snapshot
        tool.collect_snapshot = lambda: copy.deepcopy(_v2_before())
        self.addCleanup(setattr, tool, "collect_snapshot", saved)
        try:
            code = tool.main(["--out", str(out_path), "--authoritative",
                              str(auth_path)])
        finally:
            auth_path.unlink()
        self.assertEqual(0, code)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual("ok",
                          written["window_signals"]["watchdog_task"]["status"])
        self.assertEqual(str(auth_path),
                          written["authoritative_readings"]["source"]["path"])

    def test_out_without_authoritative_writes_the_same_shape(self):
        out_dir = Path(tempfile.mkdtemp())
        out_path = out_dir / "snapshot-before.json"
        saved = tool.collect_snapshot
        tool.collect_snapshot = lambda: copy.deepcopy(_v2_before())
        self.addCleanup(setattr, tool, "collect_snapshot", saved)
        code = tool.main(["--out", str(out_path)])
        self.assertEqual(0, code)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual("undecidable",
                          written["window_signals"]["watchdog_task"]["status"])
        self.assertNotIn("authoritative_readings", written)


# ------------------------------------------------- 探针脚本静态只读契约
class ProbeScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = PROBE_PATH.read_text(encoding="utf-8-sig")

    def test_probe_declares_the_window_signals(self):
        for name in tool.AUTHORITATIVE_SIGNAL_IDS:
            self.assertIn(name, self.text, name)
        self.assertIn("watchdog_task", self.text)

    def test_probe_reuses_the_watchdog_limits_functions(self):
        self.assertIn("watchdog-umshare.ps1", self.text)
        self.assertIn("-FunctionsOnly", self.text)
        self.assertIn("Get-LimitsState", self.text)
        self.assertIn("Get-FirewallState", self.text)

    def test_probe_has_no_write_or_real_action_tokens(self):
        for token in FORBIDDEN_PROBE_TOKENS:
            self.assertNotIn(token, self.text, token)

    def test_probe_writes_only_through_the_explicit_out_switch(self):
        self.assertEqual(1, self.text.count("WriteAllText"))
        guard = self.text.index("if ($Out -ne '')")
        self.assertLess(guard, self.text.index("WriteAllText"))
        self.assertIn("UTF8Encoding($false)", self.text)

    def test_probe_only_queries_the_task(self):
        self.assertIn("schtasks /query /TN", self.text)
        self.assertNotIn("schtasks /create", self.text)
        self.assertNotIn("schtasks /change", self.text)
        self.assertNotIn("schtasks /delete", self.text)
        self.assertNotIn("schtasks /run", self.text)
        self.assertNotIn("schtasks /end", self.text)

    def test_probe_never_reads_the_key_and_only_stats_raw_txt(self):
        self.assertIn("raw.txt 内容不读", self.text)
        self.assertIn("ReadAllBytes", self.text)
        self.assertNotIn("share-attribution.key", self.text)


# --------------------------------------------- 复审返修 1：允许集内的名字不崩
class WindowSignalNameTests(unittest.TestCase):
    """``WINDOW_SIGNAL_IDS`` 的其余 6 个名字也必须能合并（缺公共键则拒绝）。"""

    COMMON = {"signal": None, "status": "ok", "reason": "ok", "detail": "x"}

    def _common_entry(self, name):
        entry = dict(self.COMMON)
        entry["signal"] = name
        return entry

    def _merge_one(self, name, entry):
        return tool.merge_authoritative_readings(
            {"window_signals": {}},
            {"tool": "authoritative_readings", "schema_version": 1,
             "read_only": True, "window_signals": {name: entry}},
            source={"path": "p", "sha256": "0" * 64, "encoding": "utf-8"})

    def test_every_window_signal_name_merges_with_the_common_keys(self):
        """反例（复核者：允许集内但无专属必填键行 → KeyError）：绝不崩。"""
        for name in tool.WINDOW_SIGNAL_IDS:
            if name in tool.AUTHORITATIVE_SIGNAL_REQUIRED_KEYS:
                continue
            merged, report = self._merge_one(name, self._common_entry(name))
            self.assertEqual("ok", merged["window_signals"][name]["status"], name)
            self.assertEqual([], report["rejected_signals"], name)

    def test_watchdog_task_with_common_keys_only_is_rejected(self):
        """``watchdog_task`` 的专属必填键仍按原口径：缺键 = 拒绝，不是崩。"""
        merged, report = self._merge_one("watchdog_task",
                                         self._common_entry("watchdog_task"))
        self.assertNotIn("watchdog_task", merged["window_signals"])
        self.assertIn("missing_keys:",
                      report["rejected_signals"][0]["reason"])

    def test_window_signal_missing_the_common_keys_is_rejected(self):
        for bad in ({"signal": "funnel", "status": "ok"},
                    {"status": "ok", "reason": "ok", "detail": "x"},
                    {"signal": "funnel", "status": "maybe", "reason": "ok",
                     "detail": "x"}):
            merged, report = self._merge_one("funnel", bad)
            self.assertNotIn("funnel", merged["window_signals"], bad)
            self.assertEqual(1, len(report["rejected_signals"]), bad)

    def test_cli_authoritative_file_with_a_window_signal_is_accepted(self):
        """复核者的 CLI 复现：探针 JSON 加一条 ``funnel`` → 退出码 0 且快照写出。"""
        readings = _authoritative_readings()
        readings["window_signals"]["funnel"] = _signal("funnel", "ok",
                                                      funnel_on=True)
        auth_path = _temp_json(readings)
        out_dir = Path(tempfile.mkdtemp())
        out_path = out_dir / "snapshot-before.json"
        saved = tool.collect_snapshot
        tool.collect_snapshot = lambda: copy.deepcopy(_v2_before())
        self.addCleanup(setattr, tool, "collect_snapshot", saved)
        try:
            code = tool.main(["--out", str(out_path), "--authoritative",
                              str(auth_path)])
        finally:
            auth_path.unlink()
        self.assertEqual(0, code)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual("ok", written["window_signals"]["funnel"]["status"])
        self.assertEqual([], written["authoritative_readings"]
                          ["rejected_signals"])


# ------------------------------------------- 复审返修 2：探针的三态分支（桩脚本）
STUB_HEADER = ("param([string]$RunRoot='', [string]$WfpHelper='', "
               "[string]$AppExe='', [string]$FirewallRule='', "
               "[switch]$FunctionsOnly)\n")


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "需要 PowerShell")
class ProbeStubScriptTests(unittest.TestCase):
    """用临时桩看门狗脚本跑真探针（只读；路径与任务名都在临时目录）。"""

    def _limits(self, body):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "watchdog-umshare.ps1").write_text(STUB_HEADER + body,
                                                   encoding="ascii")
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(PROBE_PATH),
             "-RunRoot", str(tmp), "-DeployDir", str(tmp),
             "-WatchdogTaskName", "no-such-watchdog-task",
             "-AppExe", str(tmp / "um-share-python.exe"),
             "-WfpHelper", str(tmp / "wfp_helper.ps1"),
             "-RawTxtPath", str(tmp / "raw.txt"), "-BackendDir", str(tmp)],
            capture_output=True, timeout=180)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = tmp / "probe-out.json"
        out.write_bytes(proc.stdout)
        loaded = tool.load_authoritative_file(str(out))
        self.assertIsNone(loaded["error"], loaded["error"])
        return loaded["readings"]["window_signals"]["wfp_limits"]

    def test_limits_ok_when_both_functions_agree(self):
        sig = self._limits("function Get-LimitsState { return $true, 'ok' }\n"
                            "function Get-FirewallState { return $true, 'ok' }\n")
        self.assertEqual("ok", sig["status"])
        self.assertIs(True, sig["wfp_ok"])
        self.assertIs(True, sig["firewall_ok"])

    def test_missing_firewall_function_is_undecidable_not_absent(self):
        """反例（复核者指出）：缺 ``Get-FirewallState`` → undecidable，不是 absent。"""
        sig = self._limits("function Get-LimitsState { return $true, 'ok' }\n")
        self.assertEqual("undecidable", sig["status"])
        self.assertEqual("firewall_function_missing", sig["reason"])
        self.assertNotEqual("absent", sig["status"])

    def test_missing_limits_function_is_undecidable(self):
        sig = self._limits("# stub watchdog script defines no limits function\n")
        self.assertEqual("undecidable", sig["status"])
        self.assertEqual("limits_function_missing", sig["reason"])

    def test_limits_strong_not_ok_is_absent(self):
        """确定不在位仍是 ``absent``（与三态口径一致，不被上面两例带偏）。"""
        sig = self._limits(
            "function Get-LimitsState { return $false, "
            "'strong-not-ok:STRONG=FAIL' }\n"
            "function Get-FirewallState { return $true, 'ok' }\n")
        self.assertEqual("absent", sig["status"])
        self.assertEqual("wfp_not_in_place", sig["reason"])


# ------------------------------------- 复审返修 3：运行副本核对（-RunningScriptPath）
# 加此能力之前 `authoritative_readings.ps1` 的顶层键集（不给该参数时必须逐字不变）。
BASE_PAYLOAD_KEYS = [
    "tool", "schema_version", "read_only", "write_actions", "elevated",
    "identity", "probe_pid", "captured_at", "captured_at_iso", "run_root",
    "watchdog_script", "deploy_dir", "window_signals", "not_covered",
]
# 每次运行必然变化、不参与「加性」比较的字段。
VOLATILE_PAYLOAD_KEYS = {"probe_pid", "captured_at", "captured_at_iso"}

# 仓库脚本（`-WatchdogScript` 的基准）与运行副本都用同一份桩：两函数齐备，
# 探针才走 limits 的 ok 分支，且桩本身能被逐字节比较。
RUNNING_STUB = (STUB_HEADER +
                "function Get-LimitsState { return $true, 'ok' }\n"
                "function Get-FirewallState { return $true, 'ok' }\n")


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "需要 PowerShell")
class ProbeRunningCopyTests(unittest.TestCase):
    """`-RunningScriptPath` 只读运行副本核对：ok / mismatch / undecidable 三态。

    每例跑一次真探针（只读），但 ``RunRoot`` / ``DeployDir`` / ``WatchdogScript`` /
    ``RunningScriptPath`` / ``BackendDir`` 全在临时目录，任务名用不存在的名字；
    ``schtasks /query`` 与 ``Get-ScheduledTask`` 都只是只读查询。
    """

    def _run(self, tmp, repo, running_path):
        args = ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(PROBE_PATH),
                "-RunRoot", str(tmp), "-DeployDir", str(tmp),
                "-WatchdogScript", str(repo),
                "-WatchdogTaskName", "no-such-watchdog-task",
                "-AppExe", str(tmp / "um-share-python.exe"),
                "-WfpHelper", str(tmp / "wfp_helper.ps1"),
                "-RawTxtPath", str(tmp / "raw.txt"),
                "-BackendDir", str(tmp)]
        if running_path is not None:
            args += ["-RunningScriptPath", str(running_path)]
        proc = subprocess.run(args, capture_output=True, timeout=180)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = Path(tempfile.mkdtemp()) / "probe-out.json"
        out.write_bytes(proc.stdout)
        loaded = tool.load_authoritative_file(str(out))
        self.assertIsNone(loaded["error"], loaded["error"])
        return loaded["readings"]

    def _repo_and_running(self, running_body):
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo-watchdog.ps1"
        repo.write_bytes(RUNNING_STUB.encode("ascii"))
        running = tmp / "run-watchdog.ps1"
        if running_body is not None:
            running.write_bytes(running_body)
        return tmp, repo, running

    def test_missing_running_copy_is_undecidable(self):
        """运行副本不存在：exists=false 是确定事实，但比较不可判（不是「不一致」）。"""
        tmp, repo, running = self._repo_and_running(None)
        section = self._run(tmp, repo, running)["running_script_check"]
        self.assertEqual("undecidable", section["status"])
        self.assertIsNone(section["matches_repo"])
        self.assertIs(False, section["exists"])
        self.assertIsNone(section["sha256"])
        self.assertEqual("running_script_missing", section["reason"])

    def test_drifted_running_copy_is_reported_as_mismatch(self):
        """运行副本与仓库不一致（逐字节）：报 mismatch，绝不折成一致。"""
        import hashlib
        tmp, repo, running = self._repo_and_running(
            RUNNING_STUB.encode("ascii") + b"# drift\n")
        section = self._run(tmp, repo, running)["running_script_check"]
        self.assertEqual("mismatch", section["status"])
        self.assertIs(False, section["matches_repo"])
        self.assertEqual("content_mismatch", section["reason"])
        self.assertEqual(hashlib.sha256(running.read_bytes()).hexdigest().upper(),
                         section["sha256"])
        self.assertNotEqual(section["repo_sha256"], section["sha256"])

    def test_identical_running_copy_matches_repo(self):
        """逐字节一致：matches_repo=true，且 sha256 等于该文件字节的哈希。"""
        import hashlib
        tmp, repo, running = self._repo_and_running(RUNNING_STUB.encode("ascii"))
        section = self._run(tmp, repo, running)["running_script_check"]
        self.assertEqual("ok", section["status"])
        self.assertIs(True, section["matches_repo"])
        self.assertEqual("ok", section["reason"])
        self.assertIs(True, section["exists"])
        self.assertEqual(len(RUNNING_STUB.encode("ascii")), section["size"])
        self.assertEqual(hashlib.sha256(running.read_bytes()).hexdigest().upper(),
                         section["sha256"])
        self.assertEqual(section["sha256"], section["repo_sha256"])
        self.assertEqual(section["path"], str(running))
        self.assertEqual(section["repo_path"], str(repo))

    def test_without_the_parameter_the_section_is_absent(self):
        """不给参数：输出不含该节，顶层键集与加此能力之前逐字相同（加性、默认关闭）。"""
        tmp, repo, _ = self._repo_and_running(None)
        payload = self._run(tmp, repo, None)
        self.assertNotIn("running_script_check", payload)
        self.assertEqual(BASE_PAYLOAD_KEYS, list(payload.keys()))

    def test_the_parameter_is_additive_only(self):
        """同一目录跑两次：除新增节与三个易变字段外，其余键值逐字相同。"""
        tmp, repo, _ = self._repo_and_running(None)
        without = self._run(tmp, repo, None)
        with_section = self._run(tmp, repo, tmp / "absent-watchdog.ps1")
        self.assertEqual(set(BASE_PAYLOAD_KEYS) | {"running_script_check"},
                         set(with_section))
        for key in BASE_PAYLOAD_KEYS:
            if key in VOLATILE_PAYLOAD_KEYS:
                continue
            self.assertEqual(without[key], with_section[key], key)

    def test_same_path_on_both_sides_is_undecidable(self):
        """``-WatchdogScript`` 与 ``-RunningScriptPath`` 同一路径：自比恒真 → undecidable。"""
        tmp, repo, _ = self._repo_and_running(RUNNING_STUB.encode("ascii"))
        section = self._run(tmp, repo, repo)["running_script_check"]
        self.assertEqual("undecidable", section["status"])
        self.assertEqual("same_path", section["reason"])
        self.assertIsNone(section["matches_repo"])
        self.assertIs(True, section["exists"])


if __name__ == "__main__":
    unittest.main()
