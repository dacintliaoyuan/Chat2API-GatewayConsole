#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux resource-envelope measurement for the complete UM share chain.

This fixture starts the REAL product processes on loopback only, exactly like
``linux_share_chain_legacy.py``:

    fake UM (verification-only upstream)   127.0.0.1:28999
    chat2api share backend               127.0.0.1:28110
    share gateway (8100 role)              127.0.0.1:28100
    management broker (AF_UNIX)            <temp>/run/broker.sock
    management API (usagesrv cloud role)    127.0.0.1:28096

The whole component, including the fake UM and this measurement harness, is put
inside ONE transient systemd scope:

    systemd-run --scope -p MemoryMax=2G -p MemorySwapMax=0 ...

The outer process (outside the scope) starts ``systemd-run`` and monitors the
scope cgroup, so an OOM kill of the inner process still produces a group record.
The inner process writes a partial JSON after every scenario; the outer merges it
into the final report.

The load sample is deliberately long and non-empty:

* 64 concurrent streaming requests, each answered with 1 MiB of varied text,
  with a 3 s first-chunk delay so all 64 are genuinely in flight while sampled;
* 64 concurrent non-stream requests, each answered with the same 1 MiB text,
  exercising the non-empty long aggregation path.

No product source is modified, no output is truncated, no limit is relaxed, and no
fake success is fabricated.  A real ``[DONE]``/``finish_reason`` is required for
every stream, and the complete content length is checked for every answer.

Exit codes: 0 all groups measured, 1 a measured group failed its envelope checks,
2 environment missing or the scope did not apply the requested MemoryMax.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]          # ai-gateway/
TESTS = HERE / "tests"
DEFAULT_REPORT = Path(
    "D:/git/github/personal_files/SelfUseAI/tmp/um-cloud-p1-20261030/"
    "resource-envelope.json")

if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import linux_share_chain_legacy as chain                      # noqa: E402

SHARE_PORT = chain.SHARE_PORT
BACKEND_PORT = chain.BACKEND_PORT
MANAGEMENT_PORT = chain.MANAGEMENT_PORT
FAKE_UM_PORT = chain.FAKE_UM_PORT
SHARE_MODEL = chain.SHARE_MODEL

DEFAULT_ANSWER_KIB = 1024
DEFAULT_ROUNDS = 2
DEFAULT_CONCURRENCY = 64
KEYS_PER_ROUND = 16
KEY_MAX_CONCURRENCY = 17
STREAM_FIRST_CHUNK_S = 3.0
FAKE_UM_NONSTREAM_DELAY_S = 2.0
FAKE_UM_CHUNK_BYTES = 8192
SCENARIO_TIMEOUT_S = 240.0
SETTLE_TIMEOUT_S = 45.0
SAMPLE_INTERVAL_S = 0.25
READER_COMM_PREFIX = "chat2api-upstr"

_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
    "victor", "whiskey", "xray", "yankee", "zulu", "harbor", "meadow",
    "canyon", "ripple", "lantern", "compass",
)


# --------------------------------------------------------------------------
# paths and small utilities
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def _normalize_path(raw: str) -> Path:
    """Accept the Windows ``D:/...`` form when this runs inside WSL."""
    path = Path(raw)
    if os.name == "posix" and len(raw) >= 3 and raw[1] == ":" \
            and raw[2] in ("/", "\\"):
        path = Path("/mnt") / raw[0].lower() / raw[3:]
    return path


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _long_text(chars: int) -> str:
    """Deterministic varied prose; the tail is never a 1-8 char repeat."""
    parts = []
    total = 0
    index = 0
    while total < chars:
        word = _WORDS[(index * 13 + 5) % len(_WORDS)]
        parts.append(word)
        total += len(word) + 1
        index += 1
    return " ".join(parts)[:chars]


# --------------------------------------------------------------------------
# cgroup v2 / proc readers
# --------------------------------------------------------------------------
def _cgroup_dir() -> Path:
    text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("cgroup v2 unified hierarchy not found")


def _read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path):
    text = _read_text(path)
    if text is None or not text.isdigit():
        return None
    return int(text)


def _read_kv(path: Path) -> dict:
    result = {}
    text = _read_text(path)
    if not text:
        return result
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            result[parts[0]] = int(parts[1])
        except ValueError:
            pass
    return result


def _bytes_to_mb(value):
    if value is None:
        return None
    return value / (1024.0 * 1024.0)


def _proc_stat_ticks(pid: int):
    """Return (utime, stime) in clock ticks from /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2:].split()
    if len(fields) < 13:
        return None
    try:
        return int(fields[11]), int(fields[12])
    except ValueError:
        return None


def _socket_inodes(pid: int) -> set:
    inodes = set()
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def _tcp_peers(pid: int, inodes: set) -> dict:
    """Map socket inode -> local/remote port for the process' own sockets."""
    peers = {}
    if not inodes:
        return peers
    for table in ("tcp", "tcp6"):
        try:
            text = Path(f"/proc/{pid}/net/{table}").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, remote, state, inode = parts[1], parts[2], parts[3], parts[9]
            if inode not in inodes:
                continue
            try:
                local_port = int(local.rsplit(":", 1)[1], 16)
                remote_port = int(remote.rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            peers[inode] = {
                "localPort": local_port,
                "remotePort": remote_port,
                "state": state,
            }
    return peers


def _proc_snapshot(pid: int) -> dict:
    base = {"pid": pid, "alive": False, "rssMb": None,
            "anonMb": None, "fileMb": None, "shmemMb": None,
            "threads": None, "fds": None, "sockets": None,
            "readers": None, "readerThreads": None,
            "upstreamConnections": None, "clientConnections": None,
            "backendConnections": None, "socketPeers": {},
            "threadNames": {}}
    try:
        text = Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return base
    values = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if sep:
            values[key.strip()] = rest.strip()

    def _kb(name):
        raw = values.get(name)
        if not raw:
            return None
        parts = raw.split()
        if not parts:
            return None
        try:
            return int(parts[0]) / 1024.0
        except ValueError:
            return None

    threads = None
    raw_threads = values.get("Threads")
    if raw_threads:
        with contextlib.suppress(ValueError):
            threads = int(raw_threads)
    fds = 0
    sockets = 0
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            fds += 1
            try:
                if os.readlink(fd).startswith("socket:"):
                    sockets += 1
            except OSError:
                pass
    except OSError:
        fds = None
        sockets = None
    readers = 0
    reader_threads = 0
    thread_names = {}
    try:
        for tid in Path(f"/proc/{pid}/task").iterdir():
            try:
                name = (tid / "comm").read_text(
                    encoding="utf-8", errors="replace").strip()
            except OSError:
                name = "<unreadable>"
            thread_names[name] = thread_names.get(name, 0) + 1
            if name.startswith(READER_COMM_PREFIX):
                reader_threads += 1
    except OSError:
        reader_threads = None
    socket_peers = _tcp_peers(pid, _socket_inodes(pid))
    upstream = sum(
        1 for peer in socket_peers.values()
        if peer["remotePort"] == FAKE_UM_PORT and peer["state"] == "01")
    clients = sum(
        1 for peer in socket_peers.values()
        if peer["localPort"] == SHARE_PORT and peer["state"] == "01")
    backends = sum(
        1 for peer in socket_peers.values()
        if peer["remotePort"] == BACKEND_PORT and peer["state"] == "01")
    readers = upstream
    return {
        "pid": pid,
        "alive": True,
        "rssMb": _kb("VmRSS"),
        "anonMb": _kb("RssAnon"),
        "fileMb": _kb("RssFile"),
        "shmemMb": _kb("RssShmem"),
        "threads": threads,
        "fds": fds,
        "sockets": sockets,
        "readers": readers,
        "readerThreads": reader_threads,
        "upstreamConnections": upstream,
        "clientConnections": clients,
        "backendConnections": backends,
        "socketPeers": socket_peers,
        "threadNames": thread_names,
    }


def _aggregate_processes(processes: dict) -> dict:
    result = {
        "rssMb": 0.0,
        "anonMb": 0.0,
        "fileMb": 0.0,
        "shmemMb": 0.0,
        "threads": 0,
        "fds": 0,
        "sockets": 0,
        "readers": 0,
        "readerThreads": 0,
        "upstreamConnections": 0,
        "clientConnections": 0,
        "backendConnections": 0,
        "processes": processes,
        "deadProcesses": sorted(
            name for name, snap in processes.items() if not snap["alive"]),
    }
    for snap in processes.values():
        if not snap["alive"]:
            continue
        for key in ("rssMb", "anonMb", "fileMb", "shmemMb"):
            if snap[key] is not None:
                result[key] += snap[key]
        for key in ("threads", "fds", "sockets", "readers", "readerThreads",
                    "upstreamConnections", "clientConnections",
                    "backendConnections"):
            if snap[key] is not None:
                result[key] += snap[key]
    for key in ("rssMb", "anonMb", "fileMb", "shmemMb",
                "threads", "fds", "sockets", "readers", "readerThreads",
                "upstreamConnections", "clientConnections",
                "backendConnections"):
        result[key] = _round(result[key])
    return result


def _cgroup_snapshot(cgroup: Path) -> dict:
    stat = _read_kv(cgroup / "memory.stat")
    events = _read_kv(cgroup / "memory.events")
    cpu = _read_kv(cgroup / "cpu.stat")
    return {
        "currentMb": _round(_bytes_to_mb(
            _read_int(cgroup / "memory.current"))),
        "peakMb": _round(_bytes_to_mb(_read_int(cgroup / "memory.peak"))),
        "anonMb": _round(_bytes_to_mb(stat.get("anon"))),
        "fileMb": _round(_bytes_to_mb(stat.get("file"))),
        "kernelMb": _round(_bytes_to_mb(stat.get("kernel"))),
        "sockMb": _round(_bytes_to_mb(stat.get("sock"))),
        "shmemMb": _round(_bytes_to_mb(stat.get("shmem"))),
        "fileMappedMb": _round(_bytes_to_mb(stat.get("file_mapped"))),
        "inactiveFileMb": _round(_bytes_to_mb(stat.get("inactive_file"))),
        "activeFileMb": _round(_bytes_to_mb(stat.get("active_file"))),
        "swapCurrentMb": _round(_bytes_to_mb(
            _read_int(cgroup / "memory.swap.current"))),
        "swapPeakMb": _round(_bytes_to_mb(
            _read_int(cgroup / "memory.swap.peak"))),
        "pidsCurrent": _read_int(cgroup / "pids.current"),
        "pidsPeak": _read_int(cgroup / "pids.peak"),
        "cpuUsageUsec": cpu.get("usage_usec"),
        "events": events,
        "memoryMax": _read_text(cgroup / "memory.max"),
        "memoryHigh": _read_text(cgroup / "memory.high"),
        "memorySwapMax": _read_text(cgroup / "memory.swap.max"),
    }


def _host_snapshot() -> dict:
    info = {}
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if parts and parts[0].isdigit():
            info[key.strip()] = int(parts[0]) / 1024.0
    try:
        load = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
    except OSError:
        load = []
    return {
        "memTotalMb": _round(info.get("MemTotal")),
        "memFreeMb": _round(info.get("MemFree")),
        "memAvailableMb": _round(info.get("MemAvailable")),
        "cachedMb": _round(info.get("Cached")),
        "shmemMb": _round(info.get("Shmem")),
        "swapTotalMb": _round(info.get("SwapTotal")),
        "swapFreeMb": _round(info.get("SwapFree")),
        "loadavg": load,
    }


_DISPLAY_TOKENS = (
    "xwayland", "xorg", "weston", "gnome-shell", "kwin", "plasmashell",
    "mutter", "xfce4", "cinnamon",
)


def _display_snapshot() -> dict:
    processes = []
    total = 0.0
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,rss=,comm="],
            capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            raw_pid, raw_rss, comm = parts
            if not any(token in comm.lower() for token in _DISPLAY_TOKENS):
                continue
            try:
                rss_mb = int(raw_rss) / 1024.0
                pid = int(raw_pid)
            except ValueError:
                continue
            processes.append({"pid": pid, "comm": comm,
                              "rssMb": _round(rss_mb)})
            total += rss_mb
    except (OSError, subprocess.SubprocessError):
        pass
    x11_sockets = 0
    try:
        x11_sockets = len(list(Path("/tmp/.X11-unix").iterdir()))
    except OSError:
        pass
    return {
        "processes": processes,
        "rssMb": _round(total),
        "wslgMounted": Path("/mnt/wslg").exists(),
        "x11Sockets": x11_sockets,
    }


def _take_sample(cgroup: Path, app_pids: dict, harness_pids: dict,
                  include_display: bool = False) -> dict:
    cg = _cgroup_snapshot(cgroup)
    app = _aggregate_processes(
        {name: _proc_snapshot(pid) for name, pid in app_pids.items()})
    harness = _aggregate_processes(
        {name: _proc_snapshot(pid) for name, pid in harness_pids.items()})
    host = _host_snapshot()
    outside = None
    if host.get("memTotalMb") is not None \
            and host.get("memAvailableMb") is not None \
            and cg.get("currentMb") is not None:
        host_used = host["memTotalMb"] - host["memAvailableMb"]
        outside = max(0.0, host_used - cg["currentMb"])
    sample = {
        "ts": time.time(),
        "cgroup": cg,
        "app": app,
        "harness": harness,
        "host": host,
        "outsideScopeUsedMb": _round(outside),
    }
    if include_display:
        sample["display"] = _display_snapshot()
    return sample


class Sampler:
    """Continuous cgroup/process sampler; scenarios reset the window."""

    def __init__(self, cgroup: Path, app_pids: dict, harness_pids: dict,
                 interval: float = SAMPLE_INTERVAL_S):
        self.cgroup = cgroup
        self.app_pids = app_pids
        self.harness_pids = harness_pids
        self.interval = interval
        self.samples = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.sample_once()

    def sample_once(self):
        sample = _take_sample(self.cgroup, self.app_pids, self.harness_pids)
        with self._lock:
            self.samples.append(sample)
        return sample

    def reset(self):
        with self._lock:
            self.samples = []

    def snapshot(self):
        with self._lock:
            return list(self.samples)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _peak(samples, getter):
    values = []
    for sample in samples:
        value = getter(sample)
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _peak_sample(samples, getter):
    best = None
    best_value = None
    for sample in samples:
        value = getter(sample)
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_value = value
            best = sample
    return best


# --------------------------------------------------------------------------
# fake UM control (separate process, still inside the scope)
# --------------------------------------------------------------------------
def _fake_control(method: str, path: str, payload=None, timeout=10.0):
    conn = http.client.HTTPConnection("127.0.0.1", FAKE_UM_PORT,
                                     timeout=timeout)
    body = None
    headers = {"Connection": "close"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, raw
    finally:
        conn.close()


def _fake_state() -> dict:
    status, raw = _fake_control("GET", "/__control/state")
    if status != 200:
        raise RuntimeError(f"fake UM state returned {status}")
    return json.loads(raw.decode("utf-8"))


def _fake_reset() -> None:
    status, raw = _fake_control("POST", "/__control/reset", {})
    if status != 200:
        raise RuntimeError(f"fake UM reset returned {status}: {raw[:200]!r}")


def _fake_mode(mode: str) -> None:
    status, raw = _fake_control("POST", "/__control/mode", {"mode": mode})
    if status != 200:
        raise RuntimeError(f"fake UM mode {mode} returned {status}: "
                           f"{raw[:200]!r}")


# --------------------------------------------------------------------------
# HTTP load probes (streaming is consumed incrementally; nothing is retained)
# --------------------------------------------------------------------------
def _stream_probe(body: bytes, key: str, timeout: float) -> dict:
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", SHARE_PORT,
                                     timeout=timeout)
    headers = {"Connection": "close",
               "Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    status = None
    content_chars = 0
    done = False
    finish_reason = None
    events = 0
    bytes_read = 0
    error = None
    try:
        conn.request("POST", "/v1/chat/completions", body=body,
                      headers=headers)
        response = conn.getresponse()
        status = response.status
        while True:
            line = response.readline()
            if not line:
                break
            bytes_read += len(line)
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                continue
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            events += 1
            choices = payload.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str):
                        content_chars += len(content)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return {
        "status": status,
        "contentChars": content_chars,
        "done": done,
        "finishReason": finish_reason,
        "events": events,
        "bytesRead": bytes_read,
        "durationMs": int((time.monotonic() - started) * 1000),
        "error": error,
    }


def _nonstream_probe(body: bytes, key: str, timeout: float) -> dict:
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", SHARE_PORT,
                                     timeout=timeout)
    headers = {"Connection": "close",
               "Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    status = None
    content_chars = None
    finish_reason = None
    body_bytes = 0
    error = None
    try:
        conn.request("POST", "/v1/chat/completions", body=body,
                      headers=headers)
        response = conn.getresponse()
        status = response.status
        raw = response.read()
        body_bytes = len(raw)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                if isinstance(choice, dict):
                    finish_reason = choice.get("finish_reason")
                    message = choice.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            content_chars = len(content)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return {
        "status": status,
        "contentChars": content_chars,
        "done": None,
        "finishReason": finish_reason,
        "events": None,
        "bytesRead": body_bytes,
        "durationMs": int((time.monotonic() - started) * 1000),
        "error": error,
    }


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
class EnvelopeFixture(chain.ChainFixture):
    """ChainFixture with the fake UM in its own process."""

    def __init__(self, report_path: Path):
        super().__init__(report_path)
        self.fake_um_process = None
        self.cgroup_dir = None
        self.long_answer_path = None

    def start_fake_um(self):
        self.fake_um_token = chain.secrets.token_urlsafe(32)
        launcher = self.root / "fake_um_launcher.py"
        launcher.write_text(
            "import os\n"
            "import sys\n"
            "import threading\n"
            "import fake_um_server as m\n"
            "m._FakeUMServer.request_queue_size = 256\n"
            "delay = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0\n"
            "if delay > 0:\n"
            "    import time\n"
            "    _orig_chat_body = m._chat_body\n"
            "    def _chat_body(payload, reply, default_model):\n"
            "        time.sleep(delay)\n"
            "        return _orig_chat_body(payload, reply, default_model)\n"
            "    m._chat_body = _chat_body\n"
            "answer_path = sys.argv[5] if len(sys.argv) > 5 else ''\n"
            "if answer_path:\n"
            "    with open(answer_path, encoding='utf-8') as handle:\n"
            "        _answer = handle.read()\n"
            "    m._last_user_text = lambda payload: _answer\n"
            "fake = m.FakeUM(host='127.0.0.1', port=int(sys.argv[1]),\n"
            "                model=%r,\n"
            "                token=os.environ['FAKE_UM_TOKEN'],\n"
            "                chunk_size=int(sys.argv[2]),\n"
            "                slow_first_chunk_s=float(sys.argv[3]))\n"
            "fake.start()\n"
            "print('fake UM ready', flush=True)\n"
            "threading.Event().wait()\n" % SHARE_MODEL,
            encoding="utf-8")
        env = dict(self.env)
        env["FAKE_UM_TOKEN"] = self.fake_um_token
        env["PYTHONPATH"] = os.pathsep.join(
            [str(TESTS / "fake_um"), str(HERE),
             str(HERE / "chat2api"), str(TESTS)])
        self.fake_um_process = self._process(
            "fake-um",
            [sys.executable, str(launcher), str(FAKE_UM_PORT),
             str(FAKE_UM_CHUNK_BYTES), str(STREAM_FIRST_CHUNK_S),
             str(FAKE_UM_NONSTREAM_DELAY_S),
             str(self.long_answer_path or "")],
            self.paths["fake_um_log"], env=env)
        self.fake_um_process.start()
        chain._wait_for(lambda: chain._free_port(FAKE_UM_PORT) is False, 15.0)
        chain._wait_for(lambda: _fake_state().get("mode") == "normal", 10.0)
        self.fake_um = None

    def create_keys_for(self, prefix: str):
        keys = []
        for index in range(KEYS_PER_ROUND):
            name = f"{prefix}-{index}-{chain.secrets.token_hex(3)}"
            keys.append(self.create_key(
                name, max_concurrency=KEY_MAX_CONCURRENCY))

        def _all_ready():
            return all(self._models_ok(key["secret"]) for key in keys)

        chain._wait_for(_all_ready, 90.0)
        return keys

    def _models_ok(self, key: str) -> bool:
        result = chain._http_request(
            SHARE_PORT, "GET", "/v1/models", key=key, timeout=2.0)
        return result.status == 200

    def app_pids(self) -> dict:
        result = {}
        for name, attr in (
                ("backend", "backend"),
                ("share", "share"),
                ("broker", "broker"),
                ("management", "management_process")):
            process = getattr(self, attr, None)
            if process is not None and process.process is not None:
                result[name] = process.process.pid
        return result

    def harness_pids(self) -> dict:
        result = {"measurement": os.getpid()}
        if self.fake_um_process is not None \
                and self.fake_um_process.process is not None:
            result["fake-um"] = self.fake_um_process.process.pid
        return result


# --------------------------------------------------------------------------
# helpers for one scenario
# --------------------------------------------------------------------------
def _event_delta(before: dict, after: dict, key: str) -> int:
    try:
        return int(after["cgroup"]["events"].get(key, 0)) - int(
            before["cgroup"]["events"].get(key, 0))
    except (KeyError, TypeError, ValueError):
        return 0


def _cpu_delta(before: dict, after: dict) -> float:
    clk = os.sysconf("SC_CLK_TCK")
    total = 0
    for name, ticks in before.items():
        other = after.get(name)
        if other is None:
            continue
        total += max(0, (other[0] + other[1]) - (ticks[0] + ticks[1]))
    return total / float(clk)


def _reset_peak(cgroup: Path) -> None:
    with contextlib.suppress(OSError):
        (cgroup / "memory.peak").write_text("0", encoding="ascii")


def _settle(fixture: EnvelopeFixture, sampler: Sampler, app_pids: dict,
            baseline_sample: dict,
            timeout: float = SETTLE_TIMEOUT_S) -> dict:
    started = time.monotonic()
    baseline_app = baseline_sample["app"]
    last = None
    while time.monotonic() - started < timeout:
        sample = sampler.sample_once()
        last = sample
        app = sample["app"]
        if (app["threads"] is not None
                and app["fds"] is not None
                and app["sockets"] is not None
                and app["threads"] <= baseline_app["threads"]
                and app["fds"] <= baseline_app["fds"]
                and app["sockets"] <= baseline_app["sockets"]):
            break
        time.sleep(0.5)
    if last is None:
        last = _take_sample(fixture.cgroup_dir, app_pids,
                             fixture.harness_pids())
    return {
        "settleMs": int((time.monotonic() - started) * 1000),
        "rssMb": last["app"]["rssMb"],
        "threads": last["app"]["threads"],
        "fds": last["app"]["fds"],
        "sockets": last["app"]["sockets"],
        "readers": last["app"]["readers"],
        "cgroupCurrentMb": last["cgroup"]["currentMb"],
        "cgroupAnonMb": last["cgroup"]["anonMb"],
        "cgroupFileMb": last["cgroup"]["fileMb"],
        "residualVsBaselineMb": _round(
            (last["app"]["rssMb"] or 0) - (baseline_app["rssMb"] or 0)),
        "deadProcesses": last["app"]["deadProcesses"],
    }


def _run_scenario(fixture: EnvelopeFixture, sampler: Sampler, *,
                   name: str, stream: bool, keys, body: bytes,
                   answer_chars: int, concurrency: int) -> dict:
    scenario = {
        "name": name,
        "stream": stream,
        "answerKiB": answer_chars // 1024,
        "concurrencyTarget": concurrency,
        "requestsPlanned": concurrency,
        "startedAt": _now_iso(),
        "failures": [],
        "notes": [],
    }
    started = time.monotonic()
    app_pids = fixture.app_pids()
    start_ticks = {}
    for process_name, pid in app_pids.items():
        ticks = _proc_stat_ticks(pid)
        if ticks is not None:
            start_ticks[process_name] = ticks
    _reset_peak(fixture.cgroup_dir)
    start_sample = _take_sample(fixture.cgroup_dir, app_pids,
                                fixture.harness_pids(),
                                include_display=True)
    scenario["display"] = start_sample.get("display")
    scenario["displayRssPeakMb"] = (
        start_sample.get("display") or {}).get("rssMb")
    sampler.reset()

    results = [None] * concurrency
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    inflight_event = threading.Event()

    def worker(index: int):
        nonlocal in_flight, max_in_flight
        key = keys[index % len(keys)]["secret"]
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if in_flight >= concurrency:
                inflight_event.set()
        try:
            if stream:
                result = _stream_probe(body, key, SCENARIO_TIMEOUT_S)
            else:
                result = _nonstream_probe(body, key, SCENARIO_TIMEOUT_S)
        finally:
            with lock:
                in_flight -= 1
        results[index] = result

    threads = [
        threading.Thread(target=worker, args=(index,),
                          name=f"envelope-client-{index}", daemon=True)
        for index in range(concurrency)]
    for thread in threads:
        thread.start()
        # The product listener keeps a small accept backlog; this pacing
        # avoids a pure client-side connection-refused race while every
        # request is still genuinely in flight (3 s stream first chunk or
        # the fake UM's 2 s non-stream delay).
        time.sleep(0.005)
    inflight_ready = inflight_event.wait(timeout=60.0)
    inflight_sample = None
    if inflight_ready:
        for _ in range(4):
            sampler.sample_once()
            time.sleep(0.15)
        inflight_sample = _take_sample(
            fixture.cgroup_dir, app_pids, fixture.harness_pids(),
            include_display=True)
        inflight_sample["inFlight"] = concurrency
        try:
            inflight_sample["fakeRequestCount"] = _fake_state().get(
                "request_count")
        except Exception as exc:  # noqa: BLE001 - recorded
            inflight_sample["fakeRequestCountError"] = str(exc)
    else:
        scenario["notes"].append(
            "client in-flight never reached %d; max observed %d" % (
                concurrency, max_in_flight))
    for thread in threads:
        thread.join(timeout=SCENARIO_TIMEOUT_S)
    scenario["durationS"] = _round(time.monotonic() - started, 2)
    scenario["maxInFlight"] = max_in_flight
    scenario["threadsAliveAfterJoin"] = sum(
        1 for thread in threads if thread.is_alive())

    ok = 0
    failed = 0
    request_records = []
    for index, result in enumerate(results):
        record = {"index": index}
        if result is None:
            record.update({"status": None,
                           "error": "worker produced no result"})
        else:
            record.update(result)
        if stream:
            complete = (
                record.get("status") == 200
                and record.get("done") is True
                and record.get("finishReason") == "stop"
                and record.get("contentChars") == answer_chars
                and not record.get("error"))
        else:
            complete = (
                record.get("status") == 200
                and record.get("finishReason") == "stop"
                and record.get("contentChars") == answer_chars
                and not record.get("error"))
        record["complete"] = bool(complete)
        if complete:
            ok += 1
        else:
            failed += 1
            scenario["failures"].append(
                {"index": index, "status": record.get("status"),
                 "error": record.get("error"),
                 "contentChars": record.get("contentChars"),
                 "finishReason": record.get("finishReason"),
                 "done": record.get("done")})
        request_records.append(record)

    scenario["requests"] = request_records
    scenario["ok"] = ok
    scenario["failed"] = failed
    if scenario["threadsAliveAfterJoin"]:
        scenario["failures"].append(
            {"index": None, "error": "%d client threads still alive" %
                                scenario["threadsAliveAfterJoin"]})
    if stream and not inflight_ready:
        scenario["failures"].append(
            {"index": None,
             "error": "64 in-flight overlap was not observed"})

    end_sample = _take_sample(fixture.cgroup_dir, app_pids,
                               fixture.harness_pids())
    if end_sample["app"]["deadProcesses"]:
        scenario["failures"].append(
            {"index": None, "error": "app processes dead: %s" %
                                ", ".join(end_sample["app"]["deadProcesses"])})
    samples = sampler.snapshot()
    scenario["sampleCount"] = len(samples)
    scenario["rss_peak_mb"] = _round(_peak(
        samples, lambda s: s["app"]["rssMb"]))
    scenario["appAnonPeakMb"] = _round(_peak(
        samples, lambda s: s["app"]["anonMb"]))
    scenario["appFilePeakMb"] = _round(_peak(
        samples, lambda s: s["app"]["fileMb"]))
    scenario["threads_peak"] = _peak(
        samples, lambda s: s["app"]["threads"])
    scenario["fds_peak"] = _peak(samples, lambda s: s["app"]["fds"])
    scenario["sockets_peak"] = _peak(samples, lambda s: s["app"]["sockets"])
    scenario["readers_peak"] = _peak(samples, lambda s: s["app"]["readers"])
    scenario["readerThreads_peak"] = _peak(
        samples, lambda s: s["app"]["readerThreads"])
    scenario["upstreamConnections_peak"] = _peak(
        samples, lambda s: s["app"]["upstreamConnections"])
    scenario["clientConnections_peak"] = _peak(
        samples, lambda s: s["app"]["clientConnections"])
    scenario["backendConnections_peak"] = _peak(
        samples, lambda s: s["app"]["backendConnections"])
    scenario["harnessRssPeakMb"] = _round(_peak(
        samples, lambda s: s["harness"]["rssMb"]))
    scenario["cgroupPageCachePeakMb"] = _round(_peak(
        samples, lambda s: s["cgroup"]["fileMb"]))
    scenario["cgroupAnonPeakMb"] = _round(_peak(
        samples, lambda s: s["cgroup"]["anonMb"]))
    scenario["cgroupKernelPeakMb"] = _round(_peak(
        samples, lambda s: s["cgroup"]["kernelMb"]))
    scenario["cgroupSockPeakMb"] = _round(_peak(
        samples, lambda s: s["cgroup"]["sockMb"]))
    scenario["outsideScopeUsedPeakMb"] = _round(_peak(
        samples, lambda s: s["outsideScopeUsedMb"]))
    scenario["peakSample"] = _peak_sample(
        samples, lambda s: s["app"]["rssMb"])
    scenario["inflightSample"] = inflight_sample
    scenario["cgroup_peak_mb"] = _round(_bytes_to_mb(
        _read_int(fixture.cgroup_dir / "memory.peak")))
    scenario["oom"] = _event_delta(start_sample, end_sample, "oom")
    scenario["oom_kill"] = _event_delta(start_sample, end_sample, "oom_kill")
    scenario["maxEvents"] = _event_delta(start_sample, end_sample, "max")
    end_ticks = {}
    for process_name, pid in app_pids.items():
        ticks = _proc_stat_ticks(pid)
        if ticks is not None:
            end_ticks[process_name] = ticks
    scenario["cpu_seconds"] = _round(_cpu_delta(start_ticks, end_ticks))
    scenario["notes"].append(
        "fake UM stream first-chunk delay %.1fs; non-stream delay %.1fs "
        "(verification-only upstream latency, product code unchanged)" % (
            STREAM_FIRST_CHUNK_S, FAKE_UM_NONSTREAM_DELAY_S))
    scenario["notes"].append(
        "readers = backend ESTABLISHED TCP sockets to fake UM:28999; "
        "readerThreads = /proc comm prefix %r (CPython keeps python3, so this "
        "is reported as 0); upstream/client/backend connections are socket "
        "counts by port" % READER_COMM_PREFIX)
    scenario["fallback"] = _settle(fixture, sampler, app_pids, start_sample)
    scenario["failureCount"] = len(scenario["failures"])
    scenario["status"] = "passed" if not scenario["failures"] else "failed"
    return scenario


# --------------------------------------------------------------------------
# group aggregation
# --------------------------------------------------------------------------
_PEAK_KEYS = (
    "rss_peak_mb", "threads_peak", "fds_peak", "sockets_peak",
    "readers_peak", "readerThreads_peak", "upstreamConnections_peak",
    "clientConnections_peak", "backendConnections_peak",
    "cgroup_peak_mb", "appAnonPeakMb", "appFilePeakMb",
    "harnessRssPeakMb", "cgroupPageCachePeakMb", "cgroupAnonPeakMb",
    "cgroupKernelPeakMb", "cgroupSockPeakMb", "outsideScopeUsedPeakMb",
    "displayRssPeakMb",
)


def _update_group(group: dict, scenario: dict) -> None:
    for key in _PEAK_KEYS:
        value = scenario.get(key)
        if value is None:
            continue
        current = group.get(key)
        group[key] = value if current is None else max(current, value)
    for key in ("oom", "oom_kill", "maxEvents"):
        group[key] = (group.get(key) or 0) + (scenario.get(key) or 0)
    group["cpu_seconds"] = _round(
        (group.get("cpu_seconds") or 0.0) + (scenario.get("cpu_seconds") or 0.0))
    for failure in scenario.get("failures") or []:
        item = dict(failure)
        item["scenario"] = scenario["name"]
        group["failures"].append(item)
    display = scenario.get("display") or {}
    if display.get("processes"):
        group["displayProcesses"] = display["processes"]
    group["wslgMounted"] = display.get("wslgMounted")
    group["threads"] = group.get("threads_peak")
    group["fds"] = group.get("fds_peak")
    group["sockets"] = group.get("sockets_peak")
    group["readers"] = group.get("readers_peak")
    group["failureCount"] = len(group["failures"])
    group["breakdown"] = _breakdown(group)


def _breakdown(group: dict) -> dict:
    limit_mb = group["memoryMaxBytes"] / (1024.0 * 1024.0)
    cg_peak = group.get("cgroup_peak_mb")
    app_rss = group.get("rss_peak_mb")
    headroom = None
    if cg_peak is not None:
        headroom = _round(limit_mb - cg_peak)
    share = None
    if app_rss is not None and cg_peak:
        share = _round(app_rss / cg_peak * 100.0)
    return {
        "memoryMaxMb": _round(limit_mb),
        "appGroupRssPeakMb": app_rss,
        "appGroupAnonPeakMb": group.get("appAnonPeakMb"),
        "appGroupFilePeakMb": group.get("appFilePeakMb"),
        "harnessRssPeakMb": group.get("harnessRssPeakMb"),
        "cgroupAnonPeakMb": group.get("cgroupAnonPeakMb"),
        "cgroupPageCachePeakMb": group.get("cgroupPageCachePeakMb"),
        "cgroupKernelPeakMb": group.get("cgroupKernelPeakMb"),
        "cgroupSockPeakMb": group.get("cgroupSockPeakMb"),
        "outsideScopeUsedPeakMb": group.get("outsideScopeUsedPeakMb"),
        "displayRssPeakMb": group.get("displayRssPeakMb"),
        "displayProcesses": group.get("displayProcesses") or [],
        "wslgMounted": group.get("wslgMounted"),
        "cgroupPeakMb": cg_peak,
        "headroomMb": headroom,
        "appGroupShareOfCgroupPeakPct": share,
        "note": "app group = 8100 share + 8110 backend + broker + management; "
                "harness = fake UM + measurement; page cache and kernel are "
                "cgroup counters; outsideScope = host used minus this scope",
    }


def _scope_conclusion(group: dict) -> str:
    failures = group.get("failures") or []
    limit_mb = group["memoryMaxBytes"] / (1024.0 * 1024.0)
    peak = group.get("cgroup_peak_mb")
    oom = (group.get("oom") or 0) + (group.get("oom_kill") or 0)
    if failures:
        return "不达标：%d 个失败；见 failures" % len(failures)
    if peak is None:
        return "证据不完整：没有 cgroup 峰值"
    headroom = limit_mb - peak
    if oom:
        return "不达标：OOM/oom_kill=%d" % oom
    if headroom < 128:
        return "达标但余量不足：峰值 %.0f MiB，余量 %.0f MiB (%.1f%%)" % (
            peak, headroom, headroom / limit_mb * 100.0)
    return "达标（本样本）：峰值 %.0f MiB，余量 %.0f MiB (%.1f%%)" % (
        peak, headroom, headroom / limit_mb * 100.0)


# --------------------------------------------------------------------------
# inner scope run
# --------------------------------------------------------------------------
def _run_inner(args) -> int:
    partial_path = _normalize_path(args.partial)
    cgroup = _cgroup_dir()
    limit_gb = int(args.limit_gb[0] if isinstance(args.limit_gb, list)
                   else args.limit_gb)
    expected_bytes = limit_gb * 1024 ** 3
    memory_max = _read_text(cgroup / "memory.max")
    swap_max = _read_text(cgroup / "memory.swap.max")
    partial = {
        "kind": "um-share-linux-resource-envelope-partial",
        "generatedAt": _now_iso(),
        "limitGb": limit_gb,
        "cgroupPath": str(cgroup),
        "memoryMax": memory_max,
        "memorySwapMax": swap_max,
        "expectedMemoryMaxBytes": expected_bytes,
        "host": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "uname": " ".join(os.uname()),
            "nproc": os.cpu_count(),
            "wsl": "microsoft" in Path("/proc/version").read_text(
                encoding="utf-8", errors="replace").lower(),
        },
        "method": {
            "scope": "systemd-run --scope -p MemoryMax=%sG "
                     "-p MemorySwapMax=0" % limit_gb,
            "component": "fake UM 28999 + backend 28110 + share 28100 "
                         "+ broker AF_UNIX + management 28096",
            "sample": "%d concurrent long requests per scenario; "
                      "answer %d KiB; %d rounds" % (
                          DEFAULT_CONCURRENCY, args.answer_kib, args.rounds),
            "note": "fake UM and this harness are inside the same scope; "
                    "app-group RSS excludes them and they are reported "
                    "separately",
        },
        "group": {
            "limit": "%dG" % limit_gb,
            "memoryMaxBytes": expected_bytes,
            "memorySwapMaxBytes": 0,
            "rss_peak_mb": None,
            "cgroup_peak_mb": None,
            "threads": None,
            "fds": None,
            "sockets": None,
            "readers": None,
            "cpu_seconds": None,
            "oom": 0,
            "oom_kill": 0,
            "maxEvents": 0,
            "failures": [],
            "notes": [],
            "rounds": [],
            "breakdown": {},
            "conclusion": None,
        },
    }

    if memory_max is None:
        partial["group"]["failures"].append(
            "cgroup memory.max is missing; the scope did not apply")
        partial["group"]["conclusion"] = "证据不完整：没有 memory.max"
        _write_json_atomic(partial_path, partial)
        return 2
    if memory_max.isdigit() and int(memory_max) != expected_bytes:
        partial["group"]["failures"].append(
            "memory.max=%s does not match requested %d" % (
                memory_max, expected_bytes))
        partial["group"]["conclusion"] = "证据不完整：MemoryMax 未按请求生效"
        _write_json_atomic(partial_path, partial)
        return 2
    if swap_max is not None and swap_max != "0":
        partial["group"]["notes"].append(
            "memory.swap.max=%s (requested 0); swap can hide RAM pressure" %
            swap_max)
    if swap_max is None:
        partial["group"]["notes"].append(
            "memory.swap.max is absent; swap usage was not bounded")

    fixture = EnvelopeFixture(Path(args.report))
    fixture.cgroup_dir = cgroup
    sampler = None
    group = partial["group"]
    group_failures = group["failures"]
    try:
        fixture.prepare()
        answer_chars = int(args.answer_kib) * 1024
        fixture.long_answer_path = fixture.root / "long-answer.txt"
        fixture.long_answer_path.write_text(
            _long_text(answer_chars), encoding="utf-8")
        fixture.start_fake_um()
        fixture.start_backend()
        fixture.start_share()
        fixture.start_broker()
        fixture.start_management()
        settings = fixture.set_share_max(DEFAULT_CONCURRENCY)
        if settings.get("published") is not True:
            raise RuntimeError("share max 64 was not published")
        app_pids = fixture.app_pids()
        harness_pids = fixture.harness_pids()
        sampler = Sampler(cgroup, app_pids, harness_pids).start()
        group["notes"].append(
            "app group = backend 28110 + share 28100 + broker + "
            "management 28096; harness = fake UM + measurement")
        group["notes"].append(
            "the same MemoryMax scope contains the harness, so the app group "
            "has less headroom than a production host without fake UM/load")

        # The fake UM answers every request with the same 1 MiB non-empty
        # text; the client prompt stays short, so the share gateway's real
        # 1 MiB REQUEST-body limit is not what is being measured.
        prompt = "long content sample"
        body_stream = json.dumps({
            "model": SHARE_MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }, ensure_ascii=False).encode("utf-8")
        body_nonstream = json.dumps({
            "model": SHARE_MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }, ensure_ascii=False).encode("utf-8")

        for round_index in range(1, int(args.rounds) + 1):
            round_record = {
                "round": round_index,
                "baseline": _take_sample(
                    cgroup, app_pids, harness_pids, include_display=True),
                "scenarios": [],
            }
            round_record["baseline"]["cgroupMax"] = memory_max
            group["rounds"].append(round_record)
            _write_json_atomic(partial_path, partial)
            keys = fixture.create_keys_for(f"env-r{round_index}")
            round_record["keyCount"] = len(keys)
            round_record["requestsPerKey"] = DEFAULT_CONCURRENCY // len(keys)

            # -------- 64 in-flight, long streaming answer --------
            group["currentScenario"] = {
                "round": round_index, "name": "long_stream_64_inflight",
                "stream": True, "startedAt": _now_iso()}
            _write_json_atomic(partial_path, partial)
            _fake_reset()
            _fake_mode("slow_first_chunk")
            scenario = _run_scenario(
                fixture, sampler, name="long_stream_64_inflight",
                stream=True, keys=keys, body=body_stream,
                answer_chars=answer_chars,
                concurrency=DEFAULT_CONCURRENCY)
            scenario["fakeUmMode"] = "slow_first_chunk"
            scenario["fakeUmFirstChunkS"] = STREAM_FIRST_CHUNK_S
            round_record["scenarios"].append(scenario)
            _update_group(group, scenario)
            group["currentScenario"] = None
            _write_json_atomic(partial_path, partial)

            # -------- 64 concurrent long non-stream answers --------
            group["currentScenario"] = {
                "round": round_index, "name": "long_nonstream_64",
                "stream": False, "startedAt": _now_iso()}
            _write_json_atomic(partial_path, partial)
            _fake_reset()
            _fake_mode("normal")
            scenario = _run_scenario(
                fixture, sampler, name="long_nonstream_64",
                stream=False, keys=keys, body=body_nonstream,
                answer_chars=answer_chars,
                concurrency=DEFAULT_CONCURRENCY)
            scenario["fakeUmMode"] = "normal"
            scenario["fakeUmNonstreamDelayS"] = FAKE_UM_NONSTREAM_DELAY_S
            round_record["scenarios"].append(scenario)
            _update_group(group, scenario)
            group["currentScenario"] = None
            _write_json_atomic(partial_path, partial)

        group["cgroup_peak_mb"] = group.get("cgroup_peak_mb")
        group["conclusion"] = _scope_conclusion(group)
        _write_json_atomic(partial_path, partial)
        return 0 if not group_failures else 1
    except Exception as exc:  # noqa: BLE001 - recorded, not hidden
        group_failures.append({
            "scenario": "setup/run",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-4000:],
        })
        group["conclusion"] = "运行中断：" + str(exc)
        _write_json_atomic(partial_path, partial)
        return 1
    finally:
        if sampler is not None:
            sampler.stop()
        with contextlib.suppress(Exception):
            fixture.stop_all()
        group["conclusion"] = _scope_conclusion(group)
        _write_json_atomic(partial_path, partial)


# --------------------------------------------------------------------------
# outer scope driver
# --------------------------------------------------------------------------
class ScopeMonitor:
    """Read the transient scope cgroup while the inner command runs."""

    def __init__(self, unit: str, interval: float = 0.2):
        self.unit = unit
        self.interval = interval
        self.path = None
        self.max_current = 0
        self.max_peak = 0
        self.max_pids = 0
        self.max_cpu_usec = 0
        self.events = {}
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="scope-monitor", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _find(self):
        candidate = Path("/sys/fs/cgroup/system.slice") / (
            self.unit + ".scope")
        if candidate.exists():
            return candidate
        try:
            result = subprocess.run(
                ["systemctl", "show", "-p", "ControlGroup", "--value",
                 self.unit],
                capture_output=True, text=True, timeout=3)
            value = result.stdout.strip()
            if value:
                path = Path("/sys/fs/cgroup") / value.lstrip("/")
                if path.exists():
                    return path
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    def _loop(self):
        while not self._stop.wait(self.interval):
            if self.path is None:
                self.path = self._find()
                if self.path is None:
                    continue
            try:
                current = _read_int(self.path / "memory.current") or 0
                peak = _read_int(self.path / "memory.peak") or 0
                pids = _read_int(self.path / "pids.peak") or 0
                cpu = _read_kv(self.path / "cpu.stat").get("usage_usec", 0)
                events = _read_kv(self.path / "memory.events")
                self.max_current = max(self.max_current, current)
                self.max_peak = max(self.max_peak, peak)
                self.max_pids = max(self.max_pids, pids)
                self.max_cpu_usec = max(self.max_cpu_usec, cpu)
                for key, value in events.items():
                    self.events[key] = max(self.events.get(key, 0), value)
                self.samples += 1
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)


def _run_scope_group(limit_gb: int, report_path: Path, args) -> dict:
    unit = "um-envelope-%dg-%d-%d" % (
        limit_gb, os.getpid(), int(time.time()))
    partial = report_path.with_name(
        "resource-envelope-%dg-partial.json" % limit_gb)
    with contextlib.suppress(OSError):
        partial.unlink()
    inner = [
        sys.executable, str(Path(__file__).resolve()),
        "--inside-scope", "--limit-gb", str(limit_gb),
        "--report", str(report_path), "--partial", str(partial),
        "--rounds", str(args.rounds),
        "--answer-kib", str(args.answer_kib),
    ]
    cmd = [
        "systemd-run", "--scope", "--expand-environment=no",
        "--unit=" + unit,
        "-p", "MemoryMax=%dG" % limit_gb,
        "-p", "MemorySwapMax=0",
    ] + inner
    monitor = ScopeMonitor(unit).start()
    started = time.monotonic()
    timed_out = False
    proc = None
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(TESTS), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["systemctl", "stop", unit],
                           capture_output=True, text=True, timeout=30)
            proc.kill()
            stdout, stderr = proc.communicate()
    except FileNotFoundError as exc:
        stderr = "systemd-run unavailable: %s" % exc
    except Exception as exc:  # noqa: BLE001 - recorded, not hidden
        stderr = "%s: %s" % (type(exc).__name__, exc)
    finally:
        duration = time.monotonic() - started
        monitor.stop()
    inner_report = _read_json(partial)
    group = {
        "limit": "%dG" % limit_gb,
        "memoryMaxBytes": limit_gb * 1024 ** 3,
        "memorySwapMaxBytes": 0,
        "scopeUnit": unit,
        "scopeExitCode": proc.returncode if proc is not None else None,
        "scopeDurationS": _round(duration, 1),
        "timedOut": timed_out,
        "scopeStdoutTail": (stdout or "")[-2000:],
        "scopeStderrTail": (stderr or "")[-4000:],
        "rss_peak_mb": None,
        "cgroup_peak_mb": None,
        "threads": None,
        "fds": None,
        "sockets": None,
        "readers": None,
        "cpu_seconds": None,
        "oom": None,
        "oom_kill": None,
        "maxEvents": None,
        "failures": [],
        "notes": [],
        "rounds": [],
        "breakdown": {},
        "conclusion": None,
    }
    if inner_report and isinstance(inner_report.get("group"), dict):
        source = inner_report["group"]
        for key in (
                "rss_peak_mb", "cgroup_peak_mb", "threads", "fds",
                "sockets", "readers", "cpu_seconds", "oom", "oom_kill",
                "maxEvents", "rounds", "breakdown", "conclusion"):
            group[key] = source.get(key)
        group["failures"] = list(source.get("failures") or [])
        group["notes"] = list(source.get("notes") or [])
        group["memoryMaxApplied"] = inner_report.get("memoryMax")
        group["memorySwapMaxApplied"] = inner_report.get("memorySwapMax")
        group["cgroupPath"] = inner_report.get("cgroupPath")
        group["host"] = inner_report.get("host")
        group["method"] = inner_report.get("method")
        group["currentScenarioAtKill"] = source.get("currentScenario")
    else:
        group["failures"].append(
            "inside-scope produced no partial report; the scope may have "
            "been OOM-killed before the first write")
    if monitor.path is not None:
        group["notes"].append(
            "scope cgroup monitored at %s (%d samples)" % (
                monitor.path, monitor.samples))
    if monitor.max_current:
        group["cgroupCurrentObservedMb"] = _round(
            monitor.max_current / (1024.0 * 1024.0))
    if monitor.max_peak:
        group["cgroupMemoryPeakObservedMb"] = _round(
            monitor.max_peak / (1024.0 * 1024.0))
    if monitor.max_pids:
        group["pidsPeakObserved"] = monitor.max_pids
    if monitor.max_cpu_usec:
        group["cpuUsageUsecObserved"] = monitor.max_cpu_usec
    if monitor.events:
        group["scopeMemoryEvents"] = dict(monitor.events)
    if group.get("cgroup_peak_mb") is None:
        group["cgroup_peak_mb"] = group.get("cgroupMemoryPeakObservedMb")
    if timed_out:
        group["failures"].append(
            "scope command timed out after %ss" % args.timeout_s)
    if proc is not None and proc.returncode not in (0, 1):
        group["failures"].append(
            "scope command exited %s (possible OOM kill or crash)" %
            proc.returncode)
    if "oom" in (stderr or "").lower():
        group["notes"].append("scope stderr mentions OOM")
    if not group["conclusion"]:
        group["conclusion"] = _scope_conclusion(group)
    group["failureCount"] = len(group["failures"])
    return group


def _overall_conclusion(groups: list) -> dict:
    by_limit = {group["limit"]: group for group in groups}
    verdicts = {}
    for limit in ("2G", "4G"):
        group = by_limit.get(limit)
        if group is None:
            verdicts[limit] = "未测量"
            continue
        failures = group.get("failures") or []
        oom = (group.get("oom") or 0) + (group.get("oom_kill") or 0)
        if failures:
            verdicts[limit] = "不达标（%d 个失败）" % len(failures)
        elif oom:
            verdicts[limit] = "不达标（OOM/oom_kill=%d）" % oom
        elif group.get("rss_peak_mb") is None:
            verdicts[limit] = "证据不完整（没有应用组 RSS 峰值）"
        else:
            verdicts[limit] = "达标（本样本）"
    g2 = by_limit.get("2G")
    g4 = by_limit.get("4G")
    candidate = None
    if g2 is not None and verdicts.get("2G", "").startswith("不达标"):
        candidate = {
            "status": "TEST ONLY，未执行",
            "candidate": "将验证 harness（fake UM 与 64 并发负载生成）移出受 "
                         "MemoryMax 约束的 scope，只约束 8100/8110/broker/管理"
                         "四个产品进程后重测",
            "why": "2G 失败可能来自同 scope 的假 UM/测量开销；分离后才能判断"
                   "产品组本身是否超限",
            "constraints": "64 并发与 1 MiB 长内容目标不变；不删工具/图像/"
                           "长回答，不改真实结束",
            "fourGbComparison": (
                "4G 组：%s" % verdicts.get("4G")
                if g4 is not None else "4G 组未测量"),
        }
    whole_machine = None
    if g2 is not None:
        scope_peak = g2.get("cgroup_peak_mb") or 0.0
        outside = (g2.get("breakdown") or {}).get(
            "outsideScopeUsedPeakMb") or 0.0
        total = scope_peak + outside
        headroom = 2048.0 - total
        whole_machine = {
            "scopePeakMb": _round(scope_peak),
            "outsideScopeBaselineMb": _round(outside),
            "scopePlusOutsideMb": _round(total),
            "headroomMb": _round(headroom),
            "verdict": "未证明：outside-scope 基线来自 15.5 GB WSL 主机，"
                       "真实 2 GB 云机的 OS/显示/Chromium 基线未测；"
                       "不能据此声称整机 2 GB 合格",
        }
    return {
        "2G": verdicts.get("2G"),
        "4G": verdicts.get("4G"),
        "testOnlyCandidate": candidate,
        "wholeMachine2G": whole_machine,
        "note": "结论只覆盖本样本（fake UM、1 MiB 长内容、64 并发、"
                "无真实 UM 网络、无 Chromium 浏览器、无 Caddy/TLS）；"
                "不据此声称整机或真实云端容量合格。",
    }


def _run_outer(args) -> int:
    report_path = _normalize_path(args.report)
    limits = args.limit_gb or [2, 4]
    groups = []
    for limit_gb in limits:
        groups.append(_run_scope_group(limit_gb, report_path, args))
    report = {
        "kind": "um-share-linux-resource-envelope",
        "generatedAt": _now_iso(),
        "report": str(report_path),
        "method": {
            "scope": "one transient systemd scope per limit: "
                     "systemd-run --scope -p MemoryMax=<N>G "
                     "-p MemorySwapMax=0",
            "component": "fake UM 28999 + backend 28110 + share 28100 "
                         "+ broker AF_UNIX + management 28096",
            "load": "64 concurrent long requests per scenario; "
                    "1 MiB non-empty answer; 2 rounds",
            "measurement": "cgroup v2 memory.current/peak/stat/events, "
                           "per-process /proc RSS/threads/FDs/sockets/reader "
                           "threads, CPU, host /proc/meminfo",
            "harnessInScope": True,
        },
        "groups": groups,
        "conclusion": _overall_conclusion(groups),
        "failures": [],
        "notCovered": [
            "真实 UM（chat.genai.um.edu.mo）网络与生成",
            "Chromium 可见浏览器登录/MFA",
            "Caddy/TLS/公网入口",
            "云端备份与真实 SQLite 数据",
            "长时间稳定性、连接回收与多日运行",
            "2GB 主机上除本 scope 外的 OS/显示/WSLg 实际占用",
        ],
    }
    for group in groups:
        for failure in group.get("failures") or []:
            if isinstance(failure, dict):
                report["failures"].append({
                    "limit": group["limit"],
                    "scenario": failure.get("scenario"),
                    "index": failure.get("index"),
                    "error": failure.get("error"),
                })
            else:
                report["failures"].append({
                    "limit": group["limit"],
                    "error": str(failure),
                })
    _write_json_atomic(report_path, report)
    return 0 if not report["failures"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the Linux resource envelope of the UM share chain")
    parser.add_argument("--limit-gb", action="append", type=int,
                        choices=(2, 4), default=None,
                        help="MemoryMax in GiB; repeat for each limit")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--partial", default=None)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--answer-kib", type=int, default=DEFAULT_ANSWER_KIB)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--inside-scope", action="store_true")
    args = parser.parse_args(argv)
    if args.inside_scope:
        if args.partial is None:
            parser.error("--partial is required with --inside-scope")
        return _run_inner(args)
    if os.name != "posix" or not Path("/proc/self/fd").exists():
        print(json.dumps({
            "ok": False,
            "setupError": "resource envelope requires Linux /proc and cgroup v2",
        }, ensure_ascii=False))
        return 2
    if shutil.which("systemd-run") is None:
        print(json.dumps({
            "ok": False,
            "setupError": "systemd-run is unavailable",
        }, ensure_ascii=False))
        return 2
    return _run_outer(args)


if __name__ == "__main__":
    raise SystemExit(main())
