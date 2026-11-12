# -*- coding: utf-8 -*-
"""Linux browser lookup and POSIX reauthentication lock (R13-1).

Contract:

* Windows keeps the historical ``msvcrt`` byte-range mutex, including the
  zero-byte seed, the position reset and ``LK_NBLCK``/``LK_UNLCK``.
* POSIX takes ``fcntl.flock`` with the same non-blocking attempt, poll interval
  and deadline; when no lock primitive exists the acquire returns ``None`` so the
  caller refuses the session instead of skipping the mutex.
* The lock file stays ``<slot>.lock``; it is never deleted and no mtime-based
  reclaim exists.
* ``CHAT2API_BROWSER_PATH`` is authoritative: a missing or non-executable
  target is a configuration error, never a silent fallback to another browser.
* Linux tries chromium, chromium-browser, google-chrome and microsoft-edge in
  that order through ``shutil.which``.

The fake primitives below assert the selection and the exact lock/unlock calls;
the Linux-only cases drive real ``flock`` through real second processes.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = Path(_TEST_DIR).resolve().parent
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
CHAT2API_PATH = Path(os.path.join(
    os.path.dirname(_TEST_DIR), "chat2api", "chat2api.py"))
SPEC = importlib.util.spec_from_file_location(
    "chat2api_linux_platform", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

SHARE_GATEWAY_PATH = Path(os.path.join(
    os.path.dirname(_TEST_DIR), "chat2api", "share_entry.py"))
SHARE_SPEC = importlib.util.spec_from_file_location(
    "share_entry_linux_platform", SHARE_GATEWAY_PATH)
share_entry = importlib.util.module_from_spec(SHARE_SPEC)
assert SHARE_SPEC.loader is not None
sys.modules[SHARE_SPEC.name] = share_entry
SHARE_SPEC.loader.exec_module(share_entry)


class _FakeMsvcrt:
    LK_NBLCK = 0x01
    LK_UNLCK = 0x02

    def __init__(self):
        self.calls = []

    def locking(self, fd, mode, length):
        self.calls.append((mode, length))


class _FakeFcntl:
    LOCK_EX = 0x02
    LOCK_NB = 0x04
    LOCK_UN = 0x08

    def __init__(self):
        self.calls = []

    def flock(self, fd, operation):
        self.calls.append(operation)


# Real lock holders in child processes: they load chat2api.py and call the real
# lock functions only - no browser, network or service is started. The ready /
# acquired / release files make the contention deterministic.
_LOCK_CHILD_SCRIPT = '''\
import importlib.util
import json
import os
import sys
import time


def load_chat2api(path):
    spec = importlib.util.spec_from_file_location(
        "chat2api_linux_lock_child", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wait_for(path, timeout):
    deadline = time.time() + timeout
    while not os.path.exists(path):
        if time.time() > deadline:
            return False
        time.sleep(0.005)
    return True


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def main():
    module_path, slot, go_path, wait_s, release_path, out_path = sys.argv[1:7]
    chat2api = load_chat2api(module_path)
    chat2api.REAUTH_LOCK_WAIT_S = float(wait_s)
    result = {"pid": os.getpid(), "granted": False}
    write_json(out_path + ".ready", result)
    if not wait_for(go_path, 30.0):
        result["error"] = "no_go"
    else:
        started = time.time()
        handle = chat2api._acquire_reauth_lock(slot)
        result["granted"] = handle is not None
        result["started"] = started
        result["ended"] = time.time()
        write_json(out_path + ".acquired", result)
        if handle is not None:
            wait_for(release_path, 30.0)
            chat2api._release_reauth_lock(handle)
            result["ended"] = time.time()
    write_json(out_path, result)


main()
'''


class _LockChildHarness(unittest.TestCase):
    """Spawn real second processes that drive the real lock functions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "lock_child.py"
        self.script.write_text(_LOCK_CHILD_SCRIPT, encoding="utf-8")
        self.children = []
        self.logs = {}

    def _reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - wedged child
            pass

    def spawn(self, slot, go_path, wait_s, release_path, out_path):
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        try:
            proc = subprocess.Popen(
                [sys.executable, str(self.script), str(CHAT2API_PATH),
                 str(slot), str(go_path), str(wait_s), str(release_path),
                 str(out_path)],
                cwd=str(CHAT2API_PATH.parent), stdout=log, stderr=log)
        finally:
            pass
        self.children.append(proc)
        self.logs[proc.pid] = log
        self.addCleanup(self._reap, proc)
        return proc

    def _child_report(self, procs):
        parts = []
        for proc in procs:
            log = self.logs.get(proc.pid)
            tail = ""
            if log is not None:
                try:
                    log.seek(0)
                    tail = log.read().decode("utf-8", "replace")[-2000:]
                except OSError:  # pragma: no cover - closed handle
                    pass
            parts.append("pid=%s rc=%s log=%s"
                         % (proc.pid, proc.poll(), tail))
        return " | ".join(parts)

    def wait_for_path(self, path, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if Path(path).exists():
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def read_json(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)


class TestBrowserLookup(_LockChildHarness):
    def test_find_edge_delegates_to_find_browser(self):
        with mock.patch.object(
                chat2api, "find_browser", return_value="browser") as finder:
            found = chat2api.find_edge(["candidate"])
        self.assertEqual(found, "browser")
        finder.assert_called_once_with(["candidate"])

    def test_windows_platform_selects_msvcrt(self):
        fake = object()

        def importer(name):
            self.assertEqual(name, "msvcrt")
            return fake

        kind, module = chat2api._reauth_lock_primitive("win32", importer)
        self.assertEqual(kind, "msvcrt")
        self.assertIs(module, fake)

    def test_posix_platform_selects_fcntl(self):
        fake = object()

        def importer(name):
            self.assertEqual(name, "fcntl")
            return fake

        kind, module = chat2api._reauth_lock_primitive("linux", importer)
        self.assertEqual(kind, "fcntl")
        self.assertIs(module, fake)

    def test_windows_acquire_uses_byte_range_lock(self):
        fake = _FakeMsvcrt()
        slot = os.path.join(tempfile.mkdtemp(), "windows.state")
        self.addCleanup(lambda: os.path.exists(slot + ".lock")
                        and os.unlink(slot + ".lock"))
        handle = chat2api._acquire_reauth_lock(
            slot, primitive=("msvcrt", fake))
        self.assertIsNotNone(handle)
        self.assertEqual(fake.calls, [(fake.LK_NBLCK, 1)])
        self.assertEqual(os.path.getsize(slot + ".lock"), 1)
        chat2api._release_reauth_lock(handle)
        self.assertEqual(fake.calls, [(fake.LK_NBLCK, 1),
                                    (fake.LK_UNLCK, 1)])

    def test_posix_acquire_uses_flock(self):
        fake = _FakeFcntl()
        slot = os.path.join(tempfile.mkdtemp(), "posix.state")
        self.addCleanup(lambda: os.path.exists(slot + ".lock")
                        and os.unlink(slot + ".lock"))
        handle = chat2api._acquire_reauth_lock(
            slot, primitive=("fcntl", fake))
        self.assertIsNotNone(handle)
        self.assertEqual(fake.calls, [fake.LOCK_EX | fake.LOCK_NB])
        chat2api._release_reauth_lock(handle)
        self.assertEqual(fake.calls, [fake.LOCK_EX | fake.LOCK_NB,
                                     fake.LOCK_UN])

    def test_missing_lock_primitive_refuses_the_session(self):
        slot = os.path.join(tempfile.mkdtemp(), "missing.state")
        with mock.patch.object(
                chat2api, "_reauth_lock_primitive", return_value=(None, None)):
            granted, reason, ledger = chat2api.acquire_reauth_slot(
                slot, pid=111, ttl_s=60.0, now=1000.0)
        self.assertFalse(granted)
        self.assertEqual(reason, "coordination_unavailable")
        self.assertEqual(ledger["attempts_in_window"], 0)

    def test_missing_browser_is_login_unavailable(self):
        with mock.patch.object(chat2api.sys, "platform", "win32"), \
             mock.patch.object(chat2api, "find_edge", return_value=None):
            with self.assertRaises(chat2api.LoginUnavailable) as caught:
                chat2api.browser_login("https://example.invalid", "profile", 1)
        self.assertEqual(caught.exception.reason, "edge_missing")
        self.assertIn("was not found", str(caught.exception))

    def test_windows_missing_browser_stays_edge_missing(self):
        with mock.patch.object(chat2api.sys, "platform", "win32"), \
             mock.patch.object(chat2api, "find_edge", return_value=None):
            with self.assertRaises(chat2api.LoginUnavailable) as caught:
                chat2api.browser_login("https://example.invalid", "profile", 1)
        self.assertEqual(caught.exception.reason, "edge_missing")
        self.assertIn("Microsoft Edge was not found", str(caught.exception))

    def test_posix_missing_browser_is_browser_missing(self):
        with mock.patch.object(chat2api.sys, "platform", "linux"), \
             mock.patch.object(chat2api, "find_edge",
                               return_value=None), \
             mock.patch.dict(os.environ, {"DISPLAY": ":0",
                                         "WAYLAND_DISPLAY": ""}):
            with self.assertRaises(chat2api.LoginUnavailable) as caught:
                chat2api.browser_login("https://example.invalid", "profile", 1)
        self.assertEqual(caught.exception.reason, "browser_missing")
        # 历史日志仍能识别 "Edge was not found" 这个条件。
        self.assertIn("Edge was not found", str(caught.exception))

    def test_posix_missing_display_is_display_missing(self):
        for display in ("", "   "):
            with self.subTest(display=display):
                with mock.patch.object(chat2api.sys, "platform", "linux"), \
                     mock.patch.object(
                         chat2api, "find_edge",
                         return_value="/usr/bin/chromium"), \
                     mock.patch.dict(os.environ,
                                     {"DISPLAY": display,
                                      "WAYLAND_DISPLAY": ""}):
                    with self.assertRaises(
                            chat2api.LoginUnavailable) as caught:
                        chat2api.browser_login(
                            "https://example.invalid", "profile", 1)
                self.assertEqual(caught.exception.reason, "display_missing")
                self.assertIn("DISPLAY", str(caught.exception))

    def test_posix_wayland_display_reaches_the_browser_check(self):
        with mock.patch.object(chat2api.sys, "platform", "linux"), \
             mock.patch.object(chat2api, "find_edge",
                               return_value=None), \
             mock.patch.dict(os.environ, {"DISPLAY": "",
                                         "WAYLAND_DISPLAY": "wayland-0"}):
            with self.assertRaises(chat2api.LoginUnavailable) as caught:
                chat2api.browser_login("https://example.invalid", "profile", 1)
        self.assertEqual(caught.exception.reason, "browser_missing")

    def test_explicit_override_missing_path_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing-browser")
            with mock.patch.dict(
                    os.environ, {chat2api.BROWSER_PATH_ENV: missing}):
                with self.assertRaises(chat2api.BrowserConfigError) as caught:
                    chat2api.find_browser()
        self.assertEqual(caught.exception.reason, "browser_path_missing")
        self.assertIn(missing, caught.exception.detail)

    def test_explicit_override_wins_over_candidates(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        with mock.patch.dict(os.environ, {chat2api.BROWSER_PATH_ENV: path}):
            found = chat2api.find_browser(
                candidates=[r"C:\missing\msedge.exe"], platform="win32",
                which=lambda name: "/missing/" + name)
        self.assertEqual(found, path)

    def test_browser_login_maps_override_error_to_login_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing-browser")
            with mock.patch.dict(
                    os.environ, {chat2api.BROWSER_PATH_ENV: missing}):
                with self.assertRaises(chat2api.LoginUnavailable) as caught:
                    chat2api.browser_login(
                        "https://example.invalid", "profile", 1)
        self.assertEqual(caught.exception.reason, "browser_config_error")
        self.assertIn("browser_path_missing", caught.exception.detail)

    def test_linux_candidates_keep_declared_order(self):
        calls = []

        def which(name):
            calls.append(name)
            return "/opt/browsers/" + name

        existing = ("/opt/browsers/chromium-browser",
                    "/opt/browsers/google-chrome")
        with mock.patch.object(
                chat2api.os.path, "exists",
                side_effect=lambda path: path in existing):
            found = chat2api.find_browser(platform="linux", which=which)
        self.assertEqual(found, "/opt/browsers/chromium-browser")
        self.assertEqual(calls, list(chat2api.LINUX_BROWSER_NAMES))

    def test_linux_candidates_collapse_duplicate_paths(self):
        def which(name):
            if name in ("chromium", "chromium-browser"):
                return "/usr/bin/chromium"
            return None

        self.assertEqual(chat2api.linux_browser_candidates(which),
                         ["/usr/bin/chromium"])

    def test_wait_timeout_returns_none_against_a_real_holder(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        slot = str(directory / "timeout.state")
        go_path = directory / "go"
        release_path = directory / "release"
        out_path = directory / "out.json"
        proc = self.spawn(slot, go_path, 30.0, release_path, out_path)
        self.assertTrue(self.wait_for_path(str(out_path) + ".ready"),
                        self._child_report([proc]))
        go_path.touch()
        self.assertTrue(self.wait_for_path(str(out_path) + ".acquired"),
                        self._child_report([proc]))
        holder = self.read_json(str(out_path) + ".acquired")
        self.assertTrue(holder["granted"], self._child_report([proc]))
        try:
            with mock.patch.object(chat2api, "REAUTH_LOCK_WAIT_S", 0.2):
                started = time.time()
                handle = chat2api._acquire_reauth_lock(slot)
                elapsed = time.time() - started
            self.assertIsNone(handle, self._child_report([proc]))
            self.assertGreaterEqual(elapsed, 0.15)
        finally:
            release_path.touch()
        self.assertTrue(self.wait_for_path(str(out_path)),
                        self._child_report([proc]))
        self.assertEqual(proc.wait(timeout=30), 0, self._child_report([proc]))


class TestInstancePipePrefixDefault(unittest.TestCase):
    """--instance-pipe-prefix：显式值优先，缺省按平台取默认值。"""

    def test_explicit_value_wins_and_default_is_not_read(self):
        with mock.patch.object(
                share_entry, "_default_instance_pipe_prefix",
                side_effect=AssertionError("default must not be read")):
            self.assertEqual(
                share_entry._resolve_instance_pipe_prefix(
                    r"\\.\pipe\um-share-instance-exp-"),
                r"\\.\pipe\um-share-instance-exp-")

    def test_omitted_value_resolves_the_platform_default(self):
        for expected in ("/run/um-share/instance-",
                         r"\\.\pipe\um-share-instance-"):
            with self.subTest(expected=expected):
                with mock.patch.object(
                        share_entry, "_default_instance_pipe_prefix",
                        return_value=expected) as default:
                    self.assertEqual(
                        share_entry._resolve_instance_pipe_prefix(None),
                        expected)
                    default.assert_called_once_with()

    def test_public_default_seam_reaches_the_resolver(self):
        with mock.patch.object(
                share_entry, "default_instance_pipe_prefix",
                return_value="/run/um-share/instance-") as default:
            self.assertEqual(
                share_entry._resolve_instance_pipe_prefix(None),
                "/run/um-share/instance-")
            default.assert_called_once_with()

    def test_unavailable_instance_module_yields_none_not_a_guess(self):
        with mock.patch.object(
                share_entry, "_default_instance_pipe_prefix",
                side_effect=ImportError("share_instance_pipe unavailable")):
            self.assertIsNone(
                share_entry._resolve_instance_pipe_prefix(None))

    def test_parse_args_resolves_an_omitted_prefix(self):
        parser = mock.Mock()
        parser.parse_args.return_value = mock.Mock(instance_pipe_prefix=None)
        with mock.patch.object(
                share_entry, "_default_instance_pipe_prefix",
                return_value="/run/um-share/instance-"):
            args = share_entry._parse_args(parser, [])
        self.assertEqual(args.instance_pipe_prefix,
                         "/run/um-share/instance-")

    def test_parse_args_keeps_an_explicit_prefix(self):
        parser = mock.Mock()
        parser.parse_args.return_value = mock.Mock(
            instance_pipe_prefix=r"\\.\pipe\custom-")
        with mock.patch.object(
                share_entry, "_default_instance_pipe_prefix",
                side_effect=AssertionError("default must not be read")):
            args = share_entry._parse_args(parser, [])
        self.assertEqual(args.instance_pipe_prefix, r"\\.\pipe\custom-")

    def test_real_default_matches_the_instance_channel_module(self):
        import share_instance_pipe
        self.assertEqual(
            share_entry._default_instance_pipe_prefix(),
            share_instance_pipe.default_instance_pipe_prefix())

    def test_real_omitted_prefix_is_the_platform_default(self):
        import share_instance_pipe
        self.assertEqual(
            share_entry._resolve_instance_pipe_prefix(None),
            share_instance_pipe.default_instance_pipe_prefix())

    def test_posix_default_is_the_unix_socket_prefix(self):
        import share_instance_pipe
        with mock.patch.object(share_instance_pipe.os, "name", "posix"):
            self.assertEqual(
                share_instance_pipe.default_instance_pipe_prefix(),
                "/run/um-share/instance-")


@unittest.skipUnless(sys.platform == "linux",
                     "Linux-only real POSIX flock semantics")
class LinuxRealLockTests(_LockChildHarness):
    def setUp(self):
        super().setUp()
        fd, path = tempfile.mkstemp(dir=str(self.dir))
        os.close(fd)
        os.chmod(path, 0o644)
        self.non_executable = path

    def test_real_linux_primitive_is_fcntl(self):
        import fcntl
        kind, module = chat2api._reauth_lock_primitive()
        self.assertEqual(kind, "fcntl")
        self.assertIs(module, fcntl)

    def test_explicit_override_must_be_executable(self):
        with mock.patch.dict(
                os.environ,
                {chat2api.BROWSER_PATH_ENV: self.non_executable}):
            with self.assertRaises(chat2api.BrowserConfigError) as caught:
                chat2api.find_browser()
        self.assertEqual(caught.exception.reason,
                         "browser_path_not_executable")
        self.assertIn(self.non_executable, caught.exception.detail)

    def test_lock_directory_and_file_are_private(self):
        slot = str(self.dir / "private" / "private.state")
        handle = chat2api._acquire_reauth_lock(slot)
        self.assertIsNotNone(handle)
        try:
            lock_path = Path(handle.path)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(lock_path.parent.stat().st_mode & 0o777, 0o700)
        finally:
            chat2api._release_reauth_lock(handle)

    def test_two_processes_contend_for_one_lock(self):
        slot = str(self.dir / "contended.state")
        go_path = self.dir / "go"
        release_path = self.dir / "release"
        out_paths = [self.dir / "out-0.json", self.dir / "out-1.json"]
        procs = [self.spawn(slot, go_path, 0.5, release_path, out_path)
                 for out_path in out_paths]
        for out_path in out_paths:
            self.assertTrue(
                self.wait_for_path(str(out_path) + ".ready"),
                self._child_report(procs))
        go_path.touch()
        acquired = []
        for out_path in out_paths:
            self.assertTrue(
                self.wait_for_path(str(out_path) + ".acquired"),
                self._child_report(procs))
            acquired.append(self.read_json(str(out_path) + ".acquired"))
        granted = [entry for entry in acquired if entry["granted"]]
        refused = [entry for entry in acquired if not entry["granted"]]
        self.assertEqual(len(granted), 1, self._child_report(procs))
        self.assertEqual(len(refused), 1, self._child_report(procs))
        self.assertGreaterEqual(refused[0]["ended"] - refused[0]["started"],
                                0.3)
        release_path.touch()
        for out_path in out_paths:
            self.assertTrue(self.wait_for_path(str(out_path)),
                            self._child_report(procs))
        for proc in procs:
            self.assertEqual(proc.wait(timeout=30), 0,
                             self._child_report(procs))

    def test_lock_is_released_when_the_holder_process_dies(self):
        slot = str(self.dir / "crash.state")
        go_path = self.dir / "crash-go"
        release_path = self.dir / "never-released"
        out_path = self.dir / "crash-out.json"
        proc = self.spawn(slot, go_path, 30.0, release_path, out_path)
        self.assertTrue(self.wait_for_path(str(out_path) + ".ready"),
                        self._child_report([proc]))
        go_path.touch()
        self.assertTrue(self.wait_for_path(str(out_path) + ".acquired"),
                        self._child_report([proc]))
        held = self.read_json(str(out_path) + ".acquired")
        self.assertTrue(held["granted"], self._child_report([proc]))
        with mock.patch.object(chat2api, "REAUTH_LOCK_WAIT_S", 0.2):
            self.assertIsNone(chat2api._acquire_reauth_lock(slot),
                              self._child_report([proc]))
        proc.kill()
        proc.wait(timeout=30)
        with mock.patch.object(chat2api, "REAUTH_LOCK_WAIT_S", 5.0):
            handle = chat2api._acquire_reauth_lock(slot)
        self.assertIsNotNone(handle, self._child_report([proc]))
        chat2api._release_reauth_lock(handle)
        self.assertTrue(Path(slot + ".lock").exists())

    def test_external_unlink_does_not_release_the_holders_lock(self):
        # 已知平台差异，不是产品漏洞：POSIX 允许同用户删除打开中的文件，
        # 产品代码自身从不 unlink 锁文件（R13-1）。这里用硬链接保住 inode，
        # 删除原路径后第二个 flock 仍被持有者挡住；0700 目录 + 0600 文件
        # 只是缓解外部删除，不是保证。
        import fcntl
        slot = str(self.dir / "external-unlink.state")
        handle = chat2api._acquire_reauth_lock(slot)
        self.assertIsNotNone(handle)
        lock_path = Path(handle.path)
        probe_path = str(lock_path) + ".probe"
        try:
            os.link(str(lock_path), probe_path)
            os.unlink(str(lock_path))
            self.assertFalse(lock_path.exists())
            probe_fd = os.open(probe_path, os.O_RDWR)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe_fd)
        finally:
            chat2api._release_reauth_lock(handle)
            if os.path.exists(probe_path):
                os.unlink(probe_path)
            if os.path.exists(str(lock_path)):
                os.unlink(str(lock_path))


if __name__ == "__main__":
    unittest.main()
