# -*- coding: utf-8 -*-
"""CONSOLE_PUBLIC_ORIGIN resolution (D18-1).

The real deployment publishes the API at an explicit public origin
(``https://dacintapi.de5.net``).  This module owns the ONE parser for that
value: a strict https origin, never a URL with a path/query/userinfo/fragment,
and never a silently guessed host.  An absent key keeps the historical
Tailscale Funnel fallback; an invalid value is reported as an error instead of
being replaced by that fallback.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import public_url as public_url  # noqa: E402


CONFIGURED = "https://dacintapi.de5.net"
TS_URL = "https://fixture.tail-test.ts.net"


def configured(value):
    return mock.patch.dict(os.environ, {"CONSOLE_PUBLIC_ORIGIN": value})


class PublicOriginTests(unittest.TestCase):
    def test_absent_configuration_is_reported_as_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONSOLE_PUBLIC_ORIGIN", None)
            self.assertEqual(
                public_url.public_origin(),
                (None, "public_origin_unconfigured"))

    def test_empty_configuration_is_unconfigured_not_invalid(self):
        with configured(""):
            self.assertEqual(
                public_url.public_origin(),
                (None, "public_origin_unconfigured"))

    def test_configured_origin_is_accepted_and_normalized(self):
        for raw in (CONFIGURED, CONFIGURED + "/", CONFIGURED + ":443"):
            with self.subTest(raw=raw), configured(raw):
                self.assertEqual(public_url.public_origin(), (CONFIGURED, ""))
                self.assertEqual(public_url.origin_host(raw),
                                 "dacintapi.de5.net")

    def test_invalid_configuration_is_reported_and_never_guessed(self):
        rejected = (
            "http://dacintapi.de5.net",
            "https://dacintapi.de5.net/v1",
            "https://dacintapi.de5.net?x=1",
            "https://dacintapi.de5.net#frag",
            "https://user@dacintapi.de5.net",
            "https://user:pass@dacintapi.de5.net",
            "https://dacintapi.de5.net:8443",
            "https://dacintapi.de5.net:abc",
            "https://dacintapi.de5.net:",
            "https://-bad.de5.net",
            "https://bad-.de5.net",
            "https://bad..de5.net",
            "https://bad_host.de5.net",
            "https://" + "x" * 64 + ".de5.net",
            "https://dacintapi.de5.net\\evil",
            "https://dacintapi.de5.net\n",
            "https://测试.de5.net",
            "dacintapi.de5.net",
        )
        for raw in rejected:
            with self.subTest(raw=raw), configured(raw):
                self.assertEqual(public_url.public_origin(),
                                 (None, "public_origin_invalid"))
                self.assertIsNone(public_url.origin_host(raw))

    def test_non_string_configuration_is_invalid(self):
        for raw in (None, 443, {}, []):
            with self.subTest(raw=raw):
                environ = {} if raw is None else {"CONSOLE_PUBLIC_ORIGIN": raw}
                self.assertEqual(public_url.public_origin(environ),
                                 (None, "public_origin_unconfigured"
                                  if raw is None else "public_origin_invalid"))


if __name__ == "__main__":
    unittest.main()
