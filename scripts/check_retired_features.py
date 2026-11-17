#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终退役功能静态审计（Ticket #35）。

扫描本仓库的生产代码、测试、夹具、文档与脚本；退役术语、动作、端口或
模块出现在 allowlist 之外即失败。allowlist 只放两类：

* 追溯性文本：Git 历史说明、ADR、service-worker retirement、
  appearance retired preferences；
* 显式列出的“断言退役项不存在”测试与审计脚本自身。新增这类测试必须
  同时登记到 ``ALLOWLIST``，否则审计失败。

退出码：0 = 干净；1 = 发现 allowlist 之外的退役引用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- allowlist
# 逐文件列出，不使用通配符。新增断言“退役项不存在”的测试必须登记到这里。
ALLOWLIST = frozenset({
    # 追溯性文本（Git 历史 / ADR / service-worker / appearance）。
    "docs/adr/0001-one-frontend-two-consoles-build-time-role.md",
    "docs/share-chain-map.md",
    "SHARING.md",
    "web/public/sw.js",
    "web/src/shell/appearance.ts",
    "web/scripts/check-log-presentation.mjs",

    # 断言退役项不存在的测试与检查。
    "scripts/check_retired_features.py",
    "tests/test_retired_features.py",
    "tests/test_console_adapters_policy.py",
    "tests/test_console_api.py",
    "tests/test_console_log_reader.py",
    "tests/test_console_model_actions.py",
    "tests/test_console_share_production.py",
    "tests/test_console_speedtest.py",
    "tests/test_console_stats.py",
    "tests/test_gateway_connection_info.py",
    "tests/test_model_catalog_capabilities.py",
    "tests/test_model_config_adapters.py",
    "tests/test_model_context_probe.py",
    "tests/test_model_reconcile.py",
    "tests/test_model_settings_http_contract.py",
    "tests/test_model_settings_revision.py",
    "tests/test_model_sync.py",
    "tests/test_model_sync_core.py",
    "tests/test_model_sync_provider_registration.py",
    "tests/r19/local_fixture.py",
    "tests/test_model_sync_store_merge.py",
    "tests/test_share_dispatch.py",
    "tests/test_share_metering.py",
    "tests/test_share_public_supervision.py",
    "tests/test_share_revoke_state.py",
    "tests/test_share_usage_results.py",
    "tests/test_um_stats_read_surface.py",
    "tests/test_usage_service.py",
    "tests/test_zcode_config_tools.py",
    "tests/test_zcode_file_lock.py",
    "tests/test_zcode_hot_sync.py",
    "tests/test_check_port_bindings.py",
    "tests/test_cloud_role_assembly.py",
    "tests/test_magpie_prices.py",
    "tests/test_r19_local_isolation.py",
    "tests/test_r19_shared_boundary.py",
    "tests/test_r19_shared_pricing.py",
    "tests/test_r19_stats_env.py",
    "tests/test_round6_price_version_ledger.py",
    "tests/test_usage_price_versions.py",
    "web/scripts/check-um-views.mjs",
    "web/scripts/console-gate/um.mjs",
    "web/scripts/backend-contract/check-generated.mjs",
    "local/tests/test_local_composition.py",
})

# ---------------------------------------------------------------- deny list
# (name, compiled pattern, description). Case-insensitive where the retired
# surface is named case-insensitively; ``8110`` excludes the ``U+8110`` font
# codepoint and the retained ``28110`` process identity.
DENY_RULES = (
    ("workbench", re.compile(r"workbench", re.IGNORECASE),
     "退役的 workbench 逃生口"),
    ("speedtest", re.compile(r"speedtest", re.IGNORECASE),
     "退役的测速"),
    ("qoder action", re.compile(r"\bqoder\.|\bqoderglobal\.|qoder-global", re.IGNORECASE),
     "退役的 Qoder 动作"),
    ("cliproxy action", re.compile(r"\bcliproxy\.", re.IGNORECASE),
     "退役的 CLIProxy 动作"),
    ("m365 action", re.compile(r"\bm365\.", re.IGNORECASE),
     "退役的 M365 动作"),
    ("qoderQuota", re.compile(r"qoderQuota"),
     "退役的 Qoder 配额读面"),
    ("zcode", re.compile(r"zcode", re.IGNORECASE),
     "退役的 ZCode 热同步与消费方"),
    ("8110", re.compile(r"(?<!\+)\b8110\b"),
     "退役的 8110 专用后端端口"),
    ("share.backend_", re.compile(r"share\.backend_"),
     "退役的 8110 分享动作"),
    ("share.chain_", re.compile(r"share\.chain_"),
     "退役的整链动作"),
    ("model.sync_", re.compile(r"model\.sync_"),
     "退役的模型同步动作"),
    ("model.settings_", re.compile(r"model\.settings_"),
     "退役的模型设置动作"),
    ("model.task_", re.compile(r"model\.task_"),
     "退役的模型任务动作"),
    ("pricing_rules", re.compile(r"pricing_rules"),
     "退役的价目规则模块"),
    ("magpie_prices", re.compile(r"magpie_prices"),
     "退役的 Magpie 价格同步模块"),
    ("price env", re.compile(
        r"USAGE_PRICE_FILE|CHAT2API_PRICE_TABLE_FILE"),
     "退役的价格环境变量"),
    ("public_supervision", re.compile(r"public_supervision"),
     "退役的旧 Windows 公网监督模块"),
    ("share_dispatch", re.compile(r"share_dispatch"),
     "退役的 8110 预发送调度模块"),
    ("backend_supervision", re.compile(r"backend_supervision"),
     "退役的 8110 监督模块"),
    ("consumer helpers", re.compile(r"collect_consumers|KNOWN_CONSUMERS"),
     "退役的消费方收集函数"),
    ("retired services gate", re.compile(
        r"RETIRED_SERVICES|RetiredServiceError"),
     "退役的服务 gate"),
    ("old share entry", re.compile(
        r"Recover-ShareControl\.(?:ps1|bat)|start_um_share|"
        r"watch-share-backend|share-control-recovery"),
     "旧 Windows 分享链入口"),
    ("share_admin bat", re.compile(r"share_admin\.bat"),
     "旧 share 管理 bat（share/admin.py 保留）"),
    ("retired source ledger env", re.compile(
        r"M365_USAGE_LOG|QODER_USAGE_LOG"),
     "退役来源的账本环境变量"),
    ("dsh-cost-meter", re.compile(r"dsh-cost-meter"),
     "退役的 DSH 费用插件口径"),
    ("retired cost fields", re.compile(
        r"knownCostUsd|estimatedCostUsd|costCny|knownCostCny|"
        r"estimatedCostCny|priceVersions|priceVersion|pricedRequests|"
        r"unpricedRequests|pricingUnavailableRequests|costComplete|costUsd|"
        r"cacheUnknownPricedRequests"),
     "退役的费用/价格字段"),
    ("retired usage wording", re.compile(r"费用统计"),
     "退役的费用统计命名"),
    ("retired share actions", re.compile(
        r"share\.(?:start|stop|estop|retry_stop)"),
     "退役的旧 Windows 本机分享动作"),
    ("cloud dispatch", re.compile(r"云端发送调度"),
     "退役的云端预发送调度"),
)

SCAN_DIRS = (
    "app",
    "chat2api",
    "console",
    "local",
    "model_sync",
    "share",
    "shared",
    "tests",
    "docs",
    "scripts",
    "um-cloud-deploy",
    "lib",
    "web/src",
    "web/public",
    "web/scripts",
)
SCAN_ROOT_FILES = (
    "usagesrv.py",
    "check_port_bindings.py",
    "GLOSSARY.md",
    "README.md",
    "AGENTS.md",
    "web/index.html",
    "SHARING.md",
)
ROOT_SUFFIXES = (".ps1", ".bat", ".vbs")
IGNORED_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "output", "release",
    ".worktrees", "data", "profile", ".edge-profile", "tmp",
})
IGNORED_RELATIVE_DIRS = frozenset({
    "web/dist",
    "web/dist-cloud",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".mjs", ".js", ".ts", ".json", ".md", ".ps1", ".bat",
    ".vbs", ".sh", ".html", ".css", ".txt", ".example", ".yml", ".yaml",
    ".toml", ".cfg", ".ini",
})


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_files(root: Path):
    """Yield the scanned text files in the ticket's scope."""
    seen: set[Path] = set()
    for relative in SCAN_DIRS:
        base = root / relative
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = _relative(path, root)
            if any(part in IGNORED_DIR_NAMES for part in path.relative_to(root).parts):
                continue
            if any(rel == ignored or rel.startswith(ignored + "/")
                   for ignored in IGNORED_RELATIVE_DIRS):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path
    for relative in SCAN_ROOT_FILES:
        path = root / relative
        if path.is_file() and path not in seen:
            seen.add(path)
            yield path
    for path in sorted(root.iterdir()):
        if (path.is_file() and path.suffix.lower() in ROOT_SUFFIXES
                and path not in seen):
            seen.add(path)
            yield path


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """Read a scanned file, returning a readable reason when it cannot be audited.

    A file we cannot read is not evidence of a clean tree: the audit must fail
    closed and name the file instead of silently skipping its retired references.
    """
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, "读取失败（%s）：%s" % (exc.__class__.__name__, exc)
    except UnicodeDecodeError as exc:
        return None, "不是 UTF-8 文本（%s，字节偏移 %d）" % (
            exc.__class__.__name__, exc.start)


def find_violations(root: Path = ROOT, allowlist=ALLOWLIST) -> list[str]:
    """Return every retired-surface hit outside the explicit allowlist."""
    allowed = frozenset(allowlist)
    violations: list[str] = []
    for path in iter_files(root):
        relative = _relative(path, root)
        if relative in allowed:
            continue
        text, unreadable = _read_text(path)
        if unreadable is not None:
            violations.append(
                "%s: [unreadable] 无法审计该文件：%s" % (relative, unreadable))
            continue
        assert text is not None
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern, description in DENY_RULES:
                if not pattern.search(line):
                    continue
                hint = ""
                if "/tests/" in f"/{relative}" or relative.startswith("tests/"):
                    hint = "；若这是新增的断言不存在测试，请登记到 ALLOWLIST"
                violations.append(
                    "%s:%d: [%s] %s：%s%s"
                    % (relative, number, name, description, line.strip()[:120],
                       hint))
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        for violation in violations:
            print("FAIL " + violation, file=sys.stderr)
        print("FAIL retired features: %d 处 allowlist 之外的退役引用"
              % len(violations), file=sys.stderr)
        return 1
    print("PASS retired features")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
