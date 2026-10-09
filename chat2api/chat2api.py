#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Upstream: https://github.com/Sozbo-Tang/openwebui-chat2api
# Imported verbatim from base commit 58e0633c7f (2026-09-01) on 2026-09-02,
# heavily diverged since (Windows/Edge, auth state machine, admin API,
# usage ledger, streaming rewrite). Upstream HEAD as of 2026-09-10:
# 3a1927e569 (code 32df4a5f1e) - its stale-token and stream-crash fixes were
# re-implemented here independently. Re-sync manually via diff; never
# overwrite this file with upstream content.
"""
openwebui-chat2api — expose any Open WebUI instance as a local OpenAI-compatible API.

How it works:
  1. Open WebUI keeps its session token in `localStorage` on the site's origin.
  2. On first run this script opens a visible Edge window (Playwright, dedicated
     profile) where you sign in once (SSO/username/password). The token is saved
     to `token.json` and reused afterwards — no browser needed for later runs.
  3. A local HTTP server forwards OpenAI-style requests to the instance's native
     `/api/chat/completions` endpoint. Streaming (SSE) and non-streaming are
     both supported, as are Open WebUI API keys (long-lived, preferred over the
     expiring JWT).

Usage:
  python3 chat2api.py --login               # first run: sign in via Chrome
  python3 chat2api.py --list-models         # list available models
  python3 chat2api.py                       # start the API server (127.0.0.1:8000)

  # Or provide a token directly (browser DevTools -> Application -> Local Storage)
  python3 chat2api.py --token "eyJ..." --no-browser

OpenAI/Anthropic-compatible endpoints:
  GET  /v1/models             list models
  POST /v1/chat/completions   chat completions (stream=true returns SSE)
  POST /v1/embeddings         OpenAI-compatible embeddings
  POST /v1/messages           Anthropic Messages API (streaming supported)
  GET  /v1/version            version info
  GET  /ready                 upstream/auth/model-catalog readiness probe

Thinking-level variants:
  For reasoning models served behind vLLM that honour the OpenAI
  `reasoning_effort` parameter, you can register "virtual models" that bake a
  fixed thinking level into the request, e.g.:

    python3 chat2api.py --effort-model GLM-5.2-NVFP4

  This exposes GLM-5.2-NVFP4-Fast / -Low / -Medium / -High alongside the base
  model, so any OpenAI client can pick a thinking level by picking a model.
"""

import argparse
import base64
import email.utils
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import queue
import random
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import requests

# Default Open WebUI endpoint. Override with --base-url.
BASE_URL = "http://localhost:3000"

CHAT2API_VERSION = "1.0.0"
# Reviewed baseline revision. The runtime build id is detected from git when
# possible so logs show the code that is actually running, while this field
# records the source revision this reviewed copy was based on.
CHAT2API_SOURCE_REV = "e2d60349"
RUNTIME_BUILD_ID = os.environ.get("CHAT2API_BUILD", "").strip() or \
    f"{CHAT2API_SOURCE_REV}+reviewed"

HERE = os.path.dirname(os.path.abspath(__file__))
# Environment overrides let an ISOLATED instance (the dedicated share
# backend, Revision 6 R6-A1) run with its own state files; when the env
# vars are absent — the production start path — the HERE-derived defaults
# are unchanged, so the running production instance keeps its behavior.
TOKEN_FILE = os.environ.get("CHAT2API_TOKEN_FILE") or \
    os.path.join(HERE, "token.json")          # NEVER commit this file
PROFILE_DIR = os.path.join(HERE, ".edge-profile")  # dedicated Edge login state
SERVICE_LOG = os.environ.get("CHAT2API_SERVICE_LOG") or \
    os.path.join(HERE, "chat2api.log")

# Per-request usage ledger for the ai-gateway cost aggregator
# (default ../data/usage-chat2api.jsonl, relative to this file).
USAGE_LOG = os.environ.get("CHAT2API_USAGE_LOG") or \
    os.path.join(HERE, os.pardir, "data", "usage-chat2api.jsonl")
_usage_lock = threading.Lock()
_usage_disabled = False

# Local management secret for /admin/* and the /api/* passthrough. Other local
# processes (a compromised share identity) must not be able to manage this
# service or borrow its authenticated Open WebUI proxy; the loopback check in
# is_local_control_request cannot tell local processes apart, so the console
# presents this token, stored in the repo data dir the share identity cannot
# read (repo-root deny ACE).
CONTROL_HEADER = "X-Admin-Control"
CONTROL_TOKEN_FILE = os.environ.get("CHAT2API_CONTROL_TOKEN_FILE") or \
    os.path.join(HERE, os.pardir, "data", "admin-control-token")

# Optional downstream API secret. Loopback-only binding keeps the historical
# no-key behavior. Binding to a non-loopback address is refused unless this is
# configured, so a future --host 0.0.0.0 cannot silently lend the user's UM
# session to the LAN. OpenAI clients may use Authorization: Bearer <key>;
# Anthropic clients may use x-api-key: <key>. The local secret is never
# forwarded to Open WebUI.
LOCAL_API_KEY = os.environ.get("CHAT2API_LOCAL_API_KEY", "").strip()

# ------------------------------------------------- automatic reauthentication
# Plan-Revision 13 A13-1..A13-3. Two chat2api instances (the private 8000
# chain and the dedicated share backend) share ONE UM identity, so their
# automatic reauthentication must be coordinated instead of both opening a
# submission window at the same time. Both run THIS source file, so the shared
# data dir they already use for local management state is a path they both
# have without either instance knowing the other's port, control token,
# profile or ledger. The file holds a LEASE (pid + epoch values) and a bounded
# attempt ledger - never a credential, a token, a page value or any account
# field. The dir is already ignored by Git, so this runtime state cannot be
# committed by accident. Only a file that is definitely ABSENT is a first
# initialisation (R13-2): one that exists but cannot be read or parsed refuses
# the session and is left byte-for-byte as it is, for an operator to inspect or
# remove. The `<file>.lock` sibling is the mutex (R13-1): it is never deleted,
# so its existence is not evidence that anybody holds it.
REAUTH_SLOT_FILE = os.environ.get("CHAT2API_REAUTH_SLOT_FILE") or \
    os.path.join(HERE, os.pardir, "data", "chat2api-reauth.state")
# Rolling window and cap for GLOBAL reauthentication sessions. Two automatic
# sessions inside one window mean the credential was just rejected twice
# already, which is the documented UMPASS account-lock risk; the window
# self-clears so the cap is never a permanent lockout.
REAUTH_ATTEMPT_WINDOW_S = 900.0
REAUTH_MAX_ATTEMPTS_PER_WINDOW = 3
# Lease margin on top of --login-timeout: a session may spend its whole
# timeout waiting for a human, so the lease must outlive it. A crashed holder
# is therefore reclaimed by the other instance after this bound instead of
# blocking it forever.
REAUTH_SLOT_MARGIN_S = 30.0

# Bounded periodic credential check (A13-2). Interval, backoff ceiling and
# per-round probe timeout are fixed here rather than derived: an idle instance
# must notice a rejected credential, but the check must not become a poller and
# must never issue a billable inference request or refresh the model cache.
AUTH_WATCH_INTERVAL_S = 300.0
AUTH_WATCH_MAX_INTERVAL_S = 1800.0
AUTH_WATCH_BACKOFF_FACTOR = 2.0
AUTH_WATCH_PROBE_TIMEOUT_S = 20.0

# Bounded refresh of the PUBLIC /api/config snapshot (W5-3). It rides the
# same auth-watch maintenance loop (no new timer or thread): a round whose
# last successful observation is older than the TTL fetches once, a failed
# refresh is retried no sooner than CONFIG_REFRESH_BACKOFF_S after the
# attempt (default = the TTL, so a failing upstream is never probed more
# often than a healthy one - a backoff at or below the watch interval would
# degrade into probing every round), and the network call keeps the
# historical 15 s probe budget. The values are tunable so a maintenance loop
# can be observed in tests. Staleness at the defaults: the gate rides the
# watch rounds, so the summary ages at most TTL plus one round - ~20 minutes
# while the credential is valid (300 s rounds), and up to ~45 minutes while
# a rejected credential has the watch backed off to AUTH_WATCH_MAX_INTERVAL_S.
try:
    CONFIG_REFRESH_TTL_S = max(
        0.0, min(3600.0,
                 float(os.environ.get("CHAT2API_CONFIG_REFRESH_TTL_S", "900"))))
except ValueError:
    CONFIG_REFRESH_TTL_S = 900.0
try:
    CONFIG_REFRESH_BACKOFF_S = max(
        0.0, min(3600.0, float(
            os.environ.get("CHAT2API_CONFIG_REFRESH_BACKOFF_S", "900"))))
except ValueError:
    CONFIG_REFRESH_BACKOFF_S = 900.0
CONFIG_REFRESH_TIMEOUT_S = 15.0

# Click safety gate (A13-3). These are the ONLY pages the automatic state
# machine may act on: chat.genai.um.edu.mo is the Open WebUI login entry of
# the instance's own --base-url (matched by exact origin, never by substring),
# and websso.um.edu.mo is the UMPASS form whose account and password Edge
# already autofilled from the instance's own profile. Everything else -
# including the Duo prompt, which is waited on but never clicked - stops the
# automatic flow and leaves the visible window to a human.
OPENWEBUI_SSO_BUTTON_SELECTORS = (
    'button:has-text("使用 UMPASS 继续")',
    'button:has-text("Continue with UMPASS")',
)
UMPASS_SSO_HOST = "websso.um.edu.mo"
UMPASS_ACCOUNT_SELECTORS = (
    'input[name="UserName"]',
    'input[id="userNameInput"]',
)
UMPASS_PASSWORD_SELECTORS = (
    'input[name="Password"]',
    'input[id="passwordInput"]',
    'input[type="password"]',
)
UMPASS_SUBMIT_SELECTORS = (
    'input[type="submit"]',
    'button[type="submit"]',
    'span#submitButton[role="button"]',
)
UMPASS_MFA_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'text="验证码"',
    'text="Verification code"',
)
# Recognized pages the state machine only WAITS on. The Duo prompt passes on
# its own for a trusted device; a Duo prompt that asks for a code is the
# human's step, so it is never clicked.
DUO_WAIT_HOSTS = ("duosecurity.com",)
# One automatic session's bounds. A click is followed by an observation window
# (a swallowed click must be PROVEN before the single bounded retry); a page we
# intend to act on gets a settle window so a slow render is not mistaken for a
# changed site; and each stage may be clicked once plus at most one
# evidence-backed retry.
LOGIN_POLL_INTERVAL_MS = 1000
LOGIN_CLICK_OBSERVE_POLL_MS = 250
LOGIN_CLICK_TIMEOUT_MS = 15000
LOGIN_CLICK_OBSERVE_TIMEOUT_S = 8.0
LOGIN_STAGE_SETTLE_TIMEOUT_S = 20.0
LOGIN_MAX_CLICKS_PER_STAGE = 2

# Opt-in "raw upstream catalog, fresh, no local virtual aliases" contract used
# by the public share entry. Behavior-neutral for all other consumers: they
# never send the request header, so they keep the historical /v1/models reply
# (stale-cache fallback + effort virtual aliases). The response marker lets the
# share layer verify the reply is actually a fresh raw snapshot instead of a
# stale cached one.
X_MODELS_HEADER = "X-Chat2Api-Models"
X_MODELS_MARKER = "X-Chat2Api-Raw-Models"
RAW_FRESH_VALUE = "raw-fresh"

# Upstream streaming guards. UM's edge has been observed to close chat streams
# that stay silent for ~50s (2026-09-06 incident: 50.2-53.1s with zero events),
# so the first-byte budget must stay well above that to never race a healthy
# but slow prefill; it only fires when an upstream stalls without closing.
# STREAM_SOCKET_TIMEOUT_S is a per-socket-read silence bound that defines the
# WINDOW in which a fully-silent upstream releases the reader after cancel -
# it is not a cancel deadline: once the upstream sends anything, the producer
# checks cancellation on every byte and exits promptly. Active streams never
# hit it (every recv restarts it).
STREAM_FIRST_BYTE_TIMEOUT_S = 120
STREAM_WATCHDOG_POLL_S = 0.5
STREAM_SOCKET_TIMEOUT_S = 180
# Byte bound on input received while the 200 headers are still pending: SSE
# lines held back for replay (the prelude). Blank lines and comments count
# too, and they do NOT stop the first-data budget, so a chatty upstream must
# fail the request with an explicit 502 instead of pinning memory or
# withholding the headers forever. Private, tunable.
_PRELUDE_MAX_BYTES = 64 * 1024
# Independent, larger bound on the producer's not-yet-a-line byte buffer
# (bytes that have not formed a complete SSE line). It exists only so a
# line-less upstream (no "\n" at all) cannot pin memory forever. It is a
# LOCAL full-stream single-line compatibility limit: a line that never
# completes within it is rejected even if the eventual SSE text would be
# syntactically valid, so downstream clients are compatible only with lines
# up to this size. Before the 200 is committed the overflow surfaces as an
# HTTP 502 (upstream_read_error); after it, as an in-band
# upstream_read_error. Private, tunable.
_PRODUCER_LINE_BUFFER_MAX_BYTES = 1024 * 1024
# Backpressure bound on the SSE producer/consumer queue: when the handler is
# behind (or gone), the producer blocks here instead of buffering unboundedly.
_UPSTREAM_QUEUE_MAX = 256
# Browser-origin gate. A page in ANY website can fire requests at
# 127.0.0.1:8000 from the user's own browser and would otherwise borrow the
# stored UM identity (the CORS "Access-Control-Allow-Origin: *" reply makes
# such reads work). Non-browser local programs (curl, ZCode, SDKs) never send
# an Origin header, so POST endpoints reject any request that CARRIES one
# unless it is explicitly listed in CHAT2API_ALLOWED_ORIGINS (comma
# separated). Read-only GET endpoints (/health, /ready) stay open.
ALLOWED_ORIGINS = frozenset(
    part.strip() for part in
    os.environ.get("CHAT2API_ALLOWED_ORIGINS", "").split(",") if part.strip())
# Cap one request body: _read_body() trusts the client-declared
# Content-Length, so a hostile local process could otherwise pin memory.
try:
    MAX_REQUEST_BYTES = int(
        os.environ.get("CHAT2API_MAX_REQUEST_BYTES", str(32 * 1024 * 1024)))
except ValueError:
    MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = max(1, min(MAX_REQUEST_BYTES, 1024 * 1024 * 1024))
# Total wall-clock budget for ONE chat request's transparent retry loop
# (reasoning-only phase). Attempt counts alone would let a silent gateway
# (~120s first-byte budget x 6 attempts) pin an unattended client for ~12
# minutes; after this deadline every attempt surfaces its last failure.
try:
    MAX_REQUEST_SECONDS = int(
        os.environ.get("CHAT2API_MAX_REQUEST_SECONDS", "360"))
except ValueError:
    MAX_REQUEST_SECONDS = 360
MAX_REQUEST_SECONDS = max(10, min(MAX_REQUEST_SECONDS, 3600))

# UM / GLM-5.3-Flash currently rejects a single prompt containing more than
# 16 images. Long-lived coding-agent conversations can accumulate historical
# multimodal parts and repeatedly hit that application-level HTTP 400 even when
# the newest user turn is valid. Keep the current/protected turn intact by
# default (a current-turn overflow is rejected locally) and trim only the
# oldest historical image parts before the first upstream attempt. The feature
# has its own switch so setting the numeric limit to 0 never has ambiguous
# semantics.
try:
    MAX_IMAGES = int(os.environ.get("CHAT2API_MAX_IMAGES", "16"))
except ValueError:
    MAX_IMAGES = 16
MAX_IMAGES = max(1, min(MAX_IMAGES, 256))
AUTO_TRIM_HISTORICAL_IMAGES = (
    os.environ.get("CHAT2API_AUTO_TRIM_HISTORICAL_IMAGES", "1") != "0")
# Off by default: the current turn is what the user explicitly submitted this
# time, so overflowing it keeps the local 400 unless unattended operation is
# explicitly requested. When enabled, the normalizer keeps the newest
# MAX_IMAGES current-turn images, drops the oldest ones, and logs a WARNING.
# Only effective while AUTO_TRIM_HISTORICAL_IMAGES (the master switch for any
# payload rewrite) is also enabled.
AUTO_TRIM_CURRENT_TURN_IMAGES = (
    os.environ.get("CHAT2API_AUTO_TRIM_CURRENT_TURN_IMAGES", "0") == "1")
_IMAGE_PART_TYPES = frozenset(("image_url", "input_image", "image"))
_HISTORICAL_IMAGE_PLACEHOLDER = (
    "[historical image omitted by proxy due to upstream image limit]")
_CURRENT_TURN_IMAGE_PLACEHOLDER = (
    "[current-turn image omitted by proxy due to upstream image limit]")

# Repetition-loop guard. A degenerate model run can emit one short token over
# and over as the whole reasoning/content stream (observed 2026-09-16..18: pure
# "lock" repeats reaching exactly the 65533-char output budget, ~16k tokens
# wasted per round and a 5-10 minute hang before the budget cuts it). The guard
# watches ONLY the streamed reasoning/content text - never tool-call arguments,
# where repeats are legitimate - and raises the standard in-band error once the
# accumulated tail is pure repetition. Thresholds are deliberately one-sided:
# a legitimate reply essentially never ends in >=1200 chars made of one 1-8
# char pattern repeated, so a false cut is far rarer than the degeneration
# itself. Set CHAT2API_REPETITION_GUARD=0 to disable without a code rollback.
REPETITION_GUARD_MIN_TAIL_CHARS = 1200
REPETITION_GUARD_MAX_PATTERN_CHARS = 8
REPETITION_GUARD_BUFFER_CHARS = 4096
REPETITION_GUARD_ENABLED = os.environ.get("CHAT2API_REPETITION_GUARD", "1") != "0"
# Observation-only detector for long-unit line loops (see
# _line_cycle_repetition). A thinking channel can also cycle a whole line or
# a small alternating line group ("Hmm — OK." / "Hmm — hmm."), whose period
# exceeds the 1-8 char window the cutting signature accepts, so that loop is
# invisible to the cutter. This detector never cuts a stream and never
# retries; it only logs a WARNING and counts hits per request so a week of
# production evidence can set real thresholds before the cutting window is
# widened. Set CHAT2API_LINE_LOOP_OBSERVE=0 to disable the log noise.
LINE_LOOP_OBSERVE_ENABLED = os.environ.get(
    "CHAT2API_LINE_LOOP_OBSERVE", "1") != "0"
LINE_LOOP_MAX_CYCLE_LINES = 6
LINE_LOOP_MAX_LINE_CHARS = 120
LINE_LOOP_MIN_REPEATS = 10
LINE_LOOP_MIN_TAIL_CHARS = 600
LINE_LOOP_CHECK_EVERY_CHARS = 200
# Transparent retries for an early reasoning-only repetition loop. Retries
# are intentionally limited to the phase before any content/tool output has
# been written downstream; retrying after visible output would concatenate
# two independent completions in one SSE stream.
REPETITION_GUARD_AUTO_RETRY = (
    os.environ.get("CHAT2API_REPETITION_AUTO_RETRY", "1") != "0")
try:
    REPETITION_GUARD_AUTO_RETRIES = max(0, min(10, int(
        os.environ.get("CHAT2API_REPETITION_AUTO_RETRIES", "5"))))
except ValueError:
    REPETITION_GUARD_AUTO_RETRIES = 5

# Automatic recovery for transient upstream failures before visible output has
# reached the downstream client.  This budget is deliberately separate from
# model-degeneration retries: a flaky gateway must not consume the model-loop
# budget, and a model loop must not consume the network budget.
UPSTREAM_AUTO_RETRY = os.environ.get("CHAT2API_UPSTREAM_AUTO_RETRY", "1") != "0"
try:
    UPSTREAM_AUTO_RETRIES = max(0, min(10, int(
        os.environ.get("CHAT2API_UPSTREAM_AUTO_RETRIES", "5"))))
except ValueError:
    UPSTREAM_AUTO_RETRIES = 5
# A slow UM failure is qualitatively different from an immediate 502/503:
# production logs show 504/chunk truncation attempts commonly consume ~50s.
# Repeating those five times makes an otherwise healthy local proxy look hung
# for ~5 minutes. Keep the generous fast-failure budget, but cap failures
# whose attempt itself already consumed substantial wall time.
try:
    UPSTREAM_SLOW_RETRY_THRESHOLD_S = float(
        os.environ.get("CHAT2API_UPSTREAM_SLOW_RETRY_THRESHOLD_S", "30"))
except ValueError:
    UPSTREAM_SLOW_RETRY_THRESHOLD_S = 30.0
UPSTREAM_SLOW_RETRY_THRESHOLD_S = max(
    1.0, min(300.0, UPSTREAM_SLOW_RETRY_THRESHOLD_S))
try:
    UPSTREAM_SLOW_AUTO_RETRIES = max(0, min(5, int(
        os.environ.get("CHAT2API_UPSTREAM_SLOW_AUTO_RETRIES", "2"))))
except ValueError:
    UPSTREAM_SLOW_AUTO_RETRIES = 2
try:
    MAX_TOTAL_ATTEMPTS = max(1, min(30, int(
        os.environ.get("CHAT2API_MAX_TOTAL_ATTEMPTS", "12"))))
except ValueError:
    MAX_TOTAL_ATTEMPTS = 12
_UPSTREAM_RETRYABLE_STATUS = frozenset((408, 429, 500, 502, 503, 504))
_UPSTREAM_TRANSIENT_EXCEPTIONS = (
    requests.ConnectionError, requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_REPETITION_PREFLIGHT_MAX_BYTES = 2 * 1024 * 1024

# Hard cap on concurrent upstream reader threads/connections. Cancelled
# readers can outlive their handler (bounded by the silence window above), so
# a flurry of cancels must not push the thread/connection count without limit.
STREAM_MAX_CONCURRENT_READERS = 128
_reader_slots = threading.BoundedSemaphore(STREAM_MAX_CONCURRENT_READERS)

# In-band stream error messages (after the 200 headers are already sent).
# openai-node throws APIError on any SSE payload containing an "error" key, so
# these surface in clients instead of a generic protocol failure.
STREAM_ERROR_MESSAGES = {
    "upstream_eof_without_done":
        "Upstream closed the stream before the completion finished "
        "(no [DONE] received).",
    "upstream_read_error":
        "Upstream connection failed while streaming.",
    "degenerate_repetition":
        "[chat2api:degenerate_repetition] Upstream model fell into a repetition "
        "loop; the proxy cut the stream. This completion is incomplete.",
    "upstream_first_byte_timeout":
        "Upstream sent no stream data within the first-byte budget.",
    "upstream_missing_finish_reason":
        "Upstream finished the stream ([DONE]) without a finish_reason; "
        "the completion is incomplete.",
    "upstream_prelude_overflow":
        "Upstream sent too much non-event data before the first event; "
        "the held-back prelude exceeded its byte bound.",
    "upstream_stream_error":
        "Upstream reported an explicit stream error; the completion is "
        "incomplete.",
}

# Base models that get thinking-level virtual variants (see docstring).
# You can also add them on the command line with --effort-model.
EFFORT_MODELS: list[str] = []

# Suffix -> OpenAI `reasoning_effort` value. The backend must support the
# parameter (vLLM does); otherwise requests still go through unchanged.
EFFORT_LEVELS = {
    "Fast": "none",     # disable thinking, fastest
    "Low": "low",
    "Medium": "medium",
    "High": "high",
}

EDGE_PATHS = [
    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("PROGRAMFILES", ""),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
]

_logger = logging.getLogger("chat2api")
if not _logger.handlers:
    _logger.addHandler(logging.NullHandler())
_logger.setLevel(logging.INFO)
_logger.propagate = False


def configure_logging():
    """Attach the service file handler once, only for an actual service run.

    Keeping this out of module import prevents unit tests and helper imports
    from opening/writing the production chat2api.log merely by loading the
    module. ``assertLogs`` and embedding callers can still attach their own
    handlers normally.
    """
    if any(getattr(h, "_chat2api_service_handler", False)
           for h in _logger.handlers):
        return
    handler = RotatingFileHandler(
        SERVICE_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler._chat2api_service_handler = True
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def _detect_runtime_build_id():
    """Best-effort git build id for logs/version without import-time subprocesses."""
    override = os.environ.get("CHAT2API_BUILD", "").strip()
    if override:
        return override
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=HERE,
            check=False, capture_output=True, text=True, timeout=2,
        )
        value = rev.stdout.strip() if rev.returncode == 0 else ""
        if not value:
            raise RuntimeError("git revision unavailable")
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", os.path.basename(__file__)],
            cwd=HERE, check=False, capture_output=True, text=True, timeout=2,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            value += "+dirty"
        return value
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return f"{CHAT2API_SOURCE_REV}+reviewed"


def _attempt_is_slow(elapsed_s):
    return elapsed_s >= UPSTREAM_SLOW_RETRY_THRESHOLD_S


def _retry_delay_seconds(retry_number, attempt_elapsed_s, response=None,
                         slow=False):
    """Retry delay shared by chat and compat paths.

    Fast failures use short exponential jitter. Slow failures deliberately
    keep 1-3 seconds of jitter instead of immediately re-hitting UM in a
    synchronized wave after ~50-second 504/chunked failures.
    """
    if response is not None and response.status_code == 429:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(10.0, max(0.0, float(raw)))
            except ValueError:
                pass
            try:
                target = email.utils.parsedate_to_datetime(raw)
                delta = target.timestamp() - time.time()
                return min(10.0, max(0.0, delta))
            except (TypeError, ValueError, OverflowError):
                pass
    if slow:
        return random.uniform(1.0, 3.0)
    # Fast transport failures use a deterministic, bounded backoff.  Do not
    # collapse to zero merely because DNS/connect setup itself consumed >=1s:
    # that recreates the bursty five-attempt failure pattern this guard exists
    # to prevent.
    return min(2.0, 0.5 * (2 ** max(0, retry_number - 1)))


def _sleep_with_deadline(delay_s, deadline):
    """Sleep no longer than the remaining wall budget; return room-to-retry."""
    if delay_s <= 0:
        return time.monotonic() < deadline
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(delay_s, remaining))
    return time.monotonic() < deadline


class TokenStore:
    """Persist/load the Open WebUI session token."""

    def __init__(self, path: str, url: str):
        self.path = path
        self.url = url

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("url") != self.url:
                return None
            return data
        except Exception:
            return None

    def save(self, token: str, api_key: str = None):
        data = {"url": self.url, "token": token, "api_key": api_key,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(self.path, 0o600)
        return data


def find_edge(candidates=None) -> str | None:
    for p in candidates or EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def fetch_upstream_config_observed(base_url: str, timeout=15):
    """One /api/config observation with an explicit failure kind.

    Returns ``(config, failure_reason, upstream_status)``; exactly one of
    config and failure_reason is meaningful. Failure kinds (W5-3 evidence):
    ``timeout``, ``network_error``, ``upstream_auth_rejected`` (401/403),
    ``upstream_5xx``, ``unexpected_status``, ``invalid_json`` and
    ``invalid_config_shape`` (a 200 body that is not a JSON object). The
    endpoint is queried WITHOUT the user's bearer, so a failure here never
    means "credential expired" and never triggers reauthentication.
    """
    response = None
    try:
        response = requests.get(f"{base_url}/api/config", timeout=timeout)
    except requests.Timeout:
        return None, "timeout", None
    except requests.RequestException:
        return None, "network_error", None
    try:
        status = response.status_code
        if status in (401, 403):
            return None, "upstream_auth_rejected", status
        if 500 <= status <= 599:
            return None, "upstream_5xx", status
        if status != 200:
            return None, "unexpected_status", status
        try:
            payload = response.json()
        except ValueError:
            return None, "invalid_json", status
        if not isinstance(payload, dict):
            return None, "invalid_config_shape", status
        return payload, None, status
    finally:
        response.close()


def fetch_upstream_config(base_url: str, timeout=15) -> dict | None:
    """Best-effort public Open WebUI /api/config probe.

    The endpoint is intentionally queried without the user's bearer. Open
    WebUI exposes version and public feature flags there even before login;
    avoiding the bearer means a stale JWT cannot turn a harmless capability
    probe into a 401/reauth side effect. Failures of every kind fold into
    None; callers that need the failure KIND call
    ``fetch_upstream_config_observed`` instead.
    """
    config, _reason, _status = fetch_upstream_config_observed(base_url,
                                                              timeout)
    return config


def _config_api_keys_enabled(config: dict | None):
    if not isinstance(config, dict):
        return None
    features = config.get("features")
    if not isinstance(features, dict) or "enable_api_keys" not in features:
        return None
    return bool(features.get("enable_api_keys"))


def _is_loopback_bind_host(host: str) -> bool:
    if (host or "").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def credential_is_valid(base_url: str, bearer: str) -> bool:
    """Return whether a bearer is EXPLICITLY accepted by the upstream catalog.

    Thin compatibility wrapper over ``classify_credential`` (A13-2): every
    caller now shares one classification source, so a network failure, a 5xx or an
    unusable 200 body can no longer be folded into "the credential expired".
    Callers that must tell "rejected" from "cannot tell" call
    ``classify_credential`` directly instead of using this boolean.
    """
    return classify_credential(base_url, bearer)[0] == "valid"


def classify_credential(base_url: str, bearer: str, timeout=15):
    """Single-shot, side-effect-free credential classification probe.

    NEVER call Proxy.refresh()/proxy.call() from here and never touch
    ``_auth_state`` or ``models_cache``: a 401 verdict must not trigger a
    browser login, and a 200 body must not seed the model cache. Returns
    ``(verdict, reason, upstream_status)``:

    * ``needs_login`` — local bearer is empty (no upstream request);
    * ``valid``      — upstream 200 with a parsable catalog structure;
    * ``rejected``   — upstream answered 401/403 (NOT interpreted as
      "expired": the credential/access was rejected right now);
    * ``unknown``    — 5xx, network failure, timeout, or a 200 body that
      is not a usable catalog (verdict undecidable).

    ``timeout`` is the per-probe socket budget; the periodic check passes its
    own explicit bound instead of inheriting this default.
    """
    if not bearer:
        # Local-only verdict: nothing to probe, no request leaves the box.
        return "needs_login", "no_local_credential", None
    try:
        # Single attempt, no retry: this endpoint is a probe, not a proxy.
        response = requests.get(
            f"{base_url}/api/models",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=timeout,  # 15 matches credential_is_valid()'s precedent
        )
    except requests.Timeout:
        return "unknown", "timeout", None
    except requests.RequestException:
        return "unknown", "network_error", None
    try:
        status = response.status_code
        if status in (401, 403):
            return "rejected", "upstream_rejected", status
        if 500 <= status <= 599:
            return "unknown", "upstream_5xx", status
        if status != 200:
            return "unknown", "unexpected_status", status
        try:
            payload = response.json()
        except ValueError:
            # requests' JSONDecodeError is a ValueError subclass.
            return "unknown", "non_json_response", status
        # Minimum structure standard, inferred from list_models' cache use
        # (an upstream 200 body is a dict carrying a "data" list).
        if not isinstance(payload, dict) or not isinstance(payload.get("data"),
                                                           list):
            return "unknown", "invalid_catalog_shape", status
        return "valid", None, status
    finally:
        response.close()


def _is_number(value) -> bool:
    """A real, FINITE number: booleans are ints in Python and are NOT counts.

    ``json.loads`` accepts the bare tokens ``NaN``/``Infinity``, and a NaN entry
    defeats every comparison it takes part in: ``now - nan < window`` is False (the
    attempt silently leaves the ledger) and ``nan > now`` is False (the lease reads
    as expired). A slot written that way is damaged, not empty, so it must be
    refused like any other unreadable state (R13-2) instead of reopening the
    submission window and dropping the global attempt cap.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(value)


def _slot_attempts(state, now):
    """Attempt epochs inside the rolling window; junk entries are dropped.

    ``_read_reauth_slot`` validates the shape before this runs (R13-2), so a
    dropped entry can only come from a caller that built the state itself: this is
    defence in depth, never the silent repair of a damaged ledger.
    """
    attempts = []
    for entry in state.get("attempts") or []:
        if _is_number(entry):
            if now - float(entry) < REAUTH_ATTEMPT_WINDOW_S:
                attempts.append(float(entry))
    return attempts


_SLOT_KEYS = ("owner_pid", "started_epoch", "expires_epoch",
              "attempts")


def _slot_state_is_well_formed(state) -> bool:
    """Shape contract for the shared slot (R13-2).

    A present field with the wrong type is a fact this process cannot read, not an
    absent one: junk ``attempts`` read as an empty ledger would silently drop the
    global attempt cap, and a junk ``owner_pid``/``expires_epoch`` read as "no live
    owner" would open a second submission window on top of a lease the first
    instance still believes it holds. Absent keys claim nothing and are accepted;
    ``owner_pid: null`` is what a free slot looks like and is accepted too.
    """
    # A state this code wrote always carries all four keys, so a present file that
    # is missing one is a damaged ledger, not a partial truth: reading a dropped
    # ``attempts`` as an empty list would clear the global attempt cap, which is
    # exactly the "absent keys claim nothing" reading R13-2 forbids. Only the
    # absence of the FILE means "first initialisation".
    missing = [key for key in _SLOT_KEYS if key not in state]
    if missing:
        return False
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return False
    for entry in attempts:
        if not _is_number(entry):
            return False
    owner = state.get("owner_pid")
    if owner is not None and (isinstance(owner, bool)
                            or not isinstance(owner, int)):
        return False
    for key in ("started_epoch", "expires_epoch"):
        if not _is_number(state.get(key)):
            return False
    return True


def _read_reauth_slot(path: str):
    """Read the shared slot, keeping "definitely absent" apart from "unusable".

    Returns ``(state, error)``. ``error`` is ``None`` when the state was read and
    satisfies the shape contract, ``'missing'`` when the file is definitely not
    there (a first initialisation), and ``'corrupt'``/``'unreadable'`` when it
    exists but cannot be trusted. Those are NOT interchangeable (R13-2): a damaged
    ledger read as an empty one grants a session and restarts the global attempt
    count from one, which silently drops the cap that protects the UM account from
    repeated rejections.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}, "missing"
    except (ValueError, RecursionError):
        # json.JSONDecodeError (truncated/garbage bytes), UnicodeDecodeError
        # and a deeply nested payload (RecursionError is a RuntimeError, so the
        # ValueError clause alone let it escape as a 500 instead of an honest
        # refusal).
        return {}, "corrupt"
    except OSError:
        # Present but unreadable (permissions, EIO, a directory at the path).
        return {}, "unreadable"
    if not isinstance(state, dict) or not _slot_state_is_well_formed(state):
        return {}, "corrupt"
    return state, None


def _write_reauth_slot(path: str, state: dict) -> bool:
    """Atomic replace (temp sibling + os.replace) so a reader never sees a
    half-written slot. Returns False instead of raising: the caller must treat
    an unwritable slot as "coordination unavailable" and refuse the session."""
    tmp_path = "%s.%d.tmp" % (path, os.getpid())
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


REAUTH_LOCK_WAIT_S = 1.0
REAUTH_LOCK_POLL_S = 0.05


class _ReauthLock:
    """A held cross-process lock (R13-1).

    ``fd`` is the open handle that owns the kernel lock; ``path`` exists for
    diagnostics and tests only. The lock FILE is never deleted, so its existence
    says nothing about who holds the lock - exactly like model_sync.SyncLock and
    console_operations.ResourceFileLock, the two precedents this protocol copies.
    """

    __slots__ = ("fd", "path")

    def __init__(self, fd, path):
        self.fd = fd
        self.path = path


def _acquire_reauth_lock(path: str):
    """Cross-process mutex for the slot read-modify-write (A13-3, R13-1).

    The lease below is advisory and long-lived, so it cannot make the acquire step
    itself atomic: without this lock two instances read the same free slot and both
    write themselves in, and the second write silently replaced the first holder AND
    its ledger entry (two submissions counted as one). The lock covers only that
    step.

    R13-1: the mutex IS the kernel's byte-range lock (``msvcrt.locking``, the
    same protocol as ``model_sync.SyncLock``) on ``<slot>.lock``, held through an
    open handle for the whole critical section. The previous implementation created
    the file with O_EXCL, closed the handle at once, and reclaimed a "stale" file by
    mtime + unlink: that unlink was not bound to the file it had observed, so two
    parties could each delete the other's fresh lock and both end up holding one
    (a second submission window, and two attempts counted as one). Nothing is
    reclaimed now - the OS drops the lock when the holding process dies, so a crashed
    holder cannot block the next one and no observer ever has to delete a live
    holder's file. It is therefore also impossible for a second party to "take over"
    a lock: it can only wait, and the caller keeps observing.

    Returns the handle, or None when the lock could not be taken within
    REAUTH_LOCK_WAIT_S; the caller must then refuse the session rather than
    guess.
    """
    try:
        import msvcrt  # Windows-only primitive, like the precedents above
    except ImportError:  # pragma: no cover - non-Windows host
        return None
    lock_path = "%s.lock" % path
    try:
        directory = os.path.dirname(os.path.abspath(lock_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        return None
    try:
        # msvcrt.locking() locks a byte RANGE from the current position, so
        # the file must be at least one byte long and the position must be
        # reset before every lock/unlock call.
        if os.fstat(fd).st_size == 0:
            try:
                os.write(fd, b"\0")
            except OSError:
                # Two processes can both open a still-empty file, so the
                # second one's fstat may see zero bytes and its seed write
                # then hits the byte the first one already locked. That is a
                # concurrent holder, not a failed acquisition: the range lock
                # itself works on a zero-length file, so waiting below is the
                # correct move (returning None here refused a free lock).
                pass
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    deadline = time.time() + REAUTH_LOCK_WAIT_S
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return _ReauthLock(fd, lock_path)
        except OSError:
            pass
        if time.time() >= deadline:
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        time.sleep(REAUTH_LOCK_POLL_S)


def _release_reauth_lock(handle):
    """Release a handle from ``_acquire_reauth_lock``; never delete the file.

    Unlocking and closing the handle is the whole release: the file stays on disk
    for the next holder. Deleting it is exactly the path-based reclaim R13-1
    removed, so anything that is not a handle from ``_acquire_reauth_lock``
    (including ``None``, a refused acquire) is a no-op rather than an exception
    raised inside a ``finally``.
    """
    fd = getattr(handle, "fd", None)
    if fd is None:
        return
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - non-Windows host
        msvcrt = None
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_reauth_slot(path: str, pid: int, ttl_s: float, now=None):
    """Take the cross-instance reauthentication lease (A13-3).

    Returns ``(granted, reason, state)``. Two instances share one UM
    identity, so a second submission window while another session is live is
    refused; so is a session once the rolling attempt window is full. An EXPIRED
    lease is reclaimed, so a crashed holder cannot block the other instance
    forever. A same-pid re-acquire is idempotent and is not counted twice.

    A slot that cannot be read is NOT an empty slot (R13-2): ``slot_corrupt``
    and ``slot_unreadable`` refuse the session and report the state honestly,
    because granting here would restart the global attempt count from one and open a
    window on top of a lease nobody can read. The damaged file is left untouched
    for the operator to inspect or remove.
    """
    now = time.time() if now is None else now
    lock = _acquire_reauth_lock(path)
    if lock is None:
        return False, "coordination_unavailable", {
            "owner_pid": None, "expires_epoch": 0.0,
            "attempts_in_window": 0}
    try:
        state, error = _read_reauth_slot(path)
        if error not in (None, "missing"):
            reason = "slot_corrupt" if error == "corrupt" else "slot_unreadable"
            _logger.warning(
                "reauthentication slot %s path=%s - refusing sessions; the "
                "file was left untouched for an operator to inspect or remove",
                error, path)
            # The ledger was not read, so it is reported the same way as an
            # unreachable lock: nothing was observed, nothing is claimed.
            return False, reason, {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": 0}
        attempts = _slot_attempts(state, now)
        owner = state.get("owner_pid")
        expires = state.get("expires_epoch")
        live_owner = (isinstance(owner, int) and not isinstance(owner, bool)
                      and isinstance(expires, (int, float))
                      and float(expires) > now)
        if live_owner and owner != pid:
            return False, "global_session_active", {
                "owner_pid": owner, "expires_epoch": float(expires),
                "attempts_in_window": len(attempts)}
        if not live_owner and len(attempts) >= REAUTH_MAX_ATTEMPTS_PER_WINDOW:
            return False, "global_attempt_cap", {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": len(attempts)}
        same_session = live_owner and owner == pid
        new_state = {
            "owner_pid": pid,
            "started_epoch": state.get("started_epoch") if same_session else now,
            "expires_epoch": now + ttl_s,
            "attempts": attempts if same_session else attempts + [now],
        }
        if not _write_reauth_slot(path, new_state):
            return False, "slot_unavailable", {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": len(attempts)}
        return True, None, {
            "owner_pid": pid, "expires_epoch": new_state["expires_epoch"],
            "attempts_in_window": len(new_state["attempts"])}
    finally:
        _release_reauth_lock(lock)


def release_reauth_slot(path: str, pid: int, success=False):
    """Release a lease this pid holds; never release another instance's lease.

    ``success`` clears the attempt ledger (a sign-in that worked is proof the
    credential is good, so earlier rejections must not keep throttling the next
    one); a failure keeps it, which is what bounds the lockout risk.

    R13-2: a slot that cannot be read is left exactly as it is. Writing over it
    would replace a fail-closed state with an empty slot and destroy the only
    evidence an operator has; an absent slot (or one owned by another pid) is not
    written either, exactly as before.
    """
    lock = _acquire_reauth_lock(path)
    if lock is None:
        return
    try:
        state, error = _read_reauth_slot(path)
        if error is not None:
            return
        if state.get("owner_pid") != pid:
            return
        attempts = [] if success else _slot_attempts(state, time.time())
        _write_reauth_slot(path, {
            "owner_pid": None,
            "started_epoch": 0.0,
            "expires_epoch": 0.0,
            "attempts": attempts,
        })
    finally:
        _release_reauth_lock(lock)


def _same_element(left, right) -> bool:
    """Whether two matched controls are the SAME DOM element.

    Identity, not the selector that found it, decides whether a page holds one
    expected control or two: the ADFS form's input carries both ``name`` and
    ``id``, so one control answers several allowed selectors. A handle that
    cannot answer the comparison - stale, detached, or a stand-in page that
    carries no DOM identity - is never assumed to be the same element.
    """
    if left is right:
        return True
    try:
        return bool(left.evaluate("(el, other) => el === other", right))
    except Exception:
        return False


class _LoginPage:
    """Narrow page facade for the login state machine.

    The state machine touches ONLY these methods, so an offline fake page can
    drive the real loop. Field VALUES are read through ``field_filled`` which
    returns a bool and never the value itself: nothing here can print, return or
    persist an account or password.
    """

    def __init__(self, page):
        self._page = page

    def url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:
            return ""

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self._page.goto(url, wait_until=wait_until, timeout=timeout)

    def bring_to_front(self):
        self._page.bring_to_front()

    def read_token(self):
        return self._page.evaluate("localStorage.getItem('token')")

    def drop_token(self):
        self._page.evaluate("localStorage.removeItem('token')")

    def reload(self):
        self._page.reload(wait_until="domcontentloaded", timeout=30000)

    def wait(self, ms):
        self._page.wait_for_timeout(ms)

    def _visible_matches(self, selectors):
        """Targets of the visible CONTROLS across the allowed selectors only.

        One physical control is regularly reachable through several of these
        selectors - the ADFS form's input carries both ``name`` and ``id``, which
        is exactly why each list has fallbacks - so matches are deduplicated by
        element identity. Two *different* visible controls still count as two.
        """
        found = []
        seen = []
        for selector in selectors:
            try:
                locator = self._page.locator(selector)
                count = locator.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    element = locator.nth(index)
                    if not element.is_visible():
                        continue
                except Exception:
                    continue
                handle = None
                try:
                    handle = element.element_handle()
                except Exception:
                    pass  # no identity: count it as its own control, never skip
                if handle is not None:
                    if any(_same_element(handle, other) for other in seen):
                        continue
                    seen.append(handle)
                found.append((selector, index))
        return found

    def one_visible(self, selectors):
        """Exactly one visible control, or None when absent/ambiguous.

        Absent and ambiguous are the SAME outcome on purpose: an unexpected
        extra control is a changed site, and a changed site is never clicked. One
        control found through several of the allowed selectors is still ONE control.
        """
        found = self._visible_matches(selectors)
        return found[0] if len(found) == 1 else None

    def any_visible(self, selectors):
        return bool(self._visible_matches(selectors))

    def field_filled(self, selectors) -> bool:
        """Whether the single visible field holds a non-empty value.

        The value itself is deliberately never returned, logged or stored; only
        this boolean leaves the page.
        """
        target = self.one_visible(selectors)
        if target is None:
            return False
        selector, index = target
        try:
            value = self._page.locator(selector).nth(index).input_value()
        except Exception:
            return False
        return bool((value or "").strip())

    def click(self, target):
        selector, index = target
        self._page.locator(selector).nth(index).click(
            timeout=LOGIN_CLICK_TIMEOUT_MS)


def _page_origin(url: str) -> str:
    """Exact scheme://host:port origin; an unparsable URL yields "" so the
    caller's origin gate fails closed instead of matching by accident."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return "%s://%s" % (parts.scheme.lower(), parts.netloc.lower())


def _page_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, allowed: str) -> bool:
    """Exact host or a subdomain of it — never a substring match, so a
    lookalike like websso.um.edu.mo.evil.example cannot pass the gate."""
    return host == allowed or host.endswith("." + allowed)


class LoginStalled(RuntimeError):
    """The automatic flow stopped and the visible window awaits a human."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__("login requires a human: %s%s"
                         % (reason, (" (%s)" % detail) if detail else ""))
        self.reason = reason
        self.detail = detail


def _click_and_observe(page, target, before_url, cancel_event) -> str:
    """One bounded click plus observation. Returns ``advanced`` when the page
    demonstrably moved (URL changed, or the clicked control left the page) and
    ``swallowed`` only when the page is unchanged — the evidence a retry
    requires. A click that raises is not evidence of delivery, so it also
    reports ``swallowed`` after the same observation window."""
    try:
        page.click(target)
    except Exception:
        pass  # not actionable: no evidence it was delivered
    deadline = time.time() + LOGIN_CLICK_OBSERVE_TIMEOUT_S
    while time.time() < deadline:
        if cancel_event and cancel_event.is_set():
            return "cancelled"
        if page.url() != before_url:
            return "advanced"
        if not page.any_visible((target[0],)):
            return "advanced"  # the control left the DOM: something moved
        page.wait(LOGIN_CLICK_OBSERVE_POLL_MS)
    return "swallowed"


def browser_login(base_url: str, profile_dir: str, timeout_s: int,
                  on_context=None, cancel_event=None,
                  context_factory=None, credential_probe=None,
                  on_progress=None) -> str:
    """Open visible Edge and drive the two-step UM SSO in one bounded session.

    Controlled flow (A13-1/A13-3): the Open WebUI login page's
    "使用 UMPASS 继续" entry, then the UMPASS form whose account and password
    Edge already autofilled from this instance's own profile. The automatic part
    only acts on the verified Open WebUI origin (the instance's own --base-url)
    and on websso.um.edu.mo, and only while the expected control is uniquely
    visible. MFA/captcha, a missing prefill, a changed form, an unknown page
    or a swallowed click stop the automatic flow and leave the visible window to
    a human (``on_progress`` reports the reason; the token poll keeps
    running). ``context_factory(edge, profile_dir)`` and
    ``credential_probe(base_url, token)`` are the test seams: the real
    defaults are the Playwright persistent context and
    ``classify_credential``.
    """
    edge = find_edge()
    if not edge:
        raise RuntimeError("Microsoft Edge was not found")
    print("=" * 60)
    print(f"Opening Microsoft Edge: {base_url}")
    print("Sign in in the window that pops up (skipped if already logged in).")
    print(f"Waiting up to {timeout_s}s for the session token...")
    print("=" * 60)

    probe = credential_probe or classify_credential
    expected_origin = _page_origin(base_url)

    def note(phase, origin=None, manual_reason=None, detail=None):
        if not on_progress:
            return
        try:
            on_progress({"phase": phase, "origin": origin,
                          "manual_reason": manual_reason, "detail": detail})
        except Exception:
            pass  # progress reporting must never break the session

    manager = None
    playwright = None
    if context_factory is None:
        from playwright.sync_api import sync_playwright  # lazy import
        manager = sync_playwright()
    try:
        if context_factory is not None:
            ctx = context_factory(edge, profile_dir)
        else:
            playwright = manager.start()
            ctx = playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                executable_path=edge,
                args=["--start-maximized"],
                no_viewport=True,
            )
        if on_context:
            on_context(ctx)
        try:
            page = _LoginPage(ctx.pages[0] if ctx.pages else ctx.new_page())
            try:
                page.bring_to_front()
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[warn] Failed to load page (may still be redirecting): {e}")

            deadline = time.time() + timeout_s
            rejected_tokens = set()
            clicks = {"openwebui": 0, "umpass": 0}
            stage_seen = {}
            manual_reason = None
            while time.time() < deadline:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Login cancelled because chat2api is stopping")
                try:
                    token = page.read_token()
                except Exception:
                    token = None
                if token and token not in rejected_tokens:
                    verdict = probe(base_url, token)[0]
                    if verdict == "valid":
                        print("\nSigned in, token acquired.")
                        return token
                    if verdict == "rejected":
                        # An explicit rejection is the only evidence that
                        # lets this session discard a token; an inconclusive
                        # probe (unknown) must not throw a good token away.
                        rejected_tokens.add(token)
                        try:
                            page.drop_token()
                            page.reload()
                        except Exception:
                            # A page mid-navigation must not fail the whole
                            # session: the token stays rejected either way.
                            pass
                        continue
                    # Inconclusive: the token stays, and the session stops
                    # acting on the page (nothing else can decide it here)
                    # while it keeps polling for a decisive verdict.
                    manual_reason = manual_reason or \
                        "credential_probe_inconclusive"

                url = page.url()
                host = _page_host(url)
                if url and _page_origin(url) == expected_origin:
                    stage = "openwebui"
                elif _host_matches(host, UMPASS_SSO_HOST):
                    stage = "umpass"
                elif any(_host_matches(host, h) for h in DUO_WAIT_HOSTS):
                    stage = "duo"
                else:
                    stage = "unknown"

                if stage == "duo":
                    # Auto-approved for a trusted device; a prompt that wants
                    # a code is the human's step. Never clicked either way.
                    stage_seen.setdefault(stage, time.time())
                    note("awaiting_duo", origin=url)
                elif stage == "unknown":
                    stage_seen.setdefault(stage, time.time())
                    manual_reason = manual_reason or "unrecognized_page"
                    note("manual_takeover", origin=url,
                         manual_reason=manual_reason)
                elif manual_reason is not None:
                    # The automatic flow already stopped (an inconclusive
                    # probe, a human-only page, a changed site): never act
                    # on a page again, only keep polling for the token.
                    pass
                elif stage == "openwebui":
                    if clicks[stage] >= LOGIN_MAX_CLICKS_PER_STAGE:
                        manual_reason = manual_reason or "openwebui_click_swallowed"
                        note("manual_takeover", origin=url,
                             manual_reason=manual_reason)
                    else:
                        target = page.one_visible(OPENWEBUI_SSO_BUTTON_SELECTORS)
                        if target is None:
                            settled = time.time() - stage_seen.setdefault(
                                stage, time.time())
                            if settled >= LOGIN_STAGE_SETTLE_TIMEOUT_S:
                                manual_reason = manual_reason or \
                                    "openwebui_button_unrecognized"
                                note("manual_takeover", origin=url,
                                     manual_reason=manual_reason)
                        else:
                            clicks[stage] += 1
                            outcome = _click_and_observe(
                                page, target, url, cancel_event)
                            if outcome == "cancelled":
                                raise RuntimeError(
                                    "Login cancelled because chat2api is stopping")
                            note("openwebui_clicked", origin=url)
                elif stage == "umpass":
                    if clicks[stage] >= LOGIN_MAX_CLICKS_PER_STAGE:
                        manual_reason = manual_reason or "umpass_click_swallowed"
                        note("manual_takeover", origin=url,
                             manual_reason=manual_reason)
                    elif page.any_visible(UMPASS_MFA_SELECTORS):
                        manual_reason = manual_reason or "umpass_mfa_required"
                        note("manual_takeover", origin=url,
                             manual_reason=manual_reason)
                    elif not page.field_filled(UMPASS_ACCOUNT_SELECTORS) or \
                            not page.field_filled(UMPASS_PASSWORD_SELECTORS):
                        manual_reason = manual_reason or "umpass_prefill_missing"
                        note("manual_takeover", origin=url,
                             manual_reason=manual_reason)
                    else:
                        target = page.one_visible(UMPASS_SUBMIT_SELECTORS)
                        if target is None:
                            settled = time.time() - stage_seen.setdefault(
                                stage, time.time())
                            if settled >= LOGIN_STAGE_SETTLE_TIMEOUT_S:
                                manual_reason = manual_reason or \
                                    "umpass_submit_unrecognized"
                                note("manual_takeover", origin=url,
                                     manual_reason=manual_reason)
                        else:
                            clicks[stage] += 1
                            outcome = _click_and_observe(
                                page, target, url, cancel_event)
                            if outcome == "cancelled":
                                raise RuntimeError(
                                    "Login cancelled because chat2api is stopping")
                            note("umpass_submitted", origin=url)

                page.wait(LOGIN_POLL_INTERVAL_MS)

            if manual_reason is None and stage == "duo":
                # The session ended while the visible window was still on the
                # Duo prompt: never guess whether Duo wanted a code - report
                # that the automatic flow was still waiting for it.
                manual_reason = "duo_prompt_waiting"
            raise LoginStalled(manual_reason or "timeout",
                               "last page %s" % (page.url() or "unknown"))
        finally:
            if on_context:
                on_context(None)
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        if playwright is not None:
            playwright.stop()


def fetch_api_key(base_url: str, token: str, upstream_config=None) -> str | None:
    """Best-effort: fetch the Open WebUI API key (long-lived, replaces the JWT).
    Returns None if the instance does not expose it. If /api/config explicitly
    says API keys are disabled, skip the endpoint entirely.
    """
    if _config_api_keys_enabled(upstream_config) is False:
        print("Open WebUI reports API keys disabled; using the session token.")
        return None
    for method in ("GET", "POST"):
        r = None
        try:
            r = requests.request(
                method, f"{base_url}/api/v1/auths/api_key",
                headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if r.status_code == 200:
                payload = r.json()
                key = payload.get("api_key") if isinstance(payload, dict) else None
                if key:
                    print("Got Open WebUI API key.")
                    return key
        except Exception:
            pass
        finally:
            if r is not None:
                r.close()
    return None


def authenticate(args) -> dict:
    """Return {'token': ..., 'api_key': ...}, opening the browser if needed.

    A13-2: the saved credential is judged by ``classify_credential``. Only an
    explicit upstream rejection or a locally missing credential opens the browser
    window; an inconclusive probe (network failure, 5xx, unusable 200 body) keeps
    the saved credential and says so, instead of pretending it expired.
    """
    store = TokenStore(TOKEN_FILE, args.base_url)
    saved = store.load()

    if args.token:
        return {"token": args.token, "api_key": None}

    if saved and saved.get("token"):
        bearer = saved.get("api_key") or saved["token"]
        verdict, reason, upstream_status = classify_credential(args.base_url,
                                                            bearer)
        if verdict == "valid":
            return {"token": saved["token"], "api_key": saved.get("api_key")}
        if verdict == "unknown":
            print(f"[warn] Could not verify the saved credential "
                  f"({reason or 'unknown'}"
                  f"{'' if upstream_status is None else ' HTTP %d' % upstream_status}"
                  f"); keeping it and NOT opening the sign-in window.")
            return {"token": saved["token"], "api_key": saved.get("api_key")}

    token = browser_login(args.base_url, args.profile, args.login_timeout)
    config = fetch_upstream_config(args.base_url) if args.use_api_key else None
    api_key = fetch_api_key(args.base_url, token, config) \
        if args.use_api_key else None
    store.save(token, api_key)
    return {"token": token, "api_key": api_key}


def load_service_credentials(args) -> dict:
    """Load credentials without delaying the local server for validation."""
    if args.token:
        return {"token": args.token, "api_key": None}
    saved = TokenStore(TOKEN_FILE, args.base_url).load()
    if not saved:
        return {}
    return {"token": saved.get("token"), "api_key": saved.get("api_key")}


def resolve_effort(requested: str):
    """Map a virtual model id (<base>-<Suffix>) to (base_model, effort|None)."""
    for base in EFFORT_MODELS:
        if requested == base:
            return base, None
        prefix = base + "-"
        if requested.startswith(prefix):
            suffix = requested[len(prefix):]
            if suffix in EFFORT_LEVELS:
                return base, EFFORT_LEVELS[suffix]
    return None, None


# ------------------------------------------------------------ usage accounting

# ------------------------------------------- verified share attribution (C2/C3)
# A share request is attributed to a key ONLY when the share entry point (8100)
# signed a per-request marker with the shared secret. The marker carries no
# credential material (the share Key's plaintext never leaves the client side and
# never reaches this process), and it is verified here, not trusted: a missing
# marker, a missing/unreadable shared key, a stale timestamp, a wrong signature
# or a replayed request id all record the request as UNATTRIBUTED. This process
# never guesses an owner and never falls back to a client-supplied header - any
# caller that can reach this loopback port can claim nothing it cannot sign.
ATTRIBUTION_VERSION = "1"
X_ATTRIBUTION_VERSION = "X-Share-Attribution-Version"
X_ATTRIBUTION_KEY_ID = "X-Share-Key-Id"
X_ATTRIBUTION_REQUEST_ID = "X-Share-Request-Id"
X_ATTRIBUTION_TS = "X-Share-Ts"
X_ATTRIBUTION_SIGNATURE = "X-Share-Signature"
ATTRIBUTION_HEADER_NAMES = (X_ATTRIBUTION_VERSION, X_ATTRIBUTION_KEY_ID,
                           X_ATTRIBUTION_REQUEST_ID, X_ATTRIBUTION_TS,
                           X_ATTRIBUTION_SIGNATURE)
# Same clock, same machine: this is a generous bound on clock skew, not a
# replay window (the one-time request-id registry below is the replay rule).
ATTRIBUTION_TS_WINDOW_S = 300
ATTRIBUTION_MAX_ENCODED_KEY_ID = 512
ATTRIBUTION_REQUEST_ID_CHARS = 32
ATTRIBUTION_KEY_MIN_BYTES = 32
# Bounded replay registry: verified request ids are remembered once each, and
# the oldest entries are evicted past this bound (never unbounded memory).
ATTRIBUTION_SEEN_MAX = 20000
_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
# The dedicated backend runs the SAME source with its own state paths
# (console_share_production._child_env); the deployment must point BOTH sides
# at one key file (8100: --attribution-key-file, this process: the env var).
# Repository default: <ai-gateway>/data/share-attribution.key (gitignored).
SHARE_ATTRIBUTION_KEY_FILE = (
    os.environ.get("CHAT2API_SHARE_ATTRIBUTION_KEY_FILE")
    or os.environ.get("SHARE_ATTRIBUTION_KEY_FILE")
    or os.path.join(HERE, os.pardir, "data", "share-attribution.key"))
# Price table identity stamped on each row (see _price_table_version); the
# cost itself is a READ-SIDE derivation, never computed here.
PRICE_TABLE_FILE = (os.environ.get("CHAT2API_PRICE_TABLE_FILE")
                    or os.path.join(HERE, os.pardir, "data", "prices.json"))

# Request-scoped attribution facts for record_usage. One request is served by one
# thread and the handler installs the facts BEFORE any ledger row can be written,
# ALWAYS with the full fact set (a request without a verified marker installs
# key_id=None), so a previous request can never leak its attribution into the next.
_request_facts = threading.local()
_attribution_seen = {}
_attribution_seen_lock = threading.Lock()
_attribution_key_cache = {"stat": None, "secret": None}
_price_version_cache = {"stat": None, "version": None}
_stat_cache_lock = threading.Lock()


def set_request_facts(protocol, key_id=None, request_id=None,
                      attribution="absent"):
    """Install THIS request's attribution facts for record_usage."""
    _request_facts.protocol = protocol
    _request_facts.key_id = key_id
    _request_facts.request_id = request_id
    _request_facts.attribution = attribution


def _request_fact(name):
    return getattr(_request_facts, name, None)


def _b64url_decode(value):
    """Strict canonical base64url decode; None when the value is not one.

    A non-canonical encoding is refused rather than normalised, so one key id has
    exactly ONE accepted wire form and the signature covers exactly what was sent.
    """
    if not isinstance(value, str) or not value \
            or len(value) > ATTRIBUTION_MAX_ENCODED_KEY_ID:
        return None
    if any(ch not in _B64URL_ALPHABET for ch in value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        return None
    return raw


def load_share_attribution_key(path=None):
    """Read the shared attribution secret; None on ANY failure (fail-closed).

    Cached by (path, mtime, size) so a rotating file is re-read without a stat
    per request. An unreadable, empty or too-short file yields None: every
    marker then fails verification and the request is recorded as unattributed.
    Never logged, never written anywhere.
    """
    target = path or SHARE_ATTRIBUTION_KEY_FILE
    try:
        info = os.stat(target)
        stamp = (target, info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    with _stat_cache_lock:
        if _attribution_key_cache["stat"] == stamp:
            return _attribution_key_cache["secret"]
    try:
        with open(target, encoding="utf-8") as handle:
            secret = handle.read().strip()
    except (OSError, UnicodeError, ValueError):
        return None
    if len(secret) < ATTRIBUTION_KEY_MIN_BYTES:
        secret = None
    with _stat_cache_lock:
        _attribution_key_cache["stat"] = stamp
        _attribution_key_cache["secret"] = secret
    return secret


def _attribution_first_seen(request_id):
    """Remember a VERIFIED request id ONCE; False when it was already seen."""
    with _attribution_seen_lock:
        if request_id in _attribution_seen:
            return False
        _attribution_seen[request_id] = True
        while len(_attribution_seen) > ATTRIBUTION_SEEN_MAX:
            _attribution_seen.pop(next(iter(_attribution_seen)))
        return True


def verify_share_attribution(headers, now=None):
    """Verify the share entry point's marker.

    Returns ``(key_id, request_id, reason)``; key_id and request_id are None
    unless EVERY check passes. Fail-closed: a missing marker ("absent"), a
    PARTIAL marker (only some of the five headers, "partial_headers"), a
    missing key file ("no_key"), a repeated same-name header, a wrong
    version, an out-of-window timestamp, a malformed key id, a wrong
    signature and a replayed request id all yield the unattributed pair with
    the reason recorded in the row's coverage - never a partial or guessed
    identity.
    """
    values = {name: headers.get_all(name, []) for name in
              ATTRIBUTION_HEADER_NAMES}
    if not any(values.values()):
        return None, None, "absent"
    if any(len(found) > 1 for found in values.values()):
        return None, None, "repeated_header"
    if any(not found for found in values.values()):
        # Some - but not all - marker headers are present: a foreign or
        # tampered client, never a production marker (the share gateway
        # emits all five or none). Fail closed with a distinct reason
        # instead of indexing into an empty list (which used to escape as
        # an unhandled IndexError).
        return None, None, "partial_headers"
    version = values[X_ATTRIBUTION_VERSION][0]
    encoded_key_id = values[X_ATTRIBUTION_KEY_ID][0]
    request_id = values[X_ATTRIBUTION_REQUEST_ID][0]
    ts_raw = values[X_ATTRIBUTION_TS][0]
    signature = values[X_ATTRIBUTION_SIGNATURE][0]
    if version != ATTRIBUTION_VERSION:
        return None, None, "bad_version"
    if len(request_id) != ATTRIBUTION_REQUEST_ID_CHARS \
            or any(ch not in "0123456789abcdef" for ch in request_id):
        return None, None, "bad_request_id"
    secret = load_share_attribution_key()
    if not secret:
        return None, None, "no_key"
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return None, None, "bad_ts"
    current = int(time.time() if now is None else now)
    if abs(current - ts) > ATTRIBUTION_TS_WINDOW_S:
        return None, None, "ts_out_of_window"
    signed = "\n".join((version, encoded_key_id, request_id, ts_raw))
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None, None, "bad_signature"
    decoded = _b64url_decode(encoded_key_id)
    if decoded is None:
        return None, None, "bad_key_id"
    try:
        key_id = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "bad_key_id"
    if not key_id or _attribution_first_seen(request_id) is False:
        return None, None, "replayed"
    return key_id, request_id, "verified"


def _price_table_version():
    """Identity of the price table present when a row is written; None if unknown.

    Records WHICH price table content was in effect, not that this row's cost was
    computed under it - the cost is a read-side derivation (share_metering with
    the record's own time), so a later reader can tell rows written under
    different tables apart. An explicit version field wins (the versioned price
    structure); otherwise the file's content digest is used. Any failure yields
    None - never a guessed or fabricated version.
    """
    try:
        info = os.stat(PRICE_TABLE_FILE)
        stamp = (PRICE_TABLE_FILE, info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    with _stat_cache_lock:
        if _price_version_cache["stat"] == stamp:
            return _price_version_cache["version"]
    version = None
    try:
        with open(PRICE_TABLE_FILE, "rb") as handle:
            payload = handle.read()
        explicit = json.loads(payload.decode("utf-8")).get("version")
        if isinstance(explicit, (str, int)) and not isinstance(explicit, bool) \
                and str(explicit).strip():
            version = str(explicit).strip()
        else:
            version = hashlib.sha256(payload).hexdigest()[:16]
    except (OSError, UnicodeError, ValueError, AttributeError):
        version = None
    with _stat_cache_lock:
        _price_version_cache["stat"] = stamp
        _price_version_cache["version"] = version
    return version


def _est_tokens(text) -> int:
    """Rough token estimate: CJK chars count ~1 token each, the rest ~4 chars/token."""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, cjk + (len(text) - cjk) // 4)


def _message_tokens(messages) -> int:
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):  # multimodal parts; count text only
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict))
        total += _est_tokens(content or "") + 4
    return total


# ---------------------------------------------------------------------------
# Usage normalization (W5-2): one narrow boundary between the upstream usage
# shapes and the ledger row. Every producer funnels its usage block through
# these helpers; the read ends (share_metering / console_stats / usagesrv)
# keep consuming the SAME ledger row shape.
#
# A trusted token count is a non-bool, non-negative int (_reported_tokens).
# A trusted ZERO is a count and never falls back to the local estimate; a
# field the upstream did not report - or reported with an illegal value
# (bool/negative/str/float/non-finite) - stays ABSENT, so the estimate
# fallback applies with the honest coverage label instead of fabricated
# upstream evidence.
#
# Both the canonical OpenAI names (prompt_tokens/completion_tokens) and the
# newer aliases (input_tokens/output_tokens) name the SAME sides. When both
# spellings of one side carry legal values, the canonical OpenAI name wins
# with a FIXED priority; the sides are never summed.
#
# The Anthropic translation (_anthropic_usage_fields) runs BEFORE this
# boundary, so an alias here is only an OpenAI-side alias and is never
# re-interpreted as Anthropic's cache-excluding input_tokens; the cache and
# reasoning details remain subsets of their sides (readers subtract them).

def _usage_side_tokens(usage, canonical, alias):
    """Trusted value of one usage side; the canonical spelling wins."""
    value = _reported_tokens(usage.get(canonical))
    if value is None:
        value = _reported_tokens(usage.get(alias))
    return value


def _normalized_usage_fields(usage):
    """Trusted OpenAI-shaped token fields of one usage block; {} when none.

    Only fields reported with a legal count appear: prompt_tokens /
    completion_tokens (canonical; input_tokens / output_tokens as fallback
    aliases for the same side), total_tokens, and the nested details
    prompt_tokens_details.cached_tokens / completion_tokens_details.
    reasoning_tokens.  The output IS an accepted input shape, so a second pass
    is lossless (the stream path normalizes, merges, normalizes); illegal
    values dropped, trusted zeros kept, nothing invented.
    """
    if not isinstance(usage, dict):
        return {}
    fields = {}
    prompt = _usage_side_tokens(usage, "prompt_tokens", "input_tokens")
    completion = _usage_side_tokens(usage, "completion_tokens", "output_tokens")
    if prompt is not None:
        fields["prompt_tokens"] = prompt
    if completion is not None:
        fields["completion_tokens"] = completion
    total = _reported_tokens(usage.get("total_tokens"))
    if total is not None:
        fields["total_tokens"] = total
    for details_key, field_name in (
            ("prompt_tokens_details", "cached_tokens"),
            ("completion_tokens_details", "reasoning_tokens")):
        details = usage.get(details_key) or {}
        value = (_reported_tokens(details.get(field_name))
                 if isinstance(details, dict) else None)
        if value is not None:
            fields[details_key] = {field_name: value}
    return fields


def _merge_usage_snapshots(base, update):
    """Merge cumulative usage snapshots; the LATEST legal value per field wins.

    Every current producer reports running totals per field (OpenAI
    include_usage's final chunk, UM's unified usage merge, the Anthropic
    counters), so values are never summed and a later event that omits a
    field never erases the earlier report. ``base`` may be None (nothing
    observed yet); an update without any trusted field leaves ``base``
    unchanged. Returns the merged OpenAI-shaped dict, or None when neither
    snapshot carries a trusted field.

    One consequence is pinned by the W5-2 contract: a trusted ZERO is
    evidence, so latest-wins means a mid-stream all-zero placeholder block
    that is never followed by a real counter would become the final snapshot
    and the row would record zero consumption instead of the local estimate.
    No current producer emits placeholder zeros (every observed block is a
    running total), and the boundary deliberately trusts the upstream count
    over the guess - but that is the trade an incremental or placeholder
    producer would be exercising, and it is why such a producer would need
    its own merge rule rather than silently reusing this one.
    """
    merged = _normalized_usage_fields(base)
    # Each detail block carries exactly ONE count (the normalization output
    # shape), so the whole-block update below is already per-key: a later
    # snapshot that omits a detail never erases the earlier report of it.
    merged.update(_normalized_usage_fields(update))
    return merged or None


def record_usage(model, stream, started_at, usage, in_est, out_est, status="ok",
                 usage_source=None):
    """Append one JSONL line; best-effort, never breaks proxying.

    status distinguishes a completed response ("ok") from cancelled and
    upstream-failed streams, whose token counts are lower-bound estimates of
    what the upstream actually consumed.

    ``usage_source`` is the coverage of the token counts and is written as a
    ADDITIVE field (the fields above keep their exact previous meaning):
    "upstream" (the upstream reported both sides), "partial" (the upstream
    reported only one side and the other is a local estimate), "estimated"
    (no trusted upstream token evidence but local estimates exist) or "none"
    (no token evidence at all, so the counts are 0 because nothing is known,
    not because nothing was used). When it is None it is derived from the
    normalized usage fields: a trusted ZERO counts as reported evidence.

    The attribution fields are additive too and are read from THIS request's
    verified facts: key_id/request_id are set ONLY for a verified share marker
    (see verify_share_attribution) and stay null for every other request, so an
    unattributed request is never guessed onto a key. No field ever carries
    credential material.
    """
    global _usage_disabled
    if _usage_disabled:
        return
    fields = _normalized_usage_fields(usage)
    prompt = fields.get("prompt_tokens")
    completion = fields.get("completion_tokens")
    if usage_source is None:
        if prompt is not None and completion is not None:
            usage_source = "upstream"
        elif fields:
            usage_source = "partial"
        elif in_est or out_est:
            usage_source = "estimated"
        else:
            usage_source = "none"
    input_tokens = prompt if prompt is not None else in_est
    output_tokens = completion if completion is not None else out_est
    total_tokens = fields.get("total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    rec = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": "chat2api",
        "model": model,
        "stream": stream,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": (fields.get("prompt_tokens_details")
                                or {}).get("cached_tokens", 0),
        "reasoning_tokens": (fields.get("completion_tokens_details")
                             or {}).get("reasoning_tokens", 0),
        "total_tokens": total_tokens,
        # "estimated" keeps its meaning: no trusted upstream token evidence
        # at all (the counts are local estimates), not merely an absent
        # canonical spelling - an alias-only block or a junk dict is NOT a
        # fabrication.
        "estimated": not fields,
        "duration_ms": int((time.time() - started_at) * 1000),
        "usage_source": usage_source,
        "protocol": _request_fact("protocol"),
        "key_id": _request_fact("key_id"),
        "request_id": _request_fact("request_id"),
        "attribution": _request_fact("attribution") or "absent",
        "price_version": _price_table_version(),
    }
    try:
        path = os.path.abspath(USAGE_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _usage_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        _usage_disabled = True
        print("[chat2api] usage log write failed, accounting disabled", file=sys.stderr)


# ------------------------------------- Anthropic Messages usage (C3)
# The Anthropic protocol reports its usage in its OWN shape and its OWN event
# order: the input side arrives in message_start and the output side in
# message_delta, while a non-stream message carries one complete usage block.
# These helpers only TRANSLATE the reported numbers into the fields record_usage
# already reads (the ledger keeps ONE row shape for both protocols) - they never
# invent a count. A field the upstream did not report stays absent, which is what
# lets an interrupted stream record a known lower bound instead of a zero.

def _reported_tokens(value):
    """Non-negative int the upstream reported; None when it reported nothing."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _anthropic_usage_fields(usage, *, input_side=True, output_side=True):
    """Translate an Anthropic ``usage`` block; only reported fields appear.

    Anthropic counts the input side in ``input_tokens`` WITHOUT cache reads and
    cache writes, while the ledger's ``prompt_tokens`` INCLUDES the cached
    subset (the aggregator subtracts it). Cache reads therefore become the cached
    subset, and cache WRITES stay part of the non-cached input - they were fresh
    input processing, not a cache hit.
    """
    if not isinstance(usage, dict):
        return {}
    fields = {}
    if input_side:
        input_tokens = _reported_tokens(usage.get("input_tokens"))
        cache_read = _reported_tokens(usage.get("cache_read_input_tokens"))
        cache_write = _reported_tokens(usage.get("cache_creation_input_tokens"))
        if input_tokens is not None or cache_read is not None \
                or cache_write is not None:
            fields["prompt_tokens"] = ((input_tokens or 0) + (cache_read or 0)
                                      + (cache_write or 0))
        if cache_read is not None:
            fields["prompt_tokens_details"] = {"cached_tokens": cache_read}
    if output_side:
        output_tokens = _reported_tokens(usage.get("output_tokens"))
        if output_tokens is not None:
            fields["completion_tokens"] = output_tokens
    return fields


def _anthropic_message_usage(payload):
    """Complete usage of a non-stream Anthropic message; None when absent.

    A 200 body that is not an Anthropic message (or carries no usage block)
    yields None: the row then keeps the local estimate with the explicit
    "estimated" coverage instead of a fabricated zero.
    """
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    return _anthropic_usage_fields(payload.get("usage")) or None


def _anthropic_output_estimate(payload):
    """Local output-token estimate from a non-stream message body.

    Only the text blocks are counted (tool-call arguments are not text); an
    unreadable body yields 0, which keeps the row's coverage explicit.
    """
    if not isinstance(payload, dict):
        return 0
    content = payload.get("content")
    if not isinstance(content, list):
        return 0
    text = "".join(block.get("text") or "" for block in content
                   if isinstance(block, dict) and block.get("type") == "text")
    return _est_tokens(text) if text else 0


class _AnthropicStreamUsage:
    """Accumulate the usage an Anthropic SSE stream reports (cumulative).

    ``message_start`` carries the input side, ``message_delta`` the output side;
    both are cumulative counters, so the LATEST value per field wins. Only the
    side each event actually reports is read: ``message_start`` reports
    output_tokens as 0 before any output exists, and taking that as a real zero
    would record a fabricated count where the truth is still unknown.

    ONE accumulator spans the whole logical request, including a transparent
    pre-commit retry: the row records one logical request, and its counts are
    then the terminal attempt's cumulative values - a KNOWN LOWER BOUND of what
    the request consumed (an earlier, discarded attempt's own consumption is not
    added to it).
    """

    def __init__(self):
        self._fields = {}
        self.saw_input = False
        self.saw_output = False
        self.output_chars = 0

    def observe(self, event):
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            fields = _anthropic_usage_fields(usage, output_side=False)
            self.saw_input = bool(fields)
        elif event_type == "message_delta":
            fields = _anthropic_usage_fields(event.get("usage"),
                                            input_side=False)
            if "completion_tokens" in fields:
                self.saw_output = True
        else:
            return
        for name, value in fields.items():
            if name == "prompt_tokens_details":
                current = self._fields.get(name) or {}
                self._fields[name] = {
                    "cached_tokens": max(current.get("cached_tokens", 0),
                                         value["cached_tokens"])}
            else:
                self._fields[name] = max(self._fields.get(name, 0), value)

    def observe_text(self, piece):
        """Count the model output the stream carried (a known lower bound)."""
        if isinstance(piece, str) and piece:
            self.output_chars += len(piece)

    def usage(self):
        """The accumulated usage block, or None when nothing was reported."""
        return dict(self._fields) if self._fields else None

    def source(self):
        """Coverage of the accumulated counts (record_usage's usage_source)."""
        if self.saw_input and self.saw_output:
            return "upstream"
        if self._fields:
            return "partial"
        return None

    def output_estimate(self):
        """Local output-token estimate of the output seen so far."""
        return self.output_chars // 4


# ---------------------------------------------------------------- HTTP server

AUTH_LABELS = {
    "valid": "认证有效",
    "needs_login": "需要登录",
    "logging_in": "登录中",
    "failed": "登录失败",
}


class ReauthenticationRequired(RuntimeError):
    """Raised when an upstream request cannot recover its authentication."""


class ReauthenticationThrottled(ReauthenticationRequired):
    """A reauthentication session was refused BEFORE it started.

    Deliberately distinct from ReauthenticationRequired: the cross-instance
    lease or the rolling attempt cap refused a new submission window, so this
    process never judged the credential and must not report an authentication
    failure.
    """

    def __init__(self, reason: str):
        super().__init__("reauthentication refused: %s" % reason)
        self.reason = reason


def is_local_control_request(client_ip: str, host: str, origin: str | None) -> bool:
    """Allow CLI management calls while rejecting remote and browser requests."""
    try:
        if not ipaddress.ip_address(client_ip).is_loopback:
            return False
    except ValueError:
        return False
    if origin:
        return False
    try:
        hostname = urllib.parse.urlsplit(f"//{host}").hostname or ""
    except ValueError:
        return False
    hostname = hostname.lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def ensure_control_token(path: str) -> str | None:
    """Load the local control token, creating it on first use.

    Returns None when the token cannot be read or created; callers must treat
    that as "deny" and never fall back to an unauthenticated state.
    """
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    except OSError:
        return None
    value = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    except OSError:
        return None
    # Best effort: keep only the running user and SYSTEM (the file inherits
    # broad repo ACLs otherwise). Failure is acceptable because the boundary
    # that matters for the share identity is the repo-root deny ACE.
    try:
        user = os.environ.get("USERNAME") or "Administrators"
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{user}:F", "*S-1-5-18:F"],
            check=False, capture_output=True)
    except OSError:
        pass
    return value


class Proxy:
    """Holds credentials and forwards requests to the Open WebUI backend."""

    def __init__(self, creds: dict, args):
        self.creds = creds or {}
        self.base_url = args.base_url
        self.models_cache = {"at": 0, "data": None}
        self.upstream_config = None
        self._config_lock = threading.Lock()
        # W5-3 freshness of the PUBLIC /api/config snapshot. Owned by this
        # lock; the network call of a refresh NEVER runs while it is held
        # (readers stay memory-only and must be served during a slow fetch).
        # All epochs are time.time() epoch seconds:
        #   checked_epoch  - last SUCCESSFUL observation (data valid since)
        #   attempt_epoch  - last refresh attempt (success or failure)
        #   refreshing     - a refresh is currently in flight (single-flight)
        #   last_failure   - kind of the last failed attempt, or None
        self._config_freshness = {
            "checked_epoch": None,
            "attempt_epoch": None,
            "refreshing": False,
            "last_failure": None,
        }
        self.args = args
        self._auth_condition = threading.Condition()
        self._auth_state = "valid" if self.bearer() else "needs_login"
        self._auth_error = None
        self._auth_thread = None
        self._browser_context = None
        self._shutdown_event = threading.Event()
        # The maintenance-loop thread (auth watch); kept so shutdown() can
        # wait for the current bounded round instead of leaving a daemon
        # mid-refresh when the process exits.
        self._watch_thread = None
        # A9-6/§8.4 additive watch visibility: what the periodic check
        # actually did. ``_watch_last_epoch``/``_watch_last_outcome``/
        # ``_watch_rounds`` are written by run_auth_watch_round() when a
        # round finishes; ``_watch_interval_s`` is the backoff interval that
        # gates the NEXT round, published by the loop after each round. All
        # four are protected by _auth_condition. A never-run watch reads as
        # null/0 instead of pretending to be fresh.
        self._watch_last_epoch = None
        self._watch_last_outcome = None
        self._watch_rounds = 0
        self._watch_interval_s = AUTH_WATCH_INTERVAL_S
        # A13-2/A13-3 evidence. ``_auth_evidence`` is the last
        # classification this process actually observed (verdict, reason,
        # checked epoch and the last epoch a probe accepted the credential);
        # ``_auth_login`` is the automatic session's progress, including the
        # reason it stopped and left the window to a human; ``_auth_gate`` is
        # the last cross-instance lease decision. All three are additive: the
        # state/label/error contract the console reads is unchanged.
        self._auth_evidence = {
            "verdict": None,
            "reason": None,
            "upstream_status": None,
            "checked_epoch": None,
            "last_valid_epoch": None,
            "source": None,
        }
        self._auth_login = None
        self._auth_gate = None
        # Monotonic session generation: a session whose generation was
        # superseded (shutdown) discards its late result instead of writing
        # credentials or state over a newer decision.
        self._auth_generation = 0
        # Reachability is deliberately independent from authentication.  Only
        # this lock-protected state is mutated by _record_upstream_reachability;
        # readers always receive a copied snapshot.
        self._upstream_state_lock = threading.Lock()
        now = time.time()
        self._upstream_state = {
            "state": "unknown",
            "reason": "not_probed",
            "upstream_status": None,
            "checked_epoch": None,
            "changed_epoch": now,
            "outage_started_epoch": None,
            "last_ok_epoch": None,
        }

    def bearer(self) -> str:
        # Prefer the API key only while the probed Open WebUI config allows it.
        # This also makes an old stored API key harmless if the administrator
        # later disables API keys: the still-valid session JWT is used instead.
        with self._config_lock:
            api_keys_enabled = _config_api_keys_enabled(self.upstream_config)
        if api_keys_enabled is False:
            return self.creds.get("token") or ""
        return self.creds.get("api_key") or self.creds.get("token") or ""

    def auth_status(self) -> dict:
        """Console-visible authentication state (state/label/error + evidence).

        ``state``/``label``/``error`` keep their existing meanings (the label
        stays within ``AUTH_LABELS``), so existing consumers are unaffected.
        The additive ``evidence``/``login``/``gate`` keys carry what this
        process actually observed: an inconclusive probe says so instead of
        masquerading as an expiry, and a session that stopped for a human names
        the reason. The additive ``watch`` key carries the auth-watch thread's
        liveness and last round (§8.4).
        """
        with self._auth_condition:
            state = self._auth_state
            evidence = dict(self._auth_evidence)
            login = dict(self._auth_login) if self._auth_login else None
            gate = dict(self._auth_gate) if self._auth_gate else None
            error = self._auth_error
        return {
            "state": state,
            "label": self._auth_label(state, evidence, login),
            "error": error,
            "evidence": evidence,
            "login": login,
            "gate": gate,
            "watch": self._watch_status(),
        }

    def _raise_auth_required(self):
        """Raise the honest exception for the current authentication failure.

        A session refused by the cross-instance lease or the rolling attempt cap
        never judged the credential, so it must not be reported as an
        authentication failure: ReauthenticationThrottled says exactly that and,
        being a ReauthenticationRequired subclass, keeps every existing handler
        working unchanged.
        """
        error = self.auth_status()["error"] or ""
        if error.startswith("reauth_refused:"):
            raise ReauthenticationThrottled(error.split(":", 1)[1])
        raise ReauthenticationRequired(error)

    @staticmethod
    def _auth_label(state, evidence, login):
        label = AUTH_LABELS[state]
        if login and login.get("manual_reason"):
            return "%s（需人工：%s）" % (label, login["manual_reason"])
        if evidence.get("verdict") == "unknown":
            # The last check could not decide; say so instead of letting
            # the label read as if the credential had just been confirmed.
            return "%s（最近一次检查无法判断：%s）" % (
                label, evidence.get("reason") or "unknown")
        return label

    def _watch_status(self) -> dict:
        """Additive visibility for the auth-watch thread (§8.4).

        ``thread_alive`` is the honest liveness of the maintenance thread
        (False before ``begin_auth_watch()`` ever ran). The last-round fields
        are written by ``run_auth_watch_round()`` and read as null/0 until a
        round actually finished; ``next_round_due`` is the last round's epoch
        plus the backoff interval currently gating the next round (null while
        there is no round to project from). Carries no credential and no round
        content — only liveness and the last check time.
        """
        with self._auth_condition:
            thread = self._watch_thread
            last_epoch = self._watch_last_epoch
            last_outcome = self._watch_last_outcome
            rounds = self._watch_rounds
            interval = self._watch_interval_s
        return {
            "thread_alive": thread is not None and thread.is_alive(),
            "last_round_epoch": last_epoch,
            "last_round_outcome": last_outcome,
            "next_round_due": (last_epoch + interval
                               if last_epoch is not None else None),
            "rounds": rounds,
        }

    def config_summary(self) -> dict:
        """Read-only memory summary of the public upstream config.

        The four established keys keep their exact meaning. The freshness
        keys are ADDITIVE and carry what the refresh actually observed, in
        time.time() epoch seconds (None = never observed): they are the
        observation moments, never the moment this summary was read.
        """
        with self._config_lock:
            config = dict(self.upstream_config or {})
            freshness = dict(self._config_freshness)
            failure = (dict(freshness["last_failure"])
                       if freshness["last_failure"] else None)
        features = config.get("features") or {}
        oauth = config.get("oauth") or {}
        providers = oauth.get("providers") or {}
        return {
            "name": config.get("name"),
            "version": config.get("version"),
            "enable_api_keys": features.get("enable_api_keys"),
            "oauth_providers": sorted(providers) if isinstance(providers, dict) else [],
            # W5-3 additive freshness (epoch seconds; see __init__):
            "config_checked_epoch": freshness["checked_epoch"],
            "config_attempt_epoch": freshness["attempt_epoch"],
            "config_refreshing": freshness["refreshing"],
            "config_last_failure": failure,
        }

    def upstream_reachability_snapshot(self) -> dict:
        with self._upstream_state_lock:
            return dict(self._upstream_state)

    def _record_upstream_reachability(self, state, reason=None,
                                      upstream_status=None,
                                      only_if_unobserved=False):
        """Record one side-effect-free observation of upstream reachability.

        ``ok`` means an upstream HTTP response was observed, regardless of
        authentication or application status. ``unreachable`` is reserved for
        transport timeout/network failure. ``unknown`` means this observation
        could not test reachability at all (for example no local credential).
        """
        if state not in {"ok", "unreachable", "unknown"}:
            raise ValueError(f"invalid upstream reachability state: {state}")
        now = time.time()
        with self._upstream_state_lock:
            if (only_if_unobserved and
                    self._upstream_state["checked_epoch"] is not None):
                return
            previous = self._upstream_state["state"]
            had_observation = self._upstream_state["checked_epoch"] is not None
            changed = previous != state
            outage_started = self._upstream_state.get("outage_started_epoch")
            if state == "unreachable" and outage_started is None:
                outage_started = now
            if state == "ok":
                last_ok = now
            else:
                last_ok = self._upstream_state.get("last_ok_epoch")
            changed_epoch = (now if changed
                             else self._upstream_state["changed_epoch"])
            self._upstream_state = {
                "state": state,
                "reason": reason,
                "upstream_status": upstream_status,
                "checked_epoch": now,
                "changed_epoch": changed_epoch,
                "outage_started_epoch": outage_started,
                "last_ok_epoch": last_ok,
            }

            # Preserve an outage start across an inconclusive observation so a
            # later confirmed recovery reports the full interval.
            recovered_from = outage_started if state == "ok" else None
            if state == "ok":
                self._upstream_state["outage_started_epoch"] = None

        if not changed or not had_observation:
            return
        if state == "unreachable":
            _logger.warning(
                "upstream reachability changed from=%s to=unreachable "
                "outage_started_epoch=%.3f reason=%s upstream_status=%s",
                previous, outage_started, reason or "-",
                "-" if upstream_status is None else upstream_status)
        elif state == "ok" and recovered_from is not None:
            _logger.warning(
                "upstream reachability recovered from=%s to=ok "
                "outage_started_epoch=%.3f recovered_epoch=%.3f "
                "duration_s=%.3f reason=%s upstream_status=%s",
                previous, recovered_from, now, max(0.0, now - recovered_from),
                reason or "-",
                "-" if upstream_status is None else upstream_status)
        else:
            _logger.warning(
                "upstream reachability changed from=%s to=%s "
                "changed_epoch=%.3f reason=%s upstream_status=%s",
                previous, state, now, reason or "-",
                "-" if upstream_status is None else upstream_status)

    @staticmethod
    def _reachability_from_credential_probe(verdict, reason, upstream_status):
        if reason in {"timeout", "network_error"}:
            return "unreachable"
        if verdict == "needs_login":
            return "unknown"
        # Every other classify_credential result was derived from an HTTP
        # response, including 401/403/5xx and malformed application payloads.
        if upstream_status is not None:
            return "ok"
        return "unknown"

    def _fetch_config_observed(self):
        """One bounded /api/config observation (network happens HERE only)."""
        return fetch_upstream_config_observed(
            self.base_url, timeout=CONFIG_REFRESH_TIMEOUT_S)

    def _record_config_refresh(self, config, failure_reason=None,
                               upstream_status=None):
        """Fold one config observation into the freshness state.

        A success updates the data AND the checked epoch and clears the
        failure evidence; a failure keeps the previous valid config and its
        success epoch untouched and records the failure kind instead. Called
        with ``_config_lock`` HELD (the network call never runs there).
        """
        now = time.time()
        freshness = self._config_freshness
        freshness["attempt_epoch"] = now
        if config is not None:
            self.upstream_config = config
            freshness["checked_epoch"] = now
            freshness["last_failure"] = None
        else:
            freshness["last_failure"] = {
                "reason": failure_reason,
                "at_epoch": now,
                "upstream_status": upstream_status,
            }

    def _maybe_refresh_config(self):
        """TTL-gated, single-flight refresh of the public config snapshot.

        Runs ONLY from the existing auth-watch maintenance loop (no new
        timer or thread). The gate reads the freshness state under
        ``_config_lock``; the network call runs OUTSIDE it, so /health and
        UI polls are served from memory during a slow fetch. A failed
        attempt keeps the previous valid config, records its kind and is
        retried no sooner than CONFIG_REFRESH_BACKOFF_S after the attempt
        (at the defaults: the same cadence as a successful refresh, never
        every watch round).
        """
        if self._shutdown_event.is_set():
            return
        now = time.time()
        with self._config_lock:
            freshness = self._config_freshness
            if freshness["refreshing"]:
                return  # single-flight: one network fetch at a time
            checked = freshness["checked_epoch"]
            if checked is not None and now - checked < CONFIG_REFRESH_TTL_S:
                return  # still fresh: the summary stays memory-only
            failure = freshness["last_failure"]
            if (failure is not None and
                    now - freshness["attempt_epoch"] <
                    CONFIG_REFRESH_BACKOFF_S):
                return  # failure backoff: do not hammer a failing upstream
            freshness["refreshing"] = True
            previous_version = (self.upstream_config.get("version")
                                if isinstance(self.upstream_config, dict)
                                else None)
        try:
            config, failure_reason, upstream_status = \
                self._fetch_config_observed()
        except Exception as exc:  # the maintenance loop must never die here
            config, failure_reason, upstream_status = \
                None, type(exc).__name__, None
        finally:
            with self._config_lock:
                self._config_freshness["refreshing"] = False
                self._record_config_refresh(config, failure_reason,
                                            upstream_status)
        if config is None:
            # Same conservative reachability rule as probe_config: a config
            # fetch failure cannot honestly classify reachability.
            self._record_upstream_reachability(
                "unknown", reason="config_probe_failed",
                only_if_unobserved=True)
            _logger.warning(
                "upstream config refresh failed reason=%s upstream_status=%s",
                failure_reason or "-",
                "-" if upstream_status is None else upstream_status)
            return
        self._record_upstream_reachability(
            "ok", reason="config_probe_ok", upstream_status=upstream_status)
        if config.get("version") != previous_version:
            # Only a VERSION CHANGE is worth a log line at maintenance
            # cadence; identical snapshots stay silent.
            summary = self.config_summary()
            _logger.info(
                "upstream config refreshed: version=%s api_keys=%s "
                "oauth_providers=%d", summary.get("version"),
                summary.get("enable_api_keys"),
                len(summary.get("oauth_providers") or []))

    def probe_config(self):
        """Immediate public-config probe (startup / post-login flows).

        Single-flight spans the entry points: when a maintenance refresh is
        already in flight, this probe does NOT issue a second concurrent
        /api/config GET - the in-flight fetch folds its own result under
        ``_config_lock``, so the last observed snapshot answers instead
        (None before anything was observed).
        """
        with self._config_lock:
            if self._config_freshness["refreshing"]:
                return (self.upstream_config
                        if isinstance(self.upstream_config, dict) else None)
            self._config_freshness["refreshing"] = True
        try:
            config, failure_reason, upstream_status = \
                self._fetch_config_observed()
        finally:
            with self._config_lock:
                self._config_freshness["refreshing"] = False
        if config is None:
            # A config fetch failure cannot honestly call the upstream
            # unreachable; leave that distinction to /ready's credential
            # probe. The failure kind is still recorded as config evidence.
            self._record_upstream_reachability(
                "unknown", reason="config_probe_failed", only_if_unobserved=True)
            with self._config_lock:
                self._record_config_refresh(None, failure_reason,
                                            upstream_status)
            _logger.warning("upstream /api/config probe failed reason=%s",
                            failure_reason or "-")
            return None
        self._record_upstream_reachability(
            "ok", reason="config_probe_ok", upstream_status=upstream_status)
        with self._config_lock:
            self._record_config_refresh(config, failure_reason, upstream_status)
        summary = self.config_summary()
        _logger.info(
            "upstream config detected: version=%s api_keys=%s oauth_providers=%d",
            summary.get("version"), summary.get("enable_api_keys"),
            len(summary.get("oauth_providers") or []))
        return config

    def health_snapshot(self) -> dict:
        status = self.auth_status()
        # /health needs no control token, so it carries only the coarse state
        # (state/label): the evidence/login/gate detail - including the last
        # login page and the lease owner - stays behind /admin/auth/status,
        # where the control token is required.
        cached = self.models_cache.get("data")
        return {
            "status": "ok",
            "chat2api_version": CHAT2API_VERSION,
            "chat2api_build": RUNTIME_BUILD_ID,
            "backend": self.base_url,
            "models": len(cached.get("data", [])) if cached else 0,
            "authentication": {"state": status["state"],
                               "label": status["label"]},
            # §8.4 additive: the auth-watch thread's liveness and last
            # round only — no credential, no round content.
            "watch": status["watch"],
            "upstream": self.config_summary(),
            "upstream_reachability": self.upstream_reachability_snapshot(),
            "docs": "POST /v1/chat/completions",
        }

    def readiness_snapshot(self):
        """Side-effect-free readiness: current bearer must fetch a valid catalog.

        Unlike proxy.call(), this deliberately never opens a browser or changes
        auth state; monitoring a readiness endpoint must not create interactive
        login popups.
        """
        verdict, reason, upstream_status = classify_credential(
            self.base_url, self.bearer())
        self._record_upstream_reachability(
            self._reachability_from_credential_probe(
                verdict, reason, upstream_status),
            reason=reason, upstream_status=upstream_status)
        ready = verdict == "valid"
        return ready, {
            "status": "ready" if ready else "not_ready",
            "authentication": verdict,
            "reason": reason,
            "upstream_status": upstream_status,
            "upstream": self.config_summary(),
            "upstream_reachability": self.upstream_reachability_snapshot(),
        }

    def _set_browser_context(self, context):
        with self._auth_condition:
            self._browser_context = context

    def _record_auth_evidence(self, verdict, reason, upstream_status, source):
        """Record one classification observation, never a credential."""
        now = time.time()
        with self._auth_condition:
            evidence = dict(self._auth_evidence)
            evidence.update({
                "verdict": verdict,
                "reason": reason,
                "upstream_status": upstream_status,
                "checked_epoch": now,
                "source": source,
            })
            if verdict == "valid":
                evidence["last_valid_epoch"] = now
            self._auth_evidence = evidence

    def _note_login_progress(self, progress):
        """Record the session's phase (and the reason a human is needed).

        The page URL is reduced to origin+path before it is stored: a login
        redirect can carry a one-time SSO code or SAML artifact in its query
        string, and this value is also served on the unauthenticated /health.
        """
        with self._auth_condition:
            progress = dict(progress)
            origin = progress.get("origin")
            if isinstance(origin, str) and origin:
                parsed = urllib.parse.urlsplit(origin)
                progress["origin"] = urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "", ""))
            self._auth_login = progress
            if progress.get("manual_reason"):
                self._auth_error = "manual_takeover:%s" % progress["manual_reason"]

    def _session_is_current(self, generation):
        with self._auth_condition:
            return self._auth_generation == generation

    def _perform_refresh(self, generation):
        success = False
        try:
            token = browser_login(
                self.base_url,
                self.args.profile,
                self.args.login_timeout,
                on_context=self._set_browser_context,
                cancel_event=self._shutdown_event,
                on_progress=self._note_login_progress,
            )
            if not self._session_is_current(generation):
                # Late result: shutdown superseded this session, so its token
                # must not overwrite the newer state or the token file.
                with self._auth_condition:
                    self._auth_state = "failed"
                    self._auth_error = "superseded"
                _logger.warning(
                    "discarded a late reauthentication result generation=%s",
                    generation)
                return
            config = self.probe_config() if self.args.use_api_key else None
            api_key = fetch_api_key(self.base_url, token, config) \
                if self.args.use_api_key else None
            TokenStore(TOKEN_FILE, self.base_url).save(token, api_key)
            with self._auth_condition:
                self.creds = {"token": token, "api_key": api_key}
                self.models_cache = {"at": 0, "data": None}
                self._auth_state = "valid"
                self._auth_error = None
                self._auth_login = None
            success = True
            _logger.info("authentication state changed to valid")
        except Exception as exc:
            error_type = type(exc).__name__
            reason = getattr(exc, "reason", None) or error_type
            with self._auth_condition:
                self._auth_state = "failed"
                self._auth_error = reason
            _logger.warning("reauthentication failed: %s", reason)
        finally:
            release_reauth_slot(REAUTH_SLOT_FILE, os.getpid(), success=success)
            with self._auth_condition:
                self._browser_context = None
                self._auth_thread = None
                self._auth_condition.notify_all()

    def refresh(self, force=False, wait=True, deadline=None) -> bool:
        """Share one visible reauthentication session across all callers.

        A13-3: the session is gated by the cross-instance lease, so the two
        instances sharing one UM identity never open two submission windows at
        once. A refused session is reported as a throttle, never as an
        authentication failure, and the single-flight thread plus the "failed
        stays failed" rule keep a rejected credential from being retried in a
        loop.
        """
        if self.args.no_browser:
            with self._auth_condition:
                self._auth_state = "failed"
                self._auth_error = "BrowserDisabled"
            return False

        with self._auth_condition:
            if self._shutdown_event.is_set():
                return False
            if self._auth_thread is None:
                # A failed automatic attempt stays failed until the console
                # explicitly requests another one. This prevents popup loops.
                if self._auth_state == "failed" and not force:
                    return False
                granted, reason, ledger = acquire_reauth_slot(
                    REAUTH_SLOT_FILE, os.getpid(),
                    ttl_s=self.args.login_timeout + REAUTH_SLOT_MARGIN_S)
                self._auth_gate = {
                    "granted": granted,
                    "reason": reason,
                    "owner_pid": ledger.get("owner_pid"),
                    "expires_epoch": ledger.get("expires_epoch"),
                    "attempts_in_window": ledger.get("attempts_in_window"),
                    "max_attempts": REAUTH_MAX_ATTEMPTS_PER_WINDOW,
                    "window_s": REAUTH_ATTEMPT_WINDOW_S,
                    "checked_epoch": time.time(),
                }
                if not granted:
                    self._auth_error = "reauth_refused:%s" % reason
                    _logger.warning("reauthentication session refused reason=%s",
                                    reason)
                    return False
                self._auth_state = "logging_in"
                self._auth_error = None
                self._auth_login = None
                self._auth_generation += 1
                generation = self._auth_generation
                self._auth_thread = threading.Thread(
                    target=self._perform_refresh,
                    args=(generation,),
                    name="chat2api-reauthentication",
                    daemon=True,
                )
                self._auth_thread.start()
                _logger.info("reauthentication session started generation=%s",
                             generation)

            if not wait:
                return True

            wait_deadline = time.monotonic() + self.args.login_timeout + 10
            if deadline is not None:
                wait_deadline = min(wait_deadline, deadline)
            while self._auth_state == "logging_in":
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._auth_condition.wait(remaining)
            return self._auth_state == "valid"

    def validate_or_refresh(self, source="startup"):
        """Validate saved credentials without delaying the local HTTP server.

        A13-2: the verdict comes from ``classify_credential``. Only an
        explicit upstream rejection or a locally missing credential starts a
        session; an inconclusive probe keeps the last evidence and never opens
        a login window.
        """
        verdict, reason, upstream_status = classify_credential(
            self.base_url, self.bearer())
        self._record_auth_evidence(verdict, reason, upstream_status, source)
        self._record_upstream_reachability(
            self._reachability_from_credential_probe(
                verdict, reason, upstream_status),
            reason=reason, upstream_status=upstream_status)
        if verdict == "valid":
            with self._auth_condition:
                self._auth_state = "valid"
                self._auth_error = None
            return "valid"
        if verdict == "unknown":
            # Undecidable: this is NOT evidence of an expired credential,
            # so no browser window may open here.
            _logger.warning(
                "credential check inconclusive reason=%s source=%s "
                "state kept=%s", reason, source, self.auth_status()["state"])
            return "unknown"
        with self._auth_condition:
            # Never downgrade a terminal "failed": that state is what keeps a
            # rejected credential from being retried in a loop.
            if self._auth_state != "failed":
                self._auth_state = "needs_login"
            self._auth_error = None
        self.refresh(wait=False)
        return verdict

    def begin_startup_validation(self):
        def startup_probe():
            # /api/config is public in Open WebUI and gives us the exact version
            # and public feature flags without depending on a valid login.
            self.probe_config()
            self.validate_or_refresh(source="startup")

        threading.Thread(
            target=startup_probe,
            name="chat2api-startup-probe",
            daemon=True,
        ).start()

    def _record_watch_round(self, outcome):
        """Record one finished watch round for the additive watch view.

        Called at EVERY return of ``run_auth_watch_round()``, including a
        round that only observed "logging_in"/"stopped": the recorded outcome
        is what actually happened, and a skipped round must not read as a fresh
        credential check. Returns ``outcome`` unchanged so the call sites stay
        plain returns.
        """
        now = time.time()
        with self._auth_condition:
            self._watch_last_epoch = now
            self._watch_last_outcome = outcome
            self._watch_rounds += 1
        return outcome

    def _note_watch_interval(self, interval):
        """Publish the backoff interval that gates the NEXT watch round.

        ``next_round_due`` is last_round_epoch + this value, so the loop
        calls this right after it recomputes the backoff for the round it just
        ran (never a fabricated zero).
        """
        with self._auth_condition:
            self._watch_interval_s = interval

    def run_auth_watch_round(self):
        """One bounded, read-only credential check (A13-2).

        Never calls ``Proxy.call()``, never issues a billable inference
        request and never refreshes the model cache: it reuses the same
        single-shot ``/api/models`` classification as ``/ready``, then starts
        the shared single-flight session only on explicit evidence (upstream
        rejection or a locally missing credential). A session already in flight
        is left alone, so a business-triggered 401 and this check never
        reauthenticate twice. Every return records the round in the additive
        watch view (§8.4).
        """
        if self._shutdown_event.is_set():
            return self._record_watch_round("stopped")
        with self._auth_condition:
            session_in_flight = self._auth_state == "logging_in"
        if session_in_flight:
            return self._record_watch_round("logging_in")
        verdict, reason, upstream_status = classify_credential(
            self.base_url, self.bearer(),
            timeout=AUTH_WATCH_PROBE_TIMEOUT_S)
        self._record_auth_evidence(verdict, reason, upstream_status,
                                   "auth_watch")
        self._record_upstream_reachability(
            self._reachability_from_credential_probe(
                verdict, reason, upstream_status),
            reason=reason, upstream_status=upstream_status)
        if verdict == "valid":
            with self._auth_condition:
                self._auth_state = "valid"
                self._auth_error = None
            return self._record_watch_round("valid")
        if verdict == "unknown":
            # Long-idle evidence is not evidence of expiry: keep the last
            # known state and never open a login window.
            return self._record_watch_round("unknown")
        if self._shutdown_event.is_set():
            return self._record_watch_round("stopped")
        with self._auth_condition:
            session_in_flight = self._auth_state == "logging_in"
            if not session_in_flight:
                # Never downgrade a terminal "failed" either: a credential
                # the session already rejected must not be retried every round.
                if self._auth_state != "failed":
                    self._auth_state = "needs_login"
                self._auth_error = None
        if session_in_flight:
            return self._record_watch_round("logging_in")
        self.refresh(wait=False)
        return self._record_watch_round(verdict)

    def begin_auth_watch(self):
        """Start the bounded periodic credential check (A13-2).

        The first check waits one full interval, so it never duplicates the
        startup probe; a non-valid round backs off up to
        ``AUTH_WATCH_MAX_INTERVAL_S`` instead of polling harder. The same
        rounds also carry the TTL-gated public-config refresh (W5-3): one
        bounded fetch per expired-TTL round, single-flight, with failure
        backoff — no new timer or thread is created for it. The interval
        that gates the next round is published for the additive watch view
        (§8.4), so ``next_round_due`` follows the real backoff.
        """
        def watch():
            interval = AUTH_WATCH_INTERVAL_S
            while not self._shutdown_event.wait(interval):
                self._maybe_refresh_config()
                outcome = self.run_auth_watch_round()
                if outcome == "valid":
                    interval = AUTH_WATCH_INTERVAL_S
                else:
                    interval = min(interval * AUTH_WATCH_BACKOFF_FACTOR,
                                   AUTH_WATCH_MAX_INTERVAL_S)
                # Publish AFTER the round: this is the interval that will
                # gate the next round (the round above already recorded
                # its own epoch/outcome).
                self._note_watch_interval(interval)

        self._watch_thread = threading.Thread(
            target=watch,
            name="chat2api-auth-watch",
            daemon=True,
        )
        self._watch_thread.start()

    def shutdown(self):
        self._shutdown_event.set()
        with self._auth_condition:
            # Supersede any in-flight session: its late result must not write
            # credentials or state after this instance is stopping.
            self._auth_generation += 1
            context = self._browser_context
            thread = self._auth_thread
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        # The maintenance loop ends with the instance: stop waiting for a new
        # round at once and give the CURRENT bounded round a chance to
        # finish (a mid-flight config fetch is bounded by its own timeout).
        watch_thread = self._watch_thread
        if watch_thread and watch_thread is not threading.current_thread():
            watch_thread.join(timeout=3)

    def call(self, method: str, path: str, stream: bool = False,
             physical_budget=None, request_deadline=None, **kwargs):
        """Request with a single 401 -> re-auth retry. For streaming endpoints
        you MUST pass stream=True, otherwise requests buffers the whole SSE
        body until the upstream closes the connection.

        ``physical_budget`` may be a shared ``_PhysicalRequestBudget`` (new
        callers) or a legacy integer meaning how many real requests THIS call
        may still make. Every requests.request(), including a post-401 hidden
        re-send, consumes one unit before it is issued."""
        extra_headers = kwargs.pop("headers", {})
        timeout = kwargs.pop("timeout", 600)
        legacy_limit = None
        legacy_used = 0
        if physical_budget is not None and not isinstance(
                physical_budget, _PhysicalRequestBudget):
            legacy_limit = max(0, int(physical_budget))

        def budget_remaining():
            if isinstance(physical_budget, _PhysicalRequestBudget):
                return physical_budget.remaining
            if legacy_limit is None:
                return None
            return max(0, legacy_limit - legacy_used)

        def consume_budget():
            nonlocal legacy_used
            if isinstance(physical_budget, _PhysicalRequestBudget):
                return physical_budget.consume()
            if legacy_limit is None:
                return True
            if legacy_used >= legacy_limit:
                return False
            legacy_used += 1
            return True

        for attempt in (0, 1):
            bearer = self.bearer()
            if not bearer:
                if not self.refresh(deadline=request_deadline):
                    if (request_deadline is not None and
                            time.monotonic() >= request_deadline):
                        raise _RequestDeadlineExceeded(
                            "authentication wait exceeded request deadline")
                    self._raise_auth_required()
                bearer = self.bearer()
            if not consume_budget():
                raise _PhysicalBudgetExceeded(
                    "upstream physical request budget exhausted")
            r = requests.request(
                method, f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {bearer}",
                         **extra_headers},
                timeout=timeout, stream=stream, **kwargs)
            if r.status_code == 401 and attempt == 0:
                if budget_remaining() == 0:
                    # The physical request budget is exhausted: the 401 is
                    # surfaced as-is instead of spending one more request on
                    # the post-refresh re-send.
                    return r
                r.close()
                if self.bearer() != bearer:
                    continue
                # A13-2: an upstream 401 is explicit evidence that the
                # CURRENT credential was rejected, so record it before trying
                # to recover - the state must never keep claiming "valid"
                # while the business path is answering 401.
                self._record_auth_evidence("rejected", "upstream_rejected",
                                           401, "business")
                with self._auth_condition:
                    # Never downgrade a terminal "failed": that state is what
                    # keeps a rejected credential from being retried in a loop.
                    if self._auth_state not in ("logging_in", "failed"):
                        self._auth_state = "needs_login"
                if self.refresh(deadline=request_deadline):
                    continue
                if (request_deadline is not None and
                        time.monotonic() >= request_deadline):
                    raise _RequestDeadlineExceeded(
                        "authentication refresh exceeded request deadline")
                self._raise_auth_required()
            return r
        return r

    def list_models(self, force=False, raw_only=False, fresh_required=False):
        now = time.time()
        if (self.models_cache["data"] is None or force
                or now - self.models_cache["at"] > 60):
            r = None
            try:
                r = self.call("GET", "/api/models")
                if r.status_code == 200:
                    payload = r.json()
                    if (not isinstance(payload, dict)
                            or not isinstance(payload.get("data"), list)):
                        raise ValueError("invalid model catalog shape")
                    self.models_cache = {"at": now, "data": payload}
                elif fresh_required:
                    # Freshness is an explicit contract for the caller (the share
                    # entry): never fall back to the stale cache when the upstream
                    # refresh failed. Return None -> the handler answers 502.
                    return None
            except (requests.RequestException, ValueError):
                # Historical non-fresh callers already fall back to a stale
                # cache on upstream non-200. Network failures and malformed
                # 200 bodies follow the same compatibility rule; raw-fresh
                # callers must fail closed instead.
                if fresh_required:
                    return None
            finally:
                if r is not None:
                    # Release must never break the model-list logic itself
                    # (stub/short-lived responses may not carry close()).
                    try:
                        r.close()
                    except Exception:
                        pass
        data = self.models_cache["data"]
        if data is None:
            return None
        data = dict(data)
        data["data"] = list(data.get("data", []))
        # If raw_only, this is exactly the current upstream catalog (no local
        # virtual aliases), which is the share entry's model contract.
        if not raw_only:
            # Append thinking-level virtual models for each registered base model.
            for base in EFFORT_MODELS:
                src = next((m for m in data["data"] if m.get("id") == base), None)
                # A configured effort model can disappear from an otherwise valid
                # upstream snapshot.  Never expose variants without their base: the
                # resulting entries look selectable but can only fail at request time.
                if src is None:
                    continue
                for suffix in EFFORT_LEVELS:
                    vid = f"{base}-{suffix}"
                    if any(m.get("id") == vid for m in data["data"]):
                        continue
                    m = dict(src)
                    m["id"] = vid
                    m["name"] = vid
                    data["data"].append(m)
        return data


def _client_disconnected(client_socket) -> bool:
    """Whether the client side of an in-flight streaming request is gone.

    After the request body is consumed the client sends nothing more, so the
    socket only becomes readable on EOF (clean close) or reset. Unexpected
    readable data is left alone (MSG_PEEK, unconsumed) and not treated as a
    disconnect.
    """
    try:
        readable, _, _ = select.select([client_socket], [], [], 0)
    except (OSError, ValueError):
        return True  # socket already closed or invalid
    if not readable:
        return False
    try:
        return client_socket.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False  # spurious readable
    except (ConnectionError, OSError):
        return True


# Upstream response headers copied into the stream-end log line: the per-request
# correlation id Envoy returns plus the literal routing metadata. Only these
# three names are ever read, so no credential header (Authorization, cookie,
# token) can reach the log line.
_UPSTREAM_TRACE_HEADER_NAMES = ("x-request-id", "server", "connection")

# A trace value is copied verbatim only while it stays claimable: a single-line
# value no longer than this. Anything else is logged as an explicit unavailable
# placeholder, so the log line never carries an unbounded value and never
# carries a fabricated (cleaned-up or truncated) id.
_UPSTREAM_TRACE_VALUE_MAX_CHARS = 200
_UPSTREAM_TRACE_UNAVAILABLE = "unavailable"


def _has_control_chars(text):
    """True when `text` holds a character that could break the log line.

    C0 (which covers CR, LF and NUL), DEL, the C1 block and the Unicode
    line/paragraph separators. A header parser keeps these inside an obs-fold
    value, so they arrive as part of an otherwise ordinary header value.
    """
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029):
            return True
    return False


def _sanitize_upstream_trace_value(value):
    """One upstream trace header value, made safe for a single log line.

    Returns the value verbatim when it is exactly claimable — a single-line
    value within the length bound — so a real UM id stays lookup-able
    byte-for-byte. A value carrying CR/LF or any other control character (an
    upstream can fold them into a header value, which is how a forged log line
    is injected) or one that is oversized is reported as
    ``unavailable:<reason>`` instead: never a cleaned-up, escaped or truncated
    value that would still look like a claimable id. None stays None so an
    omitted header remains distinguishable from an unusable one.
    """
    if value is None:
        return None
    text = str(value)
    # Only the bound plus one character is inspected, so the safety check is
    # itself bounded even for an oversized value.
    if _has_control_chars(text[:_UPSTREAM_TRACE_VALUE_MAX_CHARS + 1]):
        return f"{_UPSTREAM_TRACE_UNAVAILABLE}:control_chars"
    if len(text) > _UPSTREAM_TRACE_VALUE_MAX_CHARS:
        return f"{_UPSTREAM_TRACE_UNAVAILABLE}:too_long"
    return text


def _upstream_trace_headers(response) -> dict:
    """Snapshot the upstream trace headers for the stream-end log line.

    A header the upstream did not send stays None, so the log line reports null
    instead of a fabricated default. Every present value goes through
    _sanitize_upstream_trace_value, so a claimable id is reported byte-for-byte
    while a value that could forge a log line is reported as unavailable.
    Header reading is best-effort: it can never fail the stream.
    """
    trace = dict.fromkeys(_UPSTREAM_TRACE_HEADER_NAMES)
    headers = getattr(response, "headers", None)
    if headers is None:
        return trace
    for name in _UPSTREAM_TRACE_HEADER_NAMES:
        try:
            trace[name] = _sanitize_upstream_trace_value(headers.get(name))
        except Exception:
            trace[name] = None
    return trace


class _DegenerateRepetitionError(Exception):
    """The streamed model text fell into a repetition loop; the guard cut it."""


class _CompatStreamRetryable(Exception):
    """A compat SSE stream died before anything reached the client; the
    caller may transparently reopen the upstream without concatenation."""


class _RequestDeadlineExceeded(RuntimeError):
    """No new upstream work may start after the per-request wall deadline."""


class _PhysicalBudgetExceeded(RuntimeError):
    """The per-request real HTTP request budget has been exhausted."""


class _PhysicalRequestBudget:
    """Shared counter for real HTTP calls, including hidden post-401 re-sends."""

    def __init__(self, limit):
        self.limit = max(0, int(limit))
        self.used = 0

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def consume(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _repetition_guard_signature(text: str) -> str | None:
    """Return a phase-independent short-cycle signature for a repeated tail.

    The minimum matching period (1..8 chars) is canonicalized across cyclic
    rotations, so adjacent chunks of the same loop still share one signature
    even if their chunk boundary lands at a different phase (``lock`` vs
    ``ockl``). Different large-but-legitimate repeated events (for example a
    200 KiB ``x`` event followed by a 200 KiB ``y`` event) do not build a
    consecutive hit streak.
    """
    if len(text) < REPETITION_GUARD_MIN_TAIL_CHARS:
        return None
    for plen in range(1, REPETITION_GUARD_MAX_PATTERN_CHARS + 1):
        pattern = text[-plen:]
        reps = -(-REPETITION_GUARD_MIN_TAIL_CHARS // plen)
        tail_needed = (pattern * reps)[-REPETITION_GUARD_MIN_TAIL_CHARS:]
        if text.endswith(tail_needed):
            rotations = (pattern[i:] + pattern[:i] for i in range(plen))
            return min(rotations)
    return None


def _repetition_guard_hit(text: str) -> bool:
    """True when the tail of ``text`` is one short pattern repeated enough."""
    return _repetition_guard_signature(text) is not None


def _line_cycle_repetition(text: str):
    """Return (signature, repeats, cycle_lines, tail_chars) for a line loop.

    Observation-only companion to the short-period signature: a repeating
    unit of whole lines (period up to ``LINE_LOOP_MAX_CYCLE_LINES`` lines)
    stays invisible to the 1-8 char cutting signature. The rolling tail is
    split into lines, the two window-edge lines are dropped because the
    buffer may have cut them in half, and the trailing lines must repeat one
    exact line group. Returns ``None`` unless the cycle repeated at least
    ``LINE_LOOP_MIN_REPEATS`` times and the repeated tail spans at least
    ``LINE_LOOP_MIN_TAIL_CHARS`` chars. Never used to cut a stream.
    """
    if len(text) < LINE_LOOP_MIN_TAIL_CHARS:
        return None
    lines = text.split("\n")
    lines = lines[1:-1]
    if len(lines) < LINE_LOOP_MIN_REPEATS:
        return None
    # Cheap pre-gate: a line-periodic tail repeats every ``period`` lines, so
    # the last line must reappear one cycle back. Normal prose fails this in
    # constant time and never reaches the periodicity walk below.
    if not any(
            len(lines) >= 2 * period
            and lines[-1] == lines[-1 - period]
            for period in range(1, LINE_LOOP_MAX_CYCLE_LINES + 1)):
        return None
    for period in range(1, LINE_LOOP_MAX_CYCLE_LINES + 1):
        cycle = lines[-period:]
        if any(len(line) > LINE_LOOP_MAX_LINE_CHARS for line in cycle):
            continue
        repeats = 1
        while lines[-period * (repeats + 1):-period * repeats] == cycle:
            repeats += 1
        if repeats < LINE_LOOP_MIN_REPEATS:
            continue
        tail_chars = repeats * sum(len(line) + 1 for line in cycle)
        if tail_chars < LINE_LOOP_MIN_TAIL_CHARS:
            continue
        rotations = ("\x00".join(cycle[i:] + cycle[:i])
                     for i in range(period))
        return min(rotations), repeats, period, tail_chars
    return None


class _LineLoopObserver:
    """Collect per-request evidence about long-unit line loops.

    One instance per streaming attempt. ``observe`` throttles itself to one
    detector run per ``LINE_LOOP_CHECK_EVERY_CHARS`` of fed text and logs a
    single WARNING per distinct cycle signature, so an evolving loop can
    surface again without flooding the log.
    """

    def __init__(self, request_id, site):
        self.request_id = request_id
        self.site = site
        self.hits = 0
        self.fired_signature = None
        self._fed_since_check = 0

    def observe(self, buf, fed_chars):
        if not (LINE_LOOP_OBSERVE_ENABLED and fed_chars > 0):
            return
        self._fed_since_check += fed_chars
        if self._fed_since_check < LINE_LOOP_CHECK_EVERY_CHARS:
            return
        self._fed_since_check = 0
        if len(buf) < LINE_LOOP_MIN_TAIL_CHARS:
            return
        observed = _line_cycle_repetition(buf)
        if observed is None:
            return
        signature, repeats, cycle_lines, tail_chars = observed
        if signature == self.fired_signature:
            return
        self.fired_signature = signature
        self.hits += 1
        _logger.warning(
            "line repetition pattern request=%s site=%s cycle_lines=%d "
            "repeats=%d tail_chars=%d sample=%.140s",
            self.request_id, self.site, cycle_lines, repeats, tail_chars,
            signature.replace("\x00", "\\n"))


def _image_part_type(part):
    """Return a recognized multimodal image-part type, else None.

    Deliberately key off the explicit part ``type`` only. A generic dict that
    merely happens to carry a ``url``/``image`` field must never be deleted.
    """
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    return part_type if part_type in _IMAGE_PART_TYPES else None


def _chat_image_stats(body):
    """Structure-only image/text statistics for one OpenAI chat payload."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        messages = []

    last_user = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = index
    # Without a user boundary we cannot safely distinguish history from the
    # active turn. Treat everything as protected/current rather than deleting.
    protected_start = last_user if last_user >= 0 else 0

    total_images = 0
    historical_images = 0
    current_turn_images = 0
    multimodal_messages = 0
    multimodal_text_chars = 0
    image_types = {}

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        multimodal_messages += 1
        for part in content:
            image_type = _image_part_type(part)
            if image_type:
                total_images += 1
                image_types[image_type] = image_types.get(image_type, 0) + 1
                if index < protected_start:
                    historical_images += 1
                else:
                    current_turn_images += 1
                continue
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    multimodal_text_chars += len(text)

    return {
        "images_total": total_images,
        "historical_images": historical_images,
        "current_turn_images": current_turn_images,
        "multimodal_messages": multimodal_messages,
        "multimodal_text_chars": multimodal_text_chars,
        "image_part_types": dict(sorted(image_types.items())),
        "protected_start": protected_start,
    }


def _normalize_chat_image_budget(body):
    """Return ``(body_to_send, stats, local_error)`` for chat image limits.

    Copy-on-write: requests at or under the limit, and requests when automatic
    trimming is disabled, reuse the original object. When trimming is needed,
    only ``body``/``messages`` and the affected message/content containers are
    copied; image bytes/base64 strings are never duplicated by a deep copy.

    The protected segment begins at the last ``role=user`` message and extends
    to the end, covering agent/tool activity belonging to the current turn.
    Images in that segment are never silently removed; only with the explicit
    ``CHAT2API_AUTO_TRIM_CURRENT_TURN_IMAGES=1`` opt-in does the normalizer
    drop the oldest current-turn images down to ``MAX_IMAGES`` (newest kept,
    WARNING logged by the caller). With no user message, every image is
    conservatively treated as protected.
    """
    before = _chat_image_stats(body)
    stats = dict(before)
    stats.update(
        images_before=before["images_total"],
        images_after=before["images_total"],
        images_trimmed=0,
        historical_removed=0,
        current_turn_trimmed=0,
    )

    if not AUTO_TRIM_HISTORICAL_IMAGES:
        return body, stats, None
    if before["images_total"] <= MAX_IMAGES:
        return body, stats, None

    current_turn_remove = 0
    if before["current_turn_images"] > MAX_IMAGES:
        if not AUTO_TRIM_CURRENT_TURN_IMAGES:
            return body, stats, {
                "type": "too_many_images_current_turn",
                "message": (
                    f"Current turn contains {before['current_turn_images']} images, "
                    f"but the upstream limit is {MAX_IMAGES}."),
            }
        # Opt-in unattended recovery: keep the newest MAX_IMAGES current-turn
        # images, drop the oldest ones here (never upstream). The current turn
        # then occupies the whole budget, so all historical images must go.
        current_turn_remove = before["current_turn_images"] - MAX_IMAGES
        historical_budget = 0
    else:
        historical_budget = MAX_IMAGES - before["current_turn_images"]
    historical_remove = max(0, before["historical_images"] - historical_budget)
    if historical_remove + current_turn_remove <= 0:
        # Defensive: the arithmetic above should make this unreachable when
        # total_images > MAX_IMAGES, but returning unchanged is safer than an
        # accidental rewrite if a future image classifier changes.
        return body, stats, None

    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, stats, None

    new_messages = list(messages)
    remaining_history = historical_remove
    remaining_current = current_turn_remove
    protected_start = before["protected_start"]

    # Scan oldest -> newest and remove only as many image parts as are
    # necessary: history retains its newest images, the protected segment
    # retains its newest when current-turn trimming is enabled.
    for index in range(len(messages)):
        if remaining_history <= 0 and remaining_current <= 0:
            break
        message = messages[index]
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        in_protected = index >= protected_start
        new_content = []
        removed_here = 0
        for part in content:
            budget_left = remaining_current if in_protected else remaining_history
            if budget_left > 0 and _image_part_type(part):
                if in_protected:
                    remaining_current -= 1
                else:
                    remaining_history -= 1
                removed_here += 1
                continue
            new_content.append(part)

        if not removed_here:
            continue

        # Preserve message position/role. An empty multimodal content array is
        # rejected by some OpenAI-compatible validators, so an image-only
        # message receives ONE short text placeholder instead of being deleted
        # or receiving one placeholder per removed image.
        if not new_content:
            placeholder = (_CURRENT_TURN_IMAGE_PLACEHOLDER if in_protected
                           else _HISTORICAL_IMAGE_PLACEHOLDER)
            new_content = [{"type": "text", "text": placeholder}]
        new_message = dict(message)
        new_message["content"] = new_content
        new_messages[index] = new_message

    if remaining_history or remaining_current:
        # Never risk deleting beyond the computed budgets.
        return body, stats, {
            "type": "image_budget_normalization_failed",
            "message": "Image budget could not be normalized safely.",
        }

    new_body = dict(body)
    new_body["messages"] = new_messages
    after = _chat_image_stats(new_body)
    stats.update(
        images_after=after["images_total"],
        images_trimmed=historical_remove + current_turn_remove,
        historical_removed=historical_remove,
        current_turn_trimmed=current_turn_remove,
    )
    return new_body, stats, None


def _sanitize_upstream_error_value(value, limit):
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def _repair_truncated_json_prefix(text):
    """Re-close a JSON object cut off by the diagnostic byte cap.

    The upstream 4xx body is captured with a character cap, so its tail —
    including every closing bracket — may be missing and ``json.loads`` fails
    on the prefix. Scan the prefix tracking string state and bracket nesting,
    append the missing closers (plus a closing quote when the cut lands
    inside a literal), and re-parse. Returns the parsed dict, or None when
    the prefix is not a plausible JSON object at all.
    """
    s = text.lstrip()
    if not s.startswith("{"):
        return None
    stack = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    repaired = s
    if in_string:
        # The cut landed inside a literal string: drop a trailing lone
        # backslash so the appended closing quote is not itself escaped.
        if escaped:
            repaired = repaired[:-1]
        repaired += '"'
    repaired += "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        payload = json.loads(repaired)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_upstream_reject_error(response_text):
    """Extract short diagnostic fields from common upstream 4xx JSON shapes."""
    message = ""
    error_type = ""
    error_code = ""
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        payload = _repair_truncated_json_prefix(response_text)
        if payload is None:
            return (_sanitize_upstream_error_value(response_text, 120),
                    "", "")

    if not isinstance(payload, dict):
        return ("", "", "")
    err = payload.get("error")
    if isinstance(err, dict):
        message = err.get("message") or ""
        error_type = err.get("type") or ""
        error_code = err.get("code") or ""
    elif isinstance(err, str):
        message = err

    # FastAPI-style validation errors (including UM's image limit) commonly use
    # a top-level ``detail`` rather than an OpenAI ``error`` object.
    if not message:
        detail = payload.get("detail")
        if isinstance(detail, str):
            message = detail
        elif isinstance(detail, dict):
            message = detail.get("message") or detail.get("msg") or ""
            error_type = error_type or detail.get("type") or ""
            error_code = error_code or detail.get("code") or ""
        elif isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                message = first.get("message") or first.get("msg") or ""
                error_type = error_type or first.get("type") or ""
                error_code = error_code or first.get("code") or ""
    if not message and isinstance(payload.get("message"), str):
        message = payload.get("message")

    return (
        _sanitize_upstream_error_value(message, 500),
        _sanitize_upstream_error_value(error_type, 60),
        _sanitize_upstream_error_value(error_code, 60),
    )


# Length bound for the reason carried out of an explicit upstream error SSE
# event. The bound keeps any client-visible error payload and log record
# small no matter what the upstream put in its error object.
_UPSTREAM_EXPLICIT_ERROR_MESSAGE_MAX_CHARS = 200


def _explicit_upstream_error_reason(chunk):
    """Stable, sanitized, bounded reason for an explicit upstream error event.

    Open WebUI 0.11.x reports a failed chat stream as a NORMAL SSE event
    ``data: {"error": {...}}`` followed by [DONE] (official middleware patch
    c0fb36c9). Only a truthy top-level ``error`` counts: regular chunks may
    carry ``"error": null``. The returned reason is the upstream's message
    text collapsed to one line and bounded - never the raw object, which may
    carry credential material or arbitrary content.
    """
    if not isinstance(chunk, dict):
        return None
    error = chunk.get("error")
    if not error:  # absent, null or empty: an ordinary chunk
        return None
    message = error
    if isinstance(error, dict):
        message = error.get("message")
    if isinstance(message, str) and message.strip():
        return _sanitize_upstream_error_value(
            message, _UPSTREAM_EXPLICIT_ERROR_MESSAGE_MAX_CHARS)
    # Unexpected error shape: keep a stable generic reason instead of
    # echoing an object whose contents are not known to be safe.
    return "upstream reported an explicit stream error"


def _upstream_reject_diagnostics(body):
    """Structure-only diagnostics for an upstream 4xx request rejection.

    Counts, sizes, ids, role sequence and a shape hash over structural
    features ONLY. The returned fields never contain message content, tool
    arguments, tool results or credentials, so the line is safe to log
    verbatim and lets us compare a repeatedly-rejected conversation against
    healthy ones without leaking anything.
    """
    diag = {}
    try:
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list):
            messages = []
        role_seq = []
        content_types = []
        max_message_chars = 0
        total_text_chars = 0
        assistant_calls = 0
        call_ids = []
        result_ids = []
        for m in messages:
            if not isinstance(m, dict):
                role_seq.append("non_dict")
                content_types.append("non_dict")
                continue
            role_seq.append(str(m.get("role")))
            content = m.get("content")
            if isinstance(content, str):
                content_types.append("str")
                max_message_chars = max(max_message_chars, len(content))
                total_text_chars += len(content)
            elif isinstance(content, list):
                content_types.append("list")
                message_text_chars = 0
                for part in content:
                    if isinstance(part, dict) and not _image_part_type(part):
                        text = part.get("text")
                        if isinstance(text, str):
                            message_text_chars += len(text)
                total_text_chars += message_text_chars
                max_message_chars = max(max_message_chars, message_text_chars)
            elif content is None:
                content_types.append("null")
            else:
                content_types.append(type(content).__name__)
            tcalls = m.get("tool_calls")
            if isinstance(tcalls, list):
                for t in tcalls:
                    if isinstance(t, dict):
                        assistant_calls += 1
                        cid = t.get("id")
                        if isinstance(cid, str):
                            call_ids.append(cid)
            if m.get("role") == "tool":
                tid = m.get("tool_call_id")
                if isinstance(tid, str):
                    result_ids.append(tid)
        tools = body.get("tools")
        tool_names = sorted(
            str((t or {}).get("function", {}).get("name", ""))
            for t in (tools or []) if isinstance(t, dict))
        tools_count = len(tools) if isinstance(tools, list) else 0
        missing = sum(1 for c in call_ids if c not in result_ids)
        orphan = sum(1 for rid in result_ids if rid not in call_ids)
        duplicate = len(call_ids) - len(set(call_ids))
        image_stats = _chat_image_stats(body)
        # Shape fingerprint: structure only (roles, ids, tool names, content
        # TYPES, top-level keys) - identical shapes with different bodies
        # collide, any structural change diverges. Never includes content.
        shape_src = json.dumps(
            {"roles": role_seq, "calls": call_ids, "results": result_ids,
             "tools": tool_names, "keys": sorted(body.keys())
             if isinstance(body, dict) else [],
             "types": content_types},
            ensure_ascii=False, sort_keys=True)
        est_text_tokens = _message_tokens(messages)
        diag.update(
            message_count=len(messages),
            # Keep the old field for log consumers, but make its text-only
            # nature explicit with the new name too.
            estimated_input_tokens=est_text_tokens,
            estimated_text_input_tokens=est_text_tokens,
            tools_count=tools_count,
            assistant_tool_calls=assistant_calls,
            tool_results=len(result_ids),
            missing_tool_results=missing,
            orphan_tool_results=orphan,
            duplicate_tool_call_ids=duplicate,
            max_message_chars=max_message_chars,
            images_total=image_stats["images_total"],
            historical_images=image_stats["historical_images"],
            current_turn_images=image_stats["current_turn_images"],
            multimodal_messages=image_stats["multimodal_messages"],
            multimodal_text_chars=image_stats["multimodal_text_chars"],
            image_part_types=image_stats["image_part_types"],
            top_level_keys=",".join(sorted(body.keys()))
            if isinstance(body, dict) else "",
            role_shape="".join(r[0] if r else "?" for r in role_seq)[:120],
            payload_shape_hash=hashlib.sha256(
                shape_src.encode("utf-8")).hexdigest()[:16])
    except Exception:
        diag["diagnostics_error"] = "failed"
    return diag


# ------------------------------------------------ output budget diagnostics
# B7-7 (Plan-Revision 7 step 7): the upstream 400 "input + reserved output >
# context" can only be attributed when the log states WHICH output cap this
# gateway actually put on the wire. A cap value quoted verbatim into the log
# is still bounded, so an absurd client number cannot inflate one line.
_OUTPUT_BUDGET_LOGGED_VALUE_MAX = 10 ** 15
# Model/effort identifiers are logged bounded for the same reason (a hostile
# client could put megabytes into "model").
_OUTPUT_BUDGET_STR_CAP = 200


def _output_budget_field(body, key):
    """Presence/legality/controlled value of ONE OpenAI output-cap field.

    Returns (present, value_for_log, valid, kind). "present" means the key
    exists in the request body; "valid" is the C1 legality contract - a
    NON-BOOL positive integer (``bool`` is an ``int`` subclass in Python, so
    True/False must be excluded explicitly, exactly like _reported_tokens).
    ``kind`` is a short type marker, never the raw value: an illegal string
    value must not enter the log.
    """
    if not isinstance(body, dict) or key not in body:
        return False, None, False, "absent"
    value = body[key]
    if value is None:
        return True, None, False, "null"
    if isinstance(value, bool):
        return True, None, False, "bool"
    if not isinstance(value, int):
        return True, None, False, type(value).__name__
    if value <= 0:
        return True, None, False, "non_positive"
    if value > _OUTPUT_BUDGET_LOGGED_VALUE_MAX:
        # Legal but absurd: presence and legality stay true, the digits stay
        # out of the log.
        return True, None, True, "oversized_int"
    return True, value, True, "ok"


def _output_budget_diagnostics(body, model, level, protocol):
    """Controlled output-budget facts about the body forwarded upstream.

    Records ONLY the presence/legality and numeric value of the two OpenAI
    output-cap fields, plus the effective model, the forwarded effort level
    and the request protocol - never the messages, tool results, credentials
    or any other part of the upstream object. The caller passes the values
    exactly as they will be forwarded; this helper never reads a config file
    or a catalog to "complete" the picture.

    ``cap_source`` is restricted to what the forwarding path can prove:
      - "client_request": a legal cap field was present in the client body
        and is forwarded unchanged (this process injects and rewrites
        nothing; Proxy.call serializes the body as-is).
      - "unknown": no legal cap field is present (or only an illegal one), so
        the effective output cap is whatever the upstream defaults to -
        unobservable here and NEVER guessed from a static config file. The
        B7-7 "384000" stays undecided until a captured request or an
        upstream rejection says otherwise.
      - "catalog_sync_declaration" belongs to this field's contract
        vocabulary for a future sync-driven cap; nothing produces it today,
        because CatalogModel carries no output cap at all.

    ``output_cap_effective`` applies the OpenAI parameter precedence
    (max_completion_tokens over max_tokens) as THIS PROCESS'S ASSUMPTION
    about the upstream's semantics; the upstream's actual adjudication of
    the two fields is NOT verified here. Both raw values are logged side by
    side with their presence/legality, so a 400/422 attribution built on
    this line is unimpaired even if the upstream resolves the precedence
    differently.
    """
    mt_present, mt_value, mt_valid, mt_kind = _output_budget_field(
        body, "max_tokens")
    mct_present, mct_value, mct_valid, mct_kind = _output_budget_field(
        body, "max_completion_tokens")
    # OpenAI parameter precedence: max_completion_tokens supersedes
    # max_tokens when both are present and legal. When the higher-precedence
    # field is present but illegal, NO effective value is claimed - the
    # upstream's own validation decides and the gateway does not guess.
    if mct_valid:
        effective, source = mct_value, "client_request"
    elif mct_present:
        effective, source = None, "unknown"
    elif mt_valid:
        effective, source = mt_value, "client_request"
    else:
        effective, source = None, "unknown"
    return {
        "model": model if isinstance(model, str) else "",
        "level": level if isinstance(level, str) else None,
        "protocol": protocol,
        "max_tokens": mt_value,
        "max_tokens_present": mt_present,
        "max_tokens_valid": mt_valid,
        "max_tokens_kind": mt_kind,
        "max_completion_tokens": mct_value,
        "max_completion_tokens_present": mct_present,
        "max_completion_tokens_valid": mct_valid,
        "max_completion_tokens_kind": mct_kind,
        "output_cap_effective": effective,
        "cap_source": source,
    }


def _format_output_budget(diag):
    """Render the diagnostics as one stable key=value log segment.

    "-" marks an unrecorded/absent value (never 0: a zero cap is illegal and
    never reaches the value field). String fields are bounded AND sanitized
    with the same convention as _sanitize_upstream_error_value: the cap cuts
    the raw head first, then every C0 control character (including \\r \\n
    \\t) and DEL becomes a space and whitespace runs collapse, so a
    client-controlled model/effort value can never forge a log line inside
    this segment. Numeric fields are never rewritten.
    """
    def _str(value):
        if not isinstance(value, str):
            return "-"
        truncated = len(value) > _OUTPUT_BUDGET_STR_CAP
        text = value[:_OUTPUT_BUDGET_STR_CAP]
        text = "".join(
            " " if (ord(ch) < 0x20 or ord(ch) == 0x7F) else ch
            for ch in text)
        text = " ".join(text.split())
        if not text:
            return "..." if truncated else "-"
        return text + ("..." if truncated else "")

    def _num(value):
        return "-" if value is None else str(value)

    return (
        f"model={_str(diag['model'])} level={_str(diag['level'])} "
        f"max_tokens_present={diag['max_tokens_present']} "
        f"max_tokens={_num(diag['max_tokens'])} "
        f"max_tokens_valid={diag['max_tokens_valid']} "
        f"max_tokens_kind={diag['max_tokens_kind']} "
        f"max_completion_tokens_present={diag['max_completion_tokens_present']} "
        f"max_completion_tokens={_num(diag['max_completion_tokens'])} "
        f"max_completion_tokens_valid={diag['max_completion_tokens_valid']} "
        f"max_completion_tokens_kind={diag['max_completion_tokens_kind']} "
        f"output_cap_effective={_num(diag['output_cap_effective'])} "
        f"cap_source={diag['cap_source']} protocol={_str(diag['protocol'])}")


def _log_upstream_reject(request_id, status_code, model, requested_model,
                         stream, body, response_text, output_budget):
    """One shared upstream 400/422 reject line for chat and Messages.

    Emits the structure-only diagnostics, the sanitized upstream error
    fields and the forwarded output-budget facts inline, so an upstream
    request-validation rejection is decidable from the log alone on BOTH
    conversation protocols. ``response_text`` is the caller's bounded
    (<= 2048 char) diagnostic slice of the upstream body; ``output_budget``
    is the diagnostics dict already recorded for the SAME request, so the
    reject line repeats the forwarded facts verbatim instead of recomputing
    them. ``forwarded_model`` is the model actually FORWARDED upstream
    (the resolved base for a chat virtual model); ``requested_model`` is
    the name the client asked for, which may still be a virtual id — both
    names are correct but different views of one request, so the reject line
    logs them separately and correlating ``forwarded_model=`` with the wire
    never reads the virtual name. A bare ``model=`` grep's first match is
    now the forwarded value (the field name ``forwarded_model=`` ends with
    ``model=``), never the requested one; a word-boundary ``\bmodel=``
    grep no longer matches this line at all. Both are
    client-controlled, so both go through the SAME newline/control-character
    sanitizer as the error fields — the raw values can never forge a second
    log line. The ``output_budget`` segment keeps the forwarded model only.
    """
    diag = _upstream_reject_diagnostics(body)
    err_msg, err_type, err_code = _extract_upstream_reject_error(response_text)
    _logger.warning(
        "upstream_reject request=%s status=%d forwarded_model=%s "
        "requested_model=%s stream=%s error_type=%s error_code=%s "
        "error_message=%r %s output_budget %s",
        request_id, status_code,
        _sanitize_upstream_error_value(model, _OUTPUT_BUDGET_STR_CAP),
        _sanitize_upstream_error_value(requested_model,
                                       _OUTPUT_BUDGET_STR_CAP),
        "stream" if stream else "nonstream",
        err_type or "-", err_code or "-", err_msg, diag,
        _format_output_budget(output_budget))


class _ReplayException:
    def __init__(self, exc):
        self.exc = exc


class _BufferedUpstream:
    """Replay preflight-consumed upstream items, then continue live."""

    def __init__(self, upstream, prefix):
        self._upstream = upstream
        self._prefix = list(prefix)
        self._index = 0

    def next_item(self, timeout_s):
        if self._index < len(self._prefix):
            item = self._prefix[self._index]
            self._index += 1
            if isinstance(item, _ReplayException):
                raise item.exc
            return item
        return self._upstream.next_item(timeout_s)

    def cancel(self):
        self._upstream.cancel()


def _reasoning_retry_preflight(upstream, first_data_deadline,
                               commit_headers=None,
                               client_disconnected=None,
                               headers_already_sent=False,
                               transient_retry_available=False):
    """Hold an early, not-yet-visible prefix long enough for safe recovery.

    Content/tool output is never delayed by this preflight: as soon as either
    appears, buffered bytes are replayed through the normal stream consumer.
    Two failures can be recovered transparently while no visible output has
    reached the client yet:
      * reasoning falls into the same short-cycle repetition twice;
      * the upstream dies/times out before producing visible output.
    The caller owns both retry budgets and decides whether transient recovery
    is currently available via ``transient_retry_available``.
    """
    prefix = []
    event_lines = []
    guard_buf = ""
    guard_signature = None
    saw_substantive = False
    reasoning_chars = 0
    buffered_bytes = 0
    headers_sent = headers_already_sent

    def result(action, retry_reason=None):
        return (action, prefix, saw_substantive, reasoning_chars,
                headers_sent, retry_reason)

    def inspect_event(lines):
        nonlocal guard_buf, guard_signature, saw_substantive
        nonlocal reasoning_chars, headers_sent
        data_lines = [line for line in lines if line.startswith("data:")]
        if not data_lines:
            return "continue"
        values = []
        for line in data_lines:
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            values.append(value)
        payload = "\n".join(values)
        if payload == "[DONE]":
            return "pass"
        if not payload:
            return "continue"
        saw_substantive = True
        try:
            chunk = json.loads(payload)
        except Exception:
            return "pass"
        if not isinstance(chunk, dict):
            return "pass"
        if _explicit_upstream_error_reason(chunk) is not None:
            # An explicit upstream error event is a definitive terminal
            # failure: replay it through the main consumer immediately (the
            # error branch there fails the request) instead of holding it
            # for a transparent retry that would mask the error.
            return "pass"

        reasoning_parts = []
        visible_or_action = False
        saw_finish = False
        for ch in chunk.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                saw_finish = True
            delta = ch.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                visible_or_action = True
            if delta.get("tool_calls") or delta.get("function_call"):
                visible_or_action = True
            rpiece = delta.get("reasoning_content")
            if not isinstance(rpiece, str):
                rpiece = delta.get("reasoning")
            if isinstance(rpiece, str) and rpiece:
                reasoning_parts.append(rpiece)

        if visible_or_action or saw_finish:
            return "pass"
        if not reasoning_parts:
            # Role/metadata-only chunks may precede reasoning. Keep them in
            # the replay buffer briefly rather than sacrificing the chance to
            # recover an early reasoning loop or an upstream disconnect.
            return "continue"

        if not headers_sent and commit_headers is not None:
            commit_headers()
            headers_sent = True

        for part in reasoning_parts:
            reasoning_chars += len(part)
            guard_buf = (guard_buf + part)[-REPETITION_GUARD_BUFFER_CHARS:]
        if len(guard_buf) < REPETITION_GUARD_MIN_TAIL_CHARS:
            return "continue"

        signature = _repetition_guard_signature(guard_buf)
        if signature is None:
            return "pass"
        if guard_signature is None:
            guard_signature = signature
            return "continue"
        if signature == guard_signature:
            return "retry"
        return "pass"

    while True:
        if client_disconnected is not None and client_disconnected():
            return result("client_disconnected")
        if (not saw_substantive and
                time.monotonic() >= first_data_deadline):
            if transient_retry_available:
                return result("upstream_retry", "FirstByteTimeout")
            return result("pass")
        try:
            item = upstream.next_item(STREAM_WATCHDOG_POLL_S)
        except queue.Empty:
            continue
        except Exception as exc:
            if (transient_retry_available and
                    isinstance(exc, _UPSTREAM_TRANSIENT_EXCEPTIONS)):
                return result("upstream_retry", type(exc).__name__)
            prefix.append(_ReplayException(exc))
            return result("pass")

        prefix.append(item)
        if item is _UPSTREAM_END:
            if transient_retry_available:
                return result("upstream_retry", "UpstreamEOF")
            return result("pass")
        buffered_bytes += len(item.encode("utf-8")) + 1
        if buffered_bytes > _REPETITION_PREFLIGHT_MAX_BYTES:
            return result("pass")

        if item:
            event_lines.append(item)
            continue

        action = inspect_event(event_lines)
        event_lines = []
        if action != "continue":
            return result(action)


# _UpstreamStream queue sentinel marking upstream EOF (distinct from an empty
# SSE line "", which is a real streamed line).
_UPSTREAM_END = object()

# Some downstream clients fail to parse streamed
# reasoning increments - GLM (delta without a reasoning field) succeeds while
# DeepSeek (delta.reasoning=...) is rejected after ~2s through the same
# request shape and the same chain. Only the dedicated share-backend instance
# (profile containing "share-backend") reshapes SSE data lines by dropping
# delta reasoning fields before relay; every other instance keeps byte
# fidelity. A line that cannot be parsed is passed through untouched - the
# shim must never swallow a stream on its own hiccup.
STRIP_REASONING_DELTA = False


def _strip_reasoning_delta_line(line: str) -> str:
    if not STRIP_REASONING_DELTA or not line.startswith("data:"):
        return line
    payload = line[len("data:"):].strip()
    if payload == "[DONE]" or not payload.startswith("{"):
        return line
    try:
        obj = json.loads(payload)
    except ValueError:
        return line
    if not isinstance(obj, dict):
        return line
    changed = False
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and (
                "reasoning" in delta or "reasoning_content" in delta):
            delta.pop("reasoning", None)
            delta.pop("reasoning_content", None)
            changed = True
    if not changed:
        return line
    return "data: " + json.dumps(obj, ensure_ascii=False,
                                 separators=(",", ":"))


class _UpstreamStream:
    """Cancellable line iterator over the upstream SSE response.

    requests blocks inside a C-level recv while waiting for upstream data, and
    on Windows neither response.close() nor socket shutdown() reliably
    interrupts that read from another thread (measured with urllib3 2.7.0);
    worse, response.close() from another thread BLOCKS until the upstream
    connection ends. The upstream read therefore runs in a daemon producer
    thread that feeds a bounded queue; the HTTP handler thread consumes it with
    short timeouts so it stays responsive to client disconnects and the
    first-byte budget.

    The producer reads the response ONE BYTE AT A TIME (iter_content(1)) and
    reassembles SSE lines itself, checking cancellation after every byte. That
    is deliberate: iter_lines() only returns control on full lines, so an
    upstream that trickles bytes without ever completing a line would keep
    resetting the socket silence timer and starve the cancel check. With
    per-byte control, a cancelled producer exits at the very next byte; a
    fully silent upstream still releases the reader within the
    STREAM_SOCKET_TIMEOUT_S silence window. Termination is a stable flag
    (_finished/_error) set in the producer's finally - never a queued item -
    so a full queue can never hide the end of the stream from the consumer.

    The producer owns response.close() in its own finally, so the handler
    never touches the upstream socket. Each producer holds one slot of the
    module-level reader semaphore, which caps the total number of concurrent
    reader threads and upstream connections.
    """

    def __init__(self, response, reader_slot=None):
        self.response = response
        self._queue = queue.Queue(maxsize=_UPSTREAM_QUEUE_MAX)
        self._thread = None
        self._slot = reader_slot
        self._finished = False
        self._error = None

    def start(self):
        self._thread = threading.Thread(
            target=self._produce, name="chat2api-upstream-reader", daemon=True
        )
        self._thread.start()

    def _enqueue(self, line):
        """Queue a line with backpressure; return False once cancelled.

        Blocks while the consumer is behind (the upstream read then applies
        TCP backpressure instead of accumulating a huge buffer); drops out as
        soon as the handler cancelled the stream.
        """
        while self._thread is not None:
            try:
                self._queue.put(line, timeout=STREAM_WATCHDOG_POLL_S)
                return True
            except queue.Full:
                continue
        return False

    def _produce(self):
        pending = bytearray()
        try:
            for chunk in self.response.iter_content(chunk_size=1):
                if self._thread is None:
                    break  # cancelled: exit at the very next byte, even mid-line
                pending.extend(chunk)
                if len(pending) > _PRODUCER_LINE_BUFFER_MAX_BYTES:
                    # A line that cannot complete within this bound means the
                    # upstream sent a very large volume with no newline at
                    # all; fail the request instead of pinning memory. This
                    # bound is deliberately far above any legitimate event
                    # (see the constant's comment) and independent of the
                    # 64 KiB prelude bound, which the handler applies only to
                    # lines it actually holds back before the 200 commit.
                    raise ValueError(
                        "upstream produced no line boundary within the "
                        "line buffer bound")
                nl = pending.find(b"\n")
                if nl != -1:
                    line = bytes(pending[:nl]).rstrip(b"\r")
                    del pending[:nl + 1]
                    decoded = line.decode("utf-8", errors="replace")
                    if self._thread is None or not self._enqueue(
                            _strip_reasoning_delta_line(decoded)):
                        break
        except Exception as exc:
            self._error = exc
        finally:
            if pending and self._thread is not None and self._error is None:
                # Trailing partial line with no final newline, delivered only
                # when the read ended cleanly: after an error the tail is a
                # fragment of a broken stream, not data worth replaying.
                try:
                    self._queue.put_nowait(pending.decode("utf-8", "replace"))
                except queue.Full:
                    pass
            self._finished = True
            try:
                self.response.close()
            except Exception:
                pass
            if self._slot is not None:
                # Close the connection first, then return the slot to the
                # reader cap exactly once (only the producer releases here).
                self._slot.release()

    def next_item(self, timeout_s):
        """Return the next SSE line, _UPSTREAM_END, or raise the stored error.

        Raises queue.Empty only while the upstream is still producing. The
        termination state is read from a stable flag (checked in short slices),
        so a full queue or a concurrent producer can never hide it.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            try:
                return self._queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self._finished:
                    if self._error is not None:
                        raise self._error
                    return _UPSTREAM_END

    def cancel(self):
        """Stop consuming immediately; do not touch the upstream socket.

        The producer drops out at its next byte and closes the response in its
        own finally (closing it here would block the handler as long as the
        upstream holds the connection; see class docstring).
        """
        self._thread = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    def _json(self, code: int, obj: dict, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_allow_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        if getattr(self, "close_connection", False):
            # Closing after this response: HTTP/1.1 clients assume keep-alive
            # unless the header says otherwise, so announce it explicitly.
            self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, detail=None,
               error_type="chat2api_error"):
        # Errors may fire before the request body is read (unknown path,
        # rejected control token, invalid local key). Reusing the connection
        # would let those unread bytes be parsed as the next request line
        # ("501 Unsupported method ({json}POST)"), so every error closes it.
        self.close_connection = True
        self._json(code, {"error": {"message": message,
                                    "type": error_type,
                                    "detail": detail}})

    def _reauthentication_error(self, detail=None):
        self._error(
            503,
            "Open WebUI authentication requires an interactive sign-in",
            detail,
            "reauthentication_required",
        )

    def _control_allowed(self) -> bool:
        if not is_local_control_request(
                self.client_address[0],
                self.headers.get("Host", ""),
                self.headers.get("Origin")):
            self._error(403, "Local management endpoint denied")
            return False
        expected = ensure_control_token(CONTROL_TOKEN_FILE)
        supplied = self.headers.get(CONTROL_HEADER, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self._error(403, "Local management token missing or invalid")
            return False
        return True

    def _api_allowed(self) -> bool:
        """Protect public compatibility endpoints when exposed off-loopback."""
        if not getattr(self.server, "require_local_api_key", False):
            return True
        expected = getattr(self.server, "local_api_key", "")
        auth = self.headers.get("Authorization", "")
        supplied = ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            # Anthropic SDKs normally authenticate with x-api-key instead of an
            # Authorization header, so accept the same local secret there too.
            supplied = self.headers.get("x-api-key", "").strip()
        if not expected or not hmac.compare_digest(supplied, expected):
            self._error(401, "Local API key missing or invalid",
                        error_type="local_auth_error")
            return False
        return True

    def _origin_allowed(self) -> bool:
        """Reject browser-originated POSTs on the default-open local setup.

        A web page in ANY site can POST to 127.0.0.1 and would otherwise
        borrow the stored UM identity through this proxy. Local programs
        (curl, ZCode, SDKs) never send Origin, so only requests that CARRY
        one are gated; explicitly allowed origins may be listed in the
        CHAT2API_ALLOWED_ORIGINS env (comma separated).
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin in ALLOWED_ORIGINS:
            return True
        self._error(
            403, "Browser origins are not allowed by default; list the "
            "origin in CHAT2API_ALLOWED_ORIGINS to permit it",
            error_type="origin_denied")
        return False

    def _cors_allow_origin(self):
        """The ACAO value for this response, or None to send no CORS header.

        POST gates already reject browser origins that are not allowlisted;
        withholding the response header too means a forgotten gate on some
        future endpoint cannot silently re-open the browser attack surface.
        """
        origin = self.headers.get("Origin")
        return origin if origin in ALLOWED_ORIGINS else None

    def _read_body(self):
        """Parse one JSON object body; every error is answered inline (None)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._error(400, "Invalid Content-Length header",
                        error_type="invalid_request")
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error(413, "Request body too large",
                        error_type="payload_too_large")
            return None
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._error(400, "Request body is not valid JSON",
                        error_type="invalid_json")
            return None
        if not isinstance(body, dict):
            self._error(400, "Request body must be a JSON object",
                        error_type="invalid_body")
            return None
        return body

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self._cors_allow_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Api-Key, Anthropic-Version, Anthropic-Beta")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy

        if path in ("/", "/health"):
            self._json(200, proxy.health_snapshot())
        elif path == "/ready":
            ready, snapshot = proxy.readiness_snapshot()
            self._json(200 if ready else 503, snapshot)
        elif path == "/admin/auth/status":
            if self._control_allowed():
                self._json(200, proxy.auth_status())
        elif path == "/admin/auth/verify":
            # GET-only, single-shot, read-only credential probe: classify
            # the CURRENT in-process bearer against the upstream catalog.
            # Never call proxy.call()/proxy.refresh() (a 401 verdict must
            # NOT trigger a browser login) and never touch _auth_state or
            # models_cache; this endpoint answers "is the bearer actually
            # accepted right now", not "please fix it".
            if self._control_allowed():
                bearer = proxy.bearer()  # plain dict read, race-benign
                verdict, reason, up_status = classify_credential(
                    proxy.base_url, bearer)
                self._json(200, {
                    "verify": verdict,
                    "reason": reason,
                    "upstream_status": up_status,
                    "checked_epoch": int(time.time()),
                    "note": "single-shot upstream /api/models probe; "
                            "auth state and model cache untouched",
                })
        elif path == "/v1/version":
            if not self._api_allowed():
                return
            try:
                r = requests.get(f"{proxy.base_url}/api/version", timeout=15)
            except requests.RequestException as exc:
                self._error(502, "Failed to fetch upstream version",
                            type(exc).__name__)
                return
            try:
                upstream_version = r.json() if r.status_code == 200 else None
            except ValueError as exc:
                self._error(502, "Upstream version response was not valid JSON",
                            type(exc).__name__)
                return
            finally:
                r.close()
            self._json(200, {
                "openwebui": upstream_version,
                "chat2api": CHAT2API_VERSION,
                "chat2api_build": RUNTIME_BUILD_ID,
                "chat2api_source_rev": CHAT2API_SOURCE_REV,
                "retry_policy": {
                    "repetition_retries": REPETITION_GUARD_AUTO_RETRIES,
                    "upstream_fast_retries": UPSTREAM_AUTO_RETRIES,
                    "upstream_slow_retries": UPSTREAM_SLOW_AUTO_RETRIES,
                    "slow_threshold_seconds": UPSTREAM_SLOW_RETRY_THRESHOLD_S,
                    "max_physical_attempts": MAX_TOTAL_ATTEMPTS,
                    "max_request_seconds": MAX_REQUEST_SECONDS,
                    "max_images": MAX_IMAGES,
                    "auto_trim_historical_images": AUTO_TRIM_HISTORICAL_IMAGES,
                    "auto_trim_current_turn_images":
                        AUTO_TRIM_CURRENT_TURN_IMAGES,
                },
            })
        elif path in ("/v1/models", "/api/models", "/api/v1/models"):
            if not self._api_allowed():
                return
            # The share entry asks for an explicit "current upstream catalog,
            # fresh, no local virtual aliases" reply via an opt-in header. Any
            # other consumer that never sends the header keeps the historical
            # behavior (stale-cache fallback + virtual aliases) unchanged.
            want_raw_fresh = "raw-fresh" in self.headers.get(X_MODELS_HEADER, "").lower()
            try:
                models = proxy.list_models(
                    force=True, raw_only=want_raw_fresh,
                    fresh_required=want_raw_fresh)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(str(exc) or None)
                return
            if models is None:
                self._error(502, "Failed to fetch model list (auth may have expired)")
                return
            extra = {X_MODELS_MARKER: "1"} if want_raw_fresh else None
            self._json(200, models, extra)
        elif path.startswith("/api/"):
            # Authenticated passthrough to other backend endpoints. Requires
            # the control token: this proxy must not lend its upstream bearer
            # to other local processes.
            if not self._control_allowed():
                return
            try:
                r = proxy.call("GET", path)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(str(exc) or None)
                return
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}")

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy

        # Verified share attribution (C2): verify the share entry point's marker
        # ONCE per request and install the facts for EVERY ledger row this
        # request writes. A direct loopback caller carries no marker and is
        # therefore recorded as unattributed - never guessed onto a key.
        if path in ("/v1/messages", "/api/v1/messages"):
            protocol = "messages"
        elif path in ("/v1/chat/completions", "/api/chat/completions",
                      "/api/v1/chat/completions"):
            protocol = "chat"
        else:
            protocol = None
        if protocol is not None:
            key_id, request_id, reason = verify_share_attribution(self.headers)
            set_request_facts(protocol, key_id, request_id, reason)

        if path == "/admin/auth/login":
            if self._control_allowed():
                # These admin handlers never read the request body; the 202
                # must close the connection so leftover bytes cannot desync a
                # reused socket (same failure class as _error). Closing is
                # preferred over draining the unneeded body.
                self.close_connection = True
                proxy.refresh(force=True, wait=False)
                self._json(202, proxy.auth_status())
        elif path == "/admin/shutdown":
            if self._control_allowed():
                self.close_connection = True
                self._json(202, {"status": "stopping"})

                def stop_server():
                    proxy.shutdown()
                    self.server.shutdown()

                threading.Thread(target=stop_server, daemon=True).start()
        elif path in ("/v1/chat/completions", "/api/chat/completions",
                          "/api/v1/chat/completions"):
            if self._api_allowed() and self._origin_allowed():
                self._chat()
        elif path in ("/v1/embeddings", "/api/v1/embeddings"):
            if self._api_allowed() and self._origin_allowed():
                self._compat_proxy("/api/v1/embeddings")
        elif path in ("/v1/messages", "/api/v1/messages"):
            if self._api_allowed() and self._origin_allowed():
                self._compat_proxy("/api/v1/messages", allow_stream=True,
                                   forward_anthropic_headers=True)
        elif path.startswith("/api/"):
            # Same control-token rule as the GET passthrough.
            if not self._control_allowed():
                return
            try:
                body = self._read_body()
                if body is None:
                    return
                r = proxy.call("POST", path, json=body)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(str(exc) or None)
                return
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}")

    def _compat_proxy(self, upstream_path, allow_stream=False,
                      forward_anthropic_headers=False):
        """Proxy a deliberately exposed compatibility endpoint.

        Transient retry rules match chat semantics: fast failures use the full
        upstream retry budget; attempts that already consumed the configured
        slow threshold use the smaller slow-failure budget. Streaming retries
        are allowed only before any SSE event has reached the downstream.

        Anthropic Messages usage is recorded here (C3): the Anthropic protocol
        reported no ledger row at all before, so share Messages traffic was
        invisible to the ledger. The OpenAI embeddings endpoint keeps its
        previous behavior (no ledger row) - it is not a conversation protocol.
        """
        proxy: Proxy = self.server.proxy
        body = self._read_body()
        if body is None:
            return
        stream = allow_stream and bool(body.get("stream"))
        # Anthropic Messages usage accounting is armed ONLY for that protocol,
        # and the stream accumulator only for a stream: a non-stream request
        # carries its complete usage in the body.
        record_messages = upstream_path == "/api/v1/messages"
        messages_model = body.get("model") or ""
        messages_in_est = _message_tokens(body.get("messages"))
        stream_usage = (_AnthropicStreamUsage()
                        if record_messages and stream else None)
        headers = {}
        if forward_anthropic_headers:
            for name in ("Anthropic-Version", "Anthropic-Beta"):
                value = self.headers.get(name)
                if value:
                    headers[name] = value

        deadline = time.monotonic() + MAX_REQUEST_SECONDS
        compat_request_id = secrets.token_hex(6)
        started_compat = time.monotonic()
        # record_usage derives duration_ms from the wall clock, while the log
        # lines below measure against the monotonic start; the ledger therefore
        # needs its own wall-clock start or the row carries a nonsense duration.
        started_compat_wall = time.time()
        logical_attempts = 0
        transient_retries_done = 0
        slow_retries_done = 0
        repetition_retries_done = 0
        physical_budget = _PhysicalRequestBudget(MAX_TOTAL_ATTEMPTS)
        if record_messages:
            # B7-7: Anthropic Messages carries a REQUIRED max_tokens; state
            # once which value this request forwards, controlled values only
            # (no message content, no credentials). Reused verbatim by the
            # 400/422 reject diagnostics below. The embeddings path logs
            # nothing here - it is not a conversation protocol and has no
            # output budget.
            messages_output_budget = _output_budget_diagnostics(
                body, messages_model, None, "messages")
            _logger.info(
                "output budget request=%s %s", compat_request_id,
                _format_output_budget(messages_output_budget))

        def record_messages_usage(status, payload=None):
            """Append ONE ledger row for this logical Messages request.

            Called exactly once per request: every terminal path below ends in a
            ``return``, while a retry path CONTINUES the loop and never
            records, so several upstream attempts of one logical request can
            never be counted twice. A non-stream 200 body carries the complete
            usage; a stream carries it through ``stream_usage``. An interrupted
            stream records its known lower bound with a non-"ok" status instead
            of a fabricated complete success.
            """
            if not record_messages:
                return
            usage, usage_source, out_est = None, None, 0
            if stream_usage is not None:
                usage = stream_usage.usage()
                usage_source = stream_usage.source()
                out_est = stream_usage.output_estimate()
            elif payload is not None:
                # The coverage is DERIVED by the normalization boundary
                # (record_usage), exactly like the stream path's source():
                # a complete block is "upstream", a single-side block is
                # "partial" (the other side stays the local estimate), and a
                # body without a usage block falls back to the estimates.
                # Hardcoding "upstream" for any non-None block would let a
                # single-side report masquerade as full upstream coverage.
                usage = _anthropic_message_usage(payload)
                out_est = _anthropic_output_estimate(payload)
            record_usage(messages_model, stream, started_compat_wall, usage,
                         messages_in_est, out_est, status=status,
                         usage_source=usage_source)

        def retry_available(slow):
            return (UPSTREAM_AUTO_RETRY and
                    transient_retries_done < UPSTREAM_AUTO_RETRIES and
                    (not slow or
                     slow_retries_done < UPSTREAM_SLOW_AUTO_RETRIES) and
                    physical_budget.remaining > 0 and
                    time.monotonic() < deadline)

        def schedule_retry(reason, attempt_elapsed_s, response=None,
                           stream_retry=False):
            nonlocal transient_retries_done, slow_retries_done
            slow = _attempt_is_slow(attempt_elapsed_s)
            if not retry_available(slow):
                return False
            transient_retries_done += 1
            if slow:
                slow_retries_done += 1
            delay = _retry_delay_seconds(
                transient_retries_done, attempt_elapsed_s,
                response=response, slow=slow)
            _logger.warning(
                "%s path=%s retry=%d/%d slow_retry=%d/%d "
                "physical_attempt=%d/%d class=%s reason=%s delay_s=%.2f",
                "compat stream-retry" if stream_retry else "compat auto-retry",
                upstream_path, transient_retries_done, UPSTREAM_AUTO_RETRIES,
                slow_retries_done, UPSTREAM_SLOW_AUTO_RETRIES,
                physical_budget.used, MAX_TOTAL_ATTEMPTS,
                "slow" if slow else "fast", reason, delay)
            return _sleep_with_deadline(delay, deadline)

        def log_summary(final):
            _logger.info(
                "request summary request=%s endpoint=%s final=%s "
                "physical_attempts=%d/%d logical_attempts=%d "
                "upstream_retries=%d slow_retries=%d "
                "repetition_retries=%d duration_ms=%d",
                compat_request_id, upstream_path, final,
                physical_budget.used, MAX_TOTAL_ATTEMPTS, logical_attempts,
                transient_retries_done, slow_retries_done,
                repetition_retries_done,
                int((time.monotonic() - started_compat) * 1000))

        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                self._error(
                    502, "Upstream compatibility endpoint exceeded the request deadline",
                    error_type="request_deadline_exceeded")
                log_summary("request_deadline_exceeded")
                record_messages_usage("request_deadline_exceeded")
                return
            if physical_budget.remaining <= 0:
                self._error(
                    502, "Upstream compatibility endpoint exhausted the physical request budget",
                    error_type="upstream_attempt_budget_exhausted")
                log_summary("physical_budget_exhausted")
                record_messages_usage("physical_budget_exhausted")
                return

            logical_attempts += 1
            attempt_started = time.monotonic()
            response = None
            try:
                response = proxy.call(
                    "POST", upstream_path, json=body, stream=stream,
                    headers=headers,
                    timeout=(max(0.05, min(15.0, remaining_s)),
                             max(0.05, min(180.0, remaining_s))),
                    physical_budget=physical_budget,
                    request_deadline=deadline)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(str(exc) or None)
                log_summary("reauthentication_required")
                record_messages_usage("reauthentication_required")
                return
            except (_RequestDeadlineExceeded, _PhysicalBudgetExceeded) as exc:
                error_type = ("request_deadline_exceeded"
                              if isinstance(exc, _RequestDeadlineExceeded)
                              else "upstream_attempt_budget_exhausted")
                self._error(502, "Upstream compatibility endpoint could not start another attempt",
                            type(exc).__name__, error_type=error_type)
                log_summary(error_type)
                record_messages_usage(error_type)
                return
            except requests.RequestException as exc:
                elapsed = time.monotonic() - attempt_started
                if schedule_retry(type(exc).__name__, elapsed):
                    continue
                self._error(502, "Failed to reach upstream compatibility endpoint",
                            type(exc).__name__)
                log_summary(type(exc).__name__)
                record_messages_usage(type(exc).__name__)
                return

            elapsed = time.monotonic() - attempt_started
            if response.status_code in _UPSTREAM_RETRYABLE_STATUS:
                status_code = response.status_code
                if retry_available(_attempt_is_slow(elapsed)):
                    delay_response = response
                    should_retry = schedule_retry(
                        f"HTTP{status_code}", elapsed, response=delay_response)
                    if should_retry:
                        response.close()
                        continue
                    response.close()
                    self._error(
                        502, "Upstream compatibility endpoint reached the request deadline",
                        f"HTTP {status_code}",
                        error_type="request_deadline_exceeded")
                    log_summary("request_deadline_exceeded")
                    record_messages_usage("request_deadline_exceeded")
                    return
                # Retry budget exhausted: preserve the compatibility endpoint's
                # historical behavior and pass the last upstream status/body
                # through instead of rewriting it to a synthetic 502.

            if stream and response.status_code == 200:
                try:
                    stream_status = self._passthrough_stream(
                        response, request_deadline=deadline,
                        request_id=compat_request_id, usage=stream_usage)
                except _CompatStreamRetryable as exc:
                    # _passthrough_stream owns/closes the response. No body was
                    # committed, so reopening the upstream cannot concatenate.
                    elapsed = time.monotonic() - attempt_started
                    reason = str(exc)
                    if reason.startswith("repetition loop detected"):
                        if (REPETITION_GUARD_AUTO_RETRY and
                                repetition_retries_done <
                                REPETITION_GUARD_AUTO_RETRIES and
                                physical_budget.remaining > 0 and
                                time.monotonic() < deadline):
                            repetition_retries_done += 1
                            _logger.warning(
                                "compat repetition auto-retry path=%s "
                                "retry=%d/%d physical_attempt=%d/%d",
                                upstream_path, repetition_retries_done,
                                REPETITION_GUARD_AUTO_RETRIES,
                                physical_budget.used, MAX_TOTAL_ATTEMPTS)
                            continue
                        _logger.warning(
                            "compat chat failure request=%s endpoint=%s "
                            "stage=degenerate_repetition phase=precommit "
                            "repetition_retries=%d physical_attempts=%d/%d "
                            "duration_ms=%d",
                            compat_request_id, upstream_path,
                            repetition_retries_done, physical_budget.used,
                            MAX_TOTAL_ATTEMPTS,
                            int((time.monotonic() - started_compat) * 1000))
                        self._error(
                            502, "Upstream model fell into a repetition loop",
                            detail=reason,
                            error_type="degenerate_repetition")
                        log_summary("degenerate_repetition")
                        record_messages_usage("degenerate_repetition")
                        return
                    if schedule_retry(str(exc), elapsed, stream_retry=True):
                        continue
                    self._error(
                        502, "Upstream compatibility stream failed before any event",
                        detail=str(exc), error_type="upstream_stream_error")
                    log_summary(f"stream:{exc}")
                    record_messages_usage("upstream_stream_error")
                    return
                if stream_status == "degenerate_repetition":
                    _logger.warning(
                        "compat chat failure request=%s endpoint=%s "
                        "stage=degenerate_repetition phase=postcommit "
                        "physical_attempts=%d/%d duration_ms=%d",
                        compat_request_id, upstream_path,
                        physical_budget.used, MAX_TOTAL_ATTEMPTS,
                        int((time.monotonic() - started_compat) * 1000))
                log_summary(stream_status or "ok")
                record_messages_usage(stream_status or "ok")
                return

            status_code = response.status_code
            # A non-stream 200 carries the complete usage in its body; read it
            # BEFORE _passthrough closes the response. A 400/422 reject line
            # needs the same early read: the diagnostic slice uses the same
            # read and cap as the chat path, and reading it only materializes
            # r.content (which _passthrough drains anyway), so the forwarded
            # bytes are unchanged. Any other status is the upstream's own
            # failure and is recorded as such, never as a fabricated success.
            response_text = ""
            if status_code in (400, 422):
                try:
                    response_text = response.text[:2048]
                except Exception:
                    response_text = ""
            payload = None
            if record_messages and status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
            if record_messages and status_code in (400, 422):
                # Same inline output-budget review as the chat path: the
                # Messages-required max_tokens (B7-7) repeats on the reject
                # line from the diagnostics recorded above. Messages has no
                # virtual-model resolution, so the forwarded model and the
                # requested model are the same value; both fields are logged
                # for a uniform reject-line shape across the two protocols.
                _log_upstream_reject(
                    compat_request_id, status_code, model=messages_model,
                    requested_model=messages_model, stream=stream, body=body,
                    response_text=response_text,
                    output_budget=messages_output_budget)
            self._passthrough(response)
            log_summary(f"HTTP{status_code}")
            record_messages_usage(f"HTTP{status_code}", payload=payload)
            return

    def _chat(self):
        proxy: Proxy = self.server.proxy
        body = self._read_body()
        if body is None:
            return
        if not body.get("messages"):
            self._error(400, "Request body is missing the messages field")
            return

        req_model = body.get("model") or ""
        started_at = time.time()
        # Per-request id so every failure log line of one downstream request
        # can be tied together in chat2api.log; it is written only to the log
        # and never added to any response body.
        request_id = secrets.token_hex(6)
        stream = bool(body.get("stream"))

        # Normalize the known UM image budget exactly once per downstream
        # request. Every subsequent compatibility/transient/repetition retry
        # reuses this same body, so retries cannot progressively discard more
        # historical images.
        body, image_stats, image_error = _normalize_chat_image_budget(body)
        if image_error is not None:
            _logger.warning(
                "image budget rejected request=%s model=%s type=%s "
                "images_before=%d historical_images=%d current_turn_images=%d "
                "limit=%d",
                request_id, req_model, image_error["type"],
                image_stats["images_before"], image_stats["historical_images"],
                image_stats["current_turn_images"], MAX_IMAGES)
            self._error(400, image_error["message"],
                        error_type=image_error["type"])
            _logger.info(
                "request summary request=%s final=%s physical_attempts=0/%d "
                "upstream_retries=0 slow_retries=0 repetition_retries=0 "
                "images_before=%d images_after=%d images_trimmed=%d duration_ms=%d",
                request_id, image_error["type"], MAX_TOTAL_ATTEMPTS,
                image_stats["images_before"], image_stats["images_after"],
                image_stats["images_trimmed"],
                int((time.time() - started_at) * 1000))
            return
        if image_stats["images_trimmed"]:
            # Dropping images the user submitted THIS turn is opt-in
            # unattended recovery - make it visible at WARNING level.
            budget_log = (_logger.warning
                          if image_stats["current_turn_trimmed"]
                          else _logger.info)
            budget_log(
                "image budget normalized request=%s model=%s images_before=%d "
                "images_after=%d historical_images=%d current_turn_images=%d "
                "historical_removed=%d current_turn_trimmed=%d",
                request_id, req_model, image_stats["images_before"],
                image_stats["images_after"], image_stats["historical_images"],
                image_stats["current_turn_images"],
                image_stats["historical_removed"],
                image_stats["current_turn_trimmed"])

        # Virtual model -> real model + reasoning effort.
        base, effort = resolve_effort(req_model)
        if base and effort is not None:
            body = dict(body)
            body["model"] = base
            body["reasoning_effort"] = effort

        if stream:
            # Ask the backend for a usage chunk so accounting is real, not
            # estimated; backends that reject the option get one plain retry.
            body.setdefault("stream_options", {"include_usage": True})

        # B7-7: state once, at the point where the request body is complete
        # (image budget normalized, virtual model resolved, stream_options
        # set), WHICH output cap this request forwards upstream. Controlled
        # values only - the line never carries message content or
        # credentials. Reused verbatim by the 400/422 reject diagnostics
        # below; the later stream_options pop never touches output-cap
        # fields, so the recorded facts stay the forwarded facts.
        output_budget = _output_budget_diagnostics(
            body, body.get("model") or "", body.get("reasoning_effort"),
            "chat")
        _logger.info("output budget request=%s %s", request_id,
                     _format_output_budget(output_budget))

        def log_failure(stage, downstream_code, upstream_status=None,
                        exc_name=None):
            # _error() intentionally stays log-free; this is the one place
            # where each failure is classified at its origin. Only the
            # discriminating fields are logged - upstream status code or
            # exception class name - never the upstream response body, the
            # request messages, or credentials.
            _logger.warning(
                "chat failure request=%s stage=%s upstream_status=%s "
                "exception=%s downstream=%d stream=%s model=%s duration_ms=%d",
                request_id, stage,
                "-" if upstream_status is None else upstream_status,
                exc_name or "-", downstream_code, stream, req_model,
                int((time.time() - started_at) * 1000))

        physical_budget = _PhysicalRequestBudget(MAX_TOTAL_ATTEMPTS)
        transient_retries_done = 0
        slow_retries_done = 0
        repetition_retries_done = 0
        compat_retry_seen = False

        # Hard wall-clock bound: attempt counts alone allow a silent gateway
        # (e.g. 120s first-byte budget x 6 attempts ≈ 12 minutes) to pin an
        # unattended client. Beyond this the request stops retrying and
        # surfaces its last failure immediately.
        request_deadline = time.monotonic() + MAX_REQUEST_SECONDS

        def chat_call_timeout():
            """Per-attempt socket read timeout capped by the request deadline:
            a silent upstream can never outlive the wall-clock budget."""
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise _RequestDeadlineExceeded(
                    "chat request wall-clock deadline exhausted")
            read_cap = STREAM_SOCKET_TIMEOUT_S if stream else 600
            return (max(0.05, min(15.0, remaining)),
                    max(0.05, min(read_cap, remaining)))

        def transient_retry_available(slow=False):
            return (UPSTREAM_AUTO_RETRY and
                    transient_retries_done < UPSTREAM_AUTO_RETRIES and
                    (not slow or
                     slow_retries_done < UPSTREAM_SLOW_AUTO_RETRIES) and
                    physical_budget.remaining > 0 and
                    time.monotonic() < request_deadline)

        def log_upstream_retry(reason, delay_s, slow):
            _logger.warning(
                "upstream auto-retry request=%s model=%s retry=%d/%d "
                "slow_retry=%d/%d physical_attempt=%d/%d class=%s "
                "reason=%s delay_s=%.2f",
                request_id, req_model, transient_retries_done,
                UPSTREAM_AUTO_RETRIES, slow_retries_done,
                UPSTREAM_SLOW_AUTO_RETRIES, physical_budget.used,
                MAX_TOTAL_ATTEMPTS, "slow" if slow else "fast",
                reason, delay_s)

        def open_chat_response():
            """Open one chat response, transparently retrying transient setup failures.

            Every real HTTP request consumes the shared physical budget,
            including a hidden post-401 re-send inside Proxy.call(). The
            stream_options compatibility retry does not consume the transient
            failure budget, but it still consumes one physical request.
            """
            nonlocal transient_retries_done, slow_retries_done, compat_retry_seen
            compat_retry_used = False
            while True:
                if time.monotonic() >= request_deadline:
                    raise _RequestDeadlineExceeded(
                        "chat request wall-clock deadline exhausted")
                if physical_budget.remaining <= 0:
                    raise _PhysicalBudgetExceeded(
                        "chat physical request budget exhausted")
                attempt_started = time.monotonic()
                try:
                    rr = proxy.call(
                        "POST", "/api/chat/completions", json=body,
                        stream=stream, timeout=chat_call_timeout(),
                        physical_budget=physical_budget,
                        request_deadline=request_deadline)
                except ReauthenticationRequired:
                    raise
                except _UPSTREAM_TRANSIENT_EXCEPTIONS as exc:
                    elapsed = time.monotonic() - attempt_started
                    slow = _attempt_is_slow(elapsed)
                    if transient_retry_available(slow):
                        transient_retries_done += 1
                        if slow:
                            slow_retries_done += 1
                        delay_s = _retry_delay_seconds(
                            transient_retries_done, elapsed, slow=slow)
                        log_upstream_retry(type(exc).__name__, delay_s, slow)
                        if _sleep_with_deadline(delay_s, request_deadline):
                            continue
                        raise _RequestDeadlineExceeded(
                            "chat retry delay reached request deadline")
                    raise
                except requests.RequestException:
                    raise

                if (rr.status_code == 400 and stream and
                        "stream_options" in body):
                    # Compatibility retry is pre-existing behavior, not an
                    # upstream-health retry. Respect the global attempt cap.
                    if physical_budget.remaining <= 0:
                        return rr, compat_retry_used
                    rr.close()
                    body.pop("stream_options", None)
                    compat_retry_used = True
                    compat_retry_seen = True
                    continue

                if rr.status_code in _UPSTREAM_RETRYABLE_STATUS:
                    elapsed = time.monotonic() - attempt_started
                    slow = _attempt_is_slow(elapsed)
                    if not transient_retry_available(slow):
                        return rr, compat_retry_used
                    transient_retries_done += 1
                    if slow:
                        slow_retries_done += 1
                    delay_s = _retry_delay_seconds(
                        transient_retries_done, elapsed,
                        response=rr, slow=slow)
                    status_code = rr.status_code
                    rr.close()
                    log_upstream_retry(f"HTTP{status_code}", delay_s, slow)
                    if _sleep_with_deadline(delay_s, request_deadline):
                        continue
                    raise _RequestDeadlineExceeded(
                        "chat retry delay reached request deadline")
                return rr, compat_retry_used

        # Concurrency cap for streaming readers: acquire BEFORE any upstream
        # access, so a full limit refuses with a clean 503 without ever
        # touching the upstream. From here on the streaming slot is owned by
        # this handler inside ONE finally: before the reader producer takes
        # over, every path closes the current response and returns the slot;
        # once start() succeeds the producer owns both and releases the slot.
        reader_slot = _reader_slots if stream else None
        if reader_slot is not None and not reader_slot.acquire(blocking=False):
            log_failure("concurrency_limit_rejected", 503)
            self._error(
                503, "Too many concurrent streaming requests",
                f"concurrent reader limit {STREAM_MAX_CONCURRENT_READERS} "
                "reached; retry later", "too_many_concurrent_streams")
            return
        # Streaming reads are bounded per socket read by chat_call_timeout()
        # above (STREAM_SOCKET_TIMEOUT_S cap, also clipped to the request
        # deadline); the non-stream path uses a flat 600s read cap there too.
        response = None
        compat_retry_used = False
        try:
            try:
                r, compat_retry_used = open_chat_response()
            except ReauthenticationRequired as exc:
                log_failure("reauthentication_required", 503,
                            exc_name=type(exc).__name__)
                self._reauthentication_error(str(exc) or None)
                return
            except (_RequestDeadlineExceeded, _PhysicalBudgetExceeded) as exc:
                stage = ("request_deadline_exceeded"
                         if isinstance(exc, _RequestDeadlineExceeded)
                         else "physical_budget_exhausted")
                log_failure(stage, 502, exc_name=type(exc).__name__)
                self._error(
                    502, "Upstream request could not start another attempt",
                    type(exc).__name__,
                    "request_deadline_exceeded" if
                    isinstance(exc, _RequestDeadlineExceeded) else
                    "upstream_attempt_budget_exhausted")
                return
            except requests.RequestException as exc:
                log_failure("compat_retry_request_exception"
                            if compat_retry_seen else
                            "upstream_request_exception", 502,
                            exc_name=type(exc).__name__)
                self._error(502, "Upstream request failed", type(exc).__name__)
                return
            response = r
            if r.status_code != 200:
                status_code = r.status_code
                # Capture only the small diagnostic after requests has the
                # response object; never log the body or credentials. The cap
                # bounds the diagnostic slice, not the rejection semantics —
                # 2048 chars keeps complete vLLM-style limit messages
                # ("maximum context length ... your prompt has N tokens")
                # inside the extractable window; longer bodies are still
                # re-closed by _repair_truncated_json_prefix.
                try:
                    response_text = r.text[:2048]
                except Exception:
                    response_text = ""
                r.close()
                if status_code in (400, 422):
                    # Request-validation failure: mapping it to 502 would let
                    # ZCode replay the SAME payload as a transient failure and
                    # re-reject it forever. Surface the real status, never
                    # enter transient retry, and log structure-only
                    # diagnostics (counts/ids/shape hash, no content) through
                    # the shared reject line. model= is the RESOLVED model
                    # this request forwarded (body["model"] after virtual-
                    # model resolution, the same value the output_budget
                    # segment repeats); requested_model= is the client's own
                    # name, which may still be a virtual id.
                    _log_upstream_reject(
                        request_id, status_code,
                        model=body.get("model") or "",
                        requested_model=req_model, stream=stream, body=body,
                        response_text=response_text,
                        output_budget=output_budget)
                    self._error(
                        status_code,
                        "Upstream rejected the request; the server log has "
                        "structured diagnostics",
                        error_type="upstream_request_rejected")
                    return
                log_failure("compat_retry_non_200" if compat_retry_used
                            else "upstream_non_200", 502,
                            upstream_status=status_code)
                self._error(502, "Upstream request failed",
                            f"HTTP {status_code}: {response_text}")
                return

            if not stream:
                try:
                    data = r.json()
                except ValueError as exc:
                    r.close()
                    log_failure("upstream_invalid_json", 502,
                                upstream_status=r.status_code,
                                exc_name=type(exc).__name__)
                    self._error(502, "Upstream returned invalid JSON",
                                type(exc).__name__)
                    return
                r.close()
                if not isinstance(data, dict):
                    log_failure("upstream_invalid_json_shape", 502,
                                upstream_status=r.status_code,
                                exc_name="InvalidResponseShape")
                    self._error(502, "Upstream returned an invalid JSON shape",
                                type(data).__name__)
                    return
                error_reason = _explicit_upstream_error_reason(data)
                if error_reason is not None:
                    # Symmetric with the stream path's explicit-error branch:
                    # a truthy top-level "error" is the failure shape Open
                    # WebUI uses for streams (the same middleware wrapper), so
                    # a 200 body carrying it is treated as a definitive
                    # terminal failure too - never relayed as a successful
                    # completion, never accounted as "ok". Only the sanitized,
                    # bounded reason travels (the raw object may carry
                    # credential material); no usage row, the same accounting
                    # rule as every other terminal upstream failure.
                    log_failure("upstream_explicit_error", 502,
                                upstream_status=r.status_code)
                    _logger.warning(
                        "upstream explicit error request=%s model=%s "
                        "message=%r", request_id, req_model, error_reason)
                    self._error(502, "Upstream request failed",
                                {"reason": "upstream_explicit_error",
                                 "upstream_message": error_reason},
                                "upstream_explicit_error")
                    return
                self._json(200, data)
                usage = data.get("usage")
                out_est = 0
                try:
                    content = data["choices"][0]["message"]["content"]
                    out_est = _est_tokens(content)
                except Exception:
                    pass
                record_usage(req_model, False, started_at, usage,
                             _message_tokens(body.get("messages")), out_est)
                _logger.info(
                    "request summary request=%s final=ok physical_attempts=%d/%d "
                    "upstream_retries=%d slow_retries=%d "
                    "images_before=%d images_after=%d images_trimmed=%d duration_ms=%d",
                    request_id, physical_budget.used, MAX_TOTAL_ATTEMPTS,
                    transient_retries_done, slow_retries_done,
                    image_stats["images_before"], image_stats["images_after"],
                    image_stats["images_trimmed"],
                    int((time.time() - started_at) * 1000))
                return

            # Streaming: forward SSE lines as-is. Two gotchas that break strict
            # clients (e.g. the eventsource-parser used by several agents):
            #   1. SSE events MUST be separated by a blank line (\n\n); otherwise
            #      the parser accumulates every `data:` line into one event and the
            #      whole stream fails to parse (silent "empty response").
            #   2. Some upstreams keep the connection alive after [DONE], so we stop
            #      reading at [DONE] and close the downstream connection to give the
            #      client a prompt end-of-stream.
            # The 200 headers are held back until the first substantive data
            # event ([DONE] is not content). Before visible output, transient
            # gateway/network failures may be retried transparently; after
            # visible output, errors stay in-band because replaying a second
            # completion would concatenate two answers.
            upstream = _UpstreamStream(r, reader_slot=reader_slot)
            # Trace headers of the response that feeds the visible stream: the
            # stream-end log line reports these so a production stall carries
            # the upstream request id it can be claimed by.
            upstream_trace = _upstream_trace_headers(r)
            try:
                upstream.start()
            except Exception as exc:
                log_failure("producer_start_failed", 502,
                            exc_name=type(exc).__name__)
                self._error(502, "Upstream stream failed to start",
                            "producer thread could not start",
                            "upstream_stream_error")
                return
            reader_slot = None  # ownership transferred to the producer
        finally:
            if reader_slot is not None:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                reader_slot.release()
        first_data_deadline = time.monotonic() + min(
            STREAM_FIRST_BYTE_TIMEOUT_S,
            max(0.05, request_deadline - time.monotonic()))
        preflight_headers_sent = False
        preflight_saw_substantive = False

        def commit_preflight_headers():
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            origin = self._cors_allow_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()

        def fail_hidden_retry(exc_name):
            """Surface a failed hidden retry according to header state."""
            self.close_connection = True
            detail = self._stream_error_detail(
                "upstream_read_error", started_at, 0, exc_name)
            try:
                if preflight_headers_sent:
                    self._stream_error_event("upstream_read_error", detail)
                else:
                    self._error(
                        502, STREAM_ERROR_MESSAGES["upstream_read_error"],
                        detail, "upstream_stream_error")
            except (ConnectionError, OSError, ValueError):
                pass
            record_usage(
                req_model, True, started_at, None,
                _message_tokens(body.get("messages")), 0,
                status="upstream_read_error")

        def start_hidden_retry_attempt(stage):
            """Open a fresh streaming attempt while no visible body was sent."""
            nonlocal upstream_trace
            retry_slot = _reader_slots
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                log_failure(f"{stage}_retry_deadline", 502,
                            exc_name="RequestDeadlineExceeded")
                fail_hidden_retry("RequestDeadlineExceeded")
                return None
            if not retry_slot.acquire(timeout=min(2.0, remaining)):
                log_failure(f"{stage}_retry_concurrency_limit", 502,
                            exc_name="RetrySlotUnavailable")
                fail_hidden_retry("RetrySlotUnavailable")
                return None

            retry_response = None
            slot_owned = True
            try:
                try:
                    rr, _compat = open_chat_response()
                except ReauthenticationRequired as exc:
                    log_failure(f"{stage}_retry_reauthentication_required",
                                502, exc_name=type(exc).__name__)
                    fail_hidden_retry(type(exc).__name__)
                    return None
                except (_RequestDeadlineExceeded,
                        _PhysicalBudgetExceeded) as exc:
                    log_failure(f"{stage}_retry_budget_exhausted", 502,
                                exc_name=type(exc).__name__)
                    fail_hidden_retry(type(exc).__name__)
                    return None
                except requests.RequestException as exc:
                    log_failure(f"{stage}_retry_request_exception", 502,
                                exc_name=type(exc).__name__)
                    fail_hidden_retry(type(exc).__name__)
                    return None
                retry_response = rr

                if rr.status_code != 200:
                    status_code = rr.status_code
                    rr.close()
                    retry_response = None
                    log_failure(f"{stage}_retry_non_200", 502,
                                upstream_status=status_code)
                    fail_hidden_retry(f"HTTP{status_code}")
                    return None

                retry_upstream = _UpstreamStream(rr, reader_slot=retry_slot)
                try:
                    retry_upstream.start()
                except Exception as exc:
                    log_failure(f"{stage}_retry_producer_start_failed", 502,
                                exc_name=type(exc).__name__)
                    fail_hidden_retry(type(exc).__name__)
                    return None
                # The retry response now feeds the visible stream: the
                # stream-end log line must report ITS trace headers, not the
                # discarded attempt's.
                upstream_trace = _upstream_trace_headers(rr)
                slot_owned = False
                return retry_upstream
            finally:
                if slot_owned:
                    if retry_response is not None:
                        try:
                            retry_response.close()
                        except Exception:
                            pass
                    retry_slot.release()

        repetition_final_origin = None
        preflight_enabled = (
            (REPETITION_GUARD_ENABLED and REPETITION_GUARD_AUTO_RETRY and
             REPETITION_GUARD_AUTO_RETRIES > 0) or
            (UPSTREAM_AUTO_RETRY and UPSTREAM_AUTO_RETRIES > 0))
        if preflight_enabled:
            stream_attempt_started = time.monotonic()
            while True:
                can_retry_transient = transient_retry_available()
                try:
                    action, prefix, saw_substantive, discarded_reasoning_chars, \
                        preflight_headers_sent, retry_reason = \
                        _reasoning_retry_preflight(
                            upstream, first_data_deadline,
                            commit_headers=(None if preflight_headers_sent
                                            else commit_preflight_headers),
                            client_disconnected=lambda: _client_disconnected(
                                self.connection),
                            headers_already_sent=preflight_headers_sent,
                            transient_retry_available=can_retry_transient)
                except (ConnectionError, OSError):
                    upstream.cancel()
                    self.close_connection = True
                    record_usage(
                        req_model, True, started_at, None,
                        _message_tokens(body.get("messages")), 0,
                        status="client_disconnected")
                    return

                preflight_saw_substantive = saw_substantive
                if action == "client_disconnected":
                    upstream.cancel()
                    self.close_connection = True
                    record_usage(
                        req_model, True, started_at, None,
                        _message_tokens(body.get("messages")),
                        discarded_reasoning_chars // 4,
                        status="client_disconnected")
                    return

                if action == "upstream_retry":
                    # The preflight only emits this action when the transient
                    # and total budgets both allow a new attempt.
                    elapsed = time.monotonic() - stream_attempt_started
                    slow = _attempt_is_slow(elapsed)
                    if not transient_retry_available(slow):
                        upstream.cancel()
                        fail_hidden_retry(retry_reason or "StreamFailure")
                        return
                    transient_retries_done += 1
                    if slow:
                        slow_retries_done += 1
                    upstream.cancel()
                    delay_s = _retry_delay_seconds(
                        transient_retries_done, elapsed, slow=slow)
                    log_upstream_retry(
                        retry_reason or "StreamFailure", delay_s, slow)
                    if discarded_reasoning_chars > 0:
                        # A zero-data transport failure consumed no observable
                        # model output, so keep the usage ledger one-record-per-
                        # final turn. If reasoning was generated then discarded,
                        # retain an estimate so retry cost is not hidden.
                        record_usage(
                            req_model, True, started_at, None,
                            _message_tokens(body.get("messages")),
                            discarded_reasoning_chars // 4,
                            status="upstream_transient_retried")
                    if not _sleep_with_deadline(delay_s, request_deadline):
                        fail_hidden_retry("RequestDeadlineExceeded")
                        return
                    upstream = start_hidden_retry_attempt("upstream")
                    if upstream is None:
                        return
                    first_data_deadline = time.monotonic() + min(
                        STREAM_FIRST_BYTE_TIMEOUT_S,
                        max(0.05, request_deadline - time.monotonic()))
                    stream_attempt_started = time.monotonic()
                    preflight_saw_substantive = False
                    continue

                if action != "retry":
                    upstream = _BufferedUpstream(upstream, prefix)
                    break

                # The guard switch is independent of the retry switches: with
                # CHAT2API_REPETITION_GUARD=0 the preflight must not classify
                # or retry repetition at all, even when it stays active for
                # transport recovery. Replay the buffered data verbatim; the
                # main loop (guard off) passes the repetition through as-is.
                if not REPETITION_GUARD_ENABLED:
                    upstream = _BufferedUpstream(upstream, prefix)
                    break

                # Preflight may still be active solely for transport recovery.
                # Respect the dedicated repetition-retry switch even in that
                # mixed mode; disabling CHAT2API_REPETITION_AUTO_RETRY must not
                # silently keep repetition retries enabled via UPSTREAM_AUTO_RETRY.
                if not REPETITION_GUARD_AUTO_RETRY:
                    repetition_final_origin = "preflight_retry_disabled"
                    upstream = _BufferedUpstream(upstream, prefix)
                    break

                if (repetition_retries_done >= REPETITION_GUARD_AUTO_RETRIES or
                        physical_budget.remaining <= 0 or
                        time.monotonic() >= request_deadline):
                    # Final failed attempt: replay it through the established
                    # guard so the client gets the normal in-band repetition
                    # error instead of another hidden retry. Remember that the
                    # terminal repetition originated before any SSE body bytes
                    # were released, even though replay will now expose it.
                    repetition_final_origin = "preflight_retry_exhausted"
                    upstream = _BufferedUpstream(upstream, prefix)
                    break

                repetition_retries_done += 1
                upstream.cancel()
                _logger.warning(
                    "repetition auto-retry request=%s model=%s retry=%d/%d "
                    "physical_attempt=%d/%d discarded_reasoning_chars=%d",
                    request_id, req_model, repetition_retries_done,
                    REPETITION_GUARD_AUTO_RETRIES, physical_budget.used,
                    MAX_TOTAL_ATTEMPTS, discarded_reasoning_chars)
                record_usage(
                    req_model, True, started_at, None,
                    _message_tokens(body.get("messages")),
                    discarded_reasoning_chars // 4,
                    status="degenerate_repetition_retried")

                upstream = start_hidden_retry_attempt("repetition")
                if upstream is None:
                    return
                first_data_deadline = time.monotonic() + min(
                    STREAM_FIRST_BYTE_TIMEOUT_S,
                    max(0.05, request_deadline - time.monotonic()))
                stream_attempt_started = time.monotonic()
                preflight_saw_substantive = False
        usage = None
        completion_chars = 0
        reasoning_chars = 0
        guard_buf = ""
        guard_streak = 0
        guard_signature = None
        line_loop_observer = _LineLoopObserver(request_id, "chat_completions")
        saw_data_lines = 0
        saw_finish_reason = False
        done_seen = False
        headers_sent = preflight_headers_sent
        # Track downstream observability separately from HTTP header commit.
        # Early reasoning preflight may commit a 200 while intentionally holding
        # every SSE body byte so a hidden retry is still safe.
        downstream_body_sent = False
        content_seen = False
        reasoning_seen = False
        tool_action_seen = False
        # The 200 commit has begun (status line written or attempted). Once
        # set, a dead downstream must never receive a second status line, so
        # every downstream write failure ends as client_disconnected.
        commit_started = preflight_headers_sent
        # SSE wire received before the first substantive data event. Held so
        # the 200 can stay pending; replayed once the stream proves alive.
        # Single-line events preserve the historical wire shape exactly; true
        # multi-line events are joined with single newlines so their SSE event
        # boundary is no longer corrupted by per-line ``\n\n`` insertion.
        prelude = []
        prelude_bytes = 0
        got_first_data = preflight_saw_substantive
        status = "ok"
        exc_name = None
        # Sanitized reason of an explicit upstream error event, when one was
        # recognized (see the error branch in process_event below); travels
        # in the failure detail, never the raw upstream object.
        explicit_error_reason = None
        event_lines = []

        def data_value(line):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            return value

        def process_event(lines, had_separator=True):
            nonlocal usage, completion_chars, reasoning_chars
            nonlocal guard_buf, guard_streak, guard_signature
            nonlocal saw_data_lines, saw_finish_reason, done_seen
            nonlocal headers_sent, commit_started, prelude_bytes
            nonlocal downstream_body_sent, content_seen, reasoning_seen
            nonlocal tool_action_seen
            nonlocal got_first_data, status, exc_name
            nonlocal explicit_error_reason

            # Preserve the previous downstream bytes for every one-line event:
            # old code wrote ``line + \"\n\n\"`` and then wrote the separator
            # blank line as another ``\n\n`` item. Only genuine multi-line
            # events change: their lines stay in one SSE event.
            if lines:
                wire = "\n".join(lines) + "\n\n"
                if had_separator:
                    wire += "\n\n"
            else:
                wire = "\n\n"

            data_lines = [line for line in lines if line.startswith("data:")]
            is_data = bool(data_lines)
            payload = "\n".join(data_value(line) for line in data_lines)

            # Parse the payload once, BEFORE the commit gate: an explicit
            # upstream error event must be recognized before the first
            # successful response is committed.
            chunk = None
            if payload and payload != "[DONE]":
                try:
                    chunk = json.loads(payload)
                except Exception:
                    chunk = None
            error_reason = None
            if isinstance(chunk, dict):
                error_reason = _explicit_upstream_error_reason(chunk)
            if error_reason is not None:
                # Open WebUI 0.11.x reports a failed stream as a NORMAL SSE
                # event ({"error": {...}}) followed by [DONE]. This is a
                # definitive terminal failure, not content: with the 200 not
                # yet committed the zero-content contract below answers one
                # HTTP 502; once headers/body went out, the same finally
                # emits exactly one in-band error of the existing shape and
                # stops without forwarding [DONE] or waiting for it. The
                # raw upstream object is never relayed or logged - it may
                # carry credential material; only the sanitized, bounded
                # reason travels.
                saw_data_lines += len(data_lines)
                status = "upstream_stream_error"
                explicit_error_reason = error_reason
                _logger.warning(
                    "upstream explicit stream error request=%s model=%s "
                    "message=%r", request_id, req_model, error_reason)
                return False

            substantive = is_data and bool(payload) and payload != "[DONE]"

            if not commit_started and substantive:
                # First substantive data event: the stream is alive. Commit the
                # 200 and replay everything held back. One try covers headers
                # and replay: a downstream that dies here ends as
                # client_disconnected and never gets a second status line.
                commit_started = True
                try:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    origin = self._cors_allow_origin()
                    if origin:
                        self.send_header("Access-Control-Allow-Origin", origin)
                    self.end_headers()
                    headers_sent = True
                    for held in prelude:
                        held_bytes = held.encode("utf-8")
                        written = self.wfile.write(held_bytes)
                        if written:
                            downstream_body_sent = True
                    prelude.clear()
                except (ConnectionError, OSError):
                    status = "client_disconnected"
                    return False
                got_first_data = True
            elif not commit_started:
                # Headers still pending: comments, blank events and empty data
                # events neither reach the client nor stop the first-data
                # budget. Bound their held wire bytes.
                held_size = len(wire.encode("utf-8"))
                if prelude_bytes + held_size > _PRELUDE_MAX_BYTES:
                    status = "upstream_prelude_overflow"
                    return False
                prelude.append(wire)
                prelude_bytes += held_size

            cut_after_forward = False
            if is_data:
                saw_data_lines += len(data_lines)
                if payload == "[DONE]":
                    if not saw_finish_reason:
                        status = "upstream_missing_finish_reason"
                        return False  # do not forward [DONE]; report as failure
                    done_seen = True
                    # Historical behavior stops on the [DONE] data line before
                    # consuming/writing its separator blank item.
                    wire = "\n".join(lines) + "\n\n"
                elif payload:
                    # ``chunk`` was parsed above, before the commit gate;
                    # error payloads were already handled by the branch there.
                    event_guard_fed = False
                    event_guard_chars = 0
                    if isinstance(chunk, dict):
                        if chunk.get("usage"):
                            # Cumulative usage snapshots across events: merge
                            # per field (latest legal value wins) instead of
                            # replacing wholesale - the final usage chunk of
                            # some upstreams omits the input side, and a
                            # wholesale replacement would drop what an
                            # earlier event already reported.
                            usage = _merge_usage_snapshots(usage,
                                                           chunk["usage"])
                        for ch in chunk.get("choices") or []:
                            if not isinstance(ch, dict):
                                continue
                            if ch.get("finish_reason"):
                                saw_finish_reason = True
                            delta = ch.get("delta") or {}
                            if not isinstance(delta, dict):
                                continue
                            piece = delta.get("content")
                            rpiece = delta.get("reasoning_content")
                            if not isinstance(rpiece, str):
                                rpiece = delta.get("reasoning")
                            if isinstance(piece, str):
                                completion_chars += len(piece)
                                if piece:
                                    content_seen = True
                            if isinstance(rpiece, str):
                                reasoning_chars += len(rpiece)
                                if rpiece:
                                    reasoning_seen = True
                            if delta.get("tool_calls") or delta.get("function_call"):
                                tool_action_seen = True
                            if REPETITION_GUARD_ENABLED:
                                # Only reasoning/content feed the guard; tool
                                # call arguments remain exempt.
                                for text_piece in (piece, rpiece):
                                    if isinstance(text_piece, str) and text_piece:
                                        guard_buf = (guard_buf + text_piece)[
                                            -REPETITION_GUARD_BUFFER_CHARS:]
                                        event_guard_fed = True
                                        event_guard_chars += len(text_piece)

                    if REPETITION_GUARD_ENABLED and event_guard_fed:
                        signature = _repetition_guard_signature(guard_buf)
                        if signature is None:
                            guard_streak = 0
                            guard_signature = None
                            line_loop_observer.observe(
                                guard_buf, event_guard_chars)
                        elif signature == guard_signature:
                            guard_streak += 1
                        else:
                            guard_signature = signature
                            guard_streak = 1
                        if guard_streak >= 2:
                            # Forward the triggering event in full first. This
                            # preserves the 200 KiB single-event contract while
                            # still cutting a loop that repeats across two or
                            # more independent chunks/events, including when
                            # each chunk itself is larger than 1200 chars.
                            cut_after_forward = True

            if commit_started:
                try:
                    wire_bytes = wire.encode("utf-8")
                    written = self.wfile.write(wire_bytes)
                    if written:
                        downstream_body_sent = True
                    self.wfile.flush()
                except (ConnectionError, OSError):
                    status = "client_disconnected"
                    return False

            if cut_after_forward:
                raise _DegenerateRepetitionError(
                    "repetition loop detected in streamed reasoning/content")
            return not done_seen

        try:
            while True:
                if not got_first_data and time.monotonic() >= first_data_deadline:
                    status = "upstream_first_byte_timeout"
                    break
                if _client_disconnected(self.connection):
                    status = "client_disconnected"
                    break
                try:
                    item = upstream.next_item(STREAM_WATCHDOG_POLL_S)
                except queue.Empty:
                    continue
                if item is _UPSTREAM_END:
                    if event_lines:
                        if not process_event(event_lines, had_separator=False):
                            break
                        event_lines = []
                    if not done_seen and status == "ok":
                        status = "upstream_eof_without_done"
                    break

                if item:
                    event_lines.append(item)
                    continue

                # Blank line terminates one SSE event. An empty event is still
                # forwarded/held as the historical blank-line item.
                lines = event_lines
                event_lines = []
                if not process_event(lines, had_separator=True):
                    break
        except _DegenerateRepetitionError:
            exc_name = "DegenerateRepetition"
            status = "degenerate_repetition"
        except Exception as exc:
            exc_name = type(exc).__name__
            status = "upstream_read_error"
        finally:
            upstream.cancel()
            self.close_connection = True
            if status == "degenerate_repetition":
                repetition_phase = (repetition_final_origin or
                                    ("postcommit" if downstream_body_sent
                                     else "precommit"))
                _logger.warning(
                    "chat failure request=%s stage=degenerate_repetition "
                    "phase=%s downstream_body_sent=%s content_seen=%s "
                    "reasoning_seen=%s tool_action_seen=%s "
                    "repetition_retries=%d physical_attempts=%d/%d "
                    "duration_ms=%d",
                    request_id, repetition_phase, downstream_body_sent,
                    content_seen, reasoning_seen, tool_action_seen,
                    repetition_retries_done, physical_budget.used,
                    MAX_TOTAL_ATTEMPTS,
                    int((time.time() - started_at) * 1000))
            # Trace fields are appended to the historical line so a production
            # stall can be claimed on the upstream side by request id. A header
            # the upstream omitted is logged as None; one it sent but that is
            # not safely claimable is logged as unavailable:<reason> (see
            # _sanitize_upstream_trace_value), never as a cleaned-up near-id.
            _logger.info(
                "stream end model=%s status=%s data_lines=%d duration_ms=%d "
                "upstream_request_id=%s upstream_server=%s "
                "upstream_connection=%s%s",
                req_model, status, saw_data_lines,
                int((time.time() - started_at) * 1000),
                upstream_trace["x-request-id"],
                upstream_trace["server"],
                upstream_trace["connection"],
                f" exception={exc_name}" if exc_name else "",
            )
            # One final per-request line so the retry budgets can be tuned
            # from evidence (are 5 retries too few/too many?) after a week.
            try:
                _logger.info(
                    "request summary request=%s final=%s physical_attempts=%d/%d "
                    "upstream_retries=%d slow_retries=%d "
                    "repetition_retries=%d line_loop_hits=%d "
                    "images_before=%d images_after=%d "
                    "images_trimmed=%d duration_ms=%d",
                    request_id, status, physical_budget.used, MAX_TOTAL_ATTEMPTS,
                    transient_retries_done, slow_retries_done,
                    repetition_retries_done, line_loop_observer.hits,
                    image_stats["images_before"],
                    image_stats["images_after"], image_stats["images_trimmed"],
                    int((time.time() - started_at) * 1000))
            except NameError:
                pass  # request summary is best-effort; never mask the outcome
            if status not in ("ok", "client_disconnected"):
                extra_detail = None
                if status == "degenerate_repetition":
                    extra_detail = {
                        "phase": (repetition_final_origin or
                                  ("postcommit" if downstream_body_sent
                                   else "precommit")),
                        "downstream_body_sent": downstream_body_sent,
                        "content_seen": content_seen,
                        "reasoning_seen": reasoning_seen,
                        "tool_action_seen": tool_action_seen,
                        "repetition_retries": repetition_retries_done,
                        "physical_attempts": physical_budget.used,
                        "transparent_retry_safe": False,
                    }
                elif explicit_error_reason:
                    # The sanitized upstream reason rides with the one error
                    # event / 502 detail; the raw upstream object never does.
                    extra_detail = {"upstream_message": explicit_error_reason}
                detail = self._stream_error_detail(
                    status, started_at, saw_data_lines, exc_name,
                    extra=extra_detail)
                if headers_sent:
                    try:
                        self._stream_error_event(status, detail)
                    except (ConnectionError, OSError, ValueError):
                        pass  # client left before the error event landed
                else:
                    # Nothing was streamed: the 200 never went out, so a
                    # non-2xx is still honest and lets retry-capable clients
                    # (ZCode classifies HTTP 502 as retryable) recover a
                    # transient upstream failure themselves.
                    try:
                        self._error(502, STREAM_ERROR_MESSAGES.get(
                            status, "Upstream stream failed."), detail,
                            "upstream_stream_error")
                    except (ConnectionError, OSError, ValueError):
                        pass  # client left before the failure was written
            output_chars = completion_chars + reasoning_chars
            out_est = (max(1, output_chars // 4) if status == "ok"
                       else output_chars // 4)
            record_usage(req_model, True, started_at, usage,
                         _message_tokens(body.get("messages")), out_est,
                         status=status)

    @staticmethod
    def _stream_error_detail(status, started_at, saw_data_lines, exc_name,
                             extra=None):
        detail = {"reason": status, "events": saw_data_lines,
                  "duration_s": round(time.time() - started_at, 1)}
        if exc_name:
            detail["exception"] = exc_name
        if isinstance(extra, dict):
            detail.update(extra)
        return detail

    def _stream_error_event(self, status, detail):
        """Send one in-band SSE error event; never a second HTTP status."""
        payload = {"error": {"message": STREAM_ERROR_MESSAGES.get(
                                 status, "Upstream stream failed."),
                             "type": "upstream_stream_error",
                             "code": status,
                             "detail": detail}}
        self.wfile.write(
            b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8")
            + b"\n\n")
        self.wfile.flush()

    def _passthrough(self, r: requests.Response):
        try:
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() in ("content-type", "content-length", "cache-control"):
                    self.send_header(k, v)
            origin = self._cors_allow_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(r.content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            r.close()

    def _passthrough_stream(self, r: requests.Response, request_deadline=None,
                            request_id="-", usage=None):
        """Forward an Anthropic-compatible SSE stream with pre-commit recovery.

        The upstream socket is read by the same cancellable producer used by
        chat streaming. That makes the first-data watchdog and downstream
        disconnect checks effective even when UM is completely silent inside a
        blocking socket read. Metadata/thinking events may be held briefly
        before body commit so an early repetition loop or zero-data disconnect
        can still be retried without concatenating two completions.

        ``usage`` (optional) observes the parsed events' usage facts WITHOUT
        touching the relayed bytes: the wire form below is still the upstream's
        exact bytes, so the Anthropic byte-fidelity contract is unchanged.
        """
        slot = _reader_slots
        if not slot.acquire(blocking=False):
            # The compat response has already been opened before we can decide
            # whether a local reader slot exists. Release it immediately when
            # the cap is full; otherwise a rejected request would still leave
            # an idle upstream connection behind.
            try:
                r.close()
            except Exception:
                pass
            self._error(503, "Too many concurrent streaming requests",
                        error_type="too_many_concurrent_streams")
            return "concurrency_limit"

        upstream = None
        slot_owned = True
        deadline = (request_deadline if request_deadline is not None
                    else time.monotonic() + STREAM_FIRST_BYTE_TIMEOUT_S)
        first_data_deadline = min(
            deadline, time.monotonic() + STREAM_FIRST_BYTE_TIMEOUT_S)

        try:
            upstream = _UpstreamStream(r, reader_slot=slot)
            try:
                upstream.start()
            except Exception as exc:
                raise _CompatStreamRetryable(
                    f"producer_start_failed:{type(exc).__name__}") from exc
            slot_owned = False  # producer now owns response.close() + slot.release()

            event_lines = []
            prelude = []
            prelude_bytes = 0
            guard_buf = ""
            guard_signature = None
            guard_streak = 0
            line_loop_observer = _LineLoopObserver(
                request_id, "anthropic_passthrough")
            committed = False
            saw_terminal = False
            status = "ok"

            def commit_headers():
                nonlocal committed
                if committed:
                    return
                self.send_response(r.status_code)
                for k, v in r.headers.items():
                    if k.lower() in ("content-type", "cache-control"):
                        self.send_header(k, v)
                origin = self._cors_allow_origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                committed = True

            def emit_in_band_error(message):
                self.wfile.write(
                    b'event: error\ndata: {"type":"error","error":'
                    b'{"type":"api_error","message":'
                    + json.dumps(message).encode("utf-8") + b"}}\n\n")
                self.wfile.flush()

            def feed_guard(piece):
                nonlocal guard_buf, guard_signature, guard_streak
                if not (REPETITION_GUARD_ENABLED and isinstance(piece, str)
                        and piece):
                    return False
                guard_buf = (guard_buf + piece)[-REPETITION_GUARD_BUFFER_CHARS:]
                sig = _repetition_guard_signature(guard_buf)
                if sig is None:
                    guard_streak = 0
                    guard_signature = None
                    line_loop_observer.observe(guard_buf, len(piece))
                    return False
                if sig == guard_signature:
                    guard_streak += 1
                else:
                    guard_signature = sig
                    guard_streak = 1
                return guard_streak >= 2

            def flush_prelude():
                nonlocal prelude_bytes
                commit_headers()
                for held in prelude:
                    self.wfile.write(held)
                if prelude:
                    self.wfile.flush()
                prelude.clear()
                prelude_bytes = 0

            def process_event(lines, had_separator=True):
                """Process one SSE event; return False when streaming should stop."""
                nonlocal prelude_bytes, status, saw_terminal
                merged = "\n".join(lines)
                wire = (merged + "\n\n").encode("utf-8") if lines else b"\n"
                if not had_separator and lines:
                    wire = (merged + "\n").encode("utf-8")

                data_text = "\n".join(
                    line[5:].lstrip() for line in lines
                    if line.startswith("data:"))
                event = None
                if data_text and data_text != "[DONE]":
                    try:
                        event = json.loads(data_text)
                    except ValueError:
                        event = None

                text_piece = None
                thinking_piece = None
                tool_action = False
                terminal = False
                if isinstance(event, dict):
                    if usage is not None:
                        # Usage facts only: the wire bytes below are untouched.
                        usage.observe(event)
                    event_type = event.get("type")
                    delta = event.get("delta") or {}
                    if isinstance(delta, dict):
                        if isinstance(delta.get("text"), str):
                            text_piece = delta.get("text")
                        if isinstance(delta.get("thinking"), str):
                            thinking_piece = delta.get("thinking")
                        if (delta.get("type") == "input_json_delta" and
                                delta.get("partial_json")):
                            tool_action = True
                    block = event.get("content_block") or {}
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_action = True
                    terminal = event_type in ("message_stop", "error")
                    if terminal:
                        # Only message_stop and error terminate the read; a
                        # message_delta carrying stop_reason is NOT a
                        # termination signal (a later usage update and the
                        # message_stop may still arrive). An in-band error
                        # event is the upstream's own failure and is recorded
                        # as such, never as "ok". EOF without a terminal
                        # event is recorded as upstream_eof_without_event.
                        saw_terminal = True
                        if event_type == "error":
                            status = "upstream_stream_error"
                if usage is not None:
                    # Known lower bound of the model output this stream carried:
                    # an interrupted stream is recorded as that bound, never as
                    # a fabricated complete success.
                    usage.observe_text(thinking_piece)
                    usage.observe_text(text_piece)

                # Once downstream bytes exist, repetition can only be cut
                # in-band; a transparent reopen would concatenate completions.
                if committed:
                    if feed_guard(thinking_piece) or feed_guard(text_piece):
                        emit_in_band_error(
                            "[chat2api:degenerate_repetition] Upstream model "
                            "fell into a repetition loop; the proxy cut the stream. "
                            "This completion is incomplete.")
                        status = "degenerate_repetition"
                        return False
                    self.wfile.write(wire)
                    self.wfile.flush()
                    return not terminal

                # No body has reached the client yet. Hold metadata and early
                # thinking so transport/repetition failures remain retryable.
                prelude.append(wire)
                prelude_bytes += len(wire)
                if prelude_bytes > _PRELUDE_MAX_BYTES:
                    flush_prelude()
                    return not terminal

                if thinking_piece:
                    if not REPETITION_GUARD_ENABLED:
                        flush_prelude()
                        return not terminal
                    if feed_guard(thinking_piece):
                        status = "degenerate_repetition"
                        raise _CompatStreamRetryable(
                            "repetition loop detected in anthropic thinking")
                    # Once enough thinking proves non-repetitive, release it so
                    # normal reasoning latency does not remain buffered forever.
                    if (len(guard_buf) >= REPETITION_GUARD_MIN_TAIL_CHARS and
                            _repetition_guard_signature(guard_buf) is None):
                        flush_prelude()
                    return not terminal

                # Normal text, tool actions and terminal events are substantive:
                # commit and replay the held Anthropic message_start/etc. first.
                if text_piece or tool_action or terminal:
                    flush_prelude()
                    return not terminal

                # Other metadata stays buffered until substantive output or the
                # bounded prelude limit above.
                return True

            try:
                while True:
                    if _client_disconnected(self.connection):
                        status = "client_disconnected"
                        return status
                    if not committed and time.monotonic() >= first_data_deadline:
                        status = "upstream_first_byte_timeout"
                        raise _CompatStreamRetryable(status)
                    try:
                        item = upstream.next_item(STREAM_WATCHDOG_POLL_S)
                    except queue.Empty:
                        continue
                    except _UPSTREAM_TRANSIENT_EXCEPTIONS as exc:
                        if not committed:
                            status = "upstream_read_error"
                            raise _CompatStreamRetryable(
                                type(exc).__name__) from exc
                        emit_in_band_error(
                            f"Upstream connection failed mid-stream "
                            f"({type(exc).__name__}); the completion is incomplete.")
                        status = "upstream_read_error"
                        return status
                    except Exception as exc:
                        if not committed:
                            self._error(
                                502, "Upstream compatibility stream was invalid",
                                type(exc).__name__,
                                error_type="upstream_stream_error")
                            status = "upstream_read_error"
                            return status
                        emit_in_band_error(
                            f"Upstream stream failed ({type(exc).__name__}); "
                            "the completion is incomplete.")
                        status = "upstream_read_error"
                        return status

                    if item is _UPSTREAM_END:
                        if event_lines:
                            if not process_event(event_lines, had_separator=False):
                                return status
                            event_lines = []
                        if not committed:
                            status = "upstream_eof_without_event"
                            raise _CompatStreamRetryable(
                                "upstream EOF before any SSE event")
                        if not saw_terminal and status == "ok":
                            # A clean EOF without a terminal event is a
                            # TRUNCATED completion: the client saw the
                            # upstream's bytes, so this is not retryable,
                            # but it must never be recorded as "ok".
                            status = "upstream_eof_without_event"
                        return status

                    if item:
                        event_lines.append(item)
                        continue

                    lines = event_lines
                    event_lines = []
                    if not lines:
                        if committed:
                            self.wfile.write(b"\n")
                            self.wfile.flush()
                        else:
                            prelude.append(b"\n")
                            prelude_bytes += 1
                            if prelude_bytes > _PRELUDE_MAX_BYTES:
                                flush_prelude()
                        continue
                    if not process_event(lines, had_separator=True):
                        return status
            except (BrokenPipeError, ConnectionResetError, OSError):
                status = "client_disconnected"
                return status
        finally:
            if upstream is not None:
                upstream.cancel()
            if slot_owned:
                try:
                    r.close()
                finally:
                    slot.release()



def main():
    global RUNTIME_BUILD_ID
    RUNTIME_BUILD_ID = _detect_runtime_build_id()
    configure_logging()
    ap = argparse.ArgumentParser(description="Open WebUI -> local OpenAI-compatible API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--base-url", default=BASE_URL, help="Open WebUI instance URL")
    ap.add_argument("--token", help="Use this JWT directly instead of browser login")
    ap.add_argument("--no-browser", action="store_true",
                    help="Never open the browser (use with --token; no re-login on 401)")
    ap.add_argument("--profile", default=PROFILE_DIR, help="Chrome user-data directory")
    ap.add_argument("--login-timeout", type=int, default=180,
                    help="Seconds to wait for manual sign-in")
    ap.add_argument("--use-api-key", action="store_true",
                    help="Fetch the long-lived Open WebUI API key after login")
    ap.add_argument("--effort-model", action="append", default=[],
                    help="Register thinking-level variants for this model "
                         "(repeatable; e.g. --effort-model GLM-5.2-NVFP4)")
    ap.add_argument("--login", action="store_true",
                    help="Sign in and save the token, then exit")
    ap.add_argument("--force-login", action="store_true",
                    help="Open Edge even when the saved credential is valid")
    ap.add_argument("--list-models", action="store_true",
                    help="List models and exit")
    args = ap.parse_args()
    if args.no_browser and not args.token:
        ap.error("--no-browser requires --token")

    EFFORT_MODELS.extend(args.effort_model)

    # R45.6c: only the dedicated share-backend instance reshapes streams for
    # the gateway's BYOK consumer; the 8000 main chain keeps byte fidelity.
    global STRIP_REASONING_DELTA
    STRIP_REASONING_DELTA = "share-backend" in (args.profile or "")

    # Create the control token before serving so the console can read it on
    # its very first management call after a restart.
    control_token = ensure_control_token(CONTROL_TOKEN_FILE)
    if control_token:
        logging.getLogger("chat2api").info("local control token ready")

    # Exit record L1: every startup names its instance, so a shared log file
    # never leaves a startup line that cannot be tied to one port and PID.
    _logger.info(
        "startup build=%s version=%s source_rev=%s "
        "role=%s host=%s port=%d pid=%d ppid=%d launched_by=%s "
        "repetition_retries=%d upstream_fast_retries=%d "
        "upstream_slow_retries=%d slow_threshold_s=%.1f "
        "max_physical_attempts=%d max_request_seconds=%d "
        "max_images=%d auto_trim_historical_images=%s "
        "auto_trim_current_turn_images=%s",
        RUNTIME_BUILD_ID, CHAT2API_VERSION, CHAT2API_SOURCE_REV,
        "share-backend" if STRIP_REASONING_DELTA else "private",
        args.host, args.port, os.getpid(), os.getppid(), launch_source(),
        REPETITION_GUARD_AUTO_RETRIES, UPSTREAM_AUTO_RETRIES,
        UPSTREAM_SLOW_AUTO_RETRIES, UPSTREAM_SLOW_RETRY_THRESHOLD_S,
        MAX_TOTAL_ATTEMPTS, MAX_REQUEST_SECONDS, MAX_IMAGES,
        AUTO_TRIM_HISTORICAL_IMAGES, AUTO_TRIM_CURRENT_TURN_IMAGES)

    if args.login:
        if args.force_login:
            token = browser_login(args.base_url, args.profile, args.login_timeout)
            config = fetch_upstream_config(args.base_url) \
                if args.use_api_key else None
            api_key = fetch_api_key(args.base_url, token, config) \
                if args.use_api_key else None
            TokenStore(TOKEN_FILE, args.base_url).save(token, api_key)
        else:
            authenticate(args)
        print(f"Signed in, token saved to {TOKEN_FILE}")
        return

    if args.list_models:
        creds = authenticate(args)
    else:
        creds = load_service_credentials(args)
    proxy = Proxy(creds, args)

    if args.list_models:
        models = proxy.list_models(force=True)
        if models is None:
            print("Failed to fetch models (token may have expired):", file=sys.stderr)
            sys.exit(1)
        print("Available models:")
        for m in models.get("data", []):
            print(f"  - {m['id']}")
        return

    require_local_api_key = bool(LOCAL_API_KEY) or not _is_loopback_bind_host(args.host)
    if require_local_api_key and not LOCAL_API_KEY:
        ap.error(
            "binding chat2api to a non-loopback address requires "
            "CHAT2API_LOCAL_API_KEY")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.proxy = proxy
    server.local_api_key = LOCAL_API_KEY
    server.require_local_api_key = require_local_api_key
    # Exit record L2: "startup" is written before the bind, so only this line
    # proves that this PID owns the port (startup without it = never bound).
    # R32-KEY-AUTH: the same line records whether this instance requires the
    # downstream key, so the console read face can bind the instance's own auth
    # requirement without reading the (unreadable) process-level environment.
    _logger.info("listening host=%s port=%d pid=%d downstream_key=%s",
                 args.host, args.port, os.getpid(),
                 "required" if require_local_api_key else "not_required")
    print("-" * 60)
    print(f"chat2api listening on http://{args.host}:{args.port}")
    print(f"backend: {args.base_url}  |  authentication: "
          f"{proxy.auth_status()['label']}")
    print("  Compatibility endpoints:")
    print("    GET  /v1/models")
    print("    POST /v1/chat/completions   (stream=true supported)")
    print("    POST /v1/embeddings")
    print("    POST /v1/messages           (Anthropic; stream=true supported)")
    print("    GET  /ready")
    print("  downstream API key: " +
          ("required" if require_local_api_key else "not required (loopback only)"))
    print(f"  retry policy: repetition={REPETITION_GUARD_AUTO_RETRIES}, "
          f"upstream-fast={UPSTREAM_AUTO_RETRIES}, "
          f"upstream-slow={UPSTREAM_SLOW_AUTO_RETRIES} "
          f"(>={UPSTREAM_SLOW_RETRY_THRESHOLD_S:g}s), "
          f"physical-attempt-cap={MAX_TOTAL_ATTEMPTS}, "
          f"wall={MAX_REQUEST_SECONDS}s")
    print(f"  image policy: max={MAX_IMAGES}, "
          f"trim-historical={AUTO_TRIM_HISTORICAL_IMAGES}, "
          f"trim-current-turn={AUTO_TRIM_CURRENT_TURN_IMAGES}")
    print(f"  build: {RUNTIME_BUILD_ID}")
    print("  Ctrl+C to exit")
    print("-" * 60)
    proxy.begin_startup_validation()
    # A13-2: the bounded periodic credential check runs for the lifetime of
    # this instance and stops with it (proxy.shutdown() sets the same event).
    proxy.begin_auth_watch()
    # Exit record L4 (L3 folded in). serve_forever() returns normally only
    # after self.server.shutdown(), whose one caller is the control-token
    # /admin/shutdown route - so a normal return IS a graceful local-control
    # stop ("admin_shutdown"). Only exits that unwind through here are
    # recorded: an external TerminateProcess (Stop-Process -Force, taskkill
    # /F, a closed console, a killed process tree) runs no finally, so a
    # missing "exit" line means "unknown", never "still running" or "crashed".
    # (Logged here rather than in the handler so no line above the
    # --login-timeout default moves: tests/test_share_gate_citations.py pins
    # that line number against HEAD.)
    exit_reason = "admin_shutdown"
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        exit_reason = "keyboard_interrupt"
        print("\nBye.")
    except BaseException as exc:
        exit_reason = "exception:" + type(exc).__name__
        raise
    finally:
        _logger.info("exit begin port=%d pid=%d reason=%s",
                     args.port, os.getpid(), exit_reason)
        proxy.shutdown()
        server.server_close()
        _logger.info("exit complete port=%d pid=%d", args.port, os.getpid())


def launch_source():
    """Who started this instance, for the startup record only.

    The launcher sets CHAT2API_LAUNCH_SOURCE (the gateway runtime writes
    ``console``/``cli``/``watch``, the share backend's parent ``console``).
    The value goes into the service log, so anything that is not a short
    lowercase token is reported as ``invalid`` rather than echoed. Defined
    after main() so it moves no line-pinned code above it.
    """
    import re
    raw = os.environ.get("CHAT2API_LAUNCH_SOURCE", "")
    if not raw:
        return "unspecified"
    return raw if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", raw) else "invalid"


if __name__ == "__main__":
    main()
