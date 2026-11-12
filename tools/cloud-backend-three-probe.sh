#!/usr/bin/env bash
# 云端后端三条症状的只读回路（diagnosing-bugs Phase 1）。
# 每条断言用户看到的具体症状，红了就是复现。
# 只读：只发 GET，不重启任何服务，不写任何状态。
# 读面清单与判据见 docs/gateway-entrypoints.md §2.1；不要在云机上再起第二个浏览器
# （2C2G 内存边界，见 um-cloud-deploy/README.md §4.1）。
set -u

SSH_CONF="C:/Users/dacint/.ssh/aliyun-um-ecs.conf"
HOST="um-share-ecs"
BASE="http://127.0.0.1:18096"
fail=0

run() {
  ssh -F "$SSH_CONF" -o BatchMode=yes "$HOST" "$1" 2>&1
}

echo "=== 1. broker 是否还在连陈旧的 instance socket ==="
out=$(run "curl -s -m 10 $BASE/api/console/share-management/status")
printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print("FAIL  status 读面解析失败: %s" % exc); raise SystemExit(1)
broker = d.get("broker") or {}
diag = broker.get("diag") or []
refused = [r for r in diag if r.get("ok") is False
           and "Connection refused" in (r.get("detail") or "")]
if refused:
    last = refused[-1]
    print("FAIL  broker.diag %d 条 ConnectionRefusedError, 最近 instance_id=%s stage=%s"
          % (len(refused), last.get("instance_id"), last.get("stage")))
    raise SystemExit(1)
print("PASS  broker.diag 无 ConnectionRefusedError（%d 条 diag）" % len(diag))
' || fail=1

echo "=== 2. public origin 是否配了 ==="
out=$(run "curl -s -m 10 $BASE/api/console/share-management/status")
printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print("FAIL  status 读面解析失败: %s" % exc); raise SystemExit(1)
reason = d.get("share_url_reason")
if reason:
    print("FAIL  share_url_reason=%r share_url_source=%r"
          % (reason, d.get("share_url_source")))
    raise SystemExit(1)
print("PASS  share_url 已配置 source=%r" % d.get("share_url_source"))
' || fail=1

echo "=== 3. display-session 是否配好（认证失效后能不能打开登录窗口） ==="
# 裸回环 curl 不带 display session cookie / X-Admin-Control / 控制台会话，
# 按设计就是 available=false + reason=display_authorization_required
# （usagesrv.py:3347-3377 的文档串明说）。这里断言的是「代理已启用且路由可用」，
# 即原因只能是这两者之一；display_proxy_unavailable / display_disabled / 配置错误
# 才是真症状。
out=$(run "curl -s -m 10 $BASE/api/console/share-management/display-session")
printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print("FAIL  display-session 解析失败: %s" % exc); raise SystemExit(1)
reason = d.get("reason") or ""
available = d.get("available")
if available is True:
    print("PASS  display-session 可用（本请求已带身份）")
elif reason == "display_authorization_required" \
        and d.get("url") == "/display/":
    print("PASS  display-session 已配置，裸请求按设计要求身份（reason=%r url=%r）"
          % (reason, d.get("url")))
else:
    print("FAIL  云端 display-session available=%r reason=%r url=%r"
          % (available, reason, d.get("url")))
    raise SystemExit(1)
' || fail=1

echo
if [ "$fail" -ne 0 ]; then
  echo "RED：上面至少一条症状还在"
  exit 1
fi
echo "GREEN：三条都不在了"
