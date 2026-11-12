# -*- coding: utf-8 -*-
"""`ledger_row_recheck` 的夹具测试（不碰生产账本）。

覆盖：

* **(a) 按内容哈希定位的四态**：找到唯一 / 未找到 / 多重命中 / 内容不符；
* **(b) 备份非前缀即失败**：中段不同、备份比账本更长两条反向分支；
* **(c) 移除后逐行比对的判定**：通过 / 保留行被改 / 多一行 / 少一行 / 丢错行；
* 端到端：`recheck` 的判据汇总、`main` 的退出码与拒绝语义
  （无 `--authorized` 不写清单、`--out` 落在禁写路径被拒、备份非前缀退出 2）。

隔离：全部夹具在 `tempfile` 临时目录里；**不读也不写生产
`ai-gateway/data/usage-chat2api.jsonl`、不碰任何备份文件、不删除任何行**。
写清单的分支只在临时目录里跑（本文件不执行任何真实移除）。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_ledger_row_recheck -q
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_PATH = (Path(__file__).resolve().parents[1] / "tools" / "um-share"
             / "ledger_row_recheck.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "um_share_ledger_row_recheck", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


# --------------------------------------------------------------- 夹具构造
def record(**overrides):
    """一条形态与账本真实记录一致的用量行；``overrides`` 按原样覆盖字段。"""
    base = {
        "time": "2026-09-25T23:03:47+08:00",
        "service": "chat2api",
        "model": "claude-x",
        "stream": True,
        "status": "ok",
        "input_tokens": 5,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 5,
        "estimated": True,
        "duration_ms": 1789437674495,
        "usage_source": "estimated",
        "protocol": "messages",
        "key_id": None,
        "request_id": None,
        "attribution": "absent",
        "price_version": "2726b086016181e3",
    }
    base.update(overrides)
    return base


def render(item, order=None):
    """渲染一行（不含换行，行尾 CR）；``order`` 可改变键顺序。"""
    keys = list(order) if order is not None else list(item)
    body = json.dumps({key: item[key] for key in keys}, ensure_ascii=False)
    return body.encode("utf-8") + b"\r"


def reordered(item):
    """同一份记录、不同字节：键顺序反转。"""
    return render(item, order=list(item)[::-1])


def ledger_bytes(rows):
    return b"".join(row + b"\n" for row in rows)


def plan_of(entries):
    """``entries``: ``[(行号, 原始行字节)]`` → 计划 JSON（内容身份 + 行内 sha256）。"""
    return {
        "measured_at": "fixture",
        "rows": [
            {
                "line": line,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "record": json.loads(raw.decode("utf-8")),
                "occurrences": 1,
            }
            for line, raw in entries
        ],
    }


def ten_targets():
    """10 行互不相同的目标行（只改 time/duration_ms，其余字段同形）。"""
    rows = []
    for index in range(1, 11):
        item = record(time=f"2026-09-25T23:{index:02d}:47+08:00",
                       duration_ms=1789437674490 + index)
        rows.append(render(item))
    return rows


# ------------------------------------------------------- (a) 按内容哈希定位
class LocateStatesTest(unittest.TestCase):
    def _locate(self, rows, entries):
        lines = tool.split_lines(ledger_bytes(rows))
        index, errors = tool.build_content_index(lines)
        self.assertEqual(errors, [])
        return tool.locate_rows(plan_of(entries)["rows"], index, lines)[0]

    def test_found_unique(self):
        target = record()
        raw = render(target)
        rows = [render(record(model="other-a")), raw,
                render(record(model="other-b"))]
        row = self._locate(rows, [(2, raw)])
        self.assertEqual(row["state"], tool.STATE_UNIQUE)
        self.assertTrue(row["found"])
        self.assertTrue(row["unique"])
        self.assertEqual(row["current_lines"], [2])
        self.assertEqual(row["current_line"], 2)
        self.assertTrue(row["record_matches_plan"])
        self.assertTrue(row["raw_sha256_matches_plan"])

    def test_missing(self):
        target = record()
        rows = [render(record(model="other-a")), render(record(model="other-b"))]
        row = self._locate(rows, [(1, render(target))])
        self.assertEqual(row["state"], tool.STATE_MISSING)
        self.assertFalse(row["found"])
        self.assertFalse(row["unique"])
        self.assertEqual(row["current_lines"], [])
        self.assertIsNone(row["current_line"])
        self.assertFalse(row["record_matches_plan"])
        self.assertFalse(row["raw_sha256_matches_plan"])

    def test_ambiguous(self):
        raw = render(record())
        rows = [raw, render(record(model="other-a")), raw]
        row = self._locate(rows, [(1, raw)])
        self.assertEqual(row["state"], tool.STATE_AMBIGUOUS)
        self.assertTrue(row["found"])
        self.assertFalse(row["unique"])
        self.assertEqual(row["current_lines"], [1, 3])
        self.assertIsNone(row["current_line"])

    def test_content_mismatch(self):
        """内容哈希命中、行内 sha256 不符：同一份记录、不同字节（键顺序不同）。"""
        target = record()
        rows = [reordered(target)]
        row = self._locate(rows, [(1, render(target))])
        self.assertEqual(row["state"], tool.STATE_CONTENT_MISMATCH)
        self.assertTrue(row["found"])
        self.assertTrue(row["unique"])
        self.assertTrue(row["record_matches_plan"])
        self.assertFalse(row["raw_sha256_matches_plan"])
        self.assertEqual(row["current_line"], 1)

    def test_content_hash_ignores_key_order_and_whitespace(self):
        item = record()
        self.assertEqual(tool.content_sha256(item),
                         tool.content_sha256(dict(reversed(list(item.items())))))
        spaced = json.dumps(item, ensure_ascii=False, indent=2)
        self.assertEqual(tool.content_sha256(item),
                         tool.content_sha256(json.loads(spaced)))

    def test_content_hash_sees_value_change(self):
        self.assertNotEqual(tool.content_sha256(record()),
                            tool.content_sha256(record(duration_ms=1)))
        self.assertNotEqual(tool.content_sha256(record()),
                            tool.content_sha256(record(model="claude-y")))

    def test_line_number_is_output_not_criterion(self):
        """计划记录的行号与当前位置不一致：仍判 unique，只进 WARN，不影响门。"""
        rows = [render(record(model="other-a")), render(record(model="b"))]
        entries = [(99, render(record(model="b")))]
        result = tool.recheck(ledger_bytes(rows), plan_of(entries), b"", expect=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["located"][0]["current_line"], 2)
        self.assertFalse(result["located"][0]["line_number_matches_plan"])
        self.assertEqual(len(result["line_number_warnings"]), 1)


# ----------------------------------------------------------- (b) 备份前缀核对
class BackupPrefixTest(unittest.TestCase):
    def test_exact_prefix(self):
        full = ledger_bytes([render(record()), render(record(model="b"))])
        prefix = full[:len(render(record())) + 1]
        verdict = tool.check_backup(prefix, full)
        self.assertTrue(verdict["is_prefix"])
        self.assertIsNone(verdict["first_diff_offset"])
        self.assertEqual(verdict["backup_bytes"], len(prefix))
        self.assertEqual(verdict["current_bytes"], len(full))
        self.assertEqual(verdict["backup_lines_total"], 2)
        self.assertEqual(verdict["current_lines_total"], 3)
        self.assertEqual(verdict["delta_lines_total"], 1)

    def test_first_diff_offset(self):
        first = render(record())
        full = ledger_bytes([first, render(record(model="b"))])
        broken = bytearray(first + b"\n")
        broken[3] = ord("X")
        verdict = tool.check_backup(bytes(broken), full)
        self.assertFalse(verdict["is_prefix"])
        self.assertEqual(verdict["first_diff_offset"], 3)
        self.assertIn("3", verdict["reason"])

    def test_backup_longer_than_ledger(self):
        first = render(record())
        full = ledger_bytes([first])
        longer = full + b"\n" + render(record(model="tail"))
        verdict = tool.check_backup(longer, full)
        self.assertFalse(verdict["is_prefix"])
        self.assertFalse(verdict["length_ok"])
        self.assertIn("不可能是前缀", verdict["reason"])

    def test_empty_backup_is_prefix(self):
        verdict = tool.check_backup(b"", ledger_bytes([render(record())]))
        self.assertTrue(verdict["is_prefix"])
        self.assertEqual(verdict["backup_lines_nonempty"], 0)

    def test_line_counts(self):
        backup = ledger_bytes([render(record()), render(record(model="b"))])
        current = ledger_bytes([render(record()), render(record(model="b")),
                                render(record(model="c"))])
        verdict = tool.check_backup(backup, current)
        self.assertEqual(verdict["backup_lines_nonempty"], 2)
        self.assertEqual(verdict["current_lines_nonempty"], 3)
        self.assertEqual(verdict["delta_lines_nonempty"], 1)
        self.assertEqual(verdict["delta_lines_total"], 1)
        self.assertTrue(verdict["backup_ends_with_newline"])
        self.assertTrue(verdict["current_ends_with_newline"])
        self.assertEqual(verdict["backup_sha256"],
                         hashlib.sha256(backup).hexdigest())


# --------------------------------------------------- (c) 移除前后逐行比对判据
class LinewiseRemovalTest(unittest.TestCase):
    def test_pass(self):
        before = [b"a", b"b", b"c", b"d", b""]
        after = [b"a", b"d", b""]
        verdict = tool.verify_linewise_removal(before, after, [2, 3])
        self.assertTrue(verdict["ok"])
        self.assertTrue(verdict["count_ok"])
        self.assertEqual(verdict["expected_after_lines_total"], 3)
        self.assertEqual(verdict["mismatches"], [])
        self.assertEqual(verdict["extra_lines"], 0)
        self.assertEqual(verdict["missing_lines"], 0)

    def test_retained_line_changed(self):
        before = [b"a", b"b", b"c", b""]
        after = [b"a", b"CHANGED", b""]
        verdict = tool.verify_linewise_removal(before, after, [2])
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["mismatches"]), 1)
        self.assertEqual(verdict["mismatches"][0]["after_line"], 2)
        self.assertEqual(verdict["mismatches"][0]["expected_before_line"], 3)

    def test_extra_line(self):
        before = [b"a", b"b", b"c"]
        after = [b"a", b"c", b"extra"]
        verdict = tool.verify_linewise_removal(before, after, [2])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["mismatches"], [])
        self.assertEqual(verdict["extra_lines"], 1)
        self.assertEqual(verdict["missing_lines"], 0)

    def test_missing_line(self):
        before = [b"a", b"b", b"c", b""]
        after = [b"a", b""]
        verdict = tool.verify_linewise_removal(before, after, [1])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["missing_lines"], 1)
        self.assertEqual(verdict["extra_lines"], 0)

    def test_wrong_dropped_line(self):
        """丢错行：行数对得上，但保留行错位——逐行比对必须抓到。"""
        before = [b"a", b"b", b"c", b""]
        after = [b"a", b"c", b""]
        verdict = tool.verify_linewise_removal(before, after, [1])
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["count_ok"])
        self.assertEqual(len(verdict["mismatches"]), 1)
        self.assertEqual(verdict["mismatches"][0]["expected_before_line"], 2)

    def test_plan_removal_is_lossless_line_deletion(self):
        raw = ledger_bytes([render(record()), render(record(model="b"))])
        lines = tool.split_lines(raw)
        planned = tool.plan_removal(lines, [2])
        self.assertEqual(b"\n".join(lines), raw)
        self.assertEqual(planned["after_bytes"],
                         ledger_bytes([render(record())]))
        self.assertEqual(planned["kept_lines_total"], 2)


# ------------------------------------------------------------ recheck 汇总
class RecheckTest(unittest.TestCase):
    def test_all_ten_unique_and_prefix(self):
        rows = ten_targets()
        full = ledger_bytes(rows)
        entries = [(index + 1, raw) for index, raw in enumerate(rows)]
        result = tool.recheck(full, plan_of(entries), full)
        self.assertTrue(result["ok"])
        self.assertEqual(result["states"][tool.STATE_UNIQUE], 10)
        self.assertEqual(result["gate_failures"], [])
        self.assertEqual(result["removal"]["current_lines"],
                         [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(result["removal"]["after_lines_nonempty"], 0)
        self.assertTrue(result["removal"]["criterion"]["ok"])
        self.assertTrue(result["anchored"]["ok"])

    def test_removal_recomputes_against_backup(self):
        """备份是前缀时，移除后的当前账本须以「移除后的备份」为前缀；覆盖边界按字节偏移算。"""
        rows = ten_targets()
        full = ledger_bytes(rows)
        backup = ledger_bytes(rows[:5])
        entries = [(index + 1, raw) for index, raw in enumerate(rows)]
        result = tool.recheck(full, plan_of(entries), backup)
        self.assertTrue(result["ok"])
        self.assertEqual(result["anchored"]["drop_lines_in_backup"],
                         [1, 2, 3, 4, 5])
        self.assertEqual(result["anchored"]["drop_lines_outside_backup"],
                         [6, 7, 8, 9, 10])
        self.assertTrue(result["anchored"]["is_prefix_after_removal"])
        self.assertEqual(result["backup"]["delta_lines_nonempty"], 5)

    def test_backup_anchored_undecidable_when_not_prefix(self):
        rows = ten_targets()
        full = ledger_bytes(rows)
        result = tool.backup_anchored_removal(b"NOT-A-PREFIX", full, [1])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["expected_after_current_sha256"])
        self.assertIn("前缀", result["reason"])

    def test_missing_row_fails(self):
        rows = ten_targets()
        full = ledger_bytes(rows)
        entries = [(index + 1, raw) for index, raw in enumerate(rows)]
        truncated = ledger_bytes(rows[:9])
        result = tool.recheck(truncated, plan_of(entries), truncated)
        self.assertFalse(result["ok"])
        self.assertEqual(result["states"][tool.STATE_MISSING], 1)
        self.assertIsNone(result["removal"])

    def test_backup_not_prefix_fails(self):
        rows = ten_targets()
        full = ledger_bytes(rows)
        entries = [(index + 1, raw) for index, raw in enumerate(rows)]
        broken = b"NOT-A-PREFIX" + full
        result = tool.recheck(full, plan_of(entries), broken)
        self.assertFalse(result["ok"])
        self.assertFalse(result["backup"]["is_prefix"])
        self.assertTrue(any("前缀" in item for item in result["gate_failures"]))

    def test_malformed_plan_fails(self):
        full = ledger_bytes([render(record())])
        for broken in ([], {"rows": "nope"}, {"rows": [{"line": 1}]}):
            result = tool.recheck(full, broken, full, expect=1)
            self.assertFalse(result["ok"])
            self.assertTrue(result["plan_problems"])
            self.assertEqual(result["located"], [])

    def test_unparsable_ledger_line_fails(self):
        full = ledger_bytes([render(record()), b"{not json"])
        entries = [(1, render(record()))]
        result = tool.recheck(full, plan_of(entries), full, expect=1)
        self.assertFalse(result["ok"])
        self.assertEqual([e["line"] for e in result["parse_errors"]], [2])


# ------------------------------------------------------- main 的退出码与拒绝
class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.rows = ten_targets()
        self.ledger = self.dir / "ledger.jsonl"
        self.ledger.write_bytes(ledger_bytes(self.rows))
        entries = [(index + 1, raw) for index, raw in enumerate(self.rows)]
        self.plan = self.dir / "plan.json"
        self.plan.write_text(json.dumps(plan_of(entries), ensure_ascii=False),
                             encoding="utf-8")
        self.backup = self.dir / "ledger.bak"
        self.backup.write_bytes(self.ledger.read_bytes())

    def _run(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--ledger", str(self.ledger),
                              "--plan", str(self.plan),
                              "--backup", str(self.backup), *extra])
        return code, out.getvalue(), err.getvalue()

    def _sha(self):
        return hashlib.sha256(self.ledger.read_bytes()).hexdigest()

    def test_default_ok_and_writes_nothing(self):
        before = self._sha()
        code, out, err = self._run()
        self.assertEqual(code, tool.EXIT_OK)
        self.assertIn("逐字节前缀：是", out)
        self.assertIn("移除前后逐行比对判据：PASS", out)
        self.assertEqual(self._sha(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["ledger.bak", "ledger.jsonl", "plan.json"])
        self.assertEqual(err, "")

    def test_json_mode(self):
        code, out, _ = self._run("--json")
        self.assertEqual(code, tool.EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["states"][tool.STATE_UNIQUE], 10)
        self.assertEqual(payload["ledger_anchor"]["read_stable"], True)

    def test_out_without_authorized_refused(self):
        out_path = self.dir / "manifest.json"
        code, _, err = self._run("--out", str(out_path))
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("拒绝写入", err)
        self.assertFalse(out_path.exists())

    def test_out_to_ledger_refused(self):
        code, _, err = self._run("--authorized", "--out", str(self.ledger))
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("拒绝写入", err)
        self.assertEqual(self._sha(), hashlib.sha256(
            ledger_bytes(self.rows)).hexdigest())

    def test_out_to_backup_refused(self):
        code, _, _ = self._run("--authorized", "--out", str(self.backup))
        self.assertEqual(code, tool.EXIT_REFUSED)

    def test_guard_out_path_covers_data_dir(self):
        ledger = self.dir / "ledger.jsonl"
        reason = tool.guard_out_path(tool.DATA_DIR / "anything.json", ledger,
                                     self.plan, self.backup)
        self.assertIsNotNone(reason)
        self.assertIn("ai-gateway/data/", reason)

    def test_authorized_writes_manifest_only(self):
        out_path = self.dir / "manifest.json"
        before = self._sha()
        code, out, _ = self._run("--authorized", "--out", str(out_path))
        self.assertEqual(code, tool.EXIT_OK)
        self.assertEqual(self._sha(), before)
        manifest = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["authorized"])
        self.assertEqual(manifest["drop_lines"], list(range(1, 11)))
        self.assertEqual(manifest["dropped_count"], 10)
        self.assertEqual(manifest["before_lines_nonempty"], 10)
        self.assertEqual(manifest["after_lines_nonempty"], 0)
        self.assertEqual(manifest["ledger_anchor"]["sha256"], before)
        self.assertEqual(manifest["backup_is_prefix"], True)
        self.assertEqual(manifest["drop_content_sha256"],
                         [tool.content_sha256(json.loads(raw.decode("utf-8")))
                          for raw in self.rows])
        self.assertEqual(manifest["after_sha256"],
                         hashlib.sha256(b"").hexdigest())
        self.assertIn("未修改账本", manifest["read_only_note"])

    def test_backup_not_prefix_exit2(self):
        broken = bytearray(self.backup.read_bytes())
        broken[0] = ord("X")
        self.backup.write_bytes(bytes(broken))
        before = self._sha()
        code, out, err = self._run()
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("逐字节前缀：否", out)
        self.assertIn("备份不是当前账本的前缀", "".join(err + out))
        self.assertEqual(self._sha(), before)

    def test_no_manifest_when_gate_fails(self):
        """重核未通过时不得产出「可执行的移除清单」。"""
        out_path = self.dir / "manifest.json"
        self.ledger.write_bytes(ledger_bytes(self.rows[:9]))
        code, _, err = self._run("--authorized", "--out", str(out_path))
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("不产出", err)
        self.assertFalse(out_path.exists())

    def test_missing_row_exit2(self):
        """账本里少一行目标行：如实报 missing，退出 2，不改账本。"""
        self.ledger.write_bytes(ledger_bytes(self.rows[:9]))
        before = self._sha()
        code, out, _ = self._run()
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("missing", out)
        self.assertIn("未找到", out)
        self.assertIn("无法重算移除计划", out)
        self.assertEqual(self._sha(), before)

    def test_ambiguous_row_exit2(self):
        """目标行在账本里出现两次：如实报 ambiguous，退出 2，不改账本。"""
        self.ledger.write_bytes(ledger_bytes(self.rows + [self.rows[0]]))
        before = self._sha()
        code, out, _ = self._run()
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("ambiguous", out)
        self.assertIn("多重命中", out)
        self.assertEqual(self._sha(), before)

    def test_missing_plan_exit2(self):
        code, _, err = self._run("--plan", str(self.dir / "nope.json"))
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("计划不可读", err)

    def test_missing_backup_exit2(self):
        code, _, err = self._run("--backup", str(self.dir / "nope.bak"))
        self.assertEqual(code, tool.EXIT_FAIL)
        self.assertIn("备份不存在", err)


if __name__ == "__main__":
    unittest.main()
