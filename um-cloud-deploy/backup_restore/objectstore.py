"""Object-store adapters for backup upload and security publication."""
from __future__ import annotations

from pathlib import Path

from .errors import ObjectStoreError
from .package import _atomic_write

class ObjectStore:
    """Narrow adapter seam for a private object store (R2/OSS/remote-sim).

    The product never imports an S3 SDK here.  A deployment supplies one
    implementation; tests use :class:`LocalObjectStore` or
    :class:`MemoryObjectStore`.  ``key`` is a relative object key and may never
    escape the store root.
    """

    def put(self, key, data):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def list(self, prefix=""):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError


def _object_key(key):
    if not isinstance(key, str) or not key or key.startswith("/"):
        raise ObjectStoreError("object key must be a non-empty relative path")
    parts = key.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ObjectStoreError(f"object key is not safe: {key!r}")
    return key


class LocalObjectStore(ObjectStore):
    """Directory-backed object store (remote-sim for isolated drills)."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        key = _object_key(key)
        target = (self.root / key).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ObjectStoreError(
                f"object key escapes the store root: {key!r}") from exc
        return target

    def put(self, key, data):
        target = self._path(key)
        _atomic_write(target, bytes(data), mode=0o600)
        return {"key": _object_key(key), "size": len(data)}

    def get(self, key):
        target = self._path(key)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise ObjectStoreError(
                f"object unreadable: {key}") from exc

    def list(self, prefix=""):
        prefix = str(prefix or "").replace("\\", "/")
        rows = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if prefix and not key.startswith(prefix):
                continue
            stat_info = path.stat()
            rows.append({"key": key, "size": stat_info.st_size,
                          "mtime": stat_info.st_mtime})
        return rows

    def delete(self, key):
        target = self._path(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True


class MemoryObjectStore(ObjectStore):
    """In-memory fake object service with an optional bounded failure script."""

    def __init__(self, fail_puts=0, fail_lists=0):
        self._objects = {}
        self._lock = __import__("threading").Lock()
        self.fail_puts = int(fail_puts)
        self.fail_lists = int(fail_lists)
        self.put_attempts = 0

    def put(self, key, data):
        key = _object_key(key)
        with self._lock:
            self.put_attempts += 1
            if self.put_attempts <= self.fail_puts:
                raise ObjectStoreError("injected object-store put failure")
            self._objects[key] = bytes(data)
        return {"key": key, "size": len(data)}

    def get(self, key):
        key = _object_key(key)
        with self._lock:
            if key not in self._objects:
                raise ObjectStoreError(f"object not found: {key}")
            return self._objects[key]

    def list(self, prefix=""):
        with self._lock:
            if self.fail_lists > 0:
                self.fail_lists -= 1
                raise ObjectStoreError("injected object-store list failure")
            rows = []
            for key in sorted(self._objects):
                if prefix and not key.startswith(prefix):
                    continue
                rows.append({"key": key, "size": len(self._objects[key]),
                              "mtime": 0.0})
            return rows

    def delete(self, key):
        key = _object_key(key)
        with self._lock:
            return self._objects.pop(key, None) is not None
