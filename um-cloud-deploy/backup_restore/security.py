"""Security journal publication and confirmation."""
from __future__ import annotations

import json

from .constants import SECURITY_FORMAT, SECURITY_PREFIX
from .errors import BackupSecurityError, ObjectStoreError
from .objectstore import ObjectStore
from .package import _json_bytes, _validate_security_state

class SecurityRecordStoreAdapter:
    """Object-store adapter for the cross-machine security journal."""

    def __init__(self, object_store, *, prefix=SECURITY_PREFIX):
        if not isinstance(object_store, ObjectStore):
            raise ObjectStoreError("security adapter needs an object store")
        self.object_store = object_store
        self.prefix = prefix

    def put_record(self, record):
        state = _validate_security_state({
            "format": SECURITY_FORMAT,
            "generation_floor": record["generation"],
            "records": [record],
        })
        record = state["records"][0]
        key = (self.prefix
               + f"{record['generation']:020d}-{record['record_id']}.json")
        self.object_store.put(key, _json_bytes(record))
        return {"key": key}

    def latest(self):
        rows = self.object_store.list(self.prefix)
        if not rows:
            raise BackupSecurityError(
                "no remote security record is available")
        rows = sorted(rows, key=lambda item: item["key"])
        key = rows[-1]["key"]
        raw = self.object_store.get(key)
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupSecurityError(
                "remote security record is not valid JSON") from exc
        state = _validate_security_state({
            "format": SECURITY_FORMAT,
            "generation_floor": record.get("generation"),
            "records": [record],
        })
        return {"confirmed": True, "key": key, "state": state}


def publish_security_records(store, adapter, *, now=None) -> dict:
    """Push pending security intents to the independent object store.

    A failed/uncertain upload leaves the row pending: the caller must never
    report cross-machine protection as complete.  Only a successful adapter
    receipt marks the row ``remote_confirmed``.
    """
    if adapter is None:
        state = store.security_state()
        return {"confirmed": 0, "pending": len(state["records"]),
                "errors": ["security adapter unavailable"]}
    state = store.security_state()
    confirmed, errors = 0, []
    for record in state["records"]:
        if record["status"] == "remote_confirmed":
            confirmed += 1
            continue
        try:
            receipt = adapter.put_record(record)
        except Exception as exc:  # noqa: BLE001 - pending is fail-closed
            errors.append({"record_id": record["record_id"],
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        key = receipt.get("key") if isinstance(receipt, dict) else None
        try:
            store.mark_security_remote_confirmed(record["record_id"], key)
        except Exception as exc:  # noqa: BLE001 - stays pending
            errors.append({"record_id": record["record_id"],
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        confirmed += 1
    return {"confirmed": confirmed,
            "pending": len(state["records"]) - confirmed,
            "errors": errors}


def latest_security_state(adapter) -> dict:
    """Read the independent latest security state; unconfirmed is not "safe"."""
    if adapter is None:
        return {"confirmed": False, "state": None,
                "reason": "security adapter unavailable"}
    try:
        latest = adapter.latest()
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return {"confirmed": False, "state": None,
                "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(latest, dict) or not latest.get("confirmed"):
        return {"confirmed": False, "state": None,
                "reason": "remote security state is not confirmed"}
    try:
        state = _validate_security_state(latest["state"])
    except BackupSecurityError as exc:
        return {"confirmed": False, "state": None, "reason": str(exc)}
    return {"confirmed": True, "state": state, "key": latest.get("key")}
