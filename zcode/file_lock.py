# -*- coding: utf-8 -*-
"""ZCode 客户端兼容的文件锁：``<file>.lock`` 目录 + owner json。

协议来源（本机已安装 3.14.4.7912 的参考源码，非 tmp 协议模型）：

* ``packages/shared/src/node/atomicFileLock.ts``（:51-236）：
  ``acquireFileLock`` 用 ``mkdir(<file>.lock)`` 取锁，写
  ``owner-<pid>-<ts>-<rand>.json``（``{pid, createdAt, token}``），
  释放只删自己的 owner 文件再 ``rmdir``；回收条件只有 owner 进程已退出
  （``process.kill(pid, 0)`` 报 ESRCH）或 owner 元数据缺失/非法且超过
  ownerless grace；达到 ``maxWait`` 直接超时，绝不在超时后再做一次陈旧回收。
* ``packages/shared/src/node/privateFilePersistence.ts``（:7-10）：
  默认 ``retryDelays [25,50,100,200,400]``、``ownerlessGrace 100ms``、
  ``maxWait 8000ms``；``withFileLock`` 只把锁包在 operation 外。
* 超时错误码 ``ZCODE_FILE_LOCK_TIMEOUT``（``errors.ts`` 常量）。

与 Node 的差异（本机 CPython 3.14.6 实测）：

* ``process.kill(pid, 0)`` 在 Node/Windows 上经 libuv 是纯存活探测；Python 的
  ``os.kill(pid, 0)`` 在旧版 Windows CPython 上会走 TerminateProcess，不能
  直接照抄。Windows 上改用 ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``：
  打不开且错误码 87（ERROR_INVALID_PARAMETER＝进程不存在）判为已退出，其余
  失败（如 5 拒绝访问）保守判为存活；打得开也不等于存活，还要对照
  ``GetExitCodeProcess`` 的退出码——已退出但句柄仍被持有的进程（如父进程
  未收割的子进程）OpenProcess 依旧成功，退出码不再是 STILL_ACTIVE(259)
  即判为已退出；POSIX 上仍用 ``os.kill(pid, 0)`` + ``errno.ESRCH``。
* 锁目录被并发替换时靠「mkdir 前后 dev/ino 一致 + 唯一 owner 文件是自己」
  校验；回收只删观察到的 owner 文件，随后 ``rmdir`` 因目录非空而拒绝删除
  后来写者的新锁，绝不误删。
* owner 元数据非法时回退到 owner 文件 mtime，并用观察者固定「同一实例」的
  首次观察时间，避免被反复 touch 的损坏锁无限延长 grace。

本模块只提供锁，不读取也不写入任何业务内容；调用方在锁内完成重读、合并与
替换（``model_sync`` 的写入窗口）。注释为简体中文。
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import math
import os
import secrets
import shutil
import stat as stat_module
import time
from pathlib import Path
from typing import Any, Iterator

# 与客户端 privateFilePersistence.ts:7-10 相同的默认参数。
DEFAULT_RETRY_DELAYS_MS: tuple[int, ...] = (25, 50, 100, 200, 400)
DEFAULT_OWNERLESS_GRACE_MS = 100
DEFAULT_MAX_WAIT_MS = 8000
# 与客户端 atomicFileLock.ts:7 相同：明显未来的 createdAt 不可信。
MAX_LOCK_METADATA_CLOCK_SKEW_MS = 5 * 60_000
# 与客户端 errors.ts 的 ZCODE_FILE_LOCK_TIMEOUT_ERROR_CODE 相同。
ZCODE_FILE_LOCK_TIMEOUT = "ZCODE_FILE_LOCK_TIMEOUT"
# 观察者表：损坏锁的 mtime 回退时间按「同一文件实例」只认首次观察。
_LOCK_INSTANCE_OBSERVATIONS: dict[str, tuple[tuple[Any, ...], int]] = {}


class ZCodeFileLockTimeout(RuntimeError):
    """等待客户端文件锁超时；``code`` 为跨实现约定的错误码。

    超时是明确失败，绝不静默继续：调用方要么如实上报失败，要么把本轮
    客户端写入整体延期，不能把「没拿到锁」当成「没有并发写者」。
    """

    code = ZCODE_FILE_LOCK_TIMEOUT

    def __init__(self, path: Path | str, lock_path: str, waited_ms: int):
        super().__init__(
            "等待 ZCode 文件锁超时（%dms，code=%s）：%s；本轮不静默继续"
            % (waited_ms, ZCODE_FILE_LOCK_TIMEOUT, lock_path))
        self.path = str(path)
        self.lock_path = lock_path
        self.waited_ms = waited_ms


def _now_ms() -> int:
    return int(time.time() * 1000)


def _random_suffix() -> str:
    # 客户端用 Math.random().toString(36).slice(2)；这里用等价的 36 进制随机串。
    # token 只是所有权标识（不是凭据），仍用密码学安全随机源，避免可预测碰撞。
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def process_alive(pid: int) -> bool:
    """owner 进程存活判定；只有可确证「不存在」才返回 False（保守）。"""
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        error_still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid))
        if not handle:
            # 87＝进程不存在（ESRCH 等价）；拒绝访问等其余失败保守视为存活，
            # 绝不因探测失败误回收活 owner 的锁（对应 Node 的 EPERM 不判死）。
            return kernel32.GetLastError() != error_invalid_parameter
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # 退出码读不到：与打不开同类，保守视为存活。
                return True
            # 进程对象能打开不等于存活：已退出但句柄仍被持有（如父进程未
            # 收割的 Popen）时 OpenProcess 仍成功，退出码已落定不再是
            # STILL_ACTIVE——对应 Node 对已退出进程抛 ESRCH。
            return exit_code.value == error_still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return getattr(exc, "errno", None) != errno.ESRCH
    except Exception:
        return True
    return True


def _parse_lock_timestamp(value: Any, observed_at: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    value = int(value)
    if value < 0 or value > observed_at + MAX_LOCK_METADATA_CLOCK_SKEW_MS:
        return None
    return value


def _parse_owner_metadata(raw: str | None, observed_at: int) -> tuple[int | None, int | None]:
    """owner 元数据 -> (createdAt, pid)；非法值返回 None（回退 mtime）。"""
    if raw is None:
        return None, None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    created_at = _parse_lock_timestamp(parsed.get("createdAt"), observed_at)
    pid = parsed.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    return created_at, pid


def _observe_lock_instance(path: str, lock_stat: os.stat_result,
                            observed_at: int) -> int:
    identity = (
        lock_stat.st_dev,
        lock_stat.st_ino,
        getattr(lock_stat, "st_birthtime_ns", None) or lock_stat.st_ctime_ns,
        lock_stat.st_mtime_ns,
    )
    previous = _LOCK_INSTANCE_OBSERVATIONS.get(path)
    if previous is not None and previous[0] == identity:
        return previous[1]
    _LOCK_INSTANCE_OBSERVATIONS[path] = (identity, observed_at)
    return observed_at


def _owner_reclaimable(owner_path: str, grace_ms: int) -> bool:
    """owner 文件是否可回收：owner 已退出，或无 pid 且超过 ownerless grace。"""
    observed_at = _now_ms()
    try:
        with io.open(owner_path, encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        raw = None
    created_at, pid = _parse_owner_metadata(raw, observed_at)
    if created_at is None:
        try:
            owner_stat = os.stat(owner_path)
        except OSError:
            return False
        created_at = _parse_lock_timestamp(
            int(owner_stat.st_mtime * 1000), observed_at)
        if created_at is None:
            created_at = _observe_lock_instance(owner_path, owner_stat,
                                                observed_at)
    owner_exited = pid is not None and not process_alive(pid)
    ownerless_stale = pid is None and observed_at - created_at >= grace_ms
    return owner_exited or ownerless_stale


def _best_effort_cleanup(owner_path: str, lock_path: str) -> None:
    try:
        os.remove(owner_path)
    except OSError:
        pass
    try:
        os.rmdir(lock_path)
    except OSError:
        pass


def _remove_abandoned_lock(lock_path: str, grace_ms: int) -> tuple[bool, OSError | None]:
    """尝试回收陈旧锁；返回 (是否已回收, 回收错误)。

    只清理「观察到的」条目：目录被替换后，后来写者的 owner 文件名不同，
    当前删除不会命中它，随后的 ``rmdir`` 也会因目录非空而拒绝。
    """
    try:
        lock_stat = os.stat(lock_path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            # 锁已消失：按「已回收」处理，调用方重试即可。
            return True, None
        return False, exc
    if stat_module.S_ISDIR(lock_stat.st_mode):
        try:
            entries = os.listdir(lock_path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return True, None
            return False, exc
        owners = [entry for entry in entries
                  if entry.startswith("owner-") and entry.endswith(".json")]
        if len(owners) == 1:
            owner_path = os.path.join(lock_path, owners[0])
            if not _owner_reclaimable(owner_path, grace_ms):
                return False, None
            # 删除唯一 owner 文件即等于所有权校验。
            try:
                os.remove(owner_path)
                os.rmdir(lock_path)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTEMPTY):
                    return False, None
                return False, exc
            return True, None
        observed_at = _now_ms()
        directory_created = _parse_lock_timestamp(
            int(lock_stat.st_mtime * 1000), observed_at)
        if directory_created is None:
            directory_created = _observe_lock_instance(
                lock_path, lock_stat, observed_at)
        if observed_at - directory_created < grace_ms:
            return False, None
        for owner in owners:
            if not _owner_reclaimable(os.path.join(lock_path, owner), grace_ms):
                return False, None
        # mkdir 成功后、owner 文件落盘前崩溃会留下空目录；损坏目录也可能
        # 含多个 owner。只清理观察到的条目，后来 owner 新增会让 rmdir 失败。
        try:
            for entry in entries:
                target = os.path.join(lock_path, entry)
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    os.remove(target)
            os.rmdir(lock_path)
        except OSError as exc:
            return False, exc
        return True, None
    # 兼容升级前遗留的单文件锁：读原字节 → 可回收 → 内容未变才删除。
    try:
        with io.open(lock_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return False, exc
    if not _owner_reclaimable(lock_path, grace_ms):
        return False, None
    try:
        with io.open(lock_path, "rb") as handle:
            if handle.read() != raw:
                return False, None
    except OSError as exc:
        return False, exc
    try:
        os.remove(lock_path)
    except OSError as exc:
        return False, exc
    return True, None


def _acquire(path: Path | str, retry_delays_ms: tuple[int, ...],
              ownerless_grace_ms: int, max_wait_ms: int):
    """按客户端协议取得 ``<path>.lock``；返回释放闭包。"""
    lock_path = str(path) + ".lock"
    pid = os.getpid()
    token = "%d-%d-%s" % (pid, _now_ms(), _random_suffix())
    owner_path = os.path.join(lock_path, "owner-%s.json" % token)
    payload = json.dumps(
        {"pid": pid, "createdAt": _now_ms(), "token": token},
        ensure_ascii=False) + "\n"
    started_at = _now_ms()
    effective_grace_ms = min(max(int(ownerless_grace_ms), 0),
                            max(int(max_wait_ms) // 2, 0))
    attempt = 0
    last_removal_error: OSError | None = None
    while True:
        created_lock = False
        try:
            os.mkdir(lock_path)
            created_lock = True
            created_stat = os.stat(lock_path)
            # flag="x" 与客户端的 {flag:"wx"} 等价：owner 文件必须新建。
            with io.open(owner_path, "x", encoding="utf-8", newline="") as handle:
                handle.write(payload)
            current_stat = os.stat(lock_path)
            current_owners = [
                entry for entry in os.listdir(lock_path)
                if entry.startswith("owner-") and entry.endswith(".json")]
            if (current_stat.st_dev != created_stat.st_dev or
                    current_stat.st_ino != created_stat.st_ino or
                    current_owners != ["owner-%s.json" % token]):
                # 取锁后目录实例或 owner 变了：本轮放弃，按 EEXIST 竞争重试。
                raise FileExistsError("lock ownership changed during acquire")
            return lambda: _best_effort_cleanup(owner_path, lock_path)
        except OSError as exc:
            if created_lock:
                _best_effort_cleanup(owner_path, lock_path)
            lost_created_lock = created_lock and exc.errno == errno.ENOENT
            if not isinstance(exc, FileExistsError) and not lost_created_lock:
                # 权限等真实错误如实抛出，绝不吞掉重试。
                raise
            elapsed_ms = _now_ms() - started_at
            if elapsed_ms >= max_wait_ms:
                # 达到 maxWait 后直接超时：绝不再做一次陈旧回收，避免越界
                # 删除后来写者的新锁。
                if isinstance(last_removal_error, PermissionError):
                    raise last_removal_error
                raise ZCodeFileLockTimeout(path, lock_path, elapsed_ms)
            removed, removal_error = _remove_abandoned_lock(
                lock_path, effective_grace_ms)
            if removed:
                attempt += 1
                continue
            if removal_error is not None:
                last_removal_error = removal_error
            remaining_ms = max(max_wait_ms - elapsed_ms, 0)
            if not retry_delays_ms or remaining_ms == 0:
                if isinstance(last_removal_error, PermissionError):
                    raise last_removal_error
                raise ZCodeFileLockTimeout(path, lock_path, elapsed_ms)
            delay_ms = retry_delays_ms[min(attempt, len(retry_delays_ms) - 1)]
            time.sleep(min(delay_ms, remaining_ms) / 1000.0)
            attempt += 1


@contextlib.contextmanager
def zcode_file_lock(
        path: Path | str,
        *,
        retry_delays_ms: tuple[int, ...] = DEFAULT_RETRY_DELAYS_MS,
        ownerless_grace_ms: int = DEFAULT_OWNERLESS_GRACE_MS,
        max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
        ) -> Iterator[None]:
    """客户端兼容锁的上下文管理器：只覆盖调用方的写入窗口。

    超时抛 :class:`ZCodeFileLockTimeout`（code=ZCODE_FILE_LOCK_TIMEOUT），
    不静默继续。锁内调用方必须重读文件、合并自己的受管范围后再替换，
    绝不把锁前的旧快照直接写回。
    """
    release = _acquire(path, tuple(retry_delays_ms), ownerless_grace_ms,
                       max_wait_ms)
    try:
        yield
    finally:
        release()
