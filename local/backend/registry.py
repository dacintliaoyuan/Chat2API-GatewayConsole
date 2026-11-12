# -*- coding: utf-8 -*-
"""Local console registry (E19-1-08).

The local application registers ONLY the local ``model.catalog`` read action. It
never imports ``console_adapters`` (whose default assembly still expands the
cloud share action cards) and never imports a ``share_*`` module: cloud Key,
broker and display actions are not part of this application.

``model.catalog`` stays a pure read: ``console_model_actions.model_catalog``
calls ``model_sync.collect_catalogs()`` (plain GETs; single-source failures
land in warnings), holds no ``SyncLock`` and writes no client configuration.
"""
from __future__ import annotations

from console import model_actions as console_model_actions
from console.operations import ActionSpec


def _model_catalog_resource(target, params):
    """Pure catalog read: no shared state is written."""
    return "model:catalog"


def build_registry():
    """The complete local action registry: only ``model.catalog``."""
    return [
        ActionSpec(
            "model.catalog",
            "模型目录只读查询（各来源 /v1/models；无探测、无计费、无启停）",
            handler=console_model_actions.model_catalog,
            resource_fn=_model_catalog_resource,
            param_schema={
                "sources": {"type": "str", "max_len": 512},
                "fresh": {"type": "bool"},
            },
            enabled=True,
            bump_generation_on_success=False,
        ),
    ]


__all__ = ["build_registry"]
