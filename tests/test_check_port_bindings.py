#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for check_port_bindings: fixture-based parser and classifier tests.

The live netstat query is never executed here; every case feeds a fixture
through parse_listeners/classify so the suite stays deterministic offline.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_port_bindings as audit  # noqa: E402

FIXTURE = """\

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       25044
  TCP    0.0.0.0:8001           0.0.0.0:0              LISTENING       25044
  TCP    127.0.0.1:8315         0.0.0.0:0              LISTENING       999
  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4
  TCP    [::]:445               [::]:0                 LISTENING       4
  UDP    0.0.0.0:5353           *:*                    4
  UDP    127.0.0.1:8320         *:*                    777
  TCP    [::1]:5173             [::]:0                 LISTENING       68116
  非法行没有协议字段
  TCP    127.0.0.1:badport      0.0.0.0:0              LISTENING       1
"""


class ParseListenersTest(unittest.TestCase):
    def test_parses_tcp_listening_and_udp_rows_only(self):
        listeners = audit.parse_listeners(FIXTURE)
        keys = {(item.proto, item.host, item.port) for item in listeners}
        self.assertIn(("TCP", "127.0.0.1", 8001), keys)
        self.assertIn(("TCP", "0.0.0.0", 8001), keys)
        self.assertIn(("UDP", "127.0.0.1", 8320), keys)
        self.assertIn(("TCP", "[::1]", 5173), keys)
        self.assertIn(("TCP", "0.0.0.0", 445), keys)
        # Non-listening rows are irrelevant here but the fixture only has
        # LISTENING rows; malformed rows must be dropped, not guessed.
        self.assertNotIn("非法", str(listeners))

    def test_malformed_and_foreign_rows_are_skipped(self):
        listeners = audit.parse_listeners(FIXTURE)
        hosts = {item.host for item in listeners}
        self.assertNotIn("非法", hosts)
        self.assertNotIn("", hosts)


class ClassifyTest(unittest.TestCase):
    def test_registered_loopback_is_ok(self):
        listeners = audit.parse_listeners(
            "  TCP    127.0.0.1:8001    0.0.0.0:0    LISTENING    25044\n")
        violations, info = audit.classify(listeners)
        self.assertEqual([], violations)
        self.assertTrue(any(line.startswith("OK") for line in info))

    def test_widened_binding_is_a_violation(self):
        listeners = audit.parse_listeners(
            "  TCP    0.0.0.0:8001         0.0.0.0:0              LISTENING       25044\n")
        violations, _ = audit.classify(listeners)
        self.assertEqual(1, len(violations))
        self.assertIn("BINDING-MISMATCH", violations[0])

    def test_unregistered_governed_port_is_a_violation(self):
        listeners = audit.parse_listeners(
            "  TCP    127.0.0.1:8315         0.0.0.0:0              LISTENING       999\n")
        violations, _ = audit.classify(listeners)
        self.assertEqual(1, len(violations))
        self.assertIn("UNREGISTERED", violations[0])
        # B0 (2026-11-01): Qoder native 8310 was retired, so the port stays
        # governed but holds no allocation; a listener there is a violation.
        self.assertNotIn(8310, audit.PORT_ALLOCATIONS)

    def test_out_of_scope_ports_never_violate(self):
        listeners = audit.parse_listeners(
            "  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4\n"
            "  UDP    0.0.0.0:5353           *:*                    4\n")
        violations, info = audit.classify(listeners)
        self.assertEqual([], violations)
        self.assertEqual(1, len(info))
        self.assertIn("治理段外监听条目", info[-1])
        self.assertIn("2", info[-1])

    def test_ipv6_loopback_counts_as_loopback(self):
        listeners = audit.parse_listeners(
            "  TCP    [::1]:5173             [::]:0                 LISTENING       68116\n")
        violations, _ = audit.classify(listeners)
        self.assertEqual([], violations)

    def test_governed_ranges_cover_retired_ports(self):
        self.assertTrue(audit.is_governed(8310))
        self.assertTrue(audit.is_governed(8320))
        self.assertTrue(audit.is_governed(8097))
        # B0 (2026-11-01): the retired Qoder native / CLIProxy gateways keep
        # their governed ports but hold no allocation, so a listener is reported
        # as UNREGISTERED instead of being silently accepted.
        self.assertNotIn(8310, audit.PORT_ALLOCATIONS)
        # 2026-11-14: 8097 is now the display port, so it IS allocated.
        self.assertIn(8097, audit.PORT_ALLOCATIONS)
        self.assertTrue(audit.is_governed(8317))  # old cliproxy port still governed
        self.assertFalse(audit.is_governed(445))
        self.assertFalse(audit.is_governed(49664))

    def test_console_ports_are_registered(self):
        # 2026-11-14: the cloud console moved 18096 -> 8096 and the display
        # moved 5901 -> 8097, both inside the governed 8090-8099 segment.
        self.assertEqual("usagesrv cloud console backend",
                         audit.PORT_ALLOCATIONS[8096][0])
        self.assertEqual("display x11vnc", audit.PORT_ALLOCATIONS[8097][0])
        self.assertTrue(audit.is_governed(8096))
        self.assertTrue(audit.is_governed(8097))
        listeners = audit.parse_listeners(
            "  TCP    127.0.0.1:8096    0.0.0.0:0    LISTENING    25044\n"
            "  TCP    127.0.0.1:8097    0.0.0.0:0    LISTENING    25045\n")
        violations, info = audit.classify(listeners)
        self.assertEqual([], violations)
        self.assertTrue(any(line.startswith("OK") for line in info))

    def test_legacy_console_ports_are_aliases_not_unregistered(self):
        # Expand-contract (2026-11-14): during the one-release window the old
        # ports are still legitimate listeners and must be reported as
        # LEGACY-ALIAS, never as UNREGISTERED.
        self.assertEqual(8096, audit.LEGACY_PORT_ALIASES[18096])
        self.assertEqual(8097, audit.LEGACY_PORT_ALIASES[5901])
        listeners = audit.parse_listeners(
            "  TCP    127.0.0.1:18096    0.0.0.0:0    LISTENING    25044\n"
            "  TCP    127.0.0.1:5901     0.0.0.0:0    LISTENING    25045\n")
        violations, info = audit.classify(listeners)
        self.assertEqual([], violations)
        aliases = [line for line in info if line.startswith("LEGACY-ALIAS")]
        self.assertEqual(2, len(aliases), info)

    def test_a_legacy_alias_widened_binding_is_a_violation(self):
        listeners = audit.parse_listeners(
            "  TCP    0.0.0.0:18096    0.0.0.0:0    LISTENING    25044\n")
        violations, _ = audit.classify(listeners)
        self.assertEqual(1, len(violations))
        self.assertIn("BINDING-MISMATCH", violations[0])
        self.assertIn("18096", violations[0])


if __name__ == "__main__":
    unittest.main()
