r"""share_instance_pipe.py — share-side instance-channel wire server (R41-1).

The in-process control endpoint of the share instance on the wire: an
``InstancePipeServer`` serves ``<instance-pipe-prefix><instance_id>`` with
the SAME bounded cycle contract as the management server (one worker per
cycle, cycle-owned handle/cancel, 1 MiB bound checked BEFORE a complete
frame is accepted) and dispatches to the REAL product handler
(``ShareServer.handle_instance_control``) — never an echo fixture. HMAC
validation, instance binding and replay consumption stay inside the share
handler; this layer only moves bounded bytes and encodes the structured
reply.

Deployment contract (R41-1):

* ``start_instance_channel`` probes the FIRST pipe instance eagerly so a
  name conflict or a DACL conversion failure is an EXPLICIT not-ready
  state: the share HTTP service keeps serving, NOTHING is killed, and the
  registration file is not written — an absent registration is the
  broker's "instance not ready" fact (never a fabricated instance id).
* ``write_registration`` self-publishes the instance identity atomically
  into the um_share-writable run\live directory. It is only the INITIAL
  deployment fact for the broker's target; every instance connection is
  re-verified against the ACTUAL pipe peer by the broker's identity
  verifier (R41-2), so a forged or rewritten registration file alone can
  never drive a load.
* The DACL comes from an SDDL parameter like the management pipe; the
  planned default carries placeholder SIDs that FAIL conversion on
  purpose, so an uncalibrated deployment refuses to serve instead of
  starting with a default ACL.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from share_broker_server import BoundedPipeServer
from share_broker_protocol import DEFAULT_DEADLINE_SECONDS, ProtocolError


def instance_pipe_name(pipe_prefix, instance_id):
    """The instance channel endpoint for ONE share instance."""
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


class InstancePipeServer(BoundedPipeServer):
    """Instance-channel wire server on the share side (R41-1).

    Dispatch runs ``share_server.handle_instance_control(body, secret)`` —
    the REAL endpoint that validates the signed instance frame (schema v2,
    instance_id binding, replay, action whitelist) inside the share's own
    guard. Protocol decode failures are answered with a structured
    bad_request frame (request_id unavailable from an unparsable body),
    never with a crash of the accept loop.
    """

    def __init__(self, share_server, instance_secret, pipe_name,
                 sddl=None, kernel=None,
                 deadline_seconds=DEFAULT_DEADLINE_SECONDS, state_dir=None,
                 clock=time.monotonic):
        self.share_server = share_server
        self.instance_secret = instance_secret
        super().__init__(pipe_name, sddl=sddl, kernel=kernel,
                         deadline_seconds=deadline_seconds,
                         state_dir=state_dir, clock=clock)

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


def probe_instance_listener(server):
    """Create ONE pipe instance eagerly to fail the channel loudly BEFORE
    the share HTTP service reports the instance as ready. A success closes
    the probe instance immediately; the serving loop re-creates per cycle
    (the same bounded cycle contract). Returns (ok, error)."""
    try:
        handle = server._create_instance(sddl=server.sddl)
    except BaseException as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    server._release_instance()
    return True, None


def start_instance_channel(share_server, instance_secret, pipe_prefix,
                           registration_path, sddl=None, kernel=None,
                           deadline_seconds=DEFAULT_DEADLINE_SECONDS):
    """Start the instance channel for a RUNNING share HTTP server.

    Returns a record {ready, pipe_name?, error?}. Never raises into the
    share main service: a listener failure (name taken, DACL refused,
    kernel error) leaves the share serving WITHOUT a registration file —
    the broker's load path stays explicitly not-ready (R41-1), and the
    HTTP share is never killed by the control channel.
    """
    instance_id = getattr(share_server, "instance_id", None)
    if not instance_id:
        return {"ready": False,
                "error": "share server has no instance identity"}
    pipe_name = instance_pipe_name(pipe_prefix, instance_id)
    server = InstancePipeServer(
        share_server, instance_secret, pipe_name, sddl=sddl, kernel=kernel,
        deadline_seconds=deadline_seconds)
    ok, error = probe_instance_listener(server)
    if not ok:
        return {"ready": False, "error": error}
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="um-share-public-instance-channel")
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
