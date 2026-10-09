#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""share_admin.py — command line client for the share MANAGEMENT SERVICE.

This CLI never opens the SQLite store directly. It calls the protected
`/api/share/*` routes on usagesrv (127.0.0.1:8096 by default) using the local
management control token, exactly like a browser session would. That keeps the
share store single-authority: there is no second writer from the CLI.

Secrets are printed only on create/rotate responses and must be saved by the
operator immediately. status/list/get never include secret_hash or plaintext.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _read_control_token(path):
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read control token file {path}: {exc}")
    if not token:
        raise SystemExit("empty management control token")
    return token


def _default_token_path():
    return Path(os.environ.get("USAGE_CONTROL_TOKEN_FILE",
                               HERE / "data" / "admin-control-token"))


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="上游分享管理 CLI（仅通过本机管理服务，唯一写入者 SQLite share_store）")
    parser.add_argument("--base-url", default=os.environ.get(
        "SHARE_MGMT_BASE_URL", "http://127.0.0.1:8096"))
    parser.add_argument("--control-token",
                        help="管理令牌；缺省读取 data/admin-control-token")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看 desired/applied 代次、Key 数与最近发布失败")
    sub.add_parser("list", help="列出 Key 元数据（不含摘要/明文）")

    get_p = sub.add_parser("get", help="查看单个 Key 元数据")
    get_p.add_argument("key_id")

    create_p = sub.add_parser("create", help="创建 Key（明文只此一次输出）")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--idempotency-key")

    patch_p = sub.add_parser("patch", help="原子改名/禁用/启用（需 expected 版本）")
    patch_p.add_argument("key_id")
    patch_p.add_argument("--name")
    patch_p.add_argument("--enable", dest="enabled", action="store_true")
    patch_p.add_argument("--disable", dest="enabled", action="store_false")
    patch_p.set_defaults(enabled=None)
    patch_p.add_argument("--expected-version", type=int, required=True)

    rotate_p = sub.add_parser("rotate", help="轮换 Key（新明文只此一次输出）")
    rotate_p.add_argument("key_id")
    rotate_p.add_argument("--idempotency-key")
    rotate_p.add_argument("--expected-version", type=int, required=True)

    delete_p = sub.add_parser("delete", help="删除（tombstone；不可恢复启用）")
    delete_p.add_argument("key_id")
    delete_p.add_argument("--expected-version", type=int, required=True)

    sub.add_parser("start", help="开启分享（需真实 broker；默认 noop 会失败）")
    sub.add_parser("stop", help="正常关闭分享（排空）")
    sub.add_parser("emergency-stop", help="紧急停止分享")

    op_p = sub.add_parser("operation", help="查询操作状态（202 后跟踪）")
    op_p.add_argument("operation_id")
    op_p.add_argument("--wait", action="store_true",
                      help="有界等待至终态（completed/failed/blocked）")
    op_p.add_argument("--wait-seconds", type=int, default=30,
                      help="等待上限（默认 30 秒）")

    return parser.parse_args(argv)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


MANAGEMENT_HOST = "127.0.0.1"
MANAGEMENT_PORT = 8096


def _validate_management_endpoint(base_url):
    """Return (host, port) for the LOCAL management service only.

    Runs BEFORE any credential is read or sent: a mistyped or hostile
    --base-url / SHARE_MGMT_BASE_URL must never become a management-token
    exfiltration path (R4-b). Only the literal loopback host on the fixed
    management port is accepted; userinfo, paths, queries, fragments, DNS
    names (rebinding) and other ports are rejected. Tests may inject an
    isolated port via SHARE_MGMT_ALLOW_PORT (test-only; never set in
    production).
    """
    if not isinstance(base_url, str) or not base_url.startswith("http://"):
        raise SystemExit(
            "management endpoint must be http://127.0.0.1:<port> (loopback only)")
    rest = base_url[len("http://"):]
    for banned in ("/", "?", "#", "@"):
        if banned in rest:
            raise SystemExit(
                f"management endpoint must not contain '{banned}'")
    host, sep, port_text = rest.partition(":")
    if not sep or not port_text.isdigit():
        raise SystemExit("management endpoint must include an explicit port")
    if host != MANAGEMENT_HOST:
        raise SystemExit(
            f"management endpoint host must be {MANAGEMENT_HOST}, got '{host}'")
    allowed_port = MANAGEMENT_PORT
    override = os.environ.get("SHARE_MGMT_ALLOW_PORT")
    if override:
        if not override.isdigit():
            raise SystemExit("SHARE_MGMT_ALLOW_PORT must be an integer (test-only)")
        allowed_port = int(override)
    port = int(port_text)
    if port != allowed_port:
        raise SystemExit(
            f"management endpoint port must be {allowed_port}, got {port}")
    return host, port


def _call(base_url, method, path, token, body=None):
    host, port = _validate_management_endpoint(base_url)
    conn = http.client.HTTPConnection(host, port, timeout=10)
    headers = {"X-Admin-Control": token, "Host": f"{host}:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        data = "{}"
        if raw:
            try:
                data = raw.decode("utf-8")
                json.loads(data)
            except Exception:
                pass
        if response.status >= 400:
            try:
                error = json.loads(data).get("error", data[:200])
            except Exception:
                error = data[:200]
            raise ApiError(response.status, error)
        return json.loads(data)
    finally:
        conn.close()


def main(argv=None) -> int:
    args = _parse_args(argv)
    # R4-b: validate the endpoint BEFORE reading or sending any credential.
    host, port = _validate_management_endpoint(args.base_url)
    base = f"http://{host}:{port}"
    token = args.control_token or _read_control_token(_default_token_path())
    try:
        if args.command == "status":
            _print(_call(base, "GET", "/api/share/status", token))
        elif args.command == "list":
            _print(_call(base, "GET", "/api/share/keys", token))
        elif args.command == "get":
            _print(_call(base, "GET", f"/api/share/keys/{args.key_id}", token))
        elif args.command == "create":
            body = {"name": args.name}
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            _print(_call(base, "POST", "/api/share/keys", token, body))
        elif args.command == "patch":
            body = {"expected_row_version": args.expected_version}
            if args.name is not None:
                body["name"] = args.name
            if args.enabled is not None:
                body["enabled"] = args.enabled
            _print(_call(base, "PATCH", f"/api/share/keys/{args.key_id}",
                         token, body))
        elif args.command == "rotate":
            body = {"expected_row_version": args.expected_version}
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            _print(_call(base, "POST",
                         f"/api/share/keys/{args.key_id}/rotate", token, body))
        elif args.command == "delete":
            _print(_call(base, "DELETE", f"/api/share/keys/{args.key_id}",
                         token, {"expected_row_version": args.expected_version}))
        elif args.command == "start":
            _print(_call(base, "POST", "/api/share/start", token))
        elif args.command == "stop":
            _print(_call(base, "POST", "/api/share/stop", token))
        elif args.command == "emergency-stop":
            _print(_call(base, "POST", "/api/share/emergency-stop", token))
        elif args.command == "operation":
            _print(_operation(base, token, args.operation_id,
                              wait=args.wait, wait_seconds=args.wait_seconds))
    except ApiError as exc:
        print(f"ERROR {exc.status}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


_TERMINAL_STATES = ("completed", "failed", "blocked", "applied")


def _operation(base, token, operation_id, wait=False, wait_seconds=30):
    """Query one operation; with --wait, poll bounded until a terminal
    state (completed/failed/blocked/applied). Output distinguishes
    saved/pending/running/applied/blocked — never a fabricated success."""
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        op = _call(base, "GET", f"/api/share/operations/{operation_id}", token)
        status = op.get("status")
        if not wait or status in _TERMINAL_STATES:
            return op
        if time.monotonic() >= deadline:
            op = dict(op)
            op["wait"] = "timed_out"
            return op
        time.sleep(0.25)


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
