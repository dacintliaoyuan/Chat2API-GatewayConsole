r"""share_broker_ops.py — real OS adapters for the broker host (D4 prereq).

Three adapters the SYSTEM broker host entry (share_broker_server.py) wires
together; each keeps product logic reviewable offline by injecting its OS
boundary:

* ``WindowsHostOps`` — start/stop/verify the SHARE HOST scheduled task via
  schtasks with controlled LIST arguments (no shell, no interpolation into a
  command line). ``verify_running``/``verify_closed`` are explicit states
  parsed from the task query, never inferred from absence of errors.
* ``make_instance_call`` — the coordinator's instance_call over the REAL
  instance channel: a named-pipe client to
  ``\\.\pipe\um-share-public-instance-<instance_id>`` signed with the shared
  instance secret (two channels / two secrets / two whitelists, §27.2-3).
* ``WindowsInstanceVerifier`` — the H6 peer-identity path: OpenProcess +
  QueryFullProcessImageName + process-token owner + creation time. The
  DECISION logic (fail-closed on unopenable process, image mismatch, owner
  mismatch) is offline-testable with an injected facade; the real OS
  behavior under SYSTEM stays D4.
* ``IcaclsAclChecker`` — the explicit ACL validator for the projection
  writer (R31-6 requires injection; None is refused at the writer). It
  parses ``icacls`` output and fails closed on mismatch, failure or
  timeout.

Nothing here touches the live RunRoot when imported; privileged actions run
only inside the authorized L2/D4 experiments.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

from share_broker_client import (BrokerClient, NamedPipeBrokerTransport)
from share_broker_protocol import DEFAULT_DEADLINE_SECONDS

INSTANCE_PIPE_PREFIX = r"\\.\pipe\um-share-public-instance-"

# Fault-record shape the broker-side bounded tail reader understands
# (contract §8.1: share-faults.jsonl, one JSON object per line). The phase
# enum is duplicated here deliberately: the broker runs in its OWN SYSTEM
# process and must never import the share gateway module (privilege
# separation) - an unknown phase line is skipped, never counted.
FAULT_PHASES = ("auth", "config", "body", "limit", "upstream", "stream")
FAULT_RECORD_KEYS = ("at", "phase", "status", "code", "upstream_status",
                     "exception_type", "duration_ms", "request_id",
                     "stream_started")
# Bounded tail read: the last 64 KiB / the last 50 lines (contract §2.3).
FAULTS_TAIL_BYTES = 64 * 1024
FAULTS_TAIL_LINES = 50


class WindowsHostOps:
    """start/stop/verify the share host task with controlled schtasks
    arguments. ``runner(args, timeout)`` is injectable for offline tests;
    production runs ``subprocess.run`` without a shell and propagates real
    exit codes (a non-zero exit is a False, never a pretend success).

    R44-3 evidence contract: the task state is an AUXILIARY signal only.
    Share-identity evidence is (a) processes whose COMMAND LINE actually
    runs the share gateway (CIM query filtered by the image name, with
    same-image NON-target processes - the broker server and the guard -
    excluded) and (b) a TCP LISTENING socket on the share port (netstat).
    Any failed or unparseable query returns None (explicit unconfirmed);
    an EMPTY result from a SUCCESSFUL query is the only empty evidence.
    Full process identity (image path/owner/creation) and OS-level
    listener semantics stay L2/D4.
    """

    _SHARE_MARKER = "share_gateway.py"
    _BROKER_MARKER = "share_broker_server.py"
    _GUARD_MARKER = "share_guard.py"

    def __init__(self, task_name, runner=None, timeout=10.0,
                 share_image="um-share-python.exe", share_port=8100,
                 expected_image_path=None, expected_owner=None,
                 run_root=r"C:\ProgramData\AIGatewayPublicShareRun",
                 watchdog_script=None,
                 firewall_rule="um-share-outbound-block",
                 share_faults_file=None, clock=time.monotonic):
        if not task_name or not isinstance(task_name, str):
            raise ValueError("task_name must be a non-empty string")
        self.task_name = task_name
        self.share_image = share_image
        self.share_port = int(share_port)
        # R49-3: full identity is decided HERE (host evidence), not only on
        # the pipe connection - the expected FULL image path and the
        # DOMAIN\user owner come from deployment config; None keeps the
        # field RECORDED but UNJUDGED (L2 calibrates real owner shapes).
        self.expected_image_path = expected_image_path
        self.expected_owner = expected_owner
        # Host-side READ-ONLY evidence (contract §2.5): the limits check
        # reuses the watchdog's OWN Get-LimitsState through a dot-source
        # (-FunctionsOnly, no loop, no dir creation) and the fault evidence
        # is a BOUNDED tail read of the share gateway's fault file. Every
        # one of these is deployment configuration; None means the evidence
        # is honestly UNavailable (never a fabricated verdict).
        self.run_root = run_root
        self.watchdog_script = watchdog_script
        self.firewall_rule = firewall_rule or "um-share-outbound-block"
        self.share_faults_file = share_faults_file
        self._runner = runner or self._default_runner
        self._timeout = timeout
        # C1: the caller's REMAINING budget is passed down to every command
        # (``budget_s``) and shared by the CONSECUTIVE queries of one
        # evidence read, so a slow task run or a slow query can never make
        # the whole verb overrun the caller's exchange deadline.
        self._clock = clock

    @staticmethod
    def _default_runner(args, timeout):
        return subprocess.run(args, shell=False, capture_output=True,
                              text=True, timeout=timeout)

    def _deadline(self, budget_s):
        """Absolute deadline for one verb, or None when no budget was given."""
        if budget_s is None:
            return None
        try:
            budget = float(budget_s)
        except (TypeError, ValueError):
            return None
        if budget != budget or budget <= 0:      # NaN or already spent
            return self._clock()
        return self._clock() + budget

    def _remaining(self, deadline):
        """Seconds left before ``deadline`` (None = unbounded); <= 0 = spent."""
        if deadline is None:
            return None
        return deadline - self._clock()

    def _command_timeout(self, deadline):
        """Per-command timeout: the configured command bound capped by the
        budget left when the command starts (zero immediately expires)."""
        remaining = self._remaining(deadline)
        if remaining is None:
            return self._timeout
        return max(0.0, min(self._timeout, remaining))

    def start_host(self, budget_s=None):
        deadline = self._deadline(budget_s)
        if deadline is not None and self._remaining(deadline) <= 0:
            return False
        return self._schtasks(["/Run"], deadline) == 0

    def stop_host(self, budget_s=None):
        deadline = self._deadline(budget_s)
        if deadline is not None and self._remaining(deadline) <= 0:
            return False
        return self._schtasks(["/End"], deadline) == 0

    def verify_running(self, budget_s=None):
        # One query per evidence source; running needs task Running AND a
        # share-gateway process AND its listener on the share port - any
        # unresolvable evidence is None (never a pretend success). The three
        # CONSECUTIVE queries share the ONE remaining budget: a query that
        # cannot finish inside it is incomplete evidence (None), never a
        # partial verdict.
        deadline = self._deadline(budget_s)
        state = self._query_state(deadline)
        if state is None:
            return None
        if state != "Running":
            return False
        procs = self._share_processes(deadline)
        if procs is None:
            return None
        if not procs:
            return False
        listeners = self._listening_pids(deadline)
        if listeners is None:
            return None
        return bool(set(procs) & listeners)

    def verify_closed(self, budget_s=None):
        # R41-3/R44-3: closed needs ALL of: task not Running, NO share
        # gateway process (broker/guard same-image processes do NOT count),
        # and NO listener on the share port (an occupied port means the
        # public entry is not closed regardless of owner). Any unresolvable
        # evidence returns None - the coordinator treats None as never
        # confirmed. The three queries share the remaining budget like
        # ``verify_running``.
        deadline = self._deadline(budget_s)
        state = self._query_state(deadline)
        if state is None:
            return None
        if state == "Running":
            return False
        procs = self._share_processes(deadline)
        if procs is None:
            return None
        if procs:
            return False
        listeners = self._listening_pids(deadline)
        if listeners is None:
            return None
        if listeners:
            return False
        return True

    def _share_processes(self, deadline=None):
        """Full share-process evidence records (R46.6-B): one CIM query
        with ProcessId + CommandLine + ExecutablePath + CreationDate. A
        row counts as the SHARE only with BOTH the gateway entry in the
        command line AND a full executable path (the image-name-only
        match is not trusted evidence; the broker server and the guard
        run the same image and are excluded by their entry). Any row
        without a usable PID, command line or image path makes the WHOLE
        evidence unusable (None = unconfirmed, never partially trusted);
        a succeeded query with no rows is the explicit empty evidence.
        Owner-SID binding and creation-time identity reuse stay with the
        per-connection verifier; this host evidence records the fields,
        L2 calibrates their real output shapes."""
        script = ("$ErrorActionPreference = 'Stop'; "
                  "try { Get-CimInstance Win32_Process -Filter \"Name='"
                  + self.share_image + "'\" | ForEach-Object { "
                  "try { $o = Invoke-CimMethod -InputObject $_ "
                  "-MethodName GetOwner -ErrorAction Stop } catch "
                  "{ $o = $null }; [pscustomobject]@{ ProcessId = "
                  "$_.ProcessId; CommandLine = $_.CommandLine; "
                  "ExecutablePath = $_.ExecutablePath; CreationDate = "
                  "$_.CreationDate; OwnerDomain = $o.Domain; OwnerUser = "
                  "$o.User } } | ConvertTo-Json -Compress } catch { "
                  "exit 1 }")
        if deadline is not None and self._remaining(deadline) <= 0:
            return None
        try:
            proc = self._runner(["powershell", "-NoProfile", "-Command",
                                 script], self._command_timeout(deadline))
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        stdout = (proc.stdout or "").strip()
        if not stdout:
            return []                  # succeeded query, no rows at all
        try:
            payload = json.loads(stdout)
        except ValueError:
            return None
        rows = payload if isinstance(payload, list) else [payload]
        pids = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            pid = row.get("ProcessId")
            cmdline = row.get("CommandLine")
            image_path = row.get("ExecutablePath")
            creation = row.get("CreationDate")
            owner_domain = row.get("OwnerDomain")
            owner_user = row.get("OwnerUser")
            # R49-3: the trusted match needs ALL fields. Any missing field
            # (including CreationDate and owner) makes the WHOLE query
            # unusable evidence (None) - never partially trusted; this is
            # the same "full record" contract the verifier enforces per
            # connection, now also on the host evidence path.
            if not isinstance(pid, int) or pid <= 0 \
                    or not isinstance(cmdline, str) \
                    or not isinstance(image_path, str) or not image_path \
                    or not isinstance(creation, str) or not creation \
                    or not isinstance(owner_domain, str) \
                    or not owner_domain \
                    or not isinstance(owner_user, str) or not owner_user:
                return None
            if self.expected_image_path and \
                    image_path.lower() != str(
                        self.expected_image_path).lower():
                continue               # full image path != expected: not target
            owner = owner_domain + chr(92) + owner_user
            if self.expected_owner and \
                    owner.lower() != str(self.expected_owner).lower():
                continue               # owner != expected: not the target
            if self._BROKER_MARKER in cmdline or self._GUARD_MARKER in cmdline:
                continue               # same-image non-target process
            if self._SHARE_MARKER in cmdline:
                pids.append(pid)
        return pids

    def _listening_pids(self, deadline=None):
        """PIDs holding a TCP LISTENING socket on the share port (netstat
        -ano; state words are not localized). None = evidence failure; an
        empty set from a succeeded query is explicit no-listener evidence;
        an unparsable LISTENING row makes the evidence unusable."""
        if deadline is not None and self._remaining(deadline) <= 0:
            return None
        try:
            proc = self._runner(["netstat", "-ano", "-p", "tcp"],
                                self._command_timeout(deadline))
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        pids = set()
        want_port = str(self.share_port)
        known_states = {"LISTENING", "ESTABLISHED", "SYN_SENT", "SYN_RECV",
                        "FIN_WAIT_1", "FIN_WAIT_2", "TIME_WAIT",
                        "CLOSE_WAIT", "LAST_ACK", "CLOSING"}
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if not parts:
                continue               # blank separators
            if len(parts) < 2:
                # A damaged bare row is only evidence-failure when it
                # claims to be TCP; other short rows are noise.
                if parts[0] == "TCP":
                    return None
                continue
            if parts[0] != "TCP":
                continue               # header lines / UDP rows
            if len(parts) < 5:
                return None            # damaged TCP row (R49-3)
            local_port = parts[1].rsplit(":", 1)[-1]
            if local_port != want_port:
                continue               # unrelated connection (81000 etc.)
            state = parts[3].upper()
            if state not in known_states:
                return None            # unknown/damaged state on OUR port
            if state != "LISTENING":
                # ESTABLISHED/TIME_WAIT/... are legal connection states,
                # not listener evidence either way - skipped legally.
                continue
            try:
                pids.add(int(parts[4]))
            except ValueError:
                return None
        return pids

    def status(self):
        state = self._query_state()
        # Additive host evidence (contract §2.5): both are READ-ONLY and both
        # answer None when the deployment did not configure the evidence
        # source - honest unavailability, never a fabricated verdict.
        return {"host": "scheduled-task", "task": self.task_name,
                "state": state,
                "host_limits": self.verify_limits(),
                "share_faults": self.read_faults()}

    # ------------------------------------------------------ read-only evidence
    @staticmethod
    def _ps_quote(value):
        """Single-quoted PowerShell literal (defense in depth): deployment
        config values are never interpolated raw into a command line."""
        return "'" + str(value).replace("'", "''") + "'"

    def verify_limits(self) -> bool | None:
        """Read-only limits verdict (True/False/None) reusing the watchdog's
        OWN ``Get-LimitsState`` (contract §2.5/§17.12): the watchdog is
        dot-sourced with ``-FunctionsOnly`` (functions only, no loop, no dir
        creation, no task) and asked for the WFP-strong + firewall-scope
        limits.

        * True  - the strong limits check re-confirmed the permit;
        * False - the strong check ran and FAILED (limit loss);
        * None  - the evidence is UNRESOLVABLE (watchdog/fault/run-root
          unwired, launch failure, unparsable output, or the watchdog's own
          ``detection-failure(...)``: the check could not name the permit it
          verifies, e.g. backend-port.txt missing) - never folded into
          True/False.
        """
        if not self.watchdog_script or not self.run_root:
            return None
        app_exe = self.expected_image_path or ""
        wfp_helper = os.path.join(str(self.run_root), "wfp_helper.ps1")
        script = (
            "$ErrorActionPreference = 'Continue'; "
            ". " + self._ps_quote(self.watchdog_script)
            + " -FunctionsOnly"
            + " -RunRoot " + self._ps_quote(self.run_root)
            + " -AppExe " + self._ps_quote(app_exe)
            + " -FirewallRule " + self._ps_quote(self.firewall_rule)
            + " -WfpHelper " + self._ps_quote(wfp_helper)
            + "; $r = Get-LimitsState; "
            + "if ($r[0]) { Write-Output 'LIMITS=OK' } "
            + "else { Write-Output 'LIMITS=FAIL'; "
            + "Write-Output ('REASON=' + $r[1]) }")
        try:
            proc = self._runner(["powershell", "-NoProfile",
                                 "-ExecutionPolicy", "Bypass",
                                 "-Command", script], self._timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        lines = (getattr(proc, "stdout", "") or "").splitlines()
        verdict = None
        reason = ""
        for line in lines:
            line = line.strip()
            if line == "LIMITS=OK":
                verdict = True
            elif line == "LIMITS=FAIL":
                verdict = False
            elif line.startswith("REASON="):
                reason = line[len("REASON="):]
        if verdict is None:
            return None
        if verdict is False and reason.startswith("detection-failure"):
            # The watchdog could not RUN the strong check (no permit port /
            # no helper): the evidence is unavailable, not a limit loss.
            return None
        return verdict

    def read_faults(self) -> dict | None:
        """Bounded tail read of the share gateway's fault file (contract
        §2.3/§8.3): the last 64 KiB / the last 50 lines, returning
        ``{last_fault, counts_by_phase, file{present,size,mtime}}``.

        A missing/unreadable file is None (honest unavailability: an old
        deployment without --fault-log never folds into "no faults"); a
        readable file with no parsable record is the explicit empty
        evidence (all-zero counts, ``last_fault: null``)."""
        path = self.share_faults_file
        if not path:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        try:
            with open(path, "rb") as handle:
                size = stat.st_size
                if size > FAULTS_TAIL_BYTES:
                    handle.seek(size - FAULTS_TAIL_BYTES)
                data = handle.read(FAULTS_TAIL_BYTES)
        except OSError:
            return None
        counts = {phase: 0 for phase in FAULT_PHASES}
        last_fault = None
        for line in data.decode("utf-8", errors="replace").splitlines()[
                -FAULTS_TAIL_LINES:]:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            phase = record.get("phase")
            if phase not in counts:
                continue          # unknown phase: skipped, never counted
            counts[phase] += 1
            last_fault = {key: record.get(key) for key in FAULT_RECORD_KEYS}
        return {"last_fault": last_fault,
                "counts_by_phase": counts,
                "file": {"present": True, "size": size,
                         "mtime": stat.st_mtime}}

    def _schtasks(self, action, deadline=None):
        if deadline is not None and self._remaining(deadline) <= 0:
            return 1
        try:
            proc = self._runner(["schtasks"] + list(action)
                                + ["/TN", self.task_name],
                                self._command_timeout(deadline))
        except (OSError, subprocess.SubprocessError):
            return 1
        return proc.returncode

    def _query_state(self, deadline=None):
        if deadline is not None and self._remaining(deadline) <= 0:
            return None
        try:
            proc = self._runner(["schtasks", "/Query", "/TN", self.task_name,
                                 "/FO", "CSV", "/NH"],
                                self._command_timeout(deadline))
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return self._parse_state(getattr(proc, "stdout", "") or "")

    # task status the query emits is LOCALIZED (zh-CN host: Running emits
    # "正在运行", Ready "就绪", Disabled "已禁用"): the state consumers decide
    # on CANONICAL English tokens ("Running"/"Ready"/...), so the raw CSV token
    # is normalized here once. An unrecognized token is returned unchanged
    # (never folded into a success - a foreign state stays unmapped and
    # fail-closed).
    _STATE_NORMALIZE = {
        "running": "Running", "正在运行": "Running", "运行中": "Running",
        "ready": "Ready", "就绪": "Ready",
        "disabled": "Disabled", "已禁用": "Disabled",
        "unknown": "Unknown", "未知": "Unknown",
        "queued": "Queued", "正在排队": "Queued", "已排队": "Queued",
    }

    @staticmethod
    def _parse_state(stdout):
        """The LAST CSV column of the task query row is the status; the token
        is normalized to the canonical English state (localization-free) via
        _STATE_NORMALIZE. An unparseable row is explicit evidence failure,
        not "not running"."""
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith('"'):
                continue
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                raw = parts[-1]
                return WindowsHostOps._STATE_NORMALIZE.get(
                    raw, raw if raw else None)
        return None


class TailscaleServiceOps:
    """Restricted TS service primitive for the SYSTEM broker (the whitelist
    extension the console TS service seam drives over the management
    channel).

    Same discipline as the WindowsHostOps schtasks gate: controlled
    argument-array ``sc.exe`` calls ONLY (no shell, no interpolation), the
    real exit code is checked, and the VERDICT is a re-queried service
    state - never an assumed success. The service name is the CLASS
    CONSTANT literal ``SERVICE_NAME`` (``"Tailscale"``, the protocol's
    TS_SERVICE_NAME): it is NOT a parameter, so no caller - management
    request, page or config - can aim the primitive at another service.

    * ``status()`` - read-only ``sc.exe query`` (no privilege needed);
      RUNNING->"running", STOPPED->"stopped", exit 1060 (service absent)
      ->"absent", other pending/paused states as their lowercase token;
      a failed/unparseable query is None (unconfirmed).
    * ``start``/``stop`` - the state change, then the state MUST be
      re-queried to the desired value; rc 1056/1062 (already in the
      desired state) are accepted as the idempotency codes, every other
      non-zero rc and any launch failure is an explicit False. The verdict
      is ALWAYS the re-query result.
    * ``last_control_rc`` - the REAL exit code of the last control command
      (None before any control and when the command could not run at all):
      the broker reads it to tell a PERMANENT refusal (rc 5 access denied,
      R13-T: 权限不足 must be reported distinctly, never folded into the
      pending-confirmation shape) apart from an in-flight transition.
    """

    SERVICE_NAME = "Tailscale"

    # sc.exe exit code for ERROR_ACCESS_DENIED on a control command (R13-T:
    # the distinct 权限不足 report needs the rc, not just a False verdict).
    RC_ACCESS_DENIED = 5

    # sc.exe STATE numeric codes (SERVICE_STATUS enum; locale-stable - the
    # numeric code is the verdict, the English token is only a fallback).
    _STATE_CODES = {1: "stopped", 2: "start_pending", 3: "stop_pending",
                    4: "running", 5: "continue_pending", 6: "pause_pending",
                    7: "paused"}
    _STATE_TOKENS = {"RUNNING": "running", "STOPPED": "stopped",
                     "PAUSED": "paused", "START_PENDING": "start_pending",
                     "STOP_PENDING": "stop_pending"}
    _STATE_LINE_RE = re.compile(r"\bSTATE\s*:\s*(\d+)\s*([A-Za-z_]+)?")
    _RC_SERVICE_MISSING = 1060
    _RC_ALREADY_RUNNING = 1056
    _RC_NOT_ACTIVE = 1062

    def __init__(self, runner=None, timeout=10.0):
        self._runner = runner or self._default_runner
        self._timeout = timeout
        self.last_control_rc = None

    @staticmethod
    def _default_runner(args, timeout):
        return subprocess.run(args, shell=False, capture_output=True,
                              text=True, timeout=timeout)

    def status(self):
        """"running"/"stopped"/"absent"/other service token, None when the
        query failed or is unparseable (never folded into a state)."""
        try:
            proc = self._runner(["sc.exe", "query", self.SERVICE_NAME],
                                self._timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == self._RC_SERVICE_MISSING:
            return "absent"
        if proc.returncode != 0:
            return None
        return self._parse_state(getattr(proc, "stdout", "") or "")

    def start(self):
        """sc.exe start + state re-check; True only when the service is
        re-confirmed RUNNING (rc 1056 = already running is accepted)."""
        rc = self._control("start")
        if rc is None or rc not in (0, self._RC_ALREADY_RUNNING):
            return False
        return self.status() == "running"

    def stop(self):
        """sc.exe stop + state re-check; True only when the service is
        re-confirmed stopped (rc 1062 = already stopped is accepted)."""
        rc = self._control("stop")
        if rc is None or rc not in (0, self._RC_NOT_ACTIVE):
            return False
        return self.status() == "stopped"

    def _control(self, action):
        try:
            proc = self._runner(["sc.exe", action, self.SERVICE_NAME],
                                self._timeout)
        except (OSError, subprocess.SubprocessError):
            self.last_control_rc = None
            return None
        self.last_control_rc = proc.returncode
        return proc.returncode

    @classmethod
    def _parse_state(cls, stdout):
        """Parse the STATE line of ``sc.exe query`` output. The numeric
        code (locale-stable SERVICE_STATUS enum) decides; the English token
        is the fallback. Anything unparsable is None - explicit
        unconfirmed, never a pretend state."""
        match = cls._STATE_LINE_RE.search(stdout)
        if not match:
            return None
        code = match.group(1)
        token = match.group(2) or ""
        state = cls._STATE_CODES.get(int(code))
        if state is None:
            state = cls._STATE_TOKENS.get(token.upper())
        return state


class _KeyFileSecret:
    """Shared helper: load a controlled secret file, never logging it. Any
    unreadable/missing/malformed file fails closed with an explicit
    ValueError - never a fallback key."""

    @staticmethod
    def read(path):
        try:
            data = Path(path).read_bytes().strip()
        except OSError as exc:
            raise ValueError(f"secret file unreadable: "
                             f"{Path(path).name}") from exc
        if not data or len(data) > 128:
            raise ValueError(f"secret file is missing or malformed: "
                             f"{Path(path).name}")
        return data.decode("utf-8")


def make_instance_call(secret_file,
                       pipe_prefix=INSTANCE_PIPE_PREFIX,
                       timeout_seconds=DEFAULT_DEADLINE_SECONDS,
                       kernel_factory=None,
                       verifier=None,
                       expect_image=None,
                       expect_owner_sid=None,
                       creation_by_instance=None,
                       on_verified=None,
                       diag_event=None):
    """Build the coordinator-compatible ``instance_call`` over the real
    instance pipe, wired into the TRUST CHAIN (R41-2):

    * the request frame carries the instance_id (the signed key set
      includes it - a pipe name is transport addressing, not identity);
    * after connecting, the peer's SERVER process identity is queried from
      the pipe itself and verified (image path / owner SID / creation
      time); a failed or mismatched identity raises BEFORE any request is
      sent — a forged or writable status file can never make the broker
      serve an instance;
    * a verified connection notifies ``on_verified(instance_id, evidence)``
      (generation refresh) and records the peer's creation time as the
      expectation for later connections (PID reuse detection).

    ``diag_event(stage, ok=, error_code=, exception_type=, detail=,
    instance_id=)`` is the OPTIONAL sanitized diagnostic hook (maintainer
    direction 2026-09-17): stage names connect/peer-identity/identity/
    exchange; only reason CODES and exception CLASS names are recorded —
    never secrets, signature material or payloads.

    ``kernel_factory`` injectable exists ONLY for offline tests."""
    secret = _KeyFileSecret.read(secret_file)
    creation_map = creation_by_instance if creation_by_instance is not None \
        else {}

    def call(action, fields, instance_id, budget_s=None):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id must be a non-empty string")
        # R-WIRE-DRAIN: the caller's REMAINING budget bounds the WHOLE
        # exchange INCLUDING the single transient retry - an exchange that cannot
        # finish inside it is refused honestly instead of eating the caller's
        # deadline. ``None`` keeps the configured per-exchange bound (the old
        # behaviour, byte for byte).
        exchange_deadline = None
        if budget_s is not None:
            try:
                budget = float(budget_s)
            except (TypeError, ValueError):
                raise ValueError("budget_s must be finite") from None
            if isinstance(budget_s, bool) or not math.isfinite(budget):
                raise ValueError("budget_s must be finite")
            if budget <= 0:
                return {"ok": False, "error_code": "deadline_exceeded",
                        "result": {"detail": "instance exchange budget "
                                             "exhausted"}}
            exchange_deadline = time.monotonic() + budget

        def remaining_budget():
            if exchange_deadline is None:
                return None
            return exchange_deadline - time.monotonic()
        pipe_name = f"{pipe_prefix}{instance_id}"
        kernel = kernel_factory() if kernel_factory is not None else None
        # R48-1/instance channel: the instance server owns ONE pipe instance
        # per connection cycle and releases it before re-creating, so an
        # immediate back-to-back exchange can land in the same serving gap the
        # management channel retries (233/232/109 on write). A caller-owned
        # transport must NEVER reconnect inside send_all (R41-2 identity was
        # verified on ITS handle), so the retry lives HERE: the WHOLE exchange
        # (fresh open -> re-verify -> signed call) is retried once on a
        # transient not_installed result. Every retry re-establishes and re-
        # verifies identity; the verification-before-request rule is intact.
        last_result = None
        for attempt in range(2):
            remaining = remaining_budget()
            if remaining is not None and remaining <= 0:
                return {
                    "ok": False, "error_code": "deadline_exceeded",
                    "result": {"detail": "instance exchange budget "
                                         "exhausted"}}
            transport = NamedPipeBrokerTransport(pipe_name, kernel=kernel)
            client = BrokerClient(lambda: transport, secret,
                                  timeout_seconds=timeout_seconds)
            attempt_deadline = (None if exchange_deadline is None else
                                min(exchange_deadline,
                                    time.monotonic() + timeout_seconds))
            try:
                if attempt_deadline is None:
                    transport.open()
                    peer_pid = transport.peer_server_pid()
                    evidence = None
                else:
                    peer_pid, evidence = transport.prepare_peer(
                        verifier, deadline=attempt_deadline,
                        expect_image=expect_image,
                        expect_owner_sid=expect_owner_sid,
                        expect_creation_filetime=creation_map.get(instance_id))
                if peer_pid is None:
                    # D4 real-pipe observation: the FIRST connection to an
                    # idle one-instance server can complete its OPEN before the
                    # OS commits the handshake - GetNamedPipeServerProcessId
                    # then reports no peer while the connection is unusable (a
                    # dead handshake, the same serving-gap class as
                    # not_installed). The WHOLE exchange is retried once with a
                    # fresh connection that RE-VERIFIES identity (R41-2 holds:
                    # every attempt verifies before any request). A second
                    # unconfirmable peer fails closed; a real identity MISMATCH
                    # below is never retried.
                    if diag_event is not None:
                        diag_event("peer-identity", ok=False,
                                   error_code="peer-unavailable",
                                   instance_id=instance_id)
                    if attempt == 0:
                        last_result = {
                            "ok": False, "error_code": "not_installed",
                            "result": {"detail": "instance pipe peer "
                                                 "identity unavailable "
                                                 "(retrying)"}}
                        continue
                    raise PermissionError(
                        "instance pipe peer identity unavailable (refused)")
                if evidence is None:
                    evidence = verifier.verify(
                        peer_pid,
                        expect_image=expect_image,
                        expect_owner_sid=expect_owner_sid,
                        expect_creation_filetime=creation_map.get(instance_id))
                if not evidence[0]:
                    reason = (evidence[1].get("reason")
                              or evidence[1].get("error") or "refused")
                    if diag_event is not None:
                        diag_event("identity", ok=False,
                                   error_code="identity-refused",
                                   detail=str(reason)[:120],
                                   instance_id=instance_id)
                    raise PermissionError(
                        f"instance pipe peer identity refused: "
                        f"{evidence[1].get('reason') or evidence[1].get('error')}")
                if diag_event is not None:
                    diag_event("identity", ok=True, instance_id=instance_id)
                creation_map[instance_id] = evidence[1]["creation_filetime"]
                if on_verified is not None:
                    on_verified(instance_id, evidence[1])
                result = client.call(action, fields, instance_id=instance_id,
                                     transport=transport,
                                     deadline=attempt_deadline).to_dict()
                if diag_event is not None:
                    diag_event("exchange", ok=bool(result.get("ok")),
                               error_code=result.get("error_code"),
                               instance_id=instance_id)
                if (not result.get("ok")
                        and result.get("error_code") == "not_installed"
                        and attempt == 0):
                    last_result = result
                    continue                      # serving gap; retry once
                return result
            except TimeoutError:
                return {"ok": False, "error_code": "deadline_exceeded",
                        "result": {"detail": "instance preparation budget exhausted"}}
            except PermissionError:
                raise
            except OSError as exc:
                if diag_event is not None:
                    diag_event("connect", ok=False,
                               exception_type=type(exc).__name__,
                               detail=str(exc)[:120], instance_id=instance_id)
                raise
            finally:
                transport.close()
        # Unreachable while the loop always returns; defensive fallback keeps
        # a structured failure rather than an untyped None.
        return last_result or {"ok": False, "error_code": "not_installed",
                               "result": {}}

    return call


class WindowsInstanceVerifier:
    """H6 peer-identity decision path (SYSTEM verifies the pipe peer is the
    expected share instance process). The decision logic is product logic:
    ANY unopenable/failed check is a refusal (fail-closed); only a matching
    image name and owner SID pass. Creation time is recorded as evidence.

    The ``facade`` injectable stands in for the Win32 calls offline; the
    real declarations run only under the authorized D4 experiment.
    """

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x00000008
    _TOKEN_USER = 1

    def __init__(self, facade=None, expected_image=None,
                 expected_owner_sid=None):
        self._facade = facade
        self.expected_image = expected_image
        self.expected_owner_sid = expected_owner_sid

    def _win(self):
        if self._facade is not None:
            return self._facade
        return self._declare_real()

    @staticmethod
    def _declare_real():
        import ctypes
        from ctypes import wintypes
        pair = {}
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        pair["kernel32"] = kernel32
        pair["advapi32"] = advapi32
        return pair

    def verify(self, pid, expect_image=None,
               expect_owner_sid=None, expect_creation_filetime=None):
        """(ok, evidence) — fail-closed decision with recorded evidence.

        R41-2: the image check is a FULL path comparison (a suffix match
        would pass a same-named binary in a writable location); the
        creation time is mandatory evidence — a failed query REFUSES, and
        when an expectation exists (recorded from the first verified
        connection) a DIFFERENT creation time is a PID-reuse refusal."""
        evidence = {"pid": pid}
        expect_image = expect_image or self.expected_image
        expect_owner = expect_owner_sid or self.expected_owner_sid
        win = self._win()
        kernel32 = win["kernel32"]
        advapi32 = win["advapi32"]
        import ctypes
        from ctypes import wintypes
        handle = kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            evidence["error"] = "OpenProcess failed"
            return False, evidence
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(512)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                       ctypes.pointer(size)):
                evidence["error"] = "QueryFullProcessImageName failed"
                return False, evidence
            image = buf.value
            evidence["image"] = image
            if expect_image and image.lower() != str(expect_image).lower():
                # FULL path comparison (R41-2): a suffix match would pass a
                # same-named binary copied into a writable location.
                evidence["reason"] = "peer image mismatch"
                return False, evidence
            creation = self._creation_time(kernel32, handle, evidence)
            if not creation:
                # R41-2: creation time is MANDATORY evidence — a failed
                # query refuses (PID reuse cannot be detected without it).
                evidence["error"] = "GetProcessTimes failed"
                return False, evidence
            if (expect_creation_filetime is not None
                    and evidence["creation_filetime"]
                    != int(expect_creation_filetime)):
                evidence["reason"] = "peer identity changed (PID reuse?)"
                return False, evidence
            token = wintypes.HANDLE(0)
            if not advapi32.OpenProcessToken(handle, self._TOKEN_QUERY,
                                             ctypes.pointer(token)):
                evidence["error"] = "OpenProcessToken failed"
                return False, evidence
            try:
                needed = wintypes.DWORD(0)
                if not advapi32.GetTokenInformation(
                        token, self._TOKEN_USER, None, 0,
                        ctypes.pointer(needed)) and needed.value == 0:
                    evidence["error"] = "GetTokenInformation size failed"
                    return False, evidence
                token_buf = ctypes.create_string_buffer(needed.value)
                if not advapi32.GetTokenInformation(
                        token, self._TOKEN_USER, token_buf, needed.value,
                        ctypes.pointer(needed)):
                    evidence["error"] = "GetTokenInformation failed"
                    return False, evidence
                sid_str = wintypes.LPWSTR(0)
                # TOKEN_USER begins with the SID pointer at offset 0.
                sid_ptr = ctypes.cast(token_buf, ctypes.POINTER(
                    ctypes.c_void_p)).contents.value
                if not advapi32.ConvertSidToStringSidW(sid_ptr,
                                                       ctypes.pointer(
                                                           sid_str)):
                    evidence["error"] = "ConvertSidToStringSid failed"
                    return False, evidence
                owner = str(sid_str.value or "")
                evidence["owner"] = owner
                if expect_owner and owner != expect_owner:
                    evidence["reason"] = "peer owner mismatch"
                    return False, evidence
                return True, evidence
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _creation_time(kernel32, handle, evidence):
        import ctypes
        from ctypes import wintypes
        creation = wintypes.FILETIME(0)
        exit_ft = wintypes.FILETIME(0)
        kernel_ft = wintypes.FILETIME(0)
        user_ft = wintypes.FILETIME(0)
        if kernel32.GetProcessTimes(handle, ctypes.pointer(creation),
                                    ctypes.pointer(exit_ft),
                                    ctypes.pointer(kernel_ft),
                                    ctypes.pointer(user_ft)):
            evidence["creation_filetime"] = int(creation.dwLowDateTime) \
                | (int(creation.dwHighDateTime) << 32)
            return True
        return False


class IcaclsAclChecker:
    """Structured ACE validator for the protected projection writer
    (R31-6/R41-5): the writer REFUSES without an injected checker, so the
    real deployment path must supply one. Parses ``icacls`` output into
    (principal, permission-code) ACE pairs and fails closed on:

    * any WRITE-code grant to a principal that is not explicitly expected
      full (stray write principals) — read-only expectations with extra
      write codes are refused the same way (RX + W is never accepted);
    * a read-only expectation whose union of ACEs lacks the required read
      grant, or carries ANY write code;
    * an expected full identity whose ACEs never grant full control;
    * ANY ACE for a deny-listed identity (presence alone refuses);
    * an unparseable line (only the echoed path line is exempt), a DENY or
      SPECIAL_ACCESS ACE that would make the verdict ambiguous, a failed
      command or a timeout.

    Duplicate ACEs for the same principal are ALL evaluated (union of the
    permission codes) — a later same-name line can never overwrite an
    earlier violation. Real ACL effectiveness on the OS stays L2.

    ``expectations``: {"full": {name: {required codes}}, "read_only":
    {name: {required codes}}, "deny": [names]}.
    """

    _WRITE_CODES = frozenset({"F", "M", "W", "D", "WD", "AD", "WEA", "WA",
                              "DC", "DE", "WDAC", "WO", "GW", "GA"})
    _READ_CODES = frozenset({"R", "RX", "RD", "REA", "RA", "RC", "X", "GR",
                             "GE", "S"})
    _FULL_CODES = frozenset({"F", "GA"})
    # R44-4: only KNOWN permission codes may enter a union - an unknown
    # short code is ambiguous, never silently safe.
    _KNOWN_CODES = _WRITE_CODES | _READ_CODES
    # icacls structural markers that are NOT permission codes.
    _FLAG_TOKENS = frozenset({"I", "OI", "CI", "ID", "NP", "OD", "DENY"})

    def __init__(self, target, expectations, runner=None, timeout=10.0):
        self.target = str(target)
        self.expectations = self._validate_expectations(expectations)
        self._runner = runner or self._default_runner
        self._timeout = timeout
        self.last_error = None

    @classmethod
    def _validate_expectations(cls, expectations):
        if not isinstance(expectations, dict):
            raise ValueError("ACL expectations must be a dict")
        full = expectations.get("full") or {}
        read_only = expectations.get("read_only") or {}
        deny = expectations.get("deny") or []
        if not full and not read_only:
            raise ValueError("ACL expectations need full or read_only "
                             "entries")
        for name in list(full) + list(read_only):
            if not isinstance(name, str) or not name:
                raise ValueError("ACL expectation names must be non-empty")
        for perms in list(full.values()) + list(read_only.values()):
            if not isinstance(perms, (set, frozenset, list, tuple)) or \
                    not perms or not all(isinstance(p, str) and p
                                         for p in perms):
                raise ValueError("ACL expectation permissions must be "
                                 "non-empty code sets")
        if not isinstance(deny, (list, tuple, set)) or \
                not all(isinstance(n, str) and n for n in deny):
            raise ValueError("ACL deny list must contain identity names")
        return {"full": {n: frozenset(p) for n, p in full.items()},
                "read_only": {n: frozenset(p) for n, p in read_only.items()},
                "deny": list(deny)}

    @staticmethod
    def _default_runner(args, timeout):
        # icacls prints in the Windows ANSI code page (never UTF-8):
        # decode as mbcs so a non-ASCII principal name cannot crash the
        # writer's ACL gate with a decode error (D4 E4 fixture exposed the
        # utf-8-decode failure on a GBK-locale host).
        return subprocess.run(args, shell=False, capture_output=True,
                              text=True, encoding="mbcs", errors="replace",
                              timeout=timeout)

    @classmethod
    def default(cls, projection_path):
        """Standard RunRoot-root expectations: SYSTEM and Administrators
        full, the um_share identity read-only, the well-known open
        identities absent. Deployment-specific identities (the real
        um_share account name/SID) are passed explicitly by deployment
        config, not hardcoded here."""
        computer = os.environ.get('COMPUTERNAME', '').strip()
        if not computer:
            raise ValueError('ACL identity cannot be resolved: COMPUTERNAME missing')
        return cls(projection_path, {
            "full": {"NT AUTHORITY\\SYSTEM": {"F"},
                     "BUILTIN\\Administrators": {"F"}},
            "read_only": {computer + "\\um_share_public": {"RX"}},
            "deny": ["Everyone", "ANONYMOUS LOGON", "BUILTIN\\Users"]})

    # Microsoft icacls prints the mandatory-integrity policy as an ACE on
    # the synthetic "Mandatory Label" principal; its codes (NW=no-write-up,
    # NR=no-read-up, NE=no-execute-up) are policy levels, NOT grants. The
    # checker therefore excludes exactly these rows from the union and
    # refuses any other unknown code (contract: strict shape validation,
    # unsupported structures refuse; real output shapes are L2-calibrated).
    _INTEGRITY_CODES = frozenset({"NW", "NR", "NE"})
    _INTEGRITY_PRINCIPAL = "mandatory label"

    @staticmethod
    def _is_icacls_summary(line):
        """The standard icacls processing trailer (localized): always the
        FINAL line of real output (``Successfully processed 1 files; Failed
        processing 0 files`` / ``已成功处理 1 个文件; 处理 0 个文件时失败``).
        It is never an ACE - a ';'-separated count summary with digits and
        no principal colon. Any other colon-less line still refuses."""
        return (";" in line and any(ch.isdigit() for ch in line))

    @classmethod
    def _parse_aces(cls, lines, target):
        """Parse icacls output into structured ACE tuples.

        The echoed path line (exact match with the queried target,
        case-insensitive) is NOT an ACE and never enters parsing; a line
        that merely STARTS with the queried path has its REMAINDER parsed
        as an ACE (a dangerous ACE behind the path prefix is never
        dropped); the trailing processing-summary line (the standard,
        localized last line of real icacls output) is skipped; every other
        line must parse as ``principal:(flags)(codes)`` with KNOWN
        permission codes only — any ambiguity raises ValueError with the
        concrete reason (fail closed)."""
        aces = []
        target_lower = target.casefold()
        for idx, line in enumerate(lines):
            if line.casefold() == target_lower:
                continue                       # echoed path header line
            text = line
            if line.casefold().startswith(target_lower):
                # R44-4: the remainder behind the path prefix is an ACE.
                text = line[len(target):]
                if not text.strip():
                    continue                   # pure path line variant
                line = text
            if ":" not in line:
                if idx == len(lines) - 1 and cls._is_icacls_summary(line):
                    continue                   # standard processing trailer
                raise ValueError("unparsable ACL line: " + line[:80])
            name, _, flag_part = line.rpartition(":")
            name = name.strip()
            if not name:
                raise ValueError("empty ACE principal")
            tokens = [t.strip() for t in flag_part.split("(") if t.strip()]
            codes = set()
            is_deny = False
            is_special = False
            for token in tokens:
                token = token.strip("() \t")
                if not token:
                    continue
                if token == "SPECIAL_ACCESS:":
                    # Per-right detail lines follow on their own lines; the
                    # effective verdict would be ambiguous -> fail closed.
                    is_special = True
                    continue
                for piece in token.replace(" ", "").split(","):
                    if not piece:
                        continue
                    if piece in cls._FLAG_TOKENS:
                        if piece == "DENY":
                            is_deny = True
                        continue
                    if piece not in cls._KNOWN_CODES:
                        raise ValueError("unknown permission code: " + piece)
                    codes.add(piece)
            if (name.casefold().startswith(cls._INTEGRITY_PRINCIPAL)
                    and codes and codes <= cls._INTEGRITY_CODES):
                # Mandatory-integrity policy row: not a grant, not a
                # stranger - excluded from the union (contract above).
                continue
            if not codes and not is_deny:
                raise ValueError("ACE without permission codes: " + name)
            aces.append((name, frozenset(codes), is_deny, is_special))
        return aces

    def check(self, path=None, strict=True):
        """True only when EVERY structured expectation holds; any failure,
        mismatch, forbidden identity or unparseable output is a refusal.

        R46.6-C supported contract: strict shape validation with
        deny-override evaluation; aggregated codes F/GA imply their parts;
        inherited ACEs ((I)) count like explicit ones; unresolved SID
        display names simply never match an expectation and refuse. With
        ``strict`` (the default) ANY allow principal outside the full/
        read_only expectations refuses - a stranger able to READ the
        control secret's directory is as disqualifying as a writer.
        ``strict=False`` (offline projection fixtures only) keeps the
        narrower write-only stray check."""
        self.last_error = None
        # The writer passes its pathlib.Path destination per contract; the
        # query target and the ACE parser both need the string form (a Path
        # has no casefold — live W3 loop exposed that as an internal_error
        # on every real publication).
        if path is not None and not isinstance(path, str):
            path = str(path)
        try:
            proc = self._runner(["icacls", path or self.target],
                                self._timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"icacls failed: {type(exc).__name__}"
            return False
        if proc.returncode != 0:
            self.last_error = (f"icacls exit {proc.returncode}: "
                               f"{(proc.stderr or '').strip()[:200]}")
            return False
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines()
                 if ln.strip()]
        try:
            aces = self._parse_aces(lines, path or self.target)
        except ValueError as exc:
            self.last_error = str(exc)
            return False
        expected_full = self.expectations["full"]
        expected_read = self.expectations["read_only"]
        deny_names = {n.casefold() for n in self.expectations["deny"]}
        allow = {}                            # principal -> ALLOW union
        denied = {}                           # principal -> DENY union
        for name, codes, is_deny, is_special in aces:
            if name.casefold() in deny_names:
                self.last_error = f"forbidden identity present: {name}"
                return False
            if is_special:
                # Per-right detail cannot be evaluated here -> fail closed.
                self.last_error = (f"unsupported SPECIAL_ACCESS ACE on "
                                   f"{name}")
                return False

            target = denied if is_deny else allow
            target[name] = target.get(name, frozenset()) | codes

        # R49-5: DENY carrying SPECIFIC codes cannot be interpreted in the
        # token model (RX + DENY R would silently keep RX although Windows
        # denies the read inside RX) -> unsupported combination refuses as
        # a whole (strict shape contract). Only aggregated DENY codes
        # (F/GA) are interpretable: they clear the effective grant.
        for dname, dcodes in denied.items():
            unsupported = dcodes - self._FULL_CODES
            if unsupported:
                self.last_error = (f"unsupported deny granularity on "
                                   f"{dname}: {sorted(unsupported)}")
                return False

        def effective(name):
            # R44-4/R49-5: a DENY ACE can never count as a grant; an
            # aggregated ALLOW (F/GA) normalizes to EVERY known code
            # before subtraction; an aggregated DENY clears the grant.
            granted_codes = allow.get(name, frozenset())
            if granted_codes & self._FULL_CODES:
                granted_codes = self._KNOWN_CODES
            deny_codes = denied.get(name, frozenset())
            if deny_codes & self._FULL_CODES:
                return frozenset()
            return granted_codes - deny_codes

        for name, required in expected_full.items():
            if name not in allow:
                self.last_error = f"missing ACL grant for {name}"
                return False
            union = effective(name)
            if not (union & self._FULL_CODES) or \
                    not required <= union | self._FULL_CODES:
                self.last_error = (f"ACL grant for {name} lacks full "
                                   f"control (got {sorted(union)})")
                return False
        for name, required in expected_read.items():
            if name not in allow:
                self.last_error = f"missing ACL grant for {name}"
                return False
            union = effective(name)
            extra_write = union & self._WRITE_CODES
            if extra_write:
                # R41-5/R44-4: RX + ANY effectively-granted write code is
                # refused; a DENY ACE can never contribute a grant.
                self.last_error = (f"read-only identity {name} holds write "
                                   f"codes {sorted(extra_write)}")
                return False
            if not (union & self._READ_CODES) or not required <= union:
                self.last_error = (f"ACL grant for {name} lacks "
                                   f"{sorted(required)} (got {sorted(union)})")
                return False
        for name in allow:
            if name in expected_full or name in expected_read:
                continue
            if strict:
                # R46.6-C: for controlled objects ANY unlisted principal
                # refuses - reading the control secret's namespace is as
                # disqualifying as writing it; expectations must name
                # every identity.
                self.last_error = (f"unexpected principal {name} holds "
                                   f"{sorted(effective(name))}")
                return False
            stray_write = effective(name) & self._WRITE_CODES
            if stray_write:
                # R41-5/R44-4: an unlisted principal with an EFFECTIVE
                # write grant can never pass; a DENY cancelling its own
                # grant is harmless.
                self.last_error = (f"unexpected write principal {name} "
                                   f"holds {sorted(stray_write)}")
                return False
        return True

    def __call__(self, path=None):
        """The writer's acl_checker contract (ProtectedProjectionWriter):
        callable(path) -> None; any violation raises so the write is
        refused BEFORE the projection file is replaced."""
        if not self.check(path):
            from share_management import BrokerError
            raise BrokerError("projection ACL check failed: "
                              f"{self.last_error}")
