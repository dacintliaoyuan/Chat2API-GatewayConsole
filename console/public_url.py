"""Resolve the console's public share origin (D18-1).

The real deployment publishes the API through an explicit public origin
(``CONSOLE_PUBLIC_ORIGIN``, for example ``https://dacintapi.de5.net``).  This
module is the console-side façade for that value: it re-exports the ONE strict
parser/config reader that lives in ``console_public_probe`` (the probe worker runs
isolated with ``python -I`` and cannot import a sibling module) and adds the
``(url, source, reason)`` resolution the console status/read faces consume.

* absent/empty configuration -> ``public_origin_unconfigured``;
* invalid value -> ``public_origin_invalid`` and NO fallback (a typo must never
  be silently replaced by a different public host);
* valid value -> normalized ``https://<host>`` (an explicit ``:443`` is accepted
  and dropped).

The old Tailscale Funnel fallback is retired with the Windows local share chain
(#33): there is no second public host. ``resolve_configured`` reports the exact
reason when the key is absent/invalid instead of hard-coding a domain in the page.
"""


PUBLIC_ORIGIN_ENV = "CONSOLE_PUBLIC_ORIGIN"
UNCONFIGURED = "public_origin_unconfigured"
INVALID = "public_origin_invalid"


class ResolvedPublicUrl(tuple):
    """``(url, source)`` plus the additive ``reason`` for the console.

    It stays a tuple so callers can keep unpacking the two historical values;
    ``source`` is ``"configured"`` or None, and ``reason`` is empty when a URL
    exists. ``None`` means unavailable, never "no URL needed".
    """

    def __new__(cls, url, source, reason):
        result = super().__new__(cls, (url, source))
        result.url = url
        result.source = source
        result.reason = reason
        return result


def configured_host():
    """The configured public host, or None (absent/invalid both refuse)."""
    from console.public_probe import origin_host, public_origin

    origin, _reason = public_origin()
    if origin is None:
        return None
    return origin_host(origin)


def resolve_configured():
    """Resolve ONLY ``CONSOLE_PUBLIC_ORIGIN``; never read a second host.

    ``source`` is ``"configured"`` when the key is valid, None when it is
    absent or invalid, and ``reason`` names the exact condition. There is no
    Funnel fallback any more (#33).
    """
    from console.public_probe import public_origin

    origin, reason = public_origin()
    if origin is not None:
        return ResolvedPublicUrl(origin, "configured", "")
    if reason == INVALID:
        return ResolvedPublicUrl(None, None, INVALID)
    return ResolvedPublicUrl(None, None, UNCONFIGURED)


def origin_host(value):
    """The strict https-origin host, or None (see console_public_probe)."""
    from console.public_probe import origin_host as parse

    return parse(value)


def public_origin(environ=None):
    """``(normalized_origin, reason)`` from ``CONSOLE_PUBLIC_ORIGIN``."""
    from console.public_probe import public_origin as read

    return read(environ)
