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

from zcode import hot_sync as zcode_hot_sync
from zcode import hot_sync_protocol as zcode_hot_sync_protocol
from tests import zcode_current_protocol_fixture as current_fixture
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
        # 运行中的安装必须能按运行时命名解析：每个区域都要定位到唯一 bundle 并
        # 提取全部元素。摘要与证据表一致时必须准入；不一致（尚未复核的新布局）
        # 必须按具体区域延期——既不静默放行，也不把它当成无法解析。
        root = zcode_hot_sync.zcode_client_root()
        if root is None:
            self.skipTest("未找到 ZCode 安装根：跳过真实安装只读核对")
        asar = root / "resources" / "app.asar"
        if not asar.is_file():
            self.skipTest("resources/app.asar 不在场：跳过真实安装只读核对")
        records = zcode_hot_sync_protocol.inspect_installed_regions(
            asar,
            lambda archive, entry: zcode_hot_sync.read_asar_entry(
                archive, entry, max_bytes=8 * 1024 * 1024),
            zcode_hot_sync.list_asar_entries(asar, "out/host/"))
        self.assertIsNotNone(records, "运行中的安装必须能按运行时命名解析")
        for record in records:
            self.assertIsNone(
                record["missing"],
                "区域 %s 无法解析：%s" % (record["region"], record["missing"]))
        admitted, detail = zcode_hot_sync.zcode_hot_sync_admission()
        if all(record["matches"] for record in records):
            self.assertTrue(
                admitted,
                "全部区域摘要与证据表一致时必须准入：%s" % detail)
        else:
            self.assertFalse(
                admitted,
                "摘要与证据表不一致时必须延期而不是放行：%s" % detail)
            self.assertIn("协议兼容性待确认", detail)
            self.assertIn(
                next(record["region"] for record in records
                     if not record["matches"]), detail)


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


class ZcodeCurrentHotSyncAdmissionTests(unittest.TestCase):
    """3.15.1 当前布局的准入回归（合成安装包，私有临时目录）。

    夹具按当前打包构造：锁/持久化闭包在自己的 chunk，配置格式/
    repository/运行时装配在宿主，共享 model/selection schema 在共享 chunk。
    区域按运行时命名定位并按别名规范化后比较摘要：等价改名/换 chunk 必须
    仍兼容，任何语义变化、原生依赖错误绑定、元素缺失或歧义一律拒绝。
    每个用例只写自己的临时 asar，绝不读写真实安装或用户配置。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.asar = self.root / "resources" / "app.asar"

    def _write(self, bundles):
        entries = dict(bundles)
        entries["package.json"] = (
            '{"name":"@zcode/desktop","version":"3.15.1"}').encode()
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes(entries))

    def _admission(self, bundles, admitted):
        with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                               return_value=_scan(self.root)), \
                mock.patch.object(zcode_hot_sync_protocol,
                                  "CURRENT_PROTOCOL_PROFILE", admitted):
            return zcode_hot_sync.zcode_hot_sync_admission()

    def _regions(self, bundles):
        """按区域提取证据（只读；不写入任何东西）。"""
        self._write(bundles)
        return zcode_hot_sync_protocol.inspect_installed_regions(
            self.asar,
            lambda archive, entry: zcode_hot_sync.read_asar_entry(
                archive, entry, max_bytes=8 * 1024 * 1024),
            zcode_hot_sync.list_asar_entries(self.asar, "out/host/"))

    def _record(self, bundles, region):
        records = self._regions(bundles)
        self.assertIsNotNone(records)
        return next(record for record in records
                    if record["region"] == region)

    def test_current_layout_is_admitted(self):
        bundles = current_fixture.sources()
        self._write(bundles)
        admitted, detail = self._admission(
            bundles, current_fixture.profile(bundles))
        self.assertTrue(admitted, detail)
        self.assertIn("3.15.1", detail)
        self.assertIn("仅展示", detail)

    def test_renamed_bindings_and_chunks_stay_compatible(self):
        # 等价改名（每个压缩符号与 chunk 名都换掉，函数体不动）后摘要不变：
        # 这正是 3.15.1 相对 3.14.5 的打包变化形态，必须仍被准入。
        bundles = current_fixture.sources()
        renamed = current_fixture.renamed()
        self.assertEqual(current_fixture.profile(renamed),
                         current_fixture.profile(bundles))
        self._write(renamed)
        admitted, detail = self._admission(
            renamed, current_fixture.profile(bundles))
        self.assertTrue(admitted, detail)

    def test_changed_lock_or_format_or_listener_is_pending(self):
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        # 关键实现变化（函数体/常量）必须延期，且如实给出原因：函数体改动
        # 走「协议兼容性待确认」，常量取值改动让区域结构不再匹配、按
        # 「协议证据无法解析或不支持」延期——两者都不放行。
        for old, new, expected in (
                (b'return!0}i(Jo,"isProcessAlive")',
                 b'return!1}i(Jo,"isProcessAlive")', "协议兼容性待确认"),
                (b"function Y(r){return typeof r.code==",
                 b"function Y(r){return typeof r.status==",
                 "协议兼容性待确认"),
                (b"var jo=5*6e4;", b"var jo=5*6e3;",
                 "协议证据无法解析或不支持"),
                (b"[25,50,100,200,400]", b"[20,50,100,200,400]",
                 "协议证据无法解析或不支持")):
            with self.subTest(change=old):
                changed = dict(bundles)
                changed[current_fixture.LOCK_CHUNK] = changed[
                    current_fixture.LOCK_CHUNK].replace(old, new)
                self._write(changed)
                admitted, detail = self._admission(changed, trusted)
                self.assertFalse(admitted, detail)
                self.assertIn(expected, detail)

    def test_misbound_native_dependency_is_pending(self):
        # 原生依赖必须按真实绑定解析：把 mkdir 换成 rmdir（同名局部变量）后
        # 规范化摘要改变，准入拒绝——只看名字存在不算证据。
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        for old, new in ((b"mkdir as Ko", b"rmdir as Ko"),
                         (b"writeFile as qo", b"readFile as qo")):
            with self.subTest(change=old):
                changed = dict(bundles)
                changed[current_fixture.LOCK_CHUNK] = changed[
                    current_fixture.LOCK_CHUNK].replace(old, new)
                self._write(changed)
                admitted, detail = self._admission(changed, trusted)
                self.assertFalse(admitted, detail)

    def test_missing_element_or_layout_is_rejected(self):
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        changed = dict(bundles)
        del changed[current_fixture.SHARED_CHUNK]
        self._write(changed)
        admitted, detail = self._admission(changed, trusted)
        self.assertFalse(admitted)
        # 三个 bundle 全不在场：两种布局都无法确认，必须拒绝。
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(_asar_bytes({
            "out/host/index.js": b"not-a-bundle"}))
        admitted, detail = self._admission(bundles, trusted)
        self.assertFalse(admitted)
        self.assertIn("协议", detail)

    def test_missing_or_decoy_cross_chunk_dependency_is_rejected(self):
        # P1（2026-10-11 返修）：实际被消费的跨 chunk 依赖必须解析。缺失目标模块、
        # 导出找不到、或把同名导出换成另一份实现（诱饵），一律 unknown（fail closed）
        # ——只用 export 名的回退会让「把 import 改到缺失文件」保持同一摘要。
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        shared = current_fixture.SHARED_CHUNK
        host = current_fixture.HOST_CHUNK
        # 缺失目标模块：宿主导入改到不存在的文件（旧 chunk 仍在场，区域内容不变）。
        missing = dict(bundles)
        missing[host] = missing[host].replace(
            b'"./chunk-SHARED00.js"', b'"./chunk-AUDITMISS.js"')
        self.assertNotEqual(missing[host], bundles[host])
        self._write(missing)
        admitted, detail = self._admission(missing, trusted)
        self.assertFalse(admitted, detail)
        self.assertIn("协议证据无法解析", detail)
        # 同名诱饵：目标导出改到另一份同名实现的 chunk，真实导出改到别的名字。
        decoy = dict(bundles)
        decoy[host] = decoy[host].replace(b"$f as $f", b"$f as Zz")
        decoy[shared] = decoy[shared].replace(
            b"static fromData(t){return t}", b"static fromData(t){return 0}")
        self._write(decoy)
        admitted, detail = self._admission(decoy, trusted)
        self.assertFalse(admitted, detail)
        # 错误 export：目标导出名不存在。
        wrong = dict(bundles)
        wrong[shared] = wrong[shared].replace(
            b"export{Ub as Ub,$f as $f,timeoutCode as Sr};",
            b"export{Ub as Ub,timeoutCode as Sr};")
        self._write(wrong)
        admitted, detail = self._admission(wrong, trusted)
        self.assertFalse(admitted, detail)

    def test_duplicate_runtime_name_is_disambiguated_by_reference_chain(self):
        # 同一 bundle 内两个同名函数（3.16.2 的 persistence 区域）：只有可证明的
        # 消歧才可解析——区域锚点 atomicWritePrivateTextFile 的引用链唯一命中本
        # 区域的声明；未被任何区域锚点引用的同名声明不得改变任何区域摘要，
        # 判定依据必须作为证据给出。
        bundles = current_fixture.sources()
        duplicated = current_fixture.duplicated()
        self.assertEqual(current_fixture.profile(duplicated),
                         current_fixture.profile(bundles))
        record = self._record(duplicated, "persistence")
        self.assertIsNone(record["missing"])
        self.assertEqual(
            [label for label, _ in record["elements"]
             if label.startswith("root:")],
            ["root:isRetryableRenameError", "root:renameWithRetry",
             "root:withFileLock", "root:atomicWritePrivateTextFile"])
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn("renameWithRetry", record["notes"][0])
        self.assertIn("Si", record["notes"][0])
        self.assertIn("root:atomicWritePrivateTextFile", record["notes"][0])
        # 消歧后摘要不变：仍按原证据表准入。
        self._write(duplicated)
        admitted, detail = self._admission(
            duplicated, current_fixture.profile(bundles))
        self.assertTrue(admitted, detail)

    def test_duplicate_runtime_name_without_a_reference_chain_is_pending(self):
        # 两个同名声明都没有被本区域锚点引用：无法证明哪个属于本区域，
        # 一律 fail-closed（不得按位置或长度猜测）。
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        unreferenced = current_fixture.unreferenced()
        record = self._record(unreferenced, "persistence")
        self.assertIn("协议同名声明无法消歧", record["missing"])
        self._write(unreferenced)
        admitted, detail = self._admission(unreferenced, trusted)
        self.assertFalse(admitted, detail)
        self.assertIn("协议证据无法解析或不支持", detail)

    def test_duplicate_runtime_name_reachable_from_two_anchors_is_pending(self):
        # 两个同名声明都被本区域锚点引用：可达性无法唯一判定，仍须 fail-closed。
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        ambiguous = current_fixture.ambiguous()
        record = self._record(ambiguous, "persistence")
        self.assertIn("协议同名声明无法消歧", record["missing"])
        self._write(ambiguous)
        admitted, detail = self._admission(ambiguous, trusted)
        self.assertFalse(admitted, detail)
        self.assertIn("协议证据无法解析或不支持", detail)

    def test_duplicate_class_runtime_name_without_a_region_reference_is_pending(
            self):
        # 同名类的消歧同样只能由本区域锚点证明：运行期装配构造了该 repository
        # 类，但那条引用属于另一个区域，不构成本区域锚点的证据——仍 fail-closed。
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        duplicated = current_fixture.class_duplicated()
        record = self._record(duplicated, "repository")
        self.assertIn("协议同名声明无法消歧", record["missing"])
        self.assertIn("NodePersonalProviderConfigRepository", record["missing"])
        self._write(duplicated)
        admitted, detail = self._admission(duplicated, trusted)
        self.assertFalse(admitted, detail)
        self.assertIn("协议证据无法解析或不支持", detail)

    def test_real_installation_regions_are_extractable(self):
        # 运行中安装的只读解析核对：每个区域都必须定位到唯一 bundle 并提取
        # 全部元素；摘要与证据表一致时准入，不一致时必须按具体区域延期
        # （3.16.2 的实现变化需要下一轮语义复核与新证据表，不在这里放行）。
        from zcode import hot_sync_protocol as proto
        root = zcode_hot_sync.zcode_client_root()
        if root is None:
            self.skipTest("未找到 ZCode 安装根：跳过真实安装只读核对")
        asar = root / "resources" / "app.asar"
        if not asar.is_file():
            self.skipTest("resources/app.asar 不在场：跳过真实安装只读核对")
        records = proto.inspect_installed_regions(
            asar,
            lambda archive, entry: zcode_hot_sync.read_asar_entry(
                archive, entry, max_bytes=8 * 1024 * 1024),
            zcode_hot_sync.list_asar_entries(asar, "out/host/"))
        self.assertIsNotNone(records, "运行中的安装必须能按运行时命名解析")
        self.assertEqual(len(records),
                         len(zcode_hot_sync_protocol._CURRENT_REGIONS))
        for record in records:
            self.assertIsNone(
                record["missing"],
                "区域 %s 无法解析：%s" % (record["region"], record["missing"]))
            self.assertTrue(record["elements"],
                            "区域 %s 未提取到任何元素" % record["region"])
        persistence = next(record for record in records
                           if record["region"] == "persistence")
        declared = (
            len(zcode_hot_sync_protocol._CURRENT_REGIONS["persistence"]["roots"])
            + len(zcode_hot_sync_protocol._CURRENT_REGIONS[
                "persistence"]["constants"]))
        self.assertGreaterEqual(len(persistence["elements"]), declared)
        self.assertLessEqual(len(persistence["elements"]), 4 * declared)
        if not persistence["matches"]:
            self.assertTrue(
                any("renameWithRetry" in note
                    for note in persistence["notes"]),
                "运行中的 persistence 区域既不匹配证据表，也没有给出同名消歧依据")
        admitted, detail = zcode_hot_sync.zcode_hot_sync_admission()
        if all(record["matches"] for record in records):
            self.assertTrue(admitted, detail)
        else:
            self.assertFalse(admitted)
            self.assertIn("协议兼容性待确认", detail)

    def test_real_installation_repointed_import_is_rejected(self):
        # P1 真实安装只读核对（内存副本，不写安装）：把 index.js 里任一条实际被
        # 消费的跨 chunk 导入改到缺失文件，其余导入与原 chunk 保留；改动后至少
        # 一个区域必须变成依赖无法解析——只按 export 名回退的旧实现会给出同一
        # 摘要并保持同一结论。基线不得已经是依赖解析失败，否则检查是空的。
        import re
        from zcode import hot_sync_protocol as proto
        root = zcode_hot_sync.zcode_client_root()
        if root is None:
            self.skipTest("未找到 ZCode 安装根：跳过真实安装只读核对")
        asar = root / "resources" / "app.asar"
        reader = lambda archive, entry: zcode_hot_sync.read_asar_entry(
            archive, entry, max_bytes=8 * 1024 * 1024)
        entries = zcode_hot_sync.list_asar_entries(asar, "out/host/")
        bundles = proto._bundles(asar, reader, entries)
        host_name = "index.js"
        host_text = bundles[host_name]

        def region_evidence(candidate):
            self._write({"out/host/" + name: text.encode("utf-8")
                         for name, text in candidate.items()})
            records = proto.inspect_installed_regions(
                self.asar, reader, entries)
            if records is None:
                return None
            return {record["region"]: record["missing"] for record in records}

        baseline = region_evidence(bundles)
        self.assertIsNotNone(baseline, "运行中的安装必须能按运行时命名解析")
        self.assertFalse(
            [region for region, missing in baseline.items()
             if missing and "协议依赖" in missing],
            "基线不得已是依赖解析失败：%s" % baseline)

        def mutated_bundles(module):
            mutated = dict(bundles)
            mutated[host_name] = host_text.replace(
                'from"' + module + '"', 'from"./AUDIT-MISSING.js"')
            self.assertNotEqual(mutated[host_name], host_text, module)
            return mutated

        modules = sorted(set(re.findall(r'from"(\./[^"]+\.js)"', host_text)))
        self.assertTrue(modules, "真实宿主应有相对 chunk 导入")
        rejected = []
        for module in modules:
            evidence = region_evidence(mutated_bundles(module))
            self.assertIsNotNone(evidence, module)
            broken = [(region, missing)
                      for region, missing in evidence.items()
                      if missing and "协议依赖" in missing]
            if broken:
                rejected.append((module, broken))
        self.assertTrue(
            rejected,
            "把实际消费的跨 chunk 导入改到缺失文件后必须至少有一个区域"
            "变成依赖无法解析：%s" % modules)

    def test_corrupt_header_or_missing_installation_is_rejected(self):
        bundles = current_fixture.sources()
        trusted = current_fixture.profile(bundles)
        self.asar.parent.mkdir(parents=True, exist_ok=True)
        self.asar.write_bytes(b"not an asar")
        admitted, detail = self._admission(bundles, trusted)
        self.assertFalse(admitted)
        self.assertIn("头部", detail)
        with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                               return_value=_scan(self.root, image_paths=[])):
            admitted, detail = zcode_hot_sync.zcode_hot_sync_admission()
        self.assertFalse(admitted)
        self.assertIn("安装", detail)


if __name__ == "__main__":
    unittest.main()
