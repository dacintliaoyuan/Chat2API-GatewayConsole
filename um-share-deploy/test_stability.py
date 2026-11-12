# test_stability.py — two stability acceptances against the share (8100):
#   T1 long streaming answer continues and ends with [DONE]
#   T2 abort a stream mid-way, then IMMEDIATELY send a new request and get an answer
# Uses friend3 key; never prints the key. Bounded (max_tokens ~1024).
import http.client
import json
import re
import time
import urllib.request
from pathlib import Path

BASE = "127.0.0.1:8100"
DATA = Path(r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data")
fails = []


def load_key():
    m = re.search(r"um-share-[A-Za-z0-9_\-]+",
                  (DATA / "um-share-friend3.txt").read_text(encoding="utf-8"))
    return m.group(0)


KEY = load_key()


def check(name, cond, detail):
    print(("PASS " if cond else "FAIL ") + name + " | " + str(detail))
    if not cond:
        fails.append(name)


# ---------- T1: long streaming answer, continuous + clean end ----------
body = json.dumps({
    "model": "GLM-5.2-NVFP4",
    "messages": [{"role": "user",
                  "content": "请写一段较长的中文介绍（约500字），介绍澳门大学，分自然段，尽量完整。"}],
    "max_tokens": 1024, "stream": True}).encode()
conn = http.client.HTTPConnection(BASE, timeout=120)
t0 = time.time()
sse_events = 0
first_delta = None
done = False
error = False
conn.putrequest("POST", "/v1/chat/completions")
conn.putheader("Authorization", "Bearer " + KEY)
conn.putheader("Content-Type", "application/json")
conn.putheader("Content-Length", str(len(body)))
conn.endheaders()
conn.send(body)
resp = conn.getresponse()
status = resp.status
sse_events = 0
if status == 200:
    while True:
        line = resp.readline()
        if not line:
            break
        if line.startswith(b"data:") and line.strip() != b"data:":
            sse_events += 1
            if sse_events == 1:
                first_delta = time.time() - t0
        if b"data: [DONE]" in line:
            done = True
            break
        if time.time() - t0 > 110:
            error = "timeout reading stream"
            break
    dur = time.time() - t0
else:
    dur = time.time() - t0
conn.close()

check("T1-stream-200", status == 200, status)
check("T1-first-chunk-quick", (first_delta is not None and first_delta < 30), first_delta)
check("T1-many-sse-events", sse_events >= 50, sse_events)
check("T1-clean-end", done and not error, (done, error))
check("T1-duration-answer-long", dur >= 5, round(dur, 1))
print("  T1 metrics: sse_events=%d dur=%.1fs" % (sse_events, dur))

# ---------- T2: abort mid-stream, then IMMEDIATELY new request ----------
def abort_mid_stream():
    b2 = json.dumps({
        "model": "GLM-5.2-NVFP4",
        "messages": [{"role": "user", "content": "请写一段很长的文章介绍人工智能，越多越好，持续输出。"}],
        "max_tokens": 1024, "stream": True}).encode()
    c = http.client.HTTPConnection(BASE, timeout=10)
    c.putrequest("POST", "/v1/chat/completions")
    c.putheader("Authorization", "Bearer " + KEY)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(b2)))
    c.endheaders()
    c.send(b2)
    r = c.getresponse()
    got_any = False
    for _ in range(200):
        line = r.readline()
        if not line:
            break
        if b"data: [DONE]" in line:
            got_any = True
            break
        # any SSE data event means streaming is underway (reasoning/content)
        if line.startswith(b"data:") and line.strip() != b"data:":
            got_any = True
            break
    # abort mid-stream: close the RESPONSE (owns the socket), then the conn
    try:
        r.close()
    except Exception:
        pass
    try:
        c.close()
    except Exception:
        pass
    return got_any


got_any = abort_mid_stream()
check("T2-abort-got-content-first", got_any, got_any)
# immediately after abort, fire a new request (+tiny retry window for slot release)
new_ok = False
attempts = 0
deadline = time.time() + 15
while time.time() < deadline and not new_ok:
    attempts += 1
    try:
        rr = urllib.request.Request(
            "http://127.0.0.1:8100/v1/chat/completions",
            data=json.dumps({
                "model": "DeepSeek-V4-Flash-0731",
                "messages": [{"role": "user", "content": "只回答一个词：恢复"}],
                "max_tokens": 64, "stream": False}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + KEY + "", "Connection": "close"})
        with urllib.request.urlopen(rr, timeout=40) as resp2:
            d = json.loads(resp2.read())
            c2 = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            new_ok = bool(c2)
            if new_ok:
                break
    except Exception as exc:  # noqa: BLE001
        last = str(exc)[:80]
        time.sleep(0.5)
check("T2-new-request-after-abort", new_ok, ("attempts=" + str(attempts)))

print("RESULT=" + ("FAIL" if fails else "PASS"))
raise SystemExit(1 if fails else 0)
