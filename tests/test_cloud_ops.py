"""P1/A10 drill: encrypted offline backup/restore of share state.

Run from ``ai-gateway``:

    python -m unittest tests.test_cloud_ops

The drill uses only isolated temporary directories and synthetic counters:

* real :class:`share_store.ShareStore` databases with temporary keys;
* the product's real ``ShareStore.backup()`` online backup path;
* a local directory standing in for the remote object store (no R2/OSS call);
* the real ``share_metering.read_share_usage`` reader for restored history.

The module writes ``tmp/um-cloud-p1-20261030/ops-sim-<platform>.json`` after the
tests (``UM_CLOUD_OPS_REPORT_DIR`` overrides the directory).  The platform suffix
keeps a Windows run and a later Linux run from overwriting each other; the report
still keeps a ``runs`` entry per platform.
"""
import json
import os
import platform
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]          # ai-gateway/
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "um-cloud-deploy"))

import backup_restore  # noqa: E402
import share.metering as share_metering  # noqa: E402
from share.store import ShareStore  # noqa: E402

SCENARIOS = []
UNCOVERED = [
    "真实 R2/OSS/阿里云对象存储未接入；本地目录仅模拟异机，未验证真实上传、下载、"
    "对象生命周期、跨账号权限或恢复材料异地保存。",
    "未启动真实生产服务、未改生产端口/配置。",
    "未验证真实 UM 上游停止生成；在途中断仅到本机回调边界，不能描述为已证明 UM 停止计量。",
    "Windows 无 POSIX 0600 权限位；密钥文件 0600 仅在 POSIX 上强制，Windows 侧依赖 ACL 配置，"
    "本演练未验证 Windows ACL。",
    "备份不接入 systemd timer；本演练只验证离线加密包与恢复，未验证真实定时器、"
    "补传或磁盘满/网络中断下的长时间行为。",
    "未实现独立撤销安全记录/旧备份自动围栏；restore 只校验包内 generation 并支持可选 "
    "require_min_generation，测试未覆盖“旧备份恢复不复活后续撤销 Key”的自动围栏。",
    "价格表/配置未随备份打包；本任务只备份 ShareStore SQLite 快照与分享用量 JSONL，"
    "未验证价格版本随恢复一起还原。",
]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _platform_key() -> str:
    return (f"{platform.system().lower()}-"
            f"{platform.python_implementation().lower()}-"
            f"{platform.python_version()}")


def _report_path() -> Path:
    override = os.environ.get("UM_CLOUD_OPS_REPORT_DIR")
    directory = (Path(override) if override
                 else REPO / "tmp" / "um-cloud-p1-20261030")
    return directory / f"ops-sim-{_platform_key()}.json"


def _cryptography_version() -> str:
    try:
        import cryptography
        return cryptography.__version__
    except Exception:  # noqa: BLE001 — 测试替身故意模拟后端失败
        return "unavailable"


def _write_report(scenarios) -> None:
    report_path = _report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    try:
        with report_path.open("r", encoding="utf-8") as handle:
            existing = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        existing = {}
    runs = existing.get("runs") if isinstance(existing, dict) else {}
    if not isinstance(runs, dict):
        runs = {}
    failures = [
        {
            "kind": "test_failure",
            "scenario": item["name"],
            "detail": item.get("failure"),
        }
        for item in scenarios if item["result"] != "passed"
    ]
    failures.extend({"kind": "uncovered", "item": item} for item in UNCOVERED)
    key = _platform_key()
    run = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cryptography": _cryptography_version(),
        "command": f"{sys.executable} -m unittest tests.test_cloud_ops",
        "generated_at": _utc_now(),
        "summary": {
            "tests": len(scenarios),
            "passed": sum(1 for item in scenarios
                          if item["result"] == "passed"),
            "failed": sum(1 for item in scenarios
                          if item["result"] != "passed"),
        },
        "scenarios": scenarios,
        "failures": failures,
    }
    runs[key] = run
    all_scenarios = []
    all_failures = []
    seen_uncovered = set()
    for run_key in sorted(runs):
        run_entry = runs[run_key]
        for item in run_entry.get("scenarios", []):
            entry = dict(item)
            entry["platform_key"] = run_key
            all_scenarios.append(entry)
        for item in run_entry.get("failures", []):
            if item.get("kind") == "uncovered":
                if item.get("item") in seen_uncovered:
                    continue
                seen_uncovered.add(item.get("item"))
            entry = dict(item)
            entry["platform_key"] = run_key
            all_failures.append(entry)
    report = {
        "task": "P1/A10 encrypted backup/restore drill",
        "generated_at": _utc_now(),
        "latest_platform": key,
        "scenarios": all_scenarios,
        "failures": all_failures,
        "uncovered": list(UNCOVERED),
        "runs": runs,
    }
    temp = report_path.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, report_path)


def tearDownModule():
    if SCENARIOS:
        _write_report(SCENARIOS)


def _usage_record(key_id, index, *, input_tokens=10, output_tokens=5):
    return {
        "time": f"2026-10-31T00:0{index}:00+08:00",
        "service": "chat2api",
        "model": "synthetic-model",
        "stream": True,
        "status": "ok",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_source": "upstream",
        "key_id": key_id,
        "request_id": f"request-{index}",
        "attribution": "verified",
        "price_version": "synthetic-v1",
    }


class CloudOpsDrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="cloud-ops-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.details = {}
        self._scenario = self._testMethodName
        self._old_secret_env = os.environ.get(
            "CHAT2API_SHARE_SECRET_KEY_FILE")
        if os.name == "posix":
            secret_path = self.root / "share-secret.key"
            backup_restore.write_backup_key(secret_path)
            os.environ["CHAT2API_SHARE_SECRET_KEY_FILE"] = str(secret_path)

            def restore_env():
                if self._old_secret_env is None:
                    os.environ.pop("CHAT2API_SHARE_SECRET_KEY_FILE", None)
                else:
                    os.environ["CHAT2API_SHARE_SECRET_KEY_FILE"] = \
                        self._old_secret_env

            self.addCleanup(restore_env)

    def tearDown(self):
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        failed = False
        skipped = False
        failure_text = None
        if result is not None:
            for test, trace in list(result.failures) + list(result.errors):
                if test is self:
                    failed = True
                    failure_text = trace
                    break
            if any(test is self for test, _ in result.skipped):
                skipped = True
        SCENARIOS.append({
            "name": self._scenario,
            "result": "skipped" if skipped else (
                "failed" if failed else "passed"),
            "details": self.details,
            "failure": failure_text,
        })

    # ------------------------------------------------------------- helpers
    def _store(self, name="share.db"):
        store = ShareStore(self.root / "source" / name)
        self.addCleanup(store.close)
        return store

    def _ledger(self, records, partial=None):
        path = self.root / "source" / "usage-share-backend.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if partial:
                handle.write(partial)
        return path

    def _key(self):
        path = self.root / "keys" / "backup.key"
        backup_restore.write_backup_key(path)
        return path, backup_restore.load_backup_key(path)

    def _assert_no_targets(self, *paths):
        for path in paths:
            self.assertFalse(Path(path).exists(), f"unexpected target: {path}")

    # -------------------------------------------------------------- backup
    def test_backup_restore_roundtrip_revoked_key_and_history(self):
        store = self._store()
        alpha = store.create_key("alpha")
        beta = store.create_key("beta")
        store.delete_key(beta["key_id"])
        ledger = self._ledger(
            [_usage_record(alpha["key_id"], 0),
             _usage_record(alpha["key_id"], 1),
             _usage_record(beta["key_id"], 2)],
            partial='{"time":"2026-10-31T00:03:00+08:00",'
                    '"key_id":"half-written"}')
        key_path, key = self._key()
        original_mode = stat.S_IMODE(key_path.stat().st_mode)
        self.details["key_file_mode"] = oct(original_mode)
        if os.name == "posix":
            self.assertEqual(original_mode, 0o600)
            os.chmod(key_path, 0o644)
            with self.assertRaises(backup_restore.BackupKeyError):
                backup_restore.load_backup_key(key_path)
            os.chmod(key_path, 0o600)
            self.assertEqual(
                stat.S_IMODE(key_path.stat().st_mode), 0o600)
        with self.assertRaises(backup_restore.BackupKeyError):
            backup_restore.write_backup_key(key_path)

        snapshot = backup_restore.snapshot_ledger(ledger)
        self.assertEqual(snapshot["records"], 3)
        self.assertGreater(snapshot["partial_bytes"], 0)
        self.assertTrue(snapshot["data"].endswith(b"\n"))
        self.assertNotIn(b"half-written", snapshot["data"])
        self.details["snapshot"] = {
            "records": snapshot["records"],
            "partial_bytes": snapshot["partial_bytes"],
            "offset": snapshot["offset"],
        }

        remote = self.root / "remote-sim"
        result = backup_restore.create_backup(
            store, ledger, key, remote, now=1_793_404_800.0)
        sealed = Path(result["backup_path"]).read_bytes()
        self.assertNotIn(b"SQLite format 3", sealed)
        self.assertNotIn(alpha["secret"].encode("utf-8"), sealed)
        self.assertEqual(result["ledger_records"], 3)
        self.assertGreater(result["ledger_partial_bytes"], 0)
        self.assertGreater(result["config_generation"], 0)
        inspected = backup_restore.inspect_backup(
            result["backup_path"], key)
        self.assertEqual(inspected["metadata"]["config_generation"],
                         result["config_generation"])
        self.assertEqual(inspected["db"]["integrity_check"], "ok")

        target_db = self.root / "restored" / "share.db"
        target_ledger = self.root / "restored" / "usage.jsonl"
        restored = backup_restore.restore_backup(
            result["backup_path"], key,
            target_db_path=target_db, target_ledger_path=target_ledger)
        self.assertEqual(restored["config_generation"],
                         result["config_generation"])
        restored_store = ShareStore(target_db)
        self.addCleanup(restored_store.close)
        self.assertTrue(restored_store.verify_secret(
            alpha["key_id"], alpha["secret"]))
        self.assertFalse(restored_store.verify_secret(
            beta["key_id"], beta["secret"]))
        self.assertEqual(restored_store.generation()["config_generation"],
                         result["config_generation"])
        beta_row = restored_store.get_key(beta["key_id"])
        self.assertIsNotNone(beta_row["deleted_at"])

        original_usage = share_metering.read_share_usage(ledger)
        restored_usage = share_metering.read_share_usage(target_ledger)
        self.assertEqual(restored_usage["requests"], 3)
        self.assertEqual(restored_usage["coverage"]["unparsedLines"], 0)
        self.assertNotIn(
            "half-written",
            [item["key_id"] for item in restored_usage["keys"]])
        self.assertEqual(
            sorted(item["key_id"] for item in restored_usage["keys"]),
            sorted(item["key_id"] for item in original_usage["keys"]
                   if item["key_id"] != "half-written"))
        alpha_usage = next(
            item for item in restored_usage["keys"]
            if item["key_id"] == alpha["key_id"])
        self.assertEqual(alpha_usage["requests"], 2)
        self.assertNotIn(b"half-written", target_ledger.read_bytes())
        self.assertTrue(target_ledger.read_bytes().endswith(b"\n"))
        self.details.update({
            "backup_id": result["backup_id"],
            "config_generation": result["config_generation"],
            "applied_generation": result["applied_generation"],
            "ledger_records": result["ledger_records"],
            "ledger_partial_bytes": result["ledger_partial_bytes"],
            "restored_requests": restored_usage["requests"],
            "restored_keys": len(restored_usage["keys"]),
            "valid_key_verifies": True,
            "revoked_key_rejected": True,
            "sealed_size": len(sealed),
        })

    def test_backup_restore_rejects_bad_key_corruption_and_repeat(self):
        store = self._store()
        alpha = store.create_key("alpha")
        ledger = self._ledger([_usage_record(alpha["key_id"], 0)])
        _key_path, key = self._key()
        result = backup_restore.create_backup(
            store, ledger, key, self.root / "remote-sim",
            now=1_793_404_801.0)
        wrong_key = bytes(reversed(range(32)))
        self.assertNotEqual(wrong_key, key)
        wrong_db = self.root / "wrong" / "share.db"
        wrong_ledger = self.root / "wrong" / "usage.jsonl"
        with self.assertRaises(backup_restore.BackupKeyMismatch):
            backup_restore.restore_backup(
                result["backup_path"], wrong_key,
                target_db_path=wrong_db, target_ledger_path=wrong_ledger)
        self._assert_no_targets(wrong_db, wrong_ledger)

        corrupted = self.root / "remote-sim" / "corrupted.umbak"
        data = bytearray(Path(result["backup_path"]).read_bytes())
        data[-1] ^= 0xFF
        corrupted.write_bytes(bytes(data))
        with self.assertRaises(backup_restore.BackupAuthenticationError):
            backup_restore.inspect_backup(corrupted, key)

        truncated = self.root / "remote-sim" / "truncated.umbak"
        truncated.write_bytes(Path(result["backup_path"]).read_bytes()[:10])
        with self.assertRaises(backup_restore.BackupIntegrityError):
            backup_restore.inspect_backup(truncated, key)

        with self.assertRaises(backup_restore.BackupGenerationError):
            backup_restore.inspect_backup(
                result["backup_path"], key,
                require_min_generation=result["config_generation"] + 1)

        target_db = self.root / "restored" / "share.db"
        target_ledger = self.root / "restored" / "usage.jsonl"
        backup_restore.restore_backup(
            result["backup_path"], key,
            target_db_path=target_db, target_ledger_path=target_ledger)
        with self.assertRaises(backup_restore.RestoreTargetExists):
            backup_restore.restore_backup(
                result["backup_path"], key,
                target_db_path=target_db, target_ledger_path=target_ledger)
        self.details.update({
            "wrong_key": "BackupKeyMismatch",
            "corrupted": "BackupAuthenticationError",
            "truncated": "BackupIntegrityError",
            "generation_floor": "BackupGenerationError",
            "repeat_restore": "RestoreTargetExists",
            "config_generation": result["config_generation"],
        })

    def test_restore_rolls_back_when_the_ledger_replace_fails(self):
        """A failure after the database replace must not leave a half-new pair.

        The failure is injected at the SECOND ``os.replace`` (ledger install).
        The database target was already replaced at that point, so the product
        must remove it and restore the pre-restore state. The default
        allow_existing=False case rolls back to "both absent".
        """
        store = self._store()
        alpha = store.create_key("alpha")
        ledger = self._ledger([_usage_record(alpha["key_id"], 0)])
        _key_path, key = self._key()
        result = backup_restore.create_backup(
            store, ledger, key, self.root / "remote-sim",
            now=1_793_404_802.0)
        target_db = self.root / "restored" / "share.db"
        target_ledger = self.root / "restored" / "usage.jsonl"
        real_replace = backup_restore.os.replace
        injected = {"done": False}

        def fail_ledger_install(src, dst):
            if (not injected["done"]
                    and str(dst) == str(target_ledger)
                    and str(src).endswith(".stage")):
                injected["done"] = True
                raise OSError("injected ledger replace failure")
            return real_replace(src, dst)

        with mock.patch.object(backup_restore.os, "replace",
                               side_effect=fail_ledger_install):
            with self.assertRaises(OSError):
                backup_restore.restore_backup(
                    result["backup_path"], key,
                    target_db_path=target_db,
                    target_ledger_path=target_ledger)
        self.assertTrue(injected["done"])
        self._assert_no_targets(target_db, target_ledger)
        self.assertEqual(list(target_db.parent.glob("*.stage")), [])
        self.assertEqual(list(target_db.parent.glob("*restore-backup*")), [])
        self.details.update({"second_replace_failure": "rolled_back"})

    def test_restore_rolls_back_to_old_bytes_when_the_ledger_replace_fails(self):
        """allow_existing=True: a failed second replace restores the old pair.

        Without this, a retry/restore into an existing target could leave the
        database new and the ledger old (or missing) after the failure.
        """
        store = self._store()
        alpha = store.create_key("alpha")
        ledger = self._ledger([_usage_record(alpha["key_id"], 0)])
        _key_path, key = self._key()
        result = backup_restore.create_backup(
            store, ledger, key, self.root / "remote-sim",
            now=1_793_404_803.0)
        target_db = self.root / "restored-existing" / "share.db"
        target_ledger = self.root / "restored-existing" / "usage.jsonl"
        target_db.parent.mkdir(parents=True, exist_ok=True)
        old_db = b"old-database-bytes"
        old_ledger = b"old-ledger-bytes\n"
        target_db.write_bytes(old_db)
        target_ledger.write_bytes(old_ledger)
        real_replace = backup_restore.os.replace
        injected = {"done": False}

        def fail_ledger_install(src, dst):
            if (not injected["done"]
                    and str(dst) == str(target_ledger)
                    and str(src).endswith(".stage")):
                injected["done"] = True
                raise OSError("injected ledger replace failure")
            return real_replace(src, dst)

        with mock.patch.object(backup_restore.os, "replace",
                               side_effect=fail_ledger_install):
            with self.assertRaises(OSError):
                backup_restore.restore_backup(
                    result["backup_path"], key,
                    target_db_path=target_db,
                    target_ledger_path=target_ledger,
                    allow_existing=True)
        self.assertTrue(injected["done"])
        self.assertEqual(target_db.read_bytes(), old_db)
        self.assertEqual(target_ledger.read_bytes(), old_ledger)
        self.details.update({"existing_second_replace_failure":
                             "old_bytes_restored"})


if __name__ == "__main__":
    unittest.main()
