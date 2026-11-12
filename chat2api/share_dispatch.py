# -*- coding: utf-8 -*-
"""Pre-send scheduling for the dedicated share backend (8110).

The 8100 share entry point admits a request (per-minute rate, per-key
concurrency, global 128 bound) and forwards it to the 8110 backend with a
signed attribution marker. This module adds ONE gate on that backend's send
boundary: before a request is actually sent upstream, it waits for its turn so a
single share Key cannot fill the upstream with a burst of simultaneous sends.
The admission rules are untouched: the wait happens INSIDE the forwarded request,
so a queued request still holds the 8100 slot it was admitted into, and the
dispatcher never cancels or preempts a generation that already started.

Contract:

* Every ticket carries the VERIFIED ``key_id`` and the ``request_id`` of the
  request. Same key: first come, first sent (FIFO). Different keys: the turn
  rotates, so one key never monopolises the rotation.
* The wait is CUMULATIVE per request: the deadline is fixed when the ticket is
  registered, and re-entering the gate or an internal retry never resets it.
  ``DEFAULT_MAX_WAIT_S`` (10s) is the default; the deployment may override
  it with ``CHAT2API_SHARE_DISPATCH_MAX_WAIT_S``.
* The wait is BOUNDED by a hard cumulative cap: when the budget is exhausted
  the wait ends with ``OUTCOME_TIMEOUT`` even if this request just reached its
  turn. The request is released exactly once WITHOUT a grant, so the caller
  answers a retryable failure instead of sending late, and it never jumps the
  same-key FIFO. A grant is therefore only possible strictly before the ticket's
  deadline. A zero budget means "no waiting": a ticket whose turn is available
  immediately is still granted; any ticket that would have to wait is refused.
* Cancel, revoke, the request deadline, a budget timeout and process exit
  release a ticket exactly once. After a release the ticket can never send again:
  a late send is refused instead of reaching the upstream.
* A committed generation is aborted ONLY by a key revoke (D18-3): the revoke
  closes the upstream response through the callback registered by the caller.
  Ordinary cancel/deadline/exit releases never preempt a committed send.

The module has no dependency on chat2api (no import cycle) and no I/O beyond
reading its own environment variable. The clock is injectable so tests never have
to wait for real time.
"""

import os
import threading
import time
from collections import OrderedDict, deque

__all__ = [
    "DEFAULT_MAX_WAIT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DISPATCH_MAX_WAIT_ENV",
    "FINISHED_REQUEST_IDS_MAX",
    "OUTCOME_CANCELLED",
    "OUTCOME_CLOSED",
    "OUTCOME_DEADLINE",
    "OUTCOME_GRANTED",
    "OUTCOME_REVOKED",
    "OUTCOME_STOPPED",
    "OUTCOME_TIMEOUT",
    "OUTCOME_WAITING",
    "ShareDispatcher",
    "ShareDispatchTicket",
    "max_wait_seconds",
]

# Cumulative send-wait budget. The dedicated 8110 backend is the only caller;
# the 8000 main chain never assembles a dispatcher, so its native JSON and its
# existing retry/budget/cancel semantics are untouched.
DISPATCH_MAX_WAIT_ENV = "CHAT2API_SHARE_DISPATCH_MAX_WAIT_S"
DEFAULT_MAX_WAIT_S = 10.0

# How long a waiting ticket sleeps before it re-checks its own deadline and the
# caller's cancellation probe. Bounded so a cancel or process stop is observed
# promptly; it never shortens or extends the cumulative budget.
DEFAULT_POLL_INTERVAL_S = 0.05

# Bounded registry of finished request ids, so a request that was released can
# never be granted a send again while memory stays bounded (same shape as the
# attribution replay registry in chat2api).
FINISHED_REQUEST_IDS_MAX = 20000

# Outcomes of a poll/wait. GRANTED permits the send; TIMEOUT ends the wait
# WITHOUT a grant (the caller answers a retryable failure). The remaining
# outcomes are the exactly-once release reasons, and every one of them forbids
# the send too.
OUTCOME_GRANTED = "granted"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_WAITING = "waiting"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_REVOKED = "revoked"
OUTCOME_DEADLINE = "deadline"
OUTCOME_STOPPED = "stopped"
OUTCOME_CLOSED = "closed"

_STATE_PENDING = "pending"
_STATE_GRANTED = "granted"
_STATE_COMMITTED = "committed"
_STATE_CLOSED = "closed"


def max_wait_seconds(environ=None):
    """The cumulative send-wait budget in seconds.

    ``CHAT2API_SHARE_DISPATCH_MAX_WAIT_S`` overrides the default. An
    unparsable, negative, NaN or infinite value is ignored (the default stays
    in effect): a broken environment entry must never disable the gate or make it
    wait forever.
    """
    raw = (os.environ if environ is None else environ).get(
        DISPATCH_MAX_WAIT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_WAIT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_WAIT_S
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return DEFAULT_MAX_WAIT_S
    return value


def _closed_outcome(reason):
    """Map a release reason onto the outcome a waiting caller sees."""
    if reason == "cancel":
        return OUTCOME_CANCELLED
    if reason == "revoke":
        return OUTCOME_REVOKED
    if reason == "exit":
        return OUTCOME_STOPPED
    if reason == "deadline":
        return OUTCOME_DEADLINE
    if reason == "timeout":
        return OUTCOME_TIMEOUT
    return OUTCOME_CLOSED


class ShareDispatchTicket:
    """ONE request's place in the pre-send rotation.

    Created by ``ShareDispatcher.register`` and never by a caller directly. The
    ticket is the single authority for "this request may send": it is granted
    exactly once, the queue position is left exactly once, and after a release the
    ticket can never permit a send again.
    """

    __slots__ = ("_dispatcher", "key_id", "request_id", "deadline",
                 "_state", "_reason", "_abort_callback", "_aborted")

    def __init__(self, dispatcher, key_id, request_id, deadline=None,
                 closed_reason=None):
        self._dispatcher = dispatcher
        self.key_id = key_id
        self.request_id = request_id
        self.deadline = deadline
        self._state = (_STATE_CLOSED if closed_reason is not None
                        else _STATE_PENDING)
        self._reason = closed_reason or ""
        self._abort_callback = None
        self._aborted = False

    @property
    def state(self):
        return self._state

    @property
    def reason(self):
        return self._reason

    @property
    def granted(self):
        """True while this ticket holds (or has consumed) the send permission."""
        return self._state in (_STATE_GRANTED, _STATE_COMMITTED)

    @property
    def timed_out(self):
        """True when the cumulative budget ended this wait without a grant."""
        return self._state == _STATE_CLOSED and self._reason == "timeout"

    @property
    def outcome(self):
        """The outcome a caller sees for this ticket's CURRENT state.

        A closed ticket keeps its release reason (cancel, revoke, deadline,
        timeout, exit), so a caller that lost the ticket between its grant and
        its commit can still name the correct refusal instead of collapsing
        every release into a generic cancellation.
        """
        if self._state == _STATE_CLOSED:
            return _closed_outcome(self._reason)
        if self._state in (_STATE_GRANTED, _STATE_COMMITTED):
            return OUTCOME_GRANTED
        return OUTCOME_WAITING

    def wait_turn(self, cancel_check=None, request_deadline=None,
                  stop_check=None):
        """Block until this request may send; return the outcome constant.

        Re-entering this method never resets the cumulative budget (the deadline
        was fixed at registration) and never grants a second send. ``GRANTED`` is
        the only outcome that permits the send. A cancel, the request deadline,
        a process stop or an exhausted budget releases the ticket exactly once and
        returns the matching outcome; the caller must then not send. The budget
        timeout is a retryable refusal, never a late send.
        """
        dispatcher = self._dispatcher
        while True:
            outcome = dispatcher.poll(self, request_deadline=request_deadline)
            if outcome != OUTCOME_WAITING:
                return outcome
            if cancel_check is not None and cancel_check():
                dispatcher.release(self, "cancel")
                return OUTCOME_CANCELLED
            if stop_check is not None and stop_check():
                dispatcher.release(self, "exit")
                return OUTCOME_STOPPED
            with dispatcher.condition:
                if self._state != _STATE_PENDING:
                    continue
                # Wakes on every grant/release; the timeout re-checks the
                # cumulative deadline even when nothing changed.
                dispatcher.condition.wait(dispatcher.poll_interval)

    def commit(self):
        """Claim the send this ticket was granted; False after a release."""
        return self._dispatcher.commit(self)

    def release(self, reason="released"):
        """Leave the queue/release the send permission exactly once."""
        return self._dispatcher.release(self, reason)

    def attach_abort(self, callback):
        """Register how a key revoke closes THIS committed upstream response.

        D18-3: only a COMMITTED ticket may be aborted, and only by a key
        revoke; ordinary cancel/deadline/exit never invokes the callback. A
        revoke that arrived BEFORE this registration closes the response at
        once, so a late send can never outlive the revoke. Returns True when
        the callback is registered or already invoked, False for a ticket that
        is still PENDING/GRANTED or was released for another reason.
        """
        if not callable(callback):
            raise ValueError("callback must be callable")
        invoke_now = False
        with self._dispatcher.condition:
            if self._state == _STATE_COMMITTED and not self._aborted:
                self._abort_callback = callback
                return True
            if (self._state == _STATE_CLOSED and self._aborted
                    and self._reason == "revoke"):
                invoke_now = True
        if not invoke_now:
            return False
        callback()
        return True

    def abort(self):
        """Abort THIS committed generation (key revoke); never a normal release.

        D18-3: only a key revoke calls this. The ticket is closed exactly
        once, its registered callback (the upstream ``Response.close``) runs
        outside the dispatcher lock, and a PENDING/GRANTED ticket is left to
        the ordinary release path. Returns True only for a committed ticket
        this call actually aborted.
        """
        with self._dispatcher.condition:
            if self._state != _STATE_COMMITTED or self._aborted:
                return False
            callback = self._abort_locked("revoke")
        if callback is not None:
            callback()
        return True

    def _abort_locked(self, reason):
        """Close a COMMITTED ticket and return its callback; lock held."""
        if self._state != _STATE_COMMITTED or self._aborted:
            return None
        self._aborted = True
        callback = self._abort_callback
        self._abort_callback = None
        self._dispatcher._close_locked(self, reason)
        return callback


class ShareDispatcher:
    """Round-robin pre-send scheduler with per-key FIFO order.

    ``max_wait`` is the cumulative budget in seconds, ``clock`` is the time
    source (``time.monotonic`` by default) and ``poll_interval`` is the bounded
    sleep of ``ShareDispatchTicket.wait_turn``. Tests inject the clock so the
    10s budget is proven without waiting for real time.
    """

    def __init__(self, max_wait=None, clock=None, poll_interval=None):
        wait = max_wait_seconds() if max_wait is None else float(max_wait)
        if wait != wait or wait in (float("inf"), float("-inf")) or wait < 0:
            raise ValueError("max_wait must be a finite non-negative number")
        interval = (DEFAULT_POLL_INTERVAL_S if poll_interval is None
                    else float(poll_interval))
        if interval <= 0 or interval != interval:
            raise ValueError("poll_interval must be a positive number")
        self.max_wait = wait
        self.poll_interval = interval
        self._clock = time.monotonic if clock is None else clock
        self.condition = threading.Condition(threading.Lock())
        # key_id -> FIFO of PENDING tickets (one key appears once in _order).
        self._queues = {}
        # Rotation cursor: the keys that still have a pending ticket, in turn order.
        self._order = deque()
        # request_id -> ticket that has not been released yet (pending/committed).
        self._tickets = OrderedDict()
        # Bounded registry of released request ids -> release reason.
        self._finished = OrderedDict()
        self._granted = 0
        self._committed = 0
        self._timed_out = 0

    # ---------------------------------------------------------- lifecycle

    def register(self, key_id, request_id, closed_reason=None):
        """Register (or re-enter) THIS request's ticket.

        Idempotent per ``request_id``: re-entering the gate returns the same
        ticket and therefore never resets the cumulative budget. A request id that
        was already released gets a CLOSED ticket, so a late send is refused
        instead of being granted a new turn. ``closed_reason`` refuses a NEW
        request before it ever enters the queue: a request admitted before an
        applied revoke notice is refused even when it reaches this process after
        the notice.
        """
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("key_id must be a non-empty string")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        with self.condition:
            existing = self._tickets.get(request_id)
            if existing is not None:
                return existing
            if request_id in self._finished:
                return ShareDispatchTicket(
                    self, key_id, request_id,
                    closed_reason=self._finished[request_id])
            if closed_reason is not None:
                self._remember_finished_locked(request_id, closed_reason)
                return ShareDispatchTicket(
                    self, key_id, request_id, closed_reason=closed_reason)
            ticket = ShareDispatchTicket(
                self, key_id, request_id,
                deadline=self._clock() + self.max_wait)
            self._tickets[request_id] = ticket
            queue = self._queues.get(key_id)
            if queue is None:
                queue = self._queues[key_id] = deque()
                self._order.append(key_id)
            queue.append(ticket)
            self.condition.notify_all()
            return ticket

    def poll(self, ticket, request_deadline=None):
        """Advance the rotation for ONE ticket without blocking.

        Returns ``OUTCOME_GRANTED`` when this request may send,
        ``OUTCOME_WAITING`` when it must wait, ``OUTCOME_TIMEOUT`` when the
        cumulative budget is spent (even at its turn), or the release outcome
        after the ticket was closed. The budget is checked before the turn, so a
        request that never got its turn before the hard cap ends as a retryable
        refusal instead of sending late or jumping the same-key FIFO.
        """
        if ticket._dispatcher is not self:
            raise ValueError("ticket belongs to another dispatcher")
        with self.condition:
            return self._poll_locked(ticket, request_deadline)

    def commit(self, ticket):
        """Claim the granted send exactly once; False after a release.

        A committed ticket stays committed: an internal retry of the same logical
        request is part of the same committed generation, so it is neither
        re-queued nor preempted. After ``release`` this returns False.
        """
        with self.condition:
            if ticket._state == _STATE_COMMITTED:
                return True
            if ticket._state != _STATE_GRANTED:
                return False
            ticket._state = _STATE_COMMITTED
            ticket._reason = "sent"
            self._committed += 1
            self.condition.notify_all()
            return True

    def release(self, ticket, reason="released"):
        """Release a ticket exactly once; False when it was already released."""
        with self.condition:
            return self._close_locked(ticket, reason)

    def revoke_key(self, key_id, reason="revoke"):
        """Release PENDING/GRANTED tickets of one key; abort COMMITTED sends.

        Returns ``(released, aborted)``: ``released`` counts the PENDING and
        GRANTED tickets closed before any upstream send, ``aborted`` counts the
        COMMITTED tickets whose upstream response a revoke closed (D18-3). A
        granted-but-not-committed send has not reached the upstream yet, so
        revoking the key must close it and refuse the late send. A committed
        generation is aborted ONLY by this revoke; an ordinary cancel,
        deadline or exit release never preempts it. A released ticket is never
        released or aborted twice.
        """
        abort_callbacks = []
        with self.condition:
            tickets = [ticket for ticket in self._tickets.values()
                       if ticket.key_id == key_id]
            released = 0
            aborted = 0
            for ticket in tickets:
                if ticket._state in (_STATE_PENDING, _STATE_GRANTED):
                    if self._close_locked(ticket, reason):
                        released += 1
                elif ticket._state == _STATE_COMMITTED:
                    if ticket._aborted:
                        continue
                    callback = ticket._abort_locked(reason)
                    aborted += 1
                    if callback is not None:
                        abort_callbacks.append(callback)
        for callback in abort_callbacks:
            callback()
        return released, aborted

    def close_all(self, reason="exit"):
        """Release every ticket exactly once (process exit). Returns the count."""
        with self.condition:
            tickets = list(self._tickets.values())
            for ticket in tickets:
                self._close_locked(ticket, reason)
            return len(tickets)

    # --------------------------------------------------------- diagnostics

    def pending_count(self):
        with self.condition:
            return sum(len(queue) for queue in self._queues.values())

    def stats(self):
        """Read-only counters for logs and tests; never used for decisions."""
        with self.condition:
            return {
                "pending": sum(len(queue) for queue in self._queues.values()),
                "keys": len(self._queues),
                "granted": self._granted,
                "committed": self._committed,
                "timed_out": self._timed_out,
                "finished": len(self._finished),
                "registered": len(self._tickets) + len(self._finished),
            }

    # ------------------------------------------------------------- internal

    def _poll_locked(self, ticket, request_deadline):
        state = ticket._state
        if state in (_STATE_GRANTED, _STATE_COMMITTED):
            return OUTCOME_GRANTED
        if state == _STATE_CLOSED:
            return _closed_outcome(ticket._reason)
        now = self._clock()
        if request_deadline is not None and now >= request_deadline:
            # The request can no longer be sent: release its queue position
            # exactly once instead of leaving a ticket nobody will ever use.
            self._close_locked(ticket, "deadline")
            return OUTCOME_DEADLINE
        if self.max_wait <= 0:
            # A zero budget means "do not queue": the request may send only
            # when its turn is available right now, otherwise it is refused
            # immediately. The rotation and ticket release stay in effect, and
            # an immediate turn is never refused just because the (zero) budget
            # is already at its limit.
            if self._is_turn_locked(ticket):
                self._grant_locked(ticket)
                return OUTCOME_GRANTED
            if self._close_locked(ticket, "timeout"):
                self._timed_out += 1
            return OUTCOME_TIMEOUT
        if ticket.deadline is not None and now >= ticket.deadline:
            # The cumulative budget is a HARD cap: a request whose budget is
            # spent is refused even when its turn just came. It is released
            # exactly once WITHOUT a grant, so the caller answers a retryable
            # failure and the request never sends late.
            if self._close_locked(ticket, "timeout"):
                self._timed_out += 1
            return OUTCOME_TIMEOUT
        if self._is_turn_locked(ticket):
            # This request's turn came while its budget was still available:
            # the same-key FIFO and the key rotation are honoured.
            self._grant_locked(ticket)
            return OUTCOME_GRANTED
        return OUTCOME_WAITING

    def _is_turn_locked(self, ticket):
        if not self._order or self._order[0] != ticket.key_id:
            return False
        queue = self._queues.get(ticket.key_id)
        return bool(queue) and queue[0] is ticket

    def _grant_locked(self, ticket):
        self._dequeue_locked(ticket)
        ticket._state = _STATE_GRANTED
        self._granted += 1
        self.condition.notify_all()

    def _dequeue_locked(self, ticket):
        queue = self._queues.get(ticket.key_id)
        if queue is None:
            return
        # Only the ticket that WAS this key's head changes the rotation; closing
        # a middle ticket must not push the whole key behind the others.
        was_head = bool(queue) and queue[0] is ticket
        try:
            queue.remove(ticket)
        except ValueError:
            return
        if not queue:
            self._queues.pop(ticket.key_id, None)
            try:
                self._order.remove(ticket.key_id)
            except ValueError:
                pass
        elif was_head and self._order and self._order[0] == ticket.key_id:
            # The key that just sent goes behind every other waiting key, so
            # the next grant rotates to the next key instead of this one.
            self._order.rotate(-1)

    def _close_locked(self, ticket, reason):
        if ticket._state == _STATE_CLOSED:
            return False
        if ticket._state == _STATE_PENDING:
            self._dequeue_locked(ticket)
        ticket._state = _STATE_CLOSED
        ticket._reason = reason
        # An ordinary release never aborts the committed upstream response:
        # only a key revoke (``_abort_locked``) may invoke this callback.
        ticket._abort_callback = None
        self._tickets.pop(ticket.request_id, None)
        self._remember_finished_locked(ticket.request_id, reason)
        self.condition.notify_all()
        return True

    def _remember_finished_locked(self, request_id, reason):
        self._finished[request_id] = reason
        self._finished.move_to_end(request_id)
        while len(self._finished) > FINISHED_REQUEST_IDS_MAX:
            self._finished.popitem(last=False)
