# -*- coding: utf-8 -*-
"""share_guard.py — Job-enforced single-process supervisor for the UM share.

Runs as the locked-down ``um_share`` account (batch logon via Task Scheduler,
which owns the account secret in LSA). It creates a Job Object whose active
process limit is exactly 1 with kill-on-close, then starts the real share
gateway (``um-share-python.exe share_gateway.py``) SUSPENDED, assigns it to the
job and resumes it. Any child the share attempts to spawn (any image) is
rejected by the Job's active-process limit, which closes the "different image /
subprocess" escape that app-id WFP filters alone cannot address.

The supervisor itself performs no network I/O and its own image is also
um-share-python.exe, so the same WFP app-id scope applies to it.

Usage (run from the run copy as um_share):
  um-share-python.exe share_guard.py --run-dir <writable dir>       --upstream-port <dedicated share-backend port> [--probe] [--port N]

The upstream port is REQUIRED whenever --child is share_gateway.py (the
default child): the share child is only ever pointed at the dedicated share
backend, never at the private chat2api instance.
"""
import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
CREATE_SUSPENDED = 4
CREATE_NO_WINDOW = 134217728
INFINITE = 4294967295
PROCESS_ALL_ACCESS = 2035711
THREAD_ALL_ACCESS = 2032639
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 8
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 8192
JobObjectExtendedLimitInformation = 9
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

class IOCOUNTERS(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_ulonglong), ('WriteOperationCount', ctypes.c_ulonglong), ('OtherOperationCount', ctypes.c_ulonglong), ('ReadTransferCount', ctypes.c_ulonglong), ('WriteTransferCount', ctypes.c_ulonglong), ('OtherTransferCount', ctypes.c_ulonglong)]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IOCOUNTERS), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

class STARTUPINFO(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPSTR), ('lpDesktop', wintypes.LPSTR), ('lpTitle', wintypes.LPSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD), ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD), ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD), ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD), ('lpReserved2', ctypes.POINTER(ctypes.c_byte)), ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE), ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def write_status(run_dir: Path, state: dict) -> None:
    (run_dir / 'guard-status.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

def read_deploy_mode(here: Path, mode_file: Path=None) -> str:
    """R35-2 pass-through: the validated deployment mode of THIS deployment
    (the same contract as select_config / Prep's Get-DeployMode). The guard
    forwards it to the share child (--auth-mode) so the instance FIXES its
    mode at start - the file never decides the format by itself."""
    mode_file = mode_file or Path(here) / 'deploy-mode.json'
    if not mode_file.exists():
        raise SystemExit(f'deployment mode file missing: {mode_file} - create it explicitly (mode=legacy before migration); mode is never inferred')
    try:
        mode = json.loads(mode_file.read_text(encoding='utf-8')).get('mode')
    except (OSError, ValueError) as exc:
        raise SystemExit(f'deployment mode file corrupt: {exc}') from exc
    if mode not in ('projection', 'legacy'):
        raise SystemExit(f"deployment mode must be 'projection' or 'legacy', got '{mode}'")
    return mode

def select_config(here: Path, mode_file: Path=None) -> Path:
    """D3/R31-4: the deployment-mode gate shared with Prep/Start (same
    contract as deploy-elevated.ps1 Get-DeployMode). The guard's launch
    config follows the EXPLICIT mode - never the mere presence of a file:

    * projection mode: the SQLite-derived projection
      (um-share-projection.json) MUST exist and must FULLY parse with the
      ONE shared parser (R35-2 - a bool schema, an invalid entry, a missing
      generation, a legacy clients file or a mixed object is refused); the
      legacy digest file is NOT read.
    * legacy mode: the explicitly not-yet-migrated digest config
      (um-share.json).
    * missing/corrupt mode file, or a projection-mode deployment whose
      projection file is missing: REFUSE to start (a lost projection must
      never silently re-introduce the old authorizations).
    """
    mode_file = mode_file or Path(here) / 'deploy-mode.json'
    mode = read_deploy_mode(here, mode_file)
    if mode == 'projection':
        projection = Path(here) / 'um-share-projection.json'
        if not projection.exists():
            raise SystemExit('projection-mode deployment but the derived projection is missing; refusing to start from the legacy digest file')
        try:
            from share_broker_protocol import ProtocolError, parse_projection_payload
        except ImportError as exc:
            raise SystemExit(f'shared projection parser unavailable: {exc}; refusing to start') from exc
        try:
            payload = json.loads(projection.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise SystemExit(f'projection-mode file is corrupt: {exc}') from exc
        try:
            parse_projection_payload(payload)
        except ProtocolError as exc:
            raise SystemExit(f'projection-mode file is not a valid derived projection: {exc}') from exc
        return projection
    if mode == 'legacy':
        return here / 'um-share.json'
    raise SystemExit(f"deployment mode must be 'projection' or 'legacy', got '{mode}'")

def share_child_argv(env_args, upstream_port):
    """R6-A1 (S3-R6A3 R-S3-4) + R13-3: the share child's ambient argv with
    the guard's EXPLICIT upstream port as the ONLY surviving value.

    Pure function (no OS access). The explicit guard argument is appended
    UNCONDITIONALLY and any ambient ``UM_SHARE_ARGS`` occurrence of the same
    option is stripped first, so "explicit beats environment" is a fact in every
    branch - and there is no default: ``upstream_port`` must be a real port, so a
    share child can never be launched with a silent private-8000 upstream.
    """
    if upstream_port is None:
        raise ValueError('share child upstream port is required (no default)')
    port = int(upstream_port)
    if port < 1 or port > 65535:
        raise ValueError(f'share child upstream port out of range: {port}')
    cleaned = []
    skip_value = False
    for token in (env_args or '').strip().split():
        if skip_value:
            skip_value = False
            continue
        if token == '--upstream-port':
            skip_value = True
            continue
        if token.startswith('--upstream-port='):
            continue
        cleaned.append(token)
    return (' '.join(cleaned) + ' --upstream-port %d' % port).strip()
FAULT_LOG_NAME = 'share-faults.jsonl'

def share_child_cmdline(exe, child, config, port, auth_mode, argv='', probe=False, fault_log=None):
    """R35-2: the share child's cmdline carries the EXPLICIT deployment mode
    (--auth-mode) so the instance fixes it at start - the file content never
    re-decides the format after a runtime swap. A9-6/§8.3: a non-None
    ``fault_log`` is forwarded as ``--fault-log "<path>"`` so the child's
    bounded fault evidence lands where the broker reads it; None keeps the
    option off the cmdline entirely. Pure function so the cmdline contract is
    unit-testable without CreateProcessW."""
    cmdline = f'"{exe}" "{child}" --config "{config}" --port {port} --auth-mode {auth_mode}'
    argv = (argv or '').strip()
    if argv:
        cmdline += f' {argv}'
    if fault_log is not None:
        cmdline += f' --fault-log "{fault_log}"'
    if probe:
        cmdline += ' --probe'
    return cmdline
PRODUCTION_PIPE_PREFIX = '\\\\.\\pipe\\um-share-public-instance-'
EXPERIMENT_PIPE_PREFIX = '\\\\.\\pipe\\um-share-public-instance-exp-'
EXPERIMENT_PORT = 9100

def build_experiment_share_args(instance_key_file, registration_file, instance_pipe_sddl, pipe_prefix=EXPERIMENT_PIPE_PREFIX):
    """D4-P: the CONTROLLED extra argv for the experiment share child.

    Pure function (no OS access). The experiment contract is fixed:

    * the instance pipe prefix MUST start with the experiment prefix
      (``um-share-public-instance-exp-``) - the production prefix and any other
      namespace refuse, so the share-side instance pipe can never collide
      with or fall back to the production one;
    * the instance pipe SDDL is REQUIRED and non-empty - the share side
      never inherits a default security descriptor, and the management
      pipe's SDDL is a DIFFERENT ACL (two pipes, two DACLs);
    * both file paths are required (an experiment share without an
      instance channel is a not-ready state, never a silent downgrade).

    Returns the UM_SHARE_ARGS payload the guard appends to the share child
    cmdline. Raises ValueError on any contract violation.
    """
    if not isinstance(pipe_prefix, str) or not pipe_prefix.startswith(EXPERIMENT_PIPE_PREFIX):
        raise ValueError(f'experiment instance pipe prefix must start with {EXPERIMENT_PIPE_PREFIX!r} (got {pipe_prefix!r})')
    for name, value in (('instance_key_file', instance_key_file), ('registration_file', registration_file)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'experiment {name} is required')
    if not isinstance(instance_pipe_sddl, str) or not instance_pipe_sddl.strip():
        raise ValueError('experiment instance pipe SDDL is required (never a default security descriptor)')
    return f'--instance-key-file "{instance_key_file}" --instance-pipe-prefix "{pipe_prefix}" --registration-file "{registration_file}" --instance-pipe-sddl "{instance_pipe_sddl}"'

def validate_experiment_guard_invocation(run_dir, port, probe, auth_mode='projection', upstream_port=None):
    """D4-P: validate the guard invocation contract for an experiment run.

    Pure function. The experiment launch is PINNED: probe mode, the fixed
    experiment port 9100, projection deployment mode (the instance must fix
    its format from the derived projection, never the legacy digest), a run
    directory that is NOT the production live run path shape, and (R13-3) the
    upstream port pinned to the experiment port - the experiment share has no
    upstream backend, so a dial would be a self-loop that fails visibly and can
    NEVER reach the production share backend or the private 8000. Returns the
    normalized (run_dir, port, probe, auth_mode) tuple; raises ValueError on
    violation.
    """
    if not isinstance(run_dir, str) or not run_dir.strip():
        raise ValueError('experiment guard --run-dir is required')
    if int(port) != EXPERIMENT_PORT:
        raise ValueError(f'experiment share port is fixed at {EXPERIMENT_PORT} (got {port})')
    if not probe:
        raise ValueError('experiment share launches in --probe mode only')
    if auth_mode != 'projection':
        raise ValueError(f"experiment deployment mode is fixed at 'projection' (got {auth_mode!r})")
    if upstream_port is None:
        raise ValueError(f'experiment share upstream port is required and fixed at {EXPERIMENT_PORT} (got None: an unpinned upstream port could reach the production share backend or the private chat2api)')
    if int(upstream_port) != EXPERIMENT_PORT:
        raise ValueError(f'experiment share upstream port is fixed at {EXPERIMENT_PORT} (got {upstream_port}: never the production share backend, never the private 8000)')
    return (run_dir, int(port), True, auth_mode)

def load_experiment_launch_contract(config_text, here):
    """D4-P (R62-D guard wiring): derive the pinned launch contract from the
    experiment config TEXT.

    Pure function (no OS access). The instance-channel contract is
    re-derived from the config's instance pipe SDDL and the guard's OWN
    directory (the same fixed paths the capture wrapper embeds); the
    UM_SHARE_ARGS environment value must equal the derived string EXACTLY,
    so a tampered environment can never redirect the instance pipe or the
    registration target. Returns the expected UM_SHARE_ARGS string; raises
    SystemExit on any violation (corrupt config, non-dict config,
    empty/placeholder SDDL, env mismatch).
    """
    try:
        exp_cfg = json.loads(config_text)
    except ValueError as exc:
        raise SystemExit(f'experiment config is corrupt: {exc}') from exc
    if not isinstance(exp_cfg, dict):
        raise SystemExit('experiment config must be an object')
    sddl = str(exp_cfg.get('instance_pipe_sddl', ''))
    if not sddl.strip() or 'placeholder' in sddl.lower() or 'todo' in sddl.lower() or ('s-1-5-18-exp' in sddl.lower()):
        raise SystemExit('experiment config instance pipe SDDL is required and must be calibrated (never a placeholder or a default descriptor)')
    return build_experiment_share_args(str(Path(here) / 'instance-channel-key'), str(Path(here) / 'run' / 'live' / 'instance-registration.json'), sddl)

def main() -> int:
    parser = argparse.ArgumentParser(description='upstream share job supervisor')
    parser.add_argument('--run-dir', required=True, help='writable status dir')
    parser.add_argument('--port', type=int, default=8100)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--child', default='share_gateway.py', help='script to run inside the job (default: the share gateway)')
    parser.add_argument('--child-args', default='', help='raw arguments appended after --child for test children')
    parser.add_argument('--upstream-port', type=int, default=None, help='dedicated share-backend port forwarded to share_gateway.py (R6-A1/R13-3); REQUIRED when --child is share_gateway.py - there is no default, so the share can never silently forward to the private chat2api 8000')
    parser.add_argument('--experiment-config', default='', help='D4 experiment: the fixed-structure experiment config file (enables the pinned launch contract; validated BEFORE any directory write, Job or process creation)')
    args = parser.parse_args()
    if args.child == 'share_gateway.py' and args.upstream_port is None:
        parser.error('--upstream-port is required when --child is share_gateway.py: pass the dedicated share-backend port explicitly (production: 8110, from deploy-elevated.ps1 ShareBackendPort). There is no default - the private chat2api port 8000 is never used implicitly.')
    run_dir = Path(args.run_dir)
    if args.experiment_config:
        config_path = Path(args.experiment_config)
        if not config_path.exists():
            raise SystemExit(f'experiment config missing: {config_path}')
        try:
            config_text = config_path.read_text(encoding='utf-8')
        except OSError as exc:
            raise SystemExit(f'experiment config unreadable: {exc}') from exc
        here_for_contract = Path(__file__).resolve().parent
        expected_args = load_experiment_launch_contract(config_text, here_for_contract)
        env_args = (os.environ.get('UM_SHARE_ARGS') or '').strip()
        if env_args != expected_args:
            raise SystemExit('experiment UM_SHARE_ARGS does not equal the config-derived launch contract; the guard refuses to start')
        auth_mode = read_deploy_mode(here_for_contract)
        validate_experiment_guard_invocation(str(run_dir), args.port, args.probe, auth_mode=auth_mode, upstream_port=args.upstream_port)
    run_dir.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    exe = here / 'um-share-python.exe'
    child = here / args.child
    gateway = here / 'share_gateway.py'
    config = select_config(here)
    if not (exe.exists() and child.exists()):
        write_status(run_dir, {'guard': 'err', 'detail': 'run copy incomplete'})
        return 2
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        write_status(run_dir, {'guard': 'err', 'detail': 'CreateJobObject failed'})
        return 2
    ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ext.BasicLimitInformation.ActiveProcessLimit = 1
    ok = kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(ext), ctypes.sizeof(ext))
    if not ok:
        write_status(run_dir, {'guard': 'err', 'detail': 'SetInformationJobObject failed'})
        return 2
    argv = (os.environ.get('UM_SHARE_ARGS') or '').strip()
    if args.child == 'share_gateway.py':
        auth_mode = read_deploy_mode(here)
        argv = share_child_argv(argv, args.upstream_port)
        cmdline = share_child_cmdline(exe, child, config, args.port, auth_mode, argv, args.probe, fault_log=str(run_dir / FAULT_LOG_NAME))
        mode = 'probe' if args.probe else 'real'
    else:
        cmdline = f'"{exe}" "{child}"'
        if args.child_args:
            cmdline += ' ' + args.child_args
        mode = 'test:' + args.child
    si = STARTUPINFO()
    si.cb = ctypes.sizeof(STARTUPINFO)
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(None, cmdline, None, None, False, CREATE_SUSPENDED | CREATE_NO_WINDOW, None, str(here), ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        err = ctypes.get_last_error()
        write_status(run_dir, {'guard': 'err', 'detail': f'CreateProcessW {err}'})
        return 2
    assigned = kernel32.AssignProcessToJobObject(job, pi.hProcess)
    if not assigned:
        err = ctypes.get_last_error()
        kernel32.TerminateProcess(pi.hProcess, 1)
        write_status(run_dir, {'guard': 'err', 'detail': f'AssignProcessToJobObject {err}'})
        return 2
    kernel32.ResumeThread(pi.hThread)
    status = {'guard': 'ok', 'pid': int(pi.dwProcessId), 'port': args.port, 'mode': mode, 'child': args.child, 'started': int(time.time())}
    if args.child == 'share_gateway.py':
        status['upstream_port'] = int(args.upstream_port)
    write_status(run_dir, status)
    kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(job)
    return int(code.value)
if __name__ == '__main__':
    raise SystemExit(main())
