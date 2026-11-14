# -*- coding: utf-8 -*-
"""console/share_production.py - production adapters for the cloud share seams.

Each adapter mirrors a REAL product surface, honestly:

* ``production_broker_ops()`` - the restricted broker operations gate, the
  narrow wrapper the orchestration's ``_broker_ops()`` contract expects.
  Host evidence and the task gate execute INSIDE the restricted broker; this
  process never runs a privileged OS command.
* ``production_desired_stop_reader()`` - read-only lifecycle view of the
  broker's protected record (broker-status.json "lifecycle"): the record
  function returns {state, stopped_at} or None (absent/malformed file -> None,
  never a fabricated state); the SEAM reader maps it to the orchestration's
  bool|None contract (stopped -> True, running -> False, no record -> None).
* ``production_seams()`` - the seam dict for console_share_actions (key-by-key
  match with the module's injection seams): the broker gate, the desired-stop
  reader, the key preconditions and the Rev9 maintenance / gateway-restart
  seams. The retired Windows TS/Funnel seams (#33) and the dedicated
  backend seams (#34) are NOT delivered any more.
* ``share_attribution_key_path()`` / ``share_attribution_status()`` - the ONE
  shared attribution key file the merged 8100 process reads at startup (C2
  verified attribution). The path is a deployment fact, not a second state
  store. The status helper reports TWO DIFFERENT facts under two different
  names: ``file_ready`` (this process, now) and ``instance_loaded`` (an
  INFERENCE from the process start time vs the file mtime, "unknown" when
  the process cannot be found).

Privacy: no credential content passes through this module - the broker
control secret stays inside the share-management loader, and the file surfaces
are the TWO fixed deployment reads below: broker-status.json (paths/state only,
no secrets) and the shared attribution key (opened ONLY to read its
presence/stripped length - the secret is never returned, printed, logged or
compared here). The attribution-key path / run-root environment variables are
read (``share_attribution_key_path``) and ONE non-secret warning line is
printed when the key FILE is not ready (path + reason code only). The 8100
running-instance probe is read-only: the port table and one process start time
- never any file content, and never a write/signal/restart.
"""

import inspect
import json
import math
import os
import subprocess
import time
from functools import partial
from pathlib import Path

from console.operations import OperationError
from share.broker_protocol import RESULT_CODES
from share.management import BrokerError

# ---- fixed production defaults (deployment facts; injectable for tests) ----
DEFAULT_BROKER_STATUS_PATH = r"C:\ProgramData\UMShareRun\broker\broker-status.json"
DEFAULT_RUNNER_TIMEOUT_S = 10.0
DETAIL_MAX_CHARS = 200

_BROKER_LOADER = None

_CODE_UNWIRED = "share_privilege_unwired"
_CODE_UNWIRED_BROKER = "share_broker_unwired"
_CODE_EXTENSION_MISSING = "share_broker_extension_missing"
_CODE_LAYER_FAILED = "share_layer_failed"
_CODE_STATUS_UNKNOWN = "share_status_unknown"










def production_desired_stop_record(*, status_path=DEFAULT_BROKER_STATUS_PATH):
    """Read the broker's protected lifecycle record from broker-status.json
    (share/broker_server.py :495 mirrors it per cycle; :535-551 defines the
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
    protected record (console/share_actions.py :225-232 contract: bool|None).

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




def configure_broker_loader(loader):
    """Explicitly wire the share broker construction loader (E19-1-06).

    The production console no longer reaches back into ``usagesrv``: the old
    implicit ``import usagesrv`` inside ``_default_broker`` is gone. The
    host/composition root passes its own loader here before assembling the
    seams; the cloud composition passes a broker object directly to
    ``production_seams(broker=...)`` instead. ``loader()`` returns the
    deployment adapter, or None when the deployment config is absent/disabled
    (the honest share_broker_unwired refusal). Passing None resets this module
    to the unwired state.
    """
    global _BROKER_LOADER
    _BROKER_LOADER = loader


def _default_broker():
    """Build the deployment broker adapter through the EXPLICIT loader.

    The loader is configured by the host (``configure_broker_loader``) — this
    module never imports ``usagesrv``. With no loader configured the refusal
    is share_broker_unwired with the deployment pointer; a loader returning
    None (absent/disabled deployment config) keeps the same honest refusal.
    A corrupt config keeps the loader's fail-closed BrokerError (never a
    fallback)."""
    loader = _BROKER_LOADER
    if loader is None:
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "share 管理部署加载器未接线（broker loader 未配置）："
            "broker 适配器不可用；部署指针：share-management.json / "
            "um-cloud-deploy/README.md（部署授权包节 5）；旧 Windows 脚本 "
            "install-share-broker.ps1 已删除；"
            "宿主须先调用 configure_broker_loader()；不冒充可用")
    broker = loader()
    if broker is None:
        raise OperationError(
            _CODE_UNWIRED_BROKER,
            "share 管理部署配置缺席/未启用（share-management.json）："
            "broker 适配器不可用；部署指针：um-cloud-deploy/README.md"
            "（部署授权包节 5）；旧 Windows 脚本 install-share-broker.ps1 "
            "已删除；不冒充可用")
    return broker


class _ProductionBrokerOps:
    """Narrow restricted-gate wrapper over the deployment-authorized parts.

    Seam surface FIXED to the orchestration's restricted primitives
    (console/share_actions.py :106-121) — nothing else is exposed:

    * ``ensure_control_ready`` -> read-only broker status before opening;
    * ``start_host``/``stop_host``  -> broker ``start``/``host-stop`` verbs
      (share/management.py adapter: start re-confirms a running host via
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
      (share/management.py :66 class, :79 ``__init__(client)``; :123 method:
      drained/timed_out plus the explicit final-stop confirmation — the
      orchestration drain protocol shape at console/share_actions.py :519);
    * ``emergency_stop`` -> BrokerClientAdapter.emergency_stop
      (share/management.py :142: the explicit ``stopped`` flag contract).
    """

    def __init__(self, broker):
        self._broker = broker

    def ensure_control_ready(self):
        """Confirm the read-only channel before the open chain owns changes."""
        try:
            snapshot = self._broker.snapshot()
        except Exception:
            # Transport details may contain local configuration or secrets.
            snapshot = None
        if (isinstance(snapshot, dict) and snapshot.get("state") == "ok"
                and isinstance(snapshot.get("broker"), dict)):
            return
        code = snapshot.get("error_code") if isinstance(snapshot, dict) else None
        if not isinstance(code, str) or code not in RESULT_CODES or code == "ok":
            code = "unconfirmed"
        raise OperationError(
            "share_broker_unavailable",
            "受限后台响应未确认（code=%s）；尚未启动或登录 8100。"
            "请先确认 um-share-broker 服务与 broker 控制通道可用，"
            "再重新读取分层状态；任务及账号状态未知，"
            "不据此判为已停止或失效" % code)

    def start_host(self, explicit=False):
        try:
            # ``explicit`` is the operator's own start (it clears a persisted
            # stop intent); a legacy broker seam without the keyword keeps the
            # historical zero-argument start.
            start = self._broker.start
            try:
                parameters = inspect.signature(start).parameters
            except (TypeError, ValueError):
                start()
            else:
                if "explicit" in parameters:
                    start(explicit=explicit)
                else:
                    start()
            return True
        except BrokerError as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in RESULT_CODES and code != "ok":
                stage = getattr(exc, "stage", None)
                if stage not in ("broker_start", "broker_start_confirmation"):
                    stage = "broker_start"
                reasons = {
                    "access_denied": "权限不足",
                    "forbidden": "受限后台拒绝此操作",
                    "not_installed": "受限后台未部署或不可达",
                    "capability_missing": "受限后台缺少启动支持",
                    "deadline_exceeded": "启动确认超时",
                    "blocked": "后台条件未满足，启动被阻止",
                    "unconfirmed": "启动未获确认",
                }
                raise OperationError(
                    "share_broker_" + code,
                    "受限启动失败（stage=%s，code=%s）：%s；请读取分层诊断"
                    % (stage, code, reasons.get(code, "后台未确认启动"))) from exc
            # Legacy adapters have no structured metadata. Keep their old
            # boolean refusal contract without exposing arbitrary error text.
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

    def repair_limits(self):
        try:
            result = self._broker.repair_limits()
        except (BrokerError, OSError):
            raise OperationError("share_repair_failed", "隔离修复后台不可达，公网保持关闭")
        if not result.ok:
            code = result.error_code
            if code in ("bad_request", "capability_missing", "not_installed"):
                raise OperationError("share_repair_unavailable", "当前管理员后台未安装隔离修复功能，需要先更新部署")
            reasons = {
                "firewall_not_satisfied": "WFP 已重建，但防火墙仍不符合要求，请重新检查安全条件",
                "watchdog_not_boot_ready": "看门狗的开机监控尚未配置，需要管理员更新部署",
                "wfp_repair_failed": "WFP 规则重建或强核验失败",
                "repair_timeout": "隔离修复超时，结果未确认，请重新检查",
                "gateway_not_closed": "分享入口未确认停止，未重建规则",
                "stop_intent_unconfirmed": "后台未确认关闭意图，未重建规则",
                "public_not_closed": "公网仍有映射，未重建规则",
                "public_unconfirmed": "无法确认公网关闭，未重建规则",
            }
            if code in reasons:
                raise OperationError("share_repair_failed", reasons[code] + "；分享不会自动开启")
            raise OperationError("share_repair_failed", "隔离修复未确认成功，公网保持关闭")
        return result.result

    def host_evidence(self, check):
        """Restricted READ-ONLY host evidence (limits/faults/watchdog).

        The broker's SYSTEM-privilege verdict passes through VERBATIM: the
        frozen reply dict {check, verdict, detail, checked_at} on success —
        returned as a _HostEvidence whose TRUTHINESS is ``verdict is True``,
        so a FAILED limits verdict can never read as a pass — or None when
        the broker is unreachable/not deployed/rejects the verb (an old
        broker: bad_request): honest unconfirmed, never a fabricated verdict.
        The concrete object (watchdog script, run root, firewall rule, fault
        file) stays broker-side deployment configuration; the wire carries
        the fixed check enum only. The additive watchdog read retains a
        stable unavailability reason for old or inaccessible brokers."""
        try:
            result = self._broker.host_evidence(check)
        except Exception:  # noqa: BLE001 - transport failures stay honest
            if check == "watchdog":
                return _HostEvidence({"check": check, "verdict": None,
                    "detail": "watchdog_broker_unavailable", "checked_at": None})
            return None
        if not result.ok:
            if check == "watchdog":
                reason = ("watchdog_capability_missing" if result.error_code
                          in ("bad_request", "capability_missing")
                          else "watchdog_query_failed" if result.error_code
                          in ("access_denied", "forbidden")
                          else "watchdog_broker_unavailable")
                return _HostEvidence({"check": check, "verdict": None,
                    "detail": reason, "checked_at": None})
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


def _shorten(text):
    text = text or ""
    if len(text) <= DETAIL_MAX_CHARS:
        return text
    return text[:DETAIL_MAX_CHARS] + "..."


# ---- shared attribution key (C2 verified attribution) ----------------------
# The merged 8100 process reads ONE key file at startup through the
# SHARE_ATTRIBUTION_KEY_FILE env var (chat2api.py). The file lives in the
# DEPLOYED RunRoot run/ dir: ai-gateway/data carries an explicit DENY ACE
# for um_share (a key there is unreadable by 8100 and silently disables
# attribution), and run/live is removed and recreated by every deploy. A
# missing, unreadable or too-short file is NOT an error: it is the explicit
# "no attribution" state (a request the gateway cannot verify is recorded
# unattributed), which is why share_attribution_status() reports it loudly
# instead of letting it hide. Because 8100 reads the file only ONCE at startup,
# a create/rotate after the gateway started is NOT visible in a running instance
# until it is restarted: that is a DIFFERENT fact from file readiness and is
# reported separately (``instance_loaded``, inferred from the gateway process
# start time vs this file's mtime — see share_entry_instance_probe).
# Generation/rotation procedure: SHARING.md.
DEFAULT_SHARE_RUN_ROOT = r"C:\ProgramData\UMShareRun"  # deploy -RunRoot default
SHARE_RUN_ROOT_ENV = "CONSOLE_SHARE_RUN_ROOT"
SHARE_ATTRIBUTION_KEY_NAME = "share-attribution.key"
SHARE_ATTRIBUTION_KEY_FILE_ENV = "CONSOLE_SHARE_ATTRIBUTION_KEY_FILE"
SHARE_ATTRIBUTION_KEY_MIN_BYTES = 32   # = share_entry/chat2api constant


def _share_run_root():
    """Deployed RunRoot (retired Windows deploy script -RunRoot default): the
    run/ dir
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
    deploy prepares (the retired Windows deploy script :461-464) and the SAME
    path the 8100 wrapper gets through SHARE_ATTRIBUTION_KEY_FILE. The override
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
    does NOT mean the running 8100 instance loaded this content, or that the
    um_share identity can read the file (each of those is a different fact —
    see share_attribution_status).
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
# (chat2api.py :1247-1251), so "the file is readable now" says NOTHING
# about the process already running: after a create/rotate it may still hold the
# previous content. The only way to tell the two apart without touching the
# process is its START TIME vs the key file mtime — an INFERENCE, not a proof
# (a content change that preserves mtime, or a clock jump, defeats it). That is
# why the read surface carries the rule and the raw evidence next to the verdict,
# and why a process that cannot be found is "unknown" — never "loaded".
SHARE_GATEWAY_PORT = 8100          # chat2api.py --port default (:1193)
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
# The 8100 share gateway runs as DAICNT\um_share through the deployed
# wrapper, so its image is the um-share python: a listener with any other
# image name is NOT assumed to be the gateway (2026-09-26 review F-16).
SHARE_GATEWAY_IMAGE_NAMES = ("um-share-python.exe",)
_CIM_IMAGE_NAME_SCRIPT = (
    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d') | "
    "ForEach-Object { $_.Name }")
# The runtime fact that decides "request attribution verified" — NOT observed
# by this read surface (the merged 8100 process writes it into each ledger row;
# chat2api.py returns the reason "verified").
_LEDGER_ATTRIBUTION_FACT = "backend_ledger_row_attribution_verified"


def _default_bytes_runner(args, timeout):
    """Controlled argument-array runner, no shell, BYTES stdout.

    Localized Windows output (netstat, PowerShell) is NOT valid UTF-8 on this
    machine: a utf-8 text decode raises UnicodeDecodeError in the reader
    thread and leaves ``stdout`` EMPTY, which would read as "the query
    succeeded and nobody listens" — a fabricated fact. Bytes plus
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
        proc = run(["pwsh", "-NoProfile", "-Command",
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


def share_entry_instance_probe(port=None, runner=None):
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
        proc = run(["pwsh", "-NoProfile", "-Command",
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
       instance loaded this content (it reads the file once at startup), or that
       the um_share identity can read the file (this process runs as the
       console user).
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
       the 8100 ledger (``ledger_file``).

    ``key_chars`` is the stripped length ONLY — never any content. The secret
    never leaves this function: it is not returned, printed, logged or compared.
    The gateway probe runs ONLY when the file is ready (there is no mtime to
    compare otherwise, and a pointless read-only query is not run).
    """
    target = path or share_attribution_key_path()
    file_ready, reason, key_chars, key_mtime = _attribution_file_fact(target)
    probe = None
    if file_ready is True:
        probe = (gateway_probe or share_entry_instance_probe)()
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
# the wave-1 BrokerClient methods (share/broker_client.py) instead of raw
# action strings. `_HostEvidence` is referenced by the methods above and
# resolves at call time.




class _HostEvidence(dict):
    """The frozen host-evidence reply ``{check, verdict, detail, checked_at}``
    whose TRUTHINESS is ``verdict is True``.

    The security gate consumes host evidence through a truthy check in the
    orchestration: a plain dict
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
            "未运行；部署指针：um-cloud-deploy/README.md；旧 Windows "
            "脚本 install-share-broker.ps1 已删除；"
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




def production_gateway_restart(budget_s=None, *, broker=None):
    """A9-4: ONE bounded 8100 single-layer restart through the restricted
    broker host path — NEVER stop-then-start through the console (hard boundary #6).

    The broker reply is authoritative: ``restarted`` + ``stopped_confirmed``
    + ``running_confirmed`` are ALL required, otherwise the action fails
    honestly (share_entry_restart_failed). An old broker (bad_request)
    maps to share_broker_extension_missing; blocked (not running / identity
    unknown / already stopped) maps to share_entry_restart_failed — the
    restart never doubles as a way to START a stopped gateway.

    ``budget_s=None`` means the protocol default
    (GATEWAY_RESTART_BUDGET_DEFAULT_S), imported lazily so the module
    header stays byte-identical for the citation pins."""
    from share.broker_protocol import (COMPONENT_SHARE_GATEWAY,
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
        blocked_code="share_entry_restart_failed",
        refused_code="share_entry_restart_failed")
    if not (payload.get("restarted") is True
            and payload.get("stopped_confirmed") is True
            and payload.get("running_confirmed") is True):
        raise OperationError(
            "share_entry_restart_failed",
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




























def production_keys_confirmed(manager=None):
    """R8-A7 open precondition: >=1 enabled share key and the published
    config in sync. Returns True/False, or None when unknown (manager
    unavailable) — never a fabricated True. Uses the caller-provided
    manager when given; lazily builds one over the same deployment broker
    otherwise (the class-level recover guard keeps this per-path safe)."""
    try:
        if manager is None:
            broker = _default_broker()
            from share.management import ShareManager
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
            from share.management import ShareManager
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
                     status_path=None, keys_manager=None, cloud=False,
                     linux_state_dir=None):
    """Assemble the console_share_actions seam dict, key-by-key.

    Keys ARE the orchestration's injection seam globals exactly
    (console/share_actions.py :40-90), so wiring is a direct
    ``setattr(console_share_actions, key, value)`` per entry:

    * _BROKER_OPS is the restricted broker gate;
    * _DESIRED_STOP_READER reads the broker's protected lifecycle record
      (injectable status_path, default the deployment path);
    * _MAINTENANCE_BEGIN / _MAINTENANCE_END / _MAINTENANCE_STATUS /
      _GATEWAY_RESTART are the wave-1 BrokerClient maintenance verbs
      bound to the SAME broker as the gate (no raw action strings here);
    * _KEYS_CONFIRMED / _KEYS_DIAGNOSIS are the key preconditions.

    The retired Windows TS/Funnel seams are NOT delivered any more (#33):
    the cloud deployment has no Tailscale/Funnel layer. The broker gate is
    assembled EAGERLY (an absent deployment config raises
    share_broker_unwired at assembly — the same honest refusal the
    production wiring already propagates); the observation seams are lazy
    partials and never run at assembly time.
    """
    if broker is None:
        broker = _default_broker()
    ops = production_broker_ops(broker=broker)
    reader_bind = {"status_path": status_path} if status_path is not None         else {}
    seams = {
        "_BROKER_OPS": ops,
        "_DESIRED_STOP_READER": partial(production_desired_stop_reader,
                                        **reader_bind),
        "_KEYS_CONFIRMED": (partial(production_keys_confirmed,
                                    manager=keys_manager)
                            if keys_manager is not None
                            else production_keys_confirmed),
        "_KEYS_DIAGNOSIS": (partial(production_keys_diagnosis,
                                    manager=keys_manager)
                            if keys_manager is not None
                            else production_keys_diagnosis),
        "_MAINTENANCE_BEGIN": partial(production_maintenance_begin,
                                      broker=broker),
        "_MAINTENANCE_END": partial(production_maintenance_end,
                                    broker=broker),
        "_MAINTENANCE_STATUS": partial(production_maintenance_status,
                                      broker=broker),
        "_GATEWAY_RESTART": partial(production_gateway_restart,
                                   broker=broker),
    }
    if cloud:
        # Explicit cloud (Linux) role: the persistent stop intent is the
        # Linux file the pid-file host ops write.
        seams["_DESIRED_STOP_READER"] = partial(
            production_linux_desired_stop_reader, state_dir=linux_state_dir)
    return seams


# ------------------------------------------------- Linux stop intent (cloud)
# Kept at the END of the module on purpose: test_share_gate_citations.py pins
# the line numbers of _paths/_child_env and their comments, so definitions
# added before them would silently re-point every citation.
# On the Linux deployment the broker's broker-status.json does not exist; the
# persistent operator/budget stop intent is the ONE file ``LinuxHostOps`` writes
# BEFORE it signals the share host. Reading it here never writes, never starts a
# process and never clears the intent: a watchdog start can never resurrect a
# deliberate stop.
LINUX_SHARE_HOST_STATE_DIR_ENV = "UM_SHARE_HOST_STATE_DIR"
DEFAULT_LINUX_SHARE_HOST_STATE_DIR = "/run/um-share"
LINUX_SHARE_HOST_INTENT_FILE = "share-host-desired.json"


def production_linux_desired_stop_record(*, state_dir=None):
    """Read the Linux persistent stop intent, or None when unreadable.

    Returns ``{"state": "running"|"stopped", "reason": str|None}``. An absent,
    unreadable or malformed record is None (no authority here) — never a
    fabricated stop/run state.
    """
    root = state_dir or os.environ.get(LINUX_SHARE_HOST_STATE_DIR_ENV) \
        or DEFAULT_LINUX_SHARE_HOST_STATE_DIR
    path = os.path.join(root, LINUX_SHARE_HOST_INTENT_FILE)
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return None
    state = record.get("state")
    if state not in ("running", "stopped"):
        return None
    reason = record.get("reason")
    return {"state": state,
            "reason": reason if isinstance(reason, str) else None}


def production_linux_desired_stop_reader(*, state_dir=None):
    """The orchestration _DESIRED_STOP_READER seam over the Linux intent:
    stopped -> True (a stop intent is in force), running -> False, no record
    -> None (no authority; the honest default). Read-only."""
    record = production_linux_desired_stop_record(state_dir=state_dir)
    if record is None:
        return None
    return record["state"] == "stopped"
