# -*- coding: utf-8 -*-
"""Read-only hot-sync admission: protocol evidence, informational version.
Synthetic source/profile replacements only exercise the admission boundary;
real installed fragment experiments are recorded separately.
"""
import contextlib
import ctypes
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zcode_hot_sync
import zcode_hot_sync_protocol
from tests.zcode_protocol_fixture import sources, profile

BUNDLE_A = "out/host/index.js"
BUNDLE_B = "out/host/chunk-FNQWK6LT.js"


def _asar_document(entries):
    """按实测布局构造 asar 头部 JSON 文档；内容按插入顺序紧排。"""
    doc = {"files": {}}
    offset = 0
    for relative_path, data in entries.items():
        parts = relative_path.split("/")
        parent = doc
        for part in parts[:-1]:
            parent = parent["files"].setdefault(part, {"files": {}})
        parent["files"][parts[-1]] = {"size": str(len(data)),
                                     "offset": str(offset)}
        offset += len(data)
    return doc


def _asar_bytes(entries, *, json_bytes=None, header_size=None):
    """按实测头部布局合成 asar 文件字节。

    实测（真实安装）：header_size（offset4/offset8）= 8 + JSON 长度 + 对齐
    填充，内容基址 = 8 + header_size；JSON 位于 offset16 起。
    """
    payload_json = (json.dumps(_asar_document(entries)).encode(
        "utf-8") if json_bytes is None else json_bytes)
    json_length = len(payload_json)
    padding = (-(8 + json_length)) % 4 if header_size is None else (
        header_size - 8 - json_length)
    assert padding >= 0
    header_size = 8 + json_length + padding
    header = struct.pack("<IIII", 4, header_size, header_size, json_length)
    return header + payload_json + b"\x00" * padding + b"".join(
        entries.values())


def _package_entry(version="3.14.4"):
    return json.dumps({"name": "@zcode/desktop",
                       "version": version}).encode("utf-8")


def _layout_entries(*, version="3.14.4", bundle_a=None,
                    bundle_b=None):
    host, schema = sources(BUNDLE_B.rsplit("/", 1)[1])
    return {"package.json": _package_entry(version),
            BUNDLE_A: host if bundle_a is None else bundle_a,
            BUNDLE_B: schema if bundle_b is None else bundle_b}


def _evidence(entries, *, version="3.14.4"):
    """Synthetic trusted protocol evidence, independent of metadata/build size."""
    return version, profile(entries[BUNDLE_A], entries[BUNDLE_B])


def _scan(root, *, image_paths=None, exited_pids=(), unreadable_pids=(),
          snapshot_error=None, enumeration_error=None):
    """构造一次完整枚举结果的合成事实（R28-ADMISSION）。

    安装根只能来自运行中进程的镜像路径（模块没有环境变量覆盖）：合成用例
    把镜像指向临时安装根下的 ZCode.exe。
    """
    if image_paths is None:
        image_paths = [(4242, str(root / "ZCode.exe"))]
    return zcode_hot_sync.ZCodeProcessScan(
        paths=tuple(image_paths), exited_pids=tuple(exited_pids),
        unreadable_pids=tuple(unreadable_pids),
        snapshot_error=snapshot_error, enumeration_error=enumeration_error)


@contextlib.contextmanager
def _admission(root, *, entries=None, admitted_version="3.14.4",
                admitted_bundles=None, image_paths=None, scan=None):
    """把进程枚举结果与模块证据表替换为合成值（只测核对逻辑）。

    枚举结果的替身钉在 ``zcode_process_scan``（完整性事实的唯一来源）；
    准入必须走它，不能只看 ``zcode_process_image_paths`` 的部分结果。
    """
    entries = _layout_entries() if entries is None else entries
    if admitted_bundles is None:
        _, admitted_bundles = _evidence(entries, version=admitted_version)
    if scan is None:
        scan = _scan(root, image_paths=image_paths)
    with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                          return_value=scan), \
            mock.patch.object(
                zcode_hot_sync_protocol, "PROTOCOL_PROFILE", admitted_bundles):
        yield


class ZcodeHotSyncAdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.asar = self.root / "resources" / "app.asar"

    def _write(self, entries=None, **kwargs):
        entries = _layout_entries() if entries is None else entries
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes(entries, **kwargs))
        return entries

    def _admission(self):
        return zcode_hot_sync.zcode_hot_sync_admission()

    # ---- 已准入 ----

    def test_admitted_when_protocol_is_compatible(self):
        entries = self._write()
        with _admission(self.root, entries=entries):
            admitted, detail = self._admission()
        self.assertTrue(admitted, detail)
        self.assertIn("3.14.4", detail)

    def test_version_and_unrelated_bundle_changes_do_not_block(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        for version in ("3.14.5", "99.0.0", "3.14.3"):
            entries = _layout_entries(version=version)
            entries[BUNDLE_A] += b";const unrelatedFeature = 12345;"
            entries[BUNDLE_B] += b";const anotherUnrelatedFeature = 1;"
            self._write(entries)
            with _admission(self.root, entries=entries, admitted_bundles=evidence):
                admitted, detail = self._admission()
            self.assertTrue(admitted, detail)
            self.assertIn(version, detail)
            self.assertIn("仅展示", detail)

    def test_changed_lock_or_format_or_listener_is_pending(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        for old, new in ((b"return 0", b"return 2"),
                         (b"var du=1", b"var du=2"),
                         (b"start(){return 1}", b"start(){return 0}")):
            with self.subTest(change=old):
                entries = dict(original)
                entries[BUNDLE_A] = entries[BUNDLE_A].replace(old, new)
                self._write(entries)
                with _admission(self.root, entries=entries, admitted_bundles=evidence):
                    admitted, detail = self._admission()
                self.assertFalse(admitted)
                self.assertIn("协议兼容性待确认", detail)

    def test_renamed_schema_chunk_with_same_protocol_is_compatible(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        entries = dict(original)
        name = "chunk-RENAMED.js"
        entries[BUNDLE_A] = entries[BUNDLE_A].replace(
            BUNDLE_B.rsplit("/", 1)[1].encode(), name.encode())
        entries["out/host/" + name] = entries.pop(BUNDLE_B)
        self._write(entries)
        with _admission(self.root, entries=entries, admitted_bundles=evidence):
            admitted, detail = self._admission()
        self.assertTrue(admitted, detail)

    def test_every_critical_protocol_region_is_checked(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        regions = zcode_hot_sync_protocol.protocol_regions(
            original[BUNDLE_A].decode(), original[BUNDLE_B].decode())
        for name, source in regions.items():
            with self.subTest(region=name):
                entries = dict(original)
                key = BUNDLE_B if name.startswith("shared_") else BUNDLE_A
                # Preserve region anchors but change critical implementation.
                pos = source.index("{") + 1 if "{" in source else source.index("=") + 1
                changed = source[:pos] + "/*critical change*/" + source[pos:]
                entries[key] = entries[key].replace(source.encode(), changed.encode())
                self._write(entries)
                with _admission(self.root, entries=entries, admitted_bundles=evidence):
                    admitted, detail = self._admission()
                self.assertFalse(admitted, detail)

    def test_dependencies_cannot_be_redirected_or_replaced_by_a_decoy(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        for old, new in ((b"readFile as fk", b"writeFile as fk"),
                         (b"tA as Ja", b"otherSchema as Ja"),
                         (b"./chunk-FNQWK6LT.js", b"./../chunk-FNQWK6LT.js")):
            with self.subTest(change=old):
                entries = dict(original)
                key = BUNDLE_A if old != b"tA as Ja" else BUNDLE_B
                entries[key] = entries[key].replace(old, new)
                entries["out/host/chunk-DECOY.js"] = original[BUNDLE_B]
                self._write(entries)
                with _admission(self.root, entries=entries, admitted_bundles=evidence):
                    admitted, detail = self._admission()
                self.assertFalse(admitted, detail)

    def test_incomplete_profile_and_ambiguous_regions_are_pending(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        incomplete = dict(evidence)
        incomplete.pop("lock")
        self._write(original)
        with _admission(self.root, entries=original, admitted_bundles=incomplete):
            self.assertFalse(self._admission()[0])
        entries = dict(original)
        entries[BUNDLE_A] += b"function m0(){}"
        self._write(entries)
        with _admission(self.root, entries=entries, admitted_bundles=evidence):
            self.assertFalse(self._admission()[0])

    def test_reader_rejects_entry_larger_than_protocol_read_limit(self):
        self._write()
        self.assertIsNone(zcode_hot_sync.read_asar_entry(
            self.asar, BUNDLE_A, max_bytes=10))

    def test_unreadable_host_protocol_is_rejected(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        entries = self._write(_layout_entries(bundle_a=b"bundle-x"))
        with _admission(self.root, entries=entries, admitted_bundles=evidence):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("协议", detail)

    def test_missing_or_changed_schema_dependency_is_rejected(self):
        original = _layout_entries()
        _, evidence = _evidence(original)
        for content in (None, original[BUNDLE_B].replace(b'var V={}',b'var V={changed:1}')):
            with self.subTest(content=content is not None):
                entries = dict(original)
                if content is None:
                    del entries[BUNDLE_B]
                else:
                    entries[BUNDLE_B] = content
                self._write(entries)
                with _admission(self.root, entries=entries, admitted_bundles=evidence):
                    admitted, detail = self._admission()
                self.assertFalse(admitted)
                self.assertIn("格式" if content is None else "shared_selection_schema",detail)

    def test_missing_package_entry_is_informational(self):
        entries = _layout_entries()
        del entries["package.json"]
        self._write(entries)
        with _admission(self.root, entries=entries):
            admitted, detail = self._admission()
        self.assertTrue(admitted, detail)
        self.assertIn("未知", detail)

    def test_absent_asar_is_rejected(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with _admission(self.root):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("app.asar", detail)

    def test_corrupt_header_is_rejected(self):
        for payload in (b"not an asar", b"", b"\x04\x00\x00\x00",
                        b'{"files": {}}'):
            with self.subTest(payload=payload):
                self.asar.parent.mkdir(parents=True, exist_ok=True)
                self.asar.write_bytes(payload)
                with _admission(self.root):
                    admitted, detail = self._admission()
                self.assertFalse(admitted)
                self.assertIn("头部", detail)

    def test_missing_install_root_is_rejected(self):
        with _admission(self.root, image_paths=[]):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("安装", detail)

    def test_oversized_entry_bounds_are_rejected(self):
        # 损坏/构造的头部：超大 size/offset 必须按未取证拒绝，绝不抛
        # MemoryError/OverflowError 穿透调用方（旧实现 read(2**62) 会抛）。
        for key, value in (("size", str(2 ** 62)), ("size", "9" * 40),
                           ("offset", str(2 ** 70))):
            with self.subTest(key=key, value=value):
                entries = _layout_entries()
                document = _asar_document(entries)
                node = document["files"]["out"]["files"]["host"]["files"][
                    "index.js"]
                node[key] = value
                raw = json.dumps(document).encode("utf-8")
                self.asar.parent.mkdir(parents=True, exist_ok=True)
                self.asar.write_bytes(_asar_bytes(entries, json_bytes=raw))
                self.assertIsNone(
                    zcode_hot_sync.read_asar_entry(self.asar, BUNDLE_A))

    def test_oversized_entry_does_not_escape_the_admission(self):
        # 损坏头部必须走「未取证延期」而不是整轮失败：准入函数本身不得
        # 抛异常。
        entries = _layout_entries()
        document = _asar_document(entries)
        document["files"]["out"]["files"]["host"]["files"]["index.js"][
            "size"] = str(2 ** 62)
        raw = json.dumps(document).encode("utf-8")
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes(entries, json_bytes=raw))
        with _admission(self.root):
            admitted, detail = self._admission()
        self.assertFalse(admitted)

    def test_deeply_nested_header_json_does_not_escape(self):
        # 损坏头部（深层嵌套 JSON 数组/对象）让 json.loads 抛 RecursionError
        # （RuntimeError 子类）：它不在既有 except 元组内时会穿透读取器与准入
        # （run_sync 的调用点不包 try），整轮同步因一个损坏 asar 失败——
        # 必须按未取证返回 None / 明确拒绝。
        for deep in (b"[" * 100000 + b"]" * 100000,
                     b'{"a":' * 100000 + b"1" + b"}" * 100000):
            with self.subTest(payload=deep[:8]):
                self.asar.parent.mkdir(parents=True, exist_ok=True)
                self.asar.write_bytes(_asar_bytes(_layout_entries(),
                                                  json_bytes=deep))
                self.assertIsNone(zcode_hot_sync.read_asar_header(self.asar))
                self.assertIsNone(
                    zcode_hot_sync.read_asar_entry(self.asar, BUNDLE_A))
                with _admission(self.root):
                    admitted, detail = self._admission()
                self.assertFalse(admitted)
                self.assertIn("头部", detail)

    def test_deeply_nested_package_json_does_not_escape(self):
        # package.json 条目内容也可以是深层嵌套 JSON：同一个 RecursionError
        # 不能从准入的版本解析处穿透调用方，按「无法解析」拒绝。
        deep = b"[" * 100000 + b"]" * 100000
        entries = _layout_entries()
        entries["package.json"] = deep
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes(entries))
        with _admission(self.root, entries=entries):
            admitted, detail = self._admission()
        self.assertTrue(admitted, detail)
        self.assertIn("未知", detail)

    # ---- 安装根发现 ----

    def test_a_leftover_environment_variable_does_not_change_the_root(self):
        # 没有环境变量覆盖：残留/误设的 ZCODE_CLIENT_ROOT 不会把核对引到
        # 另一个目录（那等于绕过「运行中的客户端 = 被核对的安装」）。
        entries = self._write()
        other = self.root / "other-install"
        with mock.patch.dict(os.environ, {"ZCODE_CLIENT_ROOT": str(other)}):
            with _admission(self.root, entries=entries):
                self.assertEqual(zcode_hot_sync.zcode_client_root(), self.root)
                admitted, _ = self._admission()
        self.assertTrue(admitted)

    def test_multiple_install_roots_are_rejected(self):
        # 两个不同安装根同时在跑：枚举顺序不决定准入结论，按歧义拒绝。
        entries = self._write()
        other = self.root / "other-install"
        with _admission(self.root, entries=entries, image_paths=[
                (1, str(other / "ZCode.exe")),
                (2, str(self.root / "ZCode.exe"))]):
            self.assertIsNone(zcode_hot_sync.zcode_client_root())
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("安装根", detail)

    def test_unreadable_process_evidence_is_rejected(self):
        # R28-ADMISSION：某个匹配进程身份/镜像不可确认（权限拒绝等），
        # 不得以其余进程的部分成功证明「唯一安装」——整体拒绝。
        entries = self._write()
        with _admission(self.root, entries=entries, scan=_scan(
                self.root, unreadable_pids=(202,))):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("202", detail)
        self.assertIn("部分结果", detail)

    def test_snapshot_error_is_rejected(self):
        entries = self._write()
        with _admission(self.root, entries=entries,
                        scan=_scan(self.root, snapshot_error=5)):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("快照", detail)
        self.assertIn("5", detail)

    def test_enumeration_error_is_rejected(self):
        entries = self._write()
        with _admission(self.root, entries=entries,
                        scan=_scan(self.root, enumeration_error=5)):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("枚举中断", detail)
        self.assertIn("5", detail)

    def test_confirmed_exit_does_not_block_the_remaining_evidence(self):
        # OpenProcess 报 87（ERROR_INVALID_PARAMETER）= 进程已确证退出：
        # 该进程被跳过，其余唯一安装仍按证据放行。
        entries = self._write()
        with _admission(self.root, entries=entries,
                        scan=_scan(self.root, exited_pids=(202,))):
            admitted, detail = self._admission()
        self.assertTrue(admitted, detail)

    def test_confirmed_exit_alone_is_not_a_running_install(self):
        entries = self._write()
        with _admission(self.root, entries=entries, scan=_scan(
                self.root, image_paths=[], exited_pids=(202,))):
            admitted, detail = self._admission()
        self.assertFalse(admitted)
        self.assertIn("安装", detail)

    def test_client_root_comes_from_the_running_process_image(self):
        scan = _scan(self.root, image_paths=[
            (1, str(self.root / "ZCode.exe")),
            (2, str(self.root / "ZCode.exe"))])
        with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                               return_value=scan):
            self.assertEqual(zcode_hot_sync.zcode_client_root(), self.root)
        with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                               return_value=_scan(self.root, image_paths=[])):
            self.assertIsNone(zcode_hot_sync.zcode_client_root())

    def test_client_root_is_none_when_evidence_is_incomplete(self):
        # 证据不完整时安装根不可判定：绝不按部分结果给出「唯一安装」。
        for scan in (_scan(self.root, unreadable_pids=(202,)),
                     _scan(self.root, snapshot_error=5),
                     _scan(self.root, enumeration_error=5)):
            with self.subTest(scan=scan):
                with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                                       return_value=scan):
                    self.assertIsNone(zcode_hot_sync.zcode_client_root())
                    self.assertEqual(
                        zcode_hot_sync.zcode_client_roots(), [])

    def test_process_enumeration_is_read_only_and_never_raises(self):
        scan = zcode_hot_sync.zcode_process_scan()
        self.assertIsInstance(scan, zcode_hot_sync.ZCodeProcessScan)
        self.assertIsInstance(scan.paths, tuple)
        for pid, image_path in scan.paths:
            self.assertIsInstance(pid, int)
            self.assertIsInstance(image_path, str)
            self.assertTrue(image_path)
        for pid in scan.exited_pids + scan.unreadable_pids:
            self.assertIsInstance(pid, int)
        # 薄封装按同一份扫描结果返回 paths（不二次枚举，避免进程
        # 起落造成的竞态）。
        with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                               return_value=scan):
            paths = zcode_hot_sync.zcode_process_image_paths()
        self.assertIsInstance(paths, list)
        self.assertEqual(paths, list(scan.paths))

    # ---- 头部读取器与条目读取 ----

    def test_read_asar_entry_returns_exact_bytes_and_rejects_unknown(self):
        entries = self._write()
        for relative_path, data in entries.items():
            self.assertEqual(
                zcode_hot_sync.read_asar_entry(self.asar, relative_path), data)
        # 目录条目与不存在的路径都没有内容区：绝不猜、返回 None。
        self.assertIsNone(
            zcode_hot_sync.read_asar_entry(self.asar, "out/host"))
        self.assertIsNone(
            zcode_hot_sync.read_asar_entry(self.asar, "no/such.js"))

    def test_read_asar_header_reports_the_content_base(self):
        self._write()
        header = zcode_hot_sync.read_asar_header(self.asar)
        self.assertIsNotNone(header)
        content_base, document = header
        self.assertIn("files", document)
        header_size = struct.unpack(
            "<IIII", self.asar.read_bytes()[:16])[1]
        self.assertEqual(content_base, 8 + header_size)

    def test_read_asar_header_rejects_corrupt_files(self):
        for payload in (b"not an asar", b"", b"\x04\x00\x00\x00"):
            with self.subTest(payload=payload):
                self.asar.parent.mkdir(parents=True, exist_ok=True)
                self.asar.write_bytes(payload)
                self.assertIsNone(
                    zcode_hot_sync.read_asar_header(self.asar))
        self.assertIsNone(zcode_hot_sync.read_asar_header(
            self.root / "absent.asar"))

    # ---- 真实安装只读核对 ----

    def test_real_installation_matches_the_evidence_table(self):
        root = zcode_hot_sync.zcode_client_root()
        if root is None:
            self.skipTest("未找到 ZCode 安装根：跳过真实安装只读核对")
        asar = root / "resources" / "app.asar"
        if not asar.is_file():
            self.skipTest("resources/app.asar 不在场：跳过真实安装只读核对")
        admitted, detail = zcode_hot_sync.zcode_hot_sync_admission()
        self.assertTrue(
            admitted,
            "真实安装与证据表不一致，请重新取证并更新证据表：%s" % detail)


class _Win32Call:
    """ctypes 函数替身：保留 restype/argtypes 赋值，只转发调用。"""

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def _fake_kernel32(root, mode):
    """只替 Win32 API 边界的 kernel32 替身；枚举器与准入走真实实现。

    两个匹配进程（101、202），镜像路径都指向 ``root/ZCode.exe``。mode：

    * ``ok``：两个进程都可确认，枚举以错误码 18（ERROR_NO_MORE_FILES）结束；
    * ``enum_error``：读到第一个进程后 Process32NextW 报错误码 5（枚举中断）；
    * ``first_failed``：Process32FirstW 报错误码 5（枚举未开始）；
    * ``no_processes``：Process32FirstW 报错误码 18（确实没有进程）；
    * ``open_denied``：第二个进程 OpenProcess 被拒（错误码 5，权限拒绝）；
    * ``open_exited``：第二个进程 OpenProcess 报错误码 87（已确证退出）；
    * ``query_denied``：第二个进程 QueryFullProcessImageNameW 被拒（错误码 5）；
    * ``snapshot_failed``：CreateToolhelp32Snapshot 报错误码 5。
    """
    pids = (101, 202)
    cursor = [0]

    def fill(pointer, pid):
        pointer._obj.szExeFile = "ZCode.exe"
        pointer._obj.th32ProcessID = pid
        return 1

    def create_snapshot(*_):
        if mode == "snapshot_failed":
            ctypes.set_last_error(5)
            return ctypes.c_void_p(-1).value
        return 777

    def first(_, pointer):
        if mode == "first_failed":
            ctypes.set_last_error(5)
            return 0
        if mode == "no_processes":
            ctypes.set_last_error(18)
            return 0
        return fill(pointer, pids[0])

    def next_(_, pointer):
        cursor[0] += 1
        if mode == "enum_error" and cursor[0] == 1:
            ctypes.set_last_error(5)
            return 0
        if cursor[0] < len(pids):
            return fill(pointer, pids[cursor[0]])
        ctypes.set_last_error(18)
        return 0

    def open_(_, __, pid):
        if pid == pids[1] and mode == "open_denied":
            ctypes.set_last_error(5)
            return 0
        if pid == pids[1] and mode == "open_exited":
            ctypes.set_last_error(87)
            return 0
        return pid

    def query(handle, _, buffer, size):
        if handle == pids[1] and mode == "query_denied":
            ctypes.set_last_error(5)
            return 0
        buffer.value = str(root / "ZCode.exe")
        return 1

    return types.SimpleNamespace(**{
        name: _Win32Call(function) for name, function in {
            "CreateToolhelp32Snapshot": create_snapshot,
            "Process32FirstW": first,
            "Process32NextW": next_,
            "OpenProcess": open_,
            "QueryFullProcessImageNameW": query,
            "CloseHandle": lambda *_: 1,
        }.items()})


class ZcodeProcessScanApiFaultTests(unittest.TestCase):
    """R28-ADMISSION 低层 API 故障回归：只替 Win32 API 与合成证据表。

    与 tmp/r28-planner-admission-repro.py 同形：mock ``ctypes.WinDLL``，枚举器
    与准入走真实实现。不操作真实进程、任务、配置或网络。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.asar = self.root / "resources" / "app.asar"

    def _write_evidence(self, *, version="3.14.4"):
        entries = _layout_entries(version=version)
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes(entries))
        _, bundles = _evidence(entries, version=version)
        return bundles

    def _admit(self, mode, *, version="3.14.4", bundles=None):
        if bundles is None:
            bundles = self._write_evidence(version=version)
        with mock.patch.object(ctypes, "WinDLL",
                               return_value=_fake_kernel32(self.root, mode)), \
                mock.patch.object(zcode_hot_sync_protocol,
                                  "PROTOCOL_PROFILE", bundles):
            return zcode_hot_sync.zcode_hot_sync_admission()

    def _scan(self, mode):
        with mock.patch.object(ctypes, "WinDLL",
                               return_value=_fake_kernel32(self.root, mode)):
            return zcode_hot_sync.zcode_process_scan()

    def test_enumeration_ending_with_no_more_files_admits(self):
        admitted, detail = self._admit("ok")
        self.assertTrue(admitted, detail)
        self.assertIn("3.14.4", detail)

    def test_enumeration_interruption_is_rejected(self):
        admitted, detail = self._admit("enum_error")
        self.assertFalse(admitted)
        self.assertIn("枚举中断", detail)
        self.assertIn("5", detail)

    def test_first_call_failure_is_rejected(self):
        admitted, detail = self._admit("first_failed")
        self.assertFalse(admitted)
        self.assertIn("枚举", detail)
        self.assertIn("5", detail)

    def test_denied_process_open_is_rejected(self):
        # 权限拒绝不是退出：读不到第二个安装就不能放行。
        admitted, detail = self._admit("open_denied")
        self.assertFalse(admitted)
        self.assertIn("202", detail)

    def test_denied_image_query_is_rejected(self):
        admitted, detail = self._admit("query_denied")
        self.assertFalse(admitted)
        self.assertIn("202", detail)

    def test_confirmed_exit_is_skipped_without_blocking(self):
        # OpenProcess 报 87（ERROR_INVALID_PARAMETER）= 已确证退出：
        # 跳过该进程，其余唯一安装仍按证据放行。
        admitted, detail = self._admit("open_exited")
        self.assertTrue(admitted, detail)

    def test_snapshot_failure_is_rejected(self):
        admitted, detail = self._admit("snapshot_failed")
        self.assertFalse(admitted)
        self.assertIn("快照", detail)
        self.assertIn("5", detail)

    def test_no_zcode_process_is_rejected(self):
        admitted, detail = self._admit("no_processes")
        self.assertFalse(admitted)
        self.assertIn("安装", detail)

    def test_scan_reports_complete_and_incomplete_facts(self):
        scan = self._scan("ok")
        self.assertEqual(scan.paths, ((101, str(self.root / "ZCode.exe")),
                                    (202, str(self.root / "ZCode.exe"))))
        self.assertEqual(scan.exited_pids, ())
        self.assertEqual(scan.unreadable_pids, ())
        self.assertIsNone(scan.snapshot_error)
        self.assertIsNone(scan.enumeration_error)
        self.assertTrue(scan.complete)
        interrupted = self._scan("enum_error")
        self.assertEqual(interrupted.paths,
                         ((101, str(self.root / "ZCode.exe")),))
        self.assertEqual(interrupted.enumeration_error, 5)
        self.assertFalse(interrupted.complete)
        exited = self._scan("open_exited")
        self.assertEqual(exited.paths,
                         ((101, str(self.root / "ZCode.exe")),))
        self.assertEqual(exited.exited_pids, (202,))
        self.assertEqual(exited.unreadable_pids, ())
        self.assertTrue(exited.complete)
        failed = self._scan("snapshot_failed")
        self.assertEqual(failed.paths, ())
        self.assertEqual(failed.snapshot_error, 5)


if __name__ == "__main__":
    unittest.main()
