"""Fake Open WebUI (UM) upstream used only for verification.

The implementation lives in :mod:`fake_um_server`; exports are resolved lazily so
``python -m tests.fake_um.fake_um_server`` does not import the module twice.
"""

from importlib import import_module

_EXPORTS = frozenset((
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "DEFAULT_PORT",
    "MODE_COMMENTS_ONLY",
    "MODE_EXPLICIT_5XX",
    "MODE_EXPLICIT_STREAM_ERROR",
    "MODE_HEADER",
    "MODE_INVALID_AUTH",
    "MODE_MISSING_TERMINAL",
    "MODE_NORMAL",
    "MODE_SILENT",
    "MODE_SLOW_FIRST_CHUNK",
    "MODES",
    "TOKEN_ENV",
    "FakeUM",
    "FakeUMConfig",
    "FakeUMError",
))

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(
            "module %r has no attribute %r" % (__name__, name))
    return getattr(import_module(".fake_um_server", __name__), name)
