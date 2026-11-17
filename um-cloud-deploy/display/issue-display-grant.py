#!/usr/bin/env python3
"""Issue a one-time display grant for the private login window.

Run this on the cloud host as root, or as the console service user with
``sudo -u um-share``, through the authenticated SSH entry.  It reads the
existing admin control token, asks the loopback console for a grant bound to the
loopback origin, and prints only the same-origin launcher URL.  The grant is
single-use and short-lived; the control token is never printed, written to a URL
or stored in the browser.

The returned URL is intended for the PC-side entry, which forwards the loopback
console port over the existing SSH tunnel and opens the URL in a browser.  The URL
fragment is not sent in an HTTP request or a server access log.
"""
import argparse
import http.client
import json
import os
import re
import stat
import sys
from urllib.parse import urlsplit

# The Rust cloud console listens on 8002; the Python rollback unit keeps 8096,
# and the legacy 18096 stays usable as an explicit --console value for one
# release cycle.  The admin control token only goes to these cloud console
# origins, never to the local 8001 console.
DEFAULT_CONSOLE = "http://127.0.0.1:8002"
# 8001 is the LOCAL console and is never a valid destination for the cloud
# admin token.
CONSOLE_PORTS = frozenset({8002, 8096, 18096})
DEFAULT_TOKEN_FILE = "/etc/um-share/admin-control-token"
MAX_TOKEN_BYTES = 1024
SERVICE_USER = "um-share"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _service_user_uid():
    """Return the display/console service uid, or None when unresolvable."""
    try:
        import pwd
    except ImportError:
        return None
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid
    except KeyError:
        return None


def _effective_uid():
    getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if getter is None:
        raise ValueError("control token ownership cannot be checked")
    return getter()


def _token_owner_allowed(owner_uid, effective_uid, service_uid):
    """Root may read root- or service-owned tokens; others only their own."""
    if effective_uid == 0:
        return owner_uid == 0 or (
            service_uid is not None and owner_uid == service_uid)
    return owner_uid == effective_uid


def read_control_token(path):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("control token is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("control token must have mode 0600")
    if not _token_owner_allowed(
            info.st_uid, _effective_uid(), _service_user_uid()):
        raise ValueError(
            "control token owner must be root, the caller, or the "
            "um-share service user")
    with open(path, "rb") as stream:
        raw = stream.read(MAX_TOKEN_BYTES + 1)
    if len(raw) > MAX_TOKEN_BYTES:
        raise ValueError("control token is unexpectedly large")
    token = raw.decode("utf-8").strip()
    if not token:
        raise ValueError("control token is empty")
    return token


def request_grant(console, token, timeout):
    parsed = urlsplit(console)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path not in (
            "", "/") or parsed.username is not None \
            or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("console must be an http://host:port origin")
    port = parsed.port or 80
    origin = f"http://{parsed.hostname}:{port}"
    connection = http.client.HTTPConnection(
        parsed.hostname, port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/console/share-management/display-grant",
            body=b"",
            headers={
                "X-Admin-Control": token,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": origin,
                "Content-Length": "0",
            })
        response = connection.getresponse()
        body = response.read(64 * 1024)
        return response.status, body
    finally:
        connection.close()


def request_console_session(console, token, timeout):
    """Use the existing console-session issuer; never send its token off-host."""
    parsed = urlsplit(console)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS \
            or parsed.path not in ("", "/") or parsed.username is not None \
            or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("console bootstrap requires a loopback origin")
    port = parsed.port or 80
    origin = f"http://{parsed.hostname}:{port}"
    if port not in CONSOLE_PORTS:
        raise ValueError("console bootstrap requires a cloud console port")
    connection = http.client.HTTPConnection(
        parsed.hostname, port, timeout=timeout)
    try:
        connection.request(
            "POST", "/api/session/reissue", body=b"",
            headers={"X-Admin-Control": token,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": origin, "Content-Length": "0"})
        response = connection.getresponse()
        return response.status, response.read(64 * 1024)
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Issue a one-time private display grant")
    parser.add_argument("--console", default=DEFAULT_CONSOLE)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--console-session", action="store_true",
                        help="Bootstrap the existing browser management session")
    args = parser.parse_args(argv)
    try:
        token = read_control_token(args.token_file)
    except (OSError, ValueError):
        print("display-grant: admin control token unavailable",
              file=sys.stderr)
        return 1
    try:
        requester = request_console_session if args.console_session else request_grant
        status, body = requester(args.console, token, args.timeout)
    except (OSError, ValueError):
        print("display-grant: console unreachable", file=sys.stderr)
        return 1
    if status != 200:
        print("display-grant: request refused", file=sys.stderr)
        return 1
    try:
        payload = json.loads(body.decode("utf-8"))
        if args.console_session:
            if not isinstance(payload, dict):
                raise ValueError("invalid console bootstrap response")
            code = payload.get("code")
            if payload.get("ok") is not True or not isinstance(code, str) \
                    or re.fullmatch(r"[A-Za-z0-9_-]{16,256}", code) is None:
                raise ValueError("invalid console bootstrap response")
            print("/display/grant#console=" + code)
            return 0
        grant = payload["grant"]
        url = payload.get("url") or ("/display/grant#grant=" + grant)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        print("display-grant: invalid response", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
