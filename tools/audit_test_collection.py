#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""审计 ``ai-gateway/tests`` 下「源码里有 ``test_`` 方法名、但 unittest 不会收集」的模块与方法。

背景（执行轮 10，维护者点名）：报告里的「四个新测试模块 147 项」是报告时点的**收集数**，
而维护者按静态 ``test_`` 方法名数出 149 项——两者差值来自收尾修正新增的两条用例。
静态数（grep ``def test_``）与收集数（``Ran N tests``）是两个口径，本脚本把两者逐模块摆在一起，
并回答更危险的问题：**有没有模块的 ``test_`` 方法根本不会被收集**。

判定口径（unittest 语义，Python 3.14 实测）：

* 只有**模块级** ``unittest.TestCase`` 子类里的 ``test`` 前缀可调用属性会被收集；
* 同类内**同名后定义覆盖**先定义 → 先定义的用例不会跑；
* 类体内被赋值/``del`` 覆盖掉的方法不会被收集；
* 模块级 ``def test_*`` 函数不会被 unittest 收集（pytest 会收集）；
* 类定义在函数/类内部（不在模块作用域）→ 不是模块属性，不会被收集；
* ``__test__ = False`` **不会**被 unittest 尊重（本脚本用实测钉住：3.14 的
  ``TestLoader.loadTestsFromModule`` 只按 ``issubclass(obj, TestCase)`` 过滤，
  不读 ``__test__``；pytest 会跳过它，所以只在「跨运行器差异」一节提示）。

只读保证：

* 默认（不加 ``--dynamic``）**不导入任何测试模块、不起子进程、不写任何文件、不发网络请求**；
  只用 ``ast`` 静态解析，并给出「模型可收集数」。
* ``--dynamic`` 才导入测试模块（与 ``python -m unittest tests.<模块>`` 相同的导入路径，
  只为收集、不执行任何用例）。注意：个别测试模块在**导入期**就会建系统临时目录
  （``tempfile.mkdtemp`` / ``tempfile.TemporaryDirectory``，见 ``test_chat2api_verify.py:27``、
  ``test_chat2api_control.py:18`` 等），所以这一步不是字面意义上的「零写盘」——
  正因如此它才需要显式开关；它不写仓库、不写 ``ai-gateway/data/``、不碰生产路径。
* 任何模式下都不重启服务、不写生产密钥、不发真实模型请求。

用法（工作目录任意）：

    python ai-gateway/tools/audit_test_collection.py              # 静态，只读
    python ai-gateway/tools/audit_test_collection.py --dynamic    # 追加加载器收集数交叉核对
    python ai-gateway/tools/audit_test_collection.py --module test_share_store

退出码：0=无差异；2=存在差异（声明但不会被收集、导入失败或模型/加载器不一致）；
3=用法或内部错误——**含 argparse 层面的用法错误**（如未知参数、缺参数值），
本脚本把 ``ArgumentParser.error`` 覆盖为退出 3，避免与「2=有差异」相撞。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

AI_GATEWAY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TESTS_DIR = AI_GATEWAY_DIR / "tests"
DISCOVER_PATTERN = "test_*.py"
TEST_PREFIX = "test"

_BOOTSTRAP = r"""
import importlib, importlib.util, json, os, sys, unittest

sys.dont_write_bytecode = True
out = {"ids": [], "all_classes": {}, "error": None}
try:
    mode, target = sys.argv[1], sys.argv[2]
    if mode == "name":
        mod = importlib.import_module(target)
    else:
        spec = importlib.util.spec_from_file_location(
            "audit_" + os.path.basename(target)[:-3], target)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    loader = unittest.TestLoader()
    def walk(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                out["ids"].append(item.id())
    walk(loader.loadTestsFromModule(mod))
    for cname, obj in vars(mod).items():
        if not isinstance(obj, type):
            continue
        is_case = issubclass(obj, unittest.TestCase)
        methods = {}
        if is_case:
            for n in dir(obj):
                if n.startswith(loader.testMethodPrefix):
                    try:
                        methods[n] = bool(callable(getattr(obj, n)))
                    except Exception as exc:
                        methods[n] = "attr-error: %s" % (exc,)
        out["all_classes"][cname] = {
            "is_testcase": bool(is_case),
            "bases": [b.__name__ for b in obj.__bases__],
            "methods": methods,
        }
except BaseException as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print("__AUDIT_JSON__" + json.dumps(out, ensure_ascii=False))
"""


class Finding:
    """一条「声明了 test_ 但不会被收集」的记录。"""

    def __init__(self, module, class_name, method, lineno, reason):
        self.module = module
        self.class_name = class_name
        self.method = method
        self.lineno = lineno
        self.reason = reason

    def render(self):
        where = "%s:%d" % (self.module, self.lineno)
        owner = self.class_name if self.class_name else "<模块级>"
        return "  %s  %s.%s —— %s" % (where, owner, self.method, self.reason)


def _scan_file(path):
    """静态扫描一个文件：返回类、方法声明与可疑点（只读，无副作用）。"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    classes = {}
    module_level_tests = []
    nested_classes = []
    nested_tests = []
    module_test_false = False
    has_load_tests = False

    def _test_false_in(body):
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__test__":
                        return True
            if isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id == "__test__":
                    return True
        return False

    def _visit(node, scope_class, scope_method):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if scope_class is None and scope_method is None:
                    entry = {
                        "name": child.name,
                        "lineno": child.lineno,
                        "bases": [ast.unparse(b) for b in child.bases],
                        "test_false": _test_false_in(child.body),
                        "methods": [],
                        "rebinds": [],
                        "deletes": [],
                        "nested_classes": [],
                    }
                    classes[child.name] = entry
                    _visit(child, child.name, None)
                else:
                    nested_classes.append((child.name, child.lineno))
                    _visit(child, scope_class, scope_method)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith(TEST_PREFIX):
                    if scope_class is None and scope_method is None:
                        module_level_tests.append((child.name, child.lineno))
                    elif scope_method is None:
                        classes[scope_class]["methods"].append(
                            (child.name, child.lineno))
                    else:
                        nested_tests.append(
                            (scope_class, scope_method, child.name, child.lineno))
                _visit(child, scope_class,
                       child.name if scope_method is None else scope_method)
                continue
            if scope_class is not None and scope_method is None:
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if (isinstance(target, ast.Name)
                                and target.id.startswith(TEST_PREFIX)):
                            classes[scope_class]["rebinds"].append(
                                (target.id, child.lineno))
                elif isinstance(child, ast.Delete):
                    for target in child.targets:
                        if (isinstance(target, ast.Name)
                                and target.id.startswith(TEST_PREFIX)):
                            classes[scope_class]["deletes"].append(
                                (target.id, child.lineno))
            _visit(child, scope_class, scope_method)

    _visit(tree, None, None)
    module_test_false = _test_false_in(tree.body)
    has_load_tests = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "load_tests"
        for node in tree.body)
    return {
        "classes": classes,
        "module_level_tests": module_level_tests,
        "nested_classes": nested_classes,
        "nested_tests": nested_tests,
        "module_test_false": module_test_false,
        "has_load_tests": has_load_tests,
    }


def _resolve_testcase(class_name, classes, memo):
    """静态判定一个类是否（传递地）继承 unittest.TestCase。None=无法判定。"""
    if class_name in memo:
        return memo[class_name]
    entry = classes.get(class_name)
    if entry is None:
        memo[class_name] = None
        return None
    memo[class_name] = None  # 防递归
    result = False
    for base in entry["bases"]:
        if base in ("unittest.TestCase", "TestCase"):
            result = True
            break
        if base in classes:
            sub = _resolve_testcase(base, classes, memo)
            if sub is None:
                result = None
                break
            if sub:
                result = True
                break
    memo[class_name] = result
    return result


def _static_findings(module_name, scan):
    """返回 (findings, notes, model_collectable)。"""
    findings = []
    notes = []
    classes = scan["classes"]
    memo = {}
    resolved = {name: _resolve_testcase(name, classes, memo)
                for name in classes}
    collectable = 0
    for name, entry in classes.items():
        is_case = resolved[name]
        if is_case is None:
            notes.append(
                "  %s 类 %s：基类 %s 无法静态判定（导入自其它模块），"
                "需要 --dynamic 才能定论"
                % (module_name, name, ", ".join(entry["bases"]) or "<无>"))
            continue
        last = {}
        for method, lineno in entry["methods"]:
            last[method] = lineno
        rebinds = dict(entry["rebinds"])
        deletes = set(m for m, _ in entry["deletes"])
        for method, lineno in entry["methods"]:
            if not is_case:
                findings.append(Finding(
                    module_name, name, method, lineno,
                    "类不继承 unittest.TestCase（基类：%s）"
                    % (", ".join(entry["bases"]) or "<无>")))
                continue
            if method in deletes:
                findings.append(Finding(
                    module_name, name, method, lineno,
                    "类体内被 ``del %s`` 删除" % method))
                continue
            if method in rebinds and rebinds[method] > lineno:
                findings.append(Finding(
                    module_name, name, method, lineno,
                    "类体内被行 %d 的赋值覆盖（属性不再是函数）"
                    % rebinds[method]))
                continue
            if last[method] != lineno:
                findings.append(Finding(
                    module_name, name, method, lineno,
                    "同名覆盖：行 %d 的后定义生效，本定义不会跑"
                    % last[method]))
                continue
            collectable += 1
        if entry["test_false"]:
            notes.append(
                "  %s 类 %s：``__test__ = False`` 被 unittest 忽略（仍会收集）"
                "——pytest 会跳过它，跨运行器口径不同"
                % (module_name, name))
    for method, lineno in scan["module_level_tests"]:
        findings.append(Finding(
            module_name, None, method, lineno,
            "模块级函数：unittest 只收集 TestCase 子类方法（pytest 会收集）"))
    for cls_name, cls_line in scan["nested_classes"]:
        notes.append(
            "  %s 嵌套类 %s（行 %d）不在模块作用域，其方法不会被收集"
            % (module_name, cls_name, cls_line))
    for cls_name, outer, method, lineno in scan["nested_tests"]:
        findings.append(Finding(
            module_name, cls_name, method, lineno,
            "嵌套在 %s 方法内的函数，不是类属性" % outer))
    if scan["module_test_false"]:
        notes.append("  %s 模块级 ``__test__ = False`` 被 unittest 忽略"
                     % module_name)
    if scan["has_load_tests"]:
        notes.append("  %s 定义了 ``load_tests`` 协议：收集可能被自定义"
                     "（本脚本静态模型不展开）" % module_name)
    return findings, notes, collectable


def _run_dynamic(module_name, tests_dir):
    if tests_dir.resolve() == DEFAULT_TESTS_DIR.resolve():
        # 与 `python -m unittest tests.<模块>` 相同的导入名（ai-gateway 为工作目录）。
        mode, target = "name", "tests." + module_name
    else:
        # 自定义目录：按文件路径加载（用于自测夹具），不假设包名。
        mode, target = "path", str(tests_dir / (module_name + ".py"))
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _BOOTSTRAP, mode, target],
        cwd=str(AI_GATEWAY_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("__AUDIT_JSON__"):
            payload = json.loads(line[len("__AUDIT_JSON__"):])
    if payload is None:
        return None, "子进程未返回收集结果（rc=%d, stderr=%s）" % (
            proc.returncode, proc.stderr.strip()[:200])
    if payload.get("error"):
        return None, "导入失败：%s" % payload["error"]
    return payload, None


class _Parser(argparse.ArgumentParser):
    """用法错误退 3，与「2=有差异」区分开。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("用法错误：%s（退出码 3）" % message, file=sys.stderr)
        raise SystemExit(3)


def main(argv=None):
    parser = _Parser(
        description="ai-gateway/tests 的 test_ 收集审计（默认静态只读）",
        epilog="退出码：0=无差异；2=有差异（声明但不会被收集/导入失败/模型与加载器不一致）；"
               "3=用法或内部错误（含 argparse 层面的用法错误）。")
    parser.add_argument("--tests-dir", default=str(DEFAULT_TESTS_DIR),
                        help="tests 目录（默认 ai-gateway/tests）")
    parser.add_argument("--module", action="append", default=None,
                        help="只审计指定模块名（可重复；默认全部）")
    parser.add_argument("--dynamic", action="store_true",
                        help="追加加载器收集数交叉核对（导入测试模块，"
                             "不执行用例；个别模块导入期会建系统临时目录）")
    parser.add_argument("--only-diff", action="store_true",
                        help="只打印有差异/有备注的模块行")
    args = parser.parse_args(argv)

    tests_dir = Path(args.tests_dir).resolve()
    if not tests_dir.is_dir():
        print("错误：tests 目录不存在：%s" % tests_dir, file=sys.stderr)
        return 3

    modules = sorted(p for p in tests_dir.glob(DISCOVER_PATTERN) if p.is_file())
    if args.module:
        wanted = set(args.module)
        modules = [p for p in modules if p.stem in wanted]
        missing = wanted - {p.stem for p in modules}
        if missing:
            print("错误：找不到模块：%s" % ", ".join(sorted(missing)),
                  file=sys.stderr)
            return 3
    if not modules:
        print("错误：%s 下没有匹配 %s 的模块" % (tests_dir, DISCOVER_PATTERN),
              file=sys.stderr)
        return 3

    mode = "静态 + 加载器交叉核对（--dynamic）" if args.dynamic else "静态（默认，不导入任何模块）"
    print("== ai-gateway tests 收集审计 ==")
    print("目录：%s" % tests_dir)
    print("模式：%s" % mode)
    print("")

    total_declared = 0
    total_collectable = 0
    total_collected = 0
    findings = []
    notes = []
    load_failures = []
    model_mismatches = []

    for path in modules:
        module_name = path.stem
        try:
            scan = _scan_file(path)
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            findings.append(Finding(
                path.name, None, "<整个文件>", getattr(exc, "lineno", 0) or 0,
                "静态解析失败（%s：%s）→ 模块不会被收集"
                % (type(exc).__name__, exc)))
            print("%-42s 静态解析失败：%s" % (path.name, exc))
            continue
        declared = sum(len(e["methods"]) for e in scan["classes"].values())
        declared += len(scan["module_level_tests"])
        declared += len(scan["nested_tests"])
        module_findings, module_notes, collectable = _static_findings(
            path.name, scan)
        findings.extend(module_findings)
        notes.extend(module_notes)
        collected = None
        if args.dynamic:
            payload, error = _run_dynamic(module_name, tests_dir)
            if error:
                load_failures.append((path.name, error))
            else:
                ids = payload["ids"]
                collected = len(ids)
                pairs = set()
                for test_id in ids:
                    parts = test_id.rsplit(".", 2)
                    if len(parts) == 3:
                        pairs.add((parts[1], parts[2]))
                declared_pairs = set()
                for class_name, entry in scan["classes"].items():
                    last = {}
                    for method, lineno in entry["methods"]:
                        last[method] = lineno
                    for method in last:
                        declared_pairs.add((class_name, method))
                external = sorted(pairs - declared_pairs)
                predicted_here = collectable
                if predicted_here + len(external) != collected:
                    model_mismatches.append(
                        (path.name, predicted_here, len(external), collected))
                for class_name, method in external:
                    notes.append(
                        "  %s 收集到 %s.%s：本文件未声明（继承自基类或"
                        "同名别名），非差异" % (path.name, class_name, method))
        total_declared += declared
        total_collectable += collectable
        if collected is not None:
            total_collected += collected
        diff = declared - collectable
        if args.only_diff and diff == 0 and not module_findings:
            continue
        line = "%-42s 声明=%-4d 模型可收集=%-4d 差值=%-3d" % (
            path.name, declared, collectable, diff)
        if collected is not None:
            line += " 收集=%d" % collected
        print(line)

    print("")
    print("== 汇总 ==")
    print("模块数=%d" % len(modules))
    print("声明 test_ 方法数=%d" % total_declared)
    print("静态模型可收集数=%d" % total_collectable)
    print("声明但不会被收集=%d" % len(findings))
    if args.dynamic:
        print("加载器实际收集数=%d" % total_collected)
        print("导入失败模块=%d" % len(load_failures))
        print("模型/加载器不一致=%d" % len(model_mismatches))
    print("")

    if findings:
        print("== 声明但不会被收集（差异）==")
        for finding in findings:
            print(finding.render())
        print("")
    if load_failures:
        print("== 导入失败（差异）==")
        for name, error in load_failures:
            print("  %s：%s" % (name, error))
        print("")
    if model_mismatches:
        print("== 模型/加载器不一致（差异）==")
        for name, predicted, external, collected in model_mismatches:
            print("  %s：模型可收集=%d + 外部收集=%d != 实际=%d"
                  % (name, predicted, external, collected))
        print("")
    if notes:
        print("== 备注（非差异）==")
        for note in notes:
            print(note)
        print("")

    if findings or load_failures or model_mismatches:
        print("结论：存在差异（退出码 2）")
        return 2
    print("结论：无差异（退出码 0）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层用法/内部错误
        print("内部错误：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(3)
