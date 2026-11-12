#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux end-to-end verification fixture for the UM share chain.

This fixture starts REAL product processes on loopback only:

    fake UM (verification-only upstream)   127.0.0.1:28999
    chat2api share backend               127.0.0.1:28110
    share gateway (8100 role)              127.0.0.1:28100
    management broker (AF_UNIX)            <temp>/run/broker.sock
    management API (usagesrv cloud role)    127.0.0.1:28096

Every path, secret, ledger, profile and SQLite file lives under one private
temporary directory.  No production ``data/`` path is read or written, no real UM
credential is used, and no Windows process is touched.

The ``core`` scenario covers the P1 chain:

* /v1/models normal and stale-cache fail-closed
* Chat stream/non-stream, Messages stream, Responses with tool results
* invalid Key, revoked Key, 20/min, per-Key concurrency, global 64/65
* management API create/rotate/revoke -> projection publish -> instance load ->
  real admission (no fake ACK)
* 10 s cumulative scheduling and per-Key rotation
* cancel/release exactly once and late result not taking a new slot
* Q11 usage write/read/summary and cache-report status (unknown vs explicit 0)

Exit codes: 0 all scenarios passed, 1 a scenario failed, 2 environment missing.

The report is a JSON document with one entry per scenario; every HTTP request is
recorded with method, path, status and elapsed milliseconds.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]          # ai-gateway/
TESTS = HERE / "tests"
DEFAULT_REPORT = Path(
    "D:/git/github/personal_files/SelfUseAI/tmp/um-cloud-p1-20261030/"
    "chain-core.json")
DEFAULT_LONG_REPORT = Path(
    "D:/git/github/personal_files/SelfUseAI/tmp/um-cloud-p1-20261030/"
    "chain-long.json")

SHARE_PORT = 28100
BACKEND_PORT = 28110
MANAGEMENT_PORT = 28096
FAKE_UM_PORT = 28999
DISPATCH_GATE_PORT = 28120
RELAY_GATE_PORT = 28130
PORTS = (SHARE_PORT, BACKEND_PORT, MANAGEMENT_PORT, FAKE_UM_PORT,
         DISPATCH_GATE_PORT, RELAY_GATE_PORT)

SHARE_MODEL = "fake-um-model"
CORE_TIMEOUT_S = 120.0
# The chain scenarios drive the REAL dispatch wait with a shortened budget so
# the timeout path is observable without waiting the production 10 seconds.
DISPATCH_MAX_WAIT_SHORT_S = 1.5

# Test-only backend launcher. It runs the REAL chat2api.main() and only gates
# the REAL ShareDispatcher register/poll timing for the R-C2 chain scenarios.
DISPATCH_ENTRY_SOURCE = r'''"""Test-only launcher: real chat2api main with a dispatch poll gate."""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import chat2api
import share_dispatch as share_dispatch

_LOCK = threading.Lock()
_RELEASE = threading.Event()
_RELEASE.set()
_ARMED = False
_DISABLED = False
_REGISTRATIONS = []
_GRANTS = []
_GRANTED_IDS = set()
# Review #7: ``_SENDS`` records the REAL upstream send order at the product's
# own ``requests.request`` boundary (inside Proxy.call, after
# ShareDispatcher.commit), never the order in which the test releases a gate.
_SENDS = []
_SEND_MARKERS = set()
_SEND_GATE_ARMED = True
_SEND_EVENTS = {}
_COMMITS = []
_COMMIT_EVENTS = {}
_COMMIT_ARMED = False
_BLOCKER = None

_original_register = share_dispatch.ShareDispatcher.register
_original_poll = share_dispatch.ShareDispatcher.poll
_original_grant_locked = share_dispatch.ShareDispatcher._grant_locked
_original_commit = share_dispatch.ShareDispatcher.commit
_original_ticket = chat2api.share_dispatch_ticket
_original_requests_request = requests.request
_CHAIN_PROMPT_RE = re.compile(r"chain key=([A-Za-z0-9_.-]+) seq=(\d+)")


def _chain_marker(payload):
    """Map one REAL upstream body back to the chain marker A1..B2.

    The chain fixture puts the marker in the last user message; serializing the
    already-built JSON body is enough to identify it without depending on any
    dispatcher or test release state.
    """
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    match = _CHAIN_PROMPT_RE.search(text)
    if match is None:
        return None
    return "%s%s" % (match.group(1), match.group(2))


def recording_upstream_request(method, url, **kwargs):
    """Record the marker of a REAL upstream send before the socket write.

    Review #7: the product calls ``requests.request`` from ``Proxy.call`` after
    ``ShareDispatcher.commit`` and before it writes the request to the upstream.
    This hook observes that real boundary, so the recorded order is the product's
    own send order even when the test does not release the sends one by one.
    """
    marker = _chain_marker(kwargs.get("json"))
    if marker is not None:
        with _LOCK:
            if _ARMED and not _DISABLED and marker not in _SEND_MARKERS:
                _SEND_MARKERS.add(marker)
                _SENDS.append(marker)
    return _original_requests_request(method, url, **kwargs)


requests.request = recording_upstream_request


def _tickets():
    """Snapshot the REAL dispatcher's live tickets (test-only read)."""
    dispatcher = chat2api._share_dispatcher
    if dispatcher is None:
        return []
    with dispatcher.condition:
        return [{"requestId": ticket.request_id,
                 "keyId": ticket.key_id,
                 "state": ticket.state}
                for ticket in dispatcher._tickets.values()]


def _state():
    # Review #11: never hold ``_LOCK`` while reading the REAL dispatcher's
    # condition (``_grant_locked`` records grants under that condition and then
    # takes ``_LOCK``).  Snapshot the launcher state first, release it, and only
    # then read the dispatcher.  This keeps the lock order one-way.
    with _LOCK:
        state = {
            "armed": _ARMED,
            "disabled": _DISABLED,
            "commitArmed": _COMMIT_ARMED,
            "registrations": [dict(item) for item in _REGISTRATIONS],
            "grants": [dict(item) for item in _GRANTS],
            "sends": list(_SENDS),
            "sendGateArmed": _SEND_GATE_ARMED,
            "commits": list(_COMMITS),
            "blocker": (None if _BLOCKER is None else {
                "requestId": _BLOCKER.request_id,
                "keyId": _BLOCKER.key_id,
            }),
        }
    dispatcher = chat2api._share_dispatcher
    state["dispatcher"] = (None if dispatcher is None
                            else dispatcher.stats())
    state["tickets"] = _tickets()
    return state


def gated_register(self, key_id, request_id, closed_reason=None):
    ticket = _original_register(self, key_id, request_id,
                                closed_reason=closed_reason)
    if ticket.state != "pending":
        # A refused (already revoked) ticket never enters the rotation, so it
        # must not be counted as a registration the test can release.
        return ticket
    with _LOCK:
        if _DISABLED or not _ARMED:
            return ticket
        _REGISTRATIONS.append({
            "requestId": request_id,
            "keyId": key_id,
            "order": len(_REGISTRATIONS) + 1,
        })
    _RELEASE.wait(30.0)
    return ticket


def gated_grant_locked(self, ticket):
    """Record the grant at the SAME critical point as the state change.

    Review #11: ``gated_poll`` used to append ``_GRANTS`` after
    ``_original_poll`` returned, i.e. after the dispatcher condition was
    released.  Two concurrent grants could then be recorded in the opposite
    order and the exact A1,B1,A2,B2,A3 assertion was racy.  This wrapper
    runs inside ``_poll_locked`` while the REAL dispatcher still holds its
    condition, so the record order IS the grant order.
    """
    _original_grant_locked(self, ticket)
    with _LOCK:
        if not _ARMED or _DISABLED:
            return
        if ticket.request_id in _GRANTED_IDS:
            return
        _GRANTED_IDS.add(ticket.request_id)
        _GRANTS.append({
            "requestId": ticket.request_id,
            "keyId": ticket.key_id,
            "order": len(_GRANTS) + 1,
        })
        _SEND_EVENTS[ticket.request_id] = threading.Event()
        if _COMMIT_ARMED:
            _COMMIT_EVENTS[ticket.request_id] = threading.Event()


def gated_poll(self, ticket, request_deadline=None):
    with _LOCK:
        armed = _ARMED and not _DISABLED
        send_gated = armed and _SEND_GATE_ARMED
    if armed:
        _RELEASE.wait(30.0)
    outcome = _original_poll(self, ticket, request_deadline)
    if outcome == share_dispatch.OUTCOME_GRANTED and send_gated:
        # Opt-in hold used only by scenarios that must stop a GRANTED ticket
        # before its REAL commit. The rotation scenario disarms this gate, so its
        # send order is decided by the product, not by the test.
        with _LOCK:
            event = _SEND_EVENTS.get(ticket.request_id)
        if event is not None:
            event.wait(30.0)
    return outcome


def gated_commit(self, ticket):
    """Hold ONE granted ticket immediately before the REAL commit.

    R-C5 GRANTED-but-not-committed chain: the product has already returned
    from ``wait_turn`` with GRANTED and passed its pre-send checks; this gate
    stops it after those checks and before ``ShareDispatcher.commit`` so the
    REAL management API can revoke the Key in that exact window. Releasing the
    gate runs the REAL commit, which returns False after the revoke; no grant,
    commit or release decision is faked.
    """
    with _LOCK:
        armed = _ARMED and not _DISABLED and _COMMIT_ARMED
        event = _COMMIT_EVENTS.get(ticket.request_id)
        if armed and event is not None:
            _COMMITS.append(ticket.request_id)
    if armed and event is not None:
        event.wait(30.0)
    return _original_commit(self, ticket)


def disabled_ticket(protocol):
    return None


def _register_blocker(payload):
    global _BLOCKER
    key_id = payload.get("keyId") or "blocked-key"
    request_id = payload.get("requestId") or "blocked-request"
    ticket = _original_register(chat2api.share_dispatcher(), key_id,
                                request_id)
    with _LOCK:
        _BLOCKER = ticket
    return {"blocked": True, "requestId": request_id, "keyId": key_id}


def _release_blocker():
    global _BLOCKER
    with _LOCK:
        ticket = _BLOCKER
        _BLOCKER = None
    if ticket is None:
        return {"released": False}
    # ``ticket.release`` takes the REAL dispatcher condition; never call it
    # while holding ``_LOCK`` (the grant wrapper takes the locks in the
    # opposite order).
    return {"released": ticket.release("test")}


def _arm():
    global _ARMED, _COMMIT_ARMED, _SEND_GATE_ARMED
    with _LOCK:
        _ARMED = True
        _DISABLED = False
        _COMMIT_ARMED = False
        _SEND_GATE_ARMED = True
        _REGISTRATIONS.clear()
        _GRANTS.clear()
        _GRANTED_IDS.clear()
        _SENDS.clear()
        _SEND_MARKERS.clear()
        _COMMITS.clear()
        _SEND_EVENTS.clear()
        _COMMIT_EVENTS.clear()
    _RELEASE.clear()
    return _state()


def _send_disarm():
    """Disable the opt-in send hold so sends race for real (Review #7)."""
    global _SEND_GATE_ARMED
    with _LOCK:
        _SEND_GATE_ARMED = False
        events = list(_SEND_EVENTS.values())
        _SEND_EVENTS.clear()
    for event in events:
        event.set()
    return _state()


def _disarm():
    global _ARMED, _COMMIT_ARMED, _SEND_GATE_ARMED
    with _LOCK:
        _ARMED = False
        _COMMIT_ARMED = False
        _SEND_GATE_ARMED = True
        _GRANTED_IDS.clear()
        events = list(_SEND_EVENTS.values())
        _SEND_EVENTS.clear()
        commit_events = list(_COMMIT_EVENTS.values())
        _COMMIT_EVENTS.clear()
    _RELEASE.set()
    for event in events:
        event.set()
    for event in commit_events:
        event.set()
    return _state()


def _release():
    _RELEASE.set()
    return _state()


def _release_send(request_id):
    with _LOCK:
        event = _SEND_EVENTS.get(request_id)
    if event is None:
        return {"released": False}
    event.set()
    return {"released": True}


def _release_commit(request_id):
    with _LOCK:
        event = _COMMIT_EVENTS.get(request_id)
    if event is None:
        return {"released": False}
    event.set()
    return {"released": True}


def _commit_arm():
    global _COMMIT_ARMED
    with _LOCK:
        _COMMIT_ARMED = True
        _COMMITS.clear()
        _COMMIT_EVENTS.clear()
    return _state()


def _commit_disarm():
    global _COMMIT_ARMED
    with _LOCK:
        _COMMIT_ARMED = False
        events = list(_COMMIT_EVENTS.values())
        _COMMIT_EVENTS.clear()
    for event in events:
        event.set()
    return _state()


def _disable():
    global _DISABLED, _COMMIT_ARMED
    with _LOCK:
        _DISABLED = True
        _COMMIT_ARMED = False
        events = list(_SEND_EVENTS.values())
        _SEND_EVENTS.clear()
        commit_events = list(_COMMIT_EVENTS.values())
        _COMMIT_EVENTS.clear()
    chat2api.share_dispatch_ticket = disabled_ticket
    _RELEASE.set()
    for event in events:
        event.set()
    for event in commit_events:
        event.set()
    return _state()


def _enable():
    global _DISABLED
    with _LOCK:
        _DISABLED = False
    chat2api.share_dispatch_ticket = _original_ticket
    return _state()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def do_GET(self):
        if self.path == "/gate/state":
            self._reply(200, _state())
            return
        self._reply(404, {"detail": "not found"})

    def do_POST(self):
        payload = self._body()
        if payload is None:
            self._reply(400, {"detail": "invalid JSON"})
            return
        if self.path == "/gate/arm":
            self._reply(200, _arm())
        elif self.path == "/gate/disarm":
            self._reply(200, _disarm())
        elif self.path == "/gate/release":
            self._reply(200, _release())
        elif self.path == "/gate/send-release":
            self._reply(200, _release_send(payload.get("requestId")))
        elif self.path == "/gate/send-disarm":
            self._reply(200, _send_disarm())
        elif self.path == "/gate/commit-release":
            self._reply(200, _release_commit(payload.get("requestId")))
        elif self.path == "/gate/commit-arm":
            self._reply(200, _commit_arm())
        elif self.path == "/gate/commit-disarm":
            self._reply(200, _commit_disarm())
        elif self.path == "/gate/disable":
            self._reply(200, _disable())
        elif self.path == "/gate/enable":
            self._reply(200, _enable())
        elif self.path == "/gate/blocker":
            self._reply(200, _register_blocker(payload))
        elif self.path == "/gate/blocker-release":
            self._reply(200, _release_blocker())
        else:
            self._reply(404, {"detail": "not found"})


def main():
    share_dispatch.ShareDispatcher.register = gated_register
    share_dispatch.ShareDispatcher.poll = gated_poll
    share_dispatch.ShareDispatcher._grant_locked = gated_grant_locked
    share_dispatch.ShareDispatcher.commit = gated_commit
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ["CHAIN_TEST_DISPATCH_GATE_PORT"])),
        _Handler)
    with open(os.environ["CHAIN_TEST_DISPATCH_GATE_STATE"], "w",
              encoding="utf-8") as handle:
        json.dump({"port": server.server_address[1]}, handle)
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.05}, daemon=True).start()
    sys.argv = ["chat2api.py"] + sys.argv[1:]
    raise SystemExit(chat2api.main())


if __name__ == "__main__":
    main()
'''

# Test-only share-gateway launcher.  It runs the REAL share_entry.main()
# and only gates the REAL HTTPConnection.request immediately before the
# 8100->8110 upstream send, so a request that 8100 already admitted can be
# held there while the management API revokes the Key.  It never fakes an
# admission, a notice or a send.
RELAY_ENTRY_SOURCE = r'''"""Test-only launcher: real share gateway with a relay send gate."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import share_entry as share_entry

_LOCK = threading.Lock()
_RELEASE = threading.Event()
_RELEASE.set()
_ARMED = False
_REGISTRATIONS = []

_original_refused = share_entry.ShareHandler._relay_refused


def gated_relay_refused(self, inflight_ctx):
    with _LOCK:
        armed = _ARMED
        if armed:
            _REGISTRATIONS.append({
                "order": len(_REGISTRATIONS) + 1,
            })
    if armed:
        _RELEASE.wait(30.0)
    return _original_refused(self, inflight_ctx)


def _state():
    with _LOCK:
        return {"armed": _ARMED,
                "registrations": [dict(item) for item in _REGISTRATIONS]}


def _arm():
    global _ARMED
    with _LOCK:
        _ARMED = True
        _REGISTRATIONS.clear()
    _RELEASE.clear()
    return _state()


def _release():
    _RELEASE.set()
    return _state()


def _disarm():
    global _ARMED
    with _LOCK:
        _ARMED = False
    _RELEASE.set()
    return _state()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/gate/state":
            self._reply(200, _state())
            return
        self._reply(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            self._reply(400, {"detail": "invalid JSON"})
            return
        if self.path == "/gate/arm":
            self._reply(200, _arm())
        elif self.path == "/gate/release":
            self._reply(200, _release())
        elif self.path == "/gate/disarm":
            self._reply(200, _disarm())
        else:
            self._reply(404, {"detail": "not found"})


def main():
    share_entry.ShareHandler._relay_refused = gated_relay_refused
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ["CHAIN_TEST_RELAY_GATE_PORT"])),
        _Handler)
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.05}, daemon=True).start()
    sys.argv = ["share_entry.py"] + sys.argv[1:]
    raise SystemExit(share_entry.main())


if __name__ == "__main__":
    main()
'''

# Long-request verification uses shortened budgets in an ISOLATED child
# process. The production defaults stay 120/180/360/12; these values only make
# the same code paths observable without waiting minutes.
LONG_FIRST_BYTE_S = 1.2
LONG_SILENCE_S = 2.0
LONG_MAX_REQUEST_S = 10
LONG_SLOW_THRESHOLD_S = 1.0
LONG_FAST_RETRIES = 5
LONG_SLOW_RETRIES = 2
LONG_MAX_ATTEMPTS = 12
LONG_REPETITION_RETRIES = 5
LONG_READ_CAP_NOMINAL_S = 600


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _normalize_report_path(raw: str) -> Path:
    """Accept the Windows ``D:/...`` form when this runs inside WSL."""
    path = Path(raw)
    if os.name == "posix" and len(raw) >= 3 and raw[1] == ":" \
            and raw[2] in ("/", "\\"):
        path = Path("/mnt") / raw[0].lower() / raw[3:]
    return path


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-limit:].decode("utf-8", "replace")
    return text


def _free_port(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _wait_for(check, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
        except Exception as exc:  # noqa: BLE001 - readiness probes are retried
            last = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    if last is not None:
        raise TimeoutError(f"timed out waiting; last error: {last}")
    raise TimeoutError("timed out waiting")


def _proc_snapshot(pid: int) -> dict:
    """Linux-only resource snapshot: threads, FDs and sockets.

    Reader threads created by the product are named
    ``chat2api-upstream-reader``; the kernel truncates ``comm`` to 15 chars,
    so the observed prefix is ``chat2api-upstr``.
    """
    task_dir = Path(f"/proc/{pid}/task")
    fd_dir = Path(f"/proc/{pid}/fd")
    tids = list(task_dir.iterdir())
    names = []
    for tid in tids:
        try:
            names.append((tid / "comm").read_text(
                encoding="utf-8", errors="replace").strip())
        except OSError:
            names.append("<unreadable>")
    fds = list(fd_dir.iterdir())
    sockets = 0
    for fd in fds:
        try:
            if os.readlink(fd).startswith("socket:"):
                sockets += 1
        except OSError:
            pass
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return {
        "threads": len(tids),
        "threadNameCounts": counts,
        "readerThreads": sum(
            1 for name in names if name.startswith("chat2api-upstr")),
        "fds": len(fds),
        "sockets": sockets,
    }


def _wait_proc_settle(pid: int, baseline: dict, timeout: float = 12.0):
    """Wait for threads/FDs to return to the pre-request baseline."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        last = _proc_snapshot(pid)
        if (last["threads"] <= baseline["threads"]
                and last["fds"] <= baseline["fds"]
                and last["sockets"] <= baseline["sockets"]):
            return last, int((time.monotonic() - started) * 1000)
        time.sleep(0.25)
    return last, int((time.monotonic() - started) * 1000)


def _stable_proc_snapshot(pid: int, timeout: float = 12.0):
    """Sample until two consecutive snapshots agree.

    A thread that is exiting between the ``/proc/<pid>/task`` listing and its
    ``comm`` read shows up as ``<unreadable>`` and inflates a baseline count;
    comparing two consecutive samples removes that transient race without
    weakening the product resource assertion.
    """
    started = time.monotonic()
    previous = None
    while time.monotonic() - started < timeout:
        current = _proc_snapshot(pid)
        if (previous is not None
                and current["threads"] == previous["threads"]
                and current["fds"] == previous["fds"]
                and current["sockets"] == previous["sockets"]
                and "<unreadable>" not in current["threadNameCounts"]):
            return current
        previous = current
        time.sleep(0.1)
    return previous if previous is not None else _proc_snapshot(pid)


def _read_first_stream_event(port, body, key, timeout=20.0,
                             on_first_data=None):
    """Read exactly the first SSE data line, then close (a client cancel)."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        conn.request("POST", "/v1/chat/completions", body=payload,
                     headers={"Connection": "close",
                               "Authorization": "Bearer " + key,
                               "Content-Type": "application/json"})
        response = conn.getresponse()
        status = response.status
        headers = {k.lower(): v for k, v in response.getheaders()}
        first_line = response.readline()
        if on_first_data is not None and first_line:
            on_first_data()
        return HttpResult("POST", "/v1/chat/completions", status, headers,
                          first_line, _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return HttpResult("POST", "/v1/chat/completions", None, {}, b"",
                          _ms(started), f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _load_module(name: str, path: Path):
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


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class HttpResult:
    def __init__(self, method, path, status, headers, body, duration_ms,
                 error=None):
        self.method = method
        self.path = path
        self.status = status
        self.headers = headers
        self.body = body
        self.duration_ms = duration_ms
        self.error = error

    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def as_record(self) -> dict:
        record = {
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "durationMs": self.duration_ms,
        }
        if self.error:
            record["error"] = self.error
        payload = self.json()
        if isinstance(payload, dict):
            record["body"] = _bounded_json(payload)
        elif self.body:
            record["bodyText"] = self.text()[:2000]
        return record


def _bounded_json(value, depth=0):
    """Keep report bodies small; never copy a whole long stream."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:12]:
            name = str(key)
            if name.lower() in ("secret", "mk", "token", "api_key",
                                "authorization", "x-admin-control",
                                "x-api-key", "cookie"):
                result[name] = "<redacted>"
            else:
                result[name] = _bounded_json(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_json(v, depth + 1) for v in value[:8]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


def _http_request(port, method, path, *, body=None, key=None,
                   timeout=30.0, stream=False, read_body=True,
                   headers=None, close_after_headers=False):
    """One loopback HTTP request.  ``stream`` reads SSE lines until EOF."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    request_headers = {"Connection": "close"}
    if headers:
        request_headers.update(headers)
    if key:
        request_headers["Authorization"] = "Bearer " + key
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        if close_after_headers:
            raw = b""
            conn.close()
            return HttpResult(method, path, status, response_headers, raw,
                              _ms(started))
        if stream:
            chunks = []
            while True:
                line = response.readline()
                if not line:
                    break
                chunks.append(line)
            raw = b"".join(chunks)
        elif read_body:
            raw = response.read()
        else:
            raw = b""
        conn.close()
        return HttpResult(method, path, status, response_headers, raw,
                          _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        try:
            conn.close()
        except Exception:
            pass
        return HttpResult(method, path, None, {}, b"", _ms(started),
                          f"{type(exc).__name__}: {exc}")



def _sse_text(raw: bytes) -> str:
    """Join the text deltas of an OpenAI or Anthropic SSE body."""
    pieces = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str):
                    pieces.append(content)
        if payload.get("type") == "content_block_delta":
            delta = payload.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)

def _stream_lines(result: HttpResult):
    return result.body.decode("utf-8", "replace").splitlines()


def _tool_turn_errors(body, call_id, output):
    """Return every structural error of one upstream tool turn.

    The check is deliberately structural: the tool result must be a ``role=tool``
    message whose ``tool_call_id`` and content match, AND an assistant message
    must declare the same id in its ``tool_calls``. A prompt echo or an output
    substring can never satisfy it.
    """
    if not isinstance(body, dict):
        return ["upstream body is not an object"]
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ["upstream body has no messages list"]
    errors = []
    assistant_calls = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            assistant_calls.extend(
                call for call in calls if isinstance(call, dict))
    if not any(call.get("id") == call_id for call in assistant_calls):
        errors.append(
            "assistant tool_calls does not declare %r" % (call_id,))
    tool_messages = [
        message for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"]
    if len(tool_messages) != 1:
        errors.append(
            "expected exactly one tool message, got %d" % (len(tool_messages),))
        return errors
    tool_message = tool_messages[0]
    if tool_message.get("tool_call_id") != call_id:
        errors.append(
            "tool message tool_call_id %r != %r"
            % (tool_message.get("tool_call_id"), call_id))
    if tool_message.get("content") != output:
        errors.append(
            "tool message content %r != %r"
            % (tool_message.get("content"), output))
    tool_index = messages.index(tool_message)
    declared_before = False
    for message in messages[:tool_index]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") == call_id:
                declared_before = True
    if not declared_before:
        errors.append("tool message does not follow its assistant call")
    return errors


def _strict_tool_turn_rejection(result, arrivals):
    """The strict negative contract for a structurally broken tool turn.

    Review #7: the old assertion was ``status == 400 OR broken_errors``, so a
    production that FORWARDED the broken structure still passed through the
    test-side checker.  The strict predicate requires BOTH:
    the client gets a structured 400 refusal AND the request never reaches the
    upstream (``arrivals`` is empty).  A forwarded request fails it even when
    ``_tool_turn_errors`` can detect the broken body.
    """
    if result is None or result.status != 400 or arrivals:
        return False
    payload = result.json()
    error = payload.get("error") if isinstance(payload, dict) else None
    return (isinstance(error, dict)
            and error.get("message") == "unsupported_responses_request")


def _hold_open_request(port, method, path, body, key, started_event,
                        finish_event, timeout=60.0):
    """Send a request, read its headers, and KEEP it in flight.

    ``started_event`` is set once the response headers arrived; the caller can
    then send another request while THIS one still owns its admission slot.
    ``finish_event`` releases the body read (the request then finishes).
    """
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Connection": "close", "Authorization": "Bearer " + key}
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        started_event.set()
        finish_event.wait(timeout)
        raw = response.read()
        return HttpResult(method, path, status, response_headers, raw,
                          _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        started_event.set()
        return HttpResult(method, path, None, {}, b"", _ms(started),
                          f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# --------------------------------------------------------------------------
# process and scenario records
# --------------------------------------------------------------------------
class ManagedProcess:
    def __init__(self, name: str, argv, env, cwd: Path, log_path: Path):
        self.name = name
        self.argv = [str(part) for part in argv]
        self.env = env
        self.cwd = str(cwd)
        self.log_path = log_path
        self.process = None
        self.started_at = None
        self.log = None

    def start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "ab", buffering=0)
        self.process = subprocess.Popen(
            self.argv, cwd=self.cwd, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
        self.started_at = time.monotonic()
        return self

    def stop(self, timeout: float = 10.0):
        process = self.process
        self.process = None
        if process is None:
            if self.log is not None:
                with contextlib.suppress(OSError):
                    self.log.close()
                self.log = None
            return
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        if self.log is not None:
            with contextlib.suppress(OSError):
                self.log.close()
            self.log = None

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def tail(self) -> str:
        return _tail(self.log_path)


class Scenario:
    def __init__(self, name: str):
        self.name = name
        self.started = time.monotonic()
        self.requests = []
        self.assertions = []
        self.failures = []
        self.notes = []
        self.evidence = {}
        self.classification = None
        self.skipped_reason = None

    def request(self, result: HttpResult, note=None):
        record = result.as_record()
        if note:
            record["note"] = note
        self.requests.append(record)
        return result

    def check(self, condition, message):
        self.assertions.append({"ok": bool(condition), "message": message})
        if not condition:
            self.failures.append(message)
        return bool(condition)

    def note(self, message):
        self.notes.append(message)

    def record_evidence(self, key, value):
        self.evidence[key] = value

    def classify(self, classification):
        self.classification = classification

    def skip(self, reason):
        self.skipped_reason = reason

    def finish(self, error=None) -> dict:
        status = "skipped" if self.skipped_reason else (
            "failed" if self.failures or error else "passed")
        record = {
            "name": self.name,
            "status": status,
            "durationMs": _ms(self.started),
            "requests": self.requests,
            "assertions": self.assertions,
        }
        if self.classification:
            record["classification"] = self.classification
        if self.skipped_reason:
            record["skippedReason"] = self.skipped_reason
        if error:
            record["error"] = str(error)
        if self.failures:
            record["failures"] = list(self.failures)
        if self.notes:
            record["notes"] = list(self.notes)
        if self.evidence:
            record["evidence"] = self.evidence
        return record


# --------------------------------------------------------------------------
# the fixture
# --------------------------------------------------------------------------
class ChainFixture:
    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.root = Path(tempfile.mkdtemp(prefix="um-share-chain-"))
        self.processes = []
        self.fake_um = None
        self.fake_um_module = None
        self.share_dispatch_module = None
        self.scenarios = []
        self.notes = []
        self._last_key = None

        self.paths = {
            "store": self.root / "share-store.db",
            "projection": self.root / "projection.json",
            "attribution": self.root / "attribution.key",
            "instance_key": self.root / "instance.key",
            "broker_key": self.root / "broker-control.key",
            "secret_key": self.root / "share-secret.key",
            "control_token": self.root / "admin-control-token",
            "session_code": self.root / "session-code",
            "management_config": self.root / "share-management.json",
            "registration": self.root / "run" / "instance-registration.json",
            "instance_prefix": self.root / "run" / "instance-",
            "broker_socket": self.root / "run" / "broker.sock",
            "broker_dir": self.root / "broker",
            "host_pid": self.root / "run" / "share-host.pid",
            "desired_state": self.root / "run",
            "profile": self.root / "profile" / "share-backend",
            "backend_dir": self.root / "backend",
            "usage_ledger": self.root / "backend" / "usage-share-backend.jsonl",
            "price_table": self.root / "prices.json",
            "chat2api_log": self.root / "chat2api.log",
            "chat2api_usage": self.root / "chat2api-usage.jsonl",
            "chat2api_token": self.root / "chat2api-token.json",
            "chat2api_control": self.root / "chat2api-control-token",
            "chat2api_reauth": self.root / "chat2api-reauth.state",
            "revoke_state": self.root / "share-revoke.state",
            "share_faults": self.root / "share-faults.jsonl",
            "fake_um_log": self.root / "fake-um.log",
            "chat2api_stdout": self.root / "chat2api.stdout.log",
            "share_stdout": self.root / "share-gateway.stdout.log",
            "broker_stdout": self.root / "broker.stdout.log",
            "management_stdout": self.root / "usagesrv.stdout.log",
            "dispatch_entry": self.root / "dispatch_entry.py",
            "dispatch_gate_state": self.root / "dispatch-gate.json",
            "relay_entry": self.root / "relay_entry.py",
        }
        self.env = self._build_env()

    # ------------------------------------------------------------ setup
    def _build_env(self) -> dict:
        env = dict(os.environ)
        env.update({
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "TEMP": str(self.root),
            "TMP": str(self.root),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "PYTHONPATH": os.pathsep.join(
                [str(HERE), str(HERE / "chat2api"), str(TESTS)]),
            "CHAT2API_TOKEN_FILE": str(self.paths["chat2api_token"]),
            "CHAT2API_USAGE_LOG": str(self.paths["usage_ledger"]),
            "CHAT2API_SERVICE_LOG": str(self.paths["chat2api_log"]),
            "CHAT2API_CONTROL_TOKEN_FILE": str(self.paths["chat2api_control"]),
            "CHAT2API_REAUTH_SLOT_FILE": str(self.paths["chat2api_reauth"]),
            "CHAT2API_SHARE_REVOKE_STATE_FILE": str(
                self.paths["revoke_state"]),
            "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": str(
                self.paths["attribution"]),
            "CHAT2API_SHARE_SECRET_KEY_FILE": str(self.paths["secret_key"]),
            "CHAT2API_PRICE_TABLE_FILE": str(self.paths["price_table"]),
            "CHAT2API_UPSTREAM_AUTO_RETRY": "0",
            "CHAT2API_UPSTREAM_AUTO_RETRIES": "0",
            "CHAT2API_REPETITION_AUTO_RETRY": "0",
            "CHAT2API_REPETITION_AUTO_RETRIES": "0",
            "CHAT2API_AUTH_WAIT_BUDGET_S": "5",
            "CHAT2API_MAX_REQUEST_SECONDS": "90",
            "CHAT2API_SHARE_DISPATCH_MAX_WAIT_S": "10",
            "SHARE_STORE_FILE": str(self.paths["store"]),
            "SHARE_MANAGEMENT_CONFIG": str(
                self.paths["management_config"]),
            "CONSOLE_SHARE_BACKEND_DIR": str(self.paths["backend_dir"]),
            "USAGE_CONTROL_TOKEN_FILE": str(self.paths["control_token"]),
            "USAGE_SESSION_CODE_FILE": str(self.paths["session_code"]),
            "USAGE_PRICE_FILE": str(self.paths["price_table"]),
            "USAGESRV_PORT": str(MANAGEMENT_PORT),
            "UM_SHARE_ROLE": "cloud",
            "CONSOLE_STATE_FILE": str(self.root / "console-state.sqlite3"),
            "CONSOLE_LOCK_DIR": str(self.root / "locks"),
            "CHAT2API_LAUNCH_SOURCE": "fixture",
            "CHAIN_TEST_RELAY_GATE_PORT": str(RELAY_GATE_PORT),
        })
        return env

    def _write_secret(self, path: Path, data: bytes, mode: int = 0o600):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with contextlib.suppress(OSError):
            os.chmod(path, mode)

    def prepare(self):
        for port in PORTS:
            if not _free_port(port):
                raise EnvironmentError(
                    f"port {port} is already in use before startup")
        for name in ("backend_dir", "broker_dir", "profile"):
            self.paths[name].mkdir(parents=True, exist_ok=True)
        run_dir = self.root / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(run_dir, 0o700)
        self._write_secret(self.paths["attribution"],
                           secrets.token_urlsafe(48).encode("utf-8"))
        self._write_secret(self.paths["instance_key"],
                           secrets.token_urlsafe(32).encode("utf-8"))
        self._write_secret(self.paths["broker_key"],
                           secrets.token_urlsafe(32).encode("utf-8"))
        self._write_secret(self.paths["secret_key"], os.urandom(32))
        self._write_secret(self.paths["control_token"],
                           secrets.token_urlsafe(24).encode("utf-8"))
        self._write_secret(self.paths["price_table"],
                           b'{"version":"fixture","models":{}}')
        self.paths["management_config"].write_text(json.dumps({
            "schema_version": 2,
            "enabled": True,
            "transport": "unix_socket",
            "socket_path": str(self.paths["broker_socket"]),
            "control_key_file": str(self.paths["broker_key"]),
        }, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(self.paths["management_config"], 0o600)

        # The initial projection comes from the REAL store implementation.
        share_store = _load_module("share_store", HERE / "share/store.py")
        store = share_store.ShareStore(str(self.paths["store"]))
        try:
            projection = store.render_projection()
        finally:
            store.close()
        self.paths["projection"].write_text(
            json.dumps(projection, ensure_ascii=False, indent=2),
            encoding="utf-8")
        # The chain runs the REAL Linux product entry
        # (share/broker_ops.py::linux_main): build_linux_server assembles the
        # AF_UNIX transport WITH the same management dispatch the Windows
        # ManagementPipeServer uses. No fixture subclass or monkeypatch is
        # involved, so this exercises the production assembly end to end.
        self._write_dispatch_entry()

    def _write_dispatch_entry(self):
        """Write test-only backend and share-gateway launchers with gates.

        The backend launcher starts the REAL ``chat2api.main()``; it only wraps
        the REAL ``ShareDispatcher`` registration/poll timing so several real HTTP
        requests can be registered before the first grant (rotation) and so a
        pre-registered blocker can hold a real request at the cumulative budget
        (timeout). The share launcher starts the REAL
        ``share_entry.main()``; it only holds the upstream send when the relay
        gate is armed, so the R-C5 chain can revoke a Key in the 8100->8110
        window. Neither fakes a grant, commit, release or admission decision.
        """
        self.paths["dispatch_entry"].write_text(
            DISPATCH_ENTRY_SOURCE, encoding="utf-8")
        self.paths["relay_entry"].write_text(
            RELAY_ENTRY_SOURCE, encoding="utf-8")

    def _process(self, name, argv, log_path, env=None):
        process = ManagedProcess(name, argv, env or self.env, HERE, log_path)
        self.processes.append(process)
        return process

    def start_fake_um(self):
        module = _load_module("fake_um_server",
                              TESTS / "fake_um" / "fake_um_server.py")
        self.fake_um_module = module
        token = secrets.token_urlsafe(32)
        fake = module.FakeUM(port=FAKE_UM_PORT, token=token,
                            model=SHARE_MODEL)
        fake.start()
        # The 64-way scenario needs the accept backlog to exceed the
        # ThreadingHTTPServer default of 5; this is the test server's own
        # socket, not a product setting.
        fake._server.request_queue_size = 256
        self.fake_um = fake
        self.fake_um_token = token
        _wait_for(lambda: _free_port(FAKE_UM_PORT) is False, 5.0)

    def start_backend(self, *, env=None, entry=None, profile=None):
        argv = [
            sys.executable, str(entry or (HERE / "chat2api" / "chat2api.py")),
            "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
            "--base-url", f"http://127.0.0.1:{FAKE_UM_PORT}",
            "--token", self.fake_um_token,
            "--no-browser",
            "--profile", str(profile or self.paths["profile"]),
        ]
        self.backend = self._process("chat2api", argv,
                                     self.paths["chat2api_stdout"],
                                     env=env or self.env)
        self.backend.start()
        _wait_for(self._backend_ready, 30.0)

    def _backend_ready(self):
        result = _http_request(BACKEND_PORT, "GET", "/ready", timeout=2.0)
        return result.status == 200

    def start_share(self):
        argv = [
            sys.executable, str(self.paths["relay_entry"]),
            "--port", str(SHARE_PORT),
            "--upstream-port", str(BACKEND_PORT),
            "--config", str(self.paths["projection"]),
            "--auth-mode", "projection",
            "--attribution-key-file", str(self.paths["attribution"]),
            "--instance-key-file", str(self.paths["instance_key"]),
            "--registration-file", str(self.paths["registration"]),
            "--instance-pipe-prefix", str(self.paths["instance_prefix"]),
            "--fault-log", str(self.paths["share_faults"]),
        ]
        self.share = self._process("share-gateway", argv,
                                    self.paths["share_stdout"])
        self.share.start()
        _wait_for(lambda: self.paths["registration"].exists(), 20.0)
        _wait_for(self._share_ready, 20.0)
        _wait_for(lambda: self.relay_gate_call(
            "GET", "/gate/state").status == 200, 20.0)

    def _share_ready(self):
        result = _http_request(SHARE_PORT, "GET", "/v1/models", key="probe",
                               timeout=1.0)
        return result.status == 401

    def relay_gate_call(self, method, path, body=None):
        return _http_request(RELAY_GATE_PORT, method, path, body=body,
                             timeout=30.0)

    def _wait_relay_gate(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            result = self.relay_gate_call("GET", "/gate/state")
            last = result.json() if result.status == 200 else {}
            if predicate(last or {}):
                return last
            time.sleep(0.05)
        raise TimeoutError("relay gate state did not match: %r" % (last,))

    def start_broker(self):
        pid = self.share.process.pid
        self.paths["host_pid"].write_text(str(pid), encoding="ascii")
        argv = [
            sys.executable, str(HERE / "share/broker_ops.py"),
            "--socket-path", str(self.paths["broker_socket"]),
            "--key-file", str(self.paths["broker_key"]),
            "--instance-key-file", str(self.paths["instance_key"]),
            "--registration-file", str(self.paths["registration"]),
            "--instance-socket-prefix", str(self.paths["instance_prefix"]),
            "--projection-path", str(self.paths["projection"]),
            "--broker-dir", str(self.paths["broker_dir"]),
            "--host-pid-file", str(self.paths["host_pid"]),
            "--state-dir", str(self.paths["desired_state"]),
            "--expected-uid", str(os.getuid()),
            "--expect-instance-image", sys.executable,
            "--expect-instance-cmdline-marker", "relay_entry.py",
            "--expect-instance-port", str(SHARE_PORT),
            "--deadline", "10",
            "--dispatch-deadline", "30",
            # REMAINDER: every remaining token is the host command, so this
            # option must stay last in the argv.
            "--host-command", sys.executable,
            str(self.paths["relay_entry"]),
        ]
        self.broker = self._process("broker", argv,
                                      self.paths["broker_stdout"])
        self.broker.start()
        self.notes.append(
            "Broker started through the REAL product entry "
            "share/broker_ops.py::linux_main; build_linux_server assembled "
            "the AF_UNIX transport with the management dispatch (no fixture "
            "subclass or monkeypatch).")
        _wait_for(lambda: self.paths["broker_socket"].exists(), 20.0)

    def start_management(self):
        argv = [sys.executable, str(HERE / "usagesrv.py")]
        self.management_process = self._process("usagesrv", argv,
                                        self.paths["management_stdout"])
        self.management_process.start()
        _wait_for(self._management_ready, 20.0)

    def start_dispatch_backend(self, max_wait=None):
        """Restart the REAL backend with the test-only dispatch poll gate.

        Only the backend process changes: it still runs ``chat2api.main()`` on the
        same port and profile. ``CHAT2API_SHARE_DISPATCH_MAX_WAIT_S`` is shortened
        so the cumulative timeout path is observable without waiting 10 seconds; a
        scenario that must observe the PENDING ticket across a management revoke
        passes a longer budget instead.
        """
        if self.backend is not None:
            self.backend.stop()
        env = dict(self.env)
        env.update({
            "CHAT2API_SHARE_DISPATCH_MAX_WAIT_S": str(
                DISPATCH_MAX_WAIT_SHORT_S if max_wait is None else max_wait),
            "CHAIN_TEST_DISPATCH_GATE_PORT": str(DISPATCH_GATE_PORT),
            "CHAIN_TEST_DISPATCH_GATE_STATE":
                str(self.paths["dispatch_gate_state"]),
        })
        with contextlib.suppress(OSError):
            self.paths["dispatch_gate_state"].unlink()
        self.start_backend(env=env, entry=self.paths["dispatch_entry"])
        self.dispatch_gate_state = _wait_for(
            self._read_dispatch_gate, 20.0)
        _wait_for(lambda: self.dispatch_gate_call(
            "GET", "/gate/state").status == 200, 20.0)

    def _read_dispatch_gate(self):
        try:
            return json.loads(self.paths["dispatch_gate_state"].read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def dispatch_gate_call(self, method, path, body=None):
        return _http_request(DISPATCH_GATE_PORT, method, path, body=body,
                             timeout=30.0)

    def fake_um_barrier(self, method, path, body=None):
        return _http_request(FAKE_UM_PORT, method, path, body=body, timeout=30.0)

    def _dispatch_gate(self):
        result = self.dispatch_gate_call("GET", "/gate/state")
        return result.json() if result.status == 200 else None

    def _wait_dispatch_gate(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self._dispatch_gate() or {}
            if predicate(last):
                return last
            time.sleep(0.05)
        raise TimeoutError("dispatch gate state did not match: %r" % (last,))

    def _wait_dispatcher_stats(self, predicate, timeout=20.0):
        """Wait for the REAL dispatcher counters reported by the gate."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            state = self._dispatch_gate() or {}
            last = state.get("dispatcher") or {}
            if predicate(last):
                return last
            time.sleep(0.05)
        raise TimeoutError(
            "dispatcher stats did not match: %r" % (last,))

    def _revoked_key_pending_tickets(self, key_id):
        """The REAL dispatcher's live PENDING/GRANTED tickets for ONE key."""
        state = self._dispatch_gate() or {}
        return [ticket for ticket in state.get("tickets", [])
                if ticket.get("keyId") == key_id
                and ticket.get("state") in ("pending", "granted")]

    def _wait_revoked_key_released(self, key_id, timeout=20.0):
        """Wait until the revoked Key owns no PENDING/GRANTED ticket."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self._revoked_key_pending_tickets(key_id)
            if not last:
                return []
            time.sleep(0.05)
        raise TimeoutError(
            "the revoked Key still owns PENDING/GRANTED tickets: %r"
            % (last,))

    def instance_status(self):
        """Read the REAL 8100 instance status through its instance channel.

        This is a read-only signed request with the instance key; it never changes
        admission and gives the live per-key ``active_count`` that the revoke's
        socket close must eventually release.
        """
        registration = json.loads(
            self.paths["registration"].read_text(encoding="utf-8"))
        secret = self.paths["instance_key"].read_text(
            encoding="utf-8").strip()
        # The test process runs from tests/, so the product module's own
        # sibling import needs the protocol module registered first.
        _load_module("share_broker_protocol",
                     HERE / "share/broker_protocol.py")
        module = _load_module("share_broker_client",
                              HERE / "share/broker_client.py")
        transport = module.UnixSocketBrokerTransport(
            registration["pipe_name"])
        client = module.BrokerClient(lambda: transport, secret)
        result = client.call("instance-status",
                             instance_id=registration["instance_id"])
        if not result.ok:
            return {"ok": False, "error_code": result.error_code}
        payload = dict(result.result or {})
        payload["ok"] = True
        return payload

    def _fake_um_barrier_state(self):
        result = self.fake_um_barrier("GET", "/__control/barrier")
        return result.json() if result.status == 200 else None

    def _wait_fake_um_barrier(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self._fake_um_barrier_state() or {}
            if predicate(last):
                return last
            time.sleep(0.05)
        raise TimeoutError(
            "fake UM barrier state did not match: %r" % (last,))

    def _management_ready(self):
        result = _http_request(
            MANAGEMENT_PORT, "GET", "/api/share/status",
            headers={"X-Admin-Control": self.control_token}, timeout=2.0)
        return result.status == 200

    @property
    def control_token(self) -> str:
        return self.paths["control_token"].read_text(
            encoding="utf-8").strip()

    # ------------------------------------------------------------ teardown
    def stop_all(self):
        for process in reversed(self.processes):
            process.stop()
        if self.fake_um is not None:
            self.fake_um.stop()

    # ------------------------------------------------------------ helpers
    def management_call(self, method, path, body=None):
        result = _http_request(
            MANAGEMENT_PORT, method, path, body=body,
            headers={"X-Admin-Control": self.control_token}, timeout=30.0)
        return result

    def get_key(self, key_id: str) -> dict:
        result = self.management_call("GET", f"/api/share/keys/{key_id}")
        if result.status != 200:
            raise RuntimeError(
                f"get_key {key_id} failed: {result.status} {result.text()}")
        return result.json()

    def create_key(self, name: str, max_concurrency: int = 17) -> dict:
        result = self.management_call(
            "POST", "/api/share/keys",
            {"name": name, "max_concurrency": max_concurrency})
        if result.status != 201:
            raise RuntimeError(
                f"create_key {name} failed: {result.status} {result.text()}")
        self.last_management_result = result
        payload = result.json()
        payload.update(self.get_key(payload["key_id"]))
        return payload

    def reveal_key(self, key_id: str, expected_row_version: int) -> dict:
        result = self.management_call(
            "GET", f"/api/console/share-management/keys/{key_id}/secret"
            f"?expected_row_version={expected_row_version}")
        if result.status != 200:
            raise RuntimeError(
                f"reveal_key {key_id} failed: {result.status} "
                f"{result.text()}")
        self.last_management_result = result
        return result.json()

    def rotate_key(self, key_id: str, expected: int) -> dict:
        result = self.management_call(
            "POST", f"/api/share/keys/{key_id}/rotate",
            {"expected_row_version": expected})
        if result.status != 200:
            raise RuntimeError(
                f"rotate_key {key_id} failed: {result.status} {result.text()}")
        self.last_management_result = result
        payload = result.json()
        payload.update(self.get_key(key_id))
        return payload

    def rotate_key_expect(self, key_id: str, expected: int) -> dict:
        """Rotate WITHOUT raising: the S2 security fence answers 503 when the
        runtime ACK cannot confirm the revoke/rotate intent; the test verifies
        the fail-closed contract instead of pretending a success."""
        result = self.management_call(
            "POST", f"/api/share/keys/{key_id}/rotate",
            {"expected_row_version": expected})
        self.last_management_result = result
        return result.json()

    def revoke_key(self, key_id: str, expected: int) -> dict:
        result = self.management_call(
            "POST", f"/api/share/keys/{key_id}/revoke-and-terminate",
            {"expected_row_version": expected})
        if result.status != 200:
            raise RuntimeError(
                f"revoke_key {key_id} failed: {result.status} "
                f"{result.text()}")
        self.last_management_result = result
        return result.json()

    def set_share_max(self, value: int) -> dict:
        result = self.management_call(
            "PATCH", "/api/share/settings",
            {"share_max_concurrency": value})
        if result.status != 200:
            raise RuntimeError(
                f"set_share_max {value} failed: {result.status} "
                f"{result.text()}")
        self.last_management_result = result
        return result.json()

    def chat(self, key, *, stream, prompt="hello", timeout=30.0):
        return _http_request(
            SHARE_PORT, "POST", "/v1/chat/completions",
            body={"model": SHARE_MODEL, "stream": stream,
                  "messages": [{"role": "user", "content": prompt}]},
            key=key, timeout=timeout, stream=stream)

    def messages(self, key, *, stream, prompt="hello", timeout=30.0):
        return _http_request(
            SHARE_PORT, "POST", "/v1/messages",
            body={"model": SHARE_MODEL, "stream": stream,
                  "max_tokens": 64,
                  "messages": [{"role": "user", "content": prompt}]},
            key=key, timeout=timeout, stream=stream)

    def responses(self, key, *, stream=False, timeout=30.0,
                  input_items=None):
        if input_items is None:
            input_items = [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "tool"}]},
                {"type": "function_call", "call_id": "call_1",
                 "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "42"},
            ]
        return _http_request(
            SHARE_PORT, "POST", "/v1/responses",
            body={
                "model": SHARE_MODEL,
                "stream": stream,
                "input": input_items,
            },
            key=key, timeout=timeout, stream=stream)

    def usage(self):
        return self.management_call("GET", "/api/share/usage")

    # ------------------------------------------------------------ scenarios
    def scenario_models_normal(self):
        scenario = Scenario("models_normal")
        self.scenarios.append(scenario)
        try:
            result = _http_request(
                SHARE_PORT, "GET", "/v1/models", key=self.keys["A"]["secret"])
            scenario.request(result, "normal catalog")
            payload = result.json() or {}
            ids = [m.get("id") for m in payload.get("data", [])
                   if isinstance(m, dict)]
            scenario.check(result.status == 200, "GET /v1/models returns 200")
            scenario.check(SHARE_MODEL in ids,
                           "catalog contains the fake UM model")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_models_stale_cache(self):
        scenario = Scenario("models_stale_cache_fail_closed")
        self.scenarios.append(scenario)
        try:
            self.fake_um.set_mode(self.fake_um_module.MODE_EXPLICIT_5XX)
            result = _http_request(
                SHARE_PORT, "GET", "/v1/models", key=self.keys["A"]["secret"])
            scenario.request(result, "upstream 500 after a cached catalog")
            payload = result.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            scenario.check(result.status in (502, 503),
                           "stale cached catalog is never served as fresh")
            scenario.check(code in ("um_unavailable",
                                   "upstream_catalog_freshness_unconfirmed"),
                           "failure names the upstream freshness problem")
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            again = _http_request(
                SHARE_PORT, "GET", "/v1/models", key=self.keys["A"]["secret"])
            scenario.request(again, "upstream restored")
            scenario.check(again.status == 200,
                           "catalog recovers after the upstream returns")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            return scenario.finish(exc)

    def scenario_chat_stream(self):
        scenario = Scenario("chat_stream")
        self.scenarios.append(scenario)
        try:
            result = self.chat(self.keys["A"]["secret"], stream=True,
                               prompt="stream hello")
            scenario.request(result, "chat stream")
            text = result.text()
            deltas = _sse_text(result.body)
            scenario.check(result.status == 200, "chat stream returns 200")
            scenario.check("data:" in text and "[DONE]" in text,
                           "chat stream has data events and [DONE]")
            scenario.check("stream hello" in deltas,
                           "chat stream carries the upstream reply")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_chat_nonstream(self):
        scenario = Scenario("chat_nonstream")
        self.scenarios.append(scenario)
        try:
            result = self.chat(self.keys["A"]["secret"], stream=False,
                               prompt="nonstream hello")
            scenario.request(result, "chat non-stream")
            payload = result.json() or {}
            content = ""
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                pass
            scenario.check(result.status == 200, "chat non-stream returns 200")
            scenario.check("nonstream hello" in content,
                           "chat non-stream carries the upstream reply")
            usage = payload.get("usage") if isinstance(payload, dict) else None
            scenario.check(isinstance(usage, dict) and usage.get("total_tokens", 0) > 0,
                           "chat non-stream carries usage")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_messages_stream(self):
        scenario = Scenario("messages_stream")
        self.scenarios.append(scenario)
        try:
            result = self.messages(self.keys["A"]["secret"], stream=True,
                                    prompt="messages hello")
            scenario.request(result, "messages stream")
            text = result.text()
            deltas = _sse_text(result.body)
            scenario.check(result.status == 200, "messages stream returns 200")
            scenario.check("event: message_start" in text,
                           "messages stream has message_start")
            scenario.check("event: content_block_delta" in text,
                           "messages stream has content_block_delta")
            scenario.check("event: message_stop" in text,
                           "messages stream has message_stop")
            scenario.check("messages hello" in deltas,
                           "messages stream carries the upstream reply")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_responses_tool_result(self):
        scenario = Scenario("responses_tool_result")
        self.scenarios.append(scenario)
        try:
            # Positive: capture the REAL upstream request body at the fake UM.
            self.fake_um.reset()
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            positive = {}

            def send_positive():
                positive["value"] = self.responses(
                    self.keys["A"]["secret"], stream=False)

            positive_thread = threading.Thread(target=send_positive,
                                               daemon=True)
            positive_thread.start()
            state = self._wait_fake_um_barrier(
                lambda item: len(item.get("arrivals", [])) == 1)
            arrival = state["arrivals"][0]
            body = arrival.get("body") or {}
            errors = _tool_turn_errors(body, "call_1", "42")
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            positive_thread.join(timeout=30)
            result = positive.get("value")
            scenario.request(result, "responses with function_call_output")
            scenario.check(
                result is not None and result.status == 200,
                "responses returns 200")
            scenario.check(
                not errors,
                "the upstream tool turn is linked by call_id and structure"
                + ("" if not errors else ": " + "; ".join(errors)))
            scenario.check(
                arrival.get("protocol") == "chat"
                and arrival.get("path") == "/api/chat/completions",
                "the tool turn reaches the chat upstream")
            scenario.record_evidence("toolTurn", {
                "arrival": arrival,
                "errors": errors,
            })
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})

            # Negative control (actual contract): an output whose call_id has no
            # function_call is a broken tool turn. The production translation
            # forwards it faithfully instead of silently dropping or repairing
            # it, so the structure check MUST detect the broken turn in the
            # real upstream body. This control also proves the strict rejection
            # predicate below is falsifiable: if production forwarded a broken
            # structure, that strict assertion would FAIL.
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            broken = {}

            def send_broken():
                broken["value"] = self.responses(
                    self.keys["A"]["secret"], stream=False,
                    input_items=[
                        {"type": "message", "role": "user",
                         "content": [{"type": "input_text",
                                      "text": "tool"}]},
                        {"type": "function_call_output",
                         "call_id": "call_missing", "output": "42"},
                    ])

            broken_thread = threading.Thread(target=send_broken, daemon=True)
            broken_thread.start()
            broken_state = self._wait_fake_um_barrier(
                lambda item: len(item.get("arrivals", [])) == 1)
            broken_arrivals = broken_state.get("arrivals", [])
            broken_errors = _tool_turn_errors(
                broken_arrivals[0].get("body") or {},
                "call_missing", "42")
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            broken_thread.join(timeout=30)
            broken_result = broken.get("value")
            scenario.request(broken_result,
                              "unlinked function_call_output (forwarded)")
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            scenario.check(
                broken_result is not None
                and broken_result.status == 200
                and bool(broken_arrivals)
                and bool(broken_errors),
                "the actual contract forwards the broken tool turn faithfully "
                "and the structure check detects it")
            scenario.check(
                not _strict_tool_turn_rejection(
                    broken_result, broken_arrivals),
                "falsifiability control: a forwarded broken structure FAILS "
                "the strict rejection predicate")
            scenario.record_evidence("toolTurnForwardedNegative", {
                "status": None if broken_result is None
                          else broken_result.status,
                "arrivals": len(broken_arrivals),
                "errors": broken_errors,
                "strictPredicate": _strict_tool_turn_rejection(
                    broken_result, broken_arrivals),
            })

            # Strict negative (review #7): a function_call_output whose
            # call_id is not a string translates to no chat message at all, so
            # the product refuses the request with a structured 400 BEFORE any
            # upstream send. BOTH conditions are required: a 400 structured
            # refusal AND zero fake UM arrivals. If production forwarded this
            # broken structure, this assertion would fail.
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            strict = {}

            def send_strict():
                strict["value"] = self.responses(
                    self.keys["A"]["secret"], stream=False,
                    input_items=[
                        {"type": "function_call_output",
                         "call_id": 123, "output": "42"},
                    ])

            strict_thread = threading.Thread(target=send_strict, daemon=True)
            strict_thread.start()
            strict_thread.join(timeout=30)
            strict_result = strict.get("value")
            scenario.request(strict_result,
                              "malformed function_call_output (strict)")
            # The refusal is synchronous and precedes any upstream call; a
            # bounded settle window makes a late asynchronous send visible.
            time.sleep(0.3)
            strict_arrivals = (
                self._fake_um_barrier_state() or {}).get("arrivals", [])
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            scenario.check(
                _strict_tool_turn_rejection(strict_result, strict_arrivals),
                "a structurally broken tool result is refused with a "
                "structured 400 and never forwarded (status=%r, arrivals=%r)"
                % (None if strict_result is None else strict_result.status,
                   len(strict_arrivals)))
            scenario.record_evidence("toolTurnStrictNegative", {
                "status": None if strict_result is None
                          else strict_result.status,
                "error": (None if strict_result is None
                          else strict_result.json()),
                "arrivals": len(strict_arrivals),
            })
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/disarm", {})
            return scenario.finish(exc)

    def scenario_invalid_key(self):
        scenario = Scenario("invalid_key_401")
        self.scenarios.append(scenario)
        try:
            result = self.chat("not-a-valid-key", stream=False)
            scenario.request(result, "invalid key")
            payload = result.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            scenario.check(result.status == 401, "invalid key returns 401")
            scenario.check(code == "invalid_api_key",
                           "invalid key names invalid_api_key")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_management_chain(self):
        scenario = Scenario("management_create_publish_load_admit")
        self.scenarios.append(scenario)
        try:
            created = self.create_key("mgmt-key", max_concurrency=17)
            scenario.request(self.last_management_result,
                             "create Key via management API")
            scenario.check(created.get("published") is True,
                           "create confirms the projection was published")
            scenario.check(created.get("in_sync") is True,
                           "create reports the desired generation is applied")
            self._last_key = created
            self.keys["MGMT"] = created
            result = self.chat(created["secret"], stream=False,
                               prompt="management hello")
            scenario.request(result, "admit after create")
            scenario.check(result.status == 200,
                           "the newly created Key is admitted by the running 8100")
            projection = json.loads(
                self.paths["projection"].read_text(encoding="utf-8"))
            scenario.check(projection.get("config_generation")
                            == created.get("desired_generation"),
                           "the projection file carries the published generation")
            scenario.check(any(entry.get("key_id") == created["key_id"]
                               for entry in projection.get("data", [])
                               if isinstance(entry, dict)),
                           "the projection file carries the new Key")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_management_rotate(self):
        scenario = Scenario("management_rotate_immediate")
        self.scenarios.append(scenario)
        try:
            created = self.keys.get("MGMT") or self._last_key
            if not created:
                scenario.skip("management create did not complete")
                return scenario.finish()
            # A rotate on a RUNNING share is genuinely applied by the REAL
            # 8110 runtime: the projection carries the new secret_version and
            # the runtime ACK covers that applied generation, so the management
            # confirms it (200) instead of pretending a failure.
            rotated = self.rotate_key_expect(created["key_id"],
                                             created["row_version"])
            scenario.request(self.last_management_result,
                             "rotate Key via management API")
            scenario.check(rotated.get("published") is True,
                           "the rotate confirms the projection was published")
            scenario.check(rotated.get("in_sync") is True,
                           "the runtime ACK covers the applied rotate generation")
            row = self.get_key(created["key_id"])
            scenario.check(row.get("row_version") == created["row_version"] + 1,
                           "the confirmed rotate committed the Key row")
            revealed = self.reveal_key(created["key_id"], row["row_version"])
            scenario.request(self.last_management_result,
                             "reveal the committed new secret")
            first_secret = revealed.get("secret")
            scenario.check(isinstance(first_secret, str)
                           and first_secret.startswith("um-share-"),
                           "the reveal route returns the committed new secret")
            old = self.chat(created["secret"], stream=False,
                             prompt="old secret after rotate")
            new = self.chat(first_secret, stream=False,
                             prompt="new secret after rotate")
            scenario.request(old, "old secret after the confirmed rotate")
            scenario.request(new, "new secret after the confirmed rotate")
            scenario.check(old.status == 401,
                           "the old Key secret is rejected immediately")
            scenario.check(new.status == 200,
                           "the rotated Key secret is admitted immediately")

            # Fail-closed: with the REAL 8100 stopped the rotate mutation
            # still commits, but its projection cannot be published.  The
            # management call answers 503 and the intent stays pending; the
            # share must NOT pretend the rotate was applied.
            self.share.stop()
            failed = self.rotate_key_expect(created["key_id"],
                                             row["row_version"])
            scenario.request(self.last_management_result,
                             "rotate Key while the 8100 is stopped (fail-closed)")
            scenario.check(self.last_management_result.status == 503,
                           "an unconfirmed rotate security intent fails closed")
            scenario.check("revoking operation not applied online"
                           in (failed.get("error") or ""),
                           "the failure names the unconfirmed revoke/rotate")
            status = self.management_call("GET", "/api/share/status")
            scenario.request(status, "status after the failed rotate")
            status_payload = status.json() or {}
            scenario.check(status_payload.get("security_pending") == 1,
                           "the rotate intent stays pending, never "
                           "falsely confirmed")
            scenario.check(status_payload.get("in_sync") is False,
                           "the failed publication is never reported in sync")
            # The store transaction already committed the second secret
            # version; the narrow reveal route returns it without a second
            # mutation.
            row = self.get_key(created["key_id"])
            scenario.check(row.get("row_version") == created["row_version"] + 2,
                           "the failed publication still committed the Key row")
            revealed = self.reveal_key(created["key_id"], row["row_version"])
            scenario.request(self.last_management_result,
                             "reveal the committed second secret")
            second_secret = revealed.get("secret")
            scenario.check(isinstance(second_secret, str)
                           and second_secret.startswith("um-share-"),
                           "the reveal route returns the committed second secret")
            # Recover the share the fail-closed emergency stop stopped: a
            # fresh REAL share process loads the committed projection at the
            # rotate generation, so the first rotated secret is gone and the
            # second one is admitted.  This is the operator recovery path,
            # not a faked ACK.
            self.share.stop()
            self.start_share()
            first = self.chat(first_secret, stream=False,
                             prompt="first rotated secret")
            second = self.chat(second_secret, stream=False,
                             prompt="second rotated secret")
            scenario.request(first, "first rotated secret after recovery")
            scenario.request(second, "second rotated secret after recovery")
            scenario.check(first.status == 401,
                           "the superseded rotated secret is rejected")
            scenario.check(second.status == 200,
                           "the latest rotated secret is admitted immediately")
            self.keys["MGMT"] = dict(row, secret=second_secret)
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_management_revoke(self):
        scenario = Scenario("management_revoke_immediate")
        self.scenarios.append(scenario)
        try:
            created = self.keys.get("MGMT")
            if not created:
                scenario.skip("management create did not complete")
                return scenario.finish()
            current = self.get_key(created["key_id"])
            revoked = self.revoke_key(created["key_id"],
                                      current["row_version"])
            scenario.request(self.last_management_result,
                             "revoke Key via management API")
            scenario.check(revoked.get("published") is True,
                           "revoke confirms the projection was published")
            result = self.chat(created["secret"], stream=False,
                               prompt="revoked hello")
            scenario.request(result, "revoked Key")
            scenario.check(result.status == 401,
                           "the revoked Key is rejected immediately")
            projection = json.loads(
                self.paths["projection"].read_text(encoding="utf-8"))
            revoked_entry = next((
                entry for entry in projection.get("data", [])
                if isinstance(entry, dict)
                and entry.get("key_id") == created["key_id"]), None)
            scenario.check(revoked_entry is not None
                           and revoked_entry.get("enabled") is False,
                           "the revoked Key is present but disabled in the "
                           "live projection")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_rate_limit(self):
        scenario = Scenario("per_key_rate_limit_20")
        self.scenarios.append(scenario)
        try:
            key = self.create_key("rate-key", max_concurrency=17)
            self.keys["RATE"] = key
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            ok = 0
            for index in range(20):
                result = self.chat(key["secret"], stream=False,
                                   prompt=f"rate {index}")
                scenario.request(result, f"request {index + 1}/20")
                if result.status == 200:
                    ok += 1
            scenario.check(ok == 20, "the first 20 requests in one minute pass")
            rejected = self.chat(key["secret"], stream=False,
                                prompt="rate 21")
            scenario.request(rejected, "request 21/20")
            payload = rejected.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            reason = error.get("reason") if isinstance(error, dict) else None
            scenario.check(rejected.status == 429,
                           "the 21st request is rejected")
            scenario.check(reason == "key_rate_limit",
                           "the 21st rejection names the per-Key rate limit")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_per_key_concurrency(self):
        scenario = Scenario("per_key_concurrency_17")
        self.scenarios.append(scenario)
        try:
            key = self.create_key("key-concurrency", max_concurrency=17)
            self.keys["CONC"] = key
            self.fake_um.reset()
            self.fake_um.config.slow_first_chunk_s = 3.0
            self.fake_um.set_mode(self.fake_um_module.MODE_SLOW_FIRST_CHUNK)
            threads = []
            results = []
            lock = threading.Lock()

            def worker(index):
                result = self.chat(key["secret"], stream=True,
                                    prompt=f"concurrency {index}",
                                    timeout=60.0)
                with lock:
                    results.append(result)

            for index in range(17):
                thread = threading.Thread(target=worker, args=(index,),
                                          daemon=True)
                threads.append(thread)
                thread.start()
                time.sleep(0.01)
            _wait_for(lambda: self._chat_count() >= 17, 30.0)
            rejected = self.chat(key["secret"], stream=False,
                                 prompt="concurrency 18")
            scenario.request(rejected, "18th request")
            payload = rejected.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            reason = error.get("reason") if isinstance(error, dict) else None
            scenario.check(rejected.status == 429,
                            "the 18th concurrent request is rejected")
            scenario.check(reason == "per_key_concurrency",
                            "the 18th rejection names the per-Key concurrency")
            for thread in threads:
                thread.join(timeout=60)
            scenario.check(all(not thread.is_alive() for thread in threads),
                            "the 17 admitted streams finish")
            scenario.check(all(result.status == 200 for result in results),
                            "all 17 admitted streams return 200")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_global_concurrency(self):
        scenario = Scenario("global_concurrency_64_65")
        self.scenarios.append(scenario)
        try:
            keys = [self.create_key(f"global-{index}", max_concurrency=17)
                    for index in range(8)]
            self.keys["GLOBAL"] = keys
            settings = self.set_share_max(64)
            scenario.check(settings.get("published") is True,
                            "the global limit 64 is published and loaded")
            self.fake_um.reset()
            self.fake_um.config.slow_first_chunk_s = 4.0
            self.fake_um.set_mode(self.fake_um_module.MODE_SLOW_FIRST_CHUNK)
            threads = []
            results = []
            lock = threading.Lock()

            def worker(key, index):
                result = self.chat(key["secret"], stream=True,
                                    prompt=f"global {index}",
                                    timeout=90.0)
                with lock:
                    results.append(result)

            for round_index in range(8):
                for key_index, key in enumerate(keys):
                    thread = threading.Thread(
                        target=worker, args=(key, round_index), daemon=True)
                    threads.append(thread)
                    thread.start()
                    # The product listener keeps a small accept backlog; this
                    # pacing avoids a pure client-side connection-refused race
                    # while every request is still genuinely in flight.
                    time.sleep(0.005)
            _wait_for(lambda: self._chat_count() >= 64, 60.0)
            rejected = self.chat(keys[0]["secret"], stream=False,
                                 prompt="global 65")
            scenario.request(rejected, "65th request")
            payload = rejected.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            reason = error.get("reason") if isinstance(error, dict) else None
            scenario.check(rejected.status == 429,
                            "the 65th request is rejected")
            scenario.check(reason == "share_concurrency",
                            "the 65th rejection names the global limit")
            for thread in threads:
                thread.join(timeout=90)
            scenario.check(all(not thread.is_alive() for thread in threads),
                            "all 64 admitted streams finish")
            scenario.check(all(result.status == 200 for result in results),
                            "all 64 admitted streams return 200")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_dispatch_rotation(self):
        scenario = Scenario("dispatch_rotation_real_http")
        self.scenarios.append(scenario)
        plan = [
            ("A", "chain key=A seq=1"),
            ("A", "chain key=A seq=2"),
            ("A", "chain key=A seq=3"),
            ("B", "chain key=B seq=1"),
            ("B", "chain key=B seq=2"),
        ]
        expected_registrations = ["A1", "A2", "A3", "B1", "B2"]
        expected_grants = ["A1", "B1", "A2", "B2", "A3"]
        try:
            self.start_dispatch_backend()
            scenario.note(
                "the REAL backend process serves every request; the test-only "
                "launcher gates only the REAL ShareDispatcher register/poll "
                "timing so five REAL HTTP requests can be registered before "
                "the first grant; the per-send hold is disarmed, so the five "
                "granted requests race to the REAL upstream concurrently; "
                "the fake UM arrival barrier holds and independently records "
                "the actual upstream requests, and the product's own "
                "requests.request boundary records the real send order; no "
                "grant, commit or release decision is faked")
            scenario.note(
                "the cumulative budget is shortened to %.1f s by the test-only "
                "launcher env; the production default stays 10 s"
                % DISPATCH_MAX_WAIT_SHORT_S)
            scenario.note(
                "the rotation is repeated 20 times with FRESH Keys per round so "
                "the per-Key 20/min admission never turns a repetition into a "
                "rate-limit failure")
            rounds = []
            for round_index in range(20):
                rounds.append(self._run_rotation_round(
                    round_index, plan, expected_registrations,
                    expected_grants))
            first = {key: value for key, value in rounds[0].items()
                     if key != "results"}
            last_round = rounds[-1]
            last_results = last_round["results"]
            last = {key: value for key, value in last_round.items()
                    if key != "results"}
            scenario.check(
                first["registrationOrder"] == expected_registrations,
                "the test deliberately registers A1,A2,A3,B1,B2, not the "
                "rotation order")
            scenario.check(
                first["grantOrder"] == expected_grants,
                "the REAL dispatcher grants by Key rotation and same-Key "
                "FIFO: %r" % (first["grantOrder"],))
            for label in ("A", "B"):
                scenario.check(
                    [marker for marker in first["grantOrder"]
                     if marker.startswith(label)]
                    == [marker for marker in expected_grants
                        if marker.startswith(label)],
                    "same-Key FIFO holds for Key %s grants: %r" % (
                        label, first["grantOrder"]))
            scenario.check(
                sorted(first["arrivalOrder"]) == sorted(expected_grants),
                "the independent fake UM arrival record observed all five "
                "markers: %r" % (first["arrivalOrder"],))
            scenario.check(
                sorted(first["sendOrder"]) == sorted(expected_grants),
                "the product-side REAL send record (requests.request, after "
                "commit) observed all five markers: %r"
                % (first["sendOrder"],))
            stability_failures = []
            for round in rounds:
                if round["grantOrder"] != expected_grants:
                    stability_failures.append(
                        "round %d grant %r" % (
                            round["round"], round["grantOrder"]))
                if sorted(round["sendOrder"]) != sorted(expected_grants):
                    stability_failures.append(
                        "round %d send markers %r" % (
                            round["round"], round["sendOrder"]))
                if sorted(round["arrivalOrder"]) != sorted(expected_grants):
                    stability_failures.append(
                        "round %d arrival markers %r" % (
                            round["round"], round["arrivalOrder"]))
                if any(status != 200 for status in round["statuses"].values()):
                    stability_failures.append(
                        "round %d statuses %r" % (
                            round["round"], round["statuses"]))
            scenario.record_evidence("rotationStability", {
                "rounds": len(rounds),
                "expected": expected_grants,
                "grantOrders": [round["grantOrder"]
                                for round in rounds],
                "arrivalOrders": [round["arrivalOrder"]
                                  for round in rounds],
                "sendOrders": [round["sendOrder"] for round in rounds],
                "sendArrivalEqual": [round["sendOrder"]
                                     == round["arrivalOrder"]
                                     for round in rounds],
                "statuses": [round["statuses"] for round in rounds],
                "durationsMs": [round["durationMs"] for round in rounds],
                "failures": stability_failures,
            })
            scenario.check(
                len(rounds) == 20 and not stability_failures,
                "20/20 REAL-HTTP rounds produced A1,B1,A2,B2,A3 in grant "
                "order; the product-side send record and the independent "
                "fake UM arrival record both contained all five markers "
                "(failures=%r)" % (stability_failures,))
            for marker, result in sorted(last_results.items()):
                scenario.request(result, "rotation %s (round 20)" % marker)
            scenario.record_evidence("rotation", first)

            # Negative: with the dispatcher disabled the same send order reaches
            # the upstream as A1,A2,A3,B1,B2, so the rotation assertion MUST
            # fail. This proves the positive assertion can fail.
            keys = last["keys"]
            self.dispatch_gate_call("POST", "/gate/disable")
            self.fake_um.reset()
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            negative_results = {}
            negative_lock = threading.Lock()

            def send_negative(label, marker):
                result = self.chat(keys[label]["secret"], stream=False,
                                     prompt=marker, timeout=60.0)
                with negative_lock:
                    negative_results[marker] = result

            negative_threads = []
            for label, marker in plan:
                thread = threading.Thread(
                    target=send_negative, args=(label, marker), daemon=True)
                negative_threads.append(thread)
                thread.start()
                self._wait_fake_um_barrier(
                    lambda state, count=len(negative_threads):
                        len(state.get("arrivals", [])) == count)
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            for thread in negative_threads:
                thread.join(timeout=60)
            negative_state = self._fake_um_barrier_state()
            negative_order = [
                "%s%d" % (entry.get("keyMarker"), entry.get("sequence"))
                for entry in negative_state.get("arrivals", [])]
            scenario.check(
                negative_order != expected_grants,
                "negative: with the dispatcher disabled the rotation order "
                "is not produced (%r)" % (negative_order,))
            scenario.record_evidence("rotationNegative", {
                "observed": negative_order,
                "expected": expected_grants,
                "statuses": [result.status
                             for result in negative_results.values()],
                "rotationHeld": negative_order == expected_grants,
            })
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            self.dispatch_gate_call("POST", "/gate/enable")
            self.dispatch_gate_call("POST", "/gate/disarm")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/enable")
            return scenario.finish(exc)

    def _run_rotation_round(self, round_index, plan,
                              expected_registrations, expected_grants):
        """Run ONE REAL-HTTP rotation round with fresh Keys.

        Review #11: every grant is recorded inside the REAL dispatcher's
        ``_grant_locked`` critical section, so the recorded grant order is the
        real grant order. Review #7: the per-send hold is disarmed for this
        round, so the five granted requests race to the REAL upstream; the send
        order comes from the product's own ``requests.request`` boundary (inside
        Proxy.call, after commit), and the fake UM arrival record is an
        independent cross-check. The caller repeats this round 20 times and fails
        the scenario when any round diverges from A1,B1,A2,B2,A3 in grant order
        or when the product-side send order disagrees with the fake UM arrival
        order.
        """
        started = time.monotonic()
        key_a = self.create_key(f"rotate-A-{round_index}",
                                 max_concurrency=17)
        key_b = self.create_key(f"rotate-B-{round_index}",
                                 max_concurrency=17)
        keys = {"A": key_a, "B": key_b}
        self.fake_um.reset()
        self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
        self.fake_um_barrier(
            "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
        self.dispatch_gate_call("POST", "/gate/arm")
        # Review #7: the per-send hold exists for the GRANTED-but-not-sent
        # scenarios; this rotation round disarms it so the five granted
        # requests reach the REAL upstream concurrently.
        self.dispatch_gate_call("POST", "/gate/send-disarm")
        results = {}
        results_lock = threading.Lock()
        threads = []

        def send(secret, marker):
            result = self.chat(secret, stream=False, prompt=marker,
                                 timeout=60.0)
            with results_lock:
                results[marker] = result

        try:
            # Registration order is deliberately NOT the rotation order: A1, A2,
            # A3, B1, B2. Each request is sent only after the previous one
            # registered at the REAL backend dispatcher.
            for index, (label, marker) in enumerate(plan):
                thread = threading.Thread(
                    target=send,
                    args=(keys[label]["secret"], marker), daemon=True)
                threads.append(thread)
                thread.start()
                expected = sum(
                    1 for item in plan[:index + 1] if item[0] == label)
                self._wait_dispatch_gate(
                    lambda state, label=label, expected=expected:
                        sum(1 for item in state.get("registrations", [])
                            if item.get("keyId")
                            == keys[label]["key_id"]) == expected)
            gate = self._wait_dispatch_gate(
                lambda state: len(state.get("registrations", [])) == 5)
            registration_markers = self._markers_for_grants(
                gate["registrations"], key_a, key_b)
            if registration_markers != expected_registrations:
                raise AssertionError(
                    "round %d registration order %r != %r" % (
                        round_index, registration_markers,
                        expected_registrations))
            self.dispatch_gate_call("POST", "/gate/release")
            gate = self._wait_dispatch_gate(
                lambda state: len(state.get("grants", [])) == 5)
            grant_markers = self._markers_for_grants(
                gate["grants"], key_a, key_b)
            if grant_markers != expected_grants:
                raise AssertionError(
                    "round %d grant order %r != %r" % (
                        round_index, grant_markers, expected_grants))
            # Review #7: the per-send gate is disarmed for this round, so the
            # five granted requests race to the REAL upstream. ``sends`` is
            # recorded by the product-side ``requests.request`` hook (inside
            # Proxy.call, after commit); it is not the test's release order.
            gate = self._wait_dispatch_gate(
                lambda state: len(state.get("sends", [])) == 5)
            send_markers = self._markers_for_sends(gate["sends"])
            if sorted(send_markers) != sorted(expected_grants):
                raise AssertionError(
                    "round %d send markers %r" % (
                        round_index, send_markers))
            barrier = self._wait_fake_um_barrier(
                lambda state: len(state.get("arrivals", [])) == 5)
            arrival_markers = [
                "%s%d" % (entry.get("keyMarker"), entry.get("sequence"))
                for entry in barrier["arrivals"]]
            if sorted(arrival_markers) != sorted(expected_grants):
                raise AssertionError(
                    "round %d arrival markers %r" % (
                        round_index, arrival_markers))
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            for thread in threads:
                thread.join(timeout=60)
            if any(thread.is_alive() for thread in threads):
                raise AssertionError(
                    "round %d: request threads still alive" % round_index)
            statuses = {
                marker: result.status
                for marker, result in results.items()
            }
            if len(statuses) != 5 or any(
                    status != 200 for status in statuses.values()):
                raise AssertionError(
                    "round %d statuses %r" % (round_index, statuses))
            self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 0
                and stats.get("keys") == 0)
            request_ids = {
                marker: result.headers.get("x-share-request-id")
                for marker, result in results.items()
            }
            if not all(
                    isinstance(value, str) and len(value) == 32
                    and all(ch in "0123456789abcdef" for ch in value)
                    for value in request_ids.values()):
                raise AssertionError(
                    "round %d request ids %r" % (round_index, request_ids))
            return {
                "round": round_index,
                "keys": keys,
                "registrationOrder": registration_markers,
                "grantOrder": grant_markers,
                "arrivalOrder": arrival_markers,
                "sendOrder": send_markers,
                "sendArrivalEqual": send_markers == arrival_markers,
                "statuses": statuses,
                "requestIds": request_ids,
                "grants": gate["grants"],
                "sends": gate["sends"],
                "arrivals": barrier["arrivals"],
                "results": results,
                "durationMs": _ms(started),
            }
        finally:
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/disarm", {})
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/disarm")
            for thread in threads:
                thread.join(timeout=5)

    @staticmethod
    def _markers_for_sends(sends):
        """Return the product-side send record in its REAL order.

        Review #7: ``sends`` is appended by the launcher's hook around
        ``requests.request`` inside the product's Proxy.call, so it already
        carries markers; no grant/release mapping is involved.
        """
        return list(sends)

    def _markers_for_grants(self, grants, key_a, key_b):
        """Map dispatcher grants back to the test markers A1..B2.

        Registration order is the deliberately non-rotating plan A1,A2,A3,B1,B2,
        so the n-th registration/grant of a Key maps to its n-th marker.
        """
        counters = {"A": 0, "B": 0}
        markers = []
        for grant in grants:
            label = ("A" if grant.get("keyId") == key_a["key_id"] else "B")
            counters[label] += 1
            markers.append("%s%d" % (label, counters[label]))
        return markers

    def scenario_dispatch_cumulative(self):
        scenario = Scenario("dispatch_cumulative_10s")
        self.scenarios.append(scenario)
        try:
            key = self.create_key("cumulative-key", max_concurrency=17)
            scenario.note(
                "the REAL backend process serves this request; a test-only "
                "blocker ticket is registered on the REAL dispatcher and never "
                "polled, so the request waits at the REAL send boundary and "
                "its shortened cumulative budget ends with a retryable 429; the "
                "fake UM arrival barrier proves the request never reached the "
                "upstream")
            self.dispatch_gate_call("POST", "/gate/disarm")
            # A test-only blocker ticket is registered on the REAL dispatcher
            # and never polled: the real request stays behind it and its real
            # cumulative budget (shortened to 1.5 s by the launcher env) ends
            # with a retryable timeout, never a late upstream send.
            blocked = self.dispatch_gate_call(
                "POST", "/gate/blocker",
                {"keyId": "blocked-key", "requestId": "blocked-request"})
            scenario.request(blocked, "register a dispatcher blocker ticket")
            scenario.check(blocked.status == 200,
                            "the dispatcher blocker ticket is registered")
            self.fake_um.reset()
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            started = time.monotonic()
            result = self.chat(key["secret"], stream=False,
                                prompt="cumulative-A", timeout=30.0)
            elapsed = time.monotonic() - started
            scenario.request(result, "cumulative wait timeout")
            payload = result.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            upstream_status = (error.get("upstream_status")
                                if isinstance(error, dict) else None)
            barrier = self._fake_um_barrier_state()
            scenario.check(result.status == 429,
                            "the share gateway relays the backend's retryable "
                            "429 share_dispatch_timeout (status=%r)" % (
                                result.status,))
            scenario.check(code == "share_dispatch_timeout",
                            "the relayed failure names share_dispatch_timeout "
                            "(code=%r)" % (code,))
            scenario.check(upstream_status == 429,
                            "the relayed failure keeps the backend's 429 as "
                            "upstream_status")
            scenario.check(result.headers.get("retry-after") == "1",
                            "the backend's Retry-After hint survives the hop")
            scenario.check(1.0 <= elapsed < 6.0,
                            "the real wait is the shortened cumulative "
                            "budget (%.2fs)" % elapsed)
            scenario.check(barrier.get("arrivals") == [],
                            "the timed-out request never reached the upstream")
            ledger = self._read_ledger()
            last = ledger[-1] if ledger else {}
            scenario.check(last.get("status") == "share_dispatch_timeout",
                            "the timeout is recorded with its own status")
            scenario.record_evidence("cumulativeWait", {
                "elapsedMs": int(elapsed * 1000),
                "status": result.status,
                "code": code,
                "upstreamStatus": upstream_status,
                "retryAfter": result.headers.get("retry-after"),
                "arrivals": barrier.get("arrivals"),
                "ledgerStatus": last.get("status"),
            })
            # The share entry maps a backend 429 onto its own 502 wrapper. Send
            # the same request straight to the dedicated backend with a valid
            # attribution marker to observe the exact 429 + Retry-After and to
            # prove the upstream still never saw it.
            share = _load_module("share_entry",
                                  HERE / "chat2api" / "share_entry.py")
            secret = self.paths["attribution"].read_text(
                encoding="utf-8").strip()
            direct_headers, direct_request_id = share.build_attribution(
                secret, key["key_id"])
            direct = _http_request(
                BACKEND_PORT, "POST", "/v1/chat/completions",
                body={"model": SHARE_MODEL, "stream": False,
                      "messages": [{"role": "user",
                                    "content": "cumulative-direct"}]},
                headers=direct_headers, timeout=30.0)
            scenario.request(direct, "direct backend cumulative wait timeout")
            direct_payload = direct.json() or {}
            direct_error = (direct_payload.get("error")
                            if isinstance(direct_payload, dict) else None)
            direct_type = (direct_error.get("type")
                            if isinstance(direct_error, dict) else None)
            scenario.check(direct.status == 429,
                            "the dedicated backend answers the exact 429")
            scenario.check(direct_type == "share_dispatch_timeout",
                            "the backend names share_dispatch_timeout")
            scenario.check(direct.headers.get("retry-after") == "1",
                            "the backend sends Retry-After: 1")
            direct_barrier = self._fake_um_barrier_state()
            scenario.check(direct_barrier.get("arrivals") == [],
                            "the direct backend request never reached the "
                            "upstream either")
            scenario.record_evidence("directBackendTimeout", {
                "requestId": direct_request_id,
                "status": direct.status,
                "type": direct_type,
                "retryAfter": direct.headers.get("retry-after"),
                "arrivals": direct_barrier.get("arrivals"),
            })
            self.dispatch_gate_call("POST", "/gate/blocker-release")
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            recovered = self.chat(key["secret"], stream=False,
                                  prompt="after blocker", timeout=30.0)
            scenario.request(recovered, "request after the blocker release")
            scenario.check(recovered.status == 200,
                            "releasing the blocker admits the next request")

            # Negative: with the dispatcher disabled the same request succeeds
            # and reaches the upstream, so the 429/no-upstream assertion MUST
            # fail. This proves the cumulative assertion can fail.
            self.dispatch_gate_call("POST", "/gate/disable")
            self.fake_um.reset()
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            negative_result = {}

            def send_negative():
                negative_result["value"] = self.chat(
                    key["secret"], stream=False,
                    prompt="cumulative-negative", timeout=30.0)

            negative_thread = threading.Thread(target=send_negative,
                                               daemon=True)
            negative_thread.start()
            negative_barrier = self._wait_fake_um_barrier(
                lambda state: len(state.get("arrivals", [])) == 1)
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            negative_thread.join(timeout=30)
            negative = negative_result.get("value")
            scenario.request(
                negative or HttpResult(
                    "POST", "/v1/chat/completions", None, {}, b"", 0,
                    "negative request did not finish"),
                "negative: dispatcher disabled")
            scenario.check(
                negative is not None and negative.status == 200,
                "negative: the disabled dispatcher admits and sends")
            scenario.check(
                not (negative is not None and negative.status == 502
                     and negative_barrier.get("arrivals") == []),
                "negative: the 429/no-upstream assertion would fail")
            scenario.record_evidence("cumulativeNegative", {
                "status": None if negative is None else negative.status,
                "arrivals": negative_barrier.get("arrivals"),
                "assertionHeld": (negative is not None
                                   and negative.status == 502
                                   and negative_barrier.get("arrivals") == []),
            })
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            self.dispatch_gate_call("POST", "/gate/enable")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/blocker-release")
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/enable")
            return scenario.finish(exc)

    def scenario_revoke_send_boundary(self):
        """R-C5: does a REAL revoke stop an 8110 PENDING send before the UM?

        Key A's first request is COMMITTED and held at the fake UM arrival
        barrier. A second same-Key request is PENDING at the REAL dispatcher
        behind a test-only rotation blocker. The REAL management API then
        revokes Key A, and the barrier is released.

        The scenario proves the fixed contract:

        * the 8100 applies the projection generation and sends ONE signed
          loopback notice BEFORE the load ACK; the 8110 releases the Key's
          PENDING ticket immediately (the dispatcher's cumulative budget did
          NOT end the wait);
        * the four required outcomes still hold (no late arrival, a retryable
          failure, a zero pending count after the test blocker is released and
          an un-preempted COMMITTED generation);
        * the negative: without a valid notice the PENDING ticket is still
          there, so the "pending=0 after revoke" assertion would fail;
        * an older generation is an idempotent no-op and never overrides the
          newer applied state.
        """
        scenario = Scenario("revoke_send_boundary")
        self.scenarios.append(scenario)
        dispatch_wait_s = 12.0
        first_marker = "chain key=A seq=1"
        second_marker = "chain key=A seq=2"
        try:
            key = self.create_key("revoke-boundary-A", max_concurrency=2)
            self.keys["REVOKE_BOUNDARY"] = key
            scenario.note(
                "REAL 8100 and 8110 processes serve every request; the "
                "first Key-A chat is COMMITTED and held at the fake UM "
                "arrival barrier; the second same-Key chat is PENDING at the "
                "REAL dispatcher behind a test-only rotation blocker; the "
                "revoke uses the REAL management API (revoke-and-terminate)")
            scenario.note(
                "the dispatch budget is %s s so the assertion proves the "
                "revoke notice, not the cumulative budget, ended the PENDING "
                "wait; the production default is 10 s"
                % dispatch_wait_s)
            self.start_dispatch_backend(max_wait=dispatch_wait_s)
            scenario.record_evidence("processes", {
                "shareGatewayPid": self.share.process.pid,
                "shareBackendPid": self.backend.process.pid,
                "brokerPid": self.broker.process.pid,
                "managementPid": self.management_process.process.pid,
            })
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            self.dispatch_gate_call("POST", "/gate/arm")

            backend_before = _stable_proc_snapshot(self.backend.process.pid)
            share_before = _stable_proc_snapshot(self.share.process.pid)
            ledger_before = len(self._read_ledger())
            faults_before = len(self._read_faults())
            instance_before = self.instance_status()
            scenario.check(
                instance_before.get("ok") is True
                and instance_before.get("active_count") == 0,
                "the 8100 instance reports no live business slot before the "
                "requests (active_count=%r)" % (
                    instance_before.get("active_count"),))
            scenario.record_evidence("beforeRequests", {
                "backend": backend_before,
                "share": share_before,
                "instance": instance_before,
                "ledgerRows": ledger_before,
            })

            results = {}
            results_lock = threading.Lock()

            def send(marker):
                result = self.chat(key["secret"], stream=False,
                                   prompt=marker, timeout=60.0)
                with results_lock:
                    results[marker] = result

            # 1) First same-Key request: grant it, commit it and let the fake
            # UM barrier hold it AFTER the commit.
            first_thread = threading.Thread(
                target=send, args=(first_marker,), daemon=True)
            first_thread.start()
            self._wait_dispatch_gate(
                lambda state: len(state.get("registrations", [])) == 1)
            self.dispatch_gate_call("POST", "/gate/release")
            gate = self._wait_dispatch_gate(
                lambda state: len(state.get("grants", [])) == 1)
            first_request_id = gate["grants"][0]["requestId"]
            self.dispatch_gate_call(
                "POST", "/gate/send-release",
                {"requestId": first_request_id})
            barrier = self._wait_fake_um_barrier(
                lambda state: len(state.get("arrivals", [])) == 1)
            scenario.check(
                barrier["arrivals"][0].get("marker") == first_marker,
                "the first same-Key request is the one held at the fake UM "
                "barrier (arrival marker %r)" % (
                    barrier["arrivals"][0].get("marker"),))
            first_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("committed") == 1
                and stats.get("pending") == 0)
            scenario.check(
                first_stats.get("committed") == 1,
                "the first same-Key request is COMMITTED at the dispatcher")
            backend_during = _stable_proc_snapshot(self.backend.process.pid)
            share_during = _stable_proc_snapshot(self.share.process.pid)
            scenario.check(
                backend_during.get("threads", 0)
                > backend_before.get("threads", 0)
                and backend_during.get("sockets", 0)
                > backend_before.get("sockets", 0),
                "the held COMMITTED generation owns extra 8110 "
                "threads/sockets (%r -> %r)" % (
                    backend_before, backend_during))
            scenario.check(
                share_during.get("threads", 0)
                > share_before.get("threads", 0)
                and share_during.get("sockets", 0)
                > share_before.get("sockets", 0),
                "the held COMMITTED generation owns extra 8100 "
                "threads/sockets (%r -> %r)" % (
                    share_before, share_during))
            scenario.record_evidence("firstCommittedHeld", {
                "dispatcher": first_stats,
                "arrivals": barrier.get("arrivals"),
                "backend": backend_during,
                "share": share_during,
            })

            # 2) A test-only blocker owns the rotation so the second same-Key
            # request stays PENDING even though Key A's queue is empty.
            blocker = self.dispatch_gate_call(
                "POST", "/gate/blocker",
                {"keyId": "blocked-key", "requestId": "blocked-request"})
            scenario.request(blocker, "register a rotation blocker")
            scenario.check(blocker.status == 200,
                           "the test-only rotation blocker is registered")
            blocker_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 1)
            scenario.check(blocker_stats.get("pending") == 1,
                           "the rotation blocker holds the dispatcher turn")

            # 3) Second same-Key request: registered, never granted.
            second_thread = threading.Thread(
                target=send, args=(second_marker,), daemon=True)
            second_thread.start()
            self._wait_dispatch_gate(
                lambda state: len(state.get("registrations", [])) == 2)
            pending_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 2
                and stats.get("committed") == 1)
            scenario.check(
                pending_stats.get("pending") == 2
                and pending_stats.get("committed") == 1,
                "the second same-Key request is PENDING while the first "
                "is COMMITTED")
            scenario.record_evidence("pendingBeforeRevoke", pending_stats)

            # 3b) Negative without a valid notice: the second same-Key ticket
            # is still PENDING and a forged notice is refused, so the assertion
            # "the revoked Key has no PENDING ticket" MUST fail here.
            before_notice_tickets = self._revoked_key_pending_tickets(
                key["key_id"])
            scenario.check(
                len(before_notice_tickets) == 1,
                "negative without a valid notice: the revoked Key still owns "
                "its PENDING ticket (pending tickets=%r)" % (
                    before_notice_tickets,))
            share = _load_module("share_entry",
                                  HERE / "chat2api" / "share_entry.py")
            secret = self.paths["attribution"].read_text(
                encoding="utf-8").strip()
            forged = share.build_revoke_notice(secret, key["key_id"], 1)
            forged["signature"] = "0" * 64
            forged_result = _http_request(
                BACKEND_PORT, "POST", "/admin/share/revoke",
                body=forged, timeout=15.0)
            scenario.request(forged_result, "forged revoke notice (negative)")
            forged_payload = forged_result.json() or {}
            forged_error = forged_payload.get("error") or {}
            forged_detail = forged_error.get("detail") or {}
            scenario.check(
                forged_result.status == 403
                and forged_detail.get("reason") == "bad_signature",
                "a forged notice is refused (status=%r, reason=%r)" % (
                    forged_result.status, forged_detail.get("reason")))
            after_forged_tickets = self._revoked_key_pending_tickets(
                key["key_id"])
            scenario.check(
                len(after_forged_tickets) == 1,
                "without a valid notice the PENDING ticket is still there "
                "(negative: pending=0 would fail; pending tickets=%r)" % (
                    after_forged_tickets,))
            scenario.record_evidence("negativeWithoutNotice", {
                "beforeTickets": before_notice_tickets,
                "forgedStatus": forged_result.status,
                "forgedReason": forged_detail.get("reason"),
                "afterForgedTickets": after_forged_tickets,
            })

            # 4) REAL management API revoke-and-terminate.
            result = self.management_call(
                "POST",
                f"/api/share/keys/{key['key_id']}/revoke-and-terminate",
                {"expected_row_version": key["row_version"]})
            scenario.request(result, "revoke Key A via the management API")
            revoked = result.json() or {}
            scenario.check(result.status == 200,
                           "the REAL management API accepts the revoke")
            scenario.check(revoked.get("published") is True,
                           "the revoke confirms the projection was published")
            scenario.check(revoked.get("in_sync") is True,
                           "the revoke reports the target generation applied")
            # D18-3: a forwarded dispatch send that has not started its
            # response keeps its socket so the backend's released count can
            # answer the retryable 429 instead of a 502.  The ACK must still
            # carry the terminate count (the one-shot list arrived) and must
            # report zero preempted sockets; the two clients' own observations
            # below are what prove the closes actually happened.
            scenario.check(
                isinstance(revoked.get("terminated"), int)
                and revoked.get("terminated") == 0,
                "the instance ACK carries the terminate count without "
                "preempting a retryable dispatch send (terminated=%r)" % (
                    revoked.get("terminated"),))
            scenario.record_evidence("revokeResponse", {
                "status": result.status,
                "published": revoked.get("published"),
                "inSync": revoked.get("in_sync"),
                "terminated": revoked.get("terminated"),
                "desiredGeneration": revoked.get("desired_generation"),
                "appliedGeneration": revoked.get("applied_generation"),
                "rowVersion": revoked.get("row_version"),
            })

            # 5) The revoke closed the 8100 CLIENT sockets AND the signed
            # loopback notice released the Key's PENDING ticket at the REAL
            # dispatcher. The only remaining PENDING ticket is the test-only
            # rotation blocker; the COMMITTED first generation keeps its slot.
            released_tickets = self._wait_revoked_key_released(
                key["key_id"], timeout=10.0)
            after_revoke_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 1
                and stats.get("committed") == 1,
                timeout=10.0)
            after_revoke_instance = self.instance_status()
            scenario.check(
                not released_tickets,
                "the revoke released the 8110 PENDING ticket of the revoked "
                "Key (pending tickets for that Key=%r)" % (
                    released_tickets,))
            scenario.check(
                after_revoke_stats.get("pending") == 1
                and after_revoke_stats.get("committed") == 1,
                "after the revoke the only remaining PENDING ticket is the "
                "test-only rotation blocker (pending=%r, committed=%r)" % (
                    after_revoke_stats.get("pending"),
                    after_revoke_stats.get("committed")))
            scenario.check(
                after_revoke_instance.get("active_count", 0) >= 1,
                "the COMMITTED first generation still holds its 8100 "
                "business slot after the revoke (active_count=%r)" % (
                    after_revoke_instance.get("active_count"),))

            # 5a) Release the test-only blocker now that the revoked Key's
            # ticket is already released.  The dispatcher's overall PENDING
            # count must return to zero; the COMMITTED first generation is
            # still committed and is never preempted.
            blocker_release = self.dispatch_gate_call(
                "POST", "/gate/blocker-release")
            scenario.request(blocker_release, "release the rotation blocker")
            final_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 0
                and stats.get("keys") == 0)
            scenario.check(
                final_stats.get("pending") == 0,
                "after the revoke the dispatcher ticket count returns to "
                "zero (pending=%r)" % (final_stats.get("pending"),))
            scenario.check(
                final_stats.get("committed") == 1
                and final_stats.get("timed_out") == 0,
                "the first ticket stayed committed and the revoked ticket was "
                "released, not timed out (committed=%r, timed_out=%r)" % (
                    final_stats.get("committed"),
                    final_stats.get("timed_out")))
            after_revoke_instance = self.instance_status()
            scenario.record_evidence("afterRevoke", {
                "dispatcher": final_stats,
                "tickets": (self._dispatch_gate() or {}).get("tickets"),
                "revokedKeyPendingTickets": released_tickets,
                "instance": after_revoke_instance,
                "barrier": self._fake_um_barrier_state(),
            })
            scenario.record_evidence("finalDispatcher", final_stats)

            # 5b) The second request is refused by the backend with the
            # retryable share_dispatch_revoked 429 BEFORE the cumulative
            # budget would have expired. The 8100 relay forwards that bounded
            # refusal, so the second client observes the 429 above.
            _wait_for(
                lambda: any(
                    row.get("status") == "share_dispatch_revoked"
                    and row.get("key_id") == key["key_id"]
                    for row in self._read_ledger()[ledger_before:]),
                10.0)
            ledger = self._read_ledger()[ledger_before:]
            second_row = next((
                row for row in ledger
                if row.get("status") == "share_dispatch_revoked"
                and row.get("key_id") == key["key_id"]), {})
            scenario.check(
                second_row.get("status") == "share_dispatch_revoked"
                and second_row.get("key_id") == key["key_id"],
                "the backend refused the second request with the retryable "
                "share_dispatch_revoked 429, never a success (status=%r)"
                % (second_row.get("status"),))
            scenario.check(
                isinstance(second_row.get("duration_ms"), (int, float))
                and second_row.get("duration_ms") < dispatch_wait_s * 1000.0,
                "the revoke notice ended the PENDING wait before the "
                "cumulative budget (duration_ms=%r < %r)" % (
                    second_row.get("duration_ms"),
                    dispatch_wait_s * 1000.0))
            scenario.record_evidence("secondRequestRevoked", {
                "dispatcher": after_revoke_stats,
                "ledger": second_row,
                "tickets": (self._dispatch_gate() or {}).get("tickets"),
            })

            # The two clients observe the revoke's existing cancel form: the
            # COMMITTED connection is closed, while the PENDING second request
            # receives the relayed retryable 429 share_dispatch_revoked.
            for marker in (first_marker, second_marker):
                _wait_for(lambda marker=marker: results.get(marker) is not None,
                          15.0)
            scenario.check(
                results[first_marker].status != 200
                and results[first_marker].error,
                "the COMMITTED first client sees the revoke's connection "
                "close, not a fabricated success (%r)" % (
                    results[first_marker].error,))
            second_payload = results[second_marker].json() or {}
            second_error = (second_payload.get("error")
                            if isinstance(second_payload, dict) else None)
            second_type = (second_error.get("type")
                           if isinstance(second_error, dict) else None)
            scenario.check(
                results[second_marker].status == 429
                and second_type == "share_dispatch_revoked",
                "the PENDING second client sees the relayed retryable 429 "
                "share_dispatch_revoked, not a fabricated success "
                "(status=%r, type=%r)" % (
                    results[second_marker].status, second_type))
            scenario.record_evidence("clientCancelForm", {
                first_marker: results[first_marker].as_record(),
                second_marker: results[second_marker].as_record(),
            })

            # 6) Release the fake UM barrier: the COMMITTED generation must
            # finish (not be preempted); the revoked Key's ticket and the
            # test-only blocker were already released, so pending stays zero.
            release = self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            scenario.request(release, "release the fake UM barrier")
            released_barrier = self._wait_fake_um_barrier(
                lambda state: state.get("released", 0) >= 1)
            scenario.check(
                released_barrier.get("timedOut") == 0,
                "the COMMITTED first generation was released, not timed out")
            scenario.check(
                len(released_barrier.get("arrivals", [])) == 1
                and all(entry.get("marker") != second_marker
                        for entry in released_barrier.get("arrivals", [])),
                "the revoked second request never reached the fake UM")
            # D18-3: the revoke CANCELS the Key's in-flight requests, so the
            # COMMITTED generation is aborted through the callback the backend
            # registered on its ticket and no success row is ever fabricated for
            # it.  The barrier release above already let that request unwind, so
            # this is the settled state, not a race: a success row here would
            # mean the revoke failed to cancel it.
            ledger = self._read_ledger()[ledger_before:]
            success_rows = [row for row in ledger
                            if row.get("status") == "ok"
                            and row.get("key_id") == key["key_id"]]
            first_row = success_rows[0] if success_rows else {}
            scenario.check(
                not success_rows,
                "the COMMITTED first generation wrote no fabricated success "
                "row after the revoke cancelled it (success rows=%r)" % (
                    success_rows,))
            after_release_tickets = self._revoked_key_pending_tickets(
                key["key_id"])
            after_release_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 0,
                timeout=10.0)
            scenario.check(
                not after_release_tickets,
                "after the barrier release the revoked Key still owns no "
                "PENDING/GRANTED ticket (pending tickets=%r)" % (
                    after_release_tickets,))
            scenario.check(
                after_release_stats.get("pending") == 0,
                "after the barrier release the dispatcher has no PENDING "
                "ticket (pending=%r)" % (
                    after_release_stats.get("pending"),))
            scenario.record_evidence("afterBarrierRelease", {
                "barrier": released_barrier,
                "dispatcher": after_release_stats,
                "firstLedger": first_row,
                "revokedKeyPendingTickets": after_release_tickets,
            })

            # 7) The PENDING wait ended through the revoke notice, NOT the
            # cumulative budget: the dispatcher's timed_out counter stayed 0.
            final_wait_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("timed_out") == 0
                and stats.get("pending") == 0,
                timeout=10.0)
            scenario.check(
                final_wait_stats.get("timed_out") == 0,
                "the cumulative budget did NOT end the PENDING wait "
                "(timed_out=%r)" % (final_wait_stats.get("timed_out"),))
            scenario.record_evidence("secondRequestNotTimedOut", {
                "dispatcher": final_wait_stats,
            })

            # 9) The 8100 business slots and the 8110 reader/socket fall back.
            final_instance = _wait_for(
                lambda: (self.instance_status().get("active_count") == 0
                         and self.instance_status()),
                15.0)
            scenario.check(
                final_instance.get("active_count") == 0,
                "the 8100 business slots fall back to zero (active_count=%r)"
                % (final_instance.get("active_count"),))
            backend_after, backend_settle_ms = _wait_proc_settle(
                self.backend.process.pid, backend_before, timeout=20.0)
            share_after, share_settle_ms = _wait_proc_settle(
                self.share.process.pid, share_before, timeout=20.0)
            scenario.check(
                backend_after.get("readerThreads") == 0,
                "the 8110 named reader count is zero after the non-stream "
                "aggregate falls back (readerThreads=%r)"
                % (backend_after.get("readerThreads"),))
            scenario.check(
                backend_after.get("threads") <= backend_before.get("threads") + 1
                and backend_after.get("sockets")
                <= backend_before.get("sockets") + 1,
                "the 8110 thread/socket counts return to baseline (%r -> %r)"
                % (backend_before, backend_after))
            scenario.check(
                share_after.get("threads") <= share_before.get("threads") + 1
                and share_after.get("fds") <= share_before.get("fds") + 1
                and share_after.get("sockets")
                <= share_before.get("sockets") + 1,
                "the 8100 thread/FD/socket counts return to baseline (%r -> %r)"
                % (share_before, share_after))
            scenario.record_evidence("resourceFallback", {
                "backendBefore": backend_before,
                "backendAfter": backend_after,
                "backendSettleMs": backend_settle_ms,
                "shareBefore": share_before,
                "shareAfter": share_after,
                "shareSettleMs": share_settle_ms,
            })

            # 10) The 8100 fault log proves the backend's retryable 429 did
            # reach the share entry (its client socket was already closed).
            faults = self._read_faults()[faults_before:]
            upstream_429 = [
                record for record in faults
                if record.get("code") == "share_dispatch_revoked"
                and record.get("upstream_status") == 429]
            scenario.check(
                bool(upstream_429),
                "the 8100 relay observed the backend's retryable 429 "
                "(code=share_dispatch_revoked, upstream_status=429)")
            scenario.record_evidence("faults", {
                "records": faults,
                "upstream429": upstream_429,
            })

            # 11) R-C5 close: the revoke notice, not the cumulative budget,
            # released the PENDING ticket.  A valid but OLDER generation must
            # not override the newer applied state: it is an idempotent no-op
            # and the revoked Key still owns no PENDING/GRANTED ticket.
            stale_generation = max(
                0, int(revoked.get("desired_generation") or 1) - 1)
            stale_notice = share.build_revoke_notice(
                secret, key["key_id"], stale_generation)
            stale_result = _http_request(
                BACKEND_PORT, "POST", "/admin/share/revoke",
                body=stale_notice, timeout=15.0)
            scenario.request(stale_result,
                             "stale-generation revoke notice")
            stale_payload = stale_result.json() or {}
            scenario.check(
                stale_result.status == 200
                and stale_payload.get("released") == 0
                and stale_payload.get("ignored") == "stale_generation",
                "an older generation never overrides the newer applied state "
                "(status=%r, released=%r, ignored=%r)" % (
                    stale_result.status, stale_payload.get("released"),
                    stale_payload.get("ignored")))
            final_tickets = self._revoked_key_pending_tickets(key["key_id"])
            scenario.check(
                not final_tickets,
                "the stale notice did not release a newer ticket (pending "
                "tickets=%r)" % (final_tickets,))
            scenario.record_evidence("staleGenerationNotice", {
                "status": stale_result.status,
                "released": stale_payload.get("released"),
                "ignored": stale_payload.get("ignored"),
                "tickets": final_tickets,
            })
            scenario.classify(
                "revoke_notice_releases_8110_pending_immediately; the "
                "COMMITTED generation is not preempted; an older generation "
                "is an idempotent no-op; the cumulative budget remains the "
                "fallback when 8110 is unreachable")

            first_thread.join(timeout=5)
            second_thread.join(timeout=5)
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/blocker-release")
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/enable")
            return scenario.finish(exc)

    def scenario_revoke_granted_not_committed(self):
        """R-C5: a REAL revoke closes a GRANTED ticket before its commit.

        The request is granted by the REAL dispatcher and passes the product's
        pre-send checks, then the test-only commit gate holds it immediately
        before the REAL ``ShareDispatcher.commit``.  The REAL management API
        revokes the Key in that window.  Releasing the gate runs the REAL
        commit, which returns False because the signed loopback notice already
        closed the GRANTED ticket; the handler answers the retryable
        ``share_dispatch_revoked`` 429 and the request never reaches the fake
        UM.  No grant, commit or release decision is faked.
        """
        scenario = Scenario("revoke_granted_not_committed")
        self.scenarios.append(scenario)
        marker = "chain key=A seq=1"
        dispatch_wait_s = 12.0
        try:
            key = self.create_key("revoke-granted-A", max_concurrency=2)
            self.keys["REVOKE_GRANTED"] = key
            scenario.note(
                "REAL 8100 and 8110 processes serve the request; the REAL "
                "dispatcher grants the ticket and the product passes its "
                "pre-send checks, then a test-only gate holds it immediately "
                "before the REAL ShareDispatcher.commit; the revoke uses the "
                "REAL management API (revoke-and-terminate)")
            scenario.note(
                "the dispatch budget is %s s so the assertion proves the "
                "revoke notice, not the cumulative budget, closed the GRANTED "
                "ticket; the production default is 10 s" % dispatch_wait_s)
            self.start_dispatch_backend(max_wait=dispatch_wait_s)
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            self.dispatch_gate_call("POST", "/gate/arm")
            self.dispatch_gate_call("POST", "/gate/commit-arm")
            ledger_before = len(self._read_ledger())
            faults_before = len(self._read_faults())
            result = {}

            def send():
                result["value"] = self.chat(
                    key["secret"], stream=False, prompt=marker, timeout=60.0)

            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            self._wait_dispatch_gate(
                lambda state: len(state.get("registrations", [])) == 1)
            self.dispatch_gate_call("POST", "/gate/release")
            gate = self._wait_dispatch_gate(
                lambda state: len(state.get("grants", [])) == 1)
            granted = gate["grants"][0]
            scenario.check(
                granted.get("keyId") == key["key_id"],
                "the REAL dispatcher granted the request's ticket")
            granted_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("granted") == 1
                and stats.get("pending") == 0
                and stats.get("committed") == 0)
            scenario.check(
                granted_stats.get("granted") == 1
                and granted_stats.get("pending") == 0
                and granted_stats.get("committed") == 0,
                "the ticket is GRANTED but not committed "
                "(granted=%r, pending=%r, committed=%r)" % (
                    granted_stats.get("granted"),
                    granted_stats.get("pending"),
                    granted_stats.get("committed")))
            # Let the product pass its pre-send checks and stop immediately
            # before the REAL commit. The send gate is only the earlier
            # launcher gate; the commit gate is the exact GRANTED-未提交 point.
            self.dispatch_gate_call(
                "POST", "/gate/send-release",
                {"requestId": granted["requestId"]})
            commit_state = self._wait_dispatch_gate(
                lambda state: granted["requestId"]
                in state.get("commits", []))
            scenario.check(
                granted["requestId"] in commit_state.get("commits", []),
                "the product reached the REAL commit boundary and is held "
                "immediately before ShareDispatcher.commit")
            tickets_before = (self._dispatch_gate() or {}).get("tickets", [])
            scenario.check(
                any(ticket.get("requestId") == granted["requestId"]
                    and ticket.get("state") == "granted"
                    for ticket in tickets_before),
                "the held ticket is still GRANTED before the revoke "
                "(tickets=%r)" % (tickets_before,))
            scenario.check(
                (self._fake_um_barrier_state() or {}).get("arrivals") == [],
                "the granted-but-uncommitted request has not reached the "
                "fake UM")

            revoked = self.management_call(
                "POST",
                f"/api/share/keys/{key['key_id']}/revoke-and-terminate",
                {"expected_row_version": key["row_version"]})
            scenario.request(revoked,
                             "revoke the granted-but-uncommitted Key")
            revoked_body = revoked.json() or {}
            scenario.check(
                revoked.status == 200
                and revoked_body.get("published") is True
                and revoked_body.get("in_sync") is True,
                "the REAL management API publishes and applies the revoke "
                "(status=%r, published=%r, in_sync=%r)" % (
                    revoked.status, revoked_body.get("published"),
                    revoked_body.get("in_sync")))
            # D18-3: the GRANTED-but-uncommitted send is forwarded and
            # retryable, so the revoke keeps its socket and answers the
            # retryable 429 (asserted below) instead of preempting it here.
            # The ACK still carries the count, proving the terminate list
            # reached the instance.
            scenario.check(
                isinstance(revoked_body.get("terminated"), int)
                and revoked_body.get("terminated") == 0,
                "the instance ACK carries the terminate count without "
                "preempting the granted-but-uncommitted send (terminated=%r)"
                % (revoked_body.get("terminated"),))

            released = self._wait_revoked_key_released(
                key["key_id"], timeout=10.0)
            after_revoke_tickets = (
                self._dispatch_gate() or {}).get("tickets", [])
            scenario.check(
                not released
                and all(ticket.get("requestId") != granted["requestId"]
                         for ticket in after_revoke_tickets),
                "the signed revoke notice closed the GRANTED ticket before "
                "its commit (live tickets=%r)" % (after_revoke_tickets,))

            commit_release = self.dispatch_gate_call(
                "POST", "/gate/commit-release",
                {"requestId": granted["requestId"]})
            scenario.request(commit_release,
                             "release the commit gate after the revoke")
            _wait_for(
                lambda: any(
                    row.get("status") == "share_dispatch_revoked"
                    and row.get("key_id") == key["key_id"]
                    for row in self._read_ledger()[ledger_before:]),
                15.0)
            _wait_for(lambda: result.get("value") is not None, 20.0)
            client_result = result.get("value")
            ledger = self._read_ledger()[ledger_before:]
            revoked_row = next((
                row for row in ledger
                if row.get("status") == "share_dispatch_revoked"
                and row.get("key_id") == key["key_id"]), {})
            scenario.check(
                revoked_row.get("status") == "share_dispatch_revoked"
                and revoked_row.get("key_id") == key["key_id"],
                "the backend recorded the retryable share_dispatch_revoked "
                "refusal, never a success (status=%r)" % (
                    revoked_row.get("status"),))
            scenario.check(
                isinstance(revoked_row.get("duration_ms"), (int, float))
                and revoked_row.get("duration_ms") < dispatch_wait_s * 1000.0,
                "the revoke notice ended the wait before the cumulative "
                "budget (duration_ms=%r < %r)" % (
                    revoked_row.get("duration_ms"),
                    dispatch_wait_s * 1000.0))
            final_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 0
                and stats.get("timed_out") == 0)
            scenario.check(
                final_stats.get("committed") == 0,
                "the revoked GRANTED ticket never committed (committed=%r)"
                % (final_stats.get("committed"),))
            scenario.check(
                final_stats.get("timed_out") == 0,
                "the cumulative budget did NOT end the wait (timed_out=%r)"
                % (final_stats.get("timed_out"),))
            barrier = self._fake_um_barrier_state() or {}
            scenario.check(
                barrier.get("arrivals") == [],
                "the revoked granted request never reached the fake UM "
                "(arrivals=%r)" % (barrier.get("arrivals"),))
            client_payload = (client_result.json()
                              if client_result is not None else None)
            client_error = (client_payload.get("error")
                            if isinstance(client_payload, dict) else None)
            client_type = (client_error.get("type")
                           if isinstance(client_error, dict) else None)
            client_upstream = (client_error.get("upstream_status")
                               if isinstance(client_error, dict) else None)
            scenario.check(
                client_result is not None
                and client_result.status == 429
                and client_type == "share_dispatch_revoked"
                and client_upstream == 429,
                "the terminal client sees the documented 429 "
                "share_dispatch_revoked (status=%r, type=%r, "
                "upstream_status=%r)" % (
                    None if client_result is None else client_result.status,
                    client_type, client_upstream))
            scenario.check(
                client_result is not None
                and client_result.headers.get("retry-after") == "1",
                "the terminal 429 carries Retry-After: 1 (%r)" % (
                    None if client_result is None
                    else client_result.headers.get("retry-after"),))
            faults = self._read_faults()[faults_before:]
            scenario.record_evidence("grantedBeforeRevoke", {
                "grant": granted,
                "dispatcher": granted_stats,
                "tickets": tickets_before,
                "barrier": self._fake_um_barrier_state(),
                "commits": commit_state.get("commits"),
            })
            scenario.record_evidence("revokeResponse", {
                "status": revoked.status,
                "published": revoked_body.get("published"),
                "inSync": revoked_body.get("in_sync"),
                "terminated": revoked_body.get("terminated"),
                "desiredGeneration": revoked_body.get("desired_generation"),
                "appliedGeneration": revoked_body.get("applied_generation"),
            })
            scenario.record_evidence("afterRevoke", {
                "tickets": after_revoke_tickets,
                "dispatcher": final_stats,
                "ledger": revoked_row,
                "barrier": barrier,
                "faults": faults,
                "client": (None if client_result is None
                           else client_result.as_record()),
            })
            scenario.classify(
                "the REAL management revoke closes a GRANTED-but-not-"
                "committed ticket through the signed loopback notice; the "
                "REAL commit returns False and the handler records "
                "share_dispatch_revoked; the request never reaches the UM")
            thread.join(timeout=5)
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            self.fake_um_barrier(
                "POST", "/__control/barrier/disarm", {})
            self.dispatch_gate_call("POST", "/gate/commit-disarm")
            self.dispatch_gate_call("POST", "/gate/enable")
            self.dispatch_gate_call("POST", "/gate/disarm")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/commit-disarm")
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/enable")
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/disarm", {})
            return scenario.finish(exc)

    def scenario_revoke_relay_window(self):
        """R-C5: an 8100-admitted request must not send after the revoke.

        The request is held by a test-only gate immediately before the REAL
        8100->8110 upstream send, so it has already passed 8100 admission but
        has NOT registered any 8110 ticket.  The REAL management API then
        revokes the Key and closes the live client socket.  Releasing the gate
        must abort before the upstream send: no new 8110 ticket is registered
        after the notice and the fake UM sees no arrival.
        """
        scenario = Scenario("revoke_relay_window")
        self.scenarios.append(scenario)
        marker = "chain key=window seq=1"
        try:
            key = self.create_key("relay-window-A", max_concurrency=2)
            self.keys["RELAY_WINDOW"] = key
            scenario.note(
                "REAL 8100 and 8110 processes serve the request; a test-only "
                "share-gateway launcher holds the REAL upstream send after "
                "8100 admission, so no 8110 ticket exists yet when the REAL "
                "management API revokes the Key")
            self.start_dispatch_backend(max_wait=12.0)
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            ledger_before = len(self._read_ledger())
            self.relay_gate_call("POST", "/gate/arm")
            result = {}

            def send():
                result["value"] = self.chat(
                    key["secret"], stream=False, prompt=marker, timeout=60.0)

            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            gate = self._wait_relay_gate(
                lambda state: len(state.get("registrations", [])) == 1)
            scenario.check(
                gate["registrations"][0].get("order") == 1,
                "the request is held at the REAL relay send check")
            scenario.check(
                not self._revoked_key_pending_tickets(key["key_id"]),
                "the held request has NOT registered any 8110 ticket before "
                "the revoke (the old 8100->8110 window)")
            scenario.check(
                (self._fake_um_barrier_state() or {}).get("arrivals") == [],
                "the held request has not reached the fake UM yet")

            revoked = self.management_call(
                "POST",
                f"/api/share/keys/{key['key_id']}/revoke-and-terminate",
                {"expected_row_version": key["row_version"]})
            scenario.request(revoked, "revoke the Key in the relay window")
            revoked_body = revoked.json() or {}
            scenario.check(
                revoked.status == 200
                and revoked_body.get("published") is True
                and revoked_body.get("in_sync") is True,
                "the REAL management API applied the revoke (status=%r)"
                % (revoked.status,))
            # D18-3: this request is still in the 8100->8110 window, so it
            # was never forwarded; the revoke leaves it to the handler's
            # pre-send check, which answers the retryable 429 (asserted below)
            # rather than a socket close.  The ACK still carries the count.
            scenario.check(
                isinstance(revoked_body.get("terminated"), int)
                and revoked_body.get("terminated") == 0,
                "the instance ACK carries the terminate count without "
                "preempting the not-yet-forwarded request (terminated=%r)"
                % (revoked_body.get("terminated"),))

            # The 8110 notice had no ticket to release: the request was still
            # in the 8100->8110 window, not yet registered.
            scenario.check(
                not self._revoked_key_pending_tickets(key["key_id"]),
                "the notice released no 8110 ticket because the request had "
                "not registered yet")
            self.relay_gate_call("POST", "/gate/release")
            _wait_for(lambda: result.get("value") is not None, 15.0)
            client_result = result["value"]
            scenario.request(client_result,
                             "request admitted before the revoke")
            client_payload = client_result.json() or {}
            client_error = (client_payload.get("error")
                            if isinstance(client_payload, dict) else None)
            client_type = (client_error.get("type")
                           if isinstance(client_error, dict) else None)
            scenario.check(
                client_result.status == 429
                and client_type == "share_dispatch_revoked",
                "the 8100 client sees the relayed retryable 429 "
                "share_dispatch_revoked, not a fabricated success "
                "(status=%r, type=%r)" % (
                    client_result.status, client_type))
            scenario.check(
                not self._revoked_key_pending_tickets(key["key_id"]),
                "the request never registered a new 8110 ticket after the "
                "notice")
            barrier = self._fake_um_barrier_state() or {}
            scenario.check(
                barrier.get("arrivals") == [],
                "the request never reached the fake UM after the revoke "
                "(arrivals=%r)" % (barrier.get("arrivals"),))
            ledger_after = self._read_ledger()[ledger_before:]
            scenario.check(
                not any(row.get("key_id") == key["key_id"]
                        for row in ledger_after),
                "the request wrote no 8110 usage row after the revoke")
            stats = self._dispatch_gate() or {}
            scenario.check(
                stats.get("dispatcher") is None,
                "the request never assembled a 8110 dispatcher after the "
                "revoke (dispatcher=%r)" % (stats.get("dispatcher"),))
            scenario.check(
                stats.get("tickets") == [],
                "the request never registered a 8110 ticket after the notice "
                "(tickets=%r)" % (stats.get("tickets"),))
            scenario.record_evidence("relayWindow", {
                "client": client_result.as_record(),
                "arrivals": barrier.get("arrivals"),
                "ledger": ledger_after,
                "dispatcher": stats.get("dispatcher"),
                "tickets": stats.get("tickets"),
                "terminated": revoked_body.get("terminated"),
            })

            # Negative: the SAME gate without a revoke must reach the fake UM.
            # This proves the positive no-send assertion can fail.
            negative_key = self.create_key("relay-window-B",
                                           max_concurrency=2)
            self.fake_um.reset()
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            self.relay_gate_call("POST", "/gate/arm")
            negative = {}

            def send_negative():
                negative["value"] = self.chat(
                    negative_key["secret"], stream=False,
                    prompt="chain key=window seq=2", timeout=60.0)

            negative_thread = threading.Thread(target=send_negative,
                                              daemon=True)
            negative_thread.start()
            self._wait_relay_gate(
                lambda state: len(state.get("registrations", [])) == 1)
            self.relay_gate_call("POST", "/gate/release")
            negative_arrival = self._wait_fake_um_barrier(
                lambda state: len(state.get("arrivals", [])) == 1)
            self.fake_um_barrier(
                "POST", "/__control/barrier/release", {"all": True})
            negative_thread.join(timeout=60)
            negative_result = negative.get("value")
            scenario.request(
                negative_result or HttpResult(
                    "POST", "/v1/chat/completions", None, {}, b"", 0,
                    "negative request did not finish"),
                "negative: no revoke in the relay window")
            scenario.check(
                negative_result is not None
                and negative_result.status == 200,
                "negative: without the revoke the held request sends "
                "(status=%r)" % (
                    None if negative_result is None
                    else negative_result.status,))
            scenario.check(
                negative_arrival["arrivals"][0].get("marker")
                == "chain key=window seq=2",
                "negative: the released request reaches the fake UM "
                "(arrival=%r)" % (
                    negative_arrival["arrivals"][0].get("marker"),))
            scenario.record_evidence("relayWindowNegative", {
                "status": None if negative_result is None
                          else negative_result.status,
                "arrivals": negative_arrival.get("arrivals"),
            })
            self.relay_gate_call("POST", "/gate/disarm")
            scenario.classify(
                "a request admitted by 8100 before the revoke never "
                "registers a new 8110 ticket or sends after the notice; "
                "without the revoke the same gate reaches the fake UM")
            thread.join(timeout=5)
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.relay_gate_call("POST", "/gate/release")
            with contextlib.suppress(Exception):
                self.fake_um_barrier(
                    "POST", "/__control/barrier/release", {"all": True})
            return scenario.finish(exc)

    def scenario_revoke_after_gateway_restart(self):
        """R6 #4: an 8100 restart during the Key's disabled period.

        The Key is enabled at the last APPLIED generation.  The broker and the
        REAL 8100 are stopped; the REAL management API then disables the Key
        while the broker is down.  The store mutation commits, but the
        projection cannot be published, so the projection file keeps the last
        APPLIED generation (the Key still enabled) and the call fails closed
        with 503.

        The REAL 8100 restarts while the Key is disabled in the store.  It
        restores the last APPLIED projection (enabled), admits a real request
        through 8100->8110 and the request waits PENDING at the REAL 8110
        dispatcher behind a test-only blocker.  The broker restarts and a
        non-revoking management change publishes the disabled generation; the
        restarted 8100 detects the enabled->disabled boundary from its
        persisted snapshot, sends the signed revoke notice and the REAL 8110
        releases the PENDING ticket.  The terminal sees the documented 429,
        the fake UM sees no arrival, and the ledger names
        share_dispatch_revoked.
        """
        scenario = Scenario("revoke_after_gateway_restart")
        self.scenarios.append(scenario)
        marker = "chain key=restart seq=1"
        try:
            key = self.create_key("revoke-restart-A", max_concurrency=17)
            self.keys["REVOKE_RESTART"] = key
            scenario.note(
                "REAL 8100 and 8110 processes serve the request; the broker "
                "and 8100 are stopped, the REAL management API disables the "
                "Key while the broker is down (fail-closed 503), the 8100 "
                "restarts with the last APPLIED enabled projection and the "
                "broker restarts for the next publication")
            self.start_dispatch_backend(max_wait=30.0)
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            self.fake_um_barrier(
                "POST", "/__control/barrier/arm", {"protocols": ["chat"]})
            self.dispatch_gate_call("POST", "/gate/arm")

            projection_before = json.loads(
                self.paths["projection"].read_text(encoding="utf-8"))
            before_entry = next((
                entry for entry in projection_before.get("data", [])
                if entry.get("key_id") == key["key_id"]), {})
            scenario.record_evidence("projectionBeforeDisable", {
                "generation": projection_before.get("config_generation"),
                "keyEnabled": before_entry.get("enabled"),
            })

            # Stop the broker: the disable mutation can still commit in the
            # store, but its publication cannot reach the 8100.
            self.broker.stop()
            disable = self.management_call(
                "POST",
                f"/api/share/keys/{key['key_id']}/revoke-and-terminate",
                {"expected_row_version": key["row_version"]})
            scenario.request(disable,
                             "revoke the Key while the broker is down")
            scenario.check(
                disable.status == 503,
                "the disable fails closed while the broker cannot publish "
                "(status=%r)" % (disable.status,))
            disabled_row = self.get_key(key["key_id"])
            scenario.check(
                disabled_row.get("enabled") is False,
                "the store has the Key disabled (enabled=%r)" % (
                    disabled_row.get("enabled"),))
            projection_after_disable = json.loads(
                self.paths["projection"].read_text(encoding="utf-8"))
            after_entry = next((
                entry for entry in projection_after_disable.get("data", [])
                if entry.get("key_id") == key["key_id"]), {})
            scenario.check(
                projection_after_disable.get("config_generation")
                == projection_before.get("config_generation")
                and after_entry.get("enabled") is True,
                "the projection file still holds the last APPLIED enabled "
                "generation (generation=%r, enabled=%r)" % (
                    projection_after_disable.get("config_generation"),
                    after_entry.get("enabled")))
            scenario.record_evidence("disableWhileBrokerDown", {
                "status": disable.status,
                "body": disable.json(),
                "storeEnabled": disabled_row.get("enabled"),
                "projectionGeneration":
                    projection_after_disable.get("config_generation"),
                "projectionEnabled": after_entry.get("enabled"),
            })

            # Restart the REAL 8100 while the Key is disabled in the store.
            # It restores the persisted APPLIED snapshot (enabled), so the
            # stale projection still admits this request.
            self.share.stop()
            self.start_share()
            restarted = self.instance_status()
            scenario.check(
                restarted.get("ok") is True
                and restarted.get("last_seen_generation") == 0,
                "the restarted 8100 starts from the persisted APPLIED "
                "projection without a load (status=%r)" % (restarted,))

            # The test-only blocker holds the REAL 8110 turn before the
            # request registers, so the request stays PENDING.
            blocker = self.dispatch_gate_call(
                "POST", "/gate/blocker",
                {"keyId": "restart-blocked-key",
                 "requestId": "restart-blocked-request"})
            scenario.request(blocker, "register a rotation blocker")
            result = {}

            def send():
                result["value"] = self.chat(
                    key["secret"], stream=False, prompt=marker, timeout=60.0)

            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            self._wait_dispatch_gate(
                lambda state: len(state.get("registrations", [])) == 1)
            self.dispatch_gate_call("POST", "/gate/release")
            pending_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 2
                and stats.get("committed") == 0)
            scenario.check(
                pending_stats.get("pending") == 2,
                "the restarted 8100 admits the request and the REAL 8110 "
                "keeps it PENDING behind the blocker (pending=%r)"
                % (pending_stats.get("pending"),))
            scenario.check(
                bool(self._revoked_key_pending_tickets(key["key_id"])),
                "the request owns a PENDING ticket at the REAL 8110 "
                "(tickets=%r)" % (
                    self._revoked_key_pending_tickets(key["key_id"]),))

            # Restart the broker and publish the disabled generation through
            # a non-revoking management change.
            self.broker.stop()
            self.start_broker()
            _wait_for(
                lambda: ((self.management_call(
                    "GET", "/api/share/status").json() or {})
                    .get("broker", {}) or {}).get("state") == "ok",
                20.0)
            current = self.get_key(key["key_id"])
            publish = self.management_call(
                "PATCH", f"/api/share/keys/{key['key_id']}",
                {"max_concurrency": 18,
                 "expected_row_version": current["row_version"]})
            scenario.request(publish,
                             "publish the disabled generation after restart")
            publish_body = publish.json() or {}
            status = self.management_call("GET", "/api/share/status")
            scenario.request(status, "management status after the publish")
            scenario.record_evidence("managementStatus", status.json())
            scenario.check(
                publish.status == 200
                and publish_body.get("published") is True
                and publish_body.get("applied_generation")
                == publish_body.get("desired_generation"),
                "the restarted broker publishes and applies the disabled "
                "generation (status=%r, published=%r, desired=%r, "
                "applied=%r)" % (
                    publish.status, publish_body.get("published"),
                    publish_body.get("desired_generation"),
                    publish_body.get("applied_generation")))

            released = self._wait_revoked_key_released(
                key["key_id"], timeout=10.0)
            remaining_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 1, timeout=10.0)
            scenario.check(
                not released,
                "the signed revoke notice from the restarted 8100 released "
                "the PENDING ticket (tickets=%r)" % (released,))
            scenario.check(
                remaining_stats.get("pending") == 1,
                "only the test-only blocker remains PENDING (pending=%r)"
                % (remaining_stats.get("pending"),))
            self.dispatch_gate_call("POST", "/gate/blocker-release")
            final_stats = self._wait_dispatcher_stats(
                lambda stats: stats.get("pending") == 0
                and stats.get("timed_out") == 0)
            scenario.check(
                final_stats.get("timed_out") == 0,
                "the revoke notice, not the cumulative budget, released the "
                "ticket (timed_out=%r)" % (final_stats.get("timed_out"),))

            _wait_for(lambda: result.get("value") is not None, 20.0)
            client_result = result.get("value")
            client_payload = (client_result.json()
                              if client_result is not None else None)
            client_error = (client_payload.get("error")
                            if isinstance(client_payload, dict) else None)
            client_type = (client_error.get("type")
                           if isinstance(client_error, dict) else None)
            scenario.check(
                client_result is not None
                and client_result.status == 429
                and client_type == "share_dispatch_revoked",
                "the terminal sees the documented 429 "
                "share_dispatch_revoked (status=%r, type=%r)" % (
                    None if client_result is None else client_result.status,
                    client_type))
            scenario.check(
                client_result is not None
                and client_result.headers.get("retry-after") == "1",
                "the terminal 429 carries Retry-After: 1 (%r)" % (
                    None if client_result is None
                    else client_result.headers.get("retry-after"),))
            barrier = self._fake_um_barrier_state() or {}
            scenario.check(
                barrier.get("arrivals") == [],
                "the request never reached the fake UM (arrivals=%r)"
                % (barrier.get("arrivals"),))
            ledger = self._read_ledger()
            revoked_row = next((
                row for row in ledger
                if row.get("status") == "share_dispatch_revoked"
                and row.get("key_id") == key["key_id"]), {})
            scenario.check(
                revoked_row.get("status") == "share_dispatch_revoked",
                "the backend recorded share_dispatch_revoked (status=%r)"
                % (revoked_row.get("status"),))
            state = json.loads(
                self.paths["revoke_state"].read_text(encoding="utf-8"))
            applied = (state.get("generations") or {}).get(key["key_id"]) or {}
            scenario.check(
                applied.get("generation")
                == publish_body.get("desired_generation"),
                "the 8110 persisted the notice generation (applied=%r, "
                "desired=%r)" % (
                    applied.get("generation"),
                    publish_body.get("desired_generation")))
            instance_after = self.instance_status()
            scenario.check(
                instance_after.get("last_seen_generation")
                == publish_body.get("desired_generation"),
                "the restarted 8100 applied the disabled generation "
                "(last_seen=%r)" % (
                    instance_after.get("last_seen_generation"),))
            scenario.record_evidence("afterRestartRevoke", {
                "publish": publish_body,
                "client": (None if client_result is None
                           else client_result.as_record()),
                "arrivals": barrier.get("arrivals"),
                "ledger": revoked_row,
                "appliedGeneration": applied,
                "instance": instance_after,
            })
            scenario.classify(
                "the 8100 restart during the Key's disabled period restores "
                "the last APPLIED enabled projection; the next publication "
                "sends the revoke notice and the REAL 8110 releases the "
                "PENDING ticket with the documented terminal 429")
            self.dispatch_gate_call("POST", "/gate/disarm")
            thread.join(timeout=5)
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/blocker-release")
            with contextlib.suppress(Exception):
                self.dispatch_gate_call("POST", "/gate/disarm")
            return scenario.finish(exc)

    def scenario_cancel_release(self):
        scenario = Scenario("cancel_release_and_late_result")
        self.scenarios.append(scenario)
        try:
            key = self.create_key("cancel-key", max_concurrency=1)
            self.keys["CANCEL"] = key
            self.fake_um.reset()
            self.fake_um.config.slow_first_chunk_s = 5.0
            self.fake_um.config.chunk_delay_s = 2.0
            self.fake_um.config.chunk_size = 2
            self.fake_um.set_mode(self.fake_um_module.MODE_SLOW_FIRST_CHUNK)

            cancelled = {"done": False}

            def cancel_worker():
                result = _http_request(
                    SHARE_PORT, "POST", "/v1/chat/completions",
                    body={"model": SHARE_MODEL, "stream": True,
                          "messages": [{"role": "user",
                                        "content": "cancel me"}]},
                    key=key["secret"], timeout=60.0,
                    close_after_headers=True)
                cancelled["result"] = result

            thread = threading.Thread(target=cancel_worker, daemon=True)
            thread.start()
            _wait_for(lambda: self._chat_count() >= 1, 20.0)
            _wait_for(lambda: cancelled.get("result") is not None, 20.0)
            scenario.request(cancelled.get("result") or HttpResult(
                "POST", "/v1/chat/completions", None, {}, b"", 0,
                "no result"), "cancelled client")

            # Wait for the cancelled request's upstream to end, so its release is
            # the only one that can be observed.  Then admit request 2 and keep
            # it in flight while request 1's late result arrives.
            _wait_for(lambda: self._chat_count() >= 1, 20.0)
            time.sleep(5.5)
            admitted = self.chat(key["secret"], stream=False,
                                 prompt="after cancel", timeout=60.0)
            scenario.request(admitted, "request after cancel")
            scenario.check(admitted.status == 200,
                            "cancelling releases the slot exactly once")

            _wait_for(lambda: self._chat_count() >= 2, 20.0)
            started_event = threading.Event()
            finish_event = threading.Event()
            held = {}

            def hold_second():
                held["result"] = _hold_open_request(
                    SHARE_PORT, "POST", "/v1/chat/completions",
                    {"model": SHARE_MODEL, "stream": True,
                     "messages": [{"role": "user",
                                   "content": "hold second"}]},
                    key["secret"], started_event, finish_event,
                    timeout=60.0)

            hold_thread = threading.Thread(target=hold_second, daemon=True)
            hold_thread.start()
            if not started_event.wait(20.0):
                raise TimeoutError("the second request never reached the gateway")
            _wait_for(lambda: self._chat_count() >= 3, 20.0)
            # The first request's late result has already happened while the
            # second request owns the only per-Key slot.  A third request must
            # still be refused: a late result never frees a NEW slot.
            rejected = self.chat(key["secret"], stream=False,
                                  prompt="while second in flight")
            scenario.request(rejected, "third request while second in flight")
            payload = rejected.json() or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            reason = error.get("reason") if isinstance(error, dict) else None
            scenario.check(rejected.status == 429 and
                            reason == "per_key_concurrency",
                            "a late result never frees the second request's slot")
            finish_event.set()
            hold_thread.join(timeout=30)
            scenario.check(held["result"].status == 200,
                            "the second request finishes successfully")
            final = None
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                result = self.chat(key["secret"], stream=False,
                                    prompt="after second", timeout=30.0)
                if result.status == 200:
                    final = result
                    break
                time.sleep(0.1)
            scenario.request(final or HttpResult(
                "POST", "/v1/chat/completions", 429, {}, b"", 0,
                "not admitted"), "request after second finishes")
            scenario.check(final is not None and final.status == 200,
                            "the slot is released once the second request ends")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_q11(self):
        scenario = Scenario("q11_usage_write_read_summary")
        self.scenarios.append(scenario)
        try:
            key_unknown = self.create_key("q11-unknown", max_concurrency=17)
            key_zero = self.create_key("q11-zero", max_concurrency=17)
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)

            unknown = self.chat(key_unknown["secret"], stream=False,
                                 prompt="cache unknown")
            scenario.request(unknown, "unknown cache report")
            scenario.check(unknown.status == 200,
                            "the unknown-cache request succeeds")
            unknown_request_id = unknown.headers.get("x-share-request-id")

            original_usage = self.fake_um_module._usage

            def usage_with_zero(payload, reply):
                usage = original_usage(payload, reply)
                usage["prompt_tokens_details"] = {"cached_tokens": 0}
                return usage

            self.fake_um_module._usage = usage_with_zero
            try:
                zero = self.chat(key_zero["secret"], stream=False,
                                  prompt="cache explicit zero")
            finally:
                self.fake_um_module._usage = original_usage
            scenario.request(zero, "explicit zero cache report")
            scenario.check(zero.status == 200,
                            "the explicit-zero request succeeds")
            zero_request_id = zero.headers.get("x-share-request-id")

            ledger = self._read_ledger()
            unknown_row = next(
                (row for row in ledger
                 if row.get("key_id") == key_unknown["key_id"]), None)
            zero_row = next(
                (row for row in ledger
                 if row.get("key_id") == key_zero["key_id"]), None)
            scenario.check(unknown_row is not None,
                            "the usage ledger contains the unknown-cache request")
            scenario.check(zero_row is not None,
                            "the usage ledger contains the explicit-zero request")
            if unknown_row is not None and zero_row is not None:
                scenario.check(
                    unknown_row.get("cached_input_tokens") == 0,
                    "unknown cache is recorded (currently as 0)")
                scenario.check(
                    zero_row.get("cached_input_tokens") == 0,
                    "explicit zero cache is recorded as 0")
                scenario.check(
                    "cache_report_status" in unknown_row
                    or "cache_report" in unknown_row
                    or "prompt_tokens_details" in unknown_row,
                    "unknown cache carries a distinguishable report status "
                    f"(cached={unknown_row.get('cached_input_tokens')!r}, "
                    f"source={unknown_row.get('usage_source')!r})")
                scenario.check(
                    "cache_report_status" in zero_row
                    or "cache_report" in zero_row
                    or "prompt_tokens_details" in zero_row,
                    "explicit zero cache carries a distinguishable report status "
                    f"(cached={zero_row.get('cached_input_tokens')!r}, "
                    f"source={zero_row.get('usage_source')!r})")
                scenario.check(
                    unknown_row.get("cached_input_tokens")
                    != zero_row.get("cached_input_tokens")
                    or ("cache_report_status" in unknown_row
                        and "cache_report_status" in zero_row
                        and unknown_row.get("cache_report_status")
                        != zero_row.get("cache_report_status")),
                    "unknown cache and explicit zero are distinguishable")
                scenario.check(
                    unknown_row.get("request_id") == unknown_request_id,
                    "the ledger row carries the response request id")
                scenario.check(
                    zero_row.get("request_id") == zero_request_id,
                    "the explicit-zero ledger row carries its request id")

            usage = self.usage()
            scenario.request(usage, "GET /api/share/usage")
            scenario.check(usage.status == 200,
                            "the share usage read returns 200")
            payload = usage.json() or {}
            scenario.check(payload.get("requests", 0) >= 2,
                            "the usage summary counts both requests")
            keys = payload.get("keys") if isinstance(payload, dict) else None
            ids = [entry.get("key_id") for entry in keys or []
                   if isinstance(entry, dict)]
            scenario.check(key_unknown["key_id"] in ids,
                            "the usage summary includes the unknown-cache Key")
            scenario.check(key_zero["key_id"] in ids,
                            "the usage summary includes the explicit-zero Key")
            scenario.check("tokenUsage" in json.dumps(payload),
                            "the usage summary exposes tokenUsage")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    # ------------------------------------------------------------ helpers
    def _chat_count(self) -> int:
        if self.fake_um is None:
            return 0
        state = self.fake_um.state()
        return sum(1 for entry in state.get("recent", [])
                   if entry.get("path") in ("/api/chat/completions",
                                            "/api/v1/messages"))

    def _read_ledger(self):
        path = self.paths["usage_ledger"]
        rows = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _read_faults(self):
        """The REAL 8100 gateway's bounded append-only fault log."""
        path = self.paths["share_faults"]
        rows = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    # ------------------------------------------------------------ main
    def run_core(self) -> dict:
        report = {
            "kind": "um-share-linux-chain-core",
            "generatedAt": _now_iso(),
            "host": {
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "cwd": str(HERE),
                "uid": os.getuid() if hasattr(os, "getuid") else None,
            },
            "ports": {
                "share": SHARE_PORT,
                "backend": BACKEND_PORT,
                "management": MANAGEMENT_PORT,
                "fakeUm": FAKE_UM_PORT,
            },
            "tempDir": str(self.root),
            "notes": self.notes,
            "scenarios": [],
        }
        try:
            self.prepare()
            self.start_fake_um()
            self.start_backend()
            self.start_share()
            self.start_broker()
            self.start_management()

            # The initial keys used by the read/protocol scenarios are created
            # through the REAL management API, so their projection was loaded by
            # the REAL instance channel before any request below.
            self.keys = {}
            for name in ("A", "B", "C"):
                self.keys[name] = self.create_key(f"core-{name}",
                                                  max_concurrency=17)
            self.set_share_max(64)

            report["scenarios"].append(self.scenario_models_normal())
            report["scenarios"].append(self.scenario_models_stale_cache())
            report["scenarios"].append(self.scenario_chat_stream())
            report["scenarios"].append(self.scenario_chat_nonstream())
            report["scenarios"].append(self.scenario_messages_stream())
            report["scenarios"].append(self.scenario_responses_tool_result())
            report["scenarios"].append(self.scenario_invalid_key())
            report["scenarios"].append(self.scenario_management_chain())
            report["scenarios"].append(self.scenario_management_rotate())
            report["scenarios"].append(self.scenario_management_revoke())
            report["scenarios"].append(self.scenario_rate_limit())
            report["scenarios"].append(self.scenario_per_key_concurrency())
            report["scenarios"].append(self.scenario_global_concurrency())
            report["scenarios"].append(self.scenario_cancel_release())
            report["scenarios"].append(self.scenario_q11())
            # The last two scenarios restart the REAL backend with the
            # test-only dispatch poll gate and a shortened cumulative budget.
            report["scenarios"].append(self.scenario_dispatch_rotation())
            report["scenarios"].append(self.scenario_dispatch_cumulative())
            # R-C5: revoke-to-send boundary with a longer dispatch budget,
            # so the PENDING ticket is still observable after the revoke.
            report["scenarios"].append(self.scenario_revoke_send_boundary())
            # R-C5: a GRANTED-but-not-committed ticket closed by the REAL
            # management revoke immediately before its commit.
            report["scenarios"].append(
                self.scenario_revoke_granted_not_committed())
            # R-C5: the 8100->8110 window: a request already admitted by
            # 8100 but held before its upstream send when the revoke lands.
            report["scenarios"].append(self.scenario_revoke_relay_window())
            # R6 #4: an 8100 restart while the Key is disabled in the store;
            # the next publication sends the revoke notice and the REAL 8110
            # releases the PENDING ticket.
            report["scenarios"].append(
                self.scenario_revoke_after_gateway_restart())
        except Exception as exc:  # noqa: BLE001
            report["setupError"] = f"{type(exc).__name__}: {exc}"
            report["setupTrace"] = traceback.format_exc()[-4000:]
        finally:
            self.stop_all()
            report["processLogs"] = {
                "fakeUm": _tail(self.paths["fake_um_log"]),
                "backend": _tail(self.paths["chat2api_stdout"]),
                "share": _tail(self.paths["share_stdout"]),
                "broker": _tail(self.paths["broker_stdout"]),
                "management": _tail(self.paths["management_stdout"]),
            }
            passed = sum(1 for item in report["scenarios"]
                         if item["status"] == "passed")
            failed = sum(1 for item in report["scenarios"]
                         if item["status"] == "failed")
            skipped = sum(1 for item in report["scenarios"]
                          if item["status"] == "skipped")
            report["summary"] = {
                "total": len(report["scenarios"]),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            }
            report["ok"] = (failed == 0 and "setupError" not in report
                             and passed > 0)
            report["exitCode"] = 0 if report["ok"] else 1
            if not report["ok"]:
                report["exitCode"] = 2 if "setupError" in report else 1
        return report


# --------------------------------------------------------------------------
# long-request / resource-fallback scenarios
# --------------------------------------------------------------------------
class LongChainFixture(ChainFixture):
    """Exercise long-request budgets and post-cancel resource fallback.

    The product's production defaults (120s first-byte, 180s silence, 360s
    wall-clock, 12 physical attempts) are verified as real constants, then the
    same code paths are driven with shortened budgets in an isolated child
    process.  No production default is changed and no 120/180/360s wait is
    performed.
    """

    def __init__(self, report_path: Path):
        super().__init__(report_path)
        self.paths["long_entry"] = self.root / "long_entry.py"
        self.paths["long_call_log"] = self.root / "long-call-log.jsonl"
        self.paths["long_plain_profile"] = (
            self.root / "profile" / "long-plain")
        self._write_long_entry()

    # ------------------------------------------------------------ launcher
    def _write_long_entry(self):
        """A test-only launcher that shortens constants then runs the real main.

        This is not a product change: ``chat2api.main()`` still parses the same
        CLI, builds the same Proxy/Handler and serves the same routes.  Only the
        module constants and a read-only call-time recorder are injected.
        """
        self.paths["long_entry"].write_text(
            "import json\n"
            "import os\n"
            "import sys\n"
            "import threading\n"
            "import chat2api\n"
            "\n"
            "first = os.environ.get('CHAIN_TEST_FIRST_BYTE_S')\n"
            "if first:\n"
            "    chat2api.STREAM_FIRST_BYTE_TIMEOUT_S = float(first)\n"
            "silence = os.environ.get('CHAIN_TEST_SOCKET_S')\n"
            "if silence:\n"
            "    chat2api.STREAM_SOCKET_TIMEOUT_S = float(silence)\n"
            "log_path = os.environ.get('CHAIN_TEST_CALL_LOG')\n"
            "if log_path:\n"
            "    lock = threading.Lock()\n"
            "    original = chat2api.Proxy.call\n"
            "    def _record(self, method, path, *args, **kwargs):\n"
            "        timeout = kwargs.get('timeout')\n"
            "        record = {'method': method, 'path': path,\n"
            "                  'stream': kwargs.get('stream'),\n"
            "                  'timeout': (list(timeout)\n"
            "                              if isinstance(timeout, (list, tuple))\n"
            "                              else timeout)}\n"
            "        with lock:\n"
            "            with open(log_path, 'a', encoding='utf-8') as handle:\n"
            "                handle.write(json.dumps(record) + '\\n')\n"
            "        return original(self, method, path, *args, **kwargs)\n"
            "    chat2api.Proxy.call = _record\n"
            "sys.argv = ['chat2api.py'] + sys.argv[1:]\n"
            "raise SystemExit(chat2api.main())\n",
            encoding="utf-8")

    def prepare_long(self):
        self.prepare()
        self.paths["long_plain_profile"].mkdir(parents=True, exist_ok=True)
        self.paths["long_call_log"].write_text("", encoding="utf-8")

    def _long_env(self, *, max_request_s=LONG_MAX_REQUEST_S,
                  first_byte_s=LONG_FIRST_BYTE_S,
                  silence_s=LONG_SILENCE_S,
                  fast_retries=0, slow_retries=0,
                  slow_threshold=LONG_SLOW_THRESHOLD_S,
                  max_attempts=LONG_MAX_ATTEMPTS,
                  repetition_retries=0):
        env = dict(self.env)
        env.update({
            "CHAT2API_MAX_REQUEST_SECONDS": str(max_request_s),
            "CHAT2API_UPSTREAM_AUTO_RETRY": "1" if fast_retries else "0",
            "CHAT2API_UPSTREAM_AUTO_RETRIES": str(fast_retries),
            "CHAT2API_UPSTREAM_SLOW_AUTO_RETRIES": str(slow_retries),
            "CHAT2API_UPSTREAM_SLOW_RETRY_THRESHOLD_S": str(slow_threshold),
            "CHAT2API_MAX_TOTAL_ATTEMPTS": str(max_attempts),
            "CHAT2API_REPETITION_AUTO_RETRY": (
                "1" if repetition_retries else "0"),
            "CHAT2API_REPETITION_AUTO_RETRIES": str(repetition_retries),
            "CHAIN_TEST_CALL_LOG": str(self.paths["long_call_log"]),
        })
        if first_byte_s is None:
            env.pop("CHAIN_TEST_FIRST_BYTE_S", None)
        else:
            env["CHAIN_TEST_FIRST_BYTE_S"] = str(first_byte_s)
        if silence_s is None:
            env.pop("CHAIN_TEST_SOCKET_S", None)
        else:
            env["CHAIN_TEST_SOCKET_S"] = str(silence_s)
        return env

    def _start_backend_group(self, name, *, profile=None, **env_kwargs):
        if getattr(self, "backend", None) is not None:
            self.backend.stop()
        self.paths["long_call_log"].write_text("", encoding="utf-8")
        env = self._long_env(**env_kwargs)
        self.start_backend(
            env=env, entry=self.paths["long_entry"],
            profile=profile or self.paths["profile"])
        time.sleep(0.5)

    # ------------------------------------------------------------ evidence
    def _log_offset(self) -> int:
        try:
            return self.paths["chat2api_log"].stat().st_size
        except OSError:
            return 0

    def _log_chunk(self, offset: int) -> str:
        try:
            data = self.paths["chat2api_log"].read_bytes()[offset:]
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    def _read_call_log(self):
        rows = []
        try:
            text = self.paths["long_call_log"].read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _last_chat_call(self, stream=None):
        for row in reversed(self._read_call_log()):
            if row.get("path") not in (
                    "/v1/chat/completions", "/api/chat/completions"):
                continue
            if stream is None or row.get("stream") is stream:
                return row
        return None

    def _module_constants(self) -> dict:
        code = (
            "import json\n"
            "import chat2api\n"
            "import share_entry\n"
            "print(json.dumps({\n"
            "  'firstByte': chat2api.STREAM_FIRST_BYTE_TIMEOUT_S,\n"
            "  'silence': chat2api.STREAM_SOCKET_TIMEOUT_S,\n"
            "  'maxRequest': chat2api.MAX_REQUEST_SECONDS,\n"
            "  'maxAttempts': chat2api.MAX_TOTAL_ATTEMPTS,\n"
            "  'fastRetries': chat2api.UPSTREAM_AUTO_RETRIES,\n"
            "  'slowRetries': chat2api.UPSTREAM_SLOW_AUTO_RETRIES,\n"
            "  'slowThreshold': "
            "chat2api.UPSTREAM_SLOW_RETRY_THRESHOLD_S,\n"
            "  'readCapNominal': share_entry.NONSTREAM_SOCKET_TIMEOUT_S,\n"
            "}))\n")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("CHAT2API_")}
        env["PYTHONPATH"] = os.pathsep.join(
            [str(HERE), str(HERE / "chat2api"), str(TESTS)])
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(HERE), env=env,
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"module constant probe failed: {result.stderr[-2000:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def _scenario(self, name, classification):
        scenario = Scenario(name)
        scenario.classify(classification)
        self.scenarios.append(scenario)
        return scenario

    # ------------------------------------------------------------ scenarios
    def scenario_default_limits(self):
        scenario = self._scenario(
            "default_limits", "defaults_120_180_360_12_5_2")
        try:
            constants = self._module_constants()
            scenario.record_evidence("constants", constants)
            scenario.check(constants.get("firstByte") == 120,
                            "default first-byte budget is 120s")
            scenario.check(constants.get("silence") == 180,
                            "default socket silence budget is 180s")
            scenario.check(constants.get("maxRequest") == 360,
                            "default request wall-clock deadline is 360s")
            scenario.check(constants.get("maxAttempts") == 12,
                            "default physical attempt cap is 12")
            scenario.check(constants.get("fastRetries") == 5,
                            "default fast retry budget is 5")
            scenario.check(constants.get("slowRetries") == 2,
                            "default slow retry budget is 2")
            scenario.check(constants.get("slowThreshold") == 30,
                            "default slow-attempt threshold is 30s")
            scenario.check(constants.get("readCapNominal") == 600,
                            "non-stream nominal read cap is 600s")
            scenario.note("constants were read from a fresh child process "
                          "with CHAT2API_* variables stripped; no 120/180s "
                          "wait was performed")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_first_byte_timeout(self):
        scenario = self._scenario(
            "first_byte_timeout", "first_byte_timeout_fires_first")
        try:
            self.fake_um.reset()
            self.fake_um.config.silent_s = 8.0
            self._start_backend_group(
                "timeouts", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_FIRST_BYTE_S,
                silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(self.fake_um_module.MODE_SILENT)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="first byte timeout", timeout=20.0)
            scenario.request(result, "silent upstream before first data")
            chunk = self._log_chunk(offset)
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("backendLogTail", chunk[-4000:])
            scenario.check(result.status == 502,
                           "first-byte timeout returns 502")
            scenario.check(900 <= result.duration_ms <= 3500,
                           "first-byte timeout fires near the shortened "
                           "1.2s budget, not the 10s request deadline")
            scenario.check("status=upstream_first_byte_timeout" in chunk,
                           "backend records upstream_first_byte_timeout")
            scenario.check("final=upstream_first_byte_timeout" in chunk,
                           "backend summary names the first-byte limit")
            scenario.check(self._chat_count() == 1,
                           "exactly one upstream attempt: retries disabled")
            scenario.note("default 120s first-byte budget shortened to 1.2s "
                          "in the isolated backend; the request deadline "
                          "was 10s, so the first-byte limit won")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_comments_blank(self):
        scenario = self._scenario(
            "comments_blank_not_first_data",
            "comments_and_blank_lines_do_not_satisfy_first_data")
        try:
            self.fake_um.reset()
            self.fake_um.config.comments_duration_s = 8.0
            self.fake_um.config.comment_interval_s = 0.1
            self._start_backend_group(
                "timeouts", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_FIRST_BYTE_S,
                silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(self.fake_um_module.MODE_COMMENTS_UNTIL)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="comments only", timeout=20.0)
            scenario.request(result, "comments and blank lines only")
            chunk = self._log_chunk(offset)
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("clientBody", result.text()[:1000])
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("backendLogTail", chunk[-4000:])
            scenario.check(result.status == 502,
                           "comment-only stream returns 502")
            scenario.check(900 <= result.duration_ms <= 3500,
                           "first-byte timeout fires despite live comments")
            scenario.check("data:" not in result.text(),
                           "no data event is fabricated from comments")
            scenario.check("status=upstream_first_byte_timeout" in chunk,
                           "backend records upstream_first_byte_timeout")
            scenario.check(self._chat_count() == 1,
                           "the comment stream reached the upstream once")
            scenario.note("the fake upstream actually wrote comment/blank "
                          "SSE events; they did not satisfy the first-data "
                          "budget")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_silence_after_first(self):
        scenario = self._scenario(
            "silence_after_first_chunk",
            "socket_silence_fires_after_first_data")
        try:
            self.fake_um.reset()
            self.fake_um.config.silent_after_first_s = 8.0
            self._start_backend_group(
                "timeouts", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_FIRST_BYTE_S,
                silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(
                self.fake_um_module.MODE_SILENT_AFTER_FIRST)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="silence after first", timeout=20.0)
            scenario.request(result, "first data then silence")
            chunk = self._log_chunk(offset)
            text = result.text()
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("firstDataSeen",
                                      "first-visible" in _sse_text(
                                          result.body))
            scenario.record_evidence("clientBody", text[:1500])
            scenario.record_evidence("backendLogTail", chunk[-4000:])
            scenario.check(result.status == 200,
                           "the 200 is committed by the first data chunk")
            scenario.check("first-visible" in _sse_text(result.body),
                           "the first content chunk reached the client")
            scenario.check("upstream_read_error" in text,
                           "the stream ends with one in-band upstream_read_error")
            scenario.check(1500 <= result.duration_ms <= 6000,
                           "the silence window (2s) fires before the 10s "
                           "request deadline")
            scenario.check("status=upstream_read_error" in chunk,
                           "backend records upstream_read_error")
            scenario.note("the upstream read failed as ConnectionError after "
                          "the shortened 2s silence window; the first data "
                          "chunk had already committed the 200")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_nonstream_read_cap_short(self):
        scenario = self._scenario(
            "nonstream_read_cap_clipped_short",
            "request_deadline_clips_600s_nonstream_read_cap")
        try:
            self.fake_um.reset()
            self.fake_um.config.nonstream_hang_s = 20.0
            self._start_backend_group(
                "readcap-short", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_FIRST_BYTE_S,
                silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(
                self.fake_um_module.MODE_NONSTREAM_HANG)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=False,
                              prompt="nonstream hang", timeout=30.0)
            scenario.request(result, "non-stream upstream sends headers then hangs")
            chunk = self._log_chunk(offset)
            call = self._last_chat_call(stream=False) or {}
            timeout_value = call.get("timeout")
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("proxyCallTimeout", timeout_value)
            scenario.record_evidence("backendLogTail", chunk[-4000:])
            scenario.check(result.status == 502,
                           "a hung non-stream read ends as 502")
            scenario.check(9000 <= result.duration_ms <= 12500,
                           "the 10s request deadline clips the read")
            scenario.check(isinstance(timeout_value, list)
                           and len(timeout_value) == 2,
                           "the real Proxy.call received a timeout tuple")
            if isinstance(timeout_value, list) and len(timeout_value) == 2:
                scenario.check(9.0 <= timeout_value[1] <= 10.5,
                               "non-stream read timeout is the remaining "
                               "deadline (~10s), not 600s")
                scenario.check(timeout_value[1] < LONG_READ_CAP_NOMINAL_S,
                               "the 600s nominal read cap is clipped")
            scenario.note("the isolated backend ran with a 10s wall-clock "
                          "deadline; the nominal 600s non-stream read cap "
                          "was clipped at the real Proxy.call boundary")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_nonstream_read_cap_default(self):
        scenario = self._scenario(
            "nonstream_read_cap_default_360",
            "default_360s_deadline_clips_600s_read_cap")
        try:
            self.fake_um.reset()
            self.fake_um.set_mode(self.fake_um_module.MODE_NORMAL)
            self._start_backend_group(
                "readcap-default", max_request_s=360,
                first_byte_s=None, silence_s=None,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=False,
                              prompt="default read cap", timeout=30.0)
            scenario.request(result, "normal non-stream request")
            call = self._last_chat_call(stream=False) or {}
            timeout_value = call.get("timeout")
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("proxyCallTimeout", timeout_value)
            scenario.record_evidence("backendLogTail",
                                      self._log_chunk(offset)[-2000:])
            scenario.check(result.status == 200,
                           "the normal non-stream request succeeds")
            scenario.check(isinstance(timeout_value, list)
                           and len(timeout_value) == 2,
                           "the real Proxy.call received a timeout tuple")
            if isinstance(timeout_value, list) and len(timeout_value) == 2:
                scenario.check(350 <= timeout_value[1] <= 360,
                               "the default 360s remaining deadline clips "
                               "the 600s non-stream read cap")
                scenario.check(timeout_value[1] < LONG_READ_CAP_NOMINAL_S,
                               "the captured read timeout is below 600s")
            scenario.note("captured at call time; the request itself was "
                          "answered immediately, so no 360s wait was made")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_retry_fast(self):
        scenario = self._scenario(
            "retry_fast_5xx_5", "fast_retry_count_5_exhausted")
        try:
            self.fake_um.reset()
            self._start_backend_group(
                "retry-fast", max_request_s=90,
                first_byte_s=LONG_SILENCE_S, silence_s=LONG_SILENCE_S,
                fast_retries=LONG_FAST_RETRIES,
                slow_retries=LONG_SLOW_RETRIES,
                slow_threshold=LONG_SLOW_THRESHOLD_S,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(self.fake_um_module.MODE_EXPLICIT_5XX)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="fast 5xx", timeout=30.0)
            scenario.request(result, "always explicit 500")
            chunk = self._log_chunk(offset)
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("backendLogTail", chunk[-5000:])
            scenario.check(result.status == 502,
                           "five exhausted fast retries end as 502")
            scenario.check(self._chat_count() == 6,
                           "five retries mean six physical attempts")
            scenario.check("retry=5/5" in chunk,
                           "the backend records retry=5/5")
            scenario.check("physical_attempt=5/12" in chunk,
                           "the fifth retry was within the 12 cap; the "
                           "sixth final attempt is counted by the fake UM")
            scenario.check("stage=upstream_non_200" in chunk,
                           "the last (sixth) attempt is an upstream non-200")
            scenario.note("the fast retry budget (5) exhausted before the "
                          "physical cap (12) and the 90s deadline")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_retry_slow(self):
        scenario = self._scenario(
            "retry_slow_5xx_2", "slow_retry_count_2_exhausted")
        try:
            self.fake_um.reset()
            self.fake_um.config.slow_5xx_s = 1.2
            self._start_backend_group(
                "retry-slow", max_request_s=90,
                first_byte_s=LONG_SILENCE_S, silence_s=LONG_SILENCE_S,
                fast_retries=LONG_FAST_RETRIES,
                slow_retries=LONG_SLOW_RETRIES,
                slow_threshold=LONG_SLOW_THRESHOLD_S,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(self.fake_um_module.MODE_SLOW_5XX)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="slow 5xx", timeout=30.0)
            scenario.request(result, "slow explicit 500 (1.2s each)")
            chunk = self._log_chunk(offset)
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("backendLogTail", chunk[-5000:])
            scenario.check(result.status == 502,
                           "two exhausted slow retries end as 502")
            scenario.check(self._chat_count() == 3,
                           "two slow retries mean three physical attempts")
            scenario.check("slow_retry=2/2" in chunk,
                           "the backend records slow_retry=2/2")
            scenario.check("physical_attempt=2/12" in chunk,
                           "the second retry was within the 12 cap; the "
                           "third final attempt is counted by the fake UM")
            scenario.note("the slow threshold was shortened to 1s so a 1.2s "
                          "attempt is classified slow")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_retry_physical(self):
        scenario = self._scenario(
            "retry_physical_12",
            "physical_attempt_budget_12_refuses_13th_retry")
        try:
            self.fake_um.reset()
            script = (["reasoning_loop"] * 4
                      + ["explicit_5xx"] * 7
                      + ["reasoning_loop"])
            self.fake_um.set_script(script)
            self._start_backend_group(
                "retry-physical", max_request_s=90,
                first_byte_s=LONG_SILENCE_S, silence_s=LONG_SILENCE_S,
                fast_retries=10, slow_retries=LONG_SLOW_RETRIES,
                slow_threshold=LONG_SLOW_THRESHOLD_S,
                max_attempts=LONG_MAX_ATTEMPTS,
                repetition_retries=LONG_REPETITION_RETRIES,
                profile=self.paths["long_plain_profile"])
            self.fake_um.set_mode(self.fake_um_module.MODE_SCRIPTED)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="physical twelve", timeout=60.0)
            scenario.request(result, "4 reasoning loops + 7 fast 5xx + 1 loop")
            chunk = self._log_chunk(offset)
            text = result.text()
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("scriptRemaining",
                                      self.fake_um.script_remaining())
            scenario.record_evidence("clientBodyTail", text[-2000:])
            scenario.record_evidence("backendLogTail", chunk[-6000:])
            scenario.check(result.status == 200,
                           "the reasoning first event committed the 200")
            scenario.check("degenerate_repetition" in text
                           and "[DONE]" not in text,
                           "the 12th attempt fails in-band with "
                           "degenerate_repetition, never as a fabricated "
                           "success terminal")
            scenario.check(self._chat_count() == LONG_MAX_ATTEMPTS,
                           "exactly 12 physical requests reached the upstream")
            scenario.check("physical_attempts=12/12" in chunk,
                           "the backend summary records physical_attempts=12/12")
            scenario.check("phase=preflight_retry_exhausted" in chunk,
                           "the fifth repetition retry was refused once the "
                           "12/12 physical budget was exhausted")
            scenario.check("repetition_retries=4" in chunk,
                           "four repetition retries ran before the physical "
                           "budget stopped the next one")
            scenario.check("upstream auto-retry" in chunk
                           and "retry=7/10" in chunk,
                           "seven transient retries were used before the cap")
            scenario.note("the 200 is not a success: the body carries the "
                          "in-band degenerate_repetition error and no "
                          "[DONE]; at 12/12 the handler's preflight stopped "
                          "the fifth repetition retry before opening a 13th "
                          "upstream request, and the real budget class "
                          "separately refuses a 13th consume")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_physical_budget_component(self):
        scenario = self._scenario(
            "physical_budget_12_component",
            "real_physical_budget_refuses_13th_consume")
        try:
            code = (
                "import json\n"
                "import chat2api\n"
                "budget = chat2api._PhysicalRequestBudget(12)\n"
                "results = [budget.consume() for _ in range(13)]\n"
                "print(json.dumps({'results': results, 'used': budget.used,\n"
                "                  'remaining': budget.remaining}))\n")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("CHAT2API_")}
            env["PYTHONPATH"] = os.pathsep.join(
                [str(HERE), str(HERE / "chat2api"), str(TESTS)])
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=str(HERE), env=env,
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(
                    f"physical budget probe failed: "
                    f"{result.stderr[-2000:]}")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            scenario.record_evidence("physicalBudget", payload)
            scenario.check(payload.get("results", [])[:12]
                           == [True] * 12,
                           "the real budget admits exactly 12 consumes")
            scenario.check(len(payload.get("results", [])) == 13
                           and payload["results"][12] is False,
                           "the real budget refuses the 13th consume")
            scenario.check(payload.get("used") == 12
                           and payload.get("remaining") == 0,
                           "the real budget ends used=12 remaining=0")
            scenario.note("the end-to-end retry_physical_12 scenario reaches "
                          "12 real upstream requests; this component check "
                          "proves the real budget class refuses a 13th")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_retry_deadline(self):
        scenario = self._scenario(
            "retry_deadline_first", "request_deadline_fires_before_retries")
        try:
            self.fake_um.reset()
            self._start_backend_group(
                "retry-deadline", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_SILENCE_S, silence_s=LONG_SILENCE_S,
                fast_retries=10, slow_retries=LONG_SLOW_RETRIES,
                slow_threshold=LONG_SLOW_THRESHOLD_S,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(self.fake_um_module.MODE_EXPLICIT_5XX)
            offset = self._log_offset()
            result = self.chat(self.keys["A"]["secret"], stream=True,
                              prompt="retry deadline", timeout=30.0)
            scenario.request(result, "always explicit 500 under a 10s deadline")
            chunk = self._log_chunk(offset)
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("elapsedMs", result.duration_ms)
            scenario.record_evidence("fakeUmRequestCount",
                                      self._chat_count())
            scenario.record_evidence("backendLogTail", chunk[-5000:])
            scenario.check(result.status == 502,
                           "the expired retry delay returns 502")
            scenario.check(9000 <= result.duration_ms <= 13000,
                           "the 10s wall-clock deadline fires before the "
                           "10-retry / 12-physical budgets")
            scenario.check(self._chat_count() < LONG_MAX_ATTEMPTS,
                           "the deadline stops before the 12th physical attempt")
            scenario.check("stage=request_deadline_exceeded" in chunk
                           or "final=request_deadline_exceeded" in chunk,
                           "the backend summary names the request deadline")
            scenario.note("default 360s wall-clock deadline shortened to 10s; "
                          "the retry sleep was clipped by the remaining "
                          "deadline, so the deadline won")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    def scenario_cancel_release(self):
        scenario = self._scenario(
            "cancel_resource_release",
            "cancel_then_reader_fd_socket_fallback")
        try:
            self.fake_um.reset()
            self.fake_um.config.silent_after_first_s = 10.0
            self._start_backend_group(
                "cancel", max_request_s=90,
                first_byte_s=LONG_SILENCE_S, silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.fake_um.set_mode(
                self.fake_um_module.MODE_SILENT_AFTER_FIRST)
            time.sleep(1.0)
            backend_before = _stable_proc_snapshot(self.backend.process.pid)
            share_before = _stable_proc_snapshot(self.share.process.pid)
            during = {}

            def capture_during_first_data():
                during["backend"] = _proc_snapshot(self.backend.process.pid)
                during["share"] = _proc_snapshot(self.share.process.pid)

            offset = self._log_offset()
            result = _read_first_stream_event(
                SHARE_PORT,
                {"model": SHARE_MODEL, "stream": True,
                 "messages": [{"role": "user",
                               "content": "cancel after first"}]},
                self.keys["A"]["secret"], timeout=20.0,
                on_first_data=capture_during_first_data)
            scenario.request(result, "client closes after the first data chunk")
            scenario.record_evidence("clientStatus", result.status)
            scenario.record_evidence("firstDataSeen",
                                      b"first-visible" in result.body)
            scenario.check(result.status == 200,
                           "the first data chunk committed the 200")
            scenario.check(b"first-visible" in result.body,
                           "the client actually received substantive data "
                           "before cancelling")
            # The share gateway has no client-disconnect watchdog while it is
            # blocked reading a silent upstream; the backend's shortened 2s
            # silence window is what releases the reader and both sockets.
            time.sleep(6.0)
            backend_after, backend_settle_ms = _wait_proc_settle(
                self.backend.process.pid, backend_before, timeout=12.0)
            share_after, share_settle_ms = _wait_proc_settle(
                self.share.process.pid, share_before, timeout=12.0)
            chunk = self._log_chunk(offset)
            scenario.record_evidence("backendBefore", backend_before)
            scenario.record_evidence("backendDuringFirstData",
                                      during.get("backend"))
            scenario.record_evidence("backendAfter", backend_after)
            scenario.record_evidence("backendSettleMs", backend_settle_ms)
            scenario.record_evidence("shareBefore", share_before)
            scenario.record_evidence("shareDuringFirstData",
                                      during.get("share"))
            scenario.record_evidence("shareAfter", share_after)
            scenario.record_evidence("shareSettleMs", share_settle_ms)
            scenario.record_evidence("backendLogTail", chunk[-5000:])
            during_backend = during.get("backend") or {}
            scenario.check(during_backend.get("threads", 0)
                           >= backend_before["threads"] + 2,
                           "two extra backend threads existed while the first "
                           "data chunk was held (handler + upstream reader)")
            scenario.check(during_backend.get("sockets", 0)
                           >= backend_before["sockets"] + 2,
                           "two extra backend sockets existed while the first "
                           "data chunk was held (downstream + upstream)")
            scenario.check(backend_after["threads"]
                           <= backend_before["threads"] + 1,
                           "backend thread count returns to the pre-request "
                           "baseline")
            scenario.check(backend_after["fds"]
                           <= backend_before["fds"] + 1,
                           "backend FD count returns to the pre-request "
                           "baseline")
            scenario.check(backend_after["sockets"]
                           <= backend_before["sockets"] + 1,
                           "backend socket count returns to the "
                           "pre-request baseline")
            scenario.check(share_after["threads"]
                           <= share_before["threads"] + 1,
                           "share gateway thread count returns to baseline")
            scenario.check(share_after["fds"] <= share_before["fds"] + 1,
                           "share gateway FD count returns to baseline")
            scenario.check(share_after["sockets"]
                           <= share_before["sockets"] + 1,
                           "share gateway socket count returns to baseline")
            scenario.check("status=upstream_read_error" in chunk,
                           "the backend reader ended through the silence window")
            scenario.note("the kernel reports every thread comm as python3, so "
                          "reader presence is evidenced by the two extra "
                          "threads/sockets held with the first data chunk and "
                          "the stream-end line, not by a thread name")
            scenario.check("status=upstream_read_error" in chunk
                           or "status=client_disconnected" in chunk,
                           "the cancelled turn releases through the silence "
                           "window rather than leaking")
            scenario.note("the share gateway is blocked on a silent upstream "
                          "and has no disconnect watchdog; the backend's "
                          "shortened 2s silence window frees the reader and "
                          "both processes fall back")
            return scenario.finish()
        except Exception as exc:  # noqa: BLE001
            return scenario.finish(exc)

    # ------------------------------------------------------------ main
    def run_long(self) -> dict:
        report = {
            "kind": "um-share-linux-chain-long",
            "generatedAt": _now_iso(),
            "host": {
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "cwd": str(HERE),
                "uid": os.getuid() if hasattr(os, "getuid") else None,
            },
            "ports": {
                "share": SHARE_PORT,
                "backend": BACKEND_PORT,
                "management": MANAGEMENT_PORT,
                "fakeUm": FAKE_UM_PORT,
            },
            "limits": {
                "defaultFirstByteSeconds": 120,
                "defaultSilenceSeconds": 180,
                "defaultMaxRequestSeconds": 360,
                "defaultMaxTotalAttempts": 12,
                "defaultFastRetries": 5,
                "defaultSlowRetries": 2,
                "shortFirstByteSeconds": LONG_FIRST_BYTE_S,
                "shortSilenceSeconds": LONG_SILENCE_S,
                "shortMaxRequestSeconds": LONG_MAX_REQUEST_S,
                "nominalNonstreamReadCapSeconds": LONG_READ_CAP_NOMINAL_S,
            },
            "tempDir": str(self.root),
            "notes": self.notes,
            "scenarios": [],
            "uncovered": [
                "the real 120s first-byte and 180s silence windows were not "
                "waited; the same code paths were driven with 1.2s/2s in an "
                "isolated child process",
                "the real 360s default deadline and the 600s non-stream read "
                "were not waited; the default constants and the real "
                "Proxy.call timeout tuple were captured instead",
                "the fake UM handler's own threads are not included in the "
                "resource counts; only the product backend and share gateway "
                "processes are measured",
            ],
        }
        try:
            if os.name != "posix" or not Path("/proc/self/fd").exists():
                raise EnvironmentError(
                    "long chain requires Linux /proc for FD/thread evidence")
            self.prepare_long()
            self.start_fake_um()
            self._start_backend_group(
                "timeouts", max_request_s=LONG_MAX_REQUEST_S,
                first_byte_s=LONG_FIRST_BYTE_S,
                silence_s=LONG_SILENCE_S,
                fast_retries=0, slow_retries=0,
                max_attempts=LONG_MAX_ATTEMPTS)
            self.start_share()
            self.start_broker()
            self.start_management()
            self.keys = {}
            for name in ("A", "B", "C"):
                self.keys[name] = self.create_key(f"long-{name}",
                                                  max_concurrency=17)
            self.set_share_max(64)

            report["scenarios"].append(self.scenario_default_limits())
            report["scenarios"].append(self.scenario_first_byte_timeout())
            report["scenarios"].append(self.scenario_comments_blank())
            report["scenarios"].append(self.scenario_silence_after_first())
            report["scenarios"].append(
                self.scenario_nonstream_read_cap_short())
            report["scenarios"].append(
                self.scenario_nonstream_read_cap_default())
            report["scenarios"].append(self.scenario_retry_fast())
            report["scenarios"].append(self.scenario_retry_slow())
            report["scenarios"].append(self.scenario_retry_physical())
            report["scenarios"].append(
                self.scenario_physical_budget_component())
            report["scenarios"].append(self.scenario_retry_deadline())
            report["scenarios"].append(self.scenario_cancel_release())
        except Exception as exc:  # noqa: BLE001
            report["setupError"] = f"{type(exc).__name__}: {exc}"
            report["setupTrace"] = traceback.format_exc()[-4000:]
        finally:
            self.stop_all()
            report["processLogs"] = {
                "fakeUm": _tail(self.paths["fake_um_log"]),
                "backend": _tail(self.paths["chat2api_log"]),
                "share": _tail(self.paths["share_stdout"]),
                "broker": _tail(self.paths["broker_stdout"]),
                "management": _tail(self.paths["management_stdout"]),
            }
            failures = []
            for item in report["scenarios"]:
                for message in item.get("failures", []):
                    failures.append({"scenario": item["name"],
                                     "message": message})
                if item.get("error"):
                    failures.append({"scenario": item["name"],
                                     "message": item["error"]})
            if "setupError" in report:
                failures.append({"scenario": "setup",
                                 "message": report["setupError"]})
            report["failures"] = failures
            passed = sum(1 for item in report["scenarios"]
                         if item["status"] == "passed")
            failed = sum(1 for item in report["scenarios"]
                         if item["status"] == "failed")
            skipped = sum(1 for item in report["scenarios"]
                          if item["status"] == "skipped")
            report["summary"] = {
                "total": len(report["scenarios"]),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            }
            report["ok"] = (not failures and "setupError" not in report
                            and passed > 0)
            report["exitCode"] = 0 if report["ok"] else (
                2 if "setupError" in report else 1)
        return report



def run_fake_um_only() -> dict:
    report = {
        "kind": "fake-um-only",
        "generatedAt": _now_iso(),
        "scenarios": [],
        "ok": False,
    }
    scenario = Scenario("fake_um_contract")
    try:
        module = _load_module("fake_um_server",
                              TESTS / "fake_um" / "fake_um_server.py")
        token = secrets.token_urlsafe(32)
        fake = module.FakeUM(port=FAKE_UM_PORT, token=token,
                            model=SHARE_MODEL)
        fake.start()
        try:
            result = _http_request(
                FAKE_UM_PORT, "GET", "/api/models", key=token)
            scenario.request(result, "fake UM catalog")
            scenario.check(result.status == 200, "fake UM answers the catalog")
            result = _http_request(
                FAKE_UM_PORT, "POST", "/api/chat/completions",
                body={"model": SHARE_MODEL, "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
                key=token)
            scenario.request(result, "fake UM non-stream")
            payload = result.json() or {}
            scenario.check(result.status == 200, "fake UM answers chat")
            scenario.check(bool(payload.get("choices")), "fake UM has a choice")
        finally:
            fake.stop()
        report["scenarios"].append(scenario.finish())
    except Exception as exc:  # noqa: BLE001
        report["scenarios"].append(scenario.finish(exc))
    failed = [item for item in report["scenarios"]
              if item["status"] != "passed"]
    report["ok"] = not failed
    report["exitCode"] = 0 if report["ok"] else 1
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Linux UM share chain end-to-end verification fixture")
    parser.add_argument("--scenario", choices=("core", "long", "fake-um"),
                        default="core")
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)
    default_report = (DEFAULT_LONG_REPORT if args.scenario == "long"
                      else DEFAULT_REPORT)
    report_path = _normalize_report_path(args.report or str(default_report))
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if args.scenario == "fake-um":
        report = run_fake_um_only()
    elif args.scenario == "long":
        if os.name != "posix" or not Path("/proc/self/fd").exists():
            report = {
                "kind": "um-share-linux-chain-long",
                "generatedAt": _now_iso(),
                "scenarios": [],
                "ok": False,
                "setupError": "long chain requires Linux /proc for "
                               "FD/thread evidence",
                "exitCode": 2,
            }
        else:
            fixture = LongChainFixture(report_path)
            report = fixture.run_long()
    else:
        if os.name != "posix":
            report = {
                "kind": "um-share-linux-chain-core",
                "generatedAt": _now_iso(),
                "scenarios": [],
                "ok": False,
                "setupError": "core chain requires Linux (AF_UNIX/SO_PEERCRED)",
                "exitCode": 2,
            }
        else:
            fixture = ChainFixture(report_path)
            report = fixture.run_core()

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps({
        "report": str(report_path),
        "ok": report.get("ok"),
        "exitCode": report.get("exitCode"),
        "summary": report.get("summary"),
    }, ensure_ascii=False, indent=2))
    return int(report.get("exitCode", 1))


if __name__ == "__main__":
    raise SystemExit(main())
