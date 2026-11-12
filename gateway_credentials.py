# -*- coding: utf-8 -*-
"""Credential reader seam for the gateway data directory.

B0 (2026-11-01): no Python consumer reads a gateway credential file any more.
The retired Qoder Global API token reader was the last one; its source and token
file were removed with the Qoder gateways, so the reader is deleted instead of
left behind as an unused export.  The two live readers stay at their owners:

* ``chat2api/chat2api.py`` reads ``chat2api/token.json`` (the UM session);
* ``lib/gateway-runtime.ps1`` ``Get-LocalControlHeader`` reads
  ``data/admin-control-token`` (PowerShell cannot import Python).

A new reader belongs here only when two or more Python consumers must agree on
the same file format — one reader per format, never a second path.
"""
