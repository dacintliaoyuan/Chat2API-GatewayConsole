# -*- coding: utf-8 -*-
"""Compatibility shim: the aggregator now lives in ``shared.nonstream_aggregate``.

Both gateway applications bundle the shared decoder, so this old path only
re-exports it. The classes and errors are the SAME objects (not copies), and
existing importers of ``chat2api/nonstream_aggregate.py`` keep working.

The package root is inserted here because an importer may put only ``chat2api/`` on
``sys.path`` (the service's own script layout); other entries are kept untouched.
"""

import sys
from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from shared.nonstream_aggregate import *  # noqa: E402,F401,F403
