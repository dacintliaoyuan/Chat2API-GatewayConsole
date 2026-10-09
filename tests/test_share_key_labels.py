"""Expense labels are a read-only projection, never a management command."""
from contextlib import closing
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import share_store


class ReadOnlyKeyLabelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="share-labels-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "name with space and #.db"
        # Legacy-compatible minimal schema; there is deliberately no meta
        # table, so constructing ShareStore would mutate or migrate this DB.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE keys (key_id TEXT PRIMARY KEY, name TEXT, "
                         "deleted_at TEXT, secret_hash TEXT)")
            conn.executemany("INSERT INTO keys VALUES (?,?,?,?)", [
                ("a", "当前名称", None, "private-digest"),
                ("b", "删除前名称", "2026-10-07", "deleted-digest"),
                ("x') OR 1=1 --", "带引号", None, "never-select-this"),
            ])

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_only_requested_public_columns_and_deleted_name_are_read(self):
        before = self.digest()
        actual = share_store.read_key_labels(self.path, ["a", "b", "unknown"])
        self.assertEqual(actual, {
            "a": {"key_id": "a", "name": "当前名称", "deleted_at": None},
            "b": {"key_id": "b", "name": "删除前名称", "deleted_at": "2026-10-07"}})
        self.assertEqual(self.digest(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         [self.path.name])

    def test_missing_store_does_not_create_file_or_parent(self):
        absent = self.path.parent / "not-created" / "store.db"
        with self.assertRaises(share_store.StoreError):
            share_store.read_key_labels(absent, ["a"])
        self.assertFalse(absent.parent.exists())

    def test_damaged_store_is_unknown_not_empty(self):
        self.path.write_bytes(b"not sqlite")
        with self.assertRaises(share_store.StoreError):
            share_store.read_key_labels(self.path, ["a"])
        self.assertEqual(self.path.read_bytes(), b"not sqlite")

    def test_missing_columns_are_unknown_and_no_schema_is_created(self):
        other = self.path.parent / "unrelated.db"
        with closing(sqlite3.connect(other)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (id TEXT)")
        before = other.read_bytes()
        with self.assertRaises(share_store.StoreError):
            share_store.read_key_labels(other, ["a"])
        self.assertEqual(other.read_bytes(), before)

    def test_ids_are_parameters_and_duplicates_do_not_duplicate_results(self):
        actual = share_store.read_key_labels(
            self.path, ["x') OR 1=1 --", "x') OR 1=1 --", None, False, ""])
        self.assertEqual(list(actual), ["x') OR 1=1 --"])

    def test_empty_selection_never_opens_any_database(self):
        with mock.patch.object(share_store.sqlite3, "connect") as connect:
            self.assertEqual(share_store.read_key_labels("missing.db", []), {})
        connect.assert_not_called()

    def test_large_selection_is_chunked_and_complete(self):
        expected = {str(i) for i in range(1001)}
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executemany("INSERT INTO keys VALUES (?,?,?,?)",
                             [(k, k, None, "private") for k in expected])
        real_connect = sqlite3.connect
        statements = []

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(share_store.sqlite3, "connect",
                               side_effect=connect):
            actual = share_store.read_key_labels(self.path, expected)
        self.assertEqual(set(actual), expected)
        self.assertTrue(all(set(row) == {"key_id", "name", "deleted_at"}
                            for row in actual.values()))
        # 名字里的「chunked」也要有证据对象：1001 个 key 必须分成多条
        # SELECT（每批 ≤ 400 个 id），否则单条 1001 参数的语句也会通过。
        selects = [stmt for stmt in statements if "SELECT key_id" in stmt]
        counts = [stmt.count("','") + 1 for stmt in selects]
        self.assertEqual(sum(counts), 1001, "逐条 id 都要被读到：%r" % selects)
        self.assertEqual(len(selects), 3,
                         "1001 个 key 应分成 3 条 SELECT：%r" % selects)
        self.assertLessEqual(max(counts), 400,
                            "每批不得超过 400 个 id：%r" % counts)

    def test_sql_connection_is_read_only_and_closed(self):
        real_connect = sqlite3.connect
        captured = []
        statements = []
        def connect(*args, **kwargs):
            self.assertTrue(kwargs["uri"])
            self.assertTrue(args[0].endswith("?mode=ro"))
            conn = real_connect(*args, **kwargs)
            captured.append(conn)
            conn.set_trace_callback(statements.append)
            return conn
        with mock.patch.object(share_store.sqlite3, "connect", side_effect=connect):
            share_store.read_key_labels(self.path, ["a"])
        self.assertTrue(any("query_only=ON" in stmt for stmt in statements))
        self.assertFalse(any("secret" in stmt.lower() for stmt in statements))
        self.assertFalse(any("CREATE" in stmt or "UPDATE" in stmt for stmt in statements))
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
