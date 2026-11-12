# verify_redeploy.py — bounded real-UM verification on the redeployed share.
# Keys read internally; never printed. Model list is expected to fail closed
# (502 upstream_catalog_freshness_unconfirmed) UNTIL chat2api is restarted with
# the raw-fresh contract; this is reported as PENDING, not as a failure, but the
# short answer + stream + auth must pass for real.
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


def req(method, path, key=None, body=None, timeout=40):
    headers = {"Connection": "close", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = __import__("json").dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc).encode()


key = load_key(DATA / "um-share-friend3.txt")
fails = []


def check(name, cond, detail):
    line = "PASS " + name if cond else "FAIL " + name
    if not cond:
        fails.append(name)
    print(line + " | " + str(detail))
    return cond


s, _ = req("GET", "/v1/models")
check("no-key-models-401", s == 401, s)
s, _ = req("GET", "/v1/models", key="um-share-wrong")
check("wrong-key-models-401", s == 401, s)
s, _ = req("GET", "/admin/auth/status", key=key)
check("admin-404", s == 404, s)
s, b = req("GET", "/v1/models", key=key)
if s == 502 and b"freshness_unconfirmed" in b:
    print("PENDING model-list | fail-closed 502 freshness_unconfirmed until chat2api restart (by design)")
else:
    check("model-list-should-gate-until-restart", s == 502, (s, b[:80]))

model = "DeepSeek-V4-Flash-0731"   # answers within a small budget; reliable for bounded checks
s, b = req("POST", "/v1/chat/completions", key=key, body={
    "model": model, "messages": [{"role": "user", "content": "只回答一个词：你好"}],
    "max_tokens": 128, "stream": False}, timeout=60)
content = ""
answered = False
try:
    d = __import__("json").loads(b)
    content = d.get("choices") and d["choices"][0].get("message", {}).get("content") or ""
    if isinstance(content, list):
        # multimodal content blocks; flatten text
        content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
    answered = bool(content)
except Exception:  # noqa: BLE001
    content = content or "(unparseable)"
check("short-chat-real-200-answered", s == 200 and answered, (s, repr(content[:60])))

s, b = req("POST", "/v1/chat/completions", key=key, body={
    "model": model, "messages": [{"role": "user", "content": "数到三"}],
    "max_tokens": 128, "stream": True}, timeout=60)
check("stream-real-200-done", s == 200 and b"data: [DONE]" in b, (s, b[-40:]))

print("RESULT=" + ("FAIL" if fails else "PASS") + ((" : " + "; ".join(fails)) if fails else ""))
raise SystemExit(1 if fails else 0)
