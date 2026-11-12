#!/usr/bin/env python3
"""Handoff 行号锚点审计：把 handoff 里的每条「文件:行号」引用解析到指定基准提交上。

背景：handoff 正文里散布 `compare_driver.py:981`、`chat2api.py:1399-1401` 这类锚点，以及
紧跟文件名的省略形式（`isolated_probe.py:50-51` … `:210` … `:249`）。被引文件在后续轮次里被改写
后，锚点会悄悄失效；本脚本把每条引用抽出来，逐条到基准提交（`--ref`，默认 `HEAD`）的**该提交内容**
上解析，并逐条打印判定：

    hit          目标文件在该提交中存在，且行区间落在该文件行数内
    miss         目标文件在该提交中存在，但行区间越界（锚点写错，须改数）
    undecidable  目标无法从该提交解析（未入库的本机产物、日志、第三方安装副本，或无法判定指向哪个文件）

退出码：全部 hit = 0；出现任何 miss 或 undecidable = 2。

注意：`hit` 只证明「这个行号在该提交的该文件里存在」，**不**证明被引内容与正文描述一致；
判定只做**行区间是否落在该文件行数内**这一项机械检查，因此**发现不了**偏一、串行（指向同文件内
另一处相近内容）或跨文件错位——「全命中／低未命中数」不是锚点正确的充分证据，语义一致必须人工比对
（`--quiet` 之外的默认输出会带出被引行原文供比对）。

只读：只调用 `git cat-file` 读提交内容，不读工作树里的被引文件、不写任何文件。

用法：
    python ai-gateway/tools/handoff_anchor_audit.py --um-handoffs --ref 8581e759
    python ai-gateway/tools/handoff_anchor_audit.py --doc handoffs/<name>.md
    python ai-gateway/tools/handoff_anchor_audit.py --um-handoffs --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------- 已知路径表

#: handoffs/ 下的两份 UM handoff（`--um-handoffs` 的审计对象）
UM_HANDOFFS = (
    "handoffs/handoff-ai-gateway-UM上游流中断定位-20260926.md",
    "handoffs/handoff-ai-gateway-UM断流归因决策-20260926.md",
)

#: 文中以短名指代的其它 handoff（`本文`、`A2 handoff`、`恢复可用 handoff` …）
HANDOFF_ALIASES = {
    "a2": "handoffs/handoff-ai-gateway-UM上游流中断定位-20260926.md",
    "attr": "handoffs/handoff-ai-gateway-UM断流归因决策-20260926.md",
    "zcode": "handoffs/handoff-ai-gateway-ZCode-UM子任务恢复可用-20260926.md",
    "review": "handoffs/handoff-ai-gateway-ZCode子代理工具流中断审查-20260925.md",
}

#: 只写短名的引用按此解析。刻意不按工作树搜索：被引文件可能正被另一条线改写，
#: 基准是提交内容，不是工作树现状。
BARE_PATHS = {
    "chat2api.py": "ai-gateway/chat2api/chat2api.py",
    "compare_driver.py": "ai-gateway/tools/um-a2/compare_driver.py",
    "isolated_probe.py": "ai-gateway/tools/um-a2/isolated_probe.py",
    "driver_fixture_check.py": "ai-gateway/tools/um-a2/driver_fixture_check.py",
    "collector_fixture_check.py": "ai-gateway/tools/um-a2/collector_fixture_check.py",
    "env_child_verify.py": "ai-gateway/tools/um-a2/env_child_verify.py",
    "gate_mutation_check.py": "ai-gateway/tools/um-a2/gate_mutation_check.py",
    "probe.py": "output/um-a2-20260926/probe.py",
    "console/share_production.py": "ai-gateway/console/share_production.py",
    "test_chat2api_stream.py": "ai-gateway/tests/test_chat2api_stream.py",
    "test_chat2api_endpoints.py": "ai-gateway/tests/test_chat2api_endpoints.py",
    "README.md": "ai-gateway/README.md",
    # 第三方安装副本，不在仓库内；显式给出以便判定为 undecidable 而不是「短名未知」
    "_context_manager.py": "playwright/sync_api/_context_manager.py",
}

#: 文件名 token：必须带扩展名，允许 `chat2api.log.1` 这类轮转后缀
FILE_TOKEN = (
    r"[A-Za-z0-9_][\w./\\-]*\.(?:py|md|json|log(?:\.\d+)?|txt|bat|ps1|ya?ml|toml|cfg|ini|html|css|ts|tsx|js)"
)

EXPLICIT_RE = re.compile(
    r"(?P<path>" + FILE_TOKEN + r")\s*:\s*(?P<start>\d+)(?:\s*[-\u2013\u2014/]\s*(?P<end>\d+))?"
)
BARE_RE = re.compile(
    r"(?<![\w:./\\-]):(?P<start>\d+)(?:\s*[-\u2013\u2014/]\s*(?P<end>\d+))?"
)

#: 短名 handoff 指代；按出现位置与显式文件名一起参与「最近先行目标」判定
ALIAS_PATTERNS = (
    (re.compile(r"(?:ZCode-UM|恢复可用|父)\s*handoff"), "zcode"),
    (re.compile(r"(?:断流)?归因决策\s*handoff"), "attr"),
    (re.compile(r"A2\s*(?:handoff|状态台账)"), "a2"),
    (re.compile(r"旧审查\s*handoff"), "review"),
    (re.compile(r"(?:本文|本文件|本\s*handoff)"), "self"),
)

#: 端口/监听等上下文里的 `:8000` 不是行号引用，抽取时排除。
#: 只对 4 位以上的数字生效：行号区间（如 `:128-129`）也可能出现在含「端口」的行里，不能一并排除。
PORT_CONTEXT = ("端口", "port", "PORT", "LISTENING", "netstat", "监听", "PID")
PORT_CONTEXT_WINDOW = 24


@dataclass
class Ref:
    doc: str
    doc_line: int
    raw: str
    target: str | None
    target_how: str
    start: int
    end: int
    verdict: str = ""
    detail: str = ""
    excerpt: str = ""


def resolve_target(token: str, doc_rel: str) -> tuple[str | None, str]:
    """把文件 token 解析成基准提交里的仓库相对路径。"""
    tok = token.replace("\\", "/")
    while tok.startswith("./"):
        tok = tok[2:]
    if "/" in tok:
        return tok, "explicit-path"
    if tok in BARE_PATHS:
        return BARE_PATHS[tok], "bare-name"
    if tok == Path(doc_rel).name:
        return doc_rel, "doc-self"
    return None, "unknown-name"


def collect_refs(doc_rel: str, text: str) -> tuple[list[Ref], list[tuple[int, str]]]:
    """抽出全部行号引用；返回 (引用列表, 被排除的疑似端口引用)。"""
    refs: list[Ref] = []
    skipped: list[tuple[int, str]] = []
    carried: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        explicit = list(EXPLICIT_RE.finditer(line))
        covered = [m.span() for m in explicit]
        bare = []
        for m in BARE_RE.finditer(line):
            if any(s <= m.start() < e for s, e in covered):
                continue
            # 端口号多为 4 位以上；行号区间常是 2-4 位，即使同一行提到「端口」也保留
            number = m.group("start")
            if len(number) >= 4 and any(
                marker in line[max(0, m.start() - PORT_CONTEXT_WINDOW):m.start()]
                for marker in PORT_CONTEXT
            ):
                skipped.append((lineno, m.group(0)))
                continue
            bare.append(m)

        events: list[tuple[int, str | None, str]] = []
        for m in explicit:
            target, how = resolve_target(m.group("path"), doc_rel)
            events.append((m.start(), target, how))
        for pattern, key in ALIAS_PATTERNS:
            for m in pattern.finditer(line):
                target = doc_rel if key == "self" else HANDOFF_ALIASES[key]
                events.append((m.start(), target, f"alias:{key}"))
        events.sort(key=lambda e: e[0])

        def preceding(pos: int) -> tuple[str | None, str]:
            best: tuple[str | None, str] | None = None
            for start, target, how in events:
                if start < pos:
                    best = (target, how)
                else:
                    break
            return best if best is not None else (carried, "carried")

        for m in explicit:
            target, how = resolve_target(m.group("path"), doc_rel)
            refs.append(
                Ref(
                    doc=doc_rel,
                    doc_line=lineno,
                    raw=m.group(0),
                    target=target,
                    target_how=how,
                    start=int(m.group("start")),
                    end=int(m.group("end") or m.group("start")),
                )
            )
        for m in bare:
            target, how = preceding(m.start())
            refs.append(
                Ref(
                    doc=doc_rel,
                    doc_line=lineno,
                    raw=m.group(0),
                    target=target,
                    target_how=how,
                    start=int(m.group("start")),
                    end=int(m.group("end") or m.group("start")),
                )
            )
        if events:
            carried = events[-1][1] or carried
    return refs, skipped


class Baseline:
    """按 `git cat-file` 读某个提交里的文件内容；只读，不碰工作树。"""

    def __init__(self, ref: str, git_root: Path, project_dir: Path) -> None:
        self.ref = ref
        self.git_root = git_root
        self.project_dir = project_dir
        self._cache: dict[str, list[str] | None] = {}

    def git_path(self, rel_path: str) -> str | None:
        """项目目录相对路径 → 基准提交里的仓库相对路径；越出仓库时返回 None。"""
        try:
            return (self.project_dir / rel_path).resolve().relative_to(self.git_root.resolve()).as_posix()
        except ValueError:
            return None

    def lines(self, rel_path: str) -> list[str] | None:
        """该文件在基准提交里的行；文件不在基准中时返回 None。"""
        if rel_path not in self._cache:
            git_path = self.git_path(rel_path)
            if git_path is None:
                self._cache[rel_path] = None
                return None
            try:
                out = subprocess.run(
                    ["git", "-C", str(self.git_root), "cat-file", "blob", f"{self.ref}:{git_path}"],
                    check=True,
                    capture_output=True,
                )
                self._cache[rel_path] = out.stdout.decode("utf-8", errors="replace").splitlines()
            except (subprocess.CalledProcessError, FileNotFoundError):
                self._cache[rel_path] = None
        return self._cache[rel_path]


def classify(ref: Ref, baseline: Baseline) -> Ref:
    if ref.target is None:
        ref.verdict = "undecidable"
        ref.detail = "无法判定指向哪个文件（短名未知）"
        return ref
    lines = baseline.lines(ref.target)
    if lines is None:
        ref.verdict = "undecidable"
        ref.detail = f"基准 {baseline.ref} 中无此文件（本机未跟踪产物／日志／第三方副本）"
        return ref
    if ref.start < 1 or ref.end < ref.start:
        ref.verdict = "miss"
        ref.detail = f"行区间非法（文件共 {len(lines)} 行）"
        return ref
    if ref.end > len(lines):
        ref.verdict = "miss"
        ref.detail = f"行区间越界：末行 {ref.end} > 文件 {len(lines)} 行"
        return ref
    ref.verdict = "hit"
    ref.detail = f"文件共 {len(lines)} 行"
    head = lines[ref.start - 1].strip()
    tail = lines[ref.end - 1].strip()
    excerpt = head if ref.end == ref.start else f"{head}  …  {tail}"
    ref.excerpt = excerpt
    return ref


def git_root_of(start: Path) -> Path:
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def read_doc(doc_rel: str, project_dir: Path) -> str:
    return (project_dir / doc_rel).read_text(encoding="utf-8", errors="replace")


def audit(docs: list[str], ref: str, project_dir: Path, git_root: Path) -> tuple[list[Ref], list[tuple[str, int, str]]]:
    baseline = Baseline(ref, git_root, project_dir)
    refs: list[Ref] = []
    skipped: list[tuple[str, int, str]] = []
    for doc_rel in docs:
        found, dropped = collect_refs(doc_rel, read_doc(doc_rel, project_dir))
        refs.extend(classify(r, baseline) for r in found)
        skipped.extend((doc_rel, n, raw) for n, raw in dropped)
    return refs, skipped


def render(refs: list[Ref], quiet: bool, max_excerpt: int) -> str:
    out: list[str] = []
    for r in sorted(refs, key=lambda r: (r.doc, r.doc_line, r.start)):
        where = f"{r.doc}:{r.doc_line}"
        target = r.target or "?"
        span = f"{r.start}" if r.start == r.end else f"{r.start}-{r.end}"
        line = f"[{r.verdict:<11}] {where}  {r.raw}  ->  {target}:{span} ({r.target_how}) {r.detail}"
        if not quiet and r.excerpt:
            line += f"\n{' ' * 13}被引原文：{r.excerpt[:max_excerpt]}"
        out.append(line)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--um-handoffs", action="store_true", help="审计 handoffs/ 下的两份 UM handoff")
    scope.add_argument("--doc", action="append", metavar="PATH", help="审计指定 handoff（项目目录相对路径，可重复）")
    parser.add_argument("--ref", default="HEAD", help="基准提交（默认 HEAD）")
    parser.add_argument("--repo", default=None, help="Git 根目录（默认按脚本位置向上探测）")
    parser.add_argument("--project", default=None, help="项目目录（默认按脚本位置推断）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只打印判定行，不打印被引原文")
    parser.add_argument("--max-excerpt", type=int, default=120, help="被引原文截断长度（默认 120 字符）")
    args = parser.parse_args(argv)

    project_dir = Path(args.project) if args.project else Path(__file__).resolve().parents[2]
    git_root = Path(args.repo) if args.repo else git_root_of(project_dir)

    docs = list(UM_HANDOFFS) if args.um_handoffs else [d.replace("\\", "/").lstrip("./") for d in args.doc]

    missing_docs = [d for d in docs if not (project_dir / d).is_file()]
    if missing_docs:
        for d in missing_docs:
            print(f"[error] 找不到 handoff：{d}", file=sys.stderr)
        return 2

    refs, skipped = audit(docs, args.ref, project_dir, git_root)
    counts = {v: sum(1 for r in refs if r.verdict == v) for v in ("hit", "miss", "undecidable")}

    if args.json:
        print(
            json.dumps(
                {
                    "ref": args.ref,
                    "docs": docs,
                    "counts": counts,
                    "skipped_port_like": [
                        {"doc": d, "doc_line": n, "raw": raw} for d, n, raw in skipped
                    ],
                    "refs": [asdict(r) for r in refs],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"基准提交：{args.ref}（只读 git cat-file，不读工作树）")
        print(f"审计对象：{'、'.join(docs)}")
        print()
        print(render(refs, args.quiet, args.max_excerpt))
        print()
        print(
            f"合计 {len(refs)} 条引用：hit={counts['hit']} "
            f"miss={counts['miss']} undecidable={counts['undecidable']}"
        )
        if skipped:
            shown = "、".join(f"{d.split('/')[-1]}:{n} {raw}" for d, n, raw in skipped)
            print(f"另排除 {len(skipped)} 处端口／监听上下文里的 `:数字`（非行号引用）：{shown}")
        print(
            "hit 只表示行区间在该提交的该文件内存在；判定不做语义比对，"
            "因此偏一、串行与跨文件错位都发现不了，被引内容是否与正文描述一致须人工比对。"
        )

    return 0 if counts["miss"] == 0 and counts["undecidable"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
