# verify_redeploy2.py — post chat2api-restart verification on the share (8100).
# Now that chat2api serves the raw-fresh contract, the SHARE model list must
# return 200 with the raw upstream catalog (no virtual aliases). Also checks
# no/wrong key 401, admin 404, friend2 revoked 401, friend3 short + stream.
import json
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


def req(method, path, key=None, body=None, timeout=60):
    headers = {"Content-Type": "application/json", "Connection": "close"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {}, exc.read()


friend3 = load_key(DATA / "um-share-friend3.txt")
friend2 = load_key(DATA / "um-share-friend2.txt")
fails = []


def check(name, cond, detail):
    print(("PASS " if cond else "FAIL ") + name + " | " + str(detail))
    if not cond:
        fails.append(name)


s, _, _ = req("GET", "/v1/models")
check("no-key-401", s == 401, s)
s, _, _ = req("GET", "/v1/models", key="um-share-wrong")
check("wrong-key-401", s == 401, s)
s, _, _ = req("GET", "/admin/auth/status", key=friend3)
check("admin-404", s == 404, s)
s, _, _ = req("GET", "/v1/models", key=friend2)
check("friend2-revoked-401", s == 401, s)

# friend3 model list: must be 200 + raw (no aliases)
s, hd, b = req("GET", "/v1/models", key=friend3)
ids = []
banned = []
if s == 200:
    try:
        p = json.loads(b)
        ids = [m.get("id") for m in p.get("data", []) if isinstance(m, dict)]
        banned = [i for i in ids if re.search(r"-(Fast|Low|Medium|High)$", i)]
    except Exception as exc:  # noqa: BLE001
        ids = ["PARSE:" + str(exc)]
check("models-200-fresh", s == 200 and len(ids) > 0, (s, ids[:6]))
check("models-no-aliases", not banned, banned)

# friend3 short + stream
model = "DeepSeek-V4-Flash-0731"
s, hd, b = req("POST", "/v1/chat/completions", key=friend3, body={
    "model": model, "messages": [{"role": "user", "content": "只回答一个词：你好"}],
    "max_tokens": 128, "stream": False})
c = ""
try:
    d = json.loads(b)
    c = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
except Exception:
    pass
check("friend3-short-answered", s == 200 and bool(c), (s, repr(c[:30])))

s, hd, b = req("POST", "/v1/chat/completions", key=friend3, body={
    "model": model, "messages": [{"role": "user", "content": "数到三"}],
    "max_tokens": 128, "stream": True}, timeout=90)
check("friend3-stream-done", s == 200 and b"data: [DONE]" in b, (s, b[-30:]))

print("RESULT=" + ("FAIL" if fails else "PASS"))
raise SystemExit(1 if fails else 0)
