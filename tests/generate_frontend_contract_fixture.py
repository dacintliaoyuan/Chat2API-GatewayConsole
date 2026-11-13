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
from test_console_model_actions import temp_sync_env, _Ctx
from test_console_api import usagesrv
from console import adapters as console_adapters
from console import log_reader as console_log_reader
from console import model_actions as actions
from console import share_actions as console_share_actions
from console import share_production as console_share_production
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
         mock.patch.object(sync, "datetime", FixedDatetime), \
         mock.patch.object(share_metering, "datetime", FixedDatetime), \
         contextlib.redirect_stdout(io.StringIO()):
        model_catalog = actions.model_catalog("", {}, _Ctx())
        # R7（2026-10-10）：显式 sources 只读该来源——夹具固定「单来源」形状，
        # 与「全部模型」聚合入口（上面的 model_catalog）并列。
        scoped_catalog = actions.model_catalog("", {"sources": json.dumps(["um"])}, _Ctx())
        probe_preview = actions.model_context_probe_preview("", {}, _Ctx())
        # 只读读回：context overrides 只经 state + catalog 计算，不触碰
        # 已退役的 model sync/settings 动作。
        contexts = {"overrides": sync.context_override_surface(sync.load_state(), catalogs),
                    "note": "Synthetic private-client snapshot"}
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
\
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
        # The local registry keeps the local cards; the cloud registry
        # carries the share cards (the local share chain is retired, #33).
        specs = (console_adapters.build_registry()
                 + console_adapters.build_cloud_registry())
        capabilities = {"actions": OperationManager.action_info(
            SimpleNamespace(_specs={s.name: s for s in specs}))}
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
        return {"metadata": {"fixture": True,
            "generatedFrom": "generate_frontend_contract_fixture.py:real handlers/private fixtures",
            "sourceHashEncoding": "utf8-lf",
            "sourceHashes": {n: hashlib.sha256((root / n).read_text(encoding="utf-8")
                .replace("\r\n", "\n").encode("utf-8")).hexdigest() for n in names}},
            "actions": capabilities, "catalog": model_catalog,
            "scoped_catalog": scoped_catalog,
            "context_overrides": contexts,
            "context_probe_preview": probe_preview,
            "share_usage": share_usage, "log_entries": logs,
            "log_sources": log_sources, "log_overview": log_overview,
            }


if __name__ == "__main__":
    result = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Generated synthetic backend contract fixture:", DESTINATION)
