#!/usr/bin/env python3
"""Generate the broker wire fixture from the Python protocol owner.

The Rust broker tests never hand-write canonical JSON, HMAC payloads or budget
numbers. They deserialize ``tests/fixtures/rust_broker_wire_rev1.json``, which this
generator produces from ``share/broker_protocol.py``.

Run from the repository root:

    python tests/generate_rust_broker_fixture.py
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = "share/broker_protocol.py"
DESTINATION = ROOT / "tests" / "fixtures" / "rust_broker_wire_rev1.json"

sys.path[:0] = [str(ROOT)]


def _load_protocol():
    """Load the protocol module under a private name.

    Other test modules put ``chat2api/`` on ``sys.path`` and register bare
    module names; a private name keeps this generator independent of test
    execution order.
    """
    path = ROOT / SOURCE_RELATIVE
    spec = importlib.util.spec_from_file_location("rust_broker_wire_protocol", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rust_broker_wire_protocol"] = module
    spec.loader.exec_module(module)
    return module


protocol = _load_protocol()


def _canonical_vectors():
    values = [
        {"b": 1, "a": [1, True, None, "é"], "c": {"z": 2, "y": "x"}},
        {
            "quote": 'a"b',
            "backslash": "a\\b",
            "controls": "\b\f\n\r\t\x1f\x7f",
            "latin": "\x80é",
            "line_separator": "\u2028",
            "astral": "\U0001f600",
        },
        {"a": 1.0, "b": 1e16, "c": 1e-5, "d": 1.2345, "e": 0.0,
         "f": -0.0, "g": 1.234e-7, "h": 1.5e20},
        {"z": -1, "y": 9007199254740993, "x": 18446744073709551615},
        [{"b": {"d": 1, "c": 2}, "a": 3}, [{"f": 4, "e": 5}]],
    ]
    return [{"value": value,
             "encoded": json.dumps(value, sort_keys=True)}
            for value in values]


def _frame_vectors():
    values = [
        {"b": 2, "a": 1},
        {"v": 2, "action": "status", "request_id": "r-1", "ts": 1,
         "fields": {}},
    ]
    frames = []
    for value in values:
        line = protocol.encode_frame(value)
        frames.append({"value": value,
                       "encoded": line.decode("utf-8")})
    return frames


def _hmac_vectors():
    bodies = [
        {"v": 2, "action": "status", "request_id": "r-1", "ts": 1,
         "fields": {}},
        {"v": 2, "action": "load", "request_id": "r-2", "ts": 2,
         "instance_id": "i-1",
         "fields": {"generation": 3, "fingerprint": "f" * 64}},
        {"v": 2, "action": "publish", "request_id": "r-3", "ts": 3,
         "fields": {"generation": 4, "fingerprint": "a" * 64,
                    "projection": {"schema_version": 1,
                                   "config_generation": 4,
                                   "data": []}}},
    ]
    vectors = []
    for body in bodies:
        signed_keys = (protocol.INSTANCE_SIGNED_KEYS
                       if "instance_id" in body
                       else protocol.MANAGEMENT_SIGNED_KEYS)
        payload = json.dumps(
            {key: body[key] for key in signed_keys if key in body},
            sort_keys=True)
        mac = hmac.new(b"test-broker-secret", payload.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        vectors.append({"secret": "test-broker-secret",
                        "signedKeys": list(signed_keys),
                        "body": body,
                        "payload": payload,
                        "mac": mac})
    return vectors


def _source_hashes(paths):
    hashes = {}
    for relative in paths:
        text = (ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        hashes[relative] = hashlib.sha256(text).hexdigest()
    return hashes


def generate():
    return {
        "metadata": {
            "fixture": True,
            "generatedFrom": "tests/generate_rust_broker_fixture.py",
            "sourceHashEncoding": "utf8-lf",
            "sourceHashes": _source_hashes([
                SOURCE_RELATIVE,
                "tests/generate_rust_broker_fixture.py",
            ]),
        },
        "canonicalJson": _canonical_vectors(),
        "frames": _frame_vectors(),
        "hmac": _hmac_vectors(),
    }


if __name__ == "__main__":
    result = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print("Generated Rust broker wire fixture:", DESTINATION)
