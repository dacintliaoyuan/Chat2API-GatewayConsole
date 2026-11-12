# -*- coding: utf-8 -*-
"""动作门 §②「生成归因密钥 + ACL」的可核对实现：默认 dry-run，写盘须显式 --apply。

材料来源（本工具**不复制**它们的字面量，只引用）：

* ``output/ai-gw-round6-20260927/metering-action-gate.md`` §②（生成密钥与 ACL，
  **须提权**）与 §⑤.2（密钥回退判据：回退只动文件、不动目录）；
* ``SHARING.md``「生成（维护者本机执行）」段（:51-100 的 PowerShell
  原文）与「轮换」段（:150-170）；
* ``ai-gateway/console/share_production.py`` 的 ``SHARE_ATTRIBUTION_KEY_NAME``
  （:845）与 ``share_attribution_key_path()``（:864-880）——**唯一的**生产路径解析器。

三种模式（默认 dry-run；任何写盘动作都必须显式开关）：

* **默认（无开关）= dry-run**：只打印将要做的每一步与判据，并核对前置条件；
  **不写任何文件、不碰 ACL、不读目标文件**。
  退出码：0 = 前置条件全部满足；2 = 任一不满足或**不可判定**
  （读不到 ≠ 不存在，见动作门 §1.5）。
* ``--apply`` = 真实生成：写文件（无 BOM、单行、不追加结尾换行）→ 写后读回
  （字节数 / SHA-256 / 逐字节内容）→ ``icacls``（``/inheritance:r`` + 四个授权项）
  → ACL 断言（两身份 Allow 读 ACE）→ 自读（**只打印长度**）。
  **任一步不符即抛 ``KeygenAbort`` 且不继续**；前置条件不全部满足时拒绝执行。
  最后一步自读跑的是**当前（--apply 下通常是提升的）身份**，所以 ``--apply`` 只报
  「本地步骤全部通过」，**不报全绿**：动作门 §2.2 ⑥ 要求的「非提升 dacint 提示符
  自读」本工具不执行（命令打印出来交维护者）。
  **本轮（执行轮 8）未执行**——只有夹具覆盖其判定函数。
* ``--rehearse-rollback`` = 回退演练：在**临时目录**里用同一套 ACL 配方造一个假密钥
  文件、核对 ACL 断言通过，然后删除该文件并核对**父目录 ACL 未变**
  （回退只动文件、不动目录；动作门 §⑤.2）。
  退出码 0 = 全部通过；2 = 任一不通过或不可判定。**这是本工具唯一会写盘的动作**，
  且只在临时目录里（临时目录在结束时删除）。

硬边界（本工具的全部承诺）：

* **绝不打印密钥内容**：只打印 ``strip()`` 后的长度与 ``hash_equal`` 这类布尔判据；
* **绝不写 ``data/``**（SHARING.md:22-23：该目录对 ``DAICNT\\um_share``
  是显式拒绝 ACE，放那里 8100 读不到 → 静默退化成未启用）；
* **绝不改 ``C:\\ProgramData\\UMShareRun`` 以外的任何生产路径**：``--apply`` 只允许目标
  落在该 RunRoot 之下，且拒绝 ``RunRoot\\run\\live``（每次部署删除重建，SHARING.md:24-25）；
* **不重启任何服务、不发模型请求、不创建分享 Key、不碰前端、不碰私人 8000**；
* 判定逻辑（前置条件、密钥形状、写后读回、ACL 断言、回退演练）都是**纯函数**：
  字典进、字典出，与 IO 分离；夹具测试（``tests/test_um_share_keygen.py``）
  直接喂字典验同一判定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

# 与 ai-gateway 产品代码同仓：把 ai-gateway 目录加进 sys.path，才能用产品代码的
# 路径解析器（本工具**不复制**它的默认 RunRoot 字面量）。
AI_GATEWAY_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if AI_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, AI_GATEWAY_DIR)

# 生产 RunRoot = deploy-elevated.ps1 的 -RunRoot 默认值（console/share_production.py:843）。
# --apply 只允许写这个根之下（动作门 §2.4 + 本任务边界：绝不改它以外的生产路径）。
ALLOWED_PRODUCTION_ROOT = r"C:\ProgramData\UMShareRun"
# 仓库内 data/：对 DAICNT\um_share 是显式拒绝 ACE（SHARING.md:22-23）。
REPO_DATA_DIR = os.path.join(AI_GATEWAY_DIR, "data")
# RunRoot\run\live：每次部署删除重建（SHARING.md:24-25），密钥会随之消失。
RUN_LIVE_DIR = os.path.join(ALLOWED_PRODUCTION_ROOT, "run", "live")

# SHARING.md:80-82 的四个授权项，顺序即 icacls 的参数顺序。
ACL_GRANTS = (
    (r"DAICNT\um_share", "R"),
    (r"DAICNT\dacint", "R"),
    ("SYSTEM", "F"),
    (r"BUILTIN\Administrators", "F"),
)
# 必须断言有 Allow 读 ACE 的两个运行身份（SHARING.md:86-93）。
REQUIRED_READ_IDENTITIES = (r"DAICNT\um_share", r"DAICNT\dacint")
# 必须断言 Allow 完全控制的身份（SHARING.md:95 的留档原文）。
REQUIRED_FULL_CONTROL_IDENTITIES = ("SYSTEM", r"BUILTIN\Administrators")

KEY_BYTES = 32                 # 32 字节系统密码学随机数
KEY_HEX_CHARS = KEY_BYTES * 2    # 32 字节 → 64 个 hex 字符（SHARING.md:41-43）
KEY_MIN_CHARS = 32              # 两端 ATTRIBUTION_KEY_MIN_BYTES（SHARING.md:34-36）

# FileSystemRights.Read = 0x20089（配方 ⑤ 用 ``-band Read -ne 0`` 判定）。
FS_RIGHTS_READ = 0x00020089
# FileSystemRights.FullControl = 0x1F01FF。
FS_RIGHTS_FULL_CONTROL = 0x001F01FF

_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
ACL_TIMEOUT_S = 60.0
ICACLS_TIMEOUT_S = 60.0

# 只读 ACL 采集：Get-Acl + 四个授权身份的 SID 解析（配方 ⑤ 的原文口径：按 SID 比较，
# 不是按名字；身份清单由 ACL_GRANTS 派生，避免两处字面量漂移）。目标路径经环境变量
# 传入，避免把路径拼进命令字符串。
_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:UM_KEYGEN_ACL_PATH
$acl = Get-Acl -LiteralPath $path
$aces = @($acl.Access | ForEach-Object {
  $sid = $null; $sidError = $null
  try {
    $sid = $_.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
  } catch { $sidError = $_.Exception.GetType().Name }
  [pscustomobject]@{
    type = $_.AccessControlType.ToString()
    sid = $sid
    sid_error = $sidError
    name = $_.IdentityReference.Value
    rights = [int]$_.FileSystemRights
    inherited = [bool]$_.IsInherited
  }
})
$identities = @{}
foreach ($who in @('%s')) {
  try {
    $identities[$who] = (New-Object System.Security.Principal.NTAccount($who)).Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
  } catch { $identities[$who] = $null }
}
ConvertTo-Json -InputObject @{
  sddl = $acl.Sddl
  owner = $acl.Owner
  inheritance_protected = [bool]$acl.AreAccessRulesProtected
  aces = $aces
  identities = $identities
} -Compress -Depth 5
""" % "', '".join(who for who, _ in ACL_GRANTS)


class KeygenAbort(Exception):
    """任一步不符即中止：绝不「继续试试看」下一步。"""

    def __init__(self, step, reason):
        super().__init__("%s：%s" % (step, reason))
        self.step = step
        self.reason = reason


# ------------------------------------------------------------------ IO 原语
def _item(item_id, title, verdict, detail):
    return {"id": item_id, "title": title, "verdict": verdict, "detail": detail}


def _decode(raw):
    """解码外部命令输出：Windows 上先按 ANSI/OEM 代码页（``mbcs``）再退 UTF-8。

    只用于留档与子串核对（身份名与权限字母都是 ASCII），绝不用于密钥内容。
    """
    candidates = []
    if os.name == "nt":
        candidates.append("mbcs")   # icacls / PowerShell 重定向输出的实际代码页
    candidates.append(locale.getpreferredencoding(False))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run_command(args, timeout=ICACLS_TIMEOUT_S):
    """跑一条外部命令，**如实**带回退出码与输出（绝不把失败折成成功）。"""
    result = {"args": list(args), "returncode": None, "output": None,
              "error": None}
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = "spawn_failed:%s" % type(exc).__name__
        return result
    result["returncode"] = proc.returncode
    result["output"] = _decode(proc.stdout or b"") + _decode(proc.stderr or b"")
    return result


def _run_powershell(script, env_extra=None, timeout=ACL_TIMEOUT_S):
    """跑一段只读 PowerShell；失败返回 ``None``（读不到 ≠ 不存在）。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _decode(proc.stdout or b"")


# ------------------------------------------------------------------ 纯判定层
def normalize_path(path):
    """纯函数：Windows 路径归一（大小写不敏感 + 绝对化）。"""
    return os.path.normcase(os.path.abspath(path))


def path_is_under(path, root):
    """纯函数：``path`` 是否在 ``root`` 之下（含 ``root`` 自身）；跨盘符返回 False。"""
    if not isinstance(path, str) or not isinstance(root, str):
        return False
    if not path or not root:
        return False
    try:
        candidate = normalize_path(path)
        base = normalize_path(root)
    except (TypeError, ValueError):
        return False
    if candidate == base:
        return True
    return candidate.startswith(base.rstrip("\\/") + os.sep)


def render_key_text(key_bytes):
    """纯函数：32 字节 → 64 个 hex 字符（SHARING.md:54-59 的等价实现）。

    只用系统密码学随机源产出的字节；``Get-Random`` 之类不保证密码学安全随机的
    源**不得**用于生成密钥（SHARING.md:38-40）。
    """
    if not isinstance(key_bytes, (bytes, bytearray)):
        raise ValueError("key_bytes must be bytes")
    if len(key_bytes) != KEY_BYTES:
        raise ValueError("key_bytes must be %d bytes, got %d"
                         % (KEY_BYTES, len(key_bytes)))
    return "".join("%02x" % byte for byte in key_bytes)


def generate_key_text():
    """32 字节系统密码学随机数（``secrets.token_bytes`` = OS CSPRNG）→ 64 hex。"""
    return render_key_text(secrets.token_bytes(KEY_BYTES))


def evaluate_key_text(key_text):
    """纯函数：生成/读回文本的形状判据（长度、hex、两端最小长度）。"""
    items = []
    length = len(key_text) if isinstance(key_text, str) else None
    items.append(_item(
        "key_hex_length", "密钥长度 = 64 个 hex 字符（32 字节）",
        "pass" if length == KEY_HEX_CHARS else "fail",
        "期望 %d、实测 %r（Base64 则 44 字符；<32 字符两端都会退化成未接线）"
        % (KEY_HEX_CHARS, length)))
    is_hex = isinstance(key_text, str) and bool(_HEX64_RE.match(key_text))
    items.append(_item(
        "key_hex_shape", "密钥只含 [0-9a-f] 且单行无空白",
        "pass" if is_hex else "fail",
        "hex64=%r（含 BOM/换行/大写/非 hex 都会在这里被抓住）" % is_hex))
    items.append(_item(
        "key_min_chars", "strip 后长度 ≥ %d（两端 ATTRIBUTION_KEY_MIN_BYTES）"
        % KEY_MIN_CHARS,
        "pass" if isinstance(length, int) and length >= KEY_MIN_CHARS else "fail",
        "strip 后长度=%r" % length))
    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok}


def evaluate_readback(expected_text, observed):
    """纯函数：写后读回的三种不符（动作门 §2.2 ③ / SHARING.md:65-76）。

    ``observed`` 形状::

        {"bytes": int|None, "text": str|None, "sha256": str|None,
         "error": str|None}

    ``text`` 用**不加 BOM** 的 UTF-8 解码（= ``UTF8Encoding($false).GetString``），
    所以多写 BOM 会留下 ``\\ufeff``、多写结尾换行会留下 ``\\n``，两者都会在这里
    被抓住。三种不符分别落在：① 字节数、② SHA-256、③ 逐字节内容。
    """
    expected_bytes = expected_text.encode("utf-8")
    want_hash = hashlib.sha256(expected_bytes).hexdigest()
    items = []

    got_bytes = observed.get("bytes") if isinstance(observed, dict) else None
    if not isinstance(got_bytes, int):
        items.append(_item("readback_bytes", "写后读回：字节数相符", "undecidable",
                          "读不到字节数（error=%r）"
                          % (observed.get("error") if isinstance(observed, dict)
                             else None)))
    else:
        items.append(_item(
            "readback_bytes", "写后读回：字节数相符",
            "pass" if got_bytes == len(expected_bytes) else "fail",
            "期望 %d 字节、实测 %d 字节（多写 BOM 或结尾换行都会在这里被抓住）"
            % (len(expected_bytes), got_bytes)))

    got_hash = observed.get("sha256") if isinstance(observed, dict) else None
    if not isinstance(got_hash, str):
        items.append(_item("readback_sha256", "写后读回：SHA-256 相符",
                          "undecidable", "读不到哈希"))
    else:
        items.append(_item(
            "readback_sha256", "写后读回：SHA-256 相符",
            "pass" if got_hash == want_hash else "fail",
            "hash_equal=%r（只打印布尔判据，绝不打印摘要本身）"
            % (got_hash == want_hash)))

    got_text = observed.get("text") if isinstance(observed, dict) else None
    if not isinstance(got_text, str):
        items.append(_item("readback_content", "写后读回：逐字节内容相符",
                          "undecidable", "读不到文本（error=%r）"
                          % (observed.get("error")
                             if isinstance(observed, dict) else None)))
    else:
        items.append(_item(
            "readback_content", "写后读回：逐字节内容相符",
            "pass" if got_text == expected_text else "fail",
            "content_equal=%r（期望长度 %d、实测长度 %d）"
            % (got_text == expected_text, len(expected_text), len(got_text))))

    stripped_length = len(got_text.strip()) if isinstance(got_text, str) else None
    items.append(_item(
        "readback_min_chars", "写后读回：strip 后长度 ≥ %d" % KEY_MIN_CHARS,
        "pass" if isinstance(stripped_length, int)
        and stripped_length >= KEY_MIN_CHARS
        else ("fail" if isinstance(stripped_length, int) else "undecidable"),
        "strip 后长度=%r" % stripped_length))

    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok}


def _ace_matches(ace, who, sid, mask, full=False):
    """纯函数：某条 ACE 是否满足「Allow + 身份相符 + 权限位包含 mask」。

    身份优先按 **SID** 比较（配方 ⑤ 原文口径）；SID 解析不了才退回按名字比较，
    名字比较用**末段**（``NT AUTHORITY\\SYSTEM`` ↔ ``SYSTEM``），避免因显示名
    前缀不同而误判成「没有读 ACE」。
    """
    if not isinstance(ace, dict):
        return False
    if ace.get("type") != "Allow":
        return False
    rights = ace.get("rights")
    if not isinstance(rights, int):
        return False
    if full:
        if rights & mask != mask:
            return False
    elif rights & mask == 0:
        return False
    ace_sid = ace.get("sid")
    if isinstance(sid, str) and sid and isinstance(ace_sid, str):
        return ace_sid.upper() == sid.upper()
    name = ace.get("name")
    if not isinstance(name, str):
        return False
    tail = name.strip().rsplit("\\", 1)[-1]
    return tail.lower() == who.rsplit("\\", 1)[-1].lower()


def evaluate_acl_assertion(acl):
    """纯函数：ACL 事实 → 逐项判定（配方 ⑤ / 动作门 §2.3 的断言）。

    ``acl`` 形状::

        {"sddl": str|None, "owner": str|None,
         "inheritance_protected": bool|None,
         "aces": [{"type": "Allow"|"Deny", "sid": str|None,
                   "sid_error": str|None, "name": str|None,
                   "rights": int|None, "inherited": bool|None}, ...]|None,
         "identities": {"DAICNT\\um_share": "S-1-5-21-...|None", ...}|None,
         "icacls_output": str|None,
         "error": str|None}

    判定项：① 查询成功；② ``/inheritance:r`` 生效（``AreAccessRulesProtected``）；
    ③ 两身份各有 Allow 读 ACE；④ SYSTEM / Administrators 各有 Allow 完全控制；
    ⑤ ``icacls`` 原文含四个授权项（动作门 §2.3 的留档判据）。
    """
    items = []
    error = acl.get("error") if isinstance(acl, dict) else None
    items.append(_item(
        "acl_query_ok", "ACL 查询成功（Get-Acl 返回可用结果）",
        "pass" if not error else "fail", "error=%r" % error))

    aces = acl.get("aces") if isinstance(acl, dict) else None
    identities = acl.get("identities") if isinstance(acl, dict) else None
    protected = acl.get("inheritance_protected") if isinstance(acl, dict) else None

    if not isinstance(aces, list):
        items.append(_item("acl_inheritance_off", "ACL 已 /inheritance:r 收紧",
                          "undecidable", "读不到 ACE 列表"))
    else:
        items.append(_item(
            "acl_inheritance_off", "ACL 已 /inheritance:r 收紧（AreAccessRulesProtected）",
            "pass" if protected is True else ("fail" if protected is False
                                            else "undecidable"),
            "inheritance_protected=%r（run/ 给 um_share 继承的 RW 若不收紧，"
            "um_share 能改写密钥）" % protected))

    if not isinstance(identities, dict):
        identities = {}
    for who in REQUIRED_READ_IDENTITIES:
        sid = identities.get(who)
        title = "%s 有 Allow 读 ACE（该端读不到密钥 → 未归属 / no_key）" % who
        if not isinstance(aces, list):
            items.append(_item("acl_read_ace:" + who, title, "undecidable",
                              "读不到 ACE 列表"))
            continue
        if not isinstance(sid, str) or not sid:
            items.append(_item(
                "acl_read_ace:" + who, title, "undecidable",
                "身份 SID 解析不了（%s），不能断言该身份有读 ACE"
                % ("identities 缺失" if not isinstance(identities, dict)
                   else "Translate 失败")))
            continue
        found = any(_ace_matches(ace, who, sid, FS_RIGHTS_READ)
                    for ace in aces)
        items.append(_item(
            "acl_read_ace:" + who, title,
            "pass" if found else "fail",
            "sid=%s allow_read_ace=%r aces=%d" % (sid, found, len(aces))))

    for who in REQUIRED_FULL_CONTROL_IDENTITIES:
        sid = identities.get(who)
        title = "%s 有 Allow 完全控制 ACE" % who
        if not isinstance(aces, list):
            items.append(_item("acl_full_control_ace:" + who, title,
                              "undecidable", "读不到 ACE 列表"))
            continue
        found = any(_ace_matches(ace, who, sid if isinstance(sid, str) else "",
                                FS_RIGHTS_FULL_CONTROL, full=True)
                    for ace in aces)
        items.append(_item("acl_full_control_ace:" + who, title,
                           "pass" if found else "fail",
                           "allow_full_control_ace=%r" % found))

    raw = acl.get("icacls_output") if isinstance(acl, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        items.append(_item("acl_icacls_grants", "icacls 原文含四个授权项",
                          "undecidable", "读不到 icacls 输出"))
    else:
        collapsed = re.sub(r"\s+", " ", raw)
        missing = ["%s:(%s)" % (who, rights)
                   for who, rights in ACL_GRANTS
                   if "%s:(%s)" % (who, rights) not in collapsed]
        items.append(_item(
            "acl_icacls_grants", "icacls 原文含四个授权项（留档判据）",
            "pass" if not missing else "fail",
            "missing=%s" % (",".join(sorted(missing)) or "无")))

    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok}


def evaluate_preconditions(facts):
    """纯函数：前置条件事实 → 逐项判定（动作门 §2.1 / §1.5 / §2.4）。

    逐项：

    1. ``target_path_resolved`` —— 生产解析器给出绝对路径，且基名 = ``SHARE_ATTRIBUTION_KEY_NAME``；
    2. ``parent_dir_ready`` —— 目标父目录存在、是目录且可写（**读不到 = 不可判定**，不得折成不存在）；
    3. ``target_absent`` —— 目标文件当前**不存在**（已存在即拒绝：轮换走 SHARING.md「轮换」段）；
    4. ``not_in_data_dir`` —— 目标不在 ``ai-gateway/data`` 下（该目录对 um_share 是显式拒绝 ACE）；
    5. ``under_allowed_root`` —— 目标在 ``C:\\ProgramData\\UMShareRun`` 之下（本工具边界的 fail-closed 守卫）；
    6. ``not_under_run_live`` —— 目标不在 ``run\\live`` 下（每次部署删除重建）；
    7. ``session_elevated`` —— 当前会话已提升（写生产秘密 + 改 RunRoot\\run ACL 须提权）。

    退出码：全部 ``pass`` 才 0；任一 ``fail``/``undecidable`` 都是 2。
    """
    items = []

    target = facts.get("target_path")
    error = facts.get("target_path_error")
    key_name = facts.get("key_name")
    if isinstance(target, str) and target and not error:
        matches = (isinstance(key_name, str)
                   and os.path.basename(target).lower() == key_name.lower())
        items.append(_item(
            "target_path_resolved",
            "生产解析器给出目标路径，且基名 = SHARE_ATTRIBUTION_KEY_NAME",
            "pass" if matches else "fail",
            "target=%r key_name=%r（解析器 = "
            "console_share_production.share_attribution_key_path()）"
            % (target, key_name)))
    else:
        items.append(_item(
            "target_path_resolved",
            "生产解析器给出目标路径，且基名 = SHARE_ATTRIBUTION_KEY_NAME",
            "fail", "解析失败：%r" % (error or "resolver_unavailable")))

    parent = facts.get("parent") if isinstance(facts.get("parent"), dict) else {}
    state = parent.get("state")
    writable = parent.get("writable")
    detail = ("path=%r state=%r writable=%r realpath_differs=%r error=%r"
              % (parent.get("path"), state, writable,
                 parent.get("realpath_differs"), parent.get("error")))
    if state == "dir" and writable is True:
        items.append(_item("parent_dir_ready", "目标父目录存在且可写", "pass",
                          detail))
    elif state in ("missing", "not_a_dir") or writable is False:
        items.append(_item("parent_dir_ready", "目标父目录存在且可写", "fail",
                          detail))
    else:
        items.append(_item(
            "parent_dir_ready", "目标父目录存在且可写", "undecidable",
            detail + "（读不到 ≠ 不存在：动作门 §1.5 明确禁止把不可读当不存在）"))

    target_facts = facts.get("target") if isinstance(facts.get("target"), dict) else {}
    exists = target_facts.get("exists")
    detail = "path=%r exists=%r error=%r" % (target_facts.get("path"), exists,
                                            target_facts.get("error"))
    if exists is False:
        items.append(_item("target_absent", "目标文件当前不存在", "pass", detail))
    elif exists is True:
        items.append(_item(
            "target_absent", "目标文件当前不存在", "fail",
            detail + "（已存在 → 轮换走 SHARING.md「轮换」段，不重复生成："
            "换掉 8100 内存里的旧值会得到全量 bad_signature）"))
    else:
        items.append(_item("target_absent", "目标文件当前不存在", "undecidable",
                          detail + "（读不到 ≠ 不存在）"))

    data_dir = facts.get("data_dir") if isinstance(facts.get("data_dir"), dict) else {}
    under_data = data_dir.get("under")
    detail = "data_dir=%r under=%r" % (data_dir.get("path"), under_data)
    items.append(_item(
        "not_in_data_dir", "目标不在 ai-gateway/data 下",
        "pass" if under_data is False else ("fail" if under_data is True
                                           else "undecidable"),
        detail + ("（该目录对 DAICNT\\um_share 是显式拒绝 ACE：8100 读不到 → "
                  "静默退化成未启用）" if under_data is not False else "")))

    under_root = facts.get("under_allowed_root")
    items.append(_item(
        "under_allowed_root",
        "目标在允许的生产根 %s 之下" % ALLOWED_PRODUCTION_ROOT,
        "pass" if under_root is True else ("fail" if under_root is False
                                          else "undecidable"),
        "under_allowed_root=%r（本工具边界：--apply 绝不改该根以外的任何生产路径）"
        % under_root))

    under_live = facts.get("under_run_live")
    items.append(_item(
        "not_under_run_live", "目标不在 RunRoot\\run\\live 下",
        "pass" if under_live is False else ("fail" if under_live is True
                                           else "undecidable"),
        "under_run_live=%r（run\\live 每次部署删除重建：密钥会随之消失）"
        % under_live))

    elevated = facts.get("elevated")
    items.append(_item(
        "session_elevated", "当前会话已提升（写生产秘密 + 改 RunRoot\\run ACL 须提权）",
        "pass" if elevated is True else ("fail" if elevated is False
                                        else "undecidable"),
        "elevated=%r（非提升 dacint 对 RunRoot 与 RunRoot\\run 都是拒绝访问）"
        % elevated))

    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok, "exit_code": 0 if ok else 2}


def evaluate_rollback_rehearsal(facts):
    """纯函数：回退演练事实 → 逐项判定（动作门 §⑤.2：回退只动文件、不动目录）。

    ``facts`` 形状::

        {"workdir": str, "temp_root": str, "target": str,
         "key_chars": int|None,
         "write": {"ok": bool, "error": str|None},
         "readback": {"ok": bool},
         "acl": {"ok": bool},
         "icacls": {"returncode": int|None, "error": str|None},
         "file_deleted": bool, "file_gone": bool|None,
         "parent_acl_before": str|None, "parent_acl_after": str|None,
         "parent_acl_error": str|None}
    """
    items = []

    workdir = facts.get("workdir")
    temp_root = facts.get("temp_root")
    in_temp = isinstance(workdir, str) and isinstance(temp_root, str) \
        and path_is_under(workdir, temp_root)
    items.append(_item(
        "rehearsal_in_temp_dir", "演练只在系统临时目录里（唯一允许的写盘）",
        "pass" if in_temp else "fail",
        "workdir=%r temp_root=%r" % (workdir, temp_root)))

    write = facts.get("write") if isinstance(facts.get("write"), dict) else {}
    readback = facts.get("readback") if isinstance(facts.get("readback"), dict) else {}
    key_chars = facts.get("key_chars")
    written = write.get("ok") is True and readback.get("ok") is True \
        and isinstance(key_chars, int) and key_chars == KEY_HEX_CHARS
    items.append(_item(
        "rehearsal_key_written",
        "假密钥按同一配方写入且写后读回一致（只给长度）",
        "pass" if written else "fail",
        "key_chars=%r write_ok=%r readback_ok=%r error=%r"
        % (key_chars, write.get("ok"), readback.get("ok"), write.get("error"))))

    icacls = facts.get("icacls") if isinstance(facts.get("icacls"), dict) else {}
    acl = facts.get("acl") if isinstance(facts.get("acl"), dict) else {}
    acl_ok = acl.get("ok") is True and icacls.get("returncode") == 0
    items.append(_item(
        "rehearsal_acl_ok", "同一套 ACL 配方设上且断言通过",
        "pass" if acl_ok else "fail",
        "icacls_rc=%r acl_ok=%r error=%r"
        % (icacls.get("returncode"), acl.get("ok"), icacls.get("error"))))

    deleted = facts.get("file_deleted") is True and facts.get("file_gone") is True
    items.append(_item(
        "rehearsal_file_deleted", "假密钥文件已删除且确实不在",
        "pass" if deleted else "fail",
        "file_deleted=%r file_gone=%r" % (facts.get("file_deleted"),
                                         facts.get("file_gone"))))

    before = facts.get("parent_acl_before")
    after = facts.get("parent_acl_after")
    error = facts.get("parent_acl_error")
    if not isinstance(before, str) or not isinstance(after, str):
        items.append(_item(
            "rehearsal_parent_acl_unchanged", "父目录 ACL 未变（回退只动文件、不动目录）",
            "undecidable", "before=%r after=%r error=%r" % (before, after, error)))
    else:
        items.append(_item(
            "rehearsal_parent_acl_unchanged", "父目录 ACL 未变（回退只动文件、不动目录）",
            "pass" if before == after else "fail",
            "parent_sddl_equal=%r" % (before == after)))

    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok, "exit_code": 0 if ok else 2}


# ------------------------------------------------------------------ 采集层
def is_elevated():
    """当前会话是否提升：True/False；判定不了返回 ``None``（绝不默认 True）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — 判定不了就如实报不可判定
        return None


def key_path():
    """解析两端必须指向的**唯一**密钥文件路径：``(path, error, key_name)``。

    解析器 = 产品代码 ``console_share_production.share_attribution_key_path()``
    （``CONSOLE_SHARE_ATTRIBUTION_KEY_FILE`` > ``CONSOLE_SHARE_RUN_ROOT`` >
    ``DEFAULT_SHARE_RUN_ROOT``）；本工具**不复制**它的字面量。
    """
    try:
        from console import share_production as console_share_production
        path = console_share_production.share_attribution_key_path()
    except Exception as exc:  # noqa: BLE001 — 解析不了就如实报，不猜路径
        return None, "resolver_failed:%s" % type(exc).__name__, None
    if not isinstance(path, str) or not path:
        return None, "resolver_returned_invalid", None
    return path, None, key_name()


def key_name():
    """读取 ``SHARE_ATTRIBUTION_KEY_NAME``（产品常量；读不到返回 ``None``）。"""
    try:
        from console import share_production as console_share_production
        name = console_share_production.SHARE_ATTRIBUTION_KEY_NAME
    except Exception:  # noqa: BLE001 — 读不到就如实报，不猜名字
        return None
    return name if isinstance(name, str) else None


def collect_preconditions(target, target_error=None, key_name=None,
                          elevated=None):
    """只读采集前置条件事实；不写任何文件、不碰 ACL、不读目标文件内容。"""
    parent = os.path.dirname(target) if isinstance(target, str) else None
    facts = {
        "target_path": target,
        "target_path_error": target_error,
        "key_name": key_name,
        "parent": {"path": parent, "state": None, "writable": None,
                   "realpath_differs": None, "error": None},
        "target": {"path": target, "exists": None, "error": None},
        "data_dir": {"path": REPO_DATA_DIR,
                     "under": path_is_under(target, REPO_DATA_DIR)
                     if isinstance(target, str) else None,
                     "error": None},
        "under_allowed_root": path_is_under(target, ALLOWED_PRODUCTION_ROOT)
        if isinstance(target, str) else None,
        "under_run_live": path_is_under(target, RUN_LIVE_DIR)
        if isinstance(target, str) else None,
        "elevated": is_elevated() if elevated is None else elevated,
    }

    if isinstance(parent, str):
        try:
            info = os.stat(parent)
        except FileNotFoundError:
            facts["parent"]["state"] = "missing"
        except PermissionError:
            # 读不到 ≠ 不存在（动作门 §1.5）：非提升 dacint 对 RunRoot\run 正是这一态。
            facts["parent"]["state"] = "unreadable"
            facts["parent"]["error"] = "PermissionError"
        except OSError as exc:
            facts["parent"]["state"] = "unreadable"
            facts["parent"]["error"] = type(exc).__name__
        else:
            facts["parent"]["state"] = ("dir" if stat.S_ISDIR(info.st_mode)
                                       else "not_a_dir")
        if facts["parent"]["state"] == "dir":
            try:
                facts["parent"]["writable"] = bool(os.access(parent, os.W_OK))
            except OSError as exc:
                facts["parent"]["error"] = type(exc).__name__
            try:
                facts["parent"]["realpath_differs"] = (
                    os.path.normcase(os.path.realpath(parent))
                    != os.path.normcase(os.path.abspath(parent)))
            except (OSError, ValueError):
                facts["parent"]["realpath_differs"] = None

    if isinstance(target, str):
        try:
            os.stat(target)
        except FileNotFoundError:
            facts["target"]["exists"] = False
        except PermissionError:
            facts["target"]["exists"] = None
            facts["target"]["error"] = "PermissionError"
        except OSError as exc:
            facts["target"]["exists"] = None
            facts["target"]["error"] = type(exc).__name__
        else:
            facts["target"]["exists"] = True
    return facts


def collect_acl(path):
    """只读采集目标文件 ACL：``Get-Acl`` + 两身份 SID 解析（配方 ⑤ 原文口径）。"""
    text = _run_powershell(_ACL_SCRIPT,
                           env_extra={"UM_KEYGEN_ACL_PATH": path})
    if text is None:
        return {"sddl": None, "owner": None, "inheritance_protected": None,
                "aces": None, "identities": None, "icacls_output": None,
                "error": "acl_query_failed"}
    parsed = parse_acl_json(text)
    return parsed


def parse_acl_json(text):
    """纯解析：PowerShell 的 JSON 输出 → ACL 事实字典（解析失败如实记 error）。"""
    out = {"sddl": None, "owner": None, "inheritance_protected": None,
           "aces": None, "identities": None, "icacls_output": None,
           "error": None}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        out["error"] = "acl_json_unparsable"
        return out
    if not isinstance(payload, dict):
        out["error"] = "acl_json_not_an_object"
        return out
    out["sddl"] = payload.get("sddl")
    out["owner"] = payload.get("owner")
    protected = payload.get("inheritance_protected")
    out["inheritance_protected"] = protected if isinstance(protected, bool) \
        else None
    aces = payload.get("aces")
    if isinstance(aces, list):
        out["aces"] = [ace for ace in aces if isinstance(ace, dict)]
    identities = payload.get("identities")
    if isinstance(identities, dict):
        out["identities"] = {str(key): value
                             for key, value in identities.items()}
    return out


def read_back(path):
    """读回文件：字节数 + 无 BOM 的 UTF-8 解码文本 + SHA-256（摘要只留内部）。"""
    out = {"bytes": None, "text": None, "sha256": None, "error": None}
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        out["error"] = "read_failed:%s" % type(exc).__name__
        return out
    out["bytes"] = len(raw)
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        out["text"] = raw.decode("utf-8")
    except UnicodeError:
        out["text"] = None
        out["error"] = "decode_failed"
    return out


# ------------------------------------------------------------------ 动作层
def write_key_file(path, key_text):
    """无 BOM、单行、UTF-8、不追加结尾换行（= SHARING.md:63 的 WriteAllText 等价）。"""
    data = key_text.encode("ascii")   # hex 是 ASCII；显式取字节，绝不依赖默认编码
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)


def apply_acl(path):
    """执行 SHARING.md:80-82 的 icacls 原文（``/inheritance:r`` + 四个 ``/grant:r``）。"""
    args = ["icacls", path, "/inheritance:r"]
    for who, rights in ACL_GRANTS:
        args.extend(["/grant:r", "%s:%s" % (who, rights)])
    return _run_command(args)


def dump_acl_raw(path):
    """执行 SHARING.md:95 的留档命令：``icacls <path>``（不带参数，只显示 ACL）。

    「icacls 有输出」不是断言；这一条只为**留档与原文核对**，断言在
    ``evaluate_acl_assertion`` 里按 SID 做。
    """
    return _run_command(["icacls", path])


def self_read_length(path):
    """以当前身份自读一次，**只返回 strip 后的长度**（绝不返回内容）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return len(handle.read().strip()), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, "self_read_failed:%s" % type(exc).__name__


def _precondition_items(result):
    return "\n".join(
        "  [%s] %s —— %s" % (item["verdict"].upper(), item["title"],
                              item["detail"])
        for item in result["items"])


def apply_key(target):
    """真实生成（写文件 + 设 ACL + 全部后验）；任一步不符即抛 ``KeygenAbort``。

    **本轮（执行轮 8）未执行**；执行轮 14 修掉自锁：前置判定与 dry-run 同一
    口径，``key_name()`` 显式传入 ``collect_preconditions``（此前缺省 None 使
    ``target_path_resolved`` 在 ``--apply`` 路径上必然 fail，工具无法真正生成
    密钥）。只有夹具覆盖其判定函数（不写生产路径）。
    """
    steps = []

    def step(name, ok, detail):
        steps.append({"step": name, "ok": bool(ok), "detail": detail})
        print("  [%s] %s —— %s" % ("OK" if ok else "ABORT", name, detail))
        if not ok:
            raise KeygenAbort(name, detail)

    print("== --apply：真实生成（写生产秘密 + 改 ACL） ==")
    print("目标路径：%s" % target)

    # 守卫：本工具边界（fail-closed）——只允许 RunRoot 之下、拒绝 run/live。
    # （data/ 与其余前置条件由紧接着的 evaluate_preconditions 逐项判定。）
    if not path_is_under(target, ALLOWED_PRODUCTION_ROOT):
        raise KeygenAbort(
            "guard_allowed_root",
            "目标 %r 不在 %s 之下：本工具绝不改该根以外的任何生产路径"
            % (target, ALLOWED_PRODUCTION_ROOT))
    if path_is_under(target, RUN_LIVE_DIR):
        raise KeygenAbort("guard_not_run_live",
                          "目标在 run\\live 下：每次部署删除重建，密钥会消失")
    step("guard_target_path", True,
         "目标在允许的生产根之下且不在 run\\live")

    # 前置条件必须全部满足（含提升；data/ 与 run/live 也在其中逐项判定）。
    # key_name 与 dry-run（main 的 :1127）同一口径：不传则
    # target_path_resolved 必然 fail（执行轮 14 修的自锁）。
    facts = collect_preconditions(target, key_name=key_name())
    result = evaluate_preconditions(facts)
    print(_precondition_items(result))
    if not result["ok"]:
        failed = [item["id"] for item in result["items"]
                  if item["verdict"] != "pass"]
        raise KeygenAbort("preconditions", "前置条件未全部满足：%r" % failed)

    # ① 生成：32 字节系统密码学随机数 → 64 hex。
    key_text = generate_key_text()
    shape = evaluate_key_text(key_text)
    step("generate_key", shape["ok"],
         "32 字节 CSPRNG → %d 个 hex 字符（只打印长度，绝不打印内容）"
         % len(key_text))
    if not shape["ok"]:
        raise KeygenAbort("generate_key", "生成结果形状不符：%r"
                          % [item for item in shape["items"]
                             if item["verdict"] != "pass"])

    # ② 写入：无 BOM、单行、UTF-8、不追加结尾换行。
    try:
        written = write_key_file(target, key_text)
    except OSError as exc:
        raise KeygenAbort("write_key_file", "写入失败：%s"
                          % type(exc).__name__)
    step("write_key_file", written == KEY_HEX_CHARS,
         "写入 %d 字节（无 BOM、单行、不追加结尾换行）" % written)

    # ③ 写后读回：字节数 + SHA-256 + 逐字节内容 + strip 后长度。
    observed = read_back(target)
    readback = evaluate_readback(key_text, observed)
    print("  写后读回：bytes=%r chars=%r"
          % (observed.get("bytes"),
             len(observed["text"].strip())
             if isinstance(observed.get("text"), str) else None))
    for item in readback["items"]:
        print("    [%s] %s —— %s" % (item["verdict"].upper(), item["title"],
                                      item["detail"]))
    step("readback", readback["ok"],
         "bytes/hash/内容/strip 长度四项判据")
    if not readback["ok"]:
        raise KeygenAbort(
            "readback",
            "写后读回不一致：**不继续**。目标文件可能处于不一致状态；"
            "删除密钥文件属维护者决定（动作门 §⑤.2），本工具不自动删除")

    # ④ ACL：icacls /inheritance:r + 四个 /grant:r。
    icacls = apply_acl(target)
    step("icacls", icacls.get("returncode") == 0,
         "rc=%r（原文：%s）" % (icacls.get("returncode"),
                                " ".join(icacls.get("args") or [])))
    if icacls.get("returncode") != 0:
        raise KeygenAbort("icacls", "ACL 未设上：%s" % (icacls.get("error")
                                                    or icacls.get("output")))

    # ⑤ ACL 断言：两身份 Allow 读 ACE + 完全控制 + /inheritance:r 生效 + 原文留档。
    dump = dump_acl_raw(target)
    acl = collect_acl(target)
    acl["icacls_output"] = dump.get("output")
    assertion = evaluate_acl_assertion(acl)
    print("  icacls 原文（留档，SHARING.md:95 的独立一条）：")
    for line in (dump.get("output") or "").splitlines():
        print("    %s" % line)
    for item in assertion["items"]:
        print("    [%s] %s —— %s" % (item["verdict"].upper(), item["title"],
                                      item["detail"]))
    step("acl_assertion", assertion["ok"], "两身份 Allow 读 ACE + 四个授权项 + "
         "/inheritance:r")
    if not assertion["ok"]:
        raise KeygenAbort("acl_assertion",
                          "ACL 断言未全部通过：**不继续**（按动作门 §⑤.2 处理）")

    # ⑥ 自读：只打印长度。**本步跑的是当前（--apply 下=提升）身份**，不是 8110
    # 的运行身份：SHARING.md:97-99 / 动作门 §2.2 ⑥ 要求的「非提升 dacint 提示符自读」
    # 本工具**不执行**（委托维护者，命令在下面打印），所以本工具**不宣布全绿**。
    length, error = self_read_length(target)
    step("self_read_current_identity",
         isinstance(length, int) and length >= KEY_MIN_CHARS,
         "当前身份（--apply 下通常是提升的）自读 strip 后长度=%r（error=%r；"
         "只给长度，绝不给内容）；**非提升 dacint 身份自读未执行**" % (length, error))

    print("== --apply 本地步骤全部通过；**仍有一项手工后验未执行** ==")
    print("未执行项（本工具不跑，动作门 §2.2 ⑥）：非提升 dacint 提示符里自读一次，"
          "只打印 strip 后长度——8110 的身份靠文件自身的显式 dacint:R ACE 读到，"
          "提升身份自读不能替代它。命令：")
    print("  python -c \"import sys;"
          "print(len(open(sys.argv[1],encoding='utf-8').read().strip()))\" %s"
          % target)
    print("其余后验（本工具不跑，动作门 §2.3 + §③）：")
    print("  8096 读面：GET http://127.0.0.1:8096/api/console/share-management/status"
          " → attribution.wired/file_ready（旧代码为 wired）")
    print("  8100 重启：run-deploy-guarded.ps1 -Stage Prep → Watchdog → "
          "Start -Mode real（动作门 §③.2）")
    return {"target": target, "steps": steps, "key_chars": len(key_text)}


def rehearse_rollback():
    """在临时目录里演练「设 ACL → 删除文件 → 父目录 ACL 未变」（动作门 §⑤.2）。"""
    workdir = tempfile.mkdtemp(prefix="um-share-keygen-rehearsal-")
    target = os.path.join(workdir, "share-attribution.key")
    facts = {"workdir": workdir, "temp_root": tempfile.gettempdir(),
             "target": target, "key_chars": None,
             "write": {"ok": False, "error": None},
             "readback": {"ok": False},
             "acl": {"ok": False},
             "icacls": {"returncode": None, "error": None},
             "file_deleted": False, "file_gone": None,
             "parent_acl_before": None, "parent_acl_after": None,
             "parent_acl_error": None}
    print("== --rehearse-rollback：临时目录里的回退演练（唯一会写盘的动作） ==")
    print("临时目录：%s" % workdir)
    print("假密钥文件：%s" % target)
    try:
        before = collect_acl(workdir)
        facts["parent_acl_before"] = before.get("sddl")
        if before.get("error"):
            facts["parent_acl_error"] = before.get("error")

        try:
            key_text = generate_key_text()
            facts["key_chars"] = len(key_text)
            written = write_key_file(target, key_text)
            facts["write"]["ok"] = written == KEY_HEX_CHARS
            observed = read_back(target)
            facts["readback"] = evaluate_readback(key_text, observed)
        except OSError as exc:
            facts["write"]["error"] = type(exc).__name__

        icacls = apply_acl(target)
        facts["icacls"] = {"returncode": icacls.get("returncode"),
                          "error": icacls.get("error")}
        dump = dump_acl_raw(target)
        acl = collect_acl(target)
        acl["icacls_output"] = dump.get("output")
        facts["acl"] = evaluate_acl_assertion(acl)
        print("  假密钥长度=%r（只给长度，绝不给内容）" % facts["key_chars"])
        for item in (facts["readback"].get("items") or []):
            print("    [%s] %s —— %s" % (item["verdict"].upper(),
                                          item["title"], item["detail"]))
        for item in facts["acl"]["items"]:
            print("    [%s] %s —— %s" % (item["verdict"].upper(),
                                          item["title"], item["detail"]))
        print("  icacls 原文（留档）：")
        for line in (dump.get("output") or "").splitlines():
            print("    %s" % line)

        try:
            os.remove(target)
            facts["file_deleted"] = True
        except OSError as exc:
            facts["write"]["error"] = "delete_failed:%s" % type(exc).__name__
        facts["file_gone"] = not os.path.exists(target)

        after = collect_acl(workdir)
        facts["parent_acl_after"] = after.get("sddl")
        if after.get("error"):
            facts["parent_acl_error"] = after.get("error")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    result = evaluate_rollback_rehearsal(facts)
    print("== 判定 ==")
    for item in result["items"]:
        print("[%s] %s —— %s" % (item["verdict"].upper(), item["title"],
                                   item["detail"]))
    print("临时目录已删除：%s" % (not os.path.exists(workdir)))
    print("结论：%s（退出码 %d）"
          % ("回退演练全部通过" if result["ok"]
             else "回退演练存在未通过项", result["exit_code"]))
    return result


# ------------------------------------------------------------------ 打印层
def format_plan(target, target_error, facts, result):
    """dry-run 打印：将要做的每一步与判据 + 前置条件逐项。"""
    lines = ["== 动作门 §② 生成归因密钥 + ACL · dry-run（不写任何文件、不碰 ACL） =="]
    lines.append("目标路径：%s" % (target if target else "（解析失败：%s）"
                                  % target_error))
    lines.append("路径解析器：console_share_production.share_attribution_key_path()"
                 "（SHARE_ATTRIBUTION_KEY_NAME=%r）" % facts.get("key_name"))
    lines.append("允许写入的生产根：%s（--apply 只允许其下；拒绝 run\\live 与 data/）"
                 % ALLOWED_PRODUCTION_ROOT)
    lines.append("")
    lines.append("将要执行的每一步与判据（原文 = SHARING.md:51-100）：")
    lines.append("  ① 生成：32 字节系统密码学随机数（secrets.token_bytes(32)，OS CSPRNG）"
                 " → %d 个 hex 字符；" % KEY_HEX_CHARS)
    lines.append("     判据：长度 == %d 且只含 [0-9a-f]；< %d 字符两端都会退化成未接线。"
                 % (KEY_HEX_CHARS, KEY_MIN_CHARS))
    lines.append("  ② 写入：无 BOM、单行、UTF-8、不追加结尾换行"
                 "（= WriteAllText + UTF8Encoding($false)）；判据：写入字节 == %d。"
                 % KEY_HEX_CHARS)
    lines.append("  ③ 写后读回：字节数 + SHA-256 + 逐字节内容 + strip 后长度；"
                 "任一不符即 throw，**不继续**。")
    lines.append("  ④ ACL：icacls %s /inheritance:r" % (target or "<target>"))
    for who, rights in ACL_GRANTS:
        lines.append("       /grant:r %s:%s" % (who, rights))
    lines.append("     必须 /inheritance:r：run/ 给 um_share 继承的 RW 若不收紧，"
                 "um_share 能改写密钥。")
    lines.append("  ⑤ ACL 断言：Get-Acl + 两身份 SID 解析 → 每个身份必须有 "
                 "Allow 且 rights & Read != 0 的 ACE；"
                 "另核对 /inheritance:r 生效与 icacls 原文含四个授权项。")
    lines.append("  ⑥ 自读：以当前身份读回并**只打印 strip 后长度**（绝不打印内容）；"
                 "非提升 dacint 提示符里须另跑一次。")
    lines.append("  ⑦ 额外后验（本工具不跑，动作门 §2.3）：8096 读面 "
                 "GET /api/console/share-management/status → attribution；"
                 "8100 重启见启动行「请求归因：已启用」。")
    lines.append("")
    lines.append("前置条件逐项（退出码 0 = 全部满足；2 = 任一不满足或不可判定）：")
    lines.append(_precondition_items(result))
    lines.append("")
    lines.append("当前会话提升：%r" % facts.get("elevated"))
    lines.append("目标父目录：%r state=%r writable=%r"
                 % (facts["parent"].get("path"), facts["parent"].get("state"),
                    facts["parent"].get("writable")))
    lines.append("目标文件：%r exists=%r" % (facts["target"].get("path"),
                                            facts["target"].get("exists")))
    lines.append("=" * 60)
    if result["ok"]:
        lines.append("结论：前置条件全部满足（退出码 0）。"
                     "授权窗口内跑：python ai-gateway/tools/um-share/"
                     "generate_attribution_key.py --apply（**须提升**）")
    else:
        not_pass = [item["id"] for item in result["items"]
                    if item["verdict"] != "pass"]
        lines.append("结论：存在未满足/不可判定项 %s（退出码 2）；"
                     "未满足时**不得**进 --apply。" % not_pass)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="动作门 §②：生成归因密钥 + ACL（默认 dry-run；写盘须显式 --apply）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="真实生成（写文件 + 设 ACL + 全部后验）；本轮未执行")
    mode.add_argument("--rehearse-rollback", action="store_true",
                      help="临时目录里的回退演练（唯一会写盘的动作）")
    parser.add_argument("--target", default=None,
                        help="覆盖目标路径（默认取生产解析器；--apply 只允许 "
                             + ALLOWED_PRODUCTION_ROOT + " 之下）")
    args = parser.parse_args(argv)

    if args.rehearse_rollback:
        result = rehearse_rollback()
        return result["exit_code"]

    if args.target:
        # 覆盖只改路径；密钥基名判据仍取产品常量（不复制字面量）。
        target, target_error, expected_name = args.target, None, key_name()
    else:
        target, target_error, expected_name = key_path()

    if args.apply:
        try:
            apply_key(target)
        except KeygenAbort as exc:
            print("[ABORT] %s：%s" % (exc.step, exc.reason), file=sys.stderr)
            return 2
        return 0

    facts = collect_preconditions(target, target_error, expected_name)
    result = evaluate_preconditions(facts)
    print(format_plan(target, target_error, facts, result))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
