"""r18 deployment assembly: security fence and share-key recovery.

The default assembly no longer contains a budget guard or an encrypted backup
timer.  The broker/console units must still fail closed when the security
table or the console read face is missing, and the explicit recovery path must keep
the AES-GCM share key.

Run from ``ai-gateway``:

    python -m unittest tests.test_cloud_deploy_assembly

Every test is offline: temporary directories stand in for the deployment
layout, ``ShareStore`` is the real product, and no systemd unit, service, R2/OSS
object or production path is touched.
"""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = Path(__file__).resolve().parents[1]      # ai-gateway/
DEPLOY = HERE / "um-cloud-deploy"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(DEPLOY))

import backup_restore  # noqa: E402
from share.store import ShareStore  # noqa: E402

SHARE_SECRET_ENV = "CHAT2API_SHARE_SECRET_KEY_FILE"
AESGCM_AAD = b"aesgcm-file-key-v1"


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def _with_uid(text):
    return text + "\nUM_SHARE_UID=1000\n"


def _env_values(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _backup_key(root):
    path = root / "backup.key"
    backup_restore.write_backup_key(path)
    return path, backup_restore.load_backup_key(path)


@contextlib.contextmanager
def _share_secret_env(value=None):
    """Isolate the process env so an inherited override cannot skew a test."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(SHARE_SECRET_ENV, None)
        if value is not None:
            os.environ[SHARE_SECRET_ENV] = str(value)
        yield


def _deployment_layout(root, name, *, share_secret_key=None):
    """Build a temp layout; console.env is written before the final resolution."""
    base = Path(root) / name

    def paths():
        return backup_restore.deployment_paths(
            current=str(base / "current"),
            etc=str(base / "etc"),
            var=str(base / "var"),
            console_var=str(base / "console"))

    layout = paths()
    for field in ("current", "etc", "var", "console"):
        Path(layout[field]).mkdir(parents=True, exist_ok=True)
    if share_secret_key is not None:
        Path(layout["etc"], "console.env").write_text(
            f"{SHARE_SECRET_ENV}={share_secret_key}\n", encoding="utf-8")
        layout = paths()
    return layout


def _write_secret(path, material):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(material)
    os.chmod(target, 0o600)
    return target


def _seal_secret(key, secret):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(
        nonce, secret.encode("utf-8"), AESGCM_AAD)


def _open_secret(key, blob):
    return AESGCM(key).decrypt(
        blob[:12], blob[12:], AESGCM_AAD).decode("utf-8")


def _aesgcm_store(path, share_key, secret="synthetic-secret"):
    """Real ShareStore schema with one AES-GCM row (no DPAPI dependency)."""
    store = ShareStore(path)
    key_id = "synthetic-aesgcm-key"
    now = "2026-11-01T00:00:00.000000Z"
    try:
        blob = _seal_secret(share_key, secret)
        store._conn.execute(
            "INSERT INTO keys(key_id,name,secret_hash,secret_version,"
            "masked_key,secret_blob,secret_algo,enabled,max_concurrency,"
            "created_at,updated_at,deleted_at,row_version)"
            " VALUES(?,?,?,1,?,?,?,1,1,?,?,NULL,1)",
            (key_id, "synthetic-aesgcm",
             hashlib.sha256(secret.encode("utf-8")).hexdigest(),
             "um-share-****test", sqlite3.Binary(blob),
             "aesgcm-file-key-v1", now, now))
        store._conn.commit()
        security = store.security_state()
    finally:
        store.close()
    return {"key_id": key_id, "secret": secret,
            "blob": blob, "security": security}


def _source_layout(root, share_key, *, share_secret_path=None,
                    include_share_secret=True):
    layout = _deployment_layout(
        root, "source", share_secret_key=share_secret_path)
    synthetic = _aesgcm_store(layout["store"], share_key)
    Path(layout["ledger"]).parent.mkdir(parents=True, exist_ok=True)
    Path(layout["ledger"]).write_text("", encoding="utf-8")
    Path(layout["config"]).write_text(
        json.dumps({"schema_version": 2, "enabled": False}), encoding="utf-8")
    backup_restore.write_security_state(
        layout["security_state"], synthetic["security"])
    backup_restore.write_backup_key(Path(layout["key_file"]))
    backup_key = backup_restore.load_backup_key(Path(layout["key_file"]))
    if include_share_secret:
        _write_secret(layout["share_secret_key"], share_key)
    return layout, synthetic, backup_key


def _destination_layout(root, security, *, share_secret_path=None):
    layout = _deployment_layout(
        root, "destination", share_secret_key=share_secret_path)
    Path(layout["store"]).write_bytes(b"synthetic-destination-sentinel")
    backup_restore.write_backup_key(Path(layout["key_file"]))
    backup_restore.write_security_state(layout["security_state"], security)
    return layout


def _seal_deployment_backup(layout, backup_key):
    store = ShareStore(layout["store"])
    try:
        return backup_restore.create_backup(
            store, layout["ledger"], backup_key,
            Path(layout["remote_dir"]),
            config_paths=[layout["config"]],
            security_state=backup_restore.read_security_state(
                layout["security_state"]),
            share_secret_key_path=layout["share_secret_key"])
    finally:
        store.close()


class DeploymentTextAssemblyTests(unittest.TestCase):
    def test_broker_unit_has_no_price_fence_or_budget_args(self):
        unit = _read("systemd/um-share-broker.service")
        self.assertNotIn("CHAT2API_PRICE_TABLE_FILE", unit)
        self.assertNotIn("prices.json", unit)
        self.assertIn("--check-deployment-assembly broker", unit)
        self.assertIn("ExecStartPre=", unit)
        self.assertNotIn("--budget-db", unit)
        self.assertNotIn("--budget-config", unit)
        self.assertNotIn("budget.json", unit)

    def test_console_env_wires_the_security_read_face(self):
        unit = _read("systemd/um-share-console.service")
        env = _with_uid(_read("config/console.env.example"))
        values = _env_values(env)
        for key in ("UM_CLOUD_SECURITY_STATE",
                     "CHAT2API_SHARE_SECRET_KEY_FILE"):
            self.assertTrue(values.get(key), key)
        for key in ("UM_CLOUD_BUDGET_DB", "UM_CLOUD_BACKUP_STATE",
                     "UM_CLOUD_BACKUP_REMOTE_DIR"):
            self.assertNotIn(key, values)
        self.assertIn("--check-deployment-assembly console", unit)
        self.assertIn("ExecStartPre=", unit)
        self.assertEqual(
            backup_restore.deployment_assembly_problems("console", unit, env), [])

    def test_broker_env_satisfies_the_start_check(self):
        unit = _read("systemd/um-share-broker.service")
        env = _with_uid(_read("config/broker.env.example"))
        self.assertEqual(
            backup_restore.deployment_assembly_problems("broker", unit, env), [])

    def test_console_env_matches_the_deployment_layout(self):
        values = _env_values(_read("config/console.env.example"))
        paths = backup_restore.deployment_paths()
        self.assertEqual(values["UM_CLOUD_SECURITY_STATE"], paths["security_state"])
        for key in ("USAGE_PRICE_FILE", "CHAT2API_PRICE_TABLE_FILE",
                    "MAGPIE_PRICE_SNAPSHOT", "MAGPIE_PRICE_STATUS"):
            self.assertNotIn(key, values)

    def test_console_env_names_the_cloudflare_origin_and_browser_path(self):
        values = _env_values(_read("config/console.env.example"))
        self.assertEqual("https://dacintapi.de5.net",
                         values["CONSOLE_PUBLIC_ORIGIN"])
        self.assertTrue(values["CHAT2API_BROWSER_PATH"])
        self.assertEqual("0", values["CHAT2API_SHARE_DISPATCH_MAX_WAIT_S"])

    def test_missing_security_state_fails_the_console_start_check(self):
        console_unit = _read("systemd/um-share-console.service")
        console_env = _with_uid(_read("config/console.env.example"))
        missing = "\n".join(
            line for line in console_env.splitlines()
            if not line.startswith("UM_CLOUD_SECURITY_STATE="))
        problems = backup_restore.deployment_assembly_problems(
            "console", console_unit, missing)
        self.assertTrue(any("UM_CLOUD_SECURITY_STATE" in item
                            for item in problems))
        with tempfile.TemporaryDirectory(prefix="assembly-security-") as tmp:
            root = Path(tmp)
            unit_path = root / "console.service"
            env_path = root / "console.env"
            unit_path.write_text(console_unit, encoding="utf-8")
            env_path.write_text(missing, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(DEPLOY / "backup_restore.py"),
                 "--check-deployment-assembly", "console",
                 str(unit_path), str(env_path)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])

    def test_cli_start_check_refuses_a_broker_unit_without_env_file(self):
        unit = _read("systemd/um-share-broker.service")
        broken = "\n".join(
            line for line in unit.splitlines()
            if "EnvironmentFile=" not in line)
        env = _with_uid(_read("config/broker.env.example"))
        with tempfile.TemporaryDirectory(prefix="assembly-cli-") as tmp:
            root = Path(tmp)
            unit_path = root / "broker.service"
            env_path = root / "broker.env"
            unit_path.write_text(broken, encoding="utf-8")
            env_path.write_text(env, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(DEPLOY / "backup_restore.py"),
                 "--check-deployment-assembly", "broker",
                 str(unit_path), str(env_path)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["problems"])
            unit_path.write_text(unit, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(DEPLOY / "backup_restore.py"),
                 "--check-deployment-assembly", "broker",
                 str(unit_path), str(env_path)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_scripts_have_no_default_backup_or_budget_dependency(self):
        install = _read("install.sh")
        update = _read("update.sh")
        rollback = _read("rollback.sh")
        for text in (install, update, rollback):
            self.assertNotIn("--deployment-backup", text)
            self.assertNotIn("run_backup_checkpoint", text)
            self.assertNotIn("--print-systemd-units", text)
            self.assertIn("--deployment-restore", text)
            self.assertIn("--recover-backup", text)
            self.assertIn("--recover-security-state", text)
            self.assertIn('if [ -n "$RECOVER_BACKUP" ]', text)
        self.assertNotIn("--budget-config", install)
        self.assertNotIn("budget.json", install)
        self.assertNotIn("um-share-backup.timer", install)
        self.assertNotIn("backup.key", install)
        self.assertNotIn("--price-table", install)
        self.assertIn("check_deployment_assembly", install)
        self.assertNotIn("--no-backup", update)
        self.assertNotIn("--no-backup", rollback)
        # A normal update/rollback never restores data; the explicit recovery
        # branch is the only caller of run_recovery.
        self.assertLess(
            update.index('log "explicit recovery: restoring'),
            update.index('log "switching current'))
        self.assertLess(
            rollback.index('log "explicit recovery: restoring'),
            rollback.index('if ! atomic_symlink "$TARGET" "$CURRENT_LINK"'))

    def test_install_offers_the_cloudflared_edge_opt_in(self):
        install = _read("install.sh")
        self.assertIn("--with-cloudflared", install)
        self.assertIn("WITH_CLOUDFLARED", install)
        self.assertIn("cloudflared.service", install)
        self.assertIn("cloudflared.yml", install)
        self.assertIn("CONSOLE_PUBLIC_ORIGIN", install)
        self.assertIn("CHAT2API_BROWSER_PATH", install)
        self.assertIn("CHAT2API_SHARE_DISPATCH_MAX_WAIT_S", install)
        self.assertTrue((DEPLOY / "config" / "cloudflared.yml.example").is_file())
        self.assertTrue((DEPLOY / "systemd" / "cloudflared.service").is_file())
        self.assertTrue((DEPLOY / "cloudflared" / "check-ingress.py").is_file())

    def test_release_sidecar_is_relative_and_the_sync_cli_is_executable(self):
        text = _read("package-release.sh")
        self.assertIn('(cd "$OUTPUT_DIR" && sha256sum '
                      '"$(basename "$ARCHIVE")")', text)
        self.assertNotIn('sha256sum "$ARCHIVE" >', text)
        self.assertNotIn("magpie_prices.py", text)

    def test_release_packaging_requires_a_production_console_line(self):
        # usagesrv.py answers 500 for every line other than production, so a
        # release package must not carry a candidate preview tree.
        package = _read("package-release.sh")
        self.assertIn("--require-web-line production", package)
        verify = _read("verify-release.ps1")
        self.assertIn("$WebLine = 'production'", verify)
        self.assertIn("web_dist.build.line", verify)

    def test_readme_points_to_the_manifest_for_counts(self):
        readme = _read("README.md")
        self.assertIn("RELEASE-MANIFEST.json", readme,
                      "the README must name the manifest as the count source")


class DeploymentShareSecretRecoveryTests(unittest.TestCase):
    """R5/R6: one actual share key, and recovery refuses unsafe packages."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="deploy-share-secret-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(_share_secret_env())
        self.share_key = secrets.token_bytes(32)

    def test_deployment_paths_uses_the_effective_share_secret_key(self):
        # With no console.env line the running console uses the product default:
        # <current>/data/share-secret.key, not a different console-state path.
        layout = _deployment_layout(self.root, "precedence")
        self.assertEqual(
            Path(layout["share_secret_key"]),
            Path(layout["current"]) / "data" / "share-secret.key")

        console_key = self.root / "configured" / "share-secret.key"
        _write_secret(console_key, b"c" * 32)
        layout = _deployment_layout(
            self.root, "precedence", share_secret_key=console_key)
        self.assertEqual(Path(layout["share_secret_key"]), console_key)

        env_key = self.root / "environment" / "share-secret.key"
        _write_secret(env_key, b"e" * 32)
        with _share_secret_env(env_key):
            layout = _deployment_layout(
                self.root, "precedence", share_secret_key=console_key)
            self.assertEqual(Path(layout["share_secret_key"]), env_key)

    def test_deployment_backup_and_restore_keep_the_configured_share_secret(self):
        custom = self.root / "source-config" / "share-secret.key"
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key, share_secret_path=custom)
        self.assertEqual(Path(source["share_secret_key"]), custom)
        default = Path(source["console"]) / "share-secret.key"
        _write_secret(default, secrets.token_bytes(32))

        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        self.assertTrue(backup["ok"])
        self.assertTrue(backup["share_secret_present"])
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        inspected = backup_restore.inspect_backup(archive, backup_key)
        self.assertEqual(
            inspected["extra_files"]["share-secret.key"], self.share_key)

        destination_custom = (
            self.root / "destination-config" / "share-secret.key")
        destination = _destination_layout(
            self.root, synthetic["security"],
            share_secret_path=destination_custom)
        destination_default = (
            Path(destination["console"]) / "share-secret.key")
        unrelated = secrets.token_bytes(32)
        _write_secret(destination_default, unrelated)

        restored = backup_restore.deployment_restore(
            archive, key=backup_key, paths=destination)

        self.assertTrue(restored["share_secret_restored"])
        self.assertEqual(restored["share_secret_verified_rows"], 1)
        self.assertEqual(Path(destination["share_secret_key"]),
                         destination_custom)
        self.assertEqual(destination_custom.read_bytes(), self.share_key)
        self.assertEqual(destination_default.read_bytes(), unrelated)
        self.assertEqual(restored["security_floor"],
                         synthetic["security"]["generation_floor"])
        conn = sqlite3.connect(str(destination["store"]))
        try:
            row = conn.execute(
                "SELECT secret_blob FROM keys WHERE key_id=?",
                (synthetic["key_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            _open_secret(self.share_key, bytes(row[0])), synthetic["secret"])

    def test_deployment_restore_hands_the_share_secret_to_the_console_owner(self):
        # The recovery runs as root but the console reads the key as its own
        # user. The restored custom-path key must be chowned/chmodded before
        # it replaces the live target, or every restored Key is unreadable.
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        destination = _destination_layout(self.root, synthetic["security"])
        destination_key = Path(destination["share_secret_key"])
        _write_secret(destination_key, secrets.token_bytes(32))
        calls = []

        def fake_fchown(fd, uid, gid):
            calls.append((uid, gid))

        with mock.patch.object(backup_restore.os, "fchown", fake_fchown,
                               create=True):
            restored = backup_restore.deployment_restore(
                archive, key=backup_key, paths=destination,
                share_secret_owner=1234, share_secret_group=2345)

        self.assertTrue(restored["share_secret_restored"])
        self.assertEqual(calls, [(1234, 2345)])
        if os.name == "posix":
            self.assertEqual(
                stat.S_IMODE(destination_key.stat().st_mode) & 0o777, 0o600)

    def test_deployment_restore_owns_the_configured_custom_key_path(self):
        # A custom console.env path is the running console's effective key.  The
        # restore must write there (not the console-state default) and hand the
        # restored file to the console uid/gid install.sh passes on recovery.
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        destination_custom = (
            self.root / "destination-config" / "share-secret.key")
        destination = _destination_layout(
            self.root, synthetic["security"],
            share_secret_path=destination_custom)
        _write_secret(destination_custom, secrets.token_bytes(32))
        calls = []

        def fake_fchown(fd, uid, gid):
            calls.append((uid, gid))

        with mock.patch.object(backup_restore.os, "fchown", fake_fchown,
                               create=True):
            restored = backup_restore.deployment_restore(
                archive, key=backup_key, paths=destination,
                share_secret_owner=1234, share_secret_group=2345)

        self.assertTrue(restored["share_secret_restored"])
        self.assertEqual(Path(destination["share_secret_key"]),
                         destination_custom)
        self.assertEqual(destination_custom.read_bytes(), self.share_key)
        self.assertEqual(calls, [(1234, 2345)])

    @unittest.skipUnless(os.name == "posix",
                         "real POSIX ownership is verified on Linux")
    def test_deployment_restore_sets_a_real_posix_share_secret_owner(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        destination = _destination_layout(self.root, synthetic["security"])
        destination_key = Path(destination["share_secret_key"])
        _write_secret(destination_key, secrets.token_bytes(32))

        backup_restore.deployment_restore(
            archive, key=backup_key, paths=destination,
            share_secret_owner=os.getuid(),
            share_secret_group=os.getgid())

        info = destination_key.stat()
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_gid, os.getgid())
        self.assertEqual(stat.S_IMODE(info.st_mode) & 0o777, 0o600)

    def test_deployment_restore_preserves_an_existing_share_secret_owner(self):
        # A live key already owned by the console user keeps that owner even
        # when the caller supplies no explicit uid/gid.
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        destination = _destination_layout(self.root, synthetic["security"])
        destination_key = Path(destination["share_secret_key"])
        _write_secret(destination_key, secrets.token_bytes(32))
        live = destination_key.stat()
        calls = []

        def fake_fchown(fd, uid, gid):
            calls.append((uid, gid))

        with mock.patch.object(backup_restore.os, "fchown", fake_fchown,
                               create=True):
            backup_restore.deployment_restore(
                archive, key=backup_key, paths=destination)

        self.assertEqual(calls, [(live.st_uid, live.st_gid)])

    def test_deployment_restore_refuses_a_backup_without_the_share_secret(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key, include_share_secret=False)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=False)
        self.assertFalse(backup["share_secret_present"])
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))

        destination = _destination_layout(self.root, synthetic["security"])
        destination_key = Path(destination["share_secret_key"])
        unrelated = secrets.token_bytes(32)
        _write_secret(destination_key, unrelated)
        before = Path(destination["store"]).read_bytes()

        with self.assertRaises(backup_restore.BackupError):
            backup_restore.deployment_restore(
                archive, key=backup_key, paths=destination)

        self.assertEqual(Path(destination["store"]).read_bytes(), before)
        self.assertEqual(destination_key.read_bytes(), unrelated)

    def test_deployment_restore_refuses_a_wrong_backup_key(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=True)
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))

        destination = _destination_layout(self.root, synthetic["security"])
        before = Path(destination["store"]).read_bytes()

        with self.assertRaises(backup_restore.BackupKeyMismatch):
            backup_restore.deployment_restore(
                archive, key=secrets.token_bytes(32), paths=destination)

        self.assertEqual(Path(destination["store"]).read_bytes(), before)

    def test_deployment_backup_refuses_a_wrong_share_secret(self):
        # A package that names a share key must prove it opens THIS snapshot's
        # AES-GCM rows before the backup is sealed.  Checking only that the
        # file exists would report success for a backup whose restored store is
        # unreadable.
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key,
            share_secret_path=self.root / "source-config" / "share-secret.key")
        _write_secret(source["share_secret_key"], secrets.token_bytes(32))
        with self.assertRaises(backup_restore.BackupKeyError):
            backup_restore.deployment_backup(
                paths=source, key=backup_key, require_share_secret=True)
        self.assertEqual(
            [], list(Path(source["remote_dir"]).rglob("*.umbak")))

    def test_deployment_restore_refuses_a_wrong_share_secret(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key,
            share_secret_path=self.root / "source-config" / "share-secret.key")
        _write_secret(source["share_secret_key"], secrets.token_bytes(32))
        # Simulate a package produced by a lower-level caller before the
        # backup-time verification existed: restore must still refuse it.
        with mock.patch.object(
                backup_restore, "_verify_share_secret_decrypts_store",
                return_value=0):
            backup = backup_restore.deployment_backup(
                paths=source, key=backup_key, require_share_secret=True)
        self.assertTrue(backup["share_secret_present"])
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))

        destination = _destination_layout(self.root, synthetic["security"])
        destination_key = Path(destination["share_secret_key"])
        unrelated = secrets.token_bytes(32)
        _write_secret(destination_key, unrelated)
        before = Path(destination["store"]).read_bytes()

        with self.assertRaises(backup_restore.BackupKeyError):
            backup_restore.deployment_restore(
                archive, key=backup_key, paths=destination)

        self.assertEqual(Path(destination["store"]).read_bytes(), before)
        self.assertEqual(destination_key.read_bytes(), unrelated)

    def test_cli_deployment_restore_refuses_a_backup_without_share_secret(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key, include_share_secret=False)
        backup = backup_restore.deployment_backup(
            paths=source, key=backup_key, require_share_secret=False)
        self.assertFalse(backup["share_secret_present"])
        archive = next(Path(source["remote_dir"]).rglob("*.umbak"))
        destination = _destination_layout(self.root, synthetic["security"])
        before = Path(destination["store"]).read_bytes()

        proc = subprocess.run(
            [sys.executable, str(DEPLOY / "backup_restore.py"),
             "--deployment-restore", "--restore-backup", str(archive),
             "--restore-security-state", destination["security_state"],
             "--deployment-current", destination["current"],
             "--deployment-etc", destination["etc"],
             "--deployment-var", destination["var"],
             "--deployment-console-var", destination["console"]],
            capture_output=True, text=True)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["ok"])
        self.assertEqual(Path(destination["store"]).read_bytes(), before)

    def test_deployment_restore_refuses_incomplete_material_before_replacing(self):
        source, synthetic, backup_key = _source_layout(
            self.root, self.share_key)
        normal = _seal_deployment_backup(source, backup_key)

        missing_state = _destination_layout(
            self.root / "security-state", synthetic["security"])
        Path(missing_state["security_state"]).unlink()
        before = Path(missing_state["store"]).read_bytes()
        with self.assertRaises(backup_restore.BackupSecurityError):
            backup_restore.deployment_restore(
                normal["backup_path"], key=backup_key,
                paths=missing_state)
        self.assertEqual(Path(missing_state["store"]).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
