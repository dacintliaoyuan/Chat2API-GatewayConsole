#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the cutover data-parity fixture (#72).

The Rust cutover reads the SAME production data directories Python writes; no
migration is allowed.  This generator builds one isolated data directory with the
REAL Python owners:

* ``share.store.ShareStore`` writes ``share-store.db`` (schema 3),
* ``share.runtime.ProtectedProjectionWriter`` writes ``run/share-projection.json``,
* ``chat2api.record_usage`` writes the append-only share ledger,
* ``chat2api.TokenStore`` writes ``share-backend/token.json``,
* ``chat2api._write_profile_lock_owner`` writes the browser profile lock.

Only the non-deterministic sources (uuid, clock, nonce, secret entropy) are
replaced, so the committed fixture is reproducible.  The generator never touches
a production path, never starts a browser and never contacts UM.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
DATA_DIR = ROOT / "tests" / "fixtures" / "cutover_parity"
FIXTURE = ROOT / "tests" / "fixtures" / "rust_cutover_parity_rev1.json"

# The production console data paths.  The fixture mirrors the real layout but
# every byte lives under a caller-provided temporary root.
PROFILE_DIR = "/var/lib/um-share-console/share-backend/edge-profile"
FIXED_TIME = datetime(2026, 11, 16, 0, 0, 0, tzinfo=timezone.utc)
FIXED_EPOCH = FIXED_TIME.timestamp()
FIXED_KEY = bytes(range(32))

ALPHA_SECRET = "um-share-alpha-0123456789abcdef"
BETA_SECRET = "um-share-beta-0123456789abcdef"
ROTATED_SECRET = "um-share-rotated-0123456789abcdef"
LEGACY_SECRET = "um-share-legacy-0123456789abcdef"


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = _load("cutover_parity_store", "share/store.py")
protocol = _load("cutover_parity_protocol", "share/broker_protocol.py")
runtime = _load("cutover_parity_runtime", "share/runtime.py")
metering = _load("cutover_parity_metering", "share/metering.py")
chat2api = _load("cutover_parity_chat2api", "chat2api/chat2api.py")


class _FixedStamp:
    def astimezone(self):
        return self

    def isoformat(self, timespec="seconds"):
        if timespec != "seconds":
            raise AssertionError("unexpected timespec")
        return "2026-11-16T08:00:00+08:00"


class _FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        return _FixedStamp()


class _Deterministic:
    """Deterministic uuid/clock/nonce/secret sources with a restore point."""

    def __init__(self):
        self._uuid_index = 0
        self._now_index = 0
        self._nonce_index = 0
        self._saved_uuid4 = store.uuid.uuid4
        self._saved_now = store._now
        self._saved_protect = store._protect_for_new_row
        self._saved_token = store.secrets.token_urlsafe

    def uuid4(self):
        self._uuid_index += 1
        return uuid.UUID(int=self._uuid_index)

    def now(self):
        self._now_index += 1
        return "2026-11-16T00:00:%02d.000000Z" % self._now_index

    def protect(self, secret):
        self._nonce_index += 1
        nonce = bytes([self._nonce_index]) * 12
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        sealed = AESGCM(FIXED_KEY).encrypt(
            nonce, secret.encode("utf-8"), b"aesgcm-file-key-v1")
        return nonce + sealed, "aesgcm-file-key-v1"

    def token_urlsafe(self, _bytes):
        return "rotated-token-0123456789abcdefghijklmnopqrstuvwxyz"

    def __enter__(self):
        store.uuid.uuid4 = self.uuid4
        store._now = self.now
        store._protect_for_new_row = self.protect
        store.secrets.token_urlsafe = self.token_urlsafe
        return self

    def __exit__(self, *_exc):
        store.uuid.uuid4 = self._saved_uuid4
        store._now = self._saved_now
        store._protect_for_new_row = self._saved_protect
        store.secrets.token_urlsafe = self._saved_token
        return False


def _build_store(db_path: Path) -> None:
    with _Deterministic():
        instance = store.ShareStore(db_path)
        try:
            alpha = instance.create_key(
                "alpha", secret=ALPHA_SECRET,
                idempotency_key="create-alpha", max_concurrency=16)
            beta = instance.create_key("beta", secret=BETA_SECRET)
            alpha_id = alpha["key_id"]
            beta_id = beta["key_id"]
            instance.rename_key(alpha_id, "alpha-renamed",
                                expected_row_version=1)
            instance.update_key(alpha_id, max_concurrency=8,
                                expected_row_version=2)
            instance.rotate_key(beta_id, idempotency_key="rotate-beta")
            instance.delete_key(beta_id)
            instance.set_share_max_concurrency(
                32, expected=64, idempotency_key="limits-1")
            instance.commit_applied(
                6, instance_id="instance-a", security_generation=5)
            instance.record_operation(
                "gateway_start", "ok", {"source": "fixture"},
                idempotency_key="op-1")
        finally:
            instance.close()

    # A legacy hash-only row: schema 3 rows written before the ciphertext
    # contract keep NULL blob/algo and answer SecretUnavailable on readback.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO keys(key_id,name,secret_hash,secret_version,"
            "masked_key,secret_blob,secret_algo,enabled,max_concurrency,"
            "created_at,updated_at,deleted_at,row_version)"
            " VALUES(?,?,?,1,?,NULL,NULL,1,1,?,?,NULL,1)",
            ("legacy-key", "legacy",
             hashlib.sha256(LEGACY_SECRET.encode("utf-8")).hexdigest(),
             "um-share-****cdef",
             "2026-11-16T00:00:10.000000Z",
             "2026-11-16T00:00:10.000000Z"))
        conn.commit()
    finally:
        conn.close()


def _dump_store(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return "\n".join(conn.iterdump()) + "\n"
    finally:
        conn.close()


def _write_projection(path: Path) -> dict:
    payload = {
        "schema_version": 2,
        "config_generation": 8,
        "share_max_concurrency": 32,
        "data": [
            {
                "key_id": "00000000-0000-0000-0000-000000000001",
                "name": "alpha-renamed",
                "secret_hash": hashlib.sha256(
                    ALPHA_SECRET.encode("utf-8")).hexdigest(),
                "secret_version": 1,
                "enabled": True,
                "max_concurrency": 8,
            },
            {
                "key_id": "legacy-key",
                "name": "legacy",
                "secret_hash": hashlib.sha256(
                    LEGACY_SECRET.encode("utf-8")).hexdigest(),
                "secret_version": 1,
                "enabled": True,
                "max_concurrency": 1,
            },
        ],
    }
    parsed = protocol.parse_projection_payload(payload)
    from share.management import _fingerprint

    fingerprint = _fingerprint(payload)
    writer = runtime.ProtectedProjectionWriter(path, lambda _path: None)
    written = writer.write(payload, payload["config_generation"],
                           fingerprint)
    if written != fingerprint:
        raise AssertionError("projection writer fingerprint mismatch")
    return {"payload": payload, "parsed": parsed,
            "fingerprint": fingerprint}


def _write_ledger(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    started_at = FIXED_EPOCH - 0.123

    def usage(prompt=None, completion=None, cached=None, reasoning=None,
              total=None):
        block = {}
        if prompt is not None:
            block["prompt_tokens"] = prompt
        if completion is not None:
            block["completion_tokens"] = completion
        if total is not None:
            block["total_tokens"] = total
        if cached is not None:
            block["prompt_tokens_details"] = {"cached_tokens": cached}
        if reasoning is not None:
            block["completion_tokens_details"] = {
                "reasoning_tokens": reasoning}
        return block

    rows = [
        ("chat", "key-alpha", "r1", "ok", usage(100, 10, 0, 0, 110), 0, 0,
         "upstream"),
        ("messages", "key-alpha", "r2", "ok", usage(200), 0, 7, None),
        ("responses", "key-deleted", "r3", "ok", None, 10, 5, "estimated"),
        ("chat", None, "r4", "ok", None, 0, 0, "none"),
        ("chat", "key-alpha", "r5", "client_disconnected",
         usage(40, 4), 0, 0, None),
        ("messages", "key-alpha", "r6", "upstream_read_error",
         usage(15, 2), 0, 0, "partial"),
    ]
    with mock.patch.object(chat2api, "USAGE_LOG", str(path)), \
            mock.patch.object(chat2api, "_usage_disabled", False), \
            mock.patch.object(chat2api, "datetime", _FixedDateTime), \
            mock.patch.object(chat2api.time, "time",
                              return_value=FIXED_EPOCH):
        for index, (protocol_name, key_id, request_id, status, usage_block,
                    in_est, out_est, source) in enumerate(rows):
            chat2api.set_request_facts(
                protocol_name, key_id, request_id,
                "verified" if key_id else "absent")
            chat2api.record_usage(
                "cutover-parity-model", index % 2 == 1, started_at,
                usage_block or None, in_est, out_est, status=status,
                usage_source=source)


def _write_token(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token_store = chat2api.TokenStore(path, "http://127.0.0.1:8000")
    with mock.patch.object(chat2api.time, "strftime",
                           return_value="2026-11-16 00:00:00"):
        token_store.save("fixture-token-value", "fixture-api-key")


def _write_profile_lock(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = profile_dir / "login-browser.lock"
    with lock_path.open("wb") as handle:
        handle.write(b"\x00")
        handle.flush()
        # The lock file is written under the isolated temp profile, but the
        # recorded diagnostic path is the production path Python uses in the
        # cloud deployment.  That keeps the fixture reproducible on every host
        # without pretending the profile moved.
        fake_handle = SimpleNamespace(
            fd=handle.fileno(),
            path=PROFILE_DIR + "/login-browser.lock")
        with mock.patch.object(chat2api.os, "getpid", return_value=4242), \
                mock.patch.object(chat2api, "_process_start_epoch",
                                  return_value=1700000000.0), \
                mock.patch.object(chat2api.time, "time",
                                  return_value=1700000000.25):
            chat2api._write_profile_lock_owner(fake_handle)


def _normalize_checked_at(value):
    if isinstance(value, dict):
        return {
            key: "<checkedAt>" if key == "checkedAt"
            else _normalize_checked_at(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_checked_at(item) for item in value]
    return value


def _file_entry(path: Path, root: Path) -> dict:
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "path": path.relative_to(root).as_posix(),
    }


def _store_views(db_path: Path) -> dict:
    instance = store.ShareStore(db_path)
    try:
        generation = instance.generation()
        keys = instance.list_keys(include_deleted=True)
        operations = instance.operations()
        records = instance.security_records()
        floor = instance.security_floor()
        state = instance.security_state()
        state.pop("exported_at", None)
        projection = instance.render_projection()
        share_max = instance.get_share_max_concurrency()
    finally:
        instance.close()
    return {
        "generation": generation,
        "keys": keys,
        "operations": operations,
        "security": {"records": records, "floor": floor, "state": state},
        "projection": projection,
        "share_max_concurrency": share_max,
    }


def _ledger_views(ledger_path: Path) -> dict:
    summary = metering.read_share_usage(
        ledger_path, now=FIXED_EPOCH)
    requests = metering.read_share_requests(
        ledger_path, now=FIXED_EPOCH)
    return {
        "summary": _normalize_checked_at(summary),
        "requests": _normalize_checked_at(requests),
    }


def _token_view(token_path: Path) -> dict:
    return chat2api.TokenStore(
        token_path, "http://127.0.0.1:8000").load()


def _profile_view(profile_dir: Path) -> dict:
    owner = chat2api._read_profile_lock_owner(str(profile_dir))
    return {"owner": owner,
            "lock_hex": (profile_dir / "login-browser.lock").read_bytes().hex()}


def build(root: Path) -> dict:
    """Write the Python-owned data directory under ``root`` and return views."""
    root = Path(root)
    (root / "run").mkdir(parents=True, exist_ok=True)
    (root / "share-backend" / "edge-profile").mkdir(
        parents=True, exist_ok=True)

    db_path = root / "share-store.db"
    projection_path = root / "run" / "share-projection.json"
    ledger_path = root / "share-backend" / "usage-share-backend.jsonl"
    token_path = root / "share-backend" / "token.json"
    profile_dir = root / "share-backend" / "edge-profile"

    _build_store(db_path)
    projection = _write_projection(projection_path)
    _write_ledger(ledger_path)
    _write_token(token_path)
    _write_profile_lock(profile_dir)
    store_views = _store_views(db_path)

    sql_path = root / "share-store.sql"
    sql_path.write_text(_dump_store(db_path), encoding="utf-8", newline="\n")
    # The Rust parity test recreates the store from the Python SQL dump; the
    # binary SQLite file is not part of the committed fixture (its bytes depend
    # on the linked SQLite build, while the dump is the logical data).
    db_path.unlink()

    return {
        "metadata": {
            "fixture": True,
            "ticket": 72,
            "generator": "tests/generate_cutover_parity_fixture.py",
            "source": "real Python ShareStore/ProjectionWriter/record_usage/TokenStore",
            "dataRoot": "tests/fixtures/cutover_parity",
            "productionPaths": [
                "/var/lib/um-share",
                "/var/lib/um-share-console",
                "/etc/um-share",
            ],
        },
        "python": {
            "store": store_views,
            "ledger": _ledger_views(ledger_path),
            "projection": projection,
            "token": _token_view(token_path),
            "profile": _profile_view(profile_dir),
        },
        "dataFiles": {
            entry["path"]: {
                "sha256": entry["sha256"],
                "size": entry["size"],
            }
            for entry in (
                _file_entry(sql_path, root),
                _file_entry(projection_path, root),
                _file_entry(ledger_path, root),
                _file_entry(token_path, root),
                _file_entry(profile_dir / "login-browser.lock", root),
            )
        },
    }


def generate() -> dict:
    root = Path(tempfile.mkdtemp(prefix="cutover-parity-"))
    try:
        return build(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_fixture() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    fixture = build(DATA_DIR)
    FIXTURE.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")


def main() -> None:
    write_fixture()
    print("wrote %s" % FIXTURE)


if __name__ == "__main__":
    main()
