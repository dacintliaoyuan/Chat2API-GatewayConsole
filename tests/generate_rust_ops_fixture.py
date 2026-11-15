# -*- coding: utf-8 -*-
"""Generate the Rust S8 ops fixture from the real Python tools.

The Rust reader must match the Python ledger reader, Cloudflare checker, public
DNS/TLS state machine and deployment assembly check, so every expected value is
produced by calling the Python implementations.  The generator only uses temporary
directories and injected DNS/TLS facts; it never reads or writes a production
ledger, never contacts the public network and never runs a backup, restore, update
or rollback.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_ops_rev1.json"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "um-cloud-deploy"))

import share.metering as metering  # noqa: E402
from console import public_probe as probe  # noqa: E402

NOW = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc).timestamp()
CHECKED_AT = "2026-09-25T13:00:00+00:00"


def record(**fields):
    rec = {
        "time": "2026-09-25T10:00:00+08:00",
        "service": "chat2api",
        "model": "GLM-5.2-NVFP4",
        "stream": False,
        "status": "ok",
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 110,
        "estimated": False,
        "duration_ms": 1000,
        "usage_source": "upstream",
        "protocol": "chat",
        "key_id": "key-a",
        "request_id": "r1",
        "attribution": "verified",
    }
    rec.update(fields)
    return rec


def write_ledger(directory, name, records, *, raw_lines=()):
    path = Path(directory) / name
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
    if text:
        text += "\n"
    for line in raw_lines:
        text += line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def normalize(value):
    if isinstance(value, dict):
        return {
            key: "<checkedAt>" if key == "checkedAt" else normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def strip_timing(value):
    if isinstance(value, dict):
        return {
            key: strip_timing(item)
            for key, item in value.items()
            if key not in ("checked_at_utc", "checked_at_epoch", "elapsed_ms")
        }
    if isinstance(value, list):
        return [strip_timing(item) for item in value]
    return value


def error_result(exc):
    return {"error": type(exc).__name__, "message": str(exc)}


def capture_error(callback):
    try:
        callback()
    except ValueError as exc:
        return exc
    raise AssertionError("expected ValueError")


def ledger_fixture():
    tmp = tempfile.TemporaryDirectory(prefix="um-rust-ops-ledger-")
    root = Path(tmp.name)
    try:
        basic_records = [
            record(protocol="chat", request_id="r1"),
            record(protocol="messages", request_id="r2", stream=True,
                   input_tokens=200, output_tokens=20,
                   cached_input_tokens=20, total_tokens=220,
                   cache_report_status="reported"),
            record(usage_source="none", input_tokens=0, output_tokens=0,
                   total_tokens=0, request_id="unknown-usage"),
            record(key_id=None, attribution="absent", request_id="direct"),
            record(key_id="", attribution="replayed", request_id="replayed"),
            {
                "time": "2026-09-24T10:00:00+08:00",
                "service": "chat2api",
                "model": "GLM-5.2-NVFP4",
                "stream": False,
                "status": "ok",
                "input_tokens": 1000,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 1000,
                "estimated": True,
                "duration_ms": 100,
            },
            record(status="client_disconnected", usage_source="partial",
                   output_tokens=0, request_id="cut"),
            record(status="upstream_read_error", usage_source="estimated",
                   request_id="failed"),
            record(time="not-a-timestamp", request_id="undated"),
            record(cache_report_status="unreported", cached_input_tokens=0,
                   request_id="unreported"),
            record(cache_report_status="reported", cached_input_tokens=0,
                   request_id="explicit-zero"),
            record(cache_report_status="reported", cached_input_tokens=20,
                   request_id="reported-hit"),
        ]
        ledger = write_ledger(
            root, "usage-share-backend.jsonl", basic_records,
            raw_lines=["{not json", ""])
        fixture = {
            "inputs": {
                "basicRecords": basic_records,
                "truncatedRecords": 200001,
                "privateRecords": [record()],
                "invalidRecords": [record()],
            },
            "basic": normalize(metering.read_share_usage(str(ledger), now=NOW)),
            "limit2": normalize(metering.read_share_usage(
                str(ledger), limit=2, now=NOW)),
            "limit0": normalize(metering.read_share_usage(
                str(ledger), limit=0, now=NOW)),
            "filtered": normalize(metering.read_share_usage(
                str(ledger), window="24h", key_id="key-a",
                model="GLM-5.2-NVFP4", result="success", now=NOW)),
            "today": normalize(metering.read_share_usage(
                str(ledger), window="today", now=NOW)),
            "unattributed": normalize(metering.read_share_usage(
                str(ledger), key_id="unattributed", now=NOW)),
            "results": normalize(metering.read_share_usage(
                str(ledger), result="cancelled", now=NOW)),
            "requests": {
                "basic": normalize(metering.read_share_requests(
                    str(ledger), now=NOW)),
                "limit2": normalize(metering.read_share_requests(
                    str(ledger), limit=2, now=NOW)),
                "cursor": normalize(metering.read_share_requests(
                    str(ledger), limit=2, cursor="6", now=NOW)),
                "filters": normalize(metering.read_share_requests(
                    str(ledger), window="24h", key_id="unattributed",
                    model="GLM-5.2-NVFP4", result="cancelled", now=NOW)),
                "today": normalize(metering.read_share_requests(
                    str(ledger), window="today", now=NOW)),
                "limit200": normalize(metering.read_share_requests(
                    str(ledger), limit=200, now=NOW)),
            },
        }

        missing = root / "missing.jsonl"
        fixture["missing"] = {
            "summary": normalize(metering.read_share_usage(str(missing), now=NOW)),
            "requests": normalize(metering.read_share_requests(
                str(missing), now=NOW)),
        }

        empty = write_ledger(root, "empty.jsonl", [])
        fixture["empty"] = {
            "summary": normalize(metering.read_share_usage(str(empty), now=NOW)),
            "requests": normalize(metering.read_share_requests(
                str(empty), now=NOW)),
        }

        truncated = write_ledger(root, "truncated.jsonl", [{}] * 200001)
        fixture["truncated"] = {
            "summary": normalize(metering.read_share_usage(str(truncated), now=NOW)),
            "requests": normalize(metering.read_share_requests(
                str(truncated), limit=2, now=NOW)),
        }

        private = write_ledger(root, "usage-chat2api.jsonl", [record()])
        fixture["errors"] = {
            "summaryPrivate": error_result(capture_error(
                lambda: metering.read_share_usage(str(private), now=NOW))),
            "requestsPrivate": error_result(capture_error(
                lambda: metering.read_share_requests(str(private), now=NOW))),
        }

        invalid = {}
        for name, kwargs, reader in (
            ("summaryWindow", {"window": "2d"}, "summary"),
            ("summaryLimitNegative", {"limit": -1}, "summary"),
            ("summaryKey", {"key_id": "k" * 129}, "summary"),
            ("summaryModel", {"model": "m" * 201}, "summary"),
            ("summaryResult", {"result": "ok"}, "summary"),
            ("requestsWindow", {"window": "2d"}, "requests"),
            ("requestsLimitZero", {"limit": 0}, "requests"),
            ("requestsLimitLarge", {"limit": 201}, "requests"),
            ("requestsKey", {"key_id": "k" * 129}, "requests"),
            ("requestsModel", {"model": "m" * 201}, "requests"),
            ("requestsResult", {"result": "ok"}, "requests"),
            ("requestsCursor", {"cursor": "not-a-cursor"}, "requests"),
            ("requestsCursorNegative", {"cursor": "-1"}, "requests"),
        ):
            function = (metering.read_share_usage if reader == "summary"
                        else metering.read_share_requests)
            invalid[name] = error_result(capture_error(
                lambda function=function, kwargs=kwargs: function(
                    str(ledger), now=NOW, **kwargs)))
        fixture["invalid"] = invalid
        return fixture
    finally:
        tmp.cleanup()


def load_ingress_checker():
    spec = importlib.util.spec_from_file_location(
        "rust_ops_check_ingress",
        ROOT / "um-cloud-deploy" / "cloudflared" / "check-ingress.py")
    checker = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(checker)
    return checker


def cloud_fixture():
    checker = load_ingress_checker()
    fixture = {
        "ingress": {},
        "publicHost": {},
        "dnsAggregate": {},
        "dnsAnswers": {},
        "negativeTtl": {},
        "summarize": {},
        "exitCodes": {},
        "deadline": {},
        "check": {},
    }

    def config(rules):
        return "ingress:\n" + "\n".join(rules) + "\n"

    def api_rule(path, host="fixture.example", service="http://127.0.0.1:8100"):
        return (f"  - hostname: {host}\n"
                f"    path: ^{path}$\n"
                f"    service: {service}\n")

    example = (ROOT / "um-cloud-deploy" / "config" /
               "cloudflared.yml.example").read_text(encoding="utf-8")
    ingress_cases = {
        "example": (example, "dacintapi.de5.net"),
        "missingPath": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "duplicatePath": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            api_rule("/v1/models"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "wrongHost": (config([
            api_rule("/v1/models", host="other.example"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "catchAllMissing": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
        ]), "fixture.example"),
        "catchAllWithHost": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - hostname: fixture.example\n    service: http_status:404\n",
        ]), "fixture.example"),
        "multipleCatchAll": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - service: http_status:404\n",
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "wrongService": (config([
            api_rule("/v1/models", service="http://127.0.0.1:8101"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "forbiddenPort": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            "  - hostname: fixture.example\n    path: ^/console$\n"
            "    service: http://127.0.0.1:8096\n",
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "notSharedPath": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            api_rule("/v1/embeddings"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "noService": (config([
            api_rule("/v1/models"),
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - hostname: fixture.example\n    path: ^/v1/extra$\n",
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "nonMappingRule": (config([
            api_rule("/v1/models"),
            "  - not-a-mapping\n",
            api_rule("/v1/chat/completions"),
            api_rule("/v1/messages"),
            api_rule("/v1/responses"),
            "  - service: http_status:404\n",
        ]), "fixture.example"),
        "missingIngress": ("hostname: fixture.example\n", "fixture.example"),
        "emptyIngress": ("ingress: []\n", "fixture.example"),
        "rootNotMapping": ("- ingress\n", "fixture.example"),
    }
    ingress_root = Path(tempfile.mkdtemp(prefix="um-rust-ops-ingress-"))
    for name, (text, host) in ingress_cases.items():
        path = ingress_root / f"{name}.yml"
        path.write_text(text, encoding="utf-8")
        fixture["ingress"][name] = checker.check_ingress(path, expect_host=host)
    invalid_yaml = ingress_root / "invalid.yml"
    invalid_yaml.write_text("ingress: [\n", encoding="utf-8")
    result = checker.check_ingress(invalid_yaml, expect_host="fixture.example")
    fixture["ingress"]["invalidYaml"] = {
        "ok": result["ok"],
        "rules": result["rules"],
        "problemPrefix": "invalid YAML:",
    }
    missing_path = ingress_root / "absent.yml"
    result = checker.check_ingress(missing_path, expect_host="fixture.example")
    fixture["ingress"]["unreadable"] = {
        "ok": result["ok"],
        "rules": result["rules"],
        "problemPrefix": "config unreadable:",
    }

    accepted = (
        "https://fixture.tail-test.ts.net",
        "https://fixture.tail-test.ts.net/",
        "https://fixture.tail-test.ts.net:443",
    )
    rejected = (
        None, "", "http://fixture.tail-test.ts.net",
        "https://127.0.0.1", "https://localhost", "https://[::1]",
        "https://example.com", "https://ts.net",
        "https://fixture.tail-test.ts.net.evil.invalid",
        "https://user@fixture.tail-test.ts.net",
        "https://fixture.tail-test.ts.net/v1",
        "https://fixture.tail-test.ts.net?x=1",
        "https://fixture.tail-test.ts.net#x",
        "https://fixture.tail-test.ts.net:80",
        "https://fixture.tail-test.ts.net:444",
        "https://-x.ts.net", "https://x-.ts.net", "https://x..ts.net",
        "https://x_.ts.net", "https://fixture\\.tail-test.ts.net",
        "https://测试.ts.net",
        "https://" + "x" * 64 + ".ts.net",
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CONSOLE_PUBLIC_ORIGIN", None)
        fixture["publicHost"]["unconfigured"] = {
            "accepted": {url: probe.public_host(url) for url in accepted},
            "rejected": {repr(url): probe.public_host(url) for url in rejected},
            "sources": {
                url: probe.public_source(
                    url.split("//", 1)[1].split(":", 1)[0])
                for url in accepted
            },
        }
    with mock.patch.dict(
            os.environ,
            {"CONSOLE_PUBLIC_ORIGIN": "https://fixture.tail-test.ts.net"},
            clear=False):
        fixture["publicHost"]["configured"] = {
            "publicOrigin": probe.public_origin(),
            "host": probe.public_host("https://fixture.tail-test.ts.net"),
            "source": probe.public_source("fixture.tail-test.ts.net"),
            "otherHost": probe.public_host("https://other.tail-test.ts.net"),
        }
    with mock.patch.dict(
            os.environ,
            {"CONSOLE_PUBLIC_ORIGIN": "http://fixture.tail-test.ts.net"},
            clear=False):
        fixture["publicHost"]["invalidConfigured"] = {
            "publicOrigin": probe.public_origin(),
            "host": probe.public_host("https://fixture.tail-test.ts.net"),
        }
    fixture["publicHost"]["unconfiguredOrigin"] = probe.public_origin({})

    cases = {
        "bothOk": [
            dns_source("cloudflare", "ok", addresses=["203.0.113.10"], ttl=60),
            dns_source("nextdns", "ok", addresses=["203.0.113.11"], ttl=30),
        ],
        "okAndEmpty": [
            dns_source("cloudflare", "ok", addresses=["203.0.113.10"]),
            dns_source("nextdns", "empty"),
        ],
        "okAndNxdomain": [
            dns_source("cloudflare", "ok", addresses=["203.0.113.10"]),
            dns_source("nextdns", "nxdomain", negative_ttl=120),
        ],
        "okAndError": [
            dns_source("cloudflare", "ok", addresses=["203.0.113.10"]),
            dns_source("nextdns", "error"),
        ],
        "bothNxdomain": [
            dns_source("cloudflare", "nxdomain", negative_ttl=60),
            dns_source("nextdns", "nxdomain", negative_ttl=120),
        ],
        "bothEmpty": [
            dns_source("cloudflare", "empty"),
            dns_source("nextdns", "empty"),
        ],
        "bothTimeout": [
            dns_source("cloudflare", "timeout"),
            dns_source("nextdns", "timeout"),
        ],
        "bothError": [
            dns_source("cloudflare", "error"),
            dns_source("nextdns", "error"),
        ],
        "notChecked": [
            dns_source("cloudflare", "not_checked"),
            dns_source("nextdns", "not_checked"),
        ],
        "truncated": [
            dns_source("cloudflare", "ok", addresses=["203.0.113.10"],
                        ttl=60, truncated=True),
            dns_source("nextdns", "ok", addresses=["203.0.113.11"],
                        ttl=30),
        ],
    }
    for name, sources in cases.items():
        fixture["dnsAggregate"][name] = strip_timing(
            normalize(probe._aggregate_dns(sources, 0.0)))

    answers = {
        "aAndAaaa": {
            "Answer": [
                {"type": 1, "data": "203.0.113.10", "TTL": 60},
                {"type": 1, "data": "203.0.113.10", "TTL": 30},
                {"type": 1, "data": "203.0.113.11", "TTL": 120},
                {"type": 28, "data": "2001:db8::1", "TTL": 60},
                {"type": 1, "data": "127.0.0.1", "TTL": 60},
                {"type": 1, "data": "not-an-address", "TTL": 60},
            ],
        },
        "empty": {"Answer": []},
        "noAnswer": {},
        "truncated": {
            "Answer": [
                {"type": 1, "data": f"198.51.100.{index}", "TTL": 60}
                for index in range(1, 8)
            ],
        },
        "badTtl": {
            "Answer": [
                {"type": 1, "data": "203.0.113.10", "TTL": True},
                {"type": 1, "data": "203.0.113.11", "TTL": -1},
                {"type": 1, "data": "203.0.113.12", "TTL": 300},
            ],
        },
    }
    for name, data in answers.items():
        addresses, ttl, truncated = probe._addresses_from_answers(data)
        fixture["dnsAnswers"][name] = {
            "addresses": addresses,
            "ttl": ttl,
            "truncated": truncated,
        }
    fixture["negativeTtl"] = {
        "soa": probe._negative_ttl({
            "Authority": [
                {"type": 6, "TTL": 60, "data": "ns 1 2 3 4 5 120"},
            ],
        }),
        "minimum": probe._negative_ttl({
            "Authority": [
                {"type": 6, "TTL": 120, "data": "ns 1 2 3 4 5 60"},
            ],
        }),
        "missing": probe._negative_ttl({"Authority": []}),
        "bad": probe._negative_ttl({
            "Authority": [
                {"type": 6, "TTL": -1, "data": "ns 1 2 3 4 5 -1"},
            ],
        }),
    }

    for name, entries in {
        "allOk": [entry("ok"), entry("ok")],
        "allOkUnchecked": [entry("ok"), unchecked()],
        "allFailed": [entry("failed"), entry("failed")],
        "allFailedUnchecked": [entry("failed"), unchecked()],
        "mixed": [entry("ok"), entry("failed")],
        "mixedUnchecked": [entry("ok"), entry("failed"), unchecked()],
        "noneChecked": [unchecked(), unchecked()],
    }.items():
        fixture["summarize"][name] = summarize_case(entries)

    fixture["exitCodes"] = {
        "ok": probe._check_exit_code("ok"),
        "notChecked": probe._check_exit_code("not_checked"),
        "failed": probe._check_exit_code("all_failed"),
        "degraded": probe._check_exit_code("degraded"),
    }
    fixture["deadline"] = {
        "default": probe._bounded_check_deadline(None),
        "valid": probe._bounded_check_deadline(30),
        "tooSmall": probe._bounded_check_deadline(0.1),
        "tooLarge": probe._bounded_check_deadline(61),
        "nan": probe._bounded_check_deadline(float("nan")),
    }

    fixture["check"] = {
        "invalidUrl": normalize_check(probe.check_public_connection(
            None, deadline_s=15)),
        "dnsTimeout": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            dns_timeout=dns_detail("timeout", "dns_timeout"))),
        "dnsEmpty": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                [], "dns_empty", dns_detail("empty", "dns_empty")))),
        "allOk": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10", "203.0.113.11"], "ok",
                dns_detail("ok", "ok",
                            ["203.0.113.10", "203.0.113.11"])))),
        "allFailed": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10"], "ok",
                dns_detail("ok", "ok", ["203.0.113.10"])),
            tls=[("failed", "tls_certificate_invalid")])),
        "mixed": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10", "203.0.113.11"], "ok",
                dns_detail("ok", "ok",
                            ["203.0.113.10", "203.0.113.11"])),
            tls=[("ok", "ok"), ("failed", "timeout")])),
        "unchecked": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10"], "ok",
                dns_detail("ok", "ok", ["203.0.113.10"])),
            tls=[("unchecked", "not_checked")])),
        "dnsDisagreement": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10"], "dns_disagreement",
                dns_detail("disagreement", "dns_disagreement",
                            ["203.0.113.10"])))),
        "dnsUnconfirmed": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10"], "dns_unconfirmed",
                dns_detail("unconfirmed", "dns_unconfirmed",
                            ["203.0.113.10"])))),
        "dnsTruncated": normalize_check(with_dns(
            probe.check_public_connection, "https://fixture.tail-test.ts.net",
            resolution=probe.PublicDnsResolution(
                ["203.0.113.10", "203.0.113.11"], "ok",
                dns_detail("ok", "ok",
                            ["203.0.113.10", "203.0.113.11"],
                            truncated=True)),
            tls=[("ok", "ok"), ("ok", "ok")])),
    }
    return fixture


def dns_source(source, state, *, addresses=(), ttl=None, negative_ttl=None,
                status=None, classification=None, truncated=False):
    return {
        "source": source,
        "state": state,
        "status": status,
        "classification": classification or state,
        "addresses": list(addresses),
        "addresses_truncated": truncated,
        "ttl_s": ttl,
        "negative_ttl_s": negative_ttl,
        "checked_at_epoch": NOW,
        "elapsed_ms": 1,
    }


def dns_detail(state, reason, addresses=(), *, sources=(),
                truncated=False, ttl=None, negative_ttl=None):
    return {
        "version": 1,
        "checked_at_utc": CHECKED_AT,
        "checked_at_epoch": NOW,
        "state": state,
        "reason": reason,
        "addresses": list(addresses),
        "addresses_truncated": truncated,
        "ttl_s": ttl,
        "negative_ttl_s": negative_ttl,
        "elapsed_ms": 0,
        "sources": list(sources),
    }


def with_dns(callback, url, *, resolution=None, dns_timeout=None, tls=None):
    def resolve(host, deadline):
        if dns_timeout is not None:
            raise probe.PublicDnsTimeout(dns_timeout)
        return resolution

    tls = tls or [("ok", "ok")]
    index = {"value": 0}

    def check(host, address, deadline):
        state, reason = tls[min(index["value"], len(tls) - 1)]
        index["value"] += 1
        return {
            "host": host,
            "ip": address,
            "state": state,
            "dns": "ok",
            "tls": "ok" if state == "ok" else (
                "not_checked" if state == "unchecked" else "failed"),
            "certificate": "ok" if state == "ok" else (
                "not_checked" if state == "unchecked" else "failed"),
            "reason": reason,
            "elapsed_ms": 0,
            "checked_at_epoch": None if state == "unchecked" else NOW,
        }

    with mock.patch.object(probe, "resolve_public_addresses", resolve), \
            mock.patch.object(probe, "_check_entry", check):
        return callback(url, deadline_s=15)


def normalize_check(result):
    return strip_timing(normalize(result))


def entry(state, *, reason=None):
    return {
        "host": "fixture.tail-test.ts.net",
        "ip": "203.0.113.10",
        "state": state,
        "dns": "ok",
        "tls": "ok" if state == "ok" else (
            "not_checked" if state == "unchecked" else "failed"),
        "certificate": "ok" if state == "ok" else (
            "not_checked" if state == "unchecked" else "failed"),
        "reason": reason or ("ok" if state == "ok" else "connection_failed"),
        "elapsed_ms": 0,
        "checked_at_epoch": None if state == "unchecked" else NOW,
    }


def unchecked():
    return {
        "host": "fixture.tail-test.ts.net",
        "ip": "203.0.113.10",
        "state": "unchecked",
        "dns": "not_checked",
        "tls": "not_checked",
        "certificate": "not_checked",
        "reason": "not_checked",
        "elapsed_ms": 0,
        "checked_at_epoch": None,
    }


def summarize_case(entries):
    result = {
        "version": 1,
        "checked_at_utc": CHECKED_AT,
        "checked_at_epoch": NOW,
        "public_url": "https://fixture.tail-test.ts.net",
        "host": "fixture.tail-test.ts.net",
        "source": "tailscale",
        "overall": "not_checked",
        "reason": "ok",
        "deadline_s": 15.0,
        "elapsed_ms": 0,
        "entries": entries,
        "dns": None,
    }
    probe._summarize_check(result)
    return strip_timing(result)


def deploy_fixture():
    import backup_restore

    unit = (ROOT / "um-cloud-deploy" / "systemd" /
            "um-share-console.service").read_text(encoding="utf-8")
    env = (ROOT / "um-cloud-deploy" / "config" /
           "console.env.example").read_text(encoding="utf-8")
    broker_unit = (ROOT / "um-cloud-deploy" / "systemd" /
                   "um-share-broker.service").read_text(encoding="utf-8")
    broker_env = (ROOT / "um-cloud-deploy" / "config" /
                  "broker.env.example").read_text(encoding="utf-8")
    with_uid = env + "\nUM_SHARE_UID=1000\n"
    broker_with_uid = broker_env + "\nUM_SHARE_UID=1000\n"

    tmp = tempfile.TemporaryDirectory(prefix="um-rust-ops-deploy-")
    root = Path(tmp.name)
    try:
        unit_path = root / "console.service"
        env_path = root / "console.env"
        missing_unit = root / "missing.service"
        unit_path.write_text(unit, encoding="utf-8")
        env_path.write_text(with_uid, encoding="utf-8")
        cli = ROOT / "um-cloud-deploy" / "backup_restore.py"
        ok = subprocess.run(
            [sys.executable, str(cli), "--check-deployment-assembly",
             "console", str(unit_path), str(env_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        missing = subprocess.run(
            [sys.executable, str(cli), "--check-deployment-assembly",
             "console", str(missing_unit), str(env_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        usage = subprocess.run(
            [sys.executable, str(cli), "--check-deployment-assembly",
             "console", str(unit_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        fixture = {
            "assembly": {
                "consoleOk": backup_restore.deployment_assembly_problems(
                    "console", unit, with_uid),
                "brokerOk": backup_restore.deployment_assembly_problems(
                    "broker", broker_unit, broker_with_uid),
                "consoleMissing": backup_restore.deployment_assembly_problems(
                    "console", "", ""),
                "brokerMissing": backup_restore.deployment_assembly_problems(
                    "broker", "", ""),
                "unknownRole": backup_restore.deployment_assembly_problems(
                    "unknown", unit, env),
                "unit": unit,
                "env": env,
                "brokerUnit": broker_unit,
                "brokerEnv": broker_env,
            },
            "cli": {
                "ok": {
                    "code": ok.returncode,
                    "body": json.loads(ok.stdout),
                },
                "missing": {
                    "code": missing.returncode,
                    "body": normalize_temp_paths(
                        json.loads(missing.stdout), root),
                },
                "usage": {
                    "code": usage.returncode,
                    "stdout": usage.stdout,
                    "stderr": usage.stderr,
                },
            },
            "updateHelp": script_help("update.sh"),
            "rollbackHelp": script_help("rollback.sh"),
            "updateUsage": script_exit("update.sh", ["--drain-budget", "1"]),
            "rollbackUsage": script_exit("rollback.sh", ["--drain-budget", "1"]),
            "updateUnknown": script_exit("update.sh", ["--unknown"]),
            "rollbackUnknown": script_exit("rollback.sh", ["--unknown"]),
        }
        fixture["cli"]["ok"]["body"]["unit"] = "<unit>"
        fixture["cli"]["ok"]["body"]["env"] = "<env>"
        fixture["cli"]["missing"]["body"]["unit"] = "<unit>"
        fixture["cli"]["missing"]["body"]["env"] = "<env>"
        return fixture
    finally:
        tmp.cleanup()


def normalize_temp_paths(value, root):
    import re

    text = json.dumps(value, ensure_ascii=False)
    text = text.replace(str(root), "<tmp>")
    text = re.sub(r"um-rust-ops-deploy-[A-Za-z0-9_]+",
                  "um-rust-ops-deploy-<tmp>", text)
    return json.loads(text)


def script_help(name):
    completed = subprocess.run(
        ["bash", str(ROOT / "um-cloud-deploy" / name), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return {"code": completed.returncode, "stdout": completed.stdout}


def script_exit(name, args):
    completed = subprocess.run(
        ["bash", str(ROOT / "um-cloud-deploy" / name), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return {"code": completed.returncode}


def generate():
    fixture = {
        "metadata": {
            "fixture": True,
            "source": "tests/generate_rust_ops_fixture.py",
            "generatedFrom": "share/metering.py + console/public_probe.py"
                            " + um-cloud-deploy/cloudflared/check-ingress.py"
                            " + um-cloud-deploy/backup_restore",
        },
        "ledger": ledger_fixture(),
        "cloudflare": cloud_fixture(),
        "deploy": deploy_fixture(),
    }
    return json.loads(json.dumps(fixture, ensure_ascii=False))


def main():
    fixture = generate()
    FIXTURE.write_text(
        json.dumps(fixture, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
