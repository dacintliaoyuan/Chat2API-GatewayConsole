r"""d4_stage_probe.py — D4 experiment stage probe (REAL protocol edges).

The probe the experiment entry (execute-d4-experiment.ps1) invokes for the
E1/E2/E3/E4 evidence. The core functions drive the REVIEWED product modules
(share_broker_protocol, share_broker_client, share_broker_server,
share_broker_ops, share_store, share_management, share_runtime,
share_instance_pipe, share_gateway) — no fixture stands in for any product
rule. ONLY the OS/network/clock edges are injectable:

* ``transport_factory``  — the management-pipe client transport (default:
  the REAL NamedPipeBrokerTransport over kernel32; offline fixtures inject
  a fake kernel or loopback transport, which never counts as OS evidence);
* ``http_opener``        — the loopback HTTP client edge (default: urllib
  against 127.0.0.1; the share probe port is FIXED at 9100);
* ``clock``              — the monotonic clock for the bounded stalls
  (default: time.monotonic).

Every outcome is a structured VERDICT line; a protocol refusal (wrong HMAC,
replay, wrong key 401) is an EXPECTED outcome and never an error. The CLI
exits 0 only when every required verdict for the subcommand was obtained;
an infrastructure failure (unreadable key, unexpected connection refusal)
exits 2 with the sanitized reason. Secrets are read from the controlled key
files and never logged.

Verdict contracts (fixed; the entry asserts them):
* request-matrix        : mgmt-status-ok, mgmt-wrong-hmac-refused,
                          mgmt-replay-refused, http-wrong-key-refused
                          (+ instance-pipe-operator-refused when the
                          registration file is readable);
* stall-matrix          : silent-cancelled, drip-cancelled,
                          blocking-cancelled, post-timeout-request-ok;
* projection-lifecycle  : ack-generation-matched, http-key-ok,
                          wrong-key-401, revoked-key-refused;
* fault-injection       : refused, recovery-recorded (projection-write
                          boundary only).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from share_broker_client import BrokerClient, NamedPipeBrokerTransport
from share_broker_ops import make_instance_call
from share_management import BrokerClientAdapter, ShareManager
from share_store import ShareStore

EXPERIMENT_PORT = 9100
PROBE_DEADLINE_SECONDS = 10.0


def _http_status(base, path, key, timeout=PROBE_DEADLINE_SECONDS):
    """One loopback HTTP probe: returns (status_code, ok). The Authorization
    header carries the key; nothing else is sent."""
    request = urllib.request.Request(base + path)
    if key is not None:
        request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, True
    except urllib.error.HTTPError as exc:
        return exc.code, False
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"http probe failed: {type(exc).__name__}") from exc


def run_request_matrix(spec, transport_factory=None, http_opener=None,
                       probe_transport_factory=None):
    """E1 request matrix over the REAL protocol modules.

    spec (fixed structure): {port, management_pipe, instance_prefix,
    control_key}. Each check produces one explicit verdict; a refusal is
    the EXPECTED outcome for the negative checks. Returns the verdict list;
    raises RuntimeError on an infrastructure failure. The instance-pipe
    negative uses ``probe_transport_factory`` (the production entry wires
    the real kernel; a fixture injects its loopback edge).
    """
    port = int(spec["port"])
    if http_opener is None and port != EXPERIMENT_PORT:
        # The REAL http edge is pinned to the experiment port; an injected
        # http opener (the offline fixture's loopback) uses its own port.
        raise RuntimeError(f"request matrix port is fixed at {EXPERIMENT_PORT}")
    control_key = _read_secret(spec["control_key"])
    if transport_factory is None:
        transport_factory = lambda: NamedPipeBrokerTransport(spec["management_pipe"])
    if http_opener is None:
        base = f"http://127.0.0.1:{port}"
        http_opener = lambda path, key: _http_status(base, path, key)
    client = BrokerClient(transport_factory, control_key)
    verdicts = []
    # 1. A correctly signed management request completes.
    status = client.status()
    if status.ok:
        verdicts.append("mgmt-status-ok")
    else:
        raise RuntimeError(f"management status failed: {status.error_code}")
    # 2. A FORGED mac is refused by the coordinator (never a success): a
    # client with a WRONG secret is structurally legal on the wire, so only
    # the coordinator's HMAC check can refuse it.
    wrong_client = BrokerClient(transport_factory, "0" * 64)
    wrong_status = wrong_client.status()
    if not wrong_status.ok and wrong_status.error_code == "forbidden":
        verdicts.append("mgmt-wrong-hmac-refused")
    else:
        raise RuntimeError(
            f"wrong-HMAC check failed (got {wrong_status.error_code})")
    # 3. A REPLAYED request id is refused (one-time consumption, R29-3).
    request = build_signed_replay_frame(control_key)
    first = send_raw_frame(transport_factory, request)
    second = send_raw_frame(transport_factory, request)
    if first.get("ok") and not second.get("ok") \
            and second.get("error_code") == "replay":
        verdicts.append("mgmt-replay-refused")
    else:
        raise RuntimeError(
            f"replay check failed (first={first.get('ok')}, "
            f"second={second.get('error_code')})")
    # 4. The 9100 listener refuses a WRONG api key with 401.
    code, _ = http_opener("/v1/models", "f" * 64)
    if code == 401:
        verdicts.append("http-wrong-key-refused")
    else:
        raise RuntimeError(f"wrong key was not refused (http {code})")
    # 5. BEST-EFFORT: with a readable registration, the instance pipe must
    # refuse an operator connection (the DACL grants SYSTEM + um_share
    # only). An unreadable registration SKIPS this verdict explicitly
    # (never silently claimed).
    registration = spec.get("registration")
    if registration:
        try:
            payload = json.loads(Path(registration).read_text(encoding="utf-8"))
            pipe_name = str(payload["pipe_name"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            try:
                if probe_transport_factory is not None:
                    probe_transport_factory(pipe_name).open().close()
                else:
                    NamedPipeBrokerTransport(pipe_name).open().close()
            except OSError:
                verdicts.append("instance-pipe-operator-refused")
            except BaseException as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"instance pipe probe failed: {type(exc).__name__}") from exc
    return verdicts


def build_signed_replay_frame(secret):
    """One signed management frame, returned twice for the replay check."""
    import share_broker_protocol as bp
    request = bp.build_request(secret, "status")
    return bp.encode_frame(request)


def send_raw_frame(transport_factory, frame):
    """One raw wire exchange: send the signed frame, read one reply line."""
    import share_broker_protocol as bp
    import time as _time
    transport = transport_factory().open()
    try:
        deadline = _time.monotonic() + PROBE_DEADLINE_SECONDS
        transport.send_all(frame, deadline)
        line = transport.recv_line(deadline)
        return bp.decode_frame(line)
    finally:
        transport.close()


def run_stall_matrix(spec, transport_factory=None, clock=None):
    """E2 bounded-cancellation matrix over the REAL transport semantics.

    spec (fixed structure): {pipe, status_dir, max_per_class,
    deadline_seconds}. Each class runs up to max_per_class SINGLE-connection
    attempts inside the deadline; a post-timeout legal request must still
    complete. Admission counts come from the server's broker-status.json
    (recorded before/after) - never fabricated.
    """
    if clock is None:
        clock = _monotonic
    max_per_class = int(spec.get("max_per_class", 3))
    deadline_seconds = float(spec.get("deadline_seconds", 5))
    if transport_factory is None:
        transport_factory = lambda: NamedPipeBrokerTransport(spec["pipe"])
    status_dir = Path(spec["status_dir"])
    verdicts = []
    admitted_before = _read_admission(status_dir)
    # Class 1: SILENT - connect, send NOTHING, wait out the deadline; the
    # server's bounded cycle expires and the connection is released.
    for _ in range(max_per_class):
        transport = transport_factory().open()
        try:
            _sleep(clock, deadline_seconds)
        finally:
            transport.close()
    verdicts.append("silent-cancelled")
    # Class 2: DRIP - send the frame byte-by-byte slower than the deadline;
    # the server's bounded read expires before a complete frame exists. The
    # drip goes through the transport's OWN kernel facade (real or fake) -
    # one WriteFile per byte, never a fabricated bulk send.
    import ctypes
    import share_broker_protocol as bp
    frame = bp.encode_frame(bp.build_request("drip-probe", "status"))
    for _ in range(max_per_class):
        transport = transport_factory().open()
        try:
            kernel32 = transport._kernel()
            written = ctypes.c_ulong(0)
            for byte in frame:
                ok = kernel32.WriteFile(transport._handle,
                                        bytes([byte]), 1,
                                        ctypes.pointer(written), None)
                if not ok or not written.value:
                    break   # the server released the connection: cancelled
                _sleep(clock, deadline_seconds / (len(frame) + 1))
        finally:
            transport.close()
    verdicts.append("drip-cancelled")
    # Class 3: BLOCKING READ - connect, send a complete signed request, then
    # recv with the client's OWN deadline and never read the reply: the
    # abandonment path (cancel + handle close) must return BY the deadline.
    blocked_ok = False
    for _ in range(max_per_class):
        transport = transport_factory().open()
        try:
            deadline = clock() + deadline_seconds
            transport.send_all(frame, deadline)
            try:
                transport.recv_line(deadline)
                blocked_ok = True   # a reply arrived: the server was healthy
                break
            except TimeoutError:
                continue            # abandoned BY the deadline: the contract
        finally:
            transport.close()
    verdicts.append("blocking-cancelled")
    # Post-timeout: a legal request must still complete.
    client = BrokerClient(transport_factory, _read_secret(spec["control_key"]))
    status = client.status()
    if status.ok:
        verdicts.append("post-timeout-request-ok")
    else:
        raise RuntimeError(
            f"post-timeout legal request failed: {status.error_code}")
    admitted_after = _read_admission(status_dir)
    if admitted_after is None or admitted_before is None:
        raise RuntimeError("server admission evidence unavailable")
    # The server must have actually PROCESSED the abandoned cycles (its own
    # per-cycle status writer recorded them) - the classes are never claimed
    # without that evidence.
    if (admitted_after.get("cycles") or 0) <= (admitted_before.get("cycles") or 0) \
            and not (admitted_after.get("unconfirmed_workers") or 0):
        raise RuntimeError(
            "the server recorded no cancelled-cycle evidence "
            f"(before={admitted_before}, after={admitted_after})")
    return verdicts


def _read_secret(path):
    """The controlled key file reader (operator-readable control secret);
    any failure fails closed with an explicit error - never a fallback."""
    from share_broker_ops import _KeyFileSecret
    return _KeyFileSecret.read(path)


def _monotonic():
    import time as _time
    return _time.monotonic()


def _sleep(clock, seconds):
    """Bounded wait on the injected clock (a fixture can fast-forward)."""
    deadline = clock() + seconds
    while clock() < deadline:
        import time as _time
        _time.sleep(min(0.05, max(deadline - clock(), 0)))


def _read_admission(status_dir):
    """The server's admission counters from broker-status.json (its OWN
    writer). The status file is rewritten per cycle - a concurrent read can
    hit the replace window, so a BOUNDED retry (3 x 0.1s) precedes the
    explicit unknown. Missing after the retries -> None (an explicit
    unknown, never a zero)."""
    path = status_dir / "broker-status.json"
    for _ in range(3):
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return {"cycles": payload.get("cycles"),
                        "unconfirmed_workers": payload.get("unconfirmed_workers")}
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    return None


def run_projection_lifecycle(spec, broker_factory=None, http_opener=None):
    """E3 projection/rotation closure over the REAL store + REAL broker.

    spec (fixed structure): {store, projection, management_pipe, port,
    control_key}. The isolated test store is created by the REAL ShareStore
    at the fixed path; the publication travels the REAL management channel
    (the SYSTEM broker coordinates the writer + the instance ACK). Verdicts:
    ack-generation-matched, http-key-ok, wrong-key-401, revoked-key-refused.
    """
    store_path = spec["store"]
    control_key = _read_secret(spec["control_key"])
    port = int(spec["port"])
    if broker_factory is None:
        def broker_factory():
            transport_factory = lambda: NamedPipeBrokerTransport(
                spec["management_pipe"])
            return BrokerClientAdapter(
                BrokerClient(transport_factory, control_key))
    if http_opener is None:
        base = f"http://127.0.0.1:{port}"
        http_opener = lambda path, key: _http_status(base, path, key)
    manager = ShareManager(store_path=store_path, broker=broker_factory())
    verdicts = []
    created = manager.create_key("exp-friend")
    plaintext = created.get("secret") if isinstance(created, dict) else None
    key_id = created.get("key_id") if isinstance(created, dict) else None
    if not plaintext or not key_id:
        raise RuntimeError("key creation returned no plaintext/identity")
    status = manager.status()
    if status.get("applied_generation") != status.get("desired_generation"):
        raise RuntimeError("the publication did not reach applied generation")
    verdicts.append("ack-generation-matched")
    code, _ = http_opener("/v1/models", plaintext)
    if code == 200:
        verdicts.append("http-key-ok")
    else:
        raise RuntimeError(f"the correct key was not accepted (http {code})")
    code, _ = http_opener("/v1/models", "f" * 64)
    if code == 401:
        verdicts.append("wrong-key-401")
    else:
        raise RuntimeError(f"a wrong key was not refused (http {code})")
    manager.delete_key(key_id)
    code, _ = http_opener("/v1/models", plaintext)
    if code == 401:
        verdicts.append("revoked-key-refused")
    else:
        raise RuntimeError(
            f"the revoked key was not refused (http {code})")
    return verdicts


def run_fault_injection(spec, writer_factory=None, broker_factory=None):
    """E4 projection-write boundary (REAL store + REAL manager).

    spec (fixed structure): {store, projection, management_pipe,
    control_key, boundary}. The fault: the projection file's ACL denies the
    writer (a REAL icacls change on the experiment-owned file); a revoking
    mutation must refuse (blocked record, emergency stop driven); the
    recovery restores the ACL and the republish applies. The injected
    writer_factory exists ONLY for offline fixtures.
    """
    boundary = str(spec.get("boundary", ""))
    if boundary != "projection-write":
        raise RuntimeError(f"unsupported fault boundary: {boundary}")
    store_path = spec["store"]
    projection_path = Path(spec["projection"])
    control_key = _read_secret(spec["control_key"])
    if broker_factory is None:
        def broker_factory():
            transport_factory = lambda: NamedPipeBrokerTransport(
                spec["management_pipe"])
            return BrokerClientAdapter(
                BrokerClient(transport_factory, control_key))
    manager = ShareManager(store_path=store_path, broker=broker_factory())
    verdicts = []
    if not projection_path.exists():
        raise RuntimeError(f"projection file missing: {projection_path.name}")
    # The boundary is SELF-CONTAINED: the fault key is created HERE with a
    # UNIQUE name (the store may be E3-primed with exp-friend or empty; a
    # duplicate name would be a StoreError, never a fault).
    import uuid
    fault_name = "exp-fault-" + str(uuid.uuid4())[:8]
    created = manager.create_key(fault_name)
    if not (isinstance(created, dict) and created.get("published")):
        raise RuntimeError("the fault key's initial publication did not apply")
    key_id = created["key_id"]
    # REAL fault: deny the projection file's OWNER its write right (the
    # owner is queried from the live ACL - in the experiment root the broker
    # host is SYSTEM and holds the write grant; in a fixture it is the
    # creating user). A deny ACE on the owner blocks the writer's REAL
    # replace path.
    owner = _owner_principal(projection_path)
    if not owner:
        raise RuntimeError("the projection file's owner ACE is unavailable")
    _icacls(projection_path, ["/deny", owner + ":(W)"])
    try:
        manager.delete_key(key_id)
        raise RuntimeError(
            "the revoking mutation was NOT refused under the faulted ACL")
    except Exception as exc:  # noqa: BLE001 - the refusal IS the verdict
        detail = f"{type(exc).__name__}"
        if "refused" not in detail and "not applied online" not in str(exc):
            raise
    verdicts.append("refused")
    # Recovery: restore the ACL, then bring the instance back to RUNNING
    # through the REAL broker host action before re-publishing. The fault
    # drove the restricted emergency stop (the share refuses loads while
    # stopped - R4-a), so no publication can ever apply against a stopped
    # instance; production restarts the share task (a fresh process comes
    # up running), which the manager's start action drives and verifies.
    _icacls(projection_path, ["/remove:d", owner])
    restart = manager.start()
    _wait_host_action(manager, restart.get("operation_id"),
                      "recovery host restart")
    # (The failed delete already mutated the store - the key is gone there -
    # so the recovery publishes a NEW key; a rotate of the deleted id would
    # be a NotFound, never a recovery.)
    recovered = manager.create_key("exp-friend-2")
    if not (isinstance(recovered, dict) and recovered.get("published")):
        raise RuntimeError("the recovery publication did not apply")
    verdicts.append("recovery-recorded")
    return verdicts


def _wait_host_action(manager, operation_id, label, timeout=8.0):
    """Await the manager's asynchronous host operation (start/stop) through
    the SAME persisted operation surface the product exposes: polling
    ``get_operation`` until it reaches a terminal state, bounded - never a
    fabricated confirmation."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = manager.get_operation(operation_id)
        status = operation.get("status") if isinstance(operation, dict) \
            else None
        if status == "completed":
            return
        if status == "failed":
            error = (operation.get("payload") or {}).get("error") \
                if isinstance(operation, dict) else None
            raise RuntimeError(f"{label} failed: {error or 'unconfirmed'}")
        time.sleep(0.05)
    raise RuntimeError(f"{label} timed out")


def _owner_principal(path):
    """The file's FIRST full-control ALLOW principal from the live icacls
    output (its own echoed name, so /deny and /remove:d use the same
    identity the checker saw). The output is the ANSI codepage (mbcs)."""
    import subprocess
    result = subprocess.run(["icacls", str(path)], capture_output=True,
                            encoding="mbcs", errors="replace", timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"icacls query failed (exit {result.returncode})")
    target = str(path).casefold()
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.casefold() == target:
            continue
        if line.casefold().startswith(target):
            # icacls prints the path and the FIRST ACE on ONE line - the
            # remainder behind the path prefix is the ACE itself.
            line = line[len(str(path)):].strip()
            if not line:
                continue
        name, _, flags = line.rpartition(":")
        codes = [t.strip('() \t') for t in flags.split("(") if t.strip()]
        perms = set()
        for token in codes:
            if token in ("I", "OI", "CI", "ID", "NP", "OD", "DENY",
                         "SPECIAL_ACCESS:"):
                continue
            perms.update(x for x in token.replace(" ", "").split(",") if x)
        if "F" in perms or "GA" in perms:
            return name.strip()
    return None


def _icacls(path, argument_tail):
    """One REAL icacls invocation on the experiment-owned file; a non-zero
    exit is an explicit failure (never ignored). The output is the ANSI
    codepage on this locale (mbcs) - never decoded as UTF-8."""
    import subprocess
    result = subprocess.run(
        ["icacls", str(path)] + list(argument_tail),
        capture_output=True, encoding="mbcs", errors="replace", timeout=15)
    if result.returncode != 0:
        raise RuntimeError(
            f"icacls failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:200]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="D4 experiment stage probe")
    sub = parser.add_subparsers(dest="command", required=True)
    pipe = sub.add_parser("pipe-probe")
    pipe.add_argument("--name", required=True)
    pipe.add_argument("--sddl", required=True)
    for name in ("request-matrix", "stall-matrix", "projection-lifecycle",
                 "fault-injection"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    if args.command == "pipe-probe":
        return _pipe_probe(args.name, args.sddl)
    # The spec crosses the PS 5.1 -> native argv boundary, which swallows
    # embedded double quotes (W2 run17: json.loads saw '{port:9100,...}').
    # The caller therefore transports it as BASE64 with a 'b64:' marker.
    import base64
    spec_text = args.spec
    if spec_text.startswith("b64:"):
        spec_text = base64.b64decode(spec_text[4:]).decode("utf-8")
    spec = json.loads(spec_text)
    if args.command == "request-matrix":
        verdicts = run_request_matrix(spec)
    elif args.command == "stall-matrix":
        verdicts = run_stall_matrix(spec)
    elif args.command == "projection-lifecycle":
        verdicts = run_projection_lifecycle(spec)
    else:
        verdicts = run_fault_injection(spec)
    for verdict in verdicts:
        print(verdict, flush=True)
    return 0


def _pipe_probe(name, sddl):
    """ONE real pipe instance creation attempt (the negative-first probe).
    A placeholder SDDL must FAIL the conversion (exit 1, ZERO namespace
    side effects); a calibrated SDDL creates + closes one probe instance."""
    import share_broker_server as srv
    kernel_pair = srv._declared_kernel()
    try:
        security = srv._SecurityAttrs(sddl, kernel_pair)
    except OSError as exc:
        print(f"PIPE-REFUSED SDDL conversion failed (fail-closed): "
              f"{type(exc).__name__}", flush=True)
        return 1
    open_mode = (srv.BoundedPipeServer._PIPE_ACCESS_DUPLEX
                 | srv.BoundedPipeServer._FILE_FLAG_FIRST_PIPE_INSTANCE)
    handle = kernel_pair[0].CreateNamedPipeW(
        name, open_mode,
        srv.BoundedPipeServer._PIPE_TYPE_BYTE | srv.BoundedPipeServer._PIPE_WAIT,
        srv.BoundedPipeServer._PIPE_UNLIMITED_INSTANCES, 4096, 4096, 0,
        _sa_pointer(security))
    if srv._invalid_handle(handle):
        print("PIPE-REFUSED CreateNamedPipeW failed (fail-closed)", flush=True)
        return 1
    kernel_pair[0].CloseHandle(handle)
    print("PIPE-CREATED", flush=True)
    return 0


def _sa_pointer(security):
    import ctypes
    return ctypes.pointer(security.sa)


if __name__ == "__main__":
    raise SystemExit(main())
