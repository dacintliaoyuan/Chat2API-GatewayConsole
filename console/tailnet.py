"""Opt-in Tailscale Serve ingress for the loopback management console.

Serve overwrites identity/forwarding headers. Trust them only on loopback,
for the configured HTTPS origin, owner and explicitly selected device IPs.
Any forwarding metadata prevents fallback to the ordinary local trust path.
"""
from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit


def has_proxy_metadata(headers):
    return any(name.lower().startswith(("tailscale-", "x-forwarded-"))
               or name.lower() == "forwarded" for name in headers.keys())


def _ip(value):
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) \
        and address.ipv4_mapped else address


def _valid_ts_hostname(hostname):
    """Accept only a concrete MagicDNS name, never the bare ts.net zone."""
    labels = hostname.split(".")
    return (len(labels) >= 4 and labels[-2:] == ["ts", "net"]
            and all(labels) and hostname == hostname.strip())


def _tailnet_address(value):
    """Return one exact Tailscale device address; CIDR/network values fail."""
    address = _ip(value)
    if address.is_unspecified or address.is_multicast or address.is_loopback \
            or address.is_link_local:
        raise ValueError("device address must be a unicast host address")
    network = ipaddress.ip_network("100.64.0.0/10") \
        if address.version == 4 else ipaddress.ip_network("fd7a:115c:a1e0::/48")
    if address not in network \
            or address in (network.network_address, network.broadcast_address):
        raise ValueError("device address must belong to Tailscale")
    return address


@dataclass(frozen=True)
class TailnetPolicy:
    enabled: bool = False
    origin: str = ""
    host: str = ""
    login: str = ""
    peers: frozenset = field(default_factory=frozenset)
    error: str = ""

    @classmethod
    def from_document(cls, data):
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int \
                or data["schema_version"] != 1:
            raise ValueError("invalid tailnet policy schema")
        if data.get("enabled") is False:
            return cls()
        if data.get("enabled") is not True:
            raise ValueError("invalid tailnet enabled flag")
        origin = data.get("origin")
        if not isinstance(origin, str) or origin != origin.strip():
            raise ValueError("invalid tailnet origin")
        parsed = urlsplit(origin)
        # Read .port explicitly so malformed authority values fail closed.
        port = parsed.port
        if parsed.scheme != "https" or not parsed.hostname \
                or not _valid_ts_hostname(parsed.hostname) \
                or parsed.username is not None or parsed.password is not None \
                or parsed.path or parsed.query or parsed.fragment \
                or any(c in origin for c in "\r\n\t ,\\") \
                or (port is not None and not 1 <= port <= 65535):
            raise ValueError("tailnet origin must be an exact ts.net HTTPS origin")
        login = data.get("allowed_login")
        if not isinstance(login, str) or not login or login != login.strip() \
                or len(login) > 256 or any(c in login for c in "\r\n\t,"):
            raise ValueError("invalid tailnet owner")
        peers = data.get("allowed_peer_ips")
        if not isinstance(peers, list) or not 1 <= len(peers) <= 16:
            raise ValueError("tailnet device addresses are required")
        normalized = set()
        for value in peers:
            if not isinstance(value, str):
                raise ValueError("invalid tailnet device address")
            address = _tailnet_address(value)
            normalized.add(str(address))
        if len(normalized) != len(peers):
            raise ValueError("tailnet device addresses must be unique")
        return cls(True, origin.lower(), parsed.netloc.lower(), login.casefold(),
                   frozenset(normalized))

    def allows(self, headers, client_ip):
        if not self.enabled:
            return False
        try:
            if not _ip(client_ip).is_loopback:
                return False
            if headers.get_all("Tailscale-Funnel-Request", []):
                return False
            values = {}
            required = ("Host", "X-Forwarded-Host", "X-Forwarded-Proto",
                        "X-Forwarded-For", "Tailscale-User-Login")
            for name in required + ("Origin",):
                rows = headers.get_all(name, [])
                if len(rows) > 1 or (name in required and len(rows) != 1):
                    return False
                values[name] = rows[0].strip() if rows else None
            return (
                values["Host"].lower() == self.host
                and values["X-Forwarded-Host"].lower() == self.host
                and values["X-Forwarded-Proto"] == "https"
                and str(_ip(values["X-Forwarded-For"])) in self.peers
                and values["Tailscale-User-Login"].casefold() == self.login
                and values["Origin"] in (None, self.origin)
            )
        except (ValueError, TypeError, AttributeError):
            return False


def load_policy(path):
    """Missing/invalid policy leaves remote access closed and local access intact."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("policy too large")
        return TailnetPolicy.from_document(json.loads(raw))
    except FileNotFoundError:
        return TailnetPolicy()
    except (OSError, ValueError, TypeError):
        return TailnetPolicy(error="tailnet_policy_unreadable_or_invalid")
