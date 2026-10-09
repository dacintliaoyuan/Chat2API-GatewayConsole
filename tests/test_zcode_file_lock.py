# -*- coding: utf-8 -*-
"""Rev11 协作锁（ZCode 客户端兼容协议）的受跟踪回归。

协议来源：本机已安装 3.14.4.7912 的参考源码
``packages/shared/src/node/atomicFileLock.ts``（:51-236）与
``packages/shared/src/node/privateFilePersistence.ts``（:7-10）：
``<file>.lock`` 目录 + ``owner-<pid>-<ts>-<rand>.json``，只有 owner 已退出
（``process.kill(pid, 0)`` 报 ESRCH 等价）或无 owner 且超过 ownerless
grace 才可回收；超时错误码 ``ZCODE_FILE_LOCK_TIMEOUT``。

隔离契约：每个用例只在自己的临时目录内构造锁文件与 owner 元数据，绝不读取
或写入真实的 ~/.zcode 配置。存活判定用本进程 pid（活）与一个不存在的高位
pid（OS 层 ERROR_INVALID_PARAMETER / ESRCH，与「进程已退出」同一条件）；
Windows 专属用例另派生一个立即退出的子进程（仅 `--version`，随即收割）
对照 GetExitCodeProcess 的退出码，不读写任何真实配置。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zcode_file_lock as lock_module
from zcode_file_lock import (
    ZCODE_FILE_LOCK_TIMEOUT,
    ZCodeFileLockTimeout,
    zcode_file_lock,
)

# 不存在的高位 pid：Windows 上 OpenProcess 报 87（参数错误），POSIX 上
# kill 报 ESRCH——与「owner 进程已退出」是同一个可确证的不存在条件。
NO_SUCH_PID = 2147483647


class ZCodeFileLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.file = self.root / "provider_config.json"
        self.file.write_text("{}", encoding="utf-8")
        self.lock_path = Path(str(self.file) + ".lock")

    def _write_owner(self, pid, token="client", created_at=None):
        """按客户端布局写一份锁目录 + owner 文件。"""
        self.lock_path.mkdir()
        payload = {
            "pid": pid,
            "token": token,
            "createdAt": int(created_at if created_at is not None
                              else time.time() * 1000),
        }
        (self.lock_path / ("owner-%s.json" % token)).write_text(
            json.dumps(payload), encoding="utf-8")

    def test_acquire_writes_the_client_owner_layout(self):
        # 锁目录 + owner-<pid>-<ts>-<rand>.json{pid,createdAt,token}：
        # 与客户端 atomicFileLock.ts:182-187 的文件名模式与字段同形。
        with zcode_file_lock(str(self.file)):
            self.assertTrue(self.lock_path.is_dir())
            owners = [name for name in os.listdir(self.lock_path)
                      if name.startswith("owner-") and name.endswith(".json")]
            self.assertEqual(len(owners), 1)
            meta = json.loads(
                (self.lock_path / owners[0]).read_text("utf-8"))
            self.assertEqual(sorted(meta), ["createdAt", "pid", "token"])
            self.assertEqual(meta["pid"], os.getpid())
            self.assertIsInstance(meta["createdAt"], int)
            self.assertTrue(meta["token"].startswith("%d-" % os.getpid()))
        # 释放后锁目录消失，且可再次取得。
        self.assertFalse(self.lock_path.exists())
        with zcode_file_lock(str(self.file)):
            self.assertTrue(self.lock_path.is_dir())

    def test_live_owner_blocks_and_times_out_with_the_client_code(self):
        # 活 owner 不可回收：等到 maxWait 后抛明确错误码，锁目录原样保留
        # （绝不静默继续，也绝不误删）。
        self._write_owner(os.getpid())
        started = time.time()
        with self.assertRaises(ZCodeFileLockTimeout) as caught:
            with zcode_file_lock(str(self.file), retry_delays_ms=(10,),
                                 max_wait_ms=250):
                self.fail("活 owner 的锁不得被取得")
        waited = (time.time() - started) * 1000
        self.assertEqual(caught.exception.code, ZCODE_FILE_LOCK_TIMEOUT)
        self.assertGreaterEqual(waited, 200)
        self.assertTrue(self.lock_path.is_dir())
        self.assertEqual(len(os.listdir(self.lock_path)), 1)

    def test_dead_owner_is_reclaimed(self):
        # owner 元数据指向已退出的进程（ESRCH 等价）→ 锁可回收、随后可取得。
        self._write_owner(NO_SUCH_PID, token="dead")
        with zcode_file_lock(str(self.file), max_wait_ms=3000):
            self.assertTrue(self.lock_path.is_dir())

    def test_ownerless_past_grace_is_reclaimed(self):
        # 无 owner 文件（mkdir 后、落盘前崩溃的形态）且超过 grace → 可回收。
        self.lock_path.mkdir()
        time.sleep(0.3)
        with zcode_file_lock(str(self.file), ownerless_grace_ms=100,
                             max_wait_ms=3000):
            self.assertTrue(self.lock_path.is_dir())

    def test_ownerless_within_grace_waits_for_the_grace(self):
        # owner 元数据无 pid 且未超 grace：先等待，约到 grace 才取得
        # （grace 被夹在 maxWait/2 内，与客户端 atomicFileLock.ts:190-193 同义）。
        self.lock_path.mkdir()
        (self.lock_path / "owner-young.json").write_text(
            json.dumps({"createdAt": int(time.time() * 1000)}),
            encoding="utf-8")
        started = time.time()
        with zcode_file_lock(str(self.file), ownerless_grace_ms=300,
                             max_wait_ms=3000, retry_delays_ms=(50,)):
            waited = (time.time() - started) * 1000
        self.assertGreaterEqual(waited, 250)

    def test_release_never_removes_a_replaced_lock(self):
        # 释放只删自己的 owner 文件；锁目录已被后来写者接管时绝不误删。
        holder = zcode_file_lock(str(self.file))
        holder.__enter__()
        replaced = self.root / "replaced.lock"
        os.rename(self.lock_path, replaced)
        self._write_owner(os.getpid(), token="other")
        holder.__exit__(None, None, None)
        self.assertTrue(self.lock_path.is_dir())
        self.assertEqual(os.listdir(self.lock_path), ["owner-other.json"])
        self.assertTrue(replaced.is_dir())

    def test_legacy_single_file_lock_is_reclaimed(self):
        # 兼容升级前遗留的单文件锁：owner 已退出 → 删除后取得。
        self.lock_path.write_text(json.dumps({
            "pid": NO_SUCH_PID,
            "createdAt": int(time.time() * 1000),
            "token": "legacy",
        }), encoding="utf-8")
        with zcode_file_lock(str(self.file), max_wait_ms=3000):
            self.assertTrue(self.lock_path.is_dir())

    def test_process_alive_checks(self):
        # 存活判定：本进程为真；不存在的高位 pid 为假（保守：其余错误判活）。
        self.assertTrue(lock_module.process_alive(os.getpid()))
        self.assertFalse(lock_module.process_alive(NO_SUCH_PID))

    @unittest.skipUnless(os.name == "nt",
                         "Windows 专属：已退出的进程对象仍可被 OpenProcess 打开")
    def test_exited_child_with_held_handle_is_not_alive(self):
        # 缺陷回归：子进程已退出、父进程仍持有 Popen 句柄（进程对象未销毁）
        # 时，对 pid 的 OpenProcess 依旧成功，旧实现据此直接判活 → 死
        # owner 的锁永不可回收。正确语义对照 GetExitCodeProcess：退出码
        # 不再是 STILL_ACTIVE(259) 即已退出，与 Node process.kill(pid, 0)
        # 对已退出进程抛 ESRCH 一致。
        import ctypes
        import subprocess

        child = subprocess.Popen(
            [sys.executable, "--version"], stdout=subprocess.DEVNULL)

        def _reap(popen):
            if popen.poll() is None:
                popen.kill()
            popen.wait()

        self.addCleanup(_reap, child)
        deadline = time.time() + 10
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(child.poll(), "python --version 应立即退出")
        # 独立事实源：句柄在手时 GetExitCodeProcess 读到真实退出码而不是
        # STILL_ACTIVE——证明「OpenProcess 成功」不等于「进程存活」。
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, child.pid)
        self.assertTrue(handle, "已退出但句柄未释放的进程对象应仍可打开")
        try:
            exit_code = ctypes.c_ulong()
            self.assertTrue(kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)))
            self.assertNotEqual(exit_code.value, still_active)
        finally:
            kernel32.CloseHandle(handle)
        self.assertFalse(lock_module.process_alive(child.pid))


if __name__ == "__main__":
    unittest.main()
