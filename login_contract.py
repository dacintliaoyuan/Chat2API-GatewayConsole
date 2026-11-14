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

# Ticket #41: only these stages end THIS round at once. A click the page
# swallowed or a control the session could not recognize leaves the automatic flow
# stopped but the token poll running, so it is a soft manual signal and never a
# terminal manual-takeover code.
LOGIN_TERMINAL_MANUAL_REASONS = frozenset((
    "umpass_mfa_required", "umpass_prefill_missing", "unrecognized_page"))
