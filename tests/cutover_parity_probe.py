#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only probe used by the cutover parity tests (#72).

The Rust tests call this script with an isolated data directory; it only reads
the files it is given and prints one JSON document.  It never writes a store,
ledger, projection, token or profile, never opens a browser and never contacts UM.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = _load("cutover_probe_store", "share/store.py")
protocol = _load("cutover_probe_protocol", "share/broker_protocol.py")
metering = _load("cutover_probe_metering", "share/metering.py")
chat2api = _load("cutover_probe_chat2api", "chat2api/chat2api.py")


def _ledger(path: Path, now: float) -> dict:
    summary = metering.read_share_usage(path, now=now)
    requests = metering.read_share_requests(path, now=now)
    return {
        "requests": summary["requests"],
        "keys": summary["keys"],
        "unattributed": summary["unattributed"],
        "results": summary["results"],
        "coverage": summary["coverage"],
        "rows": requests["rows"],
    }


def _store(path: Path) -> dict:
    instance = store.ShareStore(path)
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


def _projection(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = protocol.parse_projection_payload(payload)
    from share.management import _fingerprint

    return {"payload": payload, "parsed": parsed,
            "fingerprint": _fingerprint(payload)}


def _token(path: Path, url: str) -> dict:
    return chat2api.TokenStore(path, url).load()


def _profile(directory: Path) -> dict:
    owner = chat2api._read_profile_lock_owner(str(directory))
    lock = directory / "login-browser.lock"
    return {"owner": owner, "lock_hex": lock.read_bytes().hex()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ledger_parser = subparsers.add_parser("ledger")
    ledger_parser.add_argument("--path", required=True, type=Path)
    ledger_parser.add_argument("--now", required=True, type=float)

    store_parser = subparsers.add_parser("store")
    store_parser.add_argument("--path", required=True, type=Path)

    projection_parser = subparsers.add_parser("projection")
    projection_parser.add_argument("--path", required=True, type=Path)

    token_parser = subparsers.add_parser("token")
    token_parser.add_argument("--path", required=True, type=Path)
    token_parser.add_argument("--url", required=True)

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("--dir", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "ledger":
        output = _ledger(args.path, args.now)
    elif args.command == "store":
        output = _store(args.path)
    elif args.command == "projection":
        output = _projection(args.path)
    elif args.command == "token":
        output = _token(args.path, args.url)
    else:
        output = _profile(args.dir)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
