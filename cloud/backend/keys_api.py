# -*- coding: utf-8 -*-
"""Cloud Key API: ShareManager over the AF_UNIX broker.

E19-1-11.

The cloud composition root constructs this object; it is the ONLY Key surface
the cloud application exposes. Construction:

1. applies the explicit ``CloudConfig.process_env()`` mapping to this
   process so env-driven deployment modules and the explicit config fields
   agree;
2. builds the broker adapter from the EXPLICIT
   ``broker_socket``/``broker_key_file`` (never a repository default) and
   wraps it in ``BrokerClientAdapter``;
3. constructs ``ShareManager`` with the explicit ``share_store_file``.

The injected ``manager``/``broker`` keywords exist for isolated tests and
for the cloud fixture; they still run the env application first, so a test can
assert the wiring without opening the real store or socket.
"""
from __future__ import annotations

import os

from share_management import (BrokerClientAdapter, BrokerError, ShareManager)


class KeyApiError(Exception):
    """One Key API failure with an explicit HTTP status and machine code."""

    def __init__(self, code, message, *, status=503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class KeyApi:
    """Cloud Key command layer (create/get/list/revoke/rotate/status/drain)."""

    def __init__(self, config, *, manager=None, broker=None):
        self.config = config
        # E19-1-06 step 10: the environment is applied ONCE by the
        # composition root (``build_app`` -> ``apply_process_env``), BEFORE this
        # object is constructed. Doing it here as well would hide a root that
        # forgot the call and would apply the mapping twice.
        if broker is None:
            broker = self._build_broker(config)
        self.broker = broker
        if manager is None:
            manager = ShareManager(
                store_path=config.share_store_file, broker=broker)
        self.manager = manager

    @staticmethod
    def _build_broker(config):
        from share_broker_client import (BrokerClient,
                                        UnixSocketBrokerTransport,
                                        validate_unix_socket_path)
        from share_broker_server import read_control_key

        socket_path = validate_unix_socket_path(config.broker_socket)
        secret = read_control_key(config.broker_key_file)
        client = BrokerClient(
            lambda: UnixSocketBrokerTransport(socket_path), secret)
        return BrokerClientAdapter(client)

    # ------------------------------------------------------------- read paths
    def status(self):
        return self.manager.status()

    def list(self):
        return self.manager.list_keys()

    def get(self, key_id):
        return self.manager.get_key(key_id)

    # -------------------------------------------------------------- mutations
    def create(self, name, max_concurrency=None, idempotency_key=None):
        return self.manager.create_key(
            name, max_concurrency=max_concurrency,
            idempotency_key=idempotency_key)

    def revoke(self, key_id, expected_row_version, idempotency_key=None):
        return self.manager.revoke_and_terminate_key(
            key_id, expected_row_version=expected_row_version,
            idempotency_key=idempotency_key)

    def rotate(self, key_id, expected_row_version, idempotency_key=None):
        return self.manager.rotate_key(
            key_id, idempotency_key=idempotency_key,
            expected_row_version=expected_row_version)

    # ----------------------------------------------------------------- drain
    def drain(self):
        """Bounded broker drain; the CONFIRMED outcome is reported honestly.

        ``BrokerClientAdapter`` keeps the broker's ``BrokerResult`` on
        ``.client`` (the adapter itself has no drain verb). ``BrokerResult.ok``
        only means the broker answered with a structured reply — the drain itself
        is confirmed by ``result.drained``, ``result.timed_out`` and
        ``result.in_flight``. A reply that answered but did not drain is reported
        as an explicit failure (D18-10: 未排空不得谎报已停止), never as
        ``ok: true``.
        """
        client = getattr(self.broker, "client", None)
        if client is None or not hasattr(client, "drain"):
            raise KeyApiError(
                "broker_unavailable",
                "broker client with a drain verb is not assembled",
                status=503)
        try:
            result = client.drain()
        except (BrokerError, OSError, ValueError) as exc:
            raise KeyApiError(
                "share_broker_unavailable",
                "broker drain failed: %s: %s" % (type(exc).__name__, exc),
                status=503)
        payload = {
            "ok": bool(getattr(result, "ok", False)),
            "result": getattr(result, "result", None),
            "error_code": getattr(result, "error_code", None),
        }
        if payload["ok"]:
            confirmed, reason = _drain_confirmed(payload["result"])
            if not confirmed:
                payload["ok"] = False
                payload["error_code"] = "drain_not_confirmed"
                payload["reason"] = reason
        return payload


def _drain_confirmed(result):
    """Whether a broker drain reply really stopped the share (D18-10).

    Only an explicit ``drained: true`` with no timed-out wait and no observed
    in-flight request confirms a stop. ``in_flight`` is the broker's OBSERVED
    count; ``None`` means no instance-status observation resolved one, so it can
    never be read as zero.
    """
    if not isinstance(result, dict):
        return False, "broker drain returned no structured result"
    if result.get("drained") is not True:
        return False, "broker did not confirm the drain"
    if result.get("timed_out") is True:
        return False, "broker drain timed out"
    in_flight = result.get("in_flight")
    if in_flight is None:
        return False, "broker did not observe an in-flight count"
    if isinstance(in_flight, bool) or not isinstance(in_flight, int) \
            or in_flight != 0:
        return False, "broker observed %r request(s) still in flight" % (
            in_flight,)
    return True, None
