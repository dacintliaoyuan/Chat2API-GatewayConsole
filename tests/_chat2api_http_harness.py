# -*- coding: utf-8 -*-
"""Shared in-process HTTP isolation for chat2api.Handler test suites (B8-0).

The 2026-10-02 incident: chat/Messages suites patched ``chat2api.USAGE_LOG``
to a private temp ledger but registered the patch release AFTER the server
teardown cleanups — and ``addCleanup`` is LIFO, so the patch was restored
FIRST while request-handler threads of the response just received were still
running. Their late ``record_usage`` writes (every call site is a nested
function inside ``Handler``, so they always run on the handler thread) then
landed on the PRODUCTION ``data/usage-chat2api.jsonl``: 12 rows on 2026-10-02
alone. This harness pins the fixed lifecycle so no suite can regress it:

  1. Each accepted gateway connection owns a lease on its private ledger.
     Its recorder has a fixed path, independent of later module patches.
     A failed unittest cleanup does NOT prevent subsequent patch releases.
  2. ``TrackedHTTPServer`` counts accepted-but-unfinished handler threads.
     Its teardown is ``shutdown()`` (stop accepting) → bounded ``drain()``
     (every in-flight handler finishes, including the post-response
     request-summary/record_usage tail) → ``server_close()``. No sleeps, no
     "run it again" proofs.
  3. Cleanup restores module patches normally but retains the temp directory
     until the last accepted handler exits, even after a bounded drain fails.
     An old request cannot use the next suite's ledger or the live default.
  4. Sentinels fail immediately on: a redirect that resolves to the
     production ledger, a non-loopback bind, a real service port, a local
     key taken from the live ``CHAT2API_LOCAL_API_KEY`` environment, or an
     upstream credential that is not the declared stub literal.

The upstream credential fakeness is pinned at each call site: every suite
passes its own ``stub`` literal to ``assert_stub_credentials`` and asserts
equality with it, so wiring a real token through would change the literal.
"""

import os
import socket
import tempfile
import threading
import time
from types import FunctionType
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

# Ports the real services own (production chat2api 8000, share 8100,
# console 8110, share broker 8096, m365 bridge 8320). A test server bound to
# any of them is a configuration error, not a test failure to tolerate.
REAL_SERVICE_PORTS = frozenset({8000, 8100, 8110, 8096, 8320})

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# Sentinel default drain budget: long enough for any legitimate post-response
# tail, short enough that a wedged handler fails the test in bounded time.
DEFAULT_DRAIN_TIMEOUT_S = 10.0


class _RecorderGlobals(dict):
    """Pin the sink while preserving live test patches on all other globals."""

    def __init__(self, original, ledger):
        super().__init__(USAGE_LOG=str(ledger), _usage_disabled=False)
        self._original = original

    def __missing__(self, key):
        value = self._original[key]
        if key == "_price_table_version" and isinstance(value, FunctionType):
            # HTTP tests without an explicit price fixture must not open the
            # production data directory just to decorate a synthetic row.
            price_path = os.path.abspath(value.__globals__["PRICE_TABLE_FILE"])
            production_data = os.path.abspath(os.path.join(
                value.__globals__["HERE"], os.pardir, "data"))
            try:
                real_data = (os.path.normcase(os.path.commonpath(
                    [price_path, production_data])) == os.path.normcase(production_data))
            except ValueError:
                real_data = False
            if real_data:
                price_path = str(Path(self["USAGE_LOG"]).parent / "no-price-fixture.json")
            namespace = dict(value.__globals__)
            namespace["PRICE_TABLE_FILE"] = price_path
            return FunctionType(value.__code__, namespace, value.__name__,
                                value.__defaults__, value.__closure__)
        return value


class _LedgerBinding:
    def __init__(self, router, ledger):
        original = router.original
        self.ledger = Path(ledger)
        self.record = FunctionType(
            original.__code__, _RecorderGlobals(original.__globals__, ledger),
            original.__name__, original.__defaults__, original.__closure__)
        self.record.__kwdefaults__ = original.__kwdefaults__


class _UsageIsolation(_LedgerBinding):
    """Directory lifetime follows accepted handlers, never cleanup success."""

    def __init__(self, router, tmp, ledger):
        super().__init__(router, ledger)
        self.router, self.tmp = router, tmp
        self._lock = threading.Lock()
        self._leases = 0
        self._cleanup_requested = False
        self._released = False
        self._servers = []

    def acquire(self):
        with self._lock:
            if self._cleanup_requested:
                return False
            self._leases += 1
            return True

    def release(self):
        with self._lock:
            self._leases -= 1
        self._finish_cleanup()

    def add_server(self, server):
        with self._lock:
            if self._cleanup_requested:
                raise AssertionError("cannot attach a server to released isolation")
            self._servers.append(server)

    def cleanup(self):
        with self._lock:
            self._cleanup_requested = True
            servers = list(self._servers)
        # Covers setUp failure before install_tracked_server was registered.
        # Request a nonblocking accept-loop stop; shutdown() could wait forever
        # if a constructed server never entered serve_forever().
        failures = []
        for server in servers:
            server._BaseServer__shutdown_request = True
            try:
                try:
                    server.close_client_sockets()
                finally:
                    ThreadingHTTPServer.server_close(server)
            except Exception as exc:
                failures.append(exc)
        try:
            self._finish_cleanup()
        finally:
            if failures:
                raise failures[0]

    def _finish_cleanup(self):
        with self._lock:
            if (not self._cleanup_requested or self._leases or self._released):
                return
            self._released = True
        self.tmp.cleanup()
        self.router.forget(self)


class _UsageRouter:
    """Test-module recorder; request bindings survive all unittest cleanups.

    The router stays installed on this imported test module. Restoring it while
    an old handler is alive would recreate the same global-lookup race. This
    changes only the suite's imported module copy, never product source or a
    production process.
    """

    def __init__(self, module):
        self.module = module
        self.original = module.record_usage
        if not isinstance(self.original, FunctionType):
            raise AssertionError("HTTP isolation requires the real record_usage function")
        self.local = threading.local()
        self._lock = threading.Lock()
        self._scopes = {}

    def register(self, scope):
        with self._lock:
            self._scopes[os.path.normcase(os.path.abspath(scope.ledger))] = scope

    def forget(self, scope):
        with self._lock:
            path = os.path.normcase(os.path.abspath(scope.ledger))
            if self._scopes.get(path) is scope:
                self._scopes.pop(path)

    def current_binding(self):
        path = os.path.normcase(os.path.abspath(self.module.USAGE_LOG))
        with self._lock:
            return self._scopes.get(path)

    def __call__(self, *args, **kwargs):
        binding = getattr(self.local, "binding", None)
        if hasattr(self.local, "binding"):
            if binding is None:
                raise AssertionError("test handler has no private ledger binding")
            return binding.record(*args, **kwargs)
        binding = self.current_binding()
        if binding is None:
            raise AssertionError("test record_usage refused the restored production path")
        if not binding.acquire():
            raise AssertionError("test record_usage refused released isolation")
        try:
            return binding.record(*args, **kwargs)
        finally:
            binding.release()


def _handler_usage_router(cls):
    for klass in cls.__mro__:
        for value in vars(klass).values():
            function = getattr(value, "__func__", value)
            namespace = getattr(function, "__globals__", {})
            router = namespace.get("_chat2api_test_usage_router")
            if isinstance(router, _UsageRouter):
                return router
    return None


def _wrap_dispatch(func):
    """Wrap a Handler ``do_*`` method with request tracking.

    Every ``record_usage`` call site in chat2api.py is a nested function
    inside a ``do_*`` dispatch chain, so "no dispatched request is running"
    is exactly "no record_usage can still happen". The wrapper increments
    when the dispatch STARTS (after the request line was parsed) and
    decrements when it ENDS, so a keep-alive connection parked in the
    ``readline`` BETWEEN requests never blocks a drain.
    """

    def wrapped(self, *args, **kwargs):
        server = self.server
        with server._requests_cond:
            server._requests_active += 1
        try:
            return func(self, *args, **kwargs)
        finally:
            with server._requests_cond:
                server._requests_active -= 1
                server._requests_cond.notify_all()

    wrapped.__name__ = func.__name__
    return wrapped


def _tracked_handler_class(cls):
    """Return ``cls`` wrapped so the server can track dispatched requests."""
    if getattr(cls, "_chat2api_http_tracked", False):
        return cls
    tracked = type("TrackedHTTPHandler_" + cls.__name__, (cls,), {})
    tracked._chat2api_http_tracked = True
    for klass in cls.__mro__:
        for name, value in list(vars(klass).items()):
            if name.startswith("do_") and callable(value):
                setattr(tracked, name, _wrap_dispatch(getattr(cls, name)))
    return tracked


class TrackedHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` whose dispatched requests are countable.

    ``drain`` waits for dispatched requests, not for threads: ``requests``
    keeps its socket open after a response (keep-alive; the Session closes
    only when the Response object is collected), so handler threads linger
    in the keep-alive ``readline`` waiting for a next request that never
    comes. Those parked threads hold NO pending ledger work — the request
    they served is already complete — so draining on "requests in flight"
    is both correct and fast. ``close_client_sockets`` additionally wakes
    the parked threads at teardown.
    """

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._requests_cond = threading.Condition()
        self._requests_active = 0
        self._client_socks = {}
        self._usage_router = _handler_usage_router(args[1])
        binding = (self._usage_router.current_binding()
                   if self._usage_router is not None else None)
        self._usage_scope = (binding if isinstance(binding, _UsageIsolation)
                             else None)
        super().__init__(args[0], _tracked_handler_class(args[1]),
                         *args[2:], **kwargs)
        if self._usage_scope is not None:
            self._usage_scope.add_server(self)

    def process_request(self, request, client_address):
        scope = self._usage_scope
        if scope is not None and not scope.acquire():
            self.shutdown_request(request)
            return
        with self._requests_cond:
            self._client_socks[id(request)] = request
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._requests_cond:
                self._client_socks.pop(id(request), None)
            if scope is not None:
                scope.release()
            raise

    def process_request_thread(self, request, client_address):
        router = self._usage_router
        had_binding = router is not None and hasattr(router.local, "binding")
        previous = getattr(router.local, "binding", None) if router else None
        if router is not None:
            router.local.binding = self._usage_scope
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._requests_cond:
                self._client_socks.pop(id(request), None)
            if router is not None:
                if had_binding:
                    router.local.binding = previous
                else:
                    del router.local.binding
            if self._usage_scope is not None:
                self._usage_scope.release()

    def handle_error(self, request, client_address):
        """Silence teardown-induced connection aborts on parked readers.

        ``close_client_sockets`` wakes keep-alive readline waiters by
        closing their socket under them; socketserver would print the
        resulting BrokenPipe/ConnectionReset traceback to stderr on every
        suite. That abort is OUR teardown action, not a defect of the code
        under test, so it is silenced here; everything else keeps the
        default (loud) reporting.
        """
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def active_requests(self):
        with self._requests_cond:
            return self._requests_active

    def close_client_sockets(self):
        """Force-close every accepted client socket.

        Wakes handlers parked in the keep-alive ``readline`` and unblocks
        mid-request handlers at their next socket operation (their pending
        ledger writes, if any, still complete first — the abort can only
        SKIP a row, never redirect one).
        """
        with self._requests_cond:
            socks = list(self._client_socks.values())
            self._client_socks.clear()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def drain(self, timeout=DEFAULT_DRAIN_TIMEOUT_S):
        """Bounded wait until every DISPATCHED request finished.

        Returns the number of requests still in flight when the deadline
        passed (0 = fully drained). The condition wait wakes as soon as the
        last dispatch ends; a parked keep-alive readline does not count.
        """
        deadline = time.monotonic() + timeout
        with self._requests_cond:
            while self._requests_active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._requests_active
                self._requests_cond.wait(remaining)
        return 0


def production_ledger_path(chat2api_module):
    """The module's own production default, computed the same way it is.

    Mirrors chat2api.py's ``USAGE_LOG`` default (HERE/../data/
    usage-chat2api.jsonl) so the sentinel catches the REAL production file
    even when CHAT2API_USAGE_LOG redirects the module default for a run.
    """
    return os.path.abspath(
        os.path.join(chat2api_module.HERE, os.pardir, "data",
                     "usage-chat2api.jsonl"))


def assert_loopback_ephemeral_server(server, *, name):
    """Sentinel: the test server must be loopback-only, ephemeral port."""
    address = server.server_address[:2]
    host, port = str(address[0]), address[1]
    if host not in LOOPBACK_HOSTS:
        raise AssertionError(
            f"{name} bound to non-loopback {host!r} — tests must never expose "
            f"the gateway beyond this machine")
    if port in REAL_SERVICE_PORTS:
        raise AssertionError(
            f"{name} bound to real service port {port}; tests must use an "
            f"ephemeral port")
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        raise AssertionError(f"{name} bound to unexpected port {port!r}")


def assert_stub_credentials(chat2api_module, *, local_key=None,
                            upstream_token, stub_token_literal):
    """Sentinel: no real credential may enter a chat2api HTTP suite.

    The local key (when the suite uses one) must never equal the live
    ``CHAT2API_LOCAL_API_KEY`` (the secret the real service presents), and
    the upstream token must equal the stub literal the suite declares —
    swapping in a real upstream token changes the literal and fails here.
    A suite that runs keyless passes ``local_key=None``.
    """
    if local_key is not None:
        if not local_key or local_key == stub_token_literal:
            raise AssertionError(
                "local key must be its own non-stub test value")
        env_key = (os.environ.get("CHAT2API_LOCAL_API_KEY") or "").strip()
        if env_key and env_key == local_key:
            raise AssertionError(
                "local key equals the live CHAT2API_LOCAL_API_KEY secret")
    if not upstream_token or upstream_token != stub_token_literal:
        raise AssertionError(
            f"upstream token must be the declared stub literal "
            f"{stub_token_literal!r}, got {upstream_token!r}")


def install_usage_isolation(test, chat2api_module, *, ledger_name="usage.jsonl"):
    """Redirect the chat2api instance's usage ledger to a private temp file.

    MUST run BEFORE any server teardown is registered (setUp calls it before
    creating servers): the cleanup stack is LIFO, so this registration order

        1. temp-dir release   → runs LAST
        2. patch releases     → run after the server teardowns
        3. server teardowns   → registered later, run FIRST (drain handlers)

    The ordering makes successful teardown straightforward; safety on a failed
    cleanup instead comes from the fixed recorder and accepted-handler leases.
    Returns ``(temp_dir: Path, ledger: Path)``.
    """
    tmp = tempfile.TemporaryDirectory(prefix="chat2api-test-usage-")
    ledger = Path(tmp.name) / ledger_name
    production = production_ledger_path(chat2api_module)
    redirected = os.path.abspath(str(ledger))
    if os.path.normcase(redirected) == os.path.normcase(production):
        tmp.cleanup()
        raise AssertionError(
            f"usage redirect collapsed onto the production ledger: "
            f"{redirected}")
    router = getattr(chat2api_module, "_chat2api_test_usage_router", None)
    if router is None:
        try:
            router = _UsageRouter(chat2api_module)
        except BaseException:
            tmp.cleanup()
            raise
        chat2api_module._chat2api_test_usage_router = router
        chat2api_module.record_usage = router
    scope = _UsageIsolation(router, tmp, ledger)
    router.register(scope)
    # Registered FIRST → runs LAST, but may retain the directory until the last
    # leased handler finishes. unittest continues after a teardown exception.
    test.addCleanup(scope.cleanup)
    patcher = mock.patch.object(chat2api_module, "USAGE_LOG", str(ledger))
    patcher.start()
    # Registered after the tmp release, before the server teardowns → the
    # stop runs AFTER every server drained (see the order contract above).
    test.addCleanup(patcher.stop)
    disabled_patcher = mock.patch.object(chat2api_module, "_usage_disabled",
                                         False)
    disabled_patcher.start()
    test.addCleanup(disabled_patcher.stop)
    return Path(tmp.name), ledger


def shutdown_drain_close(server, *, name, drain_timeout=DEFAULT_DRAIN_TIMEOUT_S,
                         require_drained=True):
    """Stop accepting, wake parked readers, wait for in-flight requests, close.

    Order matters: ``shutdown()`` ends the accept loop (no new handler can
    start), ``close_client_sockets()`` wakes handlers parked in the keep-alive
    readline, then the bounded ``drain()`` waits for genuinely in-flight
    requests — including their post-response ``record_usage`` tail — to end.

    With ``require_drained=True`` (the gateway: the ONLY server whose
    handlers call record_usage), a request that still refuses to end raises
    loudly, while per-connection isolation keeps late writes private regardless
    of the remaining unittest cleanups. Stub upstreams never write the ledger
    and legitimately stall mid-dispatch in cancellation scenarios, so they pass
    ``require_drained=False``. ``server_close`` runs in every case.
    """
    try:
        server.shutdown()
        server.close_client_sockets()
        left = server.drain(drain_timeout)
        if left and require_drained:
            raise AssertionError(
                f"{left} dispatched request(s) still in flight on {name} "
                f"after {drain_timeout}s; accepted handlers retain their "
                f"private ledger isolation until they exit")
    finally:
        server.server_close()


def install_tracked_server(test, server, *, name="server",
                           drain_timeout=DEFAULT_DRAIN_TIMEOUT_S,
                           require_drained=True):
    """Register the drain-ordered teardown of a running ``TrackedHTTPServer``.

    Teardown: ``shutdown()`` (stop accepting) → ``close_client_sockets()``
    (wake parked keep-alive readers) → bounded ``drain()`` (in-flight requests
    finish — including their post-response usage/summary writes) →
    ``server_close()``. A gateway request that refuses to end within
    ``drain_timeout`` fails the test loudly instead of silently racing the
    ledger redirect; stub upstreams pass ``require_drained=False``.

    Register AFTER ``install_usage_isolation`` so this runs BEFORE the patch
    release.
    """
    assert_loopback_ephemeral_server(server, name=name)

    def teardown():
        shutdown_drain_close(server, name=name, drain_timeout=drain_timeout,
                             require_drained=require_drained)

    test.addCleanup(teardown)
    return server
