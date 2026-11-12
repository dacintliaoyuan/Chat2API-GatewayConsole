# -*- coding: utf-8 -*-
"""8110 分享专用发送前调度（share_dispatch）的回归测试。

钉死的契约：

* 单 Key 先来先发（FIFO）；不同 Key 之间轮转，一个 Key 不能独占总轮转。
* 累计等待上限默认 10 秒（环境变量可覆盖），注入时钟即可证明，不真等；
  再次进入调度器或内部重试都不重置这个预算。
* 累计预算到期而仍未轮到时，以可重试超时结束本次等待：不授予、不 commit、
  不迟到发送，也不越过同 Key FIFO；轮到自己则正常授予。
* 取消、撤销、请求截止、预算超时与进程退出都只释放一次；释放后迟到发送被拒绝。
* 已提交的生成只被撤销中止：普通取消/截止/退出从不抢占已提交发送，
  撤销对 PENDING/GRANTED 释放、对 COMMITTED 调用已注册的 abort 回调。
* 只在 8110 分享实例的发送边界启用：8000 主链、直连 8110、
  归属未验证或协议/scope 不匹配时都不装配调度器。

隔离：单元用例不启动服务、不读写生产账本；chat2api 的 HTTP 用例沿用
tests/_chat2api_http_harness 的 B8-0 生命周期（私有临时账本 + 端口哨兵），
上游是进程内桩，归因密钥写在临时目录。
"""

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _write_private(path, text):
    """Fixture secrets must pass the POSIX 0600/current-uid gate."""
    from share_secret_protection import write_private_secret_path
    write_private_secret_path(Path(path), str(text).encode("utf-8"))


STUB_TOKEN = "stub-token"
ATTRIBUTION_SECRET = "s" * 48
ATTRIBUTION_KEY_ID = "friend"
DEFAULT_CHAT_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "chat-model",
    "choices": [{"index": 0, "message": {"role": "assistant",
                                         "content": "hi"},
                  "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _load(name, path):
    """Load a module by path under ``name`` (shared with the wiring)."""
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dispatch = _load("share_dispatch", HERE / "chat2api" / "share_dispatch.py")
chat2api = _load("chat2api_share_dispatch",
                 HERE / "chat2api" / "chat2api.py")
share = _load("share_gateway_share_dispatch",
              HERE / "chat2api" / "share_gateway.py")


class _Clock:
    """Injected monotonic clock: the 10s budget is proven without waiting."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class TestShareDispatchOrdering(unittest.TestCase):
    def _clocked(self, max_wait=None):
        clock = _Clock()
        return dispatch.ShareDispatcher(max_wait=max_wait, clock=clock), clock

    # ---------- FIFO / rotation ----------

    def test_single_key_is_first_in_first_out(self):
        dispatcher, _ = self._clocked()
        first = dispatcher.register("k1", "first")
        second = dispatcher.register("k1", "second")
        third = dispatcher.register("k1", "third")
        self.assertEqual(dispatcher.poll(third), dispatch.OUTCOME_WAITING)
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_WAITING)
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_GRANTED)
        self.assertTrue(second.commit())
        self.assertEqual(dispatcher.poll(third), dispatch.OUTCOME_GRANTED)
        self.assertTrue(third.commit())
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_keys_rotate_so_one_key_cannot_monopolise(self):
        dispatcher, _ = self._clocked()
        first = dispatcher.register("k1", "k1-first")
        second = dispatcher.register("k1", "k1-second")
        other = dispatcher.register("k2", "k2-only")
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        # k1's second request must NOT jump ahead of the waiting k2.
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_WAITING)
        self.assertEqual(dispatcher.poll(other), dispatch.OUTCOME_GRANTED)
        self.assertTrue(other.commit())
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_GRANTED)

    def test_three_keys_grant_in_rotation_order(self):
        dispatcher, _ = self._clocked()
        tickets = [dispatcher.register("k%d" % i, "r%d" % i)
                   for i in range(3)]
        self.assertEqual(dispatcher.poll(tickets[1]), dispatch.OUTCOME_WAITING)
        self.assertEqual(dispatcher.poll(tickets[2]), dispatch.OUTCOME_WAITING)
        granted = []
        while len(granted) < 3:
            for ticket in tickets:
                if ticket.request_id in granted:
                    continue
                outcome = dispatcher.poll(ticket)
                if outcome == dispatch.OUTCOME_GRANTED:
                    granted.append(ticket.request_id)
                    self.assertTrue(ticket.commit())
                    break
        self.assertEqual(granted, ["r0", "r1", "r2"])

    # ---------- cumulative budget ----------

    def test_a_zero_budget_grants_an_immediately_available_turn(self):
        # Cloud console.env sets CHAT2API_SHARE_DISPATCH_MAX_WAIT_S=0: no extra
        # queue, but the rotation and ticket release still work. A request that
        # is already at its turn must send instead of being refused by the
        # zero-length budget.
        dispatcher, clock = self._clocked(max_wait=0.0)
        first = dispatcher.register("k1", "first")
        self.assertEqual(first.deadline, 0.0)
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        second = dispatcher.register("k1", "second")
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_GRANTED)
        self.assertTrue(second.commit())
        other = dispatcher.register("k2", "other")
        self.assertEqual(dispatcher.poll(other), dispatch.OUTCOME_GRANTED)
        self.assertTrue(other.commit())
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertEqual(dispatcher.stats()["timed_out"], 0)
        self.assertEqual(clock.now, 0.0)

    def test_a_zero_budget_refuses_a_ticket_that_would_have_to_wait(self):
        dispatcher, _ = self._clocked(max_wait=0.0)
        first = dispatcher.register("k1", "first")
        behind = dispatcher.register("k1", "behind")
        # `behind` is not its key's head yet: a zero budget cannot wait for
        # the FIFO turn, so it ends as the same retryable timeout.
        self.assertEqual(dispatcher.poll(behind), dispatch.OUTCOME_TIMEOUT)
        self.assertTrue(behind.timed_out)
        self.assertFalse(behind.commit())
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertEqual(dispatcher.stats()["timed_out"], 1)

    def test_default_budget_is_ten_seconds(self):
        self.assertEqual(dispatch.DEFAULT_MAX_WAIT_S, 10.0)
        self.assertEqual(dispatch.max_wait_seconds({}), 10.0)
        self.assertEqual(dispatch.max_wait_seconds(
            {dispatch.DISPATCH_MAX_WAIT_ENV: "3.5"}), 3.5)
        self.assertEqual(dispatch.max_wait_seconds(
            {dispatch.DISPATCH_MAX_WAIT_ENV: "not-a-number"}), 10.0)
        self.assertEqual(dispatch.max_wait_seconds(
            {dispatch.DISPATCH_MAX_WAIT_ENV: "-1"}), 10.0)
        with mock.patch.dict(os.environ, {dispatch.DISPATCH_MAX_WAIT_ENV: "2.5"}):
            self.assertEqual(dispatch.ShareDispatcher().max_wait, 2.5)

    def test_cumulative_budget_is_not_reset_by_reentry(self):
        dispatcher, clock = self._clocked()
        blocker = dispatcher.register("k1", "blocker")
        self.assertEqual(dispatcher.poll(blocker), dispatch.OUTCOME_GRANTED)
        self.assertTrue(blocker.commit())
        # A new key keeps the rotation away from k1, so this ticket waits.
        other = dispatcher.register("k2", "other")
        ticket = dispatcher.register("k1", "r1")
        self.assertEqual(ticket.deadline, 10.0)
        clock.now = 6.0
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_WAITING)
        # Re-entering the gate returns the SAME ticket: the budget is fixed.
        self.assertIs(dispatcher.register("k1", "r1"), ticket)
        self.assertEqual(ticket.deadline, 10.0)
        clock.now = 9.9
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_WAITING)
        clock.now = 10.0
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_TIMEOUT)
        self.assertTrue(ticket.timed_out)
        # A timed-out ticket is released exactly once and may never send.
        self.assertFalse(ticket.commit())
        self.assertEqual(dispatcher.pending_count(), 1)  # k2 still waits
        self.assertEqual(dispatcher.stats()["timed_out"], 1)

    def test_timeout_refuses_the_send_instead_of_sending_late(self):
        dispatcher, clock = self._clocked()
        blocker = dispatcher.register("k2", "blocker")
        ticket = dispatcher.register("k1", "r1")
        clock.now = 10.0
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_TIMEOUT)
        self.assertTrue(ticket.timed_out)
        self.assertEqual(ticket.state, "closed")
        # The budget ended the wait; it never grants a late send and never
        # reports the request as committed or successful.
        self.assertFalse(ticket.commit())
        self.assertFalse(ticket.release("timeout"))
        self.assertEqual(dispatcher.pending_count(), 1)
        self.assertEqual(dispatcher.stats()["timed_out"], 1)
        # A repeated poll keeps reporting the same terminal outcome, and a late
        # re-entry with the same request id is refused too.
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_TIMEOUT)
        late = dispatcher.register("k1", "r1")
        self.assertEqual(late.state, "closed")
        self.assertFalse(late.commit())
        self.assertEqual(dispatcher.poll(late), dispatch.OUTCOME_TIMEOUT)
        # The blocker was never touched: closing a timed-out middle ticket must
        # not release the queue position of another request. The blocker is
        # granted while its own budget is still available.
        clock.now = 9.9
        self.assertEqual(dispatcher.poll(blocker), dispatch.OUTCOME_GRANTED)
        self.assertTrue(blocker.commit())

    def test_a_later_same_key_ticket_times_out_without_breaking_fifo(self):
        dispatcher, clock = self._clocked()
        first = dispatcher.register("k1", "first")
        behind = dispatcher.register("k1", "behind")
        # k2 registers later, so its own cumulative deadline is still ahead.
        clock.now = 5.0
        other = dispatcher.register("k2", "other")
        clock.now = 10.0
        # `behind` is neither its key's head nor the rotation head: its budget
        # is spent, so it ends as a retryable timeout instead of jumping
        # `first` or sending late.
        self.assertEqual(dispatcher.poll(behind), dispatch.OUTCOME_TIMEOUT)
        self.assertFalse(behind.commit())
        self.assertEqual(dispatcher.pending_count(), 2)
        # Closing a middle ticket must not rotate k1 behind k2: `first` is still
        # the oldest waiter of the rotation head key and is granted while its own
        # budget (deadline 10.0) is still available.
        clock.now = 9.9
        self.assertEqual(dispatcher.poll(other), dispatch.OUTCOME_WAITING)
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        self.assertEqual(dispatcher.poll(other), dispatch.OUTCOME_GRANTED)
        self.assertTrue(other.commit())
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_budget_is_a_hard_cap_even_when_the_turn_arrives(self):
        # The reviewer's injected-clock repro: T1 is granted at 9.9 s, the other
        # Key's B1 is granted at 10.1 s (its own budget still available),
        # and T2's deadline is 10.0 s. T2 must NOT be granted at 10.2 s just
        # because it became the rotation head; the cumulative 10 s is a hard cap.
        dispatcher, clock = self._clocked()
        first = dispatcher.register("k1", "T1")
        second = dispatcher.register("k1", "T2")
        # B1 registers one second later, so its own deadline (11.0) is still
        # ahead when the rotation reaches it at 10.1.
        clock.now = 1.0
        other = dispatcher.register("k2", "B1")
        clock.now = 9.9
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertTrue(first.commit())
        clock.now = 10.1
        self.assertEqual(dispatcher.poll(other), dispatch.OUTCOME_GRANTED)
        self.assertTrue(other.commit())
        clock.now = 10.2
        # T2 is now the rotation head and the same-Key FIFO head, but its
        # deadline (10.0) has passed: it must time out, not send late.
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_TIMEOUT)
        self.assertTrue(second.timed_out)
        self.assertFalse(second.commit())
        self.assertEqual(second.state, "closed")
        self.assertEqual(dispatcher.stats()["timed_out"], 1)
        self.assertEqual(dispatcher.pending_count(), 0)

    # ---------- exactly-once releases ----------

    def test_request_deadline_releases_before_send(self):
        dispatcher, _ = self._clocked()
        ticket = dispatcher.register("k1", "r1")
        self.assertEqual(dispatcher.poll(ticket, request_deadline=0.0),
                         dispatch.OUTCOME_DEADLINE)
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertFalse(ticket.commit())
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_DEADLINE)
        self.assertFalse(ticket.release("deadline"))

    def test_cancel_releases_exactly_once_and_refuses_late_send(self):
        dispatcher, _ = self._clocked()
        ticket = dispatcher.register("k1", "r1")
        self.assertTrue(ticket.release("cancel"))
        self.assertFalse(ticket.release("cancel"))
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_CANCELLED)
        self.assertFalse(ticket.commit())
        # A late re-entry with the same request id never sends either.
        late = dispatcher.register("k1", "r1")
        self.assertEqual(late.state, "closed")
        self.assertFalse(late.commit())
        self.assertFalse(late.release("cancel"))

    def test_revoke_releases_pending_tickets_and_aborts_committed(self):
        dispatcher, _ = self._clocked()
        committed = dispatcher.register("k1", "committed")
        pending = dispatcher.register("k1", "pending")
        self.assertEqual(dispatcher.poll(committed), dispatch.OUTCOME_GRANTED)
        self.assertTrue(committed.commit())
        self.assertEqual(dispatcher.revoke_key("k1"), (1, 1))
        self.assertEqual(dispatcher.revoke_key("k1"), (0, 0))
        self.assertFalse(pending.commit())
        # A committed generation is aborted ONLY by a key revoke (D18-3).
        self.assertFalse(committed.commit())
        self.assertFalse(committed.granted)
        self.assertEqual(committed.state, "closed")
        self.assertEqual(committed.reason, "revoke")

    def test_revoke_closes_granted_and_aborts_committed(self):
        dispatcher, _ = self._clocked()
        committed = dispatcher.register("k1", "committed")
        granted = dispatcher.register("k1", "granted")
        pending = dispatcher.register("k1", "pending")
        self.assertEqual(dispatcher.poll(committed), dispatch.OUTCOME_GRANTED)
        self.assertTrue(committed.commit())
        self.assertEqual(dispatcher.poll(granted), dispatch.OUTCOME_GRANTED)
        self.assertEqual(granted.state, "granted")
        # Both the granted (not yet sent) and the pending ticket are closed;
        # the committed generation is aborted by the same revoke.
        self.assertEqual(dispatcher.revoke_key("k1"), (2, 1))
        self.assertEqual(dispatcher.revoke_key("k1"), (0, 0))
        self.assertFalse(granted.commit())
        self.assertFalse(pending.commit())
        self.assertEqual(dispatcher.poll(granted), dispatch.OUTCOME_REVOKED)
        self.assertFalse(committed.commit())
        self.assertFalse(committed.granted)
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_revoke_only_closes_the_named_key(self):
        dispatcher, _ = self._clocked()
        first = dispatcher.register("k1", "first")
        second = dispatcher.register("k2", "second")
        self.assertEqual(dispatcher.poll(first), dispatch.OUTCOME_GRANTED)
        self.assertEqual(dispatcher.revoke_key("k1"), (1, 0))
        self.assertEqual(dispatcher.revoke_key("missing"), (0, 0))
        self.assertFalse(first.commit())
        self.assertEqual(dispatcher.poll(second), dispatch.OUTCOME_GRANTED)
        self.assertTrue(second.commit())

    def test_process_exit_releases_every_ticket_exactly_once(self):
        dispatcher, _ = self._clocked()
        first = dispatcher.register("k1", "first")
        second = dispatcher.register("k2", "second")
        self.assertEqual(dispatcher.close_all("exit"), 2)
        self.assertEqual(dispatcher.close_all("exit"), 0)
        self.assertEqual(dispatcher.pending_count(), 0)
        for ticket in (first, second):
            self.assertFalse(ticket.commit())
            self.assertFalse(ticket.release("exit"))

    def test_committed_retry_is_not_requeued(self):
        dispatcher, _ = self._clocked()
        ticket = dispatcher.register("k1", "r1")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        # The internal retry of the same logical request re-enters the gate.
        self.assertIs(dispatcher.register("k1", "r1"), ticket)
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        self.assertEqual(dispatcher.pending_count(), 0)

    # ---------- committed abort (D18-3) ----------

    def test_attach_abort_closes_the_committed_response_on_revoke(self):
        dispatcher, _ = self._clocked()
        ticket = dispatcher.register("k1", "r1")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        closed = []
        self.assertTrue(ticket.attach_abort(lambda: closed.append("close")))
        self.assertEqual(dispatcher.revoke_key("k1"), (0, 1))
        self.assertEqual(closed, ["close"])
        # A repeated revoke never aborts or closes twice.
        self.assertEqual(dispatcher.revoke_key("k1"), (0, 0))
        self.assertFalse(ticket.abort())
        self.assertEqual(closed, ["close"])

    def test_attach_after_revoke_closes_immediately(self):
        # The revoke can arrive between the commit and the upstream response
        # object: the later registration must still close the response at once.
        dispatcher, _ = self._clocked()
        ticket = dispatcher.register("k1", "r1")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        self.assertEqual(dispatcher.revoke_key("k1"), (0, 1))
        closed = []
        self.assertTrue(ticket.attach_abort(lambda: closed.append("close")))
        self.assertEqual(closed, ["close"])

    def test_ordinary_release_never_aborts_a_committed_ticket(self):
        # Only a key revoke aborts a committed send; ordinary cancel,
        # deadline and process exit never preempt it (D18-3).
        for reason in ("cancel", "deadline", "exit"):
            with self.subTest(reason=reason):
                dispatcher, _ = self._clocked()
                ticket = dispatcher.register("k1", "r1")
                self.assertEqual(dispatcher.poll(ticket),
                                 dispatch.OUTCOME_GRANTED)
                self.assertTrue(ticket.commit())
                closed = []
                self.assertTrue(ticket.attach_abort(
                    lambda: closed.append("close")))
                self.assertTrue(ticket.release(reason))
                self.assertEqual(closed, [])
                self.assertEqual(ticket.state, "closed")

    def test_attach_abort_ignores_pending_and_granted_tickets(self):
        dispatcher, _ = self._clocked()
        pending = dispatcher.register("k1", "pending")
        closed = []
        self.assertFalse(pending.attach_abort(lambda: closed.append("close")))
        self.assertEqual(dispatcher.poll(pending), dispatch.OUTCOME_GRANTED)
        self.assertFalse(pending.attach_abort(lambda: closed.append("close")))
        self.assertEqual(closed, [])
        self.assertTrue(pending.commit())

    def test_register_rejects_empty_identities(self):
        dispatcher, _ = self._clocked()
        for key_id, request_id in (("", "r"), (None, "r"), ("k", ""),
                                    ("k", None), (1, "r")):
            with self.assertRaises(ValueError):
                dispatcher.register(key_id, request_id)

    # ---------- concurrent stress ----------

    def test_concurrent_stress_has_no_deadlock_and_no_loss(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=5.0)
        keys = ["key-%d" % i for i in range(4)]
        total = 40
        outcomes = {}
        errors = []
        lock = threading.Lock()

        def worker(index):
            key_id = keys[index % len(keys)]
            request_id = "req-%03d" % index
            try:
                ticket = dispatcher.register(key_id, request_id)
                outcome = ticket.wait_turn()
                if outcome == dispatch.OUTCOME_TIMEOUT:
                    # The budget ended before this request's turn: it was
                    # released exactly once without a grant, so it must never
                    # commit or release a second time.
                    if ticket.commit():
                        raise AssertionError("timed-out ticket committed")
                    if ticket.release("sent"):
                        raise AssertionError("timed-out ticket released twice")
                elif outcome == dispatch.OUTCOME_GRANTED:
                    if not ticket.commit():
                        raise AssertionError("commit refused")
                    if not ticket.release("sent"):
                        raise AssertionError("release refused")
                else:
                    raise AssertionError(outcome)
                with lock:
                    outcomes[request_id] = outcome
            except BaseException as exc:  # reported below, never swallowed
                with lock:
                    errors.append((request_id, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(total)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), total)
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertEqual(dispatcher.stats()["registered"], total)


class TestShareDispatchWiring(unittest.TestCase):
    """chat2api 的装配开关：只有 VERIFIED 归属才装配调度器。"""

    def setUp(self):
        self.addCleanup(self._reset_dispatcher)
        self._reset_dispatcher()
        self.addCleanup(chat2api._request_facts.__dict__.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(
                Path(self.tmp.name) / "revoke.state")})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        chat2api._reset_share_revoke_state()
        self.addCleanup(chat2api._reset_share_revoke_state)

    @staticmethod
    def _reset_dispatcher():
        dispatcher = chat2api._share_dispatcher
        if dispatcher is not None:
            dispatcher.close_all("test")
        chat2api._share_dispatcher = None

    def test_absent_or_unverified_attribution_never_assembles(self):
        for attribution in ("absent", "partial_headers", "bad_signature",
                             "replayed", None):
            chat2api.set_request_facts("chat", None, None, attribution)
            self.assertIsNone(chat2api.share_dispatch_ticket("chat"))
            self.assertIsNone(chat2api._share_dispatcher)

    def test_verified_attribution_without_identity_never_assembles(self):
        chat2api.set_request_facts("chat", None, "r" * 32, "verified")
        self.assertIsNone(chat2api.share_dispatch_ticket("chat"))
        chat2api.set_request_facts("chat", "key", None, "verified")
        self.assertIsNone(chat2api.share_dispatch_ticket("chat"))
        self.assertIsNone(chat2api._share_dispatcher)

    def test_protocol_mismatch_never_assembles(self):
        chat2api.set_request_facts("chat", "key", "r" * 32, "verified")
        for protocol in ("messages", "responses", "embeddings", "admin"):
            self.assertIsNone(chat2api.share_dispatch_ticket(protocol))
        self.assertIsNone(chat2api._share_dispatcher)

    def test_verified_attribution_registers_one_ticket(self):
        chat2api.set_request_facts("chat", "key", "r" * 32, "verified")
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertIsNotNone(ticket)
        self.assertIs(chat2api._request_facts.dispatch_ticket, ticket)
        self.assertEqual(chat2api._share_dispatcher.stats()["pending"], 1)
        # Re-entering the gate never resets the cumulative budget.
        self.assertIs(chat2api.share_dispatch_ticket("chat"), ticket)
        self.assertEqual(chat2api._share_dispatcher.stats()["registered"], 1)

    def test_next_request_releases_the_previous_ticket_once(self):
        chat2api.set_request_facts("chat", "key", "r1", "verified")
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertIsNotNone(ticket)
        chat2api.set_request_facts("chat", None, None, "absent")
        self.assertFalse(ticket.release("test"))
        self.assertEqual(chat2api._share_dispatcher.stats()["pending"], 0)
        self.assertEqual(chat2api._share_dispatcher.stats()["finished"], 1)

    def test_process_exit_closes_the_dispatcher_once(self):
        chat2api.set_request_facts("chat", "key", "r1", "verified")
        ticket = chat2api.share_dispatch_ticket("chat")
        chat2api._close_share_dispatch()
        self.assertFalse(ticket.commit())
        self.assertEqual(chat2api._share_dispatcher.pending_count(), 0)


class TestRevokeNoticeLoadAck(unittest.TestCase):
    """Review #14: the notice failure is observed through the REAL load ACK.

    The old test called ``_send_revoke_notices`` directly, so its name claimed
    more coverage than it had. This test drives the REAL
    ``ShareServer.handle_instance_control`` with signed schema-v2 load requests
    (the same path the broker uses), makes ONE notice sender raise, and proves
    the failure is only a bounded fault while the load ACK still succeeds.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "projection.json"
        # The load ACK now records the applied projection generation in the
        # revoke state; keep that state out of the PRODUCT default path.
        self.state_file = Path(self.tmp.name) / "revoke.state"
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        self.entry = {"key_id": "revoke-ack", "name": "friend",
                      "secret_hash": hashlib.sha256(b"secret").hexdigest(),
                      "secret_version": 1, "enabled": True,
                      "max_concurrency": 4}
        self._write(generation=1, enabled=True)
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config, upstream_port=1,
            attribution_secret="s" * 48)
        self.addCleanup(self.server.server_close)

    def _write(self, *, generation, enabled):
        entry = dict(self.entry, enabled=enabled)
        self.config.write_text(json.dumps({
            "schema_version": 2,
            "config_generation": generation,
            "share_max_concurrency": 8,
            "data": [entry],
        }), encoding="utf-8")

    def _signed_load(self, *, generation, enabled):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        self._write(generation=generation, enabled=enabled)
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        secret = "inst-secret"
        fields = {
            "generation": generation,
            "fingerprint": hashlib.sha256(json.dumps(
                payload, sort_keys=True).encode()).hexdigest(),
        }
        body = sign(secret, {
            "v": 2, "action": "load",
            "request_id": "req-load-%d" % generation,
            "ts": int(time.time()) + 1,
            "instance_id": self.server.instance_id,
            "fields": fields,
        }, INSTANCE_SIGNED_KEYS)
        return self.server.handle_instance_control(body, secret)

    def test_notice_failure_through_load_ack_records_fault_only(self):
        first = self._signed_load(generation=1, enabled=True)
        self.assertTrue(first["ok"], first)
        self.assertIn(self.entry["key_id"], self.server._projection_enabled)

        with mock.patch.object(
                self.server, "_send_revoke_notice",
                side_effect=RuntimeError("notice failed")):
            second = self._signed_load(generation=2, enabled=False)

        # The REAL load ACK still succeeds: the projection was installed, the
        # generation advanced and the ACK never turned the notice failure into a
        # load failure.
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["result"]["installed"], second)
        self.assertEqual(second["result"]["generation"], 2)
        self.assertNotIn(self.entry["key_id"],
                         self.server._projection_enabled)
        self.assertEqual(self.server.last_seen_generation, 2)
        # The failure is ONE bounded fault with the stable code, not a second
        # ACK shape and not an escaping exception.
        self.assertEqual(
            [record["code"] for record in self.server.fault_ring],
            ["share_revoke_notice_failed"])


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """In-process UM stub: records the forwarded chat body."""

    protocol_version = "HTTP/1.1"
    chat_bodies = []

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path != "/api/chat/completions":
            self._send_json(404, {"detail": "not found"})
            return
        StubUpstreamHandler.chat_bodies.append(body)
        self._send_json(200, DEFAULT_CHAT_JSON)


class TestChat2ApiSendBoundary(unittest.TestCase):
    """真实 Handler 的发送边界：8000 主链与 8110 分享实例的启用差异。"""

    def setUp(self):
        # B8-0 隔离生命周期：私有临时账本，端点只绑回环临时端口。
        self.usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-share-dispatch.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)
        # 归因密钥写在临时目录；调度器单元随用例重建，不跨用例残留。
        key_file = Path(self.usage_dir) / "share-attribution.key"
        _write_private(key_file, ATTRIBUTION_SECRET)
        self.key_file = key_file
        key_patch = mock.patch.object(
            chat2api, "SHARE_ATTRIBUTION_KEY_FILE", str(key_file))
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(
                Path(self.usage_dir) / "revoke.state")})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        chat2api._reset_share_revoke_state()
        self.addCleanup(chat2api._reset_share_revoke_state)
        chat2api._attribution_key_cache["stat"] = None
        chat2api._attribution_key_cache["secret"] = None
        self.addCleanup(self._reset_attribution_key_cache)
        # 在服务创建之前注册：本用例的 handler 全部排空后才释放调度器。
        self.addCleanup(self._reset_dispatcher)
        self._reset_dispatcher()

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                         require_drained=False)
        StubUpstreamHandler.chat_bodies = []

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = self.proxy
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="chat2api-share-dispatch")
        self.base = f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def _reset_dispatcher():
        dispatcher = chat2api._share_dispatcher
        if dispatcher is not None:
            dispatcher.close_all("test")
        chat2api._share_dispatcher = None

    @staticmethod
    def _reset_attribution_key_cache():
        chat2api._attribution_key_cache["stat"] = None
        chat2api._attribution_key_cache["secret"] = None

    def _post_chat(self, headers=None):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "chat-model",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=headers or {}, timeout=20)

    def test_default_chain_never_assembles_a_dispatcher(self):
        response = self._post_chat()
        self.assertEqual(response.status_code, 200)
        # The native JSON body and the forwarded body are unchanged.
        self.assertEqual(response.json(), DEFAULT_CHAT_JSON)
        self.assertEqual(StubUpstreamHandler.chat_bodies,
                         [{"model": "chat-model",
                           "messages": [{"role": "user", "content": "hi"}]}])
        self.assertIsNone(chat2api._share_dispatcher)

    def test_unverified_request_on_8110_never_assembles_a_dispatcher(self):
        # 直连 8110 只带部分标记：verify 返回 partial_headers，不装配调度器。
        partial = {"X-Share-Attribution-Version": "1",
                   "X-Share-Key-Id": "friend",
                   "X-Share-Request-Id": "a" * 32}
        response = self._post_chat(partial)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(chat2api._share_dispatcher)

    def test_tampered_marker_on_8110_never_assembles_a_dispatcher(self):
        # 五个标记齐全但签名错误：verify 返回 bad_signature，不装配调度器。
        headers, _ = share.build_attribution(
            ATTRIBUTION_SECRET, ATTRIBUTION_KEY_ID)
        headers[share.X_ATTRIBUTION_SIGNATURE] = "0" * 64
        response = self._post_chat(headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(chat2api._share_dispatcher)

    def test_verified_share_request_is_scheduled_once(self):
        headers, request_id = share.build_attribution(
            ATTRIBUTION_SECRET, ATTRIBUTION_KEY_ID)
        self.assertEqual(len(request_id), 32)
        response = self._post_chat(headers)
        self.assertEqual(response.status_code, 200)
        dispatcher = chat2api._share_dispatcher
        self.assertIsNotNone(dispatcher)
        stats = dispatcher.stats()
        self.assertEqual(stats["committed"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["registered"], 1)

    def test_revoke_between_grant_and_commit_is_a_retryable_429(self):
        # The revoke notice can arrive after wait_turn returned GRANTED but
        # before Proxy.call commits: commit() is then False. The handler must
        # still answer the retryable share_dispatch_revoked 429, not record the
        # request as a client disconnect.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(ATTRIBUTION_KEY_ID, "revoked-before-commit")

        class _RevokeAfterGrant:
            """Grant the REAL ticket, then revoke it before the commit."""

            def __init__(self, dispatcher, ticket):
                self._dispatcher = dispatcher
                self._ticket = ticket

            def wait_turn(self, **kwargs):
                outcome = self._ticket.wait_turn(**kwargs)
                if outcome == dispatch.OUTCOME_GRANTED:
                    self._dispatcher.revoke_key(self._ticket.key_id)
                return outcome

            def commit(self):
                return self._ticket.commit()

            @property
            def outcome(self):
                return self._ticket.outcome

        wrapper = _RevokeAfterGrant(dispatcher, ticket)
        chat2api._share_dispatcher = dispatcher
        headers, _request_id = share.build_attribution(
            ATTRIBUTION_SECRET, ATTRIBUTION_KEY_ID)
        with mock.patch.object(chat2api, "share_dispatch_ticket",
                               return_value=wrapper):
            response = self._post_chat(headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"),
                         str(chat2api.SHARE_DISPATCH_RETRY_AFTER_S))
        self.assertEqual(response.json()["error"]["type"],
                         "share_dispatch_revoked")
        # The revoked request never reached the upstream.
        self.assertEqual(StubUpstreamHandler.chat_bodies, [])
        rows = [json.loads(line) for line in
                Path(chat2api.USAGE_LOG).read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[-1]["status"], "share_dispatch_revoked")
        self.assertEqual(dispatcher.pending_count(), 0)
        self.assertEqual(dispatcher.stats()["timed_out"], 0)

    def test_dispatch_timeout_is_a_retryable_429_without_an_upstream_send(self):
        # max_wait=0 plus a never-polled blocker: every verified request's
        # ticket is never at its turn, so its cumulative budget is spent.
        dispatcher = dispatch.ShareDispatcher(max_wait=0.0)
        blocker = dispatcher.register("blocking-key", "blocking-request")
        chat2api._share_dispatcher = dispatcher
        cases = (
            ("/v1/chat/completions",
             {"model": "chat-model",
              "messages": [{"role": "user", "content": "hi"}]}),
            ("/v1/messages",
             {"model": "claude-x", "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]}),
            ("/v1/responses",
             {"model": "chat-model", "input": "hi"}),
        )
        for path, body in cases:
            headers, _request_id = share.build_attribution(
                ATTRIBUTION_SECRET, ATTRIBUTION_KEY_ID)
            # The service runs chat2api.py as a script (its directory is on
            # sys.path); the by-path test loader must expose the same module
            # name for the handler's call-time ``from share_gateway import``.
            with mock.patch.dict(sys.modules, {"share_gateway": share}):
                response = requests.post(f"{self.base}{path}", json=body,
                                        headers=headers, timeout=20)
            self.assertEqual(response.status_code, 429, path)
            self.assertEqual(
                response.headers.get("Retry-After"),
                str(chat2api.SHARE_DISPATCH_RETRY_AFTER_S), path)
            self.assertEqual(response.json()["error"]["type"],
                             "share_dispatch_timeout", path)
        # The refused requests never reached the upstream stub.
        self.assertEqual(StubUpstreamHandler.chat_bodies, [])
        # Each refusal is recorded with its OWN status: never "ok" and never
        # the client-disconnect cancellation status.
        rows = [json.loads(line) for line in
                Path(chat2api.USAGE_LOG).read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["status"] for row in rows],
                         ["share_dispatch_timeout"] * len(cases))
        self.assertEqual(dispatcher.stats()["timed_out"], len(cases))
        self.assertTrue(blocker.release("test"))


if __name__ == "__main__":
    unittest.main()
