# -*- coding: utf-8 -*-
"""Read-only gateway run-log reader for the console log-detail page (R9-R38).

Contract (handoff A38-2, extended by P3/R3):
- Source registry is server-side only. The client sends source/category ids;
  paths are resolved here from the same constants the producers use, so a
  client can never point the reader at an arbitrary file. Rotated files
  (``chat2api.log.1`` …) are part of the fixed registry, not client paths.
  The registry is inventoried from the launchers/supervisors actually in use
  (``lib/gateway-runtime.ps1`` descriptors, ``start_chat2api.bat``,
  ``lib/watch-*.ps1``, ``share_backend_supervision.py``,
  ``deploy-elevated.ps1``, ``share_broker_server.py``) — never by walking
  directories. Only the chat2api (UM) and share producers are registered; the
  retired Qoder / CLIProxy producers are gone from the runtime.
- Bounded reads: byte/row/wall-clock budgets per request; tail reads walk
  back from EOF, older pages walk back from a validated cursor. The file is
  never locked and the whole file is never read into memory.
- Cursor binds (source, category, file identity, offset). A mismatched file
  identity (rotation/truncation/append with different size) yields
  ``reset_hint`` instead of pretending the lines are contiguous.
- Missing file / unconfigured path / zero records / read failure are distinct
  outcomes: a failure is never counted as 0 rows.
- ``read_overview`` merges every registered target (not only the first
  category of a source) with one independent cursor per target, orders by
  normalized time (original text and unknown-time state preserved) and states
  source coverage / truncation / unread / failure in ``coverage``. It is a
  bounded tail/window merge, never the full history.
- Every line is redacted server-side before it leaves the machine; redaction
  never writes to the source file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_TAIL_BYTES = 262144
MAX_ROWS = 400
MAX_LINE_BYTES = 8192
MAX_QUERY_CHARS = 120
SCAN_SECONDS = 3.0
MAX_ROTATION_INDEX = 5
# Overview merge budget: bounded merged response (per-target budgets still
# apply inside read_entries); rows dropped by this budget are reported.
MAX_OVERVIEW_ROWS = 1200

# Watch directory of the chat2api / share-backend supervision entries:
# SELFUSEAI_GATEWAY_WATCH_DIR (lib/watch-chat2api.ps1:78-82,
# lib/watch-share-backend.ps1 via share_backend_supervision.DEFAULT_WATCH_DIR).
WATCH_DIR_ENV = "SELFUSEAI_GATEWAY_WATCH_DIR"
# Fixed SYSTEM deployment root of the share chain (deploy-elevated.ps1:820,
# run-broker.cmd / install-share-broker.ps1, chat2api.py:247).
SHARE_RUN_ROOT = Path(r"C:\ProgramData\UMShareRun")

_HERE = Path(__file__).resolve().parent

# Line-prefixed timestamps. Accepted producer shapes (all read from the real
# launchers / producers in this repo):
#   ``2026-09-21 10:00:00 INFO …``      python logging / lifecycle records
#   ``2026-09-21T10:00:00+08:00 INFO …`` watch records (UTC offset)
#   ``2026/09/22 22:39:14 …``             Go service / ``%DATE% %TIME%``
#   ``[launcher] 2026/10/07 周三 15:20:42.65 …`` hidden launcher records
# The level is optional: a line whose time is real but whose level is absent
# keeps ts and parse_ok=False (no level is ever invented).
_TS_RE = re.compile(
    r"^(?:(?P<tag>\[[^\]\s]{1,24}\])\s+)?"
    r"\[?(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}"
    r"(?:\s*周[一二三四五六日])?[ T]\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:\s*(?:[+-]\d{2}:?\d{2}|Z))?)\]?\s+"
    r"(?:(?P<level>TRACE|DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|ERR|"
    r"CRITICAL|CRIT|FATAL)\b[,:]?\s*)?"
    r"(?P<message>.*)$", re.IGNORECASE)

# Timestamp text -> comparable epoch seconds (see ``normalize_ts``).
_TS_PARSE_RE = re.compile(
    r"^(?P<year>\d{4})[-/](?P<month>\d{2})[-/](?P<day>\d{2})"
    r"(?:\s*周[一二三四五六日])?"
    r"[ T](?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:[.,](?P<fraction>\d+))?"
    r"\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?$")

_SECRET_RE = [
    # Whole-header shadowing first (multi-field Cookie values must not leak
    # through later key=value pairs), then value-level carriers.
    (re.compile(r"(?i)\b(cookie\s*:\s*).+$"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(x-admin-control|x-csrf-token|x-api-key|x-auth-token|x-session-token)\s*:\s*\S+"),
     r"\1 <redacted>"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization\s*:\s*basic\s+)\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|password|passwd|pwd|token|secret|credential|authorization)\b(\s*[=:]\s*)\S+"),
     r"\1\2<redacted>"),
    # Structured-carrier form: quoted JSON keys ("token": "v"). Keeps the
    # quoted key intact and shadows the quoted value only. (A leading \b
    # would never match: the quote before the key is not a word character.)
    (re.compile(r'''(?ix)("(?:api[_-]?key|apikey|password|passwd|pwd|token|secret|credential|cookie|authorization|access_token|refresh_token|session_token|sign|signature|code)")\s*:\s*"[^"]*"'''),
     r'''\1: "<redacted>"'''),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "<redacted-key>"),
    (re.compile(r"([?&](?:password|passwd|pwd|token|secret|api[_-]?key|apikey|access_token|key|code|credential)=)[^&\s\"']+",
                re.IGNORECASE), r"\1<redacted>"),
]


def redact(text: str) -> str:
    """Redact common secret carriers. Applied to every line before send;
    never writes the source file."""
    # Use each pattern's own replacement template: the fourth entry has no
    # capture group, so a shared r"\1<redacted>" template would raise
    # ``re.error: invalid group reference`` on every call.
    for pattern, repl in _SECRET_RE:
        text = pattern.sub(repl, text)
    return text


def _file_identity(target) -> str | None:
    """Generation identity of a log file as ``ino:dev:size``.

    Taken from the OPEN handle whenever possible (an append keeps the inode
    contiguous; a same-named replacement or rotation switches the
    inode/volume — exactly the signal a stale cursor needs). Accepts an open
    binary handle, a fileno, or a Path (opened briefly, e.g. in tests).
    ``None`` when the file cannot be read.
    """
    try:
        if isinstance(target, int):
            st = os.fstat(target)
        elif hasattr(target, "fileno"):
            st = os.fstat(target.fileno())
        else:
            with open(target, "rb") as opened:
                st = os.fstat(opened.fileno())
        return f"{st.st_ino}:{st.st_dev}:{st.st_size}"
    except (OSError, TypeError):
        return None


def _cursor_meta(key: str | None) -> tuple[int, int, int] | None:
    """Parse a cursor file identity into (ino, dev, size); None if malformed.

    Legacy cursors (``name:size:mtime``) do not parse here, so every legacy
    cursor is re-anchored once — honest, one-time, and never silent."""
    if not isinstance(key, str):
        return None
    parts = key.rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _registry_here() -> Path:
    return _HERE


# ---------------------------------------------------------------------------
# Paging cursor codec + bounded read (R9-R38 contract, see module docstring)
# ---------------------------------------------------------------------------


def _decode_cursor(cursor: str) -> dict | None:
    """Decode a paging cursor; return None for anything malformed.

    A cursor is ``base64(json({"src", "cat", "off", "key"}))``. Anything that
    fails to decode, has the wrong shape, or carries a non-int / bool /
    negative offset is rejected as None so the caller answers with a reset
    hint instead of guessing continuity.
    """
    if not isinstance(cursor, str):
        return None
    try:
        raw = base64.b64decode(cursor.encode("ascii"), validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:  # binascii.Error / JSON / unicode errors are ValueError kin
        return None
    if not isinstance(payload, dict):
        return None
    src = payload.get("src")
    cat = payload.get("cat")
    off = payload.get("off")
    key = payload.get("key")
    if not isinstance(src, str) or not isinstance(cat, str):
        return None
    if isinstance(off, bool) or not isinstance(off, int) or off < 0:
        return None
    if key is not None and not isinstance(key, str):
        return None
    return {"src": src, "cat": cat, "off": off, "key": key}


def _encode_cursor(src: str, cat: str, off: int, key: str | None) -> str:
    """Pack (source, category, offset, file identity) into an opaque cursor."""
    payload = json.dumps({"src": src, "cat": cat, "off": int(off), "key": key},
                         ensure_ascii=True)
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _norm_level(raw: str) -> str:
    """Fold abbreviated log levels to their canonical names."""
    level = raw.upper()
    if level == "WARN":
        return "WARNING"
    if level == "ERR":
        return "ERROR"
    return level


# Rev8 B8-5 统一运行事件字段（加性；无法证明的字段一律 None，绝不虚构）。
_EVENT_KEYS = ("timestamp", "model", "request_id", "phase", "outcome",
               "error_code", "duration_ms", "token_rate")


def _event_view(body: dict, ts: str | None) -> dict:
    """Structured run-event view of one ledger row (Rev8 B8-5, additive).

    字段：timestamp/model/request_id/phase/outcome/error_code/duration_ms/
    token_rate。model/request_id 先 redact 再截断（客户端可控值绝不原样透
    出）；outcome 由 status 归类（ok/HTTP2xx→ok，HTTP4xx/5xx/error→error，
    其余 None）；token_rate 仅当可信输出 token 与正耗时同时存在才算——
    输出 Token ÷ 整次耗时（含首字等待），kind 标 "mean_overall"（整次平
    均），绝不以请求排行冒充。无证据即 None（未知不是 0）。
    """
    status = body.get("status")
    outcome = None
    if isinstance(status, str) and status:
        low = status.lower()
        if low == "ok" or low.startswith("http2"):
            outcome = "ok"
        elif low.startswith("http4") or low.startswith("http5") or \
                low in ("error", "client_disconnected", "failed"):
            outcome = "error"
    duration = body.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int) or \
            duration <= 0:
        duration = None
    out_tokens = body.get("output_tokens")
    if isinstance(out_tokens, bool) or not isinstance(out_tokens, int):
        out_tokens = None
    token_rate = None
    token_rate_kind = None
    if out_tokens is not None and duration:
        token_rate = round(out_tokens / (duration / 1000), 2)
        token_rate_kind = "mean_overall"
    model = body.get("model")
    request_id = body.get("request_id")
    return {
        "timestamp": ts,
        "model": redact(str(model))[:64] if model is not None else None,
        "request_id": (redact(str(request_id))[:64]
                       if request_id is not None else None),
        "phase": body.get("phase") if isinstance(body.get("phase"), str)
        else None,
        "outcome": outcome,
        "error_code": (str(status)[:32] if outcome == "error" else None),
        "duration_ms": duration,
        "token_rate": token_rate,
        "token_rate_kind": token_rate_kind,
    }


def normalize_ts(ts: str | None) -> float | None:
    """Normalized epoch seconds of a log timestamp, or None when unknown.

    Accepted producer shapes (the ones actually written by the launchers and
    supervisors): ``2026-09-21 10:00:00``, ``2026-09-21 10:00:00,945``,
    ``2026-09-21T10:00:00+08:00``, ``2026/09/22 22:39:14`` (Go
    service), ``2026/10/07 周三 15:20:42.65`` (launcher
    ``%DATE% %TIME%``). A stamp without a zone is read as local time
    (that is what the producers write); a stamp with a zone is converted.
    Anything unrecognized — including ``None`` — returns None: unknown time
    is never guessed.
    """
    if not isinstance(ts, str):
        return None
    text = ts.strip()
    if re.fullmatch(r"\d{10}", text):
        # Integer epoch seconds (share broker / supervision records write
        # ``at`` as epoch seconds, e.g. 1791348801).
        return float(text)
    match = _TS_PARSE_RE.match(text)
    if match is None:
        return None
    fraction = match.group("fraction") or ""
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    zone = match.group("tz")
    if zone == "Z":
        tzinfo = timezone.utc
    elif zone:
        compact = zone.replace(":", "")
        sign = 1 if compact[0] == "+" else -1
        hours, minutes = int(compact[1:3]), int(compact[3:5])
        tzinfo = timezone(sign * timedelta(hours=hours, minutes=minutes))
    else:
        tzinfo = None
    try:
        stamp = datetime(int(match.group("year")), int(match.group("month")),
                         int(match.group("day")), int(match.group("hour")),
                         int(match.group("minute")), int(match.group("second")),
                         microsecond, tzinfo=tzinfo)
    except ValueError:
        return None
    return stamp.timestamp()


def _ledger_json_line(text: str) -> dict | None:
    """Parse one structured JSON line into (ts, message) or None.

    Call-ledger JSONL rows ({"time": ..., "model": ..., "status": ...})
    become timestamped rows; the compact message keeps the diagnostic keys
    verbatim. A structured record with a real time key but none of the
    diagnostic keys (share supervision decisions ``at_utc``, structured
    crash records ``at``) keeps the redacted original JSON text as its message so
    the time still orders the merged overview. Lines without a time key
    return None so they fall through to the verbatim path.  Rev8 B8-5: the
    parsed body rides along as ``event`` (structured run-event fields;
    redact-first, null when not provable).
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        body = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    ts = None
    for key in ("time", "ts", "at_utc", "at"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            ts = value.strip()
            break
        if isinstance(value, int) and not isinstance(value, bool):
            # Integer epoch seconds (share broker / supervision records
            # write ``at`` as epoch seconds).
            ts = str(value)
            break
    if ts is None:
        return None
    bits = []
    for key, label in (("model", "model"), ("status", "status"),
                       ("input_tokens", "in"), ("output_tokens", "out"),
                       ("cached_input_tokens", "cached"),
                       ("reasoning_tokens", "reasoning"),
                       ("duration_ms", "duration_ms"), ("credits", "credits")):
        value = body.get(key)
        if value is None or value == "":
            continue
        bits.append(f"{label}={body[key]}")
    if not bits:
        return {"ts": ts, "message": stripped,
                "event": _event_view(body, ts)}
    return {"ts": ts, "message": " ".join(bits),
            "event": _event_view(body, ts)}


_TS_TOKEN_RE = re.compile(
    r"\brequest=(?P<request>\S+)|\bfinal=(?P<final>\S+)|"
    r"\bmodel=(?P<model>\S+)|\bduration_ms=(?P<duration>\d+)|"
    r"\bstatus=(?P<status>\d{3})")


def _ts_line_event(ts: str, message: str) -> dict:
    """Light structured view of one runtime log line (Rev8 B8-5, additive).

    只提取受控键（request=/final=/model=/duration_ms=/status=），值先脱敏再
    截断；out 字段 None 时 token_rate 不存在（无法解析留 null+脱敏原文）。
    outcome 由 final= 归类：ok/HTTP200→ok，HTTP4xx/5xx→error，其余 None。
    """
    request_id = None
    model = None
    duration = None
    final = None
    status = None
    for match in _TS_TOKEN_RE.finditer(message):
        if match.group("request"):
            request_id = redact(match.group("request"))[:64]
        elif match.group("model"):
            model = redact(match.group("model"))[:64]
        elif match.group("duration"):
            try:
                value = int(match.group("duration"))
            except ValueError:
                value = None
            if value is not None and 0 < value:
                duration = value
        elif match.group("final"):
            final = match.group("final")
        elif match.group("status"):
            status = match.group("status")
    outcome = None
    error_code = None
    if final is not None:
        upper = final.upper()
        if upper == "OK" or upper == "HTTP200":
            outcome = "ok"
        elif upper.startswith("HTTP4") or upper.startswith("HTTP5"):
            outcome = "error"
            error_code = upper[:32]
    if outcome is None and status is not None:
        if status.startswith("4") or status.startswith("5"):
            outcome = "error"
            error_code = f"HTTP{status}"
    return {"timestamp": ts, "model": model, "request_id": request_id,
            "phase": None, "outcome": outcome, "error_code": error_code,
            "duration_ms": duration, "token_rate": None,
            "token_rate_kind": None}


def _null_event() -> dict:
    return {key: None for key in _EVENT_KEYS}


def _outcome(status: str, error: str | None = None) -> dict:
    """Uniform shape for non-ok outcomes: empty rows, no cursor, no path."""
    out = {"status": status, "rows": [], "scanned_bytes": 0,
           "window_start_offset": 0, "truncated": False,
           "next_cursor": None, "reset_hint": False, "file_key": None}
    if error is not None:
        out["error"] = error
    return out


def _find_category(source_id: str, category_id: str,
                   ledger_paths: dict[str, str] | None = None) -> dict | None:
    """Resolve one registered (source, category) definition, or None."""
    for src_def in build_registry(ledger_paths)["sources"]:
        if src_def.get("source") != source_id:
            continue
        for cat_def in src_def.get("categories", []):
            if cat_def.get("category") == category_id:
                return cat_def
        return None
    return None


def read_entries(
    source: str,
    category: str,
    cursor: str | None = None,
    q: str | None = None,
    level: str | None = None,
    since: str | None = None,
    until: str | None = None,
    rotation: int | None = None,
    ledger_paths: dict[str, str] | None = None,
    request_id: str | None = None,
) -> dict:
    """Read one bounded window of a registered run log.

    Tail mode (``cursor=None``) reads at most ``MAX_TAIL_BYTES`` back from
    EOF; cursor mode reads ``[max(0, off-MAX_TAIL_BYTES), off)`` behind a
    validated cursor. Every expected condition (unknown ids, missing or
    denied files, stale cursors, empty results, budget cuts) is reported
    through the ``status``/``truncated``/``reset_hint`` fields instead of
    raising. ``rows`` is always in file order (oldest first).

    ``rotation`` selects a fixed registry rotation generation (1..N for a
    rotating category, e.g. ``chat2api.log.3``); it stays a whitelist
    index, never a client path.

    ``request_id`` matches the parsed event identity exactly, within the
    same bounded window. It never expands the scan to the whole log.
    """
    if request_id is not None and (not isinstance(request_id, str) or
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request_id) is None):
        return _outcome("error", error="invalid_request_id")
    category_def = _find_category(source, category, ledger_paths)
    if category_def is None:
        return _outcome("error", error="unknown_log_source")
    raw_path = category_def.get("path")
    if not raw_path:
        return _outcome("unconfigured", error="not_configured")
    path = Path(raw_path)
    rotation_cap = category_def.get("rotation_index") if isinstance(category_def.get("rotation_index"), int) else 0
    if rotation is not None:
        if not category_def.get("rotating") or not (1 <= rotation <= rotation_cap):
            return _outcome("error", error="invalid_rotation")
        rotated = Path(f"{path}.{rotation}")
        try:
            if rotated.stat().st_size > 0:
                path = rotated
        except OSError:
            # No such rotation generation yet: fall back to the active
            # path rather than inventing content.
            pass
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return _outcome("missing", error="file_missing")
    except PermissionError:
        return _outcome("denied", error="permission_denied")
    except OSError as exc:
        # Short, path-free reason only: never echo the private log path.
        return _outcome("error", error=f"io_error:{type(exc).__name__}")

    with handle:
        size = os.fstat(handle.fileno()).st_size
        file_key = _file_identity(handle)
        if category_def.get("whole_document"):
            # The producer writes ONE atomic JSON document (share broker
            # lifecycle.json: tmp + replace), never a line log: the whole
            # bounded file is a single row. There is no paging — a passed
            # cursor answers reset_hint instead of pretending continuity.
            if cursor is not None:
                return {"status": "ok", "rows": [], "scanned_bytes": 0,
                        "window_start_offset": 0, "truncated": False,
                        "next_cursor": None, "reset_hint": True,
                        "file_key": file_key}
            handle.seek(0)
            data = handle.read(MAX_TAIL_BYTES)
            text = data.decode("utf-8", errors="replace").strip()
            rows = []
            if text:
                record = _ledger_json_line(text)
                if record is None:
                    record = {"ts": None, "message": text,
                              "event": _null_event()}
                rows.append({"ts": record.get("ts"), "level": None,
                             "message": redact(record["message"]),
                             "parse_ok": False, "cut": False,
                             "event": record.get("event")})
            return {"status": "ok", "rows": rows,
                    "scanned_bytes": len(data),
                    "window_start_offset": 0,
                    "truncated": size > len(data),
                    "next_cursor": None, "reset_hint": False,
                    "file_key": file_key}
        cur = None
        if cursor is not None:
            cur = _decode_cursor(cursor) if isinstance(cursor, str) else None
            stale = (cur is None or cur["src"] != source
                     or cur["cat"] != category or cur["off"] > size)
            if not stale and cur is not None and cur["key"] != file_key:
                # Identity is (file-index, volume, size) from the open
                # handle: appends keep the inode (contiguous), a same-named
                # replacement or rotation switches the inode/volume, and a
                # shrunken size is a detectable truncation. Malformed or
                # legacy identities re-anchor once, never silently.
                minted = _cursor_meta(cur["key"])
                current = _cursor_meta(file_key)
                stale = (minted is None or current is None
                         or minted[0] != current[0]
                         or minted[1] != current[1]
                         or current[2] < minted[2])
            if stale:
                # Forged / stale cursor: force the client to re-anchor
                # instead of pretending the pages are contiguous.
                return {"status": "ok", "rows": [], "scanned_bytes": 0,
                        "window_start_offset": 0, "truncated": False,
                        "next_cursor": None, "reset_hint": True,
                        "file_key": file_key}
        older_mode = cur is not None
        off = cur["off"] if older_mode else size
        win_start = max(0, off - MAX_TAIL_BYTES)
        handle.seek(win_start)
        # Both branches read exactly the snapshot budget: after the first
        # fstat a live file may keep growing, but the read must never
        # exceed this request's byte budget (bounded-read contract).
        data = handle.read(off - win_start)
        scanned = len(data)
        if win_start > 0:
            handle.seek(win_start - 1)
            at_line_start = handle.read(1) == b"\n"
            handle.seek(win_start)
            if not at_line_start:
                # The window starts mid-line: align to the first line
                # boundary inside the window.
                nl = data.find(b"\n")
                if nl == -1:
                    # Entire window sits inside one unfinished line.
                    return {"status": "ok", "rows": [],
                            "scanned_bytes": scanned,
                            "window_start_offset": win_start,
                            "truncated": True, "next_cursor": None,
                            "reset_hint": False, "file_key": file_key}
                win_start += nl + 1
                data = data[nl + 1:]

        parts = data.split(b"\n")
        trailing = parts.pop()
        # A non-empty trailing fragment is a line not fully written yet.
        # Tail mode: it is the file's live head -> drop it and flag
        # truncated. Older mode: the fragment continues behind ``off`` and
        # the newer page already covered it (its aligned start), so it is
        # dropped silently -- unless the cursor sits at EOF, where it is a
        # real incomplete tail.
        trailing_live = bool(trailing) and (not older_mode or off >= size)

        deadline = time.monotonic() + SCAN_SECONDS
        parsed: list[dict] = []
        starts: list[int] = []
        pos = win_start
        timed_out = False
        for part in parts:
            if time.monotonic() > deadline:
                timed_out = True
                break
            cut = len(part) > MAX_LINE_BYTES
            text = (part[:MAX_LINE_BYTES] if cut else part).decode(
                "utf-8", errors="replace")
            match = _TS_RE.match(text)
            if match:
                # The level is optional: a real producer time without a level
                # (Go service / launcher records) keeps ts and
                # parse_ok=False — no level is ever invented.
                level_text = match.group("level")
                body = match.group("message")
                tag = match.group("tag")
                if tag:
                    body = tag + " " + body
                safe = redact(body)
                parsed.append({"ts": match.group("ts"),
                               "level": _norm_level(level_text)
                               if level_text else None,
                               "message": safe,
                               "parse_ok": bool(level_text), "cut": cut,
                               "event": _ts_line_event(
                                   match.group("ts"), safe)})
                starts.append(pos)
            elif _ledger_json_line(text):
                # Slice 2 (directed review): structured ledger lines
                # ({"time": ..., "model": ...}) become real rows with a
                # timestamp for time filtering; they still carry no log
                # level, so parse_ok stays False (never invented).
                row = _ledger_json_line(text)
                parsed.append({"ts": row.get("ts"), "level": None,
                               "message": redact(row["message"]),
                               "parse_ok": False, "cut": cut,
                               "event": row.get("event")})
                starts.append(pos)
            elif parsed:
                # Traceback continuation line: keep the context attached to
                # the entry above; empty lines are preserved as-is.
                parsed[-1]["message"] += "\n" + redact(text)
                if cut:
                    parsed[-1]["cut"] = True
            else:
                # Unparsable line with no preceding entry: keep it verbatim
                # (marked), never silently drop it.  Rev8 B8-5：统一事件字段
                # 全 None（无法解析），原文已脱敏。
                parsed.append({"ts": None, "level": None,
                               "message": redact(text),
                               "parse_ok": False, "cut": cut,
                               "event": {key: None
                                         for key in _EVENT_KEYS}})
                starts.append(pos)
            pos += len(part) + 1

        q_needle = (q or "")[:MAX_QUERY_CHARS].lower()
        level_needle = _norm_level((level or "").strip()[:MAX_QUERY_CHARS])
        since_needle = (since or "")[:MAX_QUERY_CHARS]
        until_needle = (until or "")[:MAX_QUERY_CHARS]

        kept: list[tuple[int, dict]] = []
        for start, row in zip(starts, parsed):
            if request_id is not None and (row.get("event") or {}).get("request_id") != request_id:
                continue
            if q_needle and q_needle not in row["message"].lower():
                continue
            if level_needle and (not row["parse_ok"]
                                 or row["level"] != level_needle):
                continue
            if since_needle and (row["ts"] is None
                                 or row["ts"] < since_needle):
                continue
            if until_needle and (row["ts"] is None or not (
                    row["ts"].startswith(until_needle)
                    or row["ts"] <= until_needle)):
                continue
            kept.append((start, row))

        truncated = timed_out or trailing_live
        trimmed = False
        if len(kept) > MAX_ROWS:
            # Row budget: keep the newest rows; the dropped older rows stay
            # reachable through the next_cursor anchor below.
            kept = kept[-MAX_ROWS:]
            truncated = True
            trimmed = True

        next_cursor = None
        # Walk back from the start of what this page actually returned:
        # without trimming that is the aligned window start (the next page is
        # exactly contiguous); after a row-budget cut it is the first returned
        # row, so the trimmed rows stay reachable in BOTH modes (tail mode
        # previously minted the window start and silently skipped them).
        anchor = kept[0][0] if (trimmed and kept) else win_start
        if anchor > 0:
            next_cursor = _encode_cursor(source, category, anchor, file_key)
        return {"status": "ok", "rows": [row for _, row in kept],
                "scanned_bytes": scanned,
                "window_start_offset": win_start,
                "truncated": truncated,
                "next_cursor": next_cursor,
                "reset_hint": False,
                "file_key": file_key,
                **({"request_id": request_id, "request_scope": "read_window"}
                   if request_id is not None else {})}


def _share_backend_paths() -> dict[str, str | None]:
    """Resolve the dedicated share-backend (8110) deployment files.

    Path contract mirrors ``console_share_production._backend_dir``: an env
    override must be absolute, otherwise the fixed LOCALAPPDATA deployment
    directory applies. An unresolvable deployment directory yields ``None``
    per path (registered but unconfigured) instead of guessing a location;
    the caller keeps the categories so the gap stays visible.
    """
    override = os.environ.get("CONSOLE_SHARE_BACKEND_DIR")
    root = None
    if override:
        if os.path.isabs(override):
            root = override
    else:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = os.path.join(local, "SelfUseAI", "ai-gateway", "share-backend")
    # The dedicated backend runs the SAME chat2api.py source with env-redirected
    # state paths (console_share_production._ProductionBackendOps._child_env),
    # so its service log rotates like the main instance and its ledger is the
    # same JSONL shape. ``backend-lifecycle.log`` is the PARENT side's start /
    # stop record (console_share_production.py:1353, README §「退出记录」).
    return {
        "service": (os.path.join(root, "share-backend.log")
                    if root else None),
        "ledger": (os.path.join(root, "usage-share-backend.jsonl")
                   if root else None),
        "lifecycle": (os.path.join(root, "backend-lifecycle.log")
                      if root else None),
    }


def _watch_root() -> Path | None:
    """Watch directory of the supervision entries, or None when unknown.

    Mirrors lib/watch-chat2api.ps1:82-83 / share_backend_supervision.
    DEFAULT_WATCH_DIR: the SELFUSEAI_GATEWAY_WATCH_DIR override wins (it
    must be absolute — a relative override would resolve against the reader's
    cwd, not the producer's), otherwise the fixed LOCALAPPDATA directory.
    """
    override = (os.environ.get(WATCH_DIR_ENV) or "").strip()
    if override:
        return Path(override) if os.path.isabs(override) else None
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not local:
        return None
    return Path(local) / "SelfUseAI" / "ai-gateway" / "watch"


def build_registry(ledger_paths: dict[str, str] | None = None) -> dict:
    """Server-side registry: source/category ids bound to concrete files.

    Inventoried from the launchers / supervisors actually in use, one entry
    per real producer (never a directory walk):

    * chat2api (8000): service log (CHAT2API_SERVICE_LOG), ledger
      (ledger_paths), hidden-launcher console output ``chat2api.console.log``
      + previous run ``.console.prev.log`` (start_chat2api.bat:15-19) and the
      watch decision log ``chat2api-watch.log`` (lib/watch-chat2api.ps1:82);
    * console (8001): the host writes no line log (start_usage_service.bat
      has no redirect), so it stays registered with zero targets and an
      explicit ``note``;
    * share: the 8110 child log/ledger/lifecycle, its watch log, and the
      fixed SYSTEM artifacts of the 8100 share chain + broker (raw.txt,
      share-faults.jsonl, lifecycle.json — deploy-elevated.ps1:820,
      chat2api.py:247, share_broker_server.py:817-818).

    A path that cannot be resolved is registered as ``None`` (read_entries
    answers ``unconfigured``); it is never silently dropped and never
    reported as zero rows.
    """
    here = _HERE
    share_paths = _share_backend_paths()
    watch_root = _watch_root()

    def watch_path(name: str) -> str | None:
        return str(watch_root / name) if watch_root is not None else None

    share_run = SHARE_RUN_ROOT
    sources = [
        {"source": "chat2api", "display": "chat2api 主链（8000）", "categories": [
            {"category": "service", "display": "运行日志 chat2api.log",
             "path": os.environ.get("CHAT2API_SERVICE_LOG") or str(here / "chat2api" / "chat2api.log"),
             "rotating": True, "rotation_index": MAX_ROTATION_INDEX,
             # level_filter: python-logging lines parse levels, so the error
             # view is a level filter over THIS log (never a copied ledger).
             "level_filter": True},
            {"category": "ledger", "display": "调用账本 usage-chat2api.jsonl",
             "path": (ledger_paths or {}).get("chat2api"),
             "rotating": False},
            # start_chat2api.bat:15-19 — the hidden launcher's own console
            # output + Python exit code (current run + previous generation).
            {"category": "console",
             "display": "隐藏启动输出 chat2api.console.log（含启动/退出记录）",
             "path": str(here / "chat2api" / "chat2api.console.log"),
             "rotating": False},
            {"category": "console_prev",
             "display": "上一次启动输出 chat2api.console.prev.log",
             "path": str(here / "chat2api" / "chat2api.console.prev.log"),
             "rotating": False},
            # lib/watch-chat2api.ps1:82 + :108-112 — one 1 MB generation.
            {"category": "watch", "display": "守护日志 chat2api-watch.log",
             "path": watch_path("chat2api-watch.log"),
             "rotating": True, "rotation_index": 1},
        ]},
        {"source": "console", "display": "费用统计服务 8001（本控制台宿主）",
         "categories": [],
         "note": "8001 宿主不落盘行日志（start_usage_service.bat 无重定向，"
                 "usagesrv 的输出只到隐藏窗口）；控制台自身的操作记录在"
                 "「操作记录」页签（data/console-state.sqlite3），此处不伪造"
                 "日志文件。"},
        {"source": "share", "display": "分享链 8100 / 8110（独立实例，非 8000 主链）",
         "categories": [
             {"category": "service", "display": "运行日志 share-backend.log",
              "path": share_paths.get("service"),
              "rotating": True, "rotation_index": MAX_ROTATION_INDEX,
              "level_filter": True},
             {"category": "ledger", "display": "调用账本 usage-share-backend.jsonl",
              "path": share_paths.get("ledger"),
              "rotating": False},
             # console_share_production.py:1353 + README §「退出记录」— the
             # parent side's start/stop/exit record for the 8110 child.
             {"category": "lifecycle", "display": "父侧启停记录 backend-lifecycle.log",
              "path": share_paths.get("lifecycle"),
              "rotating": False},
             # share_backend_supervision.py:72-73 + lib/watch-share-backend.ps1.
             {"category": "watch", "display": "监督日志 share-backend-watch.log",
              "path": watch_path("share-backend-watch.log"),
              "rotating": True, "rotation_index": 1},
             # deploy-elevated.ps1:820-861 — the 8100 SYSTEM task's full
             # stdout/stderr capture (run\live\raw.txt).
             {"category": "gateway", "display": "8100 任务输出 raw.txt",
              "path": str(share_run / "run" / "live" / "raw.txt"),
              "rotating": False},
             # chat2api.py:247 — the append-only bounded fault log.
             {"category": "faults", "display": "8100 故障记录 share-faults.jsonl",
              "path": str(share_run / "run" / "live" / "share-faults.jsonl"),
              "rotating": False},
             # share_broker_server.py:817-818 + :840-844 — the broker's own
             # lifecycle (start/stop) record: ONE atomic JSON document
             # (tmp + replace), so it is read as a whole document.
             {"category": "broker", "display": "broker 启停记录 lifecycle.json",
              "path": str(share_run / "broker" / "lifecycle.json"),
              "rotating": False, "whole_document": True},
         ]},
    ]
    return {"sources": sources}


def _overview_sort_key(item: tuple[dict, int]) -> tuple:
    """Newest normalized time first; unknown-time rows last, stable."""
    row, seq = item
    ts_norm = row.get("ts_norm")
    if ts_norm is None:
        return (1, 0.0, seq)
    return (0, -ts_norm, seq)


def read_overview(cursors=None, q=None, level=None, since=None, until=None,
                  ledger_paths=None, row_budget=None) -> dict:
    """Merge every registered target into one bounded, honest overview.

    Unlike the detail page (one source+category), this reads EVERY registered
    (source, category) target — the first-category truncation is gone. Each
    target keeps its own cursor: ``cursors`` carries opaque strings from a
    previous response (the same codec as ``read_entries``, so the file
    identity is bound per target), and ``next_cursors`` answers one cursor
    per target. Rows are ordered by ``normalize_ts`` (newest first) with the
    original ``ts`` text and the unknown-time state preserved.

    ``coverage`` states the source coverage, the truncation / unread /
    failure counts and the per-target rows — a target that could not be read
    has ``rows: None`` (never 0) and its ``next_cursor`` is None. The
    merged list is a bounded tail/window of each target, never the full
    history; rows dropped by the merged row budget are counted in
    ``rows_dropped_by_budget`` and per target in ``rows_dropped``, and each
    target's own older pages stay reachable through its ``next_cursor``.
    """
    registry = build_registry(ledger_paths)
    cursor_map = {}
    for raw in cursors or []:
        decoded = _decode_cursor(raw) if isinstance(raw, str) else None
        if decoded is not None:
            cursor_map["%s|%s" % (decoded["src"], decoded["cat"])] = raw
    budget = MAX_OVERVIEW_ROWS if row_budget is None else int(row_budget)
    merged: list[tuple[dict, int]] = []
    targets: list[dict] = []
    next_cursors: dict[str, str | None] = {}
    seq = 0
    for src in registry["sources"]:
        source_id = src.get("source")
        for cat in src.get("categories", []):
            category_id = cat.get("category")
            key = "%s|%s" % (source_id, category_id)
            result = read_entries(source_id, category_id,
                                 cursor=cursor_map.get(key), q=q, level=level,
                                 since=since, until=until,
                                 ledger_paths=ledger_paths)
            ok = result.get("status") == "ok"
            report = {
                "source": source_id,
                "category": category_id,
                "display": cat.get("display"),
                "source_display": src.get("display"),
                "status": result.get("status"),
                "error": result.get("error"),
                "truncated": bool(result.get("truncated")),
                "reset_hint": bool(result.get("reset_hint")),
                "scanned_bytes": result.get("scanned_bytes"),
                "window_start_offset": result.get("window_start_offset"),
                # A failed / unconfigured target has rows None, never 0.
                "rows": len(result["rows"]) if ok else None,
                "rows_returned": 0,
                "rows_dropped": 0,
                "next_cursor": result.get("next_cursor"),
            }
            targets.append(report)
            next_cursors[key] = result.get("next_cursor")
            if not ok:
                continue
            for index, row in enumerate(result["rows"]):
                merged.append(({
                    **row,
                    "source": source_id,
                    "category": category_id,
                    "source_display": src.get("display"),
                    "category_display": cat.get("display"),
                    "ts_norm": normalize_ts(row.get("ts")),
                    "identity": "%s|%s|%d" % (source_id, category_id, index),
                }, seq))
                seq += 1
    merged.sort(key=_overview_sort_key)
    dropped = 0
    if 0 <= budget < len(merged):
        dropped = len(merged) - budget
        merged = merged[:budget]
    counts: dict[tuple[str, str], int] = {}
    for row, _seq in merged:
        row_key = (row["source"], row["category"])
        counts[row_key] = counts.get(row_key, 0) + 1
    for report in targets:
        row_key = (report["source"], report["category"])
        report["rows_returned"] = counts.get(row_key, 0)
        if isinstance(report["rows"], int):
            report["rows_dropped"] = report["rows"] - report["rows_returned"]
    source_reports = []
    for src in registry["sources"]:
        source_id = src.get("source")
        rows_of = [t for t in targets if t["source"] == source_id]
        read_n = sum(1 for t in rows_of if t["status"] == "ok")
        rows_read = sum(t["rows"] for t in rows_of
                        if isinstance(t["rows"], int))
        rows_returned = sum(t["rows_returned"] for t in rows_of)
        if not rows_of:
            status = "no_targets"
            note = src.get("note") or "该来源没有注册可读日志目标"
        elif read_n == len(rows_of):
            status = "ok"
            note = None
        elif read_n:
            status = "partial"
            note = "部分目标未读（见目标状态）"
        else:
            status = "unavailable"
            note = "全部目标未读（见目标状态）"
        source_reports.append({
            "source": source_id, "display": src.get("display"),
            "targets_total": len(rows_of), "targets_read": read_n,
            "targets_unread": len(rows_of) - read_n,
            "rows_read": rows_read, "rows_returned": rows_returned,
            "status": status, "note": note,
        })
    read_targets = [t for t in targets if t["status"] == "ok"]

    def _count_status(status: str) -> int:
        return sum(1 for t in targets if t["status"] == status)

    coverage = {
        "targets_total": len(targets),
        "targets_read": len(read_targets),
        "targets_unread": len(targets) - len(read_targets),
        "targets_unconfigured": _count_status("unconfigured"),
        "targets_missing": _count_status("missing"),
        "targets_denied": _count_status("denied"),
        "targets_error": _count_status("error"),
        "targets_truncated": sum(1 for t in targets if t["truncated"]),
        "rows_read": sum(t["rows"] for t in read_targets),
        "rows_returned": len(merged),
        "rows_dropped_by_budget": dropped,
        "sources_total": len(registry["sources"]),
        "sources_read": sum(1 for s in source_reports if s["targets_read"]),
        "sources_without_targets": [s["source"] for s in source_reports
                                    if s["targets_total"] == 0],
        "scope": "bounded_tail_window",
        "full_history": False,
        "note": "有界尾读/窗口合并：不是完整历史，也不代表全部日志已读完；"
                "读取失败 / 未配置 / 缺失的目标行数不计为 0（见目标 "
                "rows=None）；被合并行预算截断而未返回的行不再出现在本次响应"
                "（rows_dropped_by_budget），各目标自身的更早页仍按其 "
                "next_cursor 继续读取",
        "sources": source_reports,
    }
    if not read_targets:
        status = "unavailable"
    elif len(read_targets) == len(targets):
        status = "ok"
    else:
        status = "partial"
    return {
        "status": status,
        "rows": [row for row, _seq in merged],
        "targets": targets,
        "next_cursors": next_cursors,
        "sources": source_reports,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------------
# B5 UM log read surface (chat2api run logs, bounded window)
# ---------------------------------------------------------------------------
#
# 只读契约：
#   read_um_logs(window, levels, source, cursor, limit, now, ledger_paths) ->
#     {lines, sources, scope, earlier, coverage}
#   read_um_request_logs(request_id, limit, ledger_paths) ->
#     {requestId, lines, scope, coverage}
#
# 口径：
#   - 只读 chat2api 的固定注册分类（service/ledger/console/console_prev/
#     watch）；source 是注册 id，客户端永远不传路径。
#   - 有界尾读：复用 read_entries 的 256 KiB / 400 行 / 3 秒预算；不是完整
#     历史。分页游标是服务端铸的 opaque 串，绑定读取窗口与行键。
#   - 只有 32 位小写十六进制 request_id 才关联日志（chat2api 已验证归因
#     格式）；12 位内部短 ID 不关联，行里 requestId=null。
#   - 每行在离开本机前已由 read_entries 脱敏；这里不再回放请求、不读正文
#     或秘密。缺文件/未配置/拒绝/读取失败由 status 区分，合法空列表是
#     status="ok" + lines=[]。

UM_LOG_SOURCE = "chat2api"
# Run-log targets only. The registered "ledger" category is a usage ledger,
# not a run log: the request list already reads it, and mixing it into the
# log view would duplicate request records.
UM_LOG_CATEGORIES = ("service", "console", "console_prev", "watch")
UM_LOG_LEVELS = ("error", "warn", "info", "debug", "unknown")
UM_WINDOWS = ("today", "24h", "7d", "30d")
UM_MAX_PAGE = 200
UM_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UM_BEIJING_TZ = timezone(timedelta(hours=8))


def _um_level(level):
    if not isinstance(level, str):
        return "unknown"
    upper = level.strip().upper()
    if upper in ("ERROR", "ERR", "CRITICAL", "CRIT", "FATAL"):
        return "error"
    if upper in ("WARNING", "WARN"):
        return "warn"
    if upper in ("INFO", "NOTICE"):
        return "info"
    if upper in ("DEBUG", "TRACE"):
        return "debug"
    return "unknown"


def _um_now_ms(now):
    if now is None:
        return int(time.time() * 1000)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return int(now.timestamp() * 1000)
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return int(float(now) * 1000)
    raise TypeError("now must be None, a datetime or epoch seconds")


def _um_day_start(ms):
    stamp = datetime.fromtimestamp(ms / 1000, tz=_UM_BEIJING_TZ)
    return int(stamp.replace(hour=0, minute=0, second=0,
                             microsecond=0).timestamp() * 1000)


def _um_window(window, now_ms):
    if window not in UM_WINDOWS:
        raise ValueError(
            "invalid window %r: expected one of %s"
            % (window, ", ".join(UM_WINDOWS)))
    if window == "today":
        start = _um_day_start(now_ms)
        return start, start + 86_400_000, "今天"
    if window == "24h":
        return now_ms - 86_400_000, now_ms + 1, "近 24 小时"
    days = 7 if window == "7d" else 30
    end = _um_day_start(now_ms) + 86_400_000
    return end - days * 86_400_000, end, "近 %d 天" % days


def _um_encode_cursor(payload):
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _um_decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("invalid cursor")
    try:
        payload = json.loads(
            base64.b64decode(cursor.encode("ascii"), validate=True)
            .decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed cursor is a 400
        raise ValueError("invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("invalid cursor")
    mode = payload.get("mode")
    at = payload.get("at")
    key = payload.get("key")
    reader = payload.get("reader")
    if mode not in ("same", "older"):
        raise ValueError("invalid cursor")
    if at is not None and (isinstance(at, bool)
                            or not isinstance(at, int)):
        raise ValueError("invalid cursor")
    if key is not None and (not isinstance(key, str) or not key):
        raise ValueError("invalid cursor")
    if reader is not None and (not isinstance(reader, str) or not reader):
        raise ValueError("invalid cursor")
    if mode == "same" and at is None:
        raise ValueError("invalid cursor")
    return {"mode": mode, "at": at, "key": key, "reader": reader}


def _um_log_key(category, row, occurrence):
    payload = {
        "source": UM_LOG_SOURCE,
        "category": category,
        "ts": row.get("ts"),
        "level": row.get("level"),
        "message": row.get("message"),
        "request_id": (row.get("event") or {}).get("request_id"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True)
        .encode("utf-8")).hexdigest()[:16]
    return "%s:%s:%s:%d" % (UM_LOG_SOURCE, category, digest, occurrence)


def _um_log_row(category, row, occurrence):
    event = row.get("event") or {}
    request_id = event.get("request_id")
    if not (isinstance(request_id, str)
            and UM_REQUEST_ID_RE.match(request_id)):
        request_id = None
    stamp = normalize_ts(row.get("ts"))
    return {
        "key": _um_log_key(category, row, occurrence),
        "at": int(stamp * 1000) if stamp is not None else None,
        "level": _um_level(row.get("level")),
        "source": ("chat2api" if category == "service"
                    else "chat2api/" + category),
        "text": str(row.get("message") or "")[:4096],
        "requestId": request_id,
    }


def _um_empty_logs(status, error, source, now_ms, note=None):
    start, end, label = _um_window("today", now_ms)
    return {
        "status": status,
        "error": error,
        "lines": [],
        "sources": list(UM_LOG_CATEGORIES),
        "scope": ("chat2api 运行日志有界尾读（最多 256 KiB / 400 行）· "
                  "北京时间%s · 不是完整历史" % label),
        "earlier": None,
        "coverage": {
            "status": status,
            "error": error,
            "source": source,
            "rows_read": 0,
            "rows_returned": 0,
            "undated_rows": 0,
            "truncated": False,
            "bounded": True,
            "note": note or "读取失败/未配置不是合法空列表",
        },
    }


def read_um_logs(window="today", levels=None, source="service",
                  cursor=None, limit=80, now=None, ledger_paths=None):
    """One bounded chat2api run-log window for the UM diagnostics view.

    ``source`` is a registered category id (service/ledger/console/
    console_prev/watch), never a path. ``levels`` is a whitelist of the
    console levels; an empty list means all. The window is Beijing time.
    A missing/denied/unconfigured/failed target is a status, never an
    empty list; a readable target with no matching rows is a legal empty
    list. Every line is redacted by the reader before it leaves the machine.
    """
    if source not in UM_LOG_CATEGORIES:
        return _um_empty_logs("error", "unknown_log_source", source,
                              _um_now_ms(now))
    levels = list(levels or [])
    if any(level not in UM_LOG_LEVELS for level in levels):
        return _um_empty_logs("error", "invalid_level", source,
                              _um_now_ms(now))
    try:
        limit = max(1, min(int(limit), UM_MAX_PAGE))
    except (TypeError, ValueError):
        limit = 80
    now_ms = _um_now_ms(now)
    start, end, label = _um_window(window, now_ms)
    try:
        cur = _um_decode_cursor(cursor) if cursor is not None else None
    except ValueError:
        return _um_empty_logs("error", "invalid_cursor", source, now_ms)
    reader_cursor = cur.get("reader") if cur else None
    result = read_entries(UM_LOG_SOURCE, source, cursor=reader_cursor,
                           ledger_paths=ledger_paths)
    if result.get("status") != "ok":
        return _um_empty_logs(result.get("status") or "error",
                              result.get("error"), source, now_ms,
                              note="读取失败/未配置/缺失/拒绝不是合法空列表")
    rows = result.get("rows") or []
    mapped = []
    undated = 0
    for row in rows:
        line = _um_log_row(source, row, len(mapped))
        if line["at"] is None:
            undated += 1
            continue
        if not (start <= line["at"] < end):
            continue
        if levels and line["level"] not in levels:
            continue
        if cur is not None:
            if cur["mode"] == "same" and cur["at"] is not None:
                if (-line["at"], line["key"]) <= (
                        -cur["at"], cur["key"]):
                    continue
        mapped.append(line)
    mapped.sort(key=lambda line: (-int(line["at"]), line["key"]))
    earlier = None
    if len(mapped) > limit:
        last = mapped[limit - 1]
        earlier = _um_encode_cursor({
            "v": 1, "mode": "same", "at": int(last["at"]),
            "key": last["key"], "reader": reader_cursor})
        mapped = mapped[:limit]
    elif result.get("next_cursor"):
        earlier = _um_encode_cursor({
            "v": 1, "mode": "older", "at": None, "key": None,
            "reader": result["next_cursor"]})
    return {
        "status": "ok",
        "error": None,
        "lines": mapped,
        "sources": list(UM_LOG_CATEGORIES),
        "scope": ("chat2api %s 日志有界尾读（最多 256 KiB / 400 行）· "
                    "北京时间%s · 不是完整历史" % (source, label)),
        "earlier": earlier,
        "coverage": {
            "status": "ok",
            "error": None,
            "source": source,
            "scanned_bytes": result.get("scanned_bytes"),
            "window_start_offset": result.get("window_start_offset"),
            "rows_read": len(rows),
            "rows_returned": len(mapped),
            "undated_rows": undated,
            "truncated": bool(result.get("truncated")),
            "reset_hint": bool(result.get("reset_hint")),
            "bounded": True,
            "note": "有界尾读/窗口合并：不是完整历史",
        },
    }


def read_um_request_logs(request_id, limit=100, ledger_paths=None):
    """Lines tied to one reliable 32-hex request id, bounded per category.

    The id must be the producer's verified 32-lowercase-hex format; anything
    else is refused (never matched by proximity). The reader filters each
    bounded window by the parsed event identity, so no full-log scan and no
    request replay happens here. Missing/denied/error categories are statuses,
    never an empty list.
    """
    if not (isinstance(request_id, str)
            and UM_REQUEST_ID_RE.match(request_id)):
        raise ValueError("invalid_request_id")
    try:
        limit = max(1, min(int(limit), UM_MAX_PAGE))
    except (TypeError, ValueError):
        limit = 100
    lines = []
    categories = {}
    rows_read = 0
    for category in UM_LOG_CATEGORIES:
        result = read_entries(UM_LOG_SOURCE, category,
                               request_id=request_id,
                               ledger_paths=ledger_paths)
        status = result.get("status") or "error"
        categories[category] = {
            "status": status,
            "error": result.get("error"),
            "rows": (len(result.get("rows") or [])
                      if status == "ok" else None),
        }
        if status != "ok":
            continue
        for row in result.get("rows") or []:
            event = row.get("event") or {}
            if event.get("request_id") != request_id:
                continue
            rows_read += 1
            line = _um_log_row(category, row, len(lines))
            if line["requestId"] != request_id:
                continue
            lines.append(line)
    lines.sort(key=lambda line: (
        -(int(line["at"]) if line["at"] is not None else 0),
        line["key"]))
    lines = lines[:limit]
    ok = sum(1 for item in categories.values()
              if item["status"] == "ok")
    if ok == len(categories):
        status = "ok"
    elif ok:
        status = "partial"
    else:
        status = "unavailable"
    return {
        "requestId": request_id,
        "lines": lines,
        "scope": ("chat2api 运行日志有界尾读（service/console/"
                   "console_prev/watch）· 只匹配可靠 32 位十六进制 "
                   "request_id · 不是完整历史"),
        "coverage": {
            "status": status,
            "categories": categories,
            "rows_read": rows_read,
            "rows_returned": len(lines),
            "bounded": True,
            "note": "读取失败/拒绝/缺失的分类不是空列表；见分类状态",
        },
    }
