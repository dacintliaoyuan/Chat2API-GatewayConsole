#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Restricted broker control for the Linux UM share deployment.

This helper is the ONLY operator/systemd entry that talks to the broker host's
AF_UNIX management channel.  It never starts the 8100 gateway with a bare
``Popen``: the broker host (``share_broker_ops.py``) owns that child.  A start is
sent as the restricted broker ``start`` verb; an explicit operator start first
writes the persistent ``share-host-desired.json`` intent as ``running`` (the same
schema ``LinuxHostOps`` reads) and then sends the broker verb.

Drain semantics for updates: wait up to ``--drain-budget`` seconds for ESTABLISHED
TCP connections to the share port to reach zero (read from ``/proc/net/tcp{,6}``),
then send the broker ``drain`` verb (its own bounded in-flight wait + final stop +
closure verification).  The default total budget is 900 seconds; the broker verb's
wire deadline is at most 90 seconds, so the edge wait defaults to 810 seconds.
A timeout returns EX_TEMPFAIL and never pretends the gateway is drained.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PAYLOAD_ROOT = HERE.parents[2]
if str(PAYLOAD_ROOT) not in sys.path:
    sys.path.insert(0, str(PAYLOAD_ROOT))

EX_OK = 0
EX_REFUSED = 1
EX_UNCONFIRMED = 2
EX_USAGE = 64
EX_UNAVAILABLE = 69
EX_TEMPFAIL = 75
EX_NOPERM = 77

DEFAULT_SOCKET = "/run/um-share/broker.sock"
DEFAULT_KEY_FILE = "/etc/um-share/broker-control.key"
DEFAULT_STATE_DIR = "/run/um-share"
DEFAULT_DRAIN_BUDGET_S = 900
DEFAULT_BROKER_DRAIN_MAX_S = 90
INTENT_FILE = "share-host-desired.json"
INTENT_SCHEMA = 1


def _write_intent(state: str, reason: str) -> None:
    if state not in ("running", "stopped"):
        raise ValueError("intent state must be running or stopped")
    root = Path(os.environ.get("UM_SHARE_HOST_STATE_DIR") or DEFAULT_STATE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    target = root / INTENT_FILE
    payload = json.dumps({
        "schema_version": INTENT_SCHEMA,
        "state": state,
        "reason": reason,
        "at": int(time.time()),
    }, separators=(",", ":")).encode("utf-8")
    tmp = target.with_name(target.name + ".tmp.%d" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)


def _count_established(port: int) -> int:
    """Count ESTABLISHED TCP sockets whose local port is ``port``.

    ``/proc/net/tcp`` and ``tcp6`` are kernel facts; an unreadable table is an
    explicit failure, never an empty count.
    """
    wanted = "%04X" % int(port)
    total = 0
    readable = False
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="ascii",
                                         errors="replace").splitlines()
        except OSError:
            continue
        readable = True
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                continue
            local = fields[1]
            if ":" not in local:
                continue
            if local.rsplit(":", 1)[1].upper() == wanted:
                total += 1
    if not readable:
        raise OSError("no /proc/net/tcp table is readable")
    return total


def _wait_connections_zero(port: int, budget_s: int, interval_s: float,
                           deadline: float | None = None) -> bool:
    if deadline is None:
        deadline = time.monotonic() + max(0, int(budget_s))
    while True:
        try:
            count = _count_established(port)
        except OSError as exc:
            print(json.dumps({"ok": False, "error": "proc_net_unreadable",
                              "detail": str(exc)}), flush=True)
            return False
        print(json.dumps({"event": "drain_poll", "port": port,
                          "established": count,
                          "remaining_s": max(0, int(deadline - time.monotonic()))}),
              flush=True)
        if count == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.1, interval_s))


def _client(args):
    from share_broker_client import BrokerClient, UnixSocketBrokerTransport
    from share_broker_server import read_control_key
    try:
        secret = read_control_key(args.key_file)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "control_key_unusable",
                          "detail": type(exc).__name__}), flush=True)
        raise SystemExit(EX_NOPERM)
    return BrokerClient(lambda: UnixSocketBrokerTransport(args.socket),
                        secret, timeout_seconds=args.timeout)


def _emit(result) -> None:
    print(json.dumps({
        "ok": bool(result.ok),
        "error_code": result.error_code,
        "result": result.result,
    }, ensure_ascii=False, indent=2), flush=True)


def _status(args) -> int:
    client = _client(args)
    result = client.status()
    _emit(result)
    return EX_OK if result.ok else EX_UNCONFIRMED


def _start(args, explicit: bool) -> int:
    client = _client(args)
    if explicit:
        try:
            _write_intent("running", "explicit_start")
        except OSError as exc:
            print(json.dumps({"ok": False, "error": "intent_write_failed",
                              "detail": type(exc).__name__}), flush=True)
            return EX_NOPERM
    result = client.start()
    _emit(result)
    if result.ok and result.result.get("started") is True:
        return EX_OK
    if explicit:
        try:
            _write_intent("stopped", "start_failed")
        except OSError:
            pass
    return EX_UNCONFIRMED


def _host_stop(args) -> int:
    client = _client(args)
    result = client.host_stop()
    _emit(result)
    return EX_OK if result.ok and result.result.get("host_stopped") is True \
        else EX_UNCONFIRMED


def _drain(args, force: bool) -> int:
    if not force:
        total = max(1, int(args.drain_budget))
        # A caller may ask for a total budget below the historical 90-second
        # broker reservation (the emergency rollback path uses 60). Reserve
        # what fits instead of rejecting the documented 1..900 range; an
        # explicitly contradictory pair is still a usage error in main().
        broker_max = min(max(1, int(args.broker_drain_max)),
                        max(1, total - 1))
        edge_budget = max(1, total - broker_max)
        deadline = time.monotonic() + edge_budget
        gateway_ok = _wait_connections_zero(args.port, 0,
                                            args.poll_interval, deadline)
        backend_ok = _wait_connections_zero(args.backend_port, 0,
                                           args.poll_interval, deadline)
        if not (gateway_ok and backend_ok):
            print(json.dumps({
                "ok": False, "error": "drain_timeout",
                "port": args.port, "backend_port": args.backend_port,
                "budget_s": args.drain_budget,
                "edge_budget_s": edge_budget,
                "note": "established connections did not reach zero; "
                         "the update must not switch versions",
            }, ensure_ascii=False), flush=True)
            return EX_TEMPFAIL
    client = _client(args)
    result = client.drain()
    _emit(result)
    if not result.ok:
        return EX_UNCONFIRMED
    if result.result.get("timed_out") is True:
        return EX_TEMPFAIL
    if result.result.get("drained") is True \
            and result.result.get("stopped") is True:
        return EX_OK
    return EX_UNCONFIRMED


def _stop(args, force: bool) -> int:
    if force:
        return _host_stop(args)
    code = _drain(args, force=False)
    if code == EX_TEMPFAIL:
        print(json.dumps({
            "ok": False, "error": "stop_requires_force",
            "note": "not drained; run stop --force only with explicit "
                     "operator approval",
        }), flush=True)
        return EX_TEMPFAIL
    if code != EX_OK:
        return code
    return _host_stop(args)


def _restart(args) -> int:
    code = _stop(args, force=False)
    if code != EX_OK:
        return code
    return _start(args, explicit=True)


def _gateway_restart(args) -> int:
    client = _client(args)
    result = client.gateway_restart("share_gateway", args.restart_budget)
    _emit(result)
    ok = result.ok and result.result.get("restarted") is True \
        and result.result.get("stopped_confirmed") is True \
        and result.result.get("running_confirmed") is True
    return EX_OK if ok else EX_UNCONFIRMED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UM share broker control (AF_UNIX management channel)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help="broker management AF_UNIX socket")
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE,
                        help="broker control key (0600, current uid)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="one broker exchange timeout (1..180 s)")
    parser.add_argument("--port", type=int, default=8100,
                        help="share gateway TCP port used for the drain wait")
    parser.add_argument("--backend-port", type=int, default=8110,
                        help="share backend TCP port used for the drain wait")
    parser.add_argument("--drain-budget", type=int,
                        default=DEFAULT_DRAIN_BUDGET_S,
                        help="total drain budget in seconds (default 900)")
    parser.add_argument("--broker-drain-max", type=int, default=None,
                        help="reserved broker drain deadline (default 90, "
                             "or one second below --drain-budget)")
    parser.add_argument("--poll-interval", type=float, default=5.0,
                        help="drain poll interval in seconds")
    parser.add_argument("--restart-budget", type=int, default=120,
                        help="gateway-restart budget (1..180 s)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="read broker status")
    sub.add_parser("start", help="restricted start (respects stop intent)")
    sub.add_parser("start-explicit", help="operator start (clears stop intent)")
    sub.add_parser("drain", help="wait for established=0, then broker drain")
    stop = sub.add_parser("stop", help="drain + host-stop")
    stop.add_argument("--force", action="store_true",
                      help="skip the 15-minute wait and host-stop directly")
    sub.add_parser("restart", help="drain + host-stop + explicit start")
    sub.add_parser("gateway-restart", help="restricted 8100 single-layer restart")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if not 1 <= int(args.port) <= 65535:
        print("port must be within 1..65535", file=sys.stderr)
        return EX_USAGE
    if not 1 <= int(args.backend_port) <= 65535:
        print("backend-port must be within 1..65535", file=sys.stderr)
        return EX_USAGE
    if not 1 <= int(args.timeout) <= 180:
        print("timeout must be within 1..180 seconds", file=sys.stderr)
        return EX_USAGE
    if not 1 <= int(args.drain_budget) <= 900:
        print("drain-budget must be within 1..900 seconds", file=sys.stderr)
        return EX_USAGE
    explicit_broker_max = args.broker_drain_max is not None
    if not explicit_broker_max:
        # The historical 90-second reservation is the default, but a smaller
        # total budget (e.g. the 60-second emergency rollback path) still
        # reserves the broker time that fits inside it.
        args.broker_drain_max = min(
            DEFAULT_BROKER_DRAIN_MAX_S,
            max(1, int(args.drain_budget) - 1))
    if not 1 <= int(args.broker_drain_max) <= 90:
        print("broker-drain-max must be within 1..90 seconds", file=sys.stderr)
        return EX_USAGE
    if explicit_broker_max and int(args.broker_drain_max) >= int(
            args.drain_budget):
        print("broker-drain-max must be smaller than drain-budget", file=sys.stderr)
        return EX_USAGE
    if not 0.1 <= float(args.poll_interval) <= 60:
        print("poll-interval must be within 0.1..60 seconds", file=sys.stderr)
        return EX_USAGE
    if not 1 <= int(args.restart_budget) <= 180:
        print("restart-budget must be within 1..180 seconds", file=sys.stderr)
        return EX_USAGE
    command = args.command
    if command == "status":
        return _status(args)
    if command == "start":
        return _start(args, explicit=False)
    if command == "start-explicit":
        return _start(args, explicit=True)
    if command == "drain":
        return _drain(args, force=False)
    if command == "stop":
        return _stop(args, force=bool(args.force))
    if command == "restart":
        return _restart(args)
    if command == "gateway-restart":
        return _gateway_restart(args)
    return EX_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
