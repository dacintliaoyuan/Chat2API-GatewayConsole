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
  2. On first run this script opens a headless Edge browser (Playwright, the
     instance's existing profile) where the automatic SSO flow signs in. The token
     is saved to `token.json` and reused afterwards — no browser needed for later
     runs.
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
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests


# ``shared`` is bundled beside this app (``ai-gateway/shared``). The service may
# run this file as a script (``sys.path[0]`` is ``chat2api/``) and the test suites
# may load it by path, so the package root is inserted here when it is missing.
# Other sys.path entries are kept untouched.
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)


def _load_nonstream_aggregate():
    """Import the pure aggregator from the bundled ``shared`` package.

    ``shared`` is a REQUIRED dependency of this app: both the local and the cloud
    packages carry it. A missing package fails here explicitly instead of silently
    falling back to a second copy. No network, credential or ledger side effect
    happens here.
    """
    from shared import nonstream_aggregate
    return nonstream_aggregate


def _load_share_auth():
    """Load the pure share auth helpers beside THIS file, only in share mode.

    The private 8000 chain and the isolated local package never import it at
    module load: the import is reached from the share request path and from
    main() only when ``--share-config`` is present.
    """
    try:
        from . import share_auth
    except ImportError:
        import share_auth as share_auth
    return share_auth


_nonstream_aggregate = _load_nonstream_aggregate()
_NonStreamAggregateError = _nonstream_aggregate.AggregateError
_NonStreamChatAggregator = _nonstream_aggregate.ChatCompletionAggregator
_NonStreamAnthropicAggregator = _nonstream_aggregate.AnthropicMessageAggregator

# The merged 8100 process authenticates the share credential in-process and
# then serves the request through this same handler. These names are imported
# from the share gate module, not from a second listener.
# The share gate is imported lazily: the private 8000/local package must not
# load the cloud share module at import time.
# Default Open WebUI endpoint. Override with --base-url.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000")

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
def _writable_parent(path, fallback):
    """Keep the release-relative default when its parent is writable.

    The cloud release tree is read-only, so the merged 8100 child must not
    create its token/profile/log there.  POSIX tests run from a writable
    temp tree and keep the release-relative defaults byte for byte.
    """
    if os.name != "posix":
        return path
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        if os.access(parent, os.W_OK):
            return path
    except OSError:
        pass
    return fallback


def _writable_dir(path, fallback):
    if os.name != "posix":
        return path
    try:
        os.makedirs(path, exist_ok=True)
        if os.access(path, os.W_OK):
            return path
    except OSError:
        pass
    return fallback


TOKEN_FILE = os.environ.get("CHAT2API_TOKEN_FILE") or _writable_parent(
    os.path.join(HERE, "token.json"),          # NEVER commit this file
    "/var/lib/um-share-console/share-backend/token.json")
PROFILE_DIR = _writable_dir(
    os.path.join(HERE, ".edge-profile"),       # dedicated Edge login state
    "/var/lib/um-share-console/share-backend/.edge-profile")
SERVICE_LOG = os.environ.get("CHAT2API_SERVICE_LOG") or _writable_parent(
    os.path.join(HERE, "chat2api.log"),
    "/var/log/um-share/chat2api.log")

# Per-request usage ledger for the ai-gateway cost aggregator
# (default ../data/usage-chat2api.jsonl, relative to this file).
USAGE_LOG = os.environ.get("CHAT2API_USAGE_LOG") or _writable_parent(
    os.path.join(HERE, os.pardir, "data", "usage-chat2api.jsonl"),
    "/var/lib/um-share/usage-chat2api.jsonl")
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
# Recovery cycle (Plan-Revision 2 A3): the FIRST failure is retried at once,
# every following retry waits 30/60/300/600 seconds measured from the moment
# the previous attempt actually FAILED (never from its start). The first attempt
# plus five retries make at most six login sessions per cycle; the last failure
# pauses the automatic recovery and only an explicit manual request opens a new
# cycle. A refused session (another instance's live lease, or the rolling global
# cap) does NOT consume a retry: the scheduler waits for the earliest allowed
# moment instead.
REAUTH_RECOVERY_BACKOFF_S = (0.0, 30.0, 60.0, 300.0, 600.0)
REAUTH_MAX_SESSIONS_PER_CYCLE = 1 + len(REAUTH_RECOVERY_BACKOFF_S)
# Lease coverage on top of --login-timeout (A5): a login session may spend
# its whole human wait PLUS bounded startup, navigation, credential-probe and
# cleanup budgets, so the shared lease must outlive all of them. These are
# explicit per-phase bounds, not a fixed "safety" number, and a crashed holder
# is reclaimed by the other instance only after the whole bounded session.
REAUTH_SESSION_STARTUP_BUDGET_S = 60.0
REAUTH_SESSION_NAV_BUDGET_S = 30.0
REAUTH_SESSION_PROBE_BUDGET_S = 15.0
REAUTH_SESSION_CLEANUP_BUDGET_S = 30.0
REAUTH_SLOT_MARGIN_S = (REAUTH_SESSION_STARTUP_BUDGET_S
                       + REAUTH_SESSION_NAV_BUDGET_S
                       + REAUTH_SESSION_PROBE_BUDGET_S
                       + REAUTH_SESSION_CLEANUP_BUDGET_S)

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
# automatic flow and reports the manual reason to the operator.
OPENWEBUI_SSO_BUTTON_SELECTORS = (
    'button:has-text("使用 UMPASS 继续")',
    'button:has-text("Continue with UMPASS")',
)
UMPASS_SSO_HOST = "websso.um.edu.mo"
# The UMPASS form moved to login.um.edu.mo (Keycloak websso); keep the legacy
# websso.um.edu.mo form accepted so an older deployment and its tests still work.
UMPASS_SSO_HOSTS = (UMPASS_SSO_HOST, "login.um.edu.mo")
UMPASS_ACCOUNT_SELECTORS = (
    'input[name="UserName"]',
    'input[id="userNameInput"]',
    'input[name="username"]',
    'input[id="username"]',
)
UMPASS_PASSWORD_SELECTORS = (
    'input[name="Password"]',
    'input[id="passwordInput"]',
    'input[name="password"]',
    'input[id="password"]',
    'input[type="password"]',
)
UMPASS_SUBMIT_SELECTORS = (
    'input[type="submit"]',
    'button[type="submit"]',
    'span#submitButton[role="button"]',
    '#kc-login',
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
# Explicit rejection evidence on the UMPASS form (A2). The real site's error
# DOM is NOT verified here, so this is a conservative structural check: the page
# must still be the UMPASS form, a VISIBLE alert/error region must be present
# AND the autofilled password field must still hold its value. Anything else is
# left to a human: an unrecognized page is never guessed to be a wrong password.
UMPASS_ERROR_SELECTORS = (
    '[role="alert"]',
    '#errorText',
    '.error',
    '#usernameError',
    '#passwordError',
    '#input-error',
    '[id$="-error"]',
    '.pf-c-form__helper-text.pf-m-error',
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
# Ticket #41: a failed first navigation is retried once before the unknown
# page is treated as a manual takeover; the retry is bounded.
LOGIN_NAVIGATION_ATTEMPTS = 2
# A session with no observable progress for five minutes is stuck and must
# release its browser instead of holding it for the caller's total budget (30
# minutes on the dedicated instance).
LOGIN_IDLE_TIMEOUT_S = 300.0
# Ticket #13/#42: the terminal manual-takeover code and its reasons live in
# ONE shared module so the service, the console adapter and the display read face
# cannot drift apart. A step only the account holder can complete ends THIS round
# at once; the returned code keeps the reason so the operator can decide between a
# retry (a new round) and an on-demand visible display.
from login_contract import (  # noqa: E402  (root prepared above)
    LOGIN_TERMINAL_MANUAL_REASONS,
    MANUAL_TAKEOVER_REQUIRED_PREFIX,
)

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
# such reads work). Non-browser local programs (curl, SDKs) never send
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
# The merged share entry keeps the dedicated backend's 32 MiB default and lets a
# deployment lower/raise it explicitly (clamped to 1 GiB). The private 8000 chain
# keeps MAX_REQUEST_BYTES unchanged.
def _share_max_request_bytes():
    try:
        value = int(os.environ.get("CHAT2API_SHARE_MAX_REQUEST_BYTES",
                                  str(32 * 1024 * 1024)))
    except (TypeError, ValueError):
        value = 32 * 1024 * 1024
    return max(1, min(value, 1024 * 1024 * 1024))


SHARE_MAX_REQUEST_BYTES = _share_max_request_bytes()
# One request body must arrive within this budget. A client that opens a
# connection, declares a Content-Length and then stalls mid-body would otherwise
# pin a handler thread indefinitely (slowloris). This is the BODY read budget,
# distinct from the upstream/request wall-clock budgets below.
REQUEST_BODY_TIMEOUT_S = 60.0
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

# N1: opt-in upstream transport for the NON-streaming Chat endpoint. When it is
# enabled, a client that sent ``stream=false`` is still answered with ONE complete
# JSON body, but the UM request itself selects SSE (``stream=true`` plus
# ``stream_options``) and the gateway aggregates the deltas locally. The client's
# response mode and the ledger's ``stream`` field stay the client's own
# (``False``). Default False keeps the private 8000 chain and every other caller
# on the native non-stream transport; enabling the share entry needs the N2/N3
# gates first.
NONSTREAM_UPSTREAM_AGGREGATE = False

# Per-HTTP-request cumulative authentication wait budget (A3). It is created
# at the Handler message boundary and passed down to every internal 401 re-send,
# compatibility retry and hidden retry of THAT request. It is independent of the
# stream-reader slots below: a request waiting for authentication must never
# consume a streaming slot before it actually opens a stream. The generic
# in-process default is 30 seconds; main() resolves the 8000/8100 default (180,
# D18-5) and an explicit CLI/env value (clamped to 1..180).
try:
    AUTH_WAIT_BUDGET_S = max(0.01, min(180.0, float(
        os.environ.get("CHAT2API_AUTH_WAIT_BUDGET_S", "30"))))
except ValueError:
    AUTH_WAIT_BUDGET_S = 30.0
try:
    AUTH_WAIT_MAX_CONCURRENT = max(1, min(1024, int(
        os.environ.get("CHAT2API_AUTH_WAIT_MAX_CONCURRENT", "128"))))
except ValueError:
    AUTH_WAIT_MAX_CONCURRENT = 128
try:
    AUTH_WAIT_RECHECK_S = max(0.01, min(1.0, float(
        os.environ.get("CHAT2API_AUTH_WAIT_RECHECK_S", "1"))))
except ValueError:
    AUTH_WAIT_RECHECK_S = 1.0
# A definite next attempt is projected as an integer Retry-After, bounded by
# the rolling global-attempt window (never an unbounded or fabricated value).
AUTH_WAIT_RETRY_AFTER_MAX_S = 900
_auth_wait_slots = threading.BoundedSemaphore(AUTH_WAIT_MAX_CONCURRENT)

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

# Error envelope contract (ADR 0003 / spec #58). ``type`` is the client-facing
# category, ``code`` is the stable machine-readable contract and must keep every
# spelling an earlier release emitted. ``param`` names the offending field (or is
# null), and ``detail`` stays a structured diagnostic extension. The status ->
# category map is only a fallback: a code-specific category wins, because e.g.
# a 503 reauthentication error is an upstream failure, not a generic API error.
_ERROR_CATEGORIES = frozenset({
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "api_error",
    "overloaded_error",
    "upstream_error",
})

_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "permission_error",
    403: "permission_error",
    404: "not_found_error",
    405: "invalid_request_error",
    408: "invalid_request_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    415: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "upstream_error",
    503: "api_error",
    504: "upstream_error",
    529: "overloaded_error",
}

# Stable machine codes and their categories. Codes not listed here use the
# status fallback above; every code emitted by the gateway must stay in this map
# so a client can switch on ``code`` without parsing messages.
_ERROR_TYPE_BY_CODE = {
    # Local request shape.
    "invalid_json": "invalid_request_error",
    "invalid_body": "invalid_request_error",
    "invalid_request": "invalid_request_error",
    "unsupported_chat_request": "invalid_request_error",
    "unsupported_messages_request": "invalid_request_error",
    "unsupported_responses_request": "invalid_request_error",
    "too_many_images_current_turn": "invalid_request_error",
    "too_many_images_history": "invalid_request_error",
    "image_budget_normalization_failed": "invalid_request_error",
    "method_not_allowed": "invalid_request_error",
    "unsupported_content_encoding": "invalid_request_error",
    "request_timeout": "invalid_request_error",
    # Authentication / authorization.
    "invalid_api_key": "authentication_error",
    "local_auth_error": "authentication_error",
    "origin_denied": "permission_error",
    "local_management_denied": "permission_error",
    "invalid_control_token": "authentication_error",
    "upstream_forbidden": "permission_error",
    "reauthentication_failed": "authentication_error",
    # Routing.
    "route_not_shared": "not_found_error",
    "not_found": "not_found_error",
    # Admission / quota.
    "sharing_limit_reached": "rate_limit_error",
    "upstream_quota_exhausted": "rate_limit_error",
    # Upstream / gateway.
    "reauthentication_required": "upstream_error",
    "upstream_unavailable": "upstream_error",
    "upstream_unreachable": "upstream_error",
    "upstream_timeout": "upstream_error",
    "upstream_request_rejected": "invalid_request_error",
    "upstream_request_failed": "upstream_error",
    "upstream_explicit_error": "upstream_error",
    "upstream_stream_error": "upstream_error",
    "unexpected_upstream_response": "upstream_error",
    "invalid_upstream_response": "upstream_error",
    "request_deadline_exceeded": "upstream_error",
    "upstream_attempt_budget_exhausted": "upstream_error",
    "degenerate_repetition": "upstream_error",
    "too_many_concurrent_streams": "overloaded_error",
    "share_revoke_unassembled": "api_error",
    "api_error": "api_error",
}


def _error_category(status: int, error_code: str | None) -> str:
    """Return the client-facing category for one stable machine code."""
    category = _ERROR_TYPE_BY_CODE.get(error_code or "")
    if category is None:
        category = _ERROR_TYPE_BY_STATUS.get(status, "api_error")
    if category not in _ERROR_CATEGORIES:
        # A typo in the tables must never leak an unknown category to clients.
        return "api_error"
    return category


def _is_anthropic_error_path(path: str | None) -> bool:
    """True when the local error must use the Anthropic Messages shape."""
    try:
        route = urllib.parse.urlparse(path or "").path
    except (TypeError, ValueError):
        return False
    return route in ("/v1/messages", "/api/v1/messages")


# Methods implemented by each exact gateway route. Unknown ``/api/`` paths are the
# control-token passthrough and accept both; every other unknown path is a 404,
# exactly like the GET/POST dispatch. The table drives the 405 + ``Allow`` reply
# for every method the gateway does not implement, replacing the historical 501 HTML.
_API_ROUTE_METHODS = {
    "/": ("GET",),
    "/health": ("GET",),
    "/ready": ("GET",),
    "/v1/version": ("GET",),
    "/v1/models": ("GET",),
    "/api/models": ("GET",),
    "/api/v1/models": ("GET",),
    "/admin/auth/status": ("GET",),
    "/admin/auth/verify": ("GET",),
    "/v1/chat/completions": ("POST",),
    "/api/chat/completions": ("POST",),
    "/api/v1/chat/completions": ("POST",),
    "/v1/embeddings": ("POST",),
    "/api/v1/embeddings": ("POST",),
    "/v1/messages": ("POST",),
    "/api/v1/messages": ("POST",),
    "/v1/responses": ("POST",),
    "/api/v1/responses": ("POST",),
    "/admin/share/revoke": ("POST",),
    "/admin/auth/login": ("POST",),
    "/admin/auth/cancel": ("POST",),
    "/admin/shutdown": ("POST",),
}


def _allowed_methods_for_path(path: str | None):
    """Return the implemented methods for one route, or None when unknown."""
    try:
        route = urllib.parse.urlparse(path or "").path
    except (TypeError, ValueError):
        return None
    methods = _API_ROUTE_METHODS.get(route)
    if methods is not None:
        return methods
    if route.startswith("/api/"):
        return ("GET", "POST")
    return None


# Upstream status preservation (ADR 0003 / spec #58 / ticket #60). The status
# code is the primary contract; ``code`` stays a stable machine code and never
# replaces the status. 400/422 keep the existing request-rejected code, 402/429
# are quota/rate-limit failures, 403 is a permission failure, and the remaining
# upstream 4xx/5xx statuses reuse the established generic codes.
_UPSTREAM_ERROR_CODE_BY_STATUS = {
    400: "upstream_request_rejected",
    402: "upstream_quota_exhausted",
    403: "upstream_forbidden",
    404: "not_found",
    405: "upstream_request_rejected",
    408: "upstream_timeout",
    409: "upstream_request_rejected",
    413: "upstream_request_rejected",
    415: "upstream_request_rejected",
    422: "upstream_request_rejected",
    429: "upstream_quota_exhausted",
    500: "upstream_request_failed",
    502: "upstream_unavailable",
    503: "upstream_unavailable",
    504: "upstream_timeout",
    529: "upstream_request_failed",
}
_UPSTREAM_ERROR_MESSAGE_BY_STATUS = {
    402: "Upstream quota is exhausted",
    403: "Upstream denied the request",
    404: "Upstream resource was not found",
    408: "Upstream request timed out",
    429: "Upstream rate limit exceeded",
    500: "Upstream request failed",
    502: "Upstream is unavailable",
    503: "Upstream is unavailable",
    504: "Upstream timed out",
}
# Bounded upstream diagnostic carried in error detail. The raw body is never
# relayed; only the upstream's own message text (or a bounded slice when the
# body is not JSON) travels.
_UPSTREAM_ERROR_MESSAGE_MAX_CHARS = 500
_UPSTREAM_ERROR_BODY_CAPTURE_MAX_CHARS = 2048


def _upstream_error_code(status: int) -> str:
    return _UPSTREAM_ERROR_CODE_BY_STATUS.get(
        status, "upstream_request_failed")


def _upstream_status_message(status: int) -> str:
    return _UPSTREAM_ERROR_MESSAGE_BY_STATUS.get(
        status, "Upstream request failed")


def _hidden_retry_failure(reason: str):
    """Map one failed pre-commit retry reason to (status, error_code).

    A first-byte/header/read timeout is the gateway's own wait expiring and
    becomes a 504; every other transport failure keeps the existing 502.
    """
    text = reason or ""
    if (text == "FirstByteTimeout" or "Timeout" in text
            or "timeout" in text):
        return 504, "upstream_timeout"
    return 502, "upstream_stream_error"


def _is_upstream_timeout(exc):
    """True when a transport failure is the gateway's own wait expiring.

    ``requests`` raises ``Timeout`` for a connect/header wait, but a read timeout
    inside ``iter_content`` is wrapped as ``ConnectionError(ReadTimeoutError)``.
    The wrapper's cause is the only place that distinction survives, so a plain
    ``ConnectionError`` (refused/DNS/TLS/proxy) stays a 502.
    """
    if isinstance(exc, requests.Timeout):
        return True
    if not isinstance(exc, requests.RequestException) or not exc.args:
        return False
    cause = exc.args[0]
    return cause.__class__.__name__ in (
        "ReadTimeoutError", "ConnectTimeoutError", "TimeoutError",
        "ReadTimeout", "ConnectTimeout")

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
        except (OSError, ValueError, TypeError, AttributeError,
                RecursionError):
            return None

    def save(self, token: str, api_key: str = None):
        """Persist the credential through a controlled atomic replace (A6).

        The new document is written COMPLETELY to a sibling temp file first and
        only then replaces the target with ``os.replace``. The pre-fix
        implementation opened the target with "w", so a write interrupted
        mid-document truncated the only valid credential and the next load
        returned None (the file read as "no credential", which would start an
        unnecessary sign-in). A directory/permission/replace failure raises
        ``OSError`` after removing the temp file: the previous valid document
        stays byte-for-byte intact and the caller records the failure reason.
        """
        data = {"url": self.url, "token": token, "api_key": api_key,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        directory = os.path.dirname(os.path.abspath(self.path))
        tmp_path = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                # POSIX modes are best-effort; the atomic replace below is
                # what protects the previous valid document.
                pass
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return data


BROWSER_PATH_ENV = "CHAT2API_BROWSER_PATH"

# The on-demand manual display publishes this runtime state while it is active
# (ticket #14).  Its absence is the default: a headless login.  The file lives
# under the broker's runtime directory, which the display unit shares, and carries
# only the display name and the auth/socket paths needed by a visible browser.
LOGIN_DISPLAY_FILE_ENV = "CHAT2API_LOGIN_DISPLAY_FILE"
LOGIN_DISPLAY_FILE = "/run/um-share/login-display.env"

# Linux has no Edge-first installation convention; any Chromium-family binary
# Playwright can drive is acceptable. The order is the preference order and
# ``shutil.which`` resolves each name through PATH.
LINUX_BROWSER_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "microsoft-edge",
)

# One login browser per profile directory (ticket #11). The lock lives INSIDE the
# profile directory, so the OS releases it when the holding process dies.
PROFILE_LOGIN_LOCK_STEM = "login-browser"
PROFILE_LOGIN_LOCK_NAME = PROFILE_LOGIN_LOCK_STEM + ".lock"

# Cleanup only ever signals these Chromium-family images. Firefox and every other
# browser stay untouched even when they point at the same profile path.
PROFILE_BROWSER_PROCESS_NAMES = frozenset({
    "chrome", "chrome.exe",
    "chromium", "chromium.exe",
    "chromium-browser", "chromium-browser.exe",
    "google-chrome", "google-chrome.exe",
    "google-chrome-stable", "google-chrome-stable.exe",
    "microsoft-edge", "microsoft-edge.exe",
    "microsoft-edge-stable", "microsoft-edge-stable.exe",
    "msedge", "msedge.exe",
    "headless_shell", "headless_shell.exe",
    "chrome-headless-shell", "chrome-headless-shell.exe",
})
PROFILE_BROWSER_TERM_SIGNAL = getattr(signal, "SIGTERM", 15)
PROFILE_BROWSER_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
PROFILE_BROWSER_TERM_WAIT_S = 5.0
PROFILE_BROWSER_KILL_WAIT_S = 2.0
PROFILE_BROWSER_POLL_S = 0.1
_WINDOWS_PROCESS_QUERY_TIMEOUT_S = 10.0


class ProfileBrowserProcess:
    """One OS process that keeps a login profile open.

    ``name`` is the executable/image name and ``argv`` the already-split command
    line. The cleanup decision uses BOTH: a matching profile path alone is never
    enough (a text editor may mention it), and a Chromium-family image alone is
    never enough (it belongs to another profile).
    """

    __slots__ = ("pid", "name", "argv")

    def __init__(self, pid: int, name: str, argv):
        self.pid = int(pid)
        self.name = name or ""
        self.argv = tuple(argv or ())

    def __repr__(self):
        return "ProfileBrowserProcess(pid=%r, name=%r)" % (self.pid, self.name)


class BrowserConfigError(RuntimeError):
    """CHAT2API_BROWSER_PATH is set but unusable (explicit config error).

    Raised instead of silently falling back to another browser: the operator named
    the binary to drive, so signing in through a different one would contradict
    the configuration rather than degrade gracefully.
    """

    def __init__(self, reason: str, detail: str = ""):
        message = "browser configuration error: %s" % reason
        if detail:
            message += " (%s)" % detail
        super().__init__(message)
        self.reason = reason
        self.detail = detail


def linux_browser_candidates(which=None) -> list[str]:
    """Chromium-family executables to try on Linux, in preference order.

    ``shutil.which`` resolves each name through PATH; duplicate paths are
    collapsed because ``chromium`` and ``chromium-browser`` are commonly symlinks
    to the same binary. ``which`` is the deterministic test seam.
    """
    resolver = shutil.which if which is None else which
    found = []
    for name in LINUX_BROWSER_NAMES:
        path = resolver(name)
        if path and path not in found:
            found.append(path)
    return found


def find_browser(candidates=None, platform=None, which=None) -> str | None:
    """Resolve the browser executable to drive on this host.

    Windows keeps the historical Edge-only lookup order (``EDGE_PATHS``); Linux
    resolves ``LINUX_BROWSER_NAMES`` through PATH. ``CHAT2API_BROWSER_PATH`` is
    an explicit operator override on every platform and is authoritative: an
    unusable override raises ``BrowserConfigError`` instead of silently falling
    back to another browser. ``candidates``, ``platform`` and ``which`` are the
    deterministic test seams.
    """
    override = os.environ.get(BROWSER_PATH_ENV, "").strip()
    if override:
        if not os.path.exists(override):
            raise BrowserConfigError("browser_path_missing", override)
        if not os.path.isfile(override) or not os.access(override, os.X_OK):
            raise BrowserConfigError("browser_path_not_executable", override)
        return override
    if candidates:
        pool = candidates
    elif (sys.platform if platform is None else platform).startswith("win"):
        pool = EDGE_PATHS
    else:
        pool = linux_browser_candidates(which)
    for path in pool:
        if os.path.exists(path):
            return path
    return None


def find_edge(candidates=None) -> str | None:
    """Historical entry point kept for existing callers and test seams (A13-1).

    Delegates to ``find_browser``: on Windows the result is exactly the old
    ``EDGE_PATHS`` order, while Linux now resolves a Chromium-family browser.
    """
    return find_browser(candidates)


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
    # ``round`` is optional for compatibility with slots written before R4 and
    # with callers that never bind a recovery round. When present it must be a
    # real integer: a string/bool cannot identify the owning round.
    round_id = state.get("round")
    if round_id is not None and (isinstance(round_id, bool)
                                or not isinstance(round_id, int)):
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


def _owner_pid_alive(pid) -> bool:
    """Whether the lease owner recorded in the slot still runs on this host.

    The slot is shared by the instances of ONE host, so the recorded PID is a valid
    owner reference. A restart gives the new instance a different PID while the old
    lease keeps its full TTL, so without this probe a dead holder refuses every
    login until the TTL runs out (observed live: an ECS restart left the slot
    pointing at the previous 8100 process for 24 hours and every login answered
    ``reauth_refused:global_session_active``).

    FAIL-CLOSED: only a DEFINITE "no such process" counts as dead. A probe that
    cannot answer (permissions, unsupported platform, a non-positive PID) reports
    ALIVE so the caller keeps refusing instead of opening a second submission
    window on a lease that may still be held.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            # EPERM means the process EXISTS but is not ours; any other OSError
            # is not evidence of death either.
            return True
        return True
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_INVALID_PARAMETER is what OpenProcess reports for a PID that is
        # not running; anything else is not evidence of death.
        return ctypes.get_last_error() != 87
    except Exception:  # noqa: BLE001 - an unanswerable probe is not evidence
        return True


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
    diagnostics and tests only. ``kind``/``primitive`` record which OS primitive
    took the lock so the release path never has to guess. The lock FILE is never
    deleted, so its existence says nothing about who holds the lock - exactly like
    model_sync.SyncLock and console_operations.ResourceFileLock, the two
    precedents this protocol copies.
    """

    __slots__ = ("fd", "path", "kind", "primitive", "profile_key")

    def __init__(self, fd, path, kind=None, primitive=None,
                 profile_key=None):
        self.fd = fd
        self.path = path
        self.kind = kind
        self.primitive = primitive
        self.profile_key = profile_key


def _reauth_lock_primitive(platform=None, importer=None):
    """Select the kernel mutex primitive for this host (R13-1).

    Windows keeps the historical ``msvcrt`` byte-range lock. POSIX hosts use
    ``fcntl.flock``, which - unlike POSIX record locks - contends between
    separate opens in the SAME process as well, so the in-process concurrency
    tests and the cross-process mutex share one semantic. ``(None, None)`` means
    the host offers no usable primitive; the caller must then refuse the session
    instead of running the slot read-modify-write without a mutex.
    """
    platform = sys.platform if platform is None else platform
    importer = __import__ if importer is None else importer
    try:
        if platform.startswith("win"):
            return "msvcrt", importer("msvcrt")
        return "fcntl", importer("fcntl")
    except ImportError:
        return None, None


def _acquire_reauth_lock(path: str, primitive=None, wait_s=None,
                          error_sink=None):
    """Cross-process mutex for the slot read-modify-write (A13-3, R13-1).

    The lease below is advisory and long-lived, so it cannot make the acquire step
    itself atomic: without this lock two instances read the same free slot and both
    write themselves in, and the second write silently replaced the first holder AND
    its ledger entry (two submissions counted as one). The lock covers only that
    step.

    R13-1: the mutex IS the kernel's lock (``msvcrt.locking`` on Windows,
    ``fcntl.flock`` on POSIX) on ``<slot>.lock``, held through an open handle
    for the whole critical section. The previous implementation created the file
    with O_EXCL, closed the handle at once, and reclaimed a "stale" file by mtime
    + unlink: that unlink was not bound to the file it had observed, so two
    parties could each delete the other's fresh lock and both end up holding one
    (a second submission window, and two attempts counted as one). Nothing is
    reclaimed now - the OS drops the lock when the holding process dies, so a
    crashed holder cannot block the next one and no observer ever has to delete a
    live holder's file. It is therefore also impossible for a second party to
    "take over" a lock: it can only wait, and the caller keeps observing.
    POSIX cannot prevent an external unlink of the path by the same user; the
    0700 directory and 0600 lock file are mitigations, not guarantees. Product
    code itself never unlinks the lock file.

    ``primitive`` is the ``(kind, module)`` test seam from
    ``_reauth_lock_primitive``. ``wait_s`` defaults to
    ``REAUTH_LOCK_WAIT_S``; the login-browser profile lock passes ``0.0`` so a
    second session is refused immediately. Returns the handle, or None when the lock
    could not be taken within the wait (or no OS primitive exists); the caller
    must then refuse the session rather than guess. When ``error_sink`` is given it
    is filled with ``detail`` ONLY for a filesystem error; plain contention
    leaves it empty, because the caller must not report a held lock as a broken
    host. Ticket #42 item 13: the sink carries only the readable detail, the one
    fact its caller reads; a second unused ``reason`` key is gone.
    """
    if primitive is None:
        primitive = _reauth_lock_primitive()
    kind, module = primitive
    if module is None:
        _logger.warning(
            "reauthentication lock primitive unavailable on platform=%s - "
            "refusing sessions rather than skipping the mutex", sys.platform)
        if error_sink is not None:
            error_sink.update({
                "detail": "OS file lock primitive is unavailable on platform=%s"
                          % sys.platform,
            })
        return None
    wait_s = REAUTH_LOCK_WAIT_S if wait_s is None else max(0.0, float(wait_s))
    lock_path = "%s.lock" % path
    try:
        directory = os.path.dirname(os.path.abspath(lock_path))
        if directory:
            # POSIX mitigation for external unlink: only the directory owner (and
            # group) can remove the entry. An existing directory is never
            # re-chmodded; on Windows the mode is ignored.
            os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if kind == "fcntl":
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                _logger.warning(
                    "could not restrict reauthentication lock permissions "
                    "path=%s - refusing sessions", lock_path)
                os.close(fd)
                if error_sink is not None:
                    error_sink.update({
                        "detail": "could not restrict lock permissions on %s: %s"
                                  % (lock_path, exc),
                    })
                return None
    except OSError as exc:
        if error_sink is not None:
            error_sink.update({
                "detail": "could not open lock file %s: %s"
                          % (lock_path, exc),
            })
        return None
    try:
        if kind == "msvcrt":
            # msvcrt.locking() locks a byte RANGE from the current position,
            # so the file must be at least one byte long and the position must
            # be reset before every lock/unlock call.
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
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        if error_sink is not None:
            error_sink.update({
                "detail": "could not prepare lock file %s: %s"
                          % (lock_path, exc),
            })
        return None
    deadline = time.time() + wait_s
    while True:
        try:
            if kind == "msvcrt":
                module.locking(fd, module.LK_NBLCK, 1)
            else:
                # LOCK_NB is the same non-blocking attempt as LK_NBLCK; the
                # poll loop and deadline below stay identical for both hosts.
                module.flock(fd, module.LOCK_EX | module.LOCK_NB)
            return _ReauthLock(fd, lock_path, kind, module)
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
    kind = getattr(handle, "kind", None)
    module = getattr(handle, "primitive", None)
    if module is None:
        kind, module = _reauth_lock_primitive()
    try:
        if module is not None and kind == "msvcrt":
            os.lseek(fd, 0, os.SEEK_SET)
            module.locking(fd, module.LK_UNLCK, 1)
        elif module is not None:
            module.flock(fd, module.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ------------------------- login browser single instance (ticket #11)
#
# One profile directory can only be driven by ONE login browser. The kernel lock
# on ``<profile>/login-browser.lock`` is authoritative across processes and is
# released automatically when its holder dies. The in-process registry closes the
# same-process race as well: msvcrt byte-range locks are not guaranteed to
# contend between two handles of the same Windows process, and two Proxy instances
# in one process must still not open two browsers.
_profile_login_locks = {}
_profile_login_locks_guard = threading.Lock()


def _profile_login_lock_path(profile_dir: str) -> str:
    resolved = os.path.realpath(os.path.abspath(profile_dir))
    return os.path.join(resolved, PROFILE_LOGIN_LOCK_NAME)


def _profile_login_lock_key(profile_dir: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(profile_dir)))


def _process_start_epoch(pid):
    """Process creation epoch, used only in the lock file's diagnostics.

    ``None`` means the fact could not be read on this host (e.g. macOS without
    /proc). Diagnostics must never fail a login: an unreadable start time is
    simply absent, while the pid itself stays recorded.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, int(pid))
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME)]
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return ticks / 10_000_000.0 - 11_644_473_600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - diagnostics only, never a verdict
            return None
    try:
        stat = Path("/proc/%d/stat" % int(pid)).read_text(
            encoding="utf-8", errors="replace")
        fields = stat.rsplit(")", 1)[-1].split()
        start_ticks = int(fields[19])
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    boot = float(line.split()[1])
                    break
            else:
                return None
        hertz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        return boot + start_ticks / float(hertz)
    except (IndexError, OSError, ValueError):
        return None


def _write_profile_lock_owner(handle):
    """Record pid + process start time next to the lock byte.

    The OS lock covers byte 0 only; the diagnostic payload starts at byte 1, so
    another process can read it on Windows while the lock is held (a byte-range
    lock makes the locked byte itself inaccessible, not the rest of the file).
    """
    payload = json.dumps({
        "pid": os.getpid(),
        "process_started_epoch": _process_start_epoch(os.getpid()),
        "profile_dir": os.path.dirname(handle.path),
        "acquired_epoch": time.time(),
    }, sort_keys=True).encode("utf-8")
    try:
        os.lseek(handle.fd, 1, os.SEEK_SET)
        os.write(handle.fd, payload)
        os.ftruncate(handle.fd, 1 + len(payload))
    except OSError:
        _logger.warning(
            "could not record login lock owner path=%s", handle.path)


def _read_profile_lock_owner(profile_dir: str):
    """Best-effort diagnostic owner of the profile login lock; None if unreadable."""
    path = _profile_login_lock_path(profile_dir)
    try:
        with open(path, "rb") as handle:
            handle.seek(1)
            raw = handle.read(4096)
    except OSError:
        return None
    try:
        owner = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return owner if isinstance(owner, dict) else None


def _profile_login_lock_primitive():
    """The lock primitive of the RUNNING host, not of a simulated platform.

    Tests drive the POSIX browser path by patching ``sys.platform`` on a Windows
    host. The profile lock is a kernel object, so it must still use the host's real
    primitive; otherwise a simulated POSIX login would refuse itself with a
    misleading ``login_already_running`` because ``fcntl`` cannot be imported.
    """
    return _reauth_lock_primitive("win32" if os.name == "nt" else "linux")


def _acquire_profile_login_lock(profile_dir: str):
    """Take the ONE login-browser slot for this profile, or return None.

    No waiting: ticket #11 explicitly allows a second request to be REJECTED
    instead of joined. The lock is taken BEFORE any stale-browser cleanup and
    before the browser opens, and is held for the whole session.

    ``None`` means the profile is genuinely held by another session (or by this
    process). A missing OS lock primitive or an unreadable/uncreatable lock file
    is a configuration/filesystem error and raises ``LoginUnavailable`` with a
    readable reason instead of masquerading as ``login_already_running``
    (ticket #41).
    """
    key = _profile_login_lock_key(profile_dir)
    with _profile_login_locks_guard:
        if key in _profile_login_locks:
            return None
        primitive = _profile_login_lock_primitive()
        if primitive[1] is None:
            raise LoginUnavailable(
                "login_lock_unavailable",
                "OS file lock primitive is unavailable on platform=%s"
                % sys.platform)
        failure = {}
        handle = _acquire_reauth_lock(
            os.path.join(os.path.realpath(os.path.abspath(profile_dir)),
                         PROFILE_LOGIN_LOCK_STEM),
            primitive=primitive,
            wait_s=0.0,
            error_sink=failure)
        if handle is None:
            if failure:
                raise LoginUnavailable(
                    "login_lock_file_error", failure.get("detail") or
                    "could not acquire the profile login lock file")
            return None
        handle.profile_key = key
        _profile_login_locks[key] = handle
    _write_profile_lock_owner(handle)
    return handle


def _release_profile_login_lock(handle):
    """Release a handle from ``_acquire_profile_login_lock``; never delete it."""
    if handle is None:
        return
    key = getattr(handle, "profile_key", None)
    try:
        _release_reauth_lock(handle)
    finally:
        if key is not None:
            with _profile_login_locks_guard:
                if _profile_login_locks.get(key) is handle:
                    _profile_login_locks.pop(key, None)


def _profile_login_busy(profile_dir: str):
    owner = _read_profile_lock_owner(profile_dir) or {}
    detail = "profile %s" % os.path.abspath(profile_dir)
    pid = owner.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        detail += "; owner pid=%d" % pid
        started = owner.get("process_started_epoch")
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            detail += " started=%.3f" % float(started)
    return LoginBusy("login_already_running", detail, owner)


def _strip_optional_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _browser_process_name(name: str) -> str:
    return os.path.basename((name or "").strip()).lower()


def _profile_argument_from_argv(argv):
    """The ``--user-data-dir`` value in an argv, or None.

    Both the ``--user-data-dir=<path>`` form Playwright uses and the
    ``--user-data-dir <path>`` form are accepted.
    """
    for index, raw in enumerate(argv or ()):
        argument = _strip_optional_quotes(str(raw))
        if argument == "--user-data-dir" and index + 1 < len(argv):
            return _strip_optional_quotes(str(argv[index + 1]))
        if argument.startswith("--user-data-dir="):
            return _strip_optional_quotes(argument.split("=", 1)[1])
    return None


def _normalized_profile_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _profile_browser_process_matches(profile_dir: str, name: str, argv) -> bool:
    """Whether this process is a Chromium-family browser on THIS exact profile.

    Both halves are mandatory: Firefox or another browser is never touched even
    with the same profile, and a Chromium process on another profile (including
    a prefix or a subdirectory of this profile) is never touched either.
    """
    if _browser_process_name(name) not in PROFILE_BROWSER_PROCESS_NAMES:
        return False
    used_profile = _profile_argument_from_argv(argv)
    if not used_profile:
        return False
    return _normalized_profile_path(used_profile) == \
        _normalized_profile_path(profile_dir)


def _list_posix_profile_browser_processes(profile_dir: str,
                                          proc_root: str = "/proc"):
    """Read /proc command lines; None when the process table is unreadable."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    found = []
    own_pid = os.getpid()
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid:
            continue
        try:
            raw = Path(proc_root, entry, "cmdline").read_bytes()
        except OSError:
            # The process vanished (or cannot be read): it is not evidence of a
            # live same-profile browser, so it is skipped instead of failing the
            # whole cleanup.
            continue
        argv = [part.decode("utf-8", "replace")
                for part in raw.split(b"\0") if part]
        if not argv:
            continue
        name = os.path.basename(argv[0])
        if _profile_browser_process_matches(profile_dir, name, argv):
            found.append(ProfileBrowserProcess(pid, name, argv))
    return found


def _default_powershell_runner(script: str, timeout: float):
    """Run a read-only CIM query through pwsh, then Windows PowerShell.

    ``None`` means neither shell could be executed; the caller treats that as an
    unreadable process table (fail closed), never as "no browser".
    """
    for shell in ("pwsh", "powershell"):
        try:
            return subprocess.run(
                [shell, "-NoProfile", "-Command", script],
                capture_output=True, timeout=timeout)
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _list_windows_profile_browser_processes(profile_dir: str, runner=None):
    """Read-only Win32_Process query; None when the table is unreadable."""
    names = sorted(name for name in PROFILE_BROWSER_PROCESS_NAMES
                   if name.endswith(".exe"))
    flt = " OR ".join("Name='%s'" % name for name in names)
    script = ("Get-CimInstance Win32_Process -Filter \"%s\" | "
              "Select-Object ProcessId,Name,CommandLine | "
              "ConvertTo-Json -Compress" % flt)
    run = runner or _default_powershell_runner
    try:
        proc = run(script, _WINDOWS_PROCESS_QUERY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc is None or proc.returncode != 0:
        return None
    output = getattr(proc, "stdout", b"") or b""
    if isinstance(output, bytes):
        text = output.decode("utf-8", errors="replace")
    else:
        text = str(output)
    if not text.strip():
        return []
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    found = []
    own_pid = os.getpid()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        if pid == own_pid:
            continue
        name = row.get("Name") or ""
        command_line = row.get("CommandLine")
        if not isinstance(command_line, str) or not command_line.strip():
            continue
        try:
            argv = shlex.split(command_line, posix=False)
        except ValueError:
            continue
        argv = [_strip_optional_quotes(part) for part in argv]
        if _profile_browser_process_matches(profile_dir, name, argv):
            found.append(ProfileBrowserProcess(pid, name, argv))
    return found


def _list_profile_browser_processes(profile_dir: str):
    """Enumerate live Chromium-family processes using this exact profile."""
    if os.name == "nt":
        return _list_windows_profile_browser_processes(profile_dir)
    return _list_posix_profile_browser_processes(profile_dir)


def _signal_profile_browser(pid: int, sig: int) -> bool:
    """Send one signal; False when the process is already gone/unreachable."""
    try:
        os.kill(int(pid), sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        _logger.warning(
            "could not signal browser process pid=%s signal=%s: permission "
            "denied", pid, sig)
        return False
    except OSError as exc:
        _logger.warning(
            "could not signal browser process pid=%s signal=%s: %s",
            pid, sig, exc)
        return False


def _unique_process_pids(processes):
    seen = set()
    pids = []
    for process in processes or ():
        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        pids.append(pid)
    return pids


def _cleanup_profile_browsers(profile_dir: str, *, lister=None,
                               signal_process=None, term_wait_s=None,
                               kill_wait_s=None):
    """Terminate stale Chromium-family browsers using THIS exact profile.

    The caller already holds ``_acquire_profile_login_lock``, so no NEW login
    browser can appear while this runs. Only processes matching BOTH the
    Chromium-family image set AND the exact ``--user-data-dir`` are signalled;
    Firefox and other profiles are never touched. SIGTERM comes first, then a
    bounded wait, then SIGKILL for survivors. An unreadable process table
    refuses the session instead of launching on top of an unknown browser.
    """
    list_processes = lister or _list_profile_browser_processes
    send_signal = signal_process or _signal_profile_browser
    term_wait_s = (PROFILE_BROWSER_TERM_WAIT_S if term_wait_s is None
                   else max(0.0, float(term_wait_s)))
    kill_wait_s = (PROFILE_BROWSER_KILL_WAIT_S if kill_wait_s is None
                   else max(0.0, float(kill_wait_s)))

    processes = list_processes(profile_dir)
    if processes is None:
        raise LoginUnavailable(
            "profile_cleanup_unavailable",
            "could not enumerate browser processes for %s" % profile_dir)
    targets = [process for process in processes
               if _profile_browser_process_matches(
                   profile_dir, getattr(process, "name", ""),
                   getattr(process, "argv", ()))]
    pids = _unique_process_pids(targets)
    if not pids:
        return []

    for pid in pids:
        send_signal(pid, PROFILE_BROWSER_TERM_SIGNAL)
    remaining = set(pids)
    deadline = time.time() + term_wait_s
    while remaining:
        processes = list_processes(profile_dir)
        if processes is None:
            _logger.warning(
                "could not re-enumerate browser processes during cleanup "
                "profile=%s - refusing rather than escalating on unverified "
                "pids", profile_dir)
            raise LoginUnavailable(
                "profile_cleanup_unavailable",
                "could not re-enumerate browser processes for %s"
                % profile_dir)
        remaining = {pid for pid in remaining
                     if any(getattr(process, "pid", None) == pid
                            for process in processes)}
        if not remaining:
            return sorted(pids)
        if time.time() >= deadline:
            break
        time.sleep(PROFILE_BROWSER_POLL_S)

    for pid in sorted(remaining):
        send_signal(pid, PROFILE_BROWSER_KILL_SIGNAL)
    deadline = time.time() + kill_wait_s
    while remaining and time.time() < deadline:
        processes = list_processes(profile_dir)
        if processes is None:
            _logger.warning(
                "could not re-enumerate browser processes after SIGKILL "
                "profile=%s - refusing rather than claiming the cleanup "
                "finished", profile_dir)
            raise LoginUnavailable(
                "profile_cleanup_unavailable",
                "could not verify the cleanup of browser processes for %s"
                % profile_dir)
        remaining = {pid for pid in remaining
                     if any(getattr(process, "pid", None) == pid
                            for process in processes)}
        if remaining:
            time.sleep(PROFILE_BROWSER_POLL_S)
    if remaining:
        raise LoginUnavailable(
            "profile_cleanup_failed",
            "browser processes still use %s: %s" % (
                profile_dir, ", ".join(str(pid) for pid in sorted(remaining))))
    return sorted(pids)


def acquire_reauth_slot(path: str, pid: int, ttl_s: float, now=None,
                        round_id=None):
    """Take the cross-instance reauthentication lease (A13-3).

    Returns ``(granted, reason, state)``. Two instances share one UM
    identity, so a second submission window while another session is live is
    refused; so is a session once the rolling attempt window is full. An EXPIRED
    lease is reclaimed, so a crashed holder cannot block the other instance
    forever. A same-pid re-acquire is idempotent and is not counted twice.

    ``round_id`` binds the lease to ONE local recovery round (R4). The caller
    holds its state lock across acquire/release, but a lease may still be
    released by a late finally after the next round already acquired it; the
    round stored in the slot lets that release refuse instead of clearing the
    newer round's lease. A NEWER round takes over a same-pid live lease; an
    OLDER round never overwrites it.

    A slot that cannot be read is NOT an empty slot (R13-2): ``slot_corrupt``
    and ``slot_unreadable`` refuse the session and report the state honestly,
    because granting here would restart the global attempt count from one and open a
    window on top of a lease nobody can read. The damaged file is left untouched
    for the operator to inspect or remove.

    ``next_allowed_epoch`` is the earliest moment a refused session could be
    granted (the live lease's expiry, or the moment the oldest attempt leaves the
    rolling window); it is ``None`` when no retry is meaningful, so the recovery
    scheduler waits for exactly that moment instead of polling.
    """
    now = time.time() if now is None else now
    lock = _acquire_reauth_lock(path)
    if lock is None:
        return False, "coordination_unavailable", {
            "owner_pid": None, "expires_epoch": 0.0,
            "attempts_in_window": 0, "next_allowed_epoch": None}
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
                "attempts_in_window": 0, "next_allowed_epoch": None}
        attempts = _slot_attempts(state, now)
        owner = state.get("owner_pid")
        expires = state.get("expires_epoch")
        # The TTL alone cannot decide liveness: a restarted instance has a new
        # PID while the previous holder's lease keeps its full day. A lease whose
        # owner is definitely gone is reclaimed here, before the TTL runs out.
        live_owner = (isinstance(owner, int) and not isinstance(owner, bool)
                      and isinstance(expires, (int, float))
                      and float(expires) > now
                      and _owner_pid_alive(owner))
        if live_owner and owner != pid:
            _logger.info(
                "reauthentication slot refused reason=global_session_active "
                "owner_pid=%s pid=%s expires=%.3f",
                owner, pid, float(expires))
            return False, "global_session_active", {
                "owner_pid": owner, "expires_epoch": float(expires),
                "attempts_in_window": len(attempts),
                "next_allowed_epoch": float(expires)}
        if not live_owner and len(attempts) >= REAUTH_MAX_ATTEMPTS_PER_WINDOW:
            return False, "global_attempt_cap", {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": len(attempts),
                "next_allowed_epoch": (min(attempts)
                                      + REAUTH_ATTEMPT_WINDOW_S)}
        same_session = live_owner and owner == pid
        stored_round = state.get("round")
        if (same_session and round_id is not None
                and stored_round is not None
                and round_id < stored_round):
            # A late re-acquire from an OLDER round must never overwrite the
            # newer round's ownership or extend its TTL. The newer round keeps
            # the lease; the older one only observes this refusal.
            return False, "round_superseded", {
                "owner_pid": pid, "expires_epoch": float(expires),
                "attempts_in_window": len(attempts),
                "next_allowed_epoch": float(expires),
                "round": stored_round}
        new_state = {
            "owner_pid": pid,
            "started_epoch": state.get("started_epoch") if same_session else now,
            "expires_epoch": now + ttl_s,
            "attempts": attempts if same_session else attempts + [now],
            "round": (round_id if round_id is not None
                      else state.get("round")),
        }
        if not _write_reauth_slot(path, new_state):
            return False, "slot_unavailable", {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": len(attempts),
                "next_allowed_epoch": None}
        return True, None, {
            "owner_pid": pid, "expires_epoch": new_state["expires_epoch"],
            "attempts_in_window": len(new_state["attempts"]),
            "next_allowed_epoch": None,
            "round": new_state["round"]}
    finally:
        _release_reauth_lock(lock)


_release_round_local = threading.local()


def _release_reauth_slot_for_round(path: str, pid: int, round_id: int,
                                    success=False, refund_attempt=False):
    """Release the lease bound to ONE recovery round (R4).

    The public ``release_reauth_slot`` signature stays compatible with callers
    (and test seams) that only know ``path/pid/success``. The round is carried
    to the synchronous release through a thread-local context, so a release paused
    inside the real function still checks the round stored in the lease: a late
    finally of an OLD round refuses to clear the NEXT round's same-pid lease.

    Ticket #42 item 19: a busy session never opened a browser, so its start
    must be refunded from the shared rolling attempt ledger instead of being
    counted against the global cap.
    """
    previous = getattr(_release_round_local, "round_id", None)
    _release_round_local.round_id = round_id
    try:
        if refund_attempt:
            # Ticket #42 item 19: only a busy session refunds its start; every
            # other release keeps the historical call shape so existing test
            # seams (and callers that only know path/pid/success) still work.
            return release_reauth_slot(path, pid, success=success,
                                      refund_attempt=True)
        return release_reauth_slot(path, pid, success=success)
    finally:
        _release_round_local.round_id = previous


def release_reauth_slot(path: str, pid, success=False,
                        refund_attempt=False):
    """Release a lease this pid holds; never release another instance's lease.

    ``_release_reauth_slot_for_round`` additionally binds the release to the
    recovery round that acquired the lease (R4). A late cleanup of an OLD round
    must never clear a lease the NEXT round already holds: without a matching
    round the release is a no-op. Direct callers that pass no round keep the
    historical pid-only contract.

    A successful session resets THIS instance's recovery progress (the caller
    clears its own cycle), but it must NOT delete the global rolling ledger
    (Plan-Revision 2 A4): the hard cap counts session STARTS, so the entries
    still inside the window stay even after a success. The pre-fix code cleared
    them, which made the cap a "failed attempts only" limit instead of the
    confirmed hard cap. A failed session keeps the ledger too, which is what
    bounds the lockout risk.

    R13-2: a slot that cannot be read is left exactly as it is. Writing over it
    would replace a fail-closed state with an empty slot and destroy the only
    evidence an operator has; an absent slot (or one owned by another pid) is not
    written either, exactly as before.
    """
    round_id = getattr(_release_round_local, "round_id", None)
    lock = _acquire_reauth_lock(path)
    if lock is None:
        return
    try:
        state, error = _read_reauth_slot(path)
        if error is not None:
            return
        if state.get("owner_pid") != pid:
            return
        if round_id is not None and state.get("round") != round_id:
            # An older round's finally reached this call after a newer round
            # acquired the same-pid lease: leave the newer ownership intact.
            return
        attempts = _slot_attempts(state, time.time())
        if refund_attempt and attempts:
            # Ticket #42 item 19: the session never opened a browser (another
            # process owns the profile lock), so the start this lease appended
            # is refunded. Only THIS pid's lease reaches here, so the last
            # attempt is exactly the one this release owns.
            attempts = attempts[:-1]
        _write_reauth_slot(path, {
            "owner_pid": None,
            "started_epoch": 0.0,
            "expires_epoch": 0.0,
            "attempts": attempts,
            "round": state.get("round"),
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
    except Exception:  # noqa: BLE001 — Playwright 元素比较：页面可能已脱离，失败即不同一
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
        except Exception:  # noqa: BLE001 — 页面可能已关闭：读不到 URL 按空处理
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

    def is_closed(self) -> bool:
        """Whether the login page is gone (A2 window-reopen signal).

        A stand-in page without the Playwright method reports "not closed", so
        the offline fakes keep their previous behaviour. A closed target can
        make even ``is_closed()`` raise; that error IS the window-closed
        signal, never a credential verdict.
        """
        try:
            return bool(self._page.is_closed())
        except AttributeError:
            # A stand-in without the Playwright method is "not closed".
            return False
        except Exception as exc:  # noqa: BLE001 — Playwright 异常文本是关闭信号，不能按类型窄化
            return "closed" in str(exc).lower()

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
            except Exception:  # noqa: BLE001 — 选择器可能失效：跳过该选择器
                continue
            for index in range(count):
                try:
                    element = locator.nth(index)
                    if not element.is_visible():
                        continue
                except Exception:  # noqa: BLE001 — 元素可能在读取中消失：跳过
                    continue
                handle = None
                try:
                    handle = element.element_handle()
                except Exception:  # noqa: BLE001 — 拿不到元素句柄时按独立控件计数
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
        except Exception:  # noqa: BLE001 — 输入控件可能在读取中消失：按无值处理
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
    """The automatic flow stopped and the login session awaits a human.

    Ticket #13: a step only the account holder can complete carries the
    ``manual_takeover_required:<reason>`` code in ``reason`` and the session
    ends immediately; a Duo push wait that runs out of the total budget keeps
    the bare ``duo_prompt_waiting`` reason because it is not a manual takeover.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__("login requires a human: %s%s"
                         % (reason, (" (%s)" % detail) if detail else ""))
        self.reason = reason
        self.detail = detail


class LoginWindowClosed(RuntimeError):
    """The login page vanished while the automatic flow still needed it.

    Transient (A2): the closed window may be reopened within the same recovery
    cycle, so this is never reported as a password rejection or a pause.
    """

    def __init__(self, reason: str = "window_closed", detail: str = ""):
        super().__init__("login window closed: %s%s"
                         % (reason, (" (%s)" % detail) if detail else ""))
        self.reason = reason
        self.detail = detail


class LoginUnavailable(RuntimeError):
    """The browser cannot be used at all (A2: terminal, not transient).

    A missing Edge is a configuration fact that will not fix itself inside a
    recovery cycle, so it pauses the automatic recovery instead of spending the
    six-session budget on the same deterministic failure.
    """

    def __init__(self, reason: str = "browser_unavailable",
                 detail: str = ""):
        message = "login unavailable: %s" % reason
        if detail:
            message += " (%s)" % detail
        super().__init__(message)
        self.reason = reason
        self.detail = detail


class LoginCredentialRejected(RuntimeError):
    """The upstream explicitly rejected the submitted account/password (A2).

    Produced ONLY from structural evidence (a visible error region on the UMPASS
    form after a submitted form). Terminal for the cycle on purpose: retrying a
    rejected password would risk the documented UMPASS account lockout.
    """

    def __init__(self, reason: str = "upstream_credential_rejected",
                 detail: str = ""):
        message = "login credential rejected: %s" % reason
        if detail:
            message += " (%s)" % detail
        super().__init__(message)
        self.reason = reason
        self.detail = detail


class LoginCancelled(RuntimeError):
    """The operator explicitly cancelled THIS login round (D18-6).

    Terminal for the round but NOT a credential verdict: the service keeps
    running, the browser context is closed, and ordinary requests never reopen
    the window. Only an explicit ``/admin/auth/login`` may start a new round.
    """

    def __init__(self, reason: str = "cancelled"):
        super().__init__("login cancelled: %s" % reason)
        self.reason = reason


class LoginBusy(RuntimeError):
    """Another login session already owns this browser profile (ticket #11).

    Raised BEFORE a browser is launched. The single-instance lock is an OS file
    lock, so a crashed holder releases it automatically; the caller may retry
    after the current session ends. ``owner`` carries the lock file's diagnostic
    pid/start time when it was readable.
    """

    def __init__(self, reason: str = "login_already_running", detail: str = "",
                 owner=None):
        message = "login busy: %s" % reason
        if detail:
            message += " (%s)" % detail
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.owner = owner or {}


class LoginCleanupFailed(RuntimeError):
    """The login session ended but its browser context could not be released.

    A residual context/Chromium keeps occupying memory, so the failure is
    reported explicitly instead of being swallowed. ``primary`` carries the
    session outcome that ended the round (timeout, cancel, probe failure) when
    the close failure masked it, so the operator sees both.
    """

    def __init__(self, reason: str = "browser_cleanup_failed",
                 detail: str = "", primary=None):
        message = "login cleanup failed: %s" % reason
        if detail:
            message += " (%s)" % detail
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.primary = primary


def _login_failure_primary(exc):
    """A readable primary outcome from a session exception (ticket #41).

    ``LoginCleanupFailed.primary`` keeps the exception that actually ended the
    round; this projects its reason (or class name when it has none) so status
    and /health can report both the cleanup failure and the primary outcome.
    """
    if exc is None:
        return None
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return type(exc).__name__


def _health_primary(value):
    """The ONLY primary value /health may carry (ticket #42 item 10).

    /health needs no control token, so it accepts a reason code or an exception
    class name and nothing else: a path, a page URL or free-form text would leak
    internal detail through the unauthenticated face and is dropped.
    """
    if not isinstance(value, str) or not value:
        return None
    if all(part.isidentifier() for part in value.split(":")):
        return value
    return None


class _LoginCancelSignal:
    """OR of the shutdown event and the explicit login-cancel event."""

    def __init__(self, *events):
        self._events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self._events)


def _click_and_observe(page, target, before_url, cancel_event) -> str:
    """One bounded click plus observation. Returns ``advanced`` when the page
    demonstrably moved (URL changed, or the clicked control left the page) and
    ``swallowed`` only when the page is unchanged — the evidence a retry
    requires. A click that raises is not evidence of delivery, so it also
    reports ``swallowed`` after the same observation window."""
    try:
        page.click(target)
    except Exception:  # noqa: BLE001 — 点击失败不构成已送达证据
        pass  # not actionable: no evidence it was delivered
    deadline = time.time() + LOGIN_CLICK_OBSERVE_TIMEOUT_S
    while time.time() < deadline:
        if cancel_event and cancel_event.is_set():
            return "cancelled"
        if page.url() != before_url:
            return "advanced"
        if not page.any_visible((target[0],)):
            return "advanced"  # the control left the DOM: something moved
        try:
            page.wait(LOGIN_CLICK_OBSERVE_POLL_MS)
        except Exception:  # noqa: BLE001 — 窗口消失由调用方按 swallowed 处理
            return "swallowed"  # a vanished window is reported by the caller
    return "swallowed"


def _umpass_credential_rejected(page) -> bool:
    """Structural rejection evidence on the UMPASS form (A2).

    The real site's error DOM is NOT verified here, so the check stays
    conservative: the page must still be the UMPASS form, a VISIBLE alert/error
    region must be present AND the autofilled password field must still hold its
    value. Anything else (a changed site, a missing field, a slow render) is left
    to a human instead of being guessed as a wrong password.
    """
    if not any(_host_matches(_page_host(page.url()), h)
               for h in UMPASS_SSO_HOSTS):
        return False
    if not page.any_visible(UMPASS_ERROR_SELECTORS):
        return False
    return page.field_filled(UMPASS_PASSWORD_SELECTORS)


def _check_login_window(page, closed_check):
    """Raise the transient window-closed signal when the page is gone (A2)."""
    if closed_check():
        raise LoginWindowClosed(
            "window_closed", "last page %s" % (page.url() or "unknown"))


def _display_process_alive(pid):
    """Whether the display supervisor that published the state still runs."""
    if sys.platform.startswith("win"):
        # The cloud display is a POSIX-only path; never signal a Windows PID.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The display runs as the same service user; a permission error still
        # means the PID exists.
        return True
    except OSError:
        return False
    return True


def _login_display(environ=None):
    """Return the active manual display's DISPLAY/XAUTHORITY, or ``None``.

    The default login path is headless (ADR 0002).  Only an explicitly started
    on-demand display publishes ``login-display.env``; this function accepts that
    state only while every part it names still exists, so a stale file after a
    crash cannot turn the default path into a visible window.  The file is written
    by ``um-cloud-deploy/display/start-loopback-display.sh`` and removed when
    the display stops.
    """
    env = os.environ if environ is None else environ
    path = (env.get(LOGIN_DISPLAY_FILE_ENV) or LOGIN_DISPLAY_FILE).strip()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            values = {}
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        # No marker file is the normal headless path, not an error.
        return None
    except OSError as exc:
        _logger.warning(
            "manual display state unreadable (%s); using headless login",
            exc)
        return None
    except UnicodeError as exc:
        _logger.warning(
            "manual display state malformed (%s); using headless login",
            exc)
        return None
    display = values.get("UM_LOGIN_DISPLAY", "")
    auth = values.get("UM_LOGIN_XAUTHORITY", "")
    socket_path = values.get("UM_LOGIN_DISPLAY_SOCKET", "")
    pid = values.get("UM_LOGIN_DISPLAY_PID", "")
    number = display[1:].split(".")[0]
    if not display or not auth or not socket_path:
        _logger.warning(
            "manual display state incomplete (display/auth/socket missing); "
            "using headless login")
        return None
    if not display.startswith(":") or not number.isdigit():
        _logger.warning(
            "manual display state invalid DISPLAY=%r; using headless login",
            display)
        return None
    if not os.path.isfile(auth) or not os.access(auth, os.R_OK):
        _logger.warning(
            "manual display XAUTHORITY is missing or unreadable; "
            "using headless login")
        return None
    if not os.path.exists(socket_path):
        _logger.warning(
            "manual display socket is missing; using headless login")
        return None
    if pid:
        try:
            pid_number = int(pid)
        except ValueError:
            _logger.warning(
                "manual display PID is not an integer; using headless login")
            return None
        if pid_number <= 0 or not _display_process_alive(pid_number):
            _logger.warning(
                "manual display process is gone; using headless login")
            return None
    return {"DISPLAY": display, "XAUTHORITY": auth}


def browser_login(base_url: str, profile_dir: str, timeout_s: int,
                  on_context=None, cancel_event=None,
                  context_factory=None, credential_probe=None,
                  on_progress=None, cancel_login_event=None,
                  process_lister=None, process_signal=None) -> str:
    """Open a Chromium-family browser and drive the two-step UM SSO.

    The SAME on-disk profile passed by the caller is reused (cookie, autofill
    and the existing login state survive). The normal path is headless and creates
    no visible window; it requires neither DISPLAY nor WAYLAND_DISPLAY. When the
    operator has explicitly started the on-demand manual display, this round opens
    a visible window on that display instead, so the human can finish the step
    there. Windows drives Microsoft Edge from its installation paths; Linux drives
    the first Chromium-family binary found in PATH (or the explicit
    ``CHAT2API_BROWSER_PATH`` override). Controlled flow (A13-1/A13-3): the Open
    WebUI login page's "使用 UMPASS 继续" entry, then the UMPASS form whose
    account and password the browser already autofilled from this instance's own
    profile. The automatic part
    only acts on the verified Open WebUI origin (the instance's own --base-url)
    and on websso.um.edu.mo, and only while the expected control is uniquely
    visible. MFA/captcha, a missing prefill, a changed form, an unknown page
    or a swallowed click stop the automatic flow and report the reason to the
    operator (``on_progress``; the token poll keeps running).
    ``cancel_event`` cancels the session when the service stops and
    ``cancel_login_event`` cancels it when the operator explicitly cancels THIS
    round (D18-6): the latter raises ``LoginCancelled`` so the recovery loop
    ends the round instead of treating a closed window as a reopenable failure.

    Ticket #11: the whole session is guarded by the profile's OS single-instance
    lock (released automatically when this process dies) and, while the lock is
    held, stale Chromium-family processes using the SAME profile are terminated
    before the new browser opens. A second call on the same profile raises
    ``LoginBusy`` before any browser is created; it never joins another
    process's browser.

    Ticket #12: the caller's ``timeout_s`` remains the total budget, while a
    session with no observable progress (a page/stage change, a new token, or a
    click that moved the page) for ``LOGIN_IDLE_TIMEOUT_S`` is closed early.
    The browser context is closed on success, timeout, cancel and failure; when
    that close fails, ``LoginCleanupFailed`` reports it explicitly instead of
    leaving a residual browser unreported.

    Ticket #13: MFA, a missing prefill and an unrecognized page end THIS round
    at once with ``manual_takeover_required:<reason>`` (the finally closes the
    browser and releases the profile lock), while a Duo push prompt is still
    waited on for the account holder's approval.

    ``context_factory(edge, profile_dir)``, ``credential_probe(base_url,
    token)``, ``process_lister(profile_dir)`` and ``process_signal(pid,
    signal)`` are the test seams: the real defaults are the Playwright
    persistent context, ``classify_credential``, the OS process table and
    ``os.kill``.
    """
    try:
        edge = find_edge()
    except BrowserConfigError as exc:
        raise LoginUnavailable(
            "browser_config_error",
            "%s: %s" % (exc.reason, exc.detail)) from exc
    if not edge:
        raise LoginUnavailable(*_missing_browser_error(sys.platform))
    display_env = _login_display()
    headful = display_env is not None
    print("=" * 60)
    if headful:
        print(f"Opening visible browser on {display_env['DISPLAY']} "
              f"({edge}): {base_url}")
        print("Finish the manual step in the login window.")
    else:
        print(f"Opening headless browser ({edge}): {base_url}")
        print("Using the existing profile (skipped if already logged in).")
    print(f"Waiting up to {timeout_s}s for the session token...")
    print("=" * 60)

    probe = credential_probe or classify_credential
    expected_origin = _page_origin(base_url)
    cancel_signal = _LoginCancelSignal(cancel_event, cancel_login_event)

    def raise_if_cancelled():
        if cancel_login_event is not None and cancel_login_event.is_set():
            raise LoginCancelled()
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Login cancelled because chat2api is stopping")

    def note(phase, origin=None, manual_reason=None, detail=None):
        if not on_progress:
            return
        try:
            on_progress({"phase": phase, "origin": origin,
                          "manual_reason": manual_reason, "detail": detail})
        except Exception:  # noqa: BLE001 — 进度回调不得中断登录会话
            pass  # progress reporting must never break the session

    def stop_for_manual(reason):
        """End THIS round now: report why, then let the finally close the
        browser and release the profile lock."""
        url = page.url() or "unknown"
        note("manual_takeover", origin=url, manual_reason=reason)
        raise LoginStalled(
            MANUAL_TAKEOVER_REQUIRED_PREFIX + reason,
            "last page %s" % url)

    lock = _acquire_profile_login_lock(profile_dir)
    if lock is None:
        raise _profile_login_busy(profile_dir)
    try:
        _cleanup_profile_browsers(
            profile_dir, lister=process_lister,
            signal_process=process_signal)
        manager = None
        playwright = None
        if context_factory is None:
            from playwright.sync_api import sync_playwright  # lazy import
            manager = sync_playwright()
    except BaseException:
        _release_profile_login_lock(lock)
        raise
    # Ticket review: the outer finally reads this even when context creation
    # itself fails, so it must be defined before the try.
    session_error = None
    try:
        if context_factory is not None:
            ctx = context_factory(edge, profile_dir)
        else:
            playwright = manager.start()
            launch_options = {
                "headless": not headful,
                "executable_path": edge,
            }
            if headful:
                browser_env = {**os.environ, **display_env}
                # The on-demand display is X11; a stale Wayland hint must not
                # make Chromium pick a different backend.
                browser_env.pop("WAYLAND_DISPLAY", None)
                launch_options.update(
                    args=["--start-maximized"],
                    no_viewport=True,
                    env=browser_env,
                )
            ctx = playwright.chromium.launch_persistent_context(
                profile_dir, **launch_options)
        try:
            if on_context:
                on_context(ctx)
            page = _LoginPage(ctx.pages[0] if ctx.pages else ctx.new_page())
            navigation_error = None
            for attempt in range(LOGIN_NAVIGATION_ATTEMPTS):
                try:
                    page.bring_to_front()
                    page.goto(base_url, wait_until="domcontentloaded",
                              timeout=30000)
                    navigation_error = None
                    break
                except Exception as e:  # noqa: BLE001 — 页面加载失败仍可能重定向：继续等待
                    navigation_error = e
                    if attempt + 1 < LOGIN_NAVIGATION_ATTEMPTS:
                        print(f"[warn] Failed to load page, retrying: {e}")
            if navigation_error is not None:
                print("[warn] Failed to load page (may still be "
                      f"redirecting): {navigation_error}")

            deadline = time.time() + timeout_s
            idle_deadline = time.monotonic() + LOGIN_IDLE_TIMEOUT_S
            last_url = page.url()
            last_token = None
            rejected_tokens = set()
            clicks = {"openwebui": 0, "umpass": 0}
            stage_seen = {}
            manual_reason = None

            def mark_progress():
                nonlocal idle_deadline
                idle_deadline = time.monotonic() + LOGIN_IDLE_TIMEOUT_S

            while time.time() < deadline:
                raise_if_cancelled()
                _check_login_window(page, page.is_closed)
                if time.monotonic() >= idle_deadline:
                    raise LoginStalled(
                        "idle_timeout",
                        "no progress for %ss; last page %s"
                        % (LOGIN_IDLE_TIMEOUT_S, page.url() or "unknown"))
                try:
                    token = page.read_token()
                except Exception:  # noqa: BLE001 — 页面读取令牌可能因导航失败
                    token = None
                if token and token != last_token:
                    last_token = token
                    mark_progress()
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
                        except Exception:  # noqa: BLE001 — 页面正在导航：令牌仍保持 rejected
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
                if url != last_url:
                    last_url = url
                    mark_progress()
                host = _page_host(url)
                if url and _page_origin(url) == expected_origin:
                    stage = "openwebui"
                elif any(_host_matches(host, h) for h in UMPASS_SSO_HOSTS):
                    stage = "umpass"
                elif any(_host_matches(host, h) for h in DUO_WAIT_HOSTS):
                    stage = "duo"
                else:
                    stage = "unknown"

                if stage == "duo":
                    # Auto-approved for a trusted device; a prompt that wants
                    # a code is the human's step. Never clicked either way.
                    # Ticket #41: waiting on the Duo push IS progress - the
                    # phone approval may take longer than the idle timeout.
                    stage_seen.setdefault(stage, time.time())
                    mark_progress()
                    note("awaiting_duo", origin=url)
                elif stage == "unknown":
                    stage_seen.setdefault(stage, time.time())
                    stop_for_manual("unrecognized_page")
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
                                page, target, url, cancel_signal)
                            if outcome == "cancelled":
                                raise_if_cancelled()
                            if outcome == "advanced":
                                mark_progress()
                            note("openwebui_clicked", origin=url)
                elif stage == "umpass":
                    if clicks[stage] >= LOGIN_MAX_CLICKS_PER_STAGE:
                        manual_reason = manual_reason or "umpass_click_swallowed"
                        note("manual_takeover", origin=url,
                             manual_reason=manual_reason)
                    elif page.any_visible(UMPASS_MFA_SELECTORS):
                        stop_for_manual("umpass_mfa_required")
                    elif not page.field_filled(UMPASS_ACCOUNT_SELECTORS) or \
                            not page.field_filled(UMPASS_PASSWORD_SELECTORS):
                        stop_for_manual("umpass_prefill_missing")
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
                                page, target, url, cancel_signal)
                            if outcome == "cancelled":
                                raise_if_cancelled()
                            if outcome == "advanced":
                                mark_progress()
                            note("umpass_submitted", origin=url)
                            _check_login_window(page, page.is_closed)
                            if _umpass_credential_rejected(page):
                                # Structural evidence only: the visible form
                                # reported an error and the autofilled
                                # password is still there. A rejected
                                # password must never be retried blindly.
                                raise LoginCredentialRejected()

                try:
                    page.wait(LOGIN_POLL_INTERVAL_MS)
                except Exception:
                    # A vanished window is a transient (reopenable) signal,
                    # never a password rejection or a silent timeout.
                    _check_login_window(page, page.is_closed)
                    raise

            if manual_reason is None and stage == "duo":
                # The session ended while the visible window was still on the
                # Duo prompt: never guess whether Duo wanted a code - report
                # that the automatic flow was still waiting for it.
                manual_reason = "duo_prompt_waiting"
            raise LoginStalled(manual_reason or "timeout",
                               "last page %s" % (page.url() or "unknown"))
        except Exception as exc:
            session_error = exc
            raise
        finally:
            if on_context:
                try:
                    on_context(None)
                except Exception:  # noqa: BLE001 — 回调失败只记录，不覆盖登录结果
                    _logger.warning(
                        "login context callback failed while releasing the "
                        "browser context")
            try:
                ctx.close()
            except Exception as exc:  # noqa: BLE001 — 关闭上下文是清理步骤，不得覆盖登录结果
                detail = "%s: %s" % (type(exc).__name__, exc)
                if session_error is not None:
                    detail += "; primary outcome: %s: %s" % (
                        type(session_error).__name__, session_error)
                cleanup_error = LoginCleanupFailed(
                    "browser_cleanup_failed", detail,
                    primary=session_error)
                if session_error is not None:
                    raise cleanup_error from session_error
                raise cleanup_error
    finally:
        # Capture the outcome the outer block is already propagating BEFORE
        # the nested except replaces sys.exc_info() with the stop failure.
        active = sys.exc_info()[1]
        try:
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception as exc:  # noqa: BLE001 — stop 失败同样按浏览器清理失败上报
                    # Ticket #42 item 7: a Playwright stop failure leaves
                    # the browser process behind, so it is the SAME explicit
                    # cleanup failure as a context close failure instead of an
                    # unnamed exception in status/health.
                    primary = session_error
                    if primary is None:
                        primary = getattr(active, "primary", None)
                    detail = "playwright.stop failed: %s: %s" % (
                        type(exc).__name__, exc)
                    if active is not None and active is not session_error:
                        detail += "; primary outcome: %s: %s" % (
                            type(active).__name__, active)
                    cleanup_error = LoginCleanupFailed(
                        "browser_cleanup_failed", detail, primary=primary)
                    if primary is not None:
                        raise cleanup_error from primary
                    raise cleanup_error
        finally:
            _release_profile_login_lock(lock)


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
        except (requests.RequestException, ValueError):
            # Best-effort API-key probe: no key is the same as an unreadable
            # probe here; the caller falls back to the session token.
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

# Request-scoped attribution facts for record_usage. One request is served by one
# thread and the handler installs the facts BEFORE any ledger row can be written,
# ALWAYS with the full fact set (a request without a verified marker installs
# key_id=None), so a previous request can never leak its attribution into the next.
_request_facts = threading.local()
_attribution_seen = {}
_attribution_seen_lock = threading.Lock()
_attribution_key_cache = {"stat": None, "secret": None}
_stat_cache_lock = threading.Lock()


def set_request_facts(protocol, key_id=None, request_id=None,
                      attribution="absent", attribution_ts=None):
    """Install THIS request's attribution facts for record_usage.

    ``attribution_ts`` is the VERIFIED marker's own timestamp.
    """
    _request_facts.protocol = protocol
    _request_facts.key_id = key_id
    _request_facts.request_id = request_id
    _request_facts.attribution = attribution
    _request_facts.attribution_ts = attribution_ts
    _request_facts.verified_attribution_ts = None


def _request_fact(name):
    return getattr(_request_facts, name, None)











# ------------------------------------ share revoke notice (R-C5, in-process)
# The merged 8100 process owns the projection, so it is the only process that
# can observe "this Key became disabled/deleted at generation N".  A revoke
# closes the Key's in-flight client sockets in-process; no cross-process notice
# or shared secret is involved any more (#34).
# Review #14: applied generations are evicted by TIME, never by insertion
# order.  A notice older than the timestamp window can never pass verification,
# so only entries whose last update is older than the window are dropped; every
# stored generation inside the window stays absolute.
REVOKE_NOTICE_GENERATIONS_TTL_S = 300
_revoke_applied_generations = {}
_revoke_applied_lock = threading.RLock()
_revoke_applied_loaded = False


def _revoke_generation_updated_at(entry):
    """The eviction clock of ONE stored generation entry."""
    if not isinstance(entry, dict):
        return 0
    updated_at = entry.get("updated_at", entry.get("applied_at", 0))
    if isinstance(updated_at, bool) \
            or not isinstance(updated_at, (int, float)):
        return 0
    return updated_at


def _prune_revoke_generations_locked(now=None):
    """Drop applied generations whose notice window has fully expired."""
    now = time.time() if now is None else now
    cutoff = now - REVOKE_NOTICE_GENERATIONS_TTL_S
    for key_id, entry in list(_revoke_applied_generations.items()):
        if _revoke_generation_updated_at(entry) < cutoff:
            _revoke_applied_generations.pop(key_id, None)


def _ensure_revoke_applied_loaded():
    """Load the persisted per-Key generations exactly once (fail closed)."""
    global _revoke_applied_loaded
    with _revoke_applied_lock:
        if _revoke_applied_loaded:
            return
        state = _share_auth_module().load_revoke_state()
        _revoke_applied_generations.clear()
        _revoke_applied_generations.update(
            state.get("generations") or {})
        _prune_revoke_generations_locked()
        _revoke_applied_loaded = True


def _reset_share_revoke_state():
    """Test seam: forget both in-process caches; the state file stays."""
    global _revoke_applied_loaded
    with _revoke_applied_lock:
        _revoke_applied_generations.clear()
        _revoke_applied_loaded = False
    try:
        _share_auth_module().reset_revoke_state_cache()
    except (ImportError, AttributeError):
        pass


_SHARE_AUTH_REQUIRED = (
    "load_revoke_state", "reset_revoke_state_cache",
    "record_revoke_generation", "valid_chat_request",
    "valid_messages_request", "resolve_share_credential", "ShareGate",
    "model_catalog_limit", "messages_query", "load_config",
)


def _share_auth_module():
    """Load share_auth beside THIS file, independent of sys.path.

    The service runs this file as a script (its directory is on sys.path),
    while tests load it by path from another directory.  Reuse the module when
    it is already imported so the gate, request validators and revoke-state
    store have exactly ONE implementation.

    A failed import leaves a partially initialized module in ``sys.modules``.
    That partial module must never be reused: evict it before retrying, and
    evict it again if the retry fails, so one transient failure cannot poison
    every later request in this process.
    """
    module = sys.modules.get("share_auth")
    if module is not None and all(
            hasattr(module, name) for name in _SHARE_AUTH_REQUIRED):
        return module
    if module is not None:
        sys.modules.pop("share_auth", None)
    import importlib.util
    path = os.path.join(HERE, "share_auth.py")
    spec = importlib.util.spec_from_file_location("share_auth", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_auth"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get("share_auth") is module:
            sys.modules.pop("share_auth", None)
        raise
    return module


def _est_tokens(text) -> int:
    """Rough token estimate: CJK chars count ~1 token each, the rest ~4 chars/token."""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
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

    ``cache_report_status`` is additive (Q11): "reported" means the upstream
    supplied a trusted ``prompt_tokens_details.cached_tokens`` (a real zero is
    evidence), "unreported" means the upstream did not report a cache count.
    ``cached_input_tokens`` keeps its previous numeric shape (missing folds to
    0) so old readers are unchanged; new readers must use the status to avoid
    reading that compatibility zero as a measured cache hit.
    """
    global _usage_disabled
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
    cache_details = fields.get("prompt_tokens_details") or {}
    rec = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": "chat2api",
        "model": model,
        "stream": stream,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cache_details.get("cached_tokens", 0),
        "cache_report_status": ("reported" if "cached_tokens" in cache_details
                                else "unreported"),
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
    }
    if _usage_disabled:
        return
    try:
        path = os.path.abspath(USAGE_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _usage_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 记账失败必须降级，绝不能中断请求
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
    "cancelled": "已取消登录",
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

    POSIX (Q11/secret boundary): an existing token is read only after the
    0600/current-uid gate; a new token is created 0600 (an inherited wider
    mode cannot survive). A foreign-owned or wider existing file denies instead
    of being read. Windows keeps the existing read path plus the icacls step.
    """
    target = path
    if os.path.exists(target):
        try:
            secret_module = _load_share_secret_protection()
            value = secret_module.read_private_secret_path(
                path, max_bytes=1024, label="control token").decode(
                    "utf-8").strip()
        except (OSError, ValueError, ImportError, AttributeError):
            return None
        if value:
            return value
    value = secrets.token_urlsafe(32)
    try:
        _load_share_secret_protection().write_private_secret_path(
            path, value.encode("utf-8"))
    except (OSError, ValueError, ImportError, AttributeError):
        return None
    if os.name != "nt":
        return value
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
        # Ticket #41: the primary session outcome (timeout/manual/cancel/...)
        # that a terminal LoginCleanupFailed masked; None when there is none.
        self._auth_failure_primary = None
        self._auth_thread = None
        self._browser_context = None
        self._shutdown_event = threading.Event()
        # D18-6: ONE login window per explicit /admin/auth/login round. The
        # deadline is fixed when that round starts; a status read, a reconnect
        # (ordinary refresh) or a repeated explicit login never renews it.
        # ``_login_cancel_event`` is the per-cycle operator cancel signal; a
        # new explicit round replaces it, so a late cancel of an old round can
        # never cancel the new one.
        self._login_window = None
        self._login_cancel_event = threading.Event()
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
        # Credential generation (A1): a probe result is bound to the
        # credential/session it actually observed, so a stale verdict can never
        # overwrite newer evidence or credentials.
        self._credential_generation = 0
        # Recovery cycle (A3). ONE cancellable scheduler thread per instance
        # owns the whole automatic recovery, so the startup check, a business
        # 401, the watch round and the manual entry all join the same
        # effective cycle instead of opening one window each. ``round`` names the
        # cycle, ``session_starts`` counts the sessions it actually opened,
        # ``next_attempt_epoch`` is when the next retry may start (time.time
        # epoch, None when no retry is scheduled) and ``pause_reason`` says why
        # the automatic recovery stopped (exhausted / manual / credential
        # rejected / browser unavailable).
        self._recovery = {
            "state": "idle",
            "round": 0,
            "session_starts": 0,
            "max_sessions": REAUTH_MAX_SESSIONS_PER_CYCLE,
            "next_attempt_epoch": None,
            "last_failure_epoch": None,
            "last_failure_reason": None,
            "pause_reason": None,
        }
        self._recovery_thread = None
        # Serial boundary between the FINAL credential commit (save +
        # in-memory publish) and shutdown (A6): a commit that lost the race
        # is discarded, never a late save or a late "valid" write-back.
        self._commit_lock = threading.Lock()
        # Per-cycle wake event: a newer manual cycle wakes a WAITING loop so it
        # exits instead of holding the instance for a lease that was released.
        self._recovery_wake = threading.Event()
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
            primary = self._auth_failure_primary
            recovery = dict(self._recovery)
            window = dict(self._login_window) if self._login_window else None
        return {
            "state": state,
            "label": self._auth_label(state, evidence, login),
            "error": error,
            # Ticket #41: the primary outcome a terminal LoginCleanupFailed
            # masked (timeout/manual/cancel/...), None otherwise.
            "primary": primary,
            "evidence": evidence,
            "login": login,
            "gate": gate,
            "watch": self._watch_status(),
            # A7 additive recovery summary: progress, session count, the next
            # allowed attempt and why the automatic recovery paused. Carries
            # no credential and no page query string.
            "recovery": recovery,
            # D18-6 additive login window: the ONE deadline fixed when the
            # explicit login started (null before the first explicit login).
            # It is read-only here and never extended by a status read.
            "login_window_deadline_epoch": (window["deadline_epoch"]
                                            if window else None),
            "login_window_s": (window["window_s"] if window else None),
        }

    def _raise_auth_required(self):
        """Raise the honest exception for the current authentication failure.

        A session refused by the cross-instance lease or the rolling attempt cap
        never judged the credential, so it must not be reported as an
        authentication failure: ReauthenticationThrottled says exactly that and,
        being a ReauthenticationRequired subclass, keeps every existing handler
        working unchanged. A request whose own wait budget ended while the shared
        cycle is still working is the same kind of throttle (A3): the automatic
        recovery did NOT fail, and the console-visible state says it is still in
        progress.
        """
        status = self.auth_status()
        error = status["error"] or ""
        if error.startswith("reauth_refused:"):
            raise ReauthenticationThrottled(error.split(":", 1)[1])
        recovery = status.get("recovery") or {}
        if recovery.get("state") in ("waiting", "running"):
            raise ReauthenticationThrottled("recovery_in_progress")
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
        except Exception as exc:  # noqa: BLE001 — 维护循环不得因一次失败退出
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
        # /health needs no control token. Ticket #15 makes the operator-visible
        # manual reason (``manual_takeover_required:<reason>``) and the ONE login
        # window deadline part of it, because they explain why the automatic
        # path stopped. The login page/evidence/gate detail - including the last
        # page URL and the lease owner - still stays behind
        # /admin/auth/status, where the control token is required. Other auth
        # errors are NOT copied here.
        #
        # Ticket #41 keeps the primary session outcome a cleanup failure masked.
        # Ticket #42 item 10: because this face has no control token, the
        # primary value is restricted to a reason code / exception class name;
        # a path, page URL or free-form text is dropped instead of leaked.
        error = status["error"]
        manual_error = (error if isinstance(error, str)
                        and error.startswith(MANUAL_TAKEOVER_REQUIRED_PREFIX)
                        else None)
        cached = self.models_cache.get("data")
        return {
            "status": "ok",
            "chat2api_version": CHAT2API_VERSION,
            "chat2api_build": RUNTIME_BUILD_ID,
            "backend": self.base_url,
            "models": len(cached.get("data", [])) if cached else 0,
            "authentication": {"state": status["state"],
                               "label": status["label"],
                               "error": manual_error,
                               # Ticket #41: the primary session outcome a
                               # cleanup failure masked, so /health carries
                               # both reasons. Ticket #42 item 10: only a
                               # reason code / class name may pass; paths
                               # and page content are dropped here.
                               "primary": _health_primary(status["primary"])},
            # §8.4 additive: the auth-watch thread's liveness and last
            # round only — no credential, no round content.
            "watch": status["watch"],
            # A7 additive recovery progress: only coarse counters/times.
            # Manual reasons and page details stay behind the control token.
            "recovery": {
                "state": status["recovery"]["state"],
                "session_starts": status["recovery"]["session_starts"],
                "max_sessions": status["recovery"]["max_sessions"],
                "next_attempt_epoch":
                    status["recovery"]["next_attempt_epoch"],
            },
            # Ticket #15 additive login window: the SAME deadline the
            # control-token status reports, so the operator can see how long
            # this round may still take. Null before the first explicit login.
            "login_window_deadline_epoch":
                status["login_window_deadline_epoch"],
            "login_window_s": status["login_window_s"],
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
                # Ticket #15/#41: only a true manual stage publishes the
                # terminal ``manual_takeover_required:<reason>`` code. Ticket
                # #42 item 11: a soft manual signal (a swallowed click, an
                # unrecognized control) only names the human step in
                # ``_auth_login.manual_reason``; it never becomes an auth
                # error, so the console keeps polling the token.
                reason = progress["manual_reason"]
                if reason in LOGIN_TERMINAL_MANUAL_REASONS:
                    self._auth_error = (
                        MANUAL_TAKEOVER_REQUIRED_PREFIX + reason)

    def _session_is_current(self, generation):
        with self._auth_condition:
            return self._auth_generation == generation

    def _recovery_now(self):
        """The scheduler's clock; tests inject a fake to prove the backoff."""
        return time.time()

    def _recovery_sleep(self, seconds, wake=None):
        """Wait ``seconds`` while observing shutdown and a newer cycle (A3).

        The wait is bounded to one second so stopping the service, an explicit
        manual retry, and a live lease released by another instance are
        observed promptly; the loop rechecks the shared slot after every slice.
        """
        event = wake if wake is not None else self._recovery_wake
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._shutdown_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            event.wait(min(remaining, 1.0))
            event.clear()

    def _recovery_stop_if_restored_locked(self, generation,
                                          credential_generation,
                                          evidence_epoch):
        """End a WAITING cycle when newer valid evidence restored it; lock held.

        A business 401, the startup check or the watch round may publish a valid
        credential while this loop waits for another instance's lease. Opening a
        window then would recover a credential that is already valid, so the cycle
        becomes idle and the caller returns without touching the shared lease
        (``release_reauth_slot`` still refuses to release another pid's lease).
        A newer cycle (``_auth_generation``) owns its own summary and is left
        untouched.
        """
        if self._shutdown_event.is_set():
            self._stop_recovery_locked("stopped")
            return True
        if self._auth_generation != generation:
            # A newer cycle replaced this one; its summary is authoritative.
            return True
        evidence = self._auth_evidence
        restored = (
            self._credential_generation != credential_generation
            or (evidence.get("verdict") == "valid"
                and evidence.get("checked_epoch") is not None
                and evidence.get("checked_epoch") != evidence_epoch)
            or (self._auth_state == "valid" and not self._auth_error)
        )
        if not restored:
            return False
        self._auth_state = "valid"
        self._auth_error = None
        self._auth_failure_primary = None
        self._auth_login = None
        self._recovery["state"] = "idle"
        self._recovery["next_attempt_epoch"] = None
        self._recovery["last_failure_epoch"] = None
        self._recovery["last_failure_reason"] = None
        self._recovery["pause_reason"] = None
        self._auth_condition.notify_all()
        return True

    def _reauth_slot_ttl_s(self):
        """Lease TTL covering every bounded phase of one login interaction (visible or headless).

        The human wait plus startup, navigation, credential probe and cleanup
        are always covered. The post-login config and API-key fetches exist
        only on the opt-in ``--use-api-key`` path, so their explicit bounds
        are added only when that path is enabled (the default launchers do not
        enable it).
        """
        margin = REAUTH_SLOT_MARGIN_S
        if self.args.use_api_key:
            margin += (CONFIG_REFRESH_TIMEOUT_S
                       + 2 * REAUTH_SESSION_PROBE_BUDGET_S)
        return self.args.login_timeout + margin

    def _begin_login_window_locked(self):
        """Fix the ONE deadline of the current explicit login round (D18-6).

        Called under ``_auth_condition`` only by ``refresh(force=True)``, which
        is the explicit ``/admin/auth/login`` entry. The deadline is
        ``now + --login-timeout`` (30 minutes on the dedicated instance) and
        is computed ONCE per round: a status read, a display reconnect/takeover
        and a repeated explicit login while the round is still active never
        renew it. Only a round that already ended (valid/failed/cancelled)
        opens a new window.
        """
        if self._login_window is not None:
            active = (self._recovery["state"] in ("running", "waiting")
                      or self._auth_state == "logging_in")
            if active:
                return
        started = self._recovery_now()
        window_s = float(self.args.login_timeout)
        self._login_window = {
            "started_epoch": started,
            "window_s": window_s,
            "deadline_epoch": started + window_s,
        }

    def _stop_recovery_locked(self, pause_reason):
        """End the cycle WITHOUT claiming a credential failure; lock held.

        Used for a refusal that never judged the credential (a corrupt slot, an
        unavailable coordination file) and for shutdown: the state keeps its
        meaning, only the additive recovery summary says why nothing more will
        happen automatically.
        """
        self._recovery["state"] = "exhausted"
        self._recovery["next_attempt_epoch"] = None
        self._recovery["pause_reason"] = pause_reason
        # Ticket #41: a stop/refusal is not a cleanup failure; do not keep
        # a previous round's masked primary outcome.
        self._auth_failure_primary = None
        self._auth_condition.notify_all()

    def _fail_recovery_locked(self, reason, primary=None):
        """End the cycle with an honest failure reason; lock held (A3/A6).

        ``primary`` is the readable session outcome a cleanup failure masked
        (ticket #41); it is None for ordinary failures.
        """
        now = self._recovery_now()
        self._recovery["state"] = "exhausted"
        self._recovery["next_attempt_epoch"] = None
        self._recovery["pause_reason"] = reason
        self._recovery["last_failure_epoch"] = now
        self._recovery["last_failure_reason"] = reason
        self._auth_state = "failed"
        self._auth_error = reason
        self._auth_failure_primary = primary
        self._auth_condition.notify_all()

    def _retry_recovery_locked(self, reason):
        """Schedule the next bounded retry from the ACTUAL failure end (A3).

        Returns False when the session budget is spent: the last failure pauses
        the automatic recovery instead of looping forever.
        """
        now = self._recovery_now()
        self._recovery["last_failure_epoch"] = now
        self._recovery["last_failure_reason"] = reason
        starts = self._recovery["session_starts"]
        if starts >= self._recovery["max_sessions"]:
            self._fail_recovery_locked("attempts_exhausted")
            return False
        self._recovery["state"] = "waiting"
        self._recovery["next_attempt_epoch"] = (
            now + REAUTH_RECOVERY_BACKOFF_S[starts - 1])
        self._auth_state = "needs_login"
        self._auth_error = None
        self._auth_condition.notify_all()
        return True

    def _begin_recovery_cycle_locked(self, force=False):
        """Start the ONE recovery cycle of this instance; lock held (A1/A3).

        A running cycle is joined, never duplicated, so the startup check, a
        business 401, the watch round and the manual entry share one effective
        cycle. A paused (exhausted) cycle only restarts on an explicit manual
        request; ordinary requests and the watch round never reset the budget.
        The FIRST session is gated right here, which keeps the synchronous gate
        evidence the management entry reports; every later retry is owned by the
        single scheduler thread. The rolling global cap is still enforced by the
        shared slot, so a manual retry cannot bypass it.
        """
        thread = self._recovery_thread
        if thread is not None and thread.is_alive():
            state = self._recovery["state"]
            if state in ("running", "waiting"):
                # Ticket #41 (spec #8 story 12): a repeated explicit login
                # joins the CURRENT round, including a round that is only
                # waiting for a refused gate. It must not open a second
                # cycle or reset the 6-session budget.
                return True
            # A thread that already published valid/idle (or a terminal pause)
            # is still finishing its finally block. It is NOT the cycle to
            # join: start a new one so a fresh 401 never reuses the finished
            # cycle's credential result.
        if self._shutdown_event.is_set():
            return False
        if self._auth_state in ("failed", "cancelled") and not force:
            return False
        now = self._recovery_now()
        self._recovery.update({
            "state": "running",
            "round": self._recovery["round"] + 1,
            "session_starts": 0,
            "next_attempt_epoch": None,
            "last_failure_epoch": None,
            "last_failure_reason": None,
            "pause_reason": None,
        })
        self._auth_failure_primary = None
        _logger.info("reauthentication cycle started round=%s force=%s thread=%s",
                     self._recovery["round"], force,
                     threading.current_thread().name)
        self._auth_gate = None
        self._auth_error = None
        self._auth_login = None
        granted, reason, ledger = acquire_reauth_slot(
            REAUTH_SLOT_FILE, os.getpid(), ttl_s=self._reauth_slot_ttl_s(),
            round_id=self._recovery["round"])
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
            # A refused session never judged the credential: keep it a
            # throttle, and let the scheduler wait for the earliest allowed
            # moment instead of opening a second window.
            next_at = ledger.get("next_allowed_epoch")
            self._auth_state = "needs_login"
            self._auth_error = "reauth_refused:%s" % reason
            if reason == "global_session_active":
                # Another instance holds a live login lease. Its expiry is
                # the lease term (gate.expires_epoch), NOT this cycle's
                # retry moment: the single scheduler rechecks the shared
                # slot every second so an early release is followed at once.
                self._recovery["state"] = "waiting"
                self._recovery["next_attempt_epoch"] = None
                self._auth_condition.notify_all()
            elif next_at is None or reason == "round_superseded":
                # A terminal refusal (corrupt/unreadable/unavailable slot, or
                # an older round that must not overwrite a newer one) has no
                # earliest moment this round could succeed: pause instead of
                # polling it.
                self._recovery["state"] = "exhausted"
                self._recovery["next_attempt_epoch"] = None
                self._recovery["pause_reason"] = "reauth_refused:%s" % reason
                self._auth_condition.notify_all()
                return False
            else:
                self._recovery["state"] = "waiting"
                self._recovery["next_attempt_epoch"] = float(next_at)
                self._auth_condition.notify_all()
        else:
            self._recovery["session_starts"] = 1
            self._recovery["state"] = "running"
            self._recovery["next_attempt_epoch"] = None
            self._auth_state = "logging_in"
            self._auth_error = None
            self._auth_condition.notify_all()
        self._auth_generation += 1
        generation = self._auth_generation
        wake = threading.Event()
        self._recovery_wake = wake
        # D18-6: a fresh per-cycle cancel signal. An operator cancel of an
        # older round can never cancel this new round.
        login_cancel = threading.Event()
        self._login_cancel_event = login_cancel
        thread = threading.Thread(
            target=self._recovery_loop,
            args=(generation, granted, self._recovery["round"], wake,
                  login_cancel),
            name="chat2api-reauthentication",
            daemon=True,
        )
        self._recovery_thread = thread
        self._auth_thread = thread  # the existing single-flight view
        thread.start()
        return True

    def _perform_recovery_session(self, generation, login_cancel):
        """Open ONE login interaction (visible or headless) and classify it (A2).

        Returns ``(outcome, reason, primary)`` where outcome is ``valid``
        (credentials committed), ``manual`` (the window needs a human),
        ``rejected`` (the upstream explicitly refused the submitted
        account/password), ``unavailable`` (the browser cannot be used),
        ``busy`` (another process owns this profile's login browser: a throttle,
        not a failure), ``transient`` (a network / browser / closed-window
        failure that may be reopened), ``cancelled`` (the operator explicitly
        cancelled THIS round), ``terminal`` (a failure that cannot produce
        different evidence, including a browser context that could not be released)
        or ``superseded`` (shutdown or a newer cycle won the final commit
        race). ``primary`` is the readable primary outcome only when a
        ``LoginCleanupFailed`` masked it; otherwise it is None.
        """
        try:
            token = browser_login(
                self.base_url,
                self.args.profile,
                self.args.login_timeout,
                on_context=self._set_browser_context,
                cancel_event=self._shutdown_event,
                cancel_login_event=login_cancel,
                on_progress=self._note_login_progress,
            )
        except LoginCancelled as exc:
            return "cancelled", exc.reason, None
        except LoginBusy as exc:
            # Another process already owns this profile's login browser: it is
            # a throttle, never a credential verdict, and this instance must
            # never open a second browser (ticket #11). Ticket #41 keeps it
            # out of the failure budget and preserves the reason.
            return "busy", exc.reason, None
        except LoginStalled as exc:
            return "manual", exc.reason, None
        except LoginCredentialRejected as exc:
            return "rejected", exc.reason, None
        except LoginUnavailable as exc:
            return "unavailable", exc.reason, None
        except LoginCleanupFailed as exc:
            # The browser context could not be released: a residual browser
            # may still occupy memory, so report the cleanup failure as a
            # terminal outcome instead of retrying it. Ticket #41 keeps the
            # primary outcome so status/health can report both.
            return ("terminal", exc.reason,
                    _login_failure_primary(exc.primary))
        except LoginWindowClosed as exc:
            return "transient", exc.reason, None
        except (requests.RequestException, TimeoutError, OSError) as exc:
            return "transient", type(exc).__name__, None
        except Exception as exc:  # noqa: BLE001 — 页面操作都可能抛窗口关闭信号
            # A closed target can surface from any page operation; it is the
            # transient window-closed signal, never a credential verdict.
            if "closed" in str(exc).lower():
                return "transient", "window_closed", None
            # An unclassified failure is terminal: retrying an unknown state
            # could repeat the same wrong action.
            return "terminal", type(exc).__name__, None
        try:
            config = self.probe_config() if self.args.use_api_key else None
            api_key = fetch_api_key(self.base_url, token, config) \
                if self.args.use_api_key else None
        except Exception as exc:  # noqa: BLE001 — 凭据落盘失败按 terminal 分类
            return "terminal", type(exc).__name__, None
        outcome, reason = self._commit_credentials(
            generation, token, api_key, login_cancel)
        return outcome, reason, None

    def _commit_credentials(self, generation, token, api_key, login_cancel):
        """Serial final commit: save + publish, or discard after shutdown (A6).

        The generation/shutdown/cancel check happens INSIDE ``_commit_lock`` and
        is the LAST step before the disk write, so a session that was superseded
        while its post-login probe/fetch was still running cannot save a late
        token or write "valid" back over the stopping instance. An explicit
        operator cancel of THIS round (D18-6) discards the late credential the
        same way. A directory, permission or replace failure keeps the previous
        valid document (atomic replace) and is reported instead of being treated
        as a successful update.
        """
        with self._commit_lock:
            if (self._shutdown_event.is_set()
                    or not self._session_is_current(generation)):
                return "superseded", "superseded"
            if login_cancel.is_set():
                return "cancelled", "cancelled"
            try:
                TokenStore(TOKEN_FILE, self.base_url).save(token, api_key)
            except OSError:
                return "terminal", "token_save_failed"
            with self._auth_condition:
                self.creds = {"token": token, "api_key": api_key}
                self.models_cache = {"at": 0, "data": None}
                self._credential_generation += 1
                self._record_auth_evidence("valid", None, None, "login")
        return "valid", None

    def _recovery_loop(self, generation, granted, round_id, wake,
                       login_cancel):
        """Own ONE recovery cycle: wait, attempt, classify, retry (A2/A3).

        The loop is the single cancellable owner of the automatic recovery. A
        refused session (another instance's live lease, the rolling global cap)
        never consumed a retry: the loop rechecks the shared slot and waits for
        the earliest allowed moment. A live lease has no retry moment of its own
        (``next_attempt_epoch`` is null and the lease term stays in
        ``gate.expires_epoch``), so the single scheduler rechecks the shared
        slot every second and follows an early release at once. A transient
        failure waits the bounded backoff measured from the moment it actually
        failed. A manual, rejected or unavailable session pauses the cycle instead
        of retrying it. An explicit operator cancel of THIS round (D18-6) ends
        the cycle as ``cancelled`` and is never retried; the service keeps
        running. Before opening a window the loop rechecks this instance's
        evidence and generations: a credential already restored by valid new
        evidence ends the wait without opening a second window. Every wait also
        watches the shutdown event, so stopping the service cancels the scheduler
        at once and never treats the stop as a reopenable window. A repeated
        explicit login while this loop is running/waiting JOINS the current round;
        a loop that already owns a login interaction (visible or headless) is never
        replaced.
        """
        succeeded = False
        with self._auth_condition:
            credential_generation = self._credential_generation
            evidence_epoch = self._auth_evidence.get("checked_epoch")
        try:
            while True:
                with self._auth_condition:
                    if self._shutdown_event.is_set():
                        self._stop_recovery_locked("stopped")
                        return
                    if login_cancel.is_set():
                        self._stop_recovery_locked("cancelled")
                        return
                    if self._recovery["round"] != round_id:
                        # Defensive: a round replaced by a newer explicit
                        # login must not publish this loop's outcome.
                        return
                    if self._recovery_stop_if_restored_locked(
                            generation, credential_generation,
                            evidence_epoch):
                        return
                    next_at = self._recovery.get("next_attempt_epoch")
                    # A live lease has no retry moment of its own: recheck
                    # the shared slot once a second. Every other wait (global
                    # cap, failure backoff) has a definite next_at.
                    waiting_for_lease = (
                        not granted
                        and next_at is None
                        and self._recovery["state"] == "waiting")
                    if (next_at is None and not granted
                            and not waiting_for_lease):
                        # A terminal refusal paused the cycle; never poll it.
                        if self._recovery["state"] not in ("exhausted",
                                                           "idle"):
                            self._stop_recovery_locked("stopped")
                        return
                    wait_s = (AUTH_WAIT_RECHECK_S
                              if waiting_for_lease
                              else max(0.0, (next_at or 0.0)
                                       - self._recovery_now()))
                if wait_s > 0:
                    self._recovery_sleep(wait_s, wake)
                    with self._auth_condition:
                        if self._shutdown_event.is_set():
                            self._stop_recovery_locked("stopped")
                            return
                        if self._recovery["round"] != round_id:
                            return
                        if self._recovery_stop_if_restored_locked(
                                generation, credential_generation,
                                evidence_epoch):
                            return
                if not granted:
                    # The round ownership check, the shared-slot acquire and
                    # every state update share ONE critical section with
                    # _begin_recovery_cycle_locked(). A newer manual round
                    # therefore cannot replace this loop between the check and
                    # the acquire, and an older loop can never overwrite the
                    # newer round's lease. The file I/O is bounded by the
                    # coordination lock wait; release never takes this
                    # condition, so there is no lock-order inversion.
                    with self._auth_condition:
                        if self._shutdown_event.is_set():
                            self._stop_recovery_locked("stopped")
                            return
                        if self._recovery["round"] != round_id:
                            return
                        if self._recovery_stop_if_restored_locked(
                                generation, credential_generation,
                                evidence_epoch):
                            return
                        starts = self._recovery["session_starts"]
                        if starts >= self._recovery["max_sessions"]:
                            self._fail_recovery_locked("attempts_exhausted")
                            return
                        ttl_s = self._reauth_slot_ttl_s()
                        granted, reason, ledger = acquire_reauth_slot(
                            REAUTH_SLOT_FILE, os.getpid(), ttl_s=ttl_s,
                            round_id=round_id)
                        self._auth_gate = {
                            "granted": granted,
                            "reason": reason,
                            "owner_pid": ledger.get("owner_pid"),
                            "expires_epoch": ledger.get("expires_epoch"),
                            "attempts_in_window":
                                ledger.get("attempts_in_window"),
                            "max_attempts":
                                REAUTH_MAX_ATTEMPTS_PER_WINDOW,
                            "window_s": REAUTH_ATTEMPT_WINDOW_S,
                            "checked_epoch": time.time(),
                        }
                        if self._shutdown_event.is_set():
                            self._stop_recovery_locked("stopped")
                            if not granted:
                                return
                        elif not granted:
                            if reason == "global_session_active":
                                # Another instance holds a live lease:
                                # its expiry is NOT this cycle's retry
                                # moment. Keep waiting and let the
                                # single scheduler recheck the shared
                                # slot every second; gate.expires_epoch
                                # still carries the lease term.
                                self._recovery["state"] = "waiting"
                                self._recovery["next_attempt_epoch"] = None
                                self._auth_state = "needs_login"
                                self._auth_error = (
                                    "reauth_refused:%s" % reason)
                                self._auth_condition.notify_all()
                                continue
                            next_at = ledger.get("next_allowed_epoch")
                            if next_at is None or reason == "round_superseded":
                                # No earliest moment exists for this
                                # terminal refusal: pause and keep it
                                # visible instead of polling.
                                self._auth_state = "needs_login"
                                self._auth_error = (
                                    "reauth_refused:%s" % reason)
                                self._stop_recovery_locked(
                                    "reauth_refused:%s" % reason)
                                return
                            self._recovery["state"] = "waiting"
                            self._recovery["next_attempt_epoch"] = float(
                                next_at)
                            self._auth_state = "needs_login"
                            # A refused session is a throttle, never a
                            # credential failure: the reason stays
                            # visible to the console while the cycle
                            # keeps waiting.
                            self._auth_error = (
                                "reauth_refused:%s" % reason)
                            self._auth_condition.notify_all()
                            continue
                        else:
                            self._recovery["session_starts"] = starts + 1
                            self._recovery["state"] = "running"
                            self._recovery["next_attempt_epoch"] = None
                            self._auth_state = "logging_in"
                            self._auth_error = None
                            self._auth_condition.notify_all()
                outcome, detail, primary = self._perform_recovery_session(
                    generation, login_cancel)
                # Release the lease as soon as the session ends and BEFORE
                # publishing its outcome: a waiter that observes the terminal
                # state must never see this instance still owning the lease.
                # The release itself is bound to THIS round by the round stored
                # in the lease, so a late finally can never clear the NEXT
                # round's same-pid lease even when the new round acquires
                # while this release is paused.
                with self._auth_condition:
                    current_round = self._recovery["round"] == round_id
                if current_round:
                    _release_reauth_slot_for_round(
                        REAUTH_SLOT_FILE, os.getpid(), round_id,
                        success=(outcome == "valid"),
                        refund_attempt=(outcome == "busy"))
                granted = False
                with self._auth_condition:
                    if outcome == "valid":
                        # The final commit already published the credential
                        # INSIDE _commit_lock; the public "valid" state is
                        # published only AFTER the lease was released above,
                        # so a waiter that observes it never sees this
                        # instance still owning the lease.
                        succeeded = True
                        self._auth_state = "valid"
                        self._auth_error = None
                        self._auth_failure_primary = None
                        self._auth_login = None
                        self._recovery.update({
                            "state": "idle",
                            "next_attempt_epoch": None,
                            "last_failure_epoch": None,
                            "last_failure_reason": None,
                            "pause_reason": None,
                        })
                        self._auth_condition.notify_all()
                        _logger.info("authentication state changed to valid")
                        return
                    if self._shutdown_event.is_set() or outcome == "superseded":
                        if (not self._shutdown_event.is_set()
                                and self._recovery["round"] != round_id):
                            # A newer explicit round already replaced this
                            # one; its state wins over this late result.
                            return
                        self._auth_state = "failed"
                        self._auth_error = "superseded"
                        self._stop_recovery_locked("stopped")
                        return
                    if outcome == "cancelled" or login_cancel.is_set():
                        if self._recovery["round"] != round_id:
                            # A newer explicit round already replaced
                            # this one; its state wins over this cancel.
                            return
                        # D18-6: an explicit operator cancel is terminal
                        # for THIS round and never reopens it; the service
                        # keeps running and only a later explicit login
                        # starts a new round.
                        self._auth_state = "cancelled"
                        self._auth_error = "cancelled"
                        self._auth_login = None
                        self._stop_recovery_locked("cancelled")
                        return
                    if outcome == "busy":
                        # Ticket #41: another process owns the profile lock.
                        # This attempt never opened a session, so it must not
                        # consume the 6-session budget; keep the busy reason
                        # visible and recheck like a shared-slot wait.
                        if self._recovery["round"] != round_id:
                            return
                        if self._recovery["session_starts"] > 0:
                            self._recovery["session_starts"] -= 1
                        self._recovery["state"] = "waiting"
                        self._recovery["next_attempt_epoch"] = None
                        self._recovery["last_failure_epoch"] = (
                            self._recovery_now())
                        self._recovery["last_failure_reason"] = detail
                        self._recovery["pause_reason"] = None
                        self._auth_state = "needs_login"
                        self._auth_error = detail
                        self._auth_condition.notify_all()
                        continue
                    if outcome != "transient":
                        # manual / rejected / unavailable / terminal:
                        # retrying cannot produce different evidence.
                        self._fail_recovery_locked(detail, primary)
                        return
                    if not self._retry_recovery_locked(detail):
                        return
        finally:
            # The release is bound to THIS round by the round stored in the
            # lease. A new round can start while this thread is still alive
            # (state already idle); the late release then refuses because the
            # lease round is the newer one, so it cannot clear the NEW round's
            # lease. The shared references are cleared only for the CURRENT
            # round: a replaced waiting loop must not clear the newer cycle.
            with self._auth_condition:
                current_round = self._recovery["round"] == round_id
            if current_round:
                _release_reauth_slot_for_round(
                    REAUTH_SLOT_FILE, os.getpid(), round_id,
                    success=succeeded)
            with self._auth_condition:
                if self._recovery["round"] == round_id:
                    self._browser_context = None
                    self._auth_thread = None
                    self._recovery_thread = None
                self._auth_condition.notify_all()

    def _wait_recovery(self, deadline, auth_wait_budget=None,
                       cancel_check=None):
        """Wait for the shared cycle's terminal result, bounded by the caller.

        Only the time actually spent inside this method is charged to the
        request-scoped authentication budget; normal upstream inference between
        two internal retries is not. The budget object is shared by every retry
        of the same logical request.
        """
        if auth_wait_budget is None:
            return self._wait_recovery_inner(deadline, None, cancel_check)
        auth_wait_budget.begin_wait()
        try:
            return self._wait_recovery_inner(
                deadline, auth_wait_budget, cancel_check)
        finally:
            auth_wait_budget.end_wait()

    def _wait_recovery_inner(self, deadline, auth_wait_budget=None,
                             cancel_check=None):
        """The bounded wait body; the caller owns the budget segment.

        The caller's own wait budget (``deadline``, monotonic) and the
        request-scoped cumulative auth-wait budget are independent of the
        cycle's lifetime: when the next allowed attempt cannot happen before
        either bound, the request fails now with the throttle instead of
        blocking until then. The cycle itself keeps running. A live lease has
        no definite next attempt, so its old expiry is never an early-failure
        signal. The wait holds one of the per-instance auth-wait slots and
        releases it on success, failure, cancellation and timeout.
        """
        wait_deadline = time.monotonic() + self.args.login_timeout + 10
        if deadline is not None:
            wait_deadline = min(wait_deadline, deadline)
        budget_deadline = None
        if auth_wait_budget is not None:
            # The budget object owns the ONE expiry instant: the projection of
            # the active segment while budget remains, and the FIRST real expiry
            # once it is spent. Recomputing "now + remaining" here would turn a
            # spent budget's expiry into this new segment's start, so a later
            # request deadline would wrongly look like the earlier bound. The
            # caller (``_wait_recovery``) always owns a started segment, so the
            # fallback only keeps a hypothetical direct caller bounded.
            budget_deadline = auth_wait_budget.deadline
            if budget_deadline is None:
                budget_deadline = (time.monotonic()
                                   + auth_wait_budget.remaining)
            wait_deadline = min(wait_deadline, budget_deadline)

        def auth_budget_exhausted():
            """True when this request's own cumulative budget is spent.

            A terminal "valid" result belongs to the SHARED cycle, not to this
            request: when the synchronous auth entry (or an earlier wait)
            already consumed the whole budget, this request must not report
            success and send another upstream call. The overall request
            deadline keeps its own error mapping: when it is the earlier
            bound, the wait returns normally and ``check_before_upstream``
            reports it instead.
            """
            if budget_deadline is None:
                return False
            if time.monotonic() < budget_deadline:
                return False
            if deadline is not None and deadline < budget_deadline:
                return False
            return True

        slot_acquired = False
        with self._auth_condition:
            if self._recovery["state"] not in ("running", "waiting"):
                if self._auth_state != "valid":
                    return False
                if self._shutdown_event.is_set():
                    raise _RequestCancelled("instance stopping")
                if cancel_check is not None and cancel_check():
                    raise _RequestCancelled("client disconnected")
                if auth_budget_exhausted():
                    raise _AuthWaitTimeout("auth wait budget expired")
                return True
            if self._auth_state in ("failed", "cancelled"):
                # A terminal failure/cancel never judged a new credential:
                # answer with its reason instead of waiting.
                return False
            error = self._auth_error or ""
            if error.startswith("reauth_refused:"):
                reason = error.split(":", 1)[1]
                if reason in ("slot_corrupt", "slot_unreadable",
                              "coordination_unavailable",
                              "slot_unavailable", "round_superseded"):
                    return False
            if self._shutdown_event.is_set():
                return False
            if cancel_check is not None and cancel_check():
                raise _RequestCancelled("client disconnected")
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                if (budget_deadline is not None
                        and budget_deadline <= wait_deadline):
                    raise _AuthWaitTimeout("auth wait budget expired")
                return False
            next_at = self._recovery.get("next_attempt_epoch")
            if (next_at is not None
                    and next_at - time.time() > remaining):
                # A definite next attempt is beyond this request's remaining
                # budget: answer immediately (the caller projects Retry-After).
                return False
        if not _auth_wait_slots.acquire(blocking=False):
            raise _AuthWaitCapacityExceeded("auth wait capacity exhausted")
        slot_acquired = True
        try:
            with self._auth_condition:
                while True:
                    if self._shutdown_event.is_set():
                        return False
                    if cancel_check is not None and cancel_check():
                        raise _RequestCancelled("client disconnected")
                    if self._recovery["state"] not in ("running", "waiting"):
                        if (self._auth_state == "valid"
                                and auth_budget_exhausted()):
                            raise _AuthWaitTimeout(
                                "auth wait budget expired")
                        return self._auth_state == "valid"
                    if self._auth_state in ("failed", "cancelled"):
                        return False
                    error = self._auth_error or ""
                    if error.startswith("reauth_refused:"):
                        reason = error.split(":", 1)[1]
                        if reason in ("slot_corrupt", "slot_unreadable",
                                      "coordination_unavailable",
                                      "slot_unavailable", "round_superseded"):
                            return False
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        if (budget_deadline is not None
                                and budget_deadline <= wait_deadline):
                            raise _AuthWaitTimeout(
                                "auth wait budget expired")
                        return False
                    next_at = self._recovery.get("next_attempt_epoch")
                    if (next_at is not None
                            and next_at - time.time() > remaining):
                        return False
                    self._auth_condition.wait(
                        min(remaining, AUTH_WAIT_RECHECK_S))
        finally:
            if slot_acquired:
                _auth_wait_slots.release()

    def auth_retry_after(self):
        """Integer Retry-After for a DEFINITE next attempt, or None.

        Only a scheduled next attempt is projected. A live lease's old expiry
        (``gate.expires_epoch``) is never used because that lease may be
        released early. A terminal pause, an explicit credential rejection, a
        browser failure, a corrupt coordination file and a request-scoped wait
        timeout have no definite retry and therefore no Retry-After.
        """
        with self._auth_condition:
            recovery = dict(self._recovery)
            error = self._auth_error or ""
        if recovery.get("state") != "waiting":
            return None
        next_at = recovery.get("next_attempt_epoch")
        if next_at is None:
            return None
        if not error.startswith("reauth_refused:"):
            if recovery.get("last_failure_epoch") is None:
                return None
        delay = int(math.ceil(float(next_at) - time.time()))
        if delay <= 0:
            return None
        return min(delay, AUTH_WAIT_RETRY_AFTER_MAX_S)

    def refresh(self, force=False, wait=True, deadline=None,
                auth_wait_budget=None, cancel_check=None) -> bool:
        """Join or start the ONE automatic recovery cycle (A1/A3).

        Every trigger - the startup check, a business 401, the watch round and
        the manual entry - joins the same effective cycle. A paused (exhausted)
        cycle is only restarted by an explicit manual request (``force``); the
        rolling global cap still gates every session, so the manual request cannot
        bypass it. ``wait`` joins the cycle's terminal result bounded by the
        caller's own deadline and the request-scoped auth-wait budget;
        ``wait=False`` returns as soon as a cycle exists.
        """
        if self.args.no_browser:
            with self._auth_condition:
                self._auth_state = "failed"
                self._auth_error = "BrowserDisabled"
            return False

        # The synchronous entry includes the state-lock wait and the bounded
        # coordination-file lock wait; both are authentication entry time, so the
        # request-scoped budget charges them. Normal upstream inference and
        # retry delays are never inside this segment.
        if auth_wait_budget is not None:
            auth_wait_budget.begin_wait()
        try:
            with self._auth_condition:
                if self._shutdown_event.is_set():
                    return False
                if force:
                    # D18-6: the explicit login is the ONLY entry that may
                    # start the 30-minute window; an ordinary 401/watch
                    # refresh never does.
                    self._begin_login_window_locked()
                started = self._begin_recovery_cycle_locked(force=force)
                if not started:
                    return False
        finally:
            if auth_wait_budget is not None:
                auth_wait_budget.end_wait()

        if not wait:
            return True
        return self._wait_recovery(deadline,
                                   auth_wait_budget=auth_wait_budget,
                                   cancel_check=cancel_check)

    def cancel_login(self):
        """Cancel THIS login round without stopping the service (D18-6).

        Sets the per-cycle cancel event, closes the visible browser context and
        publishes the terminal ``cancelled`` state. The service keeps running:
        no ``shutdown``, no ``server.shutdown()`` and no restart. Ordinary
        requests never reopen the round; only an explicit
        ``/admin/auth/login`` (``refresh(force=True)``) starts a new one. The
        commit boundary is serialized with ``_commit_lock``, so a credential
        committed BEFORE this cancel keeps ``valid`` and is reported honestly
        instead of being overwritten by ``cancelled``.
        """
        with self._commit_lock:
            with self._auth_condition:
                state = self._auth_state
                active = (self._recovery["state"] in ("running", "waiting")
                          or state == "logging_in")
                if not active:
                    if state == "cancelled":
                        return {"cancelled": False, "state": "cancelled",
                                "detail": "本轮登录已取消；服务继续运行"}
                    return {"cancelled": False,
                            "state": "no_login_in_progress",
                            "detail": "当前没有进行中的登录"}
                self._login_cancel_event.set()
                self._auth_state = "cancelled"
                self._auth_error = "cancelled"
                self._auth_failure_primary = None
                self._auth_login = None
                self._recovery["state"] = "exhausted"
                self._recovery["next_attempt_epoch"] = None
                self._recovery["pause_reason"] = "cancelled"
                context = self._browser_context
                # Wake a scheduler waiting on the shared lease or a backoff.
                self._recovery_wake.set()
                self._auth_condition.notify_all()
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 — 关闭上下文是清理步骤
                    pass
        return {"cancelled": True, "state": "cancelled",
                "detail": "已取消本轮登录；服务继续运行，普通请求不会自动重开"}

    def validate_or_refresh(self, source="startup"):
        """Validate saved credentials without delaying the local HTTP server.

        A13-2: the verdict comes from ``classify_credential``. Only an
        explicit upstream rejection or a locally missing credential starts a
        session; an inconclusive probe keeps the last evidence and never opens
        a login window. A1: the probe is bound to the credential and session
        generation it actually observed, so a result that returns after a
        session started or a credential was published is discarded instead of
        overwriting the newer state (E2).
        """
        with self._auth_condition:
            generation = self._auth_generation
            credential_generation = self._credential_generation
            bearer = self.bearer()
        verdict, reason, upstream_status = classify_credential(
            self.base_url, bearer)
        with self._auth_condition:
            if (self._auth_generation != generation
                    or self._credential_generation != credential_generation
                    or self.bearer() != bearer):
                _logger.info(
                    "discarded a stale credential probe source=%s generation=%s/%s",
                    source, generation, self._auth_generation)
                return "stale"
            self._record_auth_evidence(verdict, reason, upstream_status,
                                       source)
            self._record_upstream_reachability(
                self._reachability_from_credential_probe(
                    verdict, reason, upstream_status),
                reason=reason, upstream_status=upstream_status)
            if verdict == "valid":
                self._auth_state = "valid"
                self._auth_error = None
                self._auth_failure_primary = None
                return "valid"
            if verdict == "unknown":
                # Undecidable: this is NOT evidence of an expired
                # credential, so no browser window may open here.
                return "unknown"
            # Never downgrade a terminal "failed"/"cancelled": that state is
            # what keeps a rejected credential from being retried in a loop
            # (and an operator cancel from being reopened by a probe), and its
            # reason is the honest evidence the console reads (E4).
            if self._auth_state not in ("failed", "cancelled"):
                self._auth_state = "needs_login"
                self._auth_error = None
        _logger.warning(
            "credential check rejected reason=%s source=%s state=%s",
            reason, source, self.auth_status()["state"])
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
        the shared single-flight cycle only on explicit evidence (upstream
        rejection or a locally missing credential). A session already in flight
        is left alone, so a business-triggered 401 and this check never
        reauthenticate twice. A1: the probe is bound to the credential and
        session generation it observed, so a stale result never overwrites a
        newer session's state. A6/E4: a terminal failure keeps its reason - a
        periodic check never clears it. Every return records the round in the
        additive watch view (§8.4).
        """
        if self._shutdown_event.is_set():
            return self._record_watch_round("stopped")
        with self._auth_condition:
            session_in_flight = self._auth_state == "logging_in"
            generation = self._auth_generation
            credential_generation = self._credential_generation
            bearer = self.bearer()
        if session_in_flight:
            return self._record_watch_round("logging_in")
        verdict, reason, upstream_status = classify_credential(
            self.base_url, bearer,
            timeout=AUTH_WATCH_PROBE_TIMEOUT_S)
        with self._auth_condition:
            if (self._auth_generation != generation
                    or self._credential_generation != credential_generation
                    or self.bearer() != bearer):
                _logger.info("discarded a stale auth-watch probe")
                return self._record_watch_round("stale")
            self._record_auth_evidence(verdict, reason, upstream_status,
                                       "auth_watch")
            self._record_upstream_reachability(
                self._reachability_from_credential_probe(
                    verdict, reason, upstream_status),
                reason=reason, upstream_status=upstream_status)
            if verdict == "valid":
                self._auth_state = "valid"
                self._auth_error = None
                self._auth_failure_primary = None
                return self._record_watch_round("valid")
            if verdict == "unknown":
                # Long-idle evidence is not evidence of expiry: keep the last
                # known state and never open a login window.
                return self._record_watch_round("unknown")
            if self._shutdown_event.is_set():
                return self._record_watch_round("stopped")
            if self._auth_state in ("failed", "cancelled"):
                # Never clear a terminal failure/cancel reason, and never
                # retry the rejected credential every round.
                return self._record_watch_round(verdict)
            # Never downgrade a terminal "failed" either: a credential the
            # session already rejected must not be retried every round.
            self._auth_state = "needs_login"
            self._auth_error = None
        if self._shutdown_event.is_set():
            return self._record_watch_round("stopped")
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
        """Stop this instance: supersede the session and cancel the scheduler.

        The commit boundary (A6) is the ``_commit_lock``: a session whose final
        commit already holds it either finishes that commit before this method
        returns, or (when the shutdown event was set first) discards it. No late
        save and no late "valid" write-back can follow a completed shutdown.
        """
        self._shutdown_event.set()
        # D18-6: shutdown also cancels the current login round promptly (the
        # per-cycle event interrupts a blocking browser poll), but the state
        # stays the existing shutdown outcome below, never "cancelled".
        self._login_cancel_event.set()
        with self._commit_lock:
            with self._auth_condition:
                # Supersede any in-flight session: its late result must not
                # write credentials or state after this instance is stopping.
                self._auth_generation += 1
                context = self._browser_context
                thread = self._auth_thread
                recovery_thread = self._recovery_thread
                # Wake request-scoped auth waits: service stop ends the wait
                # and the recovery, never leaving a handler blocked on it.
                self._auth_condition.notify_all()
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 — 关闭上下文是清理步骤
                pass
        for target in (thread, recovery_thread):
            if target and target is not threading.current_thread():
                target.join(timeout=3)
        # The maintenance loop ends with the instance: stop waiting for a new
        # round at once and give the CURRENT bounded round a chance to
        # finish (a mid-flight config fetch is bounded by its own timeout).
        watch_thread = self._watch_thread
        if watch_thread and watch_thread is not threading.current_thread():
            watch_thread.join(timeout=3)

    def call(self, method: str, path: str, stream: bool = False,
             physical_budget=None, request_deadline=None,
             auth_wait_budget=None, cancel_check=None, reader_lease=None,
             **kwargs):
        """Request with a single 401 -> re-auth retry. For streaming endpoints
        you MUST pass stream=True, otherwise requests buffers the whole SSE
        body until the upstream closes the connection.

        ``physical_budget`` may be a shared ``_PhysicalRequestBudget`` (new
        callers) or a legacy integer meaning how many real requests THIS call
        may still make. Every requests.request(), including a post-401 hidden
        re-send, consumes one unit before it is issued.

        ``auth_wait_budget`` is the ONE cumulative budget of the downstream
        HTTP request; this call never resets it across its internal re-sends.
        ``cancel_check`` is the request-scoped cancellation probe. Both are
        checked again after an auth wait and before every real upstream call.
        """
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

        def check_before_upstream():
            """Recheck cancellation, the auth budget and the deadline.

            ``_wait_recovery`` enforces the same budget before it reports
            success, but the shared cycle may publish "valid" between that
            return and this send: a request whose cumulative auth-wait budget
            is already spent must not open a new upstream connection. The
            overall deadline keeps its own error mapping and wins when it was
            the earlier bound.
            """
            if cancel_check is not None and cancel_check():
                raise _RequestCancelled("client disconnected")
            if self._shutdown_event.is_set():
                raise _RequestCancelled("instance stopping")
            if (auth_wait_budget is not None
                    and auth_wait_budget.remaining <= 0.0):
                budget_deadline = auth_wait_budget.deadline
                if (request_deadline is None
                        or budget_deadline is None
                        or budget_deadline <= request_deadline):
                    raise ReauthenticationThrottled("auth_wait_timeout")
            if (request_deadline is not None
                    and time.monotonic() >= request_deadline):
                raise _RequestDeadlineExceeded(
                    "request deadline exhausted before upstream call")

        def clipped_timeout():
            """Clip the socket timeout to the CURRENT overall deadline.

            A timeout computed before an auth wait must never outlive the
            request deadline after that wait.
            """
            if request_deadline is None:
                return timeout
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise _RequestDeadlineExceeded(
                    "request deadline exhausted before upstream call")
            if isinstance(timeout, tuple):
                return (max(0.001, min(timeout[0], remaining)),
                        max(0.001, min(timeout[1], remaining)))
            if isinstance(timeout, (int, float)):
                return max(0.001, min(timeout, remaining))
            return timeout

        def refresh_before_retry():
            # Authentication waiting has no upstream reader running. Release
            # the streaming-reader slot for the wait and re-acquire it before
            # the real upstream send; a full limit must fail here instead of
            # leaving an unbounded upstream connection or reader behind.
            if reader_lease is not None:
                reader_lease.release()
            try:
                ok = self.refresh(
                    deadline=request_deadline,
                    auth_wait_budget=auth_wait_budget,
                    cancel_check=cancel_check)
            except _AuthWaitTimeout:
                raise ReauthenticationThrottled("auth_wait_timeout")
            except _AuthWaitCapacityExceeded:
                raise ReauthenticationThrottled("auth_wait_capacity")
            if ok and reader_lease is not None:
                if not reader_lease.acquire(blocking=False):
                    raise _ReaderSlotUnavailable(
                        "stream reader capacity exhausted after auth wait")
            return ok

        for attempt in (0, 1):
            check_before_upstream()
            bearer = self.bearer()
            with self._auth_condition:
                credential_generation = self._credential_generation
            if not bearer:
                if not refresh_before_retry():
                    check_before_upstream()
                    self._raise_auth_required()
                bearer = self.bearer()
            check_before_upstream()
            if not consume_budget():
                raise _PhysicalBudgetExceeded(
                    "upstream physical request budget exhausted")
            r = requests.request(
                method, f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {bearer}",
                         **extra_headers},
                timeout=clipped_timeout(),
                stream=stream,
                **kwargs)
            if r.status_code != 401:
                return r
            # A4/A7: an explicit credential rejection never leaks to a
            # handler as a raw upstream response - every entry would
            # otherwise translate it differently (502, a pass-through 401,
            # or a 503 without a reason). Record the evidence bound to
            # the credential that was actually rejected, then fail with
            # the ONE shared error type and its specific reason.
            with self._auth_condition:
                stale_401 = (self.bearer() != bearer
                             or self._credential_generation
                             != credential_generation)
                if not stale_401:
                    # A13-2: an upstream 401 is explicit evidence
                    # that the CURRENT credential was rejected, so
                    # record it before trying to recover - the state
                    # must never keep claiming "valid" while the
                    # business path is answering 401.
                    self._record_auth_evidence(
                        "rejected", "upstream_rejected", 401, "business")
                    if self._auth_state not in ("logging_in", "failed",
                                                 "cancelled"):
                        self._auth_state = "needs_login"
            r.close()
            if attempt == 0 and budget_remaining() != 0:
                if stale_401:
                    # A newer credential was published while this
                    # request was in flight: retry once with it instead
                    # of judging the current credential by a stale 401
                    # (A1).
                    continue
                if refresh_before_retry():
                    continue
                check_before_upstream()
                self._raise_auth_required()
            check_before_upstream()
            raise ReauthenticationRequired("upstream_rejected")
        return r

    def list_models(self, force=False, raw_only=False, fresh_required=False,
                    auth_wait_budget=None, request_deadline=None,
                    cancel_check=None):
        now = time.time()
        if (self.models_cache["data"] is None or force
                or now - self.models_cache["at"] > 60):
            r = None
            try:
                r = self.call(
                    "GET", "/api/models",
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
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
                    except Exception:  # noqa: BLE001 — 释放响应是尽力而为
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


def _sanitize_upstream_trace_value(value, *, token=False):
    """One upstream trace header value, made safe for a single log line.

    Returns a bounded, single-line value verbatim so a real UM id remains
    lookup-able byte-for-byte. For ``token=True``, whitespace also makes the
    value unclaimable: HTTP parsing may already have unfolded CRLF into
    spaces. Other header fields may retain their ordinary spaces.
    Control characters, ambiguous token whitespace and oversized values
    are reported as ``unavailable:<reason>`` rather than cleaned, escaped or
    truncated near-ids. Inspection covers only the length bound plus one.
    None remains None, distinguishing an absent header from an unusable one.
    """
    if value is None:
        return None
    text = str(value)
    prefix = text[:_UPSTREAM_TRACE_VALUE_MAX_CHARS + 1]
    invalid_token = token and any(ch.isspace() for ch in prefix)
    if _has_control_chars(prefix) or invalid_token:
        return f"{_UPSTREAM_TRACE_UNAVAILABLE}:control_chars" + ("_or_whitespace" if invalid_token else "")
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
            trace[name] = _sanitize_upstream_trace_value(headers.get(name), token=name == "x-request-id")
        except Exception:  # noqa: BLE001 — 读取上游头是尽力而为，不得中断流
            trace[name] = None
    return trace


class _DegenerateRepetitionError(Exception):
    """The streamed model text fell into a repetition loop; the guard cut it."""


class _CompatStreamRetryable(Exception):
    """A compat SSE stream died before anything reached the client; the
    caller may transparently reopen the upstream without concatenation."""


class _RequestDeadlineExceeded(RuntimeError):
    """No new upstream work may start after the per-request wall deadline."""


class _RequestBodyTimeout(RuntimeError):
    """The client did not finish sending its request body in time."""








class _RequestCancelled(RuntimeError):
    """The downstream client is gone or the instance is stopping.

    This is a request-scoped cancellation: it ends only this request's
    authentication wait and prevents its later upstream call. The shared recovery
    cycle keeps running.
    """


class _AuthWaitTimeout(RuntimeError):
    """This request's own cumulative authentication wait budget expired."""


class _AuthWaitCapacityExceeded(RuntimeError):
    """The per-instance authentication-wait slot cap is exhausted."""


class _ReaderSlotUnavailable(RuntimeError):
    """The streaming-reader slot could not be re-acquired after auth wait."""


class _AuthWaitBudget:
    """Cumulative authentication-wait budget for ONE downstream request.

    Only time actually spent inside the authentication-wait path is charged.
    Normal upstream inference, retry delays and request-body handling between
    two waits are NOT part of this budget. The same object is shared by every
    internal 401 re-send, compatibility retry and hidden retry of one logical
    request, so a second wait can never reset it. The existing overall request
    deadline stays a separate bound.
    """

    def __init__(self, seconds=None, now=None):
        self.limit = max(0.0, min(180.0, float(
            AUTH_WAIT_BUDGET_S if seconds is None else seconds)))
        self.used = 0.0
        self._segments = 0
        self._segment_started = None
        self._deadline = None
        self._lock = threading.Lock()
        self._now = time.monotonic if now is None else now

    @property
    def remaining(self):
        with self._lock:
            used = self.used
            if self._segment_started is not None:
                used += max(0.0, self._now() - self._segment_started)
            return max(0.0, self.limit - used)

    @property
    def deadline(self):
        """Absolute monotonic instant this request's budget runs out.

        While charged time is still below the limit it is the projection from the
        active (or most recent) wait segment, so normal inference between waits
        never moves it. Once the limit is actually reached it is the FIRST instant
        the cumulative charge crossed the limit and never moves again: a later wait
        segment starts after that instant, so recomputing from its start would push
        an already spent budget's expiry into the future and hide that the budget
        expired before the overall request deadline. It is NOT an absolute deadline
        computed when the downstream request was created: a request with no wait
        segment has no deadline.
        """
        with self._lock:
            return self._deadline

    def begin_wait(self):
        """Start charging one authentication-wait segment (nesting-safe).

        A caller may charge a synchronous auth entry and then the join/wait it
        hands off to; nesting keeps ONE wall-clock segment so the outer
        ``end_wait`` cannot stop the clock while the inner one still runs.
        The segment's absolute deadline is derived from the budget remaining at
        its start, never from the request creation time. An ALREADY spent budget
        keeps the instant its charge first crossed the limit: this later segment
        must not move that real expiry forward.
        """
        with self._lock:
            if self._segments == 0:
                self._segment_started = self._now()
                if self._deadline is None or self.used < self.limit:
                    self._deadline = (self._segment_started
                                      + max(0.0, self.limit - self.used))
            self._segments += 1

    def end_wait(self):
        """Stop charging the current segment and keep its elapsed time."""
        with self._lock:
            if self._segments <= 0:
                return
            self._segments -= 1
            if self._segments == 0 and self._segment_started is not None:
                self.used += max(0.0, self._now() - self._segment_started)
                self._segment_started = None


class _ReaderSlotLease:
    """One streaming-reader slot with idempotent release.

    A streaming request takes the slot before opening upstream (so a full limit
    refuses without an upstream connection), releases it while waiting for
    authentication, and re-acquires it before the real upstream read. The
    handler and the producer may both release it; only the first release returns
    the semaphore unit.
    """

    def __init__(self, semaphore):
        self._semaphore = semaphore
        self._held = False
        self._lock = threading.Lock()

    def acquire(self, blocking=True, timeout=None):
        with self._lock:
            if self._held:
                return True
            if not self._semaphore.acquire(blocking=blocking, timeout=timeout):
                return False
            self._held = True
            return True

    def release(self):
        with self._lock:
            if not self._held:
                return False
            self._held = False
            self._semaphore.release()
            return True


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
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_upstream_reject_error(response_text):
    """Extract short diagnostic fields from common upstream 4xx JSON shapes."""
    message = ""
    error_type = ""
    error_code = ""
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError, RecursionError):
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


def _bounded_upstream_error_message(response_text):
    """Return the bounded upstream message carried in one error detail.

    The same extractor used by the 400/422 reject diagnostics supplies the
    upstream's own message text; a body that is not a JSON object falls back to
    a bounded single-line slice. The raw body is never relayed.
    """
    if not response_text:
        return None
    message, _, _ = _extract_upstream_reject_error(response_text)
    if message:
        return _sanitize_upstream_error_value(
            message, _UPSTREAM_ERROR_MESSAGE_MAX_CHARS) or None
    return _sanitize_upstream_error_value(
        response_text, _UPSTREAM_ERROR_MESSAGE_MAX_CHARS) or None


def _bounded_upstream_error_text(response):
    """Best-effort bounded text of one upstream error response.

    Reading a failed upstream body must never mask the status it came with: a
    decode/read failure yields an empty diagnostic instead of raising.
    """
    try:
        return response.text[:_UPSTREAM_ERROR_BODY_CAPTURE_MAX_CHARS]
    except Exception:  # noqa: BLE001 — 上游错误体读取失败按空文本处理
        return ""


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
    except Exception:  # noqa: BLE001 — 诊断构造失败只标记 failed
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
                         stream, body, response_text, output_budget,
                         tries=0, physical_attempts=0,
                         max_physical_attempts=MAX_TOTAL_ATTEMPTS):
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
        "upstream_reject request=%s status=%d tries=%d "
        "physical_attempts=%d/%d forwarded_model=%s "
        "requested_model=%s stream=%s error_type=%s error_code=%s "
        "error_message=%r %s output_budget %s",
        request_id, status_code, tries, physical_attempts,
        max_physical_attempts,
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
        except (ValueError, TypeError, RecursionError):
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
        except Exception as exc:  # noqa: BLE001 — 上游流读取错误按 upstream_retry/read_error 分类
            if (transient_retry_available and
                    isinstance(exc, _UPSTREAM_TRANSIENT_EXCEPTIONS)):
                return result(
                    "upstream_retry",
                    "ReadTimeout" if _is_upstream_timeout(exc)
                    else type(exc).__name__)
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
                    if self._thread is None or not self._enqueue(decoded):
                        break
        except Exception as exc:  # noqa: BLE001 — 生产者线程记录错误并结束，不能逃逸
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
            except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
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
    def _ensure_request_id(self):
        """Return this downstream request's stable correlation id.

        The id is generated lazily so an error answered before the body is read
        (405/408/413/415, local auth) still carries one, and the SAME id is
        reused by the error header, the streamed error event and the final
        failure log of one request. ``handle_one_request`` resets it per request
        so a keep-alive connection never reuses the previous id.
        """
        request_id = getattr(self, "_request_id", None)
        if not request_id:
            request_id = secrets.token_hex(6)
            self._request_id = request_id
        return request_id

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
        headers = {}
        if code >= 400:
            # A gateway-generated error must never be cached by an
            # intermediary and must carry the request id; successful bodies
            # keep their existing headers.
            headers["X-Request-Id"] = self._ensure_request_id()
            headers["Cache-Control"] = "no-store"
        if extra_headers:
            headers.update(extra_headers)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, detail=None,
               error_code: str | None = None, param: str | None = None,
               extra_headers=None, extra=None):
        """Send one local error in the client protocol's envelope.

        OpenAI-shaped routes carry ``message``/``type``/``code``/``param``/
        ``detail``. Anthropic Messages carries the top-level ``type: error`` and
        an ``error`` object with ``type``/``message``/``code`` (plus ``detail``
        when diagnostics exist). The HTTP status is never changed here: status
        mapping is a separate contract ticket. ``_json`` adds ``no-store`` and
        ``X-Request-Id`` to every self-produced error.
        """
        # Errors may fire before the request body is read (unknown path,
        # rejected control token, invalid local key). Reusing the connection
        # would let those unread bytes be parsed as the next request line
        # ("501 Unsupported method ({json}POST)"), so every error closes it.
        self.close_connection = True
        error_code = error_code or "api_error"
        error_type = _error_category(status, error_code)
        if _is_anthropic_error_path(getattr(self, "path", None)):
            error = {"type": error_type, "message": message,
                     "code": error_code}
            if detail is not None:
                error["detail"] = detail
            if extra:
                error.update(extra)
            self._json(status, {"type": "error", "error": error},
                       extra_headers)
            return
        error = {"message": message, "type": error_type,
                 "code": error_code, "param": param, "detail": detail}
        if extra:
            error.update(extra)
        self._json(status, {"error": error}, extra_headers)

    def __getattr__(self, name):
        """Answer unimplemented HTTP methods with JSON 405, never 501 HTML.

        BaseHTTPRequestHandler only looks up ``do_<METHOD>`` and, when absent,
        emits its built-in 501 HTML page. Returning this dispatcher for any
        unknown ``do_*`` method keeps every API route inside the JSON error
        contract; the dispatcher itself decides 405 + ``Allow`` for a known route
        and 404 for an unknown path.
        """
        if name.startswith("do_") and len(name) > 3:
            return self._handle_unsupported_method
        raise AttributeError(name)

    def _handle_unsupported_method(self):
        path = urllib.parse.urlparse(self.path).path
        allowed = _allowed_methods_for_path(path)
        if allowed is None:
            self._error(404, f"Unknown path: {path}", error_code="not_found")
            return
        self._error(405, "Method not allowed",
                     error_code="method_not_allowed",
                     extra_headers={"Allow": ", ".join(allowed)})

    def _reauthentication_error(self, detail=None):
        proxy = getattr(self.server, "proxy", None)
        retry_after = proxy.auth_retry_after() if proxy is not None else None
        extra_headers = ({"Retry-After": str(retry_after)}
                         if retry_after else None)
        self._error(
            503,
            "Open WebUI authentication requires an interactive sign-in",
            detail,
            "reauthentication_required",
            extra_headers=extra_headers,
        )

    def _upstream_status_error(self, status, response_text=None, *,
                               detail=None, error_code=None,
                               extra_headers=None):
        """Rebuild one upstream 4xx/5xx error in the client's protocol shape.

        The upstream status is preserved; only 401 is translated by callers into
        the gateway's own 503 reauthentication state. ``detail`` always carries
        ``upstream_status`` and a bounded ``upstream_message`` so a client can
        correlate the rebuilt body with the upstream response without receiving
        the raw body itself.
        """
        upstream_message = _bounded_upstream_error_message(response_text)
        payload = {"upstream_status": status,
                   "upstream_message": upstream_message}
        if detail:
            payload.update(detail)
        self._error(
            status,
            _upstream_status_message(status),
            payload,
            error_code=error_code or _upstream_error_code(status),
            extra_headers=extra_headers,
        )

    @staticmethod
    def _aggregate_failure_error(status, exc):
        """Map one failed SSE aggregate onto a 502 (message, detail, code).

        The reason is the aggregator's own terminal classification, never a
        guess: an explicit upstream error keeps its sanitized message, a bad frame
        and a missing terminal event name their contract, and every other failure
        (including an unexpected one) is an upstream read failure. A failed
        aggregate is never relayed as a partial success.
        """
        if status == "upstream_explicit_error":
            return ("Upstream request failed",
                    {"reason": "upstream_explicit_error",
                     "upstream_message": getattr(exc, "message", None)},
                    "upstream_explicit_error")
        if status == "upstream_bad_frame":
            return ("Upstream returned an invalid SSE frame",
                    {"reason": "upstream_bad_frame"},
                    "upstream_stream_error")
        if status == "upstream_missing_terminal":
            return ("Upstream stream ended without a complete terminal",
                    {"reason": "upstream_missing_terminal"},
                    "upstream_stream_error")
        return ("Upstream stream failed",
                {"reason": "upstream_read_error",
                 "exception": (type(exc).__name__ if exc is not None
                               else None)},
                "upstream_stream_error")

    def _consume_aggregate_stream(self, response, aggregator, reader_lease):
        """Read one upstream SSE stream into a complete aggregated body.

        The producer owns the response and the reader slot from ``start()``: it
        closes the socket and releases the slot in its own finally, so this method
        never closes either after a successful start. Returns
        ``(data, status, exc)``: ``data`` is the aggregated body only after the
        protocol's success terminal (Chat: BOTH ``[DONE]`` and a non-empty
        ``finish_reason``; Messages: ``message_stop``); otherwise ``status`` is
        ``client_disconnected``, an aggregator failure reason, or
        ``upstream_read_error`` for a transport failure. A partial completion is
        never returned as a success.
        """
        upstream = None
        try:
            upstream = _UpstreamStream(response, reader_slot=reader_lease)
            upstream.start()
        except Exception as exc:  # noqa: BLE001 — 启动失败要分类为 upstream_read_error
            if upstream is not None:
                upstream.cancel()
            try:
                response.close()
            except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                pass
            reader_lease.release()
            return None, "upstream_read_error", exc
        try:
            while True:
                if _client_disconnected(self.connection):
                    return None, "client_disconnected", None
                try:
                    item = upstream.next_item(STREAM_WATCHDOG_POLL_S)
                except queue.Empty:
                    continue
                if item is _UPSTREAM_END:
                    return aggregator.finish(), "ok", None
                aggregator.feed_line(item)
                if aggregator.finished:
                    # The protocol's terminal event is in hand: finish now
                    # instead of waiting for a keep-alive upstream to close.
                    return aggregator.finish(), "ok", None
        except _NonStreamAggregateError as exc:
            return None, exc.reason, exc
        except Exception as exc:  # noqa: BLE001 — 上游读失败按 upstream_read_error 分类
            # The only socket this loop reads is the upstream one (the client
            # is only probed, never read): a transport error here is an
            # upstream read failure, never a client disconnect.
            return None, "upstream_read_error", exc
        finally:
            upstream.cancel()

    def _control_allowed(self) -> bool:
        if not is_local_control_request(
                self.client_address[0],
                self.headers.get("Host", ""),
                self.headers.get("Origin")):
            self._error(403, "Local management endpoint denied",
                        error_code="local_management_denied")
            return False
        expected = ensure_control_token(CONTROL_TOKEN_FILE)
        supplied = self.headers.get(CONTROL_HEADER, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self._error(403, "Local management token missing or invalid",
                        error_code="invalid_control_token")
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
                        error_code="local_auth_error",
                        param="Authorization")
            return False
        return True

    def _origin_allowed(self) -> bool:
        """Reject browser-originated POSTs on the default-open local setup.

        A web page in ANY site can POST to 127.0.0.1 and would otherwise
        borrow the stored UM identity through this proxy. Local programs
        (curl, SDKs) never send Origin, so only requests that CARRY
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
            error_code="origin_denied", param="Origin")
        return False

    def _cors_allow_origin(self):
        """The ACAO value for this response, or None to send no CORS header.

        POST gates already reject browser origins that are not allowlisted;
        withholding the response header too means a forgotten gate on some
        future endpoint cannot silently re-open the browser attack surface.
        """
        origin = self.headers.get("Origin")
        return origin if origin in ALLOWED_ORIGINS else None

    def _unsupported_content_encoding(self):
        """Return the unsupported Content-Encoding tokens, or None.

        ``identity`` (and an absent header) means the body is already the JSON
        bytes this gateway parses. This batch does not decompress request
        bodies: gzip/br/zstd are all answered with 415, so the declared size
        limit keeps applying to the bytes actually received and a compressed
        payload can never expand past it.
        """
        raw = self.headers.get("Content-Encoding")
        if raw is None:
            return None
        codings = [part.strip().lower() for part in raw.split(",")
                   if part.strip()]
        if not codings or all(coding == "identity" for coding in codings):
            return None
        return ", ".join(codings)

    def _read_body_bytes(self, length):
        """Read exactly ``length`` body bytes within one total time budget.

        A client that declares a body and then stalls must not pin this handler
        forever. The socket timeout is set from the remaining body budget on
        every read, and the deadline is checked between reads, so a slow trickle
        cannot extend the 60-second budget indefinitely. The previous socket
        timeout is restored afterwards (the connection is normally closed after
        the 408 anyway, but a non-error caller must not inherit it).
        """
        connection = getattr(self, "connection", None)
        previous_timeout = None
        if connection is not None:
            try:
                previous_timeout = connection.gettimeout()
            except (AttributeError, OSError):
                connection = None
        deadline = time.monotonic() + REQUEST_BODY_TIMEOUT_S
        chunks = []
        remaining = length
        try:
            while remaining > 0:
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0:
                    raise _RequestBodyTimeout(
                        f"request body read exceeded "
                        f"{REQUEST_BODY_TIMEOUT_S:g}s")
                if connection is not None:
                    try:
                        connection.settimeout(max(0.001, remaining_budget))
                    except (AttributeError, OSError):
                        connection = None
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        except TimeoutError as exc:
            raise _RequestBodyTimeout(
                f"request body read exceeded {REQUEST_BODY_TIMEOUT_S:g}s"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.settimeout(previous_timeout)
                except (AttributeError, OSError) as exc:
                    # The connection is already gone; restoring its timeout
                    # cannot change this response and must not mask it.
                    _logger.debug(
                        "request body timeout restore skipped: %s",
                        type(exc).__name__)

    def _read_body(self):
        """Parse one JSON object body; every error is answered inline (None)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._error(400, "Invalid Content-Length header",
                        error_code="invalid_request",
                        param="Content-Length")
            return None
        unsupported_encoding = self._unsupported_content_encoding()
        if unsupported_encoding is not None:
            self._error(
                415,
                f"Unsupported Content-Encoding: {unsupported_encoding}",
                error_code="unsupported_content_encoding",
                param="Content-Encoding")
            return None
        limit = (SHARE_MAX_REQUEST_BYTES
                 if getattr(self.server, "share_gate", None) is not None
                 else MAX_REQUEST_BYTES)
        if length < 0 or length > limit:
            self._error(413, "Request body too large",
                        error_code="payload_too_large")
            return None
        if length == 0:
            return {}
        try:
            raw_body = self._read_body_bytes(length)
        except _RequestBodyTimeout:
            self._error(408, "Request body read timed out",
                        error_code="request_timeout")
            return None
        except (OSError, ValueError, RecursionError):
            self._error(400, "Request body is not valid JSON",
                        error_code="invalid_json")
            return None
        try:
            body = json.loads(raw_body)
        except (ValueError, RecursionError):
            self._error(400, "Request body is not valid JSON",
                        error_code="invalid_json")
            return None
        if not isinstance(body, dict):
            self._error(400, "Request body must be a JSON object",
                        error_code="invalid_body")
            return None
        return body

    def handle_one_request(self):
        """Serve one downstream request, then release its share slot.

        The slot belongs to exactly ONE downstream request: it is released here
        whatever path the request took - an early rejection before the send, a
        cancel, or a normal completion. Releasing twice is a no-op, so the
        request-boundary reset in ``set_request_facts`` and the process-exit
        close stay idempotent too. A process that never assembled a share gate
        has no slot and this is a no-op.
        """
        self._share_key_id = None
        self._share_ctx = None
        # One id per downstream request; a keep-alive connection must never
        # reuse the previous request's id in an error header or stream event.
        self._request_id = None
        try:
            super().handle_one_request()
        finally:
            gate = getattr(self.server, "share_gate", None)
            if gate is not None and self._share_key_id is not None:
                if self._share_ctx is not None:
                    gate.unregister_inflight(self._share_key_id,
                                            self._share_ctx)
                gate.release(self._share_key_id)
                self._share_key_id = None
                self._share_ctx = None

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

    def _share_gate_admit(self, path, raw_path, allow_api_key):
        """Authenticate and admit one share request in the merged process.

        Only the four shared routes pass through here; every other route keeps
        its normal local/admin handling. The key id installed on the request is
        the one this gate authenticated, never a client-supplied header.
        """
        gate = getattr(self.server, "share_gate", None)
        if gate is None:
            return True
        share = _load_share_auth()
        if path.startswith("/v1/models"):
            try:
                share.model_catalog_limit(raw_path)
            except ValueError:
                self._error(404, "route_not_shared",
                            error_code="route_not_shared")
                return False
        elif path.startswith("/v1/messages"):
            try:
                share.messages_query(raw_path)
            except ValueError:
                self._error(404, "route_not_shared",
                            error_code="route_not_shared")
                return False
        elif path not in ("/v1/chat/completions", "/v1/responses"):
            self._error(404, "route_not_shared",
                        error_code="route_not_shared")
            return False
        auth = gate.authenticate(self.headers, allow_api_key)
        if auth is None:
            self._error(401, "invalid_api_key",
                        error_code="invalid_api_key")
            return False
        entry, cfg = auth
        reason = {}
        if not gate.acquire(entry["key_id"], cfg["config_generation"],
                            entry["secret_version"], reason_out=reason):
            self._error(
                429, "sharing_limit_reached",
                detail={"reason": reason.get("reason"),
                         "detail": reason.get("detail")},
                error_code="sharing_limit_reached",
                extra={"reason": reason.get("reason")})
            return False
        ctx = share._InflightCtx(self.connection)
        gate.register_inflight(entry["key_id"], ctx)
        self._share_key_id = entry["key_id"]
        self._share_ctx = ctx
        protocol = ("chat" if path == "/v1/chat/completions"
                    else "messages" if path.startswith("/v1/messages")
                    else "responses")
        set_request_facts(protocol, entry["key_id"],
                          secrets.token_hex(16), "verified")
        return True

    # ---------- GET ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy
        auth_wait_budget = _AuthWaitBudget()
        request_deadline = time.monotonic() + MAX_REQUEST_SECONDS
        cancel_check = lambda: _client_disconnected(self.connection)

        if path.startswith("/v1/"):
            if not self._share_gate_admit(path, self.path,
                                          allow_api_key=True):
                return

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
                timeout = _is_upstream_timeout(exc)
                self._error(
                    504 if timeout else 502,
                    ("Upstream version request timed out" if timeout else
                     "Failed to fetch upstream version"),
                    {"reason": type(exc).__name__},
                    error_code=("upstream_timeout" if timeout else
                                "upstream_unreachable"))
                return
            try:
                upstream_version = r.json() if r.status_code == 200 else None
            except ValueError as exc:
                self._error(502, "Upstream version response was not valid JSON",
                            type(exc).__name__,
                            error_code="invalid_upstream_response")
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
                    fresh_required=want_raw_fresh,
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(
                    getattr(exc, "reason", None) or str(exc) or None)
                return
            except _RequestCancelled:
                self.close_connection = True
                return
            except _RequestDeadlineExceeded:
                self._error(
                    504, "Upstream request exceeded the request deadline",
                    {"reason": "request_deadline_exceeded"},
                    error_code="request_deadline_exceeded")
                return
            if models is None:
                self._error(502, "Failed to fetch model list (auth may have expired)",
                            error_code="upstream_unavailable")
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
                r = proxy.call(
                    "GET", path,
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(
                    getattr(exc, "reason", None) or str(exc) or None)
                return
            except _RequestCancelled:
                self.close_connection = True
                return
            except _RequestDeadlineExceeded:
                self._error(
                    504, "Upstream request exceeded the request deadline",
                    {"reason": "request_deadline_exceeded"},
                    error_code="request_deadline_exceeded")
                return
            except requests.RequestException as exc:
                timeout = _is_upstream_timeout(exc)
                self._error(
                    504 if timeout else 502,
                    ("Upstream request timed out" if timeout else
                     "Upstream request failed"),
                    {"reason": type(exc).__name__},
                    error_code=("upstream_timeout" if timeout else
                                "upstream_unreachable"))
                return
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}",
                        error_code="not_found")

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy
        auth_wait_budget = _AuthWaitBudget()
        request_deadline = time.monotonic() + MAX_REQUEST_SECONDS
        cancel_check = lambda: _client_disconnected(self.connection)

        if path.startswith("/v1/"):
            if not self._share_gate_admit(
                    path, self.path,
                    allow_api_key=path.startswith("/v1/messages")):
                return

        # Verified share attribution (C2): verify the share entry point's marker
        # ONCE per request and install the facts for EVERY ledger row this
        # request writes. A direct loopback caller carries no marker and is
        # therefore recorded as unattributed - never guessed onto a key.
        if path in ("/v1/messages", "/api/v1/messages"):
            protocol = "messages"
        elif path in ("/v1/chat/completions", "/api/chat/completions",
                      "/api/v1/chat/completions"):
            protocol = "chat"
        elif path in ("/v1/responses", "/api/v1/responses"):
            protocol = "responses"
        else:
            protocol = None
        if protocol is not None and getattr(self.server, "share_gate",
                                            None) is None:
            set_request_facts(protocol, None, None, "absent")

        if path == "/admin/share/revoke":
            # Control-token-protected in-process revoke: close every live
            # request held by one share key. The old signed notice and its
            # HMAC chain are gone with the separate 8100 process.
            gate = getattr(self.server, "share_gate", None)
            if gate is None:
                self._error(
                    503,
                    "Share revoke endpoint is not assembled in this role",
                    detail={"released": 0,
                             "reason": "share_backend_unassembled"},
                    error_code="share_revoke_unassembled")
                return
            if not self._control_allowed():
                return
            body = self._read_body()
            if body is None:
                return
            key_id = body.get("key_id")
            if not isinstance(key_id, str) or not key_id:
                self._error(400, "key_id is required",
                            error_code="invalid_request", param="key_id")
                return
            self._json(200, {"released": gate.terminate_key(key_id)})
        elif path == "/admin/auth/login":
            if self._control_allowed():
                # These admin handlers never read the request body; the 202
                # must close the connection so leftover bytes cannot desync a
                # reused socket (same failure class as _error). Closing is
                # preferred over draining the unneeded body.
                self.close_connection = True
                proxy.refresh(force=True, wait=False)
                self._json(202, proxy.auth_status())
        elif path == "/admin/auth/cancel":
            # D18-6: end THIS login round without stopping the service. The
            # control-token gate is the same as the login trigger; the answer
            # is the final state of the round (200, not a fire-and-forget
            # 202), and ordinary requests never reopen it.
            if self._control_allowed():
                self.close_connection = True
                self._json(200, proxy.cancel_login())
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
                self._chat(auth_wait_budget=auth_wait_budget,
                           request_deadline=request_deadline,
                           cancel_check=cancel_check)
        elif path in ("/v1/embeddings", "/api/v1/embeddings"):
            if self._api_allowed() and self._origin_allowed():
                self._compat_proxy(
                    "/api/v1/embeddings",
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
        elif path in ("/v1/messages", "/api/v1/messages"):
            if self._api_allowed() and self._origin_allowed():
                self._compat_proxy(
                    "/api/v1/messages", allow_stream=True,
                    forward_anthropic_headers=True,
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
        elif path in ("/v1/responses", "/api/v1/responses"):
            if self._api_allowed() and self._origin_allowed():
                self._responses(auth_wait_budget=auth_wait_budget,
                                 request_deadline=request_deadline,
                                 cancel_check=cancel_check)
        elif path.startswith("/api/"):
            # Same control-token rule as the GET passthrough.
            if not self._control_allowed():
                return
            try:
                body = self._read_body()
                if body is None:
                    return
                r = proxy.call(
                    "POST", path, json=body,
                    auth_wait_budget=auth_wait_budget,
                    request_deadline=request_deadline,
                    cancel_check=cancel_check)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(
                    getattr(exc, "reason", None) or str(exc) or None)
                return
            except _RequestCancelled:
                self.close_connection = True
                return
            except _RequestDeadlineExceeded:
                self._error(
                    504, "Upstream request exceeded the request deadline",
                    {"reason": "request_deadline_exceeded"},
                    error_code="request_deadline_exceeded")
                return
            except requests.RequestException as exc:
                timeout = _is_upstream_timeout(exc)
                self._error(
                    504 if timeout else 502,
                    ("Upstream request timed out" if timeout else
                     "Upstream request failed"),
                    {"reason": type(exc).__name__},
                    error_code=("upstream_timeout" if timeout else
                                "upstream_unreachable"))
                return
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}",
                        error_code="not_found")

    def _responses(self, auth_wait_budget=None, request_deadline=None,
                   cancel_check=None):
        """Serve the Responses subset on the existing chat upstream.

        The request contract and response translation are the same ones the share
        entry uses; this local entry keeps its own auth and body limits. Tools
        are forwarded as function tools and their calls are translated back, so a
        Responses client can use the same tools the chat endpoint accepts.
        """
        body = self._read_body()
        if body is None:
            return
        if request_deadline is None:
            request_deadline = time.monotonic() + MAX_REQUEST_SECONDS
        from shared.responses_protocol import (valid_responses_request,
                                  responses_to_chat_request,
                                  chat_to_responses_response,
                                  responses_stream_events,
                                  responses_requires_messages)
        if not valid_responses_request(body):
            self._error(400, "unsupported_responses_request",
                        error_code="unsupported_responses_request")
            return
        if responses_requires_messages(body):
            # Tool results carrying images need the one upstream protocol that
            # can represent them: Anthropic Messages. Everything else keeps
            # the Chat Completions translation.
            self._responses_via_messages(
                body, auth_wait_budget=auth_wait_budget,
                request_deadline=request_deadline,
                cancel_check=cancel_check)
            return
        chat_body = responses_to_chat_request(body)
        client_stream = chat_body.get("stream") is True
        # N2b: a stream=false Responses client may use an SSE chat upstream
        # only for the selected (verified key, model, protocol) triple; the
        # native non-stream transport is the default for everything else.
        # A direct private request has no verified key id and stays native.
        upstream_stream = client_stream or nonstream_aggregate_allowed(
            "responses", body.get("model"))
        if upstream_stream and not client_stream:
            # The upstream request must itself select streaming so UM writes
            # SSE; the client's own body is not relayed with another mode.
            chat_body["stream"] = True
            chat_body.setdefault("stream_options", {"include_usage": True})
        proxy: Proxy = self.server.proxy
        started_at = time.time()
        input_est = _message_tokens(chat_body.get("messages"))
        upstream_owned = False
        try:
            r = proxy.call("POST", "/api/chat/completions",
                           stream=upstream_stream,
                           json=chat_body,
                           auth_wait_budget=auth_wait_budget,
                           request_deadline=request_deadline,
                           cancel_check=cancel_check)
        except ReauthenticationRequired as exc:
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="reauthentication_required")
            self._reauthentication_error(
                getattr(exc, "reason", None) or str(exc) or None)
            return
        except _RequestCancelled:
            self.close_connection = True
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="client_disconnected")
            return
        except _RequestDeadlineExceeded:
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="request_deadline_exceeded")
            self._error(
                504, "Upstream request exceeded the request deadline",
                {"reason": "request_deadline_exceeded"},
                error_code="request_deadline_exceeded")
            return
        except requests.RequestException as exc:
            timeout = _is_upstream_timeout(exc)
            status = "upstream_timeout" if timeout else "upstream_unreachable"
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status=status)
            self._error(
                504 if timeout else 502,
                ("Upstream request timed out" if timeout else
                 "Upstream request failed"),
                {"reason": type(exc).__name__},
                error_code=status)
            return
        try:
            if r.status_code != 200:
                response_text = _bounded_upstream_error_text(r)
                if r.status_code == 401:
                    status = "reauthentication_required"
                    self._reauthentication_error("upstream_rejected")
                else:
                    status = f"HTTP{r.status_code}"
                    self._upstream_status_error(r.status_code, response_text)
                record_usage(body["model"], client_stream, started_at, None,
                              input_est, 0, status=status)
                return
            if not client_stream and not upstream_stream:
                try:
                    payload = r.json()
                    translated = chat_to_responses_response(
                        payload, body["model"])
                except (ValueError, UnicodeError):
                    record_usage(body["model"], False, started_at, None,
                                  input_est, 0,
                                  status="unexpected_upstream_response")
                    self._error(502, "unexpected_upstream_response",
                                error_code="unexpected_upstream_response")
                    return
                self._json(200, translated)
                out_chars = 0
                for item in translated.get("output") or []:
                    if not isinstance(item, dict):
                        continue
                    for part in item.get("content") or []:
                        if isinstance(part, dict):
                            out_chars += len(part.get("text") or "")
                record_usage(body["model"], False, started_at,
                              payload.get("usage") if isinstance(
                                  payload, dict) else None,
                              input_est,
                              max(1, out_chars // 4) if out_chars else 0)
                return
            if upstream_stream and not client_stream:
                # N2b: the chat upstream answered SSE, the Responses client
                # asked for ONE JSON body. Read the complete stream, translate
                # it exactly like the native branch, and never return a partial
                # completion as a success.
                aggregate = _NonStreamChatAggregator(
                    default_model=chat_body.get("model") or body["model"])
                aggregate_lease = _ReaderSlotLease(_reader_slots)
                if not aggregate_lease.acquire(blocking=False):
                    try:
                        r.close()
                    except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                        pass
                    record_usage(body["model"], False, started_at, None,
                                  input_est, 0,
                                  status="concurrency_limit")
                    self._error(503, "Too many concurrent streaming requests",
                                error_code="too_many_concurrent_streams")
                    return
                upstream_owned = True
                aggregate_data, aggregate_status, aggregate_exc = (
                    self._consume_aggregate_stream(
                        r, aggregate, aggregate_lease))
                translated = None
                if aggregate_data is not None:
                    try:
                        translated = chat_to_responses_response(
                            aggregate_data, body["model"])
                    except (ValueError, UnicodeError):
                        record_usage(body["model"], False, started_at,
                                      aggregate.usage(), input_est,
                                      aggregate.output_estimate(),
                                      status="unexpected_upstream_response")
                        self._error(502, "unexpected_upstream_response",
                                    error_code="unexpected_upstream_response")
                        return
                    try:
                        self._json(200, translated)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                if aggregate_status == "client_disconnected":
                    self.close_connection = True
                elif aggregate_status != "ok":
                    message, detail, error_code = (
                        self._aggregate_failure_error(
                            aggregate_status, aggregate_exc))
                    try:
                        self._error(502, message, detail, error_code)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                if aggregate_status == "ok":
                    out_chars = 0
                    for item in translated.get("output") or []:
                        if not isinstance(item, dict):
                            continue
                        for part in item.get("content") or []:
                            if isinstance(part, dict):
                                out_chars += len(part.get("text") or "")
                    record_usage(body["model"], False, started_at,
                                  aggregate_data.get("usage"), input_est,
                                  max(1, out_chars // 4) if out_chars else 0)
                elif aggregate_status == "client_disconnected":
                    record_usage(body["model"], False, started_at,
                                  aggregate.usage(), input_est,
                                  aggregate.output_estimate(),
                                  status="client_disconnected")
                else:
                    record_usage(body["model"], False, started_at,
                                  aggregate.usage(), input_est,
                                  aggregate.output_estimate(),
                                  status=f"aggregate_{aggregate_status}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", self._ensure_request_id())
            self.send_header("Connection", "close")
            self.end_headers()
            response_id = "resp_" + secrets.token_hex(12)
            created_at = int(time.time())
            usage = {}
            output_chars = 0
            status = "ok"

            def send_event(name, payload):
                if name == "response.failed":
                    error = (payload.get("response") or {}).get("error")
                    if isinstance(error, dict):
                        error.setdefault("request_id",
                                          self._ensure_request_id())
                self.wfile.write(("event: " + name + "\ndata: "
                                  + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()

            try:
                for name, payload in responses_stream_events(
                        r, body["model"], response_id, created_at):
                    send_event(name, payload)
                    if name == "response.output_text.delta":
                        output_chars += len(payload.get("delta") or "")
                    elif name == "response.function_call_arguments.delta":
                        output_chars += len(payload.get("delta") or "")
                    elif name in ("response.completed",
                                  "response.incomplete"):
                        response = payload.get("response") or {}
                        usage = response.get("usage") or {}
                        status = ("ok" if name == "response.completed"
                                  else "incomplete")
                    elif name == "response.failed":
                        error = ((payload.get("response") or {})
                                 .get("error") or {})
                        status = error.get("code") or "upstream_error"
            except (requests.RequestException, OSError) as exc:
                if isinstance(exc, requests.RequestException):
                    status = "upstream_stream_error"
                    try:
                        send_event("response.failed", {
                            "type": "response.failed",
                            "response": {"id": response_id,
                                         "object": "response",
                                         "created_at": created_at,
                                         "status": "failed",
                                         "model": body["model"], "output": [],
                                         "error": {
                                             "code": status,
                                             "message": "upstream stream "
                                                        "failed"}}})
                    except (OSError, ConnectionError):
                        status = "client_disconnected"
                else:
                    status = "client_disconnected"
            finally:
                out_est = (max(1, output_chars // 4)
                           if status == "ok" else output_chars // 4)
                record_usage(body["model"], client_stream, started_at, usage,
                              input_est, out_est, status=status)
        finally:
            if not upstream_owned:
                r.close()

    def _responses_via_messages(self, body, auth_wait_budget=None,
                                 request_deadline=None, cancel_check=None):
        """Serve Responses through Anthropic Messages when a tool result has an image.

        Chat Completions has no image slot on a tool message, so only the
        image-bearing branch switches upstream protocol. The client still sees the
        Responses wire format; the translation stays in this backend.
        """
        from shared.responses_protocol import (responses_to_messages_request,
                                  messages_to_responses_response,
                                  messages_stream_events)
        messages_body = responses_to_messages_request(body)
        client_stream = messages_body.get("stream") is True
        # N2b: a stream=false Responses client may use an SSE Messages
        # upstream only for the selected (verified key, model, protocol)
        # triple; the client protocol is still Responses and every ledger row
        # keeps the CLIENT's own mode.
        upstream_stream = client_stream or nonstream_aggregate_allowed(
            "responses", body.get("model"))
        if upstream_stream and not client_stream:
            messages_body["stream"] = True
        proxy: Proxy = self.server.proxy
        started_at = time.time()
        input_est = _message_tokens(messages_body.get("messages"))
        upstream_owned = False
        if request_deadline is None:
            request_deadline = time.monotonic() + MAX_REQUEST_SECONDS
        try:
            r = proxy.call("POST", "/api/v1/messages", stream=upstream_stream,
                           json=messages_body,
                           headers={"Anthropic-Version": "2023-06-01"},
                           auth_wait_budget=auth_wait_budget,
                           request_deadline=request_deadline,
                           cancel_check=cancel_check)
        except ReauthenticationRequired as exc:
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="reauthentication_required")
            self._reauthentication_error(
                getattr(exc, "reason", None) or str(exc) or None)
            return
        except _RequestCancelled:
            self.close_connection = True
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="client_disconnected")
            return
        except _RequestDeadlineExceeded:
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status="request_deadline_exceeded")
            self._error(
                504, "Upstream request exceeded the request deadline",
                {"reason": "request_deadline_exceeded"},
                error_code="request_deadline_exceeded")
            return
        except requests.RequestException as exc:
            timeout = _is_upstream_timeout(exc)
            status = "upstream_timeout" if timeout else "upstream_unreachable"
            record_usage(body["model"], client_stream, started_at, None,
                          input_est, 0, status=status)
            self._error(
                504 if timeout else 502,
                ("Upstream request timed out" if timeout else
                 "Upstream request failed"),
                {"reason": type(exc).__name__},
                error_code=status)
            return
        try:
            if r.status_code != 200:
                response_text = _bounded_upstream_error_text(r)
                if r.status_code == 401:
                    status = "reauthentication_required"
                    self._reauthentication_error("upstream_rejected")
                else:
                    status = f"HTTP{r.status_code}"
                    self._upstream_status_error(r.status_code, response_text)
                record_usage(body["model"], client_stream, started_at, None,
                              input_est, 0, status=status)
                return
            if not client_stream and not upstream_stream:
                try:
                    payload = r.json()
                    translated = messages_to_responses_response(
                        payload, body["model"])
                except (ValueError, UnicodeError):
                    record_usage(body["model"], False, started_at, None,
                                  input_est, 0,
                                  status="unexpected_upstream_response")
                    self._error(502, "unexpected_upstream_response")
                    return
                self._json(200, translated)
                out_chars = 0
                for item in translated.get("output") or []:
                    if not isinstance(item, dict):
                        continue
                    for part in item.get("content") or []:
                        if isinstance(part, dict):
                            out_chars += len(part.get("text") or "")
                record_usage(body["model"], False, started_at,
                              translated.get("usage"), input_est,
                              max(1, out_chars // 4) if out_chars else 0)
                return
            if upstream_stream and not client_stream:
                # N2b: the Messages upstream answered SSE, the Responses
                # client asked for ONE JSON body. Read the complete stream,
                # translate the SAME way the native branch does, and never
                # return a partial message as a success.
                aggregate = _NonStreamAnthropicAggregator(
                    default_model=messages_body.get("model") or body["model"])
                aggregate_lease = _ReaderSlotLease(_reader_slots)
                if not aggregate_lease.acquire(blocking=False):
                    try:
                        r.close()
                    except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                        pass
                    record_usage(body["model"], False, started_at, None,
                                  input_est, 0,
                                  status="concurrency_limit")
                    self._error(503, "Too many concurrent streaming requests",
                                error_code="too_many_concurrent_streams")
                    return
                upstream_owned = True
                aggregate_data, aggregate_status, aggregate_exc = (
                    self._consume_aggregate_stream(
                        r, aggregate, aggregate_lease))
                translated = None
                if aggregate_data is not None:
                    try:
                        translated = messages_to_responses_response(
                            aggregate_data, body["model"])
                    except (ValueError, UnicodeError):
                        record_usage(body["model"], False, started_at,
                                      _anthropic_usage_fields(
                                          aggregate.usage()) or None,
                                      input_est, aggregate.output_estimate(),
                                      status="unexpected_upstream_response")
                        self._error(502, "unexpected_upstream_response",
                                    error_code="unexpected_upstream_response")
                        return
                    try:
                        self._json(200, translated)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                if aggregate_status == "client_disconnected":
                    self.close_connection = True
                elif aggregate_status != "ok":
                    message, detail, error_code = (
                        self._aggregate_failure_error(
                            aggregate_status, aggregate_exc))
                    try:
                        self._error(502, message, detail, error_code)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                if aggregate_status == "ok":
                    out_chars = 0
                    for item in translated.get("output") or []:
                        if not isinstance(item, dict):
                            continue
                        for part in item.get("content") or []:
                            if isinstance(part, dict):
                                out_chars += len(part.get("text") or "")
                    record_usage(body["model"], False, started_at,
                                  translated.get("usage"), input_est,
                                  max(1, out_chars // 4) if out_chars else 0)
                elif aggregate_status == "client_disconnected":
                    record_usage(body["model"], False, started_at,
                                  _anthropic_usage_fields(
                                      aggregate.usage()) or None,
                                  input_est, aggregate.output_estimate(),
                                  status="client_disconnected")
                else:
                    record_usage(body["model"], False, started_at,
                                  _anthropic_usage_fields(
                                      aggregate.usage()) or None,
                                  input_est, aggregate.output_estimate(),
                                  status=f"aggregate_{aggregate_status}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", self._ensure_request_id())
            self.send_header("Connection", "close")
            self.end_headers()
            response_id = "resp_" + secrets.token_hex(12)
            created_at = int(time.time())
            usage = {}
            output_chars = 0
            status = "ok"

            def send_event(name, payload):
                if name == "response.failed":
                    error = (payload.get("response") or {}).get("error")
                    if isinstance(error, dict):
                        error.setdefault("request_id",
                                          self._ensure_request_id())
                self.wfile.write(("event: " + name + "\ndata: "
                                  + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()

            try:
                for name, payload in messages_stream_events(
                        r, body["model"], response_id, created_at):
                    send_event(name, payload)
                    if name == "response.output_text.delta":
                        output_chars += len(payload.get("delta") or "")
                    elif name == "response.function_call_arguments.delta":
                        output_chars += len(payload.get("delta") or "")
                    elif name in ("response.completed",
                                  "response.incomplete"):
                        response = payload.get("response") or {}
                        usage = response.get("usage") or {}
                        status = ("ok" if name == "response.completed"
                                  else "incomplete")
                    elif name == "response.failed":
                        error = ((payload.get("response") or {})
                                 .get("error") or {})
                        status = error.get("code") or "upstream_error"
            except (requests.RequestException, OSError) as exc:
                if isinstance(exc, requests.RequestException):
                    status = "upstream_stream_error"
                    try:
                        send_event("response.failed", {
                            "type": "response.failed",
                            "response": {"id": response_id,
                                         "object": "response",
                                         "created_at": created_at,
                                         "status": "failed",
                                         "model": body["model"], "output": [],
                                         "error": {
                                             "code": status,
                                             "message": "upstream stream "
                                                        "failed"}}})
                    except (OSError, ConnectionError):
                        status = "client_disconnected"
                else:
                    status = "client_disconnected"
            finally:
                out_est = (max(1, output_chars // 4)
                           if status == "ok" else output_chars // 4)
                record_usage(body["model"], client_stream, started_at, usage,
                              input_est, out_est, status=status)
        finally:
            if not upstream_owned:
                r.close()

    def _compat_proxy(self, upstream_path, allow_stream=False,
                      forward_anthropic_headers=False,
                      auth_wait_budget=None, request_deadline=None,
                      cancel_check=None):
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
        if getattr(self.server, "share_gate", None) is not None \
                and upstream_path == "/api/v1/messages" \
                and not _load_share_auth().valid_messages_request(body):
            self._error(400, "unsupported_messages_request",
                        error_code="unsupported_messages_request")
            return
        client_stream = allow_stream and bool(body.get("stream"))
        # N2b: stream=false Messages may aggregate only for the selected triple.
        upstream_stream = client_stream or (allow_stream and
                                          nonstream_aggregate_allowed(
                                              "messages", body.get("model")))
        if upstream_stream and not client_stream:
            # The upstream request must itself select streaming so UM writes
            # SSE; the client's own body is not relayed with another mode.
            body["stream"] = True
        # Anthropic Messages usage accounting is armed ONLY for that protocol,
        # and the stream accumulator only for a client stream: a non-stream
        # request carries its complete usage in the body.
        record_messages = upstream_path == "/api/v1/messages"
        messages_model = body.get("model") or ""
        messages_in_est = _message_tokens(body.get("messages"))
        stream_usage = (_AnthropicStreamUsage()
                        if record_messages and client_stream else None)
        headers = {}
        if forward_anthropic_headers:
            for name in ("Anthropic-Version", "Anthropic-Beta"):
                value = self.headers.get(name)
                if value:
                    headers[name] = value

        deadline = (time.monotonic() + MAX_REQUEST_SECONDS
                    if request_deadline is None else request_deadline)
        compat_request_id = self._ensure_request_id()
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
        # Last upstream HTTP status seen by this logical request, including an
        # attempt that is later retried. A deadline reached while waiting for a
        # retry reports it in ``detail.last_upstream_status``.
        last_upstream_status = None
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

        def record_messages_usage(status, payload=None, usage=None, out_est=None):
            """Append ONE ledger row for this logical Messages request.

            Called exactly once per request: every terminal path below ends in a
            ``return``, while a retry path CONTINUES the loop and never
            records, so several upstream attempts of one logical request can
            never be counted twice. A non-stream 200 body carries the complete
            usage; a stream carries it through ``stream_usage``. An interrupted
            stream records its known lower bound with a non-"ok" status instead
            of a fabricated complete success. A failed aggregate passes the
            aggregator's raw Anthropic usage block and output estimate through
            ``usage``/``out_est``: the same translation boundary runs, so an
            unknown side stays unknown instead of a fabricated zero.
            """
            if not record_messages:
                return
            usage_source = None
            out_est_value = 0
            if stream_usage is not None:
                usage = stream_usage.usage()
                usage_source = stream_usage.source()
                out_est_value = stream_usage.output_estimate()
            elif payload is not None:
                # The coverage is DERIVED by the normalization boundary
                # (record_usage), exactly like the stream path's source():
                # a complete block is "upstream", a single-side block is
                # "partial" (the other side stays the local estimate), and a
                # body without a usage block falls back to the estimates.
                # Hardcoding "upstream" for any non-None block would let a
                # single-side report masquerade as full upstream coverage.
                usage = _anthropic_message_usage(payload)
                out_est_value = _anthropic_output_estimate(payload)
            elif usage is not None or out_est is not None:
                usage = _anthropic_usage_fields(usage) or None
                out_est_value = out_est if out_est is not None else 0
            record_usage(messages_model, client_stream, started_compat_wall,
                         usage, messages_in_est, out_est_value, status=status,
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
                "tries=%d physical_attempts=%d/%d "
                "upstream_retries=%d slow_retries=%d "
                "repetition_retries=%d duration_ms=%d",
                compat_request_id, upstream_path, final,
                logical_attempts, physical_budget.used, MAX_TOTAL_ATTEMPTS,
                transient_retries_done, slow_retries_done,
                repetition_retries_done,
                int((time.monotonic() - started_compat) * 1000))

        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                detail = {"reason": "request_deadline_exceeded"}
                if last_upstream_status is not None:
                    detail["last_upstream_status"] = last_upstream_status
                self._error(
                    504, "Upstream compatibility endpoint exceeded the request deadline",
                    detail, error_code="request_deadline_exceeded")
                log_summary("request_deadline_exceeded")
                record_messages_usage("request_deadline_exceeded")
                return
            if physical_budget.remaining <= 0:
                self._error(
                    502, "Upstream compatibility endpoint exhausted the physical request budget",
                    error_code="upstream_attempt_budget_exhausted")
                log_summary("physical_budget_exhausted")
                record_messages_usage("physical_budget_exhausted")
                return

            logical_attempts += 1
            attempt_started = time.monotonic()
            response = None
            try:
                response = proxy.call(
                    "POST", upstream_path, json=body, stream=upstream_stream,
                    headers=headers,
                    timeout=(max(0.001, min(15.0, remaining_s)),
                             max(0.001, min(180.0, remaining_s))),
                    physical_budget=physical_budget,
                    request_deadline=deadline,
                    auth_wait_budget=auth_wait_budget,
                    cancel_check=cancel_check)
            except ReauthenticationRequired as exc:
                self._reauthentication_error(
                    getattr(exc, "reason", None) or str(exc) or None)
                log_summary("reauthentication_required")
                record_messages_usage("reauthentication_required")
                return
            except _RequestCancelled:
                self.close_connection = True
                log_summary("client_disconnected")
                record_messages_usage("client_disconnected")
                return
            except (_RequestDeadlineExceeded, _PhysicalBudgetExceeded) as exc:
                deadline_exceeded = isinstance(exc, _RequestDeadlineExceeded)
                error_type = ("request_deadline_exceeded" if deadline_exceeded
                              else "upstream_attempt_budget_exhausted")
                detail = {"reason": type(exc).__name__}
                if last_upstream_status is not None:
                    detail["last_upstream_status"] = last_upstream_status
                self._error(
                    504 if deadline_exceeded else 502,
                    ("Upstream compatibility endpoint exceeded the request "
                     "deadline" if deadline_exceeded else
                     "Upstream compatibility endpoint could not start another "
                     "attempt"),
                    detail, error_code=error_type)
                log_summary(error_type)
                record_messages_usage(error_type)
                return
            except requests.RequestException as exc:
                elapsed = time.monotonic() - attempt_started
                action = _compat_pre_output_retry(
                    self, exc, elapsed, retry_available, proxy,
                    auth_wait_budget, deadline, cancel_check,
                    compat_request_id, record_messages_usage, log_summary)
                if action == "handled":
                    return
                if action == "retry" and schedule_retry(
                        type(exc).__name__, elapsed):
                    continue
                timeout = _is_upstream_timeout(exc)
                detail = {"reason": type(exc).__name__}
                if last_upstream_status is not None:
                    detail["last_upstream_status"] = last_upstream_status
                self._error(
                    504 if timeout else 502,
                    ("Upstream compatibility endpoint timed out" if timeout
                     else "Failed to reach upstream compatibility endpoint"),
                    detail,
                    error_code=("upstream_timeout" if timeout else
                                "upstream_unreachable"))
                log_summary(("upstream_timeout" if timeout else
                             type(exc).__name__))
                record_messages_usage(("upstream_timeout" if timeout else
                                      type(exc).__name__))
                return

            elapsed = time.monotonic() - attempt_started
            if response.status_code in _UPSTREAM_RETRYABLE_STATUS:
                status_code = response.status_code
                last_upstream_status = status_code
                if retry_available(_attempt_is_slow(elapsed)):
                    delay_response = response
                    should_retry = schedule_retry(
                        f"HTTP{status_code}", elapsed, response=delay_response)
                    if should_retry:
                        response.close()
                        continue
                    response.close()
                    detail = {"reason": "request_deadline_exceeded",
                              "last_upstream_status": status_code}
                    self._error(
                        504, "Upstream compatibility endpoint reached the "
                        "request deadline", detail,
                        error_code="request_deadline_exceeded")
                    log_summary("request_deadline_exceeded")
                    record_messages_usage("request_deadline_exceeded")
                    return
                # Retry budget exhausted: the final upstream status is
                # preserved and rebuilt in the client protocol's envelope by
                # _passthrough, never folded into a synthetic 502.

            if client_stream and response.status_code == 200:
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
                            error_code="degenerate_repetition")
                        log_summary("degenerate_repetition")
                        record_messages_usage("degenerate_repetition")
                        return
                    action = _compat_pre_output_retry(
                        self, exc, elapsed, retry_available, proxy,
                        auth_wait_budget, deadline, cancel_check,
                        compat_request_id, record_messages_usage, log_summary)
                    if action == "handled":
                        return
                    if action == "retry" and schedule_retry(
                            str(exc), elapsed, stream_retry=True):
                        continue
                    status, error_code = _hidden_retry_failure(reason)
                    self._error(
                        status,
                        ("Upstream compatibility stream timed out"
                         if status == 504 else
                         "Upstream compatibility stream failed before any "
                         "event"),
                        {"reason": reason}, error_code=error_code)
                    log_summary(f"stream:{exc}")
                    record_messages_usage(
                        "upstream_timeout" if status == 504 else
                        "upstream_stream_error")
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

            if upstream_stream and not client_stream and \
                    response.status_code == 200:
                # N2b: the Messages upstream answered SSE, the client asked
                # for ONE JSON body. Read the complete stream before
                # answering: a malformed frame, an explicit upstream error, a
                # missing message_stop or a broken connection is a 502, never a
                # partial 200. The producer owns the response and the reader
                # slot from start(); a client disconnect records the cancelled
                # turn exactly like the stream path, and the ledger's stream
                # field stays the client's own (False).
                aggregate = _NonStreamAnthropicAggregator(
                    default_model=messages_model)
                aggregate_lease = _ReaderSlotLease(_reader_slots)
                if not aggregate_lease.acquire(blocking=False):
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                        pass
                    self._error(503, "Too many concurrent streaming requests",
                                error_code="too_many_concurrent_streams")
                    log_summary("concurrency_limit")
                    record_messages_usage("concurrency_limit")
                    return
                aggregate_data, aggregate_status, aggregate_exc = (
                    self._consume_aggregate_stream(
                        response, aggregate, aggregate_lease))
                if aggregate_data is not None:
                    try:
                        self._json(200, aggregate_data)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                elif aggregate_status == "client_disconnected":
                    self.close_connection = True
                else:
                    if aggregate_status == "upstream_explicit_error":
                        _logger.warning(
                            "upstream explicit stream error request=%s "
                            "endpoint=%s message=%r", compat_request_id,
                            upstream_path,
                            getattr(aggregate_exc, "message", None))
                    message, detail, error_code = (
                        self._aggregate_failure_error(
                            aggregate_status, aggregate_exc))
                    try:
                        self._error(502, message, detail, error_code)
                    except (ConnectionError, OSError, ValueError):
                        aggregate_status = "client_disconnected"
                        self.close_connection = True
                if aggregate_status == "ok":
                    record_messages_usage("ok", payload=aggregate_data)
                elif aggregate_status == "client_disconnected":
                    record_messages_usage(
                        "client_disconnected", usage=aggregate.usage(),
                        out_est=aggregate.output_estimate())
                else:
                    record_messages_usage(
                        f"aggregate_{aggregate_status}", usage=aggregate.usage(),
                        out_est=aggregate.output_estimate())
                log_summary(aggregate_status)
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
                except Exception:  # noqa: BLE001 — 上游错误体读取失败按空文本处理
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
                    requested_model=messages_model, stream=client_stream,
                    body=body, response_text=response_text,
                    output_budget=messages_output_budget,
                    tries=logical_attempts,
                    physical_attempts=physical_budget.used)
            self._passthrough(response)
            log_summary(f"HTTP{status_code}")
            record_messages_usage(f"HTTP{status_code}", payload=payload)
            return

    def _chat(self, auth_wait_budget=None, request_deadline=None,
              cancel_check=None):
        proxy: Proxy = self.server.proxy
        body = self._read_body()
        if body is None:
            return
        if getattr(self.server, "share_gate", None) is not None \
                and not _load_share_auth().valid_chat_request(body):
            self._error(400, "unsupported_chat_request",
                        error_code="unsupported_chat_request")
            return
        if not body.get("messages"):
            self._error(400, "Request body is missing the messages field",
                        error_code="invalid_request", param="messages")
            return

        req_model = body.get("model") or ""
        started_at = time.time()
        # Per-request id so every failure log line of one downstream request
        # can be tied together in chat2api.log. The SAME value is returned in
        # the X-Request-Id error header and in any in-band stream error event.
        request_id = self._ensure_request_id()
        # The client's response mode and the upstream transport mode are
        # separate (N1): R13 aggregates only a fully selected (verified key,
        # model, protocol) triple, while every existing caller keeps the native
        # transport. A direct private request has no verified key id and stays
        # native. Every downstream decision uses client_stream; only the
        # upstream request/read/reader-slot decision uses upstream_stream.
        client_stream = bool(body.get("stream"))
        upstream_stream = client_stream or nonstream_aggregate_allowed(
            "chat", req_model)

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
                        error_code=image_error["type"], param="messages")
            _logger.info(
                "request summary request=%s final=%s tries=0 "
                "physical_attempts=0/%d "
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

        if upstream_stream:
            # Ask the backend for a usage chunk so accounting is real, not
            # estimated; backends that reject the option get one plain retry.
            body.setdefault("stream_options", {"include_usage": True})
        if upstream_stream and not client_stream:
            # The client asked for ONE JSON body, but the upstream request must
            # itself select streaming so UM writes SSE instead of a buffered
            # non-stream response (N1). The client's own body is not relayed
            # with a different response mode.
            body["stream"] = True

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
                "chat failure request=%s stage=%s tries=%d "
                "physical_attempts=%d/%d upstream_status=%s "
                "exception=%s downstream=%d stream=%s model=%s duration_ms=%d",
                request_id, stage, logical_attempts, physical_budget.used,
                MAX_TOTAL_ATTEMPTS,
                "-" if upstream_status is None else upstream_status,
                exc_name or "-", downstream_code, client_stream, req_model,
                int((time.time() - started_at) * 1000))

        physical_budget = _PhysicalRequestBudget(MAX_TOTAL_ATTEMPTS)
        logical_attempts = 0
        transient_retries_done = 0
        slow_retries_done = 0
        repetition_retries_done = 0
        compat_retry_seen = False
        # Last upstream HTTP status seen by this logical request, including an
        # attempt that is later retried. A deadline reached while waiting for a
        # retry reports it in ``detail.last_upstream_status``.
        last_upstream_status = None

        # Hard wall-clock bound: attempt counts alone allow a silent gateway
        # (e.g. 120s first-byte budget x 6 attempts ≈ 12 minutes) to pin an
        # unattended client. Beyond this the request stops retrying and
        # surfaces its last failure immediately.
        request_deadline = (time.monotonic() + MAX_REQUEST_SECONDS
                            if request_deadline is None
                            else request_deadline)

        def chat_call_timeout():
            """Per-attempt socket read timeout capped by the request deadline:
            a silent upstream can never outlive the wall-clock budget."""
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise _RequestDeadlineExceeded(
                    "chat request wall-clock deadline exhausted")
            # The per-socket read cap follows the CLIENT's response mode, not
            # the upstream transport: the aggregate serves stream=false, so it
            # keeps the existing 600s non-stream cap (clipped to the request
            # deadline) and never inherits the 120/180s stream boundaries.
            read_cap = STREAM_SOCKET_TIMEOUT_S if client_stream else 600
            return (max(0.001, min(15.0, remaining)),
                    max(0.001, min(read_cap, remaining)))

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

        def open_chat_response(reader_lease=None):
            """Open one chat response, transparently retrying transient setup failures.

            Every real HTTP request consumes the shared physical budget,
            including a hidden post-401 re-send inside Proxy.call(). The
            stream_options compatibility retry does not consume the transient
            failure budget, but it still consumes one physical request.
            """
            nonlocal transient_retries_done, slow_retries_done, compat_retry_seen
            nonlocal last_upstream_status, logical_attempts
            compat_retry_used = False
            while True:
                if time.monotonic() >= request_deadline:
                    raise _RequestDeadlineExceeded(
                        "chat request wall-clock deadline exhausted")
                if physical_budget.remaining <= 0:
                    raise _PhysicalBudgetExceeded(
                        "chat physical request budget exhausted")
                logical_attempts += 1
                attempt_started = time.monotonic()
                try:
                    rr = proxy.call(
                        "POST", "/api/chat/completions", json=body,
                        stream=upstream_stream, timeout=chat_call_timeout(),
                        physical_budget=physical_budget,
                        request_deadline=request_deadline,
                        auth_wait_budget=auth_wait_budget,
                        cancel_check=cancel_check,
                        reader_lease=reader_lease)
                except ReauthenticationRequired:
                    raise
                except _RequestCancelled:
                    raise
                except _UPSTREAM_TRANSIENT_EXCEPTIONS as exc:
                    elapsed = time.monotonic() - attempt_started
                    slow = _attempt_is_slow(elapsed)
                    if transient_retry_available(slow):
                        # The pre-output credential check can wait up to the
                        # full authentication budget. Release the streaming
                        # reader slot for that wait and take it back before the
                        # retry send, exactly like the post-401 re-send path
                        # (refresh_before_retry); otherwise a 180-second wait
                        # would hold one of the reader slots and concurrent
                        # clients would see a spurious 503.
                        if reader_lease is not None:
                            reader_lease.release()
                        outcome = _pre_output_verify(
                            proxy, auth_wait_budget, request_deadline,
                            cancel_check, "chat_setup", request_id)
                        if outcome == "retry" and reader_lease is not None:
                            if not reader_lease.acquire(blocking=False):
                                raise _ReaderSlotUnavailable(
                                    "stream reader capacity exhausted after "
                                    "pre-output auth wait")
                        if outcome == "original":
                            raise
                        if outcome in ("cancelled", "stopped"):
                            raise _RequestCancelled("client disconnected")
                        if outcome in ("auth_timeout", "auth_capacity"):
                            raise ReauthenticationThrottled(outcome)
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

                last_upstream_status = rr.status_code
                if (rr.status_code == 400 and upstream_stream and
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
        reader_lease = (_ReaderSlotLease(_reader_slots)
                        if upstream_stream else None)
        if reader_lease is not None and not reader_lease.acquire(blocking=False):
            log_failure("concurrency_limit_rejected", 503)
            self._error(
                503, "Too many concurrent streaming requests",
                f"concurrent reader limit {STREAM_MAX_CONCURRENT_READERS} "
                "reached; retry later", error_code="too_many_concurrent_streams")
            return
        # Streaming reads are bounded per socket read by chat_call_timeout()
        # above (STREAM_SOCKET_TIMEOUT_S cap, also clipped to the request
        # deadline); the NATIVE non-stream path and the non-stream client of
        # the aggregate path use the existing flat 600s read cap.
        response = None
        compat_retry_used = False
        try:
            try:
                r, compat_retry_used = open_chat_response(reader_lease)
            except _ReaderSlotUnavailable:
                log_failure("concurrency_limit_reacquire_failed", 503)
                self._error(
                    503, "Too many concurrent streaming requests",
                    f"concurrent reader limit {STREAM_MAX_CONCURRENT_READERS} "
                    "reached; retry later", error_code="too_many_concurrent_streams")
                return
            except ReauthenticationRequired as exc:
                log_failure("reauthentication_required", 503,
                            exc_name=type(exc).__name__)
                self._reauthentication_error(
                    getattr(exc, "reason", None) or str(exc) or None)
                return
            except _RequestCancelled:
                self.close_connection = True
                log_failure("client_disconnected", 0)
                return
            except (_RequestDeadlineExceeded, _PhysicalBudgetExceeded) as exc:
                deadline_exceeded = isinstance(exc, _RequestDeadlineExceeded)
                stage = ("request_deadline_exceeded" if deadline_exceeded
                         else "physical_budget_exhausted")
                log_failure(stage, 504 if deadline_exceeded else 502,
                            exc_name=type(exc).__name__)
                detail = {"reason": type(exc).__name__}
                if last_upstream_status is not None:
                    detail["last_upstream_status"] = last_upstream_status
                self._error(
                    504 if deadline_exceeded else 502,
                    ("Upstream request exceeded the request deadline"
                     if deadline_exceeded else
                     "Upstream request could not start another attempt"),
                    detail,
                    error_code=("request_deadline_exceeded" if
                                deadline_exceeded else
                                "upstream_attempt_budget_exhausted"))
                return
            except requests.RequestException as exc:
                timeout = _is_upstream_timeout(exc)
                log_failure("compat_retry_request_exception"
                            if compat_retry_seen else
                            "upstream_request_exception",
                            504 if timeout else 502,
                            exc_name=type(exc).__name__)
                detail = {"reason": type(exc).__name__}
                if last_upstream_status is not None:
                    detail["last_upstream_status"] = last_upstream_status
                self._error(
                    504 if timeout else 502,
                    ("Upstream request timed out" if timeout else
                     "Upstream request failed"),
                    detail,
                    error_code=("upstream_timeout" if timeout else
                                "upstream_unreachable"))
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
                response_text = _bounded_upstream_error_text(r)
                r.close()
                if status_code == 401:
                    # The retry used a credential that was just published;
                    # its explicit rejection is a reauthentication failure,
                    # never a generic upstream 502.
                    status = "reauthentication_required"
                    self._reauthentication_error("upstream_rejected")
                elif status_code in (400, 422):
                    # Request-validation failure: mapping it to 502 would let
                    # a retry-capable client replay the SAME payload as a transient failure and
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
                        requested_model=req_model, stream=client_stream, body=body,
                        response_text=response_text,
                        output_budget=output_budget,
                        tries=logical_attempts,
                        physical_attempts=physical_budget.used)
                    self._upstream_status_error(status_code, response_text)
                    return
                log_failure("compat_retry_non_200" if compat_retry_used
                            else "upstream_non_200", status_code,
                            upstream_status=status_code)
                self._upstream_status_error(status_code, response_text)
                return

            if not upstream_stream:
                # Native non-stream transport: the upstream answered one JSON
                # body and this endpoint relays it unchanged.
                try:
                    data = r.json()
                except ValueError as exc:
                    r.close()
                    log_failure("upstream_invalid_json", 502,
                                upstream_status=r.status_code,
                                exc_name=type(exc).__name__)
                    self._error(502, "Upstream returned invalid JSON",
                                type(exc).__name__,
                                error_code="invalid_upstream_response")
                    return
                r.close()
                if not isinstance(data, dict):
                    log_failure("upstream_invalid_json_shape", 502,
                                upstream_status=r.status_code,
                                exc_name="InvalidResponseShape")
                    self._error(502, "Upstream returned an invalid JSON shape",
                                type(data).__name__,
                                error_code="invalid_upstream_response")
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
                    # credential material). This native path never read an SSE
                    # stream, so it writes no usage row (the aggregate path,
                    # which did read one, records its lower-bound row).
                    log_failure("upstream_explicit_error", 502,
                                upstream_status=r.status_code)
                    _logger.warning(
                        "upstream explicit error request=%s model=%s "
                        "message=%r", request_id, req_model, error_reason)
                    self._error(502, "Upstream request failed",
                                {"reason": "upstream_explicit_error",
                                 "upstream_message": error_reason},
                                error_code="upstream_explicit_error")
                    return
                self._json(200, data)
                usage = data.get("usage")
                out_est = 0
                try:
                    content = data["choices"][0]["message"]["content"]
                    out_est = _est_tokens(content)
                except (KeyError, IndexError, TypeError):
                    # Missing usage shape only means no estimate; the request
                    # itself already succeeded.
                    pass
                record_usage(req_model, False, started_at, usage,
                             _message_tokens(body.get("messages")), out_est)
                _logger.info(
                    "request summary request=%s final=ok tries=%d "
                    "physical_attempts=%d/%d "
                    "upstream_retries=%d slow_retries=%d "
                    "images_before=%d images_after=%d images_trimmed=%d duration_ms=%d",
                    request_id, logical_attempts, physical_budget.used,
                    MAX_TOTAL_ATTEMPTS,
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
            upstream = _UpstreamStream(r, reader_slot=reader_lease)
            # Trace headers of the response that feeds the visible stream: the
            # stream-end log line reports these so a production stall carries
            # the upstream request id it can be claimed by.
            upstream_trace = _upstream_trace_headers(r)
            try:
                upstream.start()
            except Exception as exc:  # noqa: BLE001 — 生产者启动失败要转成 502
                log_failure("producer_start_failed", 502,
                            exc_name=type(exc).__name__)
                self._error(502, "Upstream stream failed to start",
                            "producer thread could not start",
                            error_code="upstream_stream_error")
                return
            reader_lease = None  # ownership transferred to the producer
        finally:
            if reader_lease is not None:
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                        pass
                reader_lease.release()

        if not client_stream:
            # N1: the upstream transport is SSE but the client asked for ONE
            # JSON body. Consume the complete stream before answering: a
            # malformed frame, an explicit upstream error, an incomplete
            # terminal (a bare [DONE] or a bare finish_reason) or a broken
            # connection is a 502, never a partial 200.
            # The reader slot stays owned by the producer (released in its
            # finally); a client disconnect cancels the read and records the
            # cancelled turn exactly like the stream path, while the ledger's
            # stream field stays the client's own (False). A failed aggregate
            # records ONE ``aggregate_<reason>`` row (usage/estimate from the
            # aggregator), never a success row.
            aggregator = _NonStreamChatAggregator(
                default_model=body.get("model") or req_model)
            aggregate_data = None
            aggregate_status = "ok"
            aggregate_exc = None
            try:
                while True:
                    if _client_disconnected(self.connection):
                        aggregate_status = "client_disconnected"
                        break
                    try:
                        item = upstream.next_item(STREAM_WATCHDOG_POLL_S)
                    except queue.Empty:
                        continue
                    if item is _UPSTREAM_END:
                        aggregate_data = aggregator.finish()
                        break
                    aggregator.feed_line(item)
            except _NonStreamAggregateError as exc:
                aggregate_status = exc.reason
                aggregate_exc = exc
            except Exception as exc:  # noqa: BLE001 — 上游读失败按 upstream_read_error 分类
                # The only socket this loop reads is the upstream one (the
                # client is only probed, never read): a transport error here
                # is an upstream read failure, never a client disconnect.
                aggregate_status = "upstream_read_error"
                aggregate_exc = exc
            finally:
                upstream.cancel()
            if aggregate_data is not None:
                try:
                    self._json(200, aggregate_data)
                except (ConnectionError, OSError, ValueError):
                    # The body may have been partially written: never attempt
                    # a second status line on a dead downstream.
                    aggregate_status = "client_disconnected"
                    self.close_connection = True
            elif aggregate_status == "upstream_explicit_error":
                aggregate_reason = getattr(aggregate_exc, "message", None)
                log_failure("upstream_explicit_error", 502,
                            upstream_status=200)
                _logger.warning(
                    "upstream explicit stream error request=%s model=%s "
                    "message=%r", request_id, req_model, aggregate_reason)
                try:
                    self._error(
                        502, "Upstream request failed",
                        {"reason": "upstream_explicit_error",
                         "upstream_message": aggregate_reason},
                        error_code="upstream_explicit_error")
                except (ConnectionError, OSError, ValueError):
                    pass
            elif aggregate_status == "upstream_bad_frame":
                log_failure("upstream_bad_frame", 502, upstream_status=200)
                try:
                    self._error(
                        502, "Upstream returned an invalid SSE frame",
                        {"reason": "upstream_bad_frame"},
                        error_code="upstream_stream_error")
                except (ConnectionError, OSError, ValueError):
                    pass
            elif aggregate_status == "upstream_missing_terminal":
                log_failure("upstream_missing_terminal", 502,
                            upstream_status=200)
                try:
                    self._error(
                        502,
                        "Upstream stream ended without a complete terminal",
                        {"reason": "upstream_missing_terminal"},
                        error_code="upstream_stream_error")
                except (ConnectionError, OSError, ValueError):
                    pass
            elif aggregate_status == "upstream_read_error":
                log_failure(
                    "upstream_read_error", 502, upstream_status=200,
                    exc_name=(type(aggregate_exc).__name__
                               if aggregate_exc is not None else None))
                try:
                    self._error(
                        502, "Upstream stream failed",
                        {"reason": "upstream_read_error",
                         "exception": (type(aggregate_exc).__name__
                                       if aggregate_exc is not None
                                       else None)},
                        error_code="upstream_stream_error")
                except (ConnectionError, OSError, ValueError):
                    pass
            if aggregate_status == "client_disconnected":
                # The downstream is gone (or the 200 body could not be
                # written): close it, and keep the reader slot owned by the
                # producer until it releases in its own finally.
                self.close_connection = True
            if aggregate_status == "ok":
                out_est = 0
                try:
                    content = aggregate_data["choices"][0]["message"]["content"]
                    out_est = _est_tokens(content)
                except (KeyError, IndexError, TypeError):
                    # Missing usage shape only means no estimate; the request
                    # itself already succeeded.
                    pass
                record_usage(
                    req_model, False, started_at, aggregate_data.get("usage"),
                    _message_tokens(body.get("messages")), out_est)
            elif aggregate_status == "client_disconnected":
                record_usage(
                    req_model, False, started_at, aggregator.usage(),
                    _message_tokens(body.get("messages")),
                    aggregator.output_estimate(),
                    status="client_disconnected")
            else:
                # Every failed aggregate still consumed upstream output, so it
                # records ONE lower-bound row like the stream path's failure
                # rows: the aggregator's own usage tail (None stays unknown,
                # never a fabricated zero) and its output estimate. Unlike the
                # native non-stream path, this path read a real SSE stream, so
                # the upstream work is accounted for. ``stream`` stays the
                # client's own mode (False).
                record_usage(
                    req_model, False, started_at, aggregator.usage(),
                    _message_tokens(body.get("messages")),
                    aggregator.output_estimate(),
                    status=f"aggregate_{aggregate_status}")
            _logger.info(
                "request summary request=%s final=%s tries=%d "
                "physical_attempts=%d/%d "
                "upstream_retries=%d slow_retries=%d "
                "images_before=%d images_after=%d images_trimmed=%d "
                "duration_ms=%d",
                request_id, aggregate_status, logical_attempts,
                physical_budget.used,
                MAX_TOTAL_ATTEMPTS, transient_retries_done,
                slow_retries_done, image_stats["images_before"],
                image_stats["images_after"], image_stats["images_trimmed"],
                int((time.time() - started_at) * 1000))
            return
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
            self.send_header("X-Request-Id", self._ensure_request_id())
            self.send_header("Connection", "close")
            origin = self._cors_allow_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()

        def fail_hidden_retry(exc_name, *, status=502,
                              error_code="upstream_stream_error",
                              extra_detail=None,
                              response_text=None, usage_status=None):
            """Surface a failed hidden retry according to header state."""
            self.close_connection = True
            detail = self._stream_error_detail(
                "upstream_read_error", started_at, 0, exc_name,
                extra=extra_detail)
            try:
                if preflight_headers_sent:
                    self._stream_error_event("upstream_read_error", detail)
                elif response_text is not None:
                    self._upstream_status_error(
                        status, response_text,
                        detail=extra_detail, error_code=error_code)
                else:
                    self._error(
                        status,
                        STREAM_ERROR_MESSAGES["upstream_read_error"],
                        detail, error_code=error_code)
            except (ConnectionError, OSError, ValueError):
                pass
            record_usage(
                req_model, True, started_at, None,
                _message_tokens(body.get("messages")), 0,
                status=usage_status or "upstream_read_error")

        def start_hidden_retry_attempt(stage):
            """Open a fresh streaming attempt while no visible body was sent."""
            nonlocal upstream_trace
            retry_lease = _ReaderSlotLease(_reader_slots)
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                log_failure(f"{stage}_retry_deadline", 504,
                            exc_name="RequestDeadlineExceeded")
                fail_hidden_retry(
                    "RequestDeadlineExceeded", status=504,
                    error_code="request_deadline_exceeded",
                    extra_detail=(
                        {"last_upstream_status": last_upstream_status}
                        if last_upstream_status is not None else None),
                    usage_status="request_deadline_exceeded")
                return None
            if not retry_lease.acquire(timeout=min(2.0, remaining)):
                log_failure(f"{stage}_retry_concurrency_limit", 502,
                            exc_name="RetrySlotUnavailable")
                fail_hidden_retry("RetrySlotUnavailable")
                return None

            retry_response = None
            slot_owned = True
            try:
                try:
                    rr, _compat = open_chat_response(retry_lease)
                except _ReaderSlotUnavailable as exc:
                    # Authentication succeeded, but another request took the
                    # freed reader slot before the real re-send. Fail before
                    # opening any upstream connection; the retry lease was
                    # already released by Proxy.call's re-acquire failure.
                    log_failure(f"{stage}_retry_concurrency_limit", 502,
                                exc_name=type(exc).__name__)
                    fail_hidden_retry("RetrySlotUnavailable")
                    return None
                except ReauthenticationRequired as exc:
                    log_failure(f"{stage}_retry_reauthentication_required",
                                 503, exc_name=type(exc).__name__)
                    reason = getattr(exc, "reason", None) or str(exc) or None
                    if preflight_headers_sent:
                        fail_hidden_retry(
                            type(exc).__name__, status=503,
                            error_code="reauthentication_required",
                            usage_status="reauthentication_required")
                    else:
                        self.close_connection = True
                        self._reauthentication_error(reason)
                        record_usage(
                            req_model, True, started_at, None,
                            _message_tokens(body.get("messages")), 0,
                            status="reauthentication_required")
                    return None
                except (_RequestDeadlineExceeded,
                        _PhysicalBudgetExceeded) as exc:
                    deadline_exceeded = isinstance(
                        exc, _RequestDeadlineExceeded)
                    log_failure(f"{stage}_retry_budget_exhausted",
                                 504 if deadline_exceeded else 502,
                                 exc_name=type(exc).__name__)
                    fail_hidden_retry(
                        type(exc).__name__,
                        status=504 if deadline_exceeded else 502,
                        error_code=("request_deadline_exceeded"
                                    if deadline_exceeded else
                                    "upstream_attempt_budget_exhausted"),
                        extra_detail=(
                            {"last_upstream_status": last_upstream_status}
                            if last_upstream_status is not None else None),
                        usage_status=("request_deadline_exceeded"
                                      if deadline_exceeded else
                                      "upstream_attempt_budget_exhausted"))
                    return None
                except requests.RequestException as exc:
                    timeout = _is_upstream_timeout(exc)
                    log_failure(f"{stage}_retry_request_exception",
                                 504 if timeout else 502,
                                 exc_name=type(exc).__name__)
                    fail_hidden_retry(
                        type(exc).__name__,
                        status=504 if timeout else 502,
                        error_code=("upstream_timeout" if timeout else
                                    "upstream_unreachable"),
                        extra_detail=(
                            {"last_upstream_status": last_upstream_status}
                            if last_upstream_status is not None else None),
                        usage_status=("upstream_timeout" if timeout else
                                      "upstream_unreachable"))
                    return None
                retry_response = rr

                if rr.status_code != 200:
                    status_code = rr.status_code
                    response_text = _bounded_upstream_error_text(rr)
                    rr.close()
                    retry_response = None
                    log_failure(f"{stage}_retry_non_200", status_code,
                                upstream_status=status_code)
                    fail_hidden_retry(
                        f"HTTP{status_code}", status=status_code,
                        error_code=_upstream_error_code(status_code),
                        response_text=response_text,
                        usage_status=f"HTTP{status_code}")
                    return None

                retry_upstream = _UpstreamStream(
                    rr, reader_slot=retry_lease)
                try:
                    retry_upstream.start()
                except Exception as exc:  # noqa: BLE001 — 重试生产者启动失败要转成重试失败
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
                        except Exception:  # noqa: BLE001 — 关闭重试响应是尽力而为
                            pass
                    retry_lease.release()

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
                except (ReauthenticationThrottled, _RequestCancelled,
                        ConnectionError, OSError) as exc:
                    upstream.cancel()
                    _handle_preflight_failure(self, exc, req_model, started_at,
                                               body, log_failure)
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
                    _release_upstream_reader(upstream)
                    outcome = _pre_output_verify(
                        proxy, auth_wait_budget, request_deadline, cancel_check,
                        "chat_preflight", request_id)
                    if outcome in ("cancelled", "stopped"):
                        self.close_connection = True
                        record_usage(
                            req_model, True, started_at, None,
                            _message_tokens(body.get("messages")), 0,
                            status="client_disconnected")
                        return
                    if outcome in ("auth_timeout", "auth_capacity"):
                        log_failure("reauthentication_required", 503,
                                     exc_name="ReauthenticationThrottled")
                        self._reauthentication_error(outcome)
                        record_usage(
                            req_model, True, started_at, None,
                            _message_tokens(body.get("messages")), 0,
                            status="reauthentication_required")
                        return
                    if outcome == "original":
                        upstream = _BufferedUpstream(upstream, prefix)
                        break
                    # The preflight only emits this action when the transient
                    # and total budgets both allow a new attempt.
                    elapsed = time.monotonic() - stream_attempt_started
                    slow = _attempt_is_slow(elapsed)
                    if not transient_retry_available(slow):
                        _release_upstream_reader(upstream)
                        retry_status, retry_code = _hidden_retry_failure(
                            retry_reason)
                        fail_hidden_retry(
                            retry_reason or "StreamFailure",
                            status=retry_status, error_code=retry_code,
                            usage_status=("upstream_timeout"
                                          if retry_status == 504 else
                                          "upstream_read_error"))
                        return
                    transient_retries_done += 1
                    if slow:
                        slow_retries_done += 1
                    _release_upstream_reader(upstream)
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
                        fail_hidden_retry(
                            "RequestDeadlineExceeded", status=504,
                            error_code="request_deadline_exceeded",
                            extra_detail=(
                                {"last_upstream_status": last_upstream_status}
                                if last_upstream_status is not None
                                else None),
                            usage_status="request_deadline_exceeded")
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
        exc_obj = None
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
            nonlocal exc_obj

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
                except (ValueError, RecursionError):
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
                    self.send_header("X-Request-Id",
                                     self._ensure_request_id())
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
        except Exception as exc:  # noqa: BLE001 — 上游读失败按 upstream_read_error 分类
            exc_name = type(exc).__name__
            exc_obj = exc
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
                    "request summary request=%s final=%s tries=%d "
                    "physical_attempts=%d/%d "
                    "upstream_retries=%d slow_retries=%d "
                    "repetition_retries=%d line_loop_hits=%d "
                    "images_before=%d images_after=%d "
                    "images_trimmed=%d duration_ms=%d",
                    request_id, status, logical_attempts,
                    physical_budget.used, MAX_TOTAL_ATTEMPTS,
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
                    # non-2xx is still honest. A first-byte/read timeout is
                    # the gateway's own wait expiring (504); every other
                    # transport failure keeps the existing 502.
                    timeout = (status == "upstream_first_byte_timeout" or
                               (status == "upstream_read_error" and
                                _is_upstream_timeout(exc_obj)))
                    try:
                        self._error(
                            504 if timeout else 502,
                            ("Upstream stream timed out" if timeout else
                             STREAM_ERROR_MESSAGES.get(
                                 status, "Upstream stream failed.")),
                            detail,
                            error_code=("upstream_timeout" if timeout else
                                         "upstream_stream_error"))
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
        """Send one in-band SSE error event; never a second HTTP status.

        The 200 headers were already sent, so this event carries the SAME
        ``X-Request-Id`` value the response header announced (or the one a
        pre-commit error would have carried) to keep the failure correlatable.
        """
        payload = {"error": {"message": STREAM_ERROR_MESSAGES.get(
                                 status, "Upstream stream failed."),
                             "type": "upstream_stream_error",
                             "code": status,
                             "request_id": self._ensure_request_id(),
                             "detail": detail}}
        self.wfile.write(
            b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8")
            + b"\n\n")
        self.wfile.flush()

    def _passthrough(self, r: requests.Response):
        try:
            if r.status_code >= 400:
                # Every exposed upstream path keeps the upstream status but
                # rebuilds the body in the client protocol's envelope; the raw
                # upstream body is never relayed.
                self._upstream_status_error(
                    r.status_code, _bounded_upstream_error_text(r))
                return
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
            close = getattr(r, "close", None)
            if close is not None:
                close()

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
            except Exception:  # noqa: BLE001 — 关闭响应是尽力而为
                pass
            self._error(503, "Too many concurrent streaming requests",
                        error_code="too_many_concurrent_streams")
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
                self.send_header("X-Request-Id", self._ensure_request_id())
                origin = self._cors_allow_origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                committed = True

            def emit_in_band_error(message):
                payload = {"type": "error", "error": {
                    "type": "api_error", "message": message,
                    "request_id": self._ensure_request_id()}}
                self.wfile.write(
                    b"event: error\ndata: "
                    + json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    + b"\n\n")
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
                    except (ValueError, RecursionError):
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
                                "upstream_read_timeout"
                                if _is_upstream_timeout(exc)
                                else type(exc).__name__) from exc
                        emit_in_band_error(
                            f"Upstream connection failed mid-stream "
                            f"({type(exc).__name__}); the completion is incomplete.")
                        status = "upstream_read_error"
                        return status
                    except Exception as exc:  # noqa: BLE001 — 未提交的兼容流失败要转成 502
                        if not committed:
                            self._error(
                                502, "Upstream compatibility stream was invalid",
                                type(exc).__name__,
                                error_code="upstream_stream_error")
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



class _ShareInstanceControl:
    """Minimal instance-channel handler for the merged 8100 process.

    The broker sends signed load/drain/resume/emergency frames over the
    instance channel. The gate re-reads the projection file per request, so a
    load only needs to terminate the one-shot revoked keys; drain/resume flip
    admission state and emergency clears it.
    """

    def __init__(self, gate):
        self.gate = gate
        self.instance_id = secrets.token_hex(16)
        self._replay = None

    def handle(self, body, secret, now=None):
        from share.broker_protocol import (ProtocolError, ReplayRegistry,
                                          validate_instance_request)
        if self._replay is None:
            self._replay = ReplayRegistry()
        try:
            request = validate_instance_request(body, secret, now=now)
        except ProtocolError as exc:
            return {"v": 2, "request_id": body.get("request_id", ""),
                    "ok": False, "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        request_id = request["request_id"]
        if request["instance_id"] != self.instance_id:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "instance_mismatch",
                    "result": {"detail": "request targets another instance"}}
        try:
            self._replay.consume(request_id, request["ts"], now=now)
        except ProtocolError as exc:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        action = request["action"]
        result = {}
        if action == "instance-status":
            snapshot = self.gate.snapshot()
            result = {"instance_id": self.instance_id,
                      "state": snapshot["state"],
                      "active_count": sum(snapshot["active"].values()),
                      "last_seen_generation": snapshot["config_generation"]}
        elif action == "load":
            fields = request.get("fields") or {}
            terminate = fields.get("terminate_keys") or ()
            released = 0
            for key_id in terminate:
                if isinstance(key_id, str):
                    released += self.gate.terminate_key(key_id)
            snapshot = self.gate.snapshot()
            result = {"instance_id": self.instance_id,
                      "generation": fields.get("generation"),
                      "fingerprint": fields.get("fingerprint"),
                      "installed": True,
                      "state": snapshot["state"],
                      "share_max_concurrency":
                          snapshot["share_max_concurrency"],
                      "security_generation":
                          snapshot["config_generation"],
                      "released": released}
        elif action == "drain":
            self.gate.set_state("draining")
            result = {"instance_id": self.instance_id,
                      "state": "draining",
                      "active_count": sum(
                          self.gate.snapshot()["active"].values())}
        elif action == "resume":
            self.gate.set_state("running")
            result = {"instance_id": self.instance_id,
                      "state": "running",
                      "active_count": sum(
                          self.gate.snapshot()["active"].values())}
        elif action == "emergency":
            snapshot = self.gate.snapshot()
            cleared = sum(snapshot["active"].values())
            for key_id in list(snapshot["active"]):
                self.gate.terminate_key(key_id)
            self.gate.set_state("stopped")
            result = {"instance_id": self.instance_id,
                      "state": "stopped",
                      "cleared_in_flight": cleared}
        else:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "bad_request",
                    "result": {"detail": "unknown instance action"}}
        return {"v": 2, "request_id": request_id, "ok": True,
                "error_code": "ok", "result": result}


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
                    help="Never open the browser (use with --token or CHAT2API_UPSTREAM_TOKEN; no re-login on 401)")
    ap.add_argument("--profile", default=PROFILE_DIR, help="Chrome user-data directory")
    ap.add_argument("--login-timeout", type=int, default=180,
                    help="Seconds to wait for manual sign-in")
    # N3: explicit, default-off transport switch for the non-streaming
    # Chat/Messages/Responses paths. The share entry may pass it; the private
    # 8000 command line and every other caller keep the native non-stream
    # transport unless this flag is present. R13: the
    # flag alone never aggregates - the startup scope must also select the
    # verified (key, model, protocol) triple.
    ap.add_argument("--nonstream-upstream-aggregate", action="store_true",
                    help="Serve stream=false clients from one aggregated "
                         "upstream SSE stream only for the configured "
                         "trusted scope (default: native)")
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
    ap.add_argument("--auth-wait-budget-seconds", type=float, default=None,
                    help="Per-request cumulative authentication wait budget "
                         "(default: 180 private/share, 30 generic; "
                         "explicit values clamp to 1..180)")
    ap.add_argument("--share-config",
                    help="Serve the in-process share gate from this projection "
                         "config (merged 8100 process); no second listener")
    ap.add_argument("--auth-mode", choices=("projection", "legacy"),
                    default=None,
                    help="Share config format enforced at every read")
    # The broker's single-authority host command still names its instance
    # channel files; the merged process re-reads the projection file itself
    # and does not need them, so they are accepted and ignored.
    ap.add_argument("--instance-key-file", help=argparse.SUPPRESS)
    ap.add_argument("--instance-pipe-prefix", help=argparse.SUPPRESS)
    ap.add_argument("--registration-file", help=argparse.SUPPRESS)
    args = ap.parse_args()
    # E19-1-S2: an isolated launcher must not put the upstream credential in
    # argv (any same-machine process can read /proc/<pid>/cmdline). The
    # environment variable is the second source: an explicit --token still wins,
    # the saved TokenStore document stays the last resort, and --login keeps its
    # interactive meaning (it is the explicit sign-in entry, not a start path).
    if not args.login and args.token is None:
        env_token = os.environ.get(UPSTREAM_TOKEN_ENV, "").strip()
        if env_token:
            args.token = env_token
    if args.no_browser and not args.token:
        if args.login:
            # --login means "sign in and save the token, then exit". A launcher
            # credential would make authenticate() return immediately and the
            # "Signed in, token saved" line would claim a sign-in that never
            # happened, so this combination is refused instead.
            ap.error("--login --no-browser requires --token: "
                      "CHAT2API_UPSTREAM_TOKEN cannot complete a sign-in")
        ap.error("--no-browser requires --token or CHAT2API_UPSTREAM_TOKEN")

    EFFORT_MODELS.extend(args.effort_model)

    global NONSTREAM_UPSTREAM_AGGREGATE, AUTH_WAIT_BUDGET_S
    # A2-1/D18-5: the 8000/8100 processes default to 180 seconds; an
    # explicit CLI/env value is clamped to 1..180. The generic in-process
    # default keeps 30.
    AUTH_WAIT_BUDGET_S = resolve_auth_wait_budget(
        args.auth_wait_budget_seconds, bool(args.share_config))
    # N3: the switch is default-off and only the explicit flag turns it on.
    # It is set before the startup record so the loaded mode is observable.
    NONSTREAM_UPSTREAM_AGGREGATE = bool(args.nonstream_upstream_aggregate)

    # Create the control token before serving so the console can read it on
    # its very first management call after a restart.
    control_token = ensure_control_token(CONTROL_TOKEN_FILE)
    if control_token:
        logging.getLogger("chat2api").info("local control token ready")

    # Exit record L1: every startup names its instance, so a shared log file
    # never leaves a startup line that cannot be tied to one port and PID.
    _logger.info(
        "startup build=%s version=%s source_rev=%s "
        "host=%s port=%d pid=%d ppid=%d launched_by=%s "
        "repetition_retries=%d upstream_fast_retries=%d "
        "upstream_slow_retries=%d slow_threshold_s=%.1f "
        "max_physical_attempts=%d max_request_seconds=%d "
        "max_images=%d auto_trim_historical_images=%s "
        "auto_trim_current_turn_images=%s nonstream_upstream_aggregate=%s "
        "nonstream_aggregate_scope=%s auth_wait_budget_s=%g",
        RUNTIME_BUILD_ID, CHAT2API_VERSION, CHAT2API_SOURCE_REV,
        args.host, args.port, os.getpid(), os.getppid(), launch_source(),
        REPETITION_GUARD_AUTO_RETRIES, UPSTREAM_AUTO_RETRIES,
        UPSTREAM_SLOW_AUTO_RETRIES, UPSTREAM_SLOW_RETRY_THRESHOLD_S,
        MAX_TOTAL_ATTEMPTS, MAX_REQUEST_SECONDS, MAX_IMAGES,
        AUTO_TRIM_HISTORICAL_IMAGES, AUTO_TRIM_CURRENT_TURN_IMAGES,
        NONSTREAM_UPSTREAM_AGGREGATE,
        nonstream_aggregate_scope_label(), AUTH_WAIT_BUDGET_S)

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
        try:
            models = proxy.list_models(force=True)
        except ReauthenticationRequired as exc:
            print("Failed to fetch models (token may have expired):",
                  getattr(exc, "reason", None) or str(exc) or None,
                  file=sys.stderr)
            sys.exit(1)
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
    server.share_gate = (_load_share_auth().ShareGate(args.share_config,
                                    auth_mode=args.auth_mode)
                         if args.share_config else None)
    server.local_api_key = LOCAL_API_KEY
    server.instance_id = None
    instance_channel = None
    if args.instance_key_file and server.share_gate is not None:
        try:
            from share.instance_pipe import (start_instance_channel,
                                             remove_registration)
            from share.secret_protection import read_private_secret_path
            instance_secret = read_private_secret_path(
                args.instance_key_file, max_bytes=128,
                label="instance key").decode("utf-8").strip()
            if not instance_secret:
                raise ValueError("instance key is empty")
        except ImportError as exc:
            print(f"实例控制通道模块不可用：{exc}；分享主服务不受影响。",
                  file=sys.stderr, flush=True)
            instance_channel = {"ready": False,
                                "error": f"instance channel module "
                                         f"unavailable: {exc}"}
        except (OSError, ValueError) as exc:
            print(f"实例控制通道未就绪：{type(exc).__name__}: {exc}；"
                  f"分享主服务不受影响。", file=sys.stderr, flush=True)
            instance_channel = {"ready": False,
                                "error": f"instance key unreadable: "
                                         f"{type(exc).__name__}"}
        else:
            control = _ShareInstanceControl(server.share_gate)
            server.instance_id = control.instance_id
            server.handle_instance_control = control.handle
            try:
                instance_channel = start_instance_channel(
                    server, instance_secret, args.instance_pipe_prefix,
                    args.registration_file)
            except OSError as exc:
                instance_channel = {"ready": False,
                                    "error": f"instance channel "
                                             f"unavailable: "
                                             f"{type(exc).__name__}"}
        if instance_channel.get("ready"):
            print(f"实例控制通道已就绪：{instance_channel['pipe_name']}",
                  flush=True)
        else:
            print(f"实例控制通道未就绪：{instance_channel.get('error')}；"
                  f"分享主服务不受影响。", file=sys.stderr, flush=True)
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
        if instance_channel is not None:
            channel_server = instance_channel.get("server")
            if channel_server is not None:
                channel_server.stop()
            if instance_channel.get("ready") and args.registration_file:
                try:
                    remove_registration(args.registration_file)
                except OSError:
                    # The listener is already stopped; a missing registration
                    # file means the broker already learned the instance is gone.
                    pass
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


# E19-1-S2: the upstream credential may arrive through the environment so an
# isolated launcher never writes it into argv (``/proc/<pid>/cmdline`` is readable
# by any same-machine process). Precedence inside main(): explicit ``--token`` >
# this variable > the saved TokenStore document. Defined after main() on purpose:
# test_share_gate_citations.py pins the line of the --login-timeout default above,
# so this block must move no line above it.
UPSTREAM_TOKEN_ENV = "CHAT2API_UPSTREAM_TOKEN"


# R13: trusted gray-release scope for the aggregate transport. The switch alone
# is not enough: an operator selects the exact (share key, model, client protocol)
# triples that may be served from an aggregated upstream SSE stream. The value is
# read from the STARTUP environment only (never a request header or body), so a
# client cannot widen its own scope. Missing, malformed or partial configuration
# disables aggregation for EVERY request (fail-closed to native).
#
# Defined after main() on purpose: test_share_gate_citations.py pins the line of
# the --login-timeout default above, so this block must move no line above it.
NONSTREAM_AGGREGATE_SCOPE_ENV = "CHAT2API_NONSTREAM_AGGREGATE_SCOPE"
_NONSTREAM_AGGREGATE_PROTOCOLS = frozenset(("chat", "messages", "responses"))
_NONSTREAM_AGGREGATE_SCOPE_FIELDS = frozenset(
    ("key_ids", "models", "protocols"))
_NONSTREAM_AGGREGATE_SCOPE_MAX_ENTRIES = 100
_NONSTREAM_AGGREGATE_SCOPE_MAX_CHARS = 256
_NONSTREAM_AGGREGATE_SCOPE_MAX_RAW = 65536


def parse_nonstream_aggregate_scope(raw):
    """Parse the trusted aggregate allowlist; None on ANY failure.

    ``raw`` is the startup environment value: a JSON object with EXACTLY
    ``key_ids``, ``models`` and ``protocols``, each a list of non-empty
    bounded strings without duplicates. Unknown protocol names are refused too.
    The all-empty object is the explicit "disabled" scope: it selects nothing,
    so every request stays native while the startup record says the operator
    decided not to enable the gray release. A PARTIALLY empty object is
    ambiguous and refused like every other deviation, so a scope is either fully
    understood or aggregation is off for every request.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if len(raw) > _NONSTREAM_AGGREGATE_SCOPE_MAX_RAW:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) \
            or set(data) != _NONSTREAM_AGGREGATE_SCOPE_FIELDS:
        return None
    parsed = {}
    for name in _NONSTREAM_AGGREGATE_SCOPE_FIELDS:
        values = data[name]
        if not isinstance(values, list) \
                or len(values) > _NONSTREAM_AGGREGATE_SCOPE_MAX_ENTRIES:
            return None
        normalized = []
        for value in values:
            if not isinstance(value, str) or not value \
                    or len(value) > _NONSTREAM_AGGREGATE_SCOPE_MAX_CHARS \
                    or value != value.strip():
                return None
            normalized.append(value)
        if len(set(normalized)) != len(normalized):
            return None
        parsed[name] = frozenset(normalized)
    if not parsed["protocols"] <= _NONSTREAM_AGGREGATE_PROTOCOLS:
        return None
    empty = [name for name in _NONSTREAM_AGGREGATE_SCOPE_FIELDS
             if not parsed[name]]
    if empty and len(empty) != len(_NONSTREAM_AGGREGATE_SCOPE_FIELDS):
        return None
    return parsed


_NONSTREAM_AGGREGATE_SCOPE_RAW = os.environ.get(
    NONSTREAM_AGGREGATE_SCOPE_ENV)
NONSTREAM_AGGREGATE_SCOPE = parse_nonstream_aggregate_scope(
    _NONSTREAM_AGGREGATE_SCOPE_RAW)


def nonstream_aggregate_scope_label():
    """Operator-facing state of the startup scope: absent/invalid/disabled/selected."""
    if not _NONSTREAM_AGGREGATE_SCOPE_RAW:
        return "absent"
    scope = NONSTREAM_AGGREGATE_SCOPE
    if scope is None:
        return "invalid"
    if not scope["key_ids"] and not scope["models"] \
            and not scope["protocols"]:
        return "disabled"
    return "keys=%d/models=%d/protocols=%s" % (
        len(scope["key_ids"]), len(scope["models"]),
        ",".join(sorted(scope["protocols"])))


def nonstream_aggregate_allowed(protocol, model):
    """True only for a fully selected (verified key, model, protocol) triple.

    Every input is trusted and comes from this process, never from the request
    body: the key id is the VERIFIED attribution fact installed at the HTTP
    boundary (``verify_share_attribution`` -> ``set_request_facts``), the
    protocol must be the one that same boundary installed, and the model is the
    handler's own client-requested model. A direct private caller carries no
    marker, so it has no key id and stays native. The switch must be on, the
    startup scope must be valid and non-empty, and all three components must
    match; any missing or mismatched component keeps the native non-stream
    transport. The explicit all-empty scope selects nothing and therefore keeps
    every request native too.
    """
    if not NONSTREAM_UPSTREAM_AGGREGATE:
        return False
    scope = NONSTREAM_AGGREGATE_SCOPE
    if not isinstance(scope, dict):
        return False
    if _request_fact("attribution") != "verified":
        return False
    if _request_fact("protocol") != protocol:
        return False
    key_id = _request_fact("key_id")
    if not isinstance(key_id, str) or not key_id:
        return False
    if not isinstance(model, str) or not model:
        return False
    key_ids = scope.get("key_ids")
    models = scope.get("models")
    protocols = scope.get("protocols")
    if not isinstance(key_ids, (set, frozenset)) or not key_ids \
            or key_id not in key_ids:
        return False
    if not isinstance(models, (set, frozenset)) or not models \
            or model not in models:
        return False
    if not isinstance(protocols, (set, frozenset)) or not protocols \
            or protocol not in protocols:
        return False
    return True


def _load_share_secret_protection():
    """Load the repo-root secret adapter independent of sys.path.

    The service runs this file as a script from ``chat2api/`` (so the
    repository root is NOT on sys.path), while
    the adapter lives one directory up. A plain
    ``from share_secret_protection import ...`` therefore raised ImportError
    there, and the fail-closed handlers swallowed it: the control token and the
    attribution key were silently denied. Reuse the module when it is already
    imported (tests, or a flat deployment), otherwise load the parent-directory
    file by path. No secret is read here.
    """
    import importlib.util
    module = (sys.modules.get("share.secret_protection")
              or sys.modules.get("share_secret_protection"))
    if module is not None:
        return module
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "share/secret_protection.py")
    spec = importlib.util.spec_from_file_location(
        "share_secret_protection", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_secret_protection"] = module
    spec.loader.exec_module(module)
    return module


def _missing_browser_error(platform=None):
    """Classify a missing browser as ``(reason, detail)``.

    Windows keeps the historical ``edge_missing`` reason (Edge is the only
    supported browser there). POSIX accepts any Chromium-family binary, so the
    precise reason is ``browser_missing``. The detail keeps the historical
    "Edge was not found" sentence so existing logs and callers still recognize
    the condition.
    """
    name = sys.platform if platform is None else platform
    if name.startswith("win"):
        return "edge_missing", "Microsoft Edge was not found"
    return ("browser_missing",
            "Microsoft Edge was not found on Windows; on Linux none of "
            "chromium, chromium-browser, google-chrome or microsoft-edge "
            "was found in PATH")


def _release_upstream_reader(upstream):
    """Stop a failed pre-output stream and return its reader slot BEFORE wait.

    The producer still owns the socket and releases the same slot in its own
    finally; the lease release is idempotent, so the authentication wait never
    holds the streaming-reader capacity while it waits for recovery.
    """
    slot = getattr(upstream, "_slot", None)
    if slot is not None:
        try:
            slot.release()
        except Exception:  # noqa: BLE001 — 释放读槽是幂等清理
            pass
    upstream.cancel()


def _clamp_auth_wait_budget(raw, fallback):
    """Parse one operator-supplied budget; an invalid value is explicit.

    The hard ceiling is 180 seconds and the floor is 1 second: a value that
    would disable the budget is raised to 1 instead of becoming an unbounded
    wait. ``None`` means "no value was supplied", so the caller keeps its role
    default.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value):
        return fallback
    return max(1.0, min(180.0, value))


def resolve_auth_wait_budget(cli_value=None, share_gateway=False):
    """Resolve the per-request authentication-wait budget (A2-1/D18-5).

    The 8000 and 8100 processes both default to 180 seconds. An explicit CLI
    value or the existing ``CHAT2API_AUTH_WAIT_BUDGET_S`` env value overrides
    the default and is clamped to 1..180, so neither an operator value nor a
    leaked local env can exceed the hard ceiling or disable the budget. The
    generic/in-process default (``None``) stays 30 unless an explicit value
    selects another legal value.
    """
    if cli_value is not None:
        return _clamp_auth_wait_budget(cli_value, 180.0)
    raw = os.environ.get("CHAT2API_AUTH_WAIT_BUDGET_S")
    value = _clamp_auth_wait_budget(raw, None)
    if value is not None:
        return value
    if share_gateway is True:
        return 180.0
    return 30.0


def _claim_pre_output_verification(auth_wait_budget):
    """Claim the ONE bounded pre-output credential check of this request.

    The flag lives on the same per-request cumulative budget as the 401 waits,
    so hidden retries, compatibility retries and re-streams can never reset it.
    """
    lock = getattr(auth_wait_budget, "_lock", None)
    if lock is None:
        return False
    with lock:
        if getattr(auth_wait_budget, "_pre_output_verified", False):
            return False
        auth_wait_budget._pre_output_verified = True
        return True


def _pre_output_verify(proxy, auth_wait_budget, request_deadline, cancel_check,
                       stage, request_id=None):
    """At most ONE bounded credential check after a non-401 pre-output error.

    Returns a safe outcome: ``retry`` (the existing retry may proceed),
    ``original`` (surface the original error, never open a login window),
    ``cancelled``/``stopped`` (no late upstream call) or
    ``auth_timeout``/``auth_capacity`` (the request-scoped auth budget ended).
    Only an explicit ``rejected``/``needs_login`` verdict joins the SAME shared
    recovery cycle; ``unknown`` never starts a login window.
    """
    if auth_wait_budget is None:
        return "retry"
    # A credential validated moments ago is already strong evidence: a normal
    # request must not gain a new probe on every read failure. Only a stale or
    # absent "valid" verdict reaches the ONE bounded check below.
    with proxy._auth_condition:
        evidence = dict(proxy._auth_evidence)
    checked_epoch = evidence.get("checked_epoch")
    if (evidence.get("verdict") == "valid"
            and isinstance(checked_epoch, (int, float))
            and time.time() - checked_epoch < 15.0):
        return "retry"
    if not _claim_pre_output_verification(auth_wait_budget):
        return "retry"
    if cancel_check is not None and cancel_check():
        return "cancelled"
    if proxy._shutdown_event.is_set():
        return "stopped"
    now = time.monotonic()
    if request_deadline is not None and now >= request_deadline:
        # The overall request deadline keeps its own error mapping; this check
        # must not extend it.
        return "retry"
    remaining = auth_wait_budget.remaining
    if request_deadline is not None:
        remaining = min(remaining, request_deadline - now)
    if remaining <= 0.0:
        budget_deadline = auth_wait_budget.deadline
        if (budget_deadline is not None and
                (request_deadline is None
                 or budget_deadline <= request_deadline)):
            return "auth_timeout"
        return "retry"
    bearer = proxy.bearer()
    with proxy._auth_condition:
        credential_generation = proxy._credential_generation
    started = time.monotonic()
    auth_wait_budget.begin_wait()
    try:
        verdict, reason, upstream_status = classify_credential(
            proxy.base_url, bearer,
            timeout=max(0.001, min(15.0, remaining)))
    except Exception as exc:  # noqa: BLE001 — 探测失败按 unknown 上报，绝不当作登录
        verdict, reason, upstream_status = "unknown", type(exc).__name__, None
    finally:
        auth_wait_budget.end_wait()
    _logger.info(
        "pre-output credential check request=%s stage=%s verdict=%s "
        "reason=%s upstream_status=%s duration_ms=%d",
        request_id or "-", stage, verdict, reason,
        "-" if upstream_status is None else upstream_status,
        int((time.monotonic() - started) * 1000))
    if cancel_check is not None and cancel_check():
        return "cancelled"
    if proxy._shutdown_event.is_set():
        return "stopped"
    if verdict == "unknown":
        # An undecidable probe is NOT evidence of an expired credential: keep
        # the original network error and never open a login window from it.
        return "retry"
    if verdict == "valid":
        return "retry"
    with proxy._auth_condition:
        stale = (proxy.bearer() != bearer
                 or proxy._credential_generation != credential_generation)
        if not stale and verdict == "rejected":
            proxy._record_auth_evidence(
                "rejected", "upstream_rejected", upstream_status,
                "pre_output")
            if proxy._auth_state not in ("logging_in", "failed", "cancelled"):
                proxy._auth_state = "needs_login"
    if stale:
        # A late result for the old credential never overrides the new one.
        return "retry"
    try:
        recovered = proxy.refresh(
            deadline=request_deadline,
            auth_wait_budget=auth_wait_budget,
            cancel_check=cancel_check)
    except _AuthWaitTimeout:
        return "auth_timeout"
    except _AuthWaitCapacityExceeded:
        return "auth_capacity"
    except _RequestCancelled:
        return "cancelled"
    if recovered:
        return "retry"
    return "original"


def _compat_pre_output_retry(handler, exc, elapsed, retry_available, proxy,
                             auth_wait_budget, deadline, cancel_check,
                             request_id, record_usage, log_summary):
    """One bounded pre-output check on the Messages pre-output retry seam."""
    candidate = (isinstance(exc, _UPSTREAM_TRANSIENT_EXCEPTIONS)
                 or isinstance(exc, _CompatStreamRetryable))
    if not candidate or not retry_available(_attempt_is_slow(elapsed)):
        return "original"
    outcome = _pre_output_verify(
        proxy, auth_wait_budget, deadline, cancel_check,
        "messages_stream" if isinstance(exc, _CompatStreamRetryable)
        else "messages_setup", request_id)
    if outcome == "retry":
        return "retry"
    if outcome == "original":
        return "original"
    if outcome in ("cancelled", "stopped"):
        handler.close_connection = True
        log_summary("client_disconnected")
        record_usage("client_disconnected")
        return "handled"
    handler._reauthentication_error(outcome)
    log_summary("reauthentication_required")
    record_usage("reauthentication_required")
    return "handled"


def _handle_preflight_failure(handler, exc, req_model, started_at, body,
                              log_failure):
    """Finish a failed chat preflight without retrying or opening a login."""
    handler.close_connection = True
    if isinstance(exc, ReauthenticationThrottled):
        log_failure("reauthentication_required", 503,
                    exc_name=type(exc).__name__)
        handler._reauthentication_error(
            getattr(exc, "reason", None) or str(exc))
        record_usage(req_model, True, started_at, None,
                      _message_tokens(body.get("messages")), 0,
                      status="reauthentication_required")
        return
    log_failure("client_disconnected", 0)
    record_usage(req_model, True, started_at, None,
                  _message_tokens(body.get("messages")), 0,
                  status="client_disconnected")


if __name__ == "__main__":
    main()
