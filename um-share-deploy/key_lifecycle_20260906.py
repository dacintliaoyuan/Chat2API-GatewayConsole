#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""key_lifecycle_20260906.py — SUPERSEDED isolated key-lifecycle scenario.

History: this script started a real-mode share gateway on an unmapped loopback
port with a temp config and a throwaway identity, then verified
create -> works -> revoke -> 401 -> works with a fresh second identity, and
cleaned up.

It can no longer do that, for two independent reasons:

* the gateway's ``--add-client``/``--revoke-client`` CLI was REMOVED (R4):
  direct JSON mutation is no longer a writer; keys are provisioned through
  ``share_admin.py`` against the single-authority management service;
* the gateway's ``--upstream-port`` is now REQUIRED and has no default
  (R13-3), so a launcher must name its dedicated backend explicitly
  instead of silently inheriting the private chat2api 8000.

The scenario therefore REFUSES to start rather than launch a gateway whose key
steps can no longer run (and whose temp config would otherwise be pointed at a
production backend as a side effect of a diagnostic). Key lifecycle now belongs
to the management-service path: ``share_admin.py`` for provisioning and
``ai-gateway/tests/test_share_management*.py`` for the isolated scenario.
"""
import sys


def main() -> int:
    print("key_lifecycle_20260906.py is SUPERSEDED: the share gateway no "
          "longer provisions keys (--add-client/--revoke-client were removed, "
          "R4) and its --upstream-port is now required with no default "
          "(R13-3). Provision keys with share_admin.py against the "
          "management service; the isolated lifecycle scenario lives in "
          "ai-gateway/tests/test_share_management*.py.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
