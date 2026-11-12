#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""escape_probe.py — verifies the Job active-process limit closes the
"spawn a different image / subprocess to bypass WFP" path.

Run TWICE:
  1. bare as um_share (CONTROL): child processes spawn normally => the probe
     itself is able to create processes (proves the test is meaningful).
  2. under share_guard.py (i.e. already inside the 1-active-process Job):
     every child spawn must FAIL (CreateProcess rejected by the Job).

It tries several alternative images: cmd.exe, the original python.exe and the
um-share-python.exe image itself. Results go to the run/ dir and stdout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

RUN_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\ProgramData\UMShareRun\run")

CANDIDATES = [
    ("cmd", "cmd.exe", ["/c", "echo exposed"]),
    ("python-original", r"C:\Users\dacint\AppData\Local\Programs\Python\Python314\python.exe",
     ["-c", "print('exposed')"]),
    # override allows the isolated/local test to point at a scratch copy of the
    # dedicated image instead of the production C:\ProgramData copy
    ("python-run-copy",
     os.environ.get("UM_SHARE_PYTHON_PATH",
                    r"C:\ProgramData\UMShareRun\um-share-python.exe"),
     ["-c", "print('exposed')"]),
]


def spawn(label: str, exe: str, args: list) -> dict:
    entry = {"label": label, "exe": exe}
    try:
        # CREATE_NO_WINDOW so a successful spawn never flashes a console.
        proc = subprocess.run([exe] + args, capture_output=True, text=True,
                              timeout=10, creationflags=0x08000000)
        entry["spawned"] = True
        entry["rc"] = proc.returncode
        entry["out"] = (proc.stdout or proc.stderr)[:40]
    except subprocess.TimeoutExpired:
        entry["spawned"] = True
        entry["rc"] = "timeout"
    except OSError as exc:
        entry["spawned"] = False
        entry["error"] = f"{type(exc).__name__}: {exc.winerror}"
    except Exception as exc:  # noqa: BLE001
        entry["spawned"] = False
        entry["error"] = type(exc).__name__
    return entry


results = [spawn(label, exe, args) for label, exe, args in CANDIDATES]
summary = {
    "identity": os.getenv("USERNAME"),
    "under_job": bool(os.getenv("UM_SHARE_UNDER_JOB")),
    "results": results,
}
RUN_DIR.mkdir(parents=True, exist_ok=True)
(RUN_DIR / "escape-results.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
for r in results:
    print(f"escape[{r['label']}] spawned={r['spawned']} "
          f"rc={r.get('rc')} err={r.get('error')}")
