# -*- coding: utf-8 -*-
"""`ai-gateway/tools/um-share/` 两个只读工具的夹具测试（不连生产）。

覆盖：

* `share_chain_snapshot.compare_snapshots` 的每个判定分支——含「密钥缺失」
  （合法状态，必须 PASS）与「私人账本出现 key_id 非空」（必须 FAIL）两条反向分支；
* 纯解析：`netstat` 行、CIM JSON、分享入口探测、归因读面形状；
* 账本扫描：分享账本（行数/末行/已归属行/直方图）与私人账本（key_id 非空）；
* `metering_acceptance.evaluate_acceptance` 的每个分支——含「密钥缺失」与
  「私人账本 key_id 非空」两条反向分支。

隔离：夹具全在 `tempfile` 临时目录里；不连 127.0.0.1 的任何端口、不跑
netstat/CIM、不写仓库 `data/`、不重启任何服务、不生成密钥、不发模型请求。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_um_share_tools -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
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


snapshot_tool = _load("share_chain_snapshot")
acceptance_tool = _load("metering_acceptance")

FOUR_KEYS = ["key_chars", "key_file", "reason", "wired"]
SEVEN_KEYS = ["file_ready", "file_reason", "instance_loaded", "key_chars",
              "key_file", "request_attribution_detail",
              "request_attribution_observed"]


# --------------------------------------------------------------- 夹具构造
def _entry(status, code, error=None):
    return {"status": status, "code": code, "error": error}


def _listener(pids, reason=None):
    return {"pids": pids, "reason": reason}


def _snapshot(**overrides):
    """一份「重启前」现场夹具；``overrides`` 里的键按原样替换。"""
    base = {
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
                         "relation": "child_parent_is_parent_pid", "reason": "ok"},
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
        "not_covered": [],
    }
    for key, value in overrides.items():
        base[key] = value
    return copy.deepcopy(base)


def _after(**overrides):
    """一份「重启后、且已接线」现场夹具：8096 PID 已变、读面 7 键、密钥就绪。"""
    base = _snapshot()
    base["listeners"]["8096"] = _listener([60000])
    base["share_status"] = {"http_status": 200, "error": None,
                            "keys": list(SEVEN_KEYS), "shape": "seven_key",
                            "funnel_on": True, "file_ready": True,
                            "instance_loaded_value": "yes"}
    base["gateway_attribution"] = {
        "source": "console_read_surface", "file_ready": True,
        "file_reason": "ok", "key_chars": 64,
        "key_file": r"C:\ProgramData\UMShareRun\run\share-attribution.key",
        "instance_loaded_value": "yes", "instance_loaded_basis": "inference",
        "instance_loaded_reason": "process_started_at_or_after_key_mtime",
        "request_attribution_observed": False, "error": None}
    base["share_ledger"] = {"path": "usage-share-backend.jsonl", "rows": 1681,
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
    for key, value in overrides.items():
        base[key] = value
    return copy.deepcopy(base)


def _verdicts(result):
    return {item["id"]: item["verdict"] for item in result["items"]}


def _item(result, item_id):
    for item in result["items"]:
        if item["id"] == item_id:
            return item
    raise AssertionError("no item %r in %r"
                         % (item_id, [i["id"] for i in result["items"]]))


class ListenerParseTests(unittest.TestCase):
    NETSTAT = (
        "\n"
        "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       35724\n"
        "  TCP    127.0.0.1:8096         0.0.0.0:0              LISTENING       51352\n"
        "  TCP    127.0.0.1:8100         0.0.0.0:0              LISTENING       37020\n"
        "  TCP    [::1]:8100              [::]:0                 LISTENING       37020\n"
        "  TCP    127.0.0.1:8100         127.0.0.1:52000        ESTABLISHED     37020\n"
    )

    def test_parses_listening_rows_for_the_port_only(self):
        pids, reason = snapshot_tool.parse_listener_pids(self.NETSTAT, 8100)
        self.assertIsNone(reason)
        self.assertEqual([37020], pids)

    def test_absent_port_is_an_empty_list_not_an_error(self):
        pids, reason = snapshot_tool.parse_listener_pids(self.NETSTAT, 9999)
        self.assertIsNone(reason)
        self.assertEqual([], pids)

    def test_damaged_row_is_query_failed_never_an_empty_list(self):
        text = ("  TCP    127.0.0.1:8100        0.0.0.0:0        "
                "LISTENING       not-a-pid\n")
        pids, reason = snapshot_tool.parse_listener_pids(text, 8100)
        self.assertIsNone(pids)
        self.assertEqual("query_failed", reason)

    def test_missing_listener_is_reported_as_none_not_empty(self):
        result = snapshot_tool.listening_pids(
            8100, run=lambda _args: None)
        self.assertIsNone(result["pids"])
        self.assertEqual("query_failed", result["reason"])


class CimParseTests(unittest.TestCase):
    def test_single_object_payload_and_ticks_conversion(self):
        text = json.dumps({"ProcessId": 51352, "Name": "python.exe",
                            "ParentProcessId": 9412,
                            "StartTicks": "639260207577029460"})
        rows = snapshot_tool.parse_cim_processes(text)
        self.assertEqual({51352}, set(rows))
        self.assertEqual(9412, rows[51352]["parent_pid"])
        self.assertEqual("ok", rows[51352]["started_at_reason"])
        self.assertAlmostEqual(1790423957.702946, rows[51352]["started_at"],
                               places=3)

    def test_list_payload_and_empty_payload(self):
        rows = snapshot_tool.parse_cim_processes("[]")
        self.assertEqual({}, rows)
        text = json.dumps([{"ProcessId": 1, "Name": "a.exe",
                            "ParentProcessId": 2, "StartTicks": "1"}])
        rows = snapshot_tool.parse_cim_processes(text)
        self.assertEqual({1}, set(rows))

    def test_missing_start_time_is_not_a_guessed_value(self):
        text = json.dumps({"ProcessId": 7, "Name": "a.exe",
                            "ParentProcessId": 1, "StartTicks": None})
        rows = snapshot_tool.parse_cim_processes(text)
        self.assertIsNone(rows[7]["started_at"])
        self.assertEqual("start_time_unavailable",
                          rows[7]["started_at_reason"])

    def test_unparsable_output_is_none_never_empty(self):
        self.assertIsNone(snapshot_tool.parse_cim_processes("not json"))
        self.assertIsNone(snapshot_tool.parse_cim_processes(None))


class ShareEntryProbeTests(unittest.TestCase):
    def test_route_not_shared_and_invalid_api_key_are_read_back(self):
        out = snapshot_tool.share_entry_probe_result({
            "status": 404, "json": {"error": {"code": "route_not_shared"}},
            "error": None})
        self.assertEqual((404, "route_not_shared"),
                          (out["status"], out["code"]))
        out = snapshot_tool.share_entry_probe_result({
            "status": 401, "json": {"error": {"code": "invalid_api_key"}},
            "error": None})
        self.assertEqual((401, "invalid_api_key"), (out["status"], out["code"]))

    def test_unreachable_probe_is_recorded_as_an_error(self):
        out = snapshot_tool.share_entry_probe_result(
            {"status": None, "json": None, "error": "URLError"})
        self.assertIsNone(out["status"])
        self.assertEqual("URLError", out["error"])


class AttributionShapeTests(unittest.TestCase):
    def test_four_key_and_seven_key_shapes(self):
        self.assertEqual("four_key", snapshot_tool.attribution_shape(FOUR_KEYS))
        self.assertEqual("seven_key", snapshot_tool.attribution_shape(SEVEN_KEYS))

    def test_unknown_shape_is_not_folded_into_a_known_one(self):
        self.assertEqual("unknown",
                          snapshot_tool.attribution_shape(["a", "b"]))
        self.assertEqual("unknown", snapshot_tool.attribution_shape([]))


class LedgerScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="um-share-fixture-")
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, rows):
        path = self.dir / name
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                if isinstance(row, str):
                    handle.write(row + "\n")
                else:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)

    def test_share_ledger_counts_rows_last_and_attributed(self):
        path = self._write("usage-share-backend.jsonl", [
            {"time": "t0", "model": "m", "key_id": None,
             "attribution": "absent"},
            {"time": "t1", "model": "m", "key_id": "k1",
             "attribution": "verified"},
            {"time": "t2", "model": "m", "key_id": None},
        ])
        result = snapshot_tool.scan_share_ledger(path)
        self.assertEqual(3, result["rows"])
        self.assertEqual(0, result["unparsed"])
        self.assertEqual("t2", result["last"]["time"])
        self.assertIsNone(result["last"]["attribution"])
        self.assertEqual([{"index": 1, "time": "t1", "model": "m",
                           "key_id": "k1", "attribution": "verified"}],
                          result["attributed_rows"])
        # 无 attribution 字段的旧行记 legacy，写了字段但没标记的记 absent。
        self.assertEqual({"absent": 1, "legacy": 1, "verified": 1},
                          result["attribution_counts"])
        self.assertIsNone(result["error"])

    def test_share_ledger_unparsable_line_is_counted_not_hidden(self):
        path = self._write("usage-share-backend.jsonl", ["not json", "{}"])
        result = snapshot_tool.scan_share_ledger(path)
        self.assertEqual(2, result["rows"])
        self.assertEqual(1, result["unparsed"])
        # 没有 attribution 字段的旧行记 legacy（与 share_metering.py:194-200 同口径）。
        self.assertEqual({"legacy": 1}, result["attribution_counts"])

    def test_share_ledger_missing_path_is_an_error_not_empty_data(self):
        result = snapshot_tool.scan_share_ledger(None)
        self.assertEqual("ledger_path_unresolved", result["error"])

    def test_private_ledger_counts_key_id_nonempty(self):
        path = self._write("usage-chat2api.jsonl", [
            {"key_id": None, "attribution": "absent"},
            {"key_id": "   ", "attribution": "verified"},
            {"key_id": "k9", "attribution": "absent"},
        ])
        result = snapshot_tool.scan_private_ledger(path)
        self.assertEqual(3, result["rows"])
        self.assertEqual(1, result["key_id_nonempty"])
        self.assertEqual(1, result["attribution_verified"])
        self.assertIsNone(result["error"])

    def test_private_ledger_untouched_fixture_is_zero(self):
        path = self._write("usage-chat2api.jsonl", [
            {"key_id": None, "attribution": "absent"},
            {"key_id": None, "attribution": "absent"},
        ])
        result = snapshot_tool.scan_private_ledger(path)
        self.assertEqual(0, result["key_id_nonempty"])
        self.assertEqual(0, result["attribution_verified"])


class CompareSnapshotTests(unittest.TestCase):
    def test_wired_restart_passes_every_item(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after())
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("pass", _item(result, "8100_attribution")["verdict"])
        self.assertEqual("pass", _item(result, "8096_health")["verdict"])

    def test_missing_key_is_a_legal_state_and_still_passes(self):
        """反向分支 1：密钥缺失（未接线）是合法状态——其余项不变时必须全 PASS。

        注意读面形状仍是 7 键（③.3 的判据是「新代码已载入」，与密钥是否存在无关）。
        """
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            share_status={"http_status": 200, "error": None,
                            "keys": list(SEVEN_KEYS), "shape": "seven_key",
                            "funnel_on": True, "file_ready": False,
                            "instance_loaded_value": "unknown"},
            gateway_attribution={"source": "in_process_share_attribution_status",
                                  "file_ready": False, "file_reason": "missing",
                                  "key_chars": None, "key_file": None,
                                  "instance_loaded_value": "unknown",
                                  "instance_loaded_basis": "inference",
                                  "instance_loaded_reason": "key_file_not_ready",
                                  "request_attribution_observed": False,
                                  "error": None}))
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("pass", _item(result, "8100_attribution")["verdict"])

    def test_key_ready_but_instance_not_loaded_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            gateway_attribution={
                "source": "console_read_surface", "file_ready": True,
                "file_reason": "ok", "key_chars": 64, "key_file": "k",
                "instance_loaded_value": "no",
                "instance_loaded_basis": "inference",
                "instance_loaded_reason": "process_started_before_key_mtime",
                "request_attribution_observed": False, "error": None}))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "8100_attribution")["verdict"])

    def test_8096_pid_unchanged_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            listeners=dict(_snapshot()["listeners"], **{"8096": _listener([51352])})))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "8096_health")["verdict"])

    def test_gateway_pid_change_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            listeners=dict(_snapshot()["listeners"], **{"8100": _listener([999])})))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "8100_listening")["verdict"])

    def test_backend_offline_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            listeners=dict(_snapshot()["listeners"], **{"8110": _listener([])})))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "8110_listening")["verdict"])

    def test_auth_state_change_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            backend_observe={"running": True, "auth_state": "expired",
                             "auth_label": "认证过期", "ledger_file": "l",
                             "error": None}))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "8110_auth")["verdict"])

    def test_funnel_on_to_off_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            share_status=dict(_after()["share_status"], funnel_on=False)))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "funnel")["verdict"])

    def test_share_entry_route_drift_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            share_entry_probe={"root": _entry(200, None),
                                "health": _entry(404, "route_not_shared"),
                                "whitelist_models": _entry(401, "invalid_api_key")}))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "share_gateway")["verdict"])

    def test_share_ledger_row_loss_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            share_ledger=dict(_after()["share_ledger"], rows=1679)))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "share_ledger")["verdict"])

    def test_attributed_row_rewrite_fails(self):
        before_row = {"index": 0, "time": "t", "model": "m", "key_id": "k1",
                       "attribution": "verified"}
        after_row = dict(before_row, key_id="k2")
        result = snapshot_tool.compare_snapshots(
            _snapshot(share_ledger=dict(_snapshot()["share_ledger"],
                                        rows=1681,
                                        attributed_rows=[before_row])),
            _after(share_ledger=dict(_after()["share_ledger"],
                                     attributed_rows=[after_row])))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "share_ledger")["verdict"])

    def test_attributed_row_loss_fails(self):
        before_row = {"index": 0, "time": "t", "model": "m", "key_id": "k1",
                       "attribution": "verified"}
        result = snapshot_tool.compare_snapshots(
            _snapshot(share_ledger=dict(_snapshot()["share_ledger"],
                                        rows=1681,
                                        attributed_rows=[before_row])),
            _after())
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "share_ledger")["verdict"])

    def test_private_ledger_key_id_nonempty_fails(self):
        """反向分支 2：私人账本出现 key_id 非空——必须 FAIL（不得报成功）。"""
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            private_ledger={"path": "usage-chat2api.jsonl", "rows": 93133,
                             "unparsed": 0, "key_id_nonempty": 1,
                             "attribution_verified": 0, "error": None}))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "private_ledger")["verdict"])

    def test_legacy_read_surface_shape_fails(self):
        result = snapshot_tool.compare_snapshots(_snapshot(), _after(
            share_status=dict(_after()["share_status"],
                              keys=list(FOUR_KEYS), shape="four_key")))
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "read_surface_shape")["verdict"])

    def test_missing_health_field_is_undecidable(self):
        after = _after()
        del after["health_8096"]
        result = snapshot_tool.compare_snapshots(_snapshot(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable", _item(result, "8096_health")["verdict"])

    def test_missing_funnel_field_is_undecidable(self):
        after = _after()
        after["share_status"].pop("funnel_on")
        result = snapshot_tool.compare_snapshots(_snapshot(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable", _item(result, "funnel")["verdict"])

    def test_missing_ledger_rows_is_undecidable(self):
        after = _after()
        after["share_ledger"].pop("rows")
        result = snapshot_tool.compare_snapshots(_snapshot(), after)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable", _item(result, "share_ledger")["verdict"])

    def test_rendered_text_marks_every_item(self):
        text = snapshot_tool.format_comparison(
            snapshot_tool.compare_snapshots(_snapshot(), _after()))
        self.assertEqual(len(snapshot_tool.compare_snapshots(
            _snapshot(), _after())["items"]),
            sum(1 for line in text.splitlines() if line.startswith("[")))
        self.assertIn("PASS", text)
        self.assertIn("退出码 0", text)

    def test_rendered_failure_text_reports_the_failing_ids(self):
        text = snapshot_tool.format_comparison(
            snapshot_tool.compare_snapshots(_snapshot(), _after(
                private_ledger={"path": "p", "rows": 1, "unparsed": 0,
                                 "key_id_nonempty": 2,
                                 "attribution_verified": 0, "error": None})))
        self.assertIn("[FAIL]", text)
        self.assertIn("退出码 2", text)


class OperationsAndChainTests(unittest.TestCase):
    def test_operations_live_counts_queued_and_running(self):
        out = snapshot_tool.operations_summary({
            "status": 200,
            "json": {"operations": [{"state": "queued"}, {"state": "running"},
                                    {"state": "succeeded"}]},
            "error": None})
        self.assertEqual((3, 2), (out["total"], out["live"]))

    def test_operations_unavailable_payload_is_an_error(self):
        out = snapshot_tool.operations_summary(
            {"status": 200, "json": {"nope": 1}, "error": None})
        self.assertIsNone(out["total"])
        self.assertEqual("operations_payload_unavailable", out["error"])

    def test_chain_parent_relation_is_read_from_cim(self):
        listeners = {"8096": _listener([51352]), "8110": _listener([54300])}
        rows = {54300: {"name": "python.exe", "parent_pid": 51352,
                         "started_at": 1.0, "started_at_reason": "ok"}}
        out = snapshot_tool.chain_parent_relation(listeners, rows)
        self.assertEqual("child_parent_is_parent_pid", out["relation"])

    def test_chain_parent_mismatch_is_not_hidden(self):
        listeners = {"8096": _listener([51352]), "8110": _listener([54300])}
        rows = {54300: {"name": "python.exe", "parent_pid": 1,
                         "started_at": 1.0, "started_at_reason": "ok"}}
        out = snapshot_tool.chain_parent_relation(listeners, rows)
        self.assertEqual("mismatch", out["relation"])

    def test_chain_parent_query_failure_is_not_absence(self):
        listeners = {"8096": _listener([51352]), "8110": _listener([54300])}
        out = snapshot_tool.chain_parent_relation(listeners, None)
        self.assertEqual("unknown", out["relation"])
        self.assertEqual("process_query_failed", out["reason"])

    def test_gateway_facts_read_image_and_start_time(self):
        listeners = {"8100": _listener([37020])}
        rows = {37020: {"name": "um-share-python.exe", "parent_pid": 20608,
                         "started_at": 1790475720.8,
                         "started_at_reason": "ok"}}
        out = snapshot_tool.gateway_process_facts(listeners, rows)
        self.assertEqual("um-share-python.exe", out["image"])
        self.assertEqual(1790475720.8, out["started_at"])
        self.assertEqual("ok", out["reason"])

    def test_gateway_absent_is_not_listening_not_ok(self):
        out = snapshot_tool.gateway_process_facts({"8100": _listener([])}, {})
        self.assertEqual("not_listening", out["reason"])


class AcceptanceEvaluateTests(unittest.TestCase):
    def _facts(self, **overrides):
        base = {
            "attribution": {
                "source": "console_read_surface", "file_ready": True,
                "file_reason": "ok", "key_chars": 64, "key_file": "k",
                "instance_loaded_value": "yes",
                "instance_loaded_basis": "inference",
                "instance_loaded_reason": "process_started_at_or_after_key_mtime",
                "request_attribution_observed": False, "error": None},
            "share_ledger_file": "usage-share-backend.jsonl",
            "share_ledger_tail": [
                {"time": "t", "model": "m", "key_id": "k1",
                 "attribution": "verified"}],
            "share_ledger_tail_error": None,
            "share_ledger_verified_rows": 1,
            "share_ledger_verified_error": None,
            "share_usage": {"http_status": 200, "requests": 1681,
                             "keys": [{"key_id": "k1"}],
                             "keys_rows": [{"key_id": "k1", "requests": 1}],
                             "unattributed": {"key_id": None, "requests": 1680},
                             "attributionReasons": {"absent": 46},
                             "error": None},
            "private_ledger": {"path": "usage-chat2api.jsonl", "rows": 10,
                                "unparsed": 0, "key_id_nonempty": 0,
                                "attribution_verified": 0, "error": None},
        }
        for key, value in overrides.items():
            base[key] = value
        return copy.deepcopy(base)

    def test_all_three_facts_hold(self):
        result = acceptance_tool.evaluate_acceptance(self._facts())
        self.assertTrue(result["ok"], _verdicts(result))
        self.assertEqual(0, result["exit_code"])

    def test_missing_key_fails_the_gate(self):
        """反向分支 1：密钥缺失——未接线是合法状态，但门必须报非零。"""
        facts = self._facts(attribution={
            "source": "console_read_surface", "file_ready": False,
            "file_reason": "missing", "key_chars": None, "key_file": "k",
            "instance_loaded_value": "unknown",
            "instance_loaded_basis": "inference",
            "instance_loaded_reason": "key_file_not_ready",
            "request_attribution_observed": False, "error": None})
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail", _item(result, "fact1_file_ready")["verdict"])
        self.assertEqual("fail",
                          _item(result, "fact2_instance_loaded")["verdict"])

    def test_private_ledger_key_id_nonempty_fails_the_gate(self):
        """反向分支 2：私人账本出现 key_id 非空——必须报非零。"""
        facts = self._facts(private_ledger={
            "path": "usage-chat2api.jsonl", "rows": 11, "unparsed": 0,
            "key_id_nonempty": 1, "attribution_verified": 0, "error": None})
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "private_ledger_untouched")["verdict"])

    def test_private_ledger_verified_row_fails_the_gate(self):
        facts = self._facts(private_ledger={
            "path": "usage-chat2api.jsonl", "rows": 11, "unparsed": 0,
            "key_id_nonempty": 0, "attribution_verified": 1, "error": None})
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])

    def test_instance_unknown_fails_the_gate(self):
        facts = self._facts()
        facts["attribution"]["instance_loaded_value"] = "unknown"
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "fact2_instance_loaded")["verdict"])

    def test_no_verified_ledger_row_fails_the_gate(self):
        facts = self._facts(share_ledger_verified_rows=0,
                            share_ledger_tail=[{"time": "t", "model": "m",
                                                 "key_id": None,
                                                 "attribution": "absent"}])
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("fail",
                          _item(result, "fact3_request_attribution")["verdict"])

    def test_share_usage_read_failure_is_undecidable_not_pass(self):
        facts = self._facts(share_usage={"http_status": 503, "requests": None,
                                          "keys": None, "keys_rows": None,
                                          "unattributed": None,
                                          "attributionReasons": None,
                                          "error": "share_usage_unavailable"})
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable",
                          _item(result, "fact3_request_attribution")["verdict"])

    def test_private_ledger_unreadable_is_undecidable(self):
        facts = self._facts(private_ledger={
            "path": None, "rows": 0, "unparsed": 0, "key_id_nonempty": 0,
            "attribution_verified": 0, "error": "ledger_path_unresolved"})
        result = acceptance_tool.evaluate_acceptance(facts)
        self.assertEqual(2, result["exit_code"])
        self.assertEqual("undecidable",
                          _item(result, "private_ledger_untouched")["verdict"])

    def test_rendered_text_prints_the_tail_and_the_verdict(self):
        facts = self._facts()
        text = acceptance_tool.format_acceptance(
            facts, acceptance_tool.evaluate_acceptance(facts))
        self.assertIn("request_attribution_observed", text)
        self.assertIn("keys", text)
        self.assertIn("退出码 0", text)

    def test_rendered_text_reports_the_fact_source(self):
        facts = self._facts()
        facts["attribution"]["source"] = "console_read_surface"
        text = acceptance_tool.format_acceptance(
            facts, acceptance_tool.evaluate_acceptance(facts))
        self.assertIn("事实 1/2 来源：console_read_surface", text)


class LedgerTailTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="um-share-tail-")
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, rows):
        path = self.dir / name
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)

    def test_tail_keeps_only_the_agreed_fields(self):
        path = self._write("usage-share-backend.jsonl", [
            {"time": "t0", "model": "m", "key_id": None,
             "attribution": "absent", "prompt": "绝不打印的字段"},
            {"time": "t1", "model": "m", "key_id": "k1",
             "attribution": "verified"},
        ])
        rows, error = acceptance_tool.read_ledger_tail(path, 1)
        self.assertIsNone(error)
        self.assertEqual([{"time": "t1", "model": "m", "key_id": "k1",
                           "attribution": "verified"}], rows)

    def test_tail_of_a_missing_path_is_an_error_not_an_empty_tail(self):
        rows, error = acceptance_tool.read_ledger_tail(None)
        self.assertIsNone(rows)
        self.assertEqual("ledger_path_unresolved", error)

    def test_verified_row_count_matches_the_fixture(self):
        path = self._write("usage-share-backend.jsonl", [
            {"attribution": "verified"}, {"attribution": "absent"},
            {"attribution": "verified"},
        ])
        verified, error = acceptance_tool.count_verified_rows(path)
        self.assertIsNone(error)
        self.assertEqual(2, verified)

    def test_private_ledger_scan_counts_key_id_nonempty(self):
        path = self._write("usage-chat2api.jsonl", [
            {"key_id": None}, {"key_id": "k1"}, {"attribution": "verified"},
        ])
        result = acceptance_tool.scan_private_ledger(path)
        self.assertEqual(3, result["rows"])
        self.assertEqual(1, result["key_id_nonempty"])
        self.assertEqual(1, result["attribution_verified"])


if __name__ == "__main__":
    unittest.main()
