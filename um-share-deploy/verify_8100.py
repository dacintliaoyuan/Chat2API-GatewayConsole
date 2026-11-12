#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_8100.py — non-elevated functional verification of the share entry.

Runs against 127.0.0.1:8100 (local) and/or the public Funnel URL. Reads the
friend key from the restricted delivery file WITHOUT printing it. Verifies:
  * no key / wrong key -> 401
  * /admin/* and other paths -> 404
  * model list (probe: connectivity-test; real: raw upstream, no virtual
    aliases)
  * non-stream + stream chat
  * revocation behaviour is covered separately by the management-service
    path (share/admin.py + tests/test_share_management*.py), never against
    the delivered key. key_lifecycle_20260906.py is SUPERSEDED: the gateway
    no longer provisions keys and now requires an explicit upstream port.
Usage:  python verify_8100.py --base http://127.0.0.1:8100 --key-file <delivery> [--probe]
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_key(delivery_file: str) -> str:
    text = Path(delivery_file).read_text(encoding="utf-8")
    m = re.search(r"um-share-[A-Za-z0-9_\-]+", text)
    assert m, "no um-share key in delivery file"
    return m.group(0)


def req(base, path, method="GET", key=None, body=None, timeout=30, stream=False):
    url = base.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Connection": "close"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    key = load_key(args.key_file)

    failures = []

    def expect(desc, cond, detail):
        print(("PASS " if cond else "FAIL ") + desc + (" | " + str(detail) if detail else ""))
        if not cond:
            failures.append(desc)

    # negative auth
    s, _ = req(args.base, "/v1/models")
    expect("no-key /v1/models rejects", s == 401, s)
    s, _ = req(args.base, "/v1/models", key="um-share-wrong-key")
    expect("wrong-key /v1/models rejects", s == 401, s)
    s, _ = req(args.base, "/admin/auth/status", key=key)
    expect("admin path not shared", s == 404, s)
    s, _ = req(args.base, "/v1/models?x=1", key=key)
    expect("query string rejected", s == 404, s)

    # model list
    s, body = req(args.base, "/v1/models", key=key)
    expect("models 200", s == 200, s)
    try:
        data = json.loads(body)["data"]
        ids = [m["id"] for m in data]
    except Exception as exc:  # noqa: BLE001
        data = []
        ids = []
        print("  models parse err: " + repr(exc))
    print("  model ids: " + str(ids[:20]))
    if args.probe:
        expect("probe model connectivity-test", ids == ["connectivity-test"], ids)
    elif ids:
        banned = [i for i in ids if re.search(r"-(Fast|Low|Medium|High)$", i)]
        expect("no local virtual aliases leaked", not banned, banned)

    # short chat
    model = "connectivity-test" if args.probe else (ids[0] if ids else None)
    if model:
        s, body = req(args.base, "/v1/chat/completions", method="POST", key=key,
                      body={"model": model, "messages": [{"role": "user", "content": "ping"}],
                            "stream": False, "max_tokens": 16})
        expect("short chat 200", s == 200, s)
        if s == 200:
            try:
                content = json.loads(body)["choices"][0]["message"]["content"]
                print("  short answer: " + str(content)[:80])
            except Exception as exc:  # noqa: BLE001
                print("  short chat parse err: " + repr(exc) + " body=" + body[:120])
    else:
        print("  SKIP short chat (no model available)")

    # stream chat (probe only needs 200 + first event)
    if model:
        s, body = req(args.base, "/v1/chat/completions", method="POST", key=key,
                      body={"model": model, "messages": [{"role": "user", "content": "stream test"}],
                            "stream": True, "max_tokens": 16}, timeout=45)
        expect("stream chat 200", s == 200, s)
        if s == 200:
            print("  stream tail: " + body[-120:].replace("\n", " "))

    print("\nRESULT: " + ("FAILURES: " + "; ".join(failures) if failures else "ALL PASS"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
