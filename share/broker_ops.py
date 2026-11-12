r"""share/broker_ops.py — real OS adapters for the broker host (D4 prereq).

Three adapters the SYSTEM broker host entry (share/broker_server.py) wires
together; each keeps product logic reviewable offline by injecting its OS
boundary:

* ``WindowsHostOps`` — start/stop/verify the SHARE HOST scheduled task via
  schtasks with controlled LIST arguments (no shell, no interpolation into a
  command line). ``verify_running``/``verify_closed`` are explicit states
  parsed from the task query, never inferred from absence of errors.
* ``make_instance_call`` — the coordinator's instance_call over the REAL
  instance channel: a named-pipe client to
  ``\\.\pipe\um-share-instance-<instance_id>`` signed with the shared
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

import ipaddress
import json
import math
import ntpath
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from share.broker_client import (BrokerClient, NamedPipeBrokerTransport,
                                 UnixSocketBrokerTransport)
from share.broker_protocol import DEFAULT_DEADLINE_SECONDS

INSTANCE_PIPE_PREFIX = r"\\.\pipe\um-share-instance-"

# WFP and firewall run together; their read must fit the 35-second RPC.
LIMITS_CHECK_TIMEOUT_SECONDS = 25.0

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

    _SHARE_MARKER = "chat2api.py"
    _BROKER_MARKER = "broker_server.py"
    _GUARD_MARKER = "share_guard.py"
    WATCHDOG_TASK_NAME = "umshare-watchdog"

    def __init__(self, task_name, runner=None, timeout=10.0,
                 share_image="um-share-python.exe", share_port=8100,
                 expected_image_path=None, expected_owner=None,
                 run_root=r"C:\ProgramData\UMShareRun",
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
        # Native Windows tools emit OEM text; PowerShell 7 emits UTF-8.
        # Wrong decoding corrupts localized task states before normalization.
        encoding = "oem" if os.name == "nt" else "utf-8"
        if args and args[0] == "pwsh":
            encoding = "utf-8"
            # Task Scheduler and long-lived SYSTEM processes may retain PATH
            # from before PowerShell 7 was installed. Prefer its installed path.
            program_files = os.environ.get("ProgramFiles")
            installed = os.path.join(program_files, "PowerShell", "7", "pwsh.exe") \
                if program_files else None
            if installed and os.path.isfile(installed):
                args = [installed, *args[1:]]
        return subprocess.run(args, shell=False, capture_output=True,
                              text=True, encoding=encoding, errors="replace",
                              timeout=timeout)

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
        # The owner query is the DOMINANT cost of this evidence read: one WMI
        # round trip per same-image row (measured ~2s per row on this machine
        # 2026-10-19 - ~7s for the broker + share gateway + dedicated
        # backend), which pushed the readiness read past its budget and the
        # start verb past the caller's exchange deadline (``unconfirmed`` /
        # ``deadline_exceeded`` with a healthily running gateway). The owner
        # is therefore fetched ONLY for the rows that can still BE the share
        # AFTER the readable command-line filters; a broker/guard or non-share
        # same-image row can never be the match, so its owner round trip is
        # skipped (the Python side keeps the SAME filters, so the evidence
        # verdict is unchanged). The cost no longer scales with the number of
        # unrelated same-image processes.
        script = ("$ErrorActionPreference = 'Stop'; "
                  "try { Get-CimInstance Win32_Process -Filter \"Name='"
                  + self.share_image + "'\" | ForEach-Object { "
                  "$c = [string]$_.CommandLine; $o = $null; "
                  "if ($c -like '*" + self._SHARE_MARKER + "*' "
                  "-and $c -notlike '*" + self._BROKER_MARKER + "*' "
                  "-and $c -notlike '*" + self._GUARD_MARKER + "*') { "
                  "try { $o = Invoke-CimMethod -InputObject $_ "
                  "-MethodName GetOwner -ErrorAction Stop } catch "
                  "{ $o = $null } }; [pscustomobject]@{ ProcessId = "
                  "$_.ProcessId; CommandLine = $_.CommandLine; "
                  "ExecutablePath = $_.ExecutablePath; CreationDate = "
                  "$_.CreationDate; OwnerDomain = $o.Domain; OwnerUser = "
                  "$o.User } } | ConvertTo-Json -Compress } catch { "
                  "exit 1 }")
        if deadline is not None and self._remaining(deadline) <= 0:
            return None
        try:
            proc = self._runner(["pwsh", "-NoProfile", "-Command",
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
            # R46.6-B: the cheap, readable fields must be present on EVERY
            # row - any missing field makes the WHOLE query unusable
            # evidence (None = unconfirmed, never partially trusted).
            if not isinstance(pid, int) or pid <= 0 \
                    or not isinstance(cmdline, str) \
                    or not isinstance(image_path, str) or not image_path \
                    or not isinstance(creation, str) or not creation:
                return None
            if self.expected_image_path and \
                    image_path.lower() != str(
                        self.expected_image_path).lower():
                continue               # full image path != expected: not target
            if self._BROKER_MARKER in cmdline or self._GUARD_MARKER in cmdline:
                continue               # same-image non-target process
            if self._SHARE_MARKER not in cmdline:
                continue               # not the share gateway: not the target
            # R49-3: the trusted MATCH needs ALL fields, the owner
            # included - and the owner query is one WMI round trip PER ROW
            # (the dominant cost of this read; measured ~2s per row on this
            # machine 2026-10-19), so it is demanded only of the rows
            # that survived the readable filters above. A row excluded by
            # its image path or its broker/guard command-line marker can
            # never be the share: its owner round trip was pure cost and
            # pushed the read past the readiness budget (start reported
            # ``unconfirmed`` / ``deadline_exceeded`` with a healthily
            # running gateway). A candidate row with a missing or
            # mismatched owner still fails the evidence exactly as before.
            owner_domain = row.get("OwnerDomain")
            owner_user = row.get("OwnerUser")
            if not isinstance(owner_domain, str) or not owner_domain \
                    or not isinstance(owner_user, str) or not owner_user:
                return None
            owner = owner_domain + chr(92) + owner_user
            if self.expected_owner and \
                    owner.lower() != str(self.expected_owner).lower():
                continue               # owner != expected: not the target
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
        # Routine health must not monopolize the single management pipe.
        # Expensive WFP/firewall checks belong to explicit host-evidence
        # and start gates; keep their compatibility field honestly unknown.
        # Reserve reply time under the 10s wire deadline for the task read.
        deadline = self._deadline(min(self._timeout, 6.0))
        state = self._query_state(deadline)
        # Additive host evidence (contract §2.5): both are READ-ONLY and both
        # answer None when the deployment did not configure the evidence
        # source - honest unavailability, never a fabricated verdict.
        return {"host": "scheduled-task", "task": self.task_name,
                "state": state,
                "host_limits": None,
                "share_faults": self.read_faults()}

    # ------------------------------------------------------ read-only evidence
    @staticmethod
    def _ps_quote(value):
        """Single-quoted PowerShell literal (defense in depth): deployment
        config values are never interpolated raw into a command line."""
        return "'" + str(value).replace("'", "''") + "'"

    def verify_limits(self) -> bool | None:
        return self.verify_limits_evidence()["verdict"]

    def repair_limits(self) -> dict:
        """Fixed protected maintenance entry; no client-provided arguments."""
        helper = Path(self.run_root or "") / "repair-limits.ps1"
        if not self.run_root or not helper.is_file():
            return {"repaired": False, "code": "capability_missing"}
        if self.verify_closed(budget_s=10) is not True:
            return {"repaired": False, "code": "gateway_not_closed"}
        try:
            proc = self._runner(["pwsh", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(helper),
                "-RunRoot", str(self.run_root), "-BudgetSeconds", "45"], 65)
        except (OSError, subprocess.SubprocessError):
            return {"repaired": False, "code": "repair_execution_failed"}
        result = None
        for line in (getattr(proc, "stdout", "") or "").splitlines():
            if line.startswith("REPAIR_RESULT="):
                try:
                    result = json.loads(line[len("REPAIR_RESULT="):])
                except (ValueError, TypeError):
                    result = None
        if proc.returncode != 0 or not isinstance(result, dict) or result.get("repaired") is not True:
            known = {"gateway_not_closed", "stop_intent_unconfirmed", "public_unconfirmed",
                     "public_not_closed", "repair_timeout", "wfp_repair_failed",
                     "firewall_not_satisfied", "watchdog_not_boot_ready", "invalid_deployment"}
            code = result.get("code") if isinstance(result, dict) else None
            return {"repaired": False, "code": code if code in known else "repair_not_confirmed"}
        evidence = self.verify_limits_evidence()
        if evidence["verdict"] is not True or self.verify_closed(budget_s=10) is not True:
            return {"repaired": False, "code": "repair_postcheck_failed"}
        return {"repaired": True, "code": "ok"}

    def verify_limits_evidence(self) -> dict:
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
            return {"verdict": None, "detail": "limits_unwired"}
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
            + "; $r = Get-LimitsState; if ($r[0]) { $r = Get-FirewallState }; "
            + "if ($r[0]) { Write-Output 'LIMITS=OK' } "
            + "else { Write-Output 'LIMITS=FAIL'; "
            + "Write-Output ('REASON=' + $r[1]) }")
        try:
            proc = self._runner(["pwsh", "-NoProfile",
                                 "-ExecutionPolicy", "Bypass",
                                 "-Command", script], LIMITS_CHECK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return {"verdict": None, "detail": "limits_timeout"}
        except FileNotFoundError:
            return {"verdict": None, "detail": "limits_executable_missing"}
        except PermissionError:
            return {"verdict": None, "detail": "limits_permission_denied"}
        except (OSError, subprocess.SubprocessError):
            return {"verdict": None, "detail": "limits_launch_failed"}
        if proc.returncode != 0:
            return {"verdict": None, "detail": "limits_process_failed"}
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
            return {"verdict": None, "detail": "limits_output_invalid"}
        if verdict is False and reason.startswith(("detection-failure", "firewall-error", "scope-query-error")):
            # The watchdog could not RUN the strong check (no permit port /
            # no helper): the evidence is unavailable, not a limit loss.
            return {"verdict": None, "detail": "firewall-query-failed"
                    if reason.startswith(("firewall-error", "scope-query-error")) else "wfp-query-failed"}
        # Only stable reason codes cross the broker boundary, never paths or
        # arbitrary helper output. Each verdict and detail share one observation.
        known = {"wfp-filter-missing", "wfp-filter-mismatch",
                 "firewall-missing", "firewall-disabled", "firewall-scope-mismatch"}
        detail = "ok" if verdict else reason if reason in known else "limits_not_satisfied"
        return {"verdict": verdict, "detail": detail}

    def read_watchdog(self) -> dict:
        """Observe one fixed SYSTEM task and protected log metadata only.

        Task arguments are compared inside PowerShell and never returned.
        Missing is explicit; query/launch failures keep ``exists=None``.
        A log mtime is historical activity evidence, not proof of a live task.
        """
        fields = ("exists", "task_state", "execute", "execute_matches",
                  "script_path", "script_matches", "last_task_result",
                  "last_task_result_hex", "last_run_at", "heartbeat_at",
                  "heartbeat_source")
        observed = dict.fromkeys(fields)
        observed.update(task_name=self.WATCHDOG_TASK_NAME,
                        queried_at=int(time.time()), query_error=None)
        if not self.run_root or not self.watchdog_script:
            observed["query_error"] = "watchdog_configuration_unavailable"
            return observed
        script = r'''$ErrorActionPreference = 'Stop'
$taskName = __TASK__
$expectedScript = __SCRIPT__
$activityLog = __LOG__
$r = [ordered]@{exists=$null;task_state=$null;execute=$null;execute_matches=$null;
    script_matches=$null;last_task_result=$null;last_run_at=$null;heartbeat_at=$null;query_error=$null}
$task = $null
try {
    try { $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -like '*CmdletizationQuery_NotFound*') {
            $r.exists = $false; $r.query_error = 'watchdog_task_missing'
        } else { $r.query_error = 'watchdog_query_failed' }
    }
    if ($null -ne $task) {
        if (@($task).Count -ne 1) { throw 'ambiguous task observation' }
        $r.exists = $true; $r.task_state = [string]$task.State
        $actions = @($task.Actions)
        if ($actions.Count -eq 1) {
            $expectedHost = Join-Path $PSHOME 'pwsh.exe'
            $r.execute_matches = [string]$actions[0].Execute -ieq $expectedHost
            $r.execute = if ($r.execute_matches) { $expectedHost } else { [IO.Path]::GetFileName([string]$actions[0].Execute) }
            $expectedArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $expectedScript + '"'
            $r.script_matches = [string]$actions[0].Arguments -ieq $expectedArgs
        } else { $r.execute_matches = $false; $r.script_matches = $false }
        try {
            $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\' -ErrorAction Stop
            $r.last_task_result = [long]$info.LastTaskResult
            if ($info.LastRunTime.Year -ge 1970) {
                $r.last_run_at = ([DateTimeOffset]$info.LastRunTime.ToUniversalTime()).ToUnixTimeSeconds()
            }
        } catch { $r.query_error = 'watchdog_task_info_unavailable' }
        try {
            $log = Get-Item -LiteralPath $activityLog -ErrorAction Stop
            $r.heartbeat_at = ([DateTimeOffset]$log.LastWriteTimeUtc).ToUnixTimeSeconds()
        } catch { }
    }
} catch { $r.query_error = 'watchdog_query_failed' }
$r | ConvertTo-Json -Compress
'''
        script = (script.replace("__TASK__", self._ps_quote(self.WATCHDOG_TASK_NAME))
                  .replace("__SCRIPT__", self._ps_quote(self.watchdog_script))
                  .replace("__LOG__", self._ps_quote(os.path.join(
                      str(self.run_root), "watchdog-system", "watchdog.log"))))
        try:
            proc = self._runner(["pwsh", "-NoProfile", "-NonInteractive",
                                 "-Command", script], self._timeout)
        except (OSError, subprocess.SubprocessError):
            observed["query_error"] = "watchdog_query_failed"
            return observed
        if proc.returncode != 0:
            observed["query_error"] = "watchdog_query_failed"
            return observed
        try:
            text = proc.stdout or ""
            raw = json.loads(text) if len(text) <= 16384 else None
        except (ValueError, TypeError):
            raw = None
        if not isinstance(raw, dict) or type(raw.get("exists")) not in (bool, type(None)):
            observed["query_error"] = "watchdog_response_invalid"
            return observed
        observed["queried_at"] = int(time.time())
        observed["exists"] = raw.get("exists")
        errors = {"watchdog_task_missing", "watchdog_query_failed",
                  "watchdog_task_info_unavailable"}
        error = raw.get("query_error")
        observed["query_error"] = error if isinstance(error, str) and error in errors else None
        if observed["exists"] is not True:
            observed["query_error"] = ("watchdog_task_missing"
                if observed["exists"] is False else "watchdog_query_failed")
            return observed
        state = raw.get("task_state")
        observed["task_state"] = state if state in (
            "Running", "Ready", "Disabled", "Queued", "Unknown") else None
        for key in ("execute_matches", "script_matches"):
            observed[key] = raw.get(key) if type(raw.get(key)) is bool else None
        execute = raw.get("execute")
        if isinstance(execute, str) and 0 < len(execute) <= 512 \
                and not any(ord(char) < 32 for char in execute):
            if observed["execute_matches"] is True:
                observed["execute"] = execute
            else:
                leaf = ntpath.basename(execute)
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}\.exe", leaf):
                    observed["execute"] = leaf
        if observed["script_matches"] is True:
            observed["script_path"] = str(self.watchdog_script)
        result = raw.get("last_task_result")
        if type(result) is int and -(2 ** 31) <= result < 2 ** 32:
            observed["last_task_result"] = result & 0xffffffff
            observed["last_task_result_hex"] = "0x%08X" % (result & 0xffffffff)
        for key in ("last_run_at", "heartbeat_at"):
            stamp = raw.get(key)
            if type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0:
                observed[key] = stamp
        if observed["heartbeat_at"] is not None:
            observed["heartbeat_source"] = "watchdog_log_mtime"
        return observed

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
        from share.secret_protection import read_private_secret_path
        try:
            data = read_private_secret_path(
                path, max_bytes=128, label="secret file").strip()
        except (OSError, ValueError) as exc:
            raise ValueError(f"secret file unreadable: "
                             f"{Path(path).name}") from exc
        if not data:
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
        return cls(projection_path, {
            "full": {"NT AUTHORITY\\SYSTEM": {"F"},
                     "BUILTIN\\Administrators": {"F"}},
            "read_only": {"DAICNT\\um_share": {"RX"}},
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
            from share.management import BrokerError
            raise BrokerError("projection ACL check failed: "
                              f"{self.last_error}")


# ---------------------------------------------------------------------------
# POSIX (Linux) restricted adapters (2026-10-30 cloud role)
# ---------------------------------------------------------------------------
# The Windows adapters above stay the DEFAULT on Windows, byte for byte. The
# classes below are the Linux deployment's equivalents and never fabricate a
# Windows DACL, a scheduled task or a SYSTEM identity:
#
# * ``PosixProjectionAclChecker`` — the projection writer's acl_checker on
#   POSIX: parent directory not group/other writable, the file is not a
#   symlink, its owner is the service identity and no group/other write
#   bit is set. A violation raises BEFORE the projection is replaced.
# * ``LinuxHostOps`` — start/stop/verify the share host from a pid file and
#   the REAL kernel process facts (``/proc/<pid>`` command line, ``os.kill(pid,
#   0)`` liveness). Nothing here calls PowerShell, tasklist, netstat or
#   schtasks. A manual/budget stop is a PERSISTENT intent: ``stop_host``
#   writes it BEFORE signalling and ``start_host`` refuses while it is in force;
#   only an explicit human start clears it, so a watchdog can never resurrect a
#   deliberate stop across restarts.
# * ``LinuxInstanceVerifier`` / ``make_unix_instance_call`` — the instance
#   channel peer identity over AF_UNIX: SO_PEERCRED (pid/uid/gid) is KERNEL
#   evidence and the process command line/exe/creation time are read from
#   ``/proc/<pid>``. A failed or mismatched identity refuses BEFORE any
#   request is sent (never trusted from a self-reported pid or a writable
#   status file).
# * ``linux_server_plan`` / ``build_linux_server`` / ``linux_main`` — the
#   Linux broker entry: the management channel is an AF_UNIX socket server
#   (``share_broker_server.UnixSocketBrokerServer``) with the SAME bounded
#   frames, HMAC, replay registry, instance identity and exchange deadline
#   contract as the Windows named-pipe host; the host ops are the pid-file
#   ``LinuxHostOps`` instead of the SYSTEM scheduled task.
# ---------------------------------------------------------------------------

LINUX_HOST_INTENT_SCHEMA = 1
LINUX_HOST_DEFAULT_STATE_DIR = "/run/um-share"
LINUX_HOST_DEFAULT_PID_FILE = "/run/um-share/share-host.pid"
LINUX_HOST_INTENT_FILE = "share-host-desired.json"
LINUX_HOST_STOP_SIGNAL = 15      # SIGTERM
LINUX_HOST_KILL_SIGNAL = 9       # SIGKILL


def _linux_proc_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def linux_entry_script(argv):
    """The interpreter's SCRIPT operand from an argv list, or None.

    The entry script is the first POSITIONAL operand after the interpreter - never
    the last element of the command line (the production cloud command ends with
    ``--upstream-port <n>``) and never an option value. ``-c``/``-m`` run code/a
    module instead of a script file, so they return None: a script name later on
    that line (an argument to the ``-c`` code) can never be mistaken for the
    entry. An unreadable/missing shape returns None and the caller fails closed.
    """
    if not argv:
        return None
    tokens = [str(part) for part in argv]
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token == "-":
            return None                 # stdin: no script file entry
        if not token.startswith("-"):
            return token or None         # first positional = script operand
        if not token.startswith("--"):
            flags = token[1:]
            if "c" in flags or "m" in flags:
                return None              # -c/-m: code/module, no script entry
            if flags and flags[-1] in ("W", "X", "Q"):
                index += 1               # option consumes the next element
        index += 1
    if index < len(tokens):
        return tokens[index] or None
    return None


class PosixProjectionAclChecker:
    """POSIX ACL/permission validator for the protected projection writer.

    The Windows ``IcaclsAclChecker`` is a DACL audit; POSIX has no DACL, so this
    checker verifies the actual POSIX facts instead of faking one:

    * the file is NOT a symlink (a symlink could point at an attacker file);
    * the parent directory exists, is a directory and is not group/other
      writable (a writable parent lets another user swap the file);
    * the file's owner is the service identity (``expected_uid``, default the
      current effective uid);
    * the file carries no group/other WRITE bit.

    Any violation raises (the writer then refuses BEFORE the atomic replace);
    an absent file is allowed (the writer is about to create it) but its parent
    is still checked. Read-only checks: no mode is ever changed here.
    """

    _OTHER_WRITE = 0o022

    def __init__(self, expected_uid=None):
        self.expected_uid = expected_uid
        self.last_error = None

    def check(self, path):
        target = Path(path)
        parent = target.parent
        try:
            parent_stat = os.stat(parent)
        except OSError as exc:
            self.last_error = f"projection parent unreadable: {parent}"
            return False
        if not stat.S_ISDIR(parent_stat.st_mode):
            self.last_error = f"projection parent is not a directory: {parent}"
            return False
        if stat.S_IMODE(parent_stat.st_mode) & self._OTHER_WRITE:
            self.last_error = f"projection parent is group/other writable: {parent}"
            return False
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return True
        except OSError:
            self.last_error = f"projection unreadable: {target}"
            return False
        if stat.S_ISLNK(info.st_mode):
            self.last_error = f"projection is a symlink: {target}"
            return False
        if not stat.S_ISREG(info.st_mode):
            self.last_error = f"projection is not a regular file: {target}"
            return False
        expected_uid = self.expected_uid
        if expected_uid is None:
            expected_uid = os.geteuid()
        if info.st_uid != expected_uid:
            self.last_error = (f"projection owner uid {info.st_uid} != "
                               f"{expected_uid}")
            return False
        if stat.S_IMODE(info.st_mode) & self._OTHER_WRITE:
            self.last_error = f"projection is group/other writable: {target}"
            return False
        return True

    def __call__(self, path=None):
        if not self.check(path):
            from share.management import BrokerError
            raise BrokerError("projection ACL check failed: "
                              f"{self.last_error}")


def _linux_hex_address(text, width):
    """Decode one ``/proc/net/tcp{,6}`` local address (host byte order).

    The kernel prints each 32-bit word little-endian; a 4-byte row is an IPv4
    address and a 16-byte row is IPv6. An invalid width/hex payload returns
    None (the caller reports the evidence unresolvable, never a pass).
    """
    if not isinstance(text, str) or len(text) != width * 2:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))


def _linux_listening_addresses(proc_root, port, missing_is_empty=False):
    """``(addresses, reason)`` for LISTEN rows on ``port`` (read-only).

    Linux has no WFP: the deployment's isolation boundary is the gateway's own
    loopback-only LISTEN socket. ``addresses`` is the list of textual local
    addresses bound to the port; ``reason`` is non-None when the kernel table
    cannot be read, so the caller reports unconfirmed instead of a pass.
    """
    net_root = Path(proc_root, "net")
    try:
        tcp = Path(net_root, "tcp").read_text(encoding="ascii",
                                              errors="replace")
    except FileNotFoundError:
        # A missing table is empty only for the closure check; the limits
        # evidence reader treats it as unresolvable.
        return ([], None) if missing_is_empty else (None, "limits_net_unreadable")
    except OSError:
        return None, "limits_net_unreadable"
    tables = [("tcp", 4, tcp)]
    try:
        tcp6 = Path(net_root, "tcp6").read_text(encoding="ascii",
                                               errors="replace")
    except FileNotFoundError:
        tcp6 = None                      # no IPv6 table: no IPv6 listeners
    except OSError:
        return None, "limits_net_unreadable"
    if tcp6 is not None:
        tables.append(("tcp6", 16, tcp6))
    addresses = []
    for _kind, width, text in tables:
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":   # TCP_LISTEN only
                continue
            local = parts[1]
            if ":" not in local:
                continue
            address_hex, port_hex = local.rsplit(":", 1)
            try:
                local_port = int(port_hex, 16)
            except ValueError:
                return None, "limits_net_unreadable"
            if local_port != int(port):
                continue
            packed = _linux_hex_address(address_hex, width)
            if packed is None:
                return None, "limits_net_unreadable"
            try:
                addresses.append(str(ipaddress.ip_address(packed)))
            except ValueError:
                return None, "limits_net_unreadable"
    return addresses, None


class LinuxHostOps:
    """Linux restricted host ops: pid file + REAL kernel process identity.

    The state/start/stop verdicts come from the pid file and the kernel's own
    process facts: liveness is ``os.kill(pid, 0)`` (ProcessLookupError = gone,
    EPERM = exists but not ours) and identity is the ``/proc/<pid>/cmdline``
    (the expected ENTRY SCRIPT plus the exact ``--port <port>`` pair) and,
    when configured, the process uid from ``/proc/<pid>``. No PowerShell,
    tasklist, netstat or schtasks is ever called, and no Windows DACL/SYSTEM
    task is fabricated.

    PERSISTENT STOP INTENT: a manual or budget stop writes
    ``share-host-desired.json`` (state=stopped) BEFORE any signal. The intent
    survives restarts and ``start_host()`` (the watchdog/broker path) refuses
    while it is in force; only ``start_host(explicit=True)`` - the operator's
    own console start - clears it. A start is idempotent (already running is
    confirmed, never spawned twice) and a pid whose command line is not the
    share host is NEVER signalled (a bad pid can never kill an unrelated
    process).
    """

    def __init__(self, pid_file, command, *, state_dir=None,
                 expected_uid=None, expected_port=None,
                 proc_root="/proc", kill_fn=None, spawner=None,
                 clock=time.monotonic, sleeper=time.sleep,
                 start_wait_s=6.0, stop_wait_s=10.0, poll_interval_s=0.05):
        if not pid_file or not isinstance(pid_file, str):
            raise ValueError("pid_file must be a non-empty path")
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("command must be a non-empty argv list")
        self.pid_file = os.path.abspath(pid_file)
        self.command = [str(part) for part in command]
        self.state_dir = os.path.abspath(
            state_dir or os.path.dirname(self.pid_file) or ".")
        self.expected_uid = expected_uid
        self.expected_port = expected_port
        self.proc_root = str(proc_root)
        self._kill = kill_fn or os.kill
        self._spawner = spawner
        self._clock = clock
        self._sleep = sleeper
        self.start_wait_s = float(start_wait_s)
        self.stop_wait_s = float(stop_wait_s)
        self.poll_interval_s = float(poll_interval_s)
        self.last_error = None

    # ------------------------------------------------------------ facts
    def _intent_path(self):
        return os.path.join(self.state_dir, LINUX_HOST_INTENT_FILE)

    def read_intent(self):
        """Persistent stop intent: "stopped" | "running" | None (unknown).

        Absent/unreadable/malformed is None (no authority), never a fabricated
        stop or run state."""
        try:
            record = json.loads(Path(self._intent_path()).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        if record.get("schema_version") != LINUX_HOST_INTENT_SCHEMA:
            return None
        state = record.get("state")
        if state not in ("running", "stopped"):
            return None
        return state

    def _write_intent(self, state, reason):
        path = Path(self._intent_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema_version": LINUX_HOST_INTENT_SCHEMA,
                  "state": state, "reason": reason,
                  "at": int(time.time())}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)

    def clear_stop_intent(self):
        """Explicit operator start: supersede the stop intent."""
        self._write_intent("running", "explicit_start")

    def _read_pid(self):
        try:
            raw = Path(self.pid_file).read_text(encoding="ascii").strip()
        except OSError:
            return None
        if not raw.isdigit():
            return None
        pid = int(raw)
        return pid if pid > 0 else None

    def _write_pid(self, pid):
        path = Path(self.pid_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(int(pid)), encoding="ascii")
        os.replace(tmp, path)

    def _forget_pid(self):
        try:
            os.remove(self.pid_file)
        except OSError:
            pass

    def _proc_alive(self, pid):
        """Kernel liveness: True/False, None when undecidable."""
        try:
            self._kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The process exists but belongs to another identity: it is NOT a
            # verified host, and it is never signalled.
            return True
        except OSError:
            return None

    def _cmdline(self, pid):
        text = _linux_proc_text(self.proc_root, pid, "cmdline")
        if text is None:
            return None
        return [part for part in text.split("\x00") if part]

    def _uid(self, pid):
        try:
            return os.stat(os.path.join(self.proc_root, str(int(pid)))).st_uid
        except OSError:
            return None

    def _creation_epoch(self, pid):
        return _linux_proc_creation(self.proc_root, pid)

    def _identity_ok(self, pid, cmdline=None):
        if cmdline is None:
            cmdline = self._cmdline(pid)
        if not cmdline:
            return False
        marker = linux_entry_script(self.command)
        if marker:
            # The marker is the configured ENTRY SCRIPT; the process's own
            # entry operand must be that SAME script. The name appearing as an
            # argument (or the trailing --upstream-port value) never matches.
            entry = linux_entry_script(cmdline)
            if entry is None or os.path.basename(entry) != \
                    os.path.basename(marker):
                self.last_error = "process command line lacks the share entry"
                return False
        if self.expected_port is not None:
            port = str(self.expected_port)
            paired = any(cmdline[i] == "--port" and i + 1 < len(cmdline)
                         and cmdline[i + 1] == port
                         for i in range(len(cmdline)))
            if not paired:
                self.last_error = "process command line lacks the share port"
                return False
        if self.expected_uid is not None:
            uid = self._uid(pid)
            if uid is None or uid != self.expected_uid:
                self.last_error = ("process uid unknown or different "
                                   "(never signalled)")
                return False
        return True

    # ------------------------------------------------------------ verbs
    def status(self):
        pid = self._read_pid()
        intent = self.read_intent()
        if pid is None:
            return {"state": "stopped", "healthy": False,
                    "detail": "pid file absent", "process_ids": [],
                    "intent": intent}
        alive = self._proc_alive(pid)
        if alive is None:
            return {"state": "unknown", "healthy": False,
                    "detail": "process liveness undecidable",
                    "process_ids": [pid], "intent": intent}
        if not alive:
            return {"state": "stopped", "healthy": False,
                    "detail": "process gone", "process_ids": [],
                    "intent": intent}
        if not self._identity_ok(pid):
            return {"state": "conflict", "healthy": False,
                    "detail": self.last_error or "identity mismatch",
                    "process_ids": [pid], "intent": intent}
        return {"state": "running", "healthy": True, "detail": "ok",
                "process_ids": [pid], "intent": intent}

    def _linux_share_pids(self):
        """The REAL /proc scan for share-gateway processes, or None.

        A missing pid file is not proof of closure: the process may still be
        alive with its pid file removed. The cmdline is the identity signal
        (chat2api.py plus the configured port); an unreadable /proc is
        unknown, never an empty result.
        """
        root = Path(self.proc_root)
        found = []
        try:
            entries = list(root.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            args = [part.decode("utf-8", errors="replace")
                    for part in raw.split(b"\0") if part]
            if not any("chat2api.py" in arg for arg in args):
                continue
            if self.expected_port is not None:
                port = str(self.expected_port)
                paired = any(args[i] == "--port" and i + 1 < len(args)
                             and args[i + 1] == port
                             for i in range(len(args)))
                if not paired:
                    continue
            pid = int(entry.name)
            identity = self._identity_ok(pid, cmdline=args)
            if identity is True:
                found.append(pid)
            elif identity is None:
                return None
        return found

    def verify_running(self):
        """True only on confirmed pid identity OR a live gateway process.

        A pid file can be lost while the gateway is still alive; the /proc
        cmdline scan is the second evidence source. An unreadable /proc stays
        unconfirmed (False), never a fabricated stopped/running state.
        """
        pid = self._read_pid()
        if pid is not None and self._proc_alive(pid) is True                 and self._identity_ok(pid):
            return True
        pids = self._linux_share_pids()
        return bool(pids) if pids is not None else False

    def verify_closed(self):
        """True only when no process of OURS and no gateway listener remains.

        A stale/mismatched pid file alone is not proof: the pid file can be
        missing while the 8100 process is still alive. The /proc cmdline scan
        and the kernel LISTEN table are the closure evidence; an unreadable
        source is unconfirmed (False), never a fabricated True.
        """
        pids = self._linux_share_pids()
        if pids is None:
            return False
        if pids:
            return False
        if self.expected_port is not None:
            addresses, reason = _linux_listening_addresses(
                self.proc_root, self.expected_port, missing_is_empty=True)
            # The process scan is the primary closure evidence. A readable
            # listener still blocks closure; an unreadable kernel table stays
            # unknown (never folded into True).
            if reason is not None:
                return False
            if addresses:
                return False
        pid = self._read_pid()
        if pid is not None:
            self._forget_pid()
        return True

    def verify_limits_evidence(self):
        """Linux isolation evidence for the gateway port (read-only).

        Linux has no WFP layer. The deployment's limit that the public entry
        must not reach the gateway directly is the gateway's own LISTEN socket
        being bound to loopback only (the public HTTPS layer is separate and
        not deployed). This reads the REAL kernel table facts from
        ``/proc/net/tcp{,6}``:

        * True  - every LISTEN row on ``expected_port`` is a loopback address;
        * False - a non-loopback LISTEN row exists (limit loss) or the gateway
          is not listening on the port at all;
        * None  - the kernel table is unreadable (unresolvable, never folded
          into True/False).

        Read-only: no rule, process or file is ever changed.
        """
        if self.expected_port is None:
            return {"verdict": None, "detail": "limits_unwired"}
        addresses, reason = _linux_listening_addresses(
            self.proc_root, self.expected_port)
        if reason is not None:
            return {"verdict": None, "detail": reason}
        if not addresses:
            return {"verdict": False, "detail": "gateway_not_listening"}
        for address in addresses:
            try:
                loopback = ipaddress.ip_address(address).is_loopback
            except ValueError:
                return {"verdict": None, "detail": "limits_address_unreadable"}
            if not loopback:
                return {"verdict": False, "detail": "non_loopback_listener"}
        return {"verdict": True, "detail": "loopback_only"}

    def verify_limits(self) -> bool | None:
        return self.verify_limits_evidence()["verdict"]

    def start_host(self, explicit=False):
        """Start the share host; idempotent, never resurrects a stop intent.

        * already running (pid + identity confirmed) -> True, no spawn;
        * a persistent stop intent is in force -> False (unless
          ``explicit=True``, the operator's own start, which clears it);
        * otherwise spawn the fixed argv, write the pid file and confirm the
          process identity within the bounded start window.
        """
        intent = self.read_intent()
        if intent == "stopped" and not explicit:
            self.last_error = "stop intent in force; watchdog start refused"
            return False
        if self.verify_running():
            return True
        if intent == "stopped":
            try:
                self.clear_stop_intent()
            except OSError:
                # The explicit start could not supersede the stop intent:
                # never spawn a process the persistent intent still forbids.
                self.last_error = "start intent write failed; not spawned"
                return False
        pid = self._read_pid()
        if pid is not None and self._proc_alive(pid) is True:
            # A live process that is NOT our share host: never spawn a second
            # owner and never signal it; the operator must resolve the stale
            # pid file first.
            self.last_error = "pid file points at a live foreign process"
            return False
        self._forget_pid()
        try:
            if self._spawner is not None:
                proc = self._spawner(self.command)
            else:
                proc = subprocess.Popen(
                    self.command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True)
        except OSError as exc:
            self.last_error = f"spawn failed: {type(exc).__name__}"
            return False
        try:
            self._write_pid(proc.pid)
        except OSError:
            # Never leave an untracked process behind: this pid was spawned
            # by THIS call, so terminating it is the safe cleanup.
            self._signal(proc.pid, LINUX_HOST_STOP_SIGNAL)
            self.last_error = "pid write failed; spawned process terminated"
            return False
        deadline = self._clock() + self.start_wait_s
        while self._clock() < deadline:
            if self.verify_running():
                try:
                    self._write_intent("running", "started")
                except OSError:
                    # The pid+identity ARE confirmed, so the start is True;
                    # only the persistent intent could not be recorded.
                    self.last_error = "running intent write failed"
                return True
            self._sleep(self.poll_interval_s)
        self._forget_pid()
        self.last_error = "started process never verified as the share host"
        return False

    def stop_host(self, reason="manual"):
        """Stop the share host; the stop intent is written FIRST.

        A bad/mismatched pid is NEVER signalled. Returns True only when the
        process is confirmed gone (or there was nothing of ours to stop). An
        unwritable intent is an honest refusal BEFORE any signal: a stop that
        cannot be persisted could be resurrected by the watchdog across
        restarts, so the process is deliberately left running.
        """
        try:
            self._write_intent("stopped", reason)
        except OSError:
            self.last_error = "stop intent write failed; not signalled"
            return False
        pid = self._read_pid()
        if pid is None:
            # A missing pid file is not closure: scan /proc before claiming
            # the host is stopped. The broker's host-stop then verifies again.
            if self.verify_closed():
                return True
            self.last_error = "gateway process still alive without a pid file"
            return False
        alive = self._proc_alive(pid)
        if alive is None:
            self.last_error = "process liveness undecidable; not signalled"
            return False
        if alive is False:
            self._forget_pid()
            return True
        if not self._identity_ok(pid):
            self.last_error = ("recorded pid is not the share host; "
                               "never signalled")
            if self._proc_alive(pid) is False:
                self._forget_pid()
                return True
            return False
        self._signal(pid, LINUX_HOST_STOP_SIGNAL)
        deadline = self._clock() + self.stop_wait_s
        while self._clock() < deadline:
            if self._proc_alive(pid) is False:
                self._forget_pid()
                return True
            self._sleep(self.poll_interval_s)
        self._signal(pid, LINUX_HOST_KILL_SIGNAL)
        deadline = self._clock() + self.stop_wait_s
        while self._clock() < deadline:
            if self._proc_alive(pid) is False:
                self._forget_pid()
                return True
            self._sleep(self.poll_interval_s)
        self.last_error = "process still alive after the kill signal"
        return False

    def _signal(self, pid, sig):
        try:
            self._kill(int(pid), sig)
        except OSError:
            pass


def _linux_proc_text(proc_root, pid, name):
    try:
        return Path(proc_root, str(int(pid)), name).read_text(
            encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _linux_proc_creation(proc_root, pid):
    """Process creation epoch from /proc/<pid>/stat + /proc/stat btime.

    starttime is field 22 in clock ticks; the comm field may contain spaces and
    parentheses, so fields are split after the LAST ')'."""
    stat_text = _linux_proc_text(proc_root, pid, "stat")
    try:
        btime_text = Path(proc_root, "stat").read_text(
            encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        btime_text = None
    if not stat_text or not btime_text:
        return None
    try:
        tail = stat_text[stat_text.rindex(")") + 2:].split()
        start_ticks = int(tail[19])
    except (ValueError, IndexError):
        return None
    btime = None
    for line in btime_text.splitlines():
        if line.startswith("btime "):
            try:
                btime = int(line.split()[1])
            except (ValueError, IndexError):
                btime = None
            break
    if btime is None:
        return None
    try:
        ticks = int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        ticks = 100
    if ticks <= 0:
        ticks = 100
    return float(btime) + float(start_ticks) / float(ticks)


class LinuxInstanceVerifier:
    """Instance-channel peer identity on POSIX: KERNEL evidence only.

    The peer pid comes from SO_PEERCRED (never a status file). Identity is the
    REAL ``/proc/<pid>`` command line (the expected entry marker and the exact
    ``--port <port>`` pair), the exe symlink target and the process uid. The
    creation time is mandatory evidence when an expectation exists (PID reuse
    detection). Every failed/unreadable check is a refusal.
    """

    def __init__(self, *, proc_root="/proc", expected_image=None,
                 expected_uid=None, expected_cmdline_marker=None,
                 expected_port=None, exe_reader=None):
        self.proc_root = str(proc_root)
        self.expected_image = expected_image
        self.expected_uid = expected_uid
        self.expected_cmdline_marker = expected_cmdline_marker
        self.expected_port = expected_port
        self._exe_reader = exe_reader or os.readlink

    def verify(self, pid, expect_image=None, expect_uid=None,
               expect_creation_epoch=None, expect_cmdline_marker=None,
               expect_port=None):
        evidence = {"pid": pid}
        marker = expect_cmdline_marker or self.expected_cmdline_marker
        port = expect_port if expect_port is not None else self.expected_port
        uid_expected = expect_uid if expect_uid is not None \
            else self.expected_uid
        cmdline = _linux_proc_text(self.proc_root, pid, "cmdline")
        if not cmdline:
            evidence["error"] = "process command line unreadable"
            return False, evidence
        parts = [part for part in cmdline.split("\x00") if part]
        if not parts:
            evidence["error"] = "process command line empty"
            return False, evidence
        if marker:
            if not any(os.path.basename(part) == marker for part in parts):
                evidence["error"] = "instance entry marker mismatch"
                return False, evidence
        if port is not None:
            paired = any(parts[i] == "--port" and i + 1 < len(parts)
                         and parts[i + 1] == str(port)
                         for i in range(len(parts)))
            if not paired:
                evidence["error"] = "instance port mismatch"
                return False, evidence
        image = expect_image or self.expected_image
        if image:
            try:
                exe = self._exe_reader(os.path.join(
                    self.proc_root, str(int(pid)), "exe"))
            except OSError:
                evidence["error"] = "process image unreadable"
                return False
            if os.path.realpath(exe) != os.path.realpath(image):
                evidence["error"] = "process image mismatch"
                return False, evidence
            evidence["image"] = exe
        try:
            uid = os.stat(os.path.join(self.proc_root,
                                       str(int(pid)))).st_uid
        except OSError:
            evidence["error"] = "process uid unreadable"
            return False
        if uid_expected is not None and uid != uid_expected:
            evidence["error"] = "process uid mismatch"
            return False
        evidence["uid"] = uid
        creation = _linux_proc_creation(self.proc_root, pid)
        if creation is None:
            evidence["error"] = "process creation time unreadable"
            return False
        evidence["creation_epoch"] = creation
        if expect_creation_epoch is not None:
            try:
                delta = abs(float(creation) - float(expect_creation_epoch))
            except (TypeError, ValueError):
                evidence["error"] = "creation expectation malformed"
                return False
            if delta > 2.0:
                evidence["error"] = "process creation time mismatch (pid reuse)"
                return False
        return True, evidence


def make_unix_instance_call(secret_file, socket_prefix,
                             *, verifier, timeout_seconds=DEFAULT_DEADLINE_SECONDS,
                             expect_image=None, expect_uid=None,
                             expect_creation_by_instance=None,
                             expect_cmdline_marker=None, expect_port=None,
                             on_verified=None, diag_event=None,
                             transport_factory=None):
    """The coordinator-compatible ``instance_call`` over AF_UNIX.

    Same trust chain and wire contract as ``make_instance_call``: the request
    frame carries the instance id (a socket path is addressing, not identity),
    the peer's SERVER identity is verified from KERNEL facts BEFORE any request
    is sent, a verified connection refreshes the registration and records the
    creation time for PID-reuse detection, and the whole exchange is bounded by
    the caller's remaining budget (including the single transient retry).
    """
    secret = _KeyFileSecret.read(secret_file)
    creation_map = expect_creation_by_instance if \
        expect_creation_by_instance is not None else {}
    factory = transport_factory or UnixSocketBrokerTransport

    def call(action, fields, instance_id, budget_s=None):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id must be a non-empty string")
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
        last_result = None
        for attempt in range(2):
            if exchange_deadline is not None \
                    and exchange_deadline - time.monotonic() <= 0:
                return {"ok": False, "error_code": "deadline_exceeded",
                        "result": {"detail": "instance exchange budget "
                                             "exhausted"}}
            socket_path = f"{socket_prefix}{instance_id}"
            transport = factory(socket_path)
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
                        expect_uid=expect_uid,
                        expect_creation_epoch=creation_map.get(instance_id),
                        expect_cmdline_marker=expect_cmdline_marker,
                        expect_port=expect_port)
                if peer_pid is None:
                    if diag_event is not None:
                        diag_event("peer-identity", ok=False,
                                   error_code="peer-unavailable",
                                   instance_id=instance_id)
                    if attempt == 0:
                        last_result = {
                            "ok": False, "error_code": "not_installed",
                            "result": {"detail": "instance socket peer "
                                                 "identity unavailable "
                                                 "(retrying)"}}
                        continue
                    raise PermissionError(
                        "instance socket peer identity unavailable (refused)")
                if evidence is None:
                    evidence = verifier.verify(
                        peer_pid, expect_image=expect_image,
                        expect_uid=expect_uid,
                        expect_creation_epoch=creation_map.get(instance_id),
                        expect_cmdline_marker=expect_cmdline_marker,
                        expect_port=expect_port)
                if not evidence[0]:
                    reason = (evidence[1].get("error")
                              or evidence[1].get("reason") or "refused")
                    if diag_event is not None:
                        diag_event("identity", ok=False,
                                   error_code="identity-refused",
                                   detail=str(reason)[:120],
                                   instance_id=instance_id)
                    raise PermissionError(
                        f"instance socket peer identity refused: {reason}")
                if diag_event is not None:
                    diag_event("identity", ok=True, instance_id=instance_id)
                creation_map[instance_id] = evidence[1]["creation_epoch"]
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
                    continue
                return result
            except TimeoutError:
                return {"ok": False, "error_code": "deadline_exceeded",
                        "result": {"detail": "instance preparation budget "
                                             "exhausted"}}
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
        return last_result or {"ok": False, "error_code": "not_installed",
                               "result": {}}

    return call


# --------------------------------------------------------------- Linux entry
# The SAME coordinator (bounded frames, HMAC, replay registry, instance
# identity, exchange deadlines) over an AF_UNIX management socket instead of
# the Windows named pipe. No Windows DACL and no SYSTEM scheduled task is
# fabricated: the projection is protected by POSIX modes and the host is the
# pid-file ``LinuxHostOps``.


def linux_server_plan(args):
    """Validate the explicit Linux deployment facts and return the plan.

    Fails CLOSED before any listener or file is touched: an absent/relative
    socket path, a Windows named-pipe path, a relative instance socket prefix,
    a missing host command/pid file, or a missing key/projection path raise
    ValueError. The returned dict names the transport (``unix_socket``) and the
    server class the assembly will use.
    """
    from share.broker_client import validate_unix_socket_path
    from share.broker_server import UnixSocketBrokerServer

    socket_path = getattr(args, "socket_path", None)
    if not isinstance(socket_path, str) or not socket_path:
        raise ValueError("linux broker requires an explicit --socket-path")
    if socket_path.startswith("\\\\.\\pipe"):
        raise ValueError("linux broker refuses a Windows named-pipe path")
    socket_path = validate_unix_socket_path(socket_path)
    instance_prefix = getattr(args, "instance_socket_prefix", None)
    if not isinstance(instance_prefix, str) or not instance_prefix:
        raise ValueError("linux broker requires --instance-socket-prefix")
    if not os.path.isabs(instance_prefix):
        raise ValueError("instance socket prefix must be an absolute path")
    if instance_prefix.startswith("\\\\.\\pipe"):
        raise ValueError("linux broker refuses a Windows instance prefix")
    command = getattr(args, "host_command", None)
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError("linux broker requires an explicit --host-command")
    pid_file = getattr(args, "host_pid_file", None)
    if not isinstance(pid_file, str) or not os.path.isabs(pid_file):
        raise ValueError("linux broker requires an absolute --host-pid-file")
    for name in ("key_file", "instance_key_file", "registration_file",
                 "projection_path", "broker_dir"):
        value = getattr(args, name, None)
        if not isinstance(value, str) or not value:
            raise ValueError("linux broker requires --%s"
                             % name.replace("_", "-"))
    return {"transport": "unix_socket",
            "socket_path": socket_path,
            "instance_socket_prefix": instance_prefix,
            "server_class": UnixSocketBrokerServer,
            "host_pid_file": pid_file,
            "host_command": [str(part) for part in command]}


def build_linux_server(args, *, verifier=None, host_ops=None,
                       acl_checker=None, clock=time.monotonic):
    """Assemble the Linux broker host over the AF_UNIX management socket.

    The coordinator, replay registry, protected projection writer, instance
    registry and discovery chain are the SAME product objects the Windows
    assembly uses; only the transports and host ops are POSIX adapters.
    """
    from share.broker_host import (BrokerDiag, BrokerHostCoordinator,
                                   InstanceRegistry, MaintenanceLeaseStore,
                                   ReplayRegistry)
    from share.broker_server import (UnixSocketBrokerServer,
                                     _HostOpsControlCoordinator,
                                     load_registration, read_control_key)
    from share.runtime import ProtectedProjectionWriter

    plan = linux_server_plan(args)
    control_secret = read_control_key(args.key_file)
    instance_secret = read_control_key(args.instance_key_file)
    writer = ProtectedProjectionWriter(
        Path(args.projection_path),
        acl_checker=acl_checker or PosixProjectionAclChecker(
            getattr(args, "expected_uid", None)))
    instances = InstanceRegistry()
    creation_by_instance = {}

    def on_verified(instance_id, evidence):
        if instances.current() != instance_id:
            retired = instances.current()
            if retired is not None:
                creation_by_instance.pop(retired, None)
            instances.register(instance_id)

    diag = BrokerDiag()
    resolved_verifier = verifier or LinuxInstanceVerifier(
        expected_image=getattr(args, "expect_instance_image", None),
        expected_uid=getattr(args, "expected_uid", None),
        expected_cmdline_marker=getattr(
            args, "expect_instance_cmdline_marker", None),
        expected_port=getattr(args, "expect_instance_port", None))
    instance_call = make_unix_instance_call(
        args.instance_key_file,
        plan["instance_socket_prefix"],
        verifier=resolved_verifier,
        expect_image=getattr(args, "expect_instance_image", None),
        expect_uid=getattr(args, "expected_uid", None),
        expect_creation_by_instance=creation_by_instance,
        expect_cmdline_marker=getattr(
            args, "expect_instance_cmdline_marker", None),
        expect_port=getattr(args, "expect_instance_port", None),
        on_verified=on_verified, diag_event=diag.record)
    registration_path = args.registration_file

    def discover_instance():
        candidate = load_registration(registration_path)
        if candidate is None:
            diag.record("registration-read", ok=False,
                        error_code="registration-absent")
            return None
        diag.record("registration-read", ok=True,
                    instance_id=candidate.get("instance_id"))
        try:
            resp = instance_call("instance-status", {},
                                 candidate["instance_id"])
        except Exception as exc:  # noqa: BLE001  already diaged
            diag.record("instance-status", ok=False,
                        exception_type=type(exc).__name__,
                        instance_id=candidate.get("instance_id"))
            raise
        diag.record("instance-status", ok=bool(resp.get("ok")),
                    error_code=resp.get("error_code"),
                    instance_id=candidate.get("instance_id"))
        return candidate["instance_id"] if resp.get("ok") else None

    resolved_host_ops = host_ops or LinuxHostOps(
        plan["host_pid_file"], plan["host_command"],
        state_dir=getattr(args, "state_dir", None),
        expected_uid=getattr(args, "expected_uid", None),
        expected_port=getattr(args, "expect_instance_port", None),
        clock=clock)
    maintenance_cell = {}

    def maintenance_sink(record):
        server = maintenance_cell.get("server")
        if server is not None:
            server._set_maintenance(record)

    coordinator = BrokerHostCoordinator(
        writer=writer, mgmt_secret=control_secret,
        instance_secret=instance_secret, instances=instances,
        replay=ReplayRegistry(), host_ops=resolved_host_ops,
        instance_call=instance_call,
        discover_instance=discover_instance, diag=diag,
        leases=MaintenanceLeaseStore(),
        maintenance=maintenance_sink,
        drain_timeout=getattr(args, "drain_timeout", None) or 60.0,
        stop_timeout=getattr(args, "stop_timeout", None) or 30.0)
    # Only the host-evidence decorator is applied: there is no Tailscale
    # service on this platform, so the ts-service-control verb is absent and
    # an unknown verb is refused by the coordinator (never faked).
    coordinator = _HostOpsControlCoordinator(coordinator, resolved_host_ops)
    server = UnixSocketBrokerServer(
        plan["socket_path"],
        deadline_seconds=getattr(args, "deadline",
                                   DEFAULT_DEADLINE_SECONDS),
        state_dir=Path(args.broker_dir),
        dispatch_deadline_seconds=getattr(args, "dispatch_deadline", None),
        # The REAL management coordinator (same object the Windows assembly
        # passes to ManagementPipeServer): the AF_UNIX server therefore serves
        # publish/load/drain/host-stop/gateway-restart through the same
        # HMAC/replay/whitelist pipeline instead of the base
        # NotImplementedError.
        coordinator=coordinator)
    maintenance_cell["server"] = server
    return server


def linux_main(argv=None):
    """The Linux broker host process entry (AF_UNIX management channel)."""
    import argparse
    parser = argparse.ArgumentParser(
        description="UM share broker host (Linux restricted entry)")
    parser.add_argument("--socket-path", required=True,
                        help="management AF_UNIX socket path")
    parser.add_argument("--key-file", required=True,
                        help="broker-control-key path")
    parser.add_argument("--instance-key-file", required=True,
                        help="instance channel secret (shared)")
    parser.add_argument("--registration-file", required=True,
                        help="instance-registration.json self-published "
                             "by the share instance")
    parser.add_argument("--instance-socket-prefix", required=True,
                        help="instance AF_UNIX socket path prefix")
    parser.add_argument("--projection-path", required=True,
                        help="protected projection landing path")
    parser.add_argument("--broker-dir", required=True,
                        help="broker state dir")
    parser.add_argument("--host-pid-file", required=True,
                        help="share host pid file (LinuxHostOps)")
    # REMAINDER: the host command carries its own option-looking arguments
    # (--config/--port/...); a plain nargs="+" stops at the first of them and
    # the broker then rejects the gateway flags as unknown.  The unit and the
    # fixture put --host-command last so every remaining token belongs to it.
    parser.add_argument("--host-command", nargs=argparse.REMAINDER,
                        required=True,
                        help="fixed share host argv (no shell)")
    parser.add_argument("--state-dir", default=None,
                        help="desired-stop intent directory")
    parser.add_argument("--expected-uid", type=int, default=None,
                        help="expected service uid (identity evidence)")
    parser.add_argument("--expect-instance-image", default=None)
    parser.add_argument("--expect-instance-cmdline-marker", default=None)
    parser.add_argument("--expect-instance-port", type=int, default=None)
    parser.add_argument("--deadline", type=float,
                        default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--dispatch-deadline", type=float, default=130.0)
    args = parser.parse_args(argv)
    server = build_linux_server(args)
    server._write_status()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    server._write_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(linux_main())
