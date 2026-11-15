# -*- coding: utf-8 -*-
"""Process/HTTP helpers shared by the r19 cloud chain fixture.

These are test-only helpers for starting product processes on loopback and
recording bounded HTTP evidence. The legacy two-process chain tests they used to
belong to are retired; this module keeps only the helper surface the current
cloud fixture needs.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _normalize_report_path(raw: str) -> Path:
    """Accept the Windows ``D:/...`` form when this runs inside WSL."""
    path = Path(raw)
    if os.name == "posix" and len(raw) >= 3 and raw[1] == ":" \
            and raw[2] in ("/", "\\"):
        path = Path("/mnt") / raw[0].lower() / raw[3:]
    return path


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-limit:].decode("utf-8", "replace")
    return text


def _free_port(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _wait_for(check, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
        except Exception as exc:  # noqa: BLE001 - readiness probes are retried
            last = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    if last is not None:
        raise TimeoutError(f"timed out waiting; last error: {last}")
    raise TimeoutError("timed out waiting")


def _proc_snapshot(pid: int) -> dict:
    """Linux-only resource snapshot: threads, FDs and sockets.

    Reader threads created by the product are named
    ``chat2api-upstream-reader``; the kernel truncates ``comm`` to 15 chars,
    so the observed prefix is ``chat2api-upstr``.
    """
    task_dir = Path(f"/proc/{pid}/task")
    fd_dir = Path(f"/proc/{pid}/fd")
    tids = list(task_dir.iterdir())
    names = []
    for tid in tids:
        try:
            names.append((tid / "comm").read_text(
                encoding="utf-8", errors="replace").strip())
        except OSError:
            names.append("<unreadable>")
    fds = list(fd_dir.iterdir())
    sockets = 0
    for fd in fds:
        try:
            if os.readlink(fd).startswith("socket:"):
                sockets += 1
        except OSError:
            pass
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return {
        "threads": len(tids),
        "threadNameCounts": counts,
        "readerThreads": sum(
            1 for name in names if name.startswith("chat2api-upstr")),
        "fds": len(fds),
        "sockets": sockets,
    }


def _wait_proc_settle(pid: int, baseline: dict, timeout: float = 12.0):
    """Wait for threads/FDs to return to the pre-request baseline."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        last = _proc_snapshot(pid)
        if (last["threads"] <= baseline["threads"]
                and last["fds"] <= baseline["fds"]
                and last["sockets"] <= baseline["sockets"]):
            return last, int((time.monotonic() - started) * 1000)
        time.sleep(0.25)
    return last, int((time.monotonic() - started) * 1000)


def _stable_proc_snapshot(pid: int, timeout: float = 12.0):
    """Sample until two consecutive snapshots agree.

    A thread that is exiting between the ``/proc/<pid>/task`` listing and its
    ``comm`` read shows up as ``<unreadable>`` and inflates a baseline count;
    comparing two consecutive samples removes that transient race without
    weakening the product resource assertion.
    """
    started = time.monotonic()
    previous = None
    while time.monotonic() - started < timeout:
        current = _proc_snapshot(pid)
        if (previous is not None
                and current["threads"] == previous["threads"]
                and current["fds"] == previous["fds"]
                and current["sockets"] == previous["sockets"]
                and "<unreadable>" not in current["threadNameCounts"]):
            return current
        previous = current
        time.sleep(0.1)
    return previous if previous is not None else _proc_snapshot(pid)


def _read_first_stream_event(port, body, key, timeout=20.0,
                             on_first_data=None):
    """Read exactly the first SSE data line, then close (a client cancel)."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        conn.request("POST", "/v1/chat/completions", body=payload,
                     headers={"Connection": "close",
                               "Authorization": "Bearer " + key,
                               "Content-Type": "application/json"})
        response = conn.getresponse()
        status = response.status
        headers = {k.lower(): v for k, v in response.getheaders()}
        first_line = response.readline()
        if on_first_data is not None and first_line:
            on_first_data()
        return HttpResult("POST", "/v1/chat/completions", status, headers,
                          first_line, _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return HttpResult("POST", "/v1/chat/completions", None, {}, b"",
                          _ms(started), f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _load_module(name: str, path: Path):
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class HttpResult:
    def __init__(self, method, path, status, headers, body, duration_ms,
                 error=None):
        self.method = method
        self.path = path
        self.status = status
        self.headers = headers
        self.body = body
        self.duration_ms = duration_ms
        self.error = error

    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def as_record(self) -> dict:
        record = {
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "durationMs": self.duration_ms,
        }
        if self.error:
            record["error"] = self.error
        payload = self.json()
        if isinstance(payload, dict):
            record["body"] = _bounded_json(payload)
        elif self.body:
            record["bodyText"] = self.text()[:2000]
        return record


def _bounded_json(value, depth=0):
    """Keep report bodies small; never copy a whole long stream."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:12]:
            name = str(key)
            if name.lower() in ("secret", "mk", "token", "api_key",
                                "authorization", "x-admin-control",
                                "x-api-key", "cookie"):
                result[name] = "<redacted>"
            else:
                result[name] = _bounded_json(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_json(v, depth + 1) for v in value[:8]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


def _http_request(port, method, path, *, body=None, key=None,
                   timeout=30.0, stream=False, read_body=True,
                   headers=None, close_after_headers=False):
    """One loopback HTTP request.  ``stream`` reads SSE lines until EOF."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    request_headers = {"Connection": "close"}
    if headers:
        request_headers.update(headers)
    if key:
        request_headers["Authorization"] = "Bearer " + key
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        if close_after_headers:
            raw = b""
            conn.close()
            return HttpResult(method, path, status, response_headers, raw,
                              _ms(started))
        if stream:
            chunks = []
            while True:
                line = response.readline()
                if not line:
                    break
                chunks.append(line)
            raw = b"".join(chunks)
        elif read_body:
            raw = response.read()
        else:
            raw = b""
        conn.close()
        return HttpResult(method, path, status, response_headers, raw,
                          _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — close 失败不覆盖已记录的请求异常
            pass
        return HttpResult(method, path, None, {}, b"", _ms(started),
                          f"{type(exc).__name__}: {exc}")



def _sse_text(raw: bytes) -> str:
    """Join the text deltas of an OpenAI or Anthropic SSE body."""
    pieces = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str):
                    pieces.append(content)
        if payload.get("type") == "content_block_delta":
            delta = payload.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)

def _stream_lines(result: HttpResult):
    return result.body.decode("utf-8", "replace").splitlines()


def _tool_turn_errors(body, call_id, output):
    """Return every structural error of one upstream tool turn.

    The check is deliberately structural: the tool result must be a ``role=tool``
    message whose ``tool_call_id`` and content match, AND an assistant message
    must declare the same id in its ``tool_calls``. A prompt echo or an output
    substring can never satisfy it.
    """
    if not isinstance(body, dict):
        return ["upstream body is not an object"]
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ["upstream body has no messages list"]
    errors = []
    assistant_calls = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            assistant_calls.extend(
                call for call in calls if isinstance(call, dict))
    if not any(call.get("id") == call_id for call in assistant_calls):
        errors.append(
            "assistant tool_calls does not declare %r" % (call_id,))
    tool_messages = [
        message for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"]
    if len(tool_messages) != 1:
        errors.append(
            "expected exactly one tool message, got %d" % (len(tool_messages),))
        return errors
    tool_message = tool_messages[0]
    if tool_message.get("tool_call_id") != call_id:
        errors.append(
            "tool message tool_call_id %r != %r"
            % (tool_message.get("tool_call_id"), call_id))
    if tool_message.get("content") != output:
        errors.append(
            "tool message content %r != %r"
            % (tool_message.get("content"), output))
    tool_index = messages.index(tool_message)
    declared_before = False
    for message in messages[:tool_index]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") == call_id:
                declared_before = True
    if not declared_before:
        errors.append("tool message does not follow its assistant call")
    return errors


def _strict_tool_turn_rejection(result, arrivals):
    """The strict negative contract for a structurally broken tool turn.

    Review #7: the old assertion was ``status == 400 OR broken_errors``, so a
    production that FORWARDED the broken structure still passed through the
    test-side checker.  The strict predicate requires BOTH:
    the client gets a structured 400 refusal AND the request never reaches the
    upstream (``arrivals`` is empty).  A forwarded request fails it even when
    ``_tool_turn_errors`` can detect the broken body.
    """
    if result is None or result.status != 400 or arrivals:
        return False
    payload = result.json()
    error = payload.get("error") if isinstance(payload, dict) else None
    return (isinstance(error, dict)
            and error.get("message") == "unsupported_responses_request")


def _hold_open_request(port, method, path, body, key, started_event,
                        finish_event, timeout=60.0):
    """Send a request, read its headers, and KEEP it in flight.

    ``started_event`` is set once the response headers arrived; the caller can
    then send another request while THIS one still owns its admission slot.
    ``finish_event`` releases the body read (the request then finishes).
    """
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Connection": "close", "Authorization": "Bearer " + key}
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        started_event.set()
        finish_event.wait(timeout)
        raw = response.read()
        return HttpResult(method, path, status, response_headers, raw,
                          _ms(started))
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        started_event.set()
        return HttpResult(method, path, None, {}, b"", _ms(started),
                          f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# --------------------------------------------------------------------------
# process and scenario records
# --------------------------------------------------------------------------
class ManagedProcess:
    def __init__(self, name: str, argv, env, cwd: Path, log_path: Path):
        self.name = name
        self.argv = [str(part) for part in argv]
        self.env = env
        self.cwd = str(cwd)
        self.log_path = log_path
        self.process = None
        self.started_at = None
        self.log = None

    def start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "ab", buffering=0)
        self.process = subprocess.Popen(
            self.argv, cwd=self.cwd, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
        self.started_at = time.monotonic()
        return self

    def stop(self, timeout: float = 10.0):
        process = self.process
        self.process = None
        if process is None:
            if self.log is not None:
                with contextlib.suppress(OSError):
                    self.log.close()
                self.log = None
            return
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        if self.log is not None:
            with contextlib.suppress(OSError):
                self.log.close()
            self.log = None

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def tail(self) -> str:
        return _tail(self.log_path)
