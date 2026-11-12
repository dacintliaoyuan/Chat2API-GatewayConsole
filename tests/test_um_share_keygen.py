# -*- coding: utf-8 -*-
"""``ai-gateway/tools/um-share/generate_attribution_key.py`` 的夹具测试（不连生产）。

覆盖动作门 §②（生成归因密钥 + ACL）的**纯判定函数**：

* 前置条件四态（父目录存在且可写 / 目标不存在 / 不在 ``ai-gateway/data`` /
  会话已提升）+ 本工具的两条 fail-closed 边界（在 RunRoot 之下 / 不在 ``run\\live``）；
  含「**读不到 ≠ 不存在**」的反向分支（动作门 §1.5）；
* 写后读回的三种不符：多写 BOM、多写结尾换行、内容不符（同长度不同内容）；
* ACL 断言的**缺读 ACE 分支**（身份没有 Allow 读 ACE / 只有 Deny / 只有写权限 /
  身份 SID 解析不了）与 ``/inheritance:r``、四个授权项原文判据；
* 回退演练的**父目录 ACL 不变**（回退只动文件、不动目录）。

隔离：夹具全在 ``tempfile`` 临时目录里；**不连生产路径**（不 ``stat``/读/写
``C:\\ProgramData\\UMShareRun`` 与 ``ai-gateway/data``）、不需要提升、不重启任何服务、
不发模型请求。唯一的写盘动作是「无 BOM 单行写入」那一支用临时文件验证配方本身。

运行（工作目录 ``ai-gateway/``）：

    python -B -m unittest tests.test_um_share_keygen -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools" / "um-share"

UM_SHARE_SID = "S-1-5-21-1771442332-1812804548-1420652959-1006"
DACINT_SID = "S-1-5-21-1771442332-1812804548-1420652959-1001"
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"

TARGET = r"C:\ProgramData\UMShareRun\run\share-attribution.key"
PARENT = r"C:\ProgramData\UMShareRun\run"


def _load(module_name):
    path = TOOLS_DIR / (module_name + ".py")
    spec = importlib.util.spec_from_file_location("um_share_" + module_name,
                                                 path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("generate_attribution_key")


# --------------------------------------------------------------- 夹具构造
def _facts(**overrides):
    """一份「前置条件全部满足」的事实夹具；``overrides`` 里的键按原样替换。"""
    base = {
        "target_path": TARGET,
        "target_path_error": None,
        "key_name": "share-attribution.key",
        "parent": {"path": PARENT, "state": "dir", "writable": True,
                   "realpath_differs": False, "error": None},
        "target": {"path": TARGET, "exists": False, "error": None},
        "data_dir": {"path": tool.REPO_DATA_DIR, "under": False, "error": None},
        "under_allowed_root": True,
        "under_run_live": False,
        "elevated": True,
    }
    base.update(overrides)
    return base


def _ace(type_="Allow", sid=None, name=None, rights=None, inherited=False,
         sid_error=None):
    return {"type": type_, "sid": sid, "sid_error": sid_error,
            "name": name if name is not None else sid,
            "rights": tool.FS_RIGHTS_READ if rights is None else rights,
            "inherited": inherited}


def _identities():
    return {r"DAICNT\um_share": UM_SHARE_SID,
            r"DAICNT\dacint": DACINT_SID,
            "SYSTEM": SYSTEM_SID,
            r"BUILTIN\Administrators": ADMINS_SID}


def _icacls_text():
    return ("share-attribution.key BUILTIN\\Administrators:(F)\n"
            "                     NT AUTHORITY\\SYSTEM:(F)\n"
            "                     DAICNT\\dacint:(R)\n"
            "                     DAICNT\\um_share:(R)\n")


def _full_acl(**overrides):
    """一份「ACL 断言全部通过」的夹具；``overrides`` 里的键按原样替换。"""
    base = {
        "sddl": "O:SYG:SYD:PAI(A;;FA;;;BA)(A;;FA;;;SY)(A;;0x120089;;;S-1-5-21-...-1001)"
                "(A;;0x120089;;;S-1-5-21-...-1006)",
        "owner": "BUILTIN\\Administrators",
        "inheritance_protected": True,
        "aces": [
            _ace(sid=ADMINS_SID, name=r"BUILTIN\Administrators",
                 rights=tool.FS_RIGHTS_FULL_CONTROL),
            _ace(sid=SYSTEM_SID, name=r"NT AUTHORITY\SYSTEM",
                 rights=tool.FS_RIGHTS_FULL_CONTROL),
            _ace(sid=DACINT_SID, name=r"DAICNT\dacint"),
            _ace(sid=UM_SHARE_SID, name=r"DAICNT\um_share"),
        ],
        "identities": _identities(),
        "icacls_output": _icacls_text(),
        "error": None,
    }
    base.update(overrides)
    return base


def _non_pass(result):
    return [item["id"] for item in result["items"]
            if item["verdict"] != "pass"]


def _item_by_id(result, item_id):
    for item in result["items"]:
        if item["id"] == item_id:
            return item
    raise AssertionError("no item %r in %r"
                         % (item_id, [item["id"] for item in result["items"]]))


# ------------------------------------------------------------- 前置条件
class PreconditionTests(unittest.TestCase):
    def test_all_satisfied_is_zero(self):
        result = tool.evaluate_preconditions(_facts())
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual([], _non_pass(result))

    def test_item_order_is_pinned(self):
        result = tool.evaluate_preconditions(_facts())
        self.assertEqual(
            ["target_path_resolved", "parent_dir_ready", "target_absent",
             "not_in_data_dir", "under_allowed_root", "not_under_run_live",
             "session_elevated"],
            [item["id"] for item in result["items"]])

    def test_parent_missing_is_fail(self):
        facts = _facts()
        facts["parent"] = {"path": PARENT, "state": "missing", "writable": None,
                          "realpath_differs": None, "error": None}
        result = tool.evaluate_preconditions(facts)
        self.assertEqual("fail",
                         _item_by_id(result, "parent_dir_ready")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_parent_unreadable_is_undecidable_never_missing(self):
        """读不到 ≠ 不存在（动作门 §1.5）：PermissionError 必须是不可判定，不是 fail。"""
        facts = _facts()
        facts["parent"] = {"path": PARENT, "state": "unreadable", "writable": None,
                          "realpath_differs": None, "error": "PermissionError"}
        result = tool.evaluate_preconditions(facts)
        item = _item_by_id(result, "parent_dir_ready")
        self.assertEqual("undecidable", item["verdict"])
        self.assertIn("读不到 ≠ 不存在", item["detail"])
        self.assertEqual(2, result["exit_code"])

    def test_parent_not_a_dir_is_fail(self):
        facts = _facts()
        facts["parent"] = {"path": PARENT, "state": "not_a_dir", "writable": None,
                          "realpath_differs": None, "error": None}
        result = tool.evaluate_preconditions(facts)
        self.assertEqual("fail",
                         _item_by_id(result, "parent_dir_ready")["verdict"])

    def test_parent_not_writable_is_fail(self):
        facts = _facts()
        facts["parent"] = {"path": PARENT, "state": "dir", "writable": False,
                           "realpath_differs": False, "error": None}
        result = tool.evaluate_preconditions(facts)
        self.assertEqual("fail",
                         _item_by_id(result, "parent_dir_ready")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_parent_writability_unknown_is_undecidable(self):
        facts = _facts()
        facts["parent"] = {"path": PARENT, "state": "dir", "writable": None,
                           "realpath_differs": False, "error": None}
        result = tool.evaluate_preconditions(facts)
        self.assertEqual("undecidable",
                         _item_by_id(result, "parent_dir_ready")["verdict"])

    def test_existing_target_is_refused_with_rotation_pointer(self):
        facts = _facts()
        facts["target"] = {"path": TARGET, "exists": True, "error": None}
        result = tool.evaluate_preconditions(facts)
        item = _item_by_id(result, "target_absent")
        self.assertEqual("fail", item["verdict"])
        self.assertIn("轮换", item["detail"])
        self.assertEqual(2, result["exit_code"])

    def test_unreadable_target_is_undecidable_never_absent(self):
        facts = _facts()
        facts["target"] = {"path": TARGET, "exists": None,
                          "error": "PermissionError"}
        result = tool.evaluate_preconditions(facts)
        item = _item_by_id(result, "target_absent")
        self.assertEqual("undecidable", item["verdict"])
        self.assertIn("读不到 ≠ 不存在", item["detail"])

    def test_target_under_repo_data_is_fail(self):
        facts = _facts()
        facts["data_dir"] = {"path": tool.REPO_DATA_DIR, "under": True,
                            "error": None}
        result = tool.evaluate_preconditions(facts)
        self.assertEqual("fail", _item_by_id(result, "not_in_data_dir")["verdict"])

    def test_target_outside_allowed_root_is_fail(self):
        result = tool.evaluate_preconditions(_facts(under_allowed_root=False))
        self.assertEqual("fail",
                         _item_by_id(result, "under_allowed_root")["verdict"])

    def test_target_under_run_live_is_fail(self):
        result = tool.evaluate_preconditions(_facts(under_run_live=True))
        self.assertEqual("fail",
                         _item_by_id(result, "not_under_run_live")["verdict"])

    def test_non_elevated_session_is_fail(self):
        result = tool.evaluate_preconditions(_facts(elevated=False))
        item = _item_by_id(result, "session_elevated")
        self.assertEqual("fail", item["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_unknown_elevation_is_undecidable(self):
        result = tool.evaluate_preconditions(_facts(elevated=None))
        self.assertEqual("undecidable",
                         _item_by_id(result, "session_elevated")["verdict"])

    def test_resolver_failure_is_fail_and_never_guessed(self):
        result = tool.evaluate_preconditions(
            _facts(target_path=None, target_path_error="resolver_failed:ImportError"))
        item = _item_by_id(result, "target_path_resolved")
        self.assertEqual("fail", item["verdict"])
        self.assertIn("resolver_failed", item["detail"])
        self.assertEqual(2, result["exit_code"])

    def test_key_name_mismatch_is_fail(self):
        result = tool.evaluate_preconditions(_facts(key_name="other.key"))
        self.assertEqual("fail",
                         _item_by_id(result, "target_path_resolved")["verdict"])

    def test_each_item_alone_flips_the_exit_code(self):
        """任一前置不满足/不可判定都必须让退出码从 0 变 2（逐项验，不整批放过）。"""
        broken = {
            "target_path_resolved": _facts(target_path=None,
                                           target_path_error="resolver_failed:X"),
            "parent_dir_ready": _facts(parent={"path": PARENT, "state": "missing",
                                              "writable": None,
                                              "realpath_differs": None,
                                              "error": None}),
            "target_absent": _facts(target={"path": TARGET, "exists": True,
                                           "error": None}),
            "not_in_data_dir": _facts(data_dir={"path": tool.REPO_DATA_DIR,
                                               "under": True, "error": None}),
            "under_allowed_root": _facts(under_allowed_root=False),
            "not_under_run_live": _facts(under_run_live=True),
            "session_elevated": _facts(elevated=False),
        }
        self.assertEqual(set(broken), {item["id"] for item in
                                      tool.evaluate_preconditions(
                                          _facts())["items"]})
        for item_id, facts in broken.items():
            result = tool.evaluate_preconditions(facts)
            self.assertEqual(2, result["exit_code"], item_id)
            self.assertEqual([item_id], _non_pass(result))


class PathGuardTests(unittest.TestCase):
    def test_path_is_under_is_case_insensitive_and_nested(self):
        self.assertTrue(tool.path_is_under(PARENT + r"\sub\x.key", PARENT))
        self.assertTrue(tool.path_is_under(PARENT.upper(), PARENT))
        self.assertTrue(tool.path_is_under(PARENT, PARENT))

    def test_path_is_under_rejects_sibling_and_other_drive(self):
        self.assertFalse(tool.path_is_under(PARENT + r"-other\x.key", PARENT))
        self.assertFalse(tool.path_is_under(r"D:\elsewhere\x.key", PARENT))

    def test_path_is_under_rejects_non_strings(self):
        self.assertFalse(tool.path_is_under(None, PARENT))
        self.assertFalse(tool.path_is_under(PARENT, None))


# ------------------------------------------------------------- 密钥形状
class KeyShapeTests(unittest.TestCase):
    def test_render_key_text_is_64_lowercase_hex(self):
        text = tool.render_key_text(bytes(range(32)))
        self.assertEqual(64, len(text))
        self.assertEqual("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
                         text)
        self.assertTrue(tool.evaluate_key_text(text)["ok"])

    def test_render_key_text_rejects_wrong_byte_count(self):
        for count in (0, 16, 31, 33):
            with self.assertRaises(ValueError):
                tool.render_key_text(b"\x01" * count)

    def test_evaluate_key_text_flags_short_and_non_hex(self):
        short = tool.evaluate_key_text("ab" * 8)          # 16 字符 < 32
        self.assertFalse(short["ok"])
        self.assertEqual("fail", _item_by_id(short, "key_hex_length")["verdict"])
        self.assertEqual("fail", _item_by_id(short, "key_min_chars")["verdict"])

        upper = tool.evaluate_key_text("AB" * 32)         # 大写不算 hex
        self.assertEqual("fail", _item_by_id(upper, "key_hex_shape")["verdict"])

        newline = tool.evaluate_key_text("a" * 63 + "\n")  # 多写结尾换行
        self.assertEqual("fail", _item_by_id(newline, "key_hex_shape")["verdict"])

        minimum = tool.evaluate_key_text("ab" * 16)        # 恰 32 字符 = 两端下限
        self.assertEqual("pass", _item_by_id(minimum, "key_min_chars")["verdict"])

    def test_generated_key_passes_its_own_shape_checks(self):
        text = tool.generate_key_text()
        self.assertEqual(64, len(text))
        self.assertTrue(tool.evaluate_key_text(text)["ok"])

    def test_two_generated_keys_differ(self):
        self.assertNotEqual(tool.generate_key_text(), tool.generate_key_text())


class WriteRecipeTests(unittest.TestCase):
    """写入配方：无 BOM、单行、不追加结尾换行（= SHARING.md:63）。"""

    def test_write_is_no_bom_single_line_without_trailing_newline(self):
        text = tool.render_key_text(bytes(range(32)))
        with tempfile.TemporaryDirectory(prefix="um-share-keygen-fixture-") as tmp:
            path = os.path.join(tmp, "share-attribution.key")
            written = tool.write_key_file(path, text)
            self.assertEqual(64, written)
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertEqual(64, len(raw))
            self.assertNotIn(b"\xef\xbb\xbf", raw)
            self.assertNotIn(b"\n", raw)
            self.assertEqual(text, raw.decode("ascii"))
            observed = tool.read_back(path)
            self.assertTrue(tool.evaluate_readback(text, observed)["ok"])


# ------------------------------------------------------------- 写后读回
class ReadbackTests(unittest.TestCase):
    KEY = "a1" * 32

    def _observed(self, **overrides):
        """读回夹具：``bytes``/``sha256`` 由 ``text`` 派生，模拟真实文件的一致读回。

        只覆盖 ``text`` 就能造出「多写 BOM」「多写换行」「内容不符」三种不符，
        同时让字节数与哈希跟着实际字节走（不能只改文本而哈希不动）。
        """
        text = overrides.pop("text", self.KEY)
        raw = text.encode("utf-8")
        base = {"bytes": len(raw), "text": text,
                "sha256": __import__("hashlib").sha256(raw).hexdigest(),
                "error": None}
        base.update(overrides)
        return base

    def test_exact_readback_passes(self):
        result = tool.evaluate_readback(self.KEY, self._observed())
        self.assertTrue(result["ok"])
        self.assertEqual([], _non_pass(result))

    def test_extra_bom_is_caught_by_bytes_hash_and_content(self):
        observed = self._observed(text="\ufeff" + self.KEY)
        # BOM 是 3 个字节（EF BB BF），所以读回是 67 字节而不是 64。
        self.assertEqual(67, observed["bytes"])
        result = tool.evaluate_readback(self.KEY, observed)
        self.assertFalse(result["ok"])
        self.assertEqual("fail", _item_by_id(result, "readback_bytes")["verdict"])
        self.assertEqual("fail", _item_by_id(result, "readback_sha256")["verdict"])
        self.assertEqual("fail",
                         _item_by_id(result, "readback_content")["verdict"])

    def test_trailing_newline_is_caught(self):
        observed = self._observed(text=self.KEY + "\n")
        self.assertEqual(65, observed["bytes"])
        result = tool.evaluate_readback(self.KEY, observed)
        self.assertFalse(result["ok"])
        self.assertEqual("fail", _item_by_id(result, "readback_bytes")["verdict"])
        self.assertEqual("fail", _item_by_id(result, "readback_content")["verdict"])
        # strip 后仍是 64：换行不会在这里被误判成「太短」。
        self.assertEqual("pass",
                         _item_by_id(result, "readback_min_chars")["verdict"])

    def test_same_length_different_content_is_caught(self):
        observed = self._observed(text="b2" * 32)
        self.assertEqual(64, observed["bytes"])
        result = tool.evaluate_readback(self.KEY, observed)
        self.assertFalse(result["ok"])
        self.assertEqual("pass", _item_by_id(result, "readback_bytes")["verdict"])
        self.assertEqual("fail", _item_by_id(result, "readback_sha256")["verdict"])
        self.assertEqual("fail",
                         _item_by_id(result, "readback_content")["verdict"])

    def test_unreadable_readback_is_undecidable(self):
        observed = {"bytes": None, "text": None, "sha256": None,
                    "error": "read_failed:PermissionError"}
        result = tool.evaluate_readback(self.KEY, observed)
        self.assertFalse(result["ok"])
        for item_id in ("readback_bytes", "readback_sha256", "readback_content",
                        "readback_min_chars"):
            self.assertEqual("undecidable",
                             _item_by_id(result, item_id)["verdict"], item_id)

    def test_short_but_consistent_readback_fails_min_chars(self):
        short = "ab" * 8                                  # 16 字符 < 32
        observed = {"bytes": 16, "text": short,
                    "sha256": __import__("hashlib").sha256(
                        short.encode("utf-8")).hexdigest(), "error": None}
        result = tool.evaluate_readback(short, observed)
        self.assertFalse(result["ok"])
        self.assertEqual("fail",
                         _item_by_id(result, "readback_min_chars")["verdict"])

    def test_verdict_details_never_carry_the_secret(self):
        result = tool.evaluate_readback(self.KEY, self._observed())
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.KEY, blob)

    def test_read_back_missing_file_reports_error(self):
        with tempfile.TemporaryDirectory(prefix="um-share-keygen-fixture-") as tmp:
            observed = tool.read_back(os.path.join(tmp, "absent.key"))
        self.assertEqual("read_failed:FileNotFoundError", observed["error"])
        self.assertIsNone(observed["bytes"])

    def test_parse_acl_json_parses_and_reports_failures(self):
        parsed = tool.parse_acl_json(json.dumps({
            "sddl": "O:SY", "owner": "BUILTIN\\Administrators",
            "inheritance_protected": True,
            "aces": [{"type": "Allow", "sid": SYSTEM_SID, "rights": 2032127}],
            "identities": {r"DAICNT\dacint": DACINT_SID}}))
        self.assertIsNone(parsed["error"])
        self.assertTrue(parsed["inheritance_protected"])
        self.assertEqual(1, len(parsed["aces"]))

        self.assertEqual("acl_json_unparsable",
                         tool.parse_acl_json("not json")["error"])
        self.assertEqual("acl_json_not_an_object",
                         tool.parse_acl_json("[1, 2]")["error"])


# ------------------------------------------------------------- ACL 断言
class AclAssertionTests(unittest.TestCase):
    def test_all_grants_present_passes(self):
        result = tool.evaluate_acl_assertion(_full_acl())
        self.assertTrue(result["ok"])
        self.assertEqual([], _non_pass(result))

    def test_missing_read_ace_for_um_share_is_fail(self):
        acl = _full_acl()
        acl["aces"] = [ace for ace in acl["aces"] if ace["sid"] != UM_SHARE_SID]
        result = tool.evaluate_acl_assertion(acl)
        self.assertFalse(result["ok"])
        item = _item_by_id(result, r"acl_read_ace:DAICNT\um_share")
        self.assertEqual("fail", item["verdict"])
        self.assertIn("no_key", item["title"])
        self.assertEqual("pass", _item_by_id(result, r"acl_read_ace:DAICNT\dacint")["verdict"])

    def test_missing_read_ace_for_dacint_is_fail(self):
        acl = _full_acl()
        acl["aces"] = [ace for ace in acl["aces"] if ace["sid"] != DACINT_SID]
        result = tool.evaluate_acl_assertion(acl)
        self.assertFalse(result["ok"])
        self.assertEqual("fail",
                         _item_by_id(result, r"acl_read_ace:DAICNT\dacint")["verdict"])

    def test_deny_ace_is_not_a_read_ace(self):
        acl = _full_acl()
        acl["aces"] = [
            _ace(type_="Deny", sid=UM_SHARE_SID, name=r"DAICNT\um_share"),
            _ace(type_="Allow", sid=DACINT_SID, name=r"DAICNT\dacint"),
        ]
        result = tool.evaluate_acl_assertion(acl)
        self.assertEqual("fail",
                         _item_by_id(result, r"acl_read_ace:DAICNT\um_share")["verdict"])

    def test_write_only_rights_are_not_a_read_ace(self):
        """FileSystemRights.Write = 0x116：与 Read(0x20089) 相与为 0，不算读 ACE。"""
        acl = _full_acl()
        acl["aces"] = [
            _ace(sid=UM_SHARE_SID, name=r"DAICNT\um_share", rights=0x116),
            _ace(sid=DACINT_SID, name=r"DAICNT\dacint"),
        ]
        result = tool.evaluate_acl_assertion(acl)
        self.assertEqual("fail",
                         _item_by_id(result, r"acl_read_ace:DAICNT\um_share")["verdict"])

    def test_unresolvable_identity_sid_is_undecidable(self):
        acl = _full_acl()
        acl["identities"] = {r"DAICNT\um_share": None,
                            r"DAICNT\dacint": DACINT_SID,
                            "SYSTEM": SYSTEM_SID,
                            r"BUILTIN\Administrators": ADMINS_SID}
        result = tool.evaluate_acl_assertion(acl)
        item = _item_by_id(result, r"acl_read_ace:DAICNT\um_share")
        self.assertEqual("undecidable", item["verdict"])
        self.assertIn("SID 解析不了", item["detail"])

    def test_name_fallback_matches_trailing_component(self):
        """SID 解析不了时按名字末段比对：``NT AUTHORITY\\SYSTEM`` ↔ ``SYSTEM``。"""
        acl = _full_acl()
        acl["aces"] = [
            _ace(sid=None, name=r"NT AUTHORITY\SYSTEM",
                 rights=tool.FS_RIGHTS_FULL_CONTROL),
            _ace(sid=ADMINS_SID, name=r"BUILTIN\Administrators",
                 rights=tool.FS_RIGHTS_FULL_CONTROL),
            _ace(sid=DACINT_SID, name=r"DAICNT\dacint"),
            _ace(sid=UM_SHARE_SID, name=r"DAICNT\um_share"),
        ]
        self.assertTrue(tool.evaluate_acl_assertion(acl)["ok"])

    def test_inheritance_not_protected_is_fail(self):
        result = tool.evaluate_acl_assertion(_full_acl(inheritance_protected=False))
        self.assertEqual("fail",
                         _item_by_id(result, "acl_inheritance_off")["verdict"])

    def test_missing_full_control_is_fail(self):
        acl = _full_acl()
        acl["aces"] = [ace for ace in acl["aces"]
                      if not (ace["sid"] == SYSTEM_SID
                              and ace["rights"] == tool.FS_RIGHTS_FULL_CONTROL)]
        result = tool.evaluate_acl_assertion(acl)
        self.assertEqual("fail",
                         _item_by_id(result, "acl_full_control_ace:SYSTEM")["verdict"])

    def test_missing_icacls_grant_in_raw_output_is_fail(self):
        acl = _full_acl(icacls_output="share-attribution.key DAICNT\\dacint:(R)\n")
        result = tool.evaluate_acl_assertion(acl)
        item = _item_by_id(result, "acl_icacls_grants")
        self.assertEqual("fail", item["verdict"])
        self.assertIn(r"DAICNT\um_share:(R)", item["detail"])

    def test_multiline_icacls_output_is_matched(self):
        acl = _full_acl()
        self.assertEqual("pass",
                         _item_by_id(tool.evaluate_acl_assertion(acl),
                                      "acl_icacls_grants")["verdict"])

    def test_acl_query_error_is_fail(self):
        result = tool.evaluate_acl_assertion(
            _full_acl(aces=None, identities=None,
                      error="acl_query_failed"))
        self.assertEqual("fail", _item_by_id(result, "acl_query_ok")["verdict"])
        self.assertEqual("undecidable",
                         _item_by_id(result, "acl_inheritance_off")["verdict"])


# ------------------------------------------------------------- 回退演练
class RollbackRehearsalTests(unittest.TestCase):
    def _facts(self, **overrides):
        base = {
            "workdir": r"C:\Users\x\AppData\Local\Temp\um-share-keygen-rehearsal-abc",
            "temp_root": r"C:\Users\x\AppData\Local\Temp",
            "target": r"C:\Users\x\AppData\Local\Temp\um-share-keygen-rehearsal-abc\share-attribution.key",
            "key_chars": 64,
            "write": {"ok": True, "error": None},
            "readback": {"ok": True},
            "acl": {"ok": True},
            "icacls": {"returncode": 0, "error": None},
            "file_deleted": True,
            "file_gone": True,
            "parent_acl_before": "O:SYG:SYD:PAI(A;;FA;;;BA)",
            "parent_acl_after": "O:SYG:SYD:PAI(A;;FA;;;BA)",
            "parent_acl_error": None,
        }
        base.update(overrides)
        return base

    def test_all_pass_is_zero(self):
        result = tool.evaluate_rollback_rehearsal(self._facts())
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual([], _non_pass(result))

    def test_parent_acl_changed_is_fail(self):
        """回退只动文件、不动目录：父目录 ACL 变了就必须报 2。"""
        result = tool.evaluate_rollback_rehearsal(
            self._facts(parent_acl_after="O:SYG:SYD:PAI(A;;FA;;;BA)(A;;0x120089;;;S-1-5-21-1)"))
        item = _item_by_id(result, "rehearsal_parent_acl_unchanged")
        self.assertEqual("fail", item["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_parent_acl_unknown_is_undecidable(self):
        result = tool.evaluate_rollback_rehearsal(
            self._facts(parent_acl_after=None, parent_acl_error="acl_query_failed"))
        self.assertEqual("undecidable",
                         _item_by_id(result, "rehearsal_parent_acl_unchanged")["verdict"])
        self.assertEqual(2, result["exit_code"])

    def test_file_not_deleted_is_fail(self):
        result = tool.evaluate_rollback_rehearsal(
            self._facts(file_deleted=False, file_gone=False))
        self.assertEqual("fail",
                         _item_by_id(result, "rehearsal_file_deleted")["verdict"])

    def test_workdir_outside_temp_is_fail(self):
        result = tool.evaluate_rollback_rehearsal(
            self._facts(workdir=r"C:\ProgramData\UMShareRun\run"))
        self.assertEqual("fail",
                         _item_by_id(result, "rehearsal_in_temp_dir")["verdict"])

    def test_acl_or_icacls_failure_is_fail(self):
        result = tool.evaluate_rollback_rehearsal(
            self._facts(acl={"ok": False}, icacls={"returncode": 5,
                                                    "error": None}))
        self.assertEqual("fail",
                         _item_by_id(result, "rehearsal_acl_ok")["verdict"])

    def test_key_length_other_than_64_is_fail(self):
        result = tool.evaluate_rollback_rehearsal(self._facts(key_chars=32))
        self.assertEqual("fail",
                         _item_by_id(result, "rehearsal_key_written")["verdict"])


# --------------------------------------------------- dry-run / --apply 边界
class ModeBoundaryTests(unittest.TestCase):
    def test_dry_run_against_a_temp_target_writes_nothing(self):
        """默认模式（无开关）= dry-run：只打印判据，退出码 2，绝不写文件。"""
        with tempfile.TemporaryDirectory(prefix="um-share-keygen-fixture-") as tmp:
            target = os.path.join(tmp, "run", "share-attribution.key")
            code = tool.main(["--target", target])
            self.assertEqual(2, code)
            self.assertFalse(os.path.exists(target))
            self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_dry_run_reports_only_the_boundary_item_for_a_temp_target(self):
        with tempfile.TemporaryDirectory(prefix="um-share-keygen-fixture-") as tmp:
            target = os.path.join(tmp, "share-attribution.key")
            facts = tool.collect_preconditions(target, None, tool.key_name(),
                                               elevated=True)
            result = tool.evaluate_preconditions(facts)
            self.assertEqual(2, result["exit_code"])
            self.assertEqual(["under_allowed_root"], _non_pass(result))

    def test_apply_refuses_a_target_outside_the_allowed_root(self):
        with tempfile.TemporaryDirectory(prefix="um-share-keygen-fixture-") as tmp:
            target = os.path.join(tmp, "share-attribution.key")
            with self.assertRaises(tool.KeygenAbort) as caught:
                tool.apply_key(target)
            self.assertEqual("guard_allowed_root", caught.exception.step)
            self.assertFalse(os.path.exists(target))

    def test_apply_refuses_a_target_under_run_live(self):
        target = os.path.join(tool.RUN_LIVE_DIR, "share-attribution.key")
        with self.assertRaises(tool.KeygenAbort) as caught:
            tool.apply_key(target)
        self.assertEqual("guard_not_run_live", caught.exception.step)


# ------------------------------------------- --apply 前置判定不自锁（执行轮 14）
class ApplyPreconditionTests(unittest.TestCase):
    """``--apply`` 的前置判定不再自锁：与 dry-run 同一口径传入 ``key_name()``。

    自锁根因（执行轮 14）：``apply_key`` 此前调 ``collect_preconditions(target)``
    不传 ``key_name`` ⇒ ``target_path_resolved`` 在 ``--apply`` 路径上必然 fail，
    工具无法真正生成密钥（窗口二只能用 SHARING.md 原文配方绕过）。

    隔离：目标取 ``ALLOWED_PRODUCTION_ROOT`` 之下的路径只为通过 fail-closed
    边界判定；``collect_preconditions`` 与写盘/ACL/读回各步全部替身化（喂确定性
    夹具并记录调用），**绝不写生产路径、不跑真实 icacls/读盘/PowerShell**。
    """

    TARGET = os.path.join(tool.ALLOWED_PRODUCTION_ROOT, "run",
                          "share-attribution.key")

    def test_apply_passes_key_name_into_preconditions_and_completes(self):
        key_text = "ab" * tool.KEY_BYTES   # 64 hex 字符夹具，绝不写盘
        seen = {}

        def fake_collect(target, target_error=None, key_name=None,
                         elevated=None):
            seen["target"] = target
            seen["key_name"] = key_name
            return _facts(target_path=target, key_name=key_name)

        with mock.patch.object(tool, "collect_preconditions", fake_collect), \
                mock.patch.object(tool, "generate_key_text",
                                  return_value=key_text), \
                mock.patch.object(tool, "write_key_file",
                                  return_value=len(key_text)), \
                mock.patch.object(tool, "read_back",
                                  return_value={
                                      "bytes": len(key_text),
                                      "text": key_text,
                                      "sha256": hashlib.sha256(
                                          key_text.encode("ascii")).hexdigest(),
                                      "error": None}), \
                mock.patch.object(tool, "apply_acl",
                                  return_value={"returncode": 0, "args": [],
                                                "output": "", "error": None}), \
                mock.patch.object(tool, "dump_acl_raw",
                                  return_value={"output": _icacls_text()}), \
                mock.patch.object(tool, "collect_acl",
                                  return_value=_full_acl()), \
                mock.patch.object(tool, "self_read_length",
                                  return_value=(len(key_text), None)):
            out = tool.apply_key(self.TARGET)

        self.assertEqual(seen["target"], self.TARGET)
        # 自锁根因的反向对照：不传 key_name 时同一夹具必然 fail。
        self.assertEqual(
            ["target_path_resolved"],
            _non_pass(tool.evaluate_preconditions(_facts(key_name=None))))
        # key_name 与 dry-run 同一口径显式传入（不是 None）→ 判定通过。
        self.assertEqual(seen["key_name"], tool.key_name())
        self.assertIsNotNone(seen["key_name"])
        # 前置门通过后各步全绿：apply 完成而不是在 preconditions 上 abort。
        self.assertTrue(all(step["ok"] for step in out["steps"]))
        self.assertEqual(out["key_chars"], len(key_text))


if __name__ == "__main__":
    unittest.main()
