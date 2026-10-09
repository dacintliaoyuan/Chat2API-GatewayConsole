"""C1/C2 release-side regression: bounded start readiness + lifecycle
consistency of the explicit managed open action.

The gateway process needs a moment to bind the share port after the task starts;
the old single immediate ``verify_running`` reported ``unconfirmed`` for a healthy
start and the console rolled the whole chain back (5x observed
2026-10-03/06/07). The console's "layer already running" branch used to only
observe, so the protected lifecycle record stayed at the earlier
emergency-stop ``stopped`` and the supervisor kept ``managed_stop_in_force``.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import console_share_actions as orch  # noqa: E402
import share_broker_host  # noqa: E402
import share_broker_ops as share_broker_ops  # noqa: E402
import share_broker_protocol as bp  # noqa: E402
import share_broker_server as share_broker_server  # noqa: E402
from share_broker_client import BrokerClient  # noqa: E402
from share_broker_host import (BrokerHostCoordinator,  # noqa: E402
                               InstanceRegistry, ReplayRegistry)


class StepClock:
    """Monotonic fixture clock: each sleep advances it by the slept amount."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedReadinessHost:
    """Host fixture: the OS action + the readiness EVIDENCE are scripted.

    Only ``start_host``/``verify_running``/``stop_host`` live here - the
    bounded readiness confirmation (budget, cadence, never re-running the task)
    stays in the product coordinator. ``clock``/``start_cost``/``verify_cost``
    let a test simulate the DURATION of the underlying operations so the total
    budget (not just the polling wait) is observable.
    """

    def __init__(self, readiness, clock=None, start_cost=0.0,
                 verify_cost=0.0):
        self.readiness = list(readiness)
        self.start_calls = 0
        self.verify_calls = 0
        self.running = False
        self.start_result = True
        self.verify_gate = None
        self.clock = clock
        self.start_cost = start_cost
        self.verify_cost = verify_cost

    def _spend(self, cost):
        if self.clock is not None and cost:
            self.clock.now += cost

    def start_host(self):
        self.start_calls += 1
        self._spend(self.start_cost)
        if self.start_result is True:
            self.running = True
        return self.start_result

    def stop_host(self):
        self.running = False
        return True

    def verify_running(self):
        self.verify_calls += 1
        self._spend(self.verify_cost)
        gate = self.verify_gate
        if gate is not None:
            self.verify_gate = None
            gate.wait(5)
        if not self.readiness:
            return self.running
        value = self.readiness.pop(0)
        self.running = value is True
        return value

    def verify_closed(self):
        return not self.running

    def status(self):
        return {"host": "fixture", "running": self.running}


class StartReadinessTests(unittest.TestCase):
    """C1: the restricted start verb confirms readiness with a BOUNDED poll."""

    def _coordinator(self, host, ready_timeout=6.0, interval=0.25):
        clock = StepClock()
        coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=host,
            instance_call=lambda *a, **k: {"ok": True, "result": {}},
            clock=clock, sleep=clock.sleep,
            start_ready_timeout=ready_timeout,
            start_poll_interval=interval)
        return coordinator, clock

    def _start(self, coordinator):
        return coordinator.handle_management(
            bp.build_request("mgmt-k", "start", {}))

    def test_immediately_ready_runs_the_task_once_without_sleeping(self):
        host = ScriptedReadinessHost([True])
        coordinator, clock = self._coordinator(host)
        reply = self._start(coordinator)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"started": True})
        self.assertEqual(host.start_calls, 1, "任务只能被 Run 一次")
        self.assertEqual(host.verify_calls, 1)
        self.assertEqual(clock.sleeps, [], "就绪时不等待")

    def test_not_ready_a_few_times_then_ready_is_confirmed(self):
        host = ScriptedReadinessHost([False, None, False, True])
        coordinator, clock = self._coordinator(host)
        reply = self._start(coordinator)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"started": True})
        self.assertEqual(host.start_calls, 1, "就绪轮询不得重复 Run")
        self.assertEqual(host.verify_calls, 4)
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])
        self.assertLessEqual(clock.now, 6.0, "轮询必须受总时限约束")

    def test_never_ready_times_out_and_never_re_runs_the_task(self):
        host = ScriptedReadinessHost([False, None, False, None, False])
        coordinator, clock = self._coordinator(host, ready_timeout=1.0)
        reply = self._start(coordinator)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertEqual(host.start_calls, 1)
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25, 0.25])

    def test_refused_start_does_not_poll_readiness(self):
        host = ScriptedReadinessHost([True])
        host.start_result = False
        coordinator, clock = self._coordinator(host)
        reply = self._start(coordinator)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertEqual(host.start_calls, 1)
        self.assertEqual(host.verify_calls, 0, "拒绝后不得进入就绪轮询")
        self.assertEqual(clock.sleeps, [])

    def test_raising_start_is_reported_without_polling(self):
        host = ScriptedReadinessHost([True])

        def boom():
            host.start_calls += 1
            raise OSError("schtasks refused")

        host.start_host = boom
        coordinator, clock = self._coordinator(host)
        with self.assertRaises(OSError):
            self._start(coordinator)
        self.assertEqual(host.start_calls, 1)
        self.assertEqual(host.verify_calls, 0)

    def test_concurrent_stop_wins_over_late_readiness(self):
        host = ScriptedReadinessHost([False, True, True])
        coordinator, clock = self._coordinator(host, ready_timeout=2.0)
        gate = threading.Event()
        host.verify_gate = gate
        reply = {}
        worker = threading.Thread(
            target=lambda: reply.update(self._start(coordinator)))
        worker.start()
        while host.verify_calls == 0:
            time.sleep(0.01)
        stop = coordinator.handle_management(
            bp.build_request("mgmt-k", "emergency-stop", {}))
        self.assertTrue(stop["ok"], stop)
        self.assertTrue(stop["result"]["stopped"], stop)
        gate.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(reply.get("ok"), reply)
        self.assertEqual(reply.get("error_code"), "unconfirmed")
        self.assertEqual(host.start_calls, 1)

    def test_readiness_budget_stays_under_the_wire_deadline(self):
        coordinator, _clock = self._coordinator(ScriptedReadinessHost([True]))
        self.assertLess(coordinator.start_ready_timeout,
                        bp.DEFAULT_DEADLINE_SECONDS)
        self.assertLess(coordinator.start_ready_timeout,
                        orch._BACKEND_DEFAULT_S)
        self.assertGreater(coordinator.start_poll_interval, 0.0)
        self.assertLessEqual(coordinator.start_poll_interval,
                             coordinator.start_ready_timeout)

    def test_slow_task_run_exhausts_the_budget_and_never_reports_started(self):
        # 返修 2：预算覆盖整个动词——底层操作累计耗时也要算进去。旧行为是
        # start 8s + 探测 8s = 16s 仍返回成功，超过协议 10s 交换时限。
        host = ScriptedReadinessHost([True], start_cost=8.0, verify_cost=8.0)
        coordinator, clock = self._coordinator(host, ready_timeout=6.0)
        host.clock = clock
        reply = self._start(coordinator)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertEqual(host.verify_calls, 0,
                         "预算已被任务运行耗尽时不得再探测")
        self.assertLess(clock.now, bp.DEFAULT_DEADLINE_SECONDS,
                        "总耗时必须留在协议交换时限之内")

    def test_readiness_confirmed_only_after_the_budget_is_never_started(self):
        # 返修 2：就绪只在预算之后才被确认时绝不报 started——晚到的成功
        # 会在调用方超时之后留下无主运行实例。
        host = ScriptedReadinessHost([False, True], verify_cost=4.0)
        coordinator, clock = self._coordinator(host, ready_timeout=6.0)
        host.clock = clock
        reply = self._start(coordinator)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertGreaterEqual(clock.now, 6.0, "探测累计耗时已越过预算")

    def test_sleep_is_clipped_to_the_remaining_budget(self):
        # 等待本身也不得越过总时限：剩余预算不足一个轮询间隔时只等剩余时间。
        host = ScriptedReadinessHost([False] * 10)
        coordinator, clock = self._coordinator(host, ready_timeout=1.0,
                                             interval=0.6)
        host.clock = clock
        reply = self._start(coordinator)
        self.assertFalse(reply["ok"], reply)
        self.assertLessEqual(clock.now, 1.0,
                             "sleep 必须被裁剪到剩余预算，不越过总时限")
        self.assertEqual(clock.sleeps, [0.6, 0.4])


class _Gate:
    """Restricted-gate stub for the console's layer-4 open function."""

    def __init__(self, verify_running=True, start_host=True):
        self.calls = []
        self._verify_running = verify_running
        self._start_host = start_host
        self.started = False

    def verify_running(self):
        self.calls.append("verify_running")
        # After a successful start the three-evidence check reports running.
        return True if self.started else self._verify_running

    def start_host(self):
        self.calls.append("start_host")
        if self._start_host:
            self.started = True
        return self._start_host

    def stop_host(self):
        self.calls.append("stop_host")
        return True

    def verify_closed(self):
        self.calls.append("verify_closed")
        return True


class _Ledger:
    def __init__(self):
        self.owned = []
        self.facts = []

    def own(self, layer, fact):
        self.owned.append(layer)
        self.facts.append(fact)

    def note(self, text):
        self.facts.append(text)


class AlreadyRunningManagedConfirmationTests(unittest.TestCase):
    """C2: the "layer already running" branch still confirms the managed open.

    Only a SUCCESSFUL managed open action refreshes the protected lifecycle
    from the earlier emergency-stop ``stopped`` to ``running``; a port/read-surface
    observation is not the authority. The restricted start is idempotent (the task
    is already running, so Run is a no-op) and this run still owns nothing.
    """

    def _open(self, gate):
        ledger = _Ledger()
        with mock.patch.object(orch, "_BROKER_OPS", gate):
            orch._open_backend(None, ledger, {"backend": 5}, 0.01)
        return ledger

    def test_already_running_reconfirms_the_managed_open(self):
        gate = _Gate(verify_running=True, start_host=True)
        ledger = self._open(gate)
        self.assertIn("verify_running", gate.calls)
        self.assertIn("start_host", gate.calls)
        self.assertNotIn("stop_host", gate.calls,
                         "开启前已在运行的层绝不能被回滚停止")
        self.assertEqual(ledger.owned, [], "本次不归属、不回滚停止该层")

    def test_refused_managed_confirmation_fails_honestly(self):
        gate = _Gate(verify_running=True, start_host=False)
        with self.assertRaises(orch.OperationError) as caught:
            self._open(gate)
        error = caught.exception
        self.assertEqual(error.code, "share_layer_failed")
        self.assertIn("已在运行但受限任务门拒绝受管确认", error.message)
        self.assertNotIn("stop_host", gate.calls)
        self.assertEqual(gate.calls.count("stop_host"), 0)

    def test_starting_branch_owns_and_confirms(self):
        gate = _Gate(verify_running=False, start_host=True)
        with mock.patch.object(orch, "_SLEEP", lambda seconds: None):
            ledger = self._open(gate)
        self.assertEqual(ledger.owned, ["backend"])
        self.assertEqual(gate.calls.count("start_host"), 1)


class LifecycleDurabilityTests(unittest.TestCase):
    """C2 返修：受保护 lifecycle 记录写盘失败时必须如实未确认。

    旧行为：内存写成 running/stopped、磁盘写失败被吞掉、回复仍是成功；
    重新加载后又静默回退到磁盘上的旧状态。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="public-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "broker"
        self.state_dir.mkdir()

    def _server(self, host=None):
        coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=host if host is not None else ScriptedReadinessHost([True]),
            instance_call=lambda *a, **k: {
                "ok": True, "result": {"instance_id": "x", "active_count": 0}},
            clock=StepClock(), sleep=lambda seconds: None)
        return share_broker_server.ManagementPipeServer(
            coordinator, r"\\.\pipe\um-share-public-broker",
            deadline_seconds=5.0, state_dir=self.state_dir)

    def _dispatch(self, server, action):
        frame = server._dispatch(bp.build_request("mgmt-k", action, {}), {})
        return json.loads(frame)

    def _unpersistable(self):
        # tmp 路径被目录占用 → 原子写必然失败（IsADirectoryError ⊂ OSError）
        (self.state_dir / "lifecycle.json.tmp").mkdir()

    def test_start_with_unpersistable_lifecycle_is_unconfirmed(self):
        (self.state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "stopped", "at": 1234,
                        "by": "emergency-stop"}), encoding="utf-8")
        self._unpersistable()
        server = self._server()
        self.assertEqual(server.lifecycle["state"], "stopped")
        reply = self._dispatch(server, "start")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertIn("lifecycle", reply["result"]["detail"])
        # 内存不分叉：仍是加载时的 stopped
        self.assertEqual(server.lifecycle["state"], "stopped")
        self.assertEqual(server.lifecycle["by"], "emergency-stop")
        reloaded = self._server()
        self.assertEqual(reloaded.lifecycle["state"], "stopped")

    def test_drain_with_unpersistable_lifecycle_is_unconfirmed(self):
        (self.state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "running", "at": 1, "by": "start"}),
            encoding="utf-8")
        self._unpersistable()
        server = self._server()
        reply = self._dispatch(server, "drain")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertEqual(server.lifecycle["state"], "running")
        self.assertEqual(server.lifecycle["by"], "start")
        reloaded = self._server()
        self.assertEqual(reloaded.lifecycle["state"], "running")

    def test_persistable_lifecycle_still_reports_success(self):
        # 正例：能写盘时行为不变（start → running 落盘并成功）。
        (self.state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "stopped", "at": 1234,
                        "by": "emergency-stop"}), encoding="utf-8")
        server = self._server()
        reply = self._dispatch(server, "start")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"started": True})
        self.assertEqual(server.lifecycle["state"], "running")
        persisted = json.loads(
            (self.state_dir / "lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["state"], "running")
        reloaded = self._server()
        self.assertEqual(reloaded.lifecycle["state"], "running")


class BudgetedHostOps:
    """Host fixture: every simulated operation spends its own duration, clipped
    by the budget it was given, and records the budget it saw.

    Only the OS action + the simulated durations live here - the budget passing
    (deadline, cadence, never a late success) stays in the product coordinator.
    """

    def __init__(self, clock, start_cost=1.0, verify_cost=10.0):
        self.clock = clock
        self.start_cost = start_cost
        self.verify_cost = verify_cost
        self.budgets = []

    def _spend(self, cost, budget_s):
        if budget_s is None:
            self.clock.now += cost
            return cost
        spent = min(cost, max(0.0, budget_s))
        self.clock.now += spent
        return spent

    def start_host(self, budget_s=None):
        self.budgets.append(("start_host", budget_s))
        self._spend(self.start_cost, budget_s)
        return True

    def stop_host(self, budget_s=None):
        self.budgets.append(("stop_host", budget_s))
        return True

    def verify_running(self, budget_s=None):
        self.budgets.append(("verify_running", budget_s))
        spent = self._spend(self.verify_cost, budget_s)
        # 预算不够跑完这次查询 → 证据不完整（None），绝不是「未运行」
        return True if spent >= self.verify_cost else None

    def verify_closed(self, budget_s=None):
        self.budgets.append(("verify_closed", budget_s))
        return True

    def status(self):
        return {"host": "fixture"}


class BudgetedReadinessTests(unittest.TestCase):
    """C1 返修：剩余预算必须传到底层操作与连续查询。

    维护者复现：启动 1s + 查询 10s = 11s 才返回，超过客户端 10s 交换时限。
    """

    def _start_with(self, host, ready_timeout=6.0, interval=0.25):
        clock = StepClock()
        host.clock = clock
        coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=host,
            instance_call=lambda *a, **k: {"ok": True, "result": {}},
            clock=clock, sleep=clock.sleep,
            start_ready_timeout=ready_timeout,
            start_poll_interval=interval)
        return coordinator.handle_management(
            bp.build_request("mgmt-k", "start", {})), clock

    def test_slow_task_and_query_never_outlive_the_budget(self):
        host = BudgetedHostOps(None, start_cost=1.0, verify_cost=10.0)
        reply, clock = self._start_with(host)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertLessEqual(clock.now, 6.0, "总耗时不得越过就绪预算")
        self.assertLess(clock.now, bp.DEFAULT_DEADLINE_SECONDS)
        # 剩余预算确实传到了底层：启动拿整预算、查询拿更小的剩余
        self.assertEqual(host.budgets[0], ("start_host", 6.0))
        name, budget = host.budgets[1]
        self.assertEqual(name, "verify_running")
        self.assertIsNotNone(budget)
        self.assertLess(budget, 6.0)

    def test_budget_shrinks_across_consecutive_probes(self):
        host = BudgetedHostOps(None, start_cost=0.5, verify_cost=1.0)
        reply, clock = self._start_with(host)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"started": True})
        self.assertEqual(host.budgets[0], ("start_host", 6.0))
        name, budget = host.budgets[1]
        self.assertEqual(name, "verify_running")
        self.assertLess(budget, 6.0, "第二次探测拿到的是剩余预算")


class _BudgetRunner:
    """Subprocess fixture: each command spends a fixed simulated duration
    (clipped by the timeout it was given) and records that timeout."""

    def __init__(self, clock, cost=1.0):
        self.clock = clock
        self.cost = cost
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        self.clock.now += min(self.cost, timeout)
        return _BudgetProc(args)


class _BudgetProc:
    """Minimal command output per argv (task state / CIM row / netstat row)."""

    def __init__(self, args):
        self.returncode = 0
        if args[0] == "schtasks":
            self.stdout = '"umshare-public-live","Running"' + "\n"
        elif args[0] == "powershell":
            self.stdout = json.dumps([{
                "ProcessId": 4242,
                "CommandLine": "um-share-python.exe share_gateway.py",
                "ExecutablePath": "C:\\share\\um-share-python.exe",
                "CreationDate": "2026-10-08T09:00:00",
                "OwnerDomain": "HOST", "OwnerUser": "share"}]) + "\n"
        else:
            self.stdout = ("  TCP    127.0.0.1:8100   0.0.0.0:0   "
                           "LISTENING    4242" + "\n")


class HostOpsBudgetTests(unittest.TestCase):
    """C1 返修：ops 层的每次命令与连续查询都必须吃剩余预算。"""

    def _ops(self, clock, cost=1.0):
        runner = _BudgetRunner(clock, cost=cost)
        ops = share_broker_ops.WindowsHostOps(
            task_name="umshare-public-live", runner=runner, clock=clock)
        return ops, runner

    def test_each_command_is_capped_by_the_remaining_budget(self):
        clock = StepClock()
        ops, runner = self._ops(clock)
        self.assertTrue(ops.start_host(budget_s=6.0))
        self.assertEqual(runner.calls[-1][1], 6.0)

    def test_consecutive_queries_share_one_budget_and_stop_when_spent(self):
        clock = StepClock()
        ops, runner = self._ops(clock, cost=2.0)
        self.assertIsNone(ops.verify_running(budget_s=4.0))
        self.assertLessEqual(clock.now, 4.0, "三次查询共享同一预算")
        for _args, timeout in runner.calls:
            self.assertLessEqual(timeout, 4.0)
        # 只跑了两次：第三次在预算耗尽后被如实拒绝（None），不给部分结论
        self.assertEqual(len(runner.calls), 2)

    def test_a_spent_budget_never_runs_another_command(self):
        clock = StepClock()
        ops, runner = self._ops(clock)
        self.assertFalse(ops.start_host(budget_s=0.0))
        self.assertFalse(ops.stop_host(budget_s=-1.0))
        self.assertEqual(runner.calls, [], "预算已耗尽时不得再跑命令")

    def test_unbounded_calls_keep_the_configured_command_bound(self):
        clock = StepClock()
        ops, runner = self._ops(clock)
        self.assertTrue(ops.start_host())
        self.assertEqual(runner.calls[-1][1], 10.0)


class _RecordingHost:
    """Host fixture: records every op call with the budget it saw."""

    def __init__(self):
        self.calls = []
        self.running = True

    def start_host(self, budget_s=None):
        self.calls.append(("start_host", budget_s))
        return True

    def stop_host(self, budget_s=None):
        self.calls.append(("stop_host", budget_s))
        self.running = False
        return True

    def verify_running(self, budget_s=None):
        self.calls.append(("verify_running", budget_s))
        return self.running

    def verify_closed(self, budget_s=None):
        self.calls.append(("verify_closed", budget_s))
        return not self.running

    def status(self):
        return {"host": "fixture"}


class _DrainRunner:
    """Subprocess fixture: records every command, spends a fixed simulated
    duration (clipped by the timeout it was given) and models the task state so
    ``schtasks /End`` really stops it."""

    def __init__(self, clock, cost=1.0):
        self.clock = clock
        self.cost = cost
        self.running = True
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        self.clock.now += min(self.cost, timeout)
        if args[0] == "schtasks" and "/End" in args:
            self.running = False
        return _DrainProc(args, self.running)


class _DrainProc:
    """Minimal command output per argv (task state / CIM row / netstat row)."""

    def __init__(self, args, running=True):
        self.returncode = 0
        if args[0] == "schtasks":
            self.stdout = ('"umshare-public-live","%s"'
                           % ("Running" if running else "Ready")) + "\n"
        elif args[0] == "powershell":
            self.stdout = json.dumps([] if not running else [{
                "ProcessId": 4242,
                "CommandLine": "um-share-python.exe share_gateway.py",
                "ExecutablePath": "C:\\share\\um-share-python.exe",
                "CreationDate": "2026-10-08T09:00:00",
                "OwnerDomain": "HOST", "OwnerUser": "share"}]) + "\n"
        else:
            self.stdout = ("" if not running else
                           "  TCP    127.0.0.1:8100   0.0.0.0:0   "
                           "LISTENING    4242") + "\n"


class DrainStopBudgetTests(unittest.TestCase):
    """C1 返修：排空等待耗尽预算时，最终停止与关闭核验仍必须执行。

    维护者复现：等待超时后预算为零，实际停止命令被跳过
    （``timed_out=true、stopped=false``，停止命令调用 0 次）。
    """

    def _drain(self, host, drain_timeout=0.5, stop_timeout=3.0):
        clock = StepClock()
        coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=host,
            instance_call=lambda *a, **k: {
                "ok": True, "result": {"active_count": 2}},
            clock=clock, sleep=lambda seconds: None,
            drain_timeout=drain_timeout, stop_timeout=stop_timeout)
        with mock.patch.object(share_broker_host.time, "sleep",
                               lambda seconds: setattr(
                                   clock, "now", clock.now + seconds)):
            reply = coordinator.handle_management(
                bp.build_request("mgmt-k", "drain", {}))
        return reply, clock

    def test_a_timed_out_drain_still_stops_and_verifies_closure(self):
        host = _RecordingHost()
        reply, clock = self._drain(host)
        result = reply["result"]
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(result["timed_out"], result)
        self.assertTrue(result["stopped"], result)
        names = [name for name, _budget in host.calls]
        self.assertIn("stop_host", names, "排空超时后必须仍然发出停止")
        self.assertIn("verify_closed", names, "排空超时后必须仍然核验关闭")
        # 停止拿的是独立预留预算，不是排空后剩下的 0
        budgets = dict(host.calls)
        self.assertEqual(budgets["stop_host"], 3.0)
        self.assertGreater(budgets["verify_closed"], 0.0)

    def test_the_real_adapter_still_runs_the_stop_after_a_drain_timeout(self):
        # 真实适配器（WindowsHostOps + 假 runner/时钟）：排空超时后 runner 必须
        # 真的看到 schtasks /End，而且每次命令都在预留预算之内。
        clock = StepClock()
        runner = _DrainRunner(clock, cost=1.0)
        ops = share_broker_ops.WindowsHostOps(
            task_name="umshare-public-live", runner=runner, clock=clock)
        host = SimpleNamespace(
            start_host=ops.start_host, stop_host=ops.stop_host,
            verify_running=ops.verify_running, verify_closed=ops.verify_closed,
            status=ops.status)
        reply, _clock = self._drain(host)
        result = reply["result"]
        self.assertTrue(result["timed_out"], result)
        self.assertTrue(result["stopped"], result)
        commands = [args for args, _timeout in runner.calls]
        self.assertTrue(any(args[:2] == ["schtasks", "/End"] for args in commands),
                        "排空超时后真实适配器必须发出 schtasks /End")
        for _args, timeout in runner.calls:
            self.assertLessEqual(timeout, 3.0, "每次命令都在预留预算之内")


class _RecordingTransport:
    """Transport fixture: records the signed request and the exchange
    deadline; answers a canned response carrying the SAME request id."""

    def __init__(self, response):
        self.response = response
        self.request = None
        self.deadline = None
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True

    def send_all(self, data, deadline=None):
        self.deadline = deadline
        self.request = bp.decode_frame(data)

    def recv_line(self, deadline):
        return bp.encode_frame(dict(
            self.response, request_id=self.request["request_id"]))


class CloseBudgetAlignmentTests(unittest.TestCase):
    """C1 返修：客户端等待必须覆盖服务端允许的排空窗口 + 预留停止预算。

    维护者复现：客户端只等 10 秒，服务端却允许排空 60 秒 + 停止核验 20 秒；
    客户端提前放弃会放弃一个服务端仍在执行的排空，重试又会与在途停止竞争。
    """

    def _client(self, response):
        transport = _RecordingTransport(response)
        return BrokerClient(lambda: transport, "secret"), transport

    def test_the_client_deadlines_cover_the_broker_close_budgets(self):
        # 两边读同一份协议常量：客户端交换时限 >= 排空窗口 + 预留停止预算
        self.assertGreaterEqual(
            bp.DRAIN_DEADLINE_SECONDS,
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS)
        self.assertGreaterEqual(bp.EMERGENCY_DEADLINE_SECONDS,
                                bp.STOP_BUDGET_SECONDS)
        # 冻结的 1..180 界：两个时限都在界内
        self.assertLessEqual(bp.DRAIN_DEADLINE_SECONDS, 180.0)
        self.assertLessEqual(bp.EMERGENCY_DEADLINE_SECONDS, 180.0)
        # 服务端允许的整个关闭工作必须落在包装器的 130s 派发时限内
        self.assertLessEqual(
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS, 130.0)

    def test_the_drain_and_stop_verbs_pass_the_aligned_deadlines(self):
        client, transport = self._client({
            "ok": True, "error_code": "ok",
            "result": {"drained": True, "stopped": True}})
        before = time.monotonic()
        self.assertTrue(client.drain().ok)
        drain_window = transport.deadline - before
        self.assertGreaterEqual(
            drain_window,
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS,
            "排空交换时限必须覆盖服务端的整个关闭工作")
        self.assertLessEqual(drain_window, 180.0)
        client2, transport2 = self._client({
            "ok": True, "error_code": "ok", "result": {"stopped": True}})
        before = time.monotonic()
        self.assertTrue(client2.emergency_stop().ok)
        emergency_window = transport2.deadline - before
        self.assertGreaterEqual(emergency_window, bp.STOP_BUDGET_SECONDS,
                                "紧急停止时限必须覆盖预留停止预算")
        self.assertLessEqual(emergency_window, 180.0)

    def test_the_coordinator_defaults_are_the_shared_protocol_budgets(self):
        # 直接构造协调端时，默认值也来自同一份协议常量（不是各写一份数字）
        coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=SimpleNamespace(), instance_call=lambda *a, **k: {})
        self.assertEqual(coordinator.drain_timeout, bp.DRAIN_WINDOW_SECONDS)
        self.assertEqual(coordinator.stop_timeout, bp.STOP_BUDGET_SECONDS)


class _BudgetInstance:
    """Instance-channel fixture: every exchange spends a simulated duration
    (clipped by the budget it was given) and records the budget it saw."""

    def __init__(self, clock, cost=9.0, active=1):
        self.clock = clock
        self.cost = cost
        self.active = active
        self.calls = []

    def __call__(self, action, fields, instance_id, budget_s=None):
        self.calls.append((action, budget_s))
        if budget_s is None:
            spent = self.cost
        else:
            spent = min(self.cost, max(0.0, budget_s))
        self.clock.now += spent
        if budget_s is not None and spent < self.cost:
            # 预算不够跑完这次交换 → 如实失败（deadline_exceeded）
            return {"ok": False, "error_code": "deadline_exceeded",
                    "result": {"detail": "instance exchange budget exhausted"}}
        if action == "instance-status":
            return {"ok": True, "result": {"instance_id": instance_id,
                                          "active_count": self.active}}
        return {"ok": True, "result": {"instance_id": instance_id,
                                      "state": "stopped",
                                      "cleared_in_flight": 0}}


class CloseChainBudgetTests(unittest.TestCase):
    """R-WIRE-DRAIN：完整关闭链必须在客户端时限内结束。

    维护者隔离反例（两树一致）：实例交换与命令各合法地耗时 9s 时，
    旧实现排空 101.35s > 客户端 90s、紧急停止 45s > 客户端 30s。
    这里用真实协调端 + 真实 WindowsHostOps（只替最末端的实例通信、命令
    runner、时钟/睡眠与内存 writer），断言**实际累计耗时**与最终状态。
    """

    INSTANCE_COST = 9.0
    COMMAND_COST = 9.0

    def _close(self, action="drain", instance_cost=INSTANCE_COST,
               command_cost=COMMAND_COST, active=1, **kwargs):
        clock = StepClock()
        runner = _DrainRunner(clock, cost=command_cost)
        ops = share_broker_ops.WindowsHostOps(
            task_name="umshare-public-live", runner=runner, clock=clock)
        instance = _BudgetInstance(clock, cost=instance_cost, active=active)
        coordinator = share_broker_host.BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-kwargs".replace(
                "kwargs", "k"),
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=ops, instance_call=instance, clock=clock,
            sleep=lambda seconds: setattr(clock, "now",
                                         clock.now + seconds), **kwargs)
        with mock.patch.object(share_broker_host.time, "sleep",
                               lambda seconds: setattr(
                                   clock, "now", clock.now + seconds)):
            reply = coordinator.handle_management(
                bp.build_request("mgmt-k", action, {}))
        return reply, clock, instance, runner

    def test_the_nine_second_drain_stays_inside_the_client_deadline(self):
        reply, clock, instance, runner = self._close("drain")
        result = reply["result"]
        self.assertLessEqual(clock.now, bp.DRAIN_DEADLINE_SECONDS,
                             "排空整链必须在客户端时限内结束")
        self.assertTrue(result["timed_out"], result)
        # 停止仍被尝试执行（不因排空到期而跳过）
        commands = [args for args, _timeout in runner.calls]
        self.assertTrue(any(args[:2] == ["schtasks", "/End"] for args in commands),
                        "排空超时后必须仍发出 schtasks /End")
        # 每次实例交换都拿到了预算；等待阶段的最后一次查询不能越过排空截止点
        for _action, budget in instance.calls:
            self.assertIsNotNone(budget, "实例交换必须收到剩余预算")
        status_budgets = [budget for action, budget in instance.calls
                          if action == "instance-status"]
        self.assertTrue(status_budgets, instance.calls)
        self.assertLessEqual(max(status_budgets), 60.0,
                             "最后一次排空查询不得吃满一个完整交换预算")
        # 命令与实例交换都在客户端时限内
        for _args, timeout in runner.calls:
            self.assertLessEqual(timeout, bp.DRAIN_DEADLINE_SECONDS)

    def test_the_nine_second_emergency_stop_stays_inside_the_client_deadline(self):
        reply, clock, instance, runner = self._close("emergency-stop")
        result = reply["result"]
        self.assertLessEqual(clock.now, bp.EMERGENCY_DEADLINE_SECONDS,
                             "紧急停止整链必须在客户端时限内结束")
        commands = [args for args, _timeout in runner.calls]
        self.assertTrue(any(args[:2] == ["schtasks", "/End"] for args in commands),
                        "紧急停止必须发出 schtasks /End")
        for _action, budget in instance.calls:
            self.assertIsNotNone(budget, "实例交换必须收到剩余预算")
        # 紧急停止：通知 + 停止 + 关闭核验共享同一份预留预算
        self.assertLessEqual(clock.now, 20.0,
                             "紧急停止的停止阶段不得超过预留预算")

    def test_a_timely_close_completes_within_the_deadline(self):
        reply, clock, _instance, runner = self._close(
            "drain", instance_cost=0.1, command_cost=0.1, active=0)
        result = reply["result"]
        self.assertTrue(result["drained"], result)
        self.assertTrue(result["stopped"], result)
        self.assertLessEqual(clock.now, bp.DRAIN_DEADLINE_SECONDS)
        self.assertTrue(any(args[:2] == ["schtasks", "/End"]
                            for args, _timeout in runner.calls))

    def test_an_exhausted_budget_is_reported_honestly(self):
        # 实例交换合法地耗掉整份预算：停止仍被尝试，但关闭如实未确认。
        reply, clock, _instance, runner = self._close(
            "drain", instance_cost=60.0, command_cost=9.0)
        result = reply["result"]
        self.assertLessEqual(clock.now, bp.DRAIN_DEADLINE_SECONDS,
                             "整链不得越过客户端时限")
        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["stopped"],
                           "预算耗尽时不得伪造关闭确认：%r" % (result,))



if __name__ == "__main__":
    unittest.main()
