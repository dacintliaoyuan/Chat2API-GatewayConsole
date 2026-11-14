#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""share/admin.py — command line client for the share MANAGEMENT SERVICE.

This CLI never opens the SQLite store directly. It calls the protected
`/api/share/*` routes on usagesrv (127.0.0.1:8001 by default) using the local
management control token, exactly like a browser session would. That keeps the
share store single-authority: there is no second writer from the CLI.

Secrets are printed only on create/rotate responses and the explicit
`secret` readback; they must be saved by the operator immediately. status/list/get
never include secret_hash or plaintext, and the `secret` subcommand puts only the
key id and an optional row version in the URL (never the plaintext itself).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _read_control_token(path):
    try:
        from share.secret_protection import read_private_secret_path
        token = read_private_secret_path(
            path, max_bytes=1024, label="control token").decode(
                "utf-8").strip()
    except (OSError, ValueError, ImportError) as exc:
        raise SystemExit(f"cannot read control token file {path}: {exc}")
    if not token:
        raise SystemExit("empty management control token")
    return token


def _default_token_path():
    return Path(os.environ.get("USAGE_CONTROL_TOKEN_FILE",
                               HERE / "data" / "admin-control-token"))


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="UM 分享管理 CLI（仅通过本机管理服务，唯一写入者 SQLite share_store）")
    parser.add_argument("--base-url", default=os.environ.get(
        "SHARE_MGMT_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--control-token",
                        help="管理令牌；缺省读取 data/admin-control-token")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看 desired/applied 代次、Key 数与最近发布失败")
    sub.add_parser("list", help="列出 Key 元数据（不含摘要/明文）")

    get_p = sub.add_parser("get", help="查看单个 Key 元数据")
    get_p.add_argument("key_id")

    create_p = sub.add_parser("create", help="创建 Key（明文只此一次输出）")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--max-concurrency", type=int, default=16,
                          help="单 Key 并发上限（1..48，默认 16）")
    create_p.add_argument("--idempotency-key")
    patch_p = sub.add_parser("patch", help="原子改名/并发设置（需 expected 版本）")
    patch_p.add_argument("key_id")
    patch_p.add_argument("--name")
    patch_p.add_argument("--max-concurrency", type=int)
    patch_p.add_argument("--expected-version", type=int, required=True)

    rotate_p = sub.add_parser("rotate", help="轮换 Key（新明文只此一次输出）")
    rotate_p.add_argument("key_id")
    rotate_p.add_argument("--idempotency-key")
    rotate_p.add_argument("--expected-version", type=int, required=True)

    revoke_p = sub.add_parser(
        "revoke", help="撤销并中断该 Key 的在途请求（需 expected 版本）")
    revoke_p.add_argument("key_id")
    revoke_p.add_argument("--idempotency-key")
    revoke_p.add_argument("--expected-version", type=int, required=True)

    secret_p = sub.add_parser(
        "secret", help="读取单个 Key 的明文（只此一次响应，不进日志/URL）")
    secret_p.add_argument("key_id")
    secret_p.add_argument("--expected-version", type=int)

    test_p = sub.add_parser(
        "test", help="经公网 API 域名测试该 Key（会消耗少量模型用量）")
    test_p.add_argument("key_id")
    test_p.add_argument("--expected-version", type=int, required=True)

    settings_p = sub.add_parser(
        "settings", help="调整全局分享并发上限（1..64，需 expected 值）")
    settings_p.add_argument("--share-max-concurrency", type=int, required=True)
    settings_p.add_argument("--expected-share-max", type=int)

    limits_p = sub.add_parser(
        "limits", help="查看/归一化存储并发上限（显式管理动作）")
    limits_p.add_argument("--normalize", action="store_true",
                          help="把旧库 >64 全局与 >48 每 Key 调低到 D18-4 上限；"
                               "调低需运行时 ACK，未确认返回 blocked=true")

    delete_p = sub.add_parser("delete", help="删除（tombstone；不可恢复）")
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
MANAGEMENT_PORT = 8001


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
            except ValueError:
                pass
        if response.status >= 400:
            try:
                error = json.loads(data).get("error", data[:200])
            except (ValueError, AttributeError):
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
            body = {"name": args.name,
                    "max_concurrency": args.max_concurrency}
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            _print(_call(base, "POST", "/api/share/keys", token, body))
        elif args.command == "patch":
            body = {"expected_row_version": args.expected_version}
            if args.name is not None:
                body["name"] = args.name
            if args.max_concurrency is not None:
                body["max_concurrency"] = args.max_concurrency
            _print(_call(base, "PATCH", f"/api/share/keys/{args.key_id}",
                         token, body))
        elif args.command == "rotate":
            body = {"expected_row_version": args.expected_version}
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            _print(_call(base, "POST",
                         f"/api/share/keys/{args.key_id}/rotate", token, body))
        elif args.command == "revoke":
            body = {"expected_row_version": args.expected_version}
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            _print(_call(base, "POST",
                         f"/api/share/keys/{args.key_id}/revoke-and-terminate",
                         token, body))
        elif args.command == "secret":
            path = f"/api/share/keys/{args.key_id}/secret"
            if args.expected_version is not None:
                path += f"?expected_row_version={args.expected_version}"
            _print(_call(base, "GET", path, token))
        elif args.command == "test":
            _print(_call(base, "POST",
                         f"/api/share/keys/{args.key_id}/test", token,
                         {"expected_row_version": args.expected_version}))
        elif args.command == "settings":
            body = {"share_max_concurrency": args.share_max_concurrency}
            if args.expected_share_max is not None:
                body["expected_share_max"] = args.expected_share_max
            _print(_call(base, "PATCH", "/api/share/settings", token, body))
        elif args.command == "limits":
            if not args.normalize:
                _print(_call(base, "GET", "/api/share/status", token))
            else:
                _print(_call(base, "POST", "/api/share/limits/normalize",
                              token, {}))
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
