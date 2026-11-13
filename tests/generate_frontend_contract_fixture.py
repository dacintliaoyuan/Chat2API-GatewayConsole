"""Generate synthetic frontend fixtures through real backend handlers.

All state, client files, prices, keys and logs are private fixtures. No live
gateway, client configuration, credentials or user ledger is accessed.

Rev9 A9-8 adds the ``share_maintenance`` segment: the four new console
actions are driven through the REAL registry and the REAL OperationManager with
every privileged/OS/production seam injected as a private fixture (broker, TS
service, connectivity, funnel, backend gate, maintenance lease, gateway restart,
backend reauth, intent writer). Nothing here starts a process, opens a pipe, reads a
real PID/token/ledger, triggers SSO or issues a model request.
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[1])]
from test_console_model_actions import temp_sync_env, _aligned_store, _Ctx
from test_console_api import usagesrv
from console import adapters as console_adapters
from console import log_reader as console_log_reader
from console import model_actions as actions
from console import share_actions as console_share_actions
from console import share_production as console_share_production
import share.backend_supervision as supervision
from console.operations import (OperationCancelled, OperationError,
                               OperationManager, ResourceFileLock)
from model_sync import sync as sync
import share.metering as share_metering
from model_sync.sync_core import CatalogModel

DESTINATION = Path(__file__).parent / "fixtures" / "frontend_contract_rev25.json"


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def generate():
    # model_sync.collect_catalogs only ever reads ACTIVE_SOURCES ("um") and
    # rejects every retired source, so the private catalog input must not inject
    # a retired source either (it would make the published catalog disagree
    # with its own ``sources`` list).
    catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072,
        efforts=("low", "high"), default_effort="low", efforts_source="upstream")]}
    state = sync.empty_state()
    state["contextProbes"] = {"um|fixture-model": {
        "source": "um", "model_id": "fixture-model", "declared_context": 131072,
        "status": "verified", "observed_floor": 16000, "token_evidence": 4096,
        "evidence_revision": 1, "scheme_revision": sync.PROBE_SCHEME_REVISION,
        "history": [{"evidence_revision": 0, "status": "verified", "observed_floor": 8000,
                     "token_evidence": None, "reason": "retest", "archived_at": "2026-10-01T12:00:00+00:00"}],
        "last_success": "2026-10-02T11:00:00+00:00"}}
    with temp_sync_env(state_text=json.dumps(state)) as paths, \
         mock.patch.object(sync, "collect_catalogs", side_effect=lambda sources=None:
             ({key: value for key, value in catalogs.items()
               if sources is None or key in sources}, [])), \
         mock.patch.object(sync, "zcode_running", return_value=False), \
         mock.patch.object(sync, "endpoint_credential", return_value=("", None, False)), \
         mock.patch.object(sync, "datetime", FixedDatetime), \
         mock.patch.object(actions.time, "time", return_value=1790942400), \
         mock.patch.object(share_metering, "datetime", FixedDatetime), \
         contextlib.redirect_stdout(io.StringIO()):
        paths["ZCODE_PROVIDER_CONFIG"].write_text(
            json.dumps(_aligned_store(catalogs)), encoding="utf-8")
        save = actions.model_settings_save("", {"source": "um", "model_id": "fixture-model",
            "alias": "Demo 模型", "contextSource": "measured", "contextTokens": 4096,
            "defaultEffort": "high", "expected_draft_version": 0}, _Ctx())
        snapshot = sync.load_state()
        consumers, _, _ = sync.collect_consumers(selected_consumers=["DSH", "ZCode"])
        contexts = {"overrides": sync.context_override_surface(snapshot, catalogs, consumers),
                    "note": "Synthetic private-client snapshot"}
        selection = {"um|fixture-model": save["draft_version"]}
        preview = actions.model_settings_preview("", {"selection": selection}, _Ctx())
        applied = actions.model_settings_apply("", dict(selection=selection,
            preview_token=preview["preview_token"]), _Ctx())
        consumers, _, _ = sync.collect_consumers(selected_consumers=["DSH", "ZCode"])
        client_readback = {"overrides": sync.context_override_surface(sync.load_state(), catalogs, consumers)}
        model_catalog = actions.model_catalog("", {}, _Ctx())
        # R7（2026-10-10）：显式 sources 只读该来源——夹具固定「单来源」形状，
        # 与「全部模型」聚合入口（上面的 model_catalog）并列。
        scoped_catalog = actions.model_catalog("", {"sources": json.dumps(["um"])}, _Ctx())
        probe_preview = actions.model_context_probe_preview("", {}, _Ctx())
        unavailable_readback = {"overrides": sync.context_override_surface(sync.load_state(), catalogs, None)}
        deferred_save = actions.model_settings_save("", {"source": "um", "model_id": "fixture-model",
            "alias": "Demo 待同步", "expected_draft_version": 1}, _Ctx())
        deferred_selection = {"um|fixture-model": deferred_save["draft_version"]}
        deferred_preview = actions.model_settings_preview("", {"selection": deferred_selection}, _Ctx())
        # R27-HOT：运行中会真的调用安装版兼容准入；夹具场景固定为
        # 「未准入」的延期形态（真实准入见 tests/test_zcode/hot_sync.py）。
        with mock.patch.object(sync, "zcode_running", return_value=True), \
             mock.patch.object(sync, "zcode_hot_sync_admission",
                               return_value=(False, "Demo 夹具：安装版未通过取证准入")):
            deferred_apply = actions.model_settings_apply("", dict(selection=deferred_selection,
                preview_token=deferred_preview["preview_token"]), _Ctx())
        conflict_preview = actions.model_settings_preview("", {"selection": deferred_selection}, _Ctx())
        client_config = json.loads(paths["ZCODE_JSON"].read_text("utf-8"))
        client_config["synthetic_external_edit"] = True
        paths["ZCODE_JSON"].write_text(json.dumps(client_config), encoding="utf-8")
        try:
            actions.model_settings_apply("", dict(selection=deferred_selection,
                preview_token=conflict_preview["preview_token"]), _Ctx())
        except OperationError as exc:
            conflict = {"code": exc.code, "message": str(exc)}
        else:
            raise AssertionError("changed private client snapshot did not conflict")
        # §11.3 组合窗口（2026-10-10）：一次预览把草稿版本与目录差异绑在同一
        # preview_token；确认后同一受控发布轮同时消费草稿与目录变化，结果返回
        # 实际应用/延期/冲突/读回。夹具固定该形状（草稿版本、token、读回）。
        combined_save = actions.model_settings_save("", {"source": "um", "model_id": "fixture-model",
            "alias": "Demo 组合", "expected_draft_version": 2}, _Ctx())
        combined_selection = {"um|fixture-model": combined_save["draft_version"]}
        combined_preview = actions.model_sync_preview("", {"scope_mode": "all",
            "selection": combined_selection}, _Ctx())
        combined_apply = actions.model_sync_apply("", {"scope_mode": "all",
            "selection": combined_selection,
            "preview_token": combined_preview["preview_token"]}, _Ctx())
        ledger = paths["DATA_DIR"] / "usage-share-backend.jsonl"
        records = [
            {"time": "2026-10-02T12:00:00+08:00", "model": "fixture-model",
             "key_id": "fixture-key", "attribution": "verified", "status": "ok",
             "input_tokens": 100, "output_tokens": 10, "usage_source": "upstream"},
            {"time": "2026-10-02T12:01:00+08:00", "model": "fixture-model",
             "key_id": "fixture-key", "attribution": "verified", "status": "interrupted",
             "input_tokens": 20, "output_tokens": None, "usage_source": "partial"},
            {"time": "2026-10-02T12:02:00+08:00", "model": "fixture-model",
             "key_id": "fixture-key", "attribution": "verified", "status": "ok",
             "input_tokens": 25, "output_tokens": 5, "usage_source": "estimated", "estimated": True},
            {"time": "2026-10-02T12:03:00+08:00", "model": "unknown-priced-model",
             "key_id": "fixture-key", "attribution": "verified", "status": "ok",
             "input_tokens": 0, "output_tokens": 0, "usage_source": "upstream"},
            {"time": None, "model": "unknown-model", "key_id": "deleted-fixture-key",
             "attribution": "verified", "usage_source": "none"},
            {"time": "2026-10-02T12:04:00+08:00", "model": "fixture-model", "usage_source": "none"},
        ]
        ledger.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        # Six-tuple pricer (contract §3/§4): the sixth element is the record's
        # own exchange rate. A five-tuple here would make every CNY field unknown
        # and silently drop the rate from the published fixture.
        price = lambda model, at, inp, out, cache, reason: (
            ((inp + out) / 1000000 if model == "fixture-model" else 0),
            model == "fixture-model", model, False, "fixture-price-version", 7.2)
        # 读面（usagesrv._share_usage_project_key_names）现走
        # share_store.read_key_labels 的只读缝：替身必须落在这个新缝上。
        # 旧替身 mock get_share_manager 已不再被读取路径调用；若沿用，
        # 重生成会静默落回生产 data/share-store.db 并丢掉夹具名称。
        fixture_labels = {
            "fixture-key": {"key_id": "fixture-key", "name": "Demo 名称",
                            "deleted_at": None},
            "deleted-fixture-key": {"key_id": "deleted-fixture-key",
                                    "name": "Demo 已删除",
                                    "deleted_at": "2026-10-01"}}

        def fixture_read_key_labels(path, key_ids):
            wanted = set(key_ids)
            return {key_id: row for key_id, row in fixture_labels.items()
                    if key_id in wanted}

        handler = object.__new__(usagesrv.Handler)
        # The read face parses the whitelisted filters from the request line;
        # a synthetic path without a query keeps the fixture unfiltered (complete
        # history) and never touches a real request.
        handler.path = "/api/console/share-usage"
        with mock.patch.object(usagesrv, "share_usage_ledger_path", return_value=str(ledger)), \
             mock.patch.object(usagesrv, "share_usage_reader", return_value=(share_metering.read_share_usage, None)), \
             mock.patch.object(usagesrv, "estimate_record_with_version_and_rate", side_effect=price), \
             mock.patch.object(usagesrv, "read_key_labels", side_effect=fixture_read_key_labels):
            status, share_usage = handler._share_usage_response()
            assert status == 200
        log = paths["DATA_DIR"] / "runtime.log"
        log.write_text("2026-10-02 12:00:00,000 INFO request summary request=fixture-request final=HTTP400 physical_attempts=1/1 duration_ms=1000\n"
                       "2026-10-02 12:01:00,000 INFO stream end model=fixture-model status=ok duration_ms=1000 token_rate=0\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"CHAT2API_SERVICE_LOG": str(log)}):
            logs = console_log_reader.read_entries("chat2api", "service")
        # Exercise the actual aggregate reader with a non-first category and
        # an unread target. Every file is inside the private temp fixture tree.
        warning_log = paths["DATA_DIR"] / "warning.log"
        warning_log.write_text("2026-10-02 12:02:00,000 WARNING upstream retry request=fixture-warning\n", encoding="utf-8")
        registry = {"sources": [
            {"source":"chat2api", "display":"chat2api 演示网关", "categories":[
                {"category":"service", "display":"运行事件", "path":str(log)},
                {"category":"errors", "display":"警告记录", "path":str(warning_log)}]},
            {"source":"console", "display":"控制台演示服务", "categories":[
                {"category":"service", "display":"运行事件", "path":None}]}]}
        with mock.patch.object(console_log_reader, "build_registry", return_value=registry):
            log_overview = console_log_reader.read_overview()
        log_sources = {"sources":[{**source, "categories":[
            {k:v for k,v in category.items() if k != "path"}
            for category in source["categories"]]} for source in registry["sources"]]}
        for target in log_overview["targets"]:
            if target.get("file_key") is not None:
                target["file_key"] = "fixture-runtime-file-v1"
        # Filesystem identity is transport metadata, not fixture content.
        logs["file_key"] = "fixture-runtime-file-v1"
        specs = console_adapters.build_registry()
        capabilities = {"actions": OperationManager.action_info(
            SimpleNamespace(_specs={s.name: s for s in specs}))}
        share_maintenance = generate_share_maintenance(specs)
        root = Path(__file__).parents[1]
        names = ["model_sync/sync.py", "model_sync/sync_core.py", "console/model_actions.py", "console/adapters.py",
                 "console/operations.py", "share/metering.py", "console/stats.py",
                 "console/log_reader.py", "usagesrv.py",
                 # Rev9 A9-8: the new published shapes are generated from
                 # these handlers too (contract §10.2).
                 "console/share_actions.py", "console/share_production.py",
                 # Rev15: the narrow gateway-connection read surface lives in
                 # this module (the usagesrv.py route only dispatches to it), so
                 # a change to the published shape moves this hash too.
                 "console/gateway_connection.py"]
        # The supervision decision/state records come from the real
        # share_backend_supervision.run_once handler, but contract §10.2 does not
        # list that module for sourceHashes: hashing it would make the fixture
        # stale on every supervision change without freezing any published
        # frontend shape (the segment content is checked by check-generated.mjs).
        return {"metadata": {"fixture": True,
            "generatedFrom": "generate_frontend_contract_fixture.py:real handlers/private fixtures",
            "sourceHashEncoding": "utf8-lf",
            "sourceHashes": {n: hashlib.sha256((root / n).read_text(encoding="utf-8")
                .replace("\r\n", "\n").encode("utf-8")).hexdigest() for n in names}},
            "actions": capabilities, "catalog": model_catalog,
            "scoped_catalog": scoped_catalog,
            "context_overrides": contexts,
            "context_probe_preview": probe_preview, "settings_save": save,
            "settings_preview": preview, "settings_apply": applied,
            "client_readback": client_readback,
            "client_readback_unavailable": unavailable_readback,
            "settings_deferred_preview": deferred_preview, "settings_deferred_apply": deferred_apply,
            "settings_conflict_error": conflict,
            # §11.3 组合窗口（2026-10-10）：组合预览/发布的形状（草稿版本 +
            # 目录差异绑同一 token；确认后同一受控发布轮）。
            "sync_combined_preview": combined_preview,
            "sync_combined_apply": combined_apply,
            "share_usage": share_usage, "log_entries": logs,
            "log_sources": log_sources, "log_overview": log_overview,
            "share_maintenance": share_maintenance}


# ===========================================================================
# Rev9 A9-8: the share_maintenance segment (contract §10.2)
# ===========================================================================
# Every value below is synthetic Demo data. The four console actions are driven
# through the REAL registry and the REAL OperationManager; every privileged / OS /
# production seam is injected as a private fixture (broker, TS service,
# connectivity, funnel, backend gate, maintenance lease, gateway restart, backend
# reauth, intent writer). Nothing here starts a process, opens a pipe, reads a real
# PID/token/ledger, triggers SSO or issues a model request.

_SHARE_FIXTURE_EPOCH = 1790942400.0   # 2026-10-02T12:00:00Z
_SHARE_LEASE_ID = "a" * 32             # synthetic 32-hex lease id (never real)
_SHARE_INSTANCE_ID = "fixture-instance-1"


class _ShareFixtureClock:
    """Deterministic synthetic clock (private fixture).

    Wall and monotonic counters advance by a fixed step; the two are separate so
    a lock-polling loop (monotonic only) can never shift the published wall
    timestamps."""

    def __init__(self, start=_SHARE_FIXTURE_EPOCH, step=0.25):
        self._wall = float(start)
        self._monotonic = float(start)
        self._step = float(step)

    def time(self):
        self._wall += self._step
        return self._wall

    def monotonic(self):
        self._monotonic += self._step
        return self._monotonic


def _share_fixture_seq(values):
    """Scalar or ordered list -> generator whose FINAL value repeats (the same
    shape the share action tests use); exception instances are raised."""
    if not isinstance(values, (list, tuple)):
        values = [values]
    remaining = list(values)
    while True:
        if len(remaining) > 1:
            yield remaining.pop(0)
        else:
            yield remaining[0]


# ----------------------------------------------------- private fixture seams

class _FixtureBroker:
    """Private fixture restricted-broker gate: programmable ``verify_running``
    and ``host-evidence`` replies; every call is recorded."""

    def __init__(self, *, verify_running=True, verify_error=None,
                 limits=None, faults=None, watchdog=None):
        self.calls = []
        self._verify_running = verify_running
        self._verify_error = verify_error
        self._limits = dict(limits) if limits is not None else \
            _fixture_limits(now=_SHARE_FIXTURE_EPOCH)
        self._faults = dict(faults) if faults is not None else \
            _fixture_faults(now=_SHARE_FIXTURE_EPOCH)
        self._watchdog = dict(watchdog) if watchdog is not None else {
            "check": "watchdog", "detail": "ok",
            "checked_at": _SHARE_FIXTURE_EPOCH,
            "verdict": {"task_name": "umshare-watchdog", "exists": True,
                "task_state": "Running", "execute_matches": True,
                "execute": "<fixture-pwsh.exe>",
                "script_path": "<fixture-watchdog-umshare.ps1>",
                "script_matches": True, "last_task_result": 0x800710e0,
                "last_task_result_hex": "0x800710E0",
                "last_run_at": _SHARE_FIXTURE_EPOCH - 30,
                "queried_at": _SHARE_FIXTURE_EPOCH,
                "heartbeat_at": _SHARE_FIXTURE_EPOCH - 1,
                "heartbeat_source": "watchdog_log_mtime"}}

    def verify_running(self):
        self.calls.append("verify_running")
        if self._verify_error is not None:
            raise self._verify_error
        return self._verify_running

    def host_evidence(self, check):
        self.calls.append(("host_evidence", check))
        return dict({"limits": self._limits, "faults": self._faults,
                     "watchdog": self._watchdog}[check])


class _FixtureObserve:
    """Private fixture dedicated-backend observation seam; records the read-only
    ``forget_stale`` switch (diagnostics passes False — never deletes a PID)."""

    def __init__(self, obs):
        self.calls = []
        self.forget_stale = []
        self._obs = dict(obs)

    def __call__(self, forget_stale=True):
        self.calls.append("observe")
        self.forget_stale.append(forget_stale)
        return dict(self._obs)


class _FixtureBackendOps:
    """Private fixture dedicated-backend gate (pid state + restart body)."""

    def __init__(self, *, pid_state=None, restart=None):
        self.calls = []
        self.restart_kwargs = None
        self._pid_state = dict(pid_state) if pid_state is not None \
            else _fixture_pid_state()
        self._restart = dict(restart) if restart is not None \
            else _fixture_restart()

    def backend_pid_state(self):
        self.calls.append("backend_pid_state")
        return dict(self._pid_state)

    def restart(self, **kwargs):
        self.calls.append("restart")
        self.restart_kwargs = kwargs
        return dict(self._restart)


class _FixtureMaintenance:
    """Private fixture maintenance seams (begin / end / status)."""

    def __init__(self, *, begin=None, end=None, status=None):
        self.begin_calls = []
        self.end_calls = []
        self.status_calls = 0
        self._begin = _share_fixture_seq(
            begin if begin is not None else _fixture_lease_begin())
        self._end = _share_fixture_seq(
            end if end is not None else _fixture_lease_end())
        self._status = _share_fixture_seq(
            status if status is not None else _fixture_maint_view(
                now=_SHARE_FIXTURE_EPOCH))

    def begin(self, component, lease_s):
        self.begin_calls.append((component, lease_s))
        value = next(self._begin)
        if isinstance(value, BaseException):
            raise value
        return dict(value)

    def end(self, component, lease_id):
        self.end_calls.append((component, lease_id))
        value = next(self._end)
        if isinstance(value, BaseException):
            raise value
        return dict(value)

    def status(self):
        self.status_calls += 1
        value = next(self._status)
        if isinstance(value, BaseException):
            raise value
        return value


class _BlockingGatewayRestart:
    """Private fixture broker gateway-restart verb that BLOCKS until the fixture
    releases it — models the bounded, non-cancellable broker call."""

    def __init__(self, result):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.budgets = []
        self._result = dict(result)

    def __call__(self, budget_s):
        self.budgets.append(budget_s)
        self.entered.set()
        assert self.release.wait(10.0), "fixture release never arrived"
        return dict(self._result)


# ------------------------------------------------- synthetic evidence values

def _fixture_limits(*, now, verdict=True, detail="ok", checked_at=None):
    return {"check": "limits", "verdict": verdict, "detail": detail,
            "checked_at": int(now if checked_at is None else checked_at)}


def _fixture_faults(*, now, last_fault=None):
    return {"check": "faults",
            "verdict": {"last_fault": last_fault,
                        "counts_by_phase": {"auth": 0, "config": 0, "body": 0,
                                             "limit": 1, "upstream": 3,
                                             "stream": 0},
                        "file": {"present": True, "size": 4096,
                                  "mtime": float(now)}},
            "detail": "read tail ok", "checked_at": int(now)}


def _fixture_last_fault(*, now):
    return {"at": float(now), "phase": "upstream", "status": 502,
            "code": "um_unavailable", "upstream_status": 503,
            "exception_type": None, "duration_ms": 120.5,
            "request_id": "fixture-request-0001"}


def _fixture_backend_obs(*, now, running=True, auth_state="valid",
                          auth_label="认证有效", observed_at=None,
                          upstream_state="ok", checked_epoch=None,
                          attribution=None):
    if observed_at is None:
        observed_at = now
    if checked_epoch is None:
        checked_epoch = observed_at
    if attribution is None:
        attribution = {"file_ready": True, "reason": "ok",
                       "instance_loaded": {"value": "yes",
                                            "basis": "inference"}}
    return {"running": running, "auth_state": auth_state,
            "auth_label": auth_label, "observed_at": observed_at,
            "upstream_reachability": {"state": upstream_state,
                                      "checked_epoch": checked_epoch,
                                      "changed": False,
                                      "outage_started": None,
                                      "last_ok": checked_epoch},
            "attribution": attribution}


def _fixture_pid_state(**over):
    out = {"state": "running", "pid": 54300, "port": 8110,
           "identity": "verified", "identity_reason": "ok"}
    out.update(over)
    return out


def _fixture_restart(**over):
    out = {"pid_before": 54300, "pid_after": 61200, "port": 8110,
           "profile": "<backend-profile>", "identity": "verified"}
    out.update(over)
    return out


def _fixture_lease_begin(**over):
    out = {"accepted": True, "component": "share_gateway",
           "lease_id": _SHARE_LEASE_ID, "lease_expires_at": 1790000300,
           "instance_id": _SHARE_INSTANCE_ID, "round": 7,
           "state": "draining", "active_count": 3}
    out.update(over)
    return out


def _fixture_lease_end(**over):
    out = {"resumed": True, "component": "share_gateway",
           "lease_id": _SHARE_LEASE_ID, "instance_id": _SHARE_INSTANCE_ID,
           "round": 7, "state": "running", "active_count": 0}
    out.update(over)
    return out


def _fixture_lease(**over):
    out = {"lease_id": _SHARE_LEASE_ID, "component": "share_gateway",
           "instance_id": _SHARE_INSTANCE_ID, "round": 7,
           "created_at": 1790000000, "expires_at": 1790000300,
           "active_count_at_begin": 3}
    out.update(over)
    return out


def _fixture_maint_view(*, now, lease=None, lease_expired=False,
                         instance_state="running", active_count=0,
                         last_outcome=None):
    return {"checked_at": int(now), "lease": lease,
            "lease_expired": lease_expired,
            "instance": {"instance_id": _SHARE_INSTANCE_ID,
                          "state": instance_state,
                          "active_count": active_count,
                          "last_seen_generation": 12},
            "last_outcome": last_outcome}


def _fixture_outcome(*, ok=False, code="unconfirmed",
                       detail="fixture: 上次维护未确认（恢复不擦除失败）"):
    return {"at": 1790000000, "action": "gateway-restart", "ok": ok,
            "code": code, "detail": detail}


# ------------------------------------------------------- scenario plumbing

@contextlib.contextmanager
def _fixture_seams(**values):
    """Patch the console orchestration seams for ONE scenario run; every value
    is a private fixture and every patch is reverted afterwards."""
    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(
                mock.patch.object(console_share_actions, name, value))
        yield


def _console_manager(registry, tmp):
    return OperationManager(os.path.join(tmp, "console-state.sqlite3"),
                            registry, os.path.join(tmp, "console-locks"),
                            acquire_timeout_s=3.0)


def _wait_terminal(manager, op_id, timeout_s):
    deadline = time.perf_counter() + float(timeout_s)
    view = manager.get_operation(op_id)
    while view["state"] not in ("succeeded", "failed", "cancelled",
                                "interrupted"):
        assert time.perf_counter() < deadline, \
            "operation did not finish: %r" % (view,)
        time.sleep(0.02)
        view = manager.get_operation(op_id)
    return view


def _run_console_action(registry, action, params, *, seams, timeout_s=10.0):
    """Submit ONE action through the REAL registry + REAL OperationManager
    (temp state dir, injected private-fixture seams) and return the manager's
    terminal operation view."""
    with _fixture_seams(**seams):
        with tempfile.TemporaryDirectory(
                prefix="share-maintenance-fixture-") as tmp:
            manager = _console_manager(registry, tmp)
            try:
                submitted = manager.submit(action, "share", params)
                view = _wait_terminal(manager, submitted["id"], timeout_s)
            finally:
                manager.close(wait=True)
    return view


def _run_gateway_cancel(registry, seams, restart, timeout_s=10.0):
    """Cancel arrives DURING the blocking broker call: the handler returns the
    ACTUAL result and reports the cancel honestly (contract §6.1/§9.5)."""
    with _fixture_seams(**seams):
        with tempfile.TemporaryDirectory(
                prefix="share-maintenance-cancel-") as tmp:
            manager = _console_manager(registry, tmp)
            try:
                submitted = manager.submit("share.gateway_restart", "share",
                                           {"restart_budget_s": 120})
                assert restart.entered.wait(5.0), \
                    "fixture broker call was never entered"
                manager.cancel(submitted["id"])
                restart.release.set()
                view = _wait_terminal(manager, submitted["id"], timeout_s)
            finally:
                manager.close(wait=True)
    return view


def _run_queued_cancel(registry, seams):
    """Cancel arrives while the operation is QUEUED (this fixture holds the
    resource lock): the REAL manager records ``cancelled`` and the handler
    never executes."""
    with _fixture_seams(**seams):
        with tempfile.TemporaryDirectory(
                prefix="share-maintenance-queued-") as tmp:
            lock_dir = os.path.join(tmp, "console-locks")
            manager = _console_manager(registry, tmp)
            holder = ResourceFileLock(lock_dir, "share:orchestration",
                                       owner="fixture-holder", timeout_s=1.0)
            holder.acquire()
            try:
                submitted = manager.submit("share.gateway_restart", "share", {})
                time.sleep(0.3)   # let the worker reach the lock wait
                view = manager.cancel(submitted["id"])
            finally:
                holder.__exit__(None, None, None)
                manager.close(wait=True)
    return view


# --------------------------------------------------- operation publishing

def _iso_epoch(epoch):
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _fixture_operation(view, case):
    """The REAL OperationManager view (contract §10.1), with the per-run uuid
    normalized to a stable fixture id and the epoch timestamps duplicated as
    ISO-8601 UTC (the frontend data module reads ``created_at`` as a string)."""
    if view is None:
        return None
    created = view.get("created_at")
    started = view.get("started_at")
    finished = view.get("finished_at")
    return {"id": "fixture-%s-op" % case,
            "action": view["action"], "target": view["target"],
            "state": view["state"], "resource": view["resource"],
            "generation": view["generation"],
            "cancel_requested": view["cancel_requested"],
            "created_at": _iso_epoch(created),
            "started_at": _iso_epoch(started) if started is not None else None,
            "finished_at": _iso_epoch(finished) if finished is not None else None,
            "created_epoch": created, "started_epoch": started,
            "finished_epoch": finished,
            "steps": view["steps"], "result": view["result"],
            "error": view["error"],
            "idempotency_key": view["idempotency_key"]}


def _pair(layers, *keys):
    if not isinstance(layers, dict):
        return None
    return {key: layers[key] for key in keys if key in layers}


def _scenario_record(case, operation, *, result, readback, before=None,
                       after=None, last_error=None, note, **extra):
    out = {"case": case,
           "operation": _fixture_operation(operation, case),
           "result": result, "readback": readback, "before": before,
           "after": after, "last_error": last_error, "note": note}
    out.update(extra)
    return out


# ------------------------------------------------------------- the scenarios

def _scenario_normal(registry, clock):
    """1. normal: all nine layers confirmed; share.diagnostics returns."""
    now = clock.time()
    broker = _FixtureBroker(
        verify_running=True, limits=_fixture_limits(now=now),
        faults=_fixture_faults(now=now,
                               last_fault=_fixture_last_fault(now=now)))
    observe = _FixtureObserve(_fixture_backend_obs(now=clock.time()))
    seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {
            "pid": 54300, "started_at": _SHARE_FIXTURE_EPOCH,
            "reason": "ok"},
        "_share_entry_attribution": lambda: {
            "file_ready": True, "reason": "ok",
            "instance_loaded": {"value": "yes", "basis": "inference"},
            "gateway_pid": 54300,
            "gateway_started_at": _SHARE_FIXTURE_EPOCH,
            "key_file_mtime": _SHARE_FIXTURE_EPOCH},
    }
    operation = _run_console_action(registry, "share.diagnostics", {},
                                     seams=seams)
    report = operation["result"]
    assert operation["state"] == "succeeded", operation["error"]
    assert observe.forget_stale == [False, False, False]
    assert [layer["state"] for layer in report["layers"].values()] == \
        ["running", "running", "valid", "ok", "ok", "running", "on", "ok", "running"]
    return _scenario_record(
        "normal", operation, result=report,
        readback=_pair(report["layers"], "share_gateway", "share_backend"),
        note=("九层全部 confirmed（只读、无副作用）；维护视图无租约、无 "
              "last_error；观察缝 forget_stale=False（不删 backend.pid）。"))


def _scenario_unknown(registry, clock):
    """2. unknown: share_entry evidence unavailable + reason; overall returns."""
    now = clock.time()
    broker = _FixtureBroker(
        verify_error=OperationError(
            "share_broker_unwired",
            "fixture: broker 宿主验证缝不可达；不冒充运行状态"),
        limits=_fixture_limits(now=now), faults=_fixture_faults(now=now))
    observe = _FixtureObserve(_fixture_backend_obs(now=clock.time()))
    seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: None,
        "_share_entry_attribution": lambda: None,
    }
    operation = _run_console_action(registry, "share.diagnostics", {},
                                     seams=seams)
    report = operation["result"]
    gateway = report["layers"]["share_gateway"]
    assert operation["state"] == "succeeded", operation["error"]
    assert gateway["state"] == "unavailable"
    assert gateway["verdict"] == "unavailable"
    assert gateway["reason"] == "share_broker_unwired"
    assert gateway["checked_at"] is None and gateway["stale"] is True
    return _scenario_record(
        "unknown", operation, result=report,
        readback=_pair(report["layers"], "share_gateway", "share_backend"),
        note=("share_entry 证据不可得（verdict=unavailable + reason="
              "share_broker_unwired）；整体仍返回，其余层照常给出证据。"))


def _scenario_stale(registry, clock):
    """3. stale: evidence observation times exceed their TTL."""
    now = clock.time()
    old = now - 1000.0
    observe = _FixtureObserve(_fixture_backend_obs(
        now=clock.time(), observed_at=old, checked_epoch=old))
    broker = _FixtureBroker(
        verify_running=True,
        limits=_fixture_limits(now=now, checked_at=now - 1300.0),
        faults=_fixture_faults(now=now))
    seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    operation = _run_console_action(registry, "share.diagnostics", {},
                                     seams=seams)
    report = operation["result"]
    layers = report["layers"]
    assert operation["state"] == "succeeded", operation["error"]
    for key in ("backend_auth", "upstream", "security_gate"):
        assert layers[key]["stale"] is True, key
        assert layers[key]["age_s"] > layers[key]["ttl_s"], key
    assert layers["backend_auth"]["evidence"]["timestamp_basis"] == \
        "source_checked_epoch"
    assert layers["upstream"]["evidence"]["timestamp_basis"] == \
        "source_checked_epoch"
    assert layers["share_gateway"]["stale"] is False
    return _scenario_record(
        "stale", operation, result=report,
        readback=_pair(layers, "backend_auth", "upstream", "security_gate"),
        note=("checked_at 取证据自身观测时间（timestamp_basis="
              "source_checked_epoch）；backend_auth/upstream/security_gate "
              "age_s > ttl_s → stale=true；其余层新鲜。"))


def _scenario_partial_failure(registry, clock):
    """4. partial_failure: one layer unavailable, the rest confirmed; no raise."""
    def broken_connectivity():
        raise OperationError("share_status_unknown",
                             "fixture: tailscale 查询失败")

    now = clock.time()
    broker = _FixtureBroker(
        verify_running=True, limits=_fixture_limits(now=now),
        faults=_fixture_faults(now=now))
    observe = _FixtureObserve(_fixture_backend_obs(now=clock.time()))
    seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": broken_connectivity,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    operation = _run_console_action(registry, "share.diagnostics", {},
                                     seams=seams)
    report = operation["result"]
    connectivity = report["layers"]["connectivity"]
    assert operation["state"] == "succeeded", operation["error"]
    assert connectivity["state"] == "unavailable"
    assert connectivity["verdict"] == "unavailable"
    assert connectivity["reason"] == "connectivity_query_failed"
    assert report["layers"]["ts_service"]["state"] == "running"
    assert report["layers"]["security_gate"]["state"] == "ok"
    assert "connectivity" in report["note"]
    return _scenario_record(
        "partial_failure", operation, result=report,
        readback=_pair(report["layers"], "connectivity", "ts_service",
                        "security_gate"),
        note=("单层 connectivity 缝抛异常 → 只该层 unavailable + reason；"
              "整体不抛，其余层照常给出证据。"))


def _scenario_authenticating(registry, clock):
    """5. authenticating: backend_auth logging_in + reauthenticate returns it."""
    now = clock.time()
    broker = _FixtureBroker(
        verify_running=True, limits=_fixture_limits(now=now),
        faults=_fixture_faults(now=now))
    observe = _FixtureObserve(_fixture_backend_obs(
        now=clock.time(), auth_state="logging_in",
        auth_label="认证中（人工窗口已开启）"))
    reauth = {
        "accepted": True, "state": "logging_in",
        "label": "认证中（人工窗口已开启）", "triggered": False, "polled": 0,
        "instance": {"pid": 54300, "port": 8110,
                      "identity": "verified"},
        "reason": None,
        "note": "已在认证中：不重复开窗、不冷启动、不重启进程"}
    diag_seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    diag_operation = _run_console_action(registry, "share.diagnostics", {},
                                          seams=diag_seams)
    readback = _pair(diag_operation["result"]["layers"],
                      "backend_auth", "share_backend")
    assert diag_operation["state"] == "succeeded", diag_operation["error"]
    assert readback["backend_auth"]["state"] == "logging_in"
    reauth_seams = {"_BACKEND_REAUTH":
                    lambda *, cancel_check=None: dict(reauth)}
    operation = _run_console_action(registry, "share.backend_reauthenticate",
                                     {}, seams=reauth_seams)
    assert operation["state"] == "succeeded", operation["error"]
    assert operation["result"]["state"] == "logging_in"
    assert operation["result"]["triggered"] is False
    return _scenario_record(
        "authenticating", operation, result=operation["result"],
        readback=readback,
        note=("backend_auth.state=logging_in；share.backend_reauthenticate "
              "返回 state=logging_in、triggered=false（不重复开窗、"
              "不冷启动、不重启进程）。"))


def _scenario_maintenance(registry, clock):
    """6. maintenance: a lease is in force; share.backend_restart is refused."""
    now = clock.time()
    outcome = _fixture_outcome()
    view = _fixture_maint_view(now=now, lease=_fixture_lease(),
                                last_outcome=outcome)
    broker = _FixtureBroker(
        verify_running=True, limits=_fixture_limits(now=now),
        faults=_fixture_faults(now=now))
    observe = _FixtureObserve(_fixture_backend_obs(now=clock.time()))
    diag_seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(status=view).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    diag_operation = _run_console_action(registry, "share.diagnostics", {},
                                          seams=diag_seams)
    report = diag_operation["result"]
    assert diag_operation["state"] == "succeeded", diag_operation["error"]
    assert report["maintenance"]["lease"]["lease_id"] == _SHARE_LEASE_ID
    assert report["last_error"]["code"] == "unconfirmed"
    restart_seams = {
        "_BROKER_OPS": broker,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_BEGIN": _FixtureMaintenance(begin=OperationError(
            "share_maintenance_refused",
            "fixture: 已有维护租约在force（component=share_entry）；"
            "停止优先，拒绝重启（迟到结果不得重开）")).begin,
        "_MAINTENANCE_END": _FixtureMaintenance().end,
        "_MAINTENANCE_STATUS": _FixtureMaintenance(status=view).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    operation = _run_console_action(registry, "share.backend_restart", {},
                                     seams=restart_seams)
    assert operation["state"] == "failed"
    assert operation["error"]["code"] == "share_maintenance_refused"
    assert operation["result"] is None
    return _scenario_record(
        "maintenance", operation, result=None,
        readback=_pair(report["layers"], "share_gateway", "share_backend"),
        last_error=report["last_error"],
        maintenance=report["maintenance"],
        desired_stop_in_force=report["desired_stop_in_force"],
        note=("maintenance.lease 非 null（last_outcome.ok=false → last_error "
              "如实透出，恢复不擦除失败）；share.backend_restart 被拒绝 "
              "share_maintenance_refused（不调 restart、不冷启动）。"))


def _scenario_timeout(registry, clock):
    """7. timeout: bounded drain times out -> resume + drain_timeout."""
    now = clock.time()
    lease = _fixture_lease()
    drain_view = _fixture_maint_view(now=now, lease=lease, active_count=3)
    maintenance = _FixtureMaintenance(
        begin=_fixture_lease_begin(),
        end=_fixture_lease_end(),
        status=drain_view)
    seams = {
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_BEGIN": maintenance.begin,
        "_MAINTENANCE_END": maintenance.end,
        "_MAINTENANCE_STATUS": maintenance.status,
        "_DESIRED_STOP_READER": lambda: False,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    operation = _run_console_action(
        registry, "share.backend_restart",
        {"drain_budget_s": 1, "poll_interval_s": 1}, seams=seams)
    assert operation["state"] == "failed"
    error = operation["error"]
    assert error["code"] == "share_backend_drain_timeout"
    assert "drain.resumed=true" in error["message"]
    assert "active_left=3" in error["message"]
    assert maintenance.end_calls == [("share_gateway", _SHARE_LEASE_ID)]
    assert maintenance.status_calls >= 1
    assert "restart" not in seams["_BACKEND_OPS"].calls
    drain = {"state": "timed_out", "active_left": 3,
             "lease_id": _SHARE_LEASE_ID,
             "instance_id": _SHARE_INSTANCE_ID, "round": 7,
             "resumed": True}
    return _scenario_record(
        "timeout", operation, result=None,
        readback=_pair(drain_view["instance"], "instance_id", "state",
                        "active_count"),
        drain=drain, error=operation["error"],
        note=("有界排空超时：恢复 8100 接收（resumed=true，恢复不擦除失败）、"
              "未强停目标、未调 restart；active_left 为注入维护视图的在途数量。"))


def _scenario_cancelled(registry, clock):
    """8. cancelled: cancel during the blocking broker call + queued cancel.

    The delivered handler returns the ACTUAL result after a cancel arrives during
    the bounded broker call (contract §6.1/§9.5); the manager still records
    ``cancel_requested``. A second REAL operation shows the queued-cancel
    terminal state ``cancelled`` (never executed)."""
    now = clock.time()
    restart = _BlockingGatewayRestart({
        "restarted": True, "component": "share_gateway",
        "stopped_confirmed": True, "running_confirmed": True,
        "elapsed_s": 41.2, "maintenance_state": "idle"})
    broker = _FixtureBroker(
        verify_running=True, limits=_fixture_limits(now=now),
        faults=_fixture_faults(now=now))
    observe = _FixtureObserve(_fixture_backend_obs(now=clock.time()))
    seams = {
        "_BROKER_OPS": broker,
        "_TS_SERVICE_STATUS": lambda: "running",
        "_CONNECTIVITY_OK": lambda: True,
        "_FUNNEL_OBSERVED": lambda: True,
        "_BACKEND_OBSERVE": observe,
        "_BACKEND_OPS": _FixtureBackendOps(),
        "_MAINTENANCE_STATUS": _FixtureMaintenance(
            status=_fixture_maint_view(now=clock.time())).status,
        "_DESIRED_STOP_READER": lambda: False,
        "_GATEWAY_RESTART": restart,
        "_share_entry_probe": lambda: {"pid": 54300, "reason": "ok"},
        "_share_entry_attribution": lambda: {"file_ready": True,
                                               "reason": "ok"},
    }
    operation = _run_gateway_cancel(registry, seams, restart)
    assert operation["state"] == "succeeded", operation["error"]
    assert operation["cancel_requested"] is True
    notes = " ".join(step["note"] for step in operation["steps"])
    assert "取消已请求" in notes and "按实际结果返回" in notes
    assert operation["result"]["restarted"] is True
    cancelled_operation = _run_queued_cancel(registry, seams)
    assert cancelled_operation["state"] == "cancelled"
    assert cancelled_operation["error"]["code"] == "cancelled"
    assert "never executed" in cancelled_operation["error"]["message"]
    cancellation = {
        "requested": True,
        "arrived_during": "blocking_broker_call",
        "side_effect_committed": True,
        "actual_result_returned": True,
        "operation_state": operation["state"],
        "note": ("契约 §10.2 场景 8 写作 operation state=cancelled；交付的 "
                  "handler（§6.1/§9.5、测试 test_44）在阻塞调用返回后按实际"
                  "结果返回，故 cancel 请求记录在 cancel_requested、结果按实际"
                  "返回；排队阶段取消的 operation 记录见 cancelled_operation。"),
    }
    return _scenario_record(
        "cancelled", operation, result=operation["result"],
        readback=_pair(operation["result"]["after"], "share_gateway",
                        "share_backend"),
        before=operation["result"]["before"],
        after=operation["result"]["after"],
        last_error=operation["result"]["last_error"],
        cancellation=cancellation,
        cancelled_operation=_fixture_operation(cancelled_operation,
                                                "cancelled-queued"),
        note=("取消在阻塞 broker 调用期间到达：handler 按实际结果返回"
              "（不伪报回退）；排队阶段取消的真实记录见 "
              "cancelled_operation（state=cancelled）。"))


class _FixtureSupervisionOps:
    """Private fixture supervision ops gate for the A9-5 run: the read-only
    pid report; a start/verify is an assertion (the ``budget_exhausted``
    decision must never call it)."""

    def __init__(self):
        self.calls = []
        self._pid_state = {"state": "pid_file_absent", "pid": None,
                            "port_listening": False, "pid_file_present": False,
                            "pid_file": "<backend-pid-file>",
                            "pid_matches": None, "identity": "unknown",
                            "identity_reason": "pid_file_absent"}

    def backend_pid_state(self):
        self.calls.append("backend_pid_state")
        return dict(self._pid_state)

    def start(self):
        self.calls.append("start")
        raise AssertionError("budget_exhausted fixture must never start()")

    def verify_running(self):
        self.calls.append("verify_running")
        raise AssertionError("budget_exhausted fixture must never verify_running()")

    def _pid_alive(self, pid):
        raise AssertionError("budget_exhausted fixture must never query liveness")


class _FixtureSupervisionBroker:
    """Private fixture broker for the supervision run: a maintenance-status view
    through an injected fake broker client (never the real deployment adapter)."""

    def __init__(self, *, view):
        self.status_calls = 0
        self._view = dict(view)
        self.client = self

    def maintenance_status(self):
        self.status_calls += 1
        return SimpleNamespace(ok=True, result=dict(self._view))


def _share_maintenance_supervision(clock):
    """Scenario 9 (budget_exhausted): the supervision decision/state records,
    produced by the REAL ``share_backend_supervision.run_once(apply=True)`` with
    private-fixture inputs (temp watch dir, fake boot identity, fake broker view
    and fake ops gate). The temp watch path is normalized out of the bounded
    ``detail`` (the same privacy discipline as the synthetic PIDs); nothing here
    reads or writes a production path, ledger or the real broker."""
    now = clock.time()
    boot_id = "FIXTURE-HOST|2026-10-02T09:00:00.0000000Z"
    with tempfile.TemporaryDirectory(prefix="share-maintenance-watch-") as tmp:
        desired_path = os.path.join(tmp, "share-backend-desired.json")
        state_path = os.path.join(tmp, "share-backend-watch-state.json")
        log_path = os.path.join(tmp, "share-backend-watch.log")
        service_log = os.path.join(tmp, "share-backend.log")
        lifecycle_log = os.path.join(tmp, "backend-lifecycle.log")
        with mock.patch.object(supervision, "_BOOT_ID_READER",
                                lambda: boot_id), \
             mock.patch.object(supervision, "_DESIRED_STOP_READER",
                                lambda: None):
            assert supervision.write_desired_state(
                "running", source="fixture", path=desired_path)
            supervision.write_watch_state(
                {"boot_id": boot_id, "consecutive_failures": 1,
                 "restart_attempts_utc": [_iso_epoch(now - 900.0),
                                           _iso_epoch(now - 600.0),
                                           _iso_epoch(now - 300.0)],
                 "idle_reason": "", "last_decision": None}, state_path)
            # The parent lifecycle log carries the last stop as an abrupt one;
            # the child service log carries the last listening line (private).
            Path(lifecycle_log).write_text(
                "2026-10-02 11:59:00,000 INFO force stop pid=54300 port=8110"
                " by=taskkill result=dead\n", encoding="utf-8")
            Path(service_log).write_text(
                "2026-10-02 11:00:00,000 INFO listening host=127.0.0.1"
                " port=8110 pid=54300\n", encoding="utf-8")
            ops = _FixtureSupervisionOps()
            broker = _FixtureSupervisionBroker(
                view=_fixture_maint_view(now=now))
            record = supervision.run_once(
                apply=True, now=now, ops=ops, broker=broker, state_dir=tmp,
                lock_dir=os.path.join(tmp, "console-locks"),
                log_path=service_log)
            state = supervision.read_watch_state(state_path)
    assert record["action"] == "none"
    assert record["code"] == "budget_exhausted"
    assert record["confirmations"] == 2
    assert record["attempts_this_hour"] == 3
    assert record["instance"] == {"pid": None, "identity": "unknown"}
    # The refusal is decided by the IN-LOCK re-check (contract §7.2
    # step 9): the pre-lock check runs first, so the read-only probe is
    # called twice and no state is written by the refusal itself.
    assert ops.calls == ["backend_pid_state", "backend_pid_state"]
    # Pre-lock check + the in-lock re-check (contract §7.2 step 9):
    # the refusal is decided inside the lock, so the broker status is
    # read twice.
    assert broker.status_calls == 2
    assert state["idle_reason"] == "budget_exhausted"
    assert state["consecutive_failures"] == 2
    assert len(state["restart_attempts_utc"]) == 3
    decision = dict(record)
    decision["detail"] = decision["detail"].replace(
        os.path.normpath(tmp), "<watch-dir>").replace(tmp, "<watch-dir>")
    return {
        "generatedFrom": ("share.backend_supervision.run_once(apply=True) via"
                            " the real handler; private fixture inputs"
                            " (temp watch dir, fake boot id, fake broker view,"
                            " fake ops gate)"),
        "decision": decision,
        "state": {key: state[key] for key in
                  ("version", "boot_id", "consecutive_failures",
                   "restart_attempts_utc", "idle_reason")}}


def _scenario_budget_exhausted(clock):
    """9. budget_exhausted: the supervision decision/state records."""
    supervision_record = _share_maintenance_supervision(clock)
    return _scenario_record(
        "budget_exhausted", None, result=None, readback=None,
        supervision=supervision_record,
        note=("监督决策记录由真实 share_backend_supervision.run_once(apply=True)"
              " 生成：action=none、code=budget_exhausted、attempts_this_hour=3、"
              "idle_reason=budget_exhausted；本段无 console 动作"
              "（不与动作名混用）。"))


# ---------------------------------------------------------- segment assembly

_SHARE_MAINTENANCE_ACTIONS = (
    ("share.diagnostics", ["share_gateway", "share_backend"], ["read_only"]),
    ("share.backend_reauthenticate", ["share_backend"],
     ["trigger_backend_login"]),
    ("share.backend_restart", ["share_backend"],
     ["pause_gateway_acceptance", "restart_share_backend",
      "resume_gateway_acceptance"]),
    ("share.gateway_restart", ["share_gateway"], ["restart_share_entry"]),
)


def _share_maintenance_actions(specs):
    """The four Rev9 actions, published by the REAL catalog (``action_info``
    with the additive bounded ``param_schema``); ``components``/``side_effects``
    are explicit private-fixture facts (contract §1.1/§1.2/§5.2/§6.1)."""
    info = {row["action"]: row for row in OperationManager.action_info(
        SimpleNamespace(_specs={spec.name: spec for spec in specs}))}
    rows = []
    for action, components, side_effects in _SHARE_MAINTENANCE_ACTIONS:
        row = dict(info[action])
        row["components"] = list(components)
        row["side_effects"] = list(side_effects)
        row["target"] = "share"
        rows.append(row)
    return rows


def generate_share_maintenance(specs):
    """Rev9 A9-8: the share_maintenance segment (contract §10.2).

    ``actions`` is published by the REAL catalog; ``scenarios``/``supervision``
    are produced by driving the REAL handlers through the REAL OperationManager
    and the REAL ``share_backend_supervision.run_once`` with private-fixture
    inputs. Nothing here reaches a production service: the real broker deployment
    loader is guarded to fail loudly if the fixture ever needs it."""
    broker_guard = mock.Mock(side_effect=AssertionError(
        "share_maintenance fixture must never build the real broker"
        " deployment adapter"))
    lifecycle_guard = mock.Mock(side_effect=AssertionError(
        "share_maintenance fixture must never read the production broker"
        " lifecycle record"))
    clock = _ShareFixtureClock()
    with mock.patch.object(console_share_production, "_default_broker",
                            broker_guard), \
         mock.patch.object(console_share_production,
                            "production_desired_stop_record",
                            lifecycle_guard), \
         mock.patch.object(time, "time", clock.time), \
         mock.patch.object(time, "monotonic", clock.monotonic), \
         mock.patch.object(console_share_actions, "_SLEEP",
                            lambda _seconds: None):
        scenarios = [
            _scenario_normal(specs, clock),
            _scenario_unknown(specs, clock),
            _scenario_stale(specs, clock),
            _scenario_partial_failure(specs, clock),
            _scenario_authenticating(specs, clock),
            _scenario_maintenance(specs, clock),
            _scenario_timeout(specs, clock),
            _scenario_cancelled(specs, clock),
            _scenario_budget_exhausted(clock),
        ]
        supervision_record = scenarios[-1]["supervision"]
    return {
        "generatedFrom": ("console_share_actions handlers via the real registry + "
                           "OperationManager, and share_backend_supervision."
                           "run_once for the supervision decision; every seam"
                           " value is a private fixture"),
        "components": {
            "share_gateway": ("8100 分享入口进程（um-share-python.exe + "
                                "share.entry.py）"),
            "share_backend": ("8110 专用后端进程（chat2api.py --port 8110 "
                                "--profile <dir>\\edge-profile）"),
        },
        "actions": _share_maintenance_actions(specs),
        "scenarios": scenarios,
        "supervision": supervision_record,
    }


if __name__ == "__main__":
    result = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Generated synthetic backend contract fixture:", DESTINATION)
