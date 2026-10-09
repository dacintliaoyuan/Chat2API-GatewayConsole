"""share_runtime.py — deployment/runtime broker adapters for share_management.

The management command layer treats "publish projection" and "start/stop" as
broker operations. This module provides the offline projection-file broker and
documents the real deployment boundary:

* The real high-privilege broker is a SYSTEM named-pipe with OS identity ACL and
  fixed actions; it has NOT been installed in this offline phase. That real
  broker must be verified in an authorized isolation experiment before any host
  claim is made (see handoff S0/S4).
* A DeployBroker without an explicit executor is intentionally fail-closed:
  start/stop raise BrokerError instead of pretending to control the host.
* ``ProjectionFileBroker`` may be used only where a projection file can be
  written by the current process (offline fixtures / a broker-managed writable
  path). It never accepts a client-supplied path; the destination is fixed at
  construction.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from share_broker_protocol import ProtocolError, parse_projection_payload
from share_management import BrokerError


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


class ProjectionFileBroker:
    """Offline broker that atomically writes the share projection.

    ``ack=True`` means the caller assumes a test/offline share reader will
    consume the file immediately; it is used only in isolated fixtures. The real
    runtime broker must not ack until the share confirms the loaded generation.
    """

    name = "projection-file"

    def __init__(self, projection_path, ack=False):
        self.projection_path = Path(projection_path)
        self.ack = ack

    def _read_projection(self):
        try:
            return json.loads(self.projection_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def publish(self, projection, terminate_keys=None):
        # terminate_keys accepted for broker-contract parity; this offline
        # broker never forwards it (no instance channel).
        current = self._read_projection()
        if current is not None:
            current_gen = current.get("config_generation")
            incoming_gen = projection.get("config_generation")
            if isinstance(current_gen, int) and isinstance(incoming_gen, int):
                # Runtime-side monotonicity (R5-b): a later writer may never
                # regress the projection the share would load, and a
                # same-generation write must carry identical content.
                if incoming_gen < current_gen:
                    raise BrokerError(
                        f"publication generation regression refused "
                        f"(projection file at generation {current_gen} > "
                        f"target {incoming_gen})")
                if (incoming_gen == current_gen
                        and json.dumps(current, sort_keys=True)
                        != json.dumps(projection, sort_keys=True)):
                    raise BrokerError(
                        "same-generation projection content mismatch refused")
        _atomic_write_json(self.projection_path, projection)
        if self.ack:
            return {"applied_generation": projection["config_generation"]}
        # The projection file is written, but without an ack the management
        # layer must NOT report online/in_sync.
        return None

    def snapshot(self):
        if self.projection_path.exists():
            try:
                data = json.loads(self.projection_path.read_text(encoding="utf-8"))
                generation = data.get("config_generation")
            except (OSError, ValueError):
                generation = None
        else:
            generation = None
        return {"name": self.name, "state": "projection_file",
                "config_generation": generation, "ack": self.ack}

    def start(self):
        raise BrokerError("projection-file broker cannot start the share")

    def graceful_stop(self):
        raise BrokerError("projection-file broker cannot stop the share")

    def emergency_stop(self):
        raise BrokerError("projection-file broker cannot emergency-stop the share")


class ProtectedProjectionWriter:
    """Fixed-destination, FULLY-validated, atomic projection writer (D1/R29-4).

    The destination is fixed at construction — never caller-supplied. At
    construction the writer builds its generation/fingerprint baseline FROM
    the protected landing spot: an existing but corrupt or invalid file
    REFUSES publication (it is never treated as an empty state). Every write
    runs the SHARED full semantic parser (parse_projection_payload) BEFORE
    anything is replaced, writes a protected temp file, re-parses what
    landed, verifies the ACL (via the injected checker; the real Windows ACL
    audit is an L2/OS component) and only then atomically replaces the
    projection. Generation is monotonic from the persistent baseline; the
    same generation with different content is refused; a failed write keeps
    the previously effective copy. The writer NEVER touches the Key SQLite
    store (A3).
    """

    def __init__(self, projection_path, acl_checker):
        import threading
        from share_broker_protocol import parse_projection_payload
        if acl_checker is None:
            # R31-6: the ACL validator MUST be explicitly injected — a
            # missing check is a refusal, never a silent pass. Real Windows
            # ACL wiring stays in L2/D4; fixtures inject an explicit stub.
            raise ValueError(
                "ProtectedProjectionWriter requires an explicit acl_checker "
                "(None is not allowed: a missing ACL check must refuse, not "
                "pass)")
        self.path = Path(projection_path)
        self._acl_checker = acl_checker      # callable(path) -> None | raise
        self._lock = threading.Lock()
        self._parse = parse_projection_payload
        # Persist baseline from the protected landing spot (R29-4): a
        # corrupt existing file BLOCKS publication instead of being treated
        # as a fresh state.
        self._baseline_corrupt = False
        self._last_written = None
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                parse_projection_payload(existing)
                self._last_written = {"generation":
                                      existing["config_generation"],
                                      "fingerprint": self.fingerprint(existing)}
            except (OSError, ValueError) as exc:
                raise BrokerError(
                    f"protected projection file is corrupt; refusing "
                    f"publication over it: {type(exc).__name__}") from exc
            except ProtocolError as exc:
                raise BrokerError(
                    f"protected projection file is invalid; refusing "
                    f"publication over it: {exc}") from exc

    def fingerprint(self, projection):
        import hashlib
        return hashlib.sha256(
            json.dumps(projection, sort_keys=True).encode("utf-8")).hexdigest()

    def write(self, projection, expected_generation, expected_fingerprint):
        with self._lock:
            if self._baseline_corrupt:
                raise BrokerError(
                    "baseline projection is corrupt; publication refused")
            # FULL semantic validation BEFORE anything is replaced (R29-4).
            try:
                parse_projection_payload(projection)
            except ProtocolError as exc:
                raise BrokerError(f"invalid projection: {exc}") from exc
            generation = projection["config_generation"]
            if generation != expected_generation:
                raise BrokerError(
                    f"projection generation {generation} does not match the "
                    f"publication target {expected_generation}")
            fingerprint = self.fingerprint(projection)
            if fingerprint != expected_fingerprint:
                raise BrokerError(
                    "projection fingerprint does not match the publication "
                    "target")
            last = self._last_written
            if last is not None:
                if generation < last["generation"]:
                    raise BrokerError(
                        f"projection generation regression refused "
                        f"(written {last['generation']} > target {generation})")
                if generation == last["generation"]                         and fingerprint != last["fingerprint"]:
                    raise BrokerError(
                        "same-generation projection content mismatch refused")
            tmp = self.path.with_name(self.path.name + ".writer-tmp")
            try:
                tmp.write_text(
                    json.dumps(projection, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                # Verify what actually landed: reparse the bytes on disk and
                # fingerprint THE SAME bytes the share will read (R29-4
                # single-snapshot rule lives on the share side; here the
                # readback must parse and match).
                reread_text = tmp.read_text(encoding="utf-8")
                reread = json.loads(reread_text)
                parse_projection_payload(reread)
                if self.fingerprint(reread) != fingerprint:
                    raise BrokerError("temp projection readback mismatch")
                if self._acl_checker is not None:
                    # The ACL gate guards the PROTECTED LANDING file, not
                    # the fresh temp copy the writer just created: an ACL
                    # change on the real projection file (the E4 fault
                    # boundary) must be seen here or it could never refuse
                    # a publication.
                    self._acl_checker(self.path)
                os.replace(tmp, self.path)   # atomic; old copy stays on failure
            except BrokerError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            self._last_written = {"generation": generation,
                                  "fingerprint": fingerprint}
            return fingerprint


class DeployBroker:
    """Deployment broker skeleton for the existing guarded/elevated PowerShell
    stages.

    No subprocess is ever started unless an ``executor`` callable is supplied by
    an authorized deployment integration. The callable receives a fixed
    ``stage`` string (only values in ALLOWED_STAGES) and returns a status dict.
    This keeps arbitrary commands/paths/scripts out of the broker surface.
    """

    ALLOWED_STAGES = {"start", "graceful_stop", "emergency_stop", "publish_projection"}
    name = "deploy"

    def __init__(self, executor=None, mode="real", projection_path=None):
        self.executor = executor
        self.mode = mode
        self.projection_path = Path(projection_path) if projection_path else None

    def _require_executor(self):
        if self.executor is None:
            raise BrokerError(
                "deploy broker is not installed/authorized; no host action attempted")

    def publish(self, projection, terminate_keys=None):
        # terminate_keys accepted for broker-contract parity (R8-A7); this
        # file broker performs no instance load, so nothing is terminated.
        if self.projection_path is None:
            return None
        # Real deployments write this through the privileged executor to a
        # fixed run-copy path; offline callers may use ProjectionFileBroker.
        # The projection payload is passed through (never discarded) and the
        # result must include the applied/acked generation to count as applied.
        self._require_executor()
        result = self.executor("publish_projection", projection=projection)
        if isinstance(result, dict):
            return result
        return None

    def start(self):
        self._require_executor()
        return self.executor("start")

    def graceful_stop(self):
        self._require_executor()
        return self.executor("graceful_stop")

    def emergency_stop(self):
        self._require_executor()
        return self.executor("emergency_stop")

    def snapshot(self):
        return {"name": self.name, "mode": self.mode,
                "executor_installed": self.executor is not None,
                "state": "deploy-not-invoked"}


class PowerShellStageExecutor:
    """Fixed wrapper for run-deploy-guarded.ps1 stages.

    The path is set at construction (no client-supplied path). Stage names are
    the existing guarded stages; new host actions are mapped to those exact
    strings in the deployment layer. This class is for the later authorized
    integration and is not used by default in this offline phase.
    """

    def __init__(self, wrapper_ps1: Path, stage_map=None, timeout_ms=120000):
        self.wrapper_ps1 = Path(wrapper_ps1)
        self.stage_map = stage_map or {
            "start": "Start",
            "graceful_stop": "DrainStop",
            "emergency_stop": "Stop",
            "publish_projection": "PublishProjection",
        }
        self.timeout_ms = timeout_ms

    def __call__(self, stage, projection=None):
        if stage not in self.stage_map:
            raise BrokerError(f"unknown deploy stage: {stage}")
        ps_stage = self.stage_map[stage]
        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(self.wrapper_ps1), "-Stage", ps_stage,
        ]
        if stage == "start":
            cmd += ["-Mode", "real"]
        if stage == "publish_projection":
            # The real deployment layer must write the projection to its fixed
            # run-copy path and return the share-confirmed generation. Until an
            # authorized writer exists this stage must not pretend success.
            if projection is None:
                raise BrokerError("publish_projection requires projection data")
            raise BrokerError(
                "publish_projection needs the authorized elevated projection "
                "writer; not invoked in this offline phase")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout_ms / 1000)
        except subprocess.TimeoutExpired as exc:
            raise BrokerError(f"deploy stage timed out: {ps_stage}") from exc
        if proc.returncode != 0:
            raise BrokerError(
                f"deploy stage failed ({ps_stage} exit {proc.returncode})")
        return {"state": "ok", "stage": ps_stage}
