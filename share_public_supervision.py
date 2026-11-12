# -*- coding: utf-8 -*-
"""ai-gateway/share_public_supervision.py - bounded public Funnel supervision.

This module is the ONLY writer of the public-share supervision files:

* ``share-public-state.json``   - latest snapshot, recovery stage, local evidence
* ``share-public-events.jsonl`` - bounded event history
* ``share-public-budget.json``  - rolling 30 minute recovery budget (authority)
* ``share-public-cancel.json``  - human close/cancel signal
* ``share-public-watch.log``    - one bounded decision line per applied round

Import discipline (contract section 2.1): module-level imports are stdlib plus
``console_operations`` ONLY. ``console_share_production``,
``share_backend_supervision`` and ``console_public_probe`` are imported lazily
inside functions. This module NEVER constructs an OperationManager: a second
manager over the shared SQLite would mark the console's in-flight operations
interrupted. The share runtime intent and the ``share:orchestration`` resource
lock are reused from the existing supervision, never re-implemented.

Decision discipline (contract sections 2.10, 3.2 and 3.3):

1. cancel file + running intent written during THIS boot;
2. one bounded public DNS/TLS probe (no secret, no HTTP, no model request);
3. on ``all_failed``: an unconditional 10 second re-check (unless the manual
   fast path is eligible), then a second probe;
4. only when BOTH rounds fail is the recovery snapshot persisted and the local
   preconditions evaluated;
5. mapping resend -> TS stop/start -> second mapping resend, each budget slot
   persisted inside the state lock BEFORE the command is sent;
6. before an apply round probes, a read-only service watchdog starts a STOPPED
   Tailscale service and confirms it running (its own 30 minute budget); it never
   stops the service and never bypasses the cancel/intent/maintenance/security
   gates, so ``not_checked``/``public_url_unavailable`` rounds can recover;
7. the TS restart's stop is sent only after security AND connectivity are
   confirmed while Tailscale still runs; after the stop only the conditions that
   do not need the service are re-read, the start is retried ONCE on a temporary
   failure, and a permanent refusal is never retried;
8. partial availability stops the destructive chain immediately;
9. every action is re-checked against cancel/intent/lifecycle/maintenance/round
   identity while holding ``share:orchestration``; the wait and propagation
   sleeps happen OUTSIDE every lock.

Only the fixed file names of this deployment are read or written; every path is
normalized and its base name re-checked before any I/O.
"""

import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import time

from console_operations import LockBusy, ResourceFileLock

STATE_VERSION = 1
NORMAL_INTERVAL_S = 60
CONFIRM_DELAY_S = 10.0
CONFIRM_DELAY_SLICE_S = 0.5
BUDGET_WINDOW_S = 1800.0
MAPPING_LIMIT = 2
TS_RESTART_LIMIT = 1
SERVICE_START_LIMIT = 3
# A temporary/unconfirmed start is retried ONCE; a permanent refusal is not.
SERVICE_START_ATTEMPTS = 2
ACTION_RETRY_DELAY_S = 1.0
MAPPING_PROPAGATION_S = 15.0
TS_PROPAGATION_S = 20.0
TS_STOP_CONFIRM_S = 30.0
TS_START_CONFIRM_S = 60.0
PROBE_ROUND_TIMEOUT_S = 20.0
CYCLE_DEADLINE_S = 570.0
PUBLIC_CHILD_BUDGET_S = 600
BACKEND_CHILD_BUDGET_S = 180
ACTION_LOCK_RESOURCE = "share:orchestration"
STATE_LOCK_RESOURCE = "share:public-state"
LOCK_TIMEOUT_S = 3.0
LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "console-locks")
FUTURE_TOLERANCE_S = 60.0
MANUAL_SNAPSHOT_MAX_AGE_S = 600.0
EVENT_MAX_AGE_S = 30 * 24 * 3600
EVENT_MAX_BYTES = 10 * 1024 * 1024
EVENT_TARGET_BYTES = 8 * 1024 * 1024
EVENT_MAX_RETURN = 5000
STATUS_EVENT_LIMIT = 50
NOTIFY_TIMEOUT_S = 15.0

DEFAULT_WATCH_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 "SelfUseAI", "ai-gateway", "watch")
STATE_FILE_NAME = "share-public-state.json"
EVENT_FILE_NAME = "share-public-events.jsonl"
BUDGET_FILE_NAME = "share-public-budget.json"
CANCEL_FILE_NAME = "share-public-cancel.json"
LOG_FILE_NAME = "share-public-watch.log"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNCONFIRMED = 2

OVERALLS = ("ok", "degraded", "all_failed", "incomplete", "not_checked")
RECOVERY_STATES = ("idle", "pending", "running", "succeeded", "failed",
                   "manual_required", "budget_exhausted", "stopped")
RECOVERY_ACTIONS = ("none", "mapping_resend", "ts_restart",
                    "mapping_resend_after_ts", "ts_service_start")
RECOVERY_STAGES = ("none", "precheck", "mapping_resend", "ts_stop", "ts_start",
                   "ts_service", "verify", "done")
ACTION_RESULTS = ("ok", "failed", "unconfirmed", "skipped")
EVENT_STATUSES = ("ok", "degraded", "all_failed", "incomplete", "not_checked",
                  "recovery_attempted", "recovered", "manual_required",
                  "budget_exhausted", "stopped", "error")
CANCEL_SOURCES = ("manual_close", "manual_estop", "manual_retry_stop",
                  "repair_limits")

# Test-only injection seams (module-level, default None = production). The
# frozen public signatures stay unchanged; the offline self-test and the unit
# tests inject the boot identity, the runtime intent, the broker lifecycle
# record and the local notification without touching a production path.
_BOOT_ID_READER = None        # () -> str|None
_DESIRED_READER = None        # () -> dict|None
_DESIRED_STOP_READER = None   # () -> dict|None
_WALL_CLOCK = None            # () -> float|None; test-only wall clock
_NOTIFY = None                # (code: str) -> bool

# Permanent action refusals: retrying the same command cannot help, so the
# round goes straight to manual. Everything else is an unconfirmed action.
_PERMANENT_ACTION_CODES = frozenset((
    "share_ts_access_denied", "share_broker_extension_missing",
    "share_broker_unwired", "share_privilege_unwired",
))


def _utc_iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).isoformat()


def _parse_epoch(text):
    """ISO-8601 text -> epoch float, or None (never a guessed time)."""
    import datetime
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    try:
        return stamp.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _fixed_name_path(path, name):
    """Normalize + containment-check a fixed-name state file path."""
    raw = str(path)
    if not raw:
        raise ValueError("path must not be empty")
    if ".." in re.split(r"[\\/]+", raw):
        raise ValueError("path must not contain '..'")
    target = os.path.normpath(os.path.abspath(raw))
    if os.path.basename(target) != name:
        raise ValueError("path must name %r" % name)
    return target


def _paths(state_dir=None):
    """The five fixed public-supervision paths for one watch directory."""
    if state_dir is not None and ".." in re.split(r"[\\/]+", str(state_dir)):
        raise ValueError("state_dir must not contain '..'")
    base = os.path.normpath(os.path.abspath(state_dir or DEFAULT_WATCH_DIR))
    return {
        "base": base,
        "state": _fixed_name_path(os.path.join(base, STATE_FILE_NAME),
                                  STATE_FILE_NAME),
        "event": _fixed_name_path(os.path.join(base, EVENT_FILE_NAME),
                                  EVENT_FILE_NAME),
        "budget": _fixed_name_path(os.path.join(base, BUDGET_FILE_NAME),
                                   BUDGET_FILE_NAME),
        "cancel": _fixed_name_path(os.path.join(base, CANCEL_FILE_NAME),
                                   CANCEL_FILE_NAME),
        "log": _fixed_name_path(os.path.join(base, LOG_FILE_NAME),
                                LOG_FILE_NAME),
    }


def _atomic_write(path, text):
    """tmp + fsync + os.replace, mode 0o600, best-effort directory fsync."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = text.encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _read_json(path):
    """``(record, reason)``; reason is missing/unreadable/malformed."""
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
    except OSError:
        return None, "unreadable"
    except (ValueError, UnicodeError):
        return None, "malformed"
    if not isinstance(record, dict):
        return None, "malformed"
    return record, None


# ---------------------------------------------------------------------------
# Runtime intent / lifecycle / production seams (lazy imports only)
# ---------------------------------------------------------------------------

def _boot_id():
    if _BOOT_ID_READER is not None:
        try:
            value = _BOOT_ID_READER()
        except Exception:  # noqa: BLE001 - an injected reader stays honest
            return None
        return value if isinstance(value, str) and value else None
    try:
        import share_backend_supervision as backend
        value = backend.current_boot_id()
    except Exception:  # noqa: BLE001 - unavailable boot identity stays None
        return None
    return value if isinstance(value, str) and value else None


def _read_desired():
    if _DESIRED_READER is not None:
        try:
            record = _DESIRED_READER()
        except Exception:  # noqa: BLE001
            return None
        return record if isinstance(record, dict) else None
    try:
        import share_backend_supervision as backend
        record = backend.read_desired_state()
    except Exception:  # noqa: BLE001
        return None
    return record if isinstance(record, dict) else None


def _desired_stop():
    if _DESIRED_STOP_READER is not None:
        try:
            record = _DESIRED_STOP_READER()
        except Exception:  # noqa: BLE001 - no authority is not "stopped"
            return None
        return record if isinstance(record, dict) else None
    try:
        import console_share_production as production
        record = production.production_desired_stop_record()
    except Exception:  # noqa: BLE001
        return None
    return record if isinstance(record, dict) else None


def _load_production_seams():
    try:
        import console_share_production as production
        seams = production.production_seams()
    except Exception:  # noqa: BLE001 - an unwired deployment stays honest
        return {}
    return seams if isinstance(seams, dict) else {}


def _public_url(provider):
    """The current normalized public share URL, or None.

    The explicit configured origin (``CONSOLE_PUBLIC_ORIGIN``, D18-1) wins over
    the historical Tailscale Funnel URL; an invalid configured value is an error
    and is NOT replaced by the Funnel URL. With the key absent the Funnel URL is
    read exactly as before.
    """
    if provider is not None:
        try:
            value = provider()
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, str) and value else None
    try:
        import console_public_url
        configured = console_public_url.resolve(None)
        if configured.source is not None:
            return configured.url
        if configured.reason == console_public_url.INVALID:
            return None
    except Exception:  # noqa: BLE001 - unavailable URL stays None
        return None
    try:
        import console_share_production as production
        value = production.production_share_public_url()
    except Exception:  # noqa: BLE001 - unavailable URL stays None
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return console_public_url.resolve(value).url
    except Exception:  # noqa: BLE001 - unavailable URL stays None
        return None


def _host_of(url):
    if not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host if isinstance(host, str) and host else None


def _default_probe(public_url):
    import console_public_probe
    return console_public_probe.check_public_connection(
        public_url, deadline_s=PROBE_ROUND_TIMEOUT_S)


def _run_probe(probe, public_url, now, *, target_reader=None):
    """Run one probe; an internal error becomes ``probe_failed``, never a guess."""
    fn = probe if probe is not None else _default_probe
    try:
        if target_reader is not None and target_reader() != public_url:
            result = {"overall": "not_checked", "reason": "round_superseded",
                      "entries": [], "public_url": public_url}
        else:
            result = fn(public_url)
            if target_reader is not None and target_reader() != public_url:
                result = {"overall": "not_checked", "reason": "round_superseded",
                          "entries": [], "public_url": public_url}
    except Exception:  # noqa: BLE001 - the supervisor records probe_failed
        result = None
    if not isinstance(result, dict) or result.get("overall") not in OVERALLS:
        result = {
            "version": 1, "checked_at_utc": _utc_iso(now),
            "checked_at_epoch": now, "public_url": public_url,
            "host": _host_of(public_url), "overall": "not_checked",
            "reason": "probe_failed", "deadline_s": PROBE_ROUND_TIMEOUT_S,
            "elapsed_ms": 0, "entries": [],
        }
    entries = []
    for entry in result.get("entries") if isinstance(
            result.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        entries.append({
            "host": entry.get("host"), "ip": entry.get("ip"),
            "state": entry.get("state") if entry.get("state") in (
                "ok", "failed", "unchecked") else "unchecked",
            "dns": entry.get("dns") if entry.get("dns") in (
                "ok", "failed", "not_checked") else "not_checked",
            "tls": entry.get("tls") if entry.get("tls") in (
                "ok", "failed", "not_checked") else "not_checked",
            "certificate": entry.get("certificate") if entry.get(
                "certificate") in ("ok", "failed", "not_checked")
            else "not_checked",
            "reason": entry.get("reason"),
            "elapsed_ms": entry.get("elapsed_ms")
            if isinstance(entry.get("elapsed_ms"), int) else 0,
            "checked_at_epoch": entry.get("checked_at_epoch"),
        })
    result = dict(result)
    result["entries"] = entries
    result["dns"] = _sanitize_dns(result.get("dns"))
    if not isinstance(result.get("public_url"), str):
        result["public_url"] = public_url if isinstance(public_url, str) else None
    if not isinstance(result.get("host"), str):
        result["host"] = _host_of(result.get("public_url"))
    return result


# ---------------------------------------------------------------------------
# Cancel file
# ---------------------------------------------------------------------------

def read_cancel(*, state_dir=None):
    """The human cancel signal; damaged/foreign content is fail-closed."""
    path = _paths(state_dir)["cancel"]
    record, reason = _read_json(path)
    if reason == "missing":
        return {"cancelled": False, "reason": None, "unreadable": False}
    if reason is not None:
        return {"cancelled": True, "reason": "cancel_unreadable",
                "unreadable": True}
    if record.get("version") != STATE_VERSION:
        return {"cancelled": True, "reason": "cancel_unreadable",
                "unreadable": True}
    source = record.get("source")
    reason_code = record.get("reason")
    if source not in CANCEL_SOURCES or not isinstance(reason_code, str):
        return {"cancelled": True, "reason": "cancel_unreadable",
                "unreadable": True}
    return {"cancelled": True, "reason": reason_code, "unreadable": False,
            "source": source, "at_utc": record.get("at_utc")}


def _write_cancel(paths, reason, now):
    record = {
        "version": STATE_VERSION,
        "at_utc": _utc_iso(now),
        "source": reason if reason in CANCEL_SOURCES else "manual_close",
        "reason": reason if reason in CANCEL_SOURCES else "manual_close",
    }
    _atomic_write(paths["cancel"], json.dumps(record, ensure_ascii=False))


def _state_lock_dir(state_dir):
    """A test/TEMP state directory also isolates the state lock files."""
    if state_dir is None:
        return None
    return os.path.join(_paths(state_dir)["base"], "locks")


def request_cancel(reason, *, state_dir=None):
    """Idempotent cancel write inside the ``share:public-state`` lock."""
    paths = _paths(state_dir)
    source = reason if reason in CANCEL_SOURCES else "manual_close"

    def _work():
        try:
            _write_cancel(paths, source, time.time())
        except OSError:
            return {"ok": False}
        return {"ok": True}

    result = _with_state_lock(_state_lock_dir(state_dir), _work)
    return bool(result.get("ok"))


def clear_cancel(*, state_dir=None):
    paths = _paths(state_dir)

    def _work():
        try:
            os.remove(paths["cancel"])
        except FileNotFoundError:
            pass
        except OSError:
            return {"ok": False}
        return {"ok": True}

    result = _with_state_lock(_state_lock_dir(state_dir), _work)
    return bool(result.get("ok"))


# ---------------------------------------------------------------------------
# Budget (the ONLY authority; never reset by success/restart)
# ---------------------------------------------------------------------------

def _empty_budget(now):
    return {
        "version": STATE_VERSION, "window_s": BUDGET_WINDOW_S,
        "mapping_attempts_utc": [], "ts_restarts_utc": [],
        "service_starts_utc": [],
        "updated_at_utc": _utc_iso(now),
    }


def _budget_entries(raw, key, *, default_empty=False):
    """Budget timestamps; an absent NEW key reads as empty, never unreadable.

    ``service_starts_utc`` was added after the first deployed budget files, so a
    file written by an older version simply has no service-start history. Its
    absence is a legitimate empty list; a present-but-malformed value is still
    unreadable. The two original keys stay required.
    """
    if default_empty and key not in raw:
        return []
    entries = raw.get(key)
    if not isinstance(entries, list):
        raise ValueError(key)
    out = []
    for value in entries:
        if not isinstance(value, str):
            raise ValueError(key)
        epoch = _parse_epoch(value)
        if epoch is None or not math.isfinite(epoch):
            raise ValueError(key)
        out.append((value, epoch))
    return out


def _budget_view(paths, now, *, state_exists=None):
    """The read-only budget view; expired entries are pruned from the view."""
    if state_exists is None:
        state_exists = os.path.exists(paths["state"])
    raw, reason = _read_json(paths["budget"])
    if reason == "missing":
        if not state_exists:
            return _view_from_entries([], [], [], now, bootstrap=True)
        return _unreadable_budget("budget_unreadable")
    if reason is not None:
        return _unreadable_budget("budget_unreadable")
    if raw.get("version") != STATE_VERSION:
        return _unreadable_budget("budget_unreadable")
    try:
        mapping = _budget_entries(raw, "mapping_attempts_utc")
        restarts = _budget_entries(raw, "ts_restarts_utc")
        service_starts = _budget_entries(
            raw, "service_starts_utc", default_empty=True)
    except ValueError:
        return _unreadable_budget("budget_unreadable")
    for _, epoch in mapping + restarts + service_starts:
        if epoch > now + FUTURE_TOLERANCE_S:
            return _unreadable_budget("budget_unreadable")
    mapping = [(text, epoch) for text, epoch in mapping
               if epoch >= now - BUDGET_WINDOW_S]
    restarts = [(text, epoch) for text, epoch in restarts
                if epoch >= now - BUDGET_WINDOW_S]
    service_starts = [(text, epoch) for text, epoch in service_starts
                      if epoch >= now - BUDGET_WINDOW_S]
    return _view_from_entries(mapping, restarts, service_starts, now)


def _unreadable_budget(reason):
    return {
        "unreadable": True, "reason": reason, "window_s": BUDGET_WINDOW_S,
        "mapping_attempts_utc": [], "ts_restarts_utc": [],
        "service_starts_utc": [],
        "mapping_used": 0, "mapping_limit": MAPPING_LIMIT,
        "ts_restart_used": 0, "ts_restart_limit": TS_RESTART_LIMIT,
        "service_start_used": 0,
        "service_start_limit": SERVICE_START_LIMIT,
        "mapping_next_eligible_at_utc": None,
        "ts_restart_next_eligible_at_utc": None,
        "service_start_next_eligible_at_utc": None,
    }


def _next_eligible(entries, limit, now):
    if len(entries) < limit:
        return None
    return _utc_iso(entries[len(entries) - limit][1] + BUDGET_WINDOW_S)


def _view_from_entries(mapping, restarts, service_starts, now,
                        bootstrap=False):
    mapping_text = [text for text, _ in mapping]
    restart_text = [text for text, _ in restarts]
    service_start_text = [text for text, _ in service_starts]
    return {
        "unreadable": False, "reason": None, "bootstrap": bool(bootstrap),
        "window_s": BUDGET_WINDOW_S,
        "mapping_attempts_utc": mapping_text,
        "ts_restarts_utc": restart_text,
        "service_starts_utc": service_start_text,
        "mapping_used": len(mapping_text),
        "mapping_limit": MAPPING_LIMIT,
        "ts_restart_used": len(restart_text),
        "ts_restart_limit": TS_RESTART_LIMIT,
        "service_start_used": len(service_start_text),
        "service_start_limit": SERVICE_START_LIMIT,
        "mapping_next_eligible_at_utc": _next_eligible(
            mapping, MAPPING_LIMIT, now),
        "ts_restart_next_eligible_at_utc": _next_eligible(
            restarts, TS_RESTART_LIMIT, now),
        "service_start_next_eligible_at_utc": _next_eligible(
            service_starts, SERVICE_START_LIMIT, now),
    }


def _budget_allows(view):
    """Whether the recovery chain (mapping/TS restart) has any slot left.

    Service starts are a separate watchdog budget and never authorize the
    destructive recovery chain by themselves.
    """
    return (not view.get("unreadable")
            and (view.get("mapping_used", 0) < MAPPING_LIMIT
                 or view.get("ts_restart_used", 0) < TS_RESTART_LIMIT))


def _write_budget(paths, view, now):
    raw = {
        "version": STATE_VERSION, "window_s": BUDGET_WINDOW_S,
        "mapping_attempts_utc": list(view["mapping_attempts_utc"]),
        "ts_restarts_utc": list(view["ts_restarts_utc"]),
        "service_starts_utc": list(view.get("service_starts_utc", [])),
        "updated_at_utc": _utc_iso(now),
    }
    _atomic_write(paths["budget"], json.dumps(raw, ensure_ascii=False))


def _reserve_budget_in_lock(paths, kind, now):
    """Read-prune-append-atomic-write; the caller holds the state lock."""
    view = _budget_view(paths, now, state_exists=os.path.exists(paths["state"]))
    if view.get("unreadable"):
        return {"ok": False, "reason": "budget_unreadable"}
    mapping = [(text, _parse_epoch(text))
               for text in view["mapping_attempts_utc"]]
    restarts = [(text, _parse_epoch(text))
                for text in view["ts_restarts_utc"]]
    service_starts = [(text, _parse_epoch(text))
                      for text in view["service_starts_utc"]]
    if kind == "mapping":
        if view["mapping_used"] >= MAPPING_LIMIT:
            return {"ok": False, "reason": "budget_exhausted", "view": view}
        mapping.append((_utc_iso(now), now))
    elif kind == "ts_restart":
        if view["ts_restart_used"] >= TS_RESTART_LIMIT:
            return {"ok": False, "reason": "budget_exhausted", "view": view}
        restarts.append((_utc_iso(now), now))
    elif kind == "service_start":
        if view["service_start_used"] >= SERVICE_START_LIMIT:
            return {"ok": False, "reason": "budget_exhausted", "view": view}
        service_starts.append((_utc_iso(now), now))
    else:
        return {"ok": False, "reason": "budget_unreadable", "view": view}
    new_view = _view_from_entries(mapping, restarts, service_starts, now)
    try:
        _write_budget(paths, new_view, now)
    except OSError:
        return {"ok": False, "reason": "budget_unreadable", "view": view}
    return {"ok": True, "view": new_view}


# ---------------------------------------------------------------------------
# Events (30 days / 10 MB bounded history; no host, IP or secret)
# ---------------------------------------------------------------------------

def _event_record(event):
    return {
        "version": STATE_VERSION,
        "at_utc": event.get("at_utc"),
        "status": event.get("status"),
        "code": event.get("code"),
        "action": event.get("action") or "none",
    }


def _valid_event(record, now):
    if not isinstance(record, dict) or record.get("version") != STATE_VERSION:
        return None
    if record.get("status") not in EVENT_STATUSES:
        return None
    code = record.get("code")
    action = record.get("action") or "none"
    if not isinstance(code, str) or action not in RECOVERY_ACTIONS:
        return None
    epoch = _parse_epoch(record.get("at_utc"))
    if epoch is None:
        return None
    if epoch < now - EVENT_MAX_AGE_S:
        return None
    return {"version": STATE_VERSION, "at_utc": record.get("at_utc"),
            "status": record.get("status"), "code": code, "action": action,
            "_epoch": epoch}


def _parse_events(text, now):
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        valid = _valid_event(record, now)
        if valid is not None:
            kept.append(valid)
    return kept


def _events_text(records):
    return "".join(json.dumps(
        {"version": STATE_VERSION, "at_utc": item["at_utc"],
         "status": item["status"], "code": item["code"],
         "action": item["action"]}, ensure_ascii=False) + "\n"
        for item in records)


def _append_event_in_lock(paths, event, now, *, force=False):
    """Append one event; the caller holds ``share:public-state``."""
    try:
        if os.path.exists(paths["event"]):
            with open(paths["event"], encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            text = ""
    except OSError:
        return "events_unreadable"
    record = _event_record(event)
    record["_epoch"] = _parse_epoch(record["at_utc"]) or now
    # Prune against the event's OWN time: a recovery event written minutes
    # after the round started must not age out 30-day-old entries early.
    kept = _parse_events(text, record["_epoch"])
    original_count = len(kept)
    if not force and kept and kept[-1]["status"] == record["status"] \
            and kept[-1]["code"] == record["code"] \
            and kept[-1]["action"] == record["action"]:
        return "duplicate"
    kept.append(record)
    rendered = _events_text(kept)
    if len(rendered.encode("utf-8")) > EVENT_MAX_BYTES:
        while kept and len(_events_text(kept).encode("utf-8")) \
                > EVENT_TARGET_BYTES:
            kept.pop(0)
        rendered = _events_text(kept)
    pruned = len(kept) != original_count + 1
    try:
        _atomic_write(paths["event"], rendered)
    except OSError:
        return "event_prune_failed" if pruned else "event_write_failed"
    return "ok"


def _read_events(paths, *, now, limit):
    if not os.path.exists(paths["event"]):
        return [], False, False
    try:
        with open(paths["event"], encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, None, True
    kept = _parse_events(text, now)
    truncated = len(kept) > limit
    kept = kept[-limit:]
    kept.reverse()
    return [{"at_utc": item["at_utc"], "status": item["status"],
             "code": item["code"], "action": item["action"]}
            for item in kept], truncated, False


# ---------------------------------------------------------------------------
# State file (snapshot) and monotonic protection
# ---------------------------------------------------------------------------

def _read_state(paths):
    record, reason = _read_json(paths["state"])
    if reason is not None:
        return None, reason
    if record.get("version") != STATE_VERSION:
        return None, "version_mismatch"
    return record, None


def _intent_view(desired):
    if not isinstance(desired, dict):
        return {"state": "unknown", "at_utc": None, "boot_id": None,
                "source": None}
    return {
        "state": desired.get("state")
        if desired.get("state") in ("running", "stopped") else "unknown",
        "at_utc": desired.get("at_utc") if isinstance(
            desired.get("at_utc"), str) else None,
        "boot_id": desired.get("boot_id") if isinstance(
            desired.get("boot_id"), str) else None,
        "source": desired.get("source") if isinstance(
            desired.get("source"), str) else None,
    }


def _cancel_view(cancel):
    return {"cancelled": bool(cancel.get("cancelled")),
            "reason": cancel.get("reason")}


def _notification_view(prior):
    if isinstance(prior, dict):
        value = prior.get("notification")
        if isinstance(value, dict):
            return {"last_key": value.get("last_key"),
                    "last_at_utc": value.get("last_at_utc"),
                    "last_result": value.get("last_result")}
    return {"last_key": None, "last_at_utc": None, "last_result": None}


def _recovery_view(prior):
    """Preserve a non-idle recovery block so old action evidence is not lost."""
    if isinstance(prior, dict):
        recovery = prior.get("recovery")
        if isinstance(recovery, dict) and recovery.get("state") in (
                "pending", "running", "failed", "manual_required",
                "budget_exhausted", "stopped"):
            return _normalized_recovery(recovery)
    return _idle_recovery()


def _idle_recovery():
    return {"state": "idle", "action": "none", "stage": "none",
            "reason": None, "attempted_at_utc": None,
            "finished_at_utc": None, "manual": False, "actions": []}


def _normalized_recovery(recovery):
    actions = []
    for item in recovery.get("actions") if isinstance(
            recovery.get("actions"), list) else []:
        if not isinstance(item, dict):
            continue
        actions.append({
            "action": item.get("action") if item.get("action") in (
                "mapping_resend", "ts_restart", "mapping_resend_after_ts",
                "ts_service_start") else "mapping_resend",
            "at_utc": item.get("at_utc"),
            "result": item.get("result") if item.get("result") in (
                "ok", "failed", "unconfirmed", "skipped") else "failed",
        })
    return {
        "state": recovery.get("state") if recovery.get("state") in (
            RECOVERY_STATES) else "idle",
        "action": recovery.get("action") if recovery.get("action") in (
            RECOVERY_ACTIONS) else "none",
        "stage": recovery.get("stage") if recovery.get("stage") in (
            RECOVERY_STAGES) else "none",
        "reason": recovery.get("reason") if isinstance(
            recovery.get("reason"), str) else None,
        "attempted_at_utc": recovery.get("attempted_at_utc"),
        "finished_at_utc": recovery.get("finished_at_utc"),
        "manual": bool(recovery.get("manual")),
        "actions": actions,
    }


def _round_id(now):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return "%s-%d-%s" % (stamp, os.getpid(), os.urandom(2).hex())


def _local_default():
    return {"ts_service": "unavailable", "connectivity": "unavailable",
            "backend": "unavailable", "auth": "unavailable",
            "gateway": "unavailable", "security_gate": "unavailable",
            "maintenance": "unavailable", "lifecycle": "unavailable"}


def _lifecycle_local():
    """The cheap lifecycle label for a normal/gate snapshot."""
    local = _local_default()
    record = _desired_stop()
    if isinstance(record, dict):
        local["lifecycle"] = "stopped" if record.get("state") == "stopped" \
            else "running"
    return local


def _make_snapshot(*, boot_id, now, intent, probe, round_id=None,
                   phase="normal", confirmation=0, next_check_at=None,
                   next_action_at=None, local=None, recovery=None,
                   budget=None, cancel=None, notification=None, dns=None):
    entries = probe.get("entries") if isinstance(
        probe.get("entries"), list) else []
    return {
        "version": STATE_VERSION,
        "boot_id": boot_id,
        "observed_at_utc": _utc_iso(now),
        "observed_at_epoch": float(now),
        "intent": _intent_view(intent),
        "public_url": probe.get("public_url"),
        "host": probe.get("host"),
        "round": {
            "id": round_id or _round_id(now),
            "phase": phase,
            "started_at_utc": _utc_iso(now),
            "finished_at_utc": _utc_iso(now),
            "confirmation": int(confirmation),
            "next_check_at_utc": next_check_at,
            "next_action_at_utc": next_action_at,
        },
        "overall": probe.get("overall") if probe.get("overall") in (
            OVERALLS) else "not_checked",
        "reason": probe.get("reason") if isinstance(
            probe.get("reason"), str) else "probe_failed",
        "entries": entries,
        "dns": dns if isinstance(dns, dict) else (
            probe.get("dns") if isinstance(probe.get("dns"), dict) else None),
        "local": local if isinstance(local, dict) else _local_default(),
        "recovery": recovery if isinstance(recovery, dict)
        else _idle_recovery(),
        "budget": budget if isinstance(budget, dict)
        else _unreadable_budget("budget_unreadable"),
        "cancel": _cancel_view(cancel or {"cancelled": False,
                                          "reason": None}),
        "notification": notification if isinstance(notification, dict)
        else {"last_key": None, "last_at_utc": None, "last_result": None},
        "state_file": STATE_FILE_NAME,
        "event_file": EVENT_FILE_NAME,
        "budget_file": BUDGET_FILE_NAME,
        "cancel_file": CANCEL_FILE_NAME,
    }


def _with_state_lock(lock_dir, fn):
    lock = None
    try:
        lock = ResourceFileLock(lock_dir or LOCK_DIR, STATE_LOCK_RESOURCE,
                                owner="share-public-supervision",
                                timeout_s=LOCK_TIMEOUT_S)
        lock.acquire()
    except LockBusy:
        return {"ok": False, "reason": "share_public_busy"}
    except Exception:  # noqa: BLE001 - an unusable lock refuses
        return {"ok": False, "reason": "share_public_busy"}
    try:
        return fn()
    finally:
        try:
            lock.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - releasing stays best effort
            pass


def _with_action_lock(lock_dir, fn):
    lock = None
    try:
        lock = ResourceFileLock(lock_dir or LOCK_DIR, ACTION_LOCK_RESOURCE,
                                owner="share-public-supervision",
                                timeout_s=LOCK_TIMEOUT_S)
        lock.acquire()
    except LockBusy:
        return {"ok": False, "reason": "share_public_busy"}
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "share_public_busy"}
    try:
        return fn()
    finally:
        try:
            lock.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def _persist_snapshot(paths, lock_dir, snapshot, *, event=None, force_event=False,
                     binding_check=None):
    """Write one snapshot inside ``share:public-state`` (monotonic protected)."""

    def _work():
        if binding_check is not None:
            binding_reason = binding_check()
            if binding_reason is not None:
                return {"ok": False, "reason": binding_reason}
        state_existed = os.path.exists(paths["state"])
        current, reason = _read_state(paths)
        if current is not None:
            previous = current.get("observed_at_epoch")
            if isinstance(previous, (int, float)) and not isinstance(
                    previous, bool):
                new_epoch = float(snapshot["observed_at_epoch"])
                if new_epoch < float(previous):
                    return {"ok": False, "reason": "stale_result"}
                current_round = current.get("round")
                new_round = snapshot.get("round")
                if new_epoch == float(previous) \
                        and isinstance(current_round, dict) \
                        and isinstance(new_round, dict) \
                        and current_round.get("id") != new_round.get("id"):
                    return {"ok": False, "reason": "stale_result"}
        elif reason == "unreadable":
            # A read failure is UNKNOWN, never "missing": overwriting it could
            # destroy the only copy of a consumed budget.
            return {"ok": False, "reason": "state_unreadable"}
        elif reason in ("malformed", "version_mismatch"):
            corrupt = paths["state"] + ".corrupt"
            try:
                os.replace(paths["state"], corrupt)
            except OSError:
                return {"ok": False, "reason": "state_unreadable"}
        if not state_existed and not os.path.exists(paths["budget"]):
            try:
                _atomic_write(paths["budget"], json.dumps(
                    _empty_budget(float(snapshot["observed_at_epoch"])),
                    ensure_ascii=False))
            except OSError:
                return {"ok": False, "reason": "state_unreadable"}
        try:
            _atomic_write(paths["state"], json.dumps(
                snapshot, ensure_ascii=False))
        except OSError:
            return {"ok": False, "reason": "state_unreadable"}
        event_result = None
        if event is not None:
            event_result = _append_event_in_lock(
                paths, event, float(snapshot["observed_at_epoch"]),
                force=force_event)
            if event_result in ("event_write_failed", "events_unreadable",
                                "event_prune_failed"):
                _append_log(paths, {
                    "at_utc": _utc_iso(float(snapshot["observed_at_epoch"])),
                    "mode": "event", "overall": snapshot.get("overall"),
                    "code": event_result, "action": "none"})
        return {"ok": True, "event": event_result}

    return _with_state_lock(lock_dir, _work)


def _append_log(paths, record):
    """One bounded JSON line in the decision log (best effort, 1 MB rotate)."""
    try:
        if os.path.exists(paths["log"]) \
                and os.path.getsize(paths["log"]) > 1024 * 1024:
            os.replace(paths["log"], paths["log"] + ".1")
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(paths["log"]), exist_ok=True)
        with open(paths["log"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Round identity
# ---------------------------------------------------------------------------

def _same_round(current, authorized):
    if not isinstance(current, dict) or not isinstance(authorized, dict):
        return False
    current_round = current.get("round")
    authorized_round = authorized.get("round")
    current_intent = current.get("intent")
    authorized_intent = authorized.get("intent")
    if not isinstance(current_round, dict) \
            or not isinstance(authorized_round, dict) \
            or not isinstance(current_intent, dict) \
            or not isinstance(authorized_intent, dict):
        return False
    return (
        current_round.get("id") == authorized_round.get("id")
        and current.get("observed_at_epoch")
        == authorized.get("observed_at_epoch")
        and current.get("overall") == authorized.get("overall")
        and current.get("public_url") == authorized.get("public_url")
        and current_intent.get("at_utc") == authorized_intent.get("at_utc")
    )


def _still_round(paths, authorized):
    current, _ = _read_state(paths)
    return _same_round(current, authorized)


def _same_intent(current, expected):
    """Compare the live authority, not two copies of a persisted snapshot."""
    return (isinstance(current, dict) and isinstance(expected, dict)
            and all(current.get(key) == expected.get(key)
                    for key in ("state", "boot_id", "at_utc")))


def _live_binding_reason(expected, *, state_dir, process_boot):
    """Fast file-only guard using this run's already validated boot identity.

    A process does not survive a machine reboot. Re-querying CIM under the
    state lock adds latency without strengthening the commit boundary.
    """
    cancel = read_cancel(state_dir=state_dir)
    if cancel.get("cancelled"):
        return "cancel_unreadable" if cancel.get("unreadable") \
            else "cancel_requested"
    desired = _read_desired()
    gate = _intent_gate(desired, process_boot)
    if gate is not None:
        return gate
    if not _same_intent(desired, expected):
        return "round_superseded"
    record = _desired_stop()
    if isinstance(record, dict) and record.get("state") == "stopped":
        return "managed_stop_in_force"
    return None


def _copy_snapshot(snapshot):
    """A private mutable copy; the authorized object itself stays immutable."""
    return json.loads(json.dumps(snapshot, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Maintenance lease mapping
# ---------------------------------------------------------------------------

def _maintenance_from_payload(payload, now):
    if payload is None:
        return {"state": "unavailable", "payload": None}
    if not isinstance(payload, dict) or "lease" not in payload:
        return {"state": "unavailable", "payload": None}
    lease = payload.get("lease")
    if lease is None:
        return {"state": "none", "payload": payload}
    if not isinstance(lease, dict):
        return {"state": "unavailable", "payload": None}
    expired = payload.get("lease_expired")
    if expired is True:
        return {"state": "expired", "payload": payload}
    if expired is False:
        return {"state": "in_force", "payload": payload}
    expires = lease.get("lease_expires_at")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        return {"state": "expired" if float(expires) <= now else "in_force",
                "payload": payload}
    if isinstance(expires, str):
        epoch = _parse_epoch(expires)
        if epoch is not None:
            return {"state": "expired" if epoch <= now else "in_force",
                    "payload": payload}
    # Unknown expiry shape: an object lease is never treated as cleared.
    return {"state": "in_force", "payload": payload}


def _maintenance_view(seams, now):
    fn = seams.get("_MAINTENANCE_STATUS") if isinstance(seams, dict) else None
    if not callable(fn):
        return {"state": "unavailable", "payload": None}
    try:
        payload = fn()
    except Exception:  # noqa: BLE001 - unreadable is never "no lease"
        return {"state": "unavailable", "payload": None}
    return _maintenance_from_payload(payload, now)


# ---------------------------------------------------------------------------
# Read-only observations used by the full precheck and run_check
# ---------------------------------------------------------------------------

def _host_evidence(seams):
    ops = seams.get("_BROKER_OPS") if isinstance(seams, dict) else None
    fn = getattr(ops, "host_evidence", None)
    if not callable(fn):
        return None
    try:
        return fn("limits")
    except Exception:  # noqa: BLE001
        return None


def _observe_security(seams, local):
    evidence = _host_evidence(seams)
    verdict = evidence.get("verdict") if isinstance(evidence, dict) else None
    if verdict is True:
        local["security_gate"] = "ok"
        return True, None
    if verdict is False:
        local["security_gate"] = "not_satisfied"
        return False, "security_not_satisfied"
    local["security_gate"] = "unconfirmed"
    return False, "security_unconfirmed"


def _observe_connectivity(seams, local):
    state = None
    fn = seams.get("_CONNECTIVITY_STATE") if isinstance(seams, dict) else None
    if callable(fn):
        try:
            state = fn()
        except Exception:  # noqa: BLE001
            state = None
    if state in ("NeedsLogin", "RequiresAuth"):
        local["connectivity"] = "login_required"
        return False, "human_login_required"
    ok_fn = seams.get("_CONNECTIVITY_OK") if isinstance(seams, dict) else None
    if not callable(ok_fn):
        local["connectivity"] = "unavailable"
        return False, "connectivity_unconfirmed"
    try:
        ok = ok_fn()
    except Exception:  # noqa: BLE001
        ok = None
    if ok is True:
        local["connectivity"] = "ok"
        return True, None
    if ok is False:
        local["connectivity"] = "offline"
        return False, "tailscale_offline"
    local["connectivity"] = "unconfirmed"
    return False, "connectivity_unconfirmed"


def _observe_gateway(seams, local):
    ops = seams.get("_BROKER_OPS") if isinstance(seams, dict) else None
    fn = getattr(ops, "verify_running", None)
    if not callable(fn):
        local["gateway"] = "unavailable"
        return False, "gateway_unconfirmed"
    try:
        running = fn()
    except Exception:  # noqa: BLE001
        running = None
    if running is True:
        local["gateway"] = "running"
        return True, None
    if running is False:
        local["gateway"] = "not_running"
        return False, "gateway_not_running"
    local["gateway"] = "unconfirmed"
    return False, "gateway_unconfirmed"


def _observe_backend(seams, local):
    fn = seams.get("_BACKEND_OBSERVE") if isinstance(seams, dict) else None
    observe = None
    if callable(fn):
        try:
            observe = fn(forget_stale=False)
        except TypeError:
            try:
                observe = fn()
            except Exception:  # noqa: BLE001
                observe = None
        except Exception:  # noqa: BLE001
            observe = None
    if not isinstance(observe, dict):
        local["backend"] = "unavailable"
        local["auth"] = "unavailable"
        return False, "backend_unconfirmed"
    running = observe.get("running")
    auth = observe.get("auth_state")
    if running is True:
        local["backend"] = "running"
    elif running is False:
        local["backend"] = "not_running"
        local["auth"] = "unavailable"
        return False, "backend_not_running"
    else:
        local["backend"] = "unavailable"
        local["auth"] = "unavailable"
        return False, "backend_unconfirmed"
    # ONLY the two labels leave this function: never a token or response body.
    local["auth"] = auth if isinstance(auth, str) and auth else "unavailable"
    if auth != "valid":
        return False, "auth_not_ready"
    return True, None


def _observe_ts_service(seams, local):
    fn = seams.get("_TS_SERVICE_STATUS") if isinstance(seams, dict) else None
    if not callable(fn):
        local["ts_service"] = "unavailable"
        return None
    try:
        state = fn()
    except Exception:  # noqa: BLE001
        state = None
    local["ts_service"] = state if state in ("running", "stopped") \
        else "unavailable"
    return local["ts_service"]


def _observe_local(seams, now):
    """Read-only local evidence for ``run_check`` (never recovery-gating)."""
    local = _local_default()
    record = _desired_stop()
    if isinstance(record, dict):
        local["lifecycle"] = "stopped" if record.get("state") == "stopped" \
            else "running"
    else:
        local["lifecycle"] = "unavailable"
    _observe_ts_service(seams, local)
    _observe_connectivity(seams, local)
    _observe_gateway(seams, local)
    _observe_backend(seams, local)
    _observe_security(seams, local)
    view = _maintenance_view(seams, now)
    local["maintenance"] = {
        "none": "none", "in_force": "active", "expired": "expired",
        "unavailable": "unavailable"}[view["state"]]
    return local


# ---------------------------------------------------------------------------
# Manual retry eligibility
# ---------------------------------------------------------------------------

def _manual_retry_eligible(prior, current, *, now, boot_id, desired):
    """The frozen manual fast path (contract section 2.10)."""
    if not isinstance(prior, dict) or not isinstance(current, dict):
        return False
    if prior.get("overall") != "all_failed":
        return False
    if current.get("overall") != "all_failed":
        return False
    epoch = prior.get("observed_at_epoch")
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return False
    age = float(now) - float(epoch)
    if age < 0 or age > MANUAL_SNAPSHOT_MAX_AGE_S:
        return False
    if prior.get("boot_id") != boot_id:
        return False
    if not isinstance(desired, dict):
        return False
    prior_intent = prior.get("intent")
    if not isinstance(prior_intent, dict) \
            or prior_intent.get("at_utc") != desired.get("at_utc"):
        return False
    return prior.get("public_url") == current.get("public_url")


# ---------------------------------------------------------------------------
# Preconditions (ordered; expensive local observation runs at most once)
# ---------------------------------------------------------------------------

def _preconditions(seams, snapshot, now, *, state_dir=None, manual=False,
                   prior_snapshot=None, maintenance_view=None):
    paths = _paths(state_dir)
    local = _local_default()

    def _fail(reason, resolvable=False):
        return {"ok": False, "reason": reason, "resolvable": bool(resolvable),
                "local": local}

    cancel = read_cancel(state_dir=state_dir)
    if cancel.get("cancelled"):
        return _fail("cancel_unreadable" if cancel.get("unreadable")
                     else "cancel_requested")

    desired = _read_desired()
    if desired is None:
        return _fail("intent_unknown")
    if desired.get("state") != "running":
        return _fail("intent_stopped")
    boot = _boot_id()
    if not boot:
        return _fail("boot_identity_unavailable")
    if desired.get("boot_id") != boot:
        return _fail("intent_previous_boot")
    if not _same_intent(desired, snapshot.get("intent")):
        return _fail("round_superseded")
    if _dns_coverage_gap(snapshot):
        # A truncated address set is not proof that every public entry failed.
        return _fail("addresses_truncated")
    if not isinstance(seams, dict) or not seams:
        return _fail("seams_unavailable")

    record = _desired_stop()
    if isinstance(record, dict):
        local["lifecycle"] = "stopped" if record.get("state") == "stopped" \
            else "running"
        if record.get("state") == "stopped":
            return _fail("managed_stop_in_force")
    else:
        local["lifecycle"] = "unavailable"

    if maintenance_view is not None:
        view = _maintenance_from_payload(maintenance_view, now)
    else:
        view = _maintenance_view(seams, now)
    local["maintenance"] = {
        "none": "none", "in_force": "active", "expired": "expired",
        "unavailable": "unavailable"}[view["state"]]
    if view["state"] == "unavailable":
        return _fail("maintenance_unavailable")
    if view["state"] == "in_force":
        return _fail("maintenance_in_force")
    if view["state"] == "expired":
        return _fail("maintenance_lease_expired", True)

    ok, reason = _observe_security(seams, local)
    if not ok:
        return _fail(reason)
    ok, reason = _observe_connectivity(seams, local)
    if not ok:
        return _fail(reason)
    ok, reason = _observe_gateway(seams, local)
    if not ok:
        return _fail(reason)
    ok, reason = _observe_backend(seams, local)
    if not ok:
        return _fail(reason)

    entries = snapshot.get("entries") if isinstance(
        snapshot.get("entries"), list) else []
    checked = sum(1 for entry in entries
                  if isinstance(entry, dict)
                  and entry.get("state") != "unchecked")
    total = len(entries)
    if snapshot.get("overall") != "all_failed":
        return _fail("not_all_failed")
    if total <= 0 or checked != total:
        return _fail("unconfirmed")
    round_view = snapshot.get("round") if isinstance(
        snapshot.get("round"), dict) else {}
    confirmation = round_view.get("confirmation")
    if not isinstance(confirmation, int) or isinstance(confirmation, bool):
        confirmation = 0
    if confirmation >= 2:
        pass
    elif manual and confirmation >= 1 and _manual_retry_eligible(
            prior_snapshot, snapshot, now=now, boot_id=boot, desired=desired):
        pass
    else:
        return _fail("unconfirmed")

    budget = _budget_view(paths, now)
    if budget.get("unreadable"):
        return _fail("budget_unreadable")
    if not _budget_allows(budget):
        return _fail("budget_exhausted")
    return {"ok": True, "reason": "ready", "resolvable": False,
            "local": local, "budget": budget}


# ---------------------------------------------------------------------------
# Action gates and waits
# ---------------------------------------------------------------------------

def _quick_recheck(*, seams, authorized, state_dir, now, check_maintenance=True,
                   cancel_check=None, target_reader=None, check_target=True,
                   check_security=False, local=None):
    """The frozen in-lock quick re-read before every action."""
    if cancel_check is not None:
        cancel_check()
    cancel = read_cancel(state_dir=state_dir)
    if cancel.get("cancelled"):
        return {"ok": False, "reason": "cancel_unreadable"
                if cancel.get("unreadable") else "cancel_requested"}
    desired = _read_desired()
    if desired is None:
        return {"ok": False, "reason": "intent_unknown"}
    if desired.get("state") != "running":
        return {"ok": False, "reason": "intent_changed"}
    boot = _boot_id()
    if not boot:
        return {"ok": False, "reason": "boot_identity_unavailable"}
    if desired.get("boot_id") != boot:
        return {"ok": False, "reason": "intent_changed"}
    if not _same_intent(desired, authorized.get("intent")):
        return {"ok": False, "reason": "round_superseded"}
    record = _desired_stop()
    if isinstance(record, dict) and record.get("state") == "stopped":
        return {"ok": False, "reason": "managed_stop_in_force"}
    payload = None
    if check_maintenance:
        view = _maintenance_view(seams, now)
        if view["state"] == "unavailable":
            return {"ok": False, "reason": "maintenance_unavailable"}
        if view["state"] in ("in_force", "expired"):
            return {"ok": False, "reason": "maintenance_in_force"}
        payload = view.get("payload")
    if check_security:
        # A retried start must not reuse the security verdict read before the
        # previous attempt: the host evidence is re-read inside this action lock
        # immediately before the command may leave the process.
        ok, reason = _observe_security(
            seams, local if isinstance(local, dict) else {})
        if not ok:
            return {"ok": False, "reason": reason}
    # The local maintenance/security reads above can block/change the observed
    # target. Read the fixed node URL again after them, before any privileged
    # command.
    if (check_target and target_reader is not None
            and target_reader() != authorized.get("public_url")):
        return {"ok": False, "reason": "round_superseded"}
    # A human close can arrive while either bounded local read was running.
    reason = _live_binding_reason(
        authorized.get("intent"), state_dir=state_dir,
        process_boot=authorized.get("intent", {}).get("boot_id"))
    if reason is not None:
        return {"ok": False, "reason": reason}
    if not _still_round(_paths(state_dir), authorized):
        return {"ok": False, "reason": "round_superseded"}
    return {"ok": True, "reason": "ok", "maintenance": payload}


def _gate_now(*, seams, authorized, state_dir, now, check_maintenance=False,
              cancel_check=None, expected_intent=None):
    """Cancel/intent/lifecycle (and round) check used while waiting."""
    if cancel_check is not None:
        cancel_check()
    cancel = read_cancel(state_dir=state_dir)
    if cancel.get("cancelled"):
        return "cancel_unreadable" if cancel.get("unreadable") \
            else "cancel_requested"
    desired = _read_desired()
    if desired is None:
        return "intent_unknown"
    if desired.get("state") != "running":
        return "intent_changed"
    boot = _boot_id()
    if not boot:
        return "boot_identity_unavailable"
    if desired.get("boot_id") != boot:
        return "intent_changed"
    expected = authorized.get("intent") if authorized is not None \
        else expected_intent
    if expected is not None and not _same_intent(desired, expected):
        return "round_superseded"
    record = _desired_stop()
    if isinstance(record, dict) and record.get("state") == "stopped":
        return "managed_stop"
    if authorized is not None and not _still_round(
            _paths(state_dir), authorized):
        return "round_superseded"
    return None


def _wait(seconds, *, deadline, authorized, seams, state_dir, now,
          cancel_check, sleeper, monotonic, expected_intent=None):
    """Cancellable propagation wait; never holds a lock."""
    end = min(monotonic() + float(seconds), deadline)
    while True:
        reason = _gate_now(seams=seams, authorized=authorized,
                           state_dir=state_dir, now=now,
                           cancel_check=cancel_check,
                           expected_intent=expected_intent)
        if reason is not None:
            return {"ok": False, "reason": reason}
        remaining = end - monotonic()
        if remaining <= 0:
            return {"ok": True, "reason": "ok"}
        sleeper(min(CONFIRM_DELAY_SLICE_S, remaining))


def _poll_ts(target, budget, *, deadline, authorized, seams, state_dir, now,
             cancel_check, sleeper, monotonic, expected_intent=None):
    """Bounded TS service confirmation poll over the read-only seam."""
    fn = seams.get("_TS_SERVICE_STATUS") if isinstance(seams, dict) else None
    if not callable(fn):
        return {"ok": False, "reason": "seams_unavailable"}
    end = min(monotonic() + float(budget), deadline)
    while True:
        reason = _gate_now(seams=seams, authorized=authorized,
                           state_dir=state_dir, now=now,
                           cancel_check=cancel_check,
                           expected_intent=expected_intent)
        if reason is not None:
            return {"ok": False, "reason": reason}
        try:
            state = fn()
        except Exception:  # noqa: BLE001 - unreadable is not a confirmation
            state = None
        if state == target:
            return {"ok": True, "reason": "ok"}
        if monotonic() >= end:
            return {"ok": False, "reason": "timeout"}
        sleeper(min(CONFIRM_DELAY_SLICE_S, max(0.0, end - monotonic())))


def _ack_ok(ack):
    if isinstance(ack, dict):
        return ack.get("ok") is True
    return getattr(ack, "ok", False) is True


def _action_error_reason(exc):
    code = getattr(exc, "code", None)
    if code in _PERMANENT_ACTION_CODES:
        return "manual_required"
    if code == "share_ts_pending_confirmation":
        return "pending"
    return None


def _send_action(*, action, verb, kind, seams, authorized, paths, lock_dir,
                 state_dir, now, cancel_check, clock=None, target_reader=None,
                 local=None):
    """Reserve the budget, re-check, then send ONE command inside the lock.

    ``clock`` (the wall clock) is read at EACH reservation and re-check: a
    recovery round can spend minutes in the propagation waits, so a timestamp
    captured before the lock was taken must never label the later reservation.
    ``now`` stays the fallback for callers that pass a fixed wall time.
    """
    wall = clock if callable(clock) else (lambda: now)
    # The broker controls one literal Tailscale service, never an input service
    # name. During its intentional stop the node URL is unobservable; start is
    # allowed only for that same frozen intent/service. Once running, every
    # probe and mapping command revalidates the URL. External node renames are
    # sampled observations, not a transaction with the Tailscale control plane.
    check_target = not (action == "ts_restart" and verb == "start")
    # A start may only follow a still-valid security verdict: the retry window
    # and the action lock are exactly where a stale verdict could otherwise let
    # a start through. The stop is different: its pre-stop gate already read
    # security and connectivity while the service was still running.
    check_security = action == "ts_restart" and verb == "start"

    def _work():
        quick = _quick_recheck(seams=seams, authorized=authorized,
                               state_dir=state_dir, now=wall(),
                               cancel_check=cancel_check,
                               target_reader=target_reader,
                               check_target=check_target,
                               check_security=check_security, local=local)
        if not quick["ok"]:
            return {"ok": False, "reason": quick["reason"], "sent": False}
        budget = None
        if kind is not None:
            reserved = _with_state_lock(
                lock_dir,
                lambda: _reserve_budget_in_lock(paths, kind, wall()))
            if not reserved.get("ok"):
                return {"ok": False, "reason": reserved.get("reason"),
                        "sent": False, "budget": reserved.get("view")}
            budget = reserved.get("view")
        final = _quick_recheck(seams=seams, authorized=authorized,
                               state_dir=state_dir, now=wall(),
                               cancel_check=cancel_check,
                               target_reader=target_reader,
                               check_target=check_target,
                               check_security=check_security, local=local)
        if not final["ok"]:
            return {"ok": False, "reason": final["reason"], "sent": False,
                    "budget": budget}
        seam_name = "_FUNNEL_CONTROL" if action in (
            "mapping_resend", "mapping_resend_after_ts") \
            else "_TS_SERVICE_CONTROL"
        fn = seams.get(seam_name) if isinstance(seams, dict) else None
        if not callable(fn):
            return {"ok": False, "reason": "seams_unavailable", "sent": False,
                    "budget": budget}
        try:
            ack = fn(verb)
        except Exception as exc:  # noqa: BLE001 - never a fake success
            reason = _action_error_reason(exc)
            if reason == "pending":
                return {"ok": True, "sent": True, "pending": True,
                        "budget": budget}
            return {"ok": False, "reason": reason or (
                "mapping_unconfirmed" if action in (
                    "mapping_resend", "mapping_resend_after_ts")
                else ("ts_stop_unconfirmed" if verb == "stop"
                      else "ts_start_unconfirmed")),
                "sent": False, "budget": budget,
                "error_code": getattr(exc, "code", None)}
        if _ack_ok(ack):
            return {"ok": True, "sent": True, "budget": budget,
                    "detail": getattr(ack, "detail", None)
                    if not isinstance(ack, dict) else ack.get("detail")}
        return {"ok": False, "reason": (
            "mapping_unconfirmed" if action in (
                "mapping_resend", "mapping_resend_after_ts")
            else ("ts_stop_unconfirmed" if verb == "stop"
                  else "ts_start_unconfirmed")),
            "sent": True, "budget": budget}

    return _with_action_lock(lock_dir, _work)


def _read_start_conditions(seams, local):
    """The PRE-STOP gate: the service must be able to come back.

    Security and connectivity are read while Tailscale still runs. If either is
    not confirmed, no stop command is sent at all: stopping would leave the
    service in a state the supervisor cannot confirm as healthy.
    """
    ok, reason = _observe_security(seams, local)
    if not ok:
        return False, reason
    ok, reason = _observe_connectivity(seams, local)
    if not ok:
        return False, reason
    return True, None


def _read_restart_conditions(seams, local):
    """The POST-STOP gate: only conditions that do NOT need the service.

    ``tailscale status`` fails while the service is stopped, so connectivity is
    deliberately NOT re-read here. Security, maintenance, cancel and intent are
    re-checked by the action path before the start is sent.
    """
    return _observe_security(seams, local)


# Watchdog terminal reasons: a cancelled/closed/superseded round stops the
# watchdog without an action, while security/maintenance/budget/refusal need the
# operator.
_WATCHDOG_GATE_REASONS = frozenset((
    "cancel_requested", "cancel_unreadable", "intent_stopped",
    "intent_changed", "intent_unknown", "boot_identity_unavailable",
    "managed_stop", "managed_stop_in_force", "round_superseded",
))
_WATCHDOG_MANUAL_REASONS = frozenset((
    "maintenance_in_force", "maintenance_unavailable",
    "maintenance_lease_expired", "security_not_satisfied",
    "security_unconfirmed", "manual_required", "seams_unavailable",
    "budget_unreadable",
))


def _service_start_gate(*, seams, state_dir, now, cancel_check,
                        expected_intent, local):
    """Cancel/intent/lifecycle + maintenance + security, NO connectivity.

    Connectivity is exactly what a stopped service cannot provide; requiring it
    here would make the watchdog unable to ever start Tailscale again.
    """
    reason = _gate_now(seams=seams, authorized=None, state_dir=state_dir,
                       now=now, cancel_check=cancel_check,
                       expected_intent=expected_intent)
    if reason is not None:
        return reason
    view = _maintenance_view(seams, now)
    local["maintenance"] = {
        "none": "none", "in_force": "active", "expired": "expired",
        "unavailable": "unavailable"}[view["state"]]
    if view["state"] == "unavailable":
        return "maintenance_unavailable"
    if view["state"] == "in_force":
        return "maintenance_in_force"
    if view["state"] == "expired":
        return "maintenance_lease_expired"
    ok, reason = _observe_security(seams, local)
    if not ok:
        return reason
    return None


def _send_service_start(*, seams, state_dir, paths, lock_dir, now, cancel_check,
                        expected_intent, local):
    """Reserve the separate service-start budget, re-check, send ONE start.

    The budget is reserved INSIDE ``share:public-state`` before the command is
    sent, exactly like the mapping/restart slots. ``share_public_busy`` means no
    command left this process and must stay a refusal.
    """

    def _work():
        reason = _service_start_gate(
            seams=seams, state_dir=state_dir, now=now,
            cancel_check=cancel_check, expected_intent=expected_intent,
            local=local)
        if reason is not None:
            return {"ok": False, "reason": reason, "sent": False}
        reserved = _with_state_lock(
            lock_dir,
            lambda: _reserve_budget_in_lock(paths, "service_start", now))
        if not reserved.get("ok"):
            return {"ok": False, "reason": reserved.get("reason"),
                    "sent": False, "budget": reserved.get("view")}
        budget = reserved.get("view")
        reason = _service_start_gate(
            seams=seams, state_dir=state_dir, now=now,
            cancel_check=cancel_check, expected_intent=expected_intent,
            local=local)
        if reason is not None:
            return {"ok": False, "reason": reason, "sent": False,
                    "budget": budget}
        fn = seams.get("_TS_SERVICE_CONTROL") if isinstance(seams, dict) else None
        if not callable(fn):
            return {"ok": False, "reason": "seams_unavailable",
                    "sent": False, "budget": budget}
        try:
            ack = fn("start")
        except Exception as exc:  # noqa: BLE001 - never a fake success
            code = getattr(exc, "code", None)
            if code == "share_ts_pending_confirmation":
                return {"ok": True, "sent": True, "pending": True,
                        "budget": budget}
            if code in _PERMANENT_ACTION_CODES:
                return {"ok": False, "reason": "manual_required",
                        "sent": False, "budget": budget,
                        "error_code": code}
            return {"ok": False, "reason": "ts_service_start_unconfirmed",
                    "sent": False, "budget": budget, "error_code": code}
        if _ack_ok(ack):
            return {"ok": True, "sent": True, "budget": budget}
        return {"ok": False, "reason": "ts_service_start_unconfirmed",
                "sent": True, "budget": budget}

    return _with_action_lock(lock_dir, _work)


def _watchdog_terminal(*, paths, lock_dir, boot, now, desired, prior, local,
                        code, reason, state, exit_code, action_sent=False,
                        result="failed", event_status="error", event_code=None,
                        notify_code=None, action="ts_service_start"):
    """Persist an honest watchdog terminal snapshot (never a fake success)."""
    stamp = now
    local = dict(local if isinstance(local, dict) else _local_default())
    record = _desired_stop()
    if isinstance(record, dict):
        local["lifecycle"] = "stopped" if record.get("state") == "stopped" \
            else "running"
    actions = [{"action": action, "at_utc": _utc_iso(stamp),
                "result": result}] if action != "none" else []
    recovery = _recovery(
        actions, state=state, action=action,
        stage="ts_service" if action != "none" else "none",
        reason=reason, manual=state == "manual_required",
        attempted_at=_utc_iso(stamp), finished_at=_utc_iso(stamp))
    snapshot = _make_snapshot(
        boot_id=boot, now=stamp, intent=desired,
        probe={"overall": "not_checked", "reason": reason,
               "public_url": None, "host": None, "entries": []},
        phase="normal", confirmation=0,
        next_check_at=_utc_iso(stamp + NORMAL_INTERVAL_S),
        local=local, recovery=recovery,
        budget=_budget_view(paths, stamp),
        cancel=read_cancel(state_dir=paths["base"]),
        notification=_notification_view(prior))
    notify_event = None
    if notify_code is not None:
        notify_event = _apply_notification(
            snapshot, notify_code, "%s|%s" % ("not_checked", reason), stamp)
    write = _persist_snapshot(
        paths, lock_dir, snapshot,
        event=_event(stamp, event_status, event_code or code, action),
        force_event=False)
    if notify_event is not None:
        _append_event_best_effort(paths, lock_dir, notify_event, stamp,
                                  force=True)
    _append_log(paths, {"at_utc": _utc_iso(stamp), "mode": "watchdog",
                        "overall": "not_checked", "code": code,
                        "action": action})
    if not write.get("ok"):
        write_reason = write.get("reason")
        if action_sent:
            return _result("ts_service_start_unconfirmed", EXIT_UNCONFIRMED,
                           overall="not_checked", reason=write_reason,
                           snapshot=snapshot, action=action)
        if write_reason == "share_public_busy":
            return _result("share_public_busy", EXIT_REFUSED,
                           overall="not_checked", reason="share_public_busy",
                           snapshot=snapshot, action=action)
        if write_reason == "stale_result":
            return _result("round_superseded", EXIT_REFUSED,
                           overall="not_checked", reason="round_superseded",
                           snapshot=snapshot, action=action)
        return _result("state_unreadable", EXIT_REFUSED,
                       overall="not_checked", reason="state_unreadable",
                       snapshot=snapshot, action=action)
    return _result(code, exit_code, overall="not_checked", reason=reason,
                   snapshot=snapshot, action=action)


def _service_watchdog(*, seams, state_dir, lock_dir, boot, desired, prior,
                      clock, cancel_check, sleeper, monotonic, deadline):
    """Start a STOPPED Tailscale service before this round's probe.

    * read-only status; an unobservable service is NOT treated as stopped;
    * start-only: this function never sends a stop command;
    * cancel/intent/lifecycle/maintenance/security are re-checked inside the
      action lock; connectivity is deliberately not required;
    * each start consumes the separate 30 minute service-start budget.
    """
    paths = _paths(state_dir)
    local = _lifecycle_local()
    observed = _observe_ts_service(seams, local)
    if observed != "stopped":
        return {"terminal": None, "recovery": None, "local": local,
                "actions": []}
    if monotonic() >= deadline:
        return _watchdog_blocked(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            reason="deadline_exceeded")
    expected_intent = _intent_view(desired)

    # One bounded re-read of an expired maintenance lease, never maintenance-end.
    view = _maintenance_view(seams, clock())
    if view["state"] == "expired":
        view = _maintenance_view(seams, clock())
    local["maintenance"] = {
        "none": "none", "in_force": "active", "expired": "expired",
        "unavailable": "unavailable"}[view["state"]]
    if view["state"] == "unavailable":
        return {"terminal": _watchdog_terminal(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            code="maintenance_unavailable", reason="maintenance_unavailable",
            state="manual_required", exit_code=EXIT_REFUSED,
            event_status="manual_required", result="skipped",
            notify_code="share_public_manual_required"),
                "recovery": None, "local": local, "actions": []}
    if view["state"] == "in_force":
        return {"terminal": _watchdog_terminal(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            code="maintenance_in_force", reason="maintenance_in_force",
            state="manual_required", exit_code=EXIT_REFUSED,
            event_status="manual_required", result="skipped",
            notify_code="share_public_manual_required"),
                "recovery": None, "local": local, "actions": []}
    if view["state"] == "expired":
        return {"terminal": _watchdog_terminal(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            code="maintenance_lease_expired",
            reason="maintenance_lease_expired", state="manual_required",
            exit_code=EXIT_REFUSED, event_status="manual_required",
            result="skipped",
            notify_code="share_public_manual_required"),
                "recovery": None, "local": local, "actions": []}

    reason = _service_start_gate(
        seams=seams, state_dir=state_dir, now=clock(),
        cancel_check=cancel_check, expected_intent=expected_intent,
        local=local)
    if reason is not None:
        return _watchdog_blocked(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local, reason=reason)

    budget = None
    last_reason = "ts_service_start_unconfirmed"
    attempts = 0
    start_sent = False

    def _unconfirmed(reason):
        """A start may have left; its final state is unconfirmed (exit 2)."""
        local["ts_service"] = "unavailable"
        return {"terminal": _watchdog_terminal(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            code="ts_service_start_unconfirmed", reason=reason,
            state="failed", exit_code=EXIT_UNCONFIRMED,
            event_status="recovery_attempted",
            event_code="ts_service_start_unconfirmed",
            result="unconfirmed", action_sent=True),
            "recovery": None, "local": local, "actions": []}

    def _manual(reason):
        return {"terminal": _watchdog_terminal(
            paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
            desired=desired, prior=prior, local=local,
            code=reason, reason=reason,
            state="manual_required", exit_code=EXIT_REFUSED,
            event_status="manual_required",
            result="failed" if reason == "manual_required" else "skipped",
            notify_code="share_public_manual_required"),
            "recovery": None, "local": local, "actions": []}

    while attempts < SERVICE_START_ATTEMPTS:
        attempts += 1
        sent = _send_service_start(
            seams=seams, state_dir=state_dir, paths=paths,
            lock_dir=lock_dir, now=clock(), cancel_check=cancel_check,
            expected_intent=expected_intent, local=local)
        budget = sent.get("budget") or budget
        if sent.get("ok"):
            start_sent = True
            confirmed = _poll_ts(
                "running", TS_START_CONFIRM_S, deadline=deadline,
                authorized=None, seams=seams, state_dir=state_dir,
                now=clock(), cancel_check=cancel_check, sleeper=sleeper,
                monotonic=monotonic, expected_intent=expected_intent)
            if confirmed.get("ok"):
                local["ts_service"] = "running"
                stamp = clock()
                actions = [{"action": "ts_service_start",
                            "at_utc": _utc_iso(stamp), "result": "ok"}]
                recovery = _recovery(
                    actions, state="succeeded", action="ts_service_start",
                    stage="ts_service", reason="ts_service_started",
                    attempted_at=_utc_iso(stamp),
                    finished_at=_utc_iso(stamp))
                _append_event_best_effort(
                    paths, lock_dir,
                    _event(stamp, "recovery_attempted", "ts_service_start",
                           "ts_service_start"),
                    stamp, force=True)
                _append_log(paths, {"at_utc": _utc_iso(stamp),
                                    "mode": "watchdog",
                                    "overall": "not_checked",
                                    "code": "ts_service_start",
                                    "action": "ts_service_start"})
                return {"terminal": None, "recovery": recovery,
                        "local": local, "actions": actions}
            last_reason = confirmed.get("reason") or "timeout"
        else:
            last_reason = sent.get("reason") or "ts_service_start_unconfirmed"
            if sent.get("sent") or last_reason == \
                    "ts_service_start_unconfirmed":
                # The control seam raised after the command may have left.
                start_sent = True
        if last_reason in _WATCHDOG_GATE_REASONS:
            if start_sent:
                return _unconfirmed(last_reason)
            return _watchdog_blocked(
                paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
                desired=desired, prior=prior, local=local,
                reason=last_reason)
        if last_reason in _WATCHDOG_MANUAL_REASONS:
            if start_sent:
                return _unconfirmed(last_reason)
            return _manual(last_reason)
        if last_reason == "share_public_busy":
            if start_sent:
                return _unconfirmed(last_reason)
            _append_log(paths, {"at_utc": _utc_iso(clock()),
                                 "mode": "watchdog",
                                 "overall": "not_checked",
                                 "code": "share_public_busy",
                                 "action": "ts_service_start"})
            return {"terminal": _result(
                "share_public_busy", EXIT_REFUSED, overall="not_checked",
                reason="share_public_busy"), "recovery": None,
                "local": local, "actions": []}
        if last_reason == "budget_exhausted":
            if start_sent:
                return _unconfirmed(last_reason)
            return {"terminal": _watchdog_terminal(
                paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
                desired=desired, prior=prior, local=local,
                code="budget_exhausted", reason="budget_exhausted",
                state="budget_exhausted", exit_code=EXIT_REFUSED,
                event_status="budget_exhausted", result="skipped",
                notify_code="share_public_budget_exhausted"),
                "recovery": None, "local": local, "actions": []}
        if attempts >= SERVICE_START_ATTEMPTS:
            break
        if monotonic() >= deadline:
            last_reason = "deadline_exceeded"
            break
        wait = _wait(
            ACTION_RETRY_DELAY_S, deadline=deadline, authorized=None,
            seams=seams, state_dir=state_dir, now=clock(),
            cancel_check=cancel_check, sleeper=sleeper, monotonic=monotonic,
            expected_intent=expected_intent)
        if not wait.get("ok"):
            last_reason = wait.get("reason") or "deadline_exceeded"
            if last_reason in _WATCHDOG_GATE_REASONS:
                if start_sent:
                    return _unconfirmed(last_reason)
                return _watchdog_blocked(
                    paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
                    desired=desired, prior=prior, local=local,
                    reason=last_reason)
            if last_reason in _WATCHDOG_MANUAL_REASONS:
                if start_sent:
                    return _unconfirmed(last_reason)
                return _manual(last_reason)
            break
    if start_sent:
        # The start may have left but was never confirmed: UNKNOWN, exit 2.
        return _unconfirmed(last_reason)
    # No command ever left this process: the service is still observed stopped
    # (or the attempt deadline expired), which is a refusal, never a success.
    return _watchdog_blocked(
        paths=paths, lock_dir=lock_dir, boot=boot, now=clock(),
        desired=desired, prior=prior, local=local, reason=last_reason)


def _watchdog_blocked(*, paths, lock_dir, boot, now, desired, prior, local,
                      reason):
    """A watchdog gate blocked the start: no command was sent."""
    _append_log(paths, {"at_utc": _utc_iso(now), "mode": "watchdog",
                        "overall": "not_checked", "code": reason,
                        "action": "ts_service_start"})
    exit_code = EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED
    return {"terminal": _gate_snapshot(
        paths, lock_dir, boot, now, desired,
        read_cancel(state_dir=paths["base"]), prior, reason, reason,
        exit_code, local=local, event_status="not_checked",
        action="ts_service_start"), "recovery": None, "local": local,
        "actions": []}


# ---------------------------------------------------------------------------
# Recovery orchestration
# ---------------------------------------------------------------------------

def _result(code, exit_code, *, overall=None, reason=None, snapshot=None,
            probe=None, action=None):
    return {"code": code, "exit_code": int(exit_code), "overall": overall,
            "reason": reason, "action": action, "snapshot": snapshot,
            "probe": probe}


def _recovery(actions, *, state="running", action="none", stage="none",
              reason=None, manual=False, attempted_at=None, finished_at=None):
    return {"state": state, "action": action, "stage": stage,
            "reason": reason, "attempted_at_utc": attempted_at,
            "finished_at_utc": finished_at, "manual": bool(manual),
            "actions": actions}


def _event(now, status, code, action="none"):
    return {"at_utc": _utc_iso(now), "status": status, "code": code,
            "action": action}


def _recover(*, seams, snapshot, precheck, paths, lock_dir, state_dir, probe,
             url, cancel_check, sleeper, monotonic, deadline, manual,
             now, prior_snapshot, clock=None, target_reader=None,
             watchdog=None):
    if cancel_check is not None:
        cancel_check()
    # Wall-clock timestamps (budget reservations, action/event times, finished_at)
    # are read at each write: a recovery round can take minutes, so the round
    # start ``now`` must never label a later action. Monotonic time stays the
    # authority for deadlines only.
    wall = clock if callable(clock) else (lambda: now)
    authorized = snapshot
    process_boot = authorized.get("intent", {}).get("boot_id")
    current = _copy_snapshot(authorized)
    status = read_status(state_dir=state_dir, now=wall())
    if not _same_round(status.get("snapshot"), authorized):
        return _result("round_superseded", EXIT_REFUSED,
                       overall=authorized.get("overall"),
                       reason="round_superseded", snapshot=authorized)
    if target_reader is not None and target_reader() != url:
        return _gate_snapshot(
            paths, lock_dir, authorized.get("boot_id"), now,
            _read_desired(), read_cancel(state_dir=state_dir), prior_snapshot,
            "round_superseded", "round_superseded", EXIT_REFUSED)

    actions = []
    if isinstance(watchdog, dict) and isinstance(
            watchdog.get("actions"), list):
        actions = [dict(item) for item in watchdog["actions"]
                   if isinstance(item, dict)]
    attempted_at = authorized.get("observed_at_utc")
    budget = precheck.get("budget")
    recovery = _recovery(
        actions, state="running",
        action=actions[-1].get("action") if actions else "none",
        stage="ts_service" if actions else "precheck",
        manual=manual, attempted_at=attempted_at)

    def _incident_key(overall, reason):
        """Dedupe key WITHOUT the round id: a persistent fault is ONE incident.

        A new independent fault differs because the intervening notification
        (failure/recovered/degraded/manual) replaced the last key.
        """
        return _notification_key(overall, reason)

    def _progress(action):
        """Persist one recovery action/stage (same authorized round)."""
        stamp = wall()
        recovery.update(state="running", finished_at_utc=None)
        current["recovery"] = recovery
        if budget is not None:
            current["budget"] = budget
        return _write_back(
            paths, lock_dir, current, stamp, authorized,
            event=_event(stamp, "recovery_attempted", action, action),
            force_event=True)

    def _state_write_failure(write, *, action):
        """An action was sent but its state could not be persisted."""
        if actions:
            actions[-1]["result"] = "unconfirmed"
        code = "recovery_unconfirmed" if action == "ts_restart" \
            else "mapping_unconfirmed"
        return _result(code, EXIT_UNCONFIRMED,
                       overall=current.get("overall"),
                       reason="share_public_busy"
                       if write.get("reason") == "share_public_busy"
                       else ("round_superseded"
                             if write.get("reason") == "round_superseded"
                             else "state_unreadable"),
                       snapshot=current, action=action)

    def _finish(code, exit_code, state, reason, *, action="none", stage="done",
                overall=None, event_status=None, event_code=None,
                force_event=False, notify_code=None):
        stamp = wall()
        recovery.update(state=state, action=action, stage=stage,
                        reason=reason, finished_at_utc=_utc_iso(stamp))
        current["recovery"] = recovery
        current["local"] = precheck.get("local", current.get("local"))
        if overall is not None:
            current["overall"] = overall
            current["reason"] = reason
        if state in ("manual_required", "budget_exhausted"):
            current["reason"] = reason
        if budget is not None:
            current["budget"] = budget
        current["intent"] = _intent_view(_read_desired())
        current["cancel"] = _cancel_view(
            read_cancel(state_dir=state_dir))
        # Neither success nor failure of an old target belongs to a new intent.
        # Keep the sent-action evidence, but discard its obsolete probe data.
        binding_reason = _live_binding_reason(
            authorized.get("intent"), state_dir=state_dir, process_boot=process_boot)
        if (binding_reason is None and state == "succeeded"
                and target_reader is not None and target_reader() != url):
            binding_reason = "round_superseded"
        if binding_reason is not None:
            current["overall"] = "not_checked"
            current["reason"] = binding_reason
            current["entries"] = []
            current["dns"] = None
            if state == "succeeded" and overall in ("ok", "degraded"):
                stopped = binding_reason in (
                    "cancel_requested", "cancel_unreadable", "intent_stopped",
                    "managed_stop_in_force")
                state = "stopped" if stopped else "failed"
                code = reason = binding_reason
                exit_code = EXIT_OK if code == "intent_stopped" else EXIT_REFUSED
                overall = "not_checked"
                event_status = "stopped" if stopped else "error"
                event_code = code
                recovery.update(state=state, action=action, stage=stage,
                                reason=reason, finished_at_utc=_utc_iso(stamp))
            notify_code = None
        if overall == "not_checked":
            current["entries"] = []
            current["dns"] = None
        # Preserve the notification state carried by this round (including a
        # failure notification already sent in this recovery) instead of
        # re-reading the pre-round snapshot and losing that key.
        round_view = current.get("round")
        if isinstance(round_view, dict):
            round_view["finished_at_utc"] = _utc_iso(stamp)
        notify_event = None
        notify_after_commit = state == "succeeded" and overall in ("ok", "degraded")
        if notify_code is not None and not notify_after_commit:
            if notify_code == "share_public_recovered":
                key = _incident_key("recovered", "ok")
            else:
                key = "%s|%s" % (current.get("overall"), reason)
            notify_event = _apply_notification(
                current, notify_code, key, stamp)
        write = _write_back(paths, lock_dir, current, stamp, authorized,
                            event=_event(stamp, event_status or "error",
                                         event_code or code, action),
                            force_event=force_event,
                            binding_check=(lambda: _live_binding_reason(
                                authorized.get("intent"), state_dir=state_dir,
                                process_boot=process_boot))
                            if notify_after_commit else None)
        if (notify_after_commit and not write.get("ok")
                and write.get("reason") in (
                    "cancel_requested", "cancel_unreadable", "intent_stopped",
                    "intent_unknown", "intent_previous_boot",
                    "boot_identity_unavailable", "managed_stop_in_force",
                    "round_superseded")):
            invalid = write["reason"]
            stopped = invalid in ("cancel_requested", "cancel_unreadable",
                                  "intent_stopped", "managed_stop_in_force")
            return _finish(
                invalid, EXIT_OK if invalid == "intent_stopped" else EXIT_REFUSED,
                "stopped" if stopped else "failed", invalid, action=action,
                stage=stage, overall="not_checked",
                event_status="stopped" if stopped else "error",
                event_code=invalid)
        if notify_event is not None:
            _append_event_best_effort(paths, lock_dir, notify_event, stamp,
                                      force=True)
        _append_log(paths, {"at_utc": _utc_iso(stamp), "mode": "recovery",
                           "overall": current.get("overall"),
                           "code": code, "action": action})
        if not write.get("ok"):
            write_reason = write.get("reason")
            if write_reason == "round_superseded":
                if exit_code == EXIT_UNCONFIRMED:
                    # A destructive action was already sent (TS is stopped);
                    # the unconfirmed outcome must not be downgraded.
                    return _result(code, EXIT_UNCONFIRMED,
                                   overall=current.get("overall"),
                                   reason=reason, snapshot=current,
                                   action=action)
                return _result("round_superseded", EXIT_REFUSED,
                               overall=current.get("overall"),
                               reason="round_superseded", snapshot=current)
            if write_reason == "share_public_busy":
                # The action (if any) was sent, but its result could not be
                # persisted. Never report that as success.
                return _result("recovery_unconfirmed" if actions
                               else "share_public_busy",
                               EXIT_UNCONFIRMED if actions else EXIT_REFUSED,
                               overall=current.get("overall"),
                               reason="share_public_busy", snapshot=current,
                               action=action)
            return _result("recovery_unconfirmed" if actions
                           else "state_unreadable",
                           EXIT_UNCONFIRMED if actions else EXIT_REFUSED,
                           overall=current.get("overall"),
                           reason="state_unreadable", snapshot=current,
                           action=action)
        if notify_after_commit and notify_code is not None:
            invalid = _live_binding_reason(
                authorized.get("intent"), state_dir=state_dir,
                process_boot=process_boot)
            if invalid is not None:
                return _finish(
                    invalid, EXIT_OK if invalid == "intent_stopped" else EXIT_REFUSED,
                    "stopped" if invalid in ("cancel_requested", "intent_stopped")
                    else "failed", invalid, action=action, stage=stage,
                    overall="not_checked", event_status="stopped", event_code=invalid)
            key = _incident_key("recovered", "ok") \
                if notify_code == "share_public_recovered" \
                else "%s|%s" % (current.get("overall"), reason)
            notify_event = _apply_notification(current, notify_code, key, stamp)
            _persist_snapshot(
                paths, lock_dir, current, event=notify_event, force_event=True,
                binding_check=lambda: _live_binding_reason(
                    authorized.get("intent"), state_dir=state_dir,
                    process_boot=process_boot))
        return _result(code, exit_code, overall=current.get("overall"),
                       reason=reason, snapshot=current, action=action)

    # Step 0: one expired-lease re-read (never maintenance-end).
    if precheck.get("reason") == "maintenance_lease_expired":
        step = _with_action_lock(lock_dir, lambda: _expired_lease_step(
            seams=seams, authorized=authorized, state_dir=state_dir,
            now=wall(), cancel_check=cancel_check))
        if not step.get("ok"):
            reason = step.get("reason") or "maintenance_unavailable"
            if reason == "share_public_busy":
                return _result("share_public_busy", EXIT_REFUSED,
                               overall=current.get("overall"), reason=reason,
                               snapshot=current)
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop_in_force"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               event_status="stopped", event_code=reason)
            if reason == "round_superseded":
                return _finish("round_superseded", EXIT_REFUSED, "failed",
                               reason, event_status="error",
                               event_code=reason)
            code = reason if reason in (
                "maintenance_in_force", "maintenance_unavailable",
                "seams_unavailable", "budget_unreadable") \
                else "manual_required"
            return _finish(code, EXIT_REFUSED, "manual_required",
                           reason, event_status="manual_required",
                           event_code=reason, notify_code=(
                               "share_public_manual_required"))
        refreshed = _preconditions(
            seams, snapshot, wall(), state_dir=state_dir, manual=manual,
            prior_snapshot=prior_snapshot,
            maintenance_view=step.get("maintenance"))
        if not refreshed.get("ok"):
            reason = refreshed.get("reason") or "maintenance_unavailable"
            if reason in ("intent_stopped", "cancel_requested",
                          "cancel_unreadable", "managed_stop_in_force"):
                return _finish(reason, EXIT_OK if reason == "intent_stopped"
                               else EXIT_REFUSED, "stopped", reason,
                               event_status="stopped", event_code=reason)
            code = reason if reason in (
                "maintenance_in_force", "maintenance_unavailable",
                "seams_unavailable", "budget_unreadable") \
                else "manual_required"
            return _finish(code, EXIT_REFUSED, "manual_required", reason,
                           event_status="manual_required",
                           event_code=reason,
                           notify_code="share_public_manual_required")
        precheck = refreshed
        budget = precheck.get("budget")

    def _gate_failure(reason, *, action="none", stage="done",
                      event_status="manual_required", exit_code=EXIT_REFUSED,
                      state="manual_required"):
        return _finish(reason, exit_code, state, reason, action=action,
                       stage=stage, event_status=event_status,
                       event_code=reason, notify_code=(
                           "share_public_manual_required"
                           if state == "manual_required" else None))

    def _mapping_unconfirmed(reason, *, stage):
        """An already-sent mapping whose outcome cannot be confirmed."""
        if actions:
            actions[-1]["result"] = "unconfirmed"
        action = actions[-1].get("action") if actions else "mapping_resend"
        return _finish("mapping_unconfirmed", EXIT_UNCONFIRMED, "failed",
                       reason, action=action, stage=stage,
                       event_status="recovery_attempted",
                       event_code="mapping_unconfirmed")

    def _recovery_unconfirmed(reason, *, stage):
        """An already-sent TS action whose outcome cannot be confirmed."""
        if actions:
            actions[-1]["result"] = "unconfirmed"
        return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED, "failed",
                       reason, action="ts_restart", stage=stage,
                       event_status="error", event_code=reason)

    def _deadline_after_actions(stage):
        """A deadline hit AFTER a command was sent is exit 2, not a refusal."""
        if actions:
            if actions[-1].get("action") == "ts_restart":
                return _recovery_unconfirmed("deadline_exceeded", stage=stage)
            if actions[-1].get("action") in (
                    "mapping_resend", "mapping_resend_after_ts"):
                return _mapping_unconfirmed("deadline_exceeded", stage=stage)
        return _finish("deadline_exceeded", EXIT_REFUSED, "failed",
                       "deadline_exceeded", event_status="error",
                       event_code="deadline_exceeded")

    if monotonic() >= deadline:
        return _finish("deadline_exceeded", EXIT_REFUSED, "failed",
                       "deadline_exceeded", event_status="error",
                       event_code="deadline_exceeded")

    # A8: a recovery attempt starting on a fresh fault is itself a meaningful
    # notification (deduped by the authorized round identity).
    failure_event = _apply_notification(
        current, "share_public_failed",
        _incident_key("all_failed", authorized.get("reason") or "all_failed"),
        wall())
    if failure_event is not None:
        _append_event_best_effort(paths, lock_dir, failure_event, wall(),
                                  force=True)

    if (budget or {}).get("mapping_used", 0) >= MAPPING_LIMIT \
            and (budget or {}).get("ts_restart_used", 0) < TS_RESTART_LIMIT:
        # The mapping quota was consumed by an earlier round; only the
        # TS restart slot remains. The second mapping (C) stays skipped.
        sent = None
    else:
        # A. Mapping resend #1.
        sent = _send_action(action="mapping_resend", verb="on", kind="mapping",
                            seams=seams, authorized=authorized, paths=paths,
                            lock_dir=lock_dir, state_dir=state_dir, now=wall(),
                            cancel_check=cancel_check, clock=wall,
                            target_reader=target_reader)
        if not sent.get("ok"):
            reason = sent.get("reason") or "mapping_unconfirmed"
            budget = sent.get("budget") or budget
            if reason == "budget_exhausted":
                return _finish("budget_exhausted", EXIT_REFUSED,
                               "budget_exhausted", reason,
                               event_status="budget_exhausted",
                               event_code=reason,
                               notify_code="share_public_budget_exhausted")
            if reason == "manual_required" or reason in (
                    "maintenance_in_force", "maintenance_unavailable",
                    "boot_identity_unavailable"):
                return _gate_failure(reason)
            if reason == "share_public_busy":
                # A busy action lock grants no authority to write the shared state.
                return _result("share_public_busy", EXIT_REFUSED,
                               overall=current.get("overall"), reason=reason,
                               snapshot=current, action="mapping_resend")
            if reason in ("round_superseded",
                          "budget_unreadable", "seams_unavailable"):
                return _finish(reason, EXIT_REFUSED,
                               "failed" if reason == "round_superseded"
                               else "manual_required", reason,
                               event_status="error", event_code=reason)
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "intent_stopped", "managed_stop_in_force"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               event_status="stopped", event_code=reason)
            actions.append({"action": "mapping_resend", "at_utc": _utc_iso(wall()),
                             "result": "unconfirmed"})
            return _finish(reason, EXIT_UNCONFIRMED, "failed", reason,
                           action="mapping_resend", stage="mapping_resend",
                           event_status="recovery_attempted",
                           event_code=reason)
        budget = sent.get("budget") or budget
        actions.append({"action": "mapping_resend", "at_utc": _utc_iso(wall()),
                        "result": "ok"})
        recovery.update(action="mapping_resend", stage="mapping_resend")
        progress = _progress("mapping_resend")
        if not progress.get("ok"):
            return _state_write_failure(progress, action="mapping_resend")
        after = _gate_now(seams=seams, authorized=authorized,
                          state_dir=state_dir, now=wall(),
                          cancel_check=cancel_check)
        if after is not None:
            if after in ("round_superseded", "boot_identity_unavailable"):
                # The mapping command WAS sent; an unconfirmable outcome is
                # exit 2, never a pre-action refusal.
                return _mapping_unconfirmed(after, stage="mapping_resend")
            return _finish(after, EXIT_REFUSED,
                           "stopped" if after in (
                               "cancel_requested", "cancel_unreadable",
                               "intent_changed", "intent_unknown",
                               "managed_stop") else "failed",
                           after, action="mapping_resend",
                           stage="mapping_resend",
                           event_status="stopped" if after in (
                               "cancel_requested", "cancel_unreadable",
                               "intent_changed", "intent_unknown",
                               "managed_stop") else "error",
                           event_code=after)
        wait = _wait(MAPPING_PROPAGATION_S, deadline=deadline,
                     authorized=authorized, seams=seams, state_dir=state_dir,
                     now=wall(), cancel_check=cancel_check, sleeper=sleeper,
                     monotonic=monotonic)
        if not wait.get("ok"):
            reason = wait.get("reason") or "deadline_exceeded"
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               action="mapping_resend",
                               stage="mapping_resend", event_status="stopped",
                               event_code=reason)
            return _mapping_unconfirmed(
                reason if reason in ("round_superseded",
                                     "boot_identity_unavailable")
                else "deadline_exceeded",
                stage="mapping_resend")
        if monotonic() >= deadline:
            return _mapping_unconfirmed("deadline_exceeded",
                                         stage="verify")
        probe_result = _run_probe(probe, url, wall(), target_reader=target_reader)
        current["entries"] = probe_result["entries"]
        current["overall"] = probe_result["overall"]
        current["reason"] = probe_result["reason"]
        current["dns"] = _sanitize_dns(probe_result.get("dns"))
        outcome = _probe_outcome(probe_result)
        if outcome == "ok":
            return _finish("recovered", EXIT_OK, "succeeded", "ok",
                           action="mapping_resend", stage="done",
                           overall="ok", event_status="recovered",
                           event_code="ok",
                           notify_code="share_public_recovered")
        if outcome == "degraded":
            return _finish("degraded", EXIT_OK, "succeeded",
                           probe_result["reason"], action="mapping_resend",
                           stage="done", overall="degraded",
                           event_status="degraded",
                           event_code=probe_result["reason"],
                           notify_code="share_public_degraded")
        if outcome != "all_failed":
            return _finish("manual_required", EXIT_REFUSED, "manual_required",
                           probe_result["reason"], action="mapping_resend",
                           stage="verify", event_status="manual_required",
                           event_code=probe_result["reason"],
                           notify_code="share_public_manual_required")

    if monotonic() >= deadline:
        return _deadline_after_actions("mapping_resend")
    # B. TS restart (only when the restart budget has never been consumed).
    if (budget or {}).get("ts_restart_used", 0) < TS_RESTART_LIMIT:
        # PRE-STOP gate: the service must be able to come back. Security and
        # connectivity are read while Tailscale still runs; if either cannot be
        # confirmed, NO stop command is sent at all. A mapping may already have
        # been sent in this round, so that outcome stays unconfirmed (exit 2).
        local = precheck.get("local") if isinstance(
            precheck.get("local"), dict) else _local_default()
        ok, reason = _read_start_conditions(seams, local)
        current["local"] = local
        if not ok:
            destructive = [item for item in actions
                           if item.get("action") in (
                               "mapping_resend", "mapping_resend_after_ts",
                               "ts_restart")]
            if destructive:
                return _finish(reason, EXIT_UNCONFIRMED, "failed", reason,
                               action=destructive[-1].get("action"),
                               stage="ts_stop",
                               event_status="recovery_attempted",
                               event_code=reason)
            return _gate_failure(reason, stage="ts_stop")
        stop = _send_action(action="ts_restart", verb="stop",
                            kind="ts_restart", seams=seams,
                            authorized=authorized, paths=paths,
                            lock_dir=lock_dir, state_dir=state_dir, now=wall(),
                            cancel_check=cancel_check, clock=wall,
                            target_reader=target_reader)
        if not stop.get("ok"):
            reason = stop.get("reason") or "ts_stop_unconfirmed"
            budget = stop.get("budget") or budget
            if reason == "manual_required" or reason in (
                    "maintenance_in_force", "maintenance_unavailable",
                    "boot_identity_unavailable"):
                return _gate_failure(reason, action="ts_restart",
                                     stage="ts_stop")
            if reason == "budget_exhausted":
                return _finish("budget_exhausted", EXIT_REFUSED,
                               "budget_exhausted", reason,
                               event_status="budget_exhausted",
                               event_code=reason,
                               notify_code="share_public_budget_exhausted")
            if reason == "share_public_busy":
                return _result("share_public_busy", EXIT_REFUSED,
                               overall=current.get("overall"), reason=reason,
                               snapshot=current, action="ts_restart")
            if reason in ("round_superseded",
                          "budget_unreadable", "seams_unavailable"):
                return _finish(reason, EXIT_REFUSED, "failed", reason,
                               action="ts_restart", stage="ts_stop",
                               event_status="error", event_code=reason)
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop_in_force"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               action="ts_restart", stage="ts_stop",
                               event_status="stopped", event_code=reason)
            actions.append({"action": "ts_restart", "at_utc": _utc_iso(wall()),
                             "result": "unconfirmed"})
            return _finish(reason, EXIT_UNCONFIRMED, "failed", reason,
                           action="ts_restart", stage="ts_stop",
                           event_status="recovery_attempted",
                           event_code=reason)
        budget = stop.get("budget") or budget
        actions.append({"action": "ts_restart", "at_utc": _utc_iso(wall()),
                        "result": "ok"})
        recovery.update(action="ts_restart", stage="ts_stop")
        progress = _progress("ts_restart")
        if not progress.get("ok"):
            return _state_write_failure(progress, action="ts_restart")
        after = _gate_now(seams=seams, authorized=authorized,
                          state_dir=state_dir, now=wall(),
                          cancel_check=cancel_check)
        if after is not None:
            actions[-1]["result"] = "unconfirmed"
            return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED,
                           "failed", after, action="ts_restart",
                           stage="ts_stop", event_status="error",
                           event_code=after)
        confirmed = _poll_ts("stopped", TS_STOP_CONFIRM_S, deadline=deadline,
                             authorized=authorized, seams=seams,
                             state_dir=state_dir, now=wall(),
                             cancel_check=cancel_check, sleeper=sleeper,
                             monotonic=monotonic)
        if not confirmed.get("ok"):
            reason = confirmed.get("reason") or "timeout"
            actions[-1]["result"] = "unconfirmed"
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop", "round_superseded",
                          "boot_identity_unavailable"):
                return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED,
                               "failed", reason, action="ts_restart",
                               stage="ts_stop", event_status="error",
                               event_code=reason)
            return _finish("ts_stop_unconfirmed", EXIT_UNCONFIRMED, "failed",
                           reason, action="ts_restart", stage="ts_stop",
                           event_status="recovery_attempted",
                           event_code="ts_stop_unconfirmed")
        if monotonic() >= deadline:
            # TS is already stopped: an unconfirmable outcome is exit 2.
            return _recovery_unconfirmed("deadline_exceeded",
                                          stage="ts_start")
        # POST-STOP gate: connectivity is deliberately NOT re-read (a stopped
        # service cannot answer it). Security, maintenance, cancel and intent are
        # re-checked by _send_action before each start attempt, and the start is
        # retried ONCE on a temporary/unconfirmed failure. A permanent refusal
        # is never retried.
        local = precheck.get("local") if isinstance(
            precheck.get("local"), dict) else _local_default()
        ok, reason = _read_restart_conditions(seams, local)
        current["local"] = local
        if not ok:
            actions[-1]["result"] = "unconfirmed"
            return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED,
                           "failed", reason, action="ts_restart",
                           stage="ts_start", event_status="error",
                           event_code=reason)
        start_reason = "ts_start_unconfirmed"
        confirmed_running = False
        start_sent = False
        attempts = 0

        def _record_start_attempt(result):
            """Append the start attempt and report whether it may have left.

            ``sent`` is False for a refusal taken before the control seam was
            invoked. An exception from the seam is ambiguous: the command may
            already have been dispatched, so it is recorded as unconfirmed.
            """
            if result.get("ok"):
                actions.append({"action": "ts_restart",
                                "at_utc": _utc_iso(wall()),
                                "result": "ok"})
                return True
            if result.get("sent") or result.get("reason") == \
                    "ts_start_unconfirmed":
                actions.append({"action": "ts_restart",
                                "at_utc": _utc_iso(wall()),
                                "result": "unconfirmed"})
                return True
            return False

        def _ts_start_unconfirmed(reason):
            """A start may have been sent; its final state is unconfirmed."""
            if actions and actions[-1].get("action") == "ts_restart":
                actions[-1]["result"] = "unconfirmed"
            return _finish("ts_start_unconfirmed", EXIT_UNCONFIRMED,
                           "failed", reason, action="ts_restart",
                           stage="ts_start",
                           event_status="recovery_attempted",
                           event_code="ts_start_unconfirmed")

        while attempts < SERVICE_START_ATTEMPTS:
            attempts += 1
            start = _send_action(action="ts_restart", verb="start", kind=None,
                                 seams=seams, authorized=authorized,
                                 paths=paths, lock_dir=lock_dir,
                                 state_dir=state_dir, now=wall(),
                                 cancel_check=cancel_check, clock=wall,
                                 target_reader=target_reader, local=local)
            start_sent = _record_start_attempt(start) or start_sent
            if start.get("ok"):
                recovery.update(stage="ts_start")
                confirmed = _poll_ts(
                    "running", TS_START_CONFIRM_S, deadline=deadline,
                    authorized=authorized, seams=seams,
                    state_dir=state_dir, now=wall(),
                    cancel_check=cancel_check, sleeper=sleeper,
                    monotonic=monotonic)
                if confirmed.get("ok"):
                    confirmed_running = True
                    break
                start_reason = confirmed.get("reason") or "timeout"
                actions[-1]["result"] = "unconfirmed"
            else:
                start_reason = start.get("reason") or "ts_start_unconfirmed"
                if start_reason == "manual_required":
                    # The start command was PERMANENTLY refused (never
                    # executed): a manual precondition, no retry.
                    if start_sent:
                        return _ts_start_unconfirmed(start_reason)
                    actions[-1]["result"] = "failed"
                    return _gate_failure(start_reason, action="ts_restart",
                                         stage="ts_start")
                if start_reason == "seams_unavailable":
                    # The control seam is not wired: no command was sent.
                    if start_sent:
                        return _ts_start_unconfirmed(start_reason)
                    actions[-1]["result"] = "failed"
                    return _finish("seams_unavailable", EXIT_REFUSED,
                                   "manual_required", start_reason,
                                   action="ts_restart", stage="ts_start",
                                   event_status="manual_required",
                                   event_code=start_reason,
                                   notify_code="share_public_manual_required")
                if start_reason == "share_public_busy":
                    # A retry's action lock was busy, so THIS start command
                    # never left; a previous sent attempt still stays
                    # unconfirmed and must not be reported as a refusal.
                    if start_sent:
                        return _ts_start_unconfirmed(start_reason)
                    return _result("share_public_busy", EXIT_REFUSED,
                                   overall=current.get("overall"),
                                   reason=start_reason, snapshot=current,
                                   action="ts_restart")
                if start_reason in ("security_not_satisfied",
                                    "security_unconfirmed"):
                    # The security verdict was re-read inside the action lock:
                    # a start after a sent attempt is unconfirmed.
                    if start_sent:
                        return _ts_start_unconfirmed(start_reason)
                    actions[-1]["result"] = "failed"
                    return _gate_failure(start_reason, action="ts_restart",
                                         stage="ts_start")
            if start_reason in (
                    "cancel_requested", "cancel_unreadable",
                    "intent_changed", "intent_unknown",
                    "managed_stop_in_force", "maintenance_in_force",
                    "maintenance_unavailable", "round_superseded",
                    "boot_identity_unavailable"):
                if start_sent:
                    actions[-1]["result"] = "unconfirmed"
                return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED,
                               "failed", start_reason, action="ts_restart",
                               stage="ts_start", event_status="error",
                               event_code=start_reason)
            if attempts >= SERVICE_START_ATTEMPTS:
                break
            if monotonic() >= deadline:
                start_reason = "deadline_exceeded"
                break
            retry = _wait(ACTION_RETRY_DELAY_S, deadline=deadline,
                          authorized=authorized, seams=seams,
                          state_dir=state_dir, now=wall(),
                          cancel_check=cancel_check, sleeper=sleeper,
                          monotonic=monotonic)
            if not retry.get("ok"):
                start_reason = retry.get("reason") or "deadline_exceeded"
                if start_reason in (
                        "cancel_requested", "cancel_unreadable",
                        "intent_changed", "intent_unknown",
                        "managed_stop", "round_superseded",
                        "boot_identity_unavailable"):
                    actions[-1]["result"] = "unconfirmed"
                    return _finish("recovery_unconfirmed", EXIT_UNCONFIRMED,
                                   "failed", start_reason,
                                   action="ts_restart", stage="ts_start",
                                   event_status="error",
                                   event_code=start_reason)
                break
        if not confirmed_running:
            if start_sent:
                return _ts_start_unconfirmed(start_reason)
            actions[-1]["result"] = "unconfirmed"
            return _finish("ts_start_unconfirmed", EXIT_UNCONFIRMED, "failed",
                           start_reason, action="ts_restart",
                           stage="ts_start",
                           event_status="recovery_attempted",
                           event_code="ts_start_unconfirmed")
        wait = _wait(TS_PROPAGATION_S, deadline=deadline,
                     authorized=authorized, seams=seams,
                     state_dir=state_dir, now=wall(),
                     cancel_check=cancel_check, sleeper=sleeper,
                     monotonic=monotonic)
        if not wait.get("ok"):
            reason = wait.get("reason") or "deadline_exceeded"
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               action="ts_restart", stage="ts_start",
                               event_status="stopped", event_code=reason)
            return _recovery_unconfirmed(
                reason if reason in ("round_superseded",
                                     "boot_identity_unavailable")
                else "deadline_exceeded", stage="ts_start")
        if monotonic() >= deadline:
            return _recovery_unconfirmed("deadline_exceeded",
                                          stage="verify")
        probe_result = _run_probe(probe, url, wall(), target_reader=target_reader)
        current["entries"] = probe_result["entries"]
        current["overall"] = probe_result["overall"]
        current["reason"] = probe_result["reason"]
        current["dns"] = _sanitize_dns(probe_result.get("dns"))
        outcome = _probe_outcome(probe_result)
        if outcome == "ok":
            return _finish("recovered", EXIT_OK, "succeeded", "ok",
                           action="ts_restart", stage="done", overall="ok",
                           event_status="recovered", event_code="ok",
                           notify_code="share_public_recovered")
        if outcome == "degraded":
            return _finish("degraded", EXIT_OK, "succeeded",
                           probe_result["reason"], action="ts_restart",
                           stage="done", overall="degraded",
                           event_status="degraded",
                           event_code=probe_result["reason"],
                           notify_code="share_public_degraded")
        if outcome != "all_failed":
            return _finish("manual_required", EXIT_REFUSED,
                           "manual_required", probe_result["reason"],
                           action="ts_restart", stage="verify",
                           event_status="manual_required",
                           event_code=probe_result["reason"],
                           notify_code="share_public_manual_required")

    if monotonic() >= deadline:
        return _deadline_after_actions("mapping_resend")
    # C. Mapping resend #2 (still counts against the two mapping slots).
    if (budget or {}).get("mapping_used", 0) < MAPPING_LIMIT:
        sent = _send_action(action="mapping_resend_after_ts", verb="on",
                            kind="mapping", seams=seams,
                            authorized=authorized, paths=paths,
                            lock_dir=lock_dir, state_dir=state_dir, now=wall(),
                            cancel_check=cancel_check, clock=wall,
                            target_reader=target_reader)
        if not sent.get("ok"):
            reason = sent.get("reason") or "mapping_unconfirmed"
            budget = sent.get("budget") or budget
            if reason == "budget_exhausted":
                return _finish("budget_exhausted", EXIT_REFUSED,
                               "budget_exhausted", reason,
                               event_status="budget_exhausted",
                               event_code=reason,
                               notify_code="share_public_budget_exhausted")
            if reason == "manual_required" or reason in (
                    "maintenance_in_force", "maintenance_unavailable",
                    "boot_identity_unavailable"):
                return _gate_failure(reason,
                                     action="mapping_resend_after_ts",
                                     stage="mapping_resend")
            if reason == "share_public_busy":
                return _result("share_public_busy", EXIT_REFUSED,
                               overall=current.get("overall"), reason=reason,
                               snapshot=current,
                               action="mapping_resend_after_ts")
            if reason in ("round_superseded",
                          "budget_unreadable", "seams_unavailable"):
                return _finish(reason, EXIT_REFUSED,
                               "failed" if reason == "round_superseded"
                               else "manual_required", reason,
                               action="mapping_resend_after_ts",
                               stage="mapping_resend",
                               event_status="error", event_code=reason)
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "intent_stopped", "managed_stop_in_force"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               action="mapping_resend_after_ts",
                               stage="mapping_resend",
                               event_status="stopped", event_code=reason)
            actions.append({"action": "mapping_resend_after_ts",
                             "at_utc": _utc_iso(wall()),
                             "result": "unconfirmed"})
            return _finish(reason, EXIT_UNCONFIRMED, "failed", reason,
                           action="mapping_resend_after_ts",
                           stage="mapping_resend",
                           event_status="recovery_attempted",
                           event_code=reason)
        budget = sent.get("budget") or budget
        actions.append({"action": "mapping_resend_after_ts",
                        "at_utc": _utc_iso(wall()), "result": "ok"})
        recovery.update(action="mapping_resend_after_ts",
                        stage="mapping_resend")
        progress = _progress("mapping_resend_after_ts")
        if not progress.get("ok"):
            return _state_write_failure(
                progress, action="mapping_resend_after_ts")
        after = _gate_now(seams=seams, authorized=authorized,
                          state_dir=state_dir, now=wall(),
                          cancel_check=cancel_check)
        if after is not None:
            if after in ("round_superseded", "boot_identity_unavailable"):
                return _mapping_unconfirmed(after, stage="mapping_resend")
            return _finish(after, EXIT_REFUSED,
                           "stopped" if after in (
                               "cancel_requested", "cancel_unreadable",
                               "intent_changed", "intent_unknown",
                               "managed_stop") else "failed",
                           after, action="mapping_resend_after_ts",
                           stage="mapping_resend",
                           event_status="stopped" if after in (
                               "cancel_requested", "cancel_unreadable",
                               "intent_changed", "intent_unknown",
                               "managed_stop") else "error",
                           event_code=after)
        wait = _wait(MAPPING_PROPAGATION_S, deadline=deadline,
                     authorized=authorized, seams=seams,
                     state_dir=state_dir, now=wall(),
                     cancel_check=cancel_check, sleeper=sleeper,
                     monotonic=monotonic)
        if not wait.get("ok"):
            reason = wait.get("reason") or "deadline_exceeded"
            if reason in ("cancel_requested", "cancel_unreadable",
                          "intent_changed", "intent_unknown",
                          "managed_stop"):
                return _finish(reason, EXIT_REFUSED, "stopped", reason,
                               action="mapping_resend_after_ts",
                               stage="mapping_resend",
                               event_status="stopped", event_code=reason)
            return _mapping_unconfirmed(
                reason if reason in ("round_superseded",
                                     "boot_identity_unavailable")
                else "deadline_exceeded", stage="mapping_resend")
        if monotonic() >= deadline:
            return _mapping_unconfirmed("deadline_exceeded",
                                         stage="verify")
        probe_result = _run_probe(probe, url, wall(), target_reader=target_reader)
        current["entries"] = probe_result["entries"]
        current["overall"] = probe_result["overall"]
        current["reason"] = probe_result["reason"]
        current["dns"] = _sanitize_dns(probe_result.get("dns"))
        outcome = _probe_outcome(probe_result)
        if outcome == "ok":
            return _finish("recovered", EXIT_OK, "succeeded", "ok",
                           action="mapping_resend_after_ts", stage="done",
                           overall="ok", event_status="recovered",
                           event_code="ok",
                           notify_code="share_public_recovered")
        if outcome == "degraded":
            return _finish("degraded", EXIT_OK, "succeeded",
                           probe_result["reason"],
                           action="mapping_resend_after_ts", stage="done",
                           overall="degraded", event_status="degraded",
                           event_code=probe_result["reason"],
                           notify_code="share_public_degraded")
        # An unverified final probe (incomplete/not_checked) must not be folded
        # into a confirmed budget conclusion; it needs manual review.
        if outcome != "all_failed":
            return _finish("manual_required", EXIT_REFUSED,
                           "manual_required", probe_result["reason"],
                           action="mapping_resend_after_ts", stage="verify",
                           event_status="manual_required",
                           event_code=probe_result["reason"],
                           notify_code="share_public_manual_required")

    final_action = actions[-1].get("action") if actions else "none"
    final_stage = "ts_service" if final_action == "ts_service_start" else "done"
    return _finish("budget_exhausted", EXIT_REFUSED, "budget_exhausted",
                   "budget_exhausted", action=final_action,
                   stage=final_stage,
                   event_status="budget_exhausted",
                   event_code="budget_exhausted",
                   notify_code="share_public_budget_exhausted")


def _probe_outcome(probe_result):
    overall = probe_result.get("overall")
    if overall == "ok":
        return "ok"
    if overall == "degraded":
        return "degraded"
    if overall == "all_failed":
        return "all_failed"
    return "unconfirmed"


def _expired_lease_step(*, seams, authorized, state_dir, now, cancel_check):
    """ONE maintenance re-read inside the action lock (never maintenance-end)."""
    if cancel_check is not None:
        cancel_check()
    quick = _quick_recheck(seams=seams, authorized=authorized,
                           state_dir=state_dir, now=now,
                           check_maintenance=False,
                           cancel_check=cancel_check)
    if not quick.get("ok"):
        return {"ok": False, "reason": quick.get("reason")}
    view = _maintenance_view(seams, now)
    if view["state"] == "unavailable":
        return {"ok": False, "reason": "maintenance_unavailable"}
    if view["state"] in ("in_force", "expired"):
        return {"ok": False, "reason": "maintenance_in_force"}
    return {"ok": True, "maintenance": view.get("payload")}


def _write_back(paths, lock_dir, snapshot, now, authorized, *, event,
                force_event=False, binding_check=None):
    if not _still_round(paths, authorized):
        return {"ok": False, "reason": "round_superseded"}
    return _persist_snapshot(paths, lock_dir, snapshot, event=event,
                             force_event=force_event, binding_check=binding_check)


def _append_event_best_effort(paths, lock_dir, event, now, *, force=False):
    def _work():
        return {"ok": True, "event": _append_event_in_lock(
            paths, event, now, force=force)}
    return _with_state_lock(lock_dir, _work)


# ---------------------------------------------------------------------------
# Notifications (injected seam; failure never changes the round conclusion)
# ---------------------------------------------------------------------------

def _notify(code):
    if _NOTIFY is not None:
        try:
            return bool(_NOTIFY(code))
        except Exception:  # noqa: BLE001
            return False
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib",
                          "show-share-public-notification.ps1")
    if not os.path.exists(script):
        return False
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", script, "-Code", code],
            capture_output=True, timeout=NOTIFY_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _notification_key(overall, reason):
    """The notification dedupe key: ``<overall>|<reason>``, never a round id.

    A persistent fault keeps the SAME key across consecutive rounds (one
    notification); an independent fault is distinguished by the notification
    sent in between, which replaces the stored key.
    """
    return "%s|%s" % (overall, reason)


def _apply_notification(snapshot, code, key, now):
    """Deduplicated notification; only a SUCCESSFUL send updates the key."""
    notification = snapshot.get("notification") if isinstance(
        snapshot.get("notification"), dict) else {
            "last_key": None, "last_at_utc": None, "last_result": None}
    if notification.get("last_key") == key:
        return None
    if _notify(code):
        notification.update(last_key=key, last_at_utc=_utc_iso(now),
                            last_result="sent")
        snapshot["notification"] = notification
        return None
    notification["last_result"] = "failed"
    snapshot["notification"] = notification
    return _event(now, "error", "notification_failed")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_check(*, now=None, state_dir=None, lock_dir=None, seams=None,
              probe=None, url_provider=None, cancel_check=None):
    """One public DNS/TLS round + snapshot/event write (no recovery)."""
    paths = _paths(state_dir)
    now_epoch = time.time() if now is None else float(now)
    if seams is None:
        seams = _load_production_seams()
    if cancel_check is not None:
        cancel_check()
    boot = _boot_id()
    desired = _read_desired()
    expected_intent = _intent_view(desired)
    url = _public_url(url_provider)
    target_reader = lambda: _public_url(url_provider)
    probe_result = _run_probe(
        probe, url, now_epoch, target_reader=target_reader)
    reason = _live_binding_reason(
        expected_intent, state_dir=state_dir, process_boot=boot)
    if reason is None and probe_result.get("reason") == "round_superseded":
        reason = "round_superseded"
    if reason is not None:
        prior, _ = _read_state(paths)
        return _gate_snapshot(
            paths, lock_dir, boot, now_epoch, desired,
            read_cancel(state_dir=state_dir), prior, reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)
    cancel = read_cancel(state_dir=state_dir)
    prior, _ = _read_state(paths)
    budget = _budget_view(paths, now_epoch)
    local = _observe_local(seams, now_epoch)
    cancel = read_cancel(state_dir=state_dir)
    # Local observations may wait on the broker/OS. Bind their eventual snapshot
    # to the latest target outside the short file-state commit lock.
    if target_reader() != url:
        return _gate_snapshot(
            paths, lock_dir, boot, now_epoch, desired, cancel, prior,
            "round_superseded", "round_superseded", EXIT_REFUSED)
    notification = _notification_view(prior)
    recovery = _recovery_view(prior)
    snapshot = _make_snapshot(
        boot_id=boot, now=now_epoch, intent=desired, probe=probe_result,
        phase="normal", confirmation=0,
        next_check_at=_utc_iso(now_epoch + NORMAL_INTERVAL_S),
        local=local, recovery=recovery, budget=budget, cancel=cancel,
        notification=notification)
    event = None
    if prior is None or prior.get("overall") != snapshot["overall"] \
            or prior.get("reason") != snapshot["reason"]:
        event = _event(now_epoch, snapshot["overall"], snapshot["reason"])
    write = _persist_snapshot(
        paths, lock_dir, snapshot, event=event,
        binding_check=lambda: _live_binding_reason(
            expected_intent, state_dir=state_dir, process_boot=boot))
    if not write.get("ok") and write.get("reason") in (
            "cancel_requested", "cancel_unreadable", "intent_stopped",
            "intent_unknown", "intent_previous_boot", "boot_identity_unavailable",
            "managed_stop_in_force", "round_superseded"):
        reason = write["reason"]
        return _gate_snapshot(
            paths, lock_dir, boot, now_epoch, desired, cancel, prior,
            reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)
    _append_log(paths, {"at_utc": _utc_iso(now_epoch),
                        "mode": "check", "overall": snapshot["overall"],
                        "code": snapshot["reason"],
                        "action": recovery.get("action", "none")})
    if not write.get("ok") and write.get("reason") == "state_unreadable":
        _append_log(paths, {"at_utc": _utc_iso(now_epoch),
                            "mode": "check", "overall": snapshot["overall"],
                            "code": "state_unreadable",
                            "action": recovery.get("action", "none")})
        return _result(_probe_code(snapshot["overall"]),
                       EXIT_OK if snapshot["overall"] in ("ok", "degraded")
                       else EXIT_REFUSED,
                       overall=snapshot["overall"],
                       reason=snapshot["reason"], snapshot=snapshot,
                       probe=probe_result)
    if not write.get("ok"):
        return _result(write.get("reason") or "error", EXIT_REFUSED,
                       overall=snapshot["overall"],
                       reason=snapshot["reason"], snapshot=snapshot,
                       probe=probe_result)
    return _result(_probe_code(snapshot["overall"]),
                   EXIT_OK if snapshot["overall"] in ("ok", "degraded")
                   else EXIT_REFUSED,
                   overall=snapshot["overall"], reason=snapshot["reason"],
                   snapshot=snapshot, probe=probe_result)


def _probe_code(overall):
    if overall == "ok":
        return "ok"
    if overall == "degraded":
        return "degraded"
    if overall == "all_failed":
        return "all_failed"
    if overall == "incomplete":
        return "incomplete"
    return "not_checked"


def run_cycle(*, apply=True, manual=False, now=None, state_dir=None,
              lock_dir=None, seams=None, probe=None, url_provider=None,
              sleeper=time.sleep, monotonic=time.monotonic,
              cancel_check=None):
    """One bounded supervision round (normal, 10 s re-check and recovery)."""
    paths = _paths(state_dir)
    now_epoch = time.time() if now is None else float(now)
    # Wall-clock readings for budget reservations, action/event times and
    # finished_at; monotonic stays the deadline authority. An injected ``now``
    # (tests) pins the wall clock unless the test seam overrides it.
    wall = _WALL_CLOCK if callable(_WALL_CLOCK) else (
        time.time if now is None else (lambda: now_epoch))
    started = monotonic()
    deadline = started + CYCLE_DEADLINE_S
    if seams is None:
        seams = _load_production_seams()
    prior, _ = _read_state(paths)
    boot = _boot_id()
    desired = _read_desired()
    desired = dict(desired) if isinstance(desired, dict) else desired
    cancel = read_cancel(state_dir=state_dir)

    if cancel.get("cancelled"):
        code = "cancel_unreadable" if cancel.get("unreadable") \
            else "cancel_requested"
        if not apply:
            return _result(code, EXIT_REFUSED, overall="not_checked",
                           reason=code)
        return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                              cancel, prior, code, code, EXIT_REFUSED)

    gate = _intent_gate(desired, boot)
    if gate is not None:
        exit_code = EXIT_OK if gate == "intent_stopped" else EXIT_REFUSED
        if not apply:
            return _result(gate, exit_code, overall="not_checked", reason=gate)
        return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                              cancel, prior, gate, gate, exit_code)

    if monotonic() >= deadline:
        if not apply:
            return _result("deadline_exceeded", EXIT_REFUSED,
                           overall="not_checked", reason="deadline_exceeded")
        return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                              cancel, prior, "deadline_exceeded",
                              "deadline_exceeded", EXIT_REFUSED)

    if cancel_check is not None:
        cancel_check()
    # The apply-round service watchdog observes Tailscale BEFORE the public URL
    # is read. A stopped service is started and confirmed running first, so a
    # not_checked/public_url_unavailable round can recover as well. A check
    # round (apply=False) never acts.
    watchdog = None
    if apply:
        watchdog = _service_watchdog(
            seams=seams, state_dir=state_dir, lock_dir=lock_dir, boot=boot,
            desired=desired, prior=prior, clock=wall,
            cancel_check=cancel_check, sleeper=sleeper, monotonic=monotonic,
            deadline=deadline)
        if watchdog.get("terminal") is not None:
            return watchdog["terminal"]
    url = _public_url(url_provider)
    target_reader = lambda: _public_url(url_provider)
    probe_result = _run_probe(
        probe, url, now_epoch, target_reader=target_reader)
    overall = probe_result["overall"]

    if not apply:
        code = _probe_code(overall)
        return _result(code, EXIT_OK if code in ("ok", "degraded")
                       else EXIT_REFUSED, overall=overall,
                       reason=probe_result["reason"], probe=probe_result)

    if overall != "all_failed":
        return _normal_round(paths, lock_dir, seams, boot, now_epoch, desired,
                             cancel, prior, probe_result, 0, clock=wall,
                             target_reader=target_reader, watchdog=watchdog)

    # The unconditional 10 second re-check has ONE exception: a cancel/intent
    # change observed immediately (no wait, no action).
    reason = _gate_now(seams=seams, authorized=None, state_dir=state_dir,
                       now=now_epoch, cancel_check=cancel_check,
                       expected_intent=_intent_view(desired))
    if reason is not None:
        return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                              cancel, prior, reason, reason, EXIT_REFUSED)

    confirmation = 0
    fast = manual and _manual_retry_eligible(
        prior, probe_result, now=now_epoch, boot_id=boot, desired=desired)
    if fast:
        confirmation = 1
        second = probe_result
    else:
        wait = _wait(CONFIRM_DELAY_S, deadline=deadline, authorized=None,
                     seams=seams, state_dir=state_dir, now=now_epoch,
                     cancel_check=cancel_check, sleeper=sleeper,
                     monotonic=monotonic,
                     expected_intent=_intent_view(desired))
        if not wait.get("ok"):
            reason = wait.get("reason") or "deadline_exceeded"
            return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                                  cancel, prior, reason, reason,
                                  EXIT_REFUSED)
        if monotonic() >= deadline:
            return _gate_snapshot(paths, lock_dir, boot, now_epoch, desired,
                                  cancel, prior, "deadline_exceeded",
                                  "deadline_exceeded", EXIT_REFUSED)
        second = _run_probe(probe, url, now_epoch, target_reader=target_reader)
        confirmation = 2

    if second["overall"] != "all_failed":
        return _normal_round(paths, lock_dir, seams, boot, now_epoch, desired,
                             cancel, prior, second, confirmation, clock=wall,
                             target_reader=target_reader, watchdog=watchdog)

    reason = _live_binding_reason(
        _intent_view(desired), state_dir=state_dir, process_boot=boot)
    if reason is not None:
        return _gate_snapshot(
            paths, lock_dir, boot, now_epoch, desired, cancel, prior,
            reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)

    if _dns_coverage_gap(second):
        # The address list was capped before every entry was checked; this is an
        # incomplete inspection, never a confirmed all-entry failure.
        incomplete = dict(second)
        incomplete["overall"] = "incomplete"
        incomplete["reason"] = "addresses_truncated"
        return _normal_round(
            paths, lock_dir, seams, boot, now_epoch, desired, cancel, prior,
            incomplete, confirmation, clock=wall, target_reader=target_reader,
            watchdog=watchdog)

    # Persist the recovery snapshot and fix the exact round identity BEFORE
    # evaluating any precondition. The all_failed event records the moment the
    # recovery snapshot is written (wall clock), not the round start.
    detected_at = wall()
    budget = _budget_view(paths, now_epoch)
    watchdog_actions = watchdog.get("actions") if isinstance(
        watchdog, dict) and isinstance(watchdog.get("actions"), list) else []
    recovery = _recovery(
        [dict(item) for item in watchdog_actions if isinstance(item, dict)],
        state="pending",
        action=watchdog_actions[-1].get("action") if watchdog_actions else "none",
        stage="ts_service" if watchdog_actions else "precheck",
        manual=manual, attempted_at=_utc_iso(now_epoch))
    snapshot = _make_snapshot(
        boot_id=boot, now=now_epoch, intent=desired, probe=second,
        phase="recovering", confirmation=confirmation,
        next_check_at=None, next_action_at=_utc_iso(detected_at),
        local=_local_default(), recovery=recovery, budget=budget,
        cancel=cancel, notification=_notification_view(prior))
    write = _persist_snapshot(
        paths, lock_dir, snapshot,
        event=_event(detected_at, "all_failed", second["reason"]),
        force_event=False,
        binding_check=lambda: _live_binding_reason(
            _intent_view(desired), state_dir=state_dir, process_boot=boot))
    if not write.get("ok"):
        # A rejected/failed recovery-snapshot write never authorizes an action.
        write_reason = write.get("reason") or "round_superseded"
        if write_reason in (
                "cancel_requested", "cancel_unreadable", "intent_stopped",
                "intent_unknown", "intent_previous_boot",
                "boot_identity_unavailable", "managed_stop_in_force",
                "round_superseded"):
            return _gate_snapshot(
                paths, lock_dir, boot, now_epoch, desired, cancel, prior,
                write_reason, write_reason,
                EXIT_OK if write_reason == "intent_stopped" else EXIT_REFUSED)
        if write_reason == "share_public_busy":
            return _result("share_public_busy", EXIT_REFUSED,
                           overall="all_failed", reason="share_public_busy",
                           snapshot=snapshot)
        return _result("round_superseded", EXIT_REFUSED,
                       overall="all_failed", reason="round_superseded",
                       snapshot=snapshot)
    check = read_status(state_dir=state_dir, now=now_epoch)
    if not _same_round(check.get("snapshot"), snapshot):
        return _result("round_superseded", EXIT_REFUSED,
                       overall="all_failed", reason="round_superseded",
                       snapshot=snapshot)

    precheck = _preconditions(
        seams, snapshot, now_epoch, state_dir=state_dir, manual=manual,
        prior_snapshot=prior)
    # The full precheck does not observe the TS service; keep the watchdog's
    # fresh observation instead of overwriting it with "unavailable".
    local = dict(watchdog.get("local") or {}) if isinstance(
        watchdog, dict) else {}
    local.update(precheck.get("local") or {})
    if isinstance(watchdog, dict) and isinstance(watchdog.get("local"), dict) \
            and watchdog["local"].get("ts_service") in ("running", "stopped"):
        local["ts_service"] = watchdog["local"]["ts_service"]
    precheck["local"] = local
    snapshot["local"] = local
    if precheck.get("ok"):
        return _recover(
            seams=seams, snapshot=snapshot, precheck=precheck, paths=paths,
            lock_dir=lock_dir, state_dir=state_dir, probe=probe, url=url,
            cancel_check=cancel_check, sleeper=sleeper, monotonic=monotonic,
            deadline=deadline, manual=manual, now=now_epoch,
            prior_snapshot=prior, clock=wall, target_reader=target_reader,
            watchdog=watchdog)
    if precheck.get("reason") == "maintenance_lease_expired":
        return _recover(
            seams=seams, snapshot=snapshot, precheck=precheck, paths=paths,
            lock_dir=lock_dir, state_dir=state_dir, probe=probe, url=url,
            cancel_check=cancel_check, sleeper=sleeper, monotonic=monotonic,
            deadline=deadline, manual=manual, now=now_epoch,
            prior_snapshot=prior, clock=wall, target_reader=target_reader,
            watchdog=watchdog)

    reason = precheck.get("reason") or "manual_required"
    if reason in ("intent_stopped", "cancel_requested", "cancel_unreadable",
                  "managed_stop_in_force"):
        state, code, event_status, notify_code = (
            "stopped", reason, "stopped", None)
    elif reason == "budget_exhausted":
        state, code, event_status, notify_code = (
            "budget_exhausted", "budget_exhausted", "budget_exhausted",
            "share_public_budget_exhausted")
    else:
        # Keep the SPECIFIC precondition reason as the code; S5 maps each
        # reason to its own operation error instead of one generic refusal.
        state, code, event_status, notify_code = (
            "manual_required", reason, "manual_required",
            "share_public_manual_required")
    stamp = wall()
    recovery.update(state=state, reason=reason,
                    finished_at_utc=_utc_iso(stamp))
    snapshot["recovery"] = recovery
    snapshot["budget"] = precheck.get("budget", budget)
    snapshot["notification"] = _notification_view(prior)
    notify_event = None
    if notify_code is not None:
        notify_event = _apply_notification(
            snapshot, notify_code,
            "%s|%s" % (snapshot["overall"], reason), stamp)
    write = _persist_snapshot(
        paths, lock_dir, snapshot,
        event=_event(stamp, event_status, reason))
    if notify_event is not None:
        _append_event_best_effort(paths, lock_dir, notify_event, stamp,
                                  force=True)
    if not write.get("ok") and write.get("reason") == "round_superseded":
        return _result("round_superseded", EXIT_REFUSED,
                       overall="all_failed", reason="round_superseded",
                       snapshot=snapshot)
    return _result(code, EXIT_OK if code == "intent_stopped" else EXIT_REFUSED,
                   overall="all_failed", reason=reason, snapshot=snapshot)


def _intent_gate(desired, boot):
    if desired is None:
        return "intent_unknown"
    if desired.get("state") == "stopped":
        return "intent_stopped"
    if not boot:
        return "boot_identity_unavailable"
    if desired.get("boot_id") != boot:
        return "intent_previous_boot"
    return None


def _gate_snapshot(paths, lock_dir, boot, now, desired, cancel, prior, code,
                   reason, exit_code, *, local=None, recovery=None,
                   event_status="not_checked", action="none"):
    desired = _read_desired()
    snapshot = _make_snapshot(
        boot_id=boot, now=now, intent=desired,
        probe={"overall": "not_checked", "reason": reason, "public_url": None,
               "host": None, "entries": []},
        phase="normal", confirmation=0,
        next_check_at=_utc_iso(now + NORMAL_INTERVAL_S),
        local=local if isinstance(local, dict) else _lifecycle_local(),
        recovery=recovery if isinstance(recovery, dict)
        else _recovery_view(prior),
        budget=_budget_view(paths, now),
        cancel=read_cancel(state_dir=paths["base"]),
        notification=_notification_view(prior))
    event = None
    prior_recovery = prior.get("recovery") if isinstance(prior, dict) \
        and isinstance(prior.get("recovery"), dict) else {}
    if prior is None or prior.get("overall") != "not_checked" \
            or prior.get("reason") != reason \
            or prior_recovery.get("state") != snapshot["recovery"].get("state"):
        event = _event(now, event_status, reason, action)
    write = _persist_snapshot(paths, lock_dir, snapshot, event=event)
    _append_log(paths, {"at_utc": _utc_iso(now), "mode": "apply",
                        "overall": "not_checked", "code": code,
                        "action": action})
    if not write.get("ok"):
        write_reason = write.get("reason")
        if write_reason == "share_public_busy":
            return _result("share_public_busy", EXIT_REFUSED,
                           overall="not_checked", reason="share_public_busy",
                           snapshot=snapshot)
        if write_reason == "stale_result":
            return _result("round_superseded", EXIT_REFUSED,
                           overall="not_checked", reason=code,
                           snapshot=snapshot)
        return _result("state_unreadable", EXIT_REFUSED,
                       overall="not_checked", reason="state_unreadable",
                       snapshot=snapshot)
    return _result(code, exit_code, overall="not_checked", reason=reason,
                   snapshot=snapshot, action=action)


def _normal_round(paths, lock_dir, seams, boot, now, desired, cancel, prior,
                  probe_result, confirmation, clock=None, target_reader=None,
                  watchdog=None):
    wall = clock if callable(clock) else (lambda: now)
    expected_intent = _intent_view(desired)
    reason = _live_binding_reason(
        expected_intent, state_dir=paths["base"], process_boot=boot)
    if reason is None and (
            probe_result.get("reason") == "round_superseded"
            or (target_reader is not None
                and target_reader() != probe_result.get("public_url"))):
        reason = "round_superseded"
    if reason is not None:
        return _gate_snapshot(
            paths, lock_dir, boot, now, desired, cancel, prior, reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)
    overall = probe_result["overall"]
    code = _probe_code(overall)
    budget = _budget_view(paths, now)
    local = _lifecycle_local()
    recovery = _recovery_view(prior)
    if isinstance(watchdog, dict):
        if isinstance(watchdog.get("local"), dict):
            local = watchdog["local"]
        if isinstance(watchdog.get("recovery"), dict):
            recovery = watchdog["recovery"]
    snapshot = _make_snapshot(
        boot_id=boot, now=now, intent=desired, probe=probe_result,
        phase="done" if overall in ("ok", "degraded") else "normal",
        confirmation=confirmation,
        next_check_at=_utc_iso(now + NORMAL_INTERVAL_S),
        local=local, recovery=recovery,
        budget=budget, cancel=read_cancel(state_dir=paths["base"]),
        notification=_notification_view(prior))
    snapshot["notification"] = _notification_view(prior)
    notify_event = None
    notify_code = None
    outstanding = snapshot["notification"].get("last_key") or ""
    if overall == "degraded" and not _dns_only_anomaly(snapshot):
        notify_code = "share_public_degraded"
    elif overall == "ok" and (
            outstanding.startswith("degraded|")
            or outstanding.startswith("all_failed|")):
        # A real TLS/entry incident notified earlier stays outstanding across
        # intervening DNS-only rounds; the first fully normal round closes it.
        notify_code = "share_public_recovered"
    stamp = wall()
    event = None
    prior_recovery = prior.get("recovery") if isinstance(prior, dict) \
        and isinstance(prior.get("recovery"), dict) else {}
    if prior is None or prior.get("overall") != overall \
            or prior.get("reason") != probe_result["reason"] \
            or prior_recovery.get("state") != snapshot["recovery"].get("state"):
        event = _event(stamp, overall, probe_result["reason"])
    write = _persist_snapshot(
        paths, lock_dir, snapshot, event=event,
        binding_check=lambda: _live_binding_reason(
            expected_intent, state_dir=paths["base"], process_boot=boot))
    if not write.get("ok") and write.get("reason") in (
            "cancel_requested", "cancel_unreadable", "intent_stopped",
            "intent_unknown", "intent_previous_boot", "boot_identity_unavailable",
            "managed_stop_in_force", "round_superseded"):
        reason = write["reason"]
        return _gate_snapshot(
            paths, lock_dir, boot, now, desired, cancel, prior, reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)
    _append_log(paths, {"at_utc": _utc_iso(stamp), "mode": "apply",
                        "overall": overall, "code": probe_result["reason"],
                        "action": "none"})
    if not write.get("ok"):
        reason = write.get("reason") or "error"
        if reason == "state_unreadable":
            # Persistence failed: log the persistence status and still return
            # THIS round's probe conclusion (the check itself succeeded).
            _append_log(paths, {"at_utc": _utc_iso(stamp), "mode": "apply",
                                "overall": overall,
                                "code": "state_unreadable", "action": "none"})
            return _result(code, EXIT_OK if overall in ("ok", "degraded")
                           else EXIT_REFUSED, overall=overall,
                           reason=probe_result["reason"], snapshot=snapshot)
        return _result("round_superseded" if reason == "stale_result"
                       else reason, EXIT_REFUSED, overall=overall,
                       reason=reason, snapshot=snapshot)
    # A successful probe is not a recovery notification until its bound
    # snapshot has actually committed. Close uses the same state lock.
    reason = _live_binding_reason(
        expected_intent, state_dir=paths["base"], process_boot=boot)
    if reason is not None:
        return _gate_snapshot(
            paths, lock_dir, boot, now, desired, cancel, prior, reason, reason,
            EXIT_OK if reason == "intent_stopped" else EXIT_REFUSED)
    if notify_code is not None:
        key = _notification_key("recovered", "ok") \
            if notify_code == "share_public_recovered" \
            else "%s|%s" % (overall, probe_result["reason"])
        notify_event = _apply_notification(snapshot, notify_code, key, stamp)
        _persist_snapshot(
            paths, lock_dir, snapshot, event=notify_event, force_event=True,
            binding_check=lambda: _live_binding_reason(
                expected_intent, state_dir=paths["base"], process_boot=boot))
    return _result(code, EXIT_OK if overall in ("ok", "degraded")
                   else EXIT_REFUSED, overall=overall,
                   reason=probe_result["reason"], snapshot=snapshot)


def run_recovery(*, snapshot, manual=False, now=None, state_dir=None,
                 lock_dir=None, seams=None, probe=None, cancel_check=None,
                 url_provider=None):
    """Recover from an ALREADY persisted ``all_failed`` round snapshot."""
    paths = _paths(state_dir)
    now_epoch = time.time() if now is None else float(now)
    wall = _WALL_CLOCK if callable(_WALL_CLOCK) else (
        time.time if now is None else (lambda: now_epoch))
    if not isinstance(snapshot, dict) or snapshot.get("overall") != "all_failed":
        return _result("not_all_failed", EXIT_REFUSED,
                       overall=snapshot.get("overall")
                       if isinstance(snapshot, dict) else None,
                       reason="not_all_failed")
    if seams is None:
        seams = _load_production_seams()
    prior, _ = _read_state(paths)
    authorized = snapshot
    round_view = authorized.get("round")
    if not isinstance(round_view, dict) or not round_view.get("id") \
            or not isinstance(authorized.get("observed_at_epoch"),
                              (int, float)):
        round_view = dict(round_view) if isinstance(round_view, dict) else {}
        round_view["id"] = _round_id(now_epoch)
        authorized["round"] = round_view
        authorized["observed_at_epoch"] = float(now_epoch)
    write = _persist_snapshot(paths, lock_dir, authorized)
    if not write.get("ok"):
        return _result("round_superseded", EXIT_REFUSED,
                       overall="all_failed", reason="round_superseded")
    check = read_status(state_dir=state_dir, now=wall())
    if not _same_round(check.get("snapshot"), authorized):
        return _result("round_superseded", EXIT_REFUSED,
                       overall="all_failed", reason="round_superseded")
    precheck = _preconditions(seams, authorized, wall(),
                              state_dir=state_dir, manual=manual,
                              prior_snapshot=prior)
    if not precheck.get("ok") and precheck.get("reason") != "maintenance_lease_expired":
        reason = precheck.get("reason") or "manual_required"
        if reason in ("intent_stopped", "cancel_requested",
                      "cancel_unreadable", "managed_stop_in_force"):
            state, code, status = "stopped", reason, "stopped"
        elif reason == "budget_exhausted":
            state, code, status = ("budget_exhausted", "budget_exhausted",
                                   "budget_exhausted")
        else:
            # Keep the SPECIFIC precondition reason as the code; S5 maps each
            # reason to its own operation error instead of one generic refusal.
            state, code, status = "manual_required", reason, "manual_required"
        stamp = wall()
        recovery = _recovery([], state=state, reason=reason, manual=manual,
                             attempted_at=authorized.get("observed_at_utc"),
                             finished_at=_utc_iso(stamp))
        authorized["recovery"] = recovery
        authorized["local"] = precheck.get("local", _local_default())
        authorized["budget"] = precheck.get("budget",
                                              authorized.get("budget"))
        if state in ("manual_required", "budget_exhausted"):
            authorized["reason"] = reason
        _persist_snapshot(paths, lock_dir, authorized,
                          event=_event(stamp, status, reason))
        return _result(code, EXIT_OK if code == "intent_stopped"
                       else EXIT_REFUSED, overall="all_failed",
                       reason=reason, snapshot=authorized)
    url = _public_url(url_provider)
    if url != authorized.get("public_url"):
        return _result("round_superseded", EXIT_REFUSED,
                       overall="not_checked", reason="round_superseded")
    return _recover(
        seams=seams, snapshot=authorized, precheck=precheck, paths=paths,
        lock_dir=lock_dir, state_dir=state_dir, probe=probe, url=url,
        cancel_check=cancel_check, sleeper=time.sleep, monotonic=time.monotonic,
        deadline=time.monotonic() + CYCLE_DEADLINE_S, manual=manual,
        now=now_epoch, prior_snapshot=prior, clock=wall,
        target_reader=lambda: _public_url(url_provider))


def read_status(*, state_dir=None, event_limit=STATUS_EVENT_LIMIT, now=None):
    """Read-only view (never takes a lock and never writes)."""
    paths = _paths(state_dir)
    now_epoch = time.time() if now is None else float(now)
    current, reason = _read_state(paths)
    state_exists = os.path.exists(paths["state"])
    budget = _budget_view(paths, now_epoch, state_exists=state_exists)
    cancel = read_cancel(state_dir=state_dir)
    events, truncated, unreadable = _read_events(
        paths, now=now_epoch, limit=event_limit)
    return {
        "available": True, "version": STATE_VERSION,
        "checked_at": now_epoch,
        "snapshot": _sanitize_snapshot(current),
        "events": events, "events_truncated": truncated,
        "events_unreadable": unreadable,
        "budget": budget,
        "cancel": {"cancelled": cancel.get("cancelled"),
                   "reason": cancel.get("reason")},
        "unreadable": reason in ("unreadable", "malformed",
                                 "version_mismatch"),
        "reason": "state_unreadable" if reason in (
            "unreadable", "malformed", "version_mismatch") else None,
        "state_file": STATE_FILE_NAME,
    }


_ALLOWED_LOCAL = ("ts_service", "connectivity", "backend", "auth", "gateway",
                  "security_gate", "maintenance", "lifecycle")
_ALLOWED_ENTRY = ("host", "ip", "state", "dns", "tls", "certificate",
                  "reason", "elapsed_ms", "checked_at_epoch")
_DNS_SOURCES = ("cloudflare", "google", "nextdns")
_DNS_SOURCE_STATES = ("ok", "empty", "nxdomain", "error", "timeout",
                      "not_checked")
_DNS_CLASSIFICATIONS = ("ok", "no_answer", "nxdomain", "servfail", "rcode",
                        "http_error", "invalid_json", "timeout",
                        "connection_error", "not_checked")
_DNS_AGG_STATES = ("ok", "empty", "nxdomain", "error", "timeout",
                   "disagreement", "unconfirmed", "not_checked")
_DNS_ANOMALIES = ("empty", "nxdomain", "error", "timeout",
                  "disagreement", "unconfirmed")


def _sanitize_dns(value):
    """Whitelisted two-source DNS evidence; never a raw DoH response."""
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    state = state if state in _DNS_AGG_STATES else "not_checked"
    addresses = []
    truncated = value.get("addresses_truncated") is True
    for address in value.get("addresses") if isinstance(
            value.get("addresses"), list) else []:
        if not isinstance(address, str) or len(address) > 64:
            continue
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.version != 4 or not parsed.is_global:
            continue
        if str(parsed) not in addresses:
            if len(addresses) >= 4:
                truncated = True
                continue
            addresses.append(str(parsed))
    sources = []
    for source in value.get("sources") if isinstance(
            value.get("sources"), list) else []:
        if not isinstance(source, dict) or source.get("source") not in _DNS_SOURCES:
            continue
        source_state = source.get("state")
        classification = source.get("classification")
        source_addresses = []
        source_truncated = source.get("addresses_truncated") is True
        for address in source.get("addresses") if isinstance(
                source.get("addresses"), list) else []:
            if not isinstance(address, str) or len(address) > 64:
                continue
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version != 4 or not parsed.is_global:
                continue
            if str(parsed) not in source_addresses:
                if len(source_addresses) >= 4:
                    source_truncated = True
                    continue
                source_addresses.append(str(parsed))
        status = source.get("status")
        ttl = source.get("ttl_s")
        negative_ttl = source.get("negative_ttl_s")
        checked_at = source.get("checked_at_epoch")
        elapsed = source.get("elapsed_ms")
        sources.append({
            "source": source.get("source"),
            "state": source_state if source_state in _DNS_SOURCE_STATES
            else "not_checked",
            "status": status if type(status) is int and 0 <= status <= 65535
            else None,
            "classification": classification
            if classification in _DNS_CLASSIFICATIONS else "not_checked",
            "addresses": source_addresses,
            "addresses_truncated": source_truncated,
            "ttl_s": ttl if type(ttl) is int and 0 <= ttl <= 2147483647
            else None,
            "negative_ttl_s": negative_ttl if type(negative_ttl) is int
            and 0 <= negative_ttl <= 2147483647 else None,
            "checked_at_epoch": checked_at
            if isinstance(checked_at, (int, float))
            and not isinstance(checked_at, bool) and math.isfinite(checked_at)
            and checked_at > 0 else None,
            "elapsed_ms": elapsed if type(elapsed) is int
            and 0 <= elapsed <= 600000 else None,
        })
    ttl = value.get("ttl_s")
    negative_ttl = value.get("negative_ttl_s")
    reason = value.get("reason")
    checked_at = value.get("checked_at_epoch")
    return {
        "state": state,
        "reason": reason if isinstance(reason, str) and len(reason) <= 64 else None,
        "addresses": addresses,
        "addresses_truncated": truncated,
        "ttl_s": ttl if type(ttl) is int and 0 <= ttl <= 2147483647 else None,
        "negative_ttl_s": negative_ttl if type(negative_ttl) is int
        and 0 <= negative_ttl <= 2147483647 else None,
        "checked_at_epoch": checked_at
        if isinstance(checked_at, (int, float))
        and not isinstance(checked_at, bool) and math.isfinite(checked_at)
        and checked_at > 0 else None,
        "sources": sources,
    }


def _dns_only_anomaly(snapshot):
    """A page-only DNS fault: no real TLS/entry failure may hide behind it."""
    dns = snapshot.get("dns") if isinstance(snapshot, dict) else None
    if not isinstance(dns, dict) or dns.get("state") not in _DNS_ANOMALIES:
        return False
    entries = snapshot.get("entries") if isinstance(
        snapshot.get("entries"), list) else []
    return not any(isinstance(entry, dict) and (
        entry.get("state") == "failed" or entry.get("tls") == "failed"
        or entry.get("certificate") == "failed") for entry in entries)


def _dns_coverage_gap(snapshot):
    """An address-truncated round cannot authorize a confirmed all-failed."""
    dns = snapshot.get("dns") if isinstance(snapshot, dict) else None
    return bool(isinstance(dns, dict) and dns.get("addresses_truncated") is True)


def _sanitize_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return None
    out = {}
    for key in ("version", "boot_id", "observed_at_utc",
                "observed_at_epoch", "public_url", "host", "overall",
                "reason"):
        out[key] = snapshot.get(key)
    intent = snapshot.get("intent")
    out["intent"] = {
        "state": intent.get("state"), "at_utc": intent.get("at_utc"),
        "boot_id": intent.get("boot_id"), "source": intent.get("source"),
    } if isinstance(intent, dict) else None
    round_view = snapshot.get("round")
    out["round"] = {
        key: round_view.get(key) for key in (
            "id", "phase", "started_at_utc", "finished_at_utc",
            "confirmation", "next_check_at_utc", "next_action_at_utc")
    } if isinstance(round_view, dict) else None
    out["entries"] = [
        {key: entry.get(key) for key in _ALLOWED_ENTRY}
        for entry in snapshot.get("entries") or [] if isinstance(entry, dict)]
    out["dns"] = _sanitize_dns(snapshot.get("dns"))
    local = snapshot.get("local")
    out["local"] = {key: local.get(key) for key in _ALLOWED_LOCAL
                    if isinstance(local.get(key), str)} \
        if isinstance(local, dict) else None
    recovery = snapshot.get("recovery")
    if isinstance(recovery, dict):
        out["recovery"] = {
            "state": recovery.get("state"), "action": recovery.get("action"),
            "stage": recovery.get("stage"), "reason": recovery.get("reason"),
            "attempted_at_utc": recovery.get("attempted_at_utc"),
            "finished_at_utc": recovery.get("finished_at_utc"),
            "manual": recovery.get("manual"),
            "actions": [
                {key: item.get(key) for key in (
                    "action", "at_utc", "result")}
                for item in recovery.get("actions") or []
                if isinstance(item, dict)],
        }
    else:
        out["recovery"] = None
    budget = snapshot.get("budget")
    if isinstance(budget, dict):
        out["budget"] = {
            key: budget.get(key) for key in (
                "unreadable", "reason", "window_s", "mapping_used",
                "mapping_limit", "ts_restart_used", "ts_restart_limit",
                "service_start_used", "service_start_limit",
                "mapping_attempts_utc", "ts_restarts_utc",
                "service_starts_utc",
                "mapping_next_eligible_at_utc",
                "ts_restart_next_eligible_at_utc",
                "service_start_next_eligible_at_utc")
        }
    else:
        out["budget"] = None
    cancel = snapshot.get("cancel")
    out["cancel"] = {
        "cancelled": cancel.get("cancelled"),
        "reason": cancel.get("reason")} if isinstance(cancel, dict) else None
    notification = snapshot.get("notification")
    out["notification"] = {
        key: notification.get(key) for key in (
            "last_key", "last_at_utc", "last_result")} \
        if isinstance(notification, dict) else None
    for key in ("state_file", "event_file", "budget_file", "cancel_file"):
        out[key] = snapshot.get(key)
    return out


def export_diagnostics(*, state_dir=None, now=None,
                       event_limit=EVENT_MAX_RETURN):
    """Whitelisted, redacted diagnostics; no secret can be carried in."""
    status = read_status(state_dir=state_dir, event_limit=event_limit,
                         now=now)
    snapshot = _sanitize_snapshot(status.get("snapshot"))
    events = status.get("events")
    sanitized_events = [
        {"at_utc": item.get("at_utc"), "status": item.get("status"),
         "code": item.get("code"), "action": item.get("action")}
        for item in events or [] if isinstance(item, dict)
    ] if isinstance(events, list) else None
    budget = status.get("budget")
    sanitized_budget = {
        key: budget.get(key) for key in (
            "unreadable", "reason", "window_s", "mapping_used",
            "mapping_limit", "ts_restart_used", "ts_restart_limit",
            "service_start_used", "service_start_limit",
            "mapping_attempts_utc", "ts_restarts_utc",
            "service_starts_utc",
            "mapping_next_eligible_at_utc",
            "ts_restart_next_eligible_at_utc",
            "service_start_next_eligible_at_utc")
    } if isinstance(budget, dict) else None
    return {
        "export_version": 1,
        "generated_at": _utc_iso(time.time() if now is None else float(now)),
        "redaction": {"fields": "allowlist", "secrets": "excluded",
                      "credentials": "excluded"},
        "snapshot": snapshot,
        "events": sanitized_events,
        "budget": sanitized_budget,
        "cancel": status.get("cancel"),
        "limits": {
            "window_s": BUDGET_WINDOW_S, "mapping_limit": MAPPING_LIMIT,
            "ts_restart_limit": TS_RESTART_LIMIT,
            "service_start_limit": SERVICE_START_LIMIT,
            "event_max_age_s": EVENT_MAX_AGE_S,
            "event_max_bytes": EVENT_MAX_BYTES,
            "cycle_deadline_s": CYCLE_DEADLINE_S,
        },
    }


# ---------------------------------------------------------------------------
# CLI and offline self-test
# ---------------------------------------------------------------------------

def _exit_for_result(result):
    code = result.get("code")
    if code in ("recovered", "degraded", "ok", "intent_stopped"):
        return EXIT_OK
    if code in ("recovery_unconfirmed", "mapping_unconfirmed",
                "ts_stop_unconfirmed", "ts_start_unconfirmed",
                "ts_service_start_unconfirmed"):
        return EXIT_UNCONFIRMED
    return EXIT_REFUSED


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="分享网关公网 DNS/TLS 巡检与有界恢复")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="one applied supervision round")
    mode.add_argument("--check", action="store_true",
                      help="one read-only probe (writes nothing)")
    mode.add_argument("--selftest", action="store_true",
                      help="offline self-test in TEMP (no production I/O)")
    mode.add_argument("--status", action="store_true",
                      help="print the read-only status view")
    mode.add_argument("--export", action="store_true",
                      help="print the redacted diagnostics export")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--lock-dir", default=None)
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.status:
        print(json.dumps(read_status(state_dir=args.state_dir),
                         ensure_ascii=False))
        return EXIT_OK
    if args.export:
        print(json.dumps(export_diagnostics(state_dir=args.state_dir),
                         ensure_ascii=False))
        return EXIT_OK
    if args.apply:
        result = run_cycle(apply=True, state_dir=args.state_dir,
                           lock_dir=args.lock_dir)
        print(json.dumps(result, ensure_ascii=False))
        # The driver already fixed the contract exit code for this result
        # (including 2 after a destructive action); never re-derive it.
        return int(result.get("exit_code", _exit_for_result(result)))
    if args.check:
        result = run_cycle(apply=False, state_dir=args.state_dir,
                           lock_dir=args.lock_dir)
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get("exit_code", _exit_for_result(result)))
    print("必须指定 --apply、--check、--selftest、--status 或 --export；"
          "监督不做任何未请求的动作", file=sys.stderr)
    return EXIT_REFUSED


class _SelftestClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0.0, float(seconds))


class _SelftestOps:
    def __init__(self, verdict=True, gateway=True):
        self.verdict = verdict
        self.gateway = gateway

    def host_evidence(self, kind):
        return {"verdict": self.verdict, "kind": kind}

    def verify_running(self):
        return self.gateway


class _SelftestTs:
    def __init__(self):
        self.calls = []
        self.state = "running"

    def control(self, action):
        self.calls.append(action)
        self.state = "stopped" if action == "stop" else "running"
        return {"ok": True, "detail": "selftest"}

    def status(self):
        return self.state


def _selftest():
    """TEMP dirs and injected stubs only; never a production path or network."""
    import shutil
    import tempfile

    global _BOOT_ID_READER, _DESIRED_READER, _DESIRED_STOP_READER, _NOTIFY
    global _WALL_CLOCK
    root = tempfile.mkdtemp(prefix="SelfUseAI-share-public-selftest-")
    saved = (_BOOT_ID_READER, _DESIRED_READER, _DESIRED_STOP_READER,
             _WALL_CLOCK, _NOTIFY)
    boot = "selftest-boot"
    state_dir = os.path.join(root, "state")
    lock_dir = os.path.join(root, "locks")
    os.makedirs(state_dir, exist_ok=True)
    clock = _SelftestClock()
    ts = _SelftestTs()
    funnel = {"calls": []}

    def _probe(url):
        return {"version": 1, "checked_at_utc": _utc_iso(clock.value),
                "checked_at_epoch": clock.value, "public_url": url,
                "host": _host_of(url), "overall": "ok", "reason": "ok",
                "deadline_s": PROBE_ROUND_TIMEOUT_S, "elapsed_ms": 1,
                "entries": [{"host": _host_of(url), "ip": "203.0.113.10",
                             "state": "ok", "dns": "ok", "tls": "ok",
                             "certificate": "ok", "reason": "ok",
                             "elapsed_ms": 1,
                             "checked_at_epoch": clock.value}]}

    def _funnel(action):
        funnel["calls"].append(action)
        return {"ok": True, "detail": "selftest"}

    seams = {
        "_MAINTENANCE_STATUS": lambda: {"lease": None,
                                        "lease_expired": False},
        "_BROKER_OPS": _SelftestOps(),
        "_CONNECTIVITY_OK": lambda: True,
        "_CONNECTIVITY_STATE": lambda: "Running",
        "_BACKEND_OBSERVE": lambda forget_stale=True: {
            "running": True, "auth_state": "valid", "token": "never-export"},
        "_FUNNEL_CONTROL": _funnel,
        "_TS_SERVICE_CONTROL": ts.control,
        "_TS_SERVICE_STATUS": ts.status,
    }

    def _expect(condition, why):
        if not condition:
            raise AssertionError(why)

    try:
        _BOOT_ID_READER = lambda: boot
        _DESIRED_READER = lambda: {
            "state": "running", "boot_id": boot,
            "at_utc": "2026-10-24T00:00:00+00:00", "source": "selftest"}
        _DESIRED_STOP_READER = lambda: None
        _WALL_CLOCK = None
        _NOTIFY = lambda code: True

        # 1. Missing cancel is not cancelled; --check writes nothing.
        _expect(read_cancel(state_dir=state_dir)["cancelled"] is False,
                "missing cancel")
        result = run_cycle(apply=False, now=1000.0, state_dir=state_dir,
                           lock_dir=lock_dir, seams=seams, probe=_probe,
                           url_provider=lambda: "https://selftest.ts.net")
        _expect(result["code"] == "ok", "read-only probe code")
        _expect(not os.path.exists(os.path.join(state_dir, STATE_FILE_NAME)),
                "--check must not write state")

        # 2. First applied ok round bootstraps BOTH the state and the budget.
        result = run_cycle(apply=True, now=1001.0, state_dir=state_dir,
                           lock_dir=lock_dir, seams=seams, probe=_probe,
                           url_provider=lambda: "https://selftest.ts.net",
                           sleeper=clock.sleep, monotonic=clock.monotonic)
        _expect(result["code"] == "ok", "applied ok code")
        _expect(os.path.exists(os.path.join(state_dir, STATE_FILE_NAME)),
                "state written")
        _expect(os.path.exists(os.path.join(state_dir, BUDGET_FILE_NAME)),
                "budget bootstrapped")

        # 3. Two all_failed rounds recover through ONE mapping resend.
        probe_state = {"all_failed": True}

        def _failing_probe(url):
            if probe_state["all_failed"]:
                return {"version": 1, "checked_at_utc": _utc_iso(clock.value),
                        "checked_at_epoch": clock.value, "public_url": url,
                        "host": _host_of(url), "overall": "all_failed",
                        "reason": "all_entries_failed",
                        "deadline_s": PROBE_ROUND_TIMEOUT_S,
                        "elapsed_ms": 1,
                        "entries": [{"host": _host_of(url),
                                     "ip": "203.0.113.10",
                                     "state": "failed", "dns": "ok",
                                     "tls": "failed",
                                     "certificate": "not_checked",
                                     "reason": "tls_handshake_failed",
                                     "elapsed_ms": 1,
                                     "checked_at_epoch": clock.value}]}
            return _probe(url)

        # A failed probe returns all_failed for the first TWO rounds; after
        # the mapping resend the third probe succeeds.
        calls = {"count": 0}

        def _recovery_probe(url):
            calls["count"] += 1
            if calls["count"] <= 2:
                return _failing_probe(url)
            probe_state["all_failed"] = False
            return _probe(url)

        result = run_cycle(apply=True, now=1002.0, state_dir=state_dir,
                           lock_dir=lock_dir, seams=seams,
                           probe=_recovery_probe,
                           url_provider=lambda: "https://selftest.ts.net",
                           sleeper=clock.sleep, monotonic=clock.monotonic)
        _expect(result["code"] == "recovered", "recovered code")
        _expect(funnel["calls"] == ["on"], "one mapping resend")
        budget = _budget_view(_paths(state_dir), 1002.0)
        _expect(budget["mapping_used"] == 1, "mapping budget consumed")

        # 4. A stopped TS service is started by the watchdog before the probe,
        #    and the round then checks the public URL normally.
        ts.state = "stopped"
        result = run_cycle(
            apply=True, now=1003.0, state_dir=state_dir, lock_dir=lock_dir,
            seams=seams, probe=_probe,
            url_provider=lambda: "https://selftest.ts.net"
            if ts.state == "running" else None,
            sleeper=clock.sleep, monotonic=clock.monotonic)
        _expect(result["code"] == "ok", "watchdog recovered probe")
        _expect(ts.calls == ["start"], "watchdog sent one start")
        _expect(_budget_view(_paths(state_dir),
                            1003.0)["service_start_used"] == 1,
                "watchdog consumed its own budget")

        # 5. Export never carries an injected token.
        exported = export_diagnostics(state_dir=state_dir, now=1003.0)
        text = json.dumps(exported, ensure_ascii=False)
        _expect("never-export" not in text, "export redacts injected token")
    finally:
        (_BOOT_ID_READER, _DESIRED_READER, _DESIRED_STOP_READER,
         _WALL_CLOCK, _NOTIFY) = saved
        shutil.rmtree(root, ignore_errors=True)
    print("share-public-supervision self-test: PASS")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
