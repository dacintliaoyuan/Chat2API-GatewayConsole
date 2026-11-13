r"""share/broker_ops.py - real OS adapters for the POSIX broker host.

The Linux deployment wires these adapters into ``share/broker_server.py``; each
keeps product logic reviewable offline by injecting its OS boundary:

* ``LinuxHostOps`` - start/stop/verify the share host from a pid file and the
  REAL kernel process facts (``/proc/<pid>`` command line, ``os.kill(pid, 0)``
  liveness). Nothing here calls PowerShell, tasklist, netstat or schtasks.
* ``make_unix_instance_call`` - the coordinator's instance_call over the REAL
  instance channel: an AF_UNIX client to ``<prefix><instance_id>`` signed with
  the shared instance secret.
* ``LinuxInstanceVerifier`` - the peer-identity path over AF_UNIX
  SO_PEERCRED (pid/uid/gid) plus ``/proc/<pid>`` evidence. The DECISION
  logic (fail-closed on unopenable process, image/uid/cmdline mismatch) is
  offline-testable with an injected facade.
* ``PosixProjectionAclChecker`` - the explicit POSIX ACL validator for the
  projection writer: no symlink, private parent, owner uid, no group/other write.

The retired Windows scheduled-task / named-pipe / icacls adapters are gone with
the old local share chain (#33). Nothing here touches the live RunRoot when
imported; privileged actions run only inside the authorized deployment.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from share.broker_client import (BrokerClient, UnixSocketBrokerTransport)
from share.broker_protocol import DEFAULT_DEADLINE_SECONDS


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

    POSIX has no DACL, so this checker verifies the actual POSIX facts
    instead of faking one:

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

    Same trust chain and wire contract as the POSIX instance call: the request
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
