#!/usr/bin/env python3
"""Read-only validator for the Cloudflare Tunnel ingress file.

D18-1: the public edge routes only the four shared API paths to the loopback
8100 gateway and answers everything else with 404.  This checker never writes,
reloads or restarts a tunnel; it only parses the config and reports
``{ok, problems, rules}``.

Run from ``ai-gateway``:

    python um-cloud-deploy/cloudflared/check-ingress.py \
        --config um-cloud-deploy/config/cloudflared.yml.example \
        --expect-host dacintapi.de5.net
"""
import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - deployment installs PyYAML
    yaml = None
    YAML_ERROR = exc

DEFAULT_HOST = "dacintapi.de5.net"
API_PATHS = ("/v1/models", "/v1/chat/completions", "/v1/messages",
             "/v1/responses")
API_SERVICE = "http://127.0.0.1:8100"
CATCH_ALL = "http_status:404"
FORBIDDEN_PORTS = ("18096", "8110", "5901")


def _normalise_path(path):
    if not isinstance(path, str):
        return None
    text = path.strip()
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$"):
        text = text[:-1]
    return text


def check_ingress(config, *, expect_host=DEFAULT_HOST):
    """Return ``{ok, problems, rules}`` for one tunnel ingress file."""
    problems = []
    rules = []
    try:
        text = Path(config).read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "problems": [f"config unreadable: {exc}"],
                "rules": []}
    if yaml is None:
        return {"ok": False,
                "problems": [f"PyYAML is unavailable: {YAML_ERROR}"],
                "rules": []}
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"ok": False, "problems": [f"invalid YAML: {exc}"],
                "rules": []}
    if not isinstance(payload, dict):
        return {"ok": False, "problems": ["config must be a mapping"],
                "rules": []}
    ingress = payload.get("ingress")
    if not isinstance(ingress, list) or not ingress:
        return {"ok": False,
                "problems": ["ingress must be a non-empty list"], "rules": []}

    seen_paths = set()
    catch_all_index = None
    for index, rule in enumerate(ingress):
        if not isinstance(rule, dict):
            problems.append(f"rule {index} must be a mapping")
            continue
        service = rule.get("service")
        hostname = rule.get("hostname")
        path = rule.get("path")
        rules.append({
            "hostname": hostname if isinstance(hostname, str) else None,
            "path": _normalise_path(path),
            "service": service if isinstance(service, str) else None,
        })
        if not isinstance(service, str) or not service:
            problems.append(f"rule {index} has no service")
            continue
        for port in FORBIDDEN_PORTS:
            if port in service:
                problems.append(
                    f"rule {index} publishes private port {port}: {service}")
        if service == CATCH_ALL:
            if catch_all_index is not None:
                problems.append("more than one catch-all rule")
            catch_all_index = index
            if hostname is not None or path is not None:
                problems.append(
                    "the catch-all rule must not name a hostname or path")
            continue
        if service != API_SERVICE:
            problems.append(
                f"rule {index} service must be {API_SERVICE}: {service}")
            continue
        if not isinstance(hostname, str) or not hostname:
            problems.append(f"rule {index} has no hostname")
        elif expect_host and hostname != expect_host:
            problems.append(
                f"rule {index} hostname is {hostname}, expected {expect_host}")
        normalised = _normalise_path(path)
        if normalised is None:
            problems.append(f"rule {index} has no path")
        elif normalised not in API_PATHS:
            problems.append(
                f"rule {index} path is not a shared API path: {path}")
        elif normalised in seen_paths:
            problems.append(f"duplicate API path: {normalised}")
        else:
            seen_paths.add(normalised)

    if catch_all_index is None:
        problems.append("missing the catch-all http_status:404 rule")
    elif catch_all_index != len(ingress) - 1:
        problems.append("the catch-all rule must be the last ingress rule")
    for path in API_PATHS:
        if path not in seen_paths:
            problems.append(f"missing shared API path: {path}")
    return {"ok": not problems, "problems": problems, "rules": rules}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="read-only Cloudflare Tunnel ingress check")
    parser.add_argument("--config", required=True)
    parser.add_argument("--expect-host", default=DEFAULT_HOST)
    args = parser.parse_args(argv)
    result = check_ingress(args.config, expect_host=args.expect_host)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
