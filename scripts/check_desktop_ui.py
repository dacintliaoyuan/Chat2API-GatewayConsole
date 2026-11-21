#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guard the desktop UI's visual rules that a screenshot review caught.

The Slint UI is not covered by `impeccable detect` (web only). These source
checks are the mechanical subset of the design rules:

- icons come from Lucide, not hand-drawn paths;
- keyboard focus never uses a blue outline;
- field text is vertically centred and the segmented selection slides;
- page navigation uses the cross-fade, not a direct cut;
- the search affordance is an icon, not the Chinese word 搜索.

Exit codes: 0 clean; 1 rule violated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "ui"


def read(name: str) -> str:
    path = UI / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"{path.relative_to(ROOT)}: 无法读取（{error}）")


def main() -> int:
    main_ui = read("main.slint")
    if "navigate-to" not in main_ui:
        # The pre-Magpie layout is still the only UI on this branch; the rules
        # below describe the desktop/122 design and are checked where it exists.
        print("desktop UI is the pre-Magpie layout; skipping UI checks", file=sys.stderr)
        return 0
    icons = read("icons.slint")
    theme = read("theme.slint")

    problems: list[str] = []

    def require(name: str, text: str, pattern: str, reason: str) -> None:
        if not re.search(pattern, text):
            problems.append(f"{name}: {reason}")

    def forbid(name: str, text: str, pattern: str, reason: str) -> None:
        match = re.search(pattern, text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{name}:{line}: {reason}")

    # Third-party icons: the map must load Lucide SVGs, never draw a Path.
    require(
        "desktop/ui/icons.slint",
        icons,
        r"@image-url\(\"icons/",
        "图标必须引用 desktop/ui/icons/ 下的第三方 SVG",
    )
    require(
        "desktop/ui/icons.slint",
        icons,
        r"Lucide",
        "图标来源必须标为 Lucide（ISC）",
    )
    forbid(
        "desktop/ui/icons.slint",
        icons,
        r"\bPath\s*\{",
        "不要手绘图标 Path；改用第三方 Lucide SVG",
    )
    license_path = UI / "icons" / "LICENSE"
    if not license_path.is_file():
        problems.append("desktop/ui/icons/LICENSE: 缺少第三方图标许可证")
    svg_count = len(list((UI / "icons").glob("*.svg"))) if (UI / "icons").is_dir() else 0
    if svg_count < 10:
        problems.append(
            f"desktop/ui/icons/: 第三方 SVG 只有 {svg_count} 个，至少需要 10 个"
        )

    # Focus must be a background or a neutral edge, never a blue outline.
    forbid(
        "desktop/ui/theme.slint",
        theme,
        r"border-color:\s*focus\.has-focus\s*\?\s*Theme\.accent",
        "键盘焦点不要用蓝色描边；改用背景或 Theme.edge-strong",
    )
    forbid(
        "desktop/ui/theme.slint",
        theme,
        r"border-width:\s*focus\.has-focus\s*\?\s*2px",
        "键盘焦点不要用 2px 边框；改用背景/中性边框",
    )
    require(
        "desktop/ui/theme.slint",
        theme,
        r"selection\s*:=\s*Rectangle",
        "分段控件必须有滑动的选中底块",
    )
    require(
        "desktop/ui/theme.slint",
        theme,
        r"animate\s+x,\s*width",
        "分段选中底块必须做 x/width 动效",
    )
    require(
        "desktop/ui/theme.slint",
        theme,
        r"vertical-alignment:\s*center",
        "字段文字必须垂直居中",
    )
    require(
        "desktop/ui/theme.slint",
        theme,
        r"colorize:\s*root\.tint",
        "AppIcon 必须用 colorize 着色第三方 SVG",
    )

    # Page switch must cross-fade, not cut.
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"public\s+function\s+navigate-to",
        "页面切换必须走 navigate-to 的淡出/淡入",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"page-out-timer\s*:=\s*Timer",
        "页面切换必须有 page-out-timer",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"transform-rotation:",
        "实例管理的 chevron 必须做旋转动效",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"clickable:\s*true",
        "实例管理整卡必须可点",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"activated\s*=>",
        "实例管理整卡必须绑定展开回调",
    )

    # Search is an icon plus a noun placeholder, not the Chinese action word.
    forbid(
        "desktop/ui/main.slint",
        main_ui,
        r"[\"']搜索",
        "不要显示「搜索」类操作文字；用搜索图标 + 关键词 placeholder",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"icon:\s*\"search\"",
        "搜索框和查询按钮必须用 search 图标",
    )
    require(
        "desktop/ui/main.slint",
        main_ui,
        r"icon-only:\s*true",
        "查询/刷新/分页等操作按钮必须用图标",
    )

    if problems:
        print("desktop UI rules violated:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "规则见 CODING_STANDARDS.md §桌面 UI；视觉复核用 "
            "output/impeccable-ui-review 的截图。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
