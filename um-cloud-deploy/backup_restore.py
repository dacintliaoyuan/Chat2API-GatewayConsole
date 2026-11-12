"""CLI entry point for :mod:`backup_restore` (implementation lives in the package)."""
from __future__ import annotations

import sys
from pathlib import Path

_DEPLOY_DIR = Path(__file__).resolve().parent
if str(_DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_DIR))

if __name__ == "__main__":
    from backup_restore import *  # noqa: F401,F403
    from backup_restore import main
else:
    # usagesrv loads this file by path under the name ``backup_restore``, so
    # ``sys.modules['backup_restore']`` is this entry module, not the package.
    # Load the package under a private alias and re-export its names here.
    import importlib.util

    _package = sys.modules.get("backup_restore")
    if _package is None or _package is sys.modules.get(__name__):
        _package_name = "_um_backup_restore_package"
        _package_init = _DEPLOY_DIR / "backup_restore" / "__init__.py"
        _spec = importlib.util.spec_from_file_location(
            _package_name, _package_init,
            submodule_search_locations=[str(_package_init.parent)])
        if _spec is None or _spec.loader is None:
            raise ImportError(f"cannot load {_package_init}")
        _package = importlib.util.module_from_spec(_spec)
        sys.modules[_package_name] = _package
        _spec.loader.exec_module(_package)
    globals().update({
        _name: getattr(_package, _name)
        for _name in dir(_package)
        if not _name.startswith("__")
    })
    main = _package.main

if __name__ == "__main__":
    raise SystemExit(main())
