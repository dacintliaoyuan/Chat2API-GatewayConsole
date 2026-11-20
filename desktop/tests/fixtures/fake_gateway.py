#!/usr/bin/env python3
"""Minimal fake gateway for the desktop supervisor test.

It records the received CLI arguments and the login-history environment, answers
`GET /health` with 200, and exits after an authenticated
`POST /admin/shutdown`. It never prints the control token.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def parse_args(argv):
    parsed = {}
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument.startswith("--"):
            key = argument[2:]
            if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                parsed[key] = argv[index + 1]
                index += 2
            else:
                parsed[key] = True
                index += 1
        else:
            index += 1
    return parsed


def main():
    state_dir = os.environ["FAKE_GATEWAY_STATE_DIR"]
    os.makedirs(state_dir, exist_ok=True)
    args = parse_args(sys.argv[1:])
    record = {
        "pid": os.getpid(),
        "args": sys.argv[1:],
        "login_history_env": os.environ.get("CHAT2API_LOGIN_HISTORY_FILE"),
        "control_token_file": args.get("control-token-file"),
    }
    with open(
        os.path.join(state_dir, f"spawn-{os.getpid()}.json"), "w", encoding="utf-8"
    ) as record_file:
        json.dump(record, record_file)

    with open(args["control-token-file"], encoding="utf-8") as token_file:
        expected_token = token_file.read().strip()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(
                {"status": "ok", "chat2api_version": "test", "backend": "fake"}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/admin/shutdown":
                self.send_response(404)
                self.end_headers()
                return
            supplied = self.headers.get("X-Admin-Control", "")
            token_ok = supplied == expected_token
            with open(
                os.path.join(state_dir, "shutdown.json"), "w", encoding="utf-8"
            ) as shutdown_file:
                json.dump({"token_ok": token_ok}, shutdown_file)
            if not token_ok:
                self.send_response(403)
                self.end_headers()
                return
            body = json.dumps({"status": "stopping"}).encode("utf-8")
            self.send_response(202)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", int(args["port"])), Handler)
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
