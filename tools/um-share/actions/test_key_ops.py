# -*- coding: utf-8 -*-
"""分享 Key 计量闭环的**专用测试 Key** 受控增删（动作门 §④ 的前置工具）。

背景：``output/ai-gw-round6-20260927/metering-action-gate.md`` §④ 要求用一条
**专用测试 Key** 发一条有界真实请求，再按三件事实判定。本工具把「建/撤这把测试
Key」做成可核对的命令，**默认 dry-run**：只打印将写入的存储、字段与将产生的
``key_id`` 形态，不写任何东西。

权威存储与校验（本工具不另立一套）：

* 权威存储 = ``share_management.ShareManager`` 背后的 ``share_store.ShareStore``
  （SQLite 单写者；默认 ``ai-gateway/data/share-store.db``，可用
  ``SHARE_STORE_FILE`` 或 ``--store`` 覆盖）；
* 键行字段与写入语义见 ``share/store.py:519-541``（``_insert_key_conn``）与
  ``share/store.py:859-877``（``delete_key`` 软删除 tombstone）；
* 创建/撤销的**发布意图**与代次递增在同一事务里（``share/store.py:588-596``、
  ``:873-874``）；撤销是 fail-closed 方向（``share/management.py:797-803``）。

硬约束（本工具的全部承诺）：

1. **默认 dry-run，绝不写任何东西**：dry-run 只用只读连接（``mode=ro`` +
   ``PRAGMA query_only=ON``）读权威存储；存储不存在时如实报 ``store_missing``、
   读不到时如实报 ``store_unreadable``（0 字节/损坏/非 SQLite 都归这一态），
   两者都**不创建/不初始化**它。写只发生在显式 ``--apply`` 且动作是
   create/revoke 时；``--apply`` 在「不存在」或「读不到」的库上写还须显式
   ``--allow-new-store``（否则 exit 2，绝不静默初始化）。
2. **只碰测试 Key**：``--create`` 的 ``--label`` 必须以 ``zz-test-`` 开头，
   ``--revoke`` 的目标行 ``name`` 也必须以它开头；否则拒绝（退出码 2）。
   生产 Key（朋友、``小墨``、``奶龙`` 等）**不在本工具的可写范围**。
3. **绝不打印明文凭据**：dry-run 只给字段名与形态；``--apply --create`` 的
   明文只写进显式 ``--secret-out`` 文件（独占创建、拒绝覆盖），
   stdout/stderr 里绝不出现明文；``--list`` 只给 ``masked_key``。
4. **提醒用完即撤销**：plan（dry-run create/revoke）、``--list``、create 的 apply
   输出都带同一句提醒（创建后必须 ``--revoke --apply`` 把测试 Key 撤掉）；
   **拒绝路径不带**（没有建 Key）、**revoke 的 apply 成功尾部不带**（已经撤销）。
5. ``--broker console`` 才走控制台自己的装配（``usagesrv.get_share_manager()``，
   真实 broker → 会在线发布）；默认 ``--broker none`` 只写权威存储、**不发布**，
   此时 8100 不会接受该 Key（401 ``invalid_api_key``）。

退出码：

* ``0`` = dry-run 计划已打印，或 ``--apply`` 已写盘（create 已建、revoke 已写盘
  且发布已确认）；
* ``2`` = 拒绝或失败（未写盘：参数不合规、目标不是测试 Key、存储不存在/读不到、
  ``--apply --create`` 缺 ``--secret-out`` 或该文件已存在、新建/初始化库未给
  ``--allow-new-store``；写面 ``StoreError`` 也按此报，**不冒泡 traceback**）；
* ``3`` = **部分状态**：存储已变更但**在线发布未确认**（revoke 的 fail-closed
  结果；控制台会把它记为 blocked 发布意图，绝不报成功）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone

AI_GATEWAY_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if AI_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, AI_GATEWAY_DIR)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_PARTIAL = 3

# 测试 Key 与生产 Key 的标签前缀：本工具只增删带该前缀的身份。
TEST_LABEL_PREFIX = "zz-test-"
# 专用测试 Key 的每键并发上限：只要够跑一条有界请求，不放大。
TEST_KEY_MAX_CONCURRENCY = 1

KEY_ID_SHAPE = "uuid4（8-4-4-4-12 小写 hex，36 字符；写入前不可知具体值）"
MASKED_SHAPE = "um-share-****<末 4 字符>（掩码不含明文，share/store.py:85-87）"
SECRET_HASH_FIELD = "sha256(<生成的密钥>)：只写摘要，明文绝不打印"
REMINDER = ("提醒：这是专用测试 Key，验收完成必须立刻撤销 —— "
            "python ai-gateway/tools/um-share/test_key_ops.py "
            "--revoke --key-id <key_id> --apply")

KEY_COLUMNS = ("key_id", "name", "enabled", "secret_version", "masked_key",
               "max_concurrency", "created_at", "updated_at", "deleted_at",
               "row_version")


# ------------------------------------------------------------------- 只读层
def default_store_path():
    """权威存储路径：``SHARE_STORE_FILE`` 优先，否则 ``data/share-store.db``。"""
    share_management = __import__("importlib").import_module("share.management")
    return str(share_management.DEFAULT_STORE_FILE)


def read_store(path):
    """只读打开权威存储；返回 ``{path, exists, reason, error, meta,
    generation, keys}``。

    只读边界：``mode=ro`` + ``PRAGMA query_only=ON``，本函数不写、不创建、
    不截断。四态不混同（动作门 §1.5）：

    * 文件不存在 → ``exists=False``、``reason="store_missing"``（**不是**空列表）；
    * 打开或读取失败 → ``reason="store_unreadable"`` + ``error="store_*_failed:<类型>"``
      （**不是**「不存在」）；
    * 读到了 → ``reason=None``（空表就是空表）。
    """
    out = {"path": str(path), "exists": False, "reason": None, "error": None,
           "meta": {}, "generation": None, "keys": []}
    if not os.path.exists(path):
        out["reason"] = "store_missing"
        return out
    out["exists"] = True
    uri = "file:" + str(path).replace(os.sep, "/").replace("?", "%3f") \
        .replace("#", "%23") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        out["reason"] = "store_unreadable"
        out["error"] = "store_open_failed:%s" % type(exc).__name__
        return out
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        out["meta"] = {row["key"]: row["value"]
                       for row in conn.execute("SELECT key,value FROM meta")}
        gen = conn.execute("SELECT * FROM configgen").fetchone()
        out["generation"] = dict(gen) if gen is not None else None
        out["keys"] = [dict(row) for row in conn.execute(
            "SELECT " + ", ".join(KEY_COLUMNS) + " FROM keys"
            " ORDER BY created_at")]
    except sqlite3.Error as exc:
        out["reason"] = "store_unreadable"
        out["error"] = "store_read_failed:%s" % type(exc).__name__
    finally:
        conn.close()
    return out


def active_names(store):
    """当前未删除的显示名集合（create 的同名冲突判定面）。"""
    return {row.get("name") for row in store.get("keys", [])
            if row.get("deleted_at") is None}


def find_key(store, key_id):
    for row in store.get("keys", []):
        if row.get("key_id") == key_id:
            return row
    return None


def is_test_label(name):
    return isinstance(name, str) and name.startswith(TEST_LABEL_PREFIX)


# ------------------------------------------------------------- 纯判定层（拒绝）
def decide(intent):
    """纯函数：intent（dict）→ ``{mode, exit_code, refusals}``（字典进、字典出）。

    ``intent`` 键：``action``（list/create/revoke）、``apply``（bool）、
    ``label``、``key_id``、``secret_out``、``secret_out_exists``（bool）、
    ``store_exists``（bool）、``store_reason``（``None`` / ``store_missing`` /
    ``store_unreadable``）、``allow_new_store``（bool）、``max_concurrency``、
    ``key_row``（dict 或 None）、``active_names``（集合）。
    """
    refusals = []
    action = intent.get("action")
    if action not in ("list", "create", "revoke"):
        refusals.append("动作必须是 --list / --create / --revoke 之一")
    if action == "list" and intent.get("apply"):
        refusals.append("--list 是只读，不接受 --apply（它本来就不写盘）")
    max_concurrency = intent.get("max_concurrency")
    unreadable = intent.get("store_reason") == "store_unreadable"
    if action == "create":
        if isinstance(max_concurrency, bool) or not isinstance(
                max_concurrency, int) or not 1 <= max_concurrency <= 64:
            refusals.append("--max-concurrency 必须是 1..64 的整数")
        if not is_test_label(intent.get("label")):
            refusals.append(
                "--label 必须以测试前缀 %r 开头（本工具只建测试 Key，"
                "生产 Key 不在可写范围）" % TEST_LABEL_PREFIX)
        if intent.get("apply"):
            if not intent.get("secret_out"):
                refusals.append(
                    "--apply --create 必须给 --secret-out（明文只写该文件，"
                    "绝不打印；没有它就没法把测试 Key 交给探针）")
            elif intent.get("secret_out_exists"):
                refusals.append(
                    "--secret-out 已存在，拒绝覆盖（它可能已经装着另一把密钥）")
            if not intent.get("allow_new_store"):
                if unreadable:
                    # 读面把 0 字节/损坏/非 SQLite 都归入「读不到」；写面不得
                    # 静默初始化它（sqlite 会把 0 字节文件当成新库）。
                    refusals.append(
                        "存储读不到（store_unreadable）：--apply 不在无法确认的库上写"
                        "（0 字节/损坏/非 SQLite 都归这一态，可能是截断或损坏）；"
                        "确认要新建/初始化请加 --allow-new-store")
                elif not intent.get("store_exists"):
                    refusals.append(
                        "存储不存在（store_missing）：--apply 会新建一个空库（新库的 "
                        "share_max_concurrency=64、config_generation=1，不是当前生产库）；"
                        "确认要新建请加 --allow-new-store")
    if action == "revoke":
        if unreadable:
            refusals.append(
                "存储读不到（store_unreadable）：读不到目标行，不能撤销"
                "（读不到 ≠ 查不到）")
        elif not isinstance(intent.get("key_id"), str) or not intent.get("key_id"):
            refusals.append("--revoke 必须给 --key-id")
        elif intent.get("key_row") is None:
            refusals.append("--key-id 在权威存储里查不到（不猜身份）")
        else:
            row = intent["key_row"]
            if not is_test_label(row.get("name")):
                refusals.append(
                    "目标行 name=%r 不是测试 Key（不以 %r 开头）：本工具拒绝"
                    "撤销生产 Key" % (row.get("name"), TEST_LABEL_PREFIX))
            elif row.get("deleted_at") is not None:
                refusals.append(
                    "目标行已经删除过（deleted_at=%s）：没有可撤销的东西"
                    % row.get("deleted_at"))
    return {"mode": "apply" if intent.get("apply") else "dry_run",
            "action": action,
            "exit_code": EXIT_REFUSED if refusals else EXIT_OK,
            "refusals": refusals}


# ------------------------------------------------------------------ 计划层
def _now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def store_note(intent):
    """--apply 会怎么对待当前存储：不存在/读不到 + 是否已给 ``--allow-new-store``。"""
    reason = intent.get("store_reason")
    if reason == "store_missing":
        base = "存储不存在（store_missing）"
    elif reason == "store_unreadable":
        base = "存储读不到（store_unreadable；0 字节/损坏/非 SQLite 都归这一态）"
    else:
        return None
    if intent.get("allow_new_store"):
        return base + "：已给 --allow-new-store → --apply 会新建/初始化它"
    return base + "：未给 --allow-new-store → --apply 会拒绝（exit 2，不写盘）"


def plan_create(intent, store):
    """create 的将写入内容（dry-run 打印面）；不给明文、不给具体 key_id。"""
    return {
        "action": "create",
        "store": intent["store"],
        "store_exists": bool(store.get("exists")),
        "store_error": store.get("error"),
        "store_reason": store.get("reason"),
        "store_note": store_note(intent),
        "generation_now": (store.get("generation") or {}).get("config_generation"),
        "table": "keys（schema_version=%s，share/store.py:226-265）"
                 % (store.get("meta", {}).get("schema_version")),
        "key_id": KEY_ID_SHAPE,
        "rows_to_insert": [
            ("key_id", "新 uuid4（写入前不可知具体值）"),
            ("name", intent.get("label")),
            ("secret_hash", SECRET_HASH_FIELD),
            ("secret_version", 1),
            ("masked_key", MASKED_SHAPE),
            ("secret_blob", "DPAPI user-scope 密文；保护失败时为 NULL"
                             "（降级 hash-only，share/store.py:523-528）"),
            ("secret_algo", "dpapi-user-scope-v1 或 NULL"),
            ("enabled", 1),
            ("max_concurrency", intent.get("max_concurrency")),
            ("created_at / updated_at", "UTC now（写入时生成）"),
            ("deleted_at", None),
            ("row_version", 1),
        ],
        "same_transaction": [
            "configgen.config_generation +1（desired；share/store.py:399-401）",
            "ops：插入 publish intent（revoking=false, action_kind=create, "
            "key_id=<新 key_id>；share/store.py:588-596）",
        ],
        "publication": publication_note(intent),
        "reminder": REMINDER,
    }


def plan_revoke(intent, store):
    row = intent.get("key_row") or {}
    return {
        "action": "revoke",
        "store": intent["store"],
        "store_exists": bool(store.get("exists")),
        "store_error": store.get("error"),
        "generation_now": (store.get("generation") or {}).get("config_generation"),
        "target_row": {column: row.get(column) for column in KEY_COLUMNS},
        "sql": ("UPDATE keys SET enabled=0, deleted_at=<UTC now>,"
                " secret_blob=NULL, secret_algo=NULL, updated_at=<UTC now>,"
                " row_version=row_version+1"
                " WHERE key_id=? AND row_version=?（share/store.py:859-877）"),
        "same_transaction": [
            "configgen.config_generation +1（desired）",
            "ops：插入 publish intent（revoking=true, action_kind=delete, "
            "key_id=<目标 key_id>）",
        ],
        "publication": publication_note(intent),
        "fail_closed": ("撤销方向 fail-closed：发布未确认时 BrokerError(503)，"
                        "控制台把它记为 blocked 发布意图"
                        "（share/management.py:797-803）"),
        "reminder": REMINDER,
    }


def publication_note(intent):
    if intent.get("broker") == "console":
        return ("broker=console：走控制台自己的装配 usagesrv.get_share_manager()"
                "（真实 broker）→ --apply 会在线上发布这一代投影"
                "（真实生产动作，须在授权窗口内）")
    return ("broker=none：只写权威存储、**不发布**；该代次未在线确认前，"
            "8100 不接受这个 Key（401 invalid_api_key）；在线发布由控制台/"
            "授权窗口完成")


def format_plan(plan, intent):
    """打印计划；只给字段与形态，绝不给明文凭据。"""
    lines = ["== 分享测试 Key · %s（%s） =="
             % (plan["action"], "APPLY（会写盘）" if intent.get("apply")
                else "DRY-RUN（不写任何东西）")]
    lines.append("存储（权威）：%s（存在=%s%s%s）"
                 % (plan["store"], plan["store_exists"],
                    "" if not plan.get("store_reason")
                    else "，原因=%s" % plan["store_reason"],
                    "" if not plan.get("store_error")
                    else "，错误=%s" % plan["store_error"]))
    lines.append("当前 config_generation：%s" % plan.get("generation_now"))
    if plan.get("store_note"):
        lines.append("存储现状：%s" % plan["store_note"])
    if plan["action"] == "create":
        lines.append("表：%s" % plan["table"])
        lines.append("将产生的 key_id 形态：%s" % plan["key_id"])
        lines.append("将写入字段：")
        for field, value in plan["rows_to_insert"]:
            lines.append("  %s = %s" % (field, value))
        lines.append("同事务副作用：")
        for item in plan["same_transaction"]:
            lines.append("  %s" % item)
    else:
        lines.append("目标行（只读读到的现状）：")
        for column in KEY_COLUMNS:
            lines.append("  %s = %s" % (column, plan["target_row"].get(column)))
        lines.append("将写入：%s" % plan["sql"])
        lines.append("同事务副作用：")
        for item in plan["same_transaction"]:
            lines.append("  %s" % item)
    if plan.get("fail_closed"):
        lines.append("fail-closed：%s" % plan["fail_closed"])
    lines.append("发布：%s" % plan["publication"])
    lines.append("%s" % plan["reminder"])
    return "\n".join(lines)


# ------------------------------------------------------------------ 写盘层
def build_manager(intent):
    """--apply 的管理器装配：console = 控制台自己的（真实 broker）。"""
    share_management = __import__("importlib").import_module("share.management")
    if intent.get("broker") == "console":
        import usagesrv
        return usagesrv.get_share_manager()
    return share_management.ShareManager(store_path=intent["store"],
                                        broker=None)


def write_secret_exclusive(path, secret):
    """把明文独占写入 ``--secret-out``；拒绝覆盖已存在的文件。"""
    with open(path, "x", encoding="utf-8", newline="") as handle:
        handle.write(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上 chmod 只影响只读位；失败不隐藏、也不报成功
    return {"path": path, "chars": len(secret),
            "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest()}


def apply_create(intent):
    """--apply --create：写权威存储；StoreError 不冒泡，按 exit 2 如实报。"""
    share_store = __import__("importlib").import_module("share.store")
    try:
        manager = build_manager(intent)
        result = manager.create_key(name=intent["label"],
                                   max_concurrency=intent["max_concurrency"])
    except share_store.StoreError as exc:
        # 库打不开（例如非 SQLite 文件）时 sqlite 会抛 StoreError：写面不崩，
        # 按「未写盘」如实报 exit 2（与读面 store_unreadable 同一事实）。
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    secret = result.get("secret")
    if not isinstance(secret, str) or not secret:
        return {"ok": False, "error": "store_returned_no_plaintext",
                "result": {k: v for k, v in result.items() if k != "secret"}}
    written = write_secret_exclusive(intent["secret_out"], secret)
    return {"ok": True, "written": written, "result": result}


def apply_revoke(intent):
    share_management = __import__("importlib").import_module("share.management")
    share_store = __import__("importlib").import_module("share.store")
    try:
        manager = build_manager(intent)
        result = manager.delete_key(intent["key_id"])
    except share_store.StoreError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    except share_management.BrokerError as exc:
        return {"ok": False, "partial": True,
                "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "result": result}


# ------------------------------------------------------------------- CLI 层
def build_parser():
    parser = argparse.ArgumentParser(
        description="分享测试 Key 的受控增删（默认 dry-run；--apply 才写盘）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true",
                        help="只读列出权威存储里的 Key（含 deleted 行）")
    action.add_argument("--create", action="store_true",
                        help="新建测试 Key（label 必须以 %s 开头）"
                             % TEST_LABEL_PREFIX)
    action.add_argument("--revoke", action="store_true",
                        help="软删除测试 Key（目标 name 必须以 %s 开头）"
                             % TEST_LABEL_PREFIX)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="默认模式：只打印计划，不写任何东西（显式写出来也是它）")
    mode.add_argument("--apply", action="store_true",
                      help="真正写权威存储（create 需 --secret-out；"
                           "revoke 走 fail-closed 发布）")
    parser.add_argument("--label", help="测试 Key 的显示名（须 %s 前缀）"
                        % TEST_LABEL_PREFIX)
    parser.add_argument("--key-id", help="--revoke 的目标 key_id")
    parser.add_argument("--secret-out",
                        help="--apply --create：明文只写这个文件（独占创建）")
    parser.add_argument("--store", default=None,
                        help="权威存储路径（默认 SHARE_STORE_FILE 或 "
                             "data/share-store.db）")
    parser.add_argument("--broker", choices=("none", "console"), default="none",
                        help="none=只写存储不发布（默认）；console=用控制台"
                             "自己的装配发布（真实动作）")
    parser.add_argument("--max-concurrency", type=int,
                        default=TEST_KEY_MAX_CONCURRENCY,
                        help="测试 Key 的每键并发上限（默认 %d）"
                             % TEST_KEY_MAX_CONCURRENCY)
    parser.add_argument("--allow-new-store", action="store_true",
                        help="允许 --apply 在存储不存在**或读不到**时新建/初始化它"
                             "（0 字节/损坏/非 SQLite 都算读不到）")
    return parser


def intent_from_args(args):
    """CLI 参数 → 判定/计划用的 intent（不做任何 IO）。"""
    action = ("list" if args.list else "create" if args.create
              else "revoke" if args.revoke else None)
    return {"action": action, "apply": bool(args.apply),
            "label": args.label, "key_id": args.key_id,
            "secret_out": args.secret_out, "secret_out_exists": False,
            "store": str(args.store or default_store_path()),
            "store_exists": False, "allow_new_store": bool(args.allow_new_store),
            "max_concurrency": args.max_concurrency, "key_row": None,
            "active_names": set(), "broker": args.broker}


def render_list(store):
    lines = ["== 权威存储里的分享 Key（只读；绝不给明文） ==",
              "存储：%s（存在=%s%s）"
              % (store["path"], store["exists"],
                 "" if not store.get("error") else "，错误=%s" % store["error"])]
    if not store["exists"]:
        lines.append("存储不存在（原因=%s）：读不到 ≠ 空列表（--apply 会新建它，"
                     "需 --allow-new-store）" % store.get("reason"))
        return "\n".join(lines)
    if store.get("error"):
        lines.append("存储读不到（原因=%s，错误=%s）：读不到 ≠ 空列表"
                     "（--apply 会新建/初始化它，需 --allow-new-store）"
                     % (store.get("reason"), store["error"]))
        return "\n".join(lines)
    generation = store.get("generation") or {}
    lines.append("schema_version=%s config_generation=%s applied_generation=%s"
                 % (store.get("meta", {}).get("schema_version"),
                    generation.get("config_generation"),
                    generation.get("applied_generation")))
    lines.append("共 %d 行（含已删除）：" % len(store["keys"]))
    for row in store["keys"]:
        marker = "测试" if is_test_label(row.get("name")) else "生产"
        lines.append("  [%s] %s | name=%s | enabled=%s | deleted_at=%s | "
                     "masked=%s | max_concurrency=%s | row_version=%s"
                     % (marker, row.get("key_id"), row.get("name"),
                        row.get("enabled"), row.get("deleted_at"),
                        row.get("masked_key"), row.get("max_concurrency"),
                        row.get("row_version")))
    lines.append("%s" % REMINDER)
    return "\n".join(lines)


def console_store_path():
    """控制台自己的权威存储路径（``usagesrv.get_share_manager()`` 用的是它）。"""
    share_management = __import__("importlib").import_module("share.management")
    return str(share_management.DEFAULT_STORE_FILE)


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED

    intent = intent_from_args(args)
    if intent["broker"] == "console":
        # 控制台的 broker 装配只认它自己的库路径：--store 不一致就会
        # 「按 A 打印、写 B」，这里直接拒绝。
        console_store = console_store_path()
        if console_store != intent["store"]:
            print("[拒绝] --broker console 会写控制台自己的库 %s，与 --store %s "
                  "不一致：不写" % (console_store, intent["store"]),
                  file=sys.stderr)
            return EXIT_REFUSED
    store = read_store(intent["store"])
    intent["store_exists"] = store["exists"]
    intent["store_reason"] = store.get("reason")
    if intent.get("secret_out"):
        intent["secret_out_exists"] = os.path.exists(intent["secret_out"])
    if intent["action"] == "revoke" and intent.get("key_id"):
        intent["key_row"] = find_key(store, intent["key_id"])
    intent["active_names"] = active_names(store)

    decision = decide(intent)
    if decision["exit_code"] != EXIT_OK:
        print("[拒绝] 不写任何东西：", file=sys.stderr)
        for reason in decision["refusals"]:
            print("  - %s" % reason, file=sys.stderr)
        return EXIT_REFUSED

    if intent["action"] == "list":
        print(render_list(store))
        if not store["exists"]:
            print("[拒绝] 存储不存在（原因=%s）：读不到 ≠ 空列表（--apply 会新建它，"
                  "需 --allow-new-store）" % store.get("reason"), file=sys.stderr)
            return EXIT_REFUSED
        if store["error"]:
            print("[拒绝] 存储读不到（原因=%s，错误=%s）：读不到 ≠ 空列表"
                  "（--apply 会新建/初始化它，需 --allow-new-store）"
                  % (store.get("reason"), store["error"]), file=sys.stderr)
            return EXIT_REFUSED
        return EXIT_OK

    if not intent["apply"]:
        plan = (plan_create(intent, store) if intent["action"] == "create"
                else plan_revoke(intent, store))
        print(format_plan(plan, intent))
        print("（dry-run：本工具没有写任何东西；加 --apply 才写权威存储）")
        return EXIT_OK

    if intent["action"] == "create":
        print(format_plan(plan_create(intent, store), intent))
        outcome = apply_create(intent)
        if not outcome["ok"]:
            print("[失败] 未写盘：%s" % outcome["error"], file=sys.stderr)
            return EXIT_REFUSED
        result = outcome["result"]
        print("== APPLY 结果 ==")
        print("key_id = %s" % result.get("key_id"))
        print("name = %s" % result.get("name"))
        print("masked_key = %s" % result.get("mk"))
        print("config_generation(desired) = %s / applied = %s / in_sync = %s"
              % (result.get("desired_generation"),
                 result.get("applied_generation"), result.get("in_sync")))
        print("published = %s（错误=%s）"
              % (result.get("published"), result.get("publication_error")))
        print("明文已写入：%s（%d 字符，sha256=%s；绝不打印内容）"
              % (outcome["written"]["path"], outcome["written"]["chars"],
                 outcome["written"]["sha256"]))
        if result.get("published"):
            print("该 Key 已在线上发布，8100 会接受它")
        else:
            print("未在线发布：8100 不会接受它（401 invalid_api_key）；"
                  "在线发布由控制台/授权窗口完成")
        print(REMINDER)
        return EXIT_OK

    print(format_plan(plan_revoke(intent, store), intent))
    outcome = apply_revoke(intent)
    if not outcome["ok"] and outcome.get("partial"):
        print("== APPLY 结果（部分状态） ==")
        print("存储已变更：该 Key 已 enabled=0 + tombstone（拒绝已生效）；"
              "但在线发布未确认（fail-closed）")
        print("错误：%s" % outcome["error"])
        print("控制台会把它记为 blocked 发布意图；按动作门 §⑤ 处理，绝不报成功")
        print(REMINDER)
        return EXIT_PARTIAL
    if not outcome["ok"]:
        print("[失败] 未写盘：%s" % outcome["error"], file=sys.stderr)
        return EXIT_REFUSED
    print("== APPLY 结果 ==")
    print("key_id = %s" % outcome["result"].get("key_id"))
    print("deleted_at = %s" % outcome["result"].get("deleted_at"))
    print("该 Key 已软删除（enabled=0 + tombstone）；已在线发布确认")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
