# -*- coding: utf-8 -*-
"""Shared UM login contract constants (ticket #42).

chat2api publishes the terminal manual-takeover code and the console/display
read faces consume it. Keeping the reasons and prefix in ONE root module stops
the service and the console from drifting apart (ticket #42 item 12): the service,
the console adapter and the display read face all import these names.

The module is deliberately dependency-free: importing it has no side effects.
"""

# A step only the account holder can complete ends THIS login interaction. The
# returned code keeps the reason so the operator can decide between a retry (a new
# round) and an on-demand visible display.
MANUAL_TAKEOVER_REQUIRED_PREFIX = "manual_takeover_required:"

# Pre-#41 chat2api published the soft signal under this shorter prefix. The
# console and display read faces still accept it so an older instance names the
# manual step instead of reporting a generic failure.
LEGACY_MANUAL_TAKEOVER_PREFIX = "manual_takeover:"
MANUAL_TAKEOVER_PREFIXES = (
    MANUAL_TAKEOVER_REQUIRED_PREFIX, LEGACY_MANUAL_TAKEOVER_PREFIX)

# Ticket #41: only these stages end THIS round at once. A click the page
# swallowed or a control the session could not recognize leaves the automatic flow
# stopped but the token poll running, so it is a soft manual signal and never a
# terminal manual-takeover code.
LOGIN_TERMINAL_MANUAL_REASONS = frozenset((
    "umpass_mfa_required", "umpass_prefill_missing", "unrecognized_page"))

# The Duo push wait is a manual step that keeps the round alive: the console
# shows "等待 MFA（本人操作）" but the token poll keeps running. Keeping the
# union here stops the display read face from re-listing the reasons and missing a
# reason added to the terminal set.
LOGIN_SOFT_MANUAL_REASONS = frozenset(("duo_prompt_waiting",))
LOGIN_MANUAL_REASONS = (
    LOGIN_TERMINAL_MANUAL_REASONS | LOGIN_SOFT_MANUAL_REASONS)


def terminal_manual_reason(*values):
    """Return the bare terminal manual reason carried by any value, or None.

    Both the canonical and the legacy prefix are accepted; a bare reason is
    accepted too, because an older instance may publish it without a prefix.
    Keeping the parse here stops the console and the display read face from
    drifting apart (ticket #42 item 12).
    """
    for value in values:
        if not isinstance(value, str):
            continue
        reason = value
        for prefix in MANUAL_TAKEOVER_PREFIXES:
            if reason.startswith(prefix):
                reason = reason[len(prefix):]
                break
        if reason in LOGIN_TERMINAL_MANUAL_REASONS:
            return reason
    return None
