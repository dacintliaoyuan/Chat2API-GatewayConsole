"""Control status must reply before its wire deadline under slow OS reads."""
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from share.broker_host import BrokerHostCoordinator, InstanceRegistry, ReplayRegistry
from share.broker_ops import WindowsHostOps, LIMITS_CHECK_TIMEOUT_SECONDS
from share.broker_protocol import DEFAULT_DEADLINE_SECONDS, build_request


class StatusBudgetTests(unittest.TestCase):
    def make_ops(self, query_seconds, limits_seconds):
        clock = SimpleNamespace(now=0.0)
        calls = []

        def runner(args, timeout):
            calls.append((args[0], timeout))
            elapsed = query_seconds if args[0] == "schtasks" else limits_seconds
            clock.now += min(elapsed, timeout)
            if elapsed >= timeout:
                raise subprocess.TimeoutExpired(args[0], timeout)
            return SimpleNamespace(returncode=0, stdout=(
                '"umshare-live","next","Ready"' if args[0] == "schtasks"
                else "LIMITS=OK\n"))

        ops = WindowsHostOps(
            "umshare-live", runner=runner, clock=lambda: clock.now,
            watchdog_script=r"C:\isolated\watchdog.ps1",
            run_root=r"C:\isolated")
        return ops, clock, calls

    def test_real_management_status_shares_budget_and_keeps_security_unknown(self):
        ops, clock, calls = self.make_ops(3.0, 8.0)
        coordinator = BrokerHostCoordinator(
            writer=None, mgmt_secret="isolated-key", instance_secret="isolated-key",
            instances=InstanceRegistry(), replay=ReplayRegistry(), host_ops=ops,
            instance_call=lambda *args: self.fail("status must not call instance"))
        response = coordinator.handle_management(build_request("isolated-key", "status"))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["state"], "Ready")
        self.assertLessEqual(clock.now, DEFAULT_DEADLINE_SECONDS - 2.0)
        self.assertIsNone(response["result"]["host_limits"])
        self.assertEqual([program for program, _ in calls], ["schtasks"],
                         "routine status must not monopolize the one control pipe with a WFP audit")

    def test_spent_task_budget_skips_expensive_security_query(self):
        ops, clock, calls = self.make_ops(20.0, 20.0)
        snapshot = ops.status()
        self.assertIsNone(snapshot["state"])
        self.assertIsNone(snapshot["host_limits"])
        self.assertLessEqual(clock.now, DEFAULT_DEADLINE_SECONDS - 2.0)
        self.assertEqual([program for program, _ in calls], ["schtasks"])

    def test_explicit_security_gate_retains_full_check(self):
        ops, clock, calls = self.make_ops(0.1, 8.0)
        self.assertIs(ops.verify_limits(), True)
        self.assertEqual(calls, [("pwsh", LIMITS_CHECK_TIMEOUT_SECONDS)])
        self.assertEqual(clock.now, 8.0)

    def test_repeated_health_reads_do_not_repeat_the_security_audit(self):
        ops, _, calls = self.make_ops(0.1, 8.0)
        for _ in range(2):
            self.assertIsNone(ops.status()["host_limits"])
        self.assertEqual([program for program, _ in calls], ["schtasks", "schtasks"])


if __name__ == "__main__":
    unittest.main()
