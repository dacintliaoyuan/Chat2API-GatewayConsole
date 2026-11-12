# -*- coding: utf-8 -*-
"""Cloud role HTTP handlers.

The request guard, route matching, health answer, stopping/auth order and the
bounded body read live in ``app.console_backend_app.RoleHandler``. This module
keeps only the cloud product handlers: the Key API, the dedicated backend and the
cloud summary. The cloud composition root declares the route table that points at
these methods.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from share.management import (BrokerError, Conflict, Disabled, NotFound,
                             StoreError, VersionMismatch)
from shared import pricing_rules

from .backend_process import BackendProcessError
from .keys_api import KeyApiError

from app.console_backend_app import RoleHandler

KEY_ID_MAX = 128
NAME_MAX = 64
IDEMPOTENCY_KEY_MAX = 128


def load_price_table(path):
    """Read one explicit price table document (never a repository default).

    Kept in the host (the cloud root), not in ``shared.pricing_rules``: the
    shared module must stay a pure rule set with no file access.

    The document is NORMALIZED with the same pure rule the local stats reader
    uses (``shared.pricing_rules.normalize_price_table``). Handing the raw
    document to ``build_cloud_summary`` made the two ends disagree on the SAME
    file: a raw ``data/prices.json`` has no top-level ``versioned``/
    ``versionMeta``/``meta`` keys, so the cloud side reported
    ``versioned: false`` and picked the 1970 segment as "current" while the
    local side reported the 2026-09-29 segment.
    """
    text = Path(path).read_text(encoding="utf-8")
    table = json.loads(text)
    if not isinstance(table, dict):
        raise ValueError("price table must be a JSON object")
    return pricing_rules.normalize_price_table(table, now=time.time())


class CloudHandler(RoleHandler):
    server_version = "CloudConsole/1.0"

    # -------------------------------------------------------------- handlers
    def _dispatch(self, call):
        try:
            self._send_json(200, call())
        except KeyApiError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except NotFound:
            self._send_json(404, {"error": "not_found"})
        except (Conflict, Disabled, VersionMismatch):
            self._send_json(409, {"error": "conflict"})
        except BrokerError as exc:
            self._send_json(503, {"error": getattr(exc, "code", None)
                                  or "share_broker_unavailable"})
        except StoreError:
            self._send_json(400, {"error": "invalid_request"})
        except (OSError, ValueError):
            self._send_json(400, {"error": "invalid_request"})
        except BackendProcessError:
            self._send_json(503, {"error": "share_backend_unavailable"})
        except Exception:  # noqa: BLE001 - never leak details or secrets
            self._send_json(500, {"error": "internal_error"})

    def _send_summary(self):
        """The cloud summary read face (E19-1-06 step 11).

        The price table is INJECTED from ``CloudConfig.price_table_file``; a
        deployment without one answers 503 ``summary_unavailable`` instead of
        inventing prices. The local ledgers are never read.
        """
        path = self.app.config.price_table_file
        if not path:
            self._send_json(503, {"error": "summary_unavailable",
                                   "reason": "price_table_file_not_configured"})
            return
        try:
            from .summary import build_cloud_summary
            table = load_price_table(path)
            payload = build_cloud_summary(table)
        except (OSError, ValueError):
            self._send_json(503, {"error": "summary_unavailable",
                                   "reason": "price_table_unreadable"})
            return
        except Exception:  # noqa: BLE001 - never leak details
            self._send_json(500, {"error": "internal_error"})
            return
        self._send_json(200, payload)

    def _send_status(self):
        def call():
            payload = dict(self.app.keys.status())
            payload.setdefault(
                "config_generation", payload.get("desired_generation"))
            payload["backend"] = self.app.backend.status()
            return payload
        self._dispatch(call)

    def _send_keys(self):
        self._dispatch(lambda: self.app.keys.list())

    def _send_backend(self):
        self._dispatch(lambda: self.app.backend.status())

    def _send_key(self, segments):
        key_id = segments[3]
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        self._dispatch(lambda: self.app.keys.get(key_id))

    def _create_key(self, body):
        if self._reject_unknown(
                body, ("name", "max_concurrency", "idempotency_key")):
            return
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX:
            self._send_json(400, {"error": "invalid name"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        max_concurrency = body.get("max_concurrency")
        if max_concurrency is not None and (
                isinstance(max_concurrency, bool)
                or not isinstance(max_concurrency, int)
                or not 1 <= max_concurrency <= 48):
            self._send_json(
                400, {"error": "max_concurrency must be an integer in 1..48"})
            return
        try:
            payload = self.app.keys.create(
                name, max_concurrency=max_concurrency,
                idempotency_key=idempotency_key)
        except KeyApiError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except NotFound:
            self._send_json(404, {"error": "not_found"})
        except (Conflict, Disabled, VersionMismatch):
            self._send_json(409, {"error": "conflict"})
        except BrokerError as exc:
            self._send_json(503, {"error": getattr(exc, "code", None)
                                  or "share_broker_unavailable"})
        except StoreError:
            self._send_json(400, {"error": "invalid_request"})
        except (OSError, ValueError):
            self._send_json(400, {"error": "invalid_request"})
        except Exception:  # noqa: BLE001 - never leak the secret/body
            self._send_json(500, {"error": "internal_error"})
        else:
            self._send_json(201, payload)

    def _revoke_key(self, segments, body):
        key_id = segments[3]
        if self._reject_unknown(
                body, ("expected_row_version", "idempotency_key")):
            return
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        expected = body.get("expected_row_version")
        if isinstance(expected, bool) or not isinstance(expected, int) \
                or expected < 1:
            self._send_json(
                400, {"error": "expected_row_version must be a positive "
                              "integer"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        self._dispatch(lambda: self.app.keys.revoke(
            key_id, expected, idempotency_key=idempotency_key))

    def _rotate_key(self, segments, body):
        key_id = segments[3]
        if self._reject_unknown(
                body, ("expected_row_version", "idempotency_key")):
            return
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        expected = body.get("expected_row_version")
        if isinstance(expected, bool) or not isinstance(expected, int) \
                or expected < 1:
            self._send_json(
                400, {"error": "expected_row_version must be a positive "
                              "integer"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        self._dispatch(lambda: self.app.keys.rotate(
            key_id, expected, idempotency_key=idempotency_key))

    def _drain(self):
        def call():
            payload = self.app.keys.drain()
            if payload.get("ok") is not True:
                raise KeyApiError(
                    payload.get("error_code") or "share_broker_unavailable",
                    "broker drain was not confirmed", status=503)
            return payload
        self._dispatch(call)

    def _backend_start(self):
        self._dispatch(lambda: {"ok": self.app.backend.start(),
                                "status": self.app.backend.status()})

    def _backend_stop(self):
        self._dispatch(lambda: {"ok": self.app.backend.stop(),
                                "status": self.app.backend.status()})
