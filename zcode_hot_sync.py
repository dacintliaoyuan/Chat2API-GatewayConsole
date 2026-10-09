# -*- coding: utf-8 -*-
"""Read-only ZCode hot-sync admission based on protocol compatibility.

The package version is diagnostic information only. No package version,
archive length or whole-bundle hash is an admission condition. The inspected
file-lock, configuration codec and change-listener dependency closure is
checked by zcode_hot_sync_protocol; unrelated changes can upgrade without
blocking publication. An unfamiliar critical protocol implementation remains
pending until inspected, rather than guessing compatibility or restarting.

A complete process scan must establish one running installation; unreadable
processes, interrupted enumeration and ambiguous roots refuse admission.
No environment override can substitute a different installation. Archive
reads are bounded, exceptions fail closed, and admission never executes
installed JavaScript, writes configuration, or starts/stops a process.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, NamedTuple

from zcode_hot_sync_protocol import inspect_installed_protocol


_ASAR_HEADER_PREFIX_BYTES = 16
# 头部 JSON 的合理性上界：损坏的长度字段绝不触发无界读取。
_ASAR_MAX_JSON_BYTES = 64 * 1024 * 1024
_ZCODE_EXE_NAME = "zcode.exe"
_IMAGE_PATH_LIMIT = 32768
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# Win32 错误码（R28-ADMISSION）：18=枚举正常结束；87=进程已确证退出。
# 权限拒绝（5=ERROR_ACCESS_DENIED）不是退出，一律按不可确认处理。
_ERROR_NO_MORE_FILES = 18
_ERROR_INVALID_PARAMETER = 87
# 哨兵值：ctypes/Win32 不可用或枚举过程异常终止（不是 Win32 错误码）。
_ERROR_API_UNAVAILABLE = -1


class ZCodeProcessScan(NamedTuple):
    """一次 ZCode 进程枚举的完整性与结果事实（只读快照）。

    * ``paths``：身份与镜像路径都可确认的 ``(pid, 镜像路径)``，按发现顺序；
    * ``exited_pids``：已确证退出的匹配进程（``OpenProcess`` 报 87）；
    * ``unreadable_pids``：身份或镜像路径不可确认的匹配进程（含权限拒绝）；
    * ``snapshot_error``：快照创建失败的错误码（None = 成功）；
    * ``enumeration_error``：枚举中断的错误码（None = 正常结束）。

    只有 ``complete`` 为真时这份证据才完整；任何失败形态都不能用来判定
    「唯一安装」——部分成功不是证据。
    """

    paths: tuple[tuple[int, str], ...]
    exited_pids: tuple[int, ...]
    unreadable_pids: tuple[int, ...]
    snapshot_error: int | None
    enumeration_error: int | None

    @property
    def complete(self) -> bool:
        """证据是否完整：快照成功、枚举正常结束、没有不可确认的匹配进程。"""
        return (self.snapshot_error is None and self.enumeration_error is None
                and not self.unreadable_pids)


def _error_text(code: int | None) -> str:
    """错误码的可读文本（含「枚举 API 不可用」哨兵值）。"""
    if code is None:
        return "无"
    if code == _ERROR_API_UNAVAILABLE:
        return "枚举 API 不可用（非 Windows 或 ctypes 缺失）"
    return "错误码 %d" % code


def zcode_process_scan() -> ZCodeProcessScan:
    """枚举 ZCode.exe 进程，返回完整性与结果事实（只读，绝不抛异常）。

    失败语义（R28-ADMISSION，fail closed）：

    * ``CreateToolhelp32Snapshot`` 失败 → ``snapshot_error``（含错误码）；
    * ``Process32FirstW`` 返回 0：错误码 18（ERROR_NO_MORE_FILES）表示确实
      没有进程，属正常；其他错误码 → ``enumeration_error``；
    * 每个匹配进程：``OpenProcess`` 失败且错误码 87
      （ERROR_INVALID_PARAMETER）才算已确证退出 → ``exited_pids``（跳过）；
      其他错误码（含权限拒绝 5）→ ``unreadable_pids``；
      ``QueryFullProcessImageNameW`` 失败 → ``unreadable_pids``；
    * ``Process32NextW`` 返回 0：错误码 18 → 正常结束；其他 →
      ``enumeration_error``。

    调用前清 ctypes 线程 last error，避免读到上一次调用的残留错误码；
    任何失败都不抛异常穿透调用方。本函数绝不启动/停止进程，也绝不写东西。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return ZCodeProcessScan((), (), (), _ERROR_API_UNAVAILABLE, None)
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        return ZCodeProcessScan((), (), (), _ERROR_API_UNAVAILABLE, None)
    try:
        kernel32 = windll("kernel32", use_last_error=True)
    except Exception:
        return ZCodeProcessScan((), (), (), _ERROR_API_UNAVAILABLE, None)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    def last_error() -> int:
        try:
            return int(ctypes.get_last_error())
        except Exception:
            return 0

    paths: list[tuple[int, str]] = []
    exited_pids: list[int] = []
    unreadable_pids: list[int] = []
    snapshot_error: int | None = None
    enumeration_error: int | None = None
    snapshot = None
    invalid_handle = ctypes.c_void_p(-1).value
    try:
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                    wintypes.DWORD]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                        wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot in (-1, invalid_handle):
            snapshot_error = last_error()
        else:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ctypes.set_last_error(0)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            if not ok:
                code = last_error()
                if code != _ERROR_NO_MORE_FILES:
                    enumeration_error = code
            while ok:
                if str(entry.szExeFile).lower() == _ZCODE_EXE_NAME:
                    pid = int(entry.th32ProcessID)
                    ctypes.set_last_error(0)
                    handle = kernel32.OpenProcess(
                        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if not handle:
                        code = last_error()
                        if code == _ERROR_INVALID_PARAMETER:
                            # 已确证退出：只有 ERROR_INVALID_PARAMETER(87)
                            # 才算；权限拒绝（5）不是退出，按不可确认处理。
                            exited_pids.append(pid)
                        else:
                            unreadable_pids.append(pid)
                    else:
                        try:
                            buffer = ctypes.create_unicode_buffer(
                                _IMAGE_PATH_LIMIT)
                            size = wintypes.DWORD(_IMAGE_PATH_LIMIT)
                            ctypes.set_last_error(0)
                            if kernel32.QueryFullProcessImageNameW(
                                    handle, 0, buffer, ctypes.byref(size)):
                                paths.append((pid, buffer.value))
                            else:
                                unreadable_pids.append(pid)
                        finally:
                            kernel32.CloseHandle(handle)
                ctypes.set_last_error(0)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
                if not ok:
                    code = last_error()
                    if code != _ERROR_NO_MORE_FILES:
                        enumeration_error = code
    except Exception:
        # 任何意外失败都不穿透：按枚举不可用拒绝（不能判定安装）。
        if snapshot_error is None and enumeration_error is None:
            enumeration_error = _ERROR_API_UNAVAILABLE
    finally:
        if snapshot and snapshot not in (-1, invalid_handle):
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                pass
    return ZCodeProcessScan(tuple(paths), tuple(exited_pids),
                            tuple(unreadable_pids), snapshot_error,
                            enumeration_error)


def zcode_process_image_paths() -> list[tuple[int, str]]:
    """枚举正在运行的 ZCode.exe 进程镜像路径（只读薄封装）。

    返回 ``[(pid, 镜像路径)]``，只含身份与镜像都可确认的进程；非 Windows、
    API 不可用或任何一步失败时返回已确认的部分（可能为空）——调用方按
    「没找到安装根」处理，绝不猜路径、绝不抛异常。完整性事实（快照/枚举
    失败、不可确认进程）见 ``zcode_process_scan()``：**准入判定必须用它**，
    不能用这里的部分结果证明「唯一安装」。本函数绝不启动/停止进程，也绝不
    写任何东西。
    """
    return list(zcode_process_scan().paths)


def _dedupe_roots(paths: tuple[tuple[int, str], ...]) -> list[Path]:
    """从可确认的镜像路径推出安装根（去重、按发现顺序，路径异常跳过）。"""
    roots: list[Path] = []
    for _, image_path in paths:
        if not image_path:
            continue
        try:
            root = Path(image_path).parent
        except (TypeError, ValueError, OSError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def zcode_client_roots() -> list[Path]:
    """所有运行中 ZCode 实例的安装根（去重、按发现顺序）。

    只读；证据不完整（快照/枚举失败或存在不可确认进程）时返回空列表——
    不能以部分结果给出「安装根」；路径异常的条目跳过。同一处安装的多个
    进程只算一个根。
    """
    scan = zcode_process_scan()
    if not scan.complete:
        return []
    return _dedupe_roots(scan.paths)


def zcode_client_root() -> Path | None:
    """唯一安装根：全部运行实例指向同一处才返回，否则 None。

    没有环境变量覆盖：准入的前提就是「正在运行的客户端 = 被核对的安装」，
    任何覆盖都会让核对落到另一个目录（残留变量即可放行未知构建）。零个、
    多个不同安装根或证据不完整（无法判定唯一性）都返回 None——一律不放行。
    """
    scan = zcode_process_scan()
    if not scan.complete:
        return None
    roots = _dedupe_roots(scan.paths)
    if len(roots) == 1:
        return roots[0]
    return None


def read_asar_header(path: Path | str) -> tuple[int, dict[str, Any]] | None:
    """读取 asar 头部，返回 ``(内容基址, 头部 JSON)``；失败返回 None。

    布局按实测：offset0/4/8/12 四个 uint32，offset16 起 JSON；内容基址 =
    8 + 头部载荷大小。任何一步失败（文件不可读、长度字段损坏、JSON 无法
    解析或深层嵌套、顶层不是对象）都返回 None，绝不猜、绝不抛异常穿透。
    """
    try:
        with open(path, "rb") as handle:
            prefix = handle.read(_ASAR_HEADER_PREFIX_BYTES)
            if len(prefix) != _ASAR_HEADER_PREFIX_BYTES:
                return None
            _, header_size, _, json_length = struct.unpack("<IIII", prefix)
            if (json_length == 0 or json_length > _ASAR_MAX_JSON_BYTES
                    or header_size < json_length):
                return None
            payload = handle.read(json_length)
            if len(payload) != json_length:
                return None
            document = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, struct.error,
            MemoryError, OverflowError, RecursionError):
        # RecursionError（RuntimeError 子类）：深层嵌套 JSON 让 json.loads
        # 触发递归上限；它是损坏头部的失败形态，绝不穿透调用方。
        return None
    if not isinstance(document, dict) or not isinstance(
            document.get("files"), dict):
        return None
    return 8 + header_size, document


def read_asar_entry(path: Path | str,
                    relative_path: str, *, max_bytes: int | None = None) -> bytes | None:
    """按头部 offset/size 读取条目字节；任何一步失败返回 None。

    只认整数（或十进制字符串）形式的 offset/size——Electron 对
    ``size`` 写数字、对 ``offset`` 写字符串；目录条目、unpacked 条目、
    缺失条目、非法类型、越界读取与损坏头部（含深层嵌套 JSON）一律返回
    None——绝不猜、绝不抛异常穿透。
    """
    try:
        with open(path, "rb") as handle:
            prefix = handle.read(_ASAR_HEADER_PREFIX_BYTES)
            if len(prefix) != _ASAR_HEADER_PREFIX_BYTES:
                return None
            _, header_size, _, json_length = struct.unpack("<IIII", prefix)
            if (json_length == 0 or json_length > _ASAR_MAX_JSON_BYTES
                    or header_size < json_length):
                return None
            payload = handle.read(json_length)
            if len(payload) != json_length:
                return None
            document = json.loads(payload.decode("utf-8"))
            if not isinstance(document, dict):
                return None
            node: Any = document
            for part in relative_path.split("/"):
                files = node.get("files") if isinstance(node, dict) else None
                if not isinstance(files, dict) or part not in files:
                    return None
                node = files[part]
                if not isinstance(node, dict):
                    return None
            if node.get("unpacked") is True:
                return None
            offset_value = node.get("offset")
            size_value = node.get("size")
            if not isinstance(offset_value, (str, int)) or not isinstance(
                    size_value, (str, int)) or isinstance(
                        offset_value, bool) or isinstance(size_value, bool):
                return None
            offset = int(offset_value)
            size = int(size_value)
            if offset < 0 or size < 0:
                return None
            if max_bytes is not None and size > max_bytes:
                return None
            # 上界核对（损坏/构造的头部绝不触发无界读取）：条目必须完整落在
            # 内容区之内。没有这一条，size=2**62 这类值会让 read 抛
            # MemoryError 穿透调用方，整轮同步失败而不是按未取证延期。
            file_size = os.fstat(handle.fileno()).st_size
            content_start = 8 + header_size
            if content_start > file_size:
                return None
            if offset > file_size - content_start:
                return None
            if size > file_size - content_start - offset:
                return None
            handle.seek(content_start + offset)
            data = handle.read(size)
    except (OSError, ValueError, UnicodeError, struct.error,
            MemoryError, OverflowError, RecursionError):
        # 同上：深层嵌套 JSON 的 RecursionError 也是「条目不可读」。
        return None
    if len(data) != size:
        return None
    return data


def zcode_hot_sync_admission() -> tuple[bool, str]:
    """Check one confirmed installation against the hot-sync protocol.

    Lock, format and listener evidence are mandatory. Version metadata is
    displayed without being compared; unknown compatibility keeps pending.
    """
    scan = zcode_process_scan()
    if scan.snapshot_error is not None:
        return False, ("进程快照创建失败（%s），无法确认安装"
                       % _error_text(scan.snapshot_error))
    if scan.enumeration_error is not None:
        return False, ("进程枚举中断（%s），不能以部分结果判定安装"
                       % _error_text(scan.enumeration_error))
    if scan.unreadable_pids:
        return False, ("无法确认进程身份/镜像路径（pid: %s），"
                       "不能以部分结果判定唯一安装"
                       % "、".join(str(pid) for pid in scan.unreadable_pids))
    roots = _dedupe_roots(scan.paths)
    if not roots:
        if scan.exited_pids:
            return False, ("未找到运行中的 ZCode 安装（%d 个匹配进程已确证退出）"
                           % len(scan.exited_pids))
        return False, "未找到运行中的 ZCode 安装（没有匹配进程）"
    if len(roots) > 1:
        return False, ("运行中的 ZCode 来自 %d 个安装根（%s），无法判定被准入的"
                       "构建" % (len(roots),
                                "；".join(str(root) for root in roots)))
    root = roots[0]
    asar = root / "resources" / "app.asar"
    try:
        asar_present = asar.is_file()
    except OSError:
        asar_present = False
    if not asar_present:
        return False, "resources/app.asar 不在场（%s）" % asar
    if read_asar_header(asar) is None:
        return False, "resources/app.asar 头部无法解析（%s）" % asar
    version = "未知"
    package_raw = read_asar_entry(asar, "package.json", max_bytes=65536)
    if package_raw is not None:
        try:
            package = json.loads(package_raw.decode("utf-8"))
            observed = package.get("version") if isinstance(package, dict) else None
            if isinstance(observed, str) and observed.strip():
                version = " ".join(observed.split())[:64]
        except (ValueError, UnicodeError, RecursionError):
            pass  # Metadata failure never substitutes for protocol evidence.
    compatible, reason = inspect_installed_protocol(
        asar, lambda archive, entry: read_asar_entry(
            archive, entry, max_bytes=8 * 1024 * 1024))
    return compatible, "包版本 %s（仅展示）：%s" % (version, reason)
