#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate that Tailscale Serve exposes only the private console 8096.

The cloud machine must not use Funnel and must not add public listeners for
80/443/8100/8097.  ``tailscale serve status --json`` is the authoritative
view of Serve handlers.  This module is deliberately pure: it parses the JSON
document and returns human-readable errors; the shell wrapper performs the
``tailscale`` commands.

Expand-contract (2026-11-14): 8096 is the new default console port.  During the
one-release window the wrapper may pass the legacy 18096 explicitly; this module
validates whichever exact proxy it is told to expect.
"""
from __future__ import annotations

import json
import sys

EXPECTED_PROXY = "http://127.0.0.1:8096"
EXPECTED_PORT = "443"


def _funnel_enabled(document):
    if document is None:
        return False
    if not isinstance(document, dict):
        return True
    allow = document.get("AllowFunnel")
    if allow in (None, {}, False):
        return False
    if not isinstance(allow, dict):
        return True
    return any(value is not False for value in allow.values())


def validate_serve_status(document, expected_proxy=EXPECTED_PROXY):
    """Return violations; an empty list means exactly the expected proxy only."""
    errors = []
    if not isinstance(document, dict):
        return ["serve status is not an object"]

    tcp = document.get("TCP")
    if tcp not in (None, {}):
        if not isinstance(tcp, dict):
            errors.append("TCP must be an object")
        else:
            for port, value in tcp.items():
                if str(port) != EXPECTED_PORT:
                    errors.append(f"unexpected TCP port {port}")
                    continue
                if not isinstance(value, dict) or set(value) != {"HTTPS"} \
                        or value.get("HTTPS") is not True:
                    errors.append("TCP 443 must terminate HTTPS only")

    web = document.get("Web")
    if not isinstance(web, dict) or len(web) != 1:
        errors.append("exactly one web handler is required")
        return errors
    host, config = next(iter(web.items()))
    if not isinstance(host, str) or not host.endswith(":" + EXPECTED_PORT):
        errors.append("web handler must be host:443")
    if not isinstance(config, dict):
        errors.append("web handler must be an object")
        return errors
    if set(config) != {"Handlers"}:
        errors.append("web handler must contain only Handlers")
        return errors
    handlers = config.get("Handlers")
    if not isinstance(handlers, dict) or set(handlers) != {"/"}:
        errors.append("only the root handler is allowed")
        return errors
    handler = handlers["/"]
    if not isinstance(handler, dict) or set(handler) != {"Proxy"}:
        errors.append("root handler must be a single proxy")
    elif handler.get("Proxy") != expected_proxy:
        errors.append("root proxy must be " + expected_proxy)

    if _funnel_enabled(document):
        errors.append("Funnel is enabled")
    return errors


def validate_funnel_status(document):
    """Return errors when Funnel is enabled; empty means Funnel is off."""
    return ["Funnel is enabled"] if _funnel_enabled(document) else []


def main(argv):
    if len(argv) != 3:
        print("usage: tailscale_serve_policy.py <serve-json> <funnel-json>",
              file=sys.stderr)
        return 64
    try:
        serve = json.loads(argv[1] or "{}")
        funnel = json.loads(argv[2] or "{}")
    except ValueError as exc:
        print(f"tailscale status is not valid JSON: {exc}", file=sys.stderr)
        return 65
    errors = validate_serve_status(serve) + validate_funnel_status(funnel)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
