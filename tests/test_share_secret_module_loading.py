# -*- coding: utf-8 -*-
"""脚本方式启动时的 share_secret_protection 加载（R13 Linux 默认装配）。

契约：

* 服务以脚本方式运行：``cwd=chat2api/``，``sys.path[0]=chat2api/``，
  仓库根 ``ai-gateway/`` 不在 ``sys.path``。此时
  ``from share_secret_protection import ...`` 必然 ModuleNotFoundError，
  而失败会被 fail-closed 处理器吞掉，控制令牌与归属密钥都被静默拒绝。
* ``_load_share_secret_protection()`` 先复用 ``sys.modules``，否则按路径
  加载父目录的 ``share_secret_protection.py``；不修改 ``sys.path``。
* 正控：已有控制令牌与已有归属密钥都能读到。
* 负控：缺失或权限不符的密钥仍拒绝（令牌读取失败返回 None，归属密钥
  缺失、过短或 POSIX 权限过宽返回 None），绝不退化成新建或猜测。

子进程用 ``python -c`` 从 ``chat2api/`` 启动，并移除 ``PYTHONPATH``；子进程
先断言普通 import 不可用，再加载两个产品模块。父进程只准备临时密钥文件，
不启动服务、不写生产账本。所有文件路径都先校验落在显式给出的根目录内。
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
GATEWAY_DIR = TEST_DIR.parent
CHAT2API_DIR = GATEWAY_DIR / "chat2api"
CHAT2API_PATH = CHAT2API_DIR / "chat2api.py"
SHARE_GATEWAY_PATH = CHAT2API_DIR / "share_entry.py"
SECRET_PATH = GATEWAY_DIR / "share_secret_protection.py"

TOKEN = "existing-control-token-value"
ATTRIBUTION_KEY = "a" * 48


def _within(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _load(name, path, root):
    if not _within(root, path):
        raise ValueError("module path outside its root")
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_private(root, path, text):
    """Fixture secrets must pass the POSIX 0600/current-uid gate."""
    if not _within(root, path):
        raise ValueError("fixture path outside its root")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, str(text).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows chmod is best effort
        pass


_CHILD_SCRIPT = r'''
import importlib.util
import json
import os
import sys
from pathlib import Path


def within(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def load(name, path, root):
    if not within(root, path):
        raise SystemExit("module path outside its root")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    (gateway_dir, temp_root, chat2api_path, share_path, token_path,
     key_path, missing_key_path, directory_path, loose_token_path,
     loose_key_path) = sys.argv[1:11]
    for path in (token_path, key_path, missing_key_path, directory_path,
                 loose_token_path, loose_key_path):
        if not within(temp_root, path):
            raise SystemExit("fixture path outside the temp root")
    report = {
        "cwd": os.getcwd(),
        "parent_on_sys_path": any(
            os.path.abspath(entry or os.getcwd()) == gateway_dir
            for entry in sys.path),
    }
    # 先证明普通 import 在这种装配下不可用：路径加载不是冗余的。
    try:
        import share_secret_protection  # noqa: F401
    except ImportError:
        report["plain_import"] = "unavailable"
    else:
        report["plain_import"] = "available"
    chat2api = load("chat2api_secret_loading", chat2api_path, gateway_dir)
    share = load("share_entry_secret_loading", share_path, gateway_dir)
    report["token"] = chat2api.ensure_control_token(token_path)
    report["key"] = share.load_attribution_key(key_path)
    report["missing_key"] = share.load_attribution_key(missing_key_path)
    report["directory_token"] = chat2api.ensure_control_token(directory_path)
    report["loader_file"] = (
        chat2api._load_share_secret_protection().__file__)
    if os.name == "posix":
        report["loose_token"] = chat2api.ensure_control_token(loose_token_path)
        report["loose_key"] = share.load_attribution_key(loose_key_path)
    print(json.dumps(report))


main()
'''


class ScriptModeSecretLoadingTests(unittest.TestCase):
    """以真实脚本方式启动子进程，验证两条密钥读取都走路径加载。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.token_path = self.dir / "control-token"
        self.key_path = self.dir / "share-attribution.key"
        self.missing_key_path = self.dir / "missing.key"
        self.directory_path = self.dir / "token-directory"
        self.loose_token_path = self.dir / "loose-token"
        self.loose_key_path = self.dir / "loose-attribution.key"
        _write_private(self.dir, self.token_path, TOKEN)
        _write_private(self.dir, self.key_path, ATTRIBUTION_KEY)
        self.directory_path.mkdir()
        if os.name == "posix":
            _write_private(self.dir, self.loose_token_path, "loose-token")
            _write_private(self.dir, self.loose_key_path, "b" * 48)
            for path in (self.loose_token_path, self.loose_key_path):
                os.chmod(path, 0o644)

    def _run_child(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT, str(GATEWAY_DIR),
             str(self.dir), str(CHAT2API_PATH), str(SHARE_GATEWAY_PATH),
             str(self.token_path), str(self.key_path),
             str(self.missing_key_path), str(self.directory_path),
             str(self.loose_token_path), str(self.loose_key_path)],
            cwd=str(CHAT2API_DIR), env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_script_mode_has_the_parent_directory_off_sys_path(self):
        report = self._run_child()
        self.assertEqual(Path(report["cwd"]).resolve(), CHAT2API_DIR.resolve())
        self.assertFalse(report["parent_on_sys_path"])
        self.assertEqual(report["plain_import"], "unavailable")

    def test_script_mode_reads_existing_control_token_and_attribution_key(self):
        report = self._run_child()
        self.assertEqual(report["token"], TOKEN)
        self.assertEqual(report["key"], ATTRIBUTION_KEY)
        # 两条读取共用同一个按路径加载的模块（父目录文件）。
        self.assertEqual(Path(report["loader_file"]).resolve(),
                         SECRET_PATH.resolve())

    def test_script_mode_still_refuses_missing_or_loose_secrets(self):
        report = self._run_child()
        self.assertIsNone(report["missing_key"])
        self.assertIsNone(report["directory_token"])
        if os.name == "posix":
            self.assertIsNone(report["loose_token"])
            self.assertIsNone(report["loose_key"])


class ModuleReuseTests(unittest.TestCase):
    """已导入的模块必须复用，而不是重复加载出第二个模块对象。"""

    def test_loader_reuses_an_already_imported_module(self):
        chat2api = _load("chat2api_secret_reuse", CHAT2API_PATH, GATEWAY_DIR)
        share = _load("share_entry_secret_reuse", SHARE_GATEWAY_PATH,
                      GATEWAY_DIR)
        fake = SimpleNamespace(
            read_private_secret_path=lambda *args, **kwargs: b"secret",
            write_private_secret_path=lambda *args, **kwargs: None,
        )
        with mock.patch.dict(sys.modules,
                             {"share_secret_protection": fake}):
            self.assertIs(chat2api._load_share_secret_protection(), fake)
            self.assertIs(share._load_share_secret_protection(), fake)


if __name__ == "__main__":
    unittest.main()
