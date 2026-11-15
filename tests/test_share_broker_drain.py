# -*- coding: utf-8 -*-
"""D18-10: the broker drain result carries the observed in-flight count.

``BrokerHostCoordinator._drain`` already distinguishes the bounded in-flight
wait (``drained``/``timed_out``), the authoritative final stop
(``stopped``) and a lost notification. The console's whole-chain stop needs the
OBSERVED in-flight count as its own field so a drain timeout can show how many
requests were left — never a fabricated 0. The field is ``null`` when no
instance-status observation resolved a count.

Every instance/host boundary is injected; no real pipe, task or process is used.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share.broker_host as share_broker_host  # noqa: E402


class _StepClock:
    """Deterministic monotonic fixture clock.

    Every read advances the clock by a small step so the drain loop terminates
    deterministically even when the in-flight count never reaches zero; every
    sleep advances it by the slept amount too.
    """

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        self.now += 0.01
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _Host:
    """Host ops fixture: the stop/verify calls are recorded."""

    def __init__(self, closed=True):
        self.stop_calls = []
        self.closed = closed

    def stop_host(self, budget_s=None):
        self.stop_calls.append(budget_s)
        return True

    def verify_closed(self, budget_s=None):
        return self.closed

    def verify_running(self, budget_s=None):
        return True


class _Instance:
    """Instance-channel fixture: drain + a scripted instance-status sequence."""

    def __init__(self, active_seq, status_raises=False):
        self.active_seq = list(active_seq)
        self.status_raises = status_raises
        self.calls = []

    def __call__(self, action, fields, instance_id, budget_s=None):
        self.calls.append(action)
        if action == "drain":
            return {"ok": True, "result": {}}
        if action == "emergency":
            return {"ok": True, "result": {"state": "stopped"}}
        if action == "instance-status":
            if self.status_raises:
                raise OSError("instance channel lost")
            if len(self.active_seq) > 1:
                value = self.active_seq.pop(0)
            else:
                value = self.active_seq[0]
            return {"ok": True, "result": {"active_count": value}}
        raise AssertionError("unexpected action %r" % (action,))


class BrokerDrainInFlightTests(unittest.TestCase):
    def _coordinator(self, instance, host=None, drain_timeout=5.0):
        clock = _StepClock()
        host = host or _Host()
        coordinator = share_broker_host.BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=share_broker_host.InstanceRegistry(),
            replay=share_broker_host.ReplayRegistry(),
            host_ops=host,
            instance_call=instance,
            clock=clock, sleep=clock.sleep,
            drain_timeout=drain_timeout, stop_timeout=1.0)
        return coordinator, host, clock

    def test_01_drained_reports_the_observed_zero_in_flight(self):
        instance = _Instance([0])
        coordinator, host, _clock = self._coordinator(instance)
        out = coordinator._drain()
        self.assertEqual(out.result["drained"], True)
        self.assertEqual(out.result["timed_out"], False)
        self.assertEqual(out.result["stopped"], True)
        self.assertEqual(out.result["notification_failed"], False)
        self.assertEqual(out.result["in_flight"], 0)
        self.assertEqual(instance.calls,
                         ["drain", "instance-status", "emergency"])
        self.assertEqual(len(host.stop_calls), 1)

    def test_02_drain_timeout_reports_the_observed_active_count(self):
        instance = _Instance([3])
        coordinator, host, clock = self._coordinator(
            instance, drain_timeout=0.2)
        out = coordinator._drain()
        self.assertEqual(out.result["drained"], False)
        self.assertEqual(out.result["timed_out"], True)
        self.assertEqual(out.result["stopped"], True)
        # The in-flight count is the OBSERVED value, not a fabricated 0.
        self.assertEqual(out.result["in_flight"], 3)
        self.assertGreaterEqual(instance.calls.count("instance-status"), 2)
        self.assertEqual(len(host.stop_calls), 1)

    def test_03_unobserved_in_flight_stays_null(self):
        instance = _Instance([0], status_raises=True)
        coordinator, host, _clock = self._coordinator(instance)
        out = coordinator._drain()
        # No status observation resolved a count: null, never 0. The
        # fail-closed timed_out reading and the authoritative stop stay.
        self.assertIsNone(out.result["in_flight"])
        self.assertEqual(out.result["timed_out"], True)
        self.assertEqual(out.result["drained"], False)
        self.assertEqual(out.result["notification_failed"], True)
        self.assertEqual(out.result["stopped"], True)
        self.assertEqual(len(host.stop_calls), 1)


class _StrictInstance:
    """Instance fixture that rejects an empty instance_id exactly like the REAL
    AF_UNIX seam does.

    ``make_unix_instance_call`` raises ``ValueError("instance_id must be a
    non-empty string")`` before any I/O, so a coordinator that passes
    ``self.instances.current()`` without resolving it first records a
    notification failure against a perfectly healthy instance. The plain
    ``_Instance`` fixture above never validates the id, which is why this
    regression was invisible.
    """

    def __init__(self, active_count=0):
        self.active_count = active_count
        self.calls = []

    def __call__(self, action, fields, instance_id, budget_s=None):
        self.calls.append((action, instance_id))
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id must be a non-empty string")
        if action == "drain":
            return {"ok": True, "result": {}}
        if action == "instance-status":
            return {"ok": True,
                    "result": {"active_count": self.active_count}}
        if action == "emergency":
            return {"ok": True, "result": {"state": "stopped"}}
        raise AssertionError("unexpected action %r" % (action,))


class BrokerDrainDiscoveryTests(unittest.TestCase):
    """R19-broker-discovery: a drain must resolve the instance BEFORE notifying.

    The broker may start before the share writes its registration (the live
    cloud did exactly that after a restart). ``_resolve_instance`` already
    performs ONE trusted discovery for that case, but ``_drain`` and
    ``_emergency_stop`` read ``self.instances.current()`` directly, so an
    empty registry turned every drain into ``notification_failed`` and the
    update.sh drain gate refused the version switch.
    """

    def _coordinator(self, instance, register=True, drain_timeout=5.0):
        clock = _StepClock()
        host = _Host()
        coordinator = share_broker_host.BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=share_broker_host.InstanceRegistry(),
            replay=share_broker_host.ReplayRegistry(),
            host_ops=host,
            instance_call=instance,
            clock=clock, sleep=clock.sleep,
            drain_timeout=drain_timeout, stop_timeout=1.0)
        if register:
            # The real assembly wires discover_instance to read the
            # instance-registration.json the share self-published.
            def discover():
                coordinator.instances.register("instance-from-registration")
            coordinator.discover_instance = discover
        return coordinator, host, clock

    def test_10_drain_discovers_an_empty_registry_before_notifying(self):
        instance = _StrictInstance(active_count=0)
        coordinator, host, _clock = self._coordinator(instance)
        # The registry starts EMPTY: the broker came up before the share
        # published its registration.
        self.assertIsNone(coordinator.instances.current())
        out = coordinator._drain()
        self.assertEqual(out.result["notification_failed"], False)
        self.assertEqual(out.result["drained"], True)
        self.assertEqual(out.result["in_flight"], 0)
        self.assertEqual(out.result["stopped"], True)
        # Every instance call carries the discovered id, never None.
        self.assertTrue(instance.calls)
        for action, instance_id in instance.calls:
            self.assertEqual(instance_id, "instance-from-registration",
                             "%s was addressed without a resolved "
                             "instance" % action)
        self.assertEqual(len(host.stop_calls), 1)

    def test_11_emergency_stop_discovers_an_empty_registry(self):
        instance = _StrictInstance(active_count=0)
        coordinator, host, _clock = self._coordinator(instance)
        self.assertIsNone(coordinator.instances.current())
        out = coordinator._emergency_stop()
        self.assertEqual(out.result["notification_failed"], False)
        self.assertEqual(out.result["stopped"], True)
        for action, instance_id in instance.calls:
            self.assertEqual(instance_id, "instance-from-registration",
                             "%s was addressed without a resolved "
                             "instance" % action)

    def test_12_no_discovery_seam_keeps_the_notification_failure(self):
        # Without the discovery seam there is nothing trusted to address: the
        # notification failure must stay VISIBLE instead of being hidden.
        instance = _StrictInstance(active_count=0)
        coordinator, host, _clock = self._coordinator(
            instance, register=False)
        out = coordinator._drain()
        self.assertEqual(out.result["notification_failed"], True)
        self.assertEqual(out.result["stopped"], True)


if __name__ == "__main__":
    unittest.main()
