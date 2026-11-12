# verify_revoke2.py — assert the running share rejects friend2 and still serves
# friend3. Keys are read internally from the restricted delivery files and are
# NEVER printed. Writes only PASS/FAIL lines to stdout.
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8100"
DATA = Path(r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data")


def load_key(path: Path):
    m = re.search(r"um-share-[A-Za-z0-9_\-]+", path.read_text(encoding="utf-8"))
    if not m:
        raise RuntimeError("no key in " + str(path))
    return m.group(0)


def req(method="GET", key=None, body=None, timeout=40):
    headers = {"Content-Type": "application/json", "Connection": "close"}
    if key:
        headers["Authorization"] = "Bearer " + key
    import json as _json
    data = _json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + ("/v1/models" if method == "GET" else "/v1/chat/completions"),
                               data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001
        return -1


friend2 = load_key(DATA / "um-share-friend2.txt")
friend3 = load_key(DATA / "um-share-friend3.txt")
# friend2 revoked -> rejected at auth (401) even on the model list
s2 = req("GET", key=friend2)
# friend3 still valid -> a real bounded chat completion returns 200 (the model
# list itself now correctly fails closed 502 until chat2api restart, so auth is
# proven via chat, not via /v1/models)
s3 = req("POST", key=friend3, body={
    "model": "DeepSeek-V4-Flash-0731",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16, "stream": False})
ok2 = s2 == 401
ok3 = s3 == 200
print(("PASS" if ok2 else "FAIL") + f" friend2 -> {s2} (expect 401)")
print(("PASS" if ok3 else "FAIL") + f" friend3 short-chat -> {s3} (expect 200)")
raise SystemExit(0 if (ok2 and ok3) else 1)
