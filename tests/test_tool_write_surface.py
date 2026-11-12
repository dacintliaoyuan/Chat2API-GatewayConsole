#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for audit_tool_write_surface: 夹具脚本片段 → 档位与退出码。

夹具全部在临时目录里现场构造（**不改任何真实脚本**），扫描器只做 AST 静态解析：
不导入、不执行夹具、不写任何文件。覆盖维护者点名的两种情形：

* 默认路径只写 (a)(b) 档 → 退出码 0；
* 含默认路径就会发生的写动作 → 退出码 2。

另覆盖两条与 2026-09-28 复审直接相关的判定：委托 ``forget_stale`` 链的
``False`` / 默认两档，以及新增写原语必须报 ``?``（未登记，退出码 2）；
最后核对扫描器自身不写被审目录（前后文件清单与内容哈希一致）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audit_tool_write_surface as audit  # noqa: E402

#: 只读 + 显式开关：只该出现 (a)(b)。
READ_ONLY_AND_GATED = '''\
# -*- coding: utf-8 -*-
import argparse


def read_only(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_out(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    read_only("in.json")
    if args.out:
        write_out(args.out, "{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 默认路径就写（没有任何写授权开关）。
DEFAULT_PATH_WRITE = '''\
# -*- coding: utf-8 -*-


def main():
    with open("out.json", "w", encoding="utf-8") as handle:
        handle.write("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 委托链显式 forget_stale=False：只读档。
FORGET_STALE_FALSE = '''\
# -*- coding: utf-8 -*-
from console import share_production as console_share_production


def collect():
    return console_share_production.production_backend_observe(
        forget_stale=False)


def main():
    return collect()


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 委托链默认 forget_stale=True，且只在 --out 采集模式里：默认路径就会删。
FORGET_STALE_DEFAULT = '''\
# -*- coding: utf-8 -*-
import argparse
from readonly.share_chain_snapshot import share_chain_snapshot


def collect_snapshot():
    return share_chain_snapshot.backend_observe_facts()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if args.out:
        collect_snapshot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: 新的变更型 PowerShell 写原语：未登记 → ?（退出码 2）。
NEW_POWERSHELL_WRITE = '''\
# -*- coding: utf-8 -*-
import subprocess

FIX_SCRIPT = "Set-Content -LiteralPath $env:TARGET -Value x"


def main():
    return subprocess.run(["powershell", "-NoProfile", "-Command", FIX_SCRIPT])


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_fixture(directory, name, text):
    """在临时目录里写一份夹具脚本片段（唯一写动作；不碰真实脚本）。"""
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def run_scan(directory, name):
    """跑一次扫描：返回 ``(退出码, JSON)``；扫描器只打印，不写文件。"""
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = audit.main(["--root", str(directory), "--target", name,
                           "--json"])
    return code, json.loads(stream.getvalue())


def snapshot_tree(directory):
    """目录快照：相对路径 → sha256（用于核对扫描器没有写被审目录）。"""
    out = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            out[str(path.relative_to(directory))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


class WriteSurfaceScanTests(unittest.TestCase):

    def test_read_only_and_gated_only_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_fixture(tmp, "a.py", READ_ONLY_AND_GATED)
            code, data = run_scan(tmp, "a.py")
            self.assertEqual(code, 0, data)
            self.assertEqual(data["summary"]["c"], 0)
            self.assertEqual(data["summary"]["?"], 0)
            self.assertEqual(data["summary"]["stale_declarations"], 0)
            tiers = {item["tier"] for item in data["findings"]}
            self.assertTrue(tiers)
            self.assertTrue(tiers <= {"a", "b"}, tiers)
            self.assertIn("open", [item["kind"] for item in data["findings"]])
            # 只读打开进只读动作清单，不是写动作。
            self.assertIn("open_read",
                          [item["kind"] for item in data["read_only_actions"]])
            # 写动作确实落在带开关的那条路径上（--out）。
            gated = [item for item in data["findings"]
                     if item["tier"] == "b"]
            self.assertTrue(gated)
            for item in gated:
                self.assertIn("out", item["reachable"])
                self.assertEqual(item["gates"], [])
            self.assertTrue(fixture.is_file())

    def test_default_path_write_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, "b.py", DEFAULT_PATH_WRITE)
            code, data = run_scan(tmp, "b.py")
            self.assertEqual(code, 2, data)
            # 默认路径上两个写原语：open(...,'w') 与 handle.write()。
            self.assertEqual(data["summary"]["c"], 2, data["summary"])
            default_rows = [item for item in data["findings"]
                            if item["tier"] == "c"]
            self.assertEqual(len(default_rows), 2)
            self.assertEqual(sorted(item["kind"] for item in default_rows),
                             ["open", "write"])
            for item in default_rows:
                self.assertEqual(item["function"], "main")
                self.assertEqual(item["reachable"], [])
            self.assertEqual(data["exit_code"], 2)

    def test_forget_stale_false_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, "c.py", FORGET_STALE_FALSE)
            code, data = run_scan(tmp, "c.py")
            self.assertEqual(code, 0, data)
            rows = [item for item in data["findings"]
                    if item["kind"] ==
                    "delegated:production_backend_observe"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tier"], "a")
            self.assertIs(rows[0]["forget_stale"], False)

    def test_forget_stale_default_under_mode_switch_is_default_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, "d.py", FORGET_STALE_DEFAULT)
            code, data = run_scan(tmp, "d.py")
            self.assertEqual(code, 2, data)
            rows = [item for item in data["findings"]
                    if item["kind"] == "delegated:backend_observe_facts"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tier"], "c")
            self.assertIsNone(rows[0]["forget_stale"])
            # --out 只是模式/路径开关：不是写授权开关，故不能判 (b)。
            self.assertIn("out", rows[0]["reachable"])
            self.assertEqual(rows[0]["gates"], [])
            self.assertEqual(data["summary"]["c"], 1, data["summary"])

    def test_new_write_primitive_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, "e.py", NEW_POWERSHELL_WRITE)
            code, data = run_scan(tmp, "e.py")
            self.assertEqual(code, 2, data)
            rows = [item for item in data["findings"]
                    if item["kind"] == "powershell_mutation"]
            self.assertEqual(len(rows), 1)
            # 模块级（import 即执行）的变更型脚本没有开关 → (c)。
            self.assertEqual(rows[0]["tier"], "c")
            self.assertEqual(rows[0]["reachable"], [])
            self.assertEqual(data["summary"]["c"], 1, data["summary"])

    def test_scan_does_not_write_into_the_target_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, "a.py", READ_ONLY_AND_GATED)
            write_fixture(tmp, "b.py", DEFAULT_PATH_WRITE)
            before = snapshot_tree(tmp)
            run_scan(tmp, "a.py")
            run_scan(tmp, "b.py")
            self.assertEqual(snapshot_tree(tmp), before)


if __name__ == "__main__":
    unittest.main()
