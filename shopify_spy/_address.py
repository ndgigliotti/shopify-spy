"""Classify request destinations as public or non-public addresses (issue #96).

A store can point shopify-spy at loopback, private-network or cloud-metadata
addresses through redirects, image URLs and links. The session refuses those unless
the store itself is on a non-public address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Awaitable[list[IPAddress]]]

# Shorthand IPv4 (127.1, 2130706433, 0x7f.0.0.1): curl, the OS resolver or a proxy
# may turn these into addresses
_IPV4_SHORTHAND = re.compile(r"[0-9][0-9a-fx.]*", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def literal_ip(host: str) -> IPAddress | None:
    """The address a host names literally, including shorthand IPv4 forms."""
    host = host.strip("[]").removesuffix(".")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _IPV4_SHORTHAND.fullmatch(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def is_public_ip(ip: IPAddress) -> bool:
    # Unwrapping IPv4-mapped IPv6 makes 3.11 agree with 3.13+, and is_global calls
    # multicast global
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def is_local_name(host: str) -> bool:
    """``localhost`` or a ``*.localhost`` name, always loopback (RFC 6761)."""
    host = host.lower().removesuffix(".")
    return host == "localhost" or host.endswith(".localhost")


async def resolve(url: str) -> list[IPAddress]:
    """The addresses the URL's host resolves to; [] if the lookup fails."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 443)
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError, ValueError):
        return []
    addresses: list[IPAddress] = []
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if ip not in addresses:
            addresses.append(ip)
    return addresses


def non_public_reason(host: str, resolved: Sequence[IPAddress]) -> str | None:
    """Why the destination isn't public, or None if it is (any non-public answer counts)."""
    if not host:
        return None
    ip = literal_ip(host)
    if ip is not None:
        return None if is_public_ip(ip) else f"{host} is not a public address"
    if is_local_name(host):
        return f"{host} is not a public address"
    for ip in resolved:
        if not is_public_ip(ip):
            return f"{host} resolves to {ip}, not a public address"
    return None


def is_private_host(host: str, resolved: Sequence[IPAddress]) -> bool:
    """Whether a store's host is non-public; a mixed or empty answer is not.

    Stricter than ``non_public_reason``, so a store can't switch the check off by
    publishing one private record next to its public ones.
    """
    ip = literal_ip(host)
    if ip is not None:
        return not is_public_ip(ip)
    if is_local_name(host):
        return True
    return bool(resolved) and not any(is_public_ip(ip) for ip in resolved)
