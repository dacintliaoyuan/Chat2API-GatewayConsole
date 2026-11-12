"""D4-prerequisite offline tests for the real OS adapters
(ai-gateway/share/broker_ops.py).

All OS boundaries are injected: the schtasks/tasklist runner, the
instance-pipe kernel facade, the H6 identity facade and the icacls runner
are fakes in temp dirs. No real task, pipe, process identity query or ACL
audit runs here - those are the D4/L2 experiments (H3/H6 and the ACL
calibrations). The tests verify ARGUMENT control, exit-code semantics,
fail-closed decisions and the R41-2 trust-chain instance_call wiring over
the REAL instance protocol (the fake kernel forwards the signed frame to
ShareServer.handle_instance_control, never an echo).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))

import share.broker_protocol as bp  # noqa: E402
from share_entry import ShareServer  # noqa: E402

import share.broker_ops as share_broker_ops  # noqa: E402

_OPS = share_broker_ops


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _projection(generation=1):
    return {"schema_version": 1, "config_generation": generation,
            "data": [{"key_id": "uuid-1", "name": "friend1",
                      "secret_hash": "a" * 64, "secret_version": 1,
                      "enabled": True}]}


class WindowsHostOpsTests(unittest.TestCase):
    """R41-3/R44-3 semantics: the task state is an AUXILIARY signal; share
    identity is decided by COMMAND LINE (the broker same-image process is
    excluded) plus a LISTENER on the share port; failed or unparseable
    evidence is None (explicit unconfirmed) and a succeeded empty query is
    the only empty evidence."""

    def _ops(self, task_stdout='"t","x","Ready"', cim_stdout="",
             netstat_stdout="", task_rc=0, cim_rc=0, netstat_rc=0):
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args[0] == "netstat":
                return _Proc(netstat_rc, stdout=netstat_stdout)
            if args[0] == "pwsh":
                return _Proc(cim_rc, stdout=cim_stdout)
            return _Proc(task_rc, stdout=task_stdout)

        ops = _OPS.WindowsHostOps("umshare-live", runner=runner)
        return ops, calls

    _SHARE_ROW = ('[{"ProcessId": 123, "CommandLine": '
                  '"C:\\\\run\\\\um-share-python.exe \\"chat2api.py\\"'
                  ' --port 8100", "ExecutablePath": '
                  '"C:\\\\run\\\\um-share-python.exe", '
                  '"CreationDate": "20260908000000.000000+480", '
                  '"OwnerDomain": "MACHINE", '
                  '"OwnerUser": "dacint"}]')

    def test_schtasks_arguments_are_controlled_list_args(self):
        calls = []

        def runner(args, timeout):
            calls.append(args)
            return _Proc(0)

        ops = _OPS.WindowsHostOps("umshare-live", runner=runner)
        self.assertTrue(ops.start_host())
        self.assertTrue(ops.stop_host())
        self.assertEqual(calls[0], ["schtasks", "/Run", "/TN", "umshare-live"])
        self.assertEqual(calls[1], ["schtasks", "/End", "/TN", "umshare-live"])

    def test_nonzero_exit_is_false_never_success(self):
        ops = _OPS.WindowsHostOps("t", runner=lambda a, to: _Proc(1))
        self.assertFalse(ops.start_host())
        self.assertFalse(ops.stop_host())

    def test_runner_failure_is_explicit_false(self):
        def runner(args, timeout):
            raise OSError("schtasks unavailable")

        ops = _OPS.WindowsHostOps("t", runner=runner)
        self.assertFalse(ops.start_host())

    def test_verify_running_needs_task_process_and_listener(self):
        # Running 任务 + share 命令行进程 + 监听 → True。
        ops, _ = self._ops('"t","x","Running"', self._SHARE_ROW,
                           "  TCP    0.0.0.0:8100    0.0.0.0:0"
                           "    LISTENING    123")
        self.assertIs(ops.verify_running(), True)
        # Running 但无 share 进程 → False（任务状态单独不构成运行证据）。
        ops2, _ = self._ops('"t","x","Running"', "")
        self.assertIs(ops2.verify_running(), False)
        # 进程在但端口无监听 → False（监听也是运行证据的一部分）。
        ops3, _ = self._ops('"t","x","Running"', self._SHARE_ROW,
                            netstat_stdout="")
        self.assertIs(ops3.verify_running(), False)

    def test_verify_closed_requires_task_process_and_listener_gone(self):
        # 非 Running + 无 share 进程 + 无监听 → True。
        ops, _ = self._ops('"t","x","Ready"', "", netstat_stdout="")
        self.assertIs(ops.verify_closed(), True)
        # 任务非 Running 但 share 进程仍活 → False。
        ops2, _ = self._ops('"t","x","Ready"', self._SHARE_ROW,
                            netstat_stdout="")
        self.assertIs(ops2.verify_closed(), False)
        # 进程停了但监听仍占着 share 端口（公网入口未关）→ False。
        ops3, _ = self._ops('"t","x","Ready"', "",
                            "  TCP    0.0.0.0:8100    0.0.0.0:0"
                            "    LISTENING    999")
        self.assertIs(ops3.verify_closed(), False)
        # 任务 Running → False。
        ops4, _ = self._ops('"t","x","Running"', "", netstat_stdout="")
        self.assertIs(ops4.verify_closed(), False)

    def test_localized_task_state_tokens_normalize(self):
        # zh-CN 主机 schtasks 发出本地化状态词（"正在运行"/"就绪"/"已禁用"）；
        # 归一化后与 English token 等价，verify 判定不再依赖主机语言。
        row = self._SHARE_ROW
        lst = ("  TCP    0.0.0.0:8100    0.0.0.0:0    LISTENING    123")
        # "正在运行" 等价于 Running：进程中与监听在 → running True / closed False
        ops_cn, _ = self._ops('"t","x","正在运行"', row, lst)
        self.assertIs(ops_cn.verify_running(), True)
        self.assertIs(ops_cn.verify_closed(), False)
        # "就绪" 等价于 Ready：无进程无监听 → closed True
        ops_ready, _ = self._ops('"t","x","就绪"', "", netstat_stdout="")
        self.assertIs(ops_ready.verify_closed(), True)
        # 未映射的陌生令牌作为辅助信号不被折叠成 Running（不误判运行）
        ops_unknown, _ = self._ops('"t","x","???"', row, lst)
        self.assertIs(ops_unknown.verify_running(), False)
        # 不可解析的查询输出是显式证据失败（None），不是“非运行”
        ops_bad, _ = self._ops("not-a-csv-row", "")
        self.assertIs(ops_bad.verify_closed(), None)
        self.assertIs(ops_bad.verify_running(), None)

    def test_same_image_broker_does_not_count_as_share(self):
        # R44-3：同名 broker 进程不算 share——broker 活、share 停 →
        # 关闭成立（按命令行区分，不按映像名）。
        rows = ('[{"ProcessId": 10, "CommandLine": '
                '"exe share/broker_server.py --broker-dir x", '
                '"ExecutablePath": "C:\\\\run\\\\um-share-python.exe", '
                '"CreationDate": "20260908000000.000000+480", "OwnerDomain": "MACHINE", "OwnerUser": "dacint"}]')
        ops, _ = self._ops('"t","x","Ready"', rows, netstat_stdout="")
        self.assertIs(ops.verify_closed(), True)
        # share 网关行在场 → 未关闭。
        combined = ('[{"ProcessId": 10, "CommandLine": '
                    '"exe share/broker_server.py --broker-dir x", '
                    '"ExecutablePath": "C:\\\\run\\\\um-share-python.exe", '
                    '"CreationDate": "20260908000000.000000+480", "OwnerDomain": "MACHINE", "OwnerUser": "dacint"},'
                    ' {"ProcessId": 123, "CommandLine": '
                    '"exe chat2api.py --port 8100", '
                    '"ExecutablePath": "C:\\\\run\\\\um-share-python.exe", '
                    '"CreationDate": "20260908000000.000000+480", "OwnerDomain": "MACHINE", "OwnerUser": "dacint"}]')
        ops2, _ = self._ops('"t","x","Ready"', combined, netstat_stdout="")
        self.assertIs(ops2.verify_closed(), False)

    def test_unresolvable_evidence_is_none_not_closed(self):
        # 任务查询失败 → None；CIM 输出坏 JSON（exit 0）→ None；netstat
        # 坏行 → None；CIM 行缺 PID/命令行 → None。
        ops, _ = self._ops("", "", task_rc=2)
        self.assertIsNone(ops.verify_running())
        self.assertIsNone(ops.verify_closed())
        ops2, _ = self._ops('"t","x","Ready"', "not-json{{")
        self.assertIsNone(ops2.verify_closed())
        ops3, _ = self._ops('"t","x","Ready"', "",
                            "  TCP    0.0.0.0:8100    x    LISTENING    abc")
        self.assertIsNone(ops3.verify_closed())
        ops4, _ = self._ops('"t","x","Ready"',
                            '[{"CommandLine": "exe chat2api.py"}]')
        self.assertIsNone(ops4.verify_closed())
        ops5, _ = self._ops('"t","x","Ready"',
                            '[{"ProcessId": 5, "CommandLine": null}]')
        self.assertIsNone(ops5.verify_closed())
        # 未知本地化任务状态 + 无进程无监听 → closed 依进程/监听证据成立。
        ops6, _ = self._ops('"t","x","铲除中"', "", netstat_stdout="")
        self.assertIs(ops6.verify_closed(), True)

    def test_row_missing_full_image_path_is_unconfirmed(self):
        # R46.6-B：缺完整映像路径的行 = 全查询证据不可用（None），绝不
        # 部分信任。
        ops, _ = self._ops('"t","x","Ready"',
                           '[{"ProcessId": 123, "CommandLine": '
                           '"exe chat2api.py --port 8100"}]')
        self.assertIsNone(ops.verify_closed())
        # 映像路径与查询映像不一致同样不可用。
        ops2, _ = self._ops('"t","x","Ready"',
                            '[{"ProcessId": 123, "CommandLine": '
                            '"exe chat2api.py", "ExecutablePath": '
                            '"C:\\other\\different.exe"}]')
        self.assertIsNone(ops2.verify_closed())

    # ---- R49-3 red cases: listening/row evidence must not lie ----

    def test_listening_bare_tcp_row_is_unconfirmed(self):
        # 单词 "TCP" 行（长度不足）→ None（先长度检查再取 parts[1]）。
        # CIM 必须为空：有进程时 False 在 listening 检查之前返回。
        ops, _ = self._ops('"t","x","Ready"', "", "  TCP")
        self.assertIsNone(ops.verify_closed())

    def test_listening_port_prefix_is_not_confused(self):
        # 81000 不是 8100：端口不匹配的行是无关连接，合法跳过（不得把
        # 后缀包含误当目标端口监听）。
        ops, _ = self._ops('"t","x","Ready"', "",
                           "  TCP    0.0.0.0:81000    0.0.0.0:0"
                           "    LISTENING    999")
        self.assertIs(ops.verify_closed(), True)

    def test_established_row_is_legal_and_listen_still_counts(self):
        # 同端口 LISTENING + ESTABLISHED 并存：监听证据仍成立；关停后的
        # 残余 ESTABLISHED 行不得阻塞证据判断。
        both = chr(10).join([
            "  TCP    0.0.0.0:8100    0.0.0.0:0    LISTENING    123",
            "  TCP    127.0.0.1:8100   127.0.0.1:51000   ESTABLISHED    123"])
        ops, _ = self._ops('"t","x","Running"', self._SHARE_ROW, both)
        self.assertIs(ops.verify_running(), True)
        timewait = ("  TCP    127.0.0.1:8100   127.0.0.1:51000"
                    "    TIME_WAIT    0")
        ops2, _ = self._ops('"t","x","Ready"', "", timewait)
        self.assertIs(ops2.verify_closed(), True)

    def test_unknown_state_on_target_port_is_unconfirmed(self):
        # 目标端口的未知/损坏状态词 → None（不得当无监听）。
        ops, _ = self._ops('"t","x","Ready"', "", "  TCP    0.0.0.0:8100"
                           "    0.0.0.0:0    LISTEN    123")
        self.assertIsNone(ops.verify_closed())

    def test_row_missing_creation_date_is_unconfirmed(self):
        # R49-3：CreationDate 是完整证据契约的一部分——缺失 → None。
        ops, _ = self._ops('"t","x","Ready"',
                           '[{"ProcessId": 123, "CommandLine": '
                           '"exe chat2api.py", "ExecutablePath": '
                           '"C:' + chr(92) * 4 + 'run' + chr(92) * 4 +
                           'um-share-python.exe"}]')
        self.assertIsNone(ops.verify_closed())

    def test_row_owner_missing_or_mismatched(self):
        # R49-3：owner 缺失 → 证据失败；owner 不匹配期望 → 行排除（非目标）。
        ops, _ = self._ops('"t","x","Ready"',
                           '[{"ProcessId": 123, "CommandLine": '
                           '"exe chat2api.py", "ExecutablePath": '
                           '"C:\\\\run\\\\um-share-python.exe", '
                           '"CreationDate": "20260909000000.000000+480"}]')
        self.assertIsNone(ops.verify_closed())
        row2 = ('[{"ProcessId": 123, "CommandLine": '
                '"exe chat2api.py", "ExecutablePath": '
                '"C:\\\\run\\\\um-share-python.exe", '
                '"CreationDate": "20260909000000.000000+480", '
                '"OwnerDomain": "MACHINE", "OwnerUser": '
                '"someone-else"}]')
        ops2 = _OPS.WindowsHostOps(
            "t", runner=lambda a, to: _Proc(
                0, stdout=('"t","x","Ready"' if a[0] == "schtasks"
                           else "" if a[0] == "netstat" else row2)),
            expected_owner="MACHINE\\dacint")
        self.assertIs(ops2.verify_closed(), True)   # 行被排除 → 无 share 进程

    def test_row_image_path_must_match_expected_full_path(self):
        # R49-3：映像路径与期望完整路径不一致 → 行排除（非目标）。
        rows = ('[{"ProcessId": 123, "CommandLine": '
                '"exe chat2api.py", "ExecutablePath": '
                '"C:\\\\other\\\\um-share-python.exe", '
                '"CreationDate": "20260909000000.000000+480", '
                '"OwnerDomain": "MACHINE", "OwnerUser": "dacint"}]')
        ops = _OPS.WindowsHostOps(
            "t", runner=lambda a, to: _Proc(
                0, stdout=('"t","x","Ready"' if a[0] == "schtasks"
                           else "" if a[0] == "netstat" else rows)),
            expected_image_path="C:\\run\\um-share-python.exe")
        self.assertIs(ops.verify_closed(), True)

    def test_owner_is_demanded_only_of_candidate_rows(self):
        # 2026-10-19 生产事故（网关实际健康，share.open 在第 4 层报
        # unconfirmed / deadline_exceeded）：owner 查询是这条证据读的主导成本
        # （本机每行一次 WMI 往返约 2s，broker+gateway+backend 三行约 7s，
        # 越过就绪预算）。broker 行按命令行标记排除、非 share 行按
        # share 标记排除，它们永远不可能是目标，其 owner 不再参与；候选行
        # 缺 owner 仍判证据失败（None），完整记录契约不变。
        broker_row = ('[{"ProcessId": 10, "CommandLine": '
                      '"exe share/broker_server.py --broker-dir x", '
                      '"ExecutablePath": "C:\\\\run\\\\um-share-python.exe", '
                      '"CreationDate": "20260908000000.000000+480"}]')
        ops, calls = self._ops('"t","x","Ready"', broker_row,
                              netstat_stdout="")
        self.assertIs(ops.verify_closed(), True)   # broker 行非目标 → owner 不参与
        # 成本判据：owner 查询脚本只对候选行发起——脚本条件与 Python 侧
        # 过滤同源（同一组类常量），排除的行不再产生 WMI 往返。
        script = calls[1][3] if len(calls) > 1 else ""
        self.assertIn(_OPS.WindowsHostOps._SHARE_MARKER, script)
        self.assertIn(_OPS.WindowsHostOps._BROKER_MARKER, script)
        # 候选（share）行缺 owner 仍是证据失败（None），契约不变。
        share_row = ('[{"ProcessId": 123, "CommandLine": '
                     '"exe chat2api.py --port 8100", '
                     '"ExecutablePath": "C:\\\\run\\\\um-share-python.exe", '
                     '"CreationDate": "20260908000000.000000+480"}]')
        ops2, _ = self._ops('"t","x","Ready"', share_row, netstat_stdout="")
        self.assertIsNone(ops2.verify_closed())

    def test_task_name_validation(self):
        with self.assertRaises(ValueError):
            _OPS.WindowsHostOps("")


class _InstanceHandlerKernel:
    """Fake kernel for the instance-pipe CLIENT path: every CreateFileW
    returns a DIFFERENT handle (so a second open can never be masked), ops
    are tagged per handle, and the SIGNED request is forwarded to the REAL
    ShareServer.handle_instance_control - the real schema-v2 instance
    protocol, never an echo."""

    def __init__(self, share, instance_secret):
        self.share = share
        self.instance_secret = instance_secret
        self.created = {}
        self.request_bodies = []
        self.write_calls = 0
        self.open_calls = 0
        self.handles = []
        self.written_handles = []
        self.closed_handles = []
        self._next_handle = 0x1000
        self.fail_pipes = set()

    def CreateFileW(self, name, *a):
        self.open_calls += 1
        if name in self.fail_pipes:
            return (1 << 64) - 1          # INVALID_HANDLE_VALUE
        handle = self._next_handle
        self._next_handle += 0x100
        self.handles.append(handle)
        self.created["pipe"] = name
        return handle

    def WriteFile(self, h, data, n, written, o):
        self.write_calls += 1
        self.written_handles.append(h)
        body = json.loads(bytes(data[:n]).decode("utf-8"))
        self.request_bodies.append(body)
        self.created["request"] = body
        written.contents.value = n
        return 1

    def ReadFile(self, h, buf, n, read, o):
        req = self.created["request"]
        resp = self.share.handle_instance_control(req, self.instance_secret)
        payload = (json.dumps(resp, sort_keys=True) + "\n").encode("utf-8")
        buf[:len(payload)] = payload
        read.contents.value = len(payload)
        return 1

    def CloseHandle(self, h):
        self.closed_handles.append(h)
        return 1

    def GetLastError(self):
        return 0

    def GetNamedPipeServerProcessId(self, h, p):
        p.contents.value = 4242
        return 1


class _RefusingPeerKernel(_InstanceHandlerKernel):
    def GetNamedPipeServerProcessId(self, h, p):
        return 0        # query fails -> peer pid None -> must refuse


class _FakeIdentityKernel:
    """Fake Win32 facade for the H6 verifier: deterministic process facts."""

    def __init__(self, image, owner_sid, open_ok=True, token_ok=True,
                 times_ok=True, creation_low=100):
        self.image = image
        self.owner_sid = owner_sid
        self.open_ok = open_ok
        self.token_ok = token_ok
        self.times_ok = times_ok
        self.creation_low = creation_low
        self._sid_buffers = []

    def __getitem__(self, key):
        if key == "kernel32":
            k = self
            import ctypes

            class K32:
                def OpenProcess(self, access, inherit, pid):
                    return 0x4000 if k.open_ok else None

                def QueryFullProcessImageNameW(self, h, flags, buf, size):
                    buf.value = k.image
                    size[0] = len(k.image)
                    return 1

                def GetProcessTimes(self, h, c, e, ki, u):
                    if not k.times_ok:
                        return 0
                    c.contents.dwLowDateTime = k.creation_low
                    c.contents.dwHighDateTime = 0
                    return 1

                def CloseHandle(self, h):
                    return 1
            return K32()
        if key == "advapi32":
            k = self
            import ctypes

            class ADV:
                def OpenProcessToken(self, h, access, token):
                    if not k.token_ok:
                        return 0
                    token[0] = 0x3000
                    return 1

                def GetTokenInformation(self, token, cls, buf, length,
                                        needed):
                    if buf is None:
                        needed[0] = 64
                        return 0
                    ctypes.memmove(buf, b"\xef\xbe\xad\xde\x00\x00\x00\x00",
                                   8)
                    return 1

                def ConvertSidToStringSidW(self, sid, out):
                    buf = ctypes.create_unicode_buffer(k.owner_sid)
                    k._sid_buffers.append(buf)
                    out[0] = ctypes.cast(buf, ctypes.c_wchar_p)
                    return 1
            return ADV()
        raise KeyError(key)


IMAGE = r"C:\ProgramData\UMShareRun\um-share-python.exe"
OWNER = "S-1-5-21-1001"


class MakeInstanceCallTests(unittest.TestCase):
    def _secret_file(self, td, value="inst-k"):
        path = Path(td) / "instance-channel-key"
        path.write_text(value, encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
        return str(path)

    def _share(self, td):
        config = Path(td) / "um-share-projection.json"
        config.write_text(json.dumps({
            "schema_version": 1, "config_generation": 1,
            "data": [{"key_id": "u1", "name": "f1",
                      "secret_hash": "a" * 64, "secret_version": 1,
                      "enabled": True}]}), encoding="utf-8")
        return ShareServer(("127.0.0.1", 0), config, auth_mode="projection")

    def test_instance_call_real_protocol_roundtrip(self):
        # 实例通道真实协议：签名帧带 instance_id → 真实 handle_instance_
        # control 校验（mac/instance 绑定/重放）→ 结构化 ACK。
        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER),
            expected_image=IMAGE, expected_owner_sid=OWNER)
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = _InstanceHandlerKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                expect_image=IMAGE, expect_owner_sid=OWNER,
                kernel_factory=lambda: kernel)
            resp = call("instance-status", {}, share.instance_id)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["instance_id"], share.instance_id)
        self.assertEqual(kernel.created["pipe"],
                         _OPS.INSTANCE_PIPE_PREFIX + share.instance_id)
        # R44-1: exactly ONE open per exchange, and the verified handle IS
        # the handle the signed request travels on.
        self.assertEqual(kernel.open_calls, 1)
        self.assertEqual(len(kernel.handles), 1)
        self.assertEqual(kernel.written_handles, kernel.handles)
        self.assertEqual(kernel.closed_handles, kernel.handles)
        # R41-1/R41-2: the signed frame itself carries the instance id.
        self.assertEqual(kernel.request_bodies[0]["instance_id"],
                         share.instance_id)

    def test_unverified_peer_refused_after_bounded_retry(self):
        # 对端身份查询持续失败（pid None，每次连接都是死握手）→ 一次有界
        # 重试后 PermissionError，且零请求字节发出（R41-2：没有任何一次尝试
        # 在验证前发出请求）。
        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER))
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = _RefusingPeerKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                kernel_factory=lambda: kernel)
            with self.assertRaises(PermissionError):
                call("instance-status", {}, share.instance_id)
            self.assertEqual(kernel.write_calls, 0)
            # R44-1: 原交换 + 一次重试，两条连接都被完全关闭。
            self.assertEqual(kernel.open_calls, 2)
            self.assertEqual(len(kernel.closed_handles), 2)

    def test_instance_call_retries_dead_handshake_first_connection(self):
        # D4 实机观察：空闲单实例服务器的首次连接可能在 OPEN 已返回时握手
        # 尚未提交，GetNamedPipeServerProcessId 报无对端（死握手）→ 整个交换
        # 用新连接重试一次并重新验证身份；第二次成功即完成调用。身份验证在
        # 每次尝试上都先于请求（R41-2），重试不降级。
        class DeadHandshakeFirstKernel(_InstanceHandlerKernel):
            def __init__(self, share, secret):
                super().__init__(share, secret)
                self.first = True
            def GetNamedPipeServerProcessId(self, h, p):
                if self.first:
                    self.first = False
                    return 0
                return super().GetNamedPipeServerProcessId(h, p)

        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER),
            expected_image=IMAGE, expected_owner_sid=OWNER)
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = DeadHandshakeFirstKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                expect_image=IMAGE, expect_owner_sid=OWNER,
                kernel_factory=lambda: kernel)
            resp = call("instance-status", {}, share.instance_id)
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["result"]["instance_id"],
                             share.instance_id)
            # 首连接死握手（无写）+ 一次带新句柄的成功交换
            self.assertEqual(kernel.open_calls, 2)
            self.assertEqual(kernel.write_calls, 1)
            self.assertEqual(len(kernel.request_bodies), 1)
            self.assertEqual(kernel.closed_handles, kernel.handles)

    def test_identity_mismatch_refused_before_any_request(self):
        # 完整路径不匹配（同名不同路径）→ 拒绝，零请求。
        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(r"C:\Windows\System32\cmd.exe",
                                       OWNER),
            expected_image=IMAGE, expected_owner_sid=OWNER)
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = _InstanceHandlerKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                expect_image=IMAGE, kernel_factory=lambda: kernel)
            with self.assertRaises(PermissionError):
                call("load", {"generation": 2, "fingerprint": "x" * 64},
                     share.instance_id)
            self.assertEqual(kernel.write_calls, 0)

    def test_instance_call_retries_transient_write_gap_once(self):
        # R48-1/instance channel: caller-owned transport never reconnects
        # inside send_all (R41-2 identity is bound to ITS handle), so a
        # transient serving gap (write 233) surfaces as not_installed and the
        # WHOLE exchange is retried once with a FRESH open + re-verify. Every
        # handle is closed; the second exchange carries the signed request.
        class FlakyFirstWriteKernel(_InstanceHandlerKernel):
            def __init__(self, share, secret):
                super().__init__(share, secret)
                self.fail_first_write = True
            def WriteFile(self, h, data, n, written, o):
                if self.fail_first_write:
                    self.fail_first_write = False
                    self.written_handles.append(h)
                    written.contents.value = 0
                    return 0
                return super().WriteFile(h, data, n, written, o)
            def GetLastError(self):
                return 233

        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER),
            expected_image=IMAGE, expected_owner_sid=OWNER)
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = FlakyFirstWriteKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                expect_image=IMAGE, expect_owner_sid=OWNER,
                kernel_factory=lambda: kernel)
            resp = call("instance-status", {}, share.instance_id)
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["result"]["instance_id"],
                             share.instance_id)
            # one failed write, then a successful exchange on a fresh handle
            self.assertEqual(kernel.write_calls, 1)    # 成功写=第 2 次
            self.assertEqual(kernel.open_calls, 2)     # 初交换 + 一次重试
            self.assertEqual(len(kernel.request_bodies), 1)
            self.assertEqual(kernel.closed_handles, kernel.handles)

    def test_instance_call_no_infinite_retry(self):
        # 持续 233：第二次仍失败时不无限重试（最多原交换+一次重试）。
        class AlwaysFlakyKernel(_InstanceHandlerKernel):
            def WriteFile(self, h, data, n, written, o):
                self.written_handles.append(h)
                written.contents.value = 0
                return 0
            def GetLastError(self):
                return 233

        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER),
            expected_image=IMAGE, expected_owner_sid=OWNER)
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = AlwaysFlakyKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                expect_image=IMAGE, expect_owner_sid=OWNER,
                kernel_factory=lambda: kernel)
            resp = call("instance-status", {}, share.instance_id)
            self.assertFalse(resp["ok"])
            self.assertEqual(kernel.open_calls, 2)     # 原 + 一次重试
            self.assertEqual(len(kernel.closed_handles), 2)

    def test_missing_secret_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                _OPS.make_instance_call(str(Path(td) / "absent-key"))

    def test_bad_instance_id_is_refused_before_wiring(self):
        verifier = _OPS.WindowsInstanceVerifier(
            facade=_FakeIdentityKernel(IMAGE, OWNER))
        with tempfile.TemporaryDirectory() as td:
            share = self._share(td)
            self.addCleanup(share.server_close)
            kernel = _InstanceHandlerKernel(share, "inst-k")
            call = _OPS.make_instance_call(
                self._secret_file(td), verifier=verifier,
                kernel_factory=lambda: kernel)
            with self.assertRaises(ValueError):
                call("instance-status", {}, "")


class WindowsInstanceVerifierTests(unittest.TestCase):
    def test_matching_full_image_and_owner_pass(self):
        fake = _FakeIdentityKernel(IMAGE, OWNER)
        verifier = _OPS.WindowsInstanceVerifier(
            facade=fake, expected_image=IMAGE,
            expected_owner_sid=OWNER)
        ok, evidence = verifier.verify(1234)
        self.assertTrue(ok, evidence)
        self.assertEqual(evidence["image"], IMAGE)
        self.assertEqual(evidence["owner"], OWNER)
        self.assertEqual(evidence["creation_filetime"], 100)

    def test_same_name_different_path_refuses(self):
        # R41-2: FULL path comparison - a same-named binary in a writable
        # location can never pass.
        fake = _FakeIdentityKernel(r"C:\Users\dacint\um-share-python.exe",
                                   OWNER)
        verifier = _OPS.WindowsInstanceVerifier(
            facade=fake, expected_image=IMAGE)
        ok, evidence = verifier.verify(1234)
        self.assertFalse(ok)
        self.assertEqual(evidence["reason"], "peer image mismatch")

    def test_owner_mismatch_refuses(self):
        fake = _FakeIdentityKernel(IMAGE, "S-1-5-21-9999")
        verifier = _OPS.WindowsInstanceVerifier(
            facade=fake, expected_image=IMAGE, expected_owner_sid=OWNER)
        ok, evidence = verifier.verify(1234)
        self.assertFalse(ok)
        self.assertEqual(evidence["reason"], "peer owner mismatch")

    def test_creation_query_failure_refuses(self):
        # R41-2: creation time is MANDATORY evidence - a failed query is a
        # refusal, never an evidence-only note.
        fake = _FakeIdentityKernel(IMAGE, OWNER, times_ok=False)
        verifier = _OPS.WindowsInstanceVerifier(facade=fake)
        ok, evidence = verifier.verify(1234)
        self.assertFalse(ok)
        self.assertEqual(evidence["error"], "GetProcessTimes failed")

    def test_creation_expectation_mismatch_refuses_pid_reuse(self):
        fake = _FakeIdentityKernel(IMAGE, OWNER, creation_low=777)
        verifier = _OPS.WindowsInstanceVerifier(facade=fake)
        ok, evidence = verifier.verify(
            1234, expect_image=IMAGE, expect_creation_filetime=100)
        self.assertFalse(ok)
        self.assertEqual(evidence["reason"],
                         "peer identity changed (PID reuse?)")

    def test_unopenable_process_fails_closed(self):
        fake = _FakeIdentityKernel("x", "S-1-1-0", open_ok=False)
        verifier = _OPS.WindowsInstanceVerifier(facade=fake)
        ok, evidence = verifier.verify(1234)
        self.assertFalse(ok)
        self.assertEqual(evidence["error"], "OpenProcess failed")

    def test_token_failure_fails_closed(self):
        fake = _FakeIdentityKernel(IMAGE, "S-1-1-0", token_ok=False)
        verifier = _OPS.WindowsInstanceVerifier(facade=fake)
        ok, evidence = verifier.verify(1234, expect_image=IMAGE)
        self.assertFalse(ok)
        self.assertEqual(evidence["error"], "OpenProcessToken failed")


class IcaclsAclCheckerTests(unittest.TestCase):
    _STDOUT = "\r\n".join([
        r"C:\ProgramData\UMShareRun\um-share-projection.json",
        "NT AUTHORITY\\SYSTEM:(I)(F)",
        "BUILTIN\\Administrators:(I)(F)",
        "DAICNT\\um_share:(I)(RX)",
        ""])

    _EXPECTATIONS = {
        "full": {"NT AUTHORITY\\SYSTEM": {"F"},
                 "BUILTIN\\Administrators": {"F"}},
        "read_only": {"DAICNT\\um_share": {"RX"}},
        "deny": ["Everyone", "ANONYMOUS LOGON", "BUILTIN\\Users"],
    }

    def _checker(self, stdout=None, returncode=0, expectations=None):
        proc = _Proc(returncode,
                     stdout=IcaclsAclCheckerTests._STDOUT
                     if stdout is None else stdout)
        return _OPS.IcaclsAclChecker(
            r"C:\ProgramData\UMShareRun\um-share-projection.json",
            expectations or dict(IcaclsAclCheckerTests._EXPECTATIONS),
            runner=lambda args, timeout: proc)

    def test_all_expectations_met(self):
        checker = self._checker()
        self.assertTrue(checker.check())
        self.assertIsNone(checker.last_error)

    def test_rx_plus_extra_write_ace_refused(self):
        # R41-5: RX + 额外写权限必须拒绝（旧子串匹配会放过）。
        bad = self._STDOUT.replace(
            "DAICNT\\um_share:(I)(RX)",
            "DAICNT\\um_share:(I)(RX)\r\nDAICNT\\um_share:(I)(W)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("write codes", checker.last_error)

    def test_duplicate_aces_all_evaluated(self):
        # 重复同名 ACE 全部评估：read_only 主体第二条授予 F 时不可被字典
        # 覆盖丢掉（union 含写码 → 拒绝）。
        bad = self._STDOUT.replace(
            "DAICNT\\um_share:(I)(RX)",
            "DAICNT\\um_share:(I)(RX)\r\nDAICNT\\um_share:(I)(F)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        # 聚合 F 规范化为全部码：union 含 F（其余写码随规范化出现）。
        self.assertIn("write codes ['AD'", checker.last_error)

    def test_unknown_write_principal_refused(self):
        bad = self._STDOUT + "EvilPrincipal:(I)(M)\r\n"
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        # R46.6-C strict 契约：控制对象上任何未列主体拒绝（读或写同罪）。
        self.assertIn("unexpected principal EvilPrincipal",
                      checker.last_error)
        # strict=False（离线投影夹具专用）保持仅查写码的旧契约。
        checker2 = self._checker(stdout=bad)
        self.assertFalse(checker2.check(strict=False))
        self.assertIn("unexpected write principal EvilPrincipal",
                      checker2.last_error)

    def test_forbidden_identity_refuses(self):
        bad = self._STDOUT + "Everyone:(I)(RX)\r\n"
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("forbidden identity present: Everyone",
                      checker.last_error)

    def test_first_path_line_not_parsed_as_ace(self):
        # 首行是查询路径回显（含盘符冒号），不得进入 ACE 解析；回显大小写
        # 不同也按同一路径跳过，不产生误判。
        checker = self._checker()
        self.assertTrue(checker.check())
        checker2 = self._checker(stdout=self._STDOUT.replace(
            r"C:\ProgramData\UMShareRun\um-share-projection.json",
            r"c:\programdata\umsharerun\um-share-projection.json"))
        self.assertTrue(checker2.check())
        self.assertIsNone(checker2.last_error)

    def test_unparsable_line_refuses(self):
        bad = self._STDOUT.replace("NT AUTHORITY\\SYSTEM:(I)(F)",
                                   "NT AUTHORITY\\SYSTEM no flags here")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("unparsable ACL line", checker.last_error)

    def test_command_failure_and_exception_fail_closed(self):
        checker = self._checker(returncode=2)
        self.assertFalse(checker.check())

        def boom(args, timeout):
            raise OSError("icacls missing")

        checker2 = _OPS.IcaclsAclChecker(
            "x", dict(IcaclsAclCheckerTests._EXPECTATIONS), runner=boom)
        self.assertFalse(checker2.check())

    def test_call_raises_broker_error_for_writer_contract(self):
        from share.management import BrokerError
        checker = self._checker(returncode=2)
        with self.assertRaises(BrokerError):
            checker(r"C:\some\tmp-path")

    def test_writer_path_argument_is_accepted(self):
        # W3 真实闭环缺陷回归：writer 按契约传入 pathlib.Path（
        # ProtectedProjectionWriter.write 调 acl_checker(self.path)），
        # Path 无 casefold，曾在每次真实发布时以 internal_error 拒绝。
        # Path 实参与字符串实参必须得到同一判定。
        from pathlib import Path as _Path
        checker = self._checker()
        self.assertTrue(checker.check(
            _Path(r"C:\ProgramData\UMShareRun\um-share-projection.json")))
        self.assertIsNone(checker.last_error)
        bad_checker = self._checker(stdout="garbage-line\r\n")
        self.assertFalse(bad_checker.check(_Path(r"C:\some\proj.json")))

    def test_default_expectations_shape(self):
        checker = _OPS.IcaclsAclChecker.default("some-path")
        self.assertIn("full", checker.expectations)
        self.assertIn("read_only", checker.expectations)
        self.assertIn("deny", checker.expectations)
        self.assertIn("DAICNT\\um_share", checker.expectations["read_only"])
        self.assertIn("Everyone", checker.expectations["deny"])

    def test_dangerous_ace_behind_path_prefix_is_checked(self):
        # R44-4：首行「路径 + ACE」不被跳过——余量按 ACE 解析并判定。
        prefix_bad = self._STDOUT.replace(
            self._STDOUT.splitlines()[0],
            r"C:\ProgramData\UMShareRun\um-share-projection.json"
            " DAICNT\\um_share:(I)(W)", 1)
        checker = self._checker(stdout=prefix_bad)
        self.assertFalse(checker.check())
        self.assertIn("write codes ['W']", checker.last_error)
        # 路径前缀后的合法 RX ACE 正常计入。
        prefix_ok = self._STDOUT.replace(
            self._STDOUT.splitlines()[0],
            r"C:\ProgramData\UMShareRun\um-share-projection.json"
            " DAICNT\\um_share:(I)(RX)", 1)
        checker2 = self._checker(stdout=prefix_ok)
        self.assertTrue(checker2.check())

    def test_deny_only_expected_principal_is_never_a_grant(self):
        # DENY-only 的期望主体不构成授权；细分码 DENY 是未支持组合 →
        # 整体拒绝（DENY 仍永不构成授予）。
        bad = self._STDOUT.replace("DAICNT\\um_share:(I)(RX)",
                                   "DAICNT\\um_share:(DENY)(W)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("unsupported deny granularity",
                      checker.last_error)

    def test_deny_specific_code_is_unsupported_combination(self):
        # R49-5：DENY 携带细分码（W ⊂ RX 语义）在 token 模型中无法正确
        # 解释（RX 会被错误保留）→ 未支持组合整体拒绝（严格形状契约）。
        eff = self._STDOUT.replace(
            "DAICNT\\um_share:(I)(RX)",
            "DAICNT\\um_share:(DENY)(W)\r\nDAICNT\\um_share:(I)(RX)")
        checker = self._checker(stdout=eff)
        self.assertFalse(checker.check())
        self.assertIn("unsupported deny granularity",
                      checker.last_error)

    def test_deny_of_required_read_refuses(self):
        # DENY(RX) 细分码 → 未支持组合拒绝（DENY 永不等于授予，且细分
        # DENY 与聚合 ALLOW 的交互超出 token 模型）。
        bad = self._STDOUT.replace(
            "DAICNT\\um_share:(I)(RX)",
            "DAICNT\\um_share:(I)(RX)\r\nDAICNT\\um_share:(DENY)(RX)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("unsupported deny granularity", checker.last_error)

    def test_deny_full_clears_effective_grant(self):
        # 可解释的聚合 DENY：DENY(F) 抵扣一切 → 生效为空 → 缺授权拒绝。
        bad = self._STDOUT.replace(
            "DAICNT\\um_share:(I)(RX)",
            "DAICNT\\um_share:(I)(RX)\r\nDAICNT\\um_share:(DENY)(F)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("lacks ['RX']", checker.last_error)

    def test_full_grant_on_read_only_identity_refuses(self):
        # 聚合 ALLOW F 规范化为全部码（含写）→ read_only 主体持 F 拒绝。
        bad = self._STDOUT.replace("DAICNT\\um_share:(I)(RX)",
                                   "DAICNT\\um_share:(I)(F)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("write codes", checker.last_error)

    def test_unknown_permission_code_refuses(self):
        # R44-4：未知权限码不是安全——拒绝。
        bad = self._STDOUT.replace("DAICNT\\um_share:(I)(RX)",
                                   "DAICNT\\um_share:(I)(ZZ)")
        checker = self._checker(stdout=bad)
        self.assertFalse(checker.check())
        self.assertIn("unknown permission code: ZZ", checker.last_error)


class TailscaleServiceOpsTests(unittest.TestCase):
    """受限 TS 服务原语（broker 白名单扩展的服务端原语）：受控 sc.exe 参数
    数组（服务名是类常量字面量，绝不是参数）、真实 rc 检查、裁定永远是
    复核查询到的服务状态——成功从不靠假设。"""

    _QUERY_ARGS = ["sc.exe", "query", "Tailscale"]

    def test_status_argument_array_is_fixed_shape(self):
        calls = []

        def runner(args, timeout):
            calls.append(args)
            return _Proc(0, stdout="STATE              : 4  RUNNING")

        ops = _OPS.TailscaleServiceOps(runner=runner)
        self.assertEqual(ops.status(), "running")
        self.assertEqual(calls, [self._QUERY_ARGS])

    def test_service_name_is_class_constant_literal(self):
        # 服务名是协议字面量：原语没有服务名参数可被注入。
        self.assertEqual(_OPS.TailscaleServiceOps.SERVICE_NAME, "Tailscale")
        self.assertEqual(_OPS.TailscaleServiceOps.SERVICE_NAME,
                         bp.TS_SERVICE_NAME)

    def test_status_states_and_absent(self):
        ops = _OPS.TailscaleServiceOps(runner=lambda a, t: _Proc(
            0, stdout="STATE              : 1  STOPPED"))
        self.assertEqual(ops.status(), "stopped")
        ops = _OPS.TailscaleServiceOps(
            runner=lambda a, t: _Proc(1060, stdout="service does not exist"))
        self.assertEqual(ops.status(), "absent")
        ops = _OPS.TailscaleServiceOps(
            runner=lambda a, t: _Proc(0, stdout="STATE              : 2  "
                                              "START_PENDING"))
        self.assertEqual(ops.status(), "start_pending")

    def test_failed_or_unparseable_query_is_none(self):
        ops = _OPS.TailscaleServiceOps(runner=lambda a, t: _Proc(7))
        self.assertIsNone(ops.status())
        ops = _OPS.TailscaleServiceOps(
            runner=lambda a, t: _Proc(0, stdout="garbage"))
        self.assertIsNone(ops.status())
        def boom(args, timeout):
            raise OSError("sc.exe unavailable")
        self.assertIsNone(_OPS.TailscaleServiceOps(runner=boom).status())

    def test_start_verdict_is_the_rechecked_state(self):
        script = []

        def runner(args, timeout):
            calls.append(args)
            if args[1] == "start":
                return _Proc(script[0])
            return _Proc(0, stdout=script[1])

        calls = []
        ops = _OPS.TailscaleServiceOps(runner=runner)
        script[:] = [0, "STATE              : 4  RUNNING"]
        self.assertTrue(ops.start())
        self.assertEqual(calls, [["sc.exe", "start", "Tailscale"],
                                 self._QUERY_ARGS])
        script[:] = [0, "STATE              : 1  STOPPED"]
        self.assertFalse(ops.start())       # start 后仍未 RUNNING → False

    def test_start_idempotent_rc_and_failures(self):
        script = []
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args[1] == "start":
                return _Proc(script[0])
            return _Proc(script[1], stdout=script[2])

        ops = _OPS.TailscaleServiceOps(runner=runner)
        script[:] = [1056, 0, "STATE              : 4  RUNNING"]
        self.assertTrue(ops.start())        # 已在运行（1056）+ 复核 → True
        script[:] = [5, 0, "STATE              : 4  RUNNING"]
        self.assertFalse(ops.start())       # 其他 rc → 拒绝（不冒充）
        script[:] = [0, 7, ""]              # 复核查询失败 → False
        self.assertFalse(ops.start())

        def crash(args, timeout):
            raise OSError("gone")
        self.assertFalse(_OPS.TailscaleServiceOps(runner=crash).start())

    def test_rejected_control_rc_stays_readable_for_the_broker(self):
        # R13-T: a permission-denied start (sc.exe rc 5 ERROR_ACCESS_DENIED)
        # must NOT be folded into the generic "did not re-confirm" unconfirmed
        # reply - the broker reads the REJECTED rc off the ops to answer a
        # distinct access_denied, so the console can report 权限不足 instead of
        # a misleading "已执行但未确认". The verdict stays False (never a fake
        # success) and the rc stays readable after the call.
        ops = _OPS.TailscaleServiceOps(runner=lambda a, t: _Proc(
            5, stdout="Access is denied."))
        self.assertIsNone(ops.last_control_rc)          # nothing controlled yet
        self.assertFalse(ops.start())                   # refused, not pending
        self.assertEqual(ops.last_control_rc, 5)

    def test_last_control_rc_tracks_every_control_call(self):
        # The rc is the LAST control command's real exit code (including the
        # idempotency codes); a failed launch (runner raised) reads None -
        # an unrunnable command is not a readable rc.
        script = []

        def runner(args, timeout):
            if args[1] == "start":
                return _Proc(script[0])
            return _Proc(0, stdout="STATE              : 4  RUNNING")

        ops = _OPS.TailscaleServiceOps(runner=runner)
        script[:] = [0]
        self.assertTrue(ops.start())
        self.assertEqual(ops.last_control_rc, 0)
        script[:] = [1056]
        self.assertTrue(ops.start())                    # already running (1056)
        self.assertEqual(ops.last_control_rc, 1056)

        def crash(args, timeout):
            raise OSError("sc.exe unavailable")

        ops = _OPS.TailscaleServiceOps(runner=crash)
        self.assertFalse(ops.stop())
        self.assertIsNone(ops.last_control_rc)

    def test_stop_verdict_is_the_rechecked_state(self):
        script = []
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args[1] == "stop":
                return _Proc(script[0])
            return _Proc(script[1], stdout=script[2])

        ops = _OPS.TailscaleServiceOps(runner=runner)
        script[:] = [0, 0, "STATE              : 1  STOPPED"]
        self.assertTrue(ops.stop())
        self.assertEqual(calls[0], ["sc.exe", "stop", "Tailscale"])
        self.assertEqual(calls[1], self._QUERY_ARGS)
        script[:] = [0, 0, "STATE              : 4  RUNNING"]
        self.assertFalse(ops.stop())        # stop 后仍 RUNNING → False
        script[:] = [1062, 0, "STATE              : 1  STOPPED"]
        self.assertTrue(ops.stop())         # 已停止（1062）+ 复核 → True


class HostEvidenceOpsTests(unittest.TestCase):
    """只读宿主证据（契约 §2.5/§8.3）：limits 复用 watchdog 自身
    ``Get-LimitsState``（dot-source -FunctionsOnly，参数含
    -RunRoot/-AppExe/-FirewallRule/-WfpHelper）；detection-failure 与
    文件不可读一律 None（诚实不可得，绝不折成 False/"无故障"）；
    faults 有界尾读（末 64 KiB / 末 50 行）。"""

    def _ops(self, stdout="", returncode=0, raises=False, run_root=r"C:\Run",
             watchdog_script=r"C:\Run\watchdog-umshare.ps1",
             image_path=r"C:\Run\um-share-python.exe",
             faults_file=None):
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if raises:
                raise OSError("powershell unavailable")
            return _Proc(returncode, stdout=stdout)

        ops = _OPS.WindowsHostOps(
            "umshare-live", runner=runner, run_root=run_root,
            watchdog_script=watchdog_script,
            expected_image_path=image_path,
            share_faults_file=faults_file)
        return ops, calls

    def test_verify_limits_reuses_the_watchdog_get_limits_state(self):
        ops, calls = self._ops(stdout="LIMITS=OK\n")
        self.assertIs(ops.verify_limits(), True)
        self.assertEqual(len(calls), 1)
        script = calls[0][-1]
        self.assertEqual(calls[0][:2], ["pwsh", "-NoProfile"])
        # dot-source watchdog -FunctionsOnly（不启动循环、不建目录）。
        self.assertIn("Get-LimitsState", script)
        self.assertIn("Get-FirewallState", script)
        self.assertIn("-FunctionsOnly", script)
        self.assertIn("-RunRoot 'C:\\Run'", script)
        self.assertIn("-AppExe 'C:\\Run\\um-share-python.exe'", script)
        self.assertIn("-FirewallRule 'um-share-outbound-block'", script)
        self.assertIn("-WfpHelper 'C:\\Run\\wfp_helper.ps1'", script)

    def test_verify_limits_false_and_detection_failure(self):
        ops, _ = self._ops(stdout="LIMITS=FAIL\nREASON=strong-not-ok: x\n")
        self.assertIs(ops.verify_limits(), False)
        # backend-port.txt 缺失 → watchdog 报 detection-failure → None
        # （证据不可得，绝不折成 False/limit-loss）。
        ops, _ = self._ops(stdout="LIMITS=FAIL\nREASON=detection-failure("
                                  "backend-port-unavailable)\n")
        self.assertIsNone(ops.verify_limits())

    def test_limits_detail_is_bounded_and_preserves_failure_kind(self):
        ops, _ = self._ops(stdout="LIMITS=FAIL\nREASON=wfp-filter-missing\n")
        self.assertEqual(ops.verify_limits_evidence(),
                         {"verdict": False, "detail": "wfp-filter-missing"})
        ops, _ = self._ops(stdout="LIMITS=FAIL\nREASON=firewall-error: private path\n")
        self.assertEqual(ops.verify_limits_evidence(),
                         {"verdict": None, "detail": "firewall-query-failed"})

    def test_verify_limits_unavailable_is_honest_none(self):
        for kwargs in ({"watchdog_script": None}, {"run_root": None},
                       {"stdout": "", "returncode": 0},
                       {"stdout": "garbage output\n"},
                       {"stdout": "LIMITS=OK\n", "returncode": 1},
                       {"stdout": "LIMITS=OK\n", "raises": True}):
            ops, _ = self._ops(**kwargs)
            self.assertIsNone(ops.verify_limits(), kwargs)

    def test_read_faults_requires_the_configured_file(self):
        ops, calls = self._ops(faults_file=None)
        self.assertIsNone(ops.read_faults())
        self.assertEqual(calls, [])
        ops, _ = self._ops(faults_file=str(Path("no-such-file.jsonl")))
        self.assertIsNone(ops.read_faults())     # 文件不可读 → None

    def test_read_faults_parses_the_bounded_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share-faults.jsonl"
            records = []
            for index in range(60):
                records.append(json.dumps({
                    "at": 1790000000.0 + index, "phase": "limit",
                    "status": 429, "code": "sharing_limit_reached",
                    "upstream_status": None, "exception_type": None,
                    "duration_ms": 1.5, "request_id": None,
                    "stream_started": False}))
            records.append(json.dumps({
                "at": 1790000100.0, "phase": "upstream", "status": 502,
                "code": "um_unavailable", "upstream_status": 503,
                "exception_type": None, "duration_ms": 120.5,
                "request_id": "a" * 32, "stream_started": False}))
            path.write_text("\n".join(records) + "\n", encoding="utf-8")
            ops, _ = self._ops(faults_file=str(path))
            verdict = ops.read_faults()
            self.assertIsNotNone(verdict)
            # 只统计末 50 行：第 1 条 limit 行被截掉。
            self.assertEqual(verdict["counts_by_phase"]["limit"], 49)
            self.assertEqual(verdict["counts_by_phase"]["upstream"], 1)
            self.assertEqual(verdict["counts_by_phase"]["auth"], 0)
            self.assertEqual(verdict["last_fault"]["phase"], "upstream")
            self.assertEqual(verdict["last_fault"]["code"], "um_unavailable")
            self.assertEqual(verdict["last_fault"]["upstream_status"], 503)
            self.assertEqual(verdict["last_fault"]["request_id"], "a" * 32)
            self.assertEqual(set(verdict["last_fault"]),
                             set(_OPS.FAULT_RECORD_KEYS))
            self.assertIs(verdict["file"]["present"], True)
            self.assertEqual(verdict["file"]["size"], path.stat().st_size)
            self.assertIsInstance(verdict["file"]["mtime"], float)

    def test_read_faults_tail_is_bounded_to_64_kib(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share-faults.jsonl"
            lines = []
            for _ in range(20):
                lines.append(json.dumps({
                    "at": 1.0, "phase": "auth", "status": 401,
                    "code": "invalid_api_key", "upstream_status": None,
                    "exception_type": None, "duration_ms": 1.0,
                    "request_id": "x" * 4096, "stream_started": False}))
            for index in range(3):
                lines.append(json.dumps({
                    "at": 2.0 + index, "phase": "upstream", "status": 502,
                    "code": "um_unavailable", "upstream_status": 503,
                    "exception_type": None, "duration_ms": 1.0,
                    "request_id": None, "stream_started": False}))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertGreater(path.stat().st_size, 64 * 1024)
            ops, _ = self._ops(faults_file=str(path))
            verdict = ops.read_faults()
            # 末 64 KiB 起点落在 auth 行中间 → 截断行不可解析被跳过，
            # 尾部 3 条 upstream 完整可读。
            self.assertEqual(verdict["counts_by_phase"]["upstream"], 3)
            self.assertLessEqual(verdict["counts_by_phase"]["auth"], 19)
            self.assertEqual(verdict["last_fault"]["phase"], "upstream")

    def test_read_faults_skips_unknown_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share-faults.jsonl"
            path.write_text(
                json.dumps({"at": 1.0, "phase": "future_phase",
                            "status": 500, "code": "x",
                            "upstream_status": None, "exception_type": None,
                            "duration_ms": 1.0, "request_id": None,
                            "stream_started": False}) + "\n"
                + "not-json{\n"
                + json.dumps(["not", "an", "object"]) + "\n"
                + json.dumps({"at": 2.0, "phase": "stream", "status": None,
                              "code": "stream_aborted",
                              "upstream_status": None,
                              "exception_type": None, "duration_ms": 1.0,
                              "request_id": None, "stream_started": True})
                + "\n", encoding="utf-8")
            ops, _ = self._ops(faults_file=str(path))
            verdict = ops.read_faults()
            self.assertEqual(verdict["counts_by_phase"]["stream"], 1)
            self.assertEqual(sum(verdict["counts_by_phase"].values()), 1)
            self.assertEqual(verdict["last_fault"]["code"], "stream_aborted")
            self.assertIs(verdict["last_fault"]["stream_started"], True)

    def test_read_faults_empty_file_is_explicit_no_fault_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share-faults.jsonl"
            path.write_text("", encoding="utf-8")
            ops, _ = self._ops(faults_file=str(path))
            verdict = ops.read_faults()
            self.assertIsNone(verdict["last_fault"])
            self.assertEqual(verdict["counts_by_phase"],
                             {phase: 0 for phase in _OPS.FAULT_PHASES})
            self.assertIs(verdict["file"]["present"], True)

    def test_status_adds_read_only_host_evidence(self):
        ops, calls = self._ops('"t","x","Ready"', "", watchdog_script=None,
                               faults_file=None)
        payload = ops.status()
        self.assertEqual(set(payload),
                         {"host", "task", "state", "host_limits",
                          "share_faults"})
        self.assertEqual(payload["host"], "scheduled-task")
        self.assertIsNone(payload["host_limits"])
        self.assertIsNone(payload["share_faults"])
        self.assertEqual(len(calls), 1)      # 未接线：零证据查询调用
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share-faults.jsonl"
            path.write_text(json.dumps({
                "at": 3.0, "phase": "limit", "status": 429,
                "code": "sharing_limit_reached", "upstream_status": None,
                "exception_type": None, "duration_ms": 2.0,
                "request_id": None, "stream_started": False}) + "\n",
                encoding="utf-8")
            ops2, calls2 = self._ops('"t","x","Ready"', "",
                                     faults_file=str(path))
            ops2._runner = lambda args, timeout: _Proc(
                0, stdout="LIMITS=OK\n")
            payload2 = ops2.status()
            # Routine health leaves security to the explicit evidence verb.
            self.assertIsNone(payload2["host_limits"])
            self.assertEqual(payload2["share_faults"]["last_fault"]["phase"],
                             "limit")


if __name__ == "__main__":
    unittest.main()
