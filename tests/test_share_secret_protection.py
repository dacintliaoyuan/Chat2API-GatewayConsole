"""Linux share-key secret protection + strict algorithm dispatch.

Windows keeps the R11 DPAPI contract (round-trip and graceful degradation
are covered by ``test_share_store_key_reveal``); this file adds:

* the POSIX AES-256-GCM scheme: round-trip, wrong key, damaged ciphertext,
  key-file contract (32 raw bytes / 64 hex, mode 0600, current-uid owner,
  missing/damaged files refused) and the store's row-level algorithm marker;
* strict dispatch: an unknown ``secret_algo`` raises, a DPAPI row on POSIX
  raises instead of returning an empty string, and a hash-only row still
  verifies its original key;
* the lazy ``cryptography`` import: importing ``share_store`` works without
  the package, and the AES-GCM path answers a clear protection error.

Linux-only cases are collected and skipped on Windows.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_secret_protection as protection  # noqa: E402
import share_store  # noqa: E402
from share_store import (SecretUndecryptable, SecretUnavailable,  # noqa: E402
                         ShareStore, StoreError)

_IS_LINUX = sys.platform.startswith("linux")
_IS_POSIX = os.name == "posix"
_LEGACY_SECRET = "legacy-secret-0123456789abcdef"
_LEGACY_DIGEST = hashlib.sha256(
    _LEGACY_SECRET.encode("utf-8")).hexdigest()
_MISSING = object()


class _EnvKeyFileMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                               prefix="share-secret-")
        self.addCleanup(self.tmp.cleanup)
        self._saved_env = os.environ.get(protection.KEY_FILE_ENV, _MISSING)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved_env is _MISSING:
            os.environ.pop(protection.KEY_FILE_ENV, None)
        else:
            os.environ[protection.KEY_FILE_ENV] = self._saved_env

    def write_key(self, name="share-secret.key", material=None,
                  mode=0o600):
        path = Path(self.tmp.name) / name
        path.write_bytes(material if material is not None
                          else os.urandom(protection.KEY_SIZE))
        if _IS_POSIX:
            os.chmod(path, mode)
        os.environ[protection.KEY_FILE_ENV] = str(path)
        return path


class PrivateSecretFileTests(unittest.TestCase):
    """POSIX 0600/current-uid gate for control/instance/attribution/
    control-token/session-code secrets (Q11/R-posix secret boundary)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                               prefix="private-secret-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_roundtrip_and_basename_only(self):
        path = self.directory / "control-token"
        protection.write_private_secret_path(path, b"secret-value")
        self.assertEqual(
            protection.read_private_secret_path(path), b"secret-value")
        with self.assertRaises(ValueError):
            protection.read_private_secret_file(
                str(self.directory), "..", label="secret")
        with self.assertRaises(ValueError):
            protection.read_private_secret_file(
                str(self.directory), "../outside", label="secret")
        with self.assertRaises(ValueError):
            protection.read_private_secret_file(
                "relative-dir", "secret", label="secret")

    @unittest.skipUnless(_IS_POSIX, "mode/owner gate requires POSIX")
    def test_wide_mode_is_refused_and_write_forces_0600(self):
        path = self.directory / "attribution-key"
        path.write_bytes(b"secret-value")
        os.chmod(path, 0o644)
        with self.assertRaises(protection.SecretProtectionError):
            protection.read_private_secret_path(path)
        protection.write_private_secret_path(path, b"new-secret")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(
            protection.read_private_secret_path(path), b"new-secret")


class DispatchTests(unittest.TestCase):
    def test_scheme_markers_are_stable(self):
        self.assertEqual(share_store.SECRET_ALGO_DPAPI_USER,
                         "dpapi-user-scope-v1")
        self.assertEqual(protection.SECRET_ALGO_AESGCM_FILE_KEY,
                         "aesgcm-file-key-v1")

    def test_default_key_file_is_data_share_secret_key(self):
        saved = os.environ.pop(protection.KEY_FILE_ENV, _MISSING)
        try:
            self.assertEqual(
                protection.default_key_file(),
                Path(protection.__file__).resolve().parent
                / "data" / "share-secret.key")
        finally:
            if saved is not _MISSING:
                os.environ[protection.KEY_FILE_ENV] = saved

    def test_env_override_selects_key_file(self):
        saved = os.environ.get(protection.KEY_FILE_ENV, _MISSING)
        os.environ[protection.KEY_FILE_ENV] = "custom/share-secret.key"
        try:
            self.assertEqual(protection.default_key_file(),
                             Path("custom/share-secret.key"))
        finally:
            if saved is _MISSING:
                os.environ.pop(protection.KEY_FILE_ENV, None)
            else:
                os.environ[protection.KEY_FILE_ENV] = saved

    def test_unknown_algo_raises_instead_of_guessing(self):
        blob = b"x" * (protection.NONCE_SIZE + protection.TAG_SIZE)
        with self.assertRaises(protection.SecretProtectionError):
            protection.unprotect_secret(blob, "aes-256-gcm-v0")
        with self.assertRaises(StoreError):
            share_store.unprotect_secret(blob, "aes-256-gcm-v0")

    def test_module_imports_without_cryptography(self):
        # Windows has no cryptography package; a subprocess blocks it even
        # where it is installed. Importing share_store must still work
        # without the POSIX adapter (Windows copy closures may predate it),
        # and the AES-GCM path must answer a clear protection error,
        # never ImportError.
        code = (
            "import sys\n"
            "sys.modules['cryptography'] = None\n"
            "sys.modules['share_secret_protection'] = None\n"
            "import share_store\n"
            "print(share_store.SECRET_ALGO_DPAPI_USER)\n"
            "sys.modules.pop('share_secret_protection')\n"
            "import share_secret_protection as p\n"
            "print(p.SECRET_ALGO_AESGCM_FILE_KEY)\n"
            "try:\n"
            "    p.protect_secret_aesgcm('um-share-probe')\n"
            "except p.SecretProtectionError:\n"
            "    print('blocked-ok')\n")
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dpapi-user-scope-v1", result.stdout)
        self.assertIn("aesgcm-file-key-v1", result.stdout)
        self.assertIn("blocked-ok", result.stdout)


class LegacyRowTests(unittest.TestCase):
    """Hash-only rows stay verifiable and unreadable on every platform."""

    def test_hash_only_row_still_verifies_and_stays_unavailable(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                          prefix="share-secret-legacy-")
        self.addCleanup(tmp.cleanup)
        store = ShareStore(Path(tmp.name) / "share.db")
        self.addCleanup(store.close)
        store.migrate_apply({"friend1": _LEGACY_DIGEST})
        row = store.list_keys()[0]
        self.assertFalse(row["plaintext_viewable"])
        with self.assertRaises(SecretUnavailable):
            store.reveal_key_secret(row["key_id"])
        self.assertTrue(store.verify_secret(row["key_id"], _LEGACY_SECRET))


@unittest.skipUnless(_IS_LINUX,
                     "POSIX AES-GCM/key-file contract is Linux-only")
class AesGcmTests(_EnvKeyFileMixin):
    def _store(self, name="share.db"):
        path = Path(self.tmp.name) / name
        store = ShareStore(path)
        self.addCleanup(store.close)
        return store, path

    @staticmethod
    def _row_algo(path, key_id):
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute(
                "SELECT secret_algo FROM keys WHERE key_id=?",
                (key_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_raw_key_roundtrip_and_platform_dispatch(self):
        self.write_key()
        blob = share_store.protect_secret("um-share-roundtrip")
        self.assertNotIn(b"um-share-roundtrip", blob)
        self.assertEqual(protection.unprotect_secret_aesgcm(blob),
                         "um-share-roundtrip")
        self.assertEqual(protection.unprotect_secret(
            blob, protection.SECRET_ALGO_AESGCM_FILE_KEY),
            "um-share-roundtrip")
        self.assertEqual(share_store.unprotect_secret(
            blob, protection.SECRET_ALGO_AESGCM_FILE_KEY),
            "um-share-roundtrip")

    def test_hex_key_file_is_accepted(self):
        self.write_key(material=os.urandom(protection.KEY_SIZE).hex().encode())
        blob = protection.protect_secret_aesgcm("um-share-hex-key")
        self.assertEqual(protection.unprotect_secret_aesgcm(blob),
                         "um-share-hex-key")

    def test_key_file_with_trailing_newline_is_accepted(self):
        self.write_key(material=os.urandom(protection.KEY_SIZE).hex().encode()
                        + b"\n")
        blob = protection.protect_secret_aesgcm("um-share-newline")
        self.assertEqual(protection.unprotect_secret_aesgcm(blob),
                         "um-share-newline")

    def test_store_create_writes_aesgcm_marker_and_reveals(self):
        self.write_key()
        store, path = self._store()
        created = store.create_key("alpha")
        self.assertTrue(created["plaintext_viewable"])
        self.assertEqual(self._row_algo(path, created["key_id"]),
                         protection.SECRET_ALGO_AESGCM_FILE_KEY)
        self.assertEqual(store.reveal_key_secret(created["key_id"])["secret"],
                         created["secret"])
        rotated = store.rotate_key(created["key_id"])
        self.assertTrue(rotated["plaintext_viewable"])
        self.assertEqual(self._row_algo(path, created["key_id"]),
                         protection.SECRET_ALGO_AESGCM_FILE_KEY)
        self.assertEqual(store.reveal_key_secret(created["key_id"])["secret"],
                         rotated["secret"])

    def test_wrong_key_fails_closed(self):
        self.write_key(name="first.key", material=os.urandom(32))
        blob = protection.protect_secret_aesgcm("um-share-wrong-key")
        self.write_key(name="second.key", material=os.urandom(32))
        with self.assertRaises(protection.SecretProtectionError):
            protection.unprotect_secret_aesgcm(blob)

        store, path = self._store()
        self.write_key(name="first.key", material=os.urandom(32))
        created = store.create_key("bravo")
        self.write_key(name="second.key", material=os.urandom(32))
        with self.assertRaises(SecretUndecryptable):
            store.reveal_key_secret(created["key_id"])
        # The key itself stays valid; only the plaintext is unreachable and
        # the row is NOT downgraded to hash-only.
        self.assertTrue(store.verify_secret(created["key_id"],
                                           created["secret"]))
        self.assertTrue(store.get_key(created["key_id"])["plaintext_viewable"])
        self.assertEqual(self._row_algo(path, created["key_id"]),
                         protection.SECRET_ALGO_AESGCM_FILE_KEY)

    def test_damaged_ciphertext_fails_closed(self):
        self.write_key()
        blob = protection.protect_secret_aesgcm("um-share-damaged")
        for damaged in (blob[:-1] + bytes([blob[-1] ^ 0xFF]),
                        blob[:protection.NONCE_SIZE + 4],
                        b""):
            with self.subTest(damaged=damaged):
                with self.assertRaises(protection.SecretProtectionError):
                    protection.unprotect_secret_aesgcm(damaged)

        store, path = self._store()
        created = store.create_key("charlie")
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "UPDATE keys SET secret_blob=? WHERE key_id=?",
                (sqlite3.Binary(b"damaged-ciphertext"), created["key_id"]))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SecretUndecryptable):
            store.reveal_key_secret(created["key_id"])

    def test_missing_key_file_aborts_the_mutation(self):
        missing = Path(self.tmp.name) / "absent.key"
        os.environ[protection.KEY_FILE_ENV] = str(missing)
        with self.assertRaises(protection.SecretProtectionError):
            protection.load_key_file()
        store, _ = self._store()
        before = store.generation()["config_generation"]
        with self.assertRaises(StoreError):
            store.create_key("delta")
        self.assertEqual(store.list_keys(), [])
        self.assertEqual(store.generation()["config_generation"], before)

    def test_key_file_permissions_must_be_0600(self):
        path = self.write_key(mode=0o644)
        with self.assertRaises(protection.SecretProtectionError) as ctx:
            protection.load_key_file()
        self.assertIn("0600", str(ctx.exception))

        store, _ = self._store()
        with self.assertRaises(StoreError):
            store.create_key("echo")
        self.assertEqual(store.list_keys(), [])

        os.chmod(path, 0o600)
        created = store.create_key("echo")
        self.assertEqual(store.reveal_key_secret(created["key_id"])["secret"],
                         created["secret"])

    def test_key_file_owner_must_be_current_uid(self):
        self.write_key()
        with mock.patch.object(os, "getuid",
                               return_value=os.getuid() + 1):
            with self.assertRaises(protection.SecretProtectionError) as ctx:
                protection.load_key_file()
        self.assertIn("owned by the current uid", str(ctx.exception))

    def test_bad_key_material_is_rejected(self):
        for material in (b"short-key", os.urandom(31),
                         b"a" * 63, b"z" * 64,
                         b"a" * 62 + b" " + b"a"):
            with self.subTest(size=len(material)):
                self.write_key(name="bad.key", material=material)
                with self.assertRaises(protection.SecretProtectionError):
                    protection.load_key_file()

    def test_removed_key_file_makes_a_readable_row_undecryptable(self):
        path = self.write_key()
        store, _ = self._store()
        created = store.create_key("foxtrot")
        self.assertTrue(store.get_key(created["key_id"])["plaintext_viewable"])
        path.unlink()
        with self.assertRaises(SecretUndecryptable):
            store.reveal_key_secret(created["key_id"])
        # Not a silent hash-only downgrade: the row still claims and keeps
        # its ciphertext, and the hash contract still verifies the key.
        self.assertTrue(store.get_key(created["key_id"])["plaintext_viewable"])
        self.assertTrue(store.verify_secret(created["key_id"],
                                           created["secret"]))


@unittest.skipUnless(_IS_POSIX,
                     "DPAPI rows cannot be decrypted outside Windows")
class DpapiOnPosixTests(_EnvKeyFileMixin):
    def test_dpapi_row_answers_a_clear_undecryptable_error(self):
        self.write_key()
        path = Path(self.tmp.name) / "share.db"
        store = ShareStore(path)
        self.addCleanup(store.close)
        created = store.create_key("golf")
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "UPDATE keys SET secret_blob=?, secret_algo=? WHERE key_id=?",
                (sqlite3.Binary(b"dpapi-user-scope-ciphertext"),
                 share_store.SECRET_ALGO_DPAPI_USER, created["key_id"]))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SecretUndecryptable) as ctx:
            store.reveal_key_secret(created["key_id"])
        self.assertIn("dpapi", str(ctx.exception.__cause__).lower())
        with self.assertRaises(StoreError):
            share_store.unprotect_secret(
                b"dpapi-user-scope-ciphertext",
                share_store.SECRET_ALGO_DPAPI_USER)
        # A POSIX runtime must not assume DPAPI when no algorithm is given.
        with self.assertRaises(StoreError):
            share_store.unprotect_secret(b"dpapi-user-scope-ciphertext")


@unittest.skipUnless(sys.platform == "win32",
                     "Windows DPAPI regression is Windows-only")
class WindowsDpapiTests(unittest.TestCase):
    def test_new_rows_keep_the_dpapi_contract(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                          prefix="share-secret-dpapi-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "share.db"
        store = ShareStore(path)
        self.addCleanup(store.close)
        created = store.create_key("hotel")
        self.assertTrue(created["plaintext_viewable"])
        conn = sqlite3.connect(str(path))
        try:
            blob, algo = conn.execute(
                "SELECT secret_blob, secret_algo FROM keys WHERE key_id=?",
                (created["key_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(algo, share_store.SECRET_ALGO_DPAPI_USER)
        self.assertEqual(share_store.unprotect_secret(bytes(blob), algo),
                         created["secret"])
        # The R11 direct-call form stays valid on Windows.
        self.assertEqual(share_store.unprotect_secret(bytes(blob)),
                         created["secret"])


if __name__ == "__main__":
    unittest.main()
