"""Product-module loaders."""
from __future__ import annotations

import sys

from .constants import _RELEASE_ROOT
from .errors import BackupError, BackupKeyError

def _load_share_store():
    import importlib.util
    module = (sys.modules.get("share.store")
              or sys.modules.get("share_store"))
    if module is not None:
        return module
    path = _RELEASE_ROOT / "share/store.py"
    spec = importlib.util.spec_from_file_location("share_store", path)
    if spec is None or spec.loader is None:
        raise BackupError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_store"] = module
    spec.loader.exec_module(module)
    return module


def _load_share_secret_protection():
    """Load the product owner of the share-key AES-GCM scheme."""
    import importlib.util
    module = sys.modules.get("share_secret_protection")
    if module is not None:
        return module
    path = _RELEASE_ROOT / "share/secret_protection.py"
    spec = importlib.util.spec_from_file_location(
        "share_secret_protection", path)
    if spec is None or spec.loader is None:
        raise BackupKeyError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_secret_protection"] = module
    spec.loader.exec_module(module)
    return module
