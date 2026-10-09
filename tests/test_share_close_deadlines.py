"""Exercise closing deadlines through real wire and host adapters."""
import ctypes
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_broker_client as bc  # noqa: E402
import share_broker_host as bh  # noqa: E402
import share_broker_ops as ops  # noqa: E402
import share_broker_protocol as bp  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WireKernel:
    def __init__(self, clock, busy=0, open_cost=0.0):
        self.clock = clock
        self.busy = busy
        self.open_cost = open_cost
        self.opens = 0
        self.handles = []
        self.closed = []
        self.requests = []
        self.request = None

    def CreateFileW(self, *args):
        self.opens += 1
        self.clock.sleep(self.open_cost)
        if self.opens <= self.busy:
            return (1 << 64) - 1
        handle = 0x1000 + self.opens
        self.handles.append(handle)
        return handle

    def GetNamedPipeServerProcessId(self, handle, pid):
        pid.contents.value = 4242
        return 1

    def GetLastError(self):
        return 231

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def WriteFile(self, handle, data, count, written, unused):
        self.request = json.loads(bytes(data[:count]).decode("utf-8"))
        self.requests.append(self.request)
        written.contents.value = count
        return 1

    def ReadFile(self, handle, buffer, count, read, unused):
        response = bp.BrokerResult.ok({"state": "stopped"}).to_dict()
        response["request_id"] = self.request["request_id"]
        payload = (json.dumps(response) + "\n").encode("utf-8")
        buffer[:len(payload)] = payload
        read.contents.value = len(payload)
        return 1


class IdentityFacade:
    """Only Win32 facts are synthetic; WindowsInstanceVerifier is real."""

    def __init__(self, clock, cost=0.0):
        self.clock = clock
        self.cost = cost
        self.buffers = []

    def __getitem__(self, key):
        def open_process(*args):
            self.clock.sleep(self.cost)
            return 0x4000

        def image(handle, flags, buffer, size):
            buffer.value = "C:/fixture/gateway.exe"
            size[0] = len(buffer.value)
            return 1

        def times(handle, creation, *args):
            creation.contents.dwLowDateTime = 100
            creation.contents.dwHighDateTime = 0
            return 1

        def token(handle, access, output):
            output[0] = 0x3000
            return 1

        def token_info(handle, kind, buffer, length, needed):
            needed[0] = 64
            if buffer is None:
                return 0
            ctypes.memmove(buffer, bytes(8), 8)
            return 1

        def sid(value, output):
            buffer = ctypes.create_unicode_buffer("S-1-5-21-1")
            self.buffers.append(buffer)
            output[0] = ctypes.cast(buffer, ctypes.c_wchar_p)
            return 1

        if key == "kernel32":
            return SimpleNamespace(
                OpenProcess=open_process, QueryFullProcessImageNameW=image,
                GetProcessTimes=times, CloseHandle=lambda handle: 1,
                LocalFree=lambda pointer: None)
        if key == "advapi32":
            return SimpleNamespace(
                OpenProcessToken=token, GetTokenInformation=token_info,
                ConvertSidToStringSidW=sid)
        raise KeyError(key)


class InstanceDeadlineTests(unittest.TestCase):
    def exchange(self, budget, busy=0, open_cost=0.0, identity_cost=0.0,
                 kernel_class=WireKernel):
        clock = Clock()
        kernel = kernel_class(clock, busy, open_cost)
        verifier = ops.WindowsInstanceVerifier(
            facade=IdentityFacade(clock, identity_cost),
            expected_image="C:/fixture/gateway.exe",
            expected_owner_sid="S-1-5-21-1")
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "synthetic-instance-key"
            key.write_text("test-only-instance-secret", encoding="utf-8")
            call = ops.make_instance_call(
                key, kernel_factory=lambda: kernel, verifier=verifier)
            with mock.patch.object(ops.time, "monotonic", clock), \
                    mock.patch.object(ops.time, "sleep", clock.sleep):
                result = call("emergency", {}, "fixture-instance",
                              budget_s=budget)
        self.assertEqual(kernel.handles, kernel.closed)
        return result, clock, kernel

    def test_open_retry_cannot_outlive_remaining_budget_and_send(self):
        result, clock, kernel = self.exchange(0.1, busy=6)
        self.assertEqual(result["error_code"], "deadline_exceeded", result)
        self.assertFalse(result["ok"])
        self.assertEqual(kernel.requests, [])
        self.assertLessEqual(clock.now, 0.1 + 1e-8)

    def test_preparation_does_not_reset_deadline_before_sending(self):
        result, clock, kernel = self.exchange(
            5.0, open_cost=4.0, identity_cost=4.0)
        self.assertEqual(result["error_code"], "deadline_exceeded", result)
        self.assertFalse(result["ok"])
        self.assertEqual(kernel.requests, [])

    def test_zero_budget_opens_no_pipe_and_sends_no_request(self):
        result, clock, kernel = self.exchange(0.0)
        self.assertEqual(result["error_code"], "deadline_exceeded")
        self.assertEqual(kernel.opens, 0)
        self.assertEqual(kernel.requests, [])
        self.assertEqual(clock.now, 0.0)

    def test_timely_retry_still_verifies_and_completes(self):
        result, clock, kernel = self.exchange(1.0, busy=2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(kernel.requests), 1)
        self.assertEqual(kernel.opens, 3)
        self.assertLess(clock.now, 1.0)

    def test_invalid_budget_never_becomes_an_unbounded_exchange(self):
        for budget in (float("nan"), float("inf"), False, "invalid"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.exchange(budget)

    def test_fresh_connection_retry_shares_preparation_and_wire_budget(self):
        class ServingGap(WireKernel):
            writes = 0

            def WriteFile(self, *args):
                self.writes += 1
                if self.writes == 1:
                    self.clock.sleep(0.7)
                    return 0
                return super().WriteFile(*args)

            def GetLastError(self):
                return 109

        for budget, expected in ((1.0, False), (2.0, True)):
            with self.subTest(budget=budget):
                result, clock, kernel = self.exchange(
                    budget, open_cost=0.11, identity_cost=0.11,
                    kernel_class=ServingGap)
                self.assertEqual(result["ok"], expected, result)
                self.assertEqual(kernel.opens, 2)
                self.assertEqual(len(kernel.requests), int(expected))
                if not expected:
                    self.assertEqual(result["error_code"], "deadline_exceeded")

    def test_blocked_native_identity_query_cannot_send_late(self):
        clock = Clock()
        kernel = WireKernel(clock)
        facade = IdentityFacade(clock)
        native = facade["kernel32"]
        original = native.OpenProcess
        released = threading.Event()
        entered = threading.Event()

        def blocked(*args):
            entered.set()
            released.wait(1.0)
            return original(*args)

        native.OpenProcess = blocked
        verifier = ops.WindowsInstanceVerifier(
            facade={"kernel32": native, "advapi32": facade["advapi32"]},
            expected_image="C:/fixture/gateway.exe",
            expected_owner_sid="S-1-5-21-1")
        observed = []
        timer = threading.Timer(0.08, released.set)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "synthetic-instance-key"
            key.write_text("test-only-instance-secret", encoding="utf-8")
            call = ops.make_instance_call(
                key, kernel_factory=lambda: kernel, verifier=verifier,
                on_verified=lambda *args: observed.append(args))
            timer.start()
            try:
                started = time.monotonic()
                result = call("emergency", {}, "fixture-instance", budget_s=0.03)
                elapsed = time.monotonic() - started
            finally:
                released.set()
                timer.join()
        self.assertTrue(entered.is_set())
        self.assertEqual(result["error_code"], "deadline_exceeded", result)
        self.assertLess(elapsed, 0.3)
        self.assertEqual(kernel.requests, [])
        self.assertEqual(observed, [])
        self.assertEqual(kernel.handles, kernel.closed)


class StopReservationTests(unittest.TestCase):
    def test_subcommand_timeout_does_not_inflate_a_small_budget(self):
        timeouts = []
        host = ops.WindowsHostOps(
            "fixture-task", clock=Clock(),
            runner=lambda args, timeout: (
                timeouts.append(timeout) or SimpleNamespace(returncode=0)))
        self.assertTrue(host.stop_host(budget_s=0.001))
        self.assertEqual(timeouts, [0.001])

    def close(self, action):
        clock = Clock()
        commands = []
        budgets = []

        def runner(args, timeout):
            commands.append(list(args))
            clock.sleep(min(9.0, timeout))
            if timeout < 9.0:
                raise subprocess.TimeoutExpired(args, timeout)
            stdout = ('"fixture-task","Ready"\n' if args[0] == "schtasks"
                      else "[]\n" if args[0] == "powershell" else "")
            return SimpleNamespace(returncode=0, stdout=stdout)

        def instance(action, fields, instance_id, budget_s=None):
            self.assertIsNotNone(budget_s)
            budgets.append((action, budget_s))
            clock.sleep(budget_s)
            return {"ok": False, "error_code": "deadline_exceeded",
                    "result": {}}

        host = ops.WindowsHostOps(
            "fixture-task", runner=runner, clock=clock)
        instances = bh.InstanceRegistry()
        instances.register("fixture-instance")
        coordinator = bh.BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *args: None),
            mgmt_secret="fixture-management", instance_secret="fixture-key",
            instances=instances, replay=bp.ReplayRegistry(),
            host_ops=host, instance_call=instance, clock=clock,
            sleep=clock.sleep)
        with mock.patch.object(bh.time, "sleep", clock.sleep):
            reply = coordinator.handle_management(
                bp.build_request("fixture-management", action, {}))
        return reply, clock, commands, budgets

    def test_drain_notification_exhaustion_still_sends_host_stop(self):
        reply, clock, commands, budgets = self.close("drain")
        self.assertEqual(
            sum(args[:2] == ["schtasks", "/End"] for args in commands), 1)
        self.assertFalse(reply["result"]["stopped"])
        self.assertLessEqual(clock.now, bp.DRAIN_DEADLINE_SECONDS)

    def test_emergency_notification_exhaustion_still_sends_host_stop(self):
        reply, clock, commands, budgets = self.close("emergency-stop")
        self.assertEqual(
            sum(args[:2] == ["schtasks", "/End"] for args in commands), 1)
        self.assertFalse(reply["result"]["stopped"])
        self.assertLessEqual(clock.now, bp.STOP_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
