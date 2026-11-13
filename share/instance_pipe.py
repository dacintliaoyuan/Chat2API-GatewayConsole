r"""share/instance_pipe.py - POSIX share-side instance-channel server.

The in-process control endpoint of the share instance on the wire:
``UnixSocketInstanceServer`` serves ``<instance-pipe-prefix><instance_id>`` with
the SAME bounded cycle contract as the management server (one worker per cycle,
cycle-owned connection, 1 MiB bound checked BEFORE a complete frame is accepted)
and dispatches to the REAL product handler
(``ShareServer.handle_instance_control``) - never an echo fixture. HMAC
validation, instance binding and replay consumption stay inside the share handler;
this layer only moves bounded bytes and encodes the structured reply.

Deployment contract (R41-1):

* ``start_instance_channel`` probes the FIRST accepted connection eagerly so a
  bad socket path or a live endpoint is an EXPLICIT not-ready state: the share
  HTTP service keeps serving, NOTHING is killed, and the registration file is
  not written - an absent registration is the broker's "instance not ready" fact
  (never a fabricated instance id).
* ``write_registration`` self-publishes the instance identity atomically into the
  um_share-writable run\live directory. It is only the INITIAL deployment fact
  for the broker's target; every instance connection is re-verified against the
  ACTUAL socket peer by the broker's identity verifier (R41-2), so a forged or
  rewritten registration file alone can never drive a load.

POSIX (Linux) deployment: the instance channel is an AF_UNIX SOCK_STREAM
socket. The "pipe name" is then the SOCKET PATH produced by
``instance_pipe_name(prefix, instance_id)``, where the deployment-supplied
prefix is an absolute path prefix (for example ``/run/um-share/instance-``).
The server enforces the parent directory 0700 / socket file 0600, and the
broker-side peer identity is kernel evidence (SO_PEERCRED pid/uid/gid) - never
this registration file. The retired Windows named-pipe server is gone with the old
local share chain (#33).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

from share.broker_server import BoundedPipeServer, UnixSocketBrokerServer
from share.broker_protocol import DEFAULT_DEADLINE_SECONDS, ProtocolError


POSIX_INSTANCE_PIPE_PREFIX = "/run/um-share/instance-"


def default_instance_pipe_prefix():
    """The POSIX default instance-channel prefix (an absolute socket path)."""
    return POSIX_INSTANCE_PIPE_PREFIX


def instance_pipe_name(pipe_prefix, instance_id):
    """The instance channel endpoint for ONE share instance.

    POSIX: an absolute socket path ``<prefix><id>`` (the prefix is a socket
    path prefix, not a directory).
    """
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id must be a non-empty string")
    return f"{pipe_prefix}{instance_id}"


def write_registration(path, instance_id, pipe_name):
    """Atomically self-publish the instance registration record.

    The file lands in the um_share-writable run\\live directory (the broker
    only READS it). Written via temp+replace so a partially written file is
    never observable as a valid registration. Content is non-secret
    deployment fact: instance_id, pipe name, pid, epoch seconds.
    """
    path = Path(path)
    payload = {"instance_id": str(instance_id), "pipe_name": str(pipe_name),
               "pid": int(os.getpid()), "registered_at": int(time.time())}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def remove_registration(path):
    """Best-effort removal on shutdown; an absent file stays absent."""
    try:
        Path(path).unlink()
    except OSError:
        pass


class _InstanceControlDispatch:
    """Dispatch shared by the POSIX socket server.

    Dispatch runs ``share_server.handle_instance_control(body, secret)`` — the
    REAL endpoint that validates the signed instance frame (schema v2,
    instance_id binding, replay, action whitelist) inside the share's own
    guard. Protocol decode failures are answered with a structured
    bad_request frame (request_id unavailable from an unparsable body),
    never with a crash of the accept loop.
    """

    def _dispatch(self, body, record):
        try:
            body = self._decode_body(body)
            response = self.share_server.handle_instance_control(
                body, self.instance_secret)
            if not isinstance(response, dict):
                raise ValueError("instance handler returned a non-object")
        except ProtocolError as exc:
            request_id = body.get("request_id") if isinstance(body, dict) \
                else None
            return self._frame(request_id, exc.code, str(exc))
        except BaseException as exc:  # noqa: BLE001
            record["dispatch_error"] = f"{type(exc).__name__}: {exc}"
            request_id = body.get("request_id") if isinstance(body, dict) \
                else None
            return self._frame(request_id, "internal_error",
                               "instance handler failure")
        return (json.dumps(response) + "\n").encode("utf-8")


class UnixSocketInstanceServer(_InstanceControlDispatch,
                               UnixSocketBrokerServer):
    """Instance-channel wire server over a POSIX AF_UNIX socket.

    The transport is the Unix socket server (parent 0700, socket 0600,
    SO_PEERCRED peer identity).
    """

    def __init__(self, share_server, instance_secret, socket_path,
                 deadline_seconds=DEFAULT_DEADLINE_SECONDS, state_dir=None,
                 clock=time.monotonic):
        self.share_server = share_server
        self.instance_secret = instance_secret
        super().__init__(socket_path, deadline_seconds=deadline_seconds,
                         state_dir=state_dir, clock=clock)


def probe_instance_listener(server):
    """Create ONE pipe instance eagerly to fail the channel loudly BEFORE
    the share HTTP service reports the instance as ready. A success closes
    the probe instance immediately; the serving loop re-creates per cycle
    (the same bounded cycle contract). Returns (ok, error)."""
    try:
        handle = server._create_instance()
    except BaseException as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    server._release_instance()
    return True, None


def start_instance_channel(share_server, instance_secret, pipe_prefix,
                           registration_path,
                           deadline_seconds=DEFAULT_DEADLINE_SECONDS):
    """Start the instance channel for a RUNNING share HTTP server.

    Returns a record {ready, pipe_name?, error?}. Never raises into the
    share main service: a listener failure (name taken, DACL refused,
    kernel error, bad socket path) leaves the share serving WITHOUT a
    registration file — the broker's load path stays explicitly not-ready
    (R41-1), and the HTTP share is never killed by the control channel.

    The channel is an AF_UNIX socket at
    ``instance_pipe_name(pipe_prefix, instance_id)`` (mode bits are enforced
    by the socket server).
    """
    instance_id = getattr(share_server, "instance_id", None)
    if not instance_id:
        return {"ready": False,
                "error": "share server has no instance identity"}
    if pipe_prefix is None:
        pipe_prefix = default_instance_pipe_prefix()
    pipe_name = instance_pipe_name(pipe_prefix, instance_id)
    try:
        server = UnixSocketInstanceServer(
            share_server, instance_secret, pipe_name,
            deadline_seconds=deadline_seconds)
    except (OSError, ValueError) as exc:
        # A bad POSIX path (relative/too long) is the SAME explicit not-ready
        # state as a failed listener: nothing is written, nothing is killed.
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
    ok, error = probe_instance_listener(server)
    if not ok:
        return {"ready": False, "error": error}
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="um-share-instance-channel")
    thread.start()
    try:
        write_registration(registration_path, instance_id, pipe_name)
    except OSError as exc:
        # The listener serves, but the broker cannot learn the target yet:
        # still an explicit not-ready state, never a fabricated success.
        # The running server is handed back so the caller can stop it.
        return {"ready": False, "pipe_name": pipe_name, "server": server,
                "error": f"registration write failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"ready": True, "pipe_name": pipe_name, "server": server}
