# -*- coding: utf-8 -*-
"""Minimal test double for the retired ``ShareServer`` instance endpoint.

The old two-process share server is gone; production's merged 8100 process owns
the instance channel through ``chat2api._ShareInstanceControl``. Broker and
instance-pipe tests still need an endpoint that validates the signed schema-v2
frame, binds it to one instance id, consumes a replay and answers
status/load/drain/resume/emergency. This module is that endpoint and only that
endpoint: it is test-only and is never imported by production code.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path

import share_auth
from share.broker_protocol import (ProtocolError, ReplayRegistry,
                                   validate_instance_request)


class _ConnectionGate:
    """Adjustable, non-blocking TCP connection gate (accept boundary only)."""

    def __init__(self, limit):
        self._lock = threading.Lock()
        self._limit = int(limit)
        self._active = 0

    @property
    def limit(self):
        with self._lock:
            return self._limit

    def set_limit(self, limit):
        with self._lock:
            self._limit = int(limit)

    def acquire(self):
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            if self._active > 0:
                self._active -= 1


class FakeShareServer:
    """The minimal signed instance endpoint the current-chain tests need."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address=None, config=None, upstream_port=8000,
                 probe=False, auth_mode=None, attribution_secret=None,
                 fault_log=None):
        self.config = Path(config)
        self.auth_mode = auth_mode
        self.upstream_port = upstream_port
        self.probe = probe
        self.attribution_secret = attribution_secret
        self.fault_log = fault_log
        self.instance_id = secrets.token_hex(16)
        self.gate = share_auth.ShareGate(config, auth_mode=auth_mode)
        self.last_seen_generation = 0
        self.connections = _ConnectionGate(
            self.gate.share_max_concurrency + 8)
        self._replay = None
        self._stop = threading.Event()

    # -- state surfaces the old server exposed to the broker tests ----------
    @property
    def state(self):
        return self.gate.state

    @state.setter
    def state(self, value):
        self.gate.set_state(value)

    @property
    def active(self):
        return self.gate.active

    @property
    def history(self):
        return self.gate.history

    @property
    def inflight(self):
        return self.gate.inflight

    @property
    def guard(self):
        return self.gate.guard

    @property
    def share_max_concurrency(self):
        return self.gate.share_max_concurrency

    def snapshot(self):
        snapshot = self.gate.snapshot()
        snapshot["connection_limit"] = self.connections.limit
        snapshot["security_generation"] = self._security_generation()
        return snapshot

    def acquire(self, key_id, generation=None, secret_version=None,
                reason_out=None):
        return self.gate.acquire(key_id, generation=generation,
                                 secret_version=secret_version,
                                 reason_out=reason_out)

    def release(self, key_id):
        return self.gate.release(key_id)

    def register_inflight(self, key_id, ctx):
        return self.gate.register_inflight(key_id, ctx)

    def unregister_inflight(self, key_id, ctx):
        return self.gate.unregister_inflight(key_id, ctx)

    def terminate_key(self, key_id):
        return self.gate.terminate_key(key_id)

    def emergency_stop(self):
        with self.gate.guard:
            cleared = sum(self.gate.active.values())
            self.gate.state = "stopped"
            self.gate.active.clear()
        return {"instance_id": self.instance_id, "state": "stopped",
                "cleared_in_flight": cleared}

    # -- process surface (the tests run the endpoint in a thread) -----------
    def serve_forever(self):
        self._stop.wait()

    def shutdown(self):
        self._stop.set()

    def server_close(self):
        self._stop.set()

    # -- signed instance channel ----------------------------------------
    def handle_instance_control(self, body, secret, now=None):
        if self._replay is None:
            self._replay = ReplayRegistry()
        try:
            request = validate_instance_request(body, secret, now=now)
        except ProtocolError as exc:
            return {"v": 2, "request_id": body.get("request_id", ""),
                    "ok": False, "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        request_id = request["request_id"]
        if request["instance_id"] != self.instance_id:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "instance_mismatch",
                    "result": {"detail": "request targets another instance"}}
        try:
            self._replay.consume(request_id, request["ts"], now=now)
        except ProtocolError as exc:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        action = request["action"]
        if action == "instance-status":
            return self._reply(request_id, {
                "instance_id": self.instance_id,
                "state": self.gate.state,
                "active_count": sum(self.gate.active.values()),
                "last_seen_generation": self.last_seen_generation,
            })
        if action == "load":
            return self._load(request, request_id)
        if action == "drain":
            with self.gate.guard:
                if self.gate.state == "stopped":
                    return {"v": 2, "request_id": request_id, "ok": False,
                            "error_code": "blocked",
                            "result": {"detail": "instance is stopped",
                                       "state": "stopped"}}
                self.gate.state = "draining"
                return self._reply(request_id, {
                    "instance_id": self.instance_id,
                    "state": "draining",
                    "active_count": sum(self.gate.active.values()),
                })
        if action == "resume":
            with self.gate.guard:
                if self.gate.state == "stopped":
                    return {"v": 2, "request_id": request_id, "ok": False,
                            "error_code": "blocked",
                            "result": {"detail": "instance is stopped",
                                       "state": "stopped"}}
                already_running = self.gate.state == "running"
                self.gate.state = "running"
                result = {"instance_id": self.instance_id,
                          "state": "running",
                          "active_count": sum(self.gate.active.values())}
                if already_running:
                    result["already_running"] = True
                return self._reply(request_id, result)
        if action == "emergency":
            with self.gate.guard:
                cleared = sum(self.gate.active.values())
                self.gate.state = "stopped"
                self.gate.active.clear()
                return self._reply(request_id, {
                    "instance_id": self.instance_id,
                    "state": "stopped",
                    "cleared_in_flight": cleared,
                })
        return {"v": 2, "request_id": request_id, "ok": False,
                "error_code": "bad_request",
                "result": {"detail": "unknown instance action"}}

    def _load(self, request, request_id):
        fields = request.get("fields") or {}
        try:
            payload = json.loads(self.config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "fingerprint_mismatch",
                    "result": {"detail": "projection unreadable or invalid"}}
        if payload.get("config_generation") != fields.get("generation"):
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "stale_generation",
                    "result": {"detail": "loaded generation differs"}}
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        if fingerprint != fields.get("fingerprint"):
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "fingerprint_mismatch",
                    "result": {"detail": "loaded content differs"}}
        try:
            fresh = share_auth.load_config(self.config, auth_mode=self.auth_mode)
        except (OSError, ValueError, KeyError, TypeError):
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "fingerprint_mismatch",
                    "result": {"detail": "projection unreadable or invalid"}}
        with self.gate.guard:
            self.gate._cfg = fresh
            self.gate.share_max_concurrency = fresh["share_max_concurrency"]
            self.gate.key_limits = {
                entry["key_id"]: entry["max_concurrency"]
                for entry in fresh["entries"]}
            self.gate.config_generation = fresh["config_generation"]
            self.connections.set_limit(
                fresh["share_max_concurrency"] + 8)
            self.last_seen_generation = max(
                self.last_seen_generation, payload["config_generation"])
        terminated = sum(
            self.gate.terminate_key(key_id)
            for key_id in fields.get("terminate_keys") or ()
            if isinstance(key_id, str))
        result = {
            "instance_id": self.instance_id,
            "generation": payload["config_generation"],
            "fingerprint": fingerprint,
            "installed": True,
            "state": self.gate.state,
            "share_max_concurrency": self.gate.share_max_concurrency,
            "connection_limit": self.connections.limit,
            "security_generation": self._security_generation(),
        }
        if "terminate_keys" in fields:
            result["terminated"] = terminated
        return self._reply(request_id, result)

    def _security_generation(self):
        try:
            return max(self.gate.config_generation,
                       share_auth.applied_security_generation())
        except (OSError, ValueError):
            return self.gate.config_generation

    @staticmethod
    def _reply(request_id, result):
        return {"v": 2, "request_id": request_id, "ok": True,
                "error_code": "ok", "result": result}
