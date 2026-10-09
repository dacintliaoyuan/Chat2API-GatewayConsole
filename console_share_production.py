# -*- coding: utf-8 -*-
"""console_share_production.py — production adapters for the share layered
orchestration seams (console_share_actions injection points).

Each adapter mirrors a REAL local product surface, honestly:

* ``production_broker_ops()`` — the restricted broker operations gate, the
  narrow wrapper the orchestration's ``_broker_ops()`` contract expects
  (console_share_actions.py :106-121). Surface mapping (ALL host evidence
  and the task gate execute INSIDE the SYSTEM broker — the in-process
  WindowsHostOps path was removed: the usagesrv operator account cannot
  resolve any schtasks evidence for the SYSTEM task):
  - ``start_host``  -> broker ``start`` verb (share_management.py :110 —
    SYSTEM start plus the broker-side three-evidence re-confirmation);
  - ``stop_host``   -> broker ``host-stop`` verb (the SYSTEM-side task gate);
  - ``verify_running``/``verify_closed`` -> broker ``host-verify`` verb,
    verdict True/False/None passed through verbatim (share_management.py
    :155 adapter; the SYSTEM-privileged WindowsHostOps runs inside the
    broker, share_broker_ops.py :91/:110);
  - ``drain``        -> BrokerClientAdapter.graceful_stop
    (share_management.py :66 class, :79 ``__init__(client)``; :123 graceful
    stop returns a dict that carries drained/timed_out/stopped plus the
    explicit final-stop confirmation — matches the orchestration drain
    protocol shape consumed at console_share_actions.py :519-531);
  - ``emergency_stop`` -> BrokerClientAdapter.emergency_stop
    (share_management.py :142 — the explicit ``stopped`` flag contract).
  The default broker construction reuses the production loader
  (``usagesrv._load_share_management_deployment``, usagesrv.py :949-1001)
  which builds the share-management adapter in its actual construction
  shape — ``BrokerClientAdapter(BrokerClient(lambda: NamedPipeBrokerTransport(
  pipe_name), secret))`` (usagesrv.py :1000-1001). Absent/disabled
  deployment config raises share_broker_unwired; a corrupt config keeps the
  loader's fail-closed BrokerError (never a fallback).
* ``production_funnel_control("on"/"off")`` — tailscale.exe argument-array
  calls ONLY (no shell, no interpolation, fixed argument shapes):
  - "off" strictly mirrors the watchdog Close-Public three steps
    (um-share-deploy/watchdog-umshare.ps1 :220-252): ``funnel --https=443
    off`` (:235) with the exit code checked (:237), the 2s settle (:238),
    then a ``serve status`` re-check (:239-245) where rc failure, EMPTY
    output ("cannot confirm mapping state", :243) or a still-present HTTPS
    mapping (:244-245) are each an honest failure;
  - "on" uses the SHARING.md open command (chat2api/SHARING.md :33:
    ``funnel --bg --https=443 http://127.0.0.1:8100``) and re-checks the
    mapping IS present via ``serve status``.
  Returns a ``{ok, detail}`` ack whose TRUTHINESS is the ``ok`` field — the
  orchestration consumes the ack through a plain truthy check
  (console_share_actions.py :377-381, :606-611, :714-722), so a failure ack
  must be falsy, never a fake success. tailscale.exe absent raises
  share_privilege_unwired (same convention as the connectivity seam).
* ``production_connectivity_ok()`` — read-only ``tailscale status --json``
  parsing (see the second entry below for the structured contract).
* ``production_ts_service_control("start"/"stop")`` — the TS privileged
  layer executed THROUGH the broker adapter's public ``ts_service_control``
  verb over the restricted ``ts-service-control`` whitelist extension (the
  wire action is pinned by BrokerClient.ts_service_control). Four honest
  shapes, each noting its deployment pointer:
  - broker unwired/unreachable (deployment config absent/disabled, pipe
    not_installed) -> OperationError(share_broker_unwired) - the broker
    task itself is not deployed (um-share-deploy/install-share-broker.ps1);
  - the broker REJECTS the unknown verb (bad_request) -> the broker task
    predates the extension -> OperationError(share_broker_extension_missing)
    (update/restart the broker task per the deployment package);
  - the broker executed the command but its immediate re-query did not yet
    confirm the target state (STOP_PENDING/START_PENDING) ->
    OperationError(share_ts_pending_confirmation) - the orchestration's
    bounded confirmation poll over the read-only sc observation decides;
  - execution rejected (any other broker error, missing explicit
    confirmation flag) -> OperationError(share_layer_failed).
  No net start / Stop-Service / local sc call, never a simulated ack.
* ``production_ts_service_status()`` — read-only ``sc.exe query Tailscale``
  argument-array parse (the same restricted primitive the broker runs,
  shared parser): RUNNING->"running", STOPPED->"stopped", exit 1060
  (service absent) ->"absent", failed/unparseable -> None (unavailable).
* ``production_funnel_observed()`` — read-only ``serve status`` mapping
  presence: mapping -> True; clean query without a mapping -> False;
  failed/unresolvable query -> None (unavailable).
* ``production_drain(...)`` — the drain over the production broker gate:
  the gate's ``drain`` (BrokerClientAdapter.graceful_stop) result must
  carry drained/timed_out/stopped (the orchestration drain protocol
  shape, console_share_actions.py :519-531); the adapter contract refuses
  any reply without the explicit stopped=True, so a returned result IS
  the broker's SYSTEM-verified backend closure (bounded in-flight wait,
  host stop and three-evidence check all ran inside the broker).
* ``production_desired_stop_reader()`` — read-only lifecycle view of the
  broker's protected record (broker-status.json "lifecycle",
  share_broker_server.py :495/:535-565): the record function returns
  {state, stopped_at} or None (absent/malformed file -> None, never a
  fabricated state); the SEAM reader maps it to the orchestration's
  bool|None contract (console_share_actions.py :225-232: state stopped ->
  True, running -> False, no record -> None).
* ``production_connectivity_ok()`` — real Tailscale CONNECTIVITY from
  ``tailscale status --json`` (structured fields, NOT serve status and NOT
  free-text IP matching): BackendState "Running" with a non-empty
  Self.TailscaleIPs list -> True (explicitly online); the known
  disconnected states (NeedsLogin/NoState/Stopped/RequiresAuth) or an
  empty/None Self.TailscaleIPs -> False (explicitly not connected); bad
  JSON, missing keys, type anomalies or an off-contract state raise
  OperationError(share_status_unknown) — an untrusted reading is never
  folded into a confirmed offline fact. Peer addresses and an offline
  self row keeping stale IPs can no longer read True (the old full-text
  IP regex path is deleted). tailscale.exe absent raises
  share_privilege_unwired (a wiring fact, not a reading).
* ``production_seams()`` — the seam dict for console_share_actions
  (key-by-key match with the module's injection seams, ALL NINETEEN
  delivered: the broker gate, TS service control/status, connectivity,
  funnel control/observation, bounded drain, the desired-stop reader, the
  dedicated backend, the key preconditions and the Rev9 seams).
* ``share_attribution_key_path()`` / ``share_attribution_status()`` — the
  ONE shared attribution key file BOTH ends must point at (C2 verified
  attribution). The path is a deployment fact, not a second state store:
  8100 reads the file once at startup (share_gateway.py :1247-1251), 8110
  re-stats it per request (chat2api.py :1587-1590); the dedicated-backend child env
  and ``_paths()["attribution_key"]`` carry the SAME resolved path. The
  status helper reports THREE DIFFERENT facts under three different names
  (2026-09-26 review: the single ``wired`` boolean proved only that THIS
  caller could read a long-enough file, and it read as "已接线"):
  ``file_ready`` (this process, now), ``instance_loaded`` (whether the
  RUNNING 8100 instance loaded the CURRENT content — an INFERENCE from the
  process start time vs the file mtime, never a proof, "unknown" when the
  process cannot be found) and ``request_attribution`` (the runtime fact this
  read surface does NOT observe: a pointer to the 8110 ledger). The secret
  itself never leaves the helper.

Privacy: no credential content passes through this module — the broker
control secret stays inside the share-management loader, tailscale
arguments are fixed shapes, and the file surfaces are the TWO fixed
deployment reads below: broker-status.json (paths/state only, no secrets)
and the shared attribution key (opened ONLY to read its presence/stripped
length — the secret is never returned, printed, logged or compared here).
The attribution-key path / run-root environment variables are read
(``share_attribution_key_path``) and ONE non-secret warning line is
printed when the key FILE is not ready (path + reason code only). The
8100 running-instance probe is read-only: the port table and one process
start time — never any file content, and never a write/signal/restart.
"""

import csv
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from functools import partial
from pathlib import Path

from console_operations import OperationError
from share_broker_ops import TailscaleServiceOps
from share_management import BrokerError

# ---- fixed production defaults (deployment facts; injectable for tests) ----
DEFAULT_TAILSCALE_EXE = r"C:\Program Files\Tailscale\tailscale.exe"  # watchdog :36
DEFAULT_SHARE_TASK = "umshare-public-live"        # install-share-broker.ps1 :48
DEFAULT_RUNNER_TIMEOUT_S = 10.0
FUNNEL_OFF_SETTLE_S = 2.0                  # watchdog Close-Public settle (:238)
_FUNNEL_UPSTREAM = "http://127.0.0.1:8100"  # chat2api/SHARING.md :33 target
DETAIL_MAX_CHARS = 200
# The broker's protected status record (watchdog-umshare.ps1 :54 resolves the
# same default: <RunRoot>\broker\broker-status.json; install-share-broker.ps1
# :45 default RunRoot). The broker writes it atomically per cycle
# (share_broker_server.py :484-506) with the lifecycle record mirrored at
# :495. Deployment facts stay injectable — tests never read the real path.
DEFAULT_BROKER_STATUS_PATH = r"C:\ProgramData\AIGatewayPublicShareRun\broker\broker-status.json"

# Watchdog verification token (watchdog-umshare.ps1 :244 `$out -match
# 'https://[^\s]+'`): an HTTPS mapping token in the serve status output.
_HTTPS_MAPPING_RE = re.compile(r"https://\S+")

# `tailscale status --json` connectivity contract (authorized-environment
# calibration: the observed disconnected machine reports
# BackendState="NoState", Self.TailscaleIPs=None and NO Self Online field,
# so the parse keys ONLY on BackendState + Self.TailscaleIPs):
# * BackendState "Running" AND a non-empty Self.TailscaleIPs list -> True
#   (the self node holds a live tailnet address; peer IPs never decide);
# * one of the explicit disconnected states below (or an empty/None
#   TailscaleIPs under a known state) -> False — a Stopped node keeping
#   stale addresses is still offline;
# * bad JSON / missing keys / off-contract types / unknown states stay
#   share_status_unknown — never folded into a definite answer.
_TAILSCALE_OFFLINE_STATES = frozenset(("NeedsLogin", "NoState", "Stopped",
                                       "RequiresAuth"))

_CODE_UNWIRED = "share_privilege_unwired"
_CODE_UNWIRED_BROKER = "share_broker_unwired"
_CODE_EXTENSION_MISSING = "share_broker_extension_missing"
_CODE_LAYER_FAILED = "share_layer_failed"
_CODE_STATUS_UNKNOWN = "share_status_unknown"


def _default_runner(args, timeout):
    """Controlled argument-array runner, no shell (same shape as
    share_broker_ops.WindowsHostOps._default_runner :80-82)."""
    # Tailscale emits UTF-8 JSON. On Windows the locale may be CP936; using
    # the locale decoder can leave stdout empty after a reader-thread decode
    # error even when the CLI exits 0 and produced valid JSON.
    return subprocess.run(args, shell=False, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def _default_bytes_runner(args, timeout):
    """Controlled argument-array runner, no shell, BYTES stdout (same shape as
    _ProductionBackendOps._pid_alive :1071-1073 / _port_listening :1120-1122).

    Localized Windows output (netstat, PowerShell) is NOT valid UTF-8 on this
    machine: a utf-8 text decode raises UnicodeDecodeError in the reader thread
    and leaves ``stdout`` EMPTY (calibrated 2026-09-26), which would read as
    "the query succeeded and nobody listens" — a fabricated fact. Bytes plus
    ``errors="replace"`` keeps an unparsable row unparsable instead."""
    return subprocess.run(args, shell=False, capture_output=True,
                          timeout=timeout)


def _proc_text(proc):
    """stdout as text: bytes are decoded with errors="replace" (never a
    fabricated empty reading); an injected text runner is used as-is."""
    text = getattr(proc, "stdout", "") or ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    return text


def _merged_text(proc):
    """Mirror the watchdog query shape: stdout and stderr merged (the
    watchdog redirects 2>&1, :239), whitespace collapsed and trimmed
    (:242 ``($svc -join ' ').Trim()``)."""
    text = (getattr(proc, "stdout", "") or "") + " " \
        + (getattr(proc, "stderr", "") or "")
    return " ".join(text.split())


def _shorten(text):
    text = text or ""
    if len(text) <= DETAIL_MAX_CHARS:
        return text
    return text[:DETAIL_MAX_CHARS] + "..."


def _run_tailscale(args, exe_path, runner, timeout_s):
    """``runner([exe] + args, timeout)`` -> ``(rc, merged_output)``.

    tailscale.exe absent (FileNotFoundError) raises OperationError
    share_privilege_unwired — the tailscale privilege surface itself is
    missing, an honest refusal, never a fabricated state. Any other runner
    failure (OSError, timeout) returns ``(None, "")`` so each seam reports
    its own honest failure shape."""
    try:
        proc = (runner or _default_runner)([exe_path] + list(args), timeout_s)
    except FileNotFoundError:
        raise OperationError(
            _CODE_UNWIRED,
            "tailscale.exe 缺席（%s）：tailscale 特权面不可用，不冒充可用"
            % (exe_path,))
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return proc.returncode, _merged_text(proc)


def _serve_status(exe_path, runner, timeout_s):
    """The only observation command, fixed argument shape
    (``[exe, "serve", "status"]``, watchdog-umshare.ps1 :239)."""
    return _run_tailscale(["serve", "status"], exe_path, runner, timeout_s)


class _FunnelAck(dict):
    """``{ok, detail}`` seam ack whose TRUTHINESS is the ``ok`` field.

    The orchestration seams consume these acks through a plain truthy check
    (console_share_actions.py :378, :607, :715 for the funnel; the TS
    service seam follows the same contract); a non-empty failure dict would
    read truthy and fake a success, so ``__bool__`` is bound to the ok
    field — the documented {ok, detail} shape stays intact (the dict
    remains serializable) while the truthy contract stays honest."""

    def __bool__(self):
        return self.get("ok") is True


def _funnel_off(exe_path, runner, sleeper, timeout_s):
    """Strict watchdog Close-Public mirror (watchdog-umshare.ps1 :234-251):
    funnel off with rc check (:235-237) -> 2s settle (:238) -> serve status
    re-check (:239-245); rc failure, empty output and a still-present HTTPS
    mapping are each an honest failure."""
    rc, _out = _run_tailscale(["funnel", "--https=443", "off"], exe_path,
                              runner, timeout_s)
    if rc is None:
        return _FunnelAck(
            ok=False,
            detail="tailscale 调用失败（funnel off 未执行/超时），映射状态未知")
    if rc != 0:
        return _FunnelAck(ok=False,
                          detail="funnel off 命令失败 rc=%s，不冒充已撤" % (rc,))
    sleeper(FUNNEL_OFF_SETTLE_S)
    rc, out = _serve_status(exe_path, runner, timeout_s)
    if rc is None:
        return _FunnelAck(ok=False,
                          detail="serve status 查询失败，无法确认映射已移除")
    if rc != 0:
        return _FunnelAck(ok=False,
                          detail="serve status 复核失败 rc=%s，不冒充已撤" % (rc,))
    if not out:
        return _FunnelAck(ok=False,
                          detail="serve status 输出为空，无法确认映射状态"
                                 "（镜像 watchdog :243，不算撤回成功）")
    if _HTTPS_MAPPING_RE.search(out):
        return _FunnelAck(ok=False,
                          detail="复核仍见 HTTPS 映射，入口可能仍开放："
                                 + _shorten(out))
    return _FunnelAck(ok=True,
                      detail="funnel off 已执行且 serve status 复核确认映射已移除")


def _funnel_on(exe_path, runner, timeout_s):
    """SHARING.md open command + presence re-check: run
    ``funnel --bg --https=443 http://127.0.0.1:8100`` (chat2api/SHARING.md
    :33), check the exit code, then ``serve status`` must SHOW an HTTPS
    mapping — an empty or unconfirmable query is never counted as open."""
    rc, _out = _run_tailscale(
        ["funnel", "--bg", "--https=443", _FUNNEL_UPSTREAM], exe_path,
        runner, timeout_s)
    if rc is None:
        return _FunnelAck(ok=False,
                          detail="tailscale 调用失败（funnel 开启未执行/超时）")
    if rc != 0:
        return _FunnelAck(ok=False,
                          detail="funnel 开启命令失败 rc=%s，不冒充已开放" % (rc,))
    rc, out = _serve_status(exe_path, runner, timeout_s)
    if rc is None or rc != 0:
        return _FunnelAck(ok=False,
                          detail="serve status 复核失败（rc=%s），无法确认映射在场"
                                 % (rc,))
    if not out:
        return _FunnelAck(ok=False, detail="serve status 输出为空，无法确认映射在场")
    if not _HTTPS_MAPPING_RE.search(out):
        return _FunnelAck(ok=False, detail="复核未见 HTTPS 映射，入口未确认开放")
    return _FunnelAck(ok=True,
                      detail="funnel 开启已执行且 serve status 复核确认映射在场")


def production_funnel_control(action, *, exe_path=DEFAULT_TAILSCALE_EXE,
                              runner=None, sleeper=time.sleep,
                              timeout_s=DEFAULT_RUNNER_TIMEOUT_S):
    """Privileged Funnel seam ("on"/"off") over tailscale.exe ONLY.

    Fixed argument shapes (no shell, no caller-supplied strings beyond the
    two actions). Returns a ``{ok, detail}`` ack (falsy when not confirmed);
    tailscale.exe absence raises share_privilege_unwired; an unknown action
    is refused without running anything.
    """
    if action == "off":
        return _funnel_off(exe_path, runner, sleeper, timeout_s)
    if action == "on":
        return _funnel_on(exe_path, runner, timeout_s)
    raise OperationError(
        "invalid_params",
        "funnel 控制缝仅接受 on/off（收到 %r）；不执行未知动作" % (action,))


def production_funnel_observed(*, exe_path=DEFAULT_TAILSCALE_EXE, runner=None,
                               timeout_s=DEFAULT_RUNNER_TIMEOUT_S):
    """Read-only Funnel observation (console_share_actions._funnel_observed
    contract: True(on)/False(off), None = unavailable).

    The SAME ``serve status`` mapping parse as the funnel control re-checks:
    a mapping token in the output -> True (on); a SUCCEEDED query without a
    mapping (empty status) -> False (off); a failed/unresolvable query ->
    None (unavailable, never a fabricated off); tailscale.exe absent ->
    OperationError(share_privilege_unwired) — the privilege surface itself
    is missing, a wiring fact rather than an observation.
    """
    rc, out = _serve_status(exe_path, runner, timeout_s)
    if rc is None or rc != 0:
        return None
    if not out:
        return False
    return _HTTPS_MAPPING_RE.search(out) is not None


def production_connectivity_ok(*, exe_path=DEFAULT_TAILSCALE_EXE, runner=None,
                               timeout_s=DEFAULT_RUNNER_TIMEOUT_S):
    """Read-only Tailscale CONNECTIVITY from ``tailscale status --json``
    (NOT serve status; console_share_actions._connectivity_ok contract:
    True/False, honest).

    Structured contract over the CLI's own JSON fields (calibrated on the
    authorized environment: the observed disconnected machine reports
    BackendState="NoState" with Self.TailscaleIPs=None and no Self Online
    field, so the parse keys ONLY on BackendState + Self.TailscaleIPs):
    * BackendState "Running" AND a non-empty Self.TailscaleIPs list -> True
      (explicitly online; peer addresses never decide this);
    * BackendState in {NeedsLogin, NoState, Stopped, RequiresAuth}, or an
      empty/None Self.TailscaleIPs -> False (explicitly not connected — a
      Stopped node keeping stale addresses is still offline);
    * bad JSON / missing BackendState or Self / off-contract types or an
      unknown BackendState raise OperationError(share_status_unknown) —
      an untrusted reading is NEVER reported as a confirmed offline fact.
    The free-text self-node IP scan is deleted: peer IPs or an offline
    self row can no longer fabricate a True. The JSON body is read from
    stdout (the --json surface; stderr is never mixed in, mirroring the
    CLI contract that errors go to stderr); a query that yields no
    parseable JSON body (failure, timeout, empty, garbage) stays unknown
    with the rc in the detail. tailscale.exe absent ->
    OperationError(share_privilege_unwired) (wiring fact).

    Real ``tailscale status --json`` shapes stay authorized-environment
    calibrated; the fixtures in tests pin the parsed contract.
    """
    try:
        proc = (runner or _default_runner)(
            [exe_path, "status", "--json"], timeout_s)
    except FileNotFoundError:
        raise OperationError(
            _CODE_UNWIRED,
            "tailscale.exe 缺席（%s）：tailscale 特权面不可用，不冒充可用"
            % (exe_path,))
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is None:
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 调用失败/超时：连接状态未知，"
            "不冒充离线确定事实")
    body = (getattr(proc, "stdout", "") or "").strip()
    try:
        payload = json.loads(body)
    except ValueError:
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 输出不可解析（rc=%s，非 JSON）：连接"
            "状态未知，不冒充离线确定事实"
            % (getattr(proc, "returncode", None),))
    if not isinstance(payload, dict):
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 输出不是 JSON 对象：连接状态未知，"
            "不冒充离线确定事实")
    state = payload.get("BackendState")
    self_node = payload.get("Self")
    if not isinstance(state, str) or not isinstance(self_node, dict):
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 缺少 BackendState/Self 结构化键："
            "连接状态未知，不冒充离线确定事实")
    ips = self_node.get("TailscaleIPs")
    rc_ok = getattr(proc, "returncode", None) == 0
    ips_assigned = isinstance(ips, list) and bool(ips)

    # Verdict ORDER (planner L6-3 follow-up): a non-zero CLI exit can never
    # produce a True from stdout alone, and an unrecognized BackendState is
    # never folded into a confirmed offline fact — regardless of the IPs.
    if not rc_ok:
        # Error exit: only an EXPLICIT negative state is trusted; a Running
        # claim under an error exit is contradictory evidence (unknown).
        if state in _TAILSCALE_OFFLINE_STATES:
            return False
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 以错误码退出（rc=%s）且状态不可信"
            "（BackendState=%r）：连接状态未知，不冒充在线或离线确定事实"
            % (getattr(proc, "returncode", None), _shorten(str(state))))
    if state == "Running":
        if ips_assigned:
            return True
        if ips is None or (isinstance(ips, list) and not ips):
            # Backend up but no tailnet address assigned: explicitly not
            # connected yet (a known state, not an unknown reading).
            return False
        raise OperationError(
            _CODE_STATUS_UNKNOWN,
            "tailscale status --json 的 Self.TailscaleIPs 形状超出契约"
            "（%s）：连接状态未知，不冒充离线确定事实"
            % _shorten(str(ips)))
    if state in _TAILSCALE_OFFLINE_STATES:
        return False
    # Unknown BackendState: unknown, regardless of any addresses present
    # (an unrecognized state can never be an online/offline proof).
    raise OperationError(
        _CODE_STATUS_UNKNOWN,
        "tailscale status --json 形状超出契约（BackendState=%r，"
        "Self.TailscaleIPs=%s）：连接状态未知，不冒充离线确定事实"
        % (state, _shorten(str(ips))))


def production_connectivity_state(*, exe_path=DEFAULT_TAILSCALE_EXE,
                                  runner=None,
                                  timeout_s=DEFAULT_RUNNER_TIMEOUT_S):
    """Read-only Tailscale BackendState channel for the connectivity wait
    (R13-T). The SAME ``tailscale status --json`` surface as the verdict
    seam (``production_connectivity_ok`` stays the ONLY verdict authority);
    this channel carries the CLI's own structured BackendState so the
    orchestration can stop EXPLICITLY on a 需人工登录 reading instead of
    burning the whole wait budget on a generic "not ready" poll.

    Trust rule mirrors the verdict contract's verdict ORDER:

    * rc==0 with a KNOWN BackendState ("Running" or an explicit offline
      fact from _TAILSCALE_OFFLINE_STATES) -> the state verbatim;
    * an error exit trusts ONLY an explicit offline fact (BackendState in
      _TAILSCALE_OFFLINE_STATES — NeedsLogin stays trusted under rc≠0,
      exactly like the verdict seam); a Running claim under an error exit
      is contradictory evidence -> None;
    * an unrecognized BackendState (e.g. the daemon's transitional
      "Starting") reads None — an unknown state can never become ANY
      confirmed fact through this channel (the verdict seam raises
      share_status_unknown for it; here it is simply unreadable);
    * ANY unreadable reading (absent exe, launch failure, timeout, bad or
      non-object JSON, missing/non-string BackendState) -> None — the
      channel NEVER fabricates a state; the orchestration keeps polling
      and the verdict seam decides.

    Self/peers/IPs are deliberately NOT read here: this channel answers
    "which backend state is the node in", never "is it connected".
    """
    try:
        proc = (runner or _default_runner)(
            [exe_path, "status", "--json"], timeout_s)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    body = (getattr(proc, "stdout", "") or "").strip()
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("BackendState")
    if not isinstance(state, str):
        return None
    if state != "Running" and state not in _TAILSCALE_OFFLINE_STATES:
        return None
    if getattr(proc, "returncode", None) != 0 \
            and state not in _TAILSCALE_OFFLINE_STATES:
        return None
    return state


def production_ts_service_status(*, runner=None,
                                 timeout_s=DEFAULT_RUNNER_TIMEOUT_S):
    """Read-only TS service observation (console_share_actions
    ._ts_service_status contract: "running"/"stopped", None = unavailable).

    ``sc.exe query Tailscale`` argument array ONLY (the fixed restricted
    primitive, shared parser with the broker's TailscaleServiceOps): no
    privilege is needed for the query. "absent" (exit 1060, service not
    installed) and any failed/unparseable query read None (unavailable).
    """
    ops = TailscaleServiceOps(runner=runner, timeout=timeout_s)
    return ops.status()


def production_ts_service_control(action, *, broker=None):
    """TS service seam ("start"/"stop") executed THROUGH the real broker.

    The privileged action runs on the SYSTEM broker via the restricted
    ``ts-service-control`` management verb (protocol whitelist extension;
    the service name is the protocol literal, never a wire parameter). The
    honest fail-closed shapes, each fail-closed with its deployment pointer
    in the message:

    * broker unwired/unreachable — deployment config absent/disabled or the
      pipe answers not_installed -> share_broker_unwired (the broker task
      is not deployed: um-share-deploy/install-share-broker.ps1);
    * the broker rejects the UNKNOWN VERB (bad_request) — the broker task
      predates the extension -> share_broker_extension_missing (update and
      restart the broker task per the deployment authorization package);
    * the OS refused the control command (rc 5 ERROR_ACCESS_DENIED) ->
      share_ts_access_denied (R13-T: a PERMANENT 权限不足 refusal — the
      command never executed, never the pending shape, never a fake
      success);
    * execution failed — any other broker rejection, or an ok reply without
      the explicit confirmation flag -> share_layer_failed.

    Returns a truthy ``{ok, detail}`` ack ONLY for a confirmed start/stop;
    an invalid action is refused before anything is sent.
    """
    if action not in ("start", "stop"):
        raise OperationError(
            "invalid_params",
            "TS 服务控制缝仅接受 start/stop（收到 %r）；不执行未知动作"
            % (action,))
    if broker is None:
        broker = _default_broker()
    control = getattr(broker, "ts_service_control", None)
    if control is None:
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "TS 服务控制：broker 适配器无 ts_service_control 控制方法"
            "（部署配置与适配器形状不符）；部署指针："
            "um-share-deploy/install-share-broker.ps1；不冒充可用")
    try:
        result = control(action)
    except Exception as exc:  # noqa: BLE001 - transport failures stay honest
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "TS 服务控制：broker 管道不可达（%s: %s）：broker 任务未部署或"
            "未运行；部署指针：um-share-deploy/install-share-broker.ps1；"
            "不冒充可用" % (type(exc).__name__, exc)) from exc
    code = result.error_code
    if result.ok:
        flag = ("ts_service_started" if action == "start"
                else "ts_service_stopped")
        if result.result.get(flag) is True:
            return _FunnelAck(
                ok=True,
                detail="TS 服务 %s 已由 broker 受限原语执行并复核确认"
                       % ("启动" if action == "start" else "停止"))
        raise OperationError(
            _CODE_LAYER_FAILED,
            "TS 服务控制执行失败：broker 结果未显式确认（result=%r），"
            "不冒充成功" % (result.result,))
    if code == "not_installed":
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "TS 服务控制：broker 管道不可达/未部署（error_code=%s）："
            "部署指针 um-share-deploy/install-share-broker.ps1"
            "（需独立部署）；不冒充可用" % (code,))
    elif code == "bad_request":
        raise OperationError(
            _CODE_EXTENSION_MISSING,
            "TS 服务控制：broker 拒绝未知动词 ts-service-control = broker "
            "白名单扩展未部署（broker 任务代码早于扩展）；部署指针：按部署"
            "授权包节 5 更新并重启 broker 任务后开放；不冒充可用")
    elif code == "access_denied":
        # R13-T: the OS refused the control command itself (sc.exe rc 5,
        # ERROR_ACCESS_DENIED) - the command NEVER executed, so this is a
        # PERMANENT 权限不足 report with its own frozen code, never the
        # pending-confirmation shape (a bounded poll could only burn its
        # budget on a misleading "已执行" story) and never a fake success.
        raise OperationError(
            "share_ts_access_denied",
            "TS 服务控制被系统拒绝（权限不足，%s 命令未执行，%s）；"
            "需要承载 broker 任务的账户具备服务控制权限后重试；"
            "实际服务状态未改变，不冒充成功"
            % (action,
               _shorten(result.result.get("detail") or "")))
    elif code == "unconfirmed" and \
            "did not re-confirm" in (result.result.get("detail") or ""):
        # The broker EXECUTED the control command but its single immediate
        # re-query did not yet see the target state (sc.exe stop/start is
        # asynchronous: the service sits in STOP_PENDING/START_PENDING for
        # seconds). The command is in flight - the orchestration layer's
        # bounded confirmation poll over the read-only sc observation
        # decides; this is a pending-confirmation shape, not a failure.
        raise OperationError(
            "share_ts_pending_confirmation",
            "TS 服务控制命令已执行但立即复查未确认目标态（服务可能处于 "
            "STOP_PENDING/START_PENDING 过渡态）：交由编排层有界确认轮询"
            "裁定（只读服务状态观察）")
    else:
        raise OperationError(
            _CODE_LAYER_FAILED,
            "TS 服务控制执行失败：broker 拒绝（error_code=%s, detail=%s）；"
            "实际服务状态未知，不冒充成功"
            % (code, _shorten(result.result.get("detail") or "")))


def production_desired_stop_record(*, status_path=DEFAULT_BROKER_STATUS_PATH):
    """Read the broker's protected lifecycle record from broker-status.json
    (share_broker_server.py :495 mirrors it per cycle; :535-551 defines the
    {state: running|stopped, at, by} shape and :553-565 persists it).

    Returns ``{"state": ..., "stopped_at": ...}`` — state "stopped" carries
    the stopped epoch in ``stopped_at`` — or None when the file is absent,
    unreadable, malformed or the record shape is unknown: a missing record
    NEVER becomes a fabricated stop/open state (the orchestration-side view
    then honestly reports no authority).
    """
    try:
        payload = json.loads(Path(status_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lifecycle = payload.get("lifecycle") if isinstance(payload, dict) \
        else None
    if not isinstance(lifecycle, dict):
        return None
    state = lifecycle.get("state")
    if state not in ("running", "stopped"):
        return None
    at = lifecycle.get("at")
    if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
        at = None
    # stopped_at only means something for a stopped record; a running
    # record reports no stopped epoch.
    return {"state": state,
            "stopped_at": at if state == "stopped" else None}


def production_desired_stop_reader(*, status_path=DEFAULT_BROKER_STATUS_PATH):
    """The orchestration _DESIRED_STOP_READER seam over the broker's
    protected record (console_share_actions.py :225-232 contract: bool|None).

    Mapping of the record view: state "stopped" (a verified drain/
    emergency-stop confirmed by the broker) -> True (a stop intent is in
    force); state "running" (a verified start superseded it) -> False; an
    absent/unreadable/malformed record -> None (no authority here — the
    honest default). The record itself stays readable via
    production_desired_stop_record."""
    record = production_desired_stop_record(status_path=status_path)
    if record is None:
        return None
    return record["state"] == "stopped"


def _drain_with(ops, budget_s, interval_s, ctx, sleeper=time.sleep):
    """Production drain over the production gate: the SAME protocol as the
    orchestration's default drain (console_share_actions.py :519-531).

    The broker-side drain already performs the bounded in-flight wait, the
    SYSTEM-privileged host stop and the three-evidence closure check, and
    the adapter contract (graceful_stop) refuses any reply without the
    explicit ``stopped=True`` — a returned result IS the SYSTEM-verified
    backend closure. (The previous in-process verify_running observation
    was removed: the usagesrv operator account cannot resolve schtasks
    evidence for the SYSTEM task, so that poll could never terminate on
    real evidence. budget_s/interval_s stay in the seam signature — they
    are the orchestration's drain budget contract, consumed by the
    broker-side deadline and injected alternatives.)"""
    try:
        raw = ops.drain()
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            _CODE_LAYER_FAILED,
            "drain 层：broker 排空调用异常（%s: %s）"
            % (type(exc).__name__, exc))
    if isinstance(raw, dict):
        if not (raw.get("drained") is True or raw.get("timed_out") is True
                or raw.get("stopped") is True):
            raise OperationError(
                _CODE_LAYER_FAILED,
                "drain 层：broker 排空结果未确认 drained/timed_out/stopped"
                "（结果=%r）" % (raw,))
        return {"drained": True, "broker": raw,
                "observed": "broker SYSTEM 端已停后端并三证确认关闭"}
    raise OperationError(
        _CODE_LAYER_FAILED,
        "drain 层：broker 排空结果形状不符合协议（非结构化结果=%r），"
        "不继续关闭" % (raw,))


def production_drain(budget_s, interval_s, ctx, *, broker=None,
                     sleeper=time.sleep):
    """Public production drain seam: build the REAL restricted broker gate
    (or accept an injected one) and run the bounded drain protocol over it.
    Wired into the orchestration via production_seams (the SAME gate
    instance backs _BROKER_OPS and _DRAIN there)."""
    ops = production_broker_ops(broker=broker)
    return _drain_with(ops, budget_s, interval_s, ctx, sleeper=sleeper)


def _default_broker():
    """The REAL share-management deployment adapter, constructed in its
    actual production shape by the loader (usagesrv.py :949-1001:
    ``BrokerClientAdapter(BrokerClient(lambda: NamedPipeBrokerTransport(
    pipe_name), secret))``). Absent/disabled deployment config raises
    share_broker_unwired honestly with the deployment pointer; a corrupt
    config keeps the loader's fail-closed BrokerError (never a fallback)."""
    import usagesrv  # deferred: the production console process already
    # holds this module; tests inject fakes and never import it here.
    broker = usagesrv._load_share_management_deployment()
    if broker is None:
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "share 管理部署配置缺席/未启用（share-management.json）："
            "broker 适配器不可用；部署指针：um-share-deploy/"
            "install-share-broker.ps1（需独立部署）；不冒充可用")
    return broker


class _ProductionBrokerOps:
    """Narrow restricted-gate wrapper over the deployment-authorized parts.

    Seam surface FIXED to the orchestration's restricted primitives
    (console_share_actions.py :106-121) — nothing else is exposed:

    * ``start_host``/``stop_host``  -> broker ``start``/``host-stop`` verbs
      (share_management.py adapter: start re-confirms a running host via
      the broker-side three-evidence verify; host-stop is the SYSTEM-side
      task gate);
    * ``verify_running``/``verify_closed`` -> broker ``host-verify`` verb:
      the SYSTEM-privileged WindowsHostOps executes INSIDE the broker and
      the verdict passes through verbatim (True/False/None). The previous
      in-process WindowsHostOps path was removed deliberately: usagesrv
      runs as the ordinary operator account, where every schtasks query
      against the SYSTEM task is ACCESS DENIED - an evidence source that
      is unresolvable 100% of the time is not an evidence source;
    * ``drain`` -> BrokerClientAdapter.graceful_stop
      (share_management.py :66 class, :79 ``__init__(client)``; :123 method:
      drained/timed_out plus the explicit final-stop confirmation — the
      orchestration drain protocol shape at console_share_actions.py :519);
    * ``emergency_stop`` -> BrokerClientAdapter.emergency_stop
      (share_management.py :142: the explicit ``stopped`` flag contract).
    """

    def __init__(self, broker):
        self._broker = broker

    def start_host(self):
        try:
            self._broker.start()
            return True
        except BrokerError:
            return False

    def stop_host(self):
        try:
            return self._broker.host_stop()
        except BrokerError:
            return False

    def verify_running(self):
        try:
            return self._broker.host_verify("running")
        except BrokerError:
            return None

    def verify_closed(self):
        try:
            return self._broker.host_verify("closed")
        except BrokerError:
            return None

    def drain(self):
        return self._broker.graceful_stop()

    def emergency_stop(self):
        return self._broker.emergency_stop()

    def host_evidence(self, check):
        """Restricted READ-ONLY host evidence (limits/faults), A9-1.

        The broker's SYSTEM-privilege verdict passes through VERBATIM: the
        frozen reply dict {check, verdict, detail, checked_at} on success —
        returned as a _HostEvidence whose TRUTHINESS is ``verdict is True``,
        so a FAILED limits verdict can never read as a pass — or None when
        the broker is unreachable/not deployed/rejects the verb (an old
        broker: bad_request): honest unconfirmed, never a fabricated verdict.
        The concrete object (watchdog script, run root, firewall rule, fault
        file) stays broker-side deployment configuration; the wire carries
        the fixed check enum only."""
        try:
            result = self._broker.host_evidence(check)
        except Exception:  # noqa: BLE001 - transport failures stay honest
            return None
        if not result.ok:
            return None
        payload = result.result
        if not isinstance(payload, dict) or "verdict" not in payload:
            return None
        return _HostEvidence(payload)


def production_broker_ops(broker=None):
    """Assemble the restricted broker operations gate.

    ``broker`` injectable exists for isolated tests and for the deployment
    authorization package; the default builds the REAL share-management
    deployment adapter via _default_broker (the production loader shape,
    usagesrv.py :949-1001). ALL host evidence and the task gate execute
    INSIDE the SYSTEM broker (host-verify / host-stop / start verbs) — the
    in-process WindowsHostOps evidence path was removed: the usagesrv
    operator account cannot resolve any schtasks evidence for the SYSTEM
    task, so it never constituted evidence.
    """
    if broker is None:
        broker = _default_broker()
    return _ProductionBrokerOps(broker)


# ---------------------------------------------------------------------------
# Dedicated share backend (Revision 6 R6-A1 / R8-A7)
# ---------------------------------------------------------------------------
# The dedicated backend is a SECOND chat2api-class instance with its own
# port / token / profile / control secret / log / ledger — the share gateway
# forwards THERE, never to the production chat2api process (whose 64 reader
# slots and listening socket must stay untouched). Identity discipline: the
# managed PID is re-verified against its exact command line (chat2api.py +
# --port <port>) before ANY stop action, so a console stop can never kill
# the production instance by pattern.
DEFAULT_SHARE_BACKEND_PORT = 8110
DEFAULT_UM_BASE_URL = "http://localhost:3000"


def _backend_dir():
    override = os.environ.get("CONSOLE_SHARE_BACKEND_DIR")
    if override:
        if not os.path.isabs(override):
            raise ValueError("CONSOLE_SHARE_BACKEND_DIR must be absolute")
        return override
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ValueError(
            "CONSOLE_SHARE_BACKEND_DIR unresolvable: no LOCALAPPDATA "
            "(无回退；显式配置或设置环境变量)")
    return os.path.join(local, "AIGatewayPublic", "share-backend")


def _backend_port():
    raw = os.environ.get("CONSOLE_SHARE_BACKEND_PORT")
    if not raw:
        return DEFAULT_SHARE_BACKEND_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("CONSOLE_SHARE_BACKEND_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("CONSOLE_SHARE_BACKEND_PORT out of range")
    return port


def _backend_base_url():
    return os.environ.get("CONSOLE_SHARE_BACKEND_BASE_URL") or os.environ.get("AI_GATEWAY_OPENWEBUI_URL") or \
        DEFAULT_UM_BASE_URL


# ---- shared attribution key (C2 verified attribution) ----------------------
# BOTH ends must point at ONE key file, and this module is the single place
# the 8110 side resolves that path:
#   * 8100 (share gateway) reads the file ONCE at startup through the
#     SHARE_ATTRIBUTION_KEY_FILE env var (share_gateway.py :115), which the
#     deployed run wrapper sets (deploy-elevated.ps1 Start stage, $liveEnv);
#   * 8110 (dedicated backend) re-stats it PER REQUEST through
#     CHAT2API_SHARE_ATTRIBUTION_KEY_FILE (chat2api.py :1525-1528),
#     injected by _child_env below.
# The file lives in the DEPLOYED RunRoot run/ dir: ai-gateway/data carries an
# explicit DENY ACE for um_share (a key there is unreadable by 8100 and silently
# disables attribution), and run/live is removed and recreated by every deploy
# (deploy-elevated.ps1 :820-822). A missing, unreadable or too-short file is
# NOT an error: it is the explicit "no attribution" state (a request 8110
# cannot verify is recorded unattributed), which is why
# share_attribution_status() reports it loudly instead of letting it hide.
# Because 8100 reads the file only ONCE at startup, a create/rotate after the
# gateway started is NOT visible in a running instance until it is restarted:
# that is a DIFFERENT fact from file readiness and is reported separately
# (``instance_loaded``, inferred from the gateway process start time vs this
# file's mtime — see share_gateway_instance_probe). Generation/rotation
# procedure: ai-gateway/SHARING.md.
DEFAULT_SHARE_RUN_ROOT = r"C:\ProgramData\AIGatewayPublicShareRun"  # deploy -RunRoot default
SHARE_RUN_ROOT_ENV = "CONSOLE_SHARE_RUN_ROOT"
SHARE_ATTRIBUTION_KEY_NAME = "share-attribution.key"
SHARE_ATTRIBUTION_KEY_FILE_ENV = "CONSOLE_SHARE_ATTRIBUTION_KEY_FILE"
SHARE_ATTRIBUTION_KEY_MIN_BYTES = 32   # = share_gateway/chat2api constant
BACKEND_ATTRIBUTION_KEY_ENV = "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE"


def _share_run_root():
    """Deployed RunRoot (deploy-elevated.ps1 -RunRoot default): the run/ dir
    below it is where the deployed share resolves its state, so the attribution
    key belongs there. An explicit override must be absolute — a relative value
    would silently resolve somewhere else and break the one-file invariant."""
    override = os.environ.get(SHARE_RUN_ROOT_ENV)
    if override:
        if not os.path.isabs(override):
            raise ValueError("%s must be absolute" % SHARE_RUN_ROOT_ENV)
        return override
    return DEFAULT_SHARE_RUN_ROOT


def share_attribution_key_path():
    """Resolve the ONE key file both ends must point at.

    Default: <deployed RunRoot>/run/share-attribution.key — the run/ dir the
    deploy prepares (deploy-elevated.ps1 :461-464) and the SAME path the 8100
    wrapper gets through SHARE_ATTRIBUTION_KEY_FILE. The override
    (CONSOLE_SHARE_ATTRIBUTION_KEY_FILE) exists for tests and for a deployment
    that overrides the 8100 side identically: overriding only this side makes
    the two ends disagree, which the status read surface then reports as a wiring
    fact instead of hiding."""
    override = os.environ.get(SHARE_ATTRIBUTION_KEY_FILE_ENV)
    if override:
        if not os.path.isabs(override):
            raise ValueError(
                "%s must be absolute" % SHARE_ATTRIBUTION_KEY_FILE_ENV)
        return override
    return os.path.join(_share_run_root(), "run", SHARE_ATTRIBUTION_KEY_NAME)


def _attribution_file_fact(target):
    """FACT 1 (file readiness), the ONLY place it is decided.

    ``(file_ready, reason, key_chars, key_file_mtime)`` for the key file at
    ``target``, read by THIS process right now:

    * ``(True, "ok", <stripped length>, mtime)`` — the file is readable and
      its stripped content is >= SHARE_ATTRIBUTION_KEY_MIN_BYTES characters;
    * ``(False, "missing", None, None)`` — no file;
    * ``(False, "unreadable", None, None)`` — present but unreadable (ACL /
      lock / encoding): NEVER folded into "missing" (unknown is not absent);
    * ``(False, "too_short", <stripped length>, mtime)`` — below the minimum.

    ``key_chars`` is the stripped length ONLY — never any content. The secret
    never leaves this function: it is not returned, printed, logged or compared.
    ``file_ready`` is a statement about THIS caller identity at THIS moment; it
    does NOT mean the running 8100 instance loaded this content, that the
    um_share identity can read the file, or that 8110 loaded it (each of those
    is a different fact — see share_attribution_status).
    """
    try:
        info = os.stat(target)
    except FileNotFoundError:
        return (False, "missing", None, None)
    except OSError:
        return (False, "unreadable", None, None)
    try:
        with open(target, encoding="utf-8") as handle:
            secret = handle.read().strip()
    except FileNotFoundError:
        return (False, "missing", None, None)
    except (OSError, UnicodeError, ValueError):
        return (False, "unreadable", None, None)
    if len(secret) < SHARE_ATTRIBUTION_KEY_MIN_BYTES:
        return (False, "too_short", len(secret), info.st_mtime)
    return (True, "ok", len(secret), info.st_mtime)


# ---- running 8100 instance probe (read-only) ---------------------------------
# 8100 (the share gateway) reads the key file ONCE at startup
# (share_gateway.py :1247-1251), so "the file is readable now" says NOTHING
# about the process already running: after a create/rotate it may still hold the
# previous content. The only way to tell the two apart without touching the
# process is its START TIME vs the key file mtime — an INFERENCE, not a proof
# (a content change that preserves mtime, or a clock jump, defeats it). That is
# why the read surface carries the rule and the raw evidence next to the verdict,
# and why a process that cannot be found is "unknown" — never "loaded".
SHARE_GATEWAY_PORT = 8100          # share_gateway.py --port default (:1193)
# .NET epoch ticks (1970-01-01T00:00:00Z). The conversion runs INSIDE
# PowerShell because Win32_Process CreationDate prints as a LOCALIZED string
# ("09/26/2026 09:34:53"), whose Python parse would be locale-dependent
# (calibrated 2026-09-26: PID 20740 -> 1790386493.70995 = 09:34:53 local).
_DOTNET_EPOCH_TICKS = 621355968000000000
_CIM_START_EPOCH_SCRIPT = (
    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d') | ForEach-Object "
    "{ [double]($_.CreationDate.ToUniversalTime().Ticks "
    "- %d) / 10000000.0 }")
_INSTANCE_LOADED_RULE = "gateway_process_start_time_vs_key_file_mtime"
# The 8100 share gateway runs as the dedicated share identity through the deployed
# wrapper, so its image is the um-share python: a listener with any other
# image name is NOT assumed to be the gateway (2026-09-26 review F-16).
SHARE_GATEWAY_IMAGE_NAMES = ("um-share-python.exe",)
_CIM_IMAGE_NAME_SCRIPT = (
    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d') | "
    "ForEach-Object { $_.Name }")
# The runtime fact that decides "request attribution verified" — NOT observed
# by this read surface (8110 re-stats the key PER REQUEST and writes it into
# each ledger row; chat2api.py :1670 returns the reason "verified").
_LEDGER_ATTRIBUTION_FACT = "backend_ledger_row_attribution_verified"


def _listening_pids(port, runner=None):
    """Read-only ``netstat -ano -p TCP`` parse: ``(pids, reason)``.

    ``(set(), None)`` — the query succeeded and nobody LISTENs on ``port``;
    ``(None, "query_failed")`` — the query itself failed, or a LISTENING row
    could not be parsed (unusable evidence, NEVER an empty set: unknown is not
    absent). Reads the port table only; never a process handle, signal or write.
    """
    run = runner or _default_bytes_runner
    try:
        proc = run(["netstat", "-ano", "-p", "TCP"], DEFAULT_RUNNER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return (None, "query_failed")
    if getattr(proc, "returncode", 1) != 0:
        return (None, "query_failed")
    pids = set()
    suffix = ":%d" % int(port)
    for line in _proc_text(proc).splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(suffix):
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            return (None, "query_failed")   # damaged row: evidence unusable
    return (pids, None)


def _process_start_epoch(pid, runner=None):
    """Read-only ``Get-CimInstance Win32_Process`` start time of ``pid`` as
    epoch seconds: ``(epoch, reason)``.

    ``(None, "start_time_unavailable")`` when the process row or its
    CreationDate cannot be read (absent / permission / unparsable output) — an
    unreadable start time is NEVER turned into a guessed one.
    """
    run = runner or _default_bytes_runner
    try:
        proc = run(["powershell", "-NoProfile", "-Command",
                    _CIM_START_EPOCH_SCRIPT % (int(pid),
                                               _DOTNET_EPOCH_TICKS)],
                   DEFAULT_RUNNER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return (None, "start_time_unavailable")
    if getattr(proc, "returncode", 1) != 0:
        return (None, "start_time_unavailable")
    text = _proc_text(proc).strip()
    try:
        epoch = float(text)
    except ValueError:
        return (None, "start_time_unavailable")
    if not math.isfinite(epoch) or epoch <= 0:
        # "nan"/"inf"/a non-positive value can be parsed but is not a start
        # time: unusable, never compared (a NaN comparison would silently
        # read as "started earlier than the file").
        return (None, "start_time_unavailable")
    return (epoch, "ok")


def share_gateway_instance_probe(port=None, runner=None):
    """Read-only view of the process serving the share-gateway port.

    Returns ``{"pid": int|None, "started_at": float|None, "reason": code}``:

    * ``"ok"`` — exactly one LISTENING process on the port and its start
      time was read;
    * ``"not_listening"`` — the query succeeded and nobody LISTENs there
      (the gateway is not running, or does not serve that port);
    * ``"ambiguous_listener"`` — more than one LISTENING process: no single
      instance to compare, so no inference is drawn;
    * ``"query_failed"`` / ``"start_time_unavailable"`` — the port table or
      the start time could not be read.

    Identity is the LISTENING socket on the port: the command line is NOT used
    as identity evidence, because this machine's CIM query returns an EMPTY
    CommandLine for the real gateway process (calibrated 2026-09-26), and an
    identity check that can only ever answer "unreadable" would turn every real
    deployment into "unknown". It never writes, signals or restarts anything.
    """
    target_port = SHARE_GATEWAY_PORT if port is None else int(port)
    pids, reason = _listening_pids(target_port, runner=runner)
    if pids is None:
        return {"pid": None, "started_at": None, "reason": reason}
    if not pids:
        return {"pid": None, "started_at": None, "reason": "not_listening"}
    if len(pids) > 1:
        return {"pid": None, "started_at": None,
                "reason": "ambiguous_listener"}
    pid = sorted(pids)[0]
    image, image_reason = _process_image_name(pid, runner=runner)
    if image_reason != "ok":
        return {"pid": pid, "started_at": None, "reason": image_reason}
    if image.lower() not in SHARE_GATEWAY_IMAGE_NAMES:
        return {"pid": pid, "started_at": None,
                "reason": "listener_identity_unmatched"}
    started_at, reason = _process_start_epoch(pid, runner=runner)
    if started_at is None:
        return {"pid": pid, "started_at": None, "reason": reason}
    return {"pid": pid, "started_at": started_at, "reason": "ok"}


def _process_image_name(pid, runner=None):
    """``(name, reason)`` of the listener's image — identity EVIDENCE, not proof.

    Read-only; ``runner`` is injectable so tests never spawn a real query.
    """
    run = runner or _default_bytes_runner
    try:
        proc = run(["powershell", "-NoProfile", "-Command",
                    _CIM_IMAGE_NAME_SCRIPT % int(pid)],
                   DEFAULT_RUNNER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None, "image_name_unavailable"
    if getattr(proc, "returncode", 1) != 0:
        return None, "image_name_unavailable"
    name = _proc_text(proc).strip()
    if not name:
        return None, "image_name_unavailable"
    return name, "ok"


def _infer_instance_loaded(file_ready, key_mtime, probe):
    """FACT 2: ``(value, reason)`` — INFERENCE, not a proof, of whether the
    RUNNING gateway instance (8100) has loaded the CURRENT key file content.

    8100 reads the file ONCE at startup, so "process start time is not earlier
    than the file mtime" means it read THIS content; "started earlier than the
    mtime" means it holds the content from its own start (the file was created
    or rotated afterwards). An equal mtime counts as "not earlier".

    ``value`` is "yes" / "no" / "unknown": ``"unknown"`` whenever the file is
    not readable NOW (no mtime to compare, so no probe is run), the process
    cannot be found, the probe answered nothing usable, or its start time cannot
    be read — never a default "yes". ``reason`` names the precise code behind
    the value.
    """
    if file_ready is not True or key_mtime is None:
        return ("unknown", "key_file_not_ready")
    if not isinstance(probe, dict):
        return ("unknown", "query_failed")
    reason = probe.get("reason")
    started_at = probe.get("started_at")
    if reason != "ok":
        return ("unknown", reason if isinstance(reason, str) and reason
                else "query_failed")
    if not isinstance(started_at, (int, float)) or not math.isfinite(started_at):
        # A non-finite value (nan/inf) is not a start time: with the docstring
        # contract it must read "unknown", never fall through to the "no"
        # comparison (a NaN comparison would silently read as "started before").
        return ("unknown", "start_time_unavailable")
    if started_at >= key_mtime:
        return ("yes", "process_started_at_or_after_key_mtime")
    return ("no", "process_started_before_key_mtime")


def share_attribution_status(path=None, gateway_probe=None, ledger_path=None):
    """Read-only attribution FACTS; never guesses, never leaks content, and
    never lets ONE name carry THREE different facts (2026-09-26 review).

    Returns ``{"file_ready": bool, "reason": code, "key_file": <path>,
    "key_chars": int|None, "instance_loaded": {...},
    "request_attribution": {...}}``:

    1. FILE READY — ``file_ready`` / ``reason`` / ``key_file`` /
       ``key_chars``: THIS process can read the file now and its stripped
       content is long enough (the four states of
       :func:`_attribution_file_fact`). This is what the old ``wired``
       boolean proved, and nothing more: it does NOT mean the running 8100
       instance loaded this content (it reads the file once at startup), that
       the um_share identity can read the file (this process runs as the
       console user), or that 8110 loaded it (8110 re-stats it per request).
    2. INSTANCE LOADED — ``instance_loaded`` sub-dict: whether the RUNNING
       8100 instance is INFERRED to have loaded the CURRENT content, from
       its start time vs the file mtime (``value`` "yes" / "no" /
       "unknown" plus ``basis`` = "inference", ``rule``, ``reason`` and the
       raw evidence: gateway pid / start time / file mtime). It is an
       inference, not a proof, and an unfindable process is "unknown" —
       never "yes".
    3. REQUEST ATTRIBUTION — ``request_attribution`` sub-dict: NOT observed
       here at all (``verdict`` = "not_observed_here"); it only names the
       runtime fact that decides it, a row with ``attribution="verified"`` in
       the 8110 ledger (``ledger_file``).

    ``key_chars`` is the stripped length ONLY — never any content. The secret
    never leaves this function: it is not returned, printed, logged or compared.
    The gateway probe runs ONLY when the file is ready (there is no mtime to
    compare otherwise, and a pointless read-only query is not run).
    """
    target = path or share_attribution_key_path()
    file_ready, reason, key_chars, key_mtime = _attribution_file_fact(target)
    probe = None
    if file_ready is True:
        probe = (gateway_probe or share_gateway_instance_probe)()
    value, load_reason = _infer_instance_loaded(file_ready, key_mtime, probe)
    probe = probe if isinstance(probe, dict) else {}
    return {
        "file_ready": file_ready,
        "file_reason": reason,
        "key_file": target,
        "key_chars": key_chars,
        "instance_loaded": {
            "value": value,
            "basis": "inference",
            "rule": _INSTANCE_LOADED_RULE,
            "reason": load_reason,
            "gateway_pid": probe.get("pid"),
            "gateway_started_at": probe.get("started_at"),
            "key_file_mtime": key_mtime,
        },
        "request_attribution_observed": False,
        "request_attribution_detail": {
            "verdict": "not_observed_here",
            "runtime_fact": _LEDGER_ATTRIBUTION_FACT,
            "ledger_file": ledger_path,
        },
    }


class _ProductionBackendOps:
    """Start/stop/verify the dedicated share-backend process.

    The child runs the SAME chat2api.py source with isolated state paths
    (env redirects, R6-A1) — no source copy, no shared state file with the
    production instance. Every stop re-verifies the exact command line
    (chat2api.py + --port <port>) before acting, so a bare PID can never
    match the production instance; stop is graceful first (the backend's
    own /admin/shutdown with ITS control secret) and falls back to a
    verified-PID termination only when graceful fails. Returns are plain
    bools: True only on confirmed facts, False keeps the residual honest.

    Write surface (2026-09-28 review): ``start`` writes the pid file and
    ``stop``/``verify_closed`` may remove it — those are ACTION paths. The
    acceptance collectors must not inherit that: ``verify_running`` takes the
    additive ``forget_stale`` switch (``False`` = never remove) and
    ``backend_pid_state`` is a pure read-only report with no write on any
    branch.
    """

    def __init__(self, gateway_dir=None, port=None, base_url=None,
                 spawner=None, stopper=None):
        self._gateway_dir = gateway_dir   # chat2api source dir
        self._port = port
        self._base_url = base_url
        self._spawner = spawner           # test seam: (cmdline, env) -> proc
        self._stopper = stopper           # test seam: (pid) -> None|True

    def _paths(self):
        d = _backend_dir()
        port = self._port or _backend_port()
        return {
            "dir": d,
            "port": port,
            "token": os.path.join(d, "token.json"),
            "profile": os.path.join(d, "edge-profile"),
            "log": os.path.join(d, "share-backend.log"),
            "ledger": os.path.join(d, "usage-share-backend.jsonl"),
            "control_token": os.path.join(d, "control-token"),
            # The ONE key file BOTH ends point at (8100 reads it once at
            # startup; this instance re-stats it per request). Resolved by the
            # SAME function the status read surface uses, so the injected path
            # can never be a second literal.
            "attribution_key": share_attribution_key_path(),
            "pid": os.path.join(d, "backend.pid"),
        }

    def _child_cmdline(self, paths):
        # --login-timeout 600 (chat2api.py:6330 default 180): the dedicated
        # instance's SSO window stays open 10 minutes instead of 3 — the
        # first real attempt timed out exactly at 180s while the maintainer
        # was away (share-backend.log 19:14:22 RuntimeError). Production
        # chat2api (port 8000) is unaffected: this flag only reaches THIS
        # dedicated child's command line.
        return [sys.executable, "chat2api.py",
                "--base-url", self._base_url or _backend_base_url(),
                "--port", str(paths["port"]),
                "--profile", paths["profile"],
                "--login-timeout", str(_BACKEND_LOGIN_TIMEOUT_S)]

    def _child_env(self, paths):
        env = dict(os.environ)
        env["CHAT2API_TOKEN_FILE"] = paths["token"]
        env["CHAT2API_SERVICE_LOG"] = paths["log"]
        env["CHAT2API_USAGE_LOG"] = paths["ledger"]
        env["CHAT2API_CONTROL_TOKEN_FILE"] = paths["control_token"]
        # Same pattern as the four redirects above, but this value is NOT a
        # private state file: it is the ONE shared key file 8100 also reads
        # (chat2api.py :1525-1528 prefers this env var over the repository
        # default, so the dedicated instance verifies exactly the markers the
        # share gateway signs).
        env[BACKEND_ATTRIBUTION_KEY_ENV] = paths["attribution_key"]
        # The child's startup record names who started it (chat2api.py
        # launch_source); this instance is only ever started by a console action.
        env["CHAT2API_LAUNCH_SOURCE"] = "console"
        return env

    # Exit records L5/L6 (round 19): the PARENT side of this instance's life.
    # The child's own log cannot hold these: a taskkill /F never runs the
    # child's finally, so only the parent can say who stopped which PID and
    # how. Kept out of _paths() and below _child_env on purpose: both are
    # line-pinned by tests/test_share_gate_citations.py.
    _LIFECYCLE_LOG = "backend-lifecycle.log"
    _LIFECYCLE_MAX_BYTES = 256 * 1024
    # A9-3 identity evidence (contract §5.1): a NEW file next to the pid
    # file, written by start() after a successful spawn and read by
    # restart()/diagnostics. The pid file's own readers stay byte-for-byte
    # (backend.pid is still THE pid source); this record only adds the
    # five-item identity facts (profile + creation time) the strict check
    # needs. The tolerance is the watchdog's own (±2 s, same as
    # um-share-deploy/watchdog-umshare.ps1:255-281).
    _INSTANCE_FILE = "backend-instance.json"
    _INSTANCE_CREATION_TOLERANCE_S = 2.0

    def _lifecycle_path(self, paths):
        return os.path.join(paths["dir"], self._LIFECYCLE_LOG)

    def _instance_path(self, paths):
        """The dedicated instance's identity record (A9-3, contract §5.1):
        a NEW file next to backend.pid; the pid file stays the readers'
        byte-for-byte source.

        The path is NORMALIZED and CONTAINMENT-CHECKED: the record must
        live directly inside the resolved backend dir (the fixed file name
        carries no separator, so a traversal can never escape the dir)."""
        base = os.path.abspath(paths["dir"])
        path = os.path.abspath(os.path.join(base, self._INSTANCE_FILE))
        if os.path.dirname(path) != base:
            raise ValueError("instance record path escapes the backend dir")
        return path

    def _record(self, paths, message):
        """Append one parent-side lifecycle line; never fails the action.

        Only PIDs, the port, the route and the result are written - never
        tokens, keys or request content.
        """
        path = self._lifecycle_path(paths)
        try:
            if os.path.getsize(path) > self._LIFECYCLE_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass

    def start(self):
        paths = self._paths()
        if self.verify_running() is True:
            return True  # already running; never owned twice
        # Attribution visibility (2026-09-26): a missing, unreadable or
        # too-short key file is NOT a failure — the share keeps serving and
        # 8110 records unattributed whatever it cannot read — but it must be
        # visible, otherwise the operator cannot tell "not ready" from "ready
        # but nothing attributed yet". This warning is about the FILE fact
        # only: whether the running 8100 instance loaded the current content
        # is a DIFFERENT fact and is not probed here (the start action must
        # not depend on it). Never logs the secret itself.
        file_ready, reason, _chars, _mtime = _attribution_file_fact(
            paths["attribution_key"])
        if file_ready is not True:
            print("[share-backend] 归因密钥文件未就绪（原因=%s，文件=%s）："
                  "专用后端照常启动，8110 读不到密钥的请求记为未归属；"
                  "8100 是否已载入本次未探测；"
                  "生成/轮换见 ai-gateway/SHARING.md"
                  % (reason, paths["attribution_key"]),
                  flush=True)
        for d in (paths["dir"], paths["profile"]):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                raise OperationError(
                    "share_backend_failed",
                    "专用后端目录不可创建（%s）：%s" % (d, exc))
        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags = (subprocess.DETACHED_PROCESS |
                             subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            if self._spawner is not None:
                proc = self._spawner(self._child_cmdline(paths),
                                     self._child_env(paths))
            else:
                proc = subprocess.Popen(
                    self._child_cmdline(paths),
                    cwd=self._gateway_dir or ".",
                    env=self._child_env(paths),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags, close_fds=True)
        except OSError as exc:
            raise OperationError(
                "share_backend_failed",
                "专用后端进程启动失败：%s" % exc)
        with open(paths["pid"], "w", encoding="ascii") as f:
            f.write(str(proc.pid))
        self._record(paths, "started pid=%d port=%d by=console"
                     % (proc.pid, paths["port"]))
        # A9-3 identity evidence (contract §5.1): the NEW instance record
        # is written atomically next to backend.pid. It never replaces the
        # pid file (its readers stay byte-for-byte); a failed write only
        # fails closed (the next restart reads "reduced" and refuses).
        self._write_instance_record(paths, proc.pid)
        return True

    def stop(self):
        paths = self._paths()
        pid = self._read_pid(paths)
        if pid is None:
            return not self._port_listening(paths["port"])
        if not self._pid_matches(paths, pid):
            # Registered PID no longer runs the dedicated-backend command
            # line: refuse to act on it (never kill by bare PID).
            self._record(paths, "stop skipped pid=%d port=%d reason=pid_not_backend"
                         % (pid, paths["port"]))
            self._forget(paths)
            return not self._port_listening(paths["port"])
        self._record(paths, "stop requested pid=%d port=%d by=console"
                     % (pid, paths["port"]))
        self._graceful_shutdown(paths)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                self._record(paths, "stopped pid=%d port=%d by=graceful"
                             % (pid, paths["port"]))
                self._forget(paths)
                return True
            time.sleep(0.5)
        if self._stopper is not None:
            self._stopper(pid)
        else:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        alive = self._pid_alive(pid)
        self._record(paths, "force stop pid=%d port=%d by=taskkill result=%s"
                     % (pid, paths["port"], "alive" if alive else "dead"))
        if alive:
            return False
        self._forget(paths)
        return True

    def verify_running(self, forget_stale=True):
        """``True`` only on confirmed evidence, as before.

        ``forget_stale`` is the additive READ-ONLY switch (default ``True`` =
        the historical behaviour byte-for-byte). With ``forget_stale=False`` a
        stale pid file — registered PID no longer running the dedicated-backend
        command line — is LEFT IN PLACE and the verdict is reported as-is: no
        ``_forget()``, no write of any kind. The read-only acceptance
        collectors use that form so a "read-only" check can never delete a file.
        """
        paths = self._paths()
        pid = self._read_pid(paths)
        if pid is None:
            return False
        if not self._pid_matches(paths, pid):
            if forget_stale:
                self._forget(paths)
            return False
        return self._port_listening(paths["port"])

    def backend_pid_state(self, paths=None):
        """Read-only pid-file REPORT — never writes, never deletes.

        This method has no ``_forget()`` (nor any other write) on ANY branch:
        a stale pid file is reported stale and left byte-for-byte as found, so
        the acceptance collectors can describe the backend without changing it.

        The evidence is EXACTLY the evidence ``verify_running`` uses (the CIM
        identity check plus the port table), so this report can never contradict
        that verdict. ``state`` is one of:

        * ``"pid_file_absent"`` — no pid file at the resolved path;
        * ``"pid_file_unreadable"`` — the file exists but its content is not a
          PID (never folded into "absent");
        * ``"pid_stale"`` — a PID is registered but it no longer runs the
          dedicated-backend command line (process gone OR identity mismatch:
          ``_pid_matches`` cannot tell those apart, and this report does not
          guess which — it is the case ``verify_running`` answers False for and
          would have deleted the file);
        * ``"port_not_listening"`` — the PID matches but nobody LISTENs there;
        * ``"running"`` — PID matches and the port is listening.

        Additive A9-3 identity fields (contract §5.1): ``identity`` is
        ``"verified"`` (the instance record is complete AND
        ``_pid_matches_strict`` passes), ``"reduced"`` (no instance record
        but the three shared checks pass — a pre-deployment instance) or
        ``"unknown"`` (everything else, including a contradictory record);
        ``identity_reason`` names the precise code. The report still has NO
        write on any branch.
        """
        paths = paths or self._paths()
        pid_path = paths["pid"]
        present = os.path.isfile(pid_path)
        pid = self._read_pid(paths)
        out = {"pid_file": pid_path, "pid_file_present": present, "pid": pid,
               "pid_matches": None, "port_listening": None, "state": None,
               "identity": "unknown", "identity_reason": None}
        if pid is None:
            out["state"] = ("pid_file_unreadable" if present
                            else "pid_file_absent")
            out["identity_reason"] = out["state"]
            return out
        matches = self._pid_matches(paths, pid)
        out["pid_matches"] = matches
        if not matches:
            out["state"] = "pid_stale"
            out["identity_reason"] = "identity_mismatch"
            return out
        record = self._instance_record(paths)
        if record is None:
            # A pre-deployment instance: the three shared checks pass but
            # there is no instance record -> "reduced" (no CIM re-read).
            out["identity"] = "reduced"
            out["identity_reason"] = "instance_record_absent"
        else:
            identity, reason = self._identity_verdict(paths, pid, record)
            out["identity"] = identity
            out["identity_reason"] = reason
        listening = self._port_listening(paths["port"])
        out["port_listening"] = listening
        out["state"] = "running" if listening else "port_not_listening"
        return out

    def verify_closed(self):
        paths = self._paths()
        pid = self._read_pid(paths)
        if pid is None:
            return not self._port_listening(paths["port"])
        if self._pid_alive(pid) and self._pid_matches(paths, pid):
            return False
        self._forget(paths)
        return not self._port_listening(paths["port"])

    # -- internals --------------------------------------------------------
    def _read_pid(self, paths):
        try:
            with open(paths["pid"], encoding="ascii") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _forget(self, paths):
        try:
            os.remove(paths["pid"])
        except OSError:
            pass

    def _pid_alive(self, pid):
        # `tasklist /FO CSV` 把**每个字段**都加引号（`"python.exe","54300",…`），
        # 所以裸 needle `,54300` 永远不命中——原实现因此对活着的 PID 也返回
        # False（2026-09-28 由执行轮 10 的写面审计旁证发现，影响 stop() 与
        # verify_closed() 的动作判定）。改为按 CSV 第二列（PID 字段）比对。
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10)
            text = out.stdout.decode("utf-8", errors="replace")
            want = str(int(pid))
            for row in csv.reader(text.splitlines()):
                if len(row) >= 2 and row[1].strip() == want:
                    return True
            return False
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    def _pid_process(self, pid):
        """One CIM query for the process identity (image path + command
        line + creation epoch). wmic is deprecated/absent on newer Windows
        (S3-R6A3 R-S3-3: this machine has no wmic), so the evidence comes
        from PowerShell Get-CimInstance — argument-list invocation, never a
        shell.

        A9-3 (contract §5.1): the creation time is read in the SAME query,
        converted to epoch INSIDE PowerShell with the invariant culture (the
        same .NET-ticks conversion the 8100 probe uses) — the Python side
        parses one finite number only. An unreadable row or an unavailable
        creation time reads None — never a guessed identity."""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter "
                 "'ProcessId=%d') | ForEach-Object { "
                 "$_.ExecutablePath + [char]10 + $_.CommandLine + "
                 "[char]10 + $(if ($_.CreationDate) { "
                 "(([double]($_.CreationDate.ToUniversalTime().Ticks "
                 "- %d) / 10000000.0).ToString("
                 "[System.Globalization.CultureInfo]::InvariantCulture)) "
                 "} else { '' }) }" % (pid, _DOTNET_EPOCH_TICKS)],
                capture_output=True, timeout=15)
            text = out.stdout.decode("utf-8", errors="replace")
            lines = [l.strip() for l in text.splitlines()]
            image = lines[0] if len(lines) >= 1 and lines[0] else None
            cmdline = lines[1] if len(lines) >= 2 and lines[1] else None
            creation = None
            if len(lines) >= 3 and lines[2]:
                try:
                    creation = float(lines[2])
                except ValueError:
                    creation = None
            if creation is not None and not math.isfinite(creation):
                creation = None
            if creation is not None and creation <= 0:
                creation = None
            return (image, cmdline, creation)
        except (OSError, subprocess.TimeoutExpired):
            return (None, None, None)

    def _identity_ok(self, paths, image, cmdline):
        """The THREE shared identity checks (byte-for-byte the historical
        ``_pid_matches`` semantics): exact image path (the SAME interpreter
        this adapter would spawn) + the script token as an exact cmdline
        element + the exact '--port <port>' argument pair. A substring
        '8000' inside '80000' or a lookalike cmdline can never match."""
        if not image or not cmdline:
            return False
        if os.path.normcase(os.path.abspath(image)) != \
                os.path.normcase(os.path.abspath(sys.executable)):
            return False
        tokens = cmdline.split()
        script_ok = any(t.endswith("chat2api.py") for t in tokens)
        port_ok = False
        for i, t in enumerate(tokens):
            if t == "--port" and i + 1 < len(tokens) \
                    and tokens[i + 1] == str(paths["port"]):
                port_ok = True
        return script_ok and port_ok

    def _profile_ok(self, paths, cmdline):
        """The exact '--profile <paths["profile"]>' argument pair (A9-3)."""
        tokens = cmdline.split()
        for i, t in enumerate(tokens):
            if t == "--profile" and i + 1 < len(tokens) \
                    and tokens[i + 1] == paths["profile"]:
                return True
        return False

    def _identity_verdict(self, paths, pid, record):
        """``(identity, reason)`` from ONE identity read + the record.

        ``verified`` = the record is complete AND the five-item check passes;
        ``reduced``/``unknown`` are decided by the CALLER (no record ->
        reduced) so this helper only runs when a record exists. Every
        unavailable item reads ``unknown``: identity is never assumed from a
        subset."""
        image, cmdline, creation = self._pid_process(pid)
        if not self._identity_ok(paths, image, cmdline):
            return "unknown", "identity_mismatch"
        if not self._profile_ok(paths, cmdline):
            return "unknown", "instance_profile_mismatch"
        expected = record.get("creation_epoch")
        if creation is None or isinstance(expected, bool) \
                or not isinstance(expected, (int, float)):
            return "unknown", "instance_creation_unavailable"
        if abs(float(creation) - float(expected)) > \
                self._INSTANCE_CREATION_TOLERANCE_S:
            return "unknown", "instance_creation_mismatch"
        return "verified", "instance_record_confirmed"

    def _pid_matches(self, paths, pid):
        """Watchdog-grade identity: THREE checks (byte-for-byte the
        historical semantics, contract §5.1) — image + script token +
        '--port <port>'. (The five-item check restart()/diagnostics use is
        ``_pid_matches_strict``.)"""
        image, cmdline, _creation = self._pid_process(pid)
        return self._identity_ok(paths, image, cmdline)

    def _pid_matches_strict(self, paths, pid):
        """A9-3 FIVE-item identity check (contract §5.1): the three shared
        checks + the exact '--profile <paths["profile"]>' pair + the process
        creation time matching the instance record (within the watchdog's ±2 s).
        Any item unreadable/unavailable -> False: identity is NEVER assumed
        from a subset here (restart() refuses on anything but verified)."""
        if not self._pid_matches(paths, pid):
            return False
        record = self._instance_record(paths)
        if record is None:
            return False
        identity, _reason = self._identity_verdict(paths, pid, record)
        return identity == "verified"

    def _write_instance_record(self, paths, pid):
        """Atomically write the instance identity record (contract §5.1).

        The record is an identity AID, never a second state authority: the
        pid file stays byte-for-byte the readers' pid source, and a failed
        write only fails closed (a later restart then reads "reduced" and
        refuses).

        ``creation_epoch`` is THIS parent's own spawn moment and ``image`` is
        the interpreter this adapter spawned — the same "captured at first
        sight" shape the watchdog keeps for its own record
        (um-share-deploy/watchdog-umshare.ps1:255-281: its own datetime
        compared with the LIVE CIM creation within ±2 s).
        ``_pid_matches_strict`` re-reads the LIVE creation time and
        requires agreement within ±2 s, so a recycled PID never verifies.

        The destination is NORMALIZED and CONTAINMENT-CHECKED before the
        write: both the record and its tmp sibling must resolve directly
        inside the backend dir (the fixed file name carries no separator,
        so a traversal can never escape it)."""
        now = time.time()
        record = {"schema_version": 1, "pid": int(pid),
                  "creation_epoch": now, "image": sys.executable,
                  "profile": paths["profile"], "port": paths["port"],
                  "started_at": now, "source": "console"}
        base = os.path.abspath(paths["dir"])
        path = os.path.abspath(os.path.join(base, self._INSTANCE_FILE))
        tmp = os.path.abspath(path + ".tmp")
        if os.path.dirname(path) != base or os.path.dirname(tmp) != base:
            raise ValueError("instance record path escapes the backend dir")
        try:
            Path(tmp).write_text(json.dumps(record), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self._record(paths, "instance record write failed pid=%d reason=%s"
                         % (pid, type(exc).__name__))

    def _instance_record(self, paths=None):
        """Read the dedicated instance's identity record (read-only).

        Returns the validated record dict or None (absent/malformed/unknown
        schema/port mismatch) — a missing record is NEVER a guessed identity:
        the caller reports "reduced" (the three shared checks only)."""
        paths = paths or self._paths()
        try:
            record = json.loads(
                Path(self._instance_path(paths)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            return None
        pid = record.get("pid")
        port = record.get("port")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if isinstance(port, bool) or not isinstance(port, int) \
                or port != paths["port"]:
            return None
        for key in ("image", "profile"):
            if not isinstance(record.get(key), str) or not record[key]:
                return None
        return record

    def restart(self, *, drain_budget_s=60.0, poll_interval_s=1.0,
                stop_budget_s=30.0, start_budget_s=90.0,
                cancel_check=None, step=None):
        """A9-3 execution body (contract §5.2/§5.4): identity → stop →
        confirm gone → start → verify_running, with an optional per-stage
        callback.

        * the FIVE-item identity must be ``verified`` BEFORE anything is
          stopped (reduced/unknown -> share_backend_identity_unknown:
          never taskkill a bare PID, never cold-start);
        * the graceful shutdown is awaited for at most ``drain_budget_s``;
          the force kill re-verifies the FIVE-item identity BEFORE it runs
          (a replaced/recycled PID is never killed) and the disappearance
          is confirmed within ``stop_budget_s``;
        * ``start()`` is the SAME shared recovery handler the supervision
          calls, and its readiness is confirmed within ``start_budget_s``;
        * ``cancel_check`` (optional callable) is consulted ONCE before the
          destructive stop: a cancelled request stops nothing. Once the stop
          ran, this operation completes its own compensation (the start) and
          reports the actual state — no fake rollback.

        Returns the frozen ``backend`` sub-dict {pid_before, pid_after,
        port, profile, identity}; every failure raises OperationError with
        the actual stage and state (share_backend_restart_failed)."""
        paths = self._paths()
        state = self.backend_pid_state(paths)
        if state.get("state") != "running" \
                or state.get("identity") != "verified":
            raise OperationError(
                "share_backend_identity_unknown",
                "专用后端身份未核实（state=%s identity=%s reason=%s）："
                "绝不 taskkill 裸 PID、不冷启动"
                % (state.get("state"), state.get("identity"),
                   state.get("identity_reason")))
        pid_before = state["pid"]
        if cancel_check is not None:
            cancel_check()
        _step_notify(step, "正在停止专用后端（pid=%s）" % pid_before)
        self._graceful_shutdown(paths)
        deadline = time.monotonic() + max(0.0, float(drain_budget_s))
        while time.monotonic() < deadline:
            if not self._pid_alive(pid_before):
                break
            time.sleep(poll_interval_s)
        if self._pid_alive(pid_before):
            # Re-verify the FIVE-item identity BEFORE the force kill: a
            # replaced/recycled PID is never killed.
            if self._pid_matches_strict(paths, pid_before) is not True:
                raise OperationError(
                    "share_backend_restart_failed",
                    "阶段=stop：等待超时且身份复核未通过（pid=%d 仍存活）："
                    "不 taskkill 身份不明的进程" % pid_before)
            if self._stopper is not None:
                self._stopper(pid_before)
            else:
                subprocess.run(["taskkill", "/PID", str(pid_before),
                                "/T", "/F"], capture_output=True, timeout=15)
            deadline = time.monotonic() + max(0.0, float(stop_budget_s))
            while time.monotonic() < deadline:
                if not self._pid_alive(pid_before):
                    break
                time.sleep(poll_interval_s)
        if self._pid_alive(pid_before):
            raise OperationError(
                "share_backend_restart_failed",
                "阶段=stop：专用后端 %d 未确认消失（force 停止后仍存活）"
                % pid_before)
        self._forget(paths)
        _step_notify(step, "专用后端已停止（pid=%s）" % pid_before)
        if self.start() is not True:
            raise OperationError(
                "share_backend_restart_failed",
                "阶段=start：专用后端启动未获门确认")
        deadline = time.monotonic() + max(0.0, float(start_budget_s))
        running = False
        while time.monotonic() < deadline:
            if self.verify_running() is True:
                running = True
                break
            time.sleep(poll_interval_s)
        if not running:
            raise OperationError(
                "share_backend_restart_failed",
                "阶段=verify_running：专用后端启动后未确认运行")
        pid_after = self._read_pid(paths)
        _step_notify(step, "专用后端已重启（pid=%s）" % pid_after)
        return {"pid_before": pid_before, "pid_after": pid_after,
                "port": paths["port"], "profile": paths["profile"],
                "identity": "verified"}

    def _port_listening(self, port):
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        suffix = ":%d " % port
        for line in out.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "LISTENING" and                     parts[1].endswith(suffix.strip()):
                return True
        return False

    def _graceful_shutdown(self, paths):
        try:
            with open(paths["control_token"], encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            return False
        if not token:
            return False
        req = urllib.request.Request(
            "http://127.0.0.1:%d/admin/shutdown" % paths["port"],
            method="POST", headers={"X-Admin-Control": token})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Rev9 production adapters (A9-1/A9-2/A9-3/A9-4 + the shared recovery entry)
# ---------------------------------------------------------------------------
# Placement note: this block sits AFTER the line-pinned regions of
# tests/test_share_gate_citations.py (the citation pins cover lines 830-836,
# 1202-1219, 1223 and 1234-1246), so the frozen citation lines stay
# byte-identical while the new surfaces live here. The module header's import
# block is untouched on purpose: the one protocol constant the new code needs
# at module level (GATEWAY_RESTART_BUDGET_DEFAULT_S) is imported INSIDE
# production_gateway_restart, and every wire action name is reached through
# the wave-1 BrokerClient methods (share_broker_client.py) instead of raw
# action strings. `_step_notify` and `_HostEvidence` are referenced by the
# methods above and resolve at call time.


def _step_notify(step, note):
    """Optional per-stage callback for the 8110 restart (contract §5.4):
    ``step`` receives ONE bounded stage note; None = no callback."""
    if step is not None:
        step(note)


class _HostEvidence(dict):
    """The frozen host-evidence reply ``{check, verdict, detail, checked_at}``
    whose TRUTHINESS is ``verdict is True``.

    The security gate consumes host evidence through a truthy check in the
    orchestration (the _FunnelAck precedent in this module): a plain dict
    would read truthy even when ``verdict`` is False, faking a pass on a
    FAILED limits check. Binding ``__bool__`` to ``verdict is True`` keeps
    the frozen wire shape (a dict: serializable, indexable, JSON-safe) while
    a False/None verdict can never read as a pass."""

    def __bool__(self):
        return self.get("verdict") is True


def _maintenance_result(broker, call, *, blocked_code, refused_code):
    """Shared discipline for the maintenance verbs (A9-3/A9-4 console side).

    ``call`` receives the BROKER ADAPTER (its public control surface: the five
    BrokerClientAdapter verbs — never ``broker.client``) and returns the
    broker's BrokerResult. The ok payload is returned as a plain dict; every
    refusal is an OperationError with the frozen console code:

    * broker unwired/unreachable -> share_broker_unwired;
    * old broker rejects the unknown verb (bad_request) ->
      share_broker_extension_missing;
    * the instance does not know the verb (capability_missing) ->
      share_capability_missing (NEVER downgraded to a host stop/start);
    * blocked -> ``blocked_code`` (a terminal stop / stopped instance);
    * any other rejection -> ``refused_code``.
    """
    broker = broker if broker is not None else _default_broker()
    try:
        result = call(broker)
    except Exception as exc:  # noqa: BLE001 - transport failures stay honest
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "维护租约：broker 管道不可达（%s: %s）：broker 任务未部署或"
            "未运行；部署指针：um-share-deploy/install-share-broker.ps1；"
            "不冒充可用" % (type(exc).__name__, exc)) from exc
    if result.ok:
        payload = result.result
        if not isinstance(payload, dict):
            raise OperationError(
                refused_code,
                "维护租约：broker 回包形状不符合协议（非结构化结果=%r）"
                % (payload,))
        return dict(payload)
    code = result.error_code
    if code == "bad_request":
        raise OperationError(
            _CODE_EXTENSION_MISSING,
            "维护租约：broker 拒绝未知动词 maintenance-* = broker 白名单"
            "扩展未部署（broker 任务代码早于扩展）；部署指针：按部署授权包"
            "更新并重启 broker 任务后开放；不冒充可用")
    if code == "capability_missing":
        raise OperationError(
            "share_capability_missing",
            "维护租约：实例不支持 resume 动词（旧实例 bad_request/"
            "unknown action）= 能力缺失；绝不降级为 host 停/启或整链重启")
    if code == "blocked":
        raise OperationError(
            blocked_code,
            "维护租约被拒绝（实例已确认停止/已有租约在force）：停止终态"
            "绝不被维护租约重开；detail=%s"
            % _shorten(result.result.get("detail") or ""))
    raise OperationError(
        refused_code,
        "维护租约被拒绝（error_code=%s, detail=%s）：不冒充已受理"
        % (code, _shorten(result.result.get("detail") or "")))


def production_maintenance_begin(component, lease_s, *, broker=None):
    """A9-3: begin ONE bounded maintenance lease (pause 8100 acceptance).

    The broker adapter's public ``maintenance_begin`` verb runs the wave-1
    BrokerClient ``maintenance-begin`` exchange; refusals raise OperationError
    with the frozen console codes (see :func:`_maintenance_result`)."""
    return _maintenance_result(
        broker,
        lambda adapter: adapter.maintenance_begin(component, lease_s),
        blocked_code="share_maintenance_refused",
        refused_code="share_maintenance_refused")


def production_maintenance_end(component, lease_id, *, broker=None):
    """A9-3: end the CURRENT maintenance lease (resume acceptance).

    A late/expired/mismatched lease or a stopped instance is refused
    honestly — a late result never re-opens anything (see
    :func:`_maintenance_result`)."""
    return _maintenance_result(
        broker,
        lambda adapter: adapter.maintenance_end(component, lease_id),
        blocked_code="share_maintenance_refused",
        refused_code="share_maintenance_refused")


def production_maintenance_status(*, broker=None):
    """Read-only maintenance view (A9-1 ``maintenance`` key, A9-3 drain polls).

    Returns the broker payload ``{checked_at, lease, lease_expired, instance,
    last_outcome}`` or None when the broker is unreachable/not deployed/
    rejects the verb: None = NO authority here (honest), never a fabricated
    lease view."""
    try:
        broker = broker if broker is not None else _default_broker()
        result = broker.maintenance_status()
    except Exception:  # noqa: BLE001 - unwired/unreachable stays honest
        return None
    if not result.ok:
        return None
    payload = result.result
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def production_supervision_state():
    """Rev19 A19-2 监督段读面：监督自己的持久状态（只读，懒导入）。

    返回 ``{"state": <watch state>, "state_path": <状态文件路径>}`` 或 None
    （缺席/不可读/形状不对 → None，页面写「未提供 ≠ 监督未运行」）。懒导入：
    动作层只在请求该段时读监督模块（不在装配期导入）；只读：不写状态、
    不启停监督、不发请求。陈旧判定（mtime 超 TTL）由动作层做，本函数不判定。
    """
    try:
        import share_backend_supervision
        state = share_backend_supervision.read_watch_state()
        state_path = share_backend_supervision.DEFAULT_STATE_PATH
    except Exception:  # noqa: BLE001 - 读面自身故障按未提供，绝不冒充监督在跑
        return None
    if not isinstance(state, dict) or state.get("unreadable"):
        # 缺席/不可读/形状不对 → None（页面写「未提供 ≠ 监督未运行」）。
        return None
    return {"state": state, "state_path": state_path}


def production_gateway_restart(budget_s=None, *, broker=None):
    """A9-4: ONE bounded 8100 single-layer restart through the restricted
    broker host path — NEVER share.stop -> share.start (hard boundary #6).

    The broker reply is authoritative: ``restarted`` + ``stopped_confirmed``
    + ``running_confirmed`` are ALL required, otherwise the action fails
    honestly (share_gateway_restart_failed). An old broker (bad_request)
    maps to share_broker_extension_missing; blocked (not running / identity
    unknown / already stopped) maps to share_gateway_restart_failed — the
    restart never doubles as a way to START a stopped gateway.

    ``budget_s=None`` means the protocol default
    (GATEWAY_RESTART_BUDGET_DEFAULT_S), imported lazily so the module
    header stays byte-identical for the citation pins."""
    from share_broker_protocol import (COMPONENT_SHARE_GATEWAY,
                                     GATEWAY_RESTART_BUDGET_DEFAULT_S,
                                     GATEWAY_RESTART_BUDGET_MAX_S)
    if budget_s is None:
        budget_s = GATEWAY_RESTART_BUDGET_DEFAULT_S
    if isinstance(budget_s, bool) or not isinstance(budget_s, int) \
            or not 1 <= budget_s <= GATEWAY_RESTART_BUDGET_MAX_S:
        raise OperationError(
            "invalid_params",
            "restart_budget_s 必须是 1..%d 的整数（收到 %r）；不发送越界预算"
            % (GATEWAY_RESTART_BUDGET_MAX_S, budget_s))
    payload = _maintenance_result(
        broker,
        lambda adapter: adapter.gateway_restart(COMPONENT_SHARE_GATEWAY,
                                              budget_s),
        blocked_code="share_gateway_restart_failed",
        refused_code="share_gateway_restart_failed")
    if not (payload.get("restarted") is True
            and payload.get("stopped_confirmed") is True
            and payload.get("running_confirmed") is True):
        raise OperationError(
            "share_gateway_restart_failed",
            "8100 重启未三证齐（restarted=%r stopped_confirmed=%r "
            "running_confirmed=%r）：不冒充已重启"
            % (payload.get("restarted"), payload.get("stopped_confirmed"),
               payload.get("running_confirmed")))
    return payload


def production_broker_evidence(check, *, broker=None):
    """Read-only broker ``host-evidence{check}`` (limits/faults) for the A9-1
    security-gate layer and the snapshot signal.

    Returns the frozen reply dict ``{check, verdict, detail, checked_at}`` (a
    _HostEvidence whose truthiness IS the verdict) or None when the broker is
    unreachable/not deployed/rejects the verb (an old broker: bad_request) —
    honest unavailability, NEVER a fabricated verdict. A ``verdict: null`` reply
    is a REAL result (evidence unresolvable, unconfirmed) and is returned as
    such, never folded into None."""
    try:
        broker = broker if broker is not None else _default_broker()
    except OperationError:
        return None
    ops = _ProductionBrokerOps(broker)
    evidence = ops.host_evidence(check)
    if evidence is None:
        return None
    return _HostEvidence(evidence)


def _reauth_refusal_message(detail):
    """What the operator can actually do about a reauth refusal (contract
    §4.3): the ONE mapping BOTH the old login flow and the new
    ``share.backend_reauthenticate`` use — output byte-identical.

    A corrupted/unreadable coordination state is permanent: retrying the
    trigger returns the same refusal forever, so the note must name the file to
    clear instead of advising a retry nobody can satisfy. Everything else (a
    live session in the other instance, the rolling attempt cap, an
    unavailable lock) really is transient."""
    if (detail.endswith(":slot_corrupt")
            or detail.endswith(":slot_unreadable")):
        return ("该实例的跨实例状态文件已损坏或不可读，重试不会改变结果："
                "需由运维删除 data/chat2api-reauth.state 后再触发登录")
    return "请稍后重试或点「刷新状态」核对"


def _backend_control_token(paths):
    """``(token, reason)`` for the dedicated instance's control secret.

    ``(None, "unreadable"|"empty")`` when it cannot be used: the caller
    refuses with share_backend_login_unwired and sends ZERO HTTP. The secret
    is never echoed into any message, result or error."""
    try:
        with open(paths["control_token"], encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None, "unreadable"
    if not token:
        return None, "empty"
    return token, "ok"


def _auth_status_payload(paths, token=None, opener=None):
    """The parsed ``GET /admin/auth/status`` payload, or None when the
    endpoint is unreachable/unreadable/off-contract (honest unknown).

    ``token=None`` reads the dedicated control secret itself; a missing or
    empty secret returns None with ZERO HTTP. The secret is never echoed
    into any message, result or error."""
    if token is None:
        token, _reason = _backend_control_token(paths)
    if token is None:
        return None
    fetch = opener or urllib.request.urlopen
    req = urllib.request.Request(
        "http://127.0.0.1:%d/admin/auth/status" % paths["port"],
        headers={"X-Admin-Control": token})
    try:
        with fetch(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) \
            or not isinstance(payload.get("state"), str):
        return None
    return payload


def _backend_health_snapshot(paths, opener=None):
    """``(reachable, payload)`` from ONE bounded ``GET /health`` (read-only).

    ``reachable`` is True only for an HTTP 200; ``payload`` is the parsed
    JSON object when the body is a dict, else None. A cold/not-ready process
    answers (False, None) — never a fabricated reading."""
    fetch = opener or urllib.request.urlopen
    req = urllib.request.Request("http://127.0.0.1:%d/health" % paths["port"])
    try:
        with fetch(req, timeout=4) as resp:
            body = resp.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError):
                parsed = None
            return resp.status == 200, (parsed if isinstance(parsed, dict)
                                       else None)
    except Exception:  # noqa: BLE001  not-ready is just not-ready
        return False, None


def _reauth_result(accepted, state, label, triggered, polled, instance,
                    reason, note):
    """The frozen A9-2 result shape (contract §4.2); ``note`` bounded."""
    return {"accepted": accepted, "state": state, "label": label,
            "triggered": triggered, "polled": polled, "instance": instance,
            "reason": reason, "note": _shorten(note)}


def production_backend_reauth(*, ops=None, control=None, urlopen_fn=None,
                            poll_interval_s=1.5, poll_attempts=6,
                            cancel_check=None):
    """A9-2 production body: re-authenticate 8110 ONLY.

    Never cold-starts (the old login flow keeps its cold-start branch; this
    action does NOT), never restarts the process, never bypasses the human SSO:

    * the instance must be ``running`` with a ``verified`` five-item identity,
      else share_backend_not_running / share_backend_identity_unknown;
    * a missing control token refuses with share_backend_login_unwired and
      ZERO HTTP;
    * ``valid`` = zero side effects; ``logging_in`` = no second window;
      ``failed`` = refuse the rejected credential; ``needs_login`` = POST
      /admin/auth/login (non-202 refuses); ``unknown``/unreadable = no
      trigger at all (unknown is NOT expired);
    * the poll window (``poll_attempts`` x ``poll_interval_s``) reports
      valid/logging_in, preserves a ``reauth_refused:`` reason verbatim and
      raises share_backend_reauth_refused, raises
      share_backend_reauth_failed on ``failed``, and raises
      share_backend_reauth_manual when the window ends still needing a
      login (never claiming a browser is waiting).
    ``cancel_check`` is the cooperative cancel point (called at the start,
    before the trigger and between polls); the old login flow passes none."""
    opener = urlopen_fn or urllib.request.urlopen
    ops = ops or production_backend_ops()
    paths = ops._paths()
    state = ops.backend_pid_state(paths)
    if state.get("state") != "running":
        raise OperationError(
            "share_backend_not_running",
            "专用后端未运行（state=%s，原因=%s）：仅认证动作绝不冷启动"
            % (state.get("state"), state.get("identity_reason")))
    if state.get("identity") != "verified":
        raise OperationError(
            "share_backend_identity_unknown",
            "专用后端身份未核实（identity=%s，原因=%s）：仅认证动作绝不"
            "操作身份不明的进程"
            % (state.get("identity"), state.get("identity_reason")))
    instance = {"pid": state.get("pid"), "port": paths["port"],
                "identity": "verified"}
    if cancel_check is not None:
        cancel_check()
    token, reason = _backend_control_token(paths)
    if token is None:
        raise OperationError(
            "share_backend_login_unwired",
            "专用后端控制令牌缺失或为空（原因=%s，未部署或已清理）："
            "不触发认证、零 HTTP" % reason)
    payload = _auth_status_payload(paths, token, opener=opener)
    if payload is None:
        # Unreadable != expired: NEVER trigger on an unknown state.
        return _reauth_result(
            True, "unconfirmed", None, False, 0, instance,
            "auth_status_unreadable",
            "专用后端认证状态不可读：未触发认证（未知 ≠ 过期）；"
            "请稍后重试或点「刷新状态」核对")
    current = payload["state"]
    label = payload.get("label")
    error = payload.get("error")
    error = error if isinstance(error, str) and error else None
    if current == "valid":
        return _reauth_result(True, "valid", label, False, 0, instance,
                              None, "专用后端上游认证已有效（零副作用）")
    if current == "logging_in":
        return _reauth_result(True, "logging_in", label, False, 0,
                              instance, None, "登录已在进行中：不重复开窗")
    if current == "failed":
        raise OperationError(
            "share_backend_reauth_refused",
            "专用后端上次认证已被拒绝（%s）：绝不重试已被拒绝的凭据"
            % (error or label or "failed"))
    if current != "needs_login":
        # A readable state outside the frozen branches (e.g. "invalid"):
        # unknown != expired — never trigger on it.
        return _reauth_result(
            True, "unconfirmed", label, False, 0, instance,
            "instance_state_%s" % current,
            "专用后端报告认证状态 %s：未触发（未知 ≠ 过期），"
            "请点「刷新状态」核对" % current)
    if cancel_check is not None:
        cancel_check()
    trigger = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (paths["port"],
                                   _BACKEND_LOGIN_TRIGGER_PATH),
        data=b"", method="POST", headers={"X-Admin-Control": token})
    try:
        with opener(trigger, timeout=8) as resp:
            trigger_status = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        trigger_status = exc.code
        try:
            exc.read()
        except (OSError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise OperationError(
            "share_backend_reauth_failed",
            "登录触发请求失败（%s: %s）：未开始登录流程"
            % (type(exc).__name__, exc))
    if trigger_status != 202:
        raise OperationError(
            "share_backend_reauth_refused",
            "登录触发被拒（HTTP %s）：未开始登录流程" % trigger_status)
    last_state = None
    last_label = label
    polled = 0
    for _ in range(max(1, poll_attempts)):
        if cancel_check is not None:
            cancel_check()
        polled += 1
        payload = _auth_status_payload(paths, token, opener=opener)
        if payload is None:
            continue
        current = payload["state"]
        label = payload.get("label")
        error = payload.get("error")
        error = error if isinstance(error, str) and error else None
        if current == "valid":
            return _reauth_result(True, "valid", label, True, polled,
                                  instance, None, "登录完成（上游认证有效）")
        if current == "logging_in":
            return _reauth_result(
                True, "logging_in", label, True, polled, instance, None,
                "登录进行中——请在专用实例浏览器窗口完成上游登录，"
                "然后点「刷新状态」确认")
        if error is not None and error.startswith("reauth_refused:"):
            raise OperationError(
                "share_backend_reauth_refused",
                "专用实例拒绝了本次登录会话（%s）——未开启新的提交窗口；%s"
                % (error, _reauth_refusal_message(error)))
        if current == "failed":
            raise OperationError(
                "share_backend_reauth_failed",
                "专用后端登录失败（%s）" % (error or label or "failed"))
        last_state = current
        last_label = label
        time.sleep(poll_interval_s)
    raise OperationError(
        "share_backend_reauth_manual",
        "认证窗口结束仍未见有效会话（最后状态=%s）：需人工在本机完成 "
        "上游 SSO 后点「刷新状态」核对" % (last_state or "unknown"))


def _production_intent_write(state, *, source):
    """Share runtime-intent writer (the ``_INTENT_WRITE`` seam value).

    Delegates to ``share_backend_supervision.write_desired_state`` (the ONE
    writer implementation, §7.4); the import is deferred because the
    supervision module must never be imported by the console action layer.
    A missing module or a failed write returns False — the caller then says
    "意图未写" instead of pretending it was written."""
    try:
        from share_backend_supervision import write_desired_state
        return bool(write_desired_state(state, source=source))
    except Exception:  # noqa: BLE001 - never a fake True on a failed write
        return False


def production_backend_observe(forget_stale=True):
    """Read-only dedicated-backend view: /health + /admin/auth/status on
    the dedicated port with ITS control secret. Never probes port 8000 and
    never touches the production chat2api process. There is NO login_url:
    the instance's GET / is health JSON, not a login page — login is
    triggered through the console's「登录分享专用后端」action
    (production_backend_login), which drives the control-token-gated
    POST /admin/auth/login that opens the instance's own browser.

    Plan-Revision 13: the instance recovers on its own, so ``auth_label``
    is the authoritative field — chat2api puts the reason a window was left
    to a human (or that the last check was inconclusive) into the label it
    reports. The dict shape is unchanged.

    The ``attribution`` entry (2026-09-26, split into three facts after the
    review: ONE boolean must not stand for THREE different things) is a
    read-only view of the attribution chain with its own names:

    * ``file_ready`` — the ONE shared key file 8100 also reads is readable
      by THIS process now and long enough;
    * ``instance_loaded`` — whether the RUNNING 8100 instance is INFERRED to
      have loaded the CURRENT content (its start time vs the file mtime);
      "unknown" when the process cannot be found — never "yes";
    * ``request_attribution`` — NOT observed here; it points at the runtime
      fact that decides it (a row with ``attribution="verified"`` in the 8110
      ledger, whose path is carried along).

    All of them are reported even when THIS instance is NOT running (they
    are facts about the key file and the 8100 process, not about THIS 8110
    process), so the read surface can tell「文件未就绪」from「文件就绪但
    8100 实例未载入（推断）」— and it never claims the third one either
    way.

    ``forget_stale`` is the additive READ-ONLY switch (default ``True`` = the
    historical behaviour, byte-for-byte): it is forwarded verbatim to
    ``verify_running``. With ``forget_stale=False`` this observation NEVER
    deletes a stale ``backend.pid`` — the caller gets the same ``running``
    verdict with the file left in place. The dict shape is unchanged either
    way; for the honest reason behind a ``running=False`` read
    ``production_backend_pid_state()``.

    A9-1 additive evidence (contract §3.2/§3.4) for the layered
    diagnostics — the existing keys stay byte-for-byte:

    * ``observed_at`` — the epoch when THIS process read
      ``/admin/auth/status`` (None when the status was never read);
    * ``auth_evidence`` / ``auth_gate`` — the instance's own bounded
      evidence/gate sub-objects (None when absent/off-contract);
    * ``health_reachable`` — whether 8110 answered ``GET /health`` 200
      (True/False/None = not observed);
    * ``upstream_reachability`` — the ``/health`` sub-object with
      ``state`` (ok/unreachable/unknown) and ``checked_epoch``; the
      diagnostics layer reads ``state``/``checked_epoch`` from here.
    """
    ops = production_backend_ops()
    paths = ops._paths()
    running = ops.verify_running(forget_stale=forget_stale)
    out = {"running": running, "auth_state": None, "auth_label": None,
           "observed_at": None, "auth_evidence": None, "auth_gate": None,
           "health_reachable": None, "upstream_reachability": None,
           "attribution": share_attribution_status(
               paths["attribution_key"], ledger_path=paths["ledger"])}
    if running is not True:
        return out
    payload = _auth_status_payload(paths)
    if payload is None:
        # No usable status read (missing/unreadable secret, unreachable or
        # off-contract endpoint): the historical "unknown" stays, never a
        # fabricated state and never a fabricated observation time.
        out["auth_state"] = "unknown"
    else:
        out["observed_at"] = time.time()
        out["auth_state"] = payload["state"]
        out["auth_label"] = payload.get("label")
        evidence = payload.get("evidence")
        out["auth_evidence"] = evidence if isinstance(evidence, dict) else None
        gate = payload.get("gate")
        out["auth_gate"] = gate if isinstance(gate, dict) else None
    reachable, health = _backend_health_snapshot(paths)
    out["health_reachable"] = reachable
    if isinstance(health, dict):
        reach = health.get("upstream_reachability")
        out["upstream_reachability"] = reach if isinstance(reach, dict) \
            else None
    return out


def production_backend_pid_state():
    """Read-only dedicated-backend pid REPORT: the resolved pid file plus the
    identity evidence, WITHOUT any cleanup. This function has no write on any
    branch — a stale pid file is reported stale and left byte-for-byte as found
    (``_ProductionBackendOps.backend_pid_state``). Use it next to
    ``production_backend_observe(forget_stale=False)`` when a read-only
    collector must say WHY the backend is not running instead of only ``False``.
    """
    return production_backend_ops().backend_pid_state()


def _backend_auth_state(paths, opener=None):
    """``(state, label, error)`` from the dedicated instance's auth/status.

    ``error`` is the instance's own reason string (for example
    ``manual_takeover:umpass_prefill_missing`` or
    ``reauth_refused:global_session_active``), or None when it reports no
    failure. A missing/unreadable status endpoint stays "unknown" and is never
    turned into a fabricated state. The read is the SAME
    :func:`_auth_status_payload` the A9-2/A9-1 readers use (one parser,
    byte-for-byte behaviour)."""
    token, _reason = _backend_control_token(paths)
    if token is None:
        return ("unknown", None, None)
    payload = _auth_status_payload(paths, token, opener=opener)
    if payload is None:
        return ("unknown", None, None)
    error = payload.get("error")
    return (payload["state"], payload.get("label"),
            error if isinstance(error, str) and error else None)


_BACKEND_LOGIN_TRIGGER_PATH = "/admin/auth/login"

# Dedicated-instance SSO window (chat2api --login-timeout): 600s instead of
# the 180s default — the first real attempt timed out at exactly 180s with
# the maintainer away. Production chat2api keeps its own default.
_BACKEND_LOGIN_TIMEOUT_S = 600


def _backend_health_ok(paths, opener=None):
    """Bounded cold-start readiness probe: GET /health (public route in
    chat2api.py — no control header involved) must answer 200. This is the
    liveness signal that the dedicated process's HTTP control surface is
    accepting requests; auth/status on a cold-starting process would only
    read "unknown", which cannot distinguish not-ready from broken. The SAME
    :func:`_backend_health_snapshot` read the A9-1 upstream layer uses
    (one bounded GET, byte-for-byte verdict)."""
    reachable, _payload = _backend_health_snapshot(paths, opener=opener)
    return reachable


def production_backend_login(urlopen_fn=None, ops=None,
                             poll_interval_s=1.5, poll_attempts=6,
                             ready_interval_s=1.0, ready_attempts=15):
    """Console "登录分享专用后端" flow (maintainer direction 2026-09-17,
    replacing the misleading login_url pointer: the dedicated instance's
    GET / is health JSON, NOT a login page — the only login trigger is the
    control-token-gated POST /admin/auth/login, whose refresh() opens the
    instance's OWN independent-profile browser for the human UM SSO).

    Discipline:
    * every request targets the DEDICATED port (paths["port"]) with the
      dedicated control secret in the X-Admin-Control header — port 8000
      and the production chat2api process are never touched; the secret is
      never echoed into any message, result or error;
    * an already-valid instance is a no-op; a not-running instance is
      STARTED by this flow and, when the flow then FAILS, restored to its
      original stopped state (an originally-running instance is left
      running — we only ever undo our own change);
    * COLD-START READINESS GATE (maintainer review 2026-09-17): an
      instance this flow started is polled via /health until its control
      surface actually answers (a cold-starting process would make the
      very next auth/trigger request fail and wrongly roll the fresh
      instance back); only after readiness is the login submitted, and a
      readiness TIMEOUT restores the just-started instance;
    * the trigger is fire-and-forget by contract (wait=False): a short
      bounded poll reports states honestly — "logging_in" (the ONLY
      state chat2api's refresh() sets when it actually starts the browser
      flow) means the popped window is waiting for the human; when the
      window ends WITHOUT an observed auth state (endpoint unreachable or
      unreadable), the result says "触发已受理、状态未确认" and never
      claims a browser is waiting; completing the SSO is the human's
      step, never a fabricated success.

    Returns {"triggered", "state", "label", "started_here", "restored",
    "note"}; failures raise structured OperationErrors.
    """
    opener = urlopen_fn or urllib.request.urlopen
    ops = ops or production_backend_ops()
    paths = ops._paths()
    try:
        with open(paths["control_token"], encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if not token:
        raise OperationError(
            "share_backend_login_failed",
            "专用后端控制令牌缺失或为空（未部署或已清理）；不触发登录")
    started_here = False
    was_running = ops.verify_running() is True
    if not was_running:
        if ops.start() is not True:
            raise OperationError(
                "share_backend_login_failed",
                "专用后端启动未获门确认；不触发登录")
        started_here = True

    def _restore_and_raise(message):
        restored = None
        if started_here:
            ops.stop()
            restored = ops.verify_running() is not True
        suffix = ""
        if started_here:
            suffix = ("；已按原始状态恢复（专用后端已停止）"
                      if restored is True else
                      "；恢复未确认——请人工核对专用后端进程与 %s 端口后清理"
                      % paths["port"])
        raise OperationError("share_backend_login_failed", message + suffix)

    if not was_running:
        # Cold-start readiness gate (Fix A): do NOT submit the login to a
        # process that has not proven its control surface answers — that
        # would connect-refuse and wrongly restore the fresh instance.
        ready = False
        for _ in range(max(1, ready_attempts)):
            if _backend_health_ok(paths, opener=opener):
                ready = True
                break
            time.sleep(ready_interval_s)
        if not ready:
            _restore_and_raise(
                "专用后端冷启动未就绪（%.0fs 内 /health 未确认响应）；"
                "不提交登录" % (ready_attempts * ready_interval_s))

    # Already valid: nothing to trigger (bearer present); no restore —
    # a running+valid instance is the desired end state either way.
    state, label, detail = _backend_auth_state(paths, opener=opener)
    if state == "valid":
        return {"triggered": False, "state": "valid", "label": label,
                "started_here": started_here, "restored": None,
                "note": "专用后端上游认证已有效（无需触发登录）"}

    trigger = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (paths["port"],
                                   _BACKEND_LOGIN_TRIGGER_PATH),
        data=b"", method="POST",
        headers={"X-Admin-Control": token})
    try:
        with opener(trigger, timeout=8) as resp:
            trigger_status = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        trigger_status = exc.code
        try:
            exc.read()
        except (OSError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _restore_and_raise("登录触发请求失败（%s: %s）"
                           % (type(exc).__name__, exc))
    if trigger_status != 202:
        # The endpoint answers 202 for an accepted fire-and-forget trigger;
        # anything else means the login did NOT start. Report only the
        # status code — no response body, and never the control secret.
        _restore_and_raise("登录触发被拒（HTTP %s）；未开始登录流程"
                           % trigger_status)

    last_state, last_label, last_detail = None, None, None
    observed_any = False
    for _ in range(max(1, poll_attempts)):
        last_state, last_label, last_detail = _backend_auth_state(
            paths, opener=opener)
        if last_state != "unknown":
            observed_any = True
        if last_state == "valid":
            return {"triggered": True, "state": "valid",
                    "label": last_label, "started_here": started_here,
                    "restored": None, "note": "登录完成（上游认证有效）"}
        if last_state == "logging_in":
            # The ONLY state chat2api's refresh(wait=False) sets when it
            # actually starts the browser flow — here (and only here) the
            # popped window really is waiting for the human.
            return {"triggered": True, "state": "logging_in",
                    "label": last_label, "started_here": started_here,
                    "restored": None,
                    "note": "登录进行中——请在弹出的专用实例浏览器窗口完成 "
                            "上游登录，然后点「刷新状态」确认"}
        if (last_detail or "").startswith("reauth_refused:"):
            # A refused session never started, so this is NOT a failed login:
            # leave the instance exactly as it is (never stop a healthy process
            # because the cross-instance gate said no) and report the refusal
            # itself. Checked before the state branches because a refusal leaves
            # the instance in needs_login (refresh() only sets _auth_error), so a
            # state-first test would report the common refusal as "trigger
            # accepted, re-trigger if needed".
            return {"triggered": True, "state": last_state or "failed",
                    "label": last_label, "started_here": started_here,
                    "restored": None,
                    "note": "专用实例拒绝了本次登录会话（%s）——未开启新的"
                            "提交窗口，实例保持运行；%s"
                            % (last_detail, _reauth_refusal_message(last_detail))}
        if last_state == "failed":
            # Plan-Revision 13: the instance also reports WHY it stopped
            # (for example a window left to a human), so the failure text
            # carries it instead of a bare "failed" nobody can act on.
            _restore_and_raise("专用后端登录失败（%s）"
                               % (last_detail or last_label or "failed"))
        time.sleep(poll_interval_s)
    # Window ended without a terminal verdict (Fix B): the honest report
    # depends on what was ACTUALLY observed. "needs_login" observed means
    # the instance still says no login session exists; NO readable state
    # at all means the status endpoint stayed unreachable — neither may be
    # presented as "browser waiting for the human".
    if last_state == "needs_login" and observed_any:
        return {"triggered": True, "state": "needs_login",
                "label": last_label, "started_here": started_here,
                "restored": None,
                "note": "登录触发已受理；专用实例报告「需要登录」——尚未"
                        "观察到登录会话（logging_in）%s。请点「刷新状态」"
                        "核对，必要时再次触发"
                        % (("；实例自报原因：%s" % last_detail)
                           if last_detail else "")}
    return {"triggered": True, "state": last_state or "unknown",
            "label": last_label, "started_here": started_here,
            "restored": None,
            "note": "登录触发已受理（HTTP 202）；当前认证状态未确认"
                    "（状态接口未观察到可读状态）——请稍后点「刷新状态」"
                    "核对"}


def production_backend_ops():
    """Production dedicated-backend gate (fresh instance per call; the
    paths/port resolve lazily so assembly never touches the filesystem)."""
    return _ProductionBackendOps(gateway_dir=str(
        (Path(__file__).resolve().parent / "chat2api")))


def production_keys_confirmed(manager=None):
    """R8-A7 open precondition: >=1 enabled share key and the published
    config in sync. Returns True/False, or None when unknown (manager
    unavailable) — never a fabricated True. Uses the caller-provided
    manager when given; lazily builds one over the same deployment broker
    otherwise (the class-level recover guard keeps this per-path safe)."""
    try:
        if manager is None:
            broker = _default_broker()
            from share_management import ShareManager
            manager = ShareManager(broker=broker)
        status = manager.status()
    except Exception:  # noqa: BLE001  unknown, not a fake pass
        return None
    if not isinstance(status, dict) or status.get("in_sync") is not True:
        return False
    try:
        raw = manager.list_keys()
    except Exception:  # noqa: BLE001
        return None
    # ShareManager.list_keys returns {"keys": [rows]} (the /api/share/keys
    # shape); a bare list is tolerated for test seams. Found live via the
    # production exercise (E-R8-8): a dict iteration bug made the check
    # return False with keys present.
    if isinstance(raw, dict):
        keys = raw.get("keys")
    else:
        keys = raw
    enabled = [k for k in keys or []
               if isinstance(k, dict) and k.get("enabled") is True]
    return bool(enabled)


def production_keys_diagnosis(manager=None):
    """R8-A7 open-precondition DIAGNOSIS (2026-09-22 split): WHY the keys
    precondition fails, as a structured dict — the merged boolean of
    ``production_keys_confirmed`` could not tell "no enabled key" from
    "published config not in sync", which made the three production
    failures of 2026-09-22 undiagnosable.

    Same manager acquisition as production_keys_confirmed (caller-provided
    or lazily built over the same deployment broker; an assembly or
    observation failure is REPORTED, never folded into a False). Returns::

        {"in_sync": bool|None,     # None = status not observable
         "keys_ok": bool|None,     # None = key list not observable
         "enabled_keys": int,      # 0 when the key list is unreadable
         "total_keys": int,        # key rows observed via list_keys
         "reasons": [code, ...]}   # stable order; empty = all normal

    Reason codes: ``manager_unavailable`` (manager/status observation
    failed), ``config_not_in_sync`` (in_sync is not True — an absent or
    malformed field reads as in_sync None here, still fail-closed),
    ``no_enabled_keys`` (zero enabled keys), ``keys_read_failed``
    (list_keys failed: counts stay 0 and keys_ok None). Both causes
    present -> both codes in the stable order above (config before
    keys); an all-normal observation yields an empty list. Keys ARE
    listed even when out of sync on purpose: the operator needs both
    facts at once (the production failure showed in_sync=false AND
    active_keys=0 together)."""
    reasons = []
    try:
        if manager is None:
            broker = _default_broker()
            from share_management import ShareManager
            manager = ShareManager(broker=broker)
        status = manager.status()
    except Exception:  # noqa: BLE001  unknown, never folded into False
        return {"in_sync": None, "keys_ok": None, "enabled_keys": 0,
                "total_keys": 0, "reasons": ["manager_unavailable"]}
    in_sync = status.get("in_sync") if isinstance(status, dict) else None
    if in_sync is not True:
        reasons.append("config_not_in_sync")
    keys_ok = None
    enabled_count = 0
    total_count = 0
    try:
        raw = manager.list_keys()
    except Exception:  # noqa: BLE001
        reasons.append("keys_read_failed")
    else:
        # Same shape tolerance as production_keys_confirmed: the real
        # ShareManager returns {"keys": [rows]}; a bare list is tolerated
        # for test seams (E-R8-8).
        keys = raw.get("keys") if isinstance(raw, dict) else raw
        rows = [k for k in keys or [] if isinstance(k, dict)]
        enabled_count = len([k for k in rows if k.get("enabled") is True])
        total_count = len(rows)
        keys_ok = enabled_count > 0
        if enabled_count == 0:
            reasons.append("no_enabled_keys")
    return {"in_sync": in_sync, "keys_ok": keys_ok,
            "enabled_keys": enabled_count, "total_keys": total_count,
            "reasons": reasons}


def production_seams(*, broker=None, task_name=None,
                     exe_path=None, runner=None, sleeper=None,
                     status_path=None, keys_manager=None):
    """Assemble the console_share_actions seam dict, key-by-key.

    Keys ARE the orchestration's injection seam globals exactly
    (console_share_actions.py :108-150), so wiring is a direct
    ``setattr(console_share_actions, key, value)`` per entry. ALL NINETEEN
    seams are delivered (Revision 8 adds the dedicated-backend gate and
    observation plus the open-precondition key check; the 2026-09-22
    diagnosis split adds the structured WHY behind the same key
    precondition; Rev9 adds the six A9 seams — the maintenance lease
    pair + status view, the 8100 gateway restart, the 8110 reauth and the
    runtime-intent writer):

    * _BROKER_OPS / _DRAIN share ONE real gate instance (the drain seam is
      the bounded protocol over the same restricted primitives);
    * _TS_SERVICE_CONTROL runs through the same broker adapter's control
      client over the restricted ts-service-control verb;
    * _TS_SERVICE_STATUS / _CONNECTIVITY_OK / _FUNNEL_CONTROL /
      _FUNNEL_OBSERVED bind the injected tailscale/sc runners (None keeps
      the production defaults);
    * _DESIRED_STOP_READER reads the broker's protected lifecycle record
      (injectable status_path, default the deployment path);
    * _MAINTENANCE_BEGIN / _MAINTENANCE_END / _MAINTENANCE_STATUS /
      _GATEWAY_RESTART are the wave-1 BrokerClient maintenance verbs
      bound to the SAME broker as the gate (no raw action strings here);
    * _BACKEND_REAUTH is the A9-2 production body (its own lazy ops);
    * _INTENT_WRITE is the deferred supervision writer (the console action
      layer never imports the supervision module);
    * _SUPERVISION_STATE is the Rev19 A19-2 supervision-segment read face
      (lazy: it imports the supervision module only when the segment is
      requested, and reads the supervisor's OWN state file).

    The broker gate is assembled EAGERLY (an absent deployment config
    raises share_broker_unwired at assembly — the same honest refusal the
    production wiring already propagates); the observation seams are lazy
    partials and never run at assembly time. ``task_name`` is retained as
    the deployment fact for callers that build a gate explicitly, but the
    gate itself no longer runs host evidence in-process.
    """
    if broker is None:
        broker = _default_broker()
    ops = production_broker_ops(broker=broker)
    bind = {}
    if exe_path is not None:
        bind["exe_path"] = exe_path
    if runner is not None:
        bind["runner"] = runner
    funnel_bind = dict(bind)
    if sleeper is not None:
        funnel_bind["sleeper"] = sleeper
    status_bind = {"runner": runner} if runner is not None else {}
    drain_bind = {"sleeper": sleeper} if sleeper is not None else {}
    reader_bind = {"status_path": status_path} if status_path is not None \
        else {}
    return {
        "_BROKER_OPS": ops,
        "_TS_SERVICE_CONTROL": partial(production_ts_service_control,
                                       broker=broker),
        "_TS_SERVICE_STATUS": partial(production_ts_service_status,
                                      **status_bind),
        "_CONNECTIVITY_OK": partial(production_connectivity_ok, **bind),
        # R13-T: the BackendState channel for the connectivity wait (same
        # runner/exe binding as the verdict seam) - the explicit
        # 需人工登录 stop point reads the node's own structured state.
        "_CONNECTIVITY_STATE": partial(production_connectivity_state,
                                       **bind),
        "_FUNNEL_CONTROL": partial(production_funnel_control, **funnel_bind),
        "_FUNNEL_OBSERVED": partial(production_funnel_observed, **bind),
        "_DRAIN": partial(_drain_with, ops, **drain_bind),
        "_DESIRED_STOP_READER": partial(production_desired_stop_reader,
                                        **reader_bind),
        # Dedicated share backend (R6-A1/R8-A7): the five-layer chain's
        # first layer; unwired-equivalent behaviour only when the backend
        # dir cannot resolve (the gate object itself is always delivered).
        "_BACKEND_OPS": production_backend_ops(),
        "_BACKEND_OBSERVE": production_backend_observe,
        # Login trigger (2026-09-17): control-token-gated POST
        # /admin/auth/login on the dedicated instance; its own browser
        # carries the human SSO. Delivered eagerly like the gate — the
        # flow itself fail-closes on a missing control token.
        "_BACKEND_LOGIN": production_backend_login,
        "_KEYS_CONFIRMED": (partial(production_keys_confirmed,
                                    manager=keys_manager)
                            if keys_manager is not None
                            else production_keys_confirmed),
        # 2026-09-22 diagnosis split: the structured WHY behind the same
        # key precondition (no enabled keys vs config not in sync), bound
        # to the SAME manager so both seams observe one consistent view.
        "_KEYS_DIAGNOSIS": (partial(production_keys_diagnosis,
                                    manager=keys_manager)
                            if keys_manager is not None
                            else production_keys_diagnosis),
        # Rev9 A9-1/A9-2/A9-3/A9-4: the six frozen seams. The four
        # maintenance/gateway seams bind the SAME broker as the gate (the
        # wire action names live in the wave-1 BrokerClient methods); the
        # reauth seam carries its own lazy ops; the intent writer defers the
        # supervision import to call time.
        "_MAINTENANCE_BEGIN": partial(production_maintenance_begin,
                                      broker=broker),
        "_MAINTENANCE_END": partial(production_maintenance_end,
                                    broker=broker),
        "_MAINTENANCE_STATUS": partial(production_maintenance_status,
                                      broker=broker),
        "_GATEWAY_RESTART": partial(production_gateway_restart,
                                   broker=broker),
        "_BACKEND_REAUTH": production_backend_reauth,
        "_INTENT_WRITE": _production_intent_write,
        # Rev19 A19-2: the supervision segment's read face — a lazy
        # read of the supervisor's OWN state file (the same single
        # source the watch writes, A9-5). Unwired in tests/fixtures
        # (the seam stays None) so no run ever reads a production
        # state file; the page then writes 未提供.
        "_SUPERVISION_STATE": production_supervision_state,
    }
