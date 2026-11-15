# -*- coding: utf-8 -*-
"""8110 分享专用发送前调度已退役（#34）。

8100 是唯一生产分享路径：专用 8110 后端的 role/ticket/预发送调度已随本票
删除。本文件保留为「断言不存在」的回归：模块、装配符号和调度状态都不得
再出现在生产代码里。

不启动服务、不读写生产账本；只读文件系统与模块符号。
"""

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CHAT2API = HERE / "chat2api"
for _path in (HERE, CHAT2API):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


class TestRetiredShareDispatch(unittest.TestCase):
    """The pre-send dispatcher and its 8110 wiring are gone."""

    def test_dispatch_module_is_absent(self):
        self.assertFalse((CHAT2API / "share_dispatch.py").exists())
        spec = importlib.util.find_spec("share_dispatch")
        self.assertIsNone(spec)

    def test_chat2api_has_no_dispatch_symbols(self):
        chat2api = importlib.import_module("chat2api")
        for name in (
                "_load_share_dispatch", "share_dispatcher",
                "share_dispatch_ticket", "_release_share_dispatch_ticket",
                "_close_share_dispatch", "_raise_share_dispatch_refusal",
                "_await_share_send_turn", "_share_dispatcher",
                "_share_dispatcher_lock", "_ShareDispatchTimeout",
                "_ShareDispatchRevoked", "_ShareDispatchStateUnavailable",
                "SHARE_DISPATCH_PROTOCOLS",
                "SHARE_DISPATCH_STATE_UNAVAILABLE_STATUS"):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(chat2api, name))

    def test_backend_supervision_module_is_absent(self):
        self.assertFalse(
            (HERE / "share" / "backend_supervision.py").exists())
        spec = importlib.util.find_spec("share.backend_supervision")
        self.assertIsNone(spec)

    def test_cloud_composition_root_is_absent(self):
        self.assertFalse((HERE / "cloud").exists())
        self.assertIsNone(importlib.util.find_spec("cloud"))


if __name__ == "__main__":
    unittest.main()
